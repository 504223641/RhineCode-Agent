"""
抓取器单测（web_fetch 扩展 T15/T16/T17）。

**全程离线**：`client_factory` 与 `resolver` 都注入替身。这既是可测性要求，
也是安全要求——测试套件不该因为跑测试而向外发出请求（spec N5）。

三组：
1. 单跳抓取骨架（文本/HTML/二进制/字节上限/超时）
2. 连接期硬校验（逐跳、解析结果、解析失败）
3. 重定向与同主机三元组
"""

import unittest
from typing import Optional

import httpx

from rhinecode.web import fetcher
from rhinecode.web.fetcher import CONNECT_PHASE_PREFIX, MAX_REDIRECTS, MAX_RESPONSE_BYTES

# 替身 resolver 用的公网地址。
#
# ⚠ **必须是 is_global 为真的地址。** 返回 127.0.0.1 会被连接期硬校验一律拒掉，
# 而失败文案恰好是「解析到了不允许访问的目标」，看起来像功能坏了。
# 也**不要用 TEST-NET 三段**（203.0.113.x / 198.51.100.x / 192.0.2.x）——
# Python 把它们判为 is_private=True / is_global=False，一样会被拒。
PUBLIC_ADDR = "93.184.216.34"


def _assert_stub_addr_is_usable() -> None:
    """替身地址的自校验：选错会当场红，而不是等到一堆用例莫名其妙失败。"""
    import ipaddress

    ip = ipaddress.ip_address(PUBLIC_ADDR)
    assert ip.is_global and not ip.is_multicast, (
        f"替身地址 {PUBLIC_ADDR} 过不了连接期硬校验，全部用例都会失败"
    )


_assert_stub_addr_is_usable()


class _StubResponse:
    """`client.stream(...)` 上下文管理器返回的假响应。"""

    def __init__(self, status_code=200, headers=None, body=b"", chunk_size=None):
        self.status_code = status_code
        self.headers = httpx.Headers(headers or {})
        self._body = body
        self._chunk_size = chunk_size or max(1, len(body) or 1)
        self.body_read = False

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def iter_bytes(self):
        self.body_read = True
        for i in range(0, len(self._body), self._chunk_size):
            yield self._body[i : i + self._chunk_size]


class _StubClient:
    """按 URL 返回预置响应的假客户端；也可配置成抛异常。"""

    def __init__(self, routes: dict, raises: Optional[Exception] = None):
        self.routes = routes
        self.raises = raises
        self.requested: list[str] = []
        self.closed = False
        self.last_response: Optional[_StubResponse] = None

    def stream(self, method, url, **kwargs):
        self.requested.append(url)
        if self.raises is not None:
            raise self.raises
        resp = self.routes.get(url)
        if resp is None:
            raise AssertionError(f"用例没有为 {url} 预置响应")
        self.last_response = resp
        return resp

    def close(self):
        self.closed = True


def _fetch(url, routes=None, resolver=None, raises=None, client=None):
    stub = client or _StubClient(routes or {}, raises=raises)
    outcome = fetcher.fetch(
        url,
        client_factory=lambda: stub,
        resolver=resolver or (lambda host: [PUBLIC_ADDR]),
    )
    return outcome, stub


