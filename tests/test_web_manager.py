"""
WebFetchManager 编排单测（web_fetch 扩展 T20，spec F15/F16 / AC22/AC23）。

provider 与 HTTP 客户端都用替身，全程离线。
"""

import unittest
from typing import Optional

import httpx

from rhinecode.provider.base import StreamChunk
from rhinecode.trace.models import SCOPE_WEB_EXTRACT
from rhinecode.web.manager import WebFetchManager

PUBLIC_ADDR = "93.184.216.34"


class _StubResponse:
    def __init__(self, status_code=200, headers=None, body=b""):
        self.status_code = status_code
        self.headers = httpx.Headers(headers or {})
        self._body = body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def iter_bytes(self):
        yield self._body


class _StubClient:
    def __init__(self, routes):
        self.routes = routes

    def stream(self, method, url, **kwargs):
        resp = self.routes.get(url)
        if resp is None:
            raise httpx.ConnectError("no route")
        return resp

    def close(self):
        pass


class _StubProvider:
    """记录调用参数的假 Provider。"""

    def __init__(self, answer: str = "抽取答案", raises: Optional[Exception] = None,
                 error_chunk: bool = False):
        self.answer = answer
        self.raises = raises
        self.error_chunk = error_chunk
        self.calls: list[dict] = []

    def stream_chat(self, messages, thinking_effort="off", tools=None, system=None):
        self.calls.append(
            {"messages": messages, "thinking_effort": thinking_effort,
             "tools": tools, "system": system}
        )
        if self.raises is not None:
            raise self.raises
        if self.error_chunk:
            yield StreamChunk(type="error", content="上游炸了")
            return
        yield StreamChunk(type="text", content=self.answer)
        yield StreamChunk(type="done", content="")


class _RecordingRecorder:
    """记录 scope 进出与其间产生的事件的假记录器。"""

    def __init__(self):
        self.enabled = True
        self.scopes: list[str] = []
        self.events_by_scope: list[tuple[str, str]] = []
        self._stack: list[str] = ["main"]

    def scope(self, name: str):
        outer = self

        class _Ctx:
            def __enter__(self_inner):
                outer.scopes.append(name)
                outer._stack.append(name)
                return self_inner

            def __exit__(self_inner, *exc):
                outer._stack.pop()
                return False

        return _Ctx()

    def note_event(self, kind: str) -> None:
        self.events_by_scope.append((self._stack[-1], kind))


def _manager(provider, routes=None, recorder=None, context_window=65536):
    return WebFetchManager(
        provider,
        context_window,
        recorder=recorder,
        client_factory=lambda: _StubClient(routes or {}),
        resolver=lambda host: [PUBLIC_ADDR],
    )


def _html_route(url, body="<p>页面正文</p>"):
    return {url: _StubResponse(headers={"content-type": "text/html; charset=utf-8"},
                               body=body.encode("utf-8"))}


class HappyPathTests(unittest.TestCase):
    def test_extraction_result_reaches_output(self) -> None:
        url = "https://a.test/x"
        p = _StubProvider(answer="这页在讲 A")
        out, summary = _manager(p, _html_route(url)).fetch_and_extract(url, "讲了什么")
        self.assertIn("这页在讲 A", out)
        self.assertIn("<untrusted-content", out)
        self.assertIn("抽取成功", summary)

    def test_tools_is_none(self) -> None:
        """
        **抽取请求不得携带任何工具**（spec F15 / AC22）。

        与 C8 摘要、C9 笔记同源的硬约束：模型在抽取阶段物理上无法调用工具。
        """
        url = "https://a.test/x"
        p = _StubProvider()
        _manager(p, _html_route(url)).fetch_and_extract(url, "q")
        self.assertEqual(len(p.calls), 1)
        self.assertIsNone(p.calls[0]["tools"])

    def test_system_prompt_declares_untrusted(self) -> None:
        url = "https://a.test/x"
        p = _StubProvider()
        _manager(p, _html_route(url)).fetch_and_extract(url, "q")
        self.assertIn("不可信", p.calls[0]["system"])

    def test_thinking_off(self) -> None:
        url = "https://a.test/x"
        p = _StubProvider()
        _manager(p, _html_route(url)).fetch_and_extract(url, "q")
        self.assertEqual(p.calls[0]["thinking_effort"], "off")


class NoProviderCallTests(unittest.TestCase):
    """抓取没拿到正文时**根本不发抽取请求**——既是正确性，也省一次 API 调用。"""

    def test_fetch_failure_skips_extraction(self) -> None:
        p = _StubProvider()
        out, _s = _manager(p, {}).fetch_and_extract("https://a.test/x", "q")
        self.assertEqual(p.calls, [], "抓取失败时不该调 provider")
        self.assertIn("抓取失败", out)

    def test_cross_host_redirect_skips_extraction(self) -> None:
        url = "https://a.test/1"
        routes = {url: _StubResponse(status_code=302,
                                     headers={"location": "https://b.test/2"})}
        p = _StubProvider()
        out, _s = _manager(p, routes).fetch_and_extract(url, "q")
        self.assertEqual(p.calls, [])
        self.assertIn("https://b.test/2", out)

    def test_binary_skips_extraction(self) -> None:
        url = "https://a.test/f.pdf"
        routes = {url: _StubResponse(headers={"content-type": "application/pdf",
                                              "content-length": "999"})}
        p = _StubProvider()
        out, _s = _manager(p, routes).fetch_and_extract(url, "q")
        self.assertEqual(p.calls, [])
        self.assertIn("二进制", out)

    def test_blank_page_skips_extraction(self) -> None:
        url = "https://a.test/empty"
        p = _StubProvider()
        out, _s = _manager(p, _html_route(url, "<div></div>")).fetch_and_extract(url, "q")
        self.assertEqual(p.calls, [])
        self.assertIn("降级", out)


