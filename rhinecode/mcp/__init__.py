"""
MCP（Model Context Protocol）客户端包（c7）。

本包让 RhineCode 作为 MCP 客户端，在启动时按配置连接外部 MCP Server、
发现其提供的工具，并包装成 RhineCode 已有的 Tool 接口注册进工具中心。
对 Agent Loop / 权限系统 / TUI 而言，MCP 工具与内置工具完全无差别。

内部分层（下层不感知上层）：
- config.py      两层 mcp.yaml 加载、${VAR} 展开、容错
- jsonrpc.py     JSON-RPC 2.0 消息构造/解析、id 生成、错误类型（纯数据，无 I/O）
- transport.py   Transport 抽象 + StdioTransport（子进程管道）+ HttpTransport（Streamable HTTP）
- client.py      MCPClient：一个 Server 的会话（initialize / tools/list / tools/call）
- tool_adapter.py MCPTool：把远端工具包装成 Tool，并做结果转换
- manager.py     MCPManager：多 Server 连接的缓存/隔离/生命周期/状态汇总

协议版本对齐 MCP 规范 2025-11-25。
"""
