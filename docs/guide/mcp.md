# MCP 客户端

> RhineCode 用户手册 · [返回手册目录](README.md) · [返回项目 README](../../README.md)

## MCP 客户端

RhineCode 可作为 [MCP](https://modelcontextprotocol.io) 客户端接入外部 MCP Server，把它们提供的工具接进工具中心，无需改动源码。启动时自动完成「连接 → `initialize` 握手 → `tools/list` 发现 → 注册」；之后远端工具与内置工具走同一套 Agent Loop 与权限管线。

- **两种传输**：本地子进程走 stdio 管道，远程走 Streamable HTTP。底层按 JSON-RPC 2.0 收发，请求带 id、响应按 id 配对（stdio 单管道复用靠后台 reader 线程派发）；stdio 的 stderr 会被后台线程持续 drain，避免 Server 大量写错误日志时堵塞握手或工具发现。
- **命名与隔离**：远端工具注册名以 `mcp__<server>__<tool>` 为基础；若远端名字含空格、斜杠等不适合作为 function name 的字符，会规范化为安全名称，`/mcp` 明细会展示 `registered_name <- server/tool`。
- **连接生命周期**：多 Server 连接缓存、单点隔离（某 Server 连接/发现失败只跳过并记录，不影响其它 Server 与启动）；程序退出时统一关闭连接、回收 stdio 子进程。
- **权限**：MCP 工具一律视为非只读，**缺省预设 `auto` 下每次调用都会弹确认面板**。
  面板上除了工具名，还会单独列出**是哪台 Server 提供的**与**完整参数**（不截断）——
  因为这个面板问的不是「这个动作危不危险」，而是「**你信不信这台 Server**」。

  ⚠ **这句话一度不成立，别照着旧文档推理。** 老说法是「默认权限模式下每次调用都经
  人在回路确认」，它说的是 `PermissionMode.DEFAULT`，而缺省预设 `auto` 是放行档——
  那段时间里 MCP 工具其实是**直接放行、零提示零面板**的。现在权限层给了它们自己的
  一类（`remote`），第④层在放行档下判 ASK，承诺回到了原来的落点。

  **嫌吵有三条出路**，都是「你自己做的、写得下来的决定」：面板上点「永久放行」；
  在 `permissions.yaml` 里写 `allow: mcp__<server>__*` 一次放行整台 Server（见下）；
  或对单个工具写 `allow: mcp__<server>__<tool>`。反过来要**更严**就写 `deny`，
  或给子 Agent 角色声明更严的 `permission_mode`。
  ⚠ 规则一律写**不带括号**的整工具形式；带括号的写法不命中，且没有任何警告。
  若工具名被规范化，请以 `/mcp` 显示的注册名写规则。
- **唯一的例外是 `mcp_add_server`**：它会写一条 `mcpServers` 配置并**立刻拉起一个任意本地命令**，因此缺省预设下**仍然弹确认面板**（面板上会完整列出服务器名、将要执行的命令、写入哪份配置）。它启动的子进程也不再继承含密钥的环境变量，但 `mcp.yaml` 里显式写下的 `env` 照常送达。
- **可观测**：底部状态栏显示「MCP：已连接 N/M · 工具 K」；`/mcp` 命令列出每个 Server 的连接状态、传输类型、工具数、失败原因与被规范化的工具名。

> 本阶段只接 MCP 的**工具**能力，不做资源 / 提示词 / 采样，也不做 Server 健康检查与自动重连。

### 自动添加 MCP

用户可以在对话里直接提出自然语言请求，例如：

```text
帮我添加 context7 MCP
添加 @upstash/context7-mcp
添加 https://example.com/mcp
```

Agent 会先调用只读工具 `mcp_resolve_server` 解析输入：URL 会直接生成 HTTP MCP 配置；自然语言名称或包名会优先通过 NPM registry 推断 stdio MCP 包。解析成功后，Agent 会向用户说明候选来源、写入位置和将要启动的外部命令，再调用 `mcp_add_server` 写入配置并只重载该 Server。未明确范围时默认写入项目级 `<项目根>/.rhinecode/mcp.yaml`；如果用户明确说“全局、所有项目、以后都用”，则写入用户级 `~/.rhinecode/mcp.yaml`。

安全边界：

- 写入配置和首次启动外部 MCP 前仍会经过现有权限确认流程。
- Windows 下自动生成的 stdio 配置会使用 `npx.cmd`，避免 `subprocess.Popen` 找不到 `npx` 时出现 `[WinError 2]`。
- 不会自动猜测或写入真实密钥；需要凭据时应使用 `${VAR}` 环境变量占位。
- 同名同配置会 no-op；同名不同配置不会静默覆盖，必须显式替换。

### 配置

从 `mcp.example.yaml` 复制，写成两层 YAML（顶层键 `mcpServers`，`name → 条目`）：

| 位置 | 层级 | 说明 |
|------|------|------|
| `~/.rhinecode/mcp.yaml` | 用户级 | 跨项目全局默认 |
| `<项目根>/.rhinecode/mcp.yaml` | 项目级 | 随仓库走、可提交 |

两层按 Server 名字合并，同名**项目级覆盖用户级**。类型自动判定：含 `command` 视为 stdio，含 `url` 视为 http。`env` 与 `headers` 的值支持 `${VAR}` 环境变量展开（变量不存在时展开为空字符串）。

```yaml
mcpServers:
  everything:                 # stdio：本地子进程
    command: npx
    args: ["-y", "@modelcontextprotocol/server-everything"]
    env:
      TOKEN: ${MY_TOKEN}
  remote-api:                 # http：Streamable HTTP 端点
    url: https://example.com/mcp
    headers:
      Authorization: Bearer ${API_KEY}
```

容错（fail-safe）：配置文件缺失视为「无 Server」正常启动；YAML 解析失败或条目结构非法时跳过问题项并收集可读错误，绝不因配置坏掉而崩溃。

