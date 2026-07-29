"""
权限规则加载单测（web_fetch 扩展 T7，spec F13 / F6a / AC19）。

三组：
1. WebFetch 域名规则的语法校验 —— allow 写坏丢弃、deny 写坏降级为整工具拒绝
2. 分层返回 —— policy_ruleset 只含用户级 + 项目级
3. **fail-safe 反证** —— 三处「整层降级」的行为不得被改成「跳过继续」
"""

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from rhinecode.permission import config
from rhinecode.permission.models import Decision, PermissionMode, PermissionRequest


def _url_request(url: str, host: str) -> PermissionRequest:
    return PermissionRequest(
        tool_name="web_fetch",
        rule_name="WebFetch",
        specifier=url,
        kind="url",
        is_read_only=False,
        mode=PermissionMode.DEFAULT,
        host=host,
    )


class _TempLayers(unittest.TestCase):
    """把三层配置路径都重定向到临时目录，互不干扰。"""

    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        root = Path(self._tmp.name)
        self.user_dir = root / "user"
        self.project_dir = root / "project" / ".rhinecode"
        self.user_dir.mkdir(parents=True)
        self.project_dir.mkdir(parents=True)
        self._patches = [
            mock.patch.object(config, "project_config_path", lambda: self.project_dir / "permissions.yaml"),
            mock.patch.object(config, "local_config_path", lambda: self.project_dir / "permissions.local.yaml"),
        ]
        for p in self._patches:
            p.start()
        self.addCleanup(self._tmp.cleanup)
        for p in self._patches:
            self.addCleanup(p.stop)

    def _write_project(self, text: str) -> None:
        (self.project_dir / "permissions.yaml").write_text(text, encoding="utf-8")

    def _write_local(self, text: str) -> None:
        (self.project_dir / "permissions.local.yaml").write_text(text, encoding="utf-8")

    def _write_user(self, text: str) -> None:
        (self.user_dir / "permissions.yaml").write_text(text, encoding="utf-8")

    def _load(self, **kw):
        return config.load_all(self.user_dir, **kw)


class DomainSyntaxAllowTests(_TempLayers):
    def test_bad_allow_is_dropped_with_warning(self) -> None:
        # 漏写 domain: 前缀的 allow → 整条丢弃 + 一条可读警告。
        self._write_project('allow:\n  - "WebFetch(github.com)"\n')
        ruleset, _policy, errors = self._load()
        self.assertEqual(ruleset.rules, [])
        self.assertEqual(len(errors), 1)
        self.assertIn("WebFetch(github.com)", errors[0])
        self.assertIn("domain:", errors[0])

    def test_warning_mentions_policy_layer_hint(self) -> None:
        # 警告里要带上「要建立白名单请写用户级或项目级」这句（spec F6a）。
        self._write_project('allow:\n  - "WebFetch(github.com)"\n')
        _rs, _policy, errors = self._load()
        self.assertIn("用户级或项目级", errors[0])

    def test_other_rules_in_same_file_still_load(self) -> None:
        """写坏一条不影响同文件里的其它规则（AC19）。"""
        self._write_project(
            'allow:\n  - "WebFetch(github.com)"\n  - "Bash(git *)"\n  - "WebFetch(domain:a.com)"\n'
        )
        ruleset, _policy, errors = self._load()
        self.assertEqual(len(errors), 1)
        self.assertEqual(len(ruleset.rules), 2)
        self.assertEqual({r.tool for r in ruleset.rules}, {"Bash", "WebFetch"})

    def test_valid_domain_rule_passes(self) -> None:
        self._write_project('allow:\n  - "WebFetch(domain:*.github.com)"\n')
        ruleset, _policy, errors = self._load()
        self.assertEqual(errors, [])
        self.assertEqual(len(ruleset.rules), 1)
        self.assertEqual(ruleset.rules[0].pattern, "domain:*.github.com")

    def test_bare_tool_rule_passes(self) -> None:
        # 不带括号的整工具规则不走 domain 校验（spec F12）。
        self._write_project('deny:\n  - "WebFetch"\n')
        ruleset, _policy, errors = self._load()
        self.assertEqual(errors, [])
        self.assertEqual(ruleset.rules[0].pattern, "")


class DomainSyntaxDenyTests(_TempLayers):
    def test_bad_deny_degrades_to_whole_tool(self) -> None:
        """
        写坏的 deny **降级为整工具拒绝**，而不是丢弃。

        两支处理不同是刻意的：丢弃 allow 偏严（少放行），丢弃 deny 偏松（少拦）。
        后者违反 N1，所以 deny 走「看不懂拦什么就拦全部」。
        """
        self._write_project('deny:\n  - "WebFetch(github.com)"\n')
        ruleset, _policy, errors = self._load()
        self.assertEqual(len(errors), 1)
        self.assertIn("降级", errors[0])
        self.assertEqual(len(ruleset.rules), 1)
        rule = ruleset.rules[0]
        self.assertEqual(rule.effect, "deny")
        self.assertEqual(rule.tool, "WebFetch")
        self.assertEqual(rule.pattern, "")

    def test_degraded_deny_actually_blocks_everything(self) -> None:
        """降级不只是形式——它必须真的拦住任意 url 请求。"""
        self._write_project('deny:\n  - "WebFetch(github.com)"\n')
        ruleset, _policy, _errors = self._load()
        for host in ("github.com", "example.com", "anything.test"):
            hit = ruleset.evaluate(_url_request(f"https://{host}/x", host))
            self.assertIsNotNone(hit, host)
            self.assertEqual(hit.decision, Decision.DENY, host)


