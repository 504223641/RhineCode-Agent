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
from typing import Callable, Iterator, Optional

from rhinecode.config import Config
from rhinecode.provider.base import BaseProvider, Message, ToolCall
from rhinecode.tools.base import Tool
from rhinecode.tools.registry import ToolRegistry
from rhinecode.tools.path_guard import workspace_root
from rhinecode.mcp.manager import MCPManager
from rhinecode.agent.loop import Agent
from rhinecode.agent.prompt import build_default_prompt, collect_environment
from rhinecode.agent.events import AgentEvent, ClarifyOption, ConfirmDecision
from rhinecode.permission import (
    DecisionResult,
    PermissionEngine,
    PermissionMode,
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
    ):
        """
        初始化对话管理器。

        :param provider: 已实例化的 Provider，负责实际的 API 调用
        :param config: 运行配置；提供 protocol（判断思考/工具能力）、model 与 debug_log（c5）。
                       构造结构化系统提示与环境信息、决定缓存调试日志是否开启时都要用到
        :param registry: 工具注册中心；为 None 时不启用工具能力
        :param mcp_manager: MCP 连接管理器（c7）；为 None 时 /mcp 命令与状态栏 MCP 段不展示。
                            仅用于状态查询，工具已在启动时注册进 registry，不经此引用调用。
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
        # ReAct 循环引擎：持有长期依赖，每条普通消息调用一次 run()
        self._agent = Agent(provider, registry)

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

        副作用：self.history 被重置为空列表，下次请求将不携带任何上下文。
        """
        self.history = []

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
        - 其他     → 追加用户消息到 history，委托 Agent 跑循环，返回事件流

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

        # 普通消息：先追加到历史，再委托 Agent 跑循环
        self.history.append(Message(role="user", content=text))
        return self._run()

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
        assembled = build_default_prompt(env)
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

        return self._agent.run(
            self.history,
            self.thinking_effort,
            self.plan_mode,
            assembled.stable,
            assembled.dynamic,
            self._config.model,
            debug_log_path,
            self._engine,
            ask,
            self.clarify_callback,
            self.approve_plan_callback,
            self._cancel_event,
        )
