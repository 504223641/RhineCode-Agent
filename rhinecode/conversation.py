"""
对话管理模块。

ConversationManager 是 TUI 层与下层之间的唯一协调者，职责包括：
- 维护多轮对话的完整消息历史（history）
- 解析并执行斜杠命令（/think、/clear、/exit、/plan）
- 管理思考模式三档强度（off / high / max）
- 管理 Plan Mode 开关与会话级「免确认」标志、当前运行的取消信号
- 把每条普通消息委托给 Agent（ReAct 循环引擎）执行，并把其事件流交给 TUI

c4 变化：c3 的工具编排（写死的单轮往返 _stream/_execute 等）已整体迁入 agent.loop.Agent。
本类不再亲自跑循环，只负责「协调 + 状态」：每条普通消息构造一次 Agent 运行，把回调与策略
以参数/闭包注入 Agent，返回 Agent 产出的 AgentEvent 事件流。

TUI 层调用 handle_input() 获取结果，结果类型决定后续行为：
- str：斜杠命令的反馈文本，直接作为系统提示显示
- Iterator[AgentEvent]：Agent 循环的事件流，由 TUI 的 Worker 消费并逐个渲染
"""

import threading
from pathlib import Path
from typing import Callable, Iterator, Optional

from rhinecode.config import Config
from rhinecode.provider.base import BaseProvider, Message, ToolCall
from rhinecode.tools.base import Tool
from rhinecode.tools.registry import ToolRegistry
from rhinecode.tools.path_guard import workspace_root, register_read_root
from rhinecode.mcp.manager import MCPManager
from rhinecode.context import ContextManager
from rhinecode.memory import MemoryManager
from rhinecode.agent.loop import Agent
from rhinecode.agent.prompt import build_default_prompt, collect_environment
from rhinecode.agent.prompt.texts import INIT_PROMPT
from rhinecode.agent.events import (
    AgentEvent,
    AgentEventType,
    ClarifyOption,
    ConfirmDecision,
    StopReason,
)
from rhinecode.permission import (
    Decision,
    DecisionResult,
    PermissionEngine,
    PermissionMode,
    PermissionRequest,
    Rule,
    to_request,
)

# 人在回路（HITL）确认回调类型：给定工具调用、工具实例与决策结果（含拒绝原因），
# 返回四态决定（本次/本会话/永久/拒绝）。由 TUI 层实现（弹确认面板），协调层只调用。
ConfirmCallback = Callable[[ToolCall, Tool, DecisionResult], ConfirmDecision]
# 需求澄清回调：给定问题与候选项，返回用户所选概述；返回 None 表示用户取消。
ClarifyCallback = Callable[[str, list[ClarifyOption]], Optional[str]]
# 计划审批回调：给定计划文本，返回用户是否批准开始执行。
ApprovePlanCallback = Callable[[str], bool]


