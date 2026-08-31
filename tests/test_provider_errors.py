"""
Provider 错误分类（C7）的护栏。

## 为什么这一组值得写

C7 的缺口不是「代码写错了」，是**九类不同的失败收敛成同一句话**。这类缺口的
特点是**测试很容易写成同义反复**：断言「文案里有『API Key』」的用例，在一个
把所有分支都返回同一句「API Key 无效」的实现上照样全绿。

所以本文件的判据是**区分度**——九类两两不同、且每一类都说出了只有它才该说的
那个东西（配置文件路径 / 模型名 / 主机名）。

⚠ 不需要真实凭据，也不发任何请求：直接构造 SDK 异常。
"""

import logging
import unittest

import httpx
import openai

from rhinecode.provider import errors

_MODEL = "deepseek-v4-flash"
_BASE_URL = "https://api.deepseek.com/v1"
_CONFIG = "/home/u/.rhinecode/config.yaml"


def _resp(status: int, message: str = "服务端说的那句话") -> httpx.Response:
    """造一个带 body 的 httpx 响应，用于构造 SDK 异常。"""
    request = httpx.Request("POST", "https://api.deepseek.com/v1/chat/completions")
    return httpx.Response(status, request=request, json={"error": {"message": message}})


def _request() -> httpx.Request:
    return httpx.Request("POST", "https://api.deepseek.com/v1/chat/completions")


# 九类的代表性样本。⚠ **新增一类要加进这张表**——下面每条用例都遍历它，
# 漏登记的表现是「那一类没有任何护栏」，而那不报错。
def _samples() -> dict[str, Exception]:
    return {
        "401 认证": openai.AuthenticationError(
            "e", response=_resp(401), body={"message": "Authentication Fails"}
        ),
        "403 无权限": openai.PermissionDeniedError(
            "e", response=_resp(403), body={"message": "Insufficient Balance"}
        ),
        "404 模型不存在": openai.NotFoundError(
            "e", response=_resp(404), body={"message": "Model Not Exist"}
        ),
        "429 限流": openai.RateLimitError(
            "e", response=_resp(429), body={"message": "Rate limit reached"}
        ),
        "400 参数错": openai.BadRequestError(
            "e", response=_resp(400), body={"message": "Invalid parameter"}
        ),
        "500 服务端故障": openai.InternalServerError(
            "e", response=_resp(500), body={"message": "Internal error"}
        ),
        "超时": openai.APITimeoutError(request=_request()),
        "连不上": openai.APIConnectionError(request=_request()),
        "未预期": ValueError("某个解析异常"),
    }


def _classify(exc: Exception, config_path: str = _CONFIG) -> str:
    return errors.classify(
        exc, model=_MODEL, base_url=_BASE_URL, config_path=config_path
    )


class DistinctnessTest(unittest.TestCase):
    """⚠ **本组是这份文件的正主：九类必须彼此说得不一样。**"""

    def test_every_class_gets_its_own_wording(self) -> None:
        """
        九类的**说明部分**两两不同。

        这条钉的正是 C7 那个缺口本身：此前九类全都是 `str(e)`，而界面上紧跟着
        的「因流错误已停止」更是逐字相同。只断言「某一类的文案对」是不够的——
        一个把所有分支返回同一句话的实现能通过那种断言。

        ⚠ 比的是**第一行**（说明），不是整段：服务端原文附在第二行，
        而两个不同类别的服务端原文完全可能相同（R3 实测：401 与「模型名写错」
        返回的 body 逐字相同，因为 401 先于模型校验发生）。
        """
        heads = {name: _classify(e).split("\n")[0] for name, e in _samples().items()}
        self.assertEqual(
            len(set(heads.values())),
            len(heads),
            f"有两类说了同一句话：{heads}",
        )

    def test_every_class_says_what_to_do_next(self) -> None:
        """
        每一类都要给出「下一步做什么」，哪怕只是「可以直接重试」。

        这是 R3 那张表贯穿全表的第一条原则：此前九类全都收敛成「因流错误已停止」，
        **那句话不含任何行动信息**。

        判据取「说明里出现了一个动词性的落点」——用关键词表实现，
        比人肉看每条文案可靠，也比「长度大于 N」这种伪判据有意义。
        """
        actionable = ("检查", "重试", "稍后", "报 issue", "调大")
        for name, exc in _samples().items():
            with self.subTest(kind=name):
                head = _classify(exc).split("\n")[0]
                self.assertTrue(
                    any(k in head for k in actionable),
                    f"「{name}」的文案没告诉用户下一步做什么：{head}",
                )

    def test_server_message_is_never_dropped(self) -> None:
        """
        ⚠ **说明是前缀，不是替换。**

        服务端偶尔会说出你没预料到的原因（配额、地区限制、内容审核），
        盖掉它会让排查变成猜谜。这条断言原文一定还在。
        """
        for name, exc in _samples().items():
            with self.subTest(kind=name):
                text = _classify(exc)
                detail = errors._server_message(exc)
                self.assertIn(detail, text, f"「{name}」把服务端原文丢了")


