# C7 MCP 客户端 Tasks

> 基于已批准的 `spec.md` + `plan.md`。技术决策取 **A**（`rules.py` other 分支改 fnmatch，向后兼容且支持 `mcp__server__*` 通配放行）。
> 每个任务 2–5 分钟粒度，均带可运行的验证方式。

## 文件清单

| 操作 | 文件 | 职责 |
|------|------|------|
| 新建 | `rhinecode/mcp/__init__.py` | 包标识（空） |
| 修改 | `pyproject.toml` | 依赖加 `httpx>=0.27` |
| 新建 | `rhinecode/mcp/jsonrpc.py` | JSON-RPC 2.0 构造/解析、id 生成、错误类型 |
| 新建 | `rhinecode/mcp/config.py` | MCPServerConfig、两层加载、`${VAR}` 展开、容错 |
| 新建 | `mcp.example.yaml` | 配置示例（stdio + http） |
| 新建 | `rhinecode/mcp/transport.py` | Transport 抽象 + StdioTransport + HttpTransport |
| 新建 | `rhinecode/mcp/client.py` | MCPClient：initialize / list_tools / call_tool |
| 新建 | `rhinecode/mcp/tool_adapter.py` | MCPTool（Tool 子类）+ 结果转换 |
| 新建 | `rhinecode/mcp/manager.py` | MCPManager：编排/隔离/生命周期/状态 |
| 修改 | `rhinecode/permission/rules.py` | other 分支工具名改 fnmatch（决策 A） |
| 修改 | `rhinecode/__main__.py` | 加载 MCP、connect_all、注入 manager、finally close_all |
| 修改 | `rhinecode/conversation.py` | 接收 manager、`/mcp` 命令、暴露 mcp_status_line |
| 修改 | `rhinecode/tui/widgets.py` | StatusBar 加 mcp 段、CommandPanel 加 `/mcp` |
| 修改 | `rhinecode/tui/app.py` | `_refresh_status` 传 mcp_status |
| 新建 | `tests/fixtures/mock_mcp_server.py` | 最简 stdio JSON-RPC 模拟 Server |
| 新建 | `tests/test_mcp_jsonrpc.py` | 消息构造、id、错误分类 |
| 新建 | `tests/test_mcp_config.py` | 两层合并、`${VAR}`、容错 |
| 新建 | `tests/test_mcp_transport.py` | stdio 按 id 配对（用模拟 Server） |
| 新建 | `tests/test_mcp_adapter.py` | CallToolResult → ToolResult 转换 |
| 新建 | `tests/test_mcp_manager.py` | 单 Server 隔离、状态汇总、注册进 registry |
| 新建 | `tests/test_perm_other_glob.py` | other 分支 fnmatch（决策 A 回归 + 通配） |

## T0: 建包骨架 + 加依赖

**文件：** `rhinecode/mcp/__init__.py`、`pyproject.toml`
**依赖：** 无
**步骤：**
1. 新建空文件 `rhinecode/mcp/__init__.py`（包标识，加一行模块 docstring 说明这是 MCP 客户端包）。
2. 在 `pyproject.toml` 的 `dependencies` 列表追加 `"httpx>=0.27"`。
**验证：** `python -c "import rhinecode.mcp"` 无报错；`python -c "import httpx"` 无报错（确认 httpx 已装，openai 已传递依赖）。

## T1: JSON-RPC 层

**文件：** `rhinecode/mcp/jsonrpc.py`
**依赖：** T0
**步骤：**
1. 模块级线程安全自增计数器 + 锁，实现 `next_id() -> int`。
2. 实现 `build_request(id, method, params) -> dict`（`{"jsonrpc":"2.0","id":id,"method":method,"params":params}`，params 为 None 时省略键）与 `build_notification(method, params) -> dict`（无 id）。
3. 实现 `is_response(msg: dict) -> bool`（有 `id` 且含 `result` 或 `error`）。
4. 定义 `class JsonRpcError(Exception)`（属性 `code/message/data`）与 `class TransportError(Exception)`。
**验证：** `python -m compileall rhinecode/mcp/jsonrpc.py`；`python -c "from rhinecode.mcp import jsonrpc; a,b=jsonrpc.next_id(),jsonrpc.next_id(); assert b>a; print(jsonrpc.build_request(a,'ping',None))"`。

