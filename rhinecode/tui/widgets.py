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

import re
from time import monotonic
from typing import Optional

from rich.cells import cell_len
from rich.console import Group as RichGroup
from rich.highlighter import Highlighter
from rich.markdown import Markdown as RichMarkdown
from rich.segment import Segment
from rich.style import Style
from rich.text import Text as RichText
from textual.app import ComposeResult
from textual.binding import Binding
from textual.events import Key
from textual.widgets import Static, Input, OptionList
from textual.widgets.option_list import Option
from textual.containers import ScrollableContainer, Vertical
from textual.message import Message as TextualMessage

from rhinecode.agent.events import ClarifyOption
from rhinecode.commands.registry import CommandRegistry
from rhinecode.memory.session import SessionInfo
from rhinecode.tools.diff import MARK_ADD, MARK_CONTEXT, MARK_GAP, MARK_REMOVE


# 「把一段纯文本安全地嵌进 markup 字符串」的转义正则：匹配任意 `[` 及其前导反斜杠。
#
# ⚠️ **不要换回 `rich.markup.escape`**（这是一次真实崩溃的根因，见下）。
# 它的正则是 `(\\*)(\[[a-z#/@][^[]*?])` —— **必须找到闭合的 `]` 才认为这是标签**，
# 于是「括号被截断」的文本会被它整个放过：
#
#     summarize_args 把 `allowed_tools: [read_file, glob_files, ...]` 截成
#     `allowed_tools: [read_file, glo…`  → `]` 没了 → rich 不转义 → `[` 原样留下
#
# 而 **Textual 的 Content markup 比 Rich 严格**：Rich 把落单的 `[` 当普通文本放过，
# Textual 认定它是标签开头、去解析后面的内容当样式值，抛
# `MarkupError: Expected markup value`。更糟的是它抛在 `OptionList.get_content_height`
# 里——也就是**布局阶段的主线程**，不在 `show_for` 的调用栈上，没有任何 try/except
# 兜得住，Textual 直接拆掉整个 app，程序退出。
#
# 触发条件很窄（某个值的前 30 字符里有 `[`、配对的 `]` 在 30 字符之外），
# 所以它表现为「偶尔莫名其妙退出」，极难归因。
#
# 结论：这些位置嵌进去的都是**纯文本**（工具参数、路径、错误消息、决策原因），
# 里面的每个 `[` 都是字面量、没有一个是标签，因此无条件全转义才是正确语义。
_ESCAPE_BRACKET = re.compile(r"(\\*)(\[)")


def escape(text: object) -> str:
    """
    把任意文本转义成「可安全嵌入 markup 字符串」的形式。

    与 `rich.markup.escape` 的差别：**无条件转义每一个 `[`**，不要求它看起来像
    一个完整标签。理由见上方 `_ESCAPE_BRACKET` 的注释——「像标签才转义」遇上
    被截断的括号会漏掉，而漏掉的后果是整个应用崩溃退出。

    行为上它是 `rich.markup.escape` 的**严格超集**：后者会转义的，这里全都会转义。

    :param text: 任意对象，非字符串会先 `str()`
    :returns: 转义后的字符串

    副作用：无（纯函数）。
    """
    markup = str(text)
    # 前导反斜杠要一并加倍，否则 `\[` 这种「本就转义过的输入」会被二次转义成
    # 「字面反斜杠 + 标签」，语义反而错了。这一步与 rich 的做法一致。
    markup = _ESCAPE_BRACKET.sub(lambda m: f"{m.group(1)}{m.group(1)}\\{m.group(2)}", markup)
    # 结尾落单的反斜杠会把后续拼接进来的字符转义掉，补一个使其成为字面反斜杠。
    if markup.endswith("\\") and not markup.endswith("\\\\"):
        markup += "\\"
    return markup


def summarize_args(arguments: "dict | None", max_len: int = 60) -> str:
    """
    把工具调用参数压缩成单行摘要，用于工具行与确认框展示。

    取每个参数 "键=值" 拼接，值过长则截断，整体再做长度上限截断，
    目的是让用户一眼看清工具将操作什么（如 path=...），而非展示完整内容。

    :param arguments: 解析后的参数字典；None（解析失败）时返回占位提示
    :param max_len: 摘要最大长度，超出截断
    :returns: 单行参数摘要字符串
    """
    if arguments is None:
        return "<参数解析失败>"
    if not isinstance(arguments, dict):
        return "<参数格式错误>"
    parts = []
    for key, value in arguments.items():
        text = str(value).replace("\n", " ")
        if len(text) > 30:
            text = text[:30] + "…"
        parts.append(f"{key}={text}")
    summary = ", ".join(parts)
    if len(summary) > max_len:
        summary = summary[:max_len] + "…"
    return escape(summary)


# 回放时工具结果摘要的最大展示长度（取首行再截断，避免长结果撑爆历史区）
_REPLAY_RESULT_MAX_CHARS = 80


def _first_line_truncated(text: str, max_chars: int = _REPLAY_RESULT_MAX_CHARS) -> str:
    """取文本首行并按长度截断，用于回放场景的工具结果摘要（与运行时摘要口径一致）。"""
    line = (text or "").strip().split("\n", 1)[0]
    if len(line) > max_chars:
        line = line[:max_chars] + "…"
    return line


