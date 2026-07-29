"""
结果渲染单测（web_fetch 扩展 T19，spec F19/F20/F24 / AC26/AC27）。

最重要的一组是 UntrustedWrappingTests：**抽取成功与降级两条路径都要被包裹**。
「抽取不是消毒」——抽取模型读的是同一张可能被注入的页面，
它完全可能把伪装成指令的文本当作正文如实转述出来。
"""

import unittest

from rhinecode.web.models import ExtractOutcome, FetchOutcome
from rhinecode.web.render import UNTRUSTED_CLOSE, render, summary


def _ok_fetch(**kw) -> FetchOutcome:
    base = dict(
        ok=True,
        source_url="https://a.test/x",
        final_url="https://a.test/x",
        content_type="text/html; charset=utf-8",
        charset="utf-8",
        text="页面正文",
    )
    base.update(kw)
    return FetchOutcome(**base)


class UntrustedWrappingTests(unittest.TestCase):
    """两条路径都必须被不可信标记包裹。"""

    def test_successful_extraction_is_wrapped(self) -> None:
        text = render(_ok_fetch(), ExtractOutcome(ok=True, text="抽取答案"))
        self.assertIn('<untrusted-content source="https://a.test/x">', text)
        self.assertIn("抽取答案", text)
        self.assertIn(UNTRUSTED_CLOSE, text)

    def test_degraded_extraction_is_wrapped_too(self) -> None:
        """
        **本文件最重要的一条**：降级路径同样要包。

        省掉标注等于假设「过了一道模型就干净了」，那是错的——
        降级路径连那道模型都没过，直接是页面原文。
        """
        out = ExtractOutcome(ok=False, text="页面原文节选", degraded_reason="抽取超时")
        text = render(_ok_fetch(), out)
        self.assertIn('<untrusted-content source="https://a.test/x">', text)
        self.assertIn("页面原文节选", text)
        self.assertIn(UNTRUSTED_CLOSE, text)

    def test_no_extract_object_still_wrapped(self) -> None:
        text = render(_ok_fetch())
        self.assertIn("<untrusted-content", text)

    def test_metadata_is_outside_the_marker(self) -> None:
        """
        元信息在标记**外**、正文在标记**内**。

        元信息由我们的代码生成、可信；正文来自外部、不可信。混在一起的话，
        模型看到「抽取：成功」时无法确定那是系统说的还是页面里写的。
        """
        text = render(_ok_fetch(), ExtractOutcome(ok=True, text="答案"))
        meta_pos = text.index("[web_fetch]")
        open_pos = text.index("<untrusted-content")
        self.assertLess(meta_pos, open_pos, "元信息必须在标记之前")


class MetadataTests(unittest.TestCase):
    """结果里要能读出六项（spec F19 / AC26）。"""

    def test_all_fields_present(self) -> None:
        fetch = _ok_fetch(final_url="https://a.test/y", bytes_truncated=True)
        text = render(fetch, ExtractOutcome(ok=True, text="答案", chars_truncated=True))
        self.assertIn("https://a.test/x", text)          # 来源地址
        self.assertIn("https://a.test/y", text)          # 最终地址
        self.assertIn("text/html", text)                 # 内容类型
        self.assertIn("响应截断：是", text)               # 字节截断
        self.assertIn("正文截断：是", text)               # 字符截断
        self.assertIn("抽取：成功", text)                 # 是否降级

    def test_two_truncation_flags_are_independent(self) -> None:
        """
        两类截断分开展示：前者「内容真缺了一段」，后者「进上下文的那份被裁短」。
        混成一个字段会让「是否截断」含混。
        """
        text = render(
            _ok_fetch(bytes_truncated=True),
            ExtractOutcome(ok=True, text="答案", chars_truncated=False),
        )
        self.assertIn("响应截断：是", text)
        self.assertIn("正文截断：否", text)

    def test_final_url_omitted_when_same(self) -> None:
        text = render(_ok_fetch())
        self.assertEqual(text.count("https://a.test/x"), 2)  # 元信息一次 + 标记属性一次

    def test_degraded_reason_shown(self) -> None:
        out = ExtractOutcome(ok=False, text="原文", degraded_reason="抽取请求返回了空内容")
        text = render(_ok_fetch(), out)
        self.assertIn("抽取：降级", text)
        self.assertIn("抽取请求返回了空内容", text)
        self.assertIn("页面原文节选", text)