## T2: 配置层

**文件：** `rhinecode/mcp/config.py`、`mcp.example.yaml`
**依赖：** T0
**步骤：**
1. 定义 `@dataclass MCPServerConfig`（name/kind/command/args/env/url/headers，字段见 plan）。
2. `user_config_path()`=`Path.home()/".rhinecode"/"mcp.yaml"`；`project_config_path()`=`path_guard.workspace_root()/".rhinecode"/"mcp.yaml"`。
3. `_expand_env(value: str) -> str`：正则 `\$\{([^}]+)\}` 替换为 `os.environ.get(name, "")`。
4. `_parse_server(name, raw) -> tuple[MCPServerConfig|None, str|None]`：判类型（有 command→stdio，否则有 url→http，皆无→返回错误串）；对 env/headers 的字符串值逐个 `_expand_env`。
5. `_load_layer(path) -> tuple[dict, str|None]`：读 YAML，取顶层 `mcpServers` map；缺失/解析失败/结构非法 → 空 + 错误（fail-safe）。
6. `load_all() -> tuple[list[MCPServerConfig], list[str]]`：加载用户级、项目级两层原始 map，按 name 合并（项目级覆盖用户级），逐条 `_parse_server`，汇总配置与错误。
**验证：** `python -m compileall rhinecode/mcp/config.py`；后续由 T14 单测覆盖。

## T3: 传输层 — StdioTransport

**文件：** `rhinecode/mcp/transport.py`
**依赖：** T1
**步骤：**
1. 定义 `Transport(ABC)`：抽象方法 `start / request / notify / close`（签名见 plan）。
2. 定义内部 `_Pending`（`threading.Event` + 结果盒子）。
3. `StdioTransport(command, args, env, ...)`：
   - `start()`：`subprocess.Popen(..., stdin/stdout=PIPE, stderr=PIPE, env={**os.environ, **env}, text=True, encoding="utf-8", bufsize=1)`；启动守护 reader 线程。
   - reader 线程：循环 `readline()`；空串（EOF）→ 标记死亡、唤醒全部 pending（置 TransportError）后退出；否则 `json.loads`，若 `is_response` → 取 `_pending.pop(id)` 写结果并 `set()`，否则忽略（服务端通知）。
   - `request(method, params, timeout)`：`id=next_id()`；登记 pending；写锁下 `stdin.write(json.dumps(msg)+"\n"); stdin.flush()`；`event.wait(timeout)`；超时→删 pending 抛 TransportError；有 error→抛 JsonRpcError；否则返回 result。
   - `notify(method, params)`：写一行不登记 pending。
   - `close()`：关 stdin → `terminate()` → 限时 `wait` → 必要时 `kill()`；reader 为守护线程。
**验证：** `python -m compileall rhinecode/mcp/transport.py`；由 T16 用模拟 Server 端到端验证。

## T4: 传输层 — HttpTransport

**文件：** `rhinecode/mcp/transport.py`（同文件追加）
**依赖：** T1、T3（复用抽象）
**步骤：**
1. `HttpTransport(url, headers, ...)`：`start()` 建 `httpx.Client()`，持有 `self._session_id=None`。
2. `request(method, params, timeout)`：`id=next_id()`；组装头（configured headers + `Content-Type: application/json` + `Accept: application/json, text/event-stream` + 有则 `Mcp-Session-Id` + init 后 `MCP-Protocol-Version`）；`POST`。
   - 若响应头含 `Mcp-Session-Id` → 记录到 `self._session_id`。
   - `Content-Type` 含 `application/json` → 解析为 JSON-RPC 响应。
   - 含 `text/event-stream` → 用 `client.stream("POST", ...)` 迭代 `iter_lines()`，取 `data:` 行拼 JSON，返回首个 `is_response` 且 id 匹配者。
   - 非 2xx → TransportError（带状态码与响应体片段）。
   - error 对象 → JsonRpcError。
