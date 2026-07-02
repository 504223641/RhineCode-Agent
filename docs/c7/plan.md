# C7 MCP 客户端 Plan

> 基于已批准的 `spec.md`。本文档与语言相关（Python 3.11+）。协议细节对齐 MCP 规范 `2025-11-25`。

## 架构概览

新增一个自成一体的 `rhinecode/mcp/` 包，内部按「配置 → 传输 → 会话 → 适配 → 编排」五层从下到上组织，对外只通过两个接触点接入现有系统：

1. **注册接触点**：`MCPManager` 在启动阶段把发现到的远端工具包装成 `Tool` 子类（`MCPTool`）注册进现有的 `ToolRegistry`——之后 Agent Loop、权限系统、TUI 对 MCP 工具与内置工具一视同仁（spec N1）。
2. **观测接触点**：`MCPManager` 暴露状态摘要，供底部状态栏（F15）与 `/mcp` 命令（F16）展示。

包内分层（下层不感知上层）：

- **配置层** `config.py`：定位并加载用户级/项目级两层 `mcp.yaml`，做 `${VAR}` 展开与容错，产出 `MCPServerConfig` 列表。
- **JSON-RPC 层** `jsonrpc.py`：纯数据——构造/解析 JSON-RPC 2.0 消息、生成自增 id、定义协议错误异常。无 I/O。
- **传输层** `transport.py`：`Transport` 抽象 + `StdioTransport`（子进程管道）+ `HttpTransport`（Streamable HTTP）。负责「把一次请求发出去、按 id 阻塞等回对应响应」，向上屏蔽 stdio/HTTP 差异与异步收发细节（spec F6/N5）。
- **会话层** `client.py`：`MCPClient` 表示与「一个 Server」的一次会话，封装 `initialize → tools/list → tools/call` 三步（spec F7），持有一个 `Transport`。
- **适配层** `tool_adapter.py`：`MCPTool(Tool)` 把一个远端工具包装成 RhineCode 工具；`execute` 调用所属 `MCPClient.call_tool`，并把 MCP 结果转成 `ToolResult`（spec F8/F9/F10/F11）。
- **编排层** `manager.py`：`MCPManager` 管理多 Server 的连接缓存、启动发现、单点隔离、生命周期与状态汇总（spec F12/F13/F14/F15/F16）。

## 核心数据结构

### MCPServerConfig（config.py）
一个 Server 的规范化配置。

- `name: str` —— Server 名字（map 的 key），用于工具命名前缀与状态展示。
- `kind: str` —— `"stdio"` 或 `"http"`，由字段自动判定。
- `command: str | None` —— stdio 型可执行文件。
- `args: list[str]` —— stdio 型参数（缺省空列表）。
- `env: dict[str, str]` —— stdio 型环境变量（值已做 `${VAR}` 展开）。
- `url: str | None` —— http 型端点地址。
- `headers: dict[str, str]` —— http 型请求头（值已做 `${VAR}` 展开）。

### ServerState（manager.py）
一个 Server 在本次运行中的连接结果，供状态栏与 `/mcp` 展示。

- `name: str`、`kind: str`
- `connected: bool` —— 是否成功连上并列出工具。
- `tool_count: int` —— 成功注册的工具数。
- `error: str | None` —— 失败原因（连接/初始化/列表任一步失败），成功时为 None。

### JSON-RPC 辅助（jsonrpc.py）
- `next_id() -> int` —— 线程安全的自增请求 id（模块级计数器 + 锁）。
- `build_request(id, method, params) -> dict`、`build_notification(method, params) -> dict`。
- `class JsonRpcError(Exception)` —— 携带 `code: int`、`message: str`、`data`，表示对端返回的 JSON-RPC error 对象。
- `class TransportError(Exception)` —— 传输层错误（进程退出、超时、HTTP 非 2xx、连接断开等）。

