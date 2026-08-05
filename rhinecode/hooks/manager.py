"""
Hook 编排（spec F5–F7、F11、F12）：本包唯一有状态的类。

`HookManager` 负责把「一次生命周期事件」变成「若干条规则的执行 + 一个结论」：

    dispatch(event, payload_factory)
        ├─ 该事件无人监听 → 立即返回，**不构造负载**
        ├─ 构造负载 → 逐条求值条件 → 筛掉 once 已消耗的
        ├─ 逐条执行动作（async 的丢给后台线程）
        ├─ 合并结论（deny > ask > 不表态）
        └─ 埋点 → 返回 DispatchResult

## ⚠ 加锁不变量：临界区只做纯内存读写

> **动作执行、trace 埋点、跨线程调度一律在锁外。**

违反的后果是**一个 60 秒超时的 `command` 动作把整个 manager 锁死**——期间任何线程的
任何事件分发全部阻塞，界面表现为假死，而调用栈上看不出原因（大家都停在一个
`lock.acquire()` 上，没有任何一处能指向那个真正的元凶）。

这与 C11 `SkillManager` 是同一课，只是后果不同（那次是与 Textual 阻塞式
`call_from_thread` 组成确定性死锁）。`dispatch` 的写法固定为四段：

    持锁筛选并标记 → 出锁执行 → 持锁写统计 → 出锁埋点

## ⚠ 观测/自动化设施绝不能反过来阻断被观测的系统

`dispatch` 整体包在一个兜底 `try` 里：负载构造、条件求值、结论合并里的任何异常
都被吞掉并记 trace，对外返回「什么都没发生」。这与「Hook 脚本自己跑失败」
是两回事——后者在 `pre_tool_use` 上要 fail-closed（spec F7.1），前者是**我们的 bug**，
不该让用户的 Agent 停摆。
"""

import threading
from typing import Any, Callable, Optional

from rhinecode.hooks.actions import run_action
from rhinecode.hooks.conditions import evaluate
from rhinecode.hooks.models import (
    EMPTY_DISPATCH,
    NO_VERDICT,
    SEVERITY,
    ActionOutcome,
    AgentAction,
    CommandAction,
    DispatchResult,
    HookDecision,
    HookEventType,
    HookPayload,
    HookRule,
    HookVerdict,
    HttpAction,
    PromptAction,
)
from rhinecode.hooks.report import render_project_notice, render_report
from rhinecode.trace import NullRecorder, TraceEventType, TraceRecorderProtocol, clip

# 动作类型 → 记录用的短名（trace 与统计里展示）。
_ACTION_NAMES = {
    CommandAction: "command",
    PromptAction: "prompt",
    HttpAction: "http",
    AgentAction: "agent",
}


def _action_name(action: Any) -> str:
    """取动作的短名，未知类型退回类名（不抛异常——这是观测路径）。"""
    return _ACTION_NAMES.get(type(action), type(action).__name__)


# ── 拦截回灌文案（spec F6.3）──
#
# ## 为什么要写这么长
#
# 与 C6 的 `DENIED_BY_USER_FEEDBACK` 是同一个问题：工具结果这个通道天生长得像
# 「技术失败」（`ok=False` + 一段错误文本），而模型对技术失败的默认反应是
# **换个姿势重试**——改参数、换工具、绕路。
#
# 但 Hook 拦截不是技术失败，它是用户**预先写下的规则**。文案必须把这个语义掰回来：
# 说清是谁拦的、为什么、以及「不要绕」。少了最后一句，模型会把它当成
# 「这条路不通，我换一条」，而那正是用户不希望发生的事。
BLOCKED_FEEDBACK = (
    "[Hook 拦截·{name}] {reason}\n"
    "\n"
    "**这不是技术故障，也不是工具出错。** 它是用户在 hooks.yaml"
    "（来源：{source}）里预先声明的一条规则，在这次调用真正执行之前把它拦了下来。\n"
    "\n"
    "不要改参数重试、不要换用其它工具绕过——那会把一条明确的规则当成"
    "「姿势不对」，正是用户不希望发生的事。\n"
    "如果你认为这一步确实必要，请向用户说明你原本打算做什么、以及为什么需要它。"
)
# ⚠ 末句**刻意不提「请他决定是否调整这条规则」**（人眼评审时去掉的）。
#
# 那半句把「要不要削弱这道防线」主动摆上了桌面。实测（C12 场景 9，真实模型）
# 模型据此给出的两个选项之一就是「修改 hooks.yaml」——诚实，但等于把
# 「绕过规则」包装成了「建议你改规则」，而这条规则的存在本身就说明用户
# 不希望这件事发生。让模型说明意图即可，改不改规则是用户自己会想到的事。

