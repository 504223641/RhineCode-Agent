"""
Textual App 主类模块。

RhineApp 是 TUI 层的核心，负责：
1. 组合各面板（HistoryView / CommandPanel / ConfirmPanel / ClarifyPanel / InputBar / StatusBar）
2. 监听用户输入事件，交给 CommandDispatcher 统一分流（c10：普通消息与斜杠命令）
3. 将 Agent 循环产出的 AgentEvent 通过 Worker + call_from_thread 安全地渲染到 UI
4. 实现命令层的 CommandController 协议（显示、发送、模式切换、报告、状态刷新、
   清空、压缩、恢复、退出），命令处理函数经该窄接口驱动界面而不感知 Textual
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
from typing import Optional

from rich.markup import escape
from textual.app import App, ComposeResult
from textual.events import Key
from textual.widgets import Static, Input

from rhinecode.config import Config
from rhinecode.commands import (
    CommandDispatcher,
    CommandRegistry,
    ModeTarget,
    ReportTarget,
)
from rhinecode.commands.skill_commands import build_skill_command_specs
from rhinecode.conversation import ConversationManager, SessionListRequest
from rhinecode.agent.events import AgentEventType, StopReason, ConfirmDecision
from rhinecode.tui.widgets import (
    HistoryView, InputBar, StatusBar, CommandPanel, ConfirmPanel, ClarifyPanel,
    SessionPanel,
)


class RhineApp(App):
    """
    RhineCode 的 Textual 应用主类。

    布局（从上到下）：
    - HistoryView：占据除底部区域外的全部高度（height: 1fr），可滚动
    - CommandPanel：斜杠命令提示面板，默认隐藏，输入 "/" 时弹出
    - ConfirmPanel：有副作用工具执行前确认 / 计划执行审批的内联面板，默认隐藏
    - ClarifyPanel：Plan Mode 需求澄清的内联面板，默认隐藏
    - SessionPanel：/resume 会话选择的内联面板，默认隐藏（c9 交互化）
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
    /* /resume 会话选择面板：青色分隔线；显示全部会话，超出 15 行由 OptionList 自滚 */
    SessionPanel {
        height: auto;
        max-height: 15;
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

    def __init__(
        self,
        manager: ConversationManager,
        config: Config,
        command_registry: CommandRegistry,
    ):
        """
        :param manager: 已初始化的对话管理器，持有 Provider / Agent 和对话历史
        :param config: 配置对象，用于在状态栏展示 Provider 和模型信息
        :param command_registry: 启动早期构建的命令注册表（c10）。App 不自建注册表——
                                 同一实例同时注入分发器、命令面板与输入高亮器（spec F3），
                                 保证执行、补全与帮助共享同一份事实来源
        """
        super().__init__()
        self._manager = manager
        self._config = config
        # 命令层接线（c10）：单个分发器实例，提交入口的唯一分流点。
        self._command_registry = command_registry
        self._dispatcher = CommandDispatcher(command_registry)
        # 待决的用户交互（确认/澄清/审批）：None 表示当前无交互在进行；
        # 进行中时为 {"event": threading.Event, "result": Any, "kind": str}，
        # 由回调在 Worker 线程创建并阻塞、由主线程的选择/取消处理写入结果并唤醒。
        self._pending_interaction: dict | None = None
        # 当前澄清面板的候选项列表（用于把所选下标还原为概述文本）
        self._clarify_options: list = []
        # 单轮运行锁：避免多个 Worker 同时修改同一份 conversation history。
        self._stream_active = False
        # 「当前不能提交」提示是否已在本次忙碌期显示过（c11 T55）：
        # 进入流式 / 每次新面板弹出时复位，避免用户连按回车刷屏。
        self._busy_hint_shown = False
        # /resume 会话选择面板是否正在展示（c9 交互化）：
        # 展示期间输入框被禁用，此标志作为各输入路径的一致性兜底守卫。
        self._session_panel_active = False

    def compose(self) -> ComposeResult:
        """按从上到下的顺序挂载各面板（命令面板与输入框共享同一注册表，c10）。"""
        yield HistoryView()
        yield CommandPanel(self._command_registry)
        yield ConfirmPanel()
        yield ClarifyPanel()
        yield SessionPanel()
        yield InputBar(
            self._command_registry,
            placeholder="输入消息，/ 查看命令，Tab 补全，运行中按 Esc 取消，Ctrl+Q 退出",
        )
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

        # 记忆系统接线（c9）：
        # 1. 启动提示（--continue 恢复结果等）作为系统提示行显示；
        # 2. 笔记通知回调：笔记线程（非主线程）触发，必须经 call_from_thread 调回主线程渲染；
        # 3. 会话锁心跳：每 2 分钟 touch 一次，保证「进程活着锁就新鲜」（过期阈值 10 分钟）。
        # --continue 启动恢复对齐（c9 交互化）：若启动时已恢复出历史（history 非空），
        # 先把整段历史回放到聊天区，再显示启动提示——与 /resume 面板载入后的体验一致。
        if self._manager.history:
            self.query_one(HistoryView).render_history(self._manager.history)
        if self._manager.startup_notice:
            self.query_one(HistoryView).append_system(self._manager.startup_notice)
        self._manager.memory_manager.notify = self._notify_memory
        self.set_interval(120, self._manager.memory_manager.touch_session_lock)

        # Skill 激活通知（c11）：模型调 load_skill 成功后立刻刷新状态栏的
        # Skill 段，让用户当下就看到激活数变化，而不用等本轮流式结束。
        self._manager.skill_manager.notify_activation = self._notify_skill_activation

        # 启动后将焦点置于输入框，用户可以直接开始输入
        self.query_one(InputBar).focus()

    def _show_busy_hint(self, text: str) -> None:
        """
        显示一次「当前不能提交」的提示（c11 T55）。

        :param text: 提示文本

        **同一次忙碌期内只提示一次**。用户在等待时连按几次回车是很自然的，
        每次都刷一行会把聊天区淹掉，反而看不见真正的内容。

        复位规则写死三条：
        1. 进入流式时复位一次；
        2. 每次**新**面板弹出时复位一次；
        3. 面板关闭时**不**复位——关闭后就不再拦截提交了，没有复位的必要，
           而在关闭时复位反而会让「面板刚关、流式仍在跑」的窗口里多刷一行。
        """
        if self._busy_hint_shown:
            return
        self._busy_hint_shown = True
        self.query_one(HistoryView).append_system(text)

    def _notify_memory(self, text: str) -> None:
        """
        笔记更新的低打扰通知（c9 F20）。运行在笔记 daemon 线程，
        用 call_from_thread 把渲染调度回主线程（Textual 线程安全要求）。
        """
        try:
            self.call_from_thread(self.query_one(HistoryView).append_system, text)
        except Exception:
            # 应用正在退出等边缘情况：通知丢弃即可，不影响任何状态。
            pass

    def _refresh_status(self) -> None:
        """刷新状态栏，反映当前 Provider、模型、思考模式、计划模式、权限模式、上下文用量。"""
        # 上下文用量（c8）：随对话增长实时变化，故每次刷新都重新取值；
        # 返回 (文本, 是否高亮) 或 None（工具不可用的 Provider）。
        ctx = self._manager.context_status_line()
        self.query_one(StatusBar).update_status(
            self._config.protocol,
            self._config.model,
            self._manager.thinking_effort,
            self._manager.plan_mode,
            self._manager.permission_mode_value,
            # MCP 连接状态（c7）：启动后不变，随每次刷新一并带上即可。
            self._manager.mcp_status_line(),
            context_status=ctx[0] if ctx else None,
            context_warn=bool(ctx and ctx[1]),
            # 已激活 Skill 数（c11）：无激活时为 None，状态栏隐藏该段。
            skill_status=self._manager.skill_status_segment(),
        )

    # ------------------------------------------------------------------ #
    # CommandController 协议实现（c10 T43/T45）：命令处理函数经此驱动界面
    # ------------------------------------------------------------------ #
    @property
    def tools_enabled(self) -> bool:
        """当前 Provider 是否具备工具能力（委托 Manager，供 /init 等命令判断）。"""
        return self._manager.tools_enabled

    def show_user_input(self, text: str) -> None:
        """聊天区回显一次用户输入（仅显示，不写入模型历史；由分发器统一调用）。"""
        self.query_one(HistoryView).append_user(text)

    def show_message(self, text: str) -> None:
        """显示本地命令结果或错误（系统行）。"""
        self.query_one(HistoryView).append_system(text)

    def send_user_message(self, content: str, display_content: Optional[str] = None) -> None:
        """
        把用户消息交给现有对话路径：Manager 追加历史/存档后返回事件流，
        统一经 _consume_manager_result 启动后台 Worker 流式消费。
        """
        self._consume_manager_result(
            self._manager.submit_user_message(content, display_content)
        )

    def switch_mode(self, target: ModeTarget) -> str:
        """按目标模式调用对应领域方法，返回供界面显示的结果文本。"""
        if target == ModeTarget.THINKING:
            return self._manager.cycle_thinking()
        if target == ModeTarget.PLAN:
            return self._manager.toggle_plan()
        if target == ModeTarget.PERMISSION:
            return self._manager.cycle_permission()
        # 未知枚举值明确报错（不静默选默认分支）：新增 ModeTarget 时必须同步这里
        raise ValueError(f"未知的模式目标：{target!r}")

    def query_report(self, target: ReportTarget) -> str:
        """按目标报告调用对应只读领域方法。"""
        if target == ReportTarget.MCP:
            return self._manager.mcp_report()
        if target == ReportTarget.CONTEXT:
            return self._manager.context_report()
        if target == ReportTarget.MEMORY:
            return self._manager.memory_report()
        if target == ReportTarget.SKILLS:
            return self._manager.skills_report()
        if target == ReportTarget.SKILLS_PROMPT:
            return self._manager.skills_prompt_report()
        raise ValueError(f"未知的报告目标：{target!r}")

    def refresh_status(self) -> None:
        """刷新状态栏（命令处理函数显式调用，取代旧的命令字符串白名单）。"""
        self._refresh_status()

    def clear_conversation(self) -> None:
        """清空对话：领域侧清历史/开新档 + 界面侧清聊天区（确认文本由命令层显示）。"""
        self._manager.clear()
        self.query_one(HistoryView).clear_all()

    def compact_context(self) -> None:
        """手动压缩：Manager 返回事件流（阻塞的摘要 LLM 调用）走后台 Worker。"""
        self._consume_manager_result(self._manager.manual_compact())

    def resume_session(self, key: Optional[str]) -> None:
        """
        恢复会话：key 为 None 弹选择面板、非 None 直接载入。
        面板选中路径与命令路径复用本方法（c10 起不再伪造 "/resume <id>" 文本）。
        """
        self._consume_manager_result(self._manager.resume(key))

    def exit_application(self) -> None:
        """退出应用（/exit 经此退出，不再依赖 SystemExit 穿透，c10）。"""
        self.exit()

    # ---- Skill 控制器方法（c11）----

    def run_skill(self, name: str, arguments: str, display: str) -> None:
        """
        执行一个 Skill（c11 F24）。

        两种模式的返回值形态不同（共享模式返回主对话事件流、独立模式返回子对话
        事件流、找不到时返回文本），全部交给 `_consume_manager_result` 统一消费。
        """
        self._consume_manager_result(
            self._manager.run_skill(name, arguments, display)
        )

    def reload_skills(self) -> str:
        """
        热更新 Skill 定义**并重新注册斜杠短命令**（c11 F26），返回报告文本。

        分两步，顺序不能反：
        1. 领域侧 `manager.reload_skills()` 重新扫盘、重跑白名单两段校验、
           同步激活列表，产出可读报告；
        2. 界面侧用**新的** `command_infos()` 整体替换注册表里的 Skill 短命令。

        **为什么接在这里而不是 `conversation.py`**：短命令刷新需要
        `CommandRegistry`，而协调层刻意不依赖 commands 包（依赖方向是
        `commands ← tui/app ← __main__`）。而 `RhineApp` 本来就同时持有
        注册表与 SkillManager，且已经导入 commands——这正是「领域能力放
        conversation、接线放控制器方法」的既定模式，不需要新增任何回调或协议方法。

        补全菜单与输入高亮**无需刷新**：`CommandPanel` 与 `CommandHighlighter`
        持有的是注册表引用，每次按键现调 `complete()` / `resolve()`，
        替换后下一次按键就是新结果。

        副作用：重新扫盘；替换注册表的 Skill 短命令集合。
        """
        report = self._manager.reload_skills()

        skipped = self._command_registry.replace_skill_commands(
            build_skill_command_specs(self._manager.skill_manager.command_infos())
        )
        if skipped:
            # 与启动时同一口径：短命令没注册不等于 Skill 不可用，
            # 必须给出 /skills run 这个替代入口，否则用户会以为 Skill 坏了。
            names = "、".join(s.name for s in skipped)
            report += (
                f"\n\n以下短命令与已有命令冲突、未注册：{names}"
                f"\n请改用 /skills run <名字> 执行它们。"
            )
        return report

    def deactivate_skill(self, name: Optional[str]) -> str:
        """卸载已激活的 Skill；name 为 None 表示全部。返回结果文本。"""
        return self._manager.deactivate_skill(name)

    def _notify_skill_activation(self) -> None:
        """
        模型激活 Skill 后立刻刷新状态栏（c11 T53）。

        本方法由 `SkillManager` 在**工作线程**里回调，因此必须 `call_from_thread`
        跨回主线程更新界面。

        **不能用裸 lambda 而要包 try/except**：`activate()` 跑在
        `LoadSkillTool.execute()` 里，而它又在只读并发桶的 `ThreadPoolExecutor` 里；
        `future.result()` 外层的 `except Exception` 会把这里抛出的任何异常
        转成「工具执行异常」——于是应用退出竞态下一次本已成功的激活，
        会被报告成工具失败回灌给模型，模型可能因此重试或放弃。

        本回调的价值只在「激活当下立刻刷新」：`_do_stream` 的 finally 里已经
        无条件刷一次状态栏，所以即使这里丢掉一次刷新也不会留下错误状态。

        副作用：跨线程调度一次界面刷新。
        """
        try:
            self.call_from_thread(self._refresh_status)
        except Exception:
            # 应用正在退出等边缘情况：刷新丢弃即可。
            pass

    def _consume_manager_result(self, result) -> None:
        """
        统一消费 Manager 领域方法的三类返回值（c10 T44）：

        - str：本地反馈文本，直接显示；
        - SessionListRequest：打开会话选择面板；
        - 事件迭代器：设置 streaming 状态并在下一帧启动现有 Worker 流式消费
          （同一时间只允许一个流式 Worker，由 exclusive=True 与提交守卫共同保证）。
        """
        if isinstance(result, str):
            self.show_message(result)
        elif isinstance(result, SessionListRequest):
            self._show_session_panel(result)
        else:
            self._set_streaming(True)
            # 先让本帧渲染（用户输入回显等）完成，再启动后台 Worker——
            # 避免用户消息与首块回复合并在同一帧绘制（观感上像输入被延迟显示）。
            self.call_after_refresh(self._start_stream_worker, result)

    # ------------------------------------------------------------------ #
    # 输入与命令面板
    # ------------------------------------------------------------------ #
    def on_input_changed(self, event: Input.Changed) -> None:
        """
        监听输入框内容变化，控制命令提示面板的显示与过滤。

        仅当输入以 "/" 开头且仍处于命令字段（尚无空白分隔符，即尚未进入参数区）
        时显示候选面板；参数区输入、普通文本、零候选均隐藏（c10 plan 10.2）。
        交互进行中、流式运行中或会话选择面板展示中不处理。
        """
        if (
            self._pending_interaction is not None
            or self._stream_active
            or self._session_panel_active
        ):
            return
        panel = self.query_one(CommandPanel)
        value = event.value
        if value.startswith("/") and not any(ch.isspace() for ch in value):
            panel.show_for(value)
        else:
            panel.hide()

    def on_input_bar_command_completion_requested(
        self, event: InputBar.CommandCompletionRequested
    ) -> None:
        """
        处理命令字段的 Tab 补全请求（c10 T47，spec F22）。

        - 单候选：直接替换输入框命令字段；候选规范命令有参数提示时末尾保留一个空格；
        - 多候选：显示稳定排序的候选菜单（焦点保持在 InputBar，方向键经 on_key
          转发给面板移动高亮，Enter 执行当前高亮项）；
        - 零候选：隐藏面板，不做任何改动。
        """
        if (
            self._pending_interaction is not None
            or self._stream_active
            or self._session_panel_active
        ):
            return
        items = self._command_registry.complete(event.prefix)
        panel = self.query_one(CommandPanel)
        if not items:
            panel.hide()
            return
        if len(items) == 1:
            item = items[0]
            spec = self._command_registry.resolve(item.canonical_name)
            trailing = bool(spec and spec.argument_hint)
            self.query_one(InputBar).apply_completion(item.value, trailing_space=trailing)
            panel.hide()
            return
        panel.show_for(event.prefix)

    def on_key(self, event: Key) -> None:
        """
        处理特殊按键：运行中取消、命令面板导航。

        优先级：
        1. 有交互待决（确认/澄清/审批）或会话选择面板展示中 → 交给被聚焦的面板自身的
           Esc 绑定与 OptionList 原生导航处理，这里不拦截。
        2. 流式运行中 → Esc 触发取消当前 Agent 循环（spec F9）。
        3. 命令面板可见 → Up/Down 移动高亮、Esc 隐藏（焦点始终保持在 InputBar）。
        """
        # 1. 交互待决 / 会话选择面板展示中：让面板自己处理（它们各有 escape 绑定，
        #    上下键与回车由获得焦点的 OptionList 原生消化），不在此拦截
        if self._pending_interaction is not None or self._session_panel_active:
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
        处理用户提交输入（c10：唯一入口是 CommandDispatcher）。

        步骤：
        1. 保留交互待决、流式运行、会话面板展示时的提交守卫；
        2. 若命令面板有高亮候选，用候选文本替换待提交文本（Enter 执行当前高亮项，
           即使用户只输入了部分前缀，spec F23）；
        3. 隐藏命令面板后只调用 dispatcher.dispatch(text, self)——回显、命令执行、
           未知命令提示与错误边界全部由分发器统一负责，App 不再区分具体命令名，
           也不再维护状态刷新白名单。
        """
        # 交互进行中 / 流式运行中 / 会话选择面板展示中：拦下提交。
        #
        # c11 起前两种情形给出**可见提示**而不是静默 return——静默会让用户
        # 以为界面卡死了（尤其在确认面板期间，输入框并没有被禁用）。
        if self._pending_interaction is not None:
            # 本分支**可达且最常见**：只有澄清面板会禁用 InputBar，
            # 确认面板与计划审批面板期间用户点回输入框敲回车就会命中这里。
            self._show_busy_hint("正在等待你的确认，请先在面板上做出选择。")
            return
        if self._stream_active:
            self._show_busy_hint("正在运行中，可按 Esc 取消后再执行命令。")
            return
        if self._session_panel_active:
            # 本分支实际不可达：`_show_session_panel` 已经把 InputBar 设为
            # disabled，提交事件根本发不出来。保留裸 return 作为一致性兜底。
            return
        panel = self.query_one(CommandPanel)

        if panel.display and panel.highlighted is not None:
            text = panel.get_option_at_index(panel.highlighted).id
        else:
            text = event.text

        panel.hide()
        self._dispatcher.dispatch(text, self)

    def _start_stream_worker(self, gen) -> None:
        """
        在本帧渲染完成后，启动后台线程 Worker 消费 Agent 事件流。

        由 _consume_manager_result 通过 call_after_refresh 调度，运行在主线程消息循环中，
        因此可安全调用 run_worker。exclusive=True 保证同一时间只有一个流式 Worker。

        :param gen: Manager 领域方法返回的 AgentEvent 生成器
        """
        self.run_worker(
            lambda: self._do_stream(gen),
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
        if active:
            # 进入流式：复位提示标志，本轮可以再提示一次。
            self._busy_hint_shown = False
        if not active:
            self.query_one(InputBar).focus()

    # ------------------------------------------------------------------ #
    # /resume 会话选择面板（c9 交互化）
    # ------------------------------------------------------------------ #
    def _show_session_panel(self, request: SessionListRequest) -> None:
        """
        在主线程展示会话选择面板并移焦。

        与澄清面板同款处理：禁用输入框阻止用户点回输入框打字；焦点移到面板后，
        上下键/回车由 OptionList 原生消化，Esc 由面板自身绑定发 Cancelled。
        本交互由主线程发起（用户输入命令的直接结果），没有 Worker 在阻塞等待，
        因此**不需要** _interact 的 threading.Event 机制。

        :param request: Manager.resume(None) 返回的会话列表信号
        """
        self.query_one(CommandPanel).hide()
        self.query_one(InputBar).disabled = True
        self._session_panel_active = True
        panel = self.query_one(SessionPanel)
        panel.show_for(request.sessions, request.current_id)
        panel.focus()

    def _close_session_panel(self) -> None:
        """关闭会话选择面板：隐藏、恢复输入框可用并还焦。"""
        self._session_panel_active = False
        self.query_one(SessionPanel).hide()
        self.query_one(InputBar).disabled = False
        self.query_one(InputBar).focus()

    def on_session_panel_cancelled(self, event: SessionPanel.Cancelled) -> None:
        """会话选择面板按 Esc 退出：关闭面板，不做任何载入，界面原样保留。"""
        self._close_session_panel()

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
        - HISTORY：会话恢复成功（c9）——清空聊天区并整体回放携带的历史快照

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
                        f"[dim italic]💭 {escape(''.join(thinking_chunks))}[/dim italic]",
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
                    # 改文件类工具会在 res.diff 带上结构化差异，传给工具行渲染彩色 diff 块
                    self.call_from_thread(
                        widget.finish, res.ok, self._summarize_result(res), getattr(res, "diff", None)
                    )

                elif etype == AgentEventType.FINISHED:
                    line = self._finish_line(event.stop_reason, event.message)
                    if line:
                        self.call_from_thread(history_view.append_system, line)

                elif etype == AgentEventType.ERROR:
                    self.call_from_thread(history_view.append_error, event.message)

                elif etype == AgentEventType.NOTICE:
                    # 系统级提示（c8：上下文压缩发生等），以系统行展示，不影响正文/工具渲染。
                    self.call_from_thread(history_view.append_system, event.message)

                elif etype == AgentEventType.HISTORY:
                    # 会话恢复成功（c9 /resume 交互化）：清屏并整体回放历史快照。
                    # 只发起一次 call_from_thread——清空与重画在主线程一次调用内原子完成；
                    # 同时重置本 Worker 的文本/工具占位引用（旧引用指向已被移除的组件）。
                    reset_text_widgets()
                    tool_widgets.clear()
                    self.call_from_thread(history_view.render_history, event.messages)
        finally:
            self.call_from_thread(self._set_streaming, False)
            # 工具调用可能在本轮流式执行中通过 mcp_add_server 改变 MCP 连接状态；
            # 收尾时刷新状态栏，让新工具数量或失败信息立即反映到界面上。
            self.call_from_thread(self._refresh_status)

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
        if stop_reason == StopReason.PLAN_REJECTED:
            return "⏹ 计划未执行"
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
        # 新面板弹出：复位提示标志，这一次面板期间可以再提示一次。
        self._busy_hint_shown = False
        self.call_from_thread(show_fn)
        box["event"].wait()
        return box["result"]

    def _confirm_tool(self, tool_call, tool, decision) -> ConfirmDecision:
        """
        人在回路确认（c6 spec F6），返回四态决定。

        :param tool_call: 待确认的工具调用
        :param tool: 工具实例
        :param decision: 决策管线给出的 DecisionResult，面板用其 reason 告知用户为何需要确认
        """
        return self._interact(
            "confirm",
            lambda: self._show_confirm_panel(tool_call, tool, decision),
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

    def _show_confirm_panel(self, tool_call, tool, decision) -> None:
        """在主线程展示工具确认面板并移焦（c6：传入 decision 以展示拒绝/询问原因）。"""
        self.query_one(CommandPanel).hide()
        panel = self.query_one(ConfirmPanel)
        panel.show_for(tool_call, tool, decision)
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
        # 计划全文可能很长，已作为聊天记录中的普通助手消息展示；这里仅询问是否进入执行阶段。
        panel.show_prompt(
            "📋 计划已就绪，是否开始执行？",
            "✅ 开始执行  [dim]写文件/改文件/运行命令仍会逐个确认[/dim]",
            "❌ 暂不执行  [dim]停止本次执行[/dim]",
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
        处理确认/审批/澄清/会话选择面板的选择（回车/点击）。

        按「当前待决交互的种类」+「事件来源面板」分别把 option.id 解析为对应结果。
        命令面板从不取得焦点、不会触发此消息，故无需额外区分。

        注意：SessionPanel 分支必须放在 `box is None` 守卫**之前**——会话选择不走
        _pending_interaction 机制（主线程发起、无 Worker 阻塞等待），守卫会把它拦掉。
        """
        if isinstance(event.option_list, SessionPanel):
            event.stop()
            session_id = event.option.id
            self._close_session_panel()
            # 直接复用恢复控制器（c10 T48）：与 /resume <id> 命令同一领域入口，
            # 不再伪造用户没有手输的 "/resume <session_id>" 文本（也不回显它）。
            # option.id 携带完整 session_id，_resolve_key 按精确 ID 匹配必中；
            # 载入可能触发 C8 压缩（阻塞的摘要 LLM 调用），统一消费路径走后台 Worker。
            self.resume_session(session_id)
            return

        box = self._pending_interaction
        if box is None:
            return
        ol = event.option_list
        kind = box["kind"]

        if isinstance(ol, ConfirmPanel) and kind == "confirm":
            event.stop()
            mapping = {
                "yes": ConfirmDecision.ALLOW,
                "yes_session": ConfirmDecision.ALLOW_SESSION,
                "yes_permanent": ConfirmDecision.ALLOW_PERMANENT,
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