# =============================================================================
# 一、单跳抓取骨架（T15）
# =============================================================================
class PlainFetchTests(unittest.TestCase):
    def test_text_response(self) -> None:
        url = "https://a.test/x"
        routes = {url: _StubResponse(headers={"content-type": "text/plain; charset=utf-8"},
                                     body="纯文本内容".encode("utf-8"))}
        out, _ = _fetch(url, routes)
        self.assertTrue(out.ok)
        self.assertEqual(out.text, "纯文本内容")
        self.assertEqual(out.charset, "utf-8")
        self.assertEqual(out.source_url, url)
        self.assertEqual(out.final_url, url)

    def test_html_is_converted(self) -> None:
        url = "https://a.test/p"
        html = "<html><body><p>正文</p><script>bad()</script></body></html>"
        routes = {url: _StubResponse(headers={"content-type": "text/html; charset=utf-8"},
                                     body=html.encode("utf-8"))}
        out, _ = _fetch(url, routes)
        self.assertIn("正文", out.text)
        self.assertNotIn("bad()", out.text)

    def test_gbk_html_decoded(self) -> None:
        url = "https://a.test/gbk"
        html = "<html><body><p>中文文档</p></body></html>"
        routes = {url: _StubResponse(headers={"content-type": "text/html; charset=gbk"},
                                     body=html.encode("gbk"))}
        out, _ = _fetch(url, routes)
        self.assertIn("中文文档", out.text)
        self.assertEqual(out.charset, "gbk")

    def test_json_treated_as_text(self) -> None:
        url = "https://a.test/api"
        routes = {url: _StubResponse(headers={"content-type": "application/json"},
                                     body=b'{"k":1}')}
        out, _ = _fetch(url, routes)
        self.assertIn('"k"', out.text)
        self.assertFalse(out.binary)

    def test_missing_content_type_treated_as_text(self) -> None:
        # 多数简陋服务器不发 Content-Type，一律当二进制会让它们全部抓不到内容。
        url = "https://a.test/raw"
        routes = {url: _StubResponse(body=b"hello")}
        out, _ = _fetch(url, routes)
        self.assertEqual(out.text, "hello")


class BinaryResponseTests(unittest.TestCase):
    def test_binary_reports_type_and_size_without_reading_body(self) -> None:
        """
        二进制类型只回报类型与体量，**且不读响应体**。

        先看头再决定读不读，是为了避免「先下载 5 MiB 再丢弃」。
        `body_read` 就是用来钉这一点的。
        """
        url = "https://a.test/f.pdf"
        resp = _StubResponse(
            headers={"content-type": "application/pdf", "content-length": "12345"},
            body=b"%PDF-1.7 binary junk",
        )
        out, _ = _fetch(url, {url: resp})
        self.assertTrue(out.ok)
        self.assertTrue(out.binary)
        self.assertEqual(out.text, "")
        self.assertEqual(out.content_length, 12345)
        self.assertFalse(resp.body_read, "二进制响应不该读取正文")

    def test_binary_without_content_length(self) -> None:
        url = "https://a.test/f.zip"
        routes = {url: _StubResponse(headers={"content-type": "application/zip"})}
        out, _ = _fetch(url, routes)
        self.assertTrue(out.binary)
        self.assertEqual(out.content_length, -1)


class SizeLimitTests(unittest.TestCase):
    def test_over_limit_truncated_and_flagged(self) -> None:
        url = "https://a.test/big"
        body = b"x" * (MAX_RESPONSE_BYTES + 5000)
        routes = {url: _StubResponse(headers={"content-type": "text/plain"},
                                     body=body, chunk_size=64 * 1024)}
        out, _ = _fetch(url, routes)
        self.assertTrue(out.ok)
        self.assertTrue(out.bytes_truncated)
        self.assertLessEqual(len(out.text), MAX_RESPONSE_BYTES)

    def test_under_limit_not_flagged(self) -> None:
        url = "https://a.test/small"
        routes = {url: _StubResponse(headers={"content-type": "text/plain"}, body=b"abc")}
        out, _ = _fetch(url, routes)
        self.assertFalse(out.bytes_truncated)


class FailurePathTests(unittest.TestCase):
    def test_timeout_becomes_failure(self) -> None:
        out, _ = _fetch("https://a.test/x", raises=httpx.ReadTimeout("slow"))
        self.assertFalse(out.ok)
        self.assertIn("超时", out.error)

    def test_http_error_becomes_failure(self) -> None:
        out, _ = _fetch("https://a.test/x", raises=httpx.ConnectError("refused"))
        self.assertFalse(out.ok)
        self.assertIn("抓取失败", out.error)

    def test_never_raises(self) -> None:
        """契约：本函数永不抛异常，一切失败都转成 ok=False 的结果。"""
        out, _ = _fetch("https://a.test/x", raises=RuntimeError("意外"))
        self.assertFalse(out.ok)

    def test_client_is_closed(self) -> None:
        url = "https://a.test/x"
        _out, stub = _fetch(url, {url: _StubResponse(body=b"ok")})
        self.assertTrue(stub.closed)


