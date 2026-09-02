"""
向导两次网络请求的护栏（first-run-setup 扩展 T7，spec F8/F9/F10/F14 / AC12–AC14、AC19）。

**全部用注入的假客户端，一次都不联网。**

本文件里最要紧的两条：

- `SecretRedactionTest`：异常消息里带密钥时，交给界面的措辞**不许含它**
  （spec F14）。SDK 异常的字符串可能带上请求 URL、响应体甚至请求头。
- `NeverRaisesTest`：两个函数都不许抛。它们服务的是一个界面流程，
  「连不上」在这里是**要展示给用户的结果**，不是内部错误。
"""

import unittest
from types import SimpleNamespace

import openai
import httpx

from rhinecode.setup import catalog, probe
from rhinecode.setup.models import ProbeFailure


def _make_response(status: int) -> httpx.Response:
    """造一个带状态码的 httpx.Response，供构造 SDK 的 APIStatusError 用。"""
    return httpx.Response(status_code=status, request=httpx.Request("GET", "https://x/"))


class _FakeModels:
    def __init__(self, ids, error=None):
        self._ids = ids
        self._error = error

    def list(self):
        if self._error:
            raise self._error
        return SimpleNamespace(data=[SimpleNamespace(id=i) for i in self._ids])


class _FakeCompletions:
    def __init__(self, error=None):
        self._error = error
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if self._error:
            raise self._error
        return SimpleNamespace(choices=[])


class _FakeClient:
    def __init__(self, ids=(), models_error=None, chat_error=None):
        self.models = _FakeModels(list(ids), models_error)
        self.chat = SimpleNamespace(completions=_FakeCompletions(chat_error))


def _factory(client):
    """把一个假客户端包成 `_client_factory` 期望的签名。"""
    return lambda api_key, base_url, timeout: client


class ListModelsTest(unittest.TestCase):
    """第三屏拉清单（AC12/AC13）。"""

    def test_uses_server_response(self):
        """AC12：选项来自服务端，**不是**兜底清单。"""
        client = _FakeClient(ids=["a-model", "b-model"])
        result = probe.list_models("k", "u", _client_factory=_factory(client))
        self.assertFalse(result.from_fallback)
        self.assertIsNone(result.error)
        self.assertEqual([o.model_id for o in result.options], ["a-model", "b-model"])

    def test_does_not_filter_unknown_models(self):
        """
        服务端结果**一律不过滤**。

        过滤等于又一次把「我们认为有哪些模型」写死，而那正是本扩展要治的病。
        """
        client = _FakeClient(ids=["brand-new-model"])
        result = probe.list_models("k", "u", _client_factory=_factory(client))
        self.assertEqual([o.model_id for o in result.options], ["brand-new-model"])

    def test_failure_falls_back_with_reason(self):
        """AC13：拉不到 → 退兜底 + 可读原因，而不是抛出去。"""
        client = _FakeClient(models_error=openai.APIConnectionError(request=httpx.Request("GET", "https://x/")))
        result = probe.list_models("k", "u", _client_factory=_factory(client))
        self.assertTrue(result.from_fallback)
        self.assertEqual(result.options, catalog.FALLBACK_OPTIONS)
        self.assertTrue(result.error)

    def test_empty_server_list_also_falls_back(self):
        """
        连上了但一个模型都没有：形态上成功、实质上没法用，同样退兜底。

        不处理的话第三屏会显示一个空列表，用户既选不了也不知道为什么。
        """
        client = _FakeClient(ids=[])
        result = probe.list_models("k", "u", _client_factory=_factory(client))
        self.assertTrue(result.from_fallback)
        self.assertEqual(result.options, catalog.FALLBACK_OPTIONS)


