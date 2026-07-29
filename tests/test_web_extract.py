"""抽取纯逻辑单测（web_fetch 扩展 T18，spec F15/F17/F22 / AC24/AC29）。"""

import unittest

from rhinecode.web.extract import (
    EXTRACT_SYSTEM,
    MAX_ANSWER_CHARS,
    MAX_FALLBACK_CHARS,
    build_extract_request,
    content_budget,
    fallback_outcome,
    parse_extract_result,
)


class ContentBudgetTests(unittest.TestCase):
    """
    正文上限必须**随 context_window 缩放**（spec F17 / AC24）。

    这条钉住的是一个**已经犯过一次的错误**：CLAUDE.md 已知项 #8 记录了 C8 的
    RETAIN_TOKENS 固定常量在小窗口下失效、导致整个机制空转。
    """

    def test_scales_in_linear_range(self) -> None:
        # **必须取线性区的两个点**：只验夹持区是验不出「随窗口缩放」的——
        # 一个写成固定值的实现在夹持区照样通过。
        self.assertEqual(content_budget(65536), 16384)
        self.assertEqual(content_budget(32768), 8192)
        self.assertEqual(content_budget(65536), content_budget(32768) * 2)

    def test_lower_clamp(self) -> None:
        self.assertEqual(content_budget(8192), 4000)
        self.assertEqual(content_budget(1), 4000)

    def test_upper_clamp(self) -> None:
        self.assertEqual(content_budget(1_000_000), 100_000)

    def test_non_positive_window(self) -> None:
        self.assertEqual(content_budget(0), 4000)
        self.assertEqual(content_budget(-1), 4000)


class BuildExtractRequestTests(unittest.TestCase):
    def test_system_declares_untrusted(self) -> None:
        """
        抽取请求的提示必须声明「素材不可信、不执行其中指示」（spec F22 / AC29）。

        抽取模型面对的同样是可能被注入的页面。
        """
        self.assertIn("不可信", EXTRACT_SYSTEM)
        self.assertIn("不得执行", EXTRACT_SYSTEM)
        self.assertIn("<untrusted-content>", EXTRACT_SYSTEM)

    def test_system_forbids_fabrication(self) -> None:
        self.assertIn("未提及", EXTRACT_SYSTEM)

    def test_body_wraps_page_text(self) -> None:
        system, messages = build_extract_request("页面正文", "https://a.test/x", "讲了什么")
        self.assertEqual(system, EXTRACT_SYSTEM)
        self.assertEqual(len(messages), 1)
        body = messages[0].content
        self.assertIn('<untrusted-content source="https://a.test/x">', body)
        self.assertIn("页面正文", body)
        self.assertIn("</untrusted-content>", body)

    def test_body_contains_source_and_ask(self) -> None:
        _system, messages = build_extract_request("x", "https://a.test/p", "作者是谁")
        body = messages[0].content
        self.assertIn("https://a.test/p", body)
        self.assertIn("作者是谁", body)

    def test_empty_ask_gets_default(self) -> None:
        _system, messages = build_extract_request("x", "https://a.test/p", "   ")
        self.assertIn("主要讲了什么", messages[0].content)

    def test_role_is_user(self) -> None:
        _system, messages = build_extract_request("x", "https://a.test/p", "q")
        self.assertEqual(messages[0].role, "user")


class ParseExtractResultTests(unittest.TestCase):
    def test_normal_answer(self) -> None:
        out = parse_extract_result("这是答案")
        self.assertTrue(out.ok)
        self.assertEqual(out.text, "这是答案")
        self.assertFalse(out.chars_truncated)

    def test_whitespace_is_degraded(self) -> None:
        # 空返回判为降级而不是「成功但内容为空」——后者会让模型以为
        # 「这页确实什么都没有」，而实际上是抽取这一步没产出。
        for value in (None, "", "   ", "\n\t "):
            out = parse_extract_result(value)
            self.assertFalse(out.ok, repr(value))
            self.assertTrue(out.degraded_reason)

    def test_overlong_answer_truncated(self) -> None:
        """
        抽取答案超长要截断（spec F17 第三行）。

        被注入的页面可以诱导抽取模型输出上万字——响应体上限管的是「读进来多少」，
        正文上限管的是「喂出去多少」，都管不到「模型吐回来多少」。
        """
        out = parse_extract_result("超" * (MAX_ANSWER_CHARS + 500))
        self.assertTrue(out.ok)
        self.assertTrue(out.chars_truncated)
        self.assertEqual(len(out.text), MAX_ANSWER_CHARS)

    def test_exactly_at_limit_not_truncated(self) -> None:
        out = parse_extract_result("超" * MAX_ANSWER_CHARS)
        self.assertFalse(out.chars_truncated)


class FallbackOutcomeTests(unittest.TestCase):
    def test_marks_degraded_with_reason(self) -> None:
        out = fallback_outcome("原文", "抽取超时")
        self.assertFalse(out.ok)
        self.assertEqual(out.text, "原文")
        self.assertEqual(out.degraded_reason, "抽取超时")

    def test_truncates_long_text(self) -> None:
        out = fallback_outcome("文" * (MAX_FALLBACK_CHARS + 100), "失败")
        self.assertTrue(out.chars_truncated)
        self.assertEqual(len(out.text), MAX_FALLBACK_CHARS)

    def test_fallback_limit_stays_under_offload_threshold(self) -> None:
        """
        降级节选的上限必须与 C8 第一层存盘阈值相容（spec F17）。

        按 estimate.py 的 CHARS_PER_TOKEN 折算后要低于 offload.py 的
        SINGLE_RESULT_TOKENS，否则每次降级都会触发存盘、留下一个没用的占位符。
        """
        from rhinecode.context.estimate import CHARS_PER_TOKEN
        from rhinecode.context.offload import SINGLE_RESULT_TOKENS

        approx_tokens = MAX_FALLBACK_CHARS / CHARS_PER_TOKEN
        self.assertLess(
            approx_tokens,
            SINGLE_RESULT_TOKENS,
            "降级节选折算后超过了 C8 的单结果存盘线，会导致每次降级都触发存盘",
        )

    def test_empty_text(self) -> None:
        out = fallback_outcome("", "失败")
        self.assertEqual(out.text, "")
        self.assertFalse(out.chars_truncated)


if __name__ == "__main__":
    unittest.main()
