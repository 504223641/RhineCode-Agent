"""
字符集推断链单测（web_fetch 扩展 T13，spec F14a / AC21）。

重点是那条 GBK 用例：「查中文文档」是本扩展的主场景之一，
按 UTF-8 解一个 GBK 页面会整页乱码**而不报任何错**——模型拿到一堆问号，
用户以为是抓取失败，排查方向完全错。
"""

import unittest

from rhinecode.web.decode import (
    FALLBACK_CHARSET,
    charset_from_document,
    charset_from_header,
    decode,
)


class HeaderCharsetTests(unittest.TestCase):
    def test_plain(self) -> None:
        self.assertEqual(charset_from_header("text/html; charset=utf-8"), "utf-8")

    def test_quoted_and_spaced(self) -> None:
        self.assertEqual(charset_from_header('text/html; charset = "GBK"'), "gbk")

    def test_case_insensitive(self) -> None:
        self.assertEqual(charset_from_header("TEXT/HTML; CHARSET=UTF-8"), "utf-8")

    def test_absent(self) -> None:
        self.assertIsNone(charset_from_header("text/html"))
        self.assertIsNone(charset_from_header(""))

    def test_unknown_charset_rejected(self) -> None:
        # Python 不认识的名字视为「没声明」，交给下一步推断——
        # 而不是拿它去解码然后炸掉。
        self.assertIsNone(charset_from_header("text/html; charset=not-a-real-charset"))


class DocumentCharsetTests(unittest.TestCase):
    def test_html5_meta(self) -> None:
        raw = b'<html><head><meta charset="gbk"></head><body>x</body></html>'
        self.assertEqual(charset_from_document(raw), "gbk")

    def test_html4_http_equiv(self) -> None:
        raw = (
            b'<html><head><meta http-equiv="Content-Type" '
            b'content="text/html; charset=gbk"></head></html>'
        )
        self.assertEqual(charset_from_document(raw), "gbk")

    def test_absent(self) -> None:
        self.assertIsNone(charset_from_document(b"<html><body>x</body></html>"))

    def test_only_scans_head_window(self) -> None:
        # 声明必须出现在文档开头；正文深处偶然出现的相似片段不该被当成声明。
        raw = b"<html><body>" + b"x" * 5000 + b'<meta charset="gbk">' + b"</body></html>"
        self.assertIsNone(charset_from_document(raw))


class DecodeTests(unittest.TestCase):
    def test_utf8_roundtrip(self) -> None:
        text, charset = decode("中文内容".encode("utf-8"), "text/html; charset=utf-8")
        self.assertEqual(text, "中文内容")
        self.assertEqual(charset, "utf-8")

    def test_gbk_page_decoded_correctly(self) -> None:
        """
        **本文件最重要的一条**：声明 GBK 的中文页面必须被正确解码。

        按 UTF-8 解会得到一串 `�`，且不抛任何异常——这正是「乱码不报错」的形态。
        """
        body = "中文文档标题".encode("gbk")
        text, charset = decode(body, "text/html; charset=gbk")
        self.assertEqual(text, "中文文档标题")
        self.assertEqual(charset, "gbk")
        self.assertNotIn("�", text)

    def test_gbk_declared_in_document_only(self) -> None:
        # 响应头没声明时，从文档内 <meta> 拿。
        raw = '<html><head><meta charset="gbk"></head><body>中文</body></html>'.encode("gbk")
        text, charset = decode(raw, "text/html")
        self.assertEqual(charset, "gbk")
        self.assertIn("中文", text)

    def test_header_wins_over_document(self) -> None:
        # 两处声明冲突时以响应头为准（它是传输层的权威声明）。
        raw = '<html><head><meta charset="gbk"></head><body>ok</body></html>'.encode("utf-8")
        _text, charset = decode(raw, "text/html; charset=utf-8")
        self.assertEqual(charset, "utf-8")

    def test_unknown_charset_falls_back(self) -> None:
        text, charset = decode("hello".encode("utf-8"), "text/html; charset=bogus-9999")
        self.assertEqual(charset, FALLBACK_CHARSET)
        self.assertEqual(text, "hello")

    def test_no_declaration_falls_back_to_utf8(self) -> None:
        text, charset = decode("hello".encode("utf-8"), "")
        self.assertEqual(charset, FALLBACK_CHARSET)
        self.assertEqual(text, "hello")

    def test_bad_bytes_replaced_not_raised(self) -> None:
        # 个别坏字节替换成 �，不抛异常——拿到「大部分能读的文本」远好过什么都没有。
        text, _charset = decode(b"ok\xff\xfebad", "text/html; charset=utf-8")
        self.assertIn("ok", text)
        self.assertIn("�", text)

    def test_empty_body(self) -> None:
        text, charset = decode(b"", "text/html")
        self.assertEqual(text, "")
        self.assertEqual(charset, FALLBACK_CHARSET)


if __name__ == "__main__":
    unittest.main()
