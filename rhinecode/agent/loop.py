"""
Agent Loop（ReAct 自主循环）引擎，c4 的核心（spec F1/F2/F4/F5/F11/F13/F14）。

它把 c3 写死的「单轮往返」升级为真正的循环：
    一轮 = 调模型（带工具）→ 收齐文本与工具调用 → 若有工具则执行并回灌结果 → 进入下一轮
循环持续到模型本轮不再要工具（自然完成），或命中某个停止条件（迭代上限 / 用户取消 /
连续未知工具 / 流出错）。

设计原则：
- 与界面解耦（N4）：循环只产出 AgentEvent 单向事件流；需要用户决定的交互（确认 / 澄清 / 审批）
  通过传入的阻塞回调完成，循环不感知 TUI。
- 不阻塞 UI（N2）：本类预期运行在 TUI 的 Worker 线程；回调内部负责跨线程弹面板与等待。
- 不崩溃（N1）：工具执行异常统一兜底为 ToolResult(ok=False)，流错误转 FINISHED(STREAM_ERROR)。
- 历史一致（N5）：每轮按协议顺序向传入的 history 追加 assistant(含 tool_calls) 与各 tool 结果。

Plan Mode 两段式（F13）在循环内体现为每轮局部状态 execution_phase：
- 规划阶段（plan_mode 且未获批）：只暴露只读工具 + ask_user/present_plan 两个特殊工具。
- 用户通过 present_plan 批准后，execution_phase 置真，本轮后续迭代放开全部工具开始执行；
  但写文件、改文件、运行命令等副作用工具仍逐个确认，除非用户选择本会话免确认。
  Plan Mode 开关本身仍由上层维持（下一条用户消息会重新从规划阶段开始）。
"""

import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import TYPE_CHECKING, Callable, Iterator, Optional

if TYPE_CHECKING:
    # 仅类型检查期导入，运行期用字符串注解——避免与 context 层产生任何潜在导入顺序问题。
    from rhinecode.context import ContextManager

from rhinecode.provider.base import BaseProvider, Message, ToolCall
from rhinecode.tools.base import Tool, ToolResult
from rhinecode.tools.registry import ToolRegistry
from rhinecode.agent.collector import StreamCollector
from rhinecode.agent.events import (
    AgentEvent,
    AgentEventType,
    ClarifyOption,
    StopReason,
)
from rhinecode.agent.plan_tools import ASK_USER, PRESENT_PLAN, plan_schemas
from rhinecode.agent.prompt import build_system_reminder, plan_toggle_instruction
from rhinecode.agent.cache_log import log_cache_usage
from rhinecode.permission import Decision, DecisionResult, PermissionEngine, to_request

# 迭代上限：兜底安全网，任何情况下循环都不会超过这么多轮（spec N3）。
MAX_ITERATIONS = 25
# 连续「整轮都是未知工具」达到该次数即停止，避免模型在不存在的工具上空转（spec F2）。
MAX_CONSECUTIVE_UNKNOWN = 3

# 回调类型别名（由 ConversationManager 注入；均预期在 Worker 线程被调用）。
# ask：当决策管线判定为 ASK（交人工确认）时调用，弹 HITL 面板。上层闭包已封装四选项
# （本次/本会话/永久/拒绝）与对应的会话规则登记 / 永久落盘，循环只看返回的 bool（是否执行）。
AskFn = Callable[[ToolCall, Tool, DecisionResult], bool]
# clarify：弹澄清面板，返回用户所选概述；返回 None 表示用户取消
ClarifyFn = Callable[[str, list[ClarifyOption]], Optional[str]]
# approve_plan：弹「是否开始执行」审批，返回是否批准
ApprovePlanFn = Callable[[str], bool]


def _invalid_args_result(tc: ToolCall) -> ToolResult:
    return ToolResult(
        ok=False,
        output=f"工具 {tc.name} 的参数必须是 JSON 对象，请检查格式后重试。",
        summary="参数格式错误",
    )


