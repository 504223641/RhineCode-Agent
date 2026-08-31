"""
主对话流式超时（C8）的护栏。

## 这条缺口是什么

`provider/deepseek.py` 只在 `request_timeout` 非 None 时给 SDK 传超时，而那个
字段**只有分类器在用**。于是主对话退回 SDK 默认的 `read=600`——**流一旦卡住，
界面最长静止 10 分钟**，而用户唯一的手段是按 Esc（他大概率会先以为程序死了）。

## ⚠ 代码注释给的理由被实测推翻了

`config.py` 原本写着「主对话的一次请求可能生成几分钟，给它设超时会把正常工作
腰斩」。R3 起了一个流式吐 SSE 的本机服务器做三组对照：

    A 长但连续（4 秒，每 0.2s 一块），超时设 2 秒 → 4.2s 跑完，无错
    B 中途卡住（发 2 块后停 30 秒），超时设 2 秒  → 2.2s 返回超时
    C 同 B 但不设超时                             → 干等 30.4 秒

**A 是关键那一格。** httpx 的 `read` 超时是**每次读操作**的预算，不是整次请求
的总预算——流式下它管的是**块间间隔**。那句注释把「总时长上限」和「块间间隔
上限」当成了一回事。这不是笔误，是个很自然的直觉错误。

本文件的用例围绕这个「容易搞反的语义」写：既钉住行为，也把那个语义写进判据。
"""

import unittest
from pathlib import Path
from unittest.mock import patch

import httpx

from rhinecode.config import Config, load

REPO_ROOT = Path(__file__).resolve().parents[1]


def _client_kwargs(cfg: Config) -> dict:
    """造一个 Provider，把它传给 `openai.OpenAI` 的参数捞出来。"""
    from rhinecode.provider.deepseek import DeepSeekProvider

    captured: dict = {}

    class _Fake:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    with patch("openai.OpenAI", _Fake):
        DeepSeekProvider(cfg)
    return captured


def _cfg(**kwargs) -> Config:
    base = dict(
        protocol="deepseek",
        model="deepseek-v4-flash",
        base_url="https://api.deepseek.com/v1",
        api_key="k",
    )
    base.update(kwargs)
    return Config(**base)


class MainConversationTimeoutTest(unittest.TestCase):
    def test_main_conversation_gets_an_explicit_timeout(self) -> None:
        """
        主对话（没给 `request_timeout`）现在拿到一个显式的 `httpx.Timeout`，
        **不再退回 SDK 默认的 read=600**。
        """
        timeout = _client_kwargs(_cfg())["timeout"]
        self.assertIsInstance(timeout, httpx.Timeout)
        self.assertEqual(timeout.read, 90.0)
        self.assertEqual(timeout.connect, 10.0)

    def test_connect_is_shorter_than_read(self) -> None:
        """
        ⚠ 连接阶段该短、读阶段该长，这个不等关系本身就是判据。

        写反了不会报错，只是「连不上一个不存在的地址」要等一分半，
        而「模型正常思考」两秒就被掐断——两种症状都不指向超时配置。
        """
        timeout = _client_kwargs(_cfg())["timeout"]
        self.assertLess(timeout.connect, timeout.read)

    def test_read_timeout_is_generous_enough_for_a_block_gap(self) -> None:
        """
        ⚠ **这条钉的是语义，不是数值本身。**

        `read` 管的是**块间间隔**，正常生成时它在毫秒级；所以这个值只要远大于
        「一次首字节 + 网络抖动」就够了。判据取一个宽的区间：
        - 下界 30 秒：低于它就开始有腰斩长 TTFT 的风险（R3 未量过真实 TTFT 分布，
          所以取一个保守的下界而不是精确值）；
        - 上界 300 秒：**高于它这个字段就等于没设**——它治的整件事就是
          「别让界面静止十分钟」。

        有人把它当「总时长」调到 3600 时，这条会红并解释为什么。
        """
        timeout = _client_kwargs(_cfg())["timeout"]
        self.assertGreaterEqual(timeout.read, 30.0)
        self.assertLessEqual(
            timeout.read,
            300.0,
            "stream_idle_timeout 是**块间间隔**上限，不是一次请求的总时长上限。"
            "调到几百秒以上等于关掉它，而它治的整件事就是「别让界面静止十分钟」",
        )

    def test_classifier_path_is_unchanged(self) -> None:
        """
        ⚠ **反证：分类器那条路径一个字都没变。**

        `request_timeout` 非 None 时仍然原样传给 SDK（一个裸的 float），
        **不走** `httpx.Timeout` 那一支。两者的合理取值差一个数量级，
        R3 明确要求别复用同一个字段；这条钉住那个分岔。
        """
        timeout = _client_kwargs(_cfg(request_timeout=10.0))["timeout"]
        self.assertEqual(timeout, 10.0)
        self.assertNotIsInstance(timeout, httpx.Timeout)

    def test_values_are_configurable(self) -> None:
        """两个值都能从配置调。"""
        timeout = _client_kwargs(
            _cfg(stream_idle_timeout=120.0, stream_connect_timeout=5.0)
        )["timeout"]
        self.assertEqual(timeout.read, 120.0)
        self.assertEqual(timeout.connect, 5.0)


