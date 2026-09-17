"""
上下文管理编排器（c8 F3/F8/F14/F15/F16）。

ContextManager 是 Context 层唯一持有 provider 引用、唯一有副作用编排的模块，
把 LLM 摘要、会话级状态（估算锚点、熔断计数）串起来。

## ⚠ 2026-09-17：只剩一层了

c8 原设计是两层——第一层把过大的工具结果存盘、历史里只留占位（**不调模型**），
第二层在逼近窗口时调 LLM 摘要。**第一层已整层删除**，理由是它按绝对阈值触发
（合计 16 000 token），而那两个数字定于「DeepSeek 约 64K 窗口」的年代；窗口换成
1 000 000 之后它变成在**窗口的 1.6%** 处就开始删历史，真实 trace 实录里模型
连续十几轮拿不到自己刚读的文件、最终撞迭代上限、一个文件都没写出来。

对齐上游：Claude Code 与 Codex **都没有「事后删历史」这种机制**（在 Codex 整棵
树里搜 offload / spill 零命中），它们一律在工具**产出的那一刻**限量，进了历史
就不再动，唯一会改写已有历史的只有摘要，触发点在窗口的 90%～97%。

限量那一半现在由工具自己承担（`run_command.RUN_OUTPUT_MAX_CHARS` /
`read_file.READ_OUTPUT_MAX_CHARS`）——**那是唯一的体量闸门，别再往回加一层**。

对外方法（供 loop / conversation 调用）：
- before_request(history)：自动路径。每次请求前判断是否要摘要。
- record_usage(usage, sent_len)：用 API 返回的精确 prompt_tokens 更新估算锚点。
- manual_compact(history)：/compact 手动路径（更窄的 3K 余量）。
- usage_report(history)：/context 的只读文本报告。
- reset()：/clear 时清空所有会话级状态。

线程模型：预期在 TUI 的 Worker 线程内被调用（before_request 在 loop 里、manual_compact
在 /compact 生成器里），provider 调用是阻塞的，与现有同步执行模型一致。
"""

from typing import Optional

from rhinecode.hooks import HookEventType, NullHookManager
from rhinecode.provider.base import BaseProvider, Message
from rhinecode.context.estimate import estimate_tokens
from rhinecode.context.summarize import (
    SUMMARY_SYSTEM_PROMPT,
    compute_retain_index,
    parse_summary,
    reconstruct,
    render_transcript,
)
from rhinecode.context.models import CompactionNotice, ContextStats
from rhinecode.trace import (
    SCOPE_SUMMARY,
    NullRecorder,
    TraceEventType,
    TraceRecorderProtocol,
)

# 摘要连续失败达到此次数即熔断，避免在失败上死循环（F15）。
MAX_SUMMARY_FAILURES: int = 3

# 状态栏上下文段的高亮阈值：估算用量达到窗口的此比例即视为「接近上限」，
# 与自动压缩的 13K 余量线大致同一量级（64K 窗口下 80% ≈ 剩 ~13K），
# 让用户在自动摘要即将触发前就能从状态栏颜色感知到。
_WARN_RATIO: float = 0.8


def _abbrev(n: int) -> str:
    """
    把 token 数缩写成状态栏友好的短串：小于 1000 原样显示，否则按 KiB 保留一位小数，
    整千场景去掉多余的 `.0`（如 65536 → "64K"，12595 → "12.3K"，512 → "512"）。

    :param n: 待缩写的非负整数 token 数
    :returns: 缩写字符串
    """
    if n < 1000:
        return str(n)
    return f"{n / 1024:.1f}K".replace(".0K", "K")


# 自动触发余量占窗口的比例与上限。
#
# 与 `summarize.RETAIN_RATIO` 同因同源（已知项 #8）：余量原本是固定 13000，
# 在 8192 窗口下 `window - auto_margin` 直接变成**负数**，触发判据恒真——
# 每一次请求都尝试压缩，而保留区预算又比窗口还大导致每次都「无可摘要的早段」，
# 于是白跑一整轮判断、历史照样涨到溢出。
#
# 0.21 这个比例使**默认窗口 65536 及以上逐字维持原行为**：
# 65536 × 0.21 = 13762 > 13000，被上限夹回 13000。
MARGIN_RATIO: float = 0.21
MARGIN_CAP: int = 13000


