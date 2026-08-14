"""
分类器门面（c16 F10–F17）：唯一发起模型调用的地方。

## 两阶段判定（F10）

    第一阶段：快速过滤，只要求输出一个词（拦 / 不拦）
        └─ 判「不拦」 → 直接放行，**第二阶段零次调用**
        └─ 判「拦」或看不懂 → 进第二阶段
    第二阶段：带理由的复核，允许模型想清楚再说

绝大多数日常命令（`ls`、跑测试、格式化）在第一阶段就过了，只花一个 token 的
输出——**这是把延迟与费用压下去的主要手段**，也是「命令类不做缓存」这个决定
能成立的前提。对齐 Claude Code：*a fast single-token filter … followed by
chain-of-thought reasoning only if the first filter flags the transcript.*

## ⚠ 本类绝不外抛异常（N4）

一切失败——网络错误、超时、流里的 error 块、输出解析不出来——统统收敛成
`VerdictKind.FAILED`。观测与防护设施绝不能反过来阻断被观测的系统；而这里更
严重一点：本类跑在 Agent Loop 的决策预扫里，抛出去会让**整轮工具执行**炸掉。

## ⚠ 埋点一律在锁外

熔断器的 `record_*` 返回不可变快照，本类拿到快照之后才去埋点。
持锁期间做任何回调都可能与 Textual 的阻塞式跨线程调度组成确定性死锁——
本项目已经踩过四次，`breaker.py` 的模块 docstring 列着那四处。
"""

from __future__ import annotations

import time
from typing import Optional

from rhinecode.classifier import parse, prompt
from rhinecode.classifier.breaker import CircuitBreaker
from rhinecode.classifier.models import (
    BreakerState,
    ClassifierConfig,
    ReviewAction,
    Transcript,
    Verdict,
    VerdictKind,
)
from rhinecode.provider.base import BaseProvider
from rhinecode.trace import (
    SCOPE_CLASSIFIER,
    NullRecorder,
    TraceEventType,
    TraceRecorderProtocol,
)