### Transport 抽象（transport.py）
```python
class Transport(ABC):
    def start(self) -> None: ...
        # 建立连接：stdio 拉起子进程并启动后台 reader 线程；http 初始化 client（懒连接）
    def request(self, method: str, params: dict | None, timeout: float) -> dict: ...
        # 阻塞发送一个 JSON-RPC 请求，返回其 result 字典；
        # 对端返回 error → raise JsonRpcError；传输故障/超时 → raise TransportError
    def notify(self, method: str, params: dict | None) -> None: ...
        # 发送通知（无 id、不等回包），用于 notifications/initialized
    def close(self) -> None: ...
        # 释放资源：关管道、终止子进程 / 关闭 http 会话
```

### MCPClient（client.py）
- `__init__(name: str, transport: Transport, call_timeout: float)`
- `initialize() -> None` —— `transport.start()` → `request("initialize", {...})` → `notify("notifications/initialized")`。
- `list_tools() -> list[dict]` —— `request("tools/list", {cursor?})`，跟随 `nextCursor` 翻页，汇总所有 `tools`。
- `call_tool(name: str, arguments: dict) -> dict` —— `request("tools/call", {name, arguments}, call_timeout)`，返回 `CallToolResult` 字典。
- `close() -> None` —— 委托 `transport.close()`。

### MCPTool（tool_adapter.py）
继承现有 `Tool`：
- `name = f"mcp__{server}__{tool}"`（spec F9）。
- `description` = 远端 `description`（缺失回退到工具名）。
- `parameters` = 远端 `inputSchema`（缺失/非法时回退 `{"type": "object", "properties": {}}`，保证是合法 object schema）。
- `read_only = False`（spec F11）。
- `__init__(client, server_name, remote_name, description, parameters)` 记录所属 `MCPClient` 与远端原名。
- `execute(args) -> ToolResult`：调 `client.call_tool(remote_name, args)`，把结果按下述规则转 `ToolResult`；**捕获所有异常**转 `ok=False`，绝不外抛（spec N2）。

### MCPManager（manager.py）
- `connect_all(configs: list[MCPServerConfig], registry: ToolRegistry) -> None` —— 逐 Server 连接+发现+注册，逐个 try/except 隔离（spec F13），结果写入 `self.states`。
- `states: list[ServerState]`、`_clients: list[MCPClient]`。
- `status_line() -> str | None` —— 状态栏用一行摘要；无任何 Server 配置时返回 None（不占状态栏）。
- `status_report() -> str` —— `/mcp` 用的多行明细。
- `close_all() -> None` —— 关闭全部客户端（best-effort，逐个 try/except）。

## 模块设计

### config.py
**职责**：两层 `mcp.yaml` 的定位、加载、`${VAR}` 展开、容错，产出 `MCPServerConfig` 列表 + 错误列表。
**对外接口**：
- `user_config_path() / project_config_path() -> Path`（复用 `Path.home()` 与 `tools.path_guard.workspace_root()`）。
- `load_all() -> tuple[list[MCPServerConfig], list[str]]` —— 加载两层、按 name 合并（项目级覆盖用户级）、逐条解析校验，返回 (配置列表, 可读错误列表)。
**关键细节**：
- 顶层键 `mcpServers`（map：name → 条目）。
- 类型判定：含 `command` → stdio；否则含 `url` → http；两者皆无 → 记错误并跳过该条（fail-safe，F4）。
- `${VAR}` 展开：正则 `\$\{([^}]+)\}` → `os.environ.get(name, "")`（缺失展开空串，不崩溃，F3）；仅对字符串值展开。
- 容错：文件缺失 → 该层空；YAML 解析失败/结构非法 → 该层降级为空并收集错误（对齐 `permission/config.py` 的 fail-safe 风格）。
**依赖**：`yaml`、`tools.path_guard`。

