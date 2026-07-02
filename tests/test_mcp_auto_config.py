"""C7 自动添加 MCP 配置能力的回归测试。"""

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import yaml

from rhinecode.mcp import config as mcp_config
from rhinecode.mcp.auto_config import (
    resolve_mcp_query,
    server_config_from_entry,
    write_server_config,
)
from rhinecode.mcp.transport import resolve_stdio_command


class TempWorkspaceHome(unittest.TestCase):
    """为写配置测试提供隔离的项目目录和用户 home。

    被测代码会同时写项目级 `.rhinecode/mcp.yaml` 和用户级 `~/.rhinecode/mcp.yaml`；
    每个测试单独切换 cwd 并 mock `Path.home()`，避免污染开发者真实配置。
    """

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


class ResolveTests(unittest.TestCase):
    """覆盖 URL、自然语言名称、歧义和网络失败四类解析路径。"""

    def test_url_resolves_to_http_config(self) -> None:
        result = resolve_mcp_query("https://example.com/mcp")

        self.assertEqual(result.status, "resolved")
        self.assertEqual(result.candidates[0].config, {"url": "https://example.com/mcp"})
        self.assertEqual(result.candidates[0].server_name, "example")

    def test_context7_resolves_from_mock_npm_search(self) -> None:
        def fetch(url: str, timeout: float):
            # mock NPM search payload：context7 相关且明确带 MCP 的包应排到第一位。
            self.assertIn("-/v1/search", url)
            return {
                "objects": [
                    {
                        "package": {
                            "name": "@upstash/context7-mcp",
                            "description": "Context7 MCP server for documentation",
                        },
                        "score": {"final": 0.9},
                    },
                    {
                        "package": {
                            "name": "context7-helper",
                            "description": "Helper package",
                        },
                        "score": {"final": 0.8},
                    },
                ]
            }

        result = resolve_mcp_query("context7", fetch_json=fetch)

        self.assertEqual(result.status, "resolved")
        candidate = result.candidates[0]
        self.assertEqual(candidate.server_name, "context7")
        self.assertEqual(candidate.config["args"], ["-y", "@upstash/context7-mcp"])

    def test_close_candidates_return_ambiguity(self) -> None:
        def fetch(url: str, timeout: float):
            # 两个候选分数接近时不能自动写入，必须交给 Agent 向用户确认。
            return {
                "objects": [
                    {
                        "package": {"name": "alpha-mcp", "description": "alpha mcp"},
                        "score": {"final": 0.8},
                    },
                    {
                        "package": {"name": "alpha-mcp-server", "description": "alpha mcp"},
                        "score": {"final": 0.79},
                    },
                ]
            }

        result = resolve_mcp_query("alpha", fetch_json=fetch)

        self.assertEqual(result.status, "ambiguous")
        self.assertGreaterEqual(len(result.candidates), 2)

    def test_network_failure_is_readable_error(self) -> None:
        def fetch(url: str, timeout: float):
            raise RuntimeError("network down")

        result = resolve_mcp_query("context7", fetch_json=fetch)

        self.assertEqual(result.status, "error")
        self.assertIn("network down", result.message)


class WriteTests(TempWorkspaceHome):
    """覆盖 MCP YAML 写入的路径选择、幂等和冲突保护。"""

    def _read_yaml(self, path: Path) -> dict:
        return yaml.safe_load(path.read_text(encoding="utf-8"))

    def test_auto_defaults_to_project_scope(self) -> None:
        result = write_server_config(
            "context7",
            {"command": "npx.cmd", "args": ["-y", "@upstash/context7-mcp"]},
            scope="auto",
        )

        self.assertEqual(result.scope, "project")
        self.assertEqual(result.path, mcp_config.project_config_path())
        data = self._read_yaml(result.path)
        self.assertIn("context7", data["mcpServers"])

    def test_user_scope_writes_user_config(self) -> None:
        result = write_server_config(
            "remote",
            {"url": "https://example.com/mcp"},
            scope="user",
        )

        self.assertEqual(result.path, mcp_config.user_config_path())
        data = self._read_yaml(result.path)
        self.assertEqual(data["mcpServers"]["remote"]["url"], "https://example.com/mcp")

    def test_same_config_is_noop(self) -> None:
        first = write_server_config("s", {"url": "https://example.com/mcp"})
        second = write_server_config("s", {"url": "https://example.com/mcp"})

        self.assertTrue(first.changed)
        self.assertFalse(second.changed)
        self.assertEqual(second.action, "unchanged")

    def test_conflict_requires_replace(self) -> None:
        write_server_config("s", {"url": "https://old.example/mcp"})

        with self.assertRaises(ValueError):
            write_server_config("s", {"url": "https://new.example/mcp"})

        replaced = write_server_config("s", {"url": "https://new.example/mcp"}, replace=True)
        self.assertEqual(replaced.action, "replaced")
        data = self._read_yaml(replaced.path)
        self.assertEqual(data["mcpServers"]["s"]["url"], "https://new.example/mcp")

    def test_broken_yaml_is_not_overwritten(self) -> None:
        path = mcp_config.project_config_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("mcpServers: : : broken\n", encoding="utf-8")

        with self.assertRaises(ValueError):
            write_server_config("s", {"url": "https://example.com/mcp"})

        self.assertEqual(path.read_text(encoding="utf-8"), "mcpServers: : : broken\n")

    def test_server_config_from_entry(self) -> None:
        cfg = server_config_from_entry(
            "Context7",
            {"command": "npx.cmd", "args": ["-y", "@upstash/context7-mcp"]},
        )

        self.assertEqual(cfg.name, "context7")
        self.assertEqual(cfg.kind, "stdio")
        self.assertEqual(cfg.command, "npx.cmd")


class CommandResolveTests(unittest.TestCase):
    """覆盖 Windows 下裸命令名解析到 `.cmd` 的兼容逻辑。"""

    def test_windows_npx_prefers_cmd(self) -> None:
        def fake_which(name: str):
            return "C:\\node\\npx.cmd" if name == "npx.cmd" else None

        with mock.patch("rhinecode.mcp.transport.os.name", "nt"):
            with mock.patch("rhinecode.mcp.transport.shutil.which", side_effect=fake_which):
                self.assertEqual(resolve_stdio_command("npx"), "C:\\node\\npx.cmd")


if __name__ == "__main__":
    unittest.main()