class ClassifierService:
    """
    分类器门面。**线程安全**：主对话与全部子 Agent 共用同一个实例。

    :ivar _provider: 分类器专用的 Provider（可能与主对话不同模型、带超时）
    :ivar _config: 三个配置项
    :ivar _breaker: 熔断器（本类唯一的可变状态，它自己加锁）
    :ivar _recorder: 行为记录器，缺省 `NullRecorder()`

    ## 为什么本类没有缓存

    缓存的作用域是**每次运行一个**（见 `cache.py` 的类 docstring），
    而本类是会话共享的。缓存放在 `ReviewSession` 里，本类只管「真的去问一次」。
    这个分工让「A 批准过的主机对 B 生效」这件事在结构上不可能发生。
    """

    def __init__(
        self,
        provider: BaseProvider,
        config: ClassifierConfig,
        recorder: "Optional[TraceRecorderProtocol]" = None,
    ) -> None:
        """
        :param provider: 分类器用的 Provider。由装配层构造——它可能与主对话用
            不同的模型（`config.model` 非空时），也带着 `request_timeout`
        :param config: 分类器配置
        :param recorder: 行为记录器；缺省 `NullRecorder()`，不传等于不记录
        """
        self._provider = provider
        self._config = config
        self._breaker = CircuitBreaker()
        self._recorder: TraceRecorderProtocol = recorder or NullRecorder()

    # ------------------------------------------------------------------ #
    # 协议实现
    # ------------------------------------------------------------------ #
    def is_tripped(self) -> bool:
        """当前是否处于熔断。调用方据此决定 FAILED 走哪条退路（F16a）。"""
        return self._breaker.is_tripped()

    def breaker_state(self) -> BreakerState:
        """取熔断状态快照。"""
        return self._breaker.state()

    def note_manual_approval(self) -> None:
        """
        用户在确认面板上批准了一次 → 解除熔断（F15 的恢复条件）。

        对齐官方：*Approving the prompted action resumes auto mode.*

        副作用：清空熔断器的全部计数与状态。
        """
        self._breaker.reset()

    def review(self, action: ReviewAction, transcript: Transcript) -> Verdict:
        """
        对一次待判动作给出结论。**这是本类的唯一入口。**

        :param action: 待判动作
        :param transcript: 转录（用户消息 + 模型发起过的工具调用）
        :returns: 判定结果；**绝不抛异常**

        副作用：可能发起一到两次 provider 请求（消耗额度）；更新熔断计数；
                产出一条行为记录。

        ## 熔断中的行为

        直接返回 `FAILED` 且**不发任何请求**。调用方靠 `is_tripped()` 区分
        「本次失败」与「已经熔断」——判据只有一个地方，不在 `Verdict` 上再加
        一个布尔（两个来源的真值早晚会不一致）。
        """
        started = time.monotonic()

        if self._breaker.is_tripped():
            state = self._breaker.state()
            return Verdict(
                kind=VerdictKind.FAILED,
                reason=f"安全审查已停用（{state.detail}）",
            )

        # ── 第一阶段：快速过滤 ──
        try:
            text1 = self._call(prompt.render_stage1(transcript, action), prompt.STAGE1_SYSTEM)
        except Exception as exc:  # noqa: BLE001 —— 一切异常收敛为失败（N4）
            return self._fail(action, f"第一阶段调用失败：{exc}", started)

        flagged = parse.parse_stage1(text1)
        if flagged is False:
            # ⚠ **放行的唯一路径就是这一条**（以及第二阶段明确判不拦）。
            # 看不懂、超时、抛异常，没有任何一种走到这里。
            self._breaker.record_allow()
            return self._done(
                action,
                Verdict(VerdictKind.ALLOW, "快速筛查未发现问题", staged=False),
                started,
                stage1_raw=text1,
            )

        # `flagged is None`（看不懂）也走到这里——按「可疑」处理进第二阶段，
        # 而不是直接判失败。理由见 `parse.py` 的模块 docstring：
        # 第二阶段是一次真正的补救，且这不放宽任何东西。

        # ── 第二阶段：带理由的复核 ──
        try:
            text2 = self._call(prompt.render_stage2(transcript, action), prompt.STAGE2_SYSTEM)
        except Exception as exc:  # noqa: BLE001
            return self._fail(action, f"第二阶段调用失败：{exc}", started)

        parsed = parse.parse_stage2(text2)
        if parsed is None:
            return self._fail(action, "第二阶段输出无法解析", started, stage1_raw=text1)

        blocked, reason = parsed
        if blocked:
            state = self._breaker.record_block()
            verdict = Verdict(VerdictKind.BLOCK, reason, staged=True)
        else:
            state = None
            self._breaker.record_allow()
            verdict = Verdict(VerdictKind.ALLOW, reason, staged=True)
        return self._done(action, verdict, started, stage1_raw=text1, tripped=state)

    # ------------------------------------------------------------------ #
    # 内部
    # ------------------------------------------------------------------ #
    def _call(self, messages, system: str) -> str:
        """
        发一次分类器请求并把流收成完整文本。

        :param messages: 请求消息列表
        :param system: 系统提示（走可缓存的稳定通道）
        :returns: 模型输出全文
        :raises RuntimeError: 流里出现 error 块时抛出，由 `review` 收敛成失败

        副作用：一次网络请求，消耗 token 配额。

        ## ⚠ 三处不可动

        ① **`tools=None` 强制**（F11）：分类器不该、也不需要调用任何工具。
           与 c8 摘要、c9 记忆两处先例同口径。
        ② **`thinking_effort="off"`**：第一阶段只要一个词，开思考纯属浪费；
           第二阶段要的是判断不是长推理，且思考块不进 `text` 类型的块、
           收不到也用不上。
        ③ **`with` 必须包住整个 for 循环**，不能只包 `stream_chat(...)` 那一行。
           `stream_chat` 是生成器函数，调用它只造出生成器对象、函数体一行都
           没跑；真正产出请求事件是在**首次迭代**时。只包调用的话，作用域在
           迭代开始前就退出了，那条请求会被记进主对话的作用域——
           读记录的人会看到「用户只说了一句话却发了三轮请求」。
           这个坑 `context/manager.py` 里写着同一条。
        """
        parts: list[str] = []
        with self._recorder.scope(SCOPE_CLASSIFIER):
            for chunk in self._provider.stream_chat(
                messages, thinking_effort="off", tools=None, system=system
            ):
                if chunk.type == "error":
                    raise RuntimeError(chunk.content or "分类器流出错")
                if chunk.type == "text":
                    parts.append(chunk.content)
        return "".join(parts)

    def _fail(
        self,
        action: ReviewAction,
        reason: str,
        started: float,
        stage1_raw: str = "",
    ) -> Verdict:
        """
        统一处理一次判定失败：计入失败熔断、埋点、返回 FAILED。

        :param action: 待判动作
        :param reason: 失败原因（**会带进熔断快照的 detail**，F16 的价值所在）
        :param started: 起始时刻（`time.monotonic()`）
        :param stage1_raw: 第一阶段的原始输出（若已拿到）
        :returns: `FAILED` 结论

        副作用：更新熔断计数；产出一条行为记录。
        """
        state = self._breaker.record_failure(reason)
        return self._done(
            action,
            Verdict(VerdictKind.FAILED, reason),
            started,
            stage1_raw=stage1_raw,
            tripped=state,
        )

    def _done(
        self,
        action: ReviewAction,
        verdict: Verdict,
        started: float,
        stage1_raw: str = "",
        tripped: "Optional[BreakerState]" = None,
    ) -> Verdict:
        """
        补齐耗时、埋点、返回。**所有结论的唯一出口。**

        :param action: 待判动作
        :param verdict: 结论（`elapsed_ms` 尚未填）
        :param started: 起始时刻
        :param stage1_raw: 第一阶段原始输出
        :param tripped: 本次是否触发了熔断（非空即触发）
        :returns: 补齐耗时后的结论

        做成唯一出口与 `permission/engine.py` 的 `_verdict` 闭包同理由：
        埋点字段一条都不会漏填。逐条手写的话，将来加一个字段必然只加到
        某几条路径上，而那不报错——只表现为「有些判定在记录里缺字段」。

        副作用：产出一条 `classifier_verdict` 记录（**在锁外**）。
        """
        final = Verdict(
            kind=verdict.kind,
            reason=verdict.reason,
            staged=verdict.staged,
            cached=verdict.cached,
            elapsed_ms=int((time.monotonic() - started) * 1000),
        )
        state = tripped if tripped is not None else self._breaker.state()
        self._recorder.emit(
            TraceEventType.CLASSIFIER_VERDICT,
            scope_kind=action.scope,
            tool=action.tool_name,
            # 完整内容进负载（不截断），摘要行只取前若干字符——见 trace/reader.py。
            specifier=action.specifier,
            recipient=action.recipient,
            host=action.host,
            cwd=action.cwd,
            verdict=final.kind.value,
            reason=final.reason,
            staged=final.staged,
            cached=final.cached,
            elapsed_ms=final.elapsed_ms,
            # 第一阶段的原始输出：排查「为什么这条明明该过却进了第二阶段」时
            # 唯一有用的东西。解析器看不懂什么，只有原文说得清。
            stage1_raw=stage1_raw,
            breaker_tripped=state.tripped,
            breaker_reason=state.reason.value,
            breaker_detail=state.detail,
        )
        return final