class VerifyTest(unittest.TestCase):
    """第四屏终验。"""

    def test_success(self):
        client = _FakeClient()
        result = probe.verify("k", "u", "m", _client_factory=_factory(client))
        self.assertTrue(result.ok)
        self.assertIsNone(result.kind)
        self.assertGreaterEqual(result.elapsed_ms, 0)

    def test_sends_minimal_non_streaming_request(self):
        """
        `max_tokens=1` + 非流式。

        花的是用户的钱，而这里只需要一次完整往返；流式在这里没有任何用处，
        还会让失败分类多绕一层。
        """
        client = _FakeClient()
        probe.verify("k", "u", "deepseek-v4-flash", _client_factory=_factory(client))
        kwargs = client.chat.completions.calls[0]
        self.assertEqual(kwargs["max_tokens"], 1)
        self.assertFalse(kwargs["stream"])
        self.assertEqual(kwargs["model"], "deepseek-v4-flash")


class ClassifyTest(unittest.TestCase):
    """
    四类失败的分流。

    ⚠ **分四类是因为用户接下来该做的事完全不同。** 合并成一句「请求失败」的
    代价很具体：填错 key 的人会去检查网络，网络不通的人会去重填 key，
    两边都在错的方向上耗时间。
    """

    def _verify_with(self, error):
        client = _FakeClient(chat_error=error)
        return probe.verify("k", "u", "m", _client_factory=_factory(client))

    def test_auth_error(self):
        result = self._verify_with(
            openai.AuthenticationError("bad key", response=_make_response(401), body=None)
        )
        self.assertEqual(result.kind, ProbeFailure.AUTH)
        self.assertIn("密钥", result.detail)

    def test_permission_denied_is_also_auth(self):
        result = self._verify_with(
            openai.PermissionDeniedError("no", response=_make_response(403), body=None)
        )
        self.assertEqual(result.kind, ProbeFailure.AUTH)

    def test_connection_error(self):
        result = self._verify_with(
            openai.APIConnectionError(request=httpx.Request("GET", "https://x/"))
        )
        self.assertEqual(result.kind, ProbeFailure.NETWORK)

    def test_timeout_is_network(self):
        result = self._verify_with(
            openai.APITimeoutError(request=httpx.Request("GET", "https://x/"))
        )
        self.assertEqual(result.kind, ProbeFailure.NETWORK)

    def test_not_found_is_model(self):
        result = self._verify_with(
            openai.NotFoundError("nope", response=_make_response(404), body=None)
        )
        self.assertEqual(result.kind, ProbeFailure.MODEL)

    def test_bad_request_mentioning_model_is_model(self):
        """
        400 是个筐，而模型名不对最常落在这里——DeepSeek 对未知模型返回的
        就是 400 而不是 404。靠服务端说法里有没有提到 model 分流。

        猜错的代价只是措辞不够贴切；不分流的代价是「模型填错了」永远被显示成
        「未知错误」，而那正是用户最需要知道该改哪儿的一次。
        """
        result = self._verify_with(
            openai.BadRequestError(
                "Model Not Exist", response=_make_response(400), body=None
            )
        )
        self.assertEqual(result.kind, ProbeFailure.MODEL)

    def test_bad_request_without_model_is_other(self):
        result = self._verify_with(
            openai.BadRequestError(
                "something else broke", response=_make_response(400), body=None
            )
        )
        self.assertEqual(result.kind, ProbeFailure.OTHER)

    def test_rate_limit_is_other(self):
        result = self._verify_with(
            openai.RateLimitError("slow down", response=_make_response(429), body=None)
        )
        self.assertEqual(result.kind, ProbeFailure.OTHER)
        self.assertIn("限流", result.detail)

    def test_insufficient_balance_is_called_out(self):
        """余额不足（402）值得一句专门的话——它既不是 key 的问题也不是网络的问题。"""
        result = self._verify_with(
            openai.APIStatusError("no balance", response=_make_response(402), body=None)
        )
        self.assertIn("余额", result.detail)

    def test_server_error(self):
        result = self._verify_with(
            openai.InternalServerError("boom", response=_make_response(503), body=None)
        )
        self.assertEqual(result.kind, ProbeFailure.OTHER)


