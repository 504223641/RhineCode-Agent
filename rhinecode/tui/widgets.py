"""
TUI 组件模块，定义四个自定义 Textual Widget。

组件职责：
- HistoryView：对话历史展示区，支持流式逐块更新和滚动
- CommandPanel：斜杠命令提示面板，输入 "/" 时弹出，支持键盘上下选择
- InputBar：用户输入框，拦截回车事件并发出自定义消息
- StatusBar：底部状态栏，展示当前 Provider、模型和思考模式状态

渲染策略：
  HistoryView 采用 ScrollableContainer + 动态 Static 组件的方案，
  而非 RichLog。原因是 RichLog.write() 不支持行内追加（无 end="" 参数），
  无法实现流式逐字更新同一行内容；而 Static.update() 可以原地刷新，
  配合 call_from_thread 即可实现从 Worker 线程安全地驱动 UI 更新。
"""

from rich.console import Group as RichGroup
from rich.markdown import Markdown as RichMarkdown
from rich.text import Text as RichText
from textual.app import ComposeResult
from textual.widgets import Static, Input, OptionList
from textual.widgets.option_list import Option
from textual.containers import ScrollableContainer, Vertical
from textual.message import Message as TextualMessage


class HistoryView(ScrollableContainer):
    """
    对话历史展示区。

    内部维护一个 Vertical 容器（id="history-messages"），
    每条消息（用户、AI、系统提示、错误）均以独立的 Static 组件挂载其中。

    流式渲染时，begin_assistant_turn() / begin_thinking_turn() 返回新建的
    Static 组件引用，TUI 层的 Worker 在收到每个 StreamChunk 后调用
    update_widget() 原地更新该组件内容，实现逐字显示效果。
    """

    def compose(self) -> ComposeResult:
        # 内层 Vertical 作为消息列表容器，便于统一清空（remove_children）
        yield Vertical(id="history-messages")

    def _add_widget(self, markup: str) -> Static:
        """
        在历史区末尾添加一个新的 Static 消息组件并自动滚动到底部。

        :param markup: Rich markup 格式的显示内容
        :returns: 新建的 Static 组件引用（流式场景下供后续 update_widget 使用）
        """
        container = self.query_one("#history-messages", Vertical)
        widget = Static(markup, markup=True)
        container.mount(widget)
        # 每次新增消息后自动滚动到底部，保持用户视角始终看到最新内容
        self.scroll_end(animate=False)
        return widget

    def append_user(self, text: str) -> None:
        """追加一条用户消息，以青色粗体 "◈" 为前缀。"""
        self._add_widget(f"[bold #99FFFF]◈[/bold #99FFFF] {text}")

    def begin_assistant_turn(self) -> Static:
        """
        在历史区新增一个空的 AI 回复占位组件，返回其引用。

        调用方（Worker）后续通过 update_widget() 将流式文本不断写入此组件，
        实现逐字出现的视觉效果。

        :returns: 新建的 Static 组件，内容初始为带前缀的空字符串
        """
        return self._add_widget("[bold #CCFF99]Rhine[/bold #CCFF99] ")

    def begin_thinking_turn(self) -> Static:
        """
        在历史区新增一个 Extended Thinking 占位组件，返回其引用。

        思考内容以灰色斜体显示，与正文回复视觉区分。
        流式思考块到来时，Worker 通过 update_widget() 逐步追加内容。

        :returns: 新建的 Static 组件，内容初始为带思考图标的空字符串
        """
        return self._add_widget("[dim italic]💭 [/dim italic]")

    def update_widget(self, widget: Static, markup: str) -> None:
        """
        原地更新指定 Static 组件的显示内容并滚动到底部。

        此方法在 Worker 线程中通过 call_from_thread 调用，必须是线程安全的。
        Textual 的 call_from_thread 保证此方法在主线程的事件循环中执行。

        :param widget: 要更新的 Static 组件（begin_assistant_turn 等方法的返回值）
        :param markup: 新的 Rich markup 内容（完整替换，非追加）
        """
        widget.update(markup)
        self.scroll_end(animate=False)

    def update_ai_widget(self, widget: Static, content: str) -> None:
        """
        原地更新 AI 回复组件，将 content 作为 Markdown 渲染并滚动到底部。

        与 update_widget() 不同，此方法使用 Rich Markdown 渲染器，
        支持代码块语法高亮、标题、粗体、斜体、列表、表格等 Markdown 格式。
        "Rhine" 前缀以青绿色粗体单独渲染，正文内容整体作为 Markdown 文档渲染，
        两者通过 RichGroup 纵向组合后传给 Static.update()。

        此方法在 Worker 线程中通过 call_from_thread 调用，是线程安全的。

        :param widget: begin_assistant_turn() 返回的占位 Static 组件
        :param content: AI 回复的完整累积文本（原始 Markdown 格式，非 markup）
        """
        label = RichText("Rhine ", style="bold #CCFF99")
        body = RichMarkdown(content)
        # RichGroup 将前缀标签和 Markdown 正文纵向组合为单个 renderable
        widget.update(RichGroup(label, body))
        self.scroll_end(animate=False)

    def append_system(self, text: str) -> None:
        """追加一条系统提示消息，以灰色菱形 ◆ 为前缀（用于斜杠命令反馈）。"""
        self._add_widget(f"[dim]◆ {text}[/dim]")

    def append_error(self, text: str) -> None:
        """追加一条错误消息，以红色粗体显示（用于 API 错误或网络异常）。"""
        self._add_widget(f"[bold red]✗ 错误：{text}[/bold red]")

    def clear_all(self) -> None:
        """清空所有历史消息组件（对应 /clear 命令的 UI 侧操作）。"""
        self.query_one("#history-messages", Vertical).remove_children()


