# C7 MCP 自动配置补充 Spec

## 背景

C7 已支持通过 `mcp.yaml` 手动声明 MCP Server，并在启动时连接、发现工具、注册到 `ToolRegistry`。当前问题是用户必须知道包名、命令、Windows 可执行文件差异和配置文件位置，体验成本较高。

## 目标

- 用户只需告诉 Agent 要添加的 MCP 名称，Agent 自动解析配置、写入 `mcp.yaml`，并在当前会话里立即加载。
- 对外部命令保持安全边界：写配置和首次启动 MCP 前仍走现有人工确认。
- 保持 MCP 工具接入方式不变：新增 MCP 工具仍通过 `ToolRegistry` 和权限系统运行。

## 功能需求

- F17: 新增只读工具 `mcp_resolve_server`，输入 MCP 名称、包名或 URL，输出候选 server 配置、来源、置信度和风险提示；URL 直接解析为 HTTP MCP。
- F18: NPM 分发的 stdio MCP 优先通过 NPM registry 搜索推断；Windows 默认生成 `npx.cmd`，其它平台生成 `npx`。
- F19: 低置信度或多个候选接近时返回歧义结果，由 Agent 向用户确认，不写配置。
- F20: 新增有副作用工具 `mcp_add_server`，只允许写用户级 `~/.rhinecode/mcp.yaml` 或项目级 `.rhinecode/mcp.yaml`。
- F21: 写入配置时保护用户内容：坏 YAML、顶层结构异常、同名冲突且未 `replace` 时不覆盖；同名相同配置视为 no-op。
- F22: 写入成功后按 server 名称立即重载，只断开该 server 的旧连接、移除旧工具、连接新 server 并注册新工具。
- F23: Agent 提示词要求遇到“添加/安装/启用 MCP”意图时使用专用 MCP 工具，不直接手写 YAML。

## 非功能需求

- N6: 网络失败、NPM 响应异常、MCP 启动失败都返回可读错误，不使 Agent Loop 或 TUI 崩溃。
- N7: 不自动生成真实密钥；如发现可能需要 token/API key，仅提示用户配置环境变量。
- N8: 运行时重载只影响目标 server，不重连其它 MCP，不移除内置工具。

## 验收标准

- AC14: 输入 URL 可解析为 HTTP MCP 配置。
- AC15: 输入 `context7` 这类自然名，在 mock NPM 搜索结果中可选出 `@upstash/context7-mcp`。
- AC16: 多候选接近或置信度低时返回歧义，不写配置。
- AC17: 项目级、用户级、`scope=auto` 均写到正确配置文件；坏 YAML 和同名冲突被拒绝。
- AC18: 写入成功后新增 MCP 工具立即出现在 registry；替换时旧工具被移除、旧连接被关闭。
- AC19: Windows 下 stdio 启动会解析 `npx`/`npx.cmd`，避免 `[WinError 2]`。
