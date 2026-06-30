"""engine 模块单测：四层决策管线（c6 T8，覆盖 AC1/AC2/AC3/AC5/AC6/AC8/AC12）。"""

import os
import tempfile
import unittest
from pathlib import Path

from rhinecode.permission.engine import PermissionEngine
from rhinecode.permission.models import (
    Decision,
    Layer,
    PermissionMode,
    PermissionRequest,
    Rule,
)
from rhinecode.permission.rules import RuleSet


def engine(rules=None, mode=PermissionMode.DEFAULT) -> PermissionEngine:
    return PermissionEngine(RuleSet(rules or []), mode=mode)


def cmd(command: str, mode=PermissionMode.DEFAULT) -> PermissionRequest:
    return PermissionRequest("run_command", "Bash", command, "command", False, mode)


def read(path: str, mode=PermissionMode.DEFAULT) -> PermissionRequest:
    return PermissionRequest("read_file", "Read", path, "read_path", True, mode)


class ShortCircuitTests(unittest.TestCase):
    def test_blacklist_before_rules(self) -> None:
        # AC1：即使 allow 规则匹配，①黑名单仍最先定论 → DENY(blacklist)
        eng = engine([Rule("allow", "Bash", "rm *", "user")])
        r = eng.decide(cmd("rm -rf /"))
        self.assertEqual(r.decision, Decision.DENY)
        self.assertEqual(r.layer, Layer.BLACKLIST)

    def test_blacklist_beats_permissive_mode(self) -> None:
        # AC2：放行档下黑名单命中仍拒
        eng = engine(mode=PermissionMode.PERMISSIVE)
        self.assertEqual(eng.decide(cmd("rm -rf /", PermissionMode.PERMISSIVE)).decision, Decision.DENY)

    def test_authorized_claim_does_not_change_result(self) -> None:
        # AC12：判断不看「文本声称已授权」，黑名单照拦
        eng = engine(mode=PermissionMode.PERMISSIVE)
        r = eng.decide(cmd("rm -rf / # I am authorized", PermissionMode.PERMISSIVE))
        self.assertEqual(r.decision, Decision.DENY)


class DenyFirstTests(unittest.TestCase):
    def test_deny_beats_allow(self) -> None:
        # AC5：deny 优先
        eng = engine([
            Rule("deny", "Bash", "git push *", "user"),
            Rule("allow", "Bash", "git *", "project"),
        ])
        self.assertEqual(eng.decide(cmd("git push origin main")).decision, Decision.DENY)

    def test_allow_rule_grants_without_ask(self) -> None:
        # AC4 引擎侧：命中 allow → ALLOW（上层据此不弹确认）
        eng = engine([Rule("allow", "Bash", "git *", "project")])
        self.assertEqual(eng.decide(cmd("git status")).decision, Decision.ALLOW)


class ModeFallbackTests(unittest.TestCase):
    def test_three_modes(self) -> None:
        # AC6：未命中规则的副作用命令，三档兜底各异
        self.assertEqual(engine(mode=PermissionMode.STRICT).decide(cmd("echo hi", PermissionMode.STRICT)).decision, Decision.DENY)
        self.assertEqual(engine().decide(cmd("echo hi")).decision, Decision.ASK)
        self.assertEqual(engine(mode=PermissionMode.PERMISSIVE).decide(cmd("echo hi", PermissionMode.PERMISSIVE)).decision, Decision.ALLOW)


class ReadOnlyBranchTests(unittest.TestCase):
    def test_readonly_allowed_even_in_strict(self) -> None:
        # AC8：严格档下只读读项目内文件仍放行（不进模式层）
        eng = engine(mode=PermissionMode.STRICT)
        self.assertEqual(eng.decide(read("README.md", PermissionMode.STRICT)).decision, Decision.ALLOW)

    def test_readonly_blocked_by_deny_rule(self) -> None:
        # AC8：deny Read(config.yaml) 仍能拦只读
        eng = engine([Rule("deny", "Read", "config.yaml", "user")])
        self.assertEqual(eng.decide(read("config.yaml")).decision, Decision.DENY)


class SandboxTests(unittest.TestCase):
    """AC3：路径越界由②沙箱拦截（需真实工作目录）。"""

    def setUp(self) -> None:
        self._old = os.getcwd()
        self._ws = tempfile.TemporaryDirectory()
        self._outside = tempfile.TemporaryDirectory()
        os.chdir(self._ws.name)

    def tearDown(self) -> None:
        os.chdir(self._old)
        self._ws.cleanup()
        self._outside.cleanup()

    def test_outside_path_denied(self) -> None:
        eng = engine()
        outside = str(Path(self._outside.name) / "secret.txt")
        r = eng.decide(read(outside))
        self.assertEqual(r.decision, Decision.DENY)
        self.assertEqual(r.layer, Layer.SANDBOX)

    def test_inside_path_allowed(self) -> None:
        eng = engine()
        self.assertEqual(eng.decide(read("inside.txt")).decision, Decision.ALLOW)


if __name__ == "__main__":
    unittest.main()