def build_replay_items(messages) -> "list[tuple]":
    """
    把一段历史消息（provider.base.Message 列表）转换为回放渲染项序列（纯函数，可单测）。

    /resume 载入会话后，TUI 需要把整段历史画回聊天区。本函数负责「消息 → 渲染项」的
    纯逻辑转换，不做任何 UI 操作，由 HistoryView.render_history 消费。

    执行流程：
    1. 第一趟收集 {tool_call_id: 工具结果文本} 映射——载入历史已经过 SessionStore 的
       _drop_unpaired 严格配对清理，但仍用 dict.get 防御性兜底（缺结果时展示占位）；
    2. 第二趟按原始顺序产出渲染项：
       - role="user"      → ("user", 显示文本)：优先取非空 display_content
                            （c10 双内容——/init 等提示词命令回放时只显示原命令），
                            缺失或为空回退 content
       - role="assistant" → content 非空先产出 ("assistant", content)；
                            随后每个 tool_call 产出 ("tool", tool_call, 结果首行截断)。
                            content 为空且无 tool_calls 的消息整体跳过（不渲染空 Rhine 行）
       - role="tool"      → 跳过（结果已并入所属 assistant 的 tool 项）
       - 其它 role        → 防御性跳过

    已知降级：会话存档不含思考（thinking）内容，回放不出现 💭 块。

    :param messages: 历史消息列表（元素为 provider.base.Message）
    :returns: 渲染项列表，元素为 ("user", str) / ("assistant", str) / ("tool", ToolCall, str)
    """
    # 第一趟：tool_call_id → 结果文本（后到覆盖先到，正常历史中 id 唯一）
    results: dict = {}
    for msg in messages:
        if msg.role == "tool" and msg.tool_call_id:
            results[msg.tool_call_id] = msg.content
    # 第二趟：按序产出渲染项
    items: list[tuple] = []
    for msg in messages:
        if msg.role == "user":
            # 双内容（c10 F26）：display_content 非空时优先展示（提示词命令原文），
            # 旧消息 / 普通消息（None 或空串）回退完整 content。
            display = getattr(msg, "display_content", None)
            items.append(("user", display if display else msg.content))
        elif msg.role == "assistant":
            if msg.content:
                items.append(("assistant", msg.content))
            for tc in msg.tool_calls or []:
                summary = _first_line_truncated(results.get(tc.id, "（无结果）"))
                items.append(("tool", tc, summary))
        # role="tool" 与未知 role：跳过
    return items


# diff 块配色：删除/新增行用「背景色」高亮整行（不改前景字色，保持默认终端文字色），
# 上下文行、行号、概要统一用灰色前景。
# Rich 的 "on <color>" 表示设置背景色；不写前景即沿用终端默认字色。
_DIFF_REMOVE_BG = "on #5f1f1f"   # 删除行：暗红底
_DIFF_ADD_BG = "on #1f4f1f"      # 新增行：暗绿底
_DIFF_DIM = "#808080"            # 上下文/行号/概要：灰色前景
# 行号列宽度（右对齐），保证不同行的内容左缘对齐。
_DIFF_NUM_WIDTH = 6


def _count_phrase(added: int, removed: int) -> str:
    """
    把增删行数拼成自然语言概要，如 "Added 4 lines, removed 1 line"。

    规则：有增才说 Added、有删才说 removed，单数用 line、复数用 lines；
    句首词首字母大写（只删时即 "Removed 1 line"）；都为 0 时返回 "No changes"。

    :param added: 新增行数
    :param removed: 删除行数
    :returns: 概要短语
    """
    parts = []
    if added:
        parts.append(f"Added {added} line{'s' if added != 1 else ''}")
    if removed:
        parts.append(f"removed {removed} line{'s' if removed != 1 else ''}")
    if not parts:
        return "No changes"
    phrase = ", ".join(parts)
    return phrase[0].upper() + phrase[1:]


class _DiffBlock:
    """
    diff 块的自定义 Rich 渲染对象，供工具行展示。

    布局（缩进体现「隶属于上方状态行」的层级，类似树形分支）：
        ⎿  Added 4 lines, removed 1 line
            279     上下文行（灰）
            282 -   删除行（暗红底，整行高亮）
            283 +   新增行（暗绿底，整行高亮）
            ⋮               （hunk 之间的省略）

    为什么用自定义渲染对象而非现成的 RichText：
      终端里背景色只覆盖字符本身，文本短于行宽时右侧不会着色，背景条会「断在文末」。
      要做到「整行背景都是背景色」，必须把高亮行补足空格到**当前可用宽度**。
      可用宽度只有在真正渲染时才知道（且会随终端 resize 变化），因此实现 __rich_console__，
      从渲染参数 options.max_width 取得实时宽度再补空格——这样高亮条能铺满整行并自适应宽度。

    行号取值：上下文/新增显示新文件行号，删除显示旧文件行号；
    与 DiffView.to_text() 的取值规则一致，保证人看到的与回灌给模型的对得上。
    """

    def __init__(self, view) -> None:
        """:param view: tools.diff.DiffView"""
        self._view = view

    def __rich_console__(self, console, options):
        """
        Rich 渲染协议：产出本块的 Segment 序列。

        每行先组装出文本与样式，并标记是否需要「整行背景」。需要背景的行（删除/新增）
        按 options.max_width 补足空格，使背景铺满整行；其余行（概要/上下文/省略）原样输出。
        行间以换行分隔，末行不补换行，避免产生多余空行。
        """
        view = self._view
        width = options.max_width
        dim = Style.parse(_DIFF_DIM)
        remove_bg = Style.parse(_DIFF_REMOVE_BG)
        add_bg = Style.parse(_DIFF_ADD_BG)

        # 先收集每行的 (文本, 样式, 是否整行铺背景)
        rows_out: list[tuple[str, Style, bool]] = []
        # 概要分支行（灰色，无背景）
        rows_out.append((f"  ⎿  {_count_phrase(view.added, view.removed)}", dim, False))
        for row in view.rows:
            if row.marker == MARK_GAP:
                # hunk 间省略：用居中省略号表示中间有未展示的未改动内容
                rows_out.append(("        ⋮", dim, False))
                continue
            no = row.new_no if row.new_no is not None else row.old_no
            num = f"    {no:>{_DIFF_NUM_WIDTH}} "  # 4 格缩进 + 右对齐行号
            if row.marker == MARK_REMOVE:
                rows_out.append((f"{num}- {row.text}", remove_bg, True))
            elif row.marker == MARK_ADD:
                rows_out.append((f"{num}+ {row.text}", add_bg, True))
            else:  # MARK_CONTEXT：无背景，灰色前景
                rows_out.append((f"{num}  {row.text}", dim, False))
        if view.truncated:
            rows_out.append(("        …（diff 已截断）", dim, False))

        last = len(rows_out) - 1
        for idx, (text, style, fill) in enumerate(rows_out):
            if fill:
                # 补空格到整行宽度（cell_len 正确计算中文/全角宽度），使背景铺满整行
                pad = max(0, width - cell_len(text))
                yield Segment(text + " " * pad, style)
            else:
                yield Segment(text, style)
            if idx != last:
                yield Segment.line()


def render_diff_block(view) -> "_DiffBlock":
    """
    构造 diff 块的可渲染对象（见 _DiffBlock）。保留函数形式，调用方无需感知具体类型。

    :param view: tools.diff.DiffView
    :returns: 一个 Rich 可渲染对象，删除/新增行整行背景高亮、自适应宽度
    """
    return _DiffBlock(view)