class SecretRedactionTest(unittest.TestCase):
    """
    **spec F14**：交给界面的措辞里不许出现密钥。

    SDK 异常的字符串**可能带上请求 URL、响应体、甚至请求头**，
    而请求头里就是 `Authorization: Bearer <key>`。
    """

    def test_literal_key_is_scrubbed(self):
        secret = "sk-secret-key-abcdef123456"
        client = _FakeClient(
            chat_error=openai.BadRequestError(
                f"failed with header Authorization: Bearer {secret}",
                response=_make_response(400),
                body=None,
            )
        )
        result = probe.verify(secret, "u", "m", _client_factory=_factory(client))
        self.assertNotIn(secret, result.detail, "密钥原样出现在给界面的措辞里了")

    def test_other_key_shaped_tokens_are_scrubbed_too(self):
        """
        **两道打码缺一不可。**

        按字面量替换治的是「异常里带出了我们发出去的那个 key」；
        正则治的是「异常里带出了**别的** key 形态的东西」——比如服务端把
        请求体回显了一部分，里面有另一个密钥。只做前者的话，
        一个我们没预料到的来源就漏出去了。
        """
        client = _FakeClient(
            chat_error=openai.BadRequestError(
                "echo: sk-someone-elses-key-999", response=_make_response(400), body=None
            )
        )
        result = probe.verify("my-own-key", "u", "m", _client_factory=_factory(client))
        self.assertNotIn("sk-someone-elses-key-999", result.detail)

    def test_list_models_error_is_scrubbed(self):
        """拉清单那一侧同样要打码——它和终验用的是同一条翻译路径。"""
        secret = "sk-list-secret-0001"
        client = _FakeClient(
            models_error=openai.AuthenticationError(
                f"bad key {secret}", response=_make_response(401), body=None
            )
        )
        result = probe.list_models(secret, "u", _client_factory=_factory(client))
        self.assertNotIn(secret, result.error or "")

    def test_long_error_is_truncated(self):
        """
        一个 HTML 错误页有几十 KB，原样塞进界面既没用又难看。
        """
        client = _FakeClient(
            chat_error=openai.BadRequestError(
                "x" * 5000, response=_make_response(400), body=None
            )
        )
        result = probe.verify("k", "u", "m", _client_factory=_factory(client))
        self.assertLess(len(result.detail), 400)

    def test_newlines_are_flattened(self):
        """多行错误压成一行——界面上那是一个单行标签。"""
        client = _FakeClient(
            chat_error=openai.BadRequestError(
                "line one\nline two\r\nline three", response=_make_response(400), body=None
            )
        )
        result = probe.verify("k", "u", "m", _client_factory=_factory(client))
        self.assertNotIn("\n", result.detail)


class NeverRaisesTest(unittest.TestCase):
    """
    **两个函数都不许抛。**

    它们服务的是一个界面流程，「连不上」在这里是要展示给用户的结果，
    不是内部错误。让它们抛的话，界面代码就得把这套翻译再写一遍——
    而那正是「同一件事两处措辞」的形态。
    """

    def test_list_models_swallows_arbitrary_exception(self):
        client = _FakeClient(models_error=RuntimeError("完全没预料到的东西"))
        result = probe.list_models("k", "u", _client_factory=_factory(client))
        self.assertTrue(result.from_fallback)
        self.assertTrue(result.error)

    def test_verify_swallows_arbitrary_exception(self):
        client = _FakeClient(chat_error=RuntimeError("完全没预料到的东西"))
        result = probe.verify("k", "u", "m", _client_factory=_factory(client))
        self.assertFalse(result.ok)
        self.assertEqual(result.kind, ProbeFailure.OTHER)

    def test_factory_itself_blowing_up_is_also_caught(self):
        """
        连**构造客户端**都失败（地址格式非法之类）也不能抛——
        那一步同样在 try 里面，别把它挪出去。
        """

        def _boom(api_key, base_url, timeout):
            raise ValueError("地址格式不对")

        result = probe.verify("k", "not a url", "m", _client_factory=_boom)
        self.assertFalse(result.ok)


if __name__ == "__main__":
    unittest.main()