### jsonrpc.py
**职责**：JSON-RPC 2.0 消息的构造/分类与 id 生成，纯数据无 I/O。
**对外接口**：`next_id`、`build_request`、`build_notification`、`JsonRpcError`、`TransportError`、`is_response(msg)`（判断一个 dict 是否是带 id 的响应）。
**依赖**：仅标准库。

### transport.py
**职责**：屏蔽 stdio/HTTP 差异，提供「阻塞式请求 + 按 id 配对」。
**StdioTransport**：
- `start()`：`subprocess.Popen([command, *args], stdin=PIPE, stdout=PIPE, stderr=PIPE, env={**os.environ, **cfg.env}, text=True, encoding="utf-8", bufsize=1)`；启动**后台 reader 线程**循环 `readline()`。
- reader 线程：每行 `json.loads`；若是响应（有 id + result/error）→ 找到 `self._pending[id]`，写入结果并 `Event.set()`；若是服务端通知/请求 → 本章忽略（可留 TODO）。EOF/异常 → 标记连接死亡并唤醒所有挂起请求（置 `TransportError`）。
- `request()`：`id = next_id()`；建 `pending[id] = _Pending(event, box)`；持写锁把 `json.dumps(msg)+"\n"` 写入 stdin 并 flush；`event.wait(timeout)`；超时 → 删 pending 抛 `TransportError`；否则取回结果，error → 抛 `JsonRpcError`。
- `notify()`：仅写一行，不建 pending。
- `close()`：关 stdin → `terminate()` → 限时等待 → 必要时 `kill()` → join reader（守护线程，短超时）。
**HttpTransport（Streamable HTTP）**：
- 用 `httpx`（见技术决策）。`start()` 建 `httpx.Client`，无预连接。
- `request()`：`id=next_id()`；`POST url`，头部 = `cfg.headers` + `Content-Type: application/json` + `Accept: application/json, text/event-stream` +（若已有）`Mcp-Session-Id` +（init 后）`MCP-Protocol-Version`；body = JSON-RPC 请求。
  - `initialize` 的响应头若含 `Mcp-Session-Id` 则记录，后续请求回带（spec 会话管理）。
  - 响应 `Content-Type: application/json` → 直接解析 JSON-RPC 响应；
  - 响应 `text/event-stream` → 用 `client.stream(...)` 迭代 `iter_lines()`，解析 SSE `data:` 行为 JSON，取**首个 id 匹配**的响应返回，忽略交织的服务端通知；
  - 非 2xx → `TransportError`。
- `notify()`：POST 通知，期望 202/200，不解析回包。
- `close()`：若有 session id，best-effort 发 `DELETE`（带 `Mcp-Session-Id`）终止会话；关闭 `httpx.Client`。
**依赖**：`subprocess`、`threading`、`json`（stdio）；`httpx`（http）；`jsonrpc`。

### client.py
**职责**：把一个 Server 的会话三步封装成方法。
**对外接口**：见 `MCPClient` 数据结构。
**关键细节**：
- `initialize` 参数：`{"protocolVersion": "2025-11-25", "capabilities": {}, "clientInfo": {"name": "RhineCode", "version": <本项目版本>}}`；对返回的 `protocolVersion` 保持宽容（记录但不强制相等）。
- `list_tools` 处理 `nextCursor` 翻页，防止只拿到第一页。
**依赖**：`transport`、`jsonrpc`。

### tool_adapter.py
**职责**：远端工具 ↔ RhineCode `Tool` 的适配 + 结果转换。
**结果转换规则（spec F8）**：`CallToolResult` = `{content: [...], isError?: bool}`：
- 遍历 `content`：`type=="text"` → 取 `text` 累加；其它类型（image/audio/resource 等）→ 追加占位 `[非文本内容: <type>]`。
- `isError == True` → `ToolResult(ok=False, output=<累加文本>)`；否则 `ok=True`。
- `summary`：如 `f"MCP {server}/{tool} · {n} 块"`。
- 任意异常（`TransportError`/`JsonRpcError`/超时/其它）→ `ToolResult(ok=False, output=f"MCP 工具调用失败：{e}", summary="MCP 调用失败")`。
**依赖**：`tools.base`、`client`。