# 工具名 → 标题展示标签。把面向模型的内部名（snake_case）换成更易读的动词式标签，
# 与改文件工具的 "Update"/"Write" 风格统一。这里是「展示层」的映射：
# - 真实工具名仍是各工具的 name（API 用、注册中心用），此表只决定 UI 标题怎么写；
# - 未登记的工具回退到原始名，保证新增工具即便忘了登记也不会显示异常。
_TOOL_LABELS = {
    "read_file": "Read",
    "glob_files": "Glob",
    "grep_content": "Grep",
    "run_command": "Run",
    "edit_file": "Update",
    "write_file": "Write",
}


class ToolCallWidget(Static):
    """
    单个工具调用的展示行，自管理执行计时。

    标题统一用展示标签（_TOOL_LABELS，如 Read/Grep/Update）而非内部工具名（snake_case）。

    三种视觉状态：
    - 执行中：橘色，显示 "● 标签(参数摘要) 执行中… Ns"，N 由主线程定时器每秒刷新
    - 成功：绿色 "● 标签(参数摘要) 完成 (Ns)" + 下方 "⎿ 结果摘要"
    - 失败：红色 "● 标签(参数摘要) 失败 (Ns)" + 下方 "⎿ 错误摘要"

    计时不依赖 Worker 线程：on_mount 中用 set_interval 在主线程每秒触发 _tick，
    因此即使 Worker 正阻塞在工具执行/并发等待中，耗时显示仍持续更新（spec F14/N3）。
    """

    # 执行中橘色 / 成功绿色 / 失败红色 / "⎿ 摘要" 分支行灰色（次级信息）
    _COLOR_RUNNING = "#FFA500"
    _COLOR_OK = "#5FD75F"
    _COLOR_FAIL = "#FF5F5F"
    _COLOR_BRANCH = "#808080"

    def __init__(self, tool_call) -> None:
        """
        :param tool_call: provider.base.ToolCall，提供工具名与参数用于展示
        """
        super().__init__(markup=True)
        self._name = tool_call.name
        # 标题展示标签：内部名映射为易读动词式（未登记则回退原名）
        self._label = escape(_TOOL_LABELS.get(self._name, self._name))
        self._args_summary = summarize_args(tool_call.arguments)
        self._start = 0.0
        self._timer = None  # set_interval 返回的定时器，finish 时停止

    def on_mount(self) -> None:
        """挂载后记录起始时刻、立即渲染 0s，并启动每秒刷新的主线程定时器。"""
        self._start = monotonic()
        self._render_running()
        # 每秒刷新一次耗时显示；定时器运行在主线程事件循环，不占用 Worker
        self._timer = self.set_interval(1.0, self._render_running)

    def _elapsed(self) -> int:
        """返回从开始执行到现在的整数秒数。"""
        return int(monotonic() - self._start)

    def _render_running(self) -> None:
        """以橘色渲染执行中状态，显示当前已耗时。"""
        self.update(
            f"[{self._COLOR_RUNNING}]● {self._label}({self._args_summary}) 执行中… {self._elapsed()}s[/]"
        )

    def finish(self, ok: bool, summary: str, diff=None) -> None:
        """
        结束计时并切换到成功/失败终态。

        由 TUI 的 Worker 通过 call_from_thread 在主线程调用，线程安全。

        :param ok: 工具是否成功（决定绿/红与图标）
        :param summary: 结果摘要文本（已由调用方取首行/截断）
        :param diff: 可选的 tools.diff.DiffView。改文件类工具会带上它，
                     此时在状态行下方追加渲染一个彩色 diff 块；其它工具留空。

        副作用：停止计时定时器，原地更新本行内容。
        """
        if self._timer is not None:
            self._timer.stop()
        elapsed = self._elapsed()
        color = self._COLOR_OK if ok else self._COLOR_FAIL
        result = "完成" if ok else "失败"
        # 统一为两行式：第一行 "● 标题 完成/失败 (Ns)"，第二行起为 "⎿ ..." 分支。
        if diff is not None and diff.rows:
            # 改文件类工具（成功）：标题用 diff 自带的 op/path（比工具名+参数摘要更贴近改动语义），
            # 分支由 render_diff_block 产出（首行 "⎿ Added.../removed..." 概要 + 彩色 diff 行）。
            header = f"[{color}]● {escape(str(diff.op))}({escape(str(diff.path))}) {result} ({elapsed}s)[/]"
            self.update(RichGroup(RichText.from_markup(header), render_diff_block(diff)))
        else:
            # 其它工具（或改文件但无差异）：标题用 "标签(参数摘要)"，分支展示单行结果摘要。
            header = f"[{color}]● {self._label}({self._args_summary}) {result} ({elapsed}s)[/]"
            branch = RichText("  ⎿  ", style=self._COLOR_BRANCH)
            branch.append(summary, style=self._COLOR_BRANCH)
            self.update(RichGroup(RichText.from_markup(header), branch))


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
        self._add_widget(f"[bold #99FFFF]◈[/bold #99FFFF] {escape(text)}")

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

    def add_tool_widget(self, tool_call) -> "ToolCallWidget":
        """
        在历史区末尾挂载一个工具调用展示行（ToolCallWidget），返回其引用。

        Worker 线程在收到 tool_start 时通过 call_from_thread 调用本方法创建工具行
        （此时即开始橘色计时）；收到 tool_result 时再对返回的引用调用 finish() 定色。

        :param tool_call: provider.base.ToolCall，用于初始化展示内容
        :returns: 新建的 ToolCallWidget，供后续 finish() 更新
        """
        container = self.query_one("#history-messages", Vertical)
        widget = ToolCallWidget(tool_call)
        container.mount(widget)
        self.scroll_end(animate=False)
        return widget

    def append_system(self, text: str) -> None:
        """追加一条系统提示消息，以灰色菱形 ◆ 为前缀（用于斜杠命令反馈）。"""
        self._add_widget(f"[dim]◆ {escape(text)}[/dim]")

    def append_error(self, text: str) -> None:
        """追加一条错误消息，以红色粗体显示（用于 API 错误或网络异常）。"""
        self._add_widget(f"[bold red]● 错误：{escape(text)}[/bold red]")

    def clear_all(self) -> None:
        """清空所有历史消息组件（对应 /clear 命令的 UI 侧操作）。"""
        self.query_one("#history-messages", Vertical).remove_children()

    # ------------------------------------------------------------------ #
    # 会话历史回放（c9 /resume 交互化）
    # ------------------------------------------------------------------ #

    @staticmethod
    def _build_assistant_widget(content: str) -> Static:
        """
        构造一条「完整 AI 回复」的静态组件（回放场景，非流式）。

        渲染形态与流式的 update_ai_widget 一致：青绿色 "Rhine" 前缀 + Markdown 正文，
        区别只是内容一次到位、无需占位-更新两步。
        """
        label = RichText("Rhine ", style="bold #CCFF99")
        return Static(RichGroup(label, RichMarkdown(content)))

    @staticmethod
    def _build_tool_record_widget(tool_call, result_summary: str) -> Static:
        """
        构造一条「历史工具调用记录」的简化静态行（回放场景）。

        两行式：绿色 "● 标签(参数摘要)" + 灰色 "⎿ 结果首行摘要"。
        刻意**不复用 ToolCallWidget**：它的 on_mount 会启动每秒计时器并重绘「执行中」
        状态——回放时 finish() 与挂载的时序无保证，终态会被 on_mount 覆盖且定时器
        永不停止（泄漏）。历史记录也没有耗时数据，简化行语义更贴切。

        :param tool_call: provider.base.ToolCall（提供工具名与参数）
        :param result_summary: 已截断的结果摘要（build_replay_items 产出）
        """
        label = escape(_TOOL_LABELS.get(tool_call.name, tool_call.name))
        # 标题行走 markup（label 与 summarize_args 的产出都已 escape，安全）；
        # 分支行用 RichText 纯文本拼接——结果摘要来自工具输出原文，可能含 "["，
        # 纯文本渲染天然免转义（与 ToolCallWidget.finish 的 branch 同一做法）。
        header = f"[{ToolCallWidget._COLOR_OK}]● {label}({summarize_args(tool_call.arguments)})[/]"
        branch = RichText(f"  ⎿  {result_summary}", style=ToolCallWidget._COLOR_BRANCH)
        return Static(RichGroup(RichText.from_markup(header), branch))

    def render_history(self, messages) -> None:
        """
        清空聊天区并整体回放一段历史消息（/resume 载入、--continue 启动恢复）。

        必须在主线程调用（Worker 侧经 call_from_thread 转入），清空与重画在同一次
        调用内完成，对用户呈现为原子切换。性能考虑：先把全部消息构造成组件列表，
        一次 mount(*widgets) 批量挂载、末尾只滚动一次——逐条挂载+滚动会触发 N 次布局，
        长会话下明显卡顿。

        :param messages: 恢复出来的历史消息列表（provider.base.Message）

        副作用：移除聊天区现有全部组件并挂载回放组件。
        """
        container = self.query_one("#history-messages", Vertical)
        container.remove_children()
        widgets: list[Static] = []
        for item in build_replay_items(messages):
            kind = item[0]
            if kind == "user":
                widgets.append(
                    Static(f"[bold #99FFFF]◈[/bold #99FFFF] {escape(item[1])}", markup=True)
                )
            elif kind == "assistant":
                widgets.append(self._build_assistant_widget(item[1]))
            elif kind == "tool":
                widgets.append(self._build_tool_record_widget(item[1], item[2]))
        if widgets:
            container.mount(*widgets)
        self.scroll_end(animate=False)


