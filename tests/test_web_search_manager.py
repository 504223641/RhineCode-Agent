"""搜索编排层单测（web_search 扩展 T9，spec F13/F21 · AC18–AC23）。

全程离线：所有用例都注入替身客户端，**不发出任何真实请求**（spec N5）。
"""

import threading
import unittest

from rhinecode.web.search import (
    BRAVE,
    FAILURE_NO_KEY,
    FAILURE_QUOTA,
    FAILURE_SERVICE,
)
from rhinecode.web.search_manager import WebSearchManager


# ---------------------------------------------------------------------------
# 替身
# ---------------------------------------------------------------------------
class _Response:
    """一个够用的 httpx.Response 替身。"""

    def __init__(self, payload, status_code: int = 200) -> None:
        self._payload = payload
        self.status_code = status_code

    def json(self):
        return self._payload


class _Client:
    """记录调用参数的 HTTP 客户端替身。"""

    def __init__(self, payload, status_code: int = 200, raises=None) -> None:
        self._payload = payload
        self._status = status_code
        self._raises = raises
        self.calls: list = []
        self.closed = False

    def get(self, url, params=None, headers=None, timeout=None):
        self.calls.append({"url": url, "params": params, "headers": headers, "timeout": timeout})
        if self._raises is not None:
            raise self._raises
        return _Response(self._payload, self._status)

    def close(self) -> None:
        self.closed = True


_ONE_RESULT = {"web": {"results": [{"title": "T", "url": "https://a.test/1", "description": "D"}]}}


class _Factory:
    """`client_factory` 替身，顺带统计「工厂被调了几次」= 「发了几次请求」。"""

    def __init__(self, payload=_ONE_RESULT, status_code: int = 200, raises=None) -> None:
        self._payload = payload
        self._status = status_code
        self._raises = raises
        self.clients: list = []

    def __call__(self) -> _Client:
        client = _Client(self._payload, self._status, self._raises)
        self.clients.append(client)
        return client

    @property
    def call_count(self) -> int:
        return len(self.clients)


def _manager(factory=None, *, api_key="k", quota=50, max_results=5, endpoint="") -> WebSearchManager:
    return WebSearchManager(
        BRAVE, api_key, endpoint, max_results, quota, 10.0, client_factory=factory
    )


# ---------------------------------------------------------------------------
class HappyPathTests(unittest.TestCase):
    def test_success(self) -> None:
        factory = _Factory()
        outcome = _manager(factory).search("httpx 超时")
        self.assertTrue(outcome.ok)
        self.assertEqual(outcome.failure, "")
        self.assertEqual(len(outcome.results), 1)
        self.assertEqual(outcome.results[0].url, "https://a.test/1")
        self.assertEqual((outcome.used, outcome.limit), (1, 50))

    def test_request_shape(self) -> None:
        """spec F2：只发 GET、只带鉴权头与 Accept，没有请求体与其它头。"""
        factory = _Factory()
        _manager(factory).search("httpx 超时", count=3)
        call = factory.clients[0].calls[0]
        self.assertEqual(call["params"], {"q": "httpx 超时", "count": 3})
        self.assertEqual(
            sorted(call["headers"]), ["Accept", "X-Subscription-Token"]
        )
        self.assertEqual(call["timeout"], 10.0)

    def test_query_sent_verbatim(self) -> None:
        """spec F8/AC7：不做任何过滤或改写——含项目名的查询词原样发出去。"""
        factory = _Factory()
        query = "RhineCode PermissionEngine decide AttributeError"
        _manager(factory).search(query)
        self.assertEqual(factory.clients[0].calls[0]["params"]["q"], query)

    def test_count_defaults_to_config(self) -> None:
        factory = _Factory()
        _manager(factory, max_results=8).search("q")
        self.assertEqual(factory.clients[0].calls[0]["params"]["count"], 8)

    def test_count_clamped(self) -> None:
        factory = _Factory()
        m = _manager(factory)
        m.search("q", count=0)
        m.search("q", count=99)
        self.assertEqual(factory.clients[0].calls[0]["params"]["count"], 1)
        self.assertEqual(factory.clients[1].calls[0]["params"]["count"], 10)

    def test_custom_endpoint_used(self) -> None:
        factory = _Factory()
        _manager(factory, endpoint="https://proxy.internal/search").search("q")
        self.assertEqual(factory.clients[0].calls[0]["url"], "https://proxy.internal/search")

    def test_client_closed(self) -> None:
        factory = _Factory()
        _manager(factory).search("q")
        self.assertTrue(factory.clients[0].closed)


class NoKeyTests(unittest.TestCase):
    def test_no_request_and_no_count(self) -> None:
        """spec F13：未配置密钥时不发请求，因此**不计配额**。"""
        factory = _Factory()
        manager = _manager(factory, api_key="")
        outcome = manager.search("q")
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.failure, FAILURE_NO_KEY)
        self.assertEqual(factory.call_count, 0)
        self.assertEqual(manager.quota_state(), (0, 50))

    def test_whitespace_key_treated_as_missing(self) -> None:
        self.assertEqual(_manager(_Factory(), api_key="   ").search("q").failure, FAILURE_NO_KEY)


