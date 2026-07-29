"""HTML → 纯文本转换与截断单测（web_fetch 扩展 T14，spec F14 / AC20）。"""

import unittest

from rhinecode.web.convert import html_to_text, truncate


class ScriptAndStyleTests(unittest.TestCase):
    def test_script_content_dropped(self) -> None:
        html = "<html><body><p>正文</p><script>var secret = 1;</script></body></html>"
        text = html_to_text(html)
        self.assertIn("正文", text)
        self.assertNotIn("secret", text)
        self.assertNotIn("var", text)

    def test_style_content_dropped(self) -> None:
        html = "<html><head><style>body{color:red}</style></head><body>正文</body></html>"
        text = html_to_text(html)
        self.assertIn("正文", text)
        self.assertNotIn("color", text)

    def test_noscript_and_svg_dropped(self) -> None:
        html = "<body>正文<noscript>请开启JS</noscript><svg><path d='M0'/></svg></body>"
        text = html_to_text(html)
        self.assertIn("正文", text)
        self.assertNotIn("请开启JS", text)
        self.assertNotIn("M0", text)

    def test_nested_skip_tag_does_not_leak(self) -> None:
        """
        用计数而不是布尔标志跟踪跳过状态。

        ⚠ 这条用 `svg` 而不是 `script`，理由是实测出来的：`HTMLParser` 对
        `script` / `style` 用 **CDATA 模式**——内层同名开始标签压根不触发
        `handle_starttag`，第一个结束标签就收尾（浏览器同理，那之后的文本
        本来就是正文）。所以拿 `script` 测嵌套，计数与布尔标志结果相同，
        什么也钉不住。

        `svg` / `template` 走普通解析、可以合法嵌套，才是计数真正起作用的地方。
        """
        html = "<body>A<svg><svg></svg>内层结束后的内容</svg>B</body>"
        text = html_to_text(html)
        self.assertIn("A", text)
        self.assertIn("B", text)
        self.assertNotIn("内层结束后的内容", text)

    def test_script_cdata_boundary_matches_browser(self) -> None:
        """
        `<script>` 的 CDATA 语义：第一个 `</script>` 即收尾，之后是正文。

        这条把上面那个实测结论钉下来，免得有人看到 `svg` 用例后
        「顺手统一成 script」而不知道两者语义不同。
        """
        text = html_to_text("<body>A<script>x<script>y</script>z</body>")
        self.assertIn("A", text)
        self.assertNotIn("x", text)
        self.assertIn("z", text)


class EntityAndStructureTests(unittest.TestCase):
    def test_entities_decoded(self) -> None:
        text = html_to_text("<p>a &amp; b &lt;c&gt; &#65;</p>")
        self.assertIn("a & b <c> A", text)

    def test_block_tags_produce_newlines(self) -> None:
        text = html_to_text("<p>第一段</p><p>第二段</p>")
        self.assertIn("第一段", text)
        self.assertIn("第二段", text)
        self.assertIn("\n", text)

    def test_br_self_closing_produces_newline(self) -> None:
        text = html_to_text("<div>上<br/>下</div>")
        self.assertEqual(text.count("\n"), 1)

    def test_inline_tags_do_not_break_words(self) -> None:
        text = html_to_text("<p>hello <b>world</b></p>")
        self.assertIn("hello world", text)

    def test_whitespace_collapsed(self) -> None:
        text = html_to_text("<p>a     b</p>")
        self.assertIn("a b", text)

    def test_blank_lines_collapsed(self) -> None:
        text = html_to_text("<div>a</div><div></div><div></div><div></div><div>b</div>")
        self.assertNotIn("\n\n\n", text)


class RobustnessTests(unittest.TestCase):
    def test_empty_input(self) -> None:
        self.assertEqual(html_to_text(""), "")

    def test_plain_text_passthrough(self) -> None:
        self.assertEqual(html_to_text("没有任何标签"), "没有任何标签")

    def test_malformed_html_does_not_raise(self) -> None:
        # 抓取的价值在于「拿到能读的内容」，不该因为一个坏标签整次失败。
        for bad in ("<div><p>未闭合", "<<<>>>", "<a href=", "<p>a</p></div></body>"):
            html_to_text(bad)  # 不抛即通过

    def test_only_tags_yields_empty(self) -> None:
        self.assertEqual(html_to_text("<div><span></span></div>"), "")


class TruncateTests(unittest.TestCase):
    def test_under_limit(self) -> None:
        self.assertEqual(truncate("abc", 10), ("abc", False))

    def test_exactly_at_limit(self) -> None:
        self.assertEqual(truncate("abc", 3), ("abc", False))

    def test_over_limit(self) -> None:
        text, cut = truncate("abcdef", 3)
        self.assertEqual(text, "abc")
        self.assertTrue(cut)

    def test_zero_or_negative_limit_means_no_truncation(self) -> None:
        self.assertEqual(truncate("abc", 0), ("abc", False))
        self.assertEqual(truncate("abc", -1), ("abc", False))

    def test_empty_text(self) -> None:
        self.assertEqual(truncate("", 5), ("", False))


if __name__ == "__main__":
    unittest.main()
