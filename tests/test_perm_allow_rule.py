"""
`to_allow_rule` 与工具映射单测（web_fetch 扩展 T6，spec F9 / AC15）。

这个函数存在的理由是一个**当场看不出来**的缺陷：确认面板选「永久放行」时，
原先的写法会对 url 类写出 `WebFetch(https://example.com/a?token=abc)`——
本次调用照常放行了，但下次启动那条规则会被丢弃，用户点过的「永久」凭空失效，
而且查询参数里的令牌被原样写进了配置文件。

本文件的重点是两条：
1. url 类**只取主机名**，路径、查询参数、端口一个都不带进去；
2. 其余 kind **逐字等于本函数存在之前的行为**（回归护栏）。
"""

import unittest

from rhinecode.permission.adapter import to_allow_rule, to_request
from rhinecode.permission.models import PermissionMode
from rhinecode.tools.base import Tool


class _FakeTool(Tool):
    """只提供 name / read_only 两项元信息的替身，execute 不会被调用。"""

    def __init__(self, name: str, read_only: bool = False) -> None:
        self.name = name
        self.read_only = read_only

    def execute(self, args: dict):  # pragma: no cover - 本测试不执行工具
        raise AssertionError("测试中不应执行工具")


def _req(tool_name: str, args: dict, read_only: bool = False):
    return to_request(_FakeTool(tool_name, read_only), args, PermissionMode.DEFAULT)


class ToRequestUrlTests(unittest.TestCase):
    def test_url_kind_and_specifier(self) -> None:
        req = _req("web_fetch", {"url": "https://example.com/a?token=abc"})
        self.assertEqual(req.rule_name, "WebFetch")
        self.assertEqual(req.kind, "url")
        # specifier 是完整 URL 原文——面板与行为记录要留下模型实际请求的那个地址。
        self.assertEqual(req.specifier, "https://example.com/a?token=abc")

    def test_host_filled_and_normalized(self) -> None:
        req = _req("web_fetch", {"url": "https://Example.COM.:8443/a"})
        self.assertEqual(req.host, "example.com")

    def test_unparseable_url_leaves_host_empty(self) -> None:
        # 解析失败不在这里报错——②′层的 check_hard 会把这次请求拒掉。
        req = _req("web_fetch", {"url": "not a url"})
        self.assertEqual(req.host, "")

    def test_missing_url_arg(self) -> None:
        req = _req("web_fetch", {})
        self.assertEqual(req.specifier, "")
        self.assertEqual(req.host, "")


class ToAllowRuleUrlTests(unittest.TestCase):
    def test_takes_host_only(self) -> None:
        req = _req("web_fetch", {"url": "https://example.com/a"})
        self.assertEqual(to_allow_rule(req), ("WebFetch", "domain:example.com"))

    def test_drops_path_query_and_port(self) -> None:
        """
        **本文件最重要的一条**：路径、查询参数、端口都不得进入规则。

        用一个同时带三者的地址构造，逐项断言它们没被带进去——
        其中查询参数那项直接钉住「令牌被写进配置文件」这个泄漏。
        """
        req = _req("web_fetch", {"url": "https://example.com:8443/a/b?token=abc#frag"})
        rule_name, pattern = to_allow_rule(req)
        self.assertEqual(rule_name, "WebFetch")
        self.assertEqual(pattern, "domain:example.com")
        self.assertNotIn("token", pattern)
        self.assertNotIn("8443", pattern)
        self.assertNotIn("/a", pattern)
        self.assertNotIn("frag", pattern)

    def test_host_normalized_in_rule(self) -> None:
        req = _req("web_fetch", {"url": "https://Example.COM./x"})
        self.assertEqual(to_allow_rule(req), ("WebFetch", "domain:example.com"))

    def test_empty_host_falls_back_to_bare_tool(self) -> None:
        req = _req("web_fetch", {"url": "not a url"})
        self.assertEqual(to_allow_rule(req), ("WebFetch", ""))


class ToAllowRuleLegacyKindsTests(unittest.TestCase):
    """
    回归护栏：其余 kind 的翻译必须**逐字等于**抽出本函数之前的行为，
    即 `(rule_name, specifier)`。

    `to_allow_rule` 是新抽出来的单一来源，不能顺手改坏既有行为。
    """

    def _assert_legacy(self, tool_name: str, args: dict, expected: tuple[str, str]) -> None:
        req = _req(tool_name, args)
        self.assertEqual(to_allow_rule(req), expected)
        # 同时断言它与「旧写法」一致：旧写法就是直接取 (rule_name, specifier)。
        self.assertEqual(to_allow_rule(req), (req.rule_name, req.specifier))

    def test_command(self) -> None:
        self._assert_legacy("run_command", {"command": "git status"}, ("Bash", "git status"))

    def test_read_path(self) -> None:
        self._assert_legacy("read_file", {"path": "src/a.py"}, ("Read", "src/a.py"))

    def test_write_path(self) -> None:
        self._assert_legacy("write_file", {"path": "out.txt"}, ("Write", "out.txt"))

    def test_edit_path(self) -> None:
        self._assert_legacy("edit_file", {"path": "src/a.py"}, ("Edit", "src/a.py"))

    def test_glob(self) -> None:
        self._assert_legacy("glob_files", {"pattern": "**/*.py"}, ("Read", "**/*.py"))

    def test_other_kind(self) -> None:
        # 未映射工具落到 other 分支：specifier 为空，等价于整工具规则。
        self._assert_legacy("mcp__srv__tool", {}, ("mcp__srv__tool", ""))


if __name__ == "__main__":
    unittest.main()