class ConversationManager:
    """
    多轮对话管理器。

    持有对话历史、思考模式、Plan Mode 与会话级免确认等状态，以及三类交互回调，
    是 TUI 与下层之间的唯一协调点。TUI 层不直接调用 Provider / Agent，全部通过此类中转。
    """

    # /think 命令的三态循环顺序：关闭 → 高效 → 最强 → 关闭
    _EFFORT_CYCLE = {"off": "high", "high": "max", "max": "off"}
    # 各档位对应的中文显示名称
    _EFFORT_LABEL = {"off": "关闭", "high": "高效（high）", "max": "最强（max）"}

    # /perm 命令的三档循环顺序：默认 → 严格 → 放行 → 默认（c6）
    _PERM_CYCLE = {
        PermissionMode.DEFAULT: PermissionMode.STRICT,
        PermissionMode.STRICT: PermissionMode.PERMISSIVE,
        PermissionMode.PERMISSIVE: PermissionMode.DEFAULT,
    }
    # 各权限模式对应的中文显示名称
    _PERM_LABEL = {
        PermissionMode.STRICT: "严格（strict）",
        PermissionMode.DEFAULT: "默认（default）",
        PermissionMode.PERMISSIVE: "放行（permissive）",
    }

    def __init__(
        self,
        provider: BaseProvider,
        config: Config,
        registry: Optional[ToolRegistry] = None,
        mcp_manager: "Optional[MCPManager]" = None,
        resume_latest: bool = False,
    ):
        """
        初始化对话管理器。

        :param provider: 已实例化的 Provider，负责实际的 API 调用
        :param config: 运行配置；提供 protocol（判断思考/工具能力）、model 与 debug_log（c5）。
                       构造结构化系统提示与环境信息、决定缓存调试日志是否开启时都要用到
        :param registry: 工具注册中心；为 None 时不启用工具能力
        :param mcp_manager: MCP 连接管理器（c7）；为 None 时 /mcp 命令与状态栏 MCP 段不展示。
                            仅用于状态查询，工具已在启动时注册进 registry，不经此引用调用。
        :param resume_latest: True = `rhine --continue`：启动时恢复最近的未锁定会话（c9 F10）
        """
        self._provider = provider
        self._config = config
        # MCP 连接管理器：仅供 /mcp 命令与状态栏读取连接状态；工具走 registry，与此解耦。
        self._mcp_manager = mcp_manager
        # 协议名沿用配置里的 protocol，逻辑与此前一致（仅入参由字符串换成整份 config）。
        self._protocol = config.protocol
        self._registry = registry
        self.history: list[Message] = []
        # 思考模式强度：off（关闭）/ high（高效）/ max（最强）
        self.thinking_effort: str = "off"
        # Plan Mode 开关：开启时循环只放只读工具并注入引导提示（仅工具可用 Provider 生效）
        self.plan_mode: bool = False
        # 权限决策引擎（c6）：启动时加载三层 YAML 规则，持有权限模式与会话级规则。
        # 取代 c5 的「会话级一刀切免确认」标志——本会话放行改为按规则登记（见 _run 的 ask 闭包）。
        self._engine: PermissionEngine = PermissionEngine.load()
        # 当前运行的取消信号；每次运行重建，置位即让循环尽快停止
        self._cancel_event: threading.Event = threading.Event()

        # 三类交互回调，由 TUI 层在挂载后注入；为 None 时各自走 fail-closed/降级处理
        self.confirm_callback: Optional[ConfirmCallback] = None
        self.clarify_callback: Optional[ClarifyCallback] = None
        self.approve_plan_callback: Optional[ApprovePlanCallback] = None

        # 工具/循环能力仅在 DeepSeek 协议且提供了注册中心时启用（本章范围）
        self._tools_enabled = (self._protocol == "deepseek" and registry is not None)
        if registry is not None:
            self._install_path_filters(registry)
        # ReAct 循环引擎：持有长期依赖，每条普通消息调用一次 run()
        self._agent = Agent(provider, registry)

        # 上下文压缩器（c8）：仅在工具可用模式构造——压缩的主要对象是工具结果，
        # 且循环/工具只在该模式存在。长期持有，跨消息累积估算锚点与熔断状态。
        # 存盘目录锁定在项目根 .rhinecode/context/（与其它 .rhinecode 配置同处，可 gitignore）。
        self._context_manager: Optional[ContextManager] = None
        if self._tools_enabled:
            self._context_manager = ContextManager(
                provider,
                config.model,
                config.context_window,
                workspace_root() / ".rhinecode" / "context",
            )

        # 记忆系统编排者（c9）：所有 Provider 都构造——RHINE.md 注入与会话存档不依赖
        # 工具能力；自动笔记由 notes_enabled 门控（仅工具模式，F21）。
        user_dir = Path.home() / ".rhinecode"
        self.memory_manager = MemoryManager(
            provider,
            config.model,
            workspace_root(),
            user_dir,
            notes_enabled=self._tools_enabled,
        )
        # 用户级记忆目录加入只读白名单（F18）：模型可按索引 read_file 用户级笔记全文。
        # 注册本身无副作用（写类判定不受影响），无条件执行即可。
        register_read_root(user_dir / "memory")
        # 启动编排：加载 RHINE.md、清理过期会话、开新档或 --continue 恢复。
        # 返回的提示由 TUI 挂载时展示（无提示为 None）。
        self.startup_notice: Optional[str] = self.memory_manager.startup(
            resume_latest, self.history
        )

    def _install_path_filters(self, registry: ToolRegistry) -> None:
        """
        给会递归发现文件的只读工具注入文件级权限过滤器。

        read_file 的单文件路径在执行前已由权限管线判断；grep/glob 这类目录级工具还会在
        execute 内部发现更多文件，因此需要在真正读取或返回每个文件前再次用 Read 规则判定。
        """
        def allow_read_path(rel_path: str) -> bool:
            req = PermissionRequest(
                tool_name="read_file",
                rule_name="Read",
                specifier=rel_path,
                kind="read_path",
                is_read_only=True,
                mode=self._engine.mode,
            )
            return self._engine.decide(req).decision != Decision.DENY

        for name in ("grep_content", "glob_files"):
            tool = registry.get(name)
            setter = getattr(tool, "set_path_filter", None)
            if callable(setter):
                setter(allow_read_path)

    @property
    def permission_mode_value(self) -> Optional[str]:
        """
        当前权限模式的取值字符串（"strict"/"default"/"permissive"），供状态栏展示（c6）。

        仅在工具可用（DeepSeek 工具模式）时有意义——其它 Provider 没有受控工具，
        权限模式不参与任何判断，故返回 None，让状态栏不展示这一段，避免误导。

        :returns: 模式值字符串；工具不可用时返回 None
        """
        if not self._tools_enabled:
            return None
        return self._engine.mode.value

    def clear(self) -> None:
        """
        清空对话历史。

        副作用：self.history 被重置为空列表，下次请求将不携带任何上下文；
        同时重置上下文压缩器的会话级状态（估算锚点、熔断计数、已存盘幂等集合，c8 F15）——
        历史清空后旧锚点与熔断态都不再适用，必须一并归零。
        c9：会话存档随之「开新档」——旧存档保留不动、后续消息写入新文件（F8），
        笔记高水位一并归零。
        """
        self.history = []
        if self._context_manager is not None:
            self._context_manager.reset()
        self.memory_manager.on_clear()

    def request_cancel(self) -> None:
        """
        请求取消当前正在运行的 Agent 循环。

        由 TUI 在用户按取消键时调用：置位当前运行的取消信号，循环会在安全点检测并尽快停止
        （spec F9）。无运行时调用也安全（仅置位一个会在下次运行被重建的 Event）。
        """
        self._cancel_event.set()

    def handle_input(self, text: str) -> "str | Iterator[AgentEvent]":
        """
        处理用户输入，根据内容类型分发到不同处理路径。

        处理规则：
        - "/exit"  → 抛出 SystemExit，由 TUI 层捕获后调用 app.exit()
        - "/clear" → 清空 history，返回确认文本
        - "/think" → 三态循环切换 thinking_effort（off→high→max→off）；不支持的 Provider 返回提示
        - "/plan"  → 切换 Plan Mode；仅工具可用 Provider 生效，否则返回不支持提示
        - "/perm"  → 三档循环切换权限模式（默认→严格→放行）；仅工具可用 Provider 生效（c6）
        - "/memory" → 记忆系统只读报告（c9）
        - "/resume" → 无参列出最近会话；带编号/ID 返回载入事件流（c9）
        - "/init"  → 内置指令走 Agent Loop 生成 RHINE.md；仅工具可用 Provider 生效（c9）
        - 其他     → 追加用户消息到 history（并写入会话存档），委托 Agent 跑循环，返回事件流

        :param text: 用户原始输入（含前后空白）
        :returns: str（斜杠命令反馈）或 Iterator[AgentEvent]（循环事件流）
        :raises SystemExit: 用户输入 "/exit" 时抛出

        副作用：
        - "/clear" 会清空 self.history
        - "/think" 会修改 self.thinking_effort
        - "/plan" 会修改 self.plan_mode
        - 普通消息会向 self.history 追加用户消息（assistant/tool 消息由 Agent 在循环中追加）
        """
        text = text.strip()

        if text == "/exit":
            raise SystemExit

        if text == "/clear":
            self.clear()
            return "对话历史已清空"

        if text == "/think":
            # Anthropic 和 DeepSeek 均支持思考模式，OpenAI 原生协议不支持
            if self._protocol not in ("anthropic", "deepseek"):
                return "当前 Provider 不支持思考模式"
            self.thinking_effort = self._EFFORT_CYCLE[self.thinking_effort]
            label = self._EFFORT_LABEL[self.thinking_effort]
            return f"思考模式：{label}"

        if text == "/plan":
            # Plan Mode 依赖工具能力，仅在工具可用的 Provider（DeepSeek + 注册中心）下生效
            if not self._tools_enabled:
                return "当前 Provider 不支持计划模式"
            self.plan_mode = not self.plan_mode
            return "计划模式：开启" if self.plan_mode else "计划模式：关闭"

        if text == "/perm":
            # 权限模式三档循环切换（c6）；仅在工具可用的 Provider 下有意义（无工具则无可控对象）
            if not self._tools_enabled:
                return "当前 Provider 不支持权限模式"
            self._engine.set_mode(self._PERM_CYCLE[self._engine.mode])
            label = self._PERM_LABEL[self._engine.mode]
            return f"权限模式：{label}"

        if text == "/mcp":
            # 展示 MCP 各 Server 的连接状态、工具数与失败原因（c7 F16）。
            # 与 /perm 等不同，本命令纯只读、不改任何状态，也不受工具能力开关限制。
            if self._mcp_manager is None:
                return "未启用 MCP（未配置任何 MCP Server）"
            return self._mcp_manager.status_report()

        if text == "/context":
            # 只读展示当前上下文用量（c8 F16）；纯读、不改状态，同步返回字符串即可
            # （不涉及阻塞的 LLM 调用，故不必走 Worker）。
            if not self._tools_enabled or self._context_manager is None:
                return "当前 Provider 不支持上下文管理"
            return self._context_manager.usage_report(self.history)

        if text == "/compact":
            # 手动触发重量压缩（c8 F14）。摘要是阻塞的 LLM 调用，若在此同步执行会卡死 UI 线程，
            # 故返回事件流生成器交给 TUI 的后台 Worker 消费（与普通消息同一执行路径）。
            if not self._tools_enabled or self._context_manager is None:
                return "当前 Provider 不支持上下文管理"
            return self._manual_compact()

        if text == "/memory":
            # 记忆系统只读报告（c9 F19）：纯读不改状态，同步返回即可。
            return self.memory_manager.memory_report()

        if text == "/resume" or text.startswith("/resume "):
            # 会话恢复（c9 F9）。无参 → 同步返回列表；带参 → 事件流走 Worker——
            # 载入后可能触发 C8 压缩（阻塞的摘要 LLM 调用），不能卡 UI 主线程。
            key = text[len("/resume"):].strip()
            if not key:
                return self.memory_manager.resume_list()
            return self._resume_stream(key)

        if text == "/init":
            # /init（c9 F25）：把内置指令作为一条普通 user 消息交给 Agent Loop——
            # 探索用只读工具、写 RHINE.md 走 write_file 的完整权限管线（人在回路确认）。
            if not self._tools_enabled:
                return "当前 Provider 不支持 /init（需要 DeepSeek 工具模式）"
            init_msg = Message(role="user", content=INIT_PROMPT)
            self.history.append(init_msg)
            self.memory_manager.record_message(init_msg)
            return self._run()

        # 普通消息：先追加到历史（并写入会话存档，c9 F6），再委托 Agent 跑循环
        user_msg = Message(role="user", content=text)
        self.history.append(user_msg)
        self.memory_manager.record_message(user_msg)
        return self._run()

    def _manual_compact(self) -> Iterator[AgentEvent]:
        """
        /compact 的事件流：在后台 Worker 里执行一次手动压缩并反馈结果（c8 F14/F17）。

        与普通消息不同，这里**不**向 history 追加任何 user 消息——/compact 是命令而非对话；
        只调 ContextManager.manual_compact（可能原地重构 history），把结果作为 NOTICE 展示，
        再产出一个 FINISHED(COMPLETED) 让 TUI 收尾（自然完成，不额外打扰）。

        :returns: 仅含一条 NOTICE 与一条 FINISHED 的事件流

        副作用：可能发起摘要 LLM 调用并原地重构 self.history。
        """
        notice = self._context_manager.manual_compact(self.history)
        yield AgentEvent(type=AgentEventType.NOTICE, message=notice.message)
        yield AgentEvent(type=AgentEventType.FINISHED, stop_reason=StopReason.COMPLETED)

    def _resume_stream(self, key: str) -> Iterator[AgentEvent]:
        """
        /resume <编号或ID> 的事件流：在后台 Worker 里接管并载入目标会话（c9 F9/F11）。

        步骤：
        1. memory_manager.resume_into：锁检查 → 接管 → 容错载入 → 原地替换 history；
        2. 成功且有上下文压缩器时：先 reset()（旧估算锚点对应旧历史，换历史不复位会
           严重低估用量，c9 plan 技术决策），再跑一次 before_request——载入的历史若已
           逼近窗口上限就地压一次（F11③），不逼近则无动作；
        3. 结果以 NOTICE 反馈，FINISHED(COMPLETED) 收尾。

        :returns: 事件流（NOTICE 若干 + FINISHED）

        副作用：可能切换会话锁、改写 self.history、发起摘要 LLM 调用。
        """
        ok, message = self.memory_manager.resume_into(key, self.history)
        if ok and self._context_manager is not None:
            self._context_manager.reset()
            for notice in self._context_manager.before_request(self.history):
                yield AgentEvent(type=AgentEventType.NOTICE, message=notice.message)
        yield AgentEvent(type=AgentEventType.NOTICE, message=message)
        yield AgentEvent(type=AgentEventType.FINISHED, stop_reason=StopReason.COMPLETED)

    def mcp_status_line(self) -> "str | None":
        """
        返回底部状态栏用的 MCP 一行摘要（c7 F15）。

        :returns: 形如「MCP：已连接 2/3 · 工具 11」；未启用 MCP 或无 Server 时返回 None
                  （None 时状态栏不展示 MCP 段，避免误导）。

        副作用：无（仅读取 manager 状态）。
        """
        if self._mcp_manager is None:
            return None
        return self._mcp_manager.status_line()

    def context_status_line(self) -> "tuple[str, bool] | None":
        """
        返回底部状态栏用的上下文用量摘要（c8）。

        :returns: (文本, 是否高亮) 如 ("上下文：19% · 12.3K/64K", False)；
                  工具不可用的 Provider（无 ContextManager）返回 None，此时状态栏
                  不展示该段，语义与 mcp_status_line 的「None 即隐藏」一致。

        副作用：无（仅只读估算当前历史用量）。
        """
        if self._context_manager is None:
            return None
        return self._context_manager.status_line(self.history)

    def _run(self) -> Iterator[AgentEvent]:
        """
        构造一次 Agent 运行并返回其事件流。

        步骤：
        1. 重建取消信号（每次运行独立，避免上次的取消影响本次）。
        2. 采集环境信息并拼装结构化系统提示，分出 stable（可缓存）与 dynamic（动态）两段（c5）。
        3. 计算缓存调试日志路径（debug_log 关闭时为 None）。
        4. 构造 ask 闭包：把 TUI 的四态确认回调封装成循环只需的 bool 接口，并在用户选
           「本会话/永久放行」时把对应 allow 规则登记进引擎（会话级 / 写本地配置，c6 F6）。
        5. 调用 Agent.run，注入历史、思考模式、Plan Mode、系统提示两段、日志路径、权限引擎、
           四类回调与取消信号。

        :returns: Agent 产出的 AgentEvent 事件流

        副作用：重建 self._cancel_event；ask 闭包可能向引擎登记会话规则或写本地配置文件。
        """
        self._cancel_event = threading.Event()

        # 结构化系统提示（c5）：以项目根为工作目录采集环境信息，拼装出稳定/动态两段。
        # stable 逐轮不变 → 走 system 参数命中缓存；dynamic（环境信息）由循环注入 <system-reminder>。
        # Plan Mode 的引导不再在此构造，改由循环按轮节奏注入（见 loop.run / reminders）。
        project_root = str(workspace_root())
        env = collect_environment(self._config, project_root)
        # c9：把 RHINE.md 拼接结果与记忆索引填进 110/130 槽位（两者都可能为空串，
        # 为空时槽位整体跳过，输出与 c8 一致）。索引现读现截断，笔记线程会话中途
        # 更新后，下一条消息就能看到新索引。
        assembled = build_default_prompt(
            env,
            custom_instructions=self.memory_manager.custom_instructions(),
            memory_index=self.memory_manager.memory_index(),
        )
        # 一次性动态提醒（c9：恢复会话的时间跨度提醒）：并入本次 dynamic，取走即清，
        # 不进持久历史、不被存档（它是「此刻的环境事实」）。
        dynamic = assembled.dynamic
        pending = self.memory_manager.consume_pending_notice()
        if pending:
            dynamic = f"{dynamic}\n\n{pending}" if dynamic else pending
        # debug_log 开启时把缓存日志写到项目根下的固定文件，否则传 None 关闭日志。
        debug_log_path = (
            str(workspace_root() / ".rhinecode_debug.log") if self._config.debug_log else None
        )

        def ask(tool_call: ToolCall, tool: Tool, decision: DecisionResult) -> bool:
            """
            人在回路确认（仅当决策为 ASK 时由循环调用，返回是否执行）。

            - 无确认回调（理论上不该发生）→ fail-closed 拒绝，绝不擅自执行有副作用工具。
            - 否则调用 TUI 四态确认回调，按所选处理：
              本次（ALLOW）→ 放行本次；
              本会话（ALLOW_SESSION）→ 为「该工具 + 本次目标」登记一条会话级 allow 规则后放行，
                  本会话内后续相同调用经引擎直接 ALLOW，不再弹面板；
              永久（ALLOW_PERMANENT）→ 把同样的 allow 规则写入本地级配置（重启仍生效）后放行；
              拒绝（DENY）→ 不执行。

            规则的工具名与匹配模式来自 adapter 的规范化结果（与引擎判断口径一致），
            specifier 直接作为模式（精确匹配本次目标，偏保守、最小授权）。
            """
            if self.confirm_callback is None:
                return False
            choice = self.confirm_callback(tool_call, tool, decision)
            if choice == ConfirmDecision.DENY:
                return False
            if choice == ConfirmDecision.ALLOW:
                return True
            # 本会话 / 永久：构造与本次调用同口径的 allow 规则。
            req = to_request(tool, tool_call.arguments, self._engine.mode)
            rule_string = f"{req.rule_name}({req.specifier})" if req.specifier else req.rule_name
            if choice == ConfirmDecision.ALLOW_SESSION:
                self._engine.add_session_rule(
                    Rule(effect="allow", tool=req.rule_name, pattern=req.specifier, source="session")
                )
                return True
            if choice == ConfirmDecision.ALLOW_PERMANENT:
                if not self._engine.persist_local_rule(rule_string):
                    self._engine.add_session_rule(
                        Rule(effect="allow", tool=req.rule_name, pattern=req.specifier, source="session")
                    )
                return True
            return False

        events = self._agent.run(
            self.history,
            self.thinking_effort,
            self.plan_mode,
            assembled.stable,
            dynamic,
            self._config.model,
            debug_log_path,
            self._engine,
            ask,
            self.clarify_callback,
            self.approve_plan_callback,
            self._cancel_event,
            self._context_manager,
            self.memory_manager.record_message,
        )
        return self._wrap_events(events)

    def _wrap_events(self, events: Iterator[AgentEvent]) -> Iterator[AgentEvent]:
        """
        Agent 事件流的包装生成器（c9 自然停止钩子，单点接入）。

        逐个透传事件；看到 FINISHED 且停止原因为 COMPLETED（自然完成）时，先触发
        MemoryManager 的异步笔记更新再透传——钩子只是「起一个 daemon 线程」，
        本身不阻塞事件流。其它停止原因（取消/出错/迭代上限）不触发笔记：
        非自然结束的对话大概率不完整，不值得沉淀。

        :param events: Agent 产出的原始事件流
        :returns: 语义完全相同的事件流（仅多了钩子副作用）
        """
        for event in events:
            if (
                event.type == AgentEventType.FINISHED
                and event.stop_reason == StopReason.COMPLETED
            ):
                self.memory_manager.on_natural_stop(self.history)
            yield event