class QuotaTests(unittest.TestCase):
    def test_limit_enforced_and_no_request(self) -> None:
        factory = _Factory()
        manager = _manager(factory, quota=1)
        self.assertTrue(manager.search("a").ok)
        second = manager.search("b")
        self.assertFalse(second.ok)
        self.assertEqual(second.failure, FAILURE_QUOTA)
        # 第二次**没有**发请求。
        self.assertEqual(factory.call_count, 1)

    def test_zero_means_unlimited(self) -> None:
        factory = _Factory()
        manager = _manager(factory, quota=0)
        for _ in range(5):
            self.assertTrue(manager.search("q").ok)
        self.assertEqual(manager.quota_state(), (5, 0))

    def test_reset(self) -> None:
        manager = _manager(_Factory(), quota=2)
        manager.search("a")
        manager.reset_quota()
        self.assertEqual(manager.quota_state(), (0, 2))
        self.assertTrue(manager.search("b").ok)

    def test_concurrent_take_respects_limit(self) -> None:
        """
        ⚠ **必须用真线程。** 串行循环在一个「成功后再 +1」的错误实现下也会通过，
        而那种实现在并发下会让四个线程同时读到 `used == 49`、同时判定没超、
        同时发出请求。

        20 个线程抢 10 个名额，成功次数必须**恰好 10**。
        """
        factory = _Factory()
        manager = _manager(factory, quota=10)
        results: list = []
        lock = threading.Lock()
        barrier = threading.Barrier(20)

        def worker() -> None:
            barrier.wait()  # 尽量让 20 个线程同时冲进 _take_quota
            outcome = manager.search("q")
            with lock:
                results.append(outcome.ok)

        threads = [threading.Thread(target=worker) for _ in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(sum(1 for ok in results if ok), 10)
        self.assertEqual(sum(1 for ok in results if not ok), 10)
        self.assertEqual(manager.quota_state(), (10, 10))
        self.assertEqual(factory.call_count, 10)


class ServiceFailureTests(unittest.TestCase):
    """三种「请求发出去了但没拿到东西」的情形，都要退回配额（spec F13）。"""

    def test_exception_releases_quota(self) -> None:
        factory = _Factory(raises=RuntimeError("连接被重置"))
        manager = _manager(factory)
        outcome = manager.search("q")
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.failure, FAILURE_SERVICE)
        self.assertIn("连接被重置", outcome.error)
        self.assertEqual(manager.quota_state(), (0, 50))

    def test_non_2xx_releases_quota(self) -> None:
        factory = _Factory(status_code=429)
        manager = _manager(factory)
        outcome = manager.search("q")
        self.assertEqual(outcome.failure, FAILURE_SERVICE)
        self.assertIn("429", outcome.error)
        self.assertEqual(manager.quota_state(), (0, 50))

    def test_unparsable_payload_releases_quota(self) -> None:
        """`parse` 返回 `None`（结构不认识）→ 失败、退配额。"""
        factory = _Factory(payload={"unexpected": "shape"})
        manager = _manager(factory)
        outcome = manager.search("q")
        self.assertEqual(outcome.failure, FAILURE_SERVICE)
        self.assertEqual(manager.quota_state(), (0, 50))

    def test_keyboard_interrupt_propagates(self) -> None:
        """
        ⚠ `BaseException` 刻意不吞——用户按 Ctrl-C / Esc 要能中断一次卡住的搜索。

        （抄自 web_fetch 那次的教训：原本断言「连它都不该穿出去」，
        而 `except Exception` 不捕 `BaseException`，**那是对的**。）
        """
        factory = _Factory(raises=KeyboardInterrupt())
        with self.assertRaises(KeyboardInterrupt):
            _manager(factory).search("q")


class ZeroResultTests(unittest.TestCase):
    def test_empty_results_is_success_and_counts(self) -> None:
        """
        ⚠ **三态的下半在这里被真的用上。**

        `parse` 返回 `[]`（服务商确实没搜到）→ **成功、ok=True、计配额**；
        与上面 `test_unparsable_payload_releases_quota` 的 `None` 恰好相反。
        合并两者会让一次解析故障伪装成「这个词搜不到」。
        """
        factory = _Factory(payload={"web": {"results": []}})
        manager = _manager(factory)
        outcome = manager.search("zzz")
        self.assertTrue(outcome.ok)
        self.assertEqual(outcome.failure, "")
        self.assertEqual(outcome.results, ())
        self.assertEqual(manager.quota_state(), (1, 50))


class OutcomeFieldTests(unittest.TestCase):
    def test_query_preserved_on_every_path(self) -> None:
        query = "  带空白的查询  "
        for factory, api_key, quota in (
            (_Factory(), "", 50),                                   # no_key
            (_Factory(), "k", 0),                                   # 正常
            (_Factory(raises=RuntimeError("x")), "k", 50),          # service
        ):
            with self.subTest(api_key=api_key):
                outcome = _manager(factory, api_key=api_key, quota=quota).search(query)
                self.assertEqual(outcome.query, query.strip())
                self.assertEqual(outcome.provider, "brave")


if __name__ == "__main__":
    unittest.main()
