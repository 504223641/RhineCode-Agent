"""权限 other 分支 fnmatch 单测（c7 T19，决策 A）：向后兼容 + MCP 通配放行（AC9）。"""

import unittest

from rhinecode.permission.engine import PermissionEngine
from rhinecode.permission.models import Decision, PermissionMode, PermissionRequest, Rule
from rhinecode.permission.rules import RuleSet


def _mcp_req(name: str, mode=PermissionMode.DEFAULT) -> PermissionRequest:
    """构造一个 MCP 工具的权限请求（未映射 → kind=other、非只读、无 specifier）。"""
    return PermissionRequest(
        tool_name=name, rule_name=name, specifier="", kind="other", is_read_only=False, mode=mode
    )


def _engine(rules=None, mode=PermissionMode.DEFAULT) -> PermissionEngine:
    return PermissionEngine(RuleSet(rules or []), mode=mode)


class OtherBranchTests(unittest.TestCase):
    def test_no_rule_default_asks(self) -> None:
        # AC9 反面：未配规则的 MCP 工具（非只读）默认模式 → ASK。
        res = _engine().decide(_mcp_req("mcp__everything__echo"))
        self.assertEqual(res.decision, Decision.ASK)

    def test_wildcard_allows_whole_server(self) -> None:
        # AC9：allow: mcp__everything__* 命中该 Server 全部工具 → ALLOW。
        eng = _engine([Rule("allow", "mcp__everything__*", "", "user")])
        self.assertEqual(eng.decide(_mcp_req("mcp__everything__echo")).decision, Decision.ALLOW)
        self.assertEqual(eng.decide(_mcp_req("mcp__everything__add")).decision, Decision.ALLOW)

    def test_wildcard_does_not_cross_server(self) -> None:
        # 通配限定在指定 Server：别的 Server 不受影响，仍 ASK。
        eng = _engine([Rule("allow", "mcp__everything__*", "", "user")])
        self.assertEqual(eng.decide(_mcp_req("mcp__other__echo")).decision, Decision.ASK)

    def test_exact_name_still_matches(self) -> None:
        # 向后兼容：无 * 的 other 规则精确匹配同名工具。
        eng = _engine([Rule("allow", "mcp__everything__echo", "", "user")])
        self.assertEqual(eng.decide(_mcp_req("mcp__everything__echo")).decision, Decision.ALLOW)

    def test_exact_name_no_false_positive(self) -> None:
        # 向后兼容：精确规则不误伤名字相近的其它工具。
        eng = _engine([Rule("allow", "mcp__everything__echo", "", "user")])
        self.assertEqual(eng.decide(_mcp_req("mcp__everything__echo2")).decision, Decision.ASK)

    def test_deny_wildcard_beats_allow(self) -> None:
        # deny 优先：通配 deny 命中即拒，即便有 allow。
        eng = _engine([
            Rule("allow", "mcp__everything__*", "", "user"),
            Rule("deny", "mcp__everything__*", "", "user"),
        ])
        self.assertEqual(eng.decide(_mcp_req("mcp__everything__echo")).decision, Decision.DENY)


if __name__ == "__main__":
    unittest.main()