class CommandHighlighter(Highlighter):
    """
    输入框命令字段高亮器（c10 T36，spec F24）。

    通过 Textual `Input` 的公开 `highlighter` 扩展点安装（不覆写私有渲染实现）。
    规则：
    - 只检查输入开头到第一个空白之前的字段；
    - 仅当完整字段能被注册表解析（规范名或别名、大小写不敏感）时，为该字段
      施加命令强调样式；参数与后续空白保持普通样式；
    - 未完整命中的前缀（如 "/res"）不高亮，避免让用户误以为命令有效；
    - 正文内的斜杠（不在输入开头）不高亮；
    - 不改变用户输入的显示文本（大小写形式可命中，显示仍保留原文）。
    """

    # 命令字段的强调样式：与主题青色一致的加粗，与普通输入文字明显区分。
    COMMAND_STYLE = "bold #7AEEFF"

    def __init__(self, registry: CommandRegistry) -> None:
        self._registry = registry

    def highlight(self, text: RichText) -> None:
        """
        rich Highlighter 协议：原地为 text 施加样式（不改变字符内容）。

        :param text: 输入框当前内容的 rich Text 对象
        """
        plain = text.plain
        if not plain.startswith("/"):
            return
        # 命令字段 = 开头到第一个空白之前；无空白时整段都是命令字段
        end = next((i for i, ch in enumerate(plain) if ch.isspace()), len(plain))
        token = plain[:end]
        # 只有完整命中注册表（规范名或别名）才着色（spec F24）
        if self._registry.resolve(token) is not None:
            text.stylize(self.COMMAND_STYLE, 0, end)


class CommandPanel(OptionList):
    """
    斜杠命令提示面板（c10 起从注册表动态取候选，不再维护静态 COMMANDS 列表）。

    继承自 Textual OptionList，内置 Up/Down 键盘导航和 Enter 选中能力。
    默认 display:none 不占布局空间；当用户输入以 "/" 开头且光标位于命令字段时
    由 App 层调用 show_for() 使其出现，候选来自 CommandRegistry.complete()：
    - 只匹配规范名（大小写不敏感），别名不参与候选——别名仍可直接输入执行、
      /help 仍列出（2026-07 变更，见 docs/c10/checklist.md C36）；
    - 隐藏命令不出现（spec F20）；
    - 候选顺序稳定：按命令注册顺序排列。

    选中某条候选后，App 层监听 OptionList.OptionSelected 事件，
    将候选文本填入 InputBar 并自动提交（或经提交入口直接分发）。
    """

    # 默认隐藏自身，避免依赖外部 App CSS 才能初始隐藏
    DEFAULT_CSS = "CommandPanel { display: none; }"

    def __init__(self, registry: CommandRegistry, **kwargs) -> None:
        """
        :param registry: 命令注册表（与分发器、输入高亮器共享同一实例，spec F3）
        """
        super().__init__(**kwargs)
        self._registry = registry

    def show_for(self, prefix: str) -> None:
        """
        用注册表候选刷新 OptionList：有匹配则显示面板，零候选则隐藏。

        每次调用先清空现有选项再重建，避免残留上次的过滤结果；
        重建后把首个候选设为高亮，供「菜单可见时按 Enter 执行当前高亮项」
        （spec F23）。Option 的 id 即候选文本（规范名或别名）。

        :param prefix: 用户当前输入的命令字段前缀（如 "/"、"/co"、"/ctx"）
        """
        items = self._registry.complete(prefix)
        self.clear_options()
        if not items:
            self.display = False
            return
        for item in items:
            self.add_option(
                Option(f"{item.value}  [dim]{escape(item.description)}[/dim]", id=item.value)
            )
        self.display = True
        # 首个候选默认高亮：Enter 即执行（与确认面板的顺手体验一致）
        self.highlighted = 0

    def hide(self) -> None:
        """隐藏面板并收回布局空间。"""
        self.display = False