### manager.py
**职责**：多 Server 编排、隔离、生命周期、状态汇总。
**关键细节**：
- `connect_all`：遍历 configs，每个 Server 用一个大 `try/except` 包住「建 transport → MCPClient → initialize → list_tools → 逐工具 MCPTool 注册」，任一步异常 → `ServerState(connected=False, error=str(e))` 并**继续下一个**（spec F13）；成功 → 记录 `tool_count` 并把 client 存入 `_clients`。
- `status_line`：`f"MCP：已连接 {ok}/{total} · 工具 {tools}"`；`total==0` → None。
- `status_report`：逐 Server 一行，形如 `everything (stdio) ✓ 已连接 · 8 工具` / `broken (stdio) ✗ 失败：<原因>`；含启动错误列表。
**依赖**：`config`、`client`、`transport`、`tool_adapter`、`tools.registry`。

## 模块交互

### 启动发现流（在 __main__.py）
```
load(config.yaml) → create_provider
registry = ToolRegistry.default()
mcp_configs, mcp_cfg_errors = mcp.config.load_all()
manager = MCPManager()
manager.connect_all(mcp_configs, registry)     # 就地把 MCPTool 注册进 registry
    每个 Server：transport.start → initialize → notify(initialized) → tools/list → 注册 MCPTool
manager = 注入 ConversationManager（供 /mcp 与状态栏）
app = RhineApp(manager, cfg) ...
app.run()                                        # 阻塞
finally: manager.close_all()                     # 退出统一回收（spec F12/AC11）
```

### 工具调用流（运行时，无 MCP 特例）
```
模型产出 tool_call(name="mcp__everything__echo", args)
loop._execute → registry.get(name) → MCPTool 实例
adapter.to_request(MCPTool, args, mode) → kind="other"（未映射）
engine.decide：① 跳过（非 command）② 跳过（非路径）③ 规则 → ④ 模式兜底
    非只读 + 默认模式 → ASK（弹确认，spec F11/AC9）
放行 → MCPTool.execute(args) → client.call_tool → transport.request（阻塞按 id 配对）
CallToolResult → ToolResult → 回灌模型
```

### 关闭流
`app.run()` 返回后（用户 /exit 或退出），`__main__` 的 `finally` 调 `manager.close_all()` → 逐 client `transport.close()` → 关管道/终止子进程/关 httpx。

### 观测接线
- **状态栏**：`RhineApp._refresh_status` 取 `conversation.mcp_status_line()`（转发 `manager.status_line()`）→ `StatusBar.update_status(..., mcp_status=...)` 渲染（新增参数）。
- **/mcp 命令**：`conversation.handle_input` 加 `/mcp` 分支 → 返回 `manager.status_report()`；`CommandPanel.COMMANDS` 追加 `("/mcp", "查看 MCP 服务连接状态")`。

## 文件组织