class FailurePathTests(unittest.TestCase):
    def test_fetch_failure(self) -> None:
        fetch = FetchOutcome(ok=False, source_url="https://a.test/x",
                             final_url="https://a.test/x", error="抓取超时（单跳上限 30 秒）")
        text = render(fetch)
        self.assertIn("抓取失败", text)
        self.assertIn("超时", text)
        self.assertNotIn("<untrusted-content", text, "失败时没有外部正文，不该有标记")

    def test_connect_phase_failure_says_not_a_permission_problem(self) -> None:
        """
        连接期地址限制**不是**权限拒绝，文案要点明（spec F24 第三类）。

        不点明的话，模型会去建议用户改 permissions.yaml，而那改不动任何东西。
        """
        fetch = FetchOutcome(
            ok=False,
            source_url="https://a.test/x",
            final_url="https://a.test/x",
            error="连接期地址限制：主机名 a.test 解析到了不允许访问的非公网地址 10.0.0.1",
        )
        text = render(fetch)
        self.assertIn("不是权限配置问题", text)
        self.assertIn("不要改写地址重试", text)

    def test_plain_failure_does_not_add_that_note(self) -> None:
        fetch = FetchOutcome(ok=False, source_url="https://a.test/x",
                             final_url="https://a.test/x", error="抓取失败：连接被拒绝")
        self.assertNotIn("不是权限配置问题", render(fetch))


class RedirectPathTests(unittest.TestCase):
    def test_cross_host_redirect_message(self) -> None:
        fetch = _ok_fetch(text="", redirect_to="https://b.test/y")
        text = render(fetch)
        self.assertIn("未抓取", text)
        self.assertIn("https://b.test/y", text)
        self.assertIn("再发起一次调用", text)
        self.assertIn("完整的权限判定", text)

    def test_redirect_has_no_untrusted_body(self) -> None:
        fetch = _ok_fetch(text="", redirect_to="https://b.test/y")
        self.assertNotIn("<untrusted-content", render(fetch))


class BinaryPathTests(unittest.TestCase):
    def test_reports_type_and_size(self) -> None:
        fetch = _ok_fetch(content_type="application/pdf", charset="", text="",
                          binary=True, content_length=1024 * 1024)
        text = render(fetch)
        self.assertIn("application/pdf", text)
        self.assertIn("二进制", text)
        self.assertIn("未读取正文", text)
        self.assertNotIn("<untrusted-content", text)

    def test_unknown_size(self) -> None:
        fetch = _ok_fetch(content_type="application/zip", text="", binary=True, content_length=-1)
        self.assertIn("未知大小", render(fetch))


class SummaryTests(unittest.TestCase):
    def test_normal(self) -> None:
        s = summary(_ok_fetch(), ExtractOutcome(ok=True, text="答案"))
        self.assertIn("a.test", s)
        self.assertIn("抽取成功", s)

    def test_degraded(self) -> None:
        s = summary(_ok_fetch(), ExtractOutcome(ok=False, text="原文", degraded_reason="超时"))
        self.assertIn("已降级", s)

    def test_failure(self) -> None:
        fetch = FetchOutcome(ok=False, source_url="https://a.test/x",
                             final_url="https://a.test/x", error="x")
        self.assertIn("失败", summary(fetch))

    def test_redirect(self) -> None:
        s = summary(_ok_fetch(text="", redirect_to="https://b.test/"))
        self.assertIn("未抓取", s)

    def test_binary(self) -> None:
        s = summary(_ok_fetch(text="", binary=True, content_length=2048))
        self.assertIn("二进制", s)

    def test_malformed_url_does_not_raise(self) -> None:
        # 摘要不该因为地址畸形而失败。
        fetch = FetchOutcome(ok=True, source_url="not a url", final_url="not a url", text="x")
        summary(fetch)


if __name__ == "__main__":
    unittest.main()