class InputBar(Input):
    """
    用户输入框。

    继承自 Textual Input，拦截内置的 Submitted 事件，在内容非空时
    发出自定义的 InputSubmitted 消息，并自动清空输入框，为下次输入做准备。

    c10 增强：
    - 构造时接收 CommandRegistry，经 Textual `Input` 公开的 `highlighter`
      参数安装 CommandHighlighter——完整命中的命令字段着色，参数保持普通样式；
    - 值以 "/" 开头且光标仍处于命令字段时拦截 Tab，post CommandCompletionRequested
      交给 App 做单候选补全/多候选菜单；已进入参数区或普通文本时不拦截，
      保留 Textual 的正常 Tab 行为（焦点切换）。

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

    class CommandCompletionRequested(TextualMessage):
        """
        用户在命令字段按 Tab 请求补全时发出（c10 T39）。

        :param prefix: 当前输入的命令字段前缀（含开头 "/"，如 "/co"）
        """
        def __init__(self, prefix: str) -> None:
            super().__init__()
            self.prefix = prefix

    def __init__(self, registry: "Optional[CommandRegistry]" = None, **kwargs) -> None:
        """
        :param registry: 命令注册表；非 None 时安装命令字段高亮器（公开扩展点，
                         不覆写 Textual 私有渲染方法）
        :param kwargs: 透传给 Textual Input（placeholder 等）
        """
        if registry is not None:
            kwargs.setdefault("highlighter", CommandHighlighter(registry))
        super().__init__(**kwargs)

    def _command_field_end(self) -> int:
        """返回命令字段的结束位置（开头到第一个空白之前；无空白即整段长度）。"""
        return next((i for i, ch in enumerate(self.value) if ch.isspace()), len(self.value))

    def on_key(self, event: Key) -> None:
        """
        拦截命令字段内的 Tab：post 补全请求并阻止默认焦点切换（c10 T39）。

        拦截条件：值以 "/" 开头，且光标位置不超过命令字段末尾（仍在命令字段内）。
        参数区（第一个空白之后）或普通文本的 Tab 不拦截，Textual 默认行为保留。
        """
        if event.key != "tab":
            return
        if not self.value.startswith("/"):
            return
        end = self._command_field_end()
        if self.cursor_position > end:
            return  # 已进入参数区：不做命令补全
        event.stop()
        event.prevent_default()
        self.post_message(self.CommandCompletionRequested(self.value[:end]))

    def apply_completion(self, value: str, trailing_space: bool = False) -> None:
        """
        用候选文本替换命令字段并把光标移到命令字段末尾（供 App 单候选补全调用）。

        只替换第一个空白之前的命令字段，参数区内容原样保留（spec：/resume 12
        按 Tab 不改动 12）。

        :param value: 替换后的完整命令（如 "/compact"）
        :param trailing_space: True 且原输入无参数时在末尾补一个空格
                               （候选命令有参数提示时，方便用户继续输入参数）
        """
        end = self._command_field_end()
        rest = self.value[end:]
        if rest:
            self.value = value + rest
            self.cursor_position = len(value)
        else:
            suffix = " " if trailing_space else ""
            self.value = value + suffix
            self.cursor_position = len(self.value)

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


# 模式标记样式（c10 F29/F30）：
# - [DEFAULT] 用 dim（中性、低强调），深浅色主题下都可读；
# - [PLAN] 用加粗的醒目青色（与主题青一致但更饱和），两种主题下均与 DEFAULT 明显区分。
# N9 可访问性：模式不只靠颜色表达——文字本身就是 [DEFAULT]/[PLAN]，忽略样式也能区分。
_MODE_DEFAULT_MARKUP = "[dim]\\[DEFAULT][/dim]"
_MODE_PLAN_MARKUP = "[bold #00D7D7]\\[PLAN][/bold #00D7D7]"


def compose_status_text(
    provider: str,
    model: str,
    thinking_effort: str,
    plan_mode: bool = False,
    permission_mode: "str | None" = None,
    mcp_status: "str | None" = None,
    context_status: "str | None" = None,
    context_warn: bool = False,
    skill_status: "str | None" = None,
) -> str:
    """
    组装状态栏的 Content markup 文本（纯函数，c10 抽出便于单测）。

    显示格式：\\[protocol] model | 思考模式：X | [DEFAULT]/[PLAN] | 权限模式：X | MCP：… | 上下文：…
    c10 起旧的「计划模式：开/关」文字替换为醒目的模式标记（spec F29–F30）。

    :returns: 可交给 Static(markup=True) 渲染的 markup 字符串
    """
    _LABEL = {"off": "关闭", "high": "高效", "max": "最强"}
    state = _LABEL.get(thinking_effort, thinking_effort)
    # Static(markup=True) 走 Textual 的 Content markup：`[xxx]` 会被当成样式标签解析。
    # 这里 `[provider]`、`[DEFAULT]`、`[PLAN]` 的方括号是想当「字面量」显示的，必须转义
    # 开口的 `[`（写成 `\[`），否则会被解析成无效样式标签而整段消失（项目已知坑）。
    mode_seg = _MODE_PLAN_MARKUP if plan_mode else _MODE_DEFAULT_MARKUP
    text = (
        f" \\[{escape(str(provider))}] {escape(str(model))} | "
        f"思考模式：{escape(str(state))} | {mode_seg}"
    )
    if permission_mode is not None:
        _PERM = {"strict": "严格", "default": "默认", "permissive": "放行"}
        plabel = _PERM.get(permission_mode, permission_mode)
        seg = f"权限模式：{escape(str(plabel))}"
        # 放行档影响安全（灰色地带默认放行），用橘色（与确认面板同色）醒目提示。
        if permission_mode == "permissive":
            seg = f"[#FFA500]{seg}[/#FFA500]"
        text += f" | {seg}"
    # MCP 段（c7）：仅在启用且有 Server 时展示；文本可能含字面 `[`，统一 escape 兜底。
    if mcp_status is not None:
        text += f" | {escape(str(mcp_status))}"
    # 上下文段（c8）：仅在有 ContextManager 时展示。接近上限/熔断时橘色高亮——
    # 与 permissive、确认面板同色，语义都是「需要用户留意」。注意先 escape 文本再包裹
    # 颜色标签（颜色标签本身不能被转义，否则会被当字面量显示）。
    if context_status is not None:
        seg = escape(str(context_status))
        if context_warn:
            seg = f"[#FFA500]{seg}[/#FFA500]"
        text += f" | {seg}"
    # Skill 段（c11）：仅在有已激活 Skill 时展示（形如 `Skill:2`），
    # 与 MCP 段「None 即隐藏」同构——没用 Skill 的用户状态栏与 c10 完全一致。
    # 注意该文本刻意不含方括号（见 SkillManager.status_segment 的说明），
    # 这里仍走 escape 兜底，与其它段口径一致。
    if skill_status is not None:
        text += f" | {escape(str(skill_status))}"
    return text + " "


class StatusBar(Static):
    """
    底部状态栏，实时展示当前会话的关键状态信息。

    显示格式：[protocol] model | 思考模式：X | [DEFAULT]/[PLAN] | 权限模式：X | MCP：… | 上下文：…
    模式命令执行后（以及每轮流式结束时），App 层会调用 update_status() 刷新显示；
    c10 起刷新由命令处理函数经控制器显式触发，不再依赖命令字符串白名单。
    """

    def update_status(
        self,
        provider: str,
        model: str,
        thinking_effort: str,
        plan_mode: bool = False,
        permission_mode: "str | None" = None,
        mcp_status: "str | None" = None,
        context_status: "str | None" = None,
        context_warn: bool = False,
        skill_status: "str | None" = None,
    ) -> None:
        """
        刷新状态栏显示内容（文本组装见 compose_status_text 纯函数）。

        :param provider: Provider 协议名（anthropic / openai / deepseek）
        :param model: 当前使用的模型名称
        :param thinking_effort: 思考模式强度（off / high / max）
        :param plan_mode: 是否处于 Plan Mode（c10 起显示 [DEFAULT] / [PLAN] 标记）
        :param permission_mode: 权限模式取值（"strict"/"default"/"permissive"）；c6 新增。
                                为 None（工具不可用的 Provider）时不展示该段，避免误导。
        :param mcp_status: MCP 连接状态摘要；为 None（未启用 MCP）时不展示该段。
        :param context_status: 上下文用量摘要；为 None（无 ContextManager）时不展示该段。
        :param context_warn: 上下文是否接近上限或已熔断；为真时该段橘色高亮预警。
        :param skill_status: 已激活 Skill 摘要（如 "Skill:2"）；为 None 时不展示该段（c11）。
        """
        self.update(
            compose_status_text(
                provider,
                model,
                thinking_effort,
                plan_mode,
                permission_mode,
                mcp_status,
                context_status,
                context_warn,
                skill_status,
            )
        )


class ConfirmPanel(OptionList):
    """
    工具执行前的内联确认面板（取代旧的模态弹窗 ConfirmScreen）。

    交互体验与斜杠命令面板 CommandPanel 一致：出现在输入框上方，用方向键在
    「执行 / 取消」间选择，回车确认，Esc 取消，不遮挡历史区。

    用于决策管线判定为 ASK（交人工确认）的工具调用：顶部以橘色表头展示工具名、关键参数
    摘要与「为何需要确认」的原因（仅展示、不可选），下方四个可选项对应四态放行（c6 F6）。

    结果如何回传：面板本身不持有协调层状态。用户选择某项时由 OptionList 原生发出
    OptionList.OptionSelected（App 据 option.id 解析为 ConfirmDecision）；按 Esc 时发出本类的
    Cancelled 消息（App 视为拒绝）。App 再唤醒被阻塞的 Worker 线程（见 RhineApp._confirm_tool）。

    四个可选项的 option.id 约定（c6）：
    - "yes"           → 仅放行本次（ConfirmDecision.ALLOW）
    - "yes_session"   → 本会话放行（ConfirmDecision.ALLOW_SESSION，登记会话级 allow 规则）
    - "yes_permanent" → 永久放行（ConfirmDecision.ALLOW_PERMANENT，写入本地级配置）
    - "no"            → 拒绝（ConfirmDecision.DENY）

    设计取舍：确认期间 App 会把焦点临时移到本面板，从而直接复用 OptionList 原生的
    上/下/回车 选择能力（输入框为空时回车不会触发自定义提交消息，移焦到面板最稳健）。
    """

    # 默认隐藏自身，避免依赖外部 App CSS 才能初始隐藏
    DEFAULT_CSS = "ConfirmPanel { display: none; }"

    # 表头与两个可选项在 OptionList 中的索引（表头 disabled 不可选）
    _HEADER_INDEX = 0
    _YES_INDEX = 1  # 「执行」：默认高亮项，回车即执行

    class Cancelled(TextualMessage):
        """用户按 Esc 取消确认时发出，由 App 视为拒绝执行。"""
        pass

    BINDINGS = [
        # Esc 取消：发出 Cancelled 消息交给 App 处理（等价于选择「取消」）
        Binding("escape", "cancel", "取消", show=False),
    ]

    # 判定层 → 面板上显示的中文名。与 trace/reader.py 的 _LAYER_NAMES 是同一批取值，
    # 但**两处刻意不合一**：让 trace（只依赖标准库的叶子包）反向依赖 permission
    # 会破坏它的架构不变量。一致性由 tests 里遍历 Layer 的断言钉住。
    _LAYER_LABELS = {
        "blacklist": "①危险命令黑名单",
        "sandbox": "②路径沙箱",
        "network": "②′网络边界",
        "rule": "③可配置规则",
        "mode": "④权限模式",
    }

    def _url_detail_lines(self, tool_call, decision) -> list[str]:
        """
        为 URL 类请求生成补充展示行：完整地址、主机名、命中层。

        :param tool_call: 本次工具调用（从中取未经截断的原始地址）
        :param decision: DecisionResult（提供 host 与 layer）
        :returns: 已转义、可直接进 markup 的行文本列表

        ⚠ **完整 URL 进 markup 前一律用本模块的 `escape`，绝不要
        `from rich.markup import escape`。** URL 天然含 `[`
        （IPv6 字面量 `http://[::1]/`、含 `[` 的查询串），而 rich 那版只转义
        「看起来像完整标签」的 `[...]`，落单的 `[` 会被整个放过，
        然后在 Textual 的布局阶段抛 MarkupError——那是没有任何 try/except
        兜得住、会直接拆掉整个 app 的那一类。
        """
        args = tool_call.arguments if isinstance(tool_call.arguments, dict) else {}
        raw_url = str(args.get("url") or "")
        lines: list[str] = []
        if raw_url:
            # **不截断**：这一行的全部价值就在于让用户看到完整地址。
            lines.append(f"   [dim]完整地址：[/dim]{escape(raw_url)}")
        host = getattr(decision, "host", "") or ""
        if host:
            lines.append(f"   [dim]主机名：[/dim]{escape(host)}")
        layer = getattr(decision, "layer", None)
        layer_value = getattr(layer, "value", layer)
        if layer_value:
            label = self._LAYER_LABELS.get(str(layer_value), str(layer_value))
            lines.append(f"   [dim]判定来自：[/dim]{escape(label)}")
        return lines

    def show_for(self, tool_call, tool, decision=None) -> None:
        """
        为一次工具调用填充并显示确认面板（c6 四态放行）。

        每次调用先清空旧选项再重建，避免残留上一次确认的内容。
        表头（disabled）展示工具名、参数摘要与「为何需要确认」的原因供用户判断；
        其后是四个可选项（本次/本会话/永久/拒绝），默认高亮「本次放行」（_YES_INDEX）。

        :param tool_call: provider.base.ToolCall，提供工具名与参数
        :param tool: tools.base.Tool（暂用于潜在扩展，如展示描述）
        :param decision: 决策管线给出的 DecisionResult；其 reason 展示在表头说明缘由。
                         为 None 时仅展示工具信息（向后兼容）。

        副作用：修改 OptionList 选项并使面板可见。
        """
        args_summary = summarize_args(tool_call.arguments, max_len=200)
        # 原因文本：把决策原因拼到表头，让用户明白这次为什么停下来问（如默认模式无规则命中）。
        reason = f"  [dim]· {escape(decision.reason)}[/dim]" if decision is not None else ""
        safe_name = escape(str(tool_call.name))
        self.clear_options()
        # 橘色表头：醒目提示这是有副作用的操作；disabled 使其不可被选中/跳过导航
        self.add_option(
            Option(
                f"[#FFA500]⚠ 确认执行：{safe_name}({args_summary})[/#FFA500]{reason}",
                disabled=True,
            )
        )
        # URL 类专用补充行（web_fetch 扩展 F9）。
        #
        # **为什么需要它**：上面那行走 summarize_args，它把每个参数值截到 30 字符，
        # 一条 `https://docs.example.com/reference/v2?token=abc` 只会显示成
        # `url=https://docs.example.com/re…`。而用户正是靠面板上那个地址来决定
        # 放不放行的——地址被截断意味着攻击者只要把恶意部分放在第 31 个字符之后，
        # 人在回路这层就形同虚设。
        #
        # 判据用 `decision is not None and ...`：decision 是可选参数，
        # 既有代码一律先判非空再取属性。这里是**主线程布局路径**，
        # AttributeError 属于「没有任何 try/except 兜得住」的那一类。
        if decision is not None and getattr(decision, "kind", "") == "url":
            for line in self._url_detail_lines(tool_call, decision):
                self.add_option(Option(line, disabled=True))
        self.add_option(Option("✅ 本次放行  [dim]仅执行本次[/dim]", id="yes"))
        self.add_option(Option("🟢 本会话放行  [dim]本会话内相同调用不再询问[/dim]", id="yes_session"))
        self.add_option(Option("💾 永久放行  [dim]写入本地配置，重启仍生效[/dim]", id="yes_permanent"))
        self.add_option(Option("❌ 拒绝  [dim]拒绝并让模型据此调整[/dim]", id="no"))
        self.display = True
        # 默认高亮「本次放行」，回车即执行（与 / 命令面板一致的顺手体验）
        self.highlighted = self._YES_INDEX

    def show_prompt(self, title: str, yes_label: str, no_label: str) -> None:
        """
        以通用「是/否」提示复用本面板（c4 用于 Plan Mode 的「是否开始执行」审批）。

        与 show_for 不同：不展示工具名/参数，而是展示一段自定义标题，下面给出两个选项
        （id 固定为 "yes"/"no"，无第三项）。App 据 option.id=="yes" 判定是否批准。

        :param title: 表头提示文本（单行，过长请由调用方先截断）
        :param yes_label: 「是」选项的展示文本
        :param no_label: 「否」选项的展示文本

        副作用：修改 OptionList 选项并使面板可见。
        """
        self.clear_options()
        self.add_option(Option(f"[#FFA500]{escape(title)}[/#FFA500]", disabled=True))
        self.add_option(Option(yes_label, id="yes"))
        self.add_option(Option(no_label, id="no"))
        self.display = True
        self.highlighted = self._YES_INDEX

    def hide(self) -> None:
        """隐藏面板并收回布局空间。"""
        self.display = False

    def action_cancel(self) -> None:
        """Esc 绑定：发出 Cancelled 消息，由 App 解释为拒绝执行。"""
        self.post_message(self.Cancelled())


class ClarifyPanel(OptionList):
    """
    Plan Mode 需求澄清面板（spec F12）。

    模型在规划阶段通过 ask_user 工具发起提问时，App 用本面板把问题与候选项呈现给用户：
    出现在输入框上方，方向键上下选择，回车确认，Esc 取消，不遮挡历史区（与确认面板同款交互）。

    每个候选项展示「概述 + 详细描述」，但只有概述可被选中：
    - 实现方式：每个候选项渲染为「可选的概述行」+ 紧随其后的「disabled 详情行」。
      OptionList 的上下导航会自动跳过 disabled 项，从而做到「导航只在概述之间移动」，
      同时详情仍然可见，帮助用户判断（对应需求：上下移动只在概述间移动、每个选择下有详细描述）。
    - 最推荐的候选项排在第一位（由模型保证），概述文本自身已含推荐信息，不再额外加标记。

    结果如何回传：用户选中某概述行时由 OptionList 原生发出 OptionList.OptionSelected
    （option.id 为该候选项在 options 中的下标字符串，App 据此取回所选概述）；按 Esc 发出
    本类的 Cancelled 消息（App 视为用户取消澄清）。App 再唤醒被阻塞的 Worker（见 RhineApp._clarify）。
    """

    # 默认隐藏自身，避免依赖外部 App CSS 才能初始隐藏
    DEFAULT_CSS = "ClarifyPanel { display: none; }"

    class Cancelled(TextualMessage):
        """用户按 Esc 取消澄清时发出，由 App 视为用户取消。"""
        pass

    BINDINGS = [
        # Esc 取消：发出 Cancelled 消息交给 App 处理
        Binding("escape", "cancel", "取消", show=False),
    ]

    def show_for(self, question: str, options: list[ClarifyOption]) -> None:
        """
        为一次澄清提问填充并显示面板。

        先清空旧选项再重建，避免残留上一次提问内容。结构为：
        - 一个 disabled 表头（展示问题 question，不可选）
        - 对每个候选项：一个可选概述行（id=下标字符串）+ 一个 disabled 详情行（若有 detail）

        :param question: 模型要澄清的问题
        :param options: 候选项列表；第一个为最推荐项（概述文本自身已含推荐信息）

        副作用：修改 OptionList 选项并使面板可见。
        """
        self.clear_options()
        # 青色表头：展示问题本身；disabled 使其不可被选中、导航跳过
        self.add_option(Option(f"[#7AEEFF]❓ {escape(question)}[/#7AEEFF]", disabled=True))

        first_selectable: int | None = None
        for idx, opt in enumerate(options):
            # 概述行：可选，id 为该候选项下标（字符串）
            # 注：推荐顺序由模型保证（第一位即最推荐），概述文本本身已带推荐信息，
            #     故不再额外加「⭐ 推荐」前缀，避免重复提示。
            option_index = self.option_count  # 加入前的位置即本概述行的索引
            self.add_option(Option(escape(opt.summary), id=str(idx)))
            if first_selectable is None:
                first_selectable = option_index
            # 详情行：disabled，仅展示，导航会跳过
            # 不缩进，使详情与上方概述行左边缘对齐
            if opt.detail:
                self.add_option(Option(f"[dim]{escape(opt.detail)}[/dim]", disabled=True))

        self.display = True
        # 默认高亮第一个可选概述行
        if first_selectable is not None:
            self.highlighted = first_selectable

    def hide(self) -> None:
        """隐藏面板并收回布局空间。"""
        self.display = False

    def action_cancel(self) -> None:
        """Esc 绑定：发出 Cancelled 消息，由 App 解释为用户取消澄清。"""
        self.post_message(self.Cancelled())


class SessionPanel(OptionList):
    """
    /resume 的交互式会话选择面板（c9 交互化）。

    用户输入 /resume（无参）后，App 用本面板列出全部历史会话：出现在输入框上方，
    方向键上下选择、回车载入、Esc 退出，不遮挡历史区（与确认/澄清面板同款交互）。
    展示期间 App 会把焦点移到本面板，直接复用 OptionList 原生的上/下/回车导航。

    条目呈现规则：
    - 每个会话一行紧凑显示：编号、锁标记、会话 ID、（当前）标记、时间、消息数、标题；
      单行可让全部条目一屏尽收（两行式在会话多时必然滚动、扫读效率减半）。
    - 被其它实例锁定的会话（🔒）与当前会话（（当前））设为 disabled——OptionList
      导航自动跳过，从源头避免「选中后才报错」的挫败感。注意这只是 UX 优化：
      列表展示与实际载入之间存在锁竞态窗口，真正的锁检查仍在 resume_into → attach，
      竞态失败会以 NOTICE 反馈且不清屏。

    结果如何回传：用户选中某会话时由 OptionList 原生发出 OptionList.OptionSelected，
    option.id 即完整 session_id（App 据此直接以 "/resume <session_id>" 走带参载入路径，
    绕过编号解析）；按 Esc 发出本类的 Cancelled 消息（App 关闭面板、界面原样保留）。
    """

    # 默认隐藏自身，避免依赖外部 App CSS 才能初始隐藏
    DEFAULT_CSS = "SessionPanel { display: none; }"

    class Cancelled(TextualMessage):
        """用户按 Esc 退出会话选择时发出，由 App 关闭面板（不做任何载入）。"""
        pass

    BINDINGS = [
        # Esc 退出：发出 Cancelled 消息交给 App 处理（界面原样保留）
        Binding("escape", "cancel", "取消", show=False),
    ]

    def show_for(self, infos: list[SessionInfo], current_id: str) -> None:
        """
        填充会话列表并显示面板。

        先清空旧选项再重建，避免残留上次列表。结构为：
        - 一个 disabled 青色表头（操作提示，不可选）
        - 每个会话一行：可恢复项 id=session_id 可选；锁定项/当前会话 disabled

        :param infos: 全部会话信息（MemoryManager.list_resume_sessions 产出，已按时间倒序）
        :param current_id: 当前会话 ID（用于标注「（当前）」并禁用）

        副作用：修改 OptionList 选项并使面板可见、重置高亮到第一个可选项。
        """
        self.clear_options()
        self.add_option(
            Option(
                "[#7AEEFF]📂 选择要恢复的会话（↑↓ 选择，回车载入，Esc 取消）[/#7AEEFF]",
                disabled=True,
            )
        )
        first_selectable: "int | None" = None
        for i, info in enumerate(infos, start=1):
            when = info.last_time.strftime("%Y-%m-%d %H:%M") if info.last_time else "未知时间"
            is_current = info.session_id == current_id
            locked = info.locked and not is_current
            # session_id / title 都可能含 "["（title 来自用户消息原文），必须 escape，
            # 否则被 Textual markup 当标签吞掉（项目已知坑，见 CLAUDE.md 成对维护点备忘）
            line = (
                f"{i}. {'🔒 ' if locked else ''}{escape(info.session_id)}"
                f"{'（当前）' if is_current else ''} · {when} · "
                f"{info.message_count} 条 · [dim]{escape(info.title)}[/dim]"
            )
            option_index = self.option_count  # 加入前的位置即本行索引
            # 锁定/当前会话 disabled：导航自动跳过；可恢复项 id 携带完整 session_id
            self.add_option(
                Option(line, id=None if (locked or is_current) else info.session_id,
                       disabled=locked or is_current)
            )
            if first_selectable is None and not (locked or is_current):
                first_selectable = option_index
        self.display = True
        # 默认高亮第一个可选会话（最近的可恢复会话，回车即载入）
        if first_selectable is not None:
            self.highlighted = first_selectable

    def hide(self) -> None:
        """隐藏面板并收回布局空间。"""
        self.display = False

    def action_cancel(self) -> None:
        """Esc 绑定：发出 Cancelled 消息，由 App 关闭面板。"""
        self.post_message(self.Cancelled())
