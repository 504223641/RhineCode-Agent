# C7 MCP 自动配置补充 Plan

## 架构概览

在现有 `rhinecode/mcp/` 旁路新增自动配置能力，分成三块：解析、写入、重载。解析层只读且可失败降级；写入层负责安全修改 `mcp.yaml`；重载层复用 `MCPManager` 的连接和工具注册逻辑。

## 核心设计

- `rhinecode/mcp/auto_config.py` 提供 `resolve_mcp_query`、`write_server_config`、`server_config_from_entry` 等纯服务函数。
- `rhinecode/tools/mcp_config.py` 提供两个 Agent 工具：`MCPResolveServerTool` 只读，`MCPAddServerTool` 有副作用并持有 `MCPManager` 与 `ToolRegistry`。
- `MCPManager` 记录 server 到 client、工具名列表的映射，新增 `reload_server(cfg, registry)`，只清理和重连目标 server。
- `ToolRegistry` 新增 `unregister(name)`，供 MCP 重载移除旧工具。
- `StdioTransport` 启动前解析可执行文件：Windows 下优先尝试 `npx.cmd`、`npx.exe`、`npx.bat`。
- `__main__.py` 在创建 `MCPManager` 后注册 `mcp_add_server`，保证工具能调用运行时重载。

## 接口

`mcp_resolve_server`:

```json
{"query": "context7"}
```

返回 JSON 文本，包含 `status=resolved|ambiguous|error`、`candidates`、`message`。

`mcp_add_server`:

```json
{
  "scope": "auto",
  "server_name": "context7",
  "config": {"command": "npx.cmd", "args": ["-y", "@upstash/context7-mcp"]},
  "replace": false
}
```

`scope=auto` 在工具层默认项目级；Agent 可根据用户语义显式传 `project` 或 `user`。

## 技术决策

- 使用标准库 `urllib.request` 调 NPM registry，避免新增依赖。
- 写 YAML 使用现有 `pyyaml`，保留未知顶层字段，只维护 `mcpServers`。
- MCP 配置工具按普通 Tool 接入权限系统；`mcp_add_server.read_only=False` 触发现有 HITL。
- 连接失败时配置仍保留，但工具结果为失败并给出 `/mcp` 可见的原因。
