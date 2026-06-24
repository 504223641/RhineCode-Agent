"""
Textual App 主类模块。

RhineApp 是 TUI 层的核心，负责：
1. 组合各面板（HistoryView / CommandPanel / ConfirmPanel / ClarifyPanel / InputBar / StatusBar）
2. 监听用户输入事件，调用 ConversationManager 处理
3. 将 Agent 循环产出的 AgentEvent 通过 Worker + call_from_thread 安全地渲染到 UI
4. 响应斜杠命令结果（状态栏刷新、历史区清空、程序退出、Plan Mode 切换）
5. 提供三类用户交互回调（有副作用工具确认 / Plan Mode 需求澄清 / 计划执行审批）
6. 提供运行中取消（按 Esc）

线程模型：
  Textual 的事件循环运行在主线程，UI 操作必须在主线程执行。
  Agent 循环通过 run_worker(thread=True) 在独立线程中运行（消费 AgentEvent 生成器），
  Worker 通过 call_from_thread() 把每个事件的渲染操作调度回主线程，保证线程安全。
  需要用户决定的交互（确认/澄清/审批）由循环在 Worker 线程调用回调，回调内部用
  call_from_thread 在主线程弹面板、用 threading.Event 阻塞 Worker 等待用户选择（不死锁，N2）。
"""

import threading

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.events import Key
from textual.widgets import Static, Input

from rhinecode.config import Config
from rhinecode.conversation import ConversationManager
from rhinecode.agent.events import AgentEventType, StopReason, ConfirmDecision
from rhinecode.tui.widgets import (
    HistoryView, InputBar, StatusBar, CommandPanel, ConfirmPanel, ClarifyPanel,
)


