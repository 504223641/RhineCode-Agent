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
from rhinecode.tools.path_guard import main_project_root


# c14：这些用例验的是「参数怎么被规范化」，与工作目录无关，
# 统一传主项目根即可——与 c14 之前的判定结果逐字一致。
_CWD = main_project_root()


class _FakeTool(Tool):
    """只提供 name / read_only 两项元信息的替身，execute 不会被调用。"""

    def __init__(self, name: str, read_only: bool = False) -> None:
        self.name = name
        self.read_only = read_only

    def execute(self, args: dict):  # pragma: no cover - 本测试不执行工具
        raise AssertionError("测试中不应执行工具")


def _req(tool_name: str, args: dict, read_only: bool = False):
    return to_request(_FakeTool(tool_name, read_only), args, PermissionMode.DEFAULT, _CWD)


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


class SkillGrantWebFetchTests(unittest.TestCase):
    """
    Skill 的 `allowed-tools` 能为 web_fetch 预授权（T9，spec F26 / AC34）。

    `_TOOL_ALIASES` 是 CLAUDE.md 成对维护点明文要求的一处：新增工具若要在
    `allowed-tools` 里可写，就得在那张表里加一行。
    """

    def _grants(self, declarations: list[str]):
        from pathlib import Path

        from rhinecode.skills.models import SkillSource, SkillSpec
        from rhinecode.skills.validation import grants_for

        spec = SkillSpec(
            command_name="demo",
            display_name="demo",
            description="d",
            when_to_use=None,
            body="正文",
            granted_tools=tuple(declarations),
            forked=False,
            model_invocable=True,
            user_invocable=True,
            model=None,
            source=SkillSource.PROJECT,
            entry_path=Path("/x/demo.md"),
            resource_dir=None,
            resource_files=(),
        )
        return grants_for([spec])

    def test_webfetch_recognized(self) -> None:
        rules, warnings = self._grants(["WebFetch(domain:github.com)"])
        self.assertEqual(warnings, [])
        self.assertEqual(len(rules), 1)
        self.assertEqual(rules[0].tool, "WebFetch")
        self.assertEqual(rules[0].pattern, "domain:github.com")

    def test_internal_tool_name_recognized(self) -> None:
        rules, warnings = self._grants(["web_fetch"])
        self.assertEqual(warnings, [])
        self.assertEqual(rules[0].tool, "WebFetch")

    def test_bad_domain_syntax_produces_warning(self) -> None:
        """
        漏写 `domain:` 前缀时必须有警告。

        不接 `parse_rule_string` 的 warnings 出参的话，这条会顺利通过
        「认不认得工具类别」那道警告（现在认得 WebFetch 了），然后被静默丢弃——
        用户看到的现象是「我明明写了预授权，还是每次弹确认」，而 /skills 报告
        里什么都没有。
        """
        rules, warnings = self._grants(["WebFetch(github.com)"])
        self.assertEqual(rules, [])
        self.assertEqual(len(warnings), 1)
        self.assertIn("domain:", warnings[0])

    def test_grant_source_is_turn_level(self) -> None:
        """
        预授权规则的来源层不是 user/project —— 因此**不建立域名白名单**。

        这条钉住「`allowed-tools` 只放宽、从不收紧」这条既有安全承诺：
        若它能建立白名单，一个声明了 WebFetch(domain:x) 的 Skill 会在执行期间
        反向收紧其它域名的访问。
        """
        from rhinecode.permission.config import POLICY_SOURCES

        rules, _warnings = self._grants(["WebFetch(domain:github.com)"])
        self.assertNotIn(rules[0].source, POLICY_SOURCES)

    def test_warning_text_lists_webfetch(self) -> None:
        _rules, warnings = self._grants(["NoSuchTool"])
        self.assertEqual(len(warnings), 1)
        self.assertIn("WebFetch", warnings[0])


if __name__ == "__main__":
    unittest.main()
