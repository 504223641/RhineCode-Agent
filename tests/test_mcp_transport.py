"""传输/会话端到端单测（c7 T16）：stdio 三步会话 + id 配对 + 子进程回收（AC5/AC6/AC11）。

用内置的模拟 stdio Server（tests/fixtures/mock_mcp_server.py）作为真实子进程，
覆盖 StdioTransport + MCPClient 的真实收发路径。
"""

import os
import sys
import unittest

from rhinecode.mcp.client import MCPClient
from rhinecode.mcp.transport import StdioTransport

_MOCK_SERVER = os.path.join(os.path.dirname(__file__), "fixtures", "mock_mcp_server.py")


def _make_client() -> MCPClient:
    """用当前 Python 解释器拉起模拟 Server，返回未 initialize 的 MCPClient。"""
    transport = StdioTransport(sys.executable, [_MOCK_SERVER], {})
    return MCPClient("mock", transport, call_timeout=10.0)


class StdioSessionTests(unittest.TestCase):
    def test_initialize_and_list_tools(self) -> None:
        # AC5：initialize → tools/list 全流程，拿到 echo/boom。
        client = _make_client()
        try:
            client.initialize()
            self.assertEqual(client.server_protocol_version, "2025-11-25")
            tools = client.list_tools()
            names = {t["name"] for t in tools}
            self.assertEqual(names, {"echo", "boom"})
        finally:
            client.close()

    def test_call_tool_echo(self) -> None:
        # AC5：tools/call 正常返回文本内容。
        client = _make_client()
        try:
            client.initialize()
            result = client.call_tool("echo", {"text": "hi"})
            self.assertFalse(result.get("isError"))
            text = result["content"][0]["text"]
            self.assertIn("hi", text)
        finally:
            client.close()

    def test_id_pairing_no_crosstalk(self) -> None:
        # AC6：连发多个不同参数的调用，各自结果正确、不串包。
        client = _make_client()
        try:
            client.initialize()
            for i in range(5):
                result = client.call_tool("echo", {"n": i})
                text = result["content"][0]["text"]
                self.assertIn(f'"n": {i}', text)
        finally:
            client.close()

    def test_close_reaps_subprocess(self) -> None:
        # AC11：close() 后子进程被回收（poll 非 None）。
        client = _make_client()
        client.initialize()
        transport = client._transport  # 直接看底层进程状态
        client.close()
        # 给终止一点时间：close 内部已 wait，poll 应非 None
        self.assertIsNotNone(transport._proc.poll())


if __name__ == "__main__":
    unittest.main()