class ParsingTest(unittest.TestCase):
    """`load()` 的解析口径：回退默认，不抛错。"""

    def _load(self, body: str) -> Config:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "c.yaml"
            path.write_text(
                "protocol: deepseek\nmodel: m\nbase_url: u\napi_key: k\n" + body,
                encoding="utf-8",
            )
            return load(str(path))

    def test_absent_uses_defaults(self) -> None:
        cfg = self._load("")
        self.assertEqual(cfg.stream_idle_timeout, 90.0)
        self.assertEqual(cfg.stream_connect_timeout, 10.0)

    def test_explicit_values_are_read(self) -> None:
        cfg = self._load("stream_idle_timeout: 180\nstream_connect_timeout: 3\n")
        self.assertEqual(cfg.stream_idle_timeout, 180.0)
        self.assertEqual(cfg.stream_connect_timeout, 3.0)

    def test_garbage_falls_back_instead_of_raising(self) -> None:
        """
        非法值**回退默认、不抛错**（与 `context_window` 同口径，与
        `web_fetch_enabled` 的「非法值抛错」不同）。

        理由：它们是调优项，写错了最坏是数值不对，不该阻断启动。
        `web_fetch_enabled` 那种口径留给**安全开关**——一个开关被写成 `maybe`
        时静默回退会让用户以为自己关掉了网络访问而实际上没关。
        """
        cfg = self._load("stream_idle_timeout: 这不是数字\nstream_connect_timeout: -5\n")
        self.assertEqual(cfg.stream_idle_timeout, 90.0)
        self.assertEqual(cfg.stream_connect_timeout, 10.0)

    def test_source_path_is_recorded(self) -> None:
        """
        顺带钉住 C7 要的那件事：`load()` 把**实际生效的**配置路径填进 Config。
        401 的错误文案靠它告诉用户去改哪个文件。
        """
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "c.yaml"
            path.write_text(
                "protocol: deepseek\nmodel: m\nbase_url: u\napi_key: k\n",
                encoding="utf-8",
            )
            self.assertEqual(load(str(path)).source_path, str(path))


class TemplateAndDocsTest(unittest.TestCase):
    """
    ⚠ **成对维护点：`config.py` 的字段 ↔ 配置模板 ↔ `docs/internals/config.md`。**

    R3 点名这是一处**新增**的成对维护点：`request_timeout` 此前靠「刻意不进模板」
    维持一致，而这两个新字段进了模板，于是三处从此必须同步。漏改不报错——
    用户翻遍模板找不到这个旋钮，或照着文档写了个模板里没有的字段。
    """

    def test_both_fields_appear_in_the_template(self) -> None:
        from rhinecode.config import _CONFIG_TEMPLATE

        for field in ("stream_idle_timeout", "stream_connect_timeout"):
            with self.subTest(field=field):
                self.assertIn(field, _CONFIG_TEMPLATE)

    def test_template_says_it_is_a_gap_not_a_total(self) -> None:
        """
        ⚠ **模板里必须写清它是「两个数据块之间的最长间隔」。**

        不写的话下一个人会照着「总时长」去调它，然后设成 3600——而那等于把这条
        缺口原样退回去。这不是假设：原代码注释就是这么理解错的，而那句注释是
        项目自己写的。
        """
        from rhinecode.config import _CONFIG_TEMPLATE

        self.assertIn("两个数据块之间", _CONFIG_TEMPLATE)
        self.assertIn("不是", _CONFIG_TEMPLATE)

    def test_both_fields_appear_in_the_internals_doc(self) -> None:
        text = (REPO_ROOT / "docs" / "internals" / "config.md").read_text(
            encoding="utf-8"
        )
        for field in ("stream_idle_timeout", "stream_connect_timeout"):
            with self.subTest(field=field):
                self.assertIn(field, text)


if __name__ == "__main__":
    unittest.main()