# =============================================================================
# 二、连接期硬校验（T16）
# =============================================================================
class ConnectPhaseGuardTests(unittest.TestCase):
    def test_private_resolution_blocks_fetch(self) -> None:
        """域名解析到私网地址 → 整次抓取失败。"""
        out, stub = _fetch("https://a.test/x", {}, resolver=lambda h: ["10.0.0.1"])
        self.assertFalse(out.ok)
        self.assertIn(CONNECT_PHASE_PREFIX, out.error)
        self.assertEqual(stub.requested, [], "被守卫拦下时不该发出任何请求")

    def test_loopback_resolution_blocks_fetch(self) -> None:
        out, _ = _fetch("https://a.test/x", {}, resolver=lambda h: ["127.0.0.1"])
        self.assertFalse(out.ok)

    def test_any_forbidden_address_in_list_blocks(self) -> None:
        # 一个主机名可能解析出多个地址；**任一**落在禁止范围即整次失败。
        out, _ = _fetch("https://a.test/x", {}, resolver=lambda h: [PUBLIC_ADDR, "169.254.169.254"])
        self.assertFalse(out.ok)

    def test_resolution_failure_blocks(self) -> None:
        def boom(host):
            raise OSError("no such host")

        out, _ = _fetch("https://a.test/x", {}, resolver=boom)
        self.assertFalse(out.ok)
        self.assertIn(CONNECT_PHASE_PREFIX, out.error)

    def test_empty_resolution_blocks(self) -> None:
        out, _ = _fetch("https://a.test/x", {}, resolver=lambda h: [])
        self.assertFalse(out.ok)

    def test_hard_check_runs_at_connect_phase_too(self) -> None:
        # 连接期也跑 check_hard：一个 file:// 地址即使绕过了判定期也进不来。
        out, _ = _fetch("file:///C:/x", {})
        self.assertFalse(out.ok)
        self.assertIn(CONNECT_PHASE_PREFIX, out.error)

    def test_failure_message_distinguishable_from_permission_denial(self) -> None:
        """
        连接期失败**不是**权限拒绝，文案必须可区分（spec F24 第三类）。

        模型需要知道「这不是权限配置问题」，否则它会去建议用户改 permissions.yaml。
        """
        out, _ = _fetch("https://a.test/x", {}, resolver=lambda h: ["10.0.0.1"])
        self.assertIn(CONNECT_PHASE_PREFIX, out.error)
        self.assertNotIn("网络边界拒绝", out.error)


# =============================================================================
# 三、重定向与同主机三元组（T17）
# =============================================================================
def _redirect(to: str, status: int = 302) -> _StubResponse:
    return _StubResponse(status_code=status, headers={"location": to})