3. `notify`：POST 通知，不解析回包。
4. `close()`：若有 session id best-effort 发 `DELETE`；关 `httpx.Client`。
**验证：** `python -m compileall rhinecode/mcp/transport.py`（HTTP 路径不做自动化单测，留端到端手测；见 checklist）。

## T5: 会话层 MCPClient

**文件：** `rhinecode/mcp/client.py`
**依赖：** T3、T4、T1
**步骤：**
1. `MCPClient(name, transport, call_timeout)`。
2. `initialize()`：`transport.start()` → `request("initialize", {protocolVersion:"2025-11-25", capabilities:{}, clientInfo:{name:"RhineCode", version:<版本>}}, timeout)` → 记录返回的 `protocolVersion`（宽容）→ `notify("notifications/initialized", None)`。
3. `list_tools()`：循环 `request("tools/list", {"cursor":cursor} 或 {})`，累加 `result["tools"]`，跟随 `nextCursor` 直至无；返回工具 dict 列表。
4. `call_tool(name, arguments)`：`request("tools/call", {"name":name, "arguments":arguments or {}}, call_timeout)`，返回 result（CallToolResult）。
5. `close()`：`transport.close()`。
**验证：** `python -m compileall rhinecode/mcp/client.py`；由 T16 端到端覆盖三步。

## T6: 适配层 MCPTool

**文件：** `rhinecode/mcp/tool_adapter.py`
**依赖：** T5、`tools.base`
**步骤：**
1. `MCPTool(Tool)`：`__init__(client, server_name, remote_name, description, parameters)` 设 `self.name=f"mcp__{server_name}__{remote_name}"`、`self.description`、`self.parameters`（回退 `{"type":"object","properties":{}}`）、`self.read_only=False`。
2. `execute(args)`：`try` 调 `client.call_tool(remote_name, args)`；
   - 遍历 `content`：`type=="text"`→累加 `text`；否则追加 `[非文本内容: <type>]`。
   - `isError` 为真→`ToolResult(ok=False, output=文本)`；否则 `ok=True`。
   - `summary=f"MCP {server}/{tool} · {n} 块"`。
   - `except (TransportError, JsonRpcError, Exception)`→`ToolResult(ok=False, output=f"MCP 工具调用失败：{e}", summary="MCP 调用失败")`（绝不外抛）。
**验证：** `python -m compileall rhinecode/mcp/tool_adapter.py`；由 T17 覆盖转换分支。

## T7: 编排层 MCPManager

**文件：** `rhinecode/mcp/manager.py`
**依赖：** T2、T5、T6、`tools.registry`
**步骤：**
1. 定义 `@dataclass ServerState`（name/kind/connected/tool_count/error）。
2. `MCPManager`：`self.states: list[ServerState]`、`self._clients: list[MCPClient]`。
3. `_build_transport(cfg)`：按 `cfg.kind` 造 StdioTransport / HttpTransport。
4. `connect_all(configs, registry, extra_errors=None)`：逐 config 用 try/except 包住「建 transport→MCPClient→initialize→list_tools→逐工具 register(MCPTool)」；成功→存 client、记 `ServerState(connected=True, tool_count=n)`；失败→`ServerState(connected=False, error=str(e))` 并继续（隔离）。把 `extra_errors`（配置解析错误）也纳入展示。
5. `status_line()`：`total=len(states)`；`total==0`→None；否则 `f"MCP：已连接 {ok}/{total} · 工具 {tools}"`。
6. `status_report()`：多行明细，每 Server 一行（含 ✓/✗ 与失败原因）+ 配置错误。
7. `close_all()`：逐 client `try: transport.close()`。
**验证：** `python -m compileall rhinecode/mcp/manager.py`；由 T18 覆盖隔离与注册。

## T8: 权限 other 分支改 fnmatch（决策 A）