class RhineApp(App):
    """
    RhineCode 的 Textual 应用主类。

    布局（从上到下）：
    - HistoryView：占据除底部区域外的全部高度（height: 1fr），可滚动
    - CommandPanel：斜杠命令提示面板，默认隐藏，输入 "/" 时弹出
    - ConfirmPanel：有副作用工具执行前确认 / 计划执行审批的内联面板，默认隐藏
    - ClarifyPanel：Plan Mode 需求澄清的内联面板，默认隐藏
    - InputBar：固定 3 行高（含边框），用户在此输入
    - StatusBar：固定 1 行，展示 Provider / 模型 / 思考模式 / 计划模式
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
    /* 工具确认 / 计划审批面板：橘色分隔线警示「需要用户决定的操作」 */
    ConfirmPanel {
        height: auto;
        max-height: 8;
        display: none;
        border: none;
        border-top: tall #FFA500 80%;
        padding: 0 1;
        background: $boost;
    }
    /* Plan Mode 需求澄清面板：青色分隔线，与确认区分 */
    ClarifyPanel {
        height: auto;
        max-height: 12;
        display: none;
        border: none;
        border-top: tall #7AEEFF 80%;
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
        :param manager: 已初始化的对话管理器，持有 Provider / Agent 和对话历史
        :param config: 配置对象，用于在状态栏展示 Provider 和模型信息
        """
        super().__init__()
        self._manager = manager
        self._config = config
        # 待决的用户交互（确认/澄清/审批）：None 表示当前无交互在进行；
        # 进行中时为 {"event": threading.Event, "result": Any, "kind": str}，
        # 由回调在 Worker 线程创建并阻塞、由主线程的选择/取消处理写入结果并唤醒。
        self._pending_interaction: dict | None = None
        # 当前澄清面板的候选项列表（用于把所选下标还原为概述文本）
        self._clarify_options: list = []
        # 单轮运行锁：避免多个 Worker 同时修改同一份 conversation history。
        self._stream_active = False

    def compose(self) -> ComposeResult:
        """按从上到下的顺序挂载各面板。"""
        yield HistoryView()
        yield CommandPanel()
        yield ConfirmPanel()
        yield ClarifyPanel()
        yield InputBar(placeholder="输入消息，/ 查看命令，运行中按 Esc 取消，Ctrl+C 退出")
        yield StatusBar()

    def on_mount(self) -> None:
        """
        应用挂载完成后的初始化操作。

        在此时机执行而非 __init__，是因为此时 DOM 已完全构建，query_one() 可安全查找子组件。
        把三类交互回调注入协调层：协调层在循环中需要用户决定时调用它们，而不感知 Textual 细节。
        """
        self._refresh_status()
        self._manager.confirm_callback = self._confirm_tool
        self._manager.clarify_callback = self._clarify
        self._manager.approve_plan_callback = self._approve_plan
        # 启动后将焦点置于输入框，用户可以直接开始输入
        self.query_one(InputBar).focus()

    def _refresh_status(self) -> None:
        """刷新状态栏，反映当前 Provider、模型、思考模式、计划模式。"""
        self.query_one(StatusBar).update_status(
            self._config.protocol,
            self._config.model,
            self._manager.thinking_effort,
            self._manager.plan_mode,
        )

    # ------------------------------------------------------------------ #
    # 输入与命令面板
    # ------------------------------------------------------------------ #
    def on_input_changed(self, event: Input.Changed) -> None:
        """
        监听输入框内容变化，控制命令提示面板的显示与过滤。

        输入以 "/" 开头时显示并过滤命令面板，否则隐藏。交互进行中或流式运行中不处理，
        避免与确认/澄清面板或运行状态交错。
        """
        if self._pending_interaction is not None or self._stream_active:
            return
        panel = self.query_one(CommandPanel)
        if event.value.startswith("/"):
            panel.show_for(event.value)
        else:
            panel.hide()

    def on_key(self, event: Key) -> None:
        """
        处理特殊按键：运行中取消、命令面板导航。

        优先级：
        1. 有交互待决（确认/澄清/审批）→ 交给被聚焦的面板自身的 Esc 绑定处理，这里不拦截。
        2. 流式运行中 → Esc 触发取消当前 Agent 循环（spec F9）。
        3. 命令面板可见 → Up/Down 移动高亮、Esc 隐藏（焦点始终保持在 InputBar）。
        """
        # 1. 交互待决：让面板自己处理（它们各有 escape 绑定），不在此拦截
        if self._pending_interaction is not None:
            return

        # 2. 运行中按 Esc 取消循环
        if self._stream_active:
            if event.key == "escape":
                event.stop()
                self._manager.request_cancel()
            return

        # 3. 命令面板导航
        panel = self.query_one(CommandPanel)
        if not panel.display:
            return
        if event.key == "up":
            event.stop()
            panel.action_cursor_up()
        elif event.key == "down":
            event.stop()
            panel.action_cursor_down()
        elif event.key == "escape":
            event.stop()
            panel.hide()

    def on_input_bar_input_submitted(self, event: InputBar.InputSubmitted) -> None:
        """
        处理用户提交输入。

        步骤：
        1. 若命令面板有高亮条目，用高亮命令替换文本（Enter 确认逻辑）。
        2. 隐藏命令面板，显示用户消息到历史区。
        3. 调用 ConversationManager.handle_input() 分发：
           - str：斜杠命令反馈（/clear 清屏、/think 与 /plan 刷新状态栏）。
           - Iterator：Agent 事件流，启动 Worker 在后台消费并渲染。
        """
        # 交互进行中 / 流式运行中：忽略普通输入提交
        if self._pending_interaction is not None or self._stream_active:
            return
        panel = self.query_one(CommandPanel)

        if panel.display and panel.highlighted is not None:
            text = panel.get_option_at_index(panel.highlighted).id
        else:
            text = event.text

        panel.hide()
        history_view = self.query_one(HistoryView)
        history_view.append_user(text)

        try:
            result = self._manager.handle_input(text)
        except SystemExit:
            self.exit()
            return

        if isinstance(result, str):
            if text == "/clear":
                history_view.clear_all()
            history_view.append_system(result)
            # /think 与 /plan 改变了状态，需要同步刷新状态栏
            if text in ("/think", "/plan"):
                self._refresh_status()
        else:
            # 普通消息：后台线程消费 Agent 事件流；同一时间只允许一轮，避免并发写 history
            self._set_streaming(True)
            self.run_worker(
                lambda: self._do_stream(result),
                thread=True,
                exclusive=True,
            )

    def _set_streaming(self, active: bool) -> None:
        """
        切换运行忙碌状态。

        与 c3 不同：忙碌期间不禁用输入框（保持焦点，使运行中 Esc 取消可靠路由到 on_key），
        新一轮的并发提交由 on_input_bar_input_submitted 的 _stream_active 守卫拦截。
        """
        self._stream_active = active
        if not active:
            self.query_one(InputBar).focus()

    # ------------------------------------------------------------------ #
    # Agent 事件流消费
    # ------------------------------------------------------------------ #
    def _do_stream(self, gen) -> None:
        """
        在 Worker 线程中消费 AgentEvent 生成器，把每个事件渲染到 UI。

        本方法运行在独立线程，所有 UI 操作通过 call_from_thread() 调度到主线程。

        渲染策略：
        - PROGRESS：进入新一轮——重置正文/思考占位组件，使新一轮文本另起新块；第 2 轮起追加一行提示
        - THINKING / TEXT：增量更新对应占位组件（思考灰色斜体、正文 Markdown）
        - TOOL_START：新建橘色工具行并计时；同时重置正文/思考占位（工具后的文本另起块）
        - TOOL_RESULT：工具行定色（绿/红）+ 摘要
        - FINISHED：按结束原因追加系统行（自然完成不打扰）
        - ERROR：红色错误行

        :param gen: ConversationManager._run() 返回的 AgentEvent 生成器
        """
        history_view = self.query_one(HistoryView)

        thinking_widget: Static | None = None
        thinking_chunks: list[str] = []
        response_widget: Static | None = None
        response_chunks: list[str] = []
        tool_widgets: dict = {}

        def reset_text_widgets() -> None:
            """重置正文/思考占位，使后续文本另起新组件（轮次切换或工具执行后调用）。"""
            nonlocal thinking_widget, thinking_chunks, response_widget, response_chunks
            thinking_widget = None
            thinking_chunks = []
            response_widget = None
            response_chunks = []

        try:
            for event in gen:
                etype = event.type

                if etype == AgentEventType.PROGRESS:
                    reset_text_widgets()
                    # 第 2 轮起显示一行进度提示，标示循环在自主推进
                    if event.iteration >= 2:
                        self.call_from_thread(
                            history_view.append_system, f"🔄 第 {event.iteration} 轮"
                        )

                elif etype == AgentEventType.THINKING:
                    if thinking_widget is None:
                        thinking_widget = self.call_from_thread(history_view.begin_thinking_turn)
                    thinking_chunks.append(event.text)
                    self.call_from_thread(
                        history_view.update_widget,
                        thinking_widget,
                        f"[dim italic]💭 {''.join(thinking_chunks)}[/dim italic]",
                    )

                elif etype == AgentEventType.TEXT:
                    if response_widget is None:
                        response_widget = self.call_from_thread(history_view.begin_assistant_turn)
                    response_chunks.append(event.text)
                    self.call_from_thread(
                        history_view.update_ai_widget,
                        response_widget,
                        ''.join(response_chunks),
                    )

                elif etype == AgentEventType.TOOL_START:
                    # 工具开始：重置文本占位（工具后的文本另起块），新建橘色工具行
                    reset_text_widgets()
                    tc = event.tool_call
                    widget = self.call_from_thread(history_view.add_tool_widget, tc)
                    tool_widgets[tc.id] = widget

                elif etype == AgentEventType.TOOL_RESULT:
                    tc = event.tool_call
                    res = event.tool_result
                    widget = tool_widgets.get(tc.id)
                    if widget is None:
                        widget = self.call_from_thread(history_view.add_tool_widget, tc)
                        tool_widgets[tc.id] = widget
                    self.call_from_thread(widget.finish, res.ok, self._summarize_result(res))

                elif etype == AgentEventType.FINISHED:
                    line = self._finish_line(event.stop_reason, event.message)
                    if line:
                        self.call_from_thread(history_view.append_system, line)

                elif etype == AgentEventType.ERROR:
                    self.call_from_thread(history_view.append_error, event.message)
        finally:
            self.call_from_thread(self._set_streaming, False)

    @staticmethod
    def _finish_line(stop_reason, message: str) -> str:
        """
        把循环结束原因转成一行系统提示（自然完成返回空串，不打扰用户）。

        :param stop_reason: StopReason
        :param message: 循环附带的补充说明（如有则优先使用）
        :returns: 要展示的系统行；空串表示不展示
        """
        if stop_reason == StopReason.COMPLETED:
            return ""
        if stop_reason == StopReason.USER_CANCELLED:
            return "⏹ 已取消"
        if stop_reason == StopReason.MAX_ITERATIONS:
            return "⚠ " + (message or "已达迭代上限，自动停止")
        if stop_reason == StopReason.UNKNOWN_TOOL:
            return "⚠ " + (message or "连续调用未知工具，已停止")
        if stop_reason == StopReason.STREAM_ERROR:
            return "⏹ 因流错误已停止"
        return ""

    @staticmethod
    def _summarize_result(res) -> str:
        """
        把工具结果压缩为单行摘要，用于工具行的终态展示。

        优先使用工具自带的 summary；否则回退到取 output 首个非空行并截断。

        :param res: tools.base.ToolResult
        :returns: 单行摘要
        """
        if getattr(res, "summary", ""):
            return res.summary
        text = (res.output or "").strip()
        if not text:
            return "（无输出）" if res.ok else "（无错误信息）"
        first_line = text.splitlines()[0]
        if len(first_line) > 80:
            first_line = first_line[:80] + "…"
        return first_line

    # ------------------------------------------------------------------ #
    # 三类用户交互回调（均在 Worker 线程被调用，阻塞等待主线程选择）
    # ------------------------------------------------------------------ #
    def _interact(self, kind: str, show_fn, default):
        """
        统一的「阻塞式询问主线程」机制（确认/澄清/审批共用）。

        在 Worker 线程：登记一个待决交互盒（含 Event 与默认结果），用 call_from_thread 在主线程
        弹出对应面板，然后阻塞等待，直到主线程的选择/取消处理写入结果并 set() 唤醒。
        主线程事件循环不被阻塞，UI（含其它工具行计时）照常刷新（N2 不死锁）。

        :param kind: 交互种类标识（"confirm"/"clarify"/"approve"），用于结算时映射结果
        :param show_fn: 在主线程展示面板的无参函数
        :param default: 未明确选择（如异常路径）时的默认结果
        :returns: 用户选择的结果
        """
        box = {"event": threading.Event(), "result": default, "kind": kind}
        self._pending_interaction = box
        self.call_from_thread(show_fn)
        box["event"].wait()
        return box["result"]

    def _confirm_tool(self, tool_call, tool) -> ConfirmDecision:
        """有副作用工具执行前确认（spec F6），返回三态决定。"""
        return self._interact(
            "confirm",
            lambda: self._show_confirm_panel(tool_call, tool),
            ConfirmDecision.DENY,
        )

    def _clarify(self, question, options):
        """Plan Mode 需求澄清（spec F12），返回所选概述；用户取消返回 None。"""
        self._clarify_options = options
        return self._interact(
            "clarify",
            lambda: self._show_clarify_panel(question, options),
            None,
        )

    def _approve_plan(self, plan: str) -> bool:
        """Plan Mode 计划执行审批（spec F13），返回是否批准开始执行。"""
        return self._interact(
            "approve",
            lambda: self._show_approve_panel(plan),
            False,
        )

    def _show_confirm_panel(self, tool_call, tool) -> None:
        """在主线程展示工具确认面板并移焦。"""
        self.query_one(CommandPanel).hide()
        panel = self.query_one(ConfirmPanel)
        panel.show_for(tool_call, tool)
        panel.focus()

    def _show_clarify_panel(self, question, options) -> None:
        """
        在主线程展示需求澄清面板并移焦。

        与确认/审批不同：Plan Mode 澄清要求用户「只能在候选项间选择，不能输入文本」，
        因此这里禁用输入框（disabled=True），阻止用户点击输入框继续打字；
        结算交互时（_resolve_interaction）再恢复。其它交互（confirm/approve）不做此限制。
        """
        self.query_one(CommandPanel).hide()
        self.query_one(InputBar).disabled = True
        panel = self.query_one(ClarifyPanel)
        panel.show_for(question, options)
        panel.focus()

    def _show_approve_panel(self, plan: str) -> None:
        """在主线程展示计划审批面板（复用 ConfirmPanel 的通用是/否）并移焦。"""
        self.query_one(CommandPanel).hide()
        panel = self.query_one(ConfirmPanel)
        # 计划全文可能很长，表头只放简短提示；计划内容已在 present_plan 工具行中呈现
        panel.show_prompt(
            "📋 计划已就绪，是否开始执行？",
            "✅ 开始执行  [dim]放开全部工具并自动执行[/dim]",
            "❌ 暂不执行  [dim]返回继续规划[/dim]",
        )
        panel.focus()

    def _resolve_interaction(self, result) -> None:
        """
        在主线程结算一次交互：隐藏所有交互面板、还焦输入框、唤醒被阻塞的 Worker。

        幂等：无待决交互时直接返回，避免重复结算（如选择后又收到取消消息）。
        """
        box = self._pending_interaction
        if box is None:
            return
        self._pending_interaction = None
        self.query_one(ConfirmPanel).hide()
        self.query_one(ClarifyPanel).hide()
        # 恢复输入框：澄清面板期间被禁用（见 _show_clarify_panel），结算后统一解禁并还焦
        self.query_one(InputBar).disabled = False
        self.query_one(InputBar).focus()
        box["result"] = result
        box["event"].set()

    def on_option_list_option_selected(self, event) -> None:
        """
        处理确认/审批/澄清面板的选择（回车/点击）。

        按「当前待决交互的种类」+「事件来源面板」分别把 option.id 解析为对应结果。
        命令面板从不取得焦点、不会触发此消息，故无需额外区分。
        """
        box = self._pending_interaction
        if box is None:
            return
        ol = event.option_list
        kind = box["kind"]

        if isinstance(ol, ConfirmPanel) and kind == "confirm":
            event.stop()
            mapping = {
                "yes": ConfirmDecision.ALLOW,
                "yes_always": ConfirmDecision.ALLOW_ALWAYS,
                "no": ConfirmDecision.DENY,
            }
            self._resolve_interaction(mapping.get(event.option.id, ConfirmDecision.DENY))

        elif isinstance(ol, ConfirmPanel) and kind == "approve":
            event.stop()
            self._resolve_interaction(event.option.id == "yes")

        elif isinstance(ol, ClarifyPanel) and kind == "clarify":
            event.stop()
            try:
                idx = int(event.option.id)
                summary = self._clarify_options[idx].summary
            except (ValueError, IndexError, TypeError):
                summary = None
            self._resolve_interaction(summary)

    def on_confirm_panel_cancelled(self, event: ConfirmPanel.Cancelled) -> None:
        """确认/审批面板按 Esc 取消：确认视为 DENY、审批视为不批准。"""
        box = self._pending_interaction
        if box is None:
            return
        if box["kind"] == "confirm":
            self._resolve_interaction(ConfirmDecision.DENY)
        elif box["kind"] == "approve":
            self._resolve_interaction(False)

    def on_clarify_panel_cancelled(self, event: ClarifyPanel.Cancelled) -> None:
        """澄清面板按 Esc 取消：返回 None，循环据此以「用户取消」结束。"""
        if self._pending_interaction is not None:
            self._resolve_interaction(None)
