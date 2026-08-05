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
from rhinecode.trace import (
    NullRecorder,
    SCOPE_MAIN,
    TraceEventType,
    TraceRecorderProtocol,
    agent_event_payload,
    clip,
)
from rhinecode.tui.widgets import (
    HistoryView, InputBar, StatusBar, CommandPanel, ConfirmPanel, ClarifyPanel,
    SessionPanel, compose_status_text,
    # ⚠️ **必须用 widgets 的 escape，不能 `from rich.markup import escape`**。
    # 这里唯一的用途是转义**流式累积中的思考文本**，而它是最不该用 rich 那版的地方：
    # 「流式累积」意味着任何一帧都是在**任意位置**被截断的模型自由文本，
    # 而 rich 的 escape 只转义「看起来像完整标签」的 `[...]`，认不出被截断的括号。
    # 一旦漏过去，Textual 会在渲染时抛 MarkupError 并拆掉整个 app（详见
    # widgets.escape 的注释与 tests/test_tui_markup_escape.py 的现场重演）。
    #
    # 说明边界：本处**未实测复现**过崩溃（触发形态较窄，见测试里那条
    # 「两个未闭合括号」的用例）；换成安全版是因为输入性质相同——
    # 任意模型文本 × 任意截断点，没有理由赌它撞不上。
    escape,
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
        recorder: "Optional[TraceRecorderProtocol]" = None,
    ):
        """
        :param manager: 已初始化的对话管理器，持有 Provider / Agent 和对话历史
        :param config: 配置对象，用于在状态栏展示 Provider 和模型信息
        :param command_registry: 启动早期构建的命令注册表（c10）。App 不自建注册表——
                                 同一实例同时注入分发器、命令面板与输入高亮器（spec F3），
                                 保证执行、补全与帮助共享同一份事实来源
        :param recorder: 行为记录器（trace 设施）。缺省用 `NullRecorder()`，
                         **不传等于零回归**，界面层的全部埋点变成空调用
        """
        super().__init__()
        self._manager = manager
        self._config = config
        # 行为记录器：Null Object 兜底，界面各埋点无需判空（trace spec N1）
        self._recorder: TraceRecorderProtocol = recorder or NullRecorder()
        # 命令层接线（c10）：单个分发器实例，提交入口的唯一分流点。
        self._command_registry = command_registry
        self._dispatcher = CommandDispatcher(command_registry, recorder=self._recorder)
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

        **埋点位置刻意排在 call_from_thread 之后**（全阶段复测观察 O5）。

        原先这里直接调 `append_system`、绕过了 `_trace_ui_message`，后果是
        c9 的 AC19「笔记变更时界面出现低打扰提示」在**任何**基于 trace 的验收里
        都是盲区——记录里没有这条 `ui_message`，而「显示了没记」与「压根没显示」
        （notify 为 None，或下面这个 except 把异常吞了）在 trace 上完全无法区分。

        补埋点时把它放在成功调用之后，是为了让「记录里有 ⟺ 界面上真的出现过」
        成立：`call_from_thread` 是**阻塞式**的，它正常返回就意味着主线程确实
        执行完了 `append_system`。反过来若照 `show_message` 那样先记后显示，
        退出竞态下 `call_from_thread` 抛出、异常被下面吞掉，就会留下一条
        **界面上从未出现过的记录**——观测设施撒谎，而且不报错。

        这与 CLAUDE.md 记的 `_do_stream` 那条「埋点放在 call_from_thread 之前」
        **不矛盾**：那里记的是**已经流式显示过**的正文，埋点只是补记录，
        放后面等于「出错时不记录」；这里的提示则是**还没显示**，先记就是撒谎。
        判据是同一条——记录必须与用户真实看到的一致。
        """
        try:
            self.call_from_thread(self.query_one(HistoryView).append_system, text)
            self._trace_ui_message("system", text)
        except Exception:
            # 应用正在退出等边缘情况：通知丢弃即可，不影响任何状态。
            pass

    def _refresh_status(self) -> None:
        """刷新状态栏，反映当前 Provider、模型、思考模式、计划模式、权限模式、上下文用量。"""
        # 上下文用量（c8）：随对话增长实时变化，故每次刷新都重新取值；
        # 返回 (文本, 是否高亮) 或 None（工具不可用的 Provider）。
        ctx = self._manager.context_status_line()
        # 九个值先组成**一份**参数组，再同时喂给 update_status 与纯函数
        # compose_status_text（trace T44）。
        #
        # ⚠️ 不要在下面手抄第二份参数清单：`update_status` 与 `compose_status_text`
        # 是同名九参数，抄一遍会让「新增状态栏字段」这个维护点从两处涨到三处，
        # 而漏改的后果是记录里的状态栏文本与用户实际看到的不一致——最坏的一种
        # 观测设施失效（它撒谎但不报错）。
        status_args = dict(
            provider=self._config.protocol,
            model=self._config.model,
            thinking_effort=self._manager.thinking_effort,
            plan_mode=self._manager.plan_mode,
            permission_mode=self._manager.permission_mode_value,
            # MCP 连接状态（c7）：启动后不变，随每次刷新一并带上即可。
            mcp_status=self._manager.mcp_status_line(),
            context_status=ctx[0] if ctx else None,
            context_warn=bool(ctx and ctx[1]),
            # 已激活 Skill 数（c11）：无激活时为 None，状态栏隐藏该段。
            skill_status=self._manager.skill_status_segment(),
        )
        self.query_one(StatusBar).update_status(**status_args)
        # 为什么记「组装后的文本」而不是九个散字段（trace F15）：用户真正看到的
        # 那行文本是 compose_status_text 在 update_status 内部拼出来的，只记字段
        # 的话「文本快照」这个承诺不成立（比如某个字段的渲染分支写错了，
        # 记录里看不出来）。compose_status_text 是纯函数、零副作用，多调一次没成本。
        self._recorder.emit_lazy(
            TraceEventType.STATUS_BAR,
            lambda: {"text": clip(compose_status_text(**status_args))},
        )

    # ------------------------------------------------------------------ #
    # CommandController 协议实现（c10 T43/T45）：命令处理函数经此驱动界面
    # ------------------------------------------------------------------ #
    @property
    def tools_enabled(self) -> bool:
        """当前 Provider 是否具备工具能力（委托 Manager，供 /init 等命令判断）。"""
        return self._manager.tools_enabled

    def _trace_ui_message(self, source: str, text: str) -> None:
        """
        记一条 `ui_message` 事件。

        :param source: 来源（user_echo / system / error / assistant）
        :param text: **markup 转义之前的原始文本**（trace F15）

        为什么记转义前：界面为了不让字面 `[` 被 Textual 当标签吞掉，会做 `\[` 转义
        （见状态栏与历史区的既有做法）。记转义后的文本，读 trace 的人看到的是
        `\[provider]` 这种带反斜杠的怪东西，而那不是用户看到的内容也不是程序
        产生的内容，两头都对不上。
        """
        self._recorder.emit_lazy(
            TraceEventType.UI_MESSAGE,
            lambda: {"source": source, "text": clip(text)},
        )

    def show_user_input(self, text: str) -> None:
        """聊天区回显一次用户输入（仅显示，不写入模型历史；由分发器统一调用）。"""
        self._trace_ui_message("user_echo", text)
        self.query_one(HistoryView).append_user(text)

    def show_message(self, text: str) -> None:
        """显示本地命令结果或错误（系统行）。"""
        self._trace_ui_message("system", text)
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
        1. 领域侧 `manager.reload_skills()` 重新扫盘、重新解析 `allowed-tools` 声明、
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

    def _settle_session(self, session_id: Optional[str], source: str = "human") -> None:
        """
        **会话选择面板结算的唯一入口**（选中与取消两条路径都走它）。

        :param session_id: 选中的会话标识；`None` 表示取消（关闭面板不载入）
        :param source: 结算路径来源，取值集合见 `_resolve_interaction`

        执行步骤：埋一条 INTERACTION 事件 → 关闭面板 → 有 id 则载入该会话。

        ⚠️ **第一行的幂等守卫不可省**：没有面板挂着时静默返回。
        缺了它，驱动设施退出时的「强制结算」会在没有面板的情况下凭空多埋一条交互
        事件，破坏 trace「四类面板各产出恰好一条」的口径（trace AC16）。

        ⚠️ 会话选择面板走的不是 `_interact` 那条路（它由主线程发起，没有 Worker 在
        阻塞等待），所以埋点只能在结算处做，而**两条结算路径必须合并到这里**——
        分散在两处时，将来任何第三条路径都会漏掉埋点与守卫。

        副作用：产出记录事件、隐藏面板并还焦输入框、可能触发一次会话载入
        （载入会走后台 Worker，因为它可能触发阻塞的 C8 摘要调用）。
        """
        if not self._session_panel_active:
            return
        self._recorder.emit(
            TraceEventType.INTERACTION,
            kind="session",
            display=str(session_id) if session_id is not None else "",
            source=source,
            result="selected" if session_id is not None else "cancelled",
        )
        self._close_session_panel()
        if session_id is not None:
            # 直接复用恢复控制器（c10 T48）：与 /resume <id> 命令同一领域入口，
            # 不再伪造用户没有手输的 "/resume <session_id>" 文本（也不回显它）。
            # option.id 携带完整 session_id，_resolve_key 按精确 ID 匹配必中。
            self.resume_session(session_id)

    def on_session_panel_cancelled(self, event: SessionPanel.Cancelled) -> None:
        """会话选择面板按 Esc 退出：关闭面板，不做任何载入，界面原样保留。"""
        self._settle_session(None)

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
        - TOOL_PENDING：模型刚开始生成该调用的参数（可能持续几十秒）——立刻建一行
          橘色「参数生成中… Ns」，这是那段时间里界面上唯一的活体信号
        - TOOL_START：进入执行态。若 TOOL_PENDING 已建过行则**原地复用**（补参数摘要、
          重新起算耗时），否则新建；同时重置正文/思考占位（工具后的文本另起块）
        - TOOL_RESULT：工具行定色（绿/红）+ 摘要；同时把它从待定表里摘掉
        - 收尾：待定表里剩下的（取消/流出错导致没等到结果）统一标记为「未执行」，
          不留永远转圈的橘色行
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
            # 界面消息埋点（trace F15）：本轮 AI 正文在这里收尾——重置占位之前
            # 把已累积的内容记一条。记的是**已在界面上呈现的完整一段**，
            # 而不是逐块增量（那会产出几百条碎片事件）。
            if response_chunks:
                self._recorder.emit_lazy(
                    TraceEventType.UI_MESSAGE,
                    lambda text="".join(response_chunks): {
                        "source": "assistant",
                        "text": clip(text),
                    },
                )
            thinking_widget = None
            thinking_chunks = []
            response_widget = None
            response_chunks = []

        try:
            for event in gen:
                etype = event.type
                # 循环事件埋点（trace F15 + F17 字段白名单）。
                #
                # ⚠️ **TEXT / THINKING 两类刻意不记录**，这是 F17「同一份数据不重复
                # 携带」裁决的必然延伸。它们是**逐块**产出的流式增量：一次几百字的
                # 回答会切成好几百个块、每块一条事件，而按字段白名单剥掉正文之后，
                # 每条剩下的全部信息只有 `text_length: 2`——信息量为零，却把整条
                # 时间线淹掉。
                #
                # 实测（手测场景 3）：2096 条记录里 1944 条是这种噪音（93%），
                # 一次 `api_request` 与它的 `api_response` 之间夹着 322 条，人没法读。
                #
                # 丢掉的那点信息由更有用的聚合形态承载：完整正文与思考在
                # `api_response`（各一条）、界面上呈现的完整段落在 `ui_message`、
                # 块数与首块延迟也在 `api_response`（见 tracing_provider）。
                #
                # ⚠️ 必须用**默认参数绑定** `e=event`：循环内直接写
                # `lambda: agent_event_payload(event)` 捕获的是变量而不是当轮的值，
                # 全部闭包最终都指向最后一个事件（Python 闭包按引用捕获）。
                if etype not in (AgentEventType.TEXT, AgentEventType.THINKING):
                    self._recorder.emit_lazy(
                        TraceEventType.AGENT_EVENT,
                        lambda e=event: agent_event_payload(e),
                    )

                if etype == AgentEventType.PROGRESS:
                    reset_text_widgets()
                    # 第 2 轮起显示一行进度提示，标示循环在自主推进
                    if event.iteration >= 2:
                        line = f"🔄 第 {event.iteration} 轮"
                        self._trace_ui_message("system", line)
                        self.call_from_thread(history_view.append_system, line)

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

                elif etype == AgentEventType.TOOL_PENDING:
                    # 模型刚开始吐这个调用，参数还在流里（可能要几十秒）。
                    # 先建一行「参数生成中… Ns」，让界面立刻有活体信号；这一行随后
                    # 由 TOOL_START 原地转成执行态，**不会**再多建一行。
                    reset_text_widgets()
                    tc = event.tool_call
                    if tc.id not in tool_widgets:
                        tool_widgets[tc.id] = self.call_from_thread(
                            history_view.add_tool_widget, tc, True
                        )

                elif etype == AgentEventType.TOOL_START:
                    # 工具开始：重置文本占位（工具后的文本另起块）。
                    # 若 TOOL_PENDING 已经建过行，复用它并补上参数摘要；否则新建
                    # （非 DeepSeek Provider、脚本化 Provider 都不产 TOOL_PENDING）。
                    reset_text_widgets()
                    tc = event.tool_call
                    widget = tool_widgets.get(tc.id)
                    if widget is None:
                        tool_widgets[tc.id] = self.call_from_thread(
                            history_view.add_tool_widget, tc
                        )
                    else:
                        self.call_from_thread(widget.begin_running, tc)

                elif etype == AgentEventType.TOOL_RESULT:
                    tc = event.tool_call
                    res = event.tool_result
                    # pop 而不是 get：留在字典里的都是「还没定色」的行，
                    # finally 里据此把它们收尾（见下方 _settle_unfinished_tools）。
                    widget = tool_widgets.pop(tc.id, None)
                    if widget is None:
                        widget = self.call_from_thread(history_view.add_tool_widget, tc)
                    elif widget.pending:
                        # 有结果却从没进过执行态：权限拒绝 / 用户拒绝 / 规划阶段拦下
                        # 这些路径**只产 TOOL_RESULT、不产 TOOL_START**。此时参数已经
                        # 完整（就在 event.tool_call 里），补上再定色——否则标题只剩
                        # 工具名，用户看不出被拒的到底是哪一次写入。
                        self.call_from_thread(widget.begin_running, tc)
                    # 改文件类工具会在 res.diff 带上结构化差异，传给工具行渲染彩色 diff 块
                    self.call_from_thread(
                        widget.finish, res.ok, self._summarize_result(res), getattr(res, "diff", None)
                    )

                elif etype == AgentEventType.FINISHED:
                    line = self._finish_line(event.stop_reason, event.message)
                    if line:
                        self._trace_ui_message("system", line)
                        self.call_from_thread(history_view.append_system, line)

                elif etype == AgentEventType.ERROR:
                    self._trace_ui_message("error", event.message)
                    self.call_from_thread(history_view.append_error, event.message)

                elif etype == AgentEventType.NOTICE:
                    # 系统级提示（c8：上下文压缩发生等），以系统行展示，不影响正文/工具渲染。
                    self._trace_ui_message("system", event.message)
                    self.call_from_thread(history_view.append_system, event.message)

                elif etype == AgentEventType.HISTORY:
                    # 会话恢复成功（c9 /resume 交互化）：清屏并整体回放历史快照。
                    # 只发起一次 call_from_thread——清空与重画在主线程一次调用内原子完成；
                    # 同时重置本 Worker 的文本/工具占位引用（旧引用指向已被移除的组件）。
                    reset_text_widgets()
                    tool_widgets.clear()
                    self.call_from_thread(history_view.render_history, event.messages)
        finally:
            # **作用域泄漏的唯一可靠防护**，必须是 finally 的第一行（trace T42）。
            #
            # 为什么必需：Textual 的 thread worker 用**默认线程池**
            # （`Worker._run_threaded` 末行是 `run_in_executor(None, ...)`），
            # 线程会被复用。而本方法的循环体几乎全是 `call_from_thread`，
            # 应用退出竞态下它会抛 `RuntimeError`（`_notify_memory` 与
            # `_notify_skill_activation` 两处既有代码为此包了 try/except，
            # 说明这不是理论风险）；此时生成器被放弃，
            # `_run_isolated_skill` 里 `with recorder.scope(...)` 的 `__exit__`
            # 可能压根不跑。一次泄漏的 `isolated:<name>` 会污染后续复用该线程的
            # 主对话运行——概率性、极难复现。
            #
            # 为什么放在**首行**：它后面的 `call_from_thread` 自己也可能抛，
            # 放在后面就等于「出错时不复位」，防护形同虚设。
            self._recorder.bind_scope(SCOPE_MAIN)
            # **本轮最后一段 AI 正文在这里收尾**（trace F15 的补齐）。
            #
            # 为什么必须补这一次：`reset_text_widgets` 原本只在
            # 「下一轮开始 / 工具开始 / 历史回放」三个时机被调用，
            # 也就是说它**总是靠下一个动作来给上一段正文收尾**。
            # 于是一轮运行里的**最后**一段正文永远等不到那个动作，
            # 一条 `ui_message` 都不会产出——而那恰恰是用户最终看到的结论。
            #
            # 实测（P1a 端到端驱动）：跑完两轮对话，记录里 `ui_message` 只有两条
            # `user_echo`，两段 AI 正文一条都没有。后果是断言词汇「界面消息含某文本」
            # 在「最后一句话」上完全不可用，而这个缺口在界面上看不出来
            # （界面显示得好好的，只是没被记下来）。
            #
            # 位置有两条约束：
            # ① 必须在 `bind_scope(SCOPE_MAIN)` **之后**——独立模式子对话结束时
            #    线程作用域可能还是 `isolated:<name>`，而这段正文是呈现在主界面上的，
            #    该记成 `main`；
            # ② 必须在下面两个 `call_from_thread` **之前**——它们在应用退出竞态下
            #    会抛 `RuntimeError`（见上方注释），放在后面等于「出错时不记录」。
            #
            # 本调用只做内存写与一次 `emit`，不碰界面，故在 finally 里是安全的。
            reset_text_widgets()
            self.call_from_thread(self._set_streaming, False)
            # **没等到结果的工具行必须在这里收尾**，否则留在界面上一直橘着、
            # 计时器每秒还在跳，看起来程序卡在某个工具上了。
            #
            # 什么时候会有这种行：`TOOL_PENDING` 一旦播报就建了行，而它之后的
            # `TOOL_RESULT` 并不保证到达——用户按 Esc 取消、底层流出错、或本轮
            # 因取消而 break 掉剩下的调用，这几条路径都会让后面的调用一个事件都不再产。
            #
            # 放在 `_set_streaming(False)` **之后**：那是必须生效的状态复位
            # （否则输入框一直处于忙碌态），而本清理只是视觉收尾，退出竞态下
            # `call_from_thread` 抛异常时宁可丢清理也不能丢复位。
            self._settle_unfinished_tools(tool_widgets)
            # 工具调用可能在本轮流式执行中通过 mcp_add_server 改变 MCP 连接状态；
            # 收尾时刷新状态栏，让新工具数量或失败信息立即反映到界面上。
            self.call_from_thread(self._refresh_status)

    def _settle_unfinished_tools(self, tool_widgets: dict) -> None:
        """
        把本轮结束时仍未定色的工具行统一收尾为灰白的「未执行」。

        `tool_widgets` 里只会剩「建了行但没等到 TOOL_RESULT」的调用——正常拿到结果的
        在 TOOL_RESULT 分支就被 pop 掉了。因此这里的每一项都对应一次**真的没有跑**
        的调用（取消 / 流出错 / 本轮提前 break）。

        :param tool_widgets: 调用 id → ToolCallWidget，处理后被清空

        副作用：更新界面组件、清空传入的字典。异常一律吞掉——本方法在 `finally` 里
        被调用，而它只是视觉收尾，不能反过来把一次正常结束变成异常退出。
        """
        for widget in list(tool_widgets.values()):
            try:
                self.call_from_thread(widget.finish, False, "未执行（本轮已结束）")
            except Exception:
                # 应用退出竞态下 call_from_thread 会抛 RuntimeError；此时界面正在
                # 拆除，收不收尾都无意义，继续处理剩下的即可。
                pass
        tool_widgets.clear()

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
    def _interact(self, kind: str, show_fn, default, display: str = ""):
        """
        统一的「阻塞式询问主线程」机制（确认/澄清/审批共用）。

        在 Worker 线程：登记一个待决交互盒（含 Event 与默认结果），用 call_from_thread 在主线程
        弹出对应面板，然后阻塞等待，直到主线程的选择/取消处理写入结果并 set() 唤醒。
        主线程事件循环不被阻塞，UI（含其它工具行计时）照常刷新（N2 不死锁）。

        :param kind: 交互种类标识（"confirm"/"clarify"/"approve"），用于结算时映射结果
        :param show_fn: 在主线程展示面板的无参函数
        :param default: 未明确选择（如异常路径）时的默认结果
        :param display: 面板展示内容的摘要，仅用于 trace 埋点。**必须由调用方传入**——
                        展示内容全被闭进 `show_fn` 里，本方法拿不到（trace T45）
        :returns: 用户选择的结果
        """
        # `source` 记录**这次结算走的是哪条路径**，缺省 human（面板按键路径）。
        # 结算方（`_resolve_interaction`）可以覆写它，见该方法的说明。
        box = {"event": threading.Event(), "result": default, "kind": kind, "source": "human"}
        self._pending_interaction = box
        # 新面板弹出：复位提示标志，这一次面板期间可以再提示一次。
        self._busy_hint_shown = False
        self.call_from_thread(show_fn)
        box["event"].wait()
        result = box["result"]
        # 交互埋点（trace F14）：埋在**阻塞等待返回之后**（即结算时刻），
        # 一次交互恰好一条。埋在弹出时会记不到 result，而「用户选了什么」
        # 正是这条事件的全部价值。
        # source 从盒子里读而不是写死 "human"：结算可能来自面板按键，也可能来自
        # 端到端驱动设施的控制通道，二者在记录里必须能分辨。
        source = box.get("source", "human")
        self._recorder.emit_lazy(
            TraceEventType.INTERACTION,
            lambda: {
                "kind": kind,
                "display": clip(display),
                "source": source,
                "result": getattr(result, "value", result),
            },
        )
        return result

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
            display=f"{tool_call.name} {tool_call.arguments}｜{decision.reason}",
        )

    def _clarify(self, question, options):
        """Plan Mode 需求澄清（spec F12），返回所选概述；用户取消返回 None。"""
        self._clarify_options = options
        return self._interact(
            "clarify",
            lambda: self._show_clarify_panel(question, options),
            None,
            display=f"{question}｜候选：{[o.summary for o in options]}",
        )

    def _approve_plan(self, plan: str) -> bool:
        """Plan Mode 计划执行审批（spec F13），返回是否批准开始执行。"""
        return self._interact(
            "approve",
            lambda: self._show_approve_panel(plan),
            False,
            display=plan,
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

    def _resolve_interaction(self, result, source: str = "human") -> None:
        """
        在主线程结算一次交互：隐藏所有交互面板、还焦输入框、唤醒被阻塞的 Worker。

        幂等：无待决交互时直接返回，避免重复结算（如选择后又收到取消消息）。

        :param result: 结算值（四态确认枚举 / 布尔 / 澄清摘要文本 / None）
        :param source: **这次结算走的是哪条路径**，写进待决盒供 `_interact` 埋点时读取。
            取值集合：
            - `human`        面板按键路径（真人敲键，也包括测试用 Pilot 模拟的按键）
            - `driver`       端到端驱动设施经控制通道直接结算
            - `driver_forced` 驱动设施退出时的强制结算（仍属外部驱动者，单列以便审计）
            - `policy`       P1b 的固定策略应答者预留

            ⚠️ 它标注的是**结算走的哪条路径**，不是对操作者身份的断言——
            用 Pilot 模拟按键时走的是面板自身的按键路径，来源就该是 `human`。

            ⚠️ **本方法有四个调用方**，不是一个：`on_option_list_option_selected`
            的 confirm/approve/clarify 三个分支、`on_confirm_panel_cancelled`、
            `on_clarify_panel_cancelled`，以及驱动设施。前几个靠默认值 `"human"`
            兜住——这是对的，但改动本方法的人很容易以为只有一处调用方。
        """
        box = self._pending_interaction
        if box is None:
            return
        self._pending_interaction = None
        box["source"] = source
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
            # 埋点、关面板、载入三件事全在 `_settle_session` 里（它是会话面板结算的
            # 唯一入口，与 Esc 取消分支共用同一份实现）。
            self._settle_session(event.option.id)
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
