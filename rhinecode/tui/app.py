"""
Textual App 主类模块。

RhineApp 是 TUI 层的核心，负责：
1. 组合四个面板（HistoryView / CommandPanel / InputBar / StatusBar）
2. 监听用户输入事件，调用 ConversationManager 处理
3. 将流式 StreamChunk 通过 Worker + call_from_thread 安全地渲染到 UI
4. 响应斜杠命令结果（状态栏刷新、历史区清空、程序退出）
5. 管理命令提示面板的显示/隐藏和键盘导航（焦点始终保持在 InputBar）

线程模型：
  Textual 的事件循环运行在主线程，UI 操作必须在主线程执行。
  流式 API 请求通过 run_worker(thread=True) 在独立线程中运行，
  Worker 通过 call_from_thread() 将每个 StreamChunk 的渲染操作
  调度回主线程，保证线程安全。
"""

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.events import Key
from textual.widgets import Static, Input

from rhinecode.config import Config
from rhinecode.conversation import ConversationManager
from rhinecode.provider.base import StreamChunk
from rhinecode.tui.widgets import HistoryView, InputBar, StatusBar, CommandPanel


class RhineApp(App):
    """
    RhineCode 的 Textual 应用主类。

    布局（从上到下）：
    - HistoryView：占据除底部区域外的全部高度（height: 1fr），可滚动
    - CommandPanel：斜杠命令提示面板，默认隐藏，输入 "/" 时弹出
    - InputBar：固定 3 行高（含边框），用户在此输入
    - StatusBar：固定 1 行，右对齐展示当前 Provider / 模型 / 思考状态
    """

    CSS = """
    Screen {
        layout: vertical;
    }
    HistoryView {
        height: 1fr;
        border: solid #7AEEFF 60%;
        padding: 0 1;
    }
    /* 内容容器随消息增长，超出 HistoryView 高度时触发父容器滚动 */
    HistoryView > Vertical {
        height: auto;
    }
    CommandPanel {
        height: auto;
        max-height: 6;
        display: none;
        /* 清除 OptionList 自带全方向边框，统一用顶部分隔线与主题色对齐 */
        border: none;
        border-top: tall #7AEEFF 60%;
        padding: 0 1;
        background: $boost;
    }
    InputBar {
        height: 3;
        border: solid #7AEEFF 60%;
        margin-top: 0;
    }
    StatusBar {
        height: 1;
        background: #7AEEFF 20%;
        color: $text;
        text-align: right;
    }
    """

    BINDINGS = [
        # Ctrl+C 绑定到内置 quit action，确保用户可以随时退出
        Binding("ctrl+c", "quit", "退出", show=False),
    ]

    def __init__(self, manager: ConversationManager, config: Config):
        """
        :param manager: 已初始化的对话管理器，持有 Provider 和对话历史
        :param config: 配置对象，用于在状态栏展示 Provider 和模型信息
        """
        super().__init__()
        self._manager = manager
        self._config = config

    def compose(self) -> ComposeResult:
        """按从上到下的顺序挂载四个面板。"""
        yield HistoryView()
        yield CommandPanel()
        yield InputBar(placeholder="输入消息，/ 查看命令，Ctrl+C 退出")
        yield StatusBar()

    def on_mount(self) -> None:
        """
        应用挂载完成后的初始化操作。

        在此时机执行而非 __init__，是因为此时 DOM 已完全构建，
        query_one() 可以安全地查找到子组件。
        """
        self._refresh_status()
        # 启动后将焦点置于输入框，用户可以直接开始输入
        self.query_one(InputBar).focus()

    def _refresh_status(self) -> None:
        """刷新状态栏，反映当前 Provider、模型和思考模式的最新状态。"""
        self.query_one(StatusBar).update_status(
            self._config.protocol,
            self._config.model,
            self._manager.thinking_effort,
        )

    def on_input_changed(self, event: Input.Changed) -> None:
        """
        监听输入框内容变化，控制命令提示面板的显示与过滤。

        当输入以 "/" 开头时调用 CommandPanel.show_for() 过滤并显示面板；
        其他情况隐藏面板。每次内容变化都重新过滤，实现实时匹配效果。

        :param event: Textual Input 的 Changed 事件，含当前输入框完整内容
        """
        panel = self.query_one(CommandPanel)
        if event.value.startswith("/"):
            panel.show_for(event.value)
        else:
            panel.hide()

    def on_key(self, event: Key) -> None:
        """
        拦截命令面板可见时的特殊按键，焦点始终保持在 InputBar。

        设计原则：焦点永远不离开 InputBar。
        - Up/Down：仅移动面板的高亮光标，不转移焦点，用户可继续输入字符过滤命令
        - Escape：隐藏面板，恢复正常输入状态
        - Enter 在面板有高亮时的处理见 on_input_bar_input_submitted（在那里读取高亮项）

        :param event: Textual Key 事件，此时事件已经过 InputBar 处理（bubble 阶段）
        """
        panel = self.query_one(CommandPanel)

        if not panel.display:
            return  # 面板不可见时不拦截任何按键，保持原有行为

        if event.key == "up":
            event.stop()
            panel.action_cursor_up()    # 移动高亮，不转移焦点

        elif event.key == "down":
            event.stop()
            panel.action_cursor_down()  # 移动高亮，不转移焦点

        elif event.key == "escape":
            event.stop()
            panel.hide()                # 焦点本就在 InputBar，无需重新 focus()

    def on_input_bar_input_submitted(self, event: InputBar.InputSubmitted) -> None:
        """
        处理用户提交输入的事件（InputBar 发出 InputSubmitted 时触发）。

        执行步骤：
        1. 若命令面板可见且有高亮条目，用高亮命令替换输入框文本（Enter 确认逻辑）
        2. 隐藏命令提示面板
        3. 将最终文本显示到历史区
        4. 调用 ConversationManager.handle_input() 分发处理
        5. 根据返回值类型决定后续行为：
           - str：斜杠命令反馈，直接显示；/clear 还需清空历史区；/think 还需刷新状态栏（三态循环）
           - Iterator：流式生成器，启动 Worker 在后台消费并渲染

        :param event: 包含用户输入文本的事件对象（可能被高亮命令覆盖）
        """
        panel = self.query_one(CommandPanel)

        # 若面板有高亮条目，Enter 确认该命令，忽略输入框中的前缀文本（如 "/th"）
        if panel.display and panel.highlighted is not None:
            text = panel.get_option_at_index(panel.highlighted).id
        else:
            text = event.text

        panel.hide()
        history_view = self.query_one(HistoryView)

        # 先将用户消息显示到历史区，给用户即时反馈
        history_view.append_user(text)

        try:
            result = self._manager.handle_input(text)
        except SystemExit:
            # /exit 命令：ConversationManager 抛出 SystemExit，此处捕获并正常退出
            self.exit()
            return

        if isinstance(result, str):
            # 斜杠命令反馈：/clear 需要先清空历史区的 UI，再显示确认提示
            if text == "/clear":
                history_view.clear_all()
            history_view.append_system(result)
            # /think 切换了 thinking_enabled 状态，需要同步刷新状态栏
            if text == "/think":
                self._refresh_status()
        else:
            # 普通消息：在独立线程中消费流式生成器，避免阻塞 UI 主线程
            # exclusive=False 允许多个 Worker 并发（极少出现，但保持健壮性）
            self.run_worker(
                lambda: self._do_stream(result),
                thread=True,
                exclusive=False,
            )

    def _do_stream(self, gen) -> None:
        """
        在 Worker 线程中消费流式生成器，将每个 StreamChunk 渲染到 UI。

        此方法运行在独立线程，不能直接操作 UI 组件。
        所有 UI 操作通过 call_from_thread() 调度到主线程执行，
        call_from_thread 会阻塞当前 Worker 线程直到主线程执行完毕并返回结果。

        渲染策略：
        - thinking chunk：首次到来时创建思考占位组件，后续增量更新同一组件
        - text chunk：首次到来时创建 AI 回复占位组件，后续增量更新同一组件
        - error chunk：在历史区追加红色错误提示
        - done chunk：流正常结束，无需额外操作（history 已由 ConversationManager 更新）

        :param gen: ConversationManager._stream() 返回的 StreamChunk 生成器
        """
        history_view = self.query_one(HistoryView)

        # 思考内容和正文各自维护独立的占位组件和内容缓冲区
        thinking_widget: Static | None = None
        thinking_chunks: list[str] = []
        response_widget: Static | None = None
        response_chunks: list[str] = []

        for chunk in gen:
            if chunk.type == "thinking":
                # 首个思考块到来时，在主线程创建占位组件并获取其引用
                if thinking_widget is None:
                    thinking_widget = self.call_from_thread(history_view.begin_thinking_turn)
                thinking_chunks.append(chunk.content)
                # 每次以完整内容更新组件（而非追加），保证渲染正确
                self.call_from_thread(
                    history_view.update_widget,
                    thinking_widget,
                    f"[dim italic]💭 {''.join(thinking_chunks)}[/dim italic]",
                )

            elif chunk.type == "text":
                # 首个文本块到来时，创建 AI 回复占位组件
                if response_widget is None:
                    response_widget = self.call_from_thread(history_view.begin_assistant_turn)
                response_chunks.append(chunk.content)
                self.call_from_thread(
                    history_view.update_widget,
                    response_widget,
                    f"[bold green]AI:[/bold green] {''.join(response_chunks)}",
                )

            elif chunk.type == "error":
                # API 错误或网络异常，以红色显示，程序继续运行
                self.call_from_thread(history_view.append_error, chunk.content)

            elif chunk.type == "done":
                # 流正常结束，ConversationManager 已在 _stream() 中将回复追加到 history
                pass