def _derive_margin(window: int) -> int:
    """
    按窗口算出自动触发的安全余量。

    :param window: 上下文窗口上限（token）
    :returns: 余量 token 数；恒 < window，保证 `window - margin` 为正
              （否则触发判据恒真，压缩会在每一次请求上空转）

    副作用：无（纯函数）。
    """
    margin = min(int(window * MARGIN_RATIO), MARGIN_CAP)
    # 兜底：窗口小到离谱时也必须留出正的触发线。
    return max(1, min(margin, max(1, window - 1)))


class ContextManager:
    """
    编排两层压缩并维护会话级状态。构造于 ConversationManager（仅 DeepSeek 工具模式），
    跨消息长期持有，故估算锚点与熔断计数能在整个会话内累积。
    """

    def __init__(
        self,
        provider: BaseProvider,
        model: str,
        window: int,
        auto_margin: Optional[int] = None,
        recorder: "Optional[TraceRecorderProtocol]" = None,
        hook_manager=None,
    ) -> None:
        """
        :param provider: Provider，用于第二层摘要的 LLM 调用（复用 stream_chat）
        :param model: 模型名（当前仅备用/日志语义，摘要请求直接走 provider 默认模型）
        :param window: 上下文窗口上限（token），来自 config.context_window
        :param auto_margin: 自动触发的安全余量（防估算误差）。**缺省 None = 按窗口比例推导**，
                            见 `_derive_margin`；显式传值则原样采用（测试用）。
                            手动 /compact 不设余量阈值（用户主动触发即尽力压缩），故无对应参数。
        :param recorder: 行为记录器（trace 设施）。缺省 `NullRecorder()`，不传等于零回归。
        :param hook_manager: Hook 编排者（c12）。缺省 `NullHookManager()`，同样不传等于零回归。
        """
        self._recorder: TraceRecorderProtocol = recorder or NullRecorder()
        self._hooks = hook_manager if hook_manager is not None else NullHookManager()
        self._provider = provider
        self._model = model
        self.window = window
        self.auto_margin = (
            auto_margin if auto_margin is not None else _derive_margin(window)
        )
        # 估算锚点：上次 API 的精确 prompt_tokens 及其覆盖的历史条数；None 表示暂无锚点。
        self._anchor_tokens: Optional[int] = None
        self._anchor_len: int = 0
        # 熔断状态：连续失败计数与是否已熔断。
        self._summary_failures: int = 0
        self._circuit_broken: bool = False

    # ------------------------------------------------------------------ #
    # 估算
    # ------------------------------------------------------------------ #
    def _estimate(self, history: list[Message]) -> int:
        """按当前锚点估算历史 token 数（锚点+增量，见 estimate 模块）。"""
        return estimate_tokens(history, self._anchor_tokens, self._anchor_len)

    # ------------------------------------------------------------------ #
    # 自动路径：每次请求前
    # ------------------------------------------------------------------ #
    def before_request(
        self, history: list[Message], allow_summary: bool = True
    ) -> list[CompactionNotice]:
        """
        每次 API 请求前执行：按自动余量判断是否需要第二层摘要（F3）。

        ## ⚠ 这里曾经还有「第一层」，2026-09-17 删了

        原先是「先跑第一层存盘、再判断第二层」。第一层按**绝对阈值**触发
        （单条 4 000 token / 合计 16 000 token），而那两个数字定于「DeepSeek 约
        64K 窗口」的年代——窗口换成 1 000 000 之后，它变成在**窗口的 1.6%** 处
        就开始删历史里的工具结果。

        真实 trace 实录（修完存盘占位死循环之后跑的那一份）：一条「创建一个新的
        Agent」的任务跑满 25 轮迭代，17 次存盘里有 8 次删掉的是**模型还没看过**
        的内容——它读一个文件，下一轮开头那条结果就被换成 700 字的占位，于是
        照着占位的提示「缩小范围重调」，新结果又被删，如此往复；最后一个文件
        都没写出来。

        **别再往回加一层。** 限量已经收到工具产出的那一刻（见
        `run_command.RUN_OUTPUT_MAX_CHARS` / `read_file.READ_OUTPUT_MAX_CHARS`），
        那是上游两家的做法，也是唯一不会误删的位置——进了历史的东西在到达
        摘要触发线之前一个字都不动。

        :param history: 当前对话历史（摘要会原地重构这个列表）
        :param allow_summary: 是否允许第二层 LLM 摘要（c11 F21）。
            Skill 独立模式的子对话传 False——它跑的是另一条短历史，
            拿主历史的锚点估它毫无意义。**传 False 即整个方法不做任何事。**
        :returns: 本次发生的压缩动作通知列表（可能为空）

        副作用：可能原地重构 history、发起摘要 LLM 调用。
        """
        notices: list[CompactionNotice] = []
        # allow_summary 必须放在 and 链的**最前面**短路（c11 T34）：
        # _estimate 依赖的锚点对应的是**主历史**（record_usage 记的是主对话的
        # prompt_tokens 与主历史条数），而子对话传进来的是另一条短历史；
        # 拿主历史的锚点去估算它会得到一个毫无意义的值。放在最前面短路，
        # 可以保证 _estimate 在子对话路径上根本不会被调用。
        if (
            allow_summary
            and not self._circuit_broken
            and self._estimate(history) > self.window - self.auto_margin
        ):
            notices.append(self._do_summary(history))
        return notices

    # 关于子对话共享同一个 ContextManager 实例的已评估结论（c11）：
    # `_consecutive_failures` 熔断计数**不会被污染**。子对话恒不摘要
    # （allow_summary=False），根本走不到 `_do_summary`，也就不可能给
    # 主对话的熔断计数加一。
    #
    # （此处原先还有一条关于第一层存盘幂等集合的结论，随那一层于 2026-09-17 删除。）

    def record_usage(self, usage, sent_len: int) -> None:
        """
        用 API 返回的精确用量更新估算锚点。

        :param usage: 本轮 provider 返回的 Usage（取 prompt_tokens 作锚点）
        :param sent_len: 本轮发送时「纯历史消息」的条数（append reminder 前的 len(history)）

        副作用：更新 _anchor_tokens / _anchor_len。
        """
        if usage is None:
            return
        self._anchor_tokens = usage.prompt_tokens
        self._anchor_len = sent_len

    # ------------------------------------------------------------------ #
    # 手动路径：/compact
    # ------------------------------------------------------------------ #
    def manual_compact(self, history: list[Message]) -> CompactionNotice:
        """
        用户手动触发的第二层摘要（F14）。

        与自动路径不同，手动**不设余量阈值**：用户显式敲 /compact 就是主动要压，
        故不拿「窗口 − 余量」的闸门拦他，直接尝试摘要。但仍受一个物理约束——
        compute_retain_index 在历史没有够旧的早段（全部落在近 ~10K token 保留区）时
        返回 0，_do_summary 据此返回 noop「无可摘要的早段」；这是「确实没得压」而非拒绝。
        即便已熔断，用户显式请求仍尝试一次（不受自动熔断压制），但仍受单次失败计数影响。



        :param history: 当前对话历史（可能被原地重构）
        :returns: 压缩结果通知（summary / noop / circuit_break）

        副作用：可能发起摘要 LLM 调用并原地重构 history。
        """
        return self._do_summary(history, trigger="manual")

    # ------------------------------------------------------------------ #
    # 第二层摘要核心
    # ------------------------------------------------------------------ #
    def _do_summary(
        self, history: list[Message], trigger: str = "auto"
    ) -> CompactionNotice:
        """
        第二层摘要的**分发外壳**（c12）：在真正的摘要前后各产出一个 Hook 事件。

        :param history: 当前对话历史
        :param trigger: `"auto"`（余量不足自动触发）或 `"manual"`（用户敲 /compact）
        :returns: 与内层逐字相同的通知

        ## 为什么要有这层外壳

        内层 `_summarize` 有**四条 return 路径**（无早段 / 请求异常 / 解析失败 / 成功），
        逐条手写 `post_compact` 必漏一条，而漏了不报错——只是某种结局下事件凭空消失。
        用外壳把「每条 return 都要过它」变成结构上的必然，手法与
        `permission/engine.decide` 里那个唯一出口闭包 `_verdict` 相同。

        副作用：见 `_summarize`，另加两次 Hook 分发。
        """
        before_count = len(history)
        self._dispatch_compact(
            HookEventType.PRE_COMPACT, trigger=trigger, message_count=before_count
        )
        notice = self._summarize(history)
        self._dispatch_compact(
            HookEventType.POST_COMPACT,
            trigger=trigger,
            message_count=before_count,
            ok=(notice.kind == "summary"),
        )
        return notice

    def _dispatch_compact(self, event: "HookEventType", **fields) -> None:
        """分发一个压缩事件；无人监听时不构造负载，异常一律吞掉。"""
        if not self._hooks.has_listeners(event):
            return
        try:
            self._hooks.dispatch(event, lambda: dict(fields))
        except Exception:
            pass

    def _summarize(self, history: list[Message]) -> CompactionNotice:
        """
        执行一次 LLM 摘要并原地重构历史；封装失败与熔断处理（F11/F12/F15/N2）。

        流程：
        1. 算保留边界 idx；idx==0 表示没有可摘要的早段 → noop（不算失败）。
        2. 把 history[:idx] 渲染成 user 转录，配摘要系统提示、tools=None 调 provider。
        3. 收集文本、解析出正式摘要（丢草稿）；失败/空 → 计一次失败。
        4. 成功：history[:] 原地替换为 [摘要, 边界, *保留区]；锚点失效；失败计数清零。
        5. 失败累计达 MAX_SUMMARY_FAILURES → 置熔断，返回 circuit_break 通知。

        :param history: 当前对话历史（成功时原地重构）
        :returns: summary / noop / circuit_break 通知

        副作用：发起一次 provider.stream_chat（不带工具）；成功时原地修改 history 列表内容；
                更新 _anchor_tokens / _summary_failures / _circuit_broken。
        """
        idx = compute_retain_index(history, self.window)
        before_count = len(history)
        if idx <= 0:
            # 没有可摘要的早段（全部落在保留区），不动历史、不计失败。
            self._recorder.emit(
                TraceEventType.CONTEXT_COMPACTION,
                layer="summary",
                ok=False,
                retain_index=idx,
                before_count=before_count,
                after_count=before_count,
                skipped="no_early_segment",
            )
            return CompactionNotice(kind="noop", message="无可摘要的早段，已跳过压缩。")

        to_summarize = history[:idx]
        retained = history[idx:]

        try:
            text = self._call_summary_model(to_summarize)
        except Exception as e:  # noqa: BLE001 —— 任何异常都兜底为失败，绝不外抛（N2）
            self._trace_summary_failure(idx, before_count, f"摘要请求异常：{e}")
            return self._on_summary_failure(f"摘要请求异常：{e}")

        summary = parse_summary(text)
        if summary is None:
            self._trace_summary_failure(idx, before_count, "摘要模型未返回有效内容。")
            return self._on_summary_failure("摘要模型未返回有效内容。")

        # 成功：原地替换历史（用切片赋值保持 ConversationManager 持有的同一列表引用）。
        history[:] = reconstruct(summary, retained)
        # 历史索引已变，旧锚点失效——置 None，下次请求先全字符估算，待新 usage 重新锚定。
        self._anchor_tokens = None
        self._anchor_len = 0
        self._summary_failures = 0
        self._recorder.emit(
            TraceEventType.CONTEXT_COMPACTION,
            layer="summary",
            ok=True,
            retain_index=idx,
            before_count=before_count,
            after_count=len(history),
            summarized_count=len(to_summarize),
            retained_count=len(retained),
        )
        return CompactionNotice(
            kind="summary",
            message=f"已摘要早前 {len(to_summarize)} 条消息，保留近 {len(retained)} 条原文。",
        )

    def _trace_summary_failure(self, idx: int, before_count: int, reason: str) -> None:
        """
        记一条「第二层摘要失败」事件。

        失败计数与熔断标志要在**这里**读（而不是在 `_on_summary_failure` 之后），
        因为读 trace 时想知道的是「这是第几次连续失败、有没有因此熔断」——
        故意在 `_on_summary_failure` 之前调用，记的是本次失败**将被计入前**的状态
        加上「本次失败」这一事实，两者合起来就能推出熔断时机。
        """
        self._recorder.emit(
            TraceEventType.CONTEXT_COMPACTION,
            layer="summary",
            ok=False,
            retain_index=idx,
            before_count=before_count,
            after_count=before_count,
            reason=reason,
            prior_failures=self._summary_failures,
            circuit_broken=self._circuit_broken,
        )

    def _call_summary_model(self, to_summarize: list[Message]) -> str:
        """
        调 provider 生成摘要：把待摘要段转录成一条 user 消息，system 传摘要提示、不带工具。

        消费流式响应，只累积正文文本；遇到 error 块抛出异常交由上层计失败。

        :param to_summarize: 待摘要的历史消息
        :returns: 摘要模型输出的完整正文文本
        :raises RuntimeError: 流中出现 error 块时抛出（错误描述带上）
        """
        req = [Message(role="user", content=render_transcript(to_summarize))]
        parts: list[str] = []
        # 摘要调用包进 summary 作用域（trace F2）：它与主对话**共用同一个 Provider
        # 实例**，不区分的话摘要请求会被算进主对话的轮次计数，读 trace 时会看到
        # 「用户只说了一句话却发了两轮请求」这种假象。
        #
        # ⚠️ `with` 必须包住**整个 for 循环**而不只是 `stream_chat(...)` 那一行：
        # `stream_chat` 是生成器函数，调用它只是造出生成器对象、函数体一行都没跑；
        # 真正产出 `api_request` 事件是在**首次迭代**时。只包调用的话，作用域在
        # 迭代开始前就已退出，那条请求会被记成主作用域。
        with self._recorder.scope(SCOPE_SUMMARY):
            for chunk in self._provider.stream_chat(
                req, thinking_effort="off", tools=None, system=SUMMARY_SYSTEM_PROMPT
            ):
                if chunk.type == "error":
                    raise RuntimeError(chunk.content or "摘要流出错")
                if chunk.type == "text":
                    parts.append(chunk.content)
        return "".join(parts)

    def _on_summary_failure(self, reason: str) -> CompactionNotice:
        """
        统一处理一次摘要失败：累加计数，达阈值则熔断。

        :param reason: 失败原因（可读）
        :returns: 达阈值返回 circuit_break，否则返回 summary（失败）通知
        """
        self._summary_failures += 1
        if self._summary_failures >= MAX_SUMMARY_FAILURES:
            self._circuit_broken = True
            return CompactionNotice(
                kind="circuit_break",
                message=(
                    f"警告：摘要连续失败 {self._summary_failures} 次，已暂停自动压缩"
                    f"（{reason}）。可继续对话；/clear 或成功压缩后恢复。"
                ),
            )
        return CompactionNotice(kind="summary", message=f"本次摘要失败，未压缩（{reason}）。")

    # ------------------------------------------------------------------ #
    # 可观测与生命周期
    # ------------------------------------------------------------------ #
    def stats(self, history: list[Message]) -> ContextStats:
        """构造当前上下文用量快照（供 usage_report / 测试）。"""
        est = self._estimate(history)
        return ContextStats(
            estimated_tokens=est,
            window=self.window,
            headroom=self.window - est,
            circuit_broken=self._circuit_broken,
        )

    def status_line(self, history: list[Message]) -> tuple[str, bool]:
        """
        底部状态栏用的上下文用量一行摘要（对标 MCP 的 status_line）。

        基于只读的 stats() 组装，不重复估算逻辑、无副作用。格式如
        「上下文：19% · 12.3K/64K」，熔断时追加「 已熔断」提示。

        :param history: 当前对话历史
        :returns: (文本, 是否高亮预警)。高亮条件：估算占比达窗口的 _WARN_RATIO，
                  或第二层摘要已熔断（用橘色提醒用户接近上限 / 自动压缩已失效）。
        """
        s = self.stats(history)
        # window 恒 >0（config 的 _parse_int 保证），除零仅作防御性兜底。
        percent = round(s.estimated_tokens / s.window * 100) if s.window > 0 else 0
        text = f"上下文：{percent}% · {_abbrev(s.estimated_tokens)}/{_abbrev(s.window)}"
        warn = s.estimated_tokens >= s.window * _WARN_RATIO or s.circuit_broken
        if s.circuit_broken:
            # `⚠` 换成文字（F28/F30）：状态栏是单色单行，一个符号说不清
            # 「熔断」是什么意思，而这一段的全部作用就是让用户知道自动压缩停了
            text += " 已熔断"
        return text, warn

    def usage_report(self, history: list[Message]) -> str:
        """
        /context 命令的只读文本报告（F16）。

        :param history: 当前对话历史
        :returns: 多行中文报告
        """
        s = self.stats(history)
        lines = [
            "上下文用量（近似估算）",
            f"  估算 token：{s.estimated_tokens}",
            f"  窗口上限：{s.window}",
            f"  距上限余量：{s.headroom}",
        ]
        if s.circuit_broken:
            lines.append("  警告：自动摘要已熔断（连续失败），/clear 后恢复")
        return "\n".join(lines)

    def reset(self) -> None:
        """
        /clear 时重置所有会话级状态：锚点与熔断归零（F15 熔断复位）。

        副作用：清空锚点与熔断状态。
        """
        self._anchor_tokens = None
        self._anchor_len = 0
        self._summary_failures = 0
        self._circuit_broken = False