class RedirectTests(unittest.TestCase):
    def test_same_host_followed(self) -> None:
        a, b = "https://a.test/1", "https://a.test/2"
        routes = {a: _redirect(b),
                  b: _StubResponse(headers={"content-type": "text/plain"}, body=b"done")}
        out, stub = _fetch(a, routes)
        self.assertTrue(out.ok)
        self.assertEqual(out.text, "done")
        self.assertEqual(out.source_url, a)
        self.assertEqual(out.final_url, b)
        self.assertEqual(stub.requested, [a, b])

    def test_relative_location_resolved(self) -> None:
        a, b = "https://a.test/dir/1", "https://a.test/dir/2"
        routes = {a: _redirect("2"),
                  b: _StubResponse(headers={"content-type": "text/plain"}, body=b"ok")}
        out, _ = _fetch(a, routes)
        self.assertEqual(out.final_url, b)

    def test_cross_host_not_followed(self) -> None:
        """
        跨主机重定向**不跟随**——自动跟随等于给域名策略开后门：
        放行 a.test 之后，它只要回一个 302 就能把抓取导向任意地址。
        """
        a, b = "https://a.test/1", "https://b.test/2"
        out, stub = _fetch(a, {a: _redirect(b)})
        self.assertTrue(out.ok)
        self.assertEqual(out.redirect_to, b)
        self.assertEqual(out.text, "")
        self.assertEqual(stub.requested, [a], "不该请求跨主机目标")

    def test_different_port_treated_as_cross_host(self) -> None:
        """
        端口不同即按跨主机处理。

        域名规则只匹配主机名，但重定向的跟随发生在权限判定**之后**、
        没有第二次复核，所以这里的判据必须比域名规则更严。
        """
        a, b = "https://a.test/1", "https://a.test:8443/2"
        out, _ = _fetch(a, {a: _redirect(b)})
        self.assertEqual(out.redirect_to, b)

    def test_different_scheme_treated_as_cross_host(self) -> None:
        a, b = "https://a.test/1", "http://a.test/2"
        out, _ = _fetch(a, {a: _redirect(b)})
        self.assertEqual(out.redirect_to, b)

    def test_default_port_normalized_as_same_host(self) -> None:
        # https://a.test 与 https://a.test:443 是同主机。
        a, b = "https://a.test/1", "https://a.test:443/2"
        routes = {a: _redirect(b),
                  b: _StubResponse(headers={"content-type": "text/plain"}, body=b"ok")}
        out, _ = _fetch(a, routes)
        self.assertIsNone(out.redirect_to)
        self.assertEqual(out.text, "ok")

    def test_credentialed_same_host_redirect_blocked_by_per_hop_check(self) -> None:
        """
        **本组最重要的一条**：`https://a.test/x` → `http://user:pass@a.test/`。

        它同时证明两件事：
        - 协议不同已按跨主机处理（不跟随）；
        - 即便判为同主机，逐跳 check_hard 也会在下一跳把内嵌凭据拦下。

        若 check_hard 只在判定期对原始地址跑一次，这两项限制对新地址完全失效。
        """
        a = "https://a.test/x"
        target = "http://user:pass@a.test/"
        out, stub = _fetch(a, {a: _redirect(target)})
        self.assertNotEqual(out.text, "credential-leak")
        self.assertEqual(stub.requested, [a], "不该向带凭据的地址发出请求")

    def test_same_host_credentialed_redirect_is_guarded(self) -> None:
        # 构造一个**同协议同端口**、只多了凭据的跳转，确保逐跳守卫真的拦得住。
        a = "https://a.test/x"
        target = "https://user:pass@a.test/y"
        out, stub = _fetch(a, {a: _redirect(target)})
        # 同主机 → 会续跳 → 下一跳的 check_hard 命中凭据限制 → 整次失败
        self.assertFalse(out.ok)
        self.assertIn(CONNECT_PHASE_PREFIX, out.error)
        self.assertEqual(stub.requested, [a])

    def test_redirect_loop_hits_limit(self) -> None:
        a, b = "https://a.test/1", "https://a.test/2"
        routes = {a: _redirect(b), b: _redirect(a)}
        out, stub = _fetch(a, routes)
        self.assertFalse(out.ok)
        self.assertIn("重定向次数超过上限", out.error)
        self.assertEqual(len(stub.requested), MAX_REDIRECTS + 1)

    def test_redirect_without_location(self) -> None:
        a = "https://a.test/1"
        out, _ = _fetch(a, {a: _StubResponse(status_code=302)})
        self.assertFalse(out.ok)
        self.assertIn("跳转目标", out.error)


class OfflineDisciplineTest(unittest.TestCase):
    """本文件不得发出任何真实网络请求或域名解析。"""

    def test_no_real_network_used(self) -> None:
        import socket as socket_module
        from unittest import mock

        url = "https://a.test/x"
        routes = {url: _StubResponse(headers={"content-type": "text/plain"}, body=b"ok")}
        # 把三个入口都断掉：getaddrinfo 是关键那个——判定/连接期最可能误引入的
        # I/O 就是域名解析，而它不经过 socket.socket。
        with mock.patch.object(socket_module, "getaddrinfo", side_effect=AssertionError("联网了")), \
             mock.patch.object(socket_module, "create_connection", side_effect=AssertionError("联网了")), \
             mock.patch.object(socket_module, "socket", side_effect=AssertionError("联网了")):
            out, _ = _fetch(url, routes)
        self.assertTrue(out.ok)


if __name__ == "__main__":
    unittest.main()
