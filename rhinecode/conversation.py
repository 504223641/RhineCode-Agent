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

from rhinecode.provider.base import BaseProvider, Message, ToolCall
from rhinecode.tools.base import Tool
from rhinecode.tools.registry import ToolRegistry
from rhinecode.agent.loop import Agent
from rhinecode.agent.prompt import build_plan_prompt
from rhinecode.agent.events import AgentEvent, ClarifyOption, ConfirmDecision

# 执行前确认回调类型：给定工具调用与工具实例，返回三态决定（执行/不再询问/拒绝）。
# 由 TUI 层实现（弹确认面板），协调层只调用、不感知 TUI 细节。
ConfirmCallback = Callable[[ToolCall, Tool], ConfirmDecision]
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

    def __init__(
        self,
        provider: BaseProvider,
        provider_protocol: str,
        registry: Optional[ToolRegistry] = None,
    ):
        """
        初始化对话管理器。

        :param provider: 已实例化的 Provider，负责实际的 API 调用
        :param provider_protocol: Provider 的协议名（如 "anthropic"），
                                  用于判断是否支持思考模式与工具/循环能力
        :param registry: 工具注册中心；为 None 时不启用工具能力
        """
        self._provider = provider
        self._protocol = provider_protocol
        self._registry = registry
        self.history: list[Message] = []
        # 思考模式强度：off（关闭）/ high（高效）/ max（最强）
        self.thinking_effort: str = "off"
        # Plan Mode 开关：开启时循环只放只读工具并注入引导提示（仅工具可用 Provider 生效）
        self.plan_mode: bool = False
        # 会话级「免确认」：用户在确认面板选过「不再询问」后置真，本会话后续有副作用工具自动执行
        self._always_allow: bool = False
        # 当前运行的取消信号；每次运行重建，置位即让循环尽快停止
        self._cancel_event: threading.Event = threading.Event()

        # 三类交互回调，由 TUI 层在挂载后注入；为 None 时各自走 fail-closed/降级处理
        self.confirm_callback: Optional[ConfirmCallback] = None
        self.clarify_callback: Optional[ClarifyCallback] = None
        self.approve_plan_callback: Optional[ApprovePlanCallback] = None

        # 工具/循环能力仅在 DeepSeek 协议且提供了注册中心时启用（本章范围）
        self._tools_enabled = (provider_protocol == "deepseek" and registry is not None)
        # ReAct 循环引擎：持有长期依赖，每条普通消息调用一次 run()
        self._agent = Agent(provider, registry)

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

        # 普通消息：先追加到历史，再委托 Agent 跑循环
        self.history.append(Message(role="user", content=text))
        return self._run()

    def _run(self) -> Iterator[AgentEvent]:
        """
        构造一次 Agent 运行并返回其事件流。

        步骤：
        1. 重建取消信号（每次运行独立，避免上次的取消影响本次）。
        2. Plan Mode 时构造引导 system prompt（仅注入本次请求，不写入持久历史）。
        3. 构造 confirm 闭包：把 TUI 的三态确认回调 + 会话级免确认，封装成循环只需的 bool 接口。
        4. 调用 Agent.run，把历史、思考模式、Plan Mode、提示、三类回调与取消信号注入。

        :returns: Agent 产出的 AgentEvent 事件流

        副作用：重建 self._cancel_event；可能在执行中置位 self._always_allow。
        """
        self._cancel_event = threading.Event()
        system_prompt = build_plan_prompt() if self.plan_mode else None

        def confirm(tool_call: ToolCall, tool: Tool) -> bool:
            """
            有副作用工具执行前确认（供循环调用，返回是否执行）。

            - 会话级免确认已开启 → 直接放行
            - 否则调用 TUI 三态确认回调：
              ALLOW_ALWAYS → 置位会话级免确认并放行；ALLOW → 放行；DENY → 拒绝
            - 无确认回调（理论上不该发生）→ fail-closed 拒绝，绝不擅自执行有副作用工具
            """
            if self._always_allow:
                return True
            if self.confirm_callback is None:
                return False
            decision = self.confirm_callback(tool_call, tool)
            if decision == ConfirmDecision.ALLOW_ALWAYS:
                self._always_allow = True
                return True
            return decision == ConfirmDecision.ALLOW

        return self._agent.run(
            self.history,
            self.thinking_effort,
            self.plan_mode,
            system_prompt,
            confirm,
            self.clarify_callback,
            self.approve_plan_callback,
            self._cancel_event,
        )
