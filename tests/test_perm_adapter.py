"""adapter 模块单测：工具调用 → PermissionRequest 规范化映射（c6 T7）。"""

import unittest

from rhinecode.permission.adapter import to_request
from rhinecode.permission.models import PermissionMode
from rhinecode.tools.read_file import ReadFileTool
from rhinecode.tools.write_file import WriteFileTool
from rhinecode.tools.edit_file import EditFileTool
from rhinecode.tools.run_command import RunCommandTool
from rhinecode.tools.glob_files import GlobTool
from rhinecode.tools.grep_content import GrepTool
from rhinecode.tools.path_guard import main_project_root


# c14：这些用例验的是「参数怎么被规范化」，与工作目录无关，
# 统一传主项目根即可——与 c14 之前的判定结果逐字一致。
_CWD = main_project_root()


class AdapterTests(unittest.TestCase):
    def test_run_command_maps_to_bash_command(self) -> None:
        req = to_request(RunCommandTool(), {"command": "git status"}, PermissionMode.DEFAULT, _CWD)
        self.assertEqual((req.rule_name, req.specifier, req.kind), ("Bash", "git status", "command"))
        self.assertFalse(req.is_read_only)

    def test_read_file_maps_to_read_path(self) -> None:
        req = to_request(ReadFileTool(), {"path": "src/a.py"}, PermissionMode.DEFAULT, _CWD)
        self.assertEqual((req.rule_name, req.specifier, req.kind), ("Read", "src/a.py", "read_path"))
        self.assertTrue(req.is_read_only)

    def test_glob_maps_to_glob_kind(self) -> None:
        req = to_request(GlobTool(), {"pattern": "**/*.py"}, PermissionMode.DEFAULT, _CWD)
        self.assertEqual((req.rule_name, req.specifier, req.kind), ("Read", "**/*.py", "glob"))

    def test_grep_defaults_path_to_dot(self) -> None:
        req = to_request(GrepTool(), {"pattern": "needle"}, PermissionMode.DEFAULT, _CWD)
        self.assertEqual((req.rule_name, req.specifier, req.kind), ("Read", ".", "read_path"))

    def test_write_and_edit_map_to_write_path(self) -> None:
        wreq = to_request(WriteFileTool(), {"path": "out.txt"}, PermissionMode.DEFAULT, _CWD)
        self.assertEqual((wreq.rule_name, wreq.kind), ("Write", "write_path"))
        ereq = to_request(EditFileTool(), {"path": "out.txt"}, PermissionMode.DEFAULT, _CWD)
        self.assertEqual((ereq.rule_name, ereq.kind), ("Edit", "write_path"))


if __name__ == "__main__":
    unittest.main()