# Hook 自身执行失败时的拦截原因（spec F7.1 fail-closed）。
#
# 文案必须与「工具执行异常」明确可区分——那是**两种完全不同的排查方向**：
# 一个是去看 hooks.yaml 里的脚本，一个是去看工具实现。
HOOK_FAILED_REASON = (
    "Hook 自身执行失败，出于安全考虑本次调用已被拦下（fail-closed）。详情：{detail}"
)

# Hook 把 ALLOW 升级为 ASK 时，展示给**用户**的原因（进确认面板）。
ASK_REASON = "Hook 规则「{name}」（来源：{source}）要求这次调用由你确认。{reason}"


class HookManager:
    """
    Hook 规则的持有者与分发器。

    构造后规则集不可变（热更新不在本章范围）；可变的只有三样**本次运行**的状态：
    `once` 消耗标记、执行统计、待注入文本队列——全部受同一把锁保护。

    线程安全：`dispatch` 会被多个线程调用（Agent 的 Worker 线程、只读工具的并发池
    线程、`async` 动作的后台线程），因此三样状态的读写都在临界区内。
    """

    def __init__(
        self,
        rules: Optional[list[HookRule]] = None,
        warnings: Optional[list[str]] = None,
        recorder: "Optional[TraceRecorderProtocol]" = None,
        client_factory: Optional[Callable[[], Any]] = None,
        *,
        user_path: str = "~/.rhinecode/hooks.yaml",
        project_path: str = "<项目根>/.rhinecode/hooks.yaml",
    ):
        """
        :param rules: 已加载的规则（顺序即执行顺序：用户级 → 项目级 → 层内声明序）
        :param warnings: 加载期警告，原样进 `/hooks` 报告与启动提示
        :param recorder: 行为记录器；缺省 `NullRecorder()`（**不传等于零回归**）
        :param client_factory: 造 HTTP 客户端的工厂，透传给 `http` 动作。
                               缺省 None（用真 `httpx.Client`）；测试与端到端场景靠它离线跑
        :param user_path / project_path: 仅用于 `/hooks` 报告里展示配置位置
        """
        self._rules: list[HookRule] = list(rules or [])
        self._warnings: list[str] = list(warnings or [])
        self._recorder: TraceRecorderProtocol = recorder or NullRecorder()
        self._client_factory = client_factory
        self._user_path = user_path
        self._project_path = project_path

        # 按事件建索引，使 `has_listeners` 是 O(1)——它在每一次工具调用前都会被问一次。
        self._by_event: dict[HookEventType, list[HookRule]] = {}
        for rule in self._rules:
            self._by_event.setdefault(rule.event, []).append(rule)

        self._lock = threading.Lock()
        self._once_consumed: set[str] = set()
        self._stats: dict[str, dict[str, Any]] = {}
        self._injections: list[str] = []

        # 三个公共字段由本类统一填充（见 `_common_fields`）。
        self._session_id: str = ""
        self._cwd: str = ""

    # ------------------------------------------------------------------ #
    # 公共字段
    # ------------------------------------------------------------------ #
    def bind_context(self, session_id: str = "", cwd: str = "") -> None:
        """
        绑定公共字段的取值（会话 ID 与项目根）。

        :param session_id: 当前会话 ID；`/clear`、`/resume` 切换会话后要重新绑定
        :param cwd: 项目根绝对路径

        由装配层调用一次、协调层在会话切换时再调用。**不传的字段保持原值**，
        因此调用方可以只更新 `session_id`。

        副作用：改写内部字段（仅供负载构造使用，不参与任何判定）。
        """
        if session_id:
            self._session_id = session_id
        if cwd:
            self._cwd = cwd

    def _common_fields(self, event: HookEventType) -> dict[str, Any]:
        """
        构造三个公共字段（spec F2 开头）。

        **由本类统一填充，而不是让五个分发点各拼一次**——那是典型的漏改点：
        少填一个 `session_id` 不报错，只是某个事件的负载里悄悄少一个字段，
        而用户写的 `session_id: xxx` 条件从此永不命中。
        """
        return {"event": event.value, "session_id": self._session_id, "cwd": self._cwd}

    # ------------------------------------------------------------------ #
    # 受保护的埋点漏斗
    # ------------------------------------------------------------------ #
    # 与 `agent/loop.py` 同形态、同理由：记录器自己已经保证 emit 不抛，但本层
    # 是「自动化设施」，它的异常有机会顺着 `pre_tool_use` 的 fail-closed 变成
    # 「你的工具被拦了」。一处加固即全类生效，好过在十几个埋点处各写一次 try。
    def _safe_emit(self, event_type, **payload) -> None:
        """记一条事件，吞掉任何异常（含记录器实现本身抛错的情形）。"""
        try:
            self._recorder.emit(event_type, **payload)
        except Exception:
            pass

    # ------------------------------------------------------------------ #
    # 只读查询
    # ------------------------------------------------------------------ #
    @property
    def enabled(self) -> bool:
        """是否加载到了任何规则。为 False 时全部分发都是零成本空操作。"""
        return bool(self._rules)

    @property
    def rules(self) -> list[HookRule]:
        """已加载规则的只读副本（顺序即执行顺序）。"""
        return list(self._rules)

    @property
    def warnings(self) -> list[str]:
        """加载期警告的只读副本。"""
        return list(self._warnings)

    def has_listeners(self, event: HookEventType) -> bool:
        """
        该事件是否有规则监听。

        **分发前必须先问它**（spec N7）：为假时 `dispatch` 不会调用 `payload_factory`，
        也就不会去构造负载。`pre_tool_use` 的负载要展开整个 `tool_input`
        （一次 `write_file` 就是整份文件内容），零命中时白构造一遍不可接受。

        副作用：无。
        """
        return bool(self._by_event.get(event))

    def report(self) -> str:
        """渲染 `/hooks` 报告。纯只读，不改任何状态。"""
        with self._lock:
            stats = {k: dict(v) for k, v in self._stats.items()}
            consumed = set(self._once_consumed)
        for key in consumed:
            stats.setdefault(key, {})["once_consumed"] = True
        return render_report(
            self._rules,
            self._warnings,
            stats,
            user_path=self._user_path,
            project_path=self._project_path,
        )

    def project_notice(self) -> Optional[str]:
        """渲染项目级规则的启动提示（spec F9.1）；无项目级规则时返回 None。"""
        return render_project_notice([r for r in self._rules if r.source == "project"])

    # ------------------------------------------------------------------ #
    # 注入队列
    # ------------------------------------------------------------------ #
    def consume_injections(self) -> str:
        """
        取走并清空全部待注入文本（`prompt` 动作的产物）。

        :returns: 多条按触发顺序用空行连接；队列为空时返回空串

        **「一次性」语义完全由本方法保证**（spec F4.2）：取走即清，因此注入文本
        只会出现在紧接着的那一次 API 请求里。持续注入会让 `<system-reminder>`
        无限增长，而且用户完全无法预期它什么时候消失。

        副作用：清空内部队列。
        """
        with self._lock:
            if not self._injections:
                return ""
            texts = self._injections
            self._injections = []
        return "\n\n".join(texts)

    # ------------------------------------------------------------------ #
    # 分发
    # ------------------------------------------------------------------ #
    def dispatch(
        self,
        event: HookEventType,
        payload_factory: Optional[Callable[[], dict[str, Any]]] = None,
    ) -> DispatchResult:
        """
        分发一次生命周期事件。

        :param event: 事件类型
        :param payload_factory: **惰性**负载构造器，返回字段字典。
                                该事件无人监听时**根本不会被调用**（spec N7）
        :returns: `DispatchResult`；无人监听或出错时为 `EMPTY_DISPATCH`

        副作用：可能起子进程、发 HTTP 请求、起后台线程；写内部统计与注入队列；产 trace 事件。

        **本方法不抛任何异常**——见模块 docstring 末节。
        """
        if not self.has_listeners(event):
            return EMPTY_DISPATCH
        try:
            return self._dispatch(event, payload_factory)
        except Exception as exc:  # noqa: BLE001 —— 观测/自动化设施绝不能阻断主流程
            self._safe_emit(
                TraceEventType.HOOK_DISPATCH,
                event=event.value,
                matched=0,
                executed=0,
                verdict=HookDecision.NONE.value,
                error=f"{type(exc).__name__}: {exc}",
            )
            return EMPTY_DISPATCH

    def _dispatch(
        self,
        event: HookEventType,
        payload_factory: Optional[Callable[[], dict[str, Any]]],
    ) -> DispatchResult:
        """`dispatch` 的主体，四段式加锁。异常由调用方兜底。"""
        listeners = self._by_event.get(event, [])
        raw = payload_factory() if payload_factory is not None else {}
        fields: dict[str, Any] = dict(raw) if isinstance(raw, dict) else {}
        # 公共字段**后写入，因此覆盖同名的展开字段**（spec F3.2：公共字段优先）。
        # 工具参数里恰好也叫 `cwd` 的情况真实存在，让它盖掉「项目根」会让
        # 一条按项目筛选的条件在某些工具上突然错位。
        fields.update(self._common_fields(event))
        payload = HookPayload(event, fields)

        # 条件求值在锁外：规则不可变、`evaluate` 是纯函数，没有任何共享状态。
        candidates = [r for r in listeners if evaluate(r.condition, fields)]

        # ── 第一段：持锁筛 once 并标记 ──
        with self._lock:
            to_run: list[HookRule] = []
            for rule in candidates:
                if rule.once:
                    if rule.key in self._once_consumed:
                        continue
                    self._once_consumed.add(rule.key)
                to_run.append(rule)

        # ── 第二段：出锁执行 ──
        outcomes: list[tuple[HookRule, ActionOutcome]] = []
        for rule in to_run:
            if rule.run_async:
                # 异步动作的结果**不参与结论合并**——`pre_tool_use` 上已在加载期
                # 禁止 async，所以不存在「异步却要拿结论」的情形。
                threading.Thread(
                    target=self._execute,
                    args=(rule, payload),
                    daemon=True,
                    name=f"hook-{rule.key}",
                ).start()
                continue
            outcomes.append((rule, self._execute(rule, payload)))

        verdict = self._merge(event, outcomes)
        result = DispatchResult(verdict, len(candidates), len(to_run))

        # ── 第四段：出锁埋点 ──
        # **零命中也产出这条事件**：它是排查「我的 hook 为什么没跑」的第一现场。
        # 看到「命中 0 条」说明事件确实触发了、是条件没匹配上；一条都看不到
        # 则说明分发点压根没接上——两种情况的排查方向完全不同。
        self._safe_emit(
            TraceEventType.HOOK_DISPATCH,
            event=event.value,
            matched=result.matched,
            executed=result.executed,
            verdict=verdict.decision.value,
            rule=verdict.rule_name,
        )
        return result

    def _execute(self, rule: HookRule, payload: HookPayload) -> ActionOutcome:
        """
        执行单条规则的动作，写统计并埋点。

        :returns: 执行结果；动作抛异常时转成 `ok=False`（不向上抛）

        本方法**同时是异步线程的入口**，因此绝不能抛——后台线程里的异常无人接管，
        只会打印到 stderr 把界面搅乱。

        副作用：执行动作（可能起子进程 / 发请求）；写统计与注入队列；产 trace 事件。
        """
        try:
            outcome = run_action(rule.action, payload, self._client_factory)
        except Exception as exc:  # noqa: BLE001 —— run_action 内部已兜底，这里是纵深防御
            outcome = ActionOutcome(
                ok=False, detail=f"Hook 动作抛出异常：{type(exc).__name__}: {exc}"
            )

        # ── 第三段：持锁写统计与注入队列（纯内存，瞬间完成）──
        with self._lock:
            stat = self._stats.setdefault(
                rule.key, {"triggered": 0, "failures": 0, "last_verdict": "", "last_ms": 0.0}
            )
            stat["triggered"] += 1
            stat["last_ms"] = outcome.duration_ms
            stat["last_verdict"] = outcome.verdict.value
            if not outcome.ok:
                stat["failures"] += 1
            if outcome.injected_text:
                self._injections.append(outcome.injected_text)

        self._safe_emit(
            TraceEventType.HOOK_EXECUTE,
            rule=rule.name,
            key=rule.key,
            source=rule.source,
            event=payload.event.value,
            action_type=_action_name(rule.action),
            is_async=rule.run_async,
            ok=outcome.ok,
            verdict=outcome.verdict.value,
            duration_ms=outcome.duration_ms,
            detail=clip(outcome.detail),
        )
        return outcome

    # ------------------------------------------------------------------ #
    # 结论合并
    # ------------------------------------------------------------------ #
    def _merge(
        self,
        event: HookEventType,
        outcomes: list[tuple[HookRule, ActionOutcome]],
    ) -> HookVerdict:
        """
        把多条动作结果合并成一个结论（spec F6.2 + F7.1）。

        :param event: 本次事件；决定要不要做 fail-closed
        :param outcomes: `(规则, 结果)` 列表
        :returns: 最严的那个结论；全部不表态时返回 `NO_VERDICT`

        合并规则：**按 `SEVERITY` 取最严**（DENY > ASK > 不表态），并记住做出该结论的
        第一条规则。与权限规则的「deny 优先」同哲学——一个只能收紧的系统里，
        「谁先谁后」不该改变最终结论。

        **fail-closed 只在 `pre_tool_use` 上生效**（spec F7.1）：该事件下
        `ok=False` 的动作一律计为 DENY。其余事件的失败不参与合并，只进统计与埋点。

        副作用：无。
        """
        best = NO_VERDICT
        best_severity = SEVERITY[HookDecision.NONE]

        for rule, outcome in outcomes:
            decision = outcome.verdict
            reason = outcome.reason

            # fail-closed：拦截类事件上，Hook 自身跑失败即按拦截处理。
            #
            # 理由（spec F7.1）：一个 pre_tool_use Hook 的存在本身就声明了
            # 「这个位置需要检查」。fail-open 会让一条上周就写坏的安全 Hook
            # **静默失效**而用户毫不知情；fail-closed 会让 Agent 立刻干不了活——
            # 那是**可见**的，五秒内就会去修。
            if event == HookEventType.PRE_TOOL_USE and not outcome.ok:
                decision = HookDecision.DENY
                reason = HOOK_FAILED_REASON.format(detail=outcome.detail or "无更多信息")

            if decision == HookDecision.NONE:
                continue

            severity = SEVERITY[decision]
            if severity <= best_severity:
                continue
            best_severity = severity
            best = HookVerdict(
                decision=decision,
                reason=self._format_reason(rule, decision, reason),
                rule_name=rule.name,
            )

        return best

    @staticmethod
    def _format_reason(rule: HookRule, decision: HookDecision, reason: str) -> str:
        """
        构造结论的展示文案。

        两种结论的**读者不同**，因此文案完全不同：
        - DENY 的读者是**模型**（作为工具结果回灌）→ 必须写明「不要绕过」
        - ASK 的读者是**用户**（进确认面板）→ 要说清是哪条规则要求确认的

        副作用：无。
        """
        if decision == HookDecision.DENY:
            return BLOCKED_FEEDBACK.format(
                name=rule.name,
                source=rule.source,
                reason=reason or "该规则未给出具体原因。",
            )
        return ASK_REASON.format(
            name=rule.name, source=rule.source, reason=reason or ""
        ).rstrip()


__all__ = ["ASK_REASON", "BLOCKED_FEEDBACK", "HOOK_FAILED_REASON", "HookManager"]
