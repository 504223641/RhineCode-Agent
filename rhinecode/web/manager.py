"""
`WebFetchManager`（web_fetch 扩展 spec F15/F16）：抓取 + 抽取的编排。

**本模块是 `web` 包里唯一持有 provider 引用、唯一有副作用编排的地方。**
这条分工沿用 `context/manager.py` 与 `memory/manager.py` 的既有约定：
一层里只有 manager 有副作用，其余模块（decode / convert / extract / render）
保持纯逻辑，可脱离网络与模型单测。

## 一次调用的流程

    fetcher.fetch()
      ├─ 失败 / 跨主机重定向 / 二进制  → 直接 render，**不进抽取**（也省一次 API 调用）
      └─ 成功
           ├─ truncate 到 content_budget(context_window)
           ├─ build_extract_request → provider.stream_chat(tools=None)
           │    └─ 异常 / 空返回 → 降级为原文节选
           └─ render（不可信标记 + 元信息）
"""

from typing import Callable, Optional

from rhinecode.provider.base import BaseProvider
from rhinecode.trace import NullRecorder, TraceRecorderProtocol
from rhinecode.trace.models import SCOPE_WEB_EXTRACT
from rhinecode.web import extract as extract_mod
from rhinecode.web import fetcher, render
from rhinecode.web.convert import truncate
from rhinecode.web.models import ExtractOutcome


class WebFetchManager:
    """
    抓取与抽取的编排者。

    :ivar _provider: 抽取阶段用的 Provider（与主对话**共用同一个实例**）
    :ivar _context_window: 配置的上下文窗口，用于算正文预算
    :ivar _recorder: 行为记录器（缺省 NullRecorder）
    :ivar _client_factory: HTTP 客户端工厂，透传给 fetcher
    :ivar _resolver: 主机名解析函数，透传给 fetcher

    ## 没有 extract_model 参数

    第 2 轮 plan 曾设计过「抽取用一个更便宜的模型」，实现时发现做不到：
    换模型的既有旁路（`ConversationManager._provider_for`）需要整份 `Config`
    且是协调层的**私有方法**，`web` 作为叶子包复用不了——「复用既有机制」是假的，
    只能新造第二份实现。按 YAGNI 直接砍掉，抽取一律沿用全局 provider。
    """

    def __init__(
        self,
        provider: BaseProvider,
        context_window: int,
        *,
        recorder: Optional[TraceRecorderProtocol] = None,
        client_factory: Optional[Callable] = None,
        resolver: Optional[Callable] = None,
    ) -> None:
        self._provider = provider
        self._context_window = context_window
        self._recorder = recorder if recorder is not None else NullRecorder()
        self._client_factory = client_factory
        self._resolver = resolver

    def fetch_and_extract(self, url: str, ask: str) -> tuple[bool, str, str]:
        """
        抓取一个地址并按提问抽取要点。

        :param url: 目标地址（已通过权限判定）
        :param ask: 「要从这页提取什么」
        :returns: `(抓取是否成功, 回灌模型的完整文本, TUI 单行摘要)`

        **第一项不是「本函数有没有出错」，而是「这次抓取有没有拿到内容」。**
        它决定 `ToolResult.ok`，进而决定 TUI 把这一行显示成绿色还是红色。
        不区分的话，一次被连接期守卫拦下的抓取会显示成**绿色成功**、
        只是正文里写着「抓取失败」——界面在撒谎。

        跨主机重定向算「没拿到内容」（`ok=False`）：本次确实没抓到东西，
        模型需要对新地址再发一次。二进制内容算**成功**——我们如实回报了
        它的类型与体量，那就是这次调用能给出的全部信息。

        **对普通异常永不抛出**：抓取失败、抽取失败都转成可读的结果文本。

        ⚠ **边界**：`BaseException`（`KeyboardInterrupt` / `SystemExit`）**刻意不吞**。
        下面用的是 `except Exception` 而不是 `except BaseException`——用户按 Ctrl-C / Esc
        要能中断一次卡住的抓取，把中断信号吞掉才是 bug。

        副作用：发起 HTTP 请求；成功时再发起一次模型请求（除非注入了替身）。
        """
        outcome = fetcher.fetch(
            url,
            client_factory=self._client_factory,
            resolver=self._resolver,
        )

        # 抓取没拿到正文的三种情形，都不进抽取——既是正确性，也省一次 API 调用。
        if (not outcome.ok) or outcome.redirect_to or outcome.binary:
            got_content = outcome.ok and not outcome.redirect_to
            return got_content, render.render(outcome), render.summary(outcome)

        budget = extract_mod.content_budget(self._context_window)
        page_text, _cut = truncate(outcome.text, budget)

        extracted = self._extract(page_text, outcome.source_url, ask)
        return True, render.render(outcome, extracted), render.summary(outcome, extracted)

    # ------------------------------------------------------------------ #
    # 抽取
    # ------------------------------------------------------------------ #
    def _extract(self, page_text: str, source_url: str, ask: str) -> ExtractOutcome:
        """
        发起一次抽取请求；任何失败都降级为原文节选。

        :param page_text: 已截断的正文
        :param source_url: 来源地址（写进不可信标记）
        :param ask: 调用方的提问
        :returns: ExtractOutcome（ok=False 表示走了降级路径）

        副作用：发起一次 provider 请求。
        """
        if not page_text.strip():
            return extract_mod.fallback_outcome(page_text, "页面没有可读的正文内容")

        system, messages = extract_mod.build_extract_request(page_text, source_url, ask)
        try:
            answer = self._call_provider(system, messages)
        except Exception as exc:  # noqa: BLE001 —— 抽取失败必须降级而不是失败
            # 降级而不是失败：抽取多一次网络往返即多一条失败路径，
            # 不降级的话一次抖动就让整个工具不可用（spec F16）。
            return extract_mod.fallback_outcome(page_text, f"抽取请求失败：{exc}")

        result = extract_mod.parse_extract_result(answer)
        if not result.ok:
            # 空返回同样降级，但保留 parse 给出的原因。
            return extract_mod.fallback_outcome(page_text, result.degraded_reason)
        return result

    def _call_provider(self, system: str, messages: list) -> str:
        """
        调一次 provider 并累积正文。

        :param system: 抽取系统提示
        :param messages: 消息列表（一条 user）
        :returns: 模型返回的完整正文
        :raises RuntimeError: 流中出现 error 块

        **`tools=None` 是硬约束**（spec F15）：抽取阶段模型在物理上无法调用任何工具。
        与 C8 摘要、C9 记忆同源。

        ⚠️ `with` 必须包住**整个 for 循环**而不只是 `stream_chat(...)` 那一行：
        `stream_chat` 是生成器函数，调用它只是造出生成器对象、函数体一行都没跑；
        真正产出 `api_request` 事件是在**首次迭代**时。只包调用的话，作用域在迭代
        开始前就已退出，那条请求会被记成主作用域——`--scope web_extract` 会返回空，
        看起来像「没记录到」而不是「记错作用域」。
        （这条不是新发现，`context/manager.py` 有一段专门为此写的注释，照抄它的形态。）
        """
        parts: list[str] = []
        with self._recorder.scope(SCOPE_WEB_EXTRACT):
            for chunk in self._provider.stream_chat(
                messages, thinking_effort="off", tools=None, system=system
            ):
                if chunk.type == "error":
                    raise RuntimeError(chunk.content or "抽取流出错")
                if chunk.type == "text":
                    parts.append(chunk.content)
        return "".join(parts)
