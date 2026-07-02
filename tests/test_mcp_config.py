"""MCP 配置层单测（c7 T14）：两层合并、${VAR} 展开、类型判定、容错（AC1–AC4）。"""

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from rhinecode.mcp import config


class TempWorkspaceHome(unittest.TestCase):
    """把 cwd 与 home 指向临时目录，隔离真实配置，保证测试确定性。"""

    def setUp(self) -> None:
        self._old_cwd = os.getcwd()
        self._ws = tempfile.TemporaryDirectory()
        self._home = tempfile.TemporaryDirectory()
        os.chdir(self._ws.name)
        self._home_patch = mock.patch(
            "rhinecode.mcp.config.Path.home", return_value=Path(self._home.name)
        )
        self._home_patch.start()

    def tearDown(self) -> None:
        self._home_patch.stop()
        os.chdir(self._old_cwd)
        self._ws.cleanup()
        self._home.cleanup()

    def _write(self, path: Path, text: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")


class LoadTests(TempWorkspaceHome):
    def test_missing_files_yield_empty(self) -> None:
        # AC4：两层文件都缺失 → 空配置、无错误，正常启动。
        configs, errors = config.load_all()
        self.assertEqual(configs, [])
        self.assertEqual(errors, [])

    def test_stdio_and_http_kind_detection(self) -> None:
        # AC2：含 command → stdio；含 url → http。
        self._write(
            config.project_config_path(),
            "mcpServers:\n"
            "  local:\n"
            "    command: npx\n"
            "    args: ['-y', 'pkg']\n"
            "  remote:\n"
            "    url: https://example.com/mcp\n",
        )
        configs, errors = config.load_all()
        self.assertEqual(errors, [])
        by_name = {c.name: c for c in configs}
        self.assertEqual(by_name["local"].kind, "stdio")
        self.assertEqual(by_name["local"].command, "npx")
        self.assertEqual(by_name["local"].args, ["-y", "pkg"])
        self.assertEqual(by_name["remote"].kind, "http")
        self.assertEqual(by_name["remote"].url, "https://example.com/mcp")

    def test_project_overrides_user_same_name(self) -> None:
        # AC1：同名 Server 项目级覆盖用户级。
        self._write(
            config.user_config_path(),
            "mcpServers:\n  s:\n    command: user-cmd\n",
        )
        self._write(
            config.project_config_path(),
            "mcpServers:\n  s:\n    command: project-cmd\n",
        )
        configs, _ = config.load_all()
        self.assertEqual(len(configs), 1)
        self.assertEqual(configs[0].command, "project-cmd")

    def test_layers_merge_distinct_names(self) -> None:
        # AC1：不同名 Server 合并共存。
        self._write(config.user_config_path(), "mcpServers:\n  a:\n    command: ca\n")
        self._write(config.project_config_path(), "mcpServers:\n  b:\n    url: http://x\n")
        configs, _ = config.load_all()
        self.assertEqual({c.name for c in configs}, {"a", "b"})

    def test_env_expansion_set_and_unset(self) -> None:
        # AC3：${VAR} 设置时展开为值，未设置时展开为空串。
        self._write(
            config.project_config_path(),
            "mcpServers:\n"
            "  s:\n"
            "    command: c\n"
            "    env:\n"
            "      SET: ${MCP_TEST_SET}\n"
            "      UNSET: ${MCP_TEST_UNSET_XYZ}\n",
        )
        with mock.patch.dict(os.environ, {"MCP_TEST_SET": "hello"}, clear=False):
            os.environ.pop("MCP_TEST_UNSET_XYZ", None)
            configs, _ = config.load_all()
        env = configs[0].env
        self.assertEqual(env["SET"], "hello")
        self.assertEqual(env["UNSET"], "")

    def test_header_expansion(self) -> None:
        # AC3：headers 值同样支持 ${VAR}。
        self._write(
            config.project_config_path(),
            "mcpServers:\n"
            "  s:\n"
            "    url: http://x\n"
            "    headers:\n"
            "      Authorization: Bearer ${MCP_TEST_TOKEN}\n",
        )
        with mock.patch.dict(os.environ, {"MCP_TEST_TOKEN": "abc"}, clear=False):
            configs, _ = config.load_all()
        self.assertEqual(configs[0].headers["Authorization"], "Bearer abc")

    def test_invalid_entry_skipped_with_error(self) -> None:
        # AC4：既无 command 也无 url 的条目被跳过并收集错误，其余正常。
        self._write(
            config.project_config_path(),
            "mcpServers:\n"
            "  good:\n"
            "    command: c\n"
            "  bad:\n"
            "    foo: bar\n",
        )
        configs, errors = config.load_all()
        self.assertEqual([c.name for c in configs], ["good"])
        self.assertTrue(any("bad" in e for e in errors))

    def test_broken_yaml_layer_degrades(self) -> None:
        # AC4：YAML 解析失败的层降级为空并收集错误，不崩溃。
        self._write(config.project_config_path(), "mcpServers: : : not yaml\n")
        configs, errors = config.load_all()
        self.assertEqual(configs, [])
        self.assertTrue(len(errors) >= 1)


if __name__ == "__main__":
    unittest.main()
