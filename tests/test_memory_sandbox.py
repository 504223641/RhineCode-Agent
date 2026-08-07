"""沙箱只读白名单单测（c9 T12 / AC17）：读放行、写仍拒、未注册路径仍拒。"""

import unittest
import tempfile
from pathlib import Path

from rhinecode.tools.path_guard import (
    PathGuardError,
    register_read_root,
    clear_read_roots,
    resolve_readable,
    is_readable_path,
    is_within_workspace,
)
from rhinecode.tools.read_file import ReadFileTool
from rhinecode.permission.engine import PermissionEngine
from rhinecode.permission.models import Decision, PermissionMode, PermissionRequest
from rhinecode.permission.rules import RuleSet
from rhinecode.tools.path_guard import main_project_root


def _cwd():
    """c14：这些用例会 chdir 到临时工作区，因此每次现取进程当前目录。"""
    return main_project_root()


def _read_request(path: str) -> PermissionRequest:
    return PermissionRequest(
        tool_name="read_file", rule_name="Read", specifier=path,
        kind="read_path", is_read_only=True, mode=PermissionMode.DEFAULT, cwd=_cwd(),
    )


def _write_request(path: str) -> PermissionRequest:
    return PermissionRequest(
        tool_name="write_file", rule_name="Write", specifier=path,
        kind="write_path", is_read_only=False, mode=PermissionMode.DEFAULT, cwd=_cwd(),
    )


class SandboxWhitelistTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        # 模拟工作区外的用户级 memory 目录
        self.memory_dir = Path(self._tmp.name) / "home" / ".rhinecode" / "memory"
        self.memory_dir.mkdir(parents=True)
        self.note = self.memory_dir / "some-note.md"
        self.note.write_text("---\nname: x\nsummary: y\ncategory: project\n---\n正文\n", encoding="utf-8")
        # 工作区外的「未注册」敏感文件（同一临时根，但不在白名单目录内）
        self.outside = Path(self._tmp.name) / "secret.txt"
        self.outside.write_text("secret", encoding="utf-8")

        clear_read_roots()
        register_read_root(self.memory_dir)

    def tearDown(self) -> None:
        clear_read_roots()
        self._tmp.cleanup()

    def test_registered_dir_readable(self) -> None:
        """白名单目录内的绝对路径可解析、可判定为可读。"""
        resolved = resolve_readable(str(self.note), _cwd())
        self.assertEqual(resolved, self.note.resolve())
        self.assertTrue(is_readable_path(str(self.note), _cwd()))

    def test_unregistered_outside_still_denied(self) -> None:
        """未注册的工作区外路径：读判定仍拒绝（白名单不放大其它面）。"""
        self.assertFalse(is_readable_path(str(self.outside), _cwd()))
        with self.assertRaises(PathGuardError):
            resolve_readable(str(self.outside), _cwd())

    def test_parent_ref_still_denied(self) -> None:
        """含 `..` 的路径即便最终落在白名单内也拒绝（防绕过审计）。"""
        sneaky = str(self.memory_dir / "sub" / ".." / "some-note.md")
        self.assertFalse(is_readable_path(sneaky, _cwd()))

    def test_read_file_tool_reads_whitelisted(self) -> None:
        """read_file 工具能读白名单目录里的笔记全文（AC17）。"""
        result = ReadFileTool().execute({"path": str(self.note)}, cwd=_cwd())
        self.assertTrue(result.ok, result.output)
        self.assertIn("正文", result.output)

    def test_read_file_tool_outside_denied(self) -> None:
        result = ReadFileTool().execute({"path": str(self.outside)}, cwd=_cwd())
        self.assertFalse(result.ok)

    def test_engine_read_allows_whitelist_write_denies(self) -> None:
        """权限引擎②沙箱层：读白名单路径放行；写同一路径仍越界拒绝。"""
        engine = PermissionEngine(RuleSet([]))
        read_decision = engine.decide(_read_request(str(self.note)))
        self.assertEqual(read_decision.decision, Decision.ALLOW)
        write_decision = engine.decide(_write_request(str(self.note)))
        self.assertEqual(write_decision.decision, Decision.DENY)
        self.assertIn("越界", write_decision.reason)

    def test_workspace_semantics_unchanged(self) -> None:
        """原有工作区判定不受白名单影响（写类照旧只认工作区）。"""
        self.assertFalse(is_within_workspace(str(self.note), _cwd()))
        self.assertTrue(is_within_workspace("rhinecode/config.py", _cwd()))

    def test_clear_read_roots_restores(self) -> None:
        clear_read_roots()
        self.assertFalse(is_readable_path(str(self.note), _cwd()))


if __name__ == "__main__":
    unittest.main()
