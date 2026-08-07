"""config 模块单测：规则解析、三层加载、容错、永久放行回写（c6 T5，对应 AC10）。"""

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from rhinecode.permission import config
from rhinecode.permission.models import Decision, PermissionMode, PermissionRequest
from rhinecode.tools.path_guard import main_project_root

# c14：这些用例验的是权限判定本身，与工作目录无关。统一传主项目根，
# 判定结果与 c14 之前逐字一致。
_CWD = main_project_root()


class ParseRuleStringTests(unittest.TestCase):
    def test_with_pattern(self) -> None:
        rule = config.parse_rule_string("Bash(git *)", "allow", "user")
        self.assertEqual((rule.effect, rule.tool, rule.pattern, rule.source), ("allow", "Bash", "git *", "user"))

    def test_without_pattern(self) -> None:
        rule = config.parse_rule_string("Bash", "deny", "project")
        self.assertEqual((rule.tool, rule.pattern), ("Bash", ""))

    def test_empty_returns_none(self) -> None:
        self.assertIsNone(config.parse_rule_string("  ", "allow", "user"))


class TempWorkspaceHomeTest(unittest.TestCase):
    """把 cwd 与 home 都指向临时目录，隔离真实配置，保证测试确定性。"""

    def setUp(self) -> None:
        self._old_cwd = os.getcwd()
        self._ws = tempfile.TemporaryDirectory()
        self._home = tempfile.TemporaryDirectory()
        os.chdir(self._ws.name)
        self._home_patch = mock.patch("rhinecode.permission.config.Path.home", return_value=Path(self._home.name))
        self._home_patch.start()

    def tearDown(self) -> None:
        self._home_patch.stop()
        os.chdir(self._old_cwd)
        self._ws.cleanup()
        self._home.cleanup()

    def _write(self, path: Path, text: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")


class ConfigLoadTests(TempWorkspaceHomeTest):
    def test_missing_files_yield_empty(self) -> None:
        ruleset, policy, errors = config.load_all()
        self.assertEqual(ruleset.rules, [])
        self.assertEqual(policy.rules, [])
        self.assertEqual(errors, [])

    def test_project_rules_loaded(self) -> None:
        self._write(config.project_config_path(), "allow:\n  - \"Bash(git *)\"\ndeny:\n  - \"Bash(git push *)\"\n")
        ruleset, policy, errors = config.load_all()
        self.assertEqual(errors, [])
        self.assertEqual(len(ruleset.rules), 2)
        # 项目级属「策略层」，两条都该进 policy（web_fetch 扩展 spec F6a）
        self.assertEqual(len(policy.rules), 2)

    def test_bad_yaml_degrades_without_crash(self) -> None:
        # 坏 YAML → 该层降级为空 + 收集可读错误，不崩溃、不放权（AC10）
        self._write(config.project_config_path(), "allow: [unclosed\n")
        ruleset, _policy, errors = config.load_all()
        self.assertEqual(ruleset.rules, [])
        self.assertEqual(len(errors), 1)

    def test_append_local_allow_persists(self) -> None:
        config.append_local_allow("Bash(git status)")
        self.assertTrue(config.local_config_path().exists())
        ruleset, _policy, _errors = config.load_all()
        req = PermissionRequest("run_command", "Bash", "git status", "command", False, PermissionMode.DEFAULT, _CWD)
        result = ruleset.evaluate(req)
        self.assertIsNotNone(result)
        self.assertEqual(result.decision, Decision.ALLOW)

    def test_append_local_allow_does_not_overwrite_broken_yaml(self) -> None:
        broken = "allow: [unclosed\n"
        self._write(config.local_config_path(), broken)

        with self.assertRaisesRegex(ValueError, "未写入"):
            config.append_local_allow("Bash(git status)")

        self.assertEqual(config.local_config_path().read_text(encoding="utf-8"), broken)


if __name__ == "__main__":
    unittest.main()