class PolicyRulesetLayeringTests(_TempLayers):
    def test_user_and_project_enter_policy(self) -> None:
        self._write_user('allow:\n  - "WebFetch(domain:a.com)"\n')
        self._write_project('allow:\n  - "WebFetch(domain:b.com)"\n')
        ruleset, policy, _errors = self._load()
        self.assertEqual(len(ruleset.rules), 2)
        self.assertEqual(len(policy.rules), 2)

    def test_local_does_not_enter_policy(self) -> None:
        """
        **本地级不建立白名单**（spec F6a）——它是「永久放行」自动写入的授权记录。

        不做这个区分的话，用户点一次「永久放行」就把自己锁死：
        其它域名从「弹确认」变成「硬拒且永不再问」。
        """
        self._write_local('allow:\n  - "WebFetch(domain:a.com)"\n')
        ruleset, policy, _errors = self._load()
        self.assertEqual(len(ruleset.rules), 1)  # 全量里有它，照常放行 a.com
        self.assertEqual(policy.rules, [])       # 但白名单没被建立
        self.assertFalse(policy.has_allow_for("WebFetch"))

    def test_local_deny_still_effective(self) -> None:
        """拒绝规则不受来源层区分影响——任何层级的 deny 一律生效。"""
        self._write_local('deny:\n  - "WebFetch(domain:evil.com)"\n')
        ruleset, _policy, _errors = self._load()
        hit = ruleset.evaluate(_url_request("https://evil.com/x", "evil.com"))
        self.assertIsNotNone(hit)
        self.assertEqual(hit.decision, Decision.DENY)


class WebFetchDisabledTests(_TempLayers):
    def test_disabled_skips_domain_validation(self) -> None:
        """
        能力关闭时不做域名语法校验，**零警告**（spec F4「逐字一致」）。

        多出一条警告就不叫逐字一致了。
        """
        self._write_project('allow:\n  - "WebFetch(github.com)"\n')
        _rs, _policy, errors = self._load(web_fetch_enabled=False)
        self.assertEqual(errors, [])

    def test_disabled_keeps_rule_as_is(self) -> None:
        # 关闭时该条按普通规则解析（与本扩展之前的行为一致），只是永远匹配不上任何东西。
        self._write_project('allow:\n  - "WebFetch(github.com)"\n')
        ruleset, _policy, _errors = self._load(web_fetch_enabled=False)
        self.assertEqual(len(ruleset.rules), 1)
        self.assertEqual(ruleset.rules[0].pattern, "github.com")


class FailSafeDegradationTests(_TempLayers):
    """
    **三处「整层降级」的行为不得被改成「跳过继续」** —— 本文件最重要的一组。

    返回类型从「单个错误」改成「消息列表」只是为了让单条级警告能多条并存；
    若顺手把整文件/整字段级的 return 也改成 continue，fail-safe 会从偏严滑向偏松，
    而**功能上完全看不出来**。
    """

    def test_bad_yaml_degrades_whole_layer(self) -> None:
        self._write_project("allow: [unclosed\n")
        ruleset, _policy, errors = self._load()
        self.assertEqual(ruleset.rules, [])
        self.assertEqual(len(errors), 1)

    def test_non_mapping_top_level_degrades_whole_layer(self) -> None:
        self._write_project("- just\n- a\n- list\n")
        ruleset, _policy, errors = self._load()
        self.assertEqual(ruleset.rules, [])
        self.assertEqual(len(errors), 1)

    def test_non_list_deny_field_degrades_whole_layer_including_allow(self) -> None:
        """
        **反证**：`deny` 写成非列表时，同层的 `allow` **也不得生效**。

        若把这处 return 改成「记下警告后 continue」，这个文件会变成
        「deny 全丢、allow 照常生效」——从「整层降级为空（少放行，偏严）」
        滑向「只丢拒绝规则（偏松）」，直接违反 N1。

        既有测试挡不住它：那条只覆盖了坏 YAML，且断言是 `len(errors) == 1`，
        改成累加后仍然是 1。
        """
        self._write_project('allow:\n  - "Bash(git *)"\ndeny: not-a-list\n')
        ruleset, _policy, errors = self._load()
        self.assertEqual(ruleset.rules, [], "deny 字段非法时，同层的 allow 也必须一并失效")
        self.assertEqual(len(errors), 1)

    def test_missing_file_is_not_an_error(self) -> None:
        ruleset, policy, errors = self._load()
        self.assertEqual(ruleset.rules, [])
        self.assertEqual(policy.rules, [])
        self.assertEqual(errors, [])


class ParseRuleStringCallerCompatTests(unittest.TestCase):
    """
    `parse_rule_string` 的另外两个调用方不传新参数也必须能跑。

    返回类型保持 Optional[Rule] 是硬要求——改成元组的话，
    `engine.persist_local_rule` 与 `skills.validation.grants_for` 里
    `if rule is not None: ...append(rule)` 会把**元组本身**塞进规则列表，
    下一次求值访问 `.effect` 时 AttributeError。
    """

    def test_returns_rule_or_none_not_tuple(self) -> None:
        rule = config.parse_rule_string("Bash(git *)", "allow", "local")
        self.assertIsNotNone(rule)
        self.assertEqual(rule.effect, "allow")   # 元组没有 .effect
        self.assertIsNone(config.parse_rule_string("", "allow", "local"))

    def test_no_warnings_list_does_not_crash(self) -> None:
        # 不传 warnings 时，发现问题也只是静默丢弃/降级，不抛异常。
        self.assertIsNone(config.parse_rule_string("WebFetch(github.com)", "allow", "user"))
        degraded = config.parse_rule_string("WebFetch(github.com)", "deny", "user")
        self.assertIsNotNone(degraded)
        self.assertEqual(degraded.pattern, "")


if __name__ == "__main__":
    unittest.main()
