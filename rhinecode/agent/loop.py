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
from typing import Callable, Iterator, Optional

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

# 迭代上限：兜底安全网，任何情况下循环都不会超过这么多轮（spec N3）。
MAX_ITERATIONS = 25
# 连续「整轮都是未知工具」达到该次数即停止，避免模型在不存在的工具上空转（spec F2）。
MAX_CONSECUTIVE_UNKNOWN = 3

# 回调类型别名（由 ConversationManager 注入；均预期在 Worker 线程被调用）。
# confirm：有副作用工具是否执行（已在上层闭包里解析过三态与会话级免确认，循环只看 bool）
ConfirmFn = Callable[[ToolCall, Tool], bool]
# clarify：弹澄清面板，返回用户所选概述；返回 None 表示用户取消
ClarifyFn = Callable[[str, list[ClarifyOption]], Optional[str]]
# approve_plan：弹「是否开始执行」审批，返回是否批准
ApprovePlanFn = Callable[[str], bool]


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
        system_prompt: Optional[str],
        confirm: ConfirmFn,
        clarify: Optional[ClarifyFn],
        approve_plan: Optional[ApprovePlanFn],
        cancel_event: threading.Event,
    ) -> Iterator[AgentEvent]:
        """
        跑一次完整的 ReAct 循环，逐个产出 AgentEvent。

        :param history: 对话历史（同一引用）；循环会向其追加 assistant 与 tool 结果消息（N5）
        :param thinking_effort: 思考模式强度（透传给 provider）
        :param plan_mode: 是否处于 Plan Mode
        :param system_prompt: 需前置注入的 system 提示（Plan Mode 时非空），不写入持久 history
        :param confirm: 有副作用工具是否执行的回调（已解析三态/免确认，返回 bool）
        :param clarify: 需求澄清回调（ask_user 用），可为 None
        :param approve_plan: 计划审批回调（present_plan 用），可为 None
        :param cancel_event: 取消信号；循环在安全点轮询，置位即尽快停止
        :returns: AgentEvent 迭代器；末尾必为一个 FINISHED 事件

        副作用：向 history 追加消息；通过 provider 发起多次网络请求；通过回调与用户交互。
        """
        execution_phase = False       # Plan Mode 下是否已获批执行
        consecutive_unknown = 0       # 连续「整轮仅未知工具」的次数

        for iteration in range(1, MAX_ITERATIONS + 1):
            # 安全点 1：进入新一轮前检查取消
            if cancel_event.is_set():
                yield AgentEvent(type=AgentEventType.FINISHED, stop_reason=StopReason.USER_CANCELLED)
                return

            yield AgentEvent(type=AgentEventType.PROGRESS, iteration=iteration)

            tools = self._schema_for(plan_mode, execution_phase)

            # 组装请求消息：Plan Mode 时把引导提示作为 system 消息前置（不入持久 history）
            req_messages: list[Message] = []
            if system_prompt:
                req_messages.append(Message(role="system", content=system_prompt))
            req_messages.extend(history)

            # ---------- 双路收集本轮流式响应 ----------
            collector = StreamCollector()
            stream_error: Optional[str] = None
            for chunk in self._provider.stream_chat(req_messages, thinking_effort, tools=tools):
                if chunk.type == "error":
                    stream_error = chunk.content
                    break
                ev = collector.feed(chunk)
                if ev is not None:
                    yield ev

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
                confirm, clarify, approve_plan,
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
        confirm: ConfirmFn,
        clarify: Optional[ClarifyFn],
        approve_plan: Optional[ApprovePlanFn],
        cancel_event: threading.Event,
    ) -> Iterator[AgentEvent]:
        """
        执行本轮所有工具调用，按类别分流：

        - 特殊工具（ask_user / present_plan）：串行，路由到 clarify / approve_plan 回调
        - 普通只读工具：并发执行（互不冲突，提升效率）
        - 普通有副作用工具 / 未知工具：串行执行（副作用工具按需确认）

        所有结果写入 results（键为 tool_call.id），并把状态变化记录到 ctx 供主循环判断。
        每个工具开始/结束分别产出 TOOL_START / TOOL_RESULT 事件。

        :param tool_calls: 本轮工具调用
        :param results: 输出参数，写入 id → ToolResult
        :param ctx: 本轮上下文，记录 cancelled/approved/known/unknown
        :param confirm/clarify/approve_plan: 三类交互回调
        :param execution_phase: Plan Mode 是否已获批执行（仅影响本轮可用工具集合）
        :param cancel_event: 取消信号，串行执行前检查

        副作用：实际执行工具（可能读写文件、跑命令）；通过回调与用户交互。
        """
        special: list[ToolCall] = []
        readonly: list[tuple[ToolCall, Tool]] = []
        side_effect: list[tuple[ToolCall, Optional[Tool]]] = []

        # 分流：先认特殊工具名，再按是否在注册中心 / 是否只读归类
        for tc in tool_calls:
            if tc.name in (ASK_USER, PRESENT_PLAN):
                special.append(tc)
                ctx.known_count += 1
                continue
            tool = self._registry.get(tc.name) if self._registry else None
            if tool is None:
                side_effect.append((tc, None))
                ctx.unknown_count += 1
            elif tool.read_only:
                readonly.append((tc, tool))
                ctx.known_count += 1
            else:
                side_effect.append((tc, tool))
                ctx.known_count += 1

        # 只读工具：并发
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

        # 有副作用 / 未知工具：串行
        for tc, tool in side_effect:
            if ctx.cancelled or ctx.plan_rejected:
                break
            if cancel_event.is_set():
                ctx.cancelled = True
                break
            yield from self._run_one_serial(tc, tool, results, confirm)

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

        if tc.arguments is None:
            res = ToolResult(ok=False, output=f"工具 {tc.name} 的参数 JSON 解析失败，请检查格式后重试。")
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
            if tc.arguments is None:
                res = ToolResult(ok=False, output=f"工具 {tc.name} 的参数 JSON 解析失败，请检查格式后重试。")
                results[tc.id] = res
                yield AgentEvent(type=AgentEventType.TOOL_RESULT, tool_call=tc, tool_result=res)

        valid = [(tc, tool) for tc, tool in items if tc.arguments is not None]
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
        results: dict[str, ToolResult],
        confirm: ConfirmFn,
    ) -> Iterator[AgentEvent]:
        """
        串行执行单个有副作用工具（或处理未知工具 / 参数错误），迁移自 c3 并适配 c4。

        已知的有副作用工具始终调 confirm（已封装三态 + 会话免确认）决定是否执行；
        只有用户明确选择「本会话不再询问」后，上层 confirm 闭包才会自动放行。
        """
        # 未知工具：结构化错误
        if tool is None:
            yield AgentEvent(type=AgentEventType.TOOL_START, tool_call=tc)
            res = ToolResult(ok=False, output=f"未知工具: {tc.name}")
            results[tc.id] = res
            yield AgentEvent(type=AgentEventType.TOOL_RESULT, tool_call=tc, tool_result=res)
            return

        # 参数解析失败
        if tc.arguments is None:
            yield AgentEvent(type=AgentEventType.TOOL_START, tool_call=tc)
            res = ToolResult(ok=False, output=f"工具 {tc.name} 的参数 JSON 解析失败，请检查格式后重试。")
            results[tc.id] = res
            yield AgentEvent(type=AgentEventType.TOOL_RESULT, tool_call=tc, tool_result=res)
            return

        # 计划审批只开放执行阶段，不等于免确认；副作用工具仍逐个走确认回调。
        approved = confirm(tc, tool)
        if not approved:
            res = ToolResult(ok=False, output="用户拒绝执行该工具。")
            results[tc.id] = res
            yield AgentEvent(type=AgentEventType.TOOL_RESULT, tool_call=tc, tool_result=res)
            return

        yield AgentEvent(type=AgentEventType.TOOL_START, tool_call=tc)
        try:
            res = tool.execute(tc.arguments)
        except Exception as e:
            res = ToolResult(ok=False, output=f"工具执行异常: {e}")
        results[tc.id] = res
        yield AgentEvent(type=AgentEventType.TOOL_RESULT, tool_call=tc, tool_result=res)