class DegradationTests(unittest.TestCase):
    """抽取失败必须降级而不是失败（spec F16 / AC23）。"""

    def test_provider_exception_degrades(self) -> None:
        url = "https://a.test/x"
        p = _StubProvider(raises=RuntimeError("上游超时"))
        out, summary = _manager(p, _html_route(url)).fetch_and_extract(url, "q")
        self.assertIn("抽取：降级", out)
        self.assertIn("页面原文节选", out)
        self.assertIn("页面正文", out, "降级时要退回本地转换的正文")
        self.assertIn("已降级", summary)

    def test_error_chunk_degrades(self) -> None:
        url = "https://a.test/x"
        p = _StubProvider(error_chunk=True)
        out, _s = _manager(p, _html_route(url)).fetch_and_extract(url, "q")
        self.assertIn("抽取：降级", out)

    def test_empty_answer_degrades(self) -> None:
        url = "https://a.test/x"
        p = _StubProvider(answer="   ")
        out, _s = _manager(p, _html_route(url)).fetch_and_extract(url, "q")
        self.assertIn("抽取：降级", out)

    def test_degraded_body_still_wrapped(self) -> None:
        """降级路径的正文同样被不可信标记包裹——「抽取不是消毒」。"""
        url = "https://a.test/x"
        p = _StubProvider(raises=RuntimeError("x"))
        out, _s = _manager(p, _html_route(url)).fetch_and_extract(url, "q")
        self.assertIn("<untrusted-content", out)


class BudgetTests(unittest.TestCase):
    def test_content_budget_follows_context_window(self) -> None:
        """窗口变小时，喂给抽取模型的正文随之变短。"""
        url = "https://a.test/big"
        long_text = "文" * 60_000
        routes = _html_route(url, f"<p>{long_text}</p>")

        big = _StubProvider()
        _manager(big, routes, context_window=65536).fetch_and_extract(url, "q")
        small = _StubProvider()
        _manager(small, dict(routes), context_window=32768).fetch_and_extract(url, "q")

        big_len = len(big.calls[0]["messages"][0].content)
        small_len = len(small.calls[0]["messages"][0].content)
        self.assertGreater(big_len, small_len)


class TraceScopeTests(unittest.TestCase):
    def test_provider_call_wrapped_in_web_extract_scope(self) -> None:
        url = "https://a.test/x"
        rec = _RecordingRecorder()
        p = _StubProvider()
        _manager(p, _html_route(url), recorder=rec).fetch_and_extract(url, "q")
        self.assertIn(SCOPE_WEB_EXTRACT, rec.scopes)

    def test_scope_wraps_the_whole_iteration_not_just_the_call(self) -> None:
        """
        ⚠ `with` 必须包住**整个流消费循环**。

        `stream_chat` 是生成器函数——调用它只造出生成器对象、函数体一行都没跑，
        真正产出 api_request 事件是在**首次迭代**时。只包调用的话，作用域在迭代
        开始前就退出了，请求会被记成主作用域，`--scope web_extract` 返回空。

        这条用「在生成器 yield 时记一个事件、断言它落在 web_extract 作用域里」来验，
        而不是只断言 scope() 被调用过——后者在错误实现下照样通过。
        """
        url = "https://a.test/x"
        rec = _RecordingRecorder()

        class _NotingProvider(_StubProvider):
            def stream_chat(self, messages, thinking_effort="off", tools=None, system=None):
                # 模拟真实 Provider：事件在首次迭代时才产出。
                rec.note_event("api_request")
                yield StreamChunk(type="text", content="答案")
                yield StreamChunk(type="done", content="")

        _manager(_NotingProvider(), _html_route(url), recorder=rec).fetch_and_extract(url, "q")
        self.assertIn(
            (SCOPE_WEB_EXTRACT, "api_request"),
            rec.events_by_scope,
            "抽取请求被记进了错误的作用域——with 没有包住整个迭代循环",
        )


class NeverRaisesTests(unittest.TestCase):
    """
    契约：`fetch_and_extract` 对**普通异常**永不抛出，一律转成可读结果。

    ⚠ **边界：`BaseException`（KeyboardInterrupt / SystemExit）刻意不吞。**
    用 `except Exception` 而不是 `except BaseException` 是有意的——
    用户按 Ctrl-C / Esc 要能中断一次卡住的抓取，把中断信号吞掉才是 bug。
    """

    def test_ordinary_exceptions_become_text(self) -> None:
        url = "https://a.test/x"
        for exc in (RuntimeError("x"), ValueError("y"), OSError("z"), TypeError("w")):
            p = _StubProvider(raises=exc)
            out, _s = _manager(p, _html_route(url)).fetch_and_extract(url, "q")
            self.assertIn("降级", out, repr(exc))

    def test_keyboard_interrupt_propagates(self) -> None:
        url = "https://a.test/x"
        p = _StubProvider(raises=KeyboardInterrupt())
        with self.assertRaises(KeyboardInterrupt):
            _manager(p, _html_route(url)).fetch_and_extract(url, "q")


if __name__ == "__main__":
    unittest.main()