class CommandPanel(OptionList):
    """
    斜杠命令提示面板。

    继承自 Textual OptionList，内置 Up/Down 键盘导航和 Enter 选中能力。
    默认 display:none 不占布局空间；当用户输入以 "/" 开头时由 App 层调用
    show_for() 使其出现，并根据已输入内容进行前缀过滤。

    选中某条命令后，App 层监听 OptionList.OptionSelected 事件，
    将命令文本填入 InputBar 并自动提交。

    COMMANDS 是所有内置命令的注册表，新增命令只需在此列表追加即可，
    无需修改其他代码。
    """

    # 默认隐藏自身，避免依赖外部 App CSS 才能初始隐藏
    DEFAULT_CSS = "CommandPanel { display: none; }"

    # 命令注册表：(命令文本, 简要描述)
    COMMANDS: list[tuple[str, str]] = [
        ("/think", "循环切换思考模式：关闭 → 高效 → 最强（Anthropic/DeepSeek 支持）"),
        ("/clear", "清空当前对话历史"),
        ("/exit",  "退出 RhineCode"),
    ]

    def show_for(self, prefix: str) -> None:
        """
        根据用户已输入的前缀过滤命令并刷新 OptionList，有匹配则显示面板，无匹配则隐藏。

        每次调用会先清空现有选项再重新填充，避免残留上次的过滤结果。
        Option 的 id 设置为命令文本本身，方便 OptionSelected 事件中直接取用。

        :param prefix: 用户当前输入内容（如 "/"、"/th"、"/clear"）
        """
        matched = [(cmd, desc) for cmd, desc in self.COMMANDS if cmd.startswith(prefix)]
        self.clear_options()
        if not matched:
            self.display = False
            return
        for cmd, desc in matched:
            self.add_option(Option(f"{cmd}  [dim]{desc}[/dim]", id=cmd))
        self.display = True

    def hide(self) -> None:
        """隐藏面板并收回布局空间。"""
        self.display = False


class InputBar(Input):
    """
    用户输入框。

    继承自 Textual Input，拦截内置的 Submitted 事件，在内容非空时
    发出自定义的 InputSubmitted 消息，并自动清空输入框，为下次输入做准备。

    使用自定义消息而非直接调用方法，是为了保持 Widget 间的松耦合：
    InputBar 不需要持有 App 或其他组件的引用。
    """

    class InputSubmitted(TextualMessage):
        """
        用户提交输入时发出的自定义消息。

        :param text: 经过 strip 处理的用户输入文本（保证非空）
        """
        def __init__(self, text: str) -> None:
            super().__init__()
            self.text = text

    def on_input_submitted(self, event: Input.Submitted) -> None:
        """
        拦截 Textual 内置的回车提交事件。

        过滤空输入（纯空格），防止触发无意义的 API 请求。
        提交后立即清空输入框，恢复待输入状态。

        副作用：向消息总线 post InputSubmitted 消息；清空 self.value。
        """
        text = self.value.strip()
        if text:
            self.post_message(self.InputSubmitted(text))
            self.value = ""


class StatusBar(Static):
    """
    底部状态栏，实时展示当前会话的关键状态信息。

    显示格式：[protocol] model | 思考模式：开启/关闭
    每次 /think 命令执行后，App 层会调用 update_status() 刷新显示。
    """

    def update_status(self, provider: str, model: str, thinking_effort: str) -> None:
        """
        刷新状态栏显示内容。

        :param provider: Provider 协议名（anthropic / openai / deepseek）
        :param model: 当前使用的模型名称
        :param thinking_effort: 思考模式强度（off / high / max）
        """
        _LABEL = {"off": "关闭", "high": "高效", "max": "最强"}
        state = _LABEL.get(thinking_effort, thinking_effort)
        self.update(f" [{provider}] {model} | 思考模式：{state} ")