**文件：** `rhinecode/permission/rules.py`
**依赖：** 无
**步骤：**
1. 在 `_rule_matches` 的 other 分支：把 `return rule.pattern == ""` 前的工具名比较从「`rule.tool != request.rule_name` 精确」改为——先判 `rule.pattern == ""`，工具名用 `fnmatch.fnmatch(request.rule_name, rule.tool)` 匹配（顶部 `import fnmatch`）。
2. 注意：只影响 other 分支的工具名匹配；`command/path` 分支的 `rule.tool != request.rule_name` 精确判断保持不变（这些分支工具名是 Bash/Read/Edit/Write 固定体系名）。更新该分支中文注释，说明 fnmatch 对无 `*` 的名字等价精确匹配（向后兼容），并支持 `mcp__server__*`。
**验证：** `python -m compileall rhinecode/permission/rules.py`；由 T19 覆盖回归 + 通配。

## T9: 入口接线

**文件：** `rhinecode/__main__.py`
**依赖：** T2、T7
**步骤：**
1. import `rhinecode.mcp.config`、`rhinecode.mcp.manager.MCPManager`。
2. `registry = ToolRegistry.default()` 之后：`mcp_configs, mcp_errors = mcp_config.load_all()`；`manager = MCPManager()`；`manager.connect_all(mcp_configs, registry, extra_errors=mcp_errors)`。
3. `ConversationManager(provider, cfg, registry, mcp_manager=manager)`（新增参数）。
4. 用 `try/finally` 包住 `app.run()`，`finally: manager.close_all()`（spec F12/AC11）。
**验证：** `python -m compileall rhinecode/__main__.py`；`rhinecode --config config.yaml`（无 mcp.yaml 时）正常启动、无 MCP 工具、无报错。

## T10: 协调层接线 + /mcp 命令

**文件：** `rhinecode/conversation.py`
**依赖：** T7
**步骤：**
1. `__init__` 增参 `mcp_manager: Optional[MCPManager] = None`，存 `self._mcp_manager`。
2. `handle_input` 加 `/mcp` 分支：返回 `self._mcp_manager.status_report()`（无 manager 时提示「未启用 MCP」）。
3. 加方法 `mcp_status_line() -> str | None`：转发 `self._mcp_manager.status_line()`（无则 None）。
**验证：** `python -m compileall rhinecode/conversation.py`；由 T18/端到端覆盖。

## T11: 状态栏 + 补全列表

**文件：** `rhinecode/tui/widgets.py`
**依赖：** T10
**步骤：**
1. `StatusBar.update_status` 增参 `mcp_status: str | None = None`；非 None 时在文本尾部追加 ` | {escape(mcp_status)}` 段（注意含字面 `[` 需 `escape`）。
2. `CommandPanel.COMMANDS` 追加 `("/mcp", "查看 MCP 服务连接状态")`（成对维护，spec F16）。
**验证：** `python -m compileall rhinecode/tui/widgets.py`；输入 `/` 补全面板能看到 `/mcp`（端到端）。

## T12: app 刷新状态栏

**文件：** `rhinecode/tui/app.py`
**依赖：** T10、T11
**步骤：**
1. `_refresh_status` 取 `self.manager.mcp_status_line()`（协调层实例）并作为 `mcp_status=` 传入 `StatusBar.update_status(...)`。
2. 因 MCP 状态启动后不变，无需加入命令刷新白名单；确保初始渲染即带上。
**验证：** `python -m compileall rhinecode/tui/app.py`；启动后状态栏显示「MCP：已连接 …」（端到端）。

## T13: 模拟 stdio Server

**文件：** `tests/fixtures/mock_mcp_server.py`
**依赖：** T1（消息格式对齐）
**步骤：**
1. 独立可执行脚本：从 stdin 逐行读 JSON-RPC，按 method 回：
   - `initialize`→回 `{protocolVersion, capabilities:{}, serverInfo:{name,version}}`。
   - `notifications/initialized`→通知，无回包。
   - `tools/list`→回 `{tools:[{name:"echo", description:"...", inputSchema:{type:object,...}}, {name:"boom",...}]}`。
   - `tools/call`：`echo`→回 `{content:[{type:text,text:<回显参数>}], isError:false}`；`boom`→`{content:[{type:text,text:"failed"}], isError:true}`。
2. 每个响应写一行 JSON + flush，按请求 id 回带。
**验证：** `python -m compileall tests/fixtures/mock_mcp_server.py`；手动 `echo '{...}' | python tests/fixtures/mock_mcp_server.py` 观察回包（可选）。