class SpecificContentTest(unittest.TestCase):
    """每一类说出了只有它才该说的那个东西。"""

    def test_auth_error_names_the_config_file(self) -> None:
        """
        ⚠ 401 **必须打印实际生效的配置文件路径**。

        `--config` 与用户级 `~/.rhinecode/config.yaml` 是两条来源，而「key 填错了」
        时用户最常见的下一步动作就是去改**另一个**文件——不报路径的话，他会一边
        改一个没在用的文件、一边以为程序坏了。
        """
        text = _classify(_samples()["401 认证"])
        self.assertIn(_CONFIG, text)

    def test_auth_error_degrades_cleanly_without_a_path(self) -> None:
        """
        反证：拿不到路径时（未经 `load()` 构造的 Config）**不能打印一对空括号**。

        那种「（）」比不打印更糟——它看起来像程序出了 bug。
        """
        text = _classify(_samples()["401 认证"], config_path="")
        self.assertNotIn("（）", text)
        self.assertIn("api_key", text)

    def test_model_related_errors_name_the_model(self) -> None:
        """403 与 404 都要把 `model` 的值带上——那正是要改的那个字段。"""
        for name in ("403 无权限", "404 模型不存在"):
            with self.subTest(kind=name):
                self.assertIn(_MODEL, _classify(_samples()[name]))

    def test_connection_error_names_the_host(self) -> None:
        """
        连接类必须报出主机名。

        此前它只有一句 `Connection error.`，而「把主机名打出来」是最省事也最
        有效的一步——用户一眼就能看出 base_url 是不是写错了。
        """
        self.assertIn("api.deepseek.com", _classify(_samples()["连不上"]))

    def test_timeout_is_matched_before_connection_error(self) -> None:
        """
        ⚠ **`APITimeoutError` 继承自 `APIConnectionError`，分支顺序不能反。**

        反过来的话一次超时会被报成「连不上，请检查网络」，而网络明明是通的
        ——用户会去查代理、查 DNS，而问题在别处。这条用例就是那个顺序的护栏。
        """
        self.assertTrue(issubclass(openai.APITimeoutError, openai.APIConnectionError))
        text = _classify(_samples()["超时"])
        self.assertIn("超时", text)
        self.assertNotIn("连不上", text)

    def test_bad_request_does_not_guess_at_context_length(self) -> None:
        """
        ⚠ **反证：400 刻意不做「上下文超长」的二次分流。**

        R3 的分类表给了一条「命中长度类关键词 → 提示 /compact」的分支，但同时
        注明它**没有实测过**——触发它要真发一次超长请求（需有效凭据且花钱），
        而 DeepSeek 返回的 message 长什么样没人见过。凭 OpenAI 的文案去猜的
        后果是：一条「试试 /compact」出现在与长度毫无关系的参数错误上，
        用户照做之后问题还在，而他已经不信这条提示了。

        这条用例钉住那个「刻意不做」。有人拿真实超长请求量过之后再加分支，
        届时把这条用例一起改掉。
        """
        text = _classify(_samples()["400 参数错"])
        self.assertNotIn("/compact", text)
        self.assertIn("issue", text)


class LogLevelTest(unittest.TestCase):
    """分级依据是「作者要不要看到它」，不是「有多严重」。"""

    def test_levels(self) -> None:
        cases = {
            "401 认证": logging.INFO,
            "404 模型不存在": logging.INFO,
            "429 限流": logging.INFO,
            "超时": logging.INFO,
            "连不上": logging.INFO,
            # 400 是唯一需要作者看到服务端原文的一类；5xx 说明对端出了问题
            "400 参数错": logging.WARNING,
            "500 服务端故障": logging.WARNING,
            # 未预期 = 解析异常或 SDK 内部错，作者必须看到完整堆栈
            "未预期": logging.ERROR,
        }
        samples = _samples()
        for name, want in cases.items():
            with self.subTest(kind=name):
                self.assertEqual(errors.log_level_for(samples[name]), want)

    def test_only_unexpected_errors_carry_a_stack(self) -> None:
        """
        `is_sdk_error` 决定要不要记堆栈。已分类的九类里堆栈只是噪音，
        未预期的那一类才是作者唯一能拿到的线索。
        """
        samples = _samples()
        self.assertFalse(errors.is_sdk_error(samples["未预期"]))
        for name in ("401 认证", "429 限流", "超时", "连不上", "500 服务端故障"):
            with self.subTest(kind=name):
                self.assertTrue(errors.is_sdk_error(samples[name]))


class ProviderWiringTest(unittest.TestCase):
    """接线：`stream_chat` 真的用上了分类表，而不是又回到 `str(e)`。"""

    def test_stream_chat_yields_the_classified_message(self) -> None:
        from unittest.mock import MagicMock, patch

        from rhinecode.config import Config
        from rhinecode.provider.deepseek import DeepSeekProvider

        cfg = Config(
            protocol="deepseek",
            model=_MODEL,
            base_url=_BASE_URL,
            api_key="k",
            source_path=_CONFIG,
        )
        with patch("openai.OpenAI", return_value=MagicMock()):
            provider = DeepSeekProvider(cfg)

        provider._client.chat.completions.create = MagicMock(
            side_effect=_samples()["401 认证"]
        )

        chunks = list(provider.stream_chat([]))
        errs = [c for c in chunks if c.type == "error"]
        self.assertEqual(len(errs), 1)
        # 分类文案与配置文件路径都要在——只断言前者的话，
        # 一个把 config_path 忘了传的接线照样能过
        self.assertIn("API Key", errs[0].content)
        self.assertIn(_CONFIG, errs[0].content)


if __name__ == "__main__":
    unittest.main()