class _RoundContext:
    """
    单轮工具执行的上下文，用于把 _execute 内部发生的「状态变化」回传给主循环。

    （用普通对象而非返回值，是因为 _execute 是生成器，需边 yield 事件边记录状态。）

    :param cancelled: 本轮交互中用户取消（如澄清面板按 Esc）
    :param approved: 本轮 present_plan 获得用户批准（据此切换到执行阶段）
    :param plan_rejected: 本轮 present_plan 被用户拒绝，主循环应立即停止
    :param known_count: 本轮命中的已知工具（含特殊工具）数量
    :param unknown_count: 本轮命中的未知工具数量
    """

    def __init__(self) -> None:
        self.cancelled: bool = False
        self.approved: bool = False
        self.plan_rejected: bool = False
        self.known_count: int = 0
        self.unknown_count: int = 0


class Agent:
    """
    ReAct 循环引擎。构造时持有长期依赖（provider、工具注册中心），每条用户消息调用一次 run()。

    run() 不持有跨消息状态：迭代计数、执行阶段、连续未知计数等都是 run() 内的局部变量；
    会话级状态（如「免确认」）由上层封进 confirm 闭包，Plan Mode 开关由上层按消息传入。
    """

    def __init__(self, provider: BaseProvider, registry: Optional[ToolRegistry]):
        """
        :param provider: 已实例化的 Provider，负责实际 API 调用
        :param registry: 工具注册中心；为 None 时不向模型暴露任何工具（退化为纯对话循环）
        """
        self._provider = provider
        self._registry = registry

    # ------------------------------------------------------------------ #
    # 工具集策略
    # ------------------------------------------------------------------ #
    def _schema_for(self, plan_mode: bool, execution_phase: bool) -> Optional[list[dict]]:
        """
        计算本轮要发给模型的工具 schema 列表。

        - 无注册中心 → None（纯对话，不带工具）
        - Plan Mode 且未获批执行（规划阶段）→ 只读工具 + ask_user/present_plan 特殊工具
        - 其余（普通模式，或 Plan Mode 已获批的执行阶段）→ 全部工具

        :param plan_mode: 是否处于 Plan Mode
        :param execution_phase: Plan Mode 下是否已获批进入执行阶段
        :returns: 工具 schema 列表，或 None
        """
        if self._registry is None:
            return None
        if plan_mode and not execution_phase:
            return self._registry.readonly_schemas() + plan_schemas()
        return self._registry.schemas()

    # ------------------------------------------------------------------ #
    # 主循环
    # ------------------------------------------------------------------ #
    def run(
        self,
        history: list[Message],
        thinking_effort: str,
        plan_mode: bool,
        stable: str,
        dynamic: str,
        model: str,
        debug_log_path: Optional[str],
        engine: PermissionEngine,
        ask: AskFn,
        clarify: Optional[ClarifyFn],
        approve_plan: Optional[ApprovePlanFn],
        cancel_event: threading.Event,
        context_manager: "Optional[ContextManager]" = None,
    ) -> Iterator[AgentEvent]:
        """
        跑一次完整的 ReAct 循环，逐个产出 AgentEvent。

        :param history: 对话历史（同一引用）；循环会向其追加 assistant 与 tool 结果消息（N5）
        :param thinking_effort: 思考模式强度（透传给 provider）
        :param plan_mode: 是否处于 Plan Mode
        :param stable: 稳定系统提示（可缓存通道）；逐轮以 system 参数传给 provider，内容不变以命中缓存
        :param dynamic: 动态内容（环境信息等）；每轮与会话级开关提醒合并进 <system-reminder> 注入
        :param model: 当前模型名，仅用于缓存调试日志记录
        :param debug_log_path: 缓存调试日志文件路径；为 None 表示关闭日志（c5 F10）
        :param engine: 权限决策引擎（c6）；每个工具执行前调 engine.decide 算放行/拒绝/问
        :param ask: 人工确认回调；仅当决策为 ASK 时调用，返回是否执行（已封装四选项与规则登记）
        :param clarify: 需求澄清回调（ask_user 用），可为 None
        :param approve_plan: 计划审批回调（present_plan 用），可为 None
        :param cancel_event: 取消信号；循环在安全点轮询，置位即尽快停止
        :param context_manager: 上下文压缩器（c8）；为 None（非 DeepSeek 工具模式）时不压缩，
                                行为与 c8 之前完全一致。非 None 时每轮请求前跑两层压缩、
                                每轮拿到 usage 后更新估算锚点。
        :returns: AgentEvent 迭代器；末尾必为一个 FINISHED 事件

        副作用：向 history 追加消息；通过 provider 发起多次网络请求；通过回调与用户交互；
                若 debug_log_path 非空，每轮把缓存用量追加写入该文件。
        """
        execution_phase = False       # Plan Mode 下是否已获批执行
        consecutive_unknown = 0       # 连续「整轮仅未知工具」的次数

        for iteration in range(1, MAX_ITERATIONS + 1):
            # 安全点 1：进入新一轮前检查取消
            if cancel_event.is_set():
                yield AgentEvent(type=AgentEventType.FINISHED, stop_reason=StopReason.USER_CANCELLED)
                return

            yield AgentEvent(type=AgentEventType.PROGRESS, iteration=iteration)

            # 上下文压缩（c8 F3）：每次 API 请求前先跑两层压缩（先第一层存盘、再按需第二层摘要），
            # 把可能过长的历史压回 token 预算内。压缩可能原地修改 history（改写工具结果 / 重构列表）。
            # 每条压缩动作都以 NOTICE 事件反馈给 TUI（F17）。context_manager 为 None 时整段跳过，
            # 保持 c8 之前的行为不变（N1 低侵入）。
            if context_manager is not None:
                for notice in context_manager.before_request(history):
                    yield AgentEvent(type=AgentEventType.NOTICE, message=notice.message)

            tools = self._schema_for(plan_mode, execution_phase)

            # 组装请求消息（c5 分通道）：
            # - 稳定系统提示走 stream_chat 的 system 参数（可缓存前缀），不进 messages；
            # - 动态内容（dynamic 环境信息）+ 会话级开关（Plan Mode）按轮节奏的提醒，
            #   合并进一条 <system-reminder> 系统消息，追加到历史「末尾」。
            #   放末尾而非中间，是为了不破坏历史本身的前缀缓存——变化的提醒只成不缓存的尾巴。
            # Plan Mode 提醒仅在「规划阶段」（plan_mode 且尚未获批执行）注入。
            toggle = plan_toggle_instruction(
                iteration, active=plan_mode and not execution_phase
            )
            reminder = build_system_reminder(dynamic, toggle)
            # 记录本轮「纯历史消息条数」作为估算锚点长度（c8）：必须在 append reminder 之前、
            # 用 history 的长度而非 req_messages——reminder 是每轮临时拼的尾巴，不属于持久历史，
            # 而估算锚点覆盖的正是 usage 对应的这段纯历史。
            sent_len = len(history)
            req_messages: list[Message] = list(history)
            if reminder:
                req_messages.append(Message(role="system", content=reminder))

            # ---------- 双路收集本轮流式响应 ----------
            collector = StreamCollector()
            stream_error: Optional[str] = None
            for chunk in self._provider.stream_chat(
                req_messages, thinking_effort, tools=tools, system=stable
            ):
                if chunk.type == "error":
                    stream_error = chunk.content
                    break
                ev = collector.feed(chunk)
                if ev is not None:
                    yield ev

            # 缓存命中调试日志（c5 F10）：本轮拿到用量就记一行，用于验证稳定前缀是否命中缓存。
            # 即使随后判定流出错也照常记录——usage 可能已先于错误到达，记录它有助于排查。
            if debug_log_path and collector.usage is not None:
                log_cache_usage(collector.usage, model, debug_log_path)

            # 更新估算锚点（c8）：本轮 usage.prompt_tokens 是 API 亲口给出的精确输入 token 数，
            # 覆盖 sent_len 条纯历史消息；后续请求只需对锚点之后的新增消息做字符估算。
            if context_manager is not None and collector.usage is not None:
                context_manager.record_usage(collector.usage, sent_len)

            # 停止条件：流出错
            if stream_error is not None:
                yield AgentEvent(type=AgentEventType.ERROR, message=stream_error)
                yield AgentEvent(type=AgentEventType.FINISHED, stop_reason=StopReason.STREAM_ERROR)
                return

            text = collector.text
            tool_calls = collector.tool_calls

            # 停止条件：模型本轮不要工具 → 自然完成（也是纯对话/无工具的正常路径，F14）
            if not tool_calls:
                if text:
                    history.append(Message(role="assistant", content=text))
                yield AgentEvent(type=AgentEventType.FINISHED, stop_reason=StopReason.COMPLETED)
                return

            # 有工具调用：先把 assistant(含 tool_calls) 追加历史（N5）
            history.append(Message(role="assistant", content=text, tool_calls=tool_calls))

            # ---------- 执行工具并回灌 ----------
            results: dict[str, ToolResult] = {}
            ctx = _RoundContext()
            yield from self._execute(
                tool_calls, results, ctx,
                engine, ask, clarify, approve_plan,
                cancel_event,
            )

            # 按原始顺序把每个工具结果作为 role="tool" 消息回灌历史
            for tc in tool_calls:
                res = results.get(tc.id)
                output = res.output if res is not None else "工具未产生结果"
                history.append(Message(role="tool", tool_call_id=tc.id, content=output))

            # 计划获批 → 本轮后续迭代进入执行阶段
            if ctx.approved:
                execution_phase = True

            # 计划被拒绝 → 终止当前 Agent 回合，不再把结果交回模型继续下一轮
            if ctx.plan_rejected:
                yield AgentEvent(type=AgentEventType.FINISHED, stop_reason=StopReason.PLAN_REJECTED)
                return

            # 停止条件：澄清面板被用户取消
            if ctx.cancelled:
                yield AgentEvent(type=AgentEventType.FINISHED, stop_reason=StopReason.USER_CANCELLED)
                return

            # 连续未知工具统计：本轮「有未知且无已知」算一次连续，否则清零
            if ctx.unknown_count > 0 and ctx.known_count == 0:
                consecutive_unknown += 1
            else:
                consecutive_unknown = 0
            if consecutive_unknown >= MAX_CONSECUTIVE_UNKNOWN:
                yield AgentEvent(
                    type=AgentEventType.FINISHED,
                    stop_reason=StopReason.UNKNOWN_TOOL,
                    message=f"连续 {consecutive_unknown} 轮调用未知工具，已停止。",
                )
                return

            # 安全点 2：本轮结束后检查取消
            if cancel_event.is_set():
                yield AgentEvent(type=AgentEventType.FINISHED, stop_reason=StopReason.USER_CANCELLED)
                return

        # 走完上限仍未结束 → 兜底停止（N3）
        yield AgentEvent(
            type=AgentEventType.FINISHED,
            stop_reason=StopReason.MAX_ITERATIONS,
            message=f"已达到迭代上限（{MAX_ITERATIONS} 轮），自动停止。",
        )

    # ------------------------------------------------------------------ #
    # 工具执行：特殊工具路由 + 只读并发 + 副作用串行
    # ------------------------------------------------------------------ #
    def _execute(
        self,
        tool_calls: list[ToolCall],
        results: dict[str, ToolResult],
        ctx: _RoundContext,
        engine: PermissionEngine,
        ask: AskFn,
        clarify: Optional[ClarifyFn],
        approve_plan: Optional[ApprovePlanFn],
        cancel_event: threading.Event,
    ) -> Iterator[AgentEvent]:
        """
        执行本轮所有工具调用：先做权限「决策预扫」，再按类别分流执行（c6）。

        决策预扫（每个已知工具调用执行前由代码算放行/拒绝/问，spec F1）：
        - 用 adapter.to_request 把调用规范化，调 engine.decide 得到 DecisionResult。
        - ALLOW + 只读 → 进并发桶（无需确认）。
        - ALLOW + 副作用 → 进串行桶，直接执行（命中 allow 规则即免确认，spec AC4）。
        - DENY → 进串行桶，产出结构化拒绝结果、不执行（不终止循环，spec F8）。
        - ASK → 进串行桶，执行前调 ask 回调弹 HITL 面板。

        分流后：
        - 特殊工具（ask_user / present_plan）：串行，路由到 clarify / approve_plan。
        - 只读且 ALLOW：并发执行（互不冲突，提升效率）。
        - 其余（副作用 / 被拒 / 待确认 / 未知 / 参数错误）：串行执行。

        所有结果写入 results（键为 tool_call.id）；状态变化记录到 ctx 供主循环判断。

        :param tool_calls: 本轮工具调用
        :param results: 输出参数，写入 id → ToolResult
        :param ctx: 本轮上下文，记录 cancelled/approved/known/unknown
        :param engine: 权限决策引擎（决策预扫用）
        :param ask: 人工确认回调（仅 ASK 时调用）
        :param clarify/approve_plan: 特殊工具的交互回调
        :param cancel_event: 取消信号，串行执行前检查

        副作用：实际执行工具（可能读写文件、跑命令）；通过回调与用户交互。
        """
        special: list[ToolCall] = []
        readonly: list[tuple[ToolCall, Tool]] = []
        # 串行桶元素：(调用, 工具或None, 决策或None)。
        # tool=None → 未知工具；decision=None → 参数解析失败（两者都不进引擎）。
        serial: list[tuple[ToolCall, Optional[Tool], Optional[DecisionResult]]] = []

        # 分流 + 决策预扫
        for tc in tool_calls:
            if tc.name in (ASK_USER, PRESENT_PLAN):
                special.append(tc)
                ctx.known_count += 1
                continue
            tool = self._registry.get(tc.name) if self._registry else None
            if tool is None:
                serial.append((tc, None, None))
                ctx.unknown_count += 1
                continue
            ctx.known_count += 1
            if not isinstance(tc.arguments, dict):
                # 参数解析失败或非对象 JSON：不进引擎，留待串行路径产出结构化错误。
                serial.append((tc, tool, None))
                continue
            # 权限决策：规范化 → engine.decide。
            decision = engine.decide(to_request(tool, tc.arguments, engine.mode))
            if decision.decision == Decision.ALLOW and tool.read_only:
                readonly.append((tc, tool))
            else:
                serial.append((tc, tool, decision))

        # 只读且放行：并发
        if readonly:
            yield from self._run_readonly_concurrent(readonly, results)

        # 特殊工具：串行（需用户交互）
        for tc in special:
            if cancel_event.is_set():
                ctx.cancelled = True
                break
            yield from self._run_special(tc, results, ctx, clarify, approve_plan)
            if ctx.cancelled or ctx.plan_rejected:
                break

        # 其余：串行
        for tc, tool, decision in serial:
            if ctx.cancelled or ctx.plan_rejected:
                break
            if cancel_event.is_set():
                ctx.cancelled = True
                break
            yield from self._run_one_serial(tc, tool, decision, results, ask)

    def _run_special(
        self,
        tc: ToolCall,
        results: dict[str, ToolResult],
        ctx: _RoundContext,
        clarify: Optional[ClarifyFn],
        approve_plan: Optional[ApprovePlanFn],
    ) -> Iterator[AgentEvent]:
        """
        执行一个特殊交互工具（ask_user / present_plan），路由到对应回调。

        ask_user：解析 question/options → 调 clarify 弹澄清面板 → 所选概述作为结果；
                  用户取消（返回 None）→ 置 ctx.cancelled，结果记为「用户取消」。
        present_plan：解析 plan → 调 approve_plan 弹审批 → 批准则置 ctx.approved。

        回调缺失或参数非法时返回结构化错误，保证不崩溃（N1）。
        """
        yield AgentEvent(type=AgentEventType.TOOL_START, tool_call=tc)

        if not isinstance(tc.arguments, dict):
            res = _invalid_args_result(tc)
            results[tc.id] = res
            yield AgentEvent(type=AgentEventType.TOOL_RESULT, tool_call=tc, tool_result=res)
            return

        if tc.name == ASK_USER:
            if clarify is None:
                res = ToolResult(ok=False, output="当前不支持向用户澄清（缺少澄清回调）。")
            else:
                question = str(tc.arguments.get("question", "")).strip()
                options = self._parse_options(tc.arguments.get("options"))
                if not options:
                    res = ToolResult(ok=False, output="ask_user 需要至少一个候选项 options。")
                else:
                    chosen = clarify(question, options)
                    if chosen is None:
                        ctx.cancelled = True
                        res = ToolResult(ok=False, output="用户取消了澄清。", summary="用户取消")
                    else:
                        res = ToolResult(ok=True, output=f"用户选择：{chosen}", summary=f"用户选择：{chosen}")
        else:  # PRESENT_PLAN
            if approve_plan is None:
                res = ToolResult(ok=False, output="当前不支持计划审批（缺少审批回调）。")
            else:
                plan = str(tc.arguments.get("plan", "")).strip()
                if plan:
                    yield AgentEvent(type=AgentEventType.TEXT, text=plan)
                approved = approve_plan(plan)
                if approved:
                    ctx.approved = True
                    res = ToolResult(ok=True, output="用户已批准计划，开始执行。", summary="已批准，开始执行")
                else:
                    ctx.plan_rejected = True
                    res = ToolResult(
                        ok=True,
                        output="用户暂未批准计划，已停止本次执行。",
                        summary="未批准",
                    )

        results[tc.id] = res
        yield AgentEvent(type=AgentEventType.TOOL_RESULT, tool_call=tc, tool_result=res)

    @staticmethod
    def _parse_options(raw) -> list[ClarifyOption]:
        """
        把模型给出的 options 原始数据（list[dict]）解析为 list[ClarifyOption]。

        宽松解析：跳过非字典项、缺 summary 的项；detail 缺省为空串。保证不因模型输出瑕疵崩溃。
        """
        if not isinstance(raw, list):
            return []
        options: list[ClarifyOption] = []
        for item in raw:
            if not isinstance(item, dict):
                continue
            summary = str(item.get("summary", "")).strip()
            if not summary:
                continue
            detail = str(item.get("detail", "")).strip()
            options.append(ClarifyOption(summary=summary, detail=detail))
        return options

    def _run_readonly_concurrent(
        self,
        items: list[tuple[ToolCall, Tool]],
        results: dict[str, ToolResult],
    ) -> Iterator[AgentEvent]:
        """
        并发执行一组只读工具（迁移自 c3，改产出 AgentEvent）。

        先为每个调用产出 TOOL_START，再用线程池并发执行，谁先完成先产出 TOOL_RESULT。
        参数解析失败的调用不进线程池，直接结构化错误。
        """
        for tc, _tool in items:
            yield AgentEvent(type=AgentEventType.TOOL_START, tool_call=tc)

        for tc, _tool in items:
            if not isinstance(tc.arguments, dict):
                res = _invalid_args_result(tc)
                results[tc.id] = res
                yield AgentEvent(type=AgentEventType.TOOL_RESULT, tool_call=tc, tool_result=res)

        valid = [(tc, tool) for tc, tool in items if isinstance(tc.arguments, dict)]
        if not valid:
            return

        with ThreadPoolExecutor(max_workers=len(valid)) as executor:
            future_to_tc = {
                executor.submit(tool.execute, tc.arguments): tc
                for tc, tool in valid
            }
            for future in as_completed(future_to_tc):
                tc = future_to_tc[future]
                try:
                    res = future.result()
                except Exception as e:
                    res = ToolResult(ok=False, output=f"工具执行异常: {e}")
                results[tc.id] = res
                yield AgentEvent(type=AgentEventType.TOOL_RESULT, tool_call=tc, tool_result=res)

    def _run_one_serial(
        self,
        tc: ToolCall,
        tool: Optional[Tool],
        decision: Optional[DecisionResult],
        results: dict[str, ToolResult],
        ask: AskFn,
    ) -> Iterator[AgentEvent]:
        """
        串行处理单个工具调用：未知 / 参数错误 / 权限拒绝 / 待确认 / 放行（c6）。

        各分支的结果都写入 results 并产出事件；任何「不执行」的分支（未知、参数错误、
        权限拒绝、用户拒绝）都返回 ok=False 的结构化结果回灌模型，但**不终止循环**
        （spec F8）——与未知工具/参数错误的既有处理一致。

        :param tc: 工具调用
        :param tool: 对应工具实例；None 表示未知工具
        :param decision: 权限决策结果；None 表示参数解析失败（未进引擎）
        :param results: 输出参数，写入 id → ToolResult
        :param ask: 人工确认回调（仅 decision 为 ASK 时调用）
        """
        # 未知工具：结构化错误（沿用既有行为，含 TOOL_START）
        if tool is None:
            yield AgentEvent(type=AgentEventType.TOOL_START, tool_call=tc)
            res = ToolResult(ok=False, output=f"未知工具: {tc.name}")
            results[tc.id] = res
            yield AgentEvent(type=AgentEventType.TOOL_RESULT, tool_call=tc, tool_result=res)
            return

        # 参数解析失败（decision 为 None）
        if decision is None:
            yield AgentEvent(type=AgentEventType.TOOL_START, tool_call=tc)
            res = _invalid_args_result(tc)
            results[tc.id] = res
            yield AgentEvent(type=AgentEventType.TOOL_RESULT, tool_call=tc, tool_result=res)
            return

        # 权限拒绝：产出结构化拒绝结果，不执行（与「用户拒绝」一致，仅产出 TOOL_RESULT）
        if decision.decision == Decision.DENY:
            res = ToolResult(
                ok=False,
                output=f"[权限拒绝·{decision.layer.value}] {decision.reason}",
                summary="权限拒绝",
            )
            results[tc.id] = res
            yield AgentEvent(type=AgentEventType.TOOL_RESULT, tool_call=tc, tool_result=res)
            return

        # 待确认：弹 HITL 面板，用户拒绝则不执行
        if decision.decision == Decision.ASK:
            approved = ask(tc, tool, decision)
            if not approved:
                res = ToolResult(ok=False, output="用户拒绝执行该工具。")
                results[tc.id] = res
                yield AgentEvent(type=AgentEventType.TOOL_RESULT, tool_call=tc, tool_result=res)
                return

        # 放行（ALLOW 或确认通过）：执行工具
        yield AgentEvent(type=AgentEventType.TOOL_START, tool_call=tc)
        try:
            res = tool.execute(tc.arguments)
        except Exception as e:
            res = ToolResult(ok=False, output=f"工具执行异常: {e}")
        results[tc.id] = res
        yield AgentEvent(type=AgentEventType.TOOL_RESULT, tool_call=tc, tool_result=res)
