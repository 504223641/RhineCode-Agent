# C7 MCP 客户端 Checklist

> 每一项通过运行代码或观察行为验证，聚焦系统行为，与具体实现解耦。
> 括号内为验证方式。对应 spec 的 AC 与 F 需求。

## 配置加载（F1–F4）

- [ ] 两层 `mcp.yaml` 合并，同名 Server 项目级覆盖用户级（验证：`python -m unittest tests.test_mcp_config`，覆盖 AC1 用例）。
- [ ] 含 `command` 判为 stdio、含 `url` 判为 http（验证：同上，AC2 用例）。
- [ ] `env`/`headers` 的 `${VAR}` 在设置环境变量时展开为其值、未设置时为空串且不崩溃（验证：同上，AC3 用例）。
- [ ] 配置文件缺失→空列表正常返回；某条目结构非法→跳过该条并收集可读错误，其余不受影响（验证：同上，AC4 用例）。

## JSON-RPC 与传输（F5–F7）

- [ ] `next_id` 递增唯一，`build_request/notification` 结构合法，`is_response` 分类正确（验证：`python -m unittest tests.test_mcp_jsonrpc`）。
- [ ] 连接模拟 stdio Server 可完成 `initialize → tools/list → tools/call` 全流程，`tools/list` 返回的工具可取得（验证：`python -m unittest tests.test_mcp_transport`，AC5）。
- [ ] 连发多个不同 id 的请求，各响应按 id 正确配对、不串包（验证：同上，AC6）。

## 工具接入与结果转换（F8–F11）

- [ ] 远端工具以 `mcp__<server>__<tool>` 命名，`read_only is False`（验证：`python -m unittest tests.test_mcp_adapter`，AC7）。
- [ ] `tools/call` 文本内容被拼接为工具结果正文；`isError=true` 时结果为失败；非文本块以占位文本代替（验证：同上，AC8）。
- [ ] 远端调用抛异常时转为 `ok=False` 的 `ToolResult`，绝不外抛使循环崩溃（验证：同上，N2 用例）。

## 权限接入（F11 / 决策 A）

- [ ] 未配规则时 MCP 工具（非只读）在默认权限模式下 `decide` 返回 ASK（验证：`python -m unittest tests.test_perm_other_glob`，AC9 反面）。
- [ ] 配置 `allow: mcp__<server>__*` 后命中规则、`decide` 返回 ALLOW（验证：同上，AC9）。
- [ ] 无 `*` 的 other 规则仍精确匹配、不误伤其它工具名（验证：同上，向后兼容回归）。

## 编排与隔离（F12–F14）

- [ ] 正常 Server 与必然失败 Server 共存时，正常 Server 工具注册进 `ToolRegistry`、失败 Server 记录原因且不影响前者（验证：`python -m unittest tests.test_mcp_manager`，AC10）。
- [ ] `status_line()` / `status_report()` 反映已连接数、工具数、失败原因（验证：同上，AC12 数据面）。
- [ ] `close_all()` 关闭连接不抛异常；stdio 子进程被回收（验证：`tests.test_mcp_transport` 中 `close()` 后 `poll()` 非 None，AC11）。

## 集成

- [ ] `MCPManager.connect_all` 注册的 `MCPTool` 出现在 `ToolRegistry.schemas()` 中，且能被 `registry.get("mcp__...")` 取回（验证：`tests.test_mcp_manager` 断言 registry 内容）。
- [ ] `__main__` 在无 `mcp.yaml` 时正常启动、无 MCP 工具、无报错（验证：`rhinecode --config config.yaml` 启动观察）。
- [ ] `MCPTool` 与内置工具走同一套 `loop._execute` + `engine.decide` 路径，无 MCP 特例分支（验证：阅读 `loop.py`/`engine.py` 无新增 MCP 判断 + 全测通过）。

## 编译与测试

- [ ] 项目编译无错误（验证：`python -m compileall rhinecode tests`）。
- [ ] 所有单元测试通过（验证：`python -m unittest discover -s tests`）。

## 端到端场景

- [ ] **场景 1（真实 stdio Server 接入）**：配置一个真实 MCP Server（如 `npx -y @modelcontextprotocol/server-everything`）于 `mcp.yaml`，启动 RhineCode → 底部状态栏显示「MCP：已连接 …」；对模型提出需要该 Server 工具的请求 → 模型调用 `mcp__everything__<tool>`，默认模式弹确认面板，确认后返回结果并回灌（验证：tmux/真实终端观察，对照 F9/F11/F14/F15）。
- [ ] **场景 2（`/mcp` 命令）**：运行时输入 `/mcp` → 列出各 Server 的连接状态、传输类型、工具数与失败原因；输入 `/` 时补全面板含 `/mcp`（验证：真实终端观察，AC13/F16）。
- [ ] **场景 3（单 Server 失败隔离）**：`mcp.yaml` 同时配一个正常 Server 与一个不存在 command 的 Server，启动 → 正常 Server 工具可用，`/mcp` 显示失败 Server 及原因，RhineCode 正常运行（验证：真实终端观察，AC10/F13）。
- [ ] **场景 4（退出回收）**：启动接入 stdio Server 后 `/exit` 退出 → 无残留子进程（验证：退出后用 `Get-Process` / 任务管理器确认无遗留 server 进程，AC11/F12）。