```
rhinecode/
├── mcp/                      —— 新增包
│   ├── __init__.py
│   ├── config.py            —— MCPServerConfig、两层加载、${VAR} 展开、容错
│   ├── jsonrpc.py           —— JSON-RPC 2.0 构造/解析、id 生成、错误类型
│   ├── transport.py         —— Transport 抽象 + StdioTransport + HttpTransport
│   ├── client.py            —— MCPClient：initialize / list_tools / call_tool
│   ├── tool_adapter.py      —— MCPTool（Tool 子类）+ 结果转换
│   └── manager.py           —— MCPManager：编排/隔离/生命周期/状态
├── __main__.py              —— 修改：加载 MCP、connect_all、注入 manager、finally close_all
├── conversation.py          —— 修改：接收 manager、/mcp 命令、暴露 mcp_status_line
├── tui/
│   ├── app.py               —— 修改：_refresh_status 传 mcp_status
│   └── widgets.py           —— 修改：StatusBar.update_status 加 mcp 段、CommandPanel.COMMANDS 加 /mcp
├── permission/rules.py      —— （可选，见技术决策）other 分支工具名 glob 匹配
mcp.example.yaml             —— 新增：配置示例（stdio + http）
tests/
├── test_mcp_config.py       —— 两层合并、${VAR}、容错
├── test_mcp_jsonrpc.py      —— 消息构造、id、错误分类
├── test_mcp_transport.py    —— stdio 收发按 id 配对（用内置模拟 Server 脚本）
├── test_mcp_adapter.py      —— CallToolResult → ToolResult 转换（含 isError、非文本占位）
├── test_mcp_manager.py      —— 单 Server 隔离、状态汇总、注册进 registry
└── fixtures/mock_mcp_server.py —— 最简 stdio JSON-RPC Server（讲 initialize/tools/list/tools/call）
```

## 技术决策

| 决策点 | 选择 | 理由 |
|--------|------|------|
| 同步 vs 异步 | **同步 + 后台 reader 线程** | 现有 `Tool.execute` 与 Agent Loop 全是同步（Textual Worker 线程模型）。引入 asyncio 事件循环会与线程模型冲突。stdio 用「写在调用线程 + 后台线程读并按 id 唤醒」实现阻塞请求；HTTP 请求本就 request-scoped，直接阻塞 POST。 |
| HTTP 库 | **httpx（新增显式依赖）** | 需要流式读取 SSE（`text/event-stream`）；httpx 的 `stream()`/`iter_lines()` 干净，且 openai 已传递依赖 httpx（已装），显式声明避免依赖传递脆弱。stdlib urllib 处理 SSE 繁琐。 |
| 传输类型判定 | 含 `command`→stdio，否则含 `url`→http | 无需用户显式写 type 字段，配置更简洁；两者皆无则报错跳过。 |
| stdio 子进程环境 | 继承 `os.environ` 再叠加配置 `env` | 多数 MCP Server 依赖 PATH 等基础环境；叠加式最省心，配置 env 优先级更高。 |
| 工具命名 | `mcp__<server>__<tool>` | 与 Claude Code 一致，前缀天然隔离，不可能与内置/他 Server 重名（spec F9）。 |
| MCP 工具只读性 | 一律 `read_only=False` | 外部 Server 不可信，默认每次确认（spec F11）；不信任 `readOnlyHint`。 |
| 权限映射 | 不加 `_TOOL_MAP`，走 `other` 分支 | spec 明确不做细粒度映射；`other` 分支已能「按工具名匹配规则 + 模式兜底」，MCP 工具开箱走完整权限管线。 |
| **通配放行 `mcp__server__*`** | **建议：小改 `rules.py` 的 other 分支，用 fnmatch 匹配工具名** | 现状 other 分支要求工具名**精确相等**，`mcp__everything__*` 匹配不上（见下）。改为 fnmatch 后：普通工具名无 `*` → 仍是精确匹配（**向后兼容**），MCP 用户可用 `allow: mcp__everything__*` 一次放行整个 Server。**需你拍板**：接受此小改（+1 单测）则 AC9 保留通配写法；否则 AC9 退化为精确名 `allow: mcp__everything__echo`。 |
| 调用超时 | `MCPTool` 调用带超时上限（默认常量，如 60s） | 防止无响应 Server 挂死 Agent Loop（spec N3）。 |
| 关闭时机 | `__main__` 在 `app.run()` 后 `finally` 调 `close_all()` | `app.run()` 阻塞到退出，之后统一回收最简单可靠（spec F12/AC11）。 |
| 测试用 Server | 内置最简 stdio 模拟 Server 脚本 | 真实 npx Server 依赖外网/Node，CI 不稳；自带脚本讲 JSON-RPC 即可覆盖 AC5/AC6。 |
