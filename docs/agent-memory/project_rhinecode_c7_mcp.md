---
name: project_rhinecode_c7_mcp
description: RhineCode c7 MCP 客户端已实现并全测通过（109 测试），架构、决策与验收状态
metadata:
  type: project
---

RhineCode c7 阶段（MCP 客户端）已按 /spec 四文档（`docs/c7/spec.md→plan.md→task.md→checklist.md`，均经用户逐份审批）完成开发，T0–T20 全部落地，`python -m unittest discover -s tests` **109 测试全绿**。分支 `c7`。

新增包 `rhinecode/mcp/`（五层，下层不感知上层）：`config.py`（两层 mcp.yaml 合并/`${VAR}` 展开/fail-safe）、`jsonrpc.py`（纯数据）、`transport.py`（`Transport` 抽象 + `StdioTransport` 后台 reader 线程按 id 配对 + `HttpTransport` Streamable HTTP+SSE）、`client.py`（`MCPClient` 三步会话）、`tool_adapter.py`（`MCPTool(Tool)` + `CallToolResult→ToolResult`）、`manager.py`（`MCPManager` 多 Server 缓存/隔离/生命周期/状态）。接线：`__main__.py`（`connect_all` + `finally close_all`）、`conversation.py`（`/mcp` + `mcp_status_line`）、`tui/widgets.py`（状态栏 MCP 段 + `CommandPanel` 加 `/mcp`）、`tui/app.py`（`_refresh_status` 传 mcp_status）。

四个锁定的关键决策：① 独立两层 `mcp.yaml`（user+project，项目级覆盖）；② 工具命名 `mcp__<server>__<tool>`；③ MCP 工具一律 `read_only=False`（默认走确认）；④ 状态栏 + `/mcp` 双通道可观测。技术：同步 + 后台线程（不引入 asyncio，契合 Textual Worker）；新增依赖 `httpx`；**决策 A**——改 `permission/rules.py` 的 `other` 分支为 `fnmatch` 工具名匹配（无 `*` 等价精确、向后兼容），使 `allow: mcp__server__*` 可一次放行整个 Server。

踩坑记录：Windows 上 Python 子进程默认按 cp936 写 stdout，而 MCP 规范要求 UTF-8 → reader 线程 `UnicodeDecodeError` 崩溃。修复：模拟 Server `sys.stdout.reconfigure(encoding="utf-8")` + `StdioTransport` 的 Popen 加 `errors="replace"` 兜底。

不做（留后续）：resources/prompts/sampling、健康检查/自动重连、细粒度权限映射、旧版 HTTP+SSE、图片渲染。**待人工验证**：checklist 的 4 个端到端场景（需 npx/node + 真实终端 TUI，无法在无头环境跑）。README.md 与 CLAUDE.md 已同步更新到 c7。延续自 [[project_rhinecode_c6_permissions]]。
