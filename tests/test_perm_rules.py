"""rules 模块单测：deny 优先求值（c6 T4，对应 AC5）。"""

import unittest

from rhinecode.permission.models import Decision, PermissionMode, PermissionRequest, Rule
from rhinecode.permission.rules import RuleSet


def cmd_request(command: str) -> PermissionRequest:
    return PermissionRequest(
        tool_name="run_command",
        rule_name="Bash",
        specifier=command,
        kind="command",
        is_read_only=False,
        mode=PermissionMode.DEFAULT,
    )


class RuleSetTests(unittest.TestCase):
    def test_deny_beats_allow_across_layers(self) -> None:
        # 用户级 deny + 项目级 allow，deny 优先（AC5）：层级远近不影响，deny 赢
        rs = RuleSet([
            Rule("deny", "Bash", "git push *", "user"),
            Rule("allow", "Bash", "git *", "project"),
        ])
        result = rs.evaluate(cmd_request("git push origin main"))
        self.assertIsNotNone(result)
        self.assertEqual(result.decision, Decision.DENY)

    def test_allow_when_only_allow_matches(self) -> None:
        rs = RuleSet([Rule("allow", "Bash", "git *", "project")])
        result = rs.evaluate(cmd_request("git status"))
        self.assertIsNotNone(result)
        self.assertEqual(result.decision, Decision.ALLOW)

    def test_none_when_no_match(self) -> None:
        rs = RuleSet([Rule("allow", "Bash", "npm *", "user")])
        self.assertIsNone(rs.evaluate(cmd_request("git status")))

    def test_path_rule_matches_read(self) -> None:
        rs = RuleSet([Rule("deny", "Read", "config.yaml", "user")])
        req = PermissionRequest("read_file", "Read", "config.yaml", "read_path", True, PermissionMode.DEFAULT)
        result = rs.evaluate(req)
        self.assertIsNotNone(result)
        self.assertEqual(result.decision, Decision.DENY)


if __name__ == "__main__":
    unittest.main()
