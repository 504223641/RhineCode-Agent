"""
域名模式匹配单测（web_fetch 扩展 T1，spec F11 / AC17）。

覆盖 spec F11 的四行语义表，外加归一化两条。**其中「反证」是重点**：
`example.*` 不得匹配 `example.evil.com` —— 那是安全要求而非风格选择，
若通配跨点，一条本意放行「example 各国域名」的规则会连带放行攻击者自行注册的域名。
"""

import unittest

from rhinecode.permission.matching import match_domain


class MatchDomainExactTests(unittest.TestCase):
    """无通配的精确匹配：只匹配自己，不匹配任何子域。"""

    def test_exact_matches_itself(self) -> None:
        self.assertTrue(match_domain("example.com", "example.com"))

    def test_exact_does_not_match_subdomain(self) -> None:
        # spec F11 第一行：`example.com` 不匹配 `api.example.com`。
        # 想放行子域必须显式写 `*.example.com`。
        self.assertFalse(match_domain("example.com", "api.example.com"))

    def test_exact_does_not_match_suffix_trick(self) -> None:
        # 防「后缀粘连」：notexample.com 与 example.com.evil.com 都不该命中。
        self.assertFalse(match_domain("example.com", "notexample.com"))
        self.assertFalse(match_domain("example.com", "example.com.evil.com"))


class MatchDomainWildcardSubdomainTests(unittest.TestCase):
    """前导 `*.`：匹配任意深度子域，但不匹配裸域本身。"""

    def test_matches_any_depth(self) -> None:
        self.assertTrue(match_domain("*.example.com", "api.example.com"))
        self.assertTrue(match_domain("*.example.com", "a.b.example.com"))
        self.assertTrue(match_domain("*.example.com", "a.b.c.d.example.com"))

    def test_does_not_match_bare_domain(self) -> None:
        # spec F11 第二行明确：`*.example.com` **不**匹配裸域 `example.com`。
        # 两者都要放行时得写两条规则。
        self.assertFalse(match_domain("*.example.com", "example.com"))

    def test_does_not_match_unrelated_suffix(self) -> None:
        self.assertFalse(match_domain("*.example.com", "api.notexample.com"))
        self.assertFalse(match_domain("*.example.com", "evil.com"))


class MatchDomainMatchAllTests(unittest.TestCase):
    """单独一个 `*` 与空模式：匹配一切主机名。"""

    def test_star_matches_everything(self) -> None:
        self.assertTrue(match_domain("*", "example.com"))
        self.assertTrue(match_domain("*", "a.b.c.example.org"))
        self.assertTrue(match_domain("*", "8.8.8.8"))

    def test_empty_pattern_matches_everything(self) -> None:
        # 与 match_command / match_path 同口径：空模式 = 匹配该工具全部调用，
        # 对应配置里写 `WebFetch` 而不是 `WebFetch(domain:...)`。
        self.assertTrue(match_domain("", "example.com"))


class MatchDomainNoCrossDotTests(unittest.TestCase):
    """
    非前导位置的 `*` 只匹配两点之间的一段 —— 本文件最重要的一组。

    这不是风格选择，是安全要求：跨点匹配会让 `example.*` 连带放行
    攻击者可以自行注册的 `example.evil.com`。
    """

    def test_trailing_star_matches_single_label(self) -> None:
        # spec F11 第四行正例：`*` 取到 `org`。
        self.assertTrue(match_domain("example.*", "example.org"))
        self.assertTrue(match_domain("example.*", "example.cn"))

    def test_trailing_star_does_not_cross_dot(self) -> None:
        # **反证**：`*` 若能跨点就会命中，这条断言就是钉住它不能跨。
        self.assertFalse(match_domain("example.*", "example.evil.com"))
        self.assertFalse(match_domain("example.*", "example.a.b"))

    def test_leading_star_without_dot_does_not_cross_dot(self) -> None:
        # `*example.com`（没有那个点）同样受「不跨点」约束，
        # 与前导 `*.` 是两种不同的写法，别混。
        self.assertTrue(match_domain("*example.com", "myexample.com"))
        self.assertFalse(match_domain("*example.com", "a.myexample.com"))

    def test_interior_star_does_not_cross_dot(self) -> None:
        self.assertTrue(match_domain("a.*.com", "a.b.com"))
        self.assertFalse(match_domain("a.*.com", "a.b.c.com"))


class MatchDomainNormalizationTests(unittest.TestCase):
    """归一化：大小写不敏感 + 末尾点等价。"""

    def test_case_insensitive(self) -> None:
        self.assertTrue(match_domain("Example.COM", "example.com"))
        self.assertTrue(match_domain("example.com", "EXAMPLE.CoM"))
        self.assertTrue(match_domain("*.Example.com", "API.example.COM"))

    def test_trailing_dot_equivalent(self) -> None:
        # `example.com.` 是完全限定域名的书写形式，与 `example.com` 指同一个域。
        # 不归一化的话，多写/少写一个点就能绕过一条 allow 规则。
        self.assertTrue(match_domain("example.com.", "example.com"))
        self.assertTrue(match_domain("example.com", "example.com."))
        self.assertTrue(match_domain("example.com.", "example.com."))

    def test_surrounding_whitespace_ignored(self) -> None:
        self.assertTrue(match_domain("  example.com  ", "example.com"))


if __name__ == "__main__":
    unittest.main()
