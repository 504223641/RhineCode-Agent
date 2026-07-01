"""编排层单测（c7 T18）：单 Server 隔离、注册进 registry、状态汇总（AC10/AC12）。"""

import os
import sys
import unittest

from rhinecode.mcp.config import MCPServerConfig
from rhinecode.mcp.manager import MCPManager
from rhinecode.tools.registry import ToolRegistry

_MOCK_SERVER = os.path.join(os.path.dirname(__file__), "fixtures", "mock_mcp_server.py")


def _good_cfg() -> MCPServerConfig:
    """指向模拟 Server 的正常 stdio 配置。"""
    return MCPServerConfig(name="good", kind="stdio", command=sys.executable, args=[_MOCK_SERVER])


def _bad_cfg() -> MCPServerConfig:
    """指向不存在可执行文件的必然失败配置。"""
    return MCPServerConfig(name="bad", kind="stdio", command="definitely_not_a_real_command_xyz", args=[])


class ManagerTests(unittest.TestCase):
    def test_registers_tools_into_registry(self) -> None:
        # AC7/AC10：正常 Server 的工具以 mcp__ 前缀注册进 registry。
        registry = ToolRegistry()
        manager = MCPManager()
        try:
            manager.connect_all([_good_cfg()], registry)
            self.assertIsNotNone(registry.get("mcp__good__echo"))
            self.assertIsNotNone(registry.get("mcp__good__boom"))
            names = [s["function"]["name"] for s in registry.schemas()]
            self.assertIn("mcp__good__echo", names)
        finally:
            manager.close_all()

    def test_single_server_failure_isolated(self) -> None:
        # AC10：失败 Server 被跳过并记录原因，正常 Server 不受影响。
        registry = ToolRegistry()
        manager = MCPManager()
        try:
            manager.connect_all([_good_cfg(), _bad_cfg()], registry)
            states = {s.name: s for s in manager.states}
            self.assertTrue(states["good"].connected)
            self.assertFalse(states["bad"].connected)
            self.assertIsNotNone(states["bad"].error)
            # 正常 Server 工具仍可用
            self.assertIsNotNone(registry.get("mcp__good__echo"))
        finally:
            manager.close_all()

    def test_status_line_and_report(self) -> None:
        # AC12：状态摘要反映已连接数、工具数、失败原因。
        registry = ToolRegistry()
        manager = MCPManager()
        try:
            manager.connect_all([_good_cfg(), _bad_cfg()], registry)
            line = manager.status_line()
            self.assertIn("1/2", line)  # 2 个里连上 1 个
            self.assertIn("工具 2", line)  # good 提供 echo/boom
            report = manager.status_report()
            self.assertIn("good", report)
            self.assertIn("bad", report)
            self.assertIn("✗", report)
        finally:
            manager.close_all()

    def test_no_servers_status_line_none(self) -> None:
        manager = MCPManager()
        manager.connect_all([], ToolRegistry())
        self.assertIsNone(manager.status_line())

    def test_config_errors_surface_in_report(self) -> None:
        manager = MCPManager()
        manager.connect_all([], ToolRegistry(), extra_errors=["坏配置示例"])
        self.assertIn("坏配置示例", manager.status_report())


if __name__ == "__main__":
    unittest.main()