## T14: 配置单测

**文件：** `tests/test_mcp_config.py`
**依赖：** T2
**步骤：**
1. 用 monkeypatch/临时目录覆盖 `user_config_path`/`project_config_path`，写两层 YAML 验证合并 + 项目级覆盖同名（AC1）。
2. stdio 与 http 各一，验证 kind 判定（AC2）。
3. `env`/`headers` 写 `${VAR}`，设/不设环境变量验证展开值/空串（AC3）。
4. 文件缺失→空列表；结构非法条目→跳过 + 收集错误（AC4）。
**验证：** `python -m unittest tests.test_mcp_config`。

## T15: JSON-RPC 单测

**文件：** `tests/test_mcp_jsonrpc.py`
**依赖：** T1
**步骤：**
1. `next_id` 递增且唯一（AC6 基础）。
2. `build_request`/`build_notification` 结构正确（有/无 id）。
3. `is_response` 对响应/通知分类正确。
**验证：** `python -m unittest tests.test_mcp_jsonrpc`。

## T16: 传输/会话端到端单测

**文件：** `tests/test_mcp_transport.py`
**依赖：** T3、T5、T13
**步骤：**
1. 以 `sys.executable tests/fixtures/mock_mcp_server.py` 起 StdioTransport + MCPClient。
2. `initialize` → `list_tools` 返回 echo/boom（AC5）。
3. 连发多个不同 id 的 `tools/call`，验证各自结果按 id 正确配对不串包（AC6）。
4. `close()` 后子进程被回收（`poll()` 非 None，AC11）。
**验证：** `python -m unittest tests.test_mcp_transport`。

## T17: 适配转换单测

**文件：** `tests/test_mcp_adapter.py`
**依赖：** T6
**步骤：**
1. 用假 client（stub `call_tool` 返回构造的 CallToolResult）：
   - 纯文本 content → `ok=True`，output 为拼接文本（AC8）。
   - `isError=true` → `ok=False`（AC8）。
   - 含非文本块 → 出现 `[非文本内容: ...]` 占位。
   - `call_tool` 抛异常 → `ok=False` 且不外抛（N2）。
2. 验证 `name == "mcp__<server>__<tool>"`、`read_only is False`（AC7/AC9 前提）。
**验证：** `python -m unittest tests.test_mcp_adapter`。

## T18: 编排/隔离单测

**文件：** `tests/test_mcp_manager.py`
**依赖：** T7、T13
**步骤：**
1. 一个正常 stdio Server（模拟脚本）+ 一个必然失败的 Server（不存在的 command）：`connect_all` 后正常 Server 工具注册进 registry、失败 Server 记录 error 且不影响前者（AC10）。
2. `status_line()`/`status_report()` 反映「已连接数/工具数/失败原因」（AC12 数据面）。
3. `close_all()` 不抛异常。
**验证：** `python -m unittest tests.test_mcp_manager`。

## T19: 权限 fnmatch 单测

**文件：** `tests/test_perm_other_glob.py`
**依赖：** T8
**步骤：**
1. 回归：无 `*` 的 other 规则（如 `some_tool`）仍精确匹配、不误伤别的工具名（向后兼容）。
2. 通配：`allow: mcp__everything__*` 命中 `mcp__everything__echo`，默认模式下 `engine.decide` 返回 ALLOW（AC9）。
3. 未配规则时 MCP 工具（非只读）默认模式返回 ASK（AC9 反面）。
**验证：** `python -m unittest tests.test_perm_other_glob`。

## T20: 全量校验

**文件：** —（收尾）
**依赖：** 以上全部
**步骤：**
1. `python -m compileall rhinecode tests`。
2. `python -m unittest discover -s tests`。
3. 修复所有失败后再进入验收（checklist）。
**验证：** 两条命令均通过、无 ERROR。

## 执行顺序

```
T0 → T1 → T2 → T3 → T4 → T5 → T6 → T7
                                      ↘
T8（独立，可并行）                      T9 → T10 → T11 → T12
                                      ↘
测试：T13 → {T14,T15,T16,T17,T18} ，T19 依赖 T8
最后：T20 全量校验
```
