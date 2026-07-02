"""
会话层（c7，spec F7）：MCPClient 封装「与一个 Server 的一次会话」的三步。

一次 MCP 会话按顺序进行：
1. initialize —— 握手：交换协议版本与能力，告知对端「我是谁」。
2. notifications/initialized —— 客户端在收到 initialize 响应后发的通知，表示「握手完成，可以开工」。
3. tools/list / tools/call —— 列出工具、调用工具（可多次）。

MCPClient 只负责「按协议组织这三步」，具体收发交给注入的 Transport（stdio 或 http），
因此对传输方式无感。
"""

from typing import Optional

from rhinecode.mcp.transport import Transport

# 客户端自报信息与支持的协议版本（对齐 MCP 规范 2025-11-25）。
_PROTOCOL_VERSION = "2025-11-25"
_CLIENT_NAME = "RhineCode"
_CLIENT_VERSION = "0.1.0"

# 握手/列表阶段的默认超时（秒）。工具调用超时单独由 call_timeout 控制。
_HANDSHAKE_TIMEOUT = 30.0


class MCPClient:
    """
    一个 MCP Server 的会话客户端。

    :ivar name: Server 名字（用于日志/状态）
    :ivar server_protocol_version: 对端在 initialize 里回报的协议版本（宽容记录，不强校验）
    """

    def __init__(self, name: str, transport: Transport, call_timeout: float = 60.0):
        """
        :param name: Server 名字
        :param transport: 已构造但未 start 的传输实例
        :param call_timeout: tools/call 的超时上限（秒，spec N3）
        """
        self.name = name
        self._transport = transport
        self._call_timeout = call_timeout
        self.server_protocol_version: Optional[str] = None

    def initialize(self) -> None:
        """
        建立连接并完成握手。

        执行步骤：
        1. transport.start()（拉起子进程 / 建 http client）。
        2. 发 initialize 请求，记录对端协议版本。
        3. 发 notifications/initialized 通知，宣告握手完成。

        :raises TransportError/JsonRpcError: 任一步失败向上抛出（由 manager 逐 Server 兜底隔离）

        副作用：建立连接、可能拉起子进程。
        """
        self._transport.start()
        result = self._transport.request(
            "initialize",
            {
                "protocolVersion": _PROTOCOL_VERSION,
                "capabilities": {},  # 本章不声明 roots/sampling 等客户端能力
                "clientInfo": {"name": _CLIENT_NAME, "version": _CLIENT_VERSION},
            },
            _HANDSHAKE_TIMEOUT,
        )
        self.server_protocol_version = result.get("protocolVersion")
        # 握手完成通知：无回包。
        self._transport.notify("notifications/initialized", None)

    def list_tools(self) -> list[dict]:
        """
        列出该 Server 的全部工具，自动跟随分页游标。

        MCP 的 tools/list 结果可能带 nextCursor 表示还有下一页；这里循环翻页直到取完，
        避免只拿到第一页工具。

        :returns: 工具 dict 列表（每个含 name / description / inputSchema 等）

        副作用：发起一次或多次 tools/list 请求。
        """
        tools: list[dict] = []
        cursor: Optional[str] = None
        while True:
            params = {"cursor": cursor} if cursor else {}
            result = self._transport.request("tools/list", params, _HANDSHAKE_TIMEOUT)
            batch = result.get("tools")
            if isinstance(batch, list):
                tools.extend(t for t in batch if isinstance(t, dict))
            cursor = result.get("nextCursor")
            if not cursor:
                break
        return tools

    def call_tool(self, name: str, arguments: Optional[dict]) -> dict:
        """
        调用一个远端工具。

        :param name: 远端工具原名（不带 mcp__ 前缀）
        :param arguments: 参数字典（None 视为空）
        :returns: CallToolResult 字典（含 content 列表与可选 isError）

        副作用：发起一次 tools/call 请求（受 call_timeout 约束）。
        """
        return self._transport.request(
            "tools/call",
            {"name": name, "arguments": arguments or {}},
            self._call_timeout,
        )

    def close(self) -> None:
        """关闭底层传输（best-effort）。"""
        self._transport.close()
