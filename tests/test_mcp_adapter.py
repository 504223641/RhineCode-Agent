"""适配层单测（c7 T17）：CallToolResult → ToolResult 转换、命名、只读性、异常兜底（AC7/AC8/N2）。"""

import unittest

from rhinecode.mcp.tool_adapter import MCPTool


class _StubClient:
    """假 client：call_tool 返回预置结果或抛异常，隔离真实传输。"""

    def __init__(self, result=None, exc=None):
        self._result = result
        self._exc = exc
        self.calls: list[tuple] = []

    def call_tool(self, name, arguments):
        self.calls.append((name, arguments))
        if self._exc is not None:
            raise self._exc
        return self._result


def _tool(client, params=None) -> MCPTool:
    return MCPTool(client, "srv", "echo", "回显", params)


class NamingTests(unittest.TestCase):
    def test_name_prefix(self) -> None:
        # AC7：注册名带 mcp__<server>__<tool> 前缀。
        t = _tool(_StubClient())
        self.assertEqual(t.name, "mcp__srv__echo")

    def test_not_read_only(self) -> None:
        # AC9 前提：MCP 工具一律非只读。
        self.assertFalse(_tool(_StubClient()).read_only)

    def test_empty_schema_fallback(self) -> None:
        t = _tool(_StubClient(), params=None)
        self.assertEqual(t.parameters, {"type": "object", "properties": {}})

    def test_description_fallback_to_name(self) -> None:
        t = MCPTool(_StubClient(), "srv", "echo", "", None)
        self.assertEqual(t.description, "echo")


class ConversionTests(unittest.TestCase):
    def test_text_content_ok(self) -> None:
        # AC8：文本内容拼接为正文，ok=True。
        client = _StubClient(result={"content": [{"type": "text", "text": "hello"}], "isError": False})
        res = _tool(client).execute({"text": "x"})
        self.assertTrue(res.ok)
        self.assertEqual(res.output, "hello")
        self.assertEqual(client.calls[0], ("echo", {"text": "x"}))

    def test_multiple_text_blocks_joined(self) -> None:
        client = _StubClient(result={"content": [
            {"type": "text", "text": "a"},
            {"type": "text", "text": "b"},
        ]})
        res = _tool(client).execute({})
        self.assertEqual(res.output, "a\nb")

    def test_is_error_marks_failure(self) -> None:
        # AC8：isError=true → ok=False。
        client = _StubClient(result={"content": [{"type": "text", "text": "failed"}], "isError": True})
        res = _tool(client).execute({})
        self.assertFalse(res.ok)
        self.assertEqual(res.output, "failed")

    def test_non_text_block_placeholder(self) -> None:
        client = _StubClient(result={"content": [{"type": "image", "data": "..."}], "isError": False})
        res = _tool(client).execute({})
        self.assertIn("[非文本内容: image]", res.output)

    def test_exception_is_caught(self) -> None:
        # N2：call_tool 抛异常 → ok=False，绝不外抛。
        client = _StubClient(exc=RuntimeError("boom"))
        res = _tool(client).execute({})
        self.assertFalse(res.ok)
        self.assertIn("boom", res.output)


if __name__ == "__main__":
    unittest.main()
