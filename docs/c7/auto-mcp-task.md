# C7 MCP 自动配置补充 Tasks

## 文件清单

| 操作 | 文件 | 职责 |
| --- | --- | --- |
| 新增 | `rhinecode/mcp/auto_config.py` | MCP 名称解析、配置写入、配置转对象 |
| 新增 | `rhinecode/tools/mcp_config.py` | `mcp_resolve_server` / `mcp_add_server` |
| 修改 | `rhinecode/tools/registry.py` | 注册解析工具，支持 unregister |
| 修改 | `rhinecode/mcp/manager.py` | 记录 server 工具，支持 reload_server |
| 修改 | `rhinecode/mcp/transport.py` | stdio 命令解析，修复 Windows npx |
| 修改 | `rhinecode/__main__.py` | 注册 add 工具并注入 manager/registry |
| 修改 | `rhinecode/agent/prompt/texts/tool_usage.py` | MCP 添加意图使用专用工具 |
| 修改 | `rhinecode/tui/app.py` | Agent 结束后刷新状态栏 |
| 新增 | `tests/test_mcp_auto_config.py` | 解析与写入单测 |
| 修改 | `tests/test_mcp_manager.py` | 运行时重载单测 |

## 任务

- T21: 实现 `auto_config.py`，覆盖 URL、NPM 搜索、歧义判断、scope/path、写入保护。
- T22: 实现 `mcp_config.py` 两个 Tool，输出稳定 JSON 和可读摘要。
- T23: 扩展 registry 和 manager，支持按 server 清理旧工具与旧 client。
- T24: 接入启动流程和提示词，确保 Agent 选择专用 MCP 工具。
- T25: 补充 Windows 命令解析，优先解决 `npx` 找不到的问题。
- T26: 补充测试并运行 `python -m unittest discover -s tests -p "test_*.py"`。
