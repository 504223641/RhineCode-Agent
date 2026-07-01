# RhineCode

RhineCode 是一个用 Python + Textual 实现的终端 AI 编程助手，交互体验参考 Claude Code。

当前版本以 DeepSeek Provider 为主实现了 C7 阶段能力：在 C6 五层防御权限系统、C5 结构化系统提示与 C4 Agent Loop 基础上，加入一个 **MCP（Model Context Protocol）客户端**——启动时按配置连接外部 MCP Server（本地子进程走 stdio、远程走 Streamable HTTP），发现其工具并包装成 RhineCode 已有的 `Tool` 接口注册进工具中心，对 Agent Loop、权限系统、TUI 完全无感。

在此之下，每个工具执行前仍由代码（而非模型/prompt）计算「放行 / 拒绝 / 问用户」，被拒不终止循环、把结构化原因回灌模型。模型可以在一次用户请求中循环读取项目、搜索代码、执行工具（含 MCP 远端工具）、回灌结果并继续下一轮，直到自然完成或命中停止条件。Anthropic / OpenAI Provider 目前保持纯对话能力。

## 功能

- **ReAct Agent Loop**：自动执行“调用模型 → 执行工具 → 回灌结果 → 再调用模型”的多轮循环。
- **流式输出**：正文与思考内容逐块渲染，后台 Worker 不阻塞 TUI 主线程。
- **DeepSeek 工具系统**：支持读文件、glob 找文件、grep 搜内容、写文件、精确编辑文件、运行命令。
- **MCP 客户端**：启动时按配置连接外部 MCP Server（stdio 子进程 / Streamable HTTP），自动发现并注册其工具（命名为 `mcp__<server>__<tool>`），Agent 调用时无感；多 Server 连接缓存与隔离，单个挂掉不影响其它；底部状态栏显示连接状态，`/mcp` 查看明细。
- **五层防御权限系统**：每个工具执行前由 `permission/` 包的纯逻辑引擎按固定顺序计算决定——①危险命令黑名单（不可被任何配置/模式放开）→ ②路径沙箱 → ③可配置规则（`Tool(模式)`，deny 永远优先）→ ④权限模式（严格/默认/放行，`/perm` 切换）→ ⑤人在回路（确认面板四选项）。
- **可配置权限规则**：三层 YAML（用户级、项目级、本地级）声明 allow/deny；跨层合并后 deny 优先求值。
- **Plan Mode**：`/plan` 开启后先只允许只读调研和需求澄清，完整计划进入聊天记录，经用户批准后才进入执行阶段。与权限模式正交。
- **人在回路确认**：判定为「问用户」时弹出内联确认面板，四选项——本次放行 / 本会话放行 / 永久放行 / 拒绝；计划获批不等于免确认。
- **明确停止原因**：支持自然完成、迭代上限、用户取消、计划拒绝、连续未知工具、流错误等停止路径。
- **Textual TUI**：历史区、命令提示、工具行、彩色 diff、确认/澄清面板、输入框和状态栏组合成终端界面。
- **结构化系统提示**：全局提示按身份、约束、任务模式、工具使用等模块拼装，稳定内容走可缓存通道，环境信息与 Plan Mode 提醒走 `<system-reminder>` 动态注入。

> 工具调用、Plan Mode 与权限系统目前仅在 `protocol: deepseek` 且启用默认工具注册中心时可用。

## 安装

要求：Python 3.11+

```bash
git clone <repo-url>
cd RhineCode-Agent
pip install -e .
```

## 配置

复制示例配置文件并填入 API Key：

```bash
cp config.example.yaml config.yaml
```

示例：

```yaml
protocol: deepseek
model: deepseek-chat
base_url: https://api.deepseek.com
api_key: YOUR_API_KEY
```

必填字段：

| 字段 | 说明 |
|------|------|
| `protocol` | `anthropic` / `openai` / `deepseek` |
| `model` | 模型名称 |
| `base_url` | API 请求地址 |
| `api_key` | 认证密钥 |

可选字段：

| 字段 | 说明 |
|------|------|
| `debug_log` | 是否把每次请求的缓存命中/未命中 token 追加到 `.rhinecode_debug.log`，默认开启 |

## 启动

```bash
python -m rhinecode --config config.yaml
```

也可以使用安装后的命令：

```bash
rhinecode --config config.yaml
```

## 斜杠命令

| 命令 | 说明 |
|------|------|
| `/think` | 在 off / high / max 间循环切换思考模式（Anthropic / DeepSeek 生效） |
| `/plan` | 切换 Plan Mode：先规划、澄清和审批，再执行（DeepSeek 工具模式生效） |
| `/perm` | 在 默认 / 严格 / 放行 间循环切换权限模式，只影响「规则未命中」的灰色地带兜底（DeepSeek 工具模式生效） |
| `/mcp` | 查看各 MCP Server 的连接状态、传输类型、注册工具数与失败原因 |
| `/clear` | 清空当前对话历史 |
| `/exit` | 退出程序 |

运行中按 `Esc` 会请求取消当前 Agent Loop；如果正在等待确认或澄清，则由当前面板处理取消。

## 工具能力

DeepSeek 工具模式会向模型暴露以下工具：

| 工具 | 说明 | 只读 |
|------|------|------|
| `read_file` | 读取项目内文本文件，并带行号返回内容 | 是 |
| `glob_files` | 按 glob 模式查找项目内文件 | 是 |
| `grep_content` | 在项目内文本文件中按正则搜索内容 | 是 |
| `write_file` | 新建或覆盖项目内文件，并生成 diff | 否 |
| `edit_file` | 用唯一匹配的原文片段精确替换；支持 `edits` 数组批量替换 | 否 |
| `run_command` | 在项目工作目录下执行 shell 命令 | 否 |

只读工具经权限引擎放行后可并发执行；有副作用工具串行执行，是否需要确认由权限系统的五层决策管线决定（命中 allow 规则免确认、命中 deny 规则直接拒绝、灰色地带按权限模式兜底）。当决策为「问用户」时弹出确认面板，四选项：本次放行 / 本会话放行 / 永久放行 / 拒绝。

## MCP 客户端

RhineCode 可作为 [MCP](https://modelcontextprotocol.io) 客户端接入外部 MCP Server，把它们提供的工具接进工具中心，无需改动源码。启动时自动完成「连接 → `initialize` 握手 → `tools/list` 发现 → 注册」；之后远端工具与内置工具走同一套 Agent Loop 与权限管线。

- **两种传输**：本地子进程走 stdio 管道，远程走 Streamable HTTP。底层按 JSON-RPC 2.0 收发，请求带 id、响应按 id 配对（stdio 单管道复用靠后台 reader 线程派发）。
- **命名与隔离**：远端工具注册名为 `mcp__<server>__<tool>`，与内置工具、其它 Server 天然隔离，不会重名冲突。
- **连接生命周期**：多 Server 连接缓存、单点隔离（某 Server 连接/发现失败只跳过并记录，不影响其它 Server 与启动）；程序退出时统一关闭连接、回收 stdio 子进程。
- **安全默认**：MCP 工具一律视为非只读，默认权限模式下每次调用都经人在回路确认；可用权限规则 `allow: mcp__<server>__*` 一次放行整个 Server（见下）。
- **可观测**：底部状态栏显示「MCP：已连接 N/M · 工具 K」；`/mcp` 命令列出每个 Server 的连接状态、传输类型、工具数与失败原因。

> 本阶段只接 MCP 的**工具**能力，不做资源 / 提示词 / 采样，也不做 Server 健康检查与自动重连。

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

## 权限系统

每个工具执行前，`permission/` 包的纯逻辑引擎按固定顺序跑一条决策管线，得出「放行 / 拒绝 / 问用户」——权限由代码强制，不由模型或 prompt 决定（可抵抗 prompt 注入）。第一个能下定论的层即返回：

1. **危险命令黑名单**（`blacklist.py`）：正则拦截 `rm -rf` / `git push --force` / fork 炸弹 / `format`、`Remove-Item -Recurse -Force` 等已知高危命令，复合命令逐段 + 整条双重检查；**不可被任何配置或权限模式放开**。
2. **路径沙箱**（复用 `path_guard`）：文件、glob、grep 工具以启动时的当前工作目录为项目根；拒绝含 `..`、解析后越界的绝对路径、指向项目外的符号链接。
3. **可配置规则**（`rules.py`）：三层 YAML 的 allow/deny，deny 永远优先。
4. **权限模式**（`/perm`）：严格 / 默认 / 放行，只兜底「规则未命中」的灰色地带，翻不了 ①②③ 的 deny。
5. **人在回路**：判定为「问用户」时弹确认面板，四选项（本次 / 本会话 / 永久 / 拒绝）。

被拒不终止 Agent Loop，结构化拒绝原因回灌模型让其调整策略。

### 可配置权限规则

从 `permissions.example.yaml` 复制，写成三层 YAML（`allow` / `deny` 列表，每条形如 `Tool(模式)`）：

| 位置 | 层级 | 说明 |
|------|------|------|
| `~/.rhinecode/permissions.yaml` | 用户级 | 跨项目全局默认 |
| `<项目根>/.rhinecode/permissions.yaml` | 项目级 | 随仓库走、可提交 |
| `<项目根>/.rhinecode/permissions.local.yaml` | 本地级 | git 忽略；「永久放行」自动写这里 |

- 规则语法：`Bash(git *)`、`Read(config.yaml)`、`Write(src/**)`、`Edit(...)`；只写工具名（不带括号）表示匹配该工具全部调用。
- 工具名映射：`Bash`→`run_command`，`Read`→`read_file`/`glob_files`/`grep_content`，`Write`→`write_file`，`Edit`→`edit_file`。
- 命令用前缀 + glob（末尾 ` *` 或 `:*` 带词边界，`npm:*` 不误伤 `npmx`）；文件用 gitignore 风格（`.env` 任意深度命中、`src/**` 跨目录）。
- MCP 远端工具未做细粒度映射，落到「整工具规则」的 `other` 分支：规则直接写工具名，且支持 `fnmatch` 通配——`allow: mcp__everything__echo` 精确放行单个工具，`allow: mcp__everything__*` 一次放行整个 Server（无 `*` 时等价精确匹配，向后兼容普通工具名）。
- 三层合并后按 **deny 永远优先**求值（不按层级覆盖）：任一条 deny 命中即拒绝，deny 不可被 allow 翻案；没有 deny 命中、有 allow 命中则放行；都没命中交给权限模式兜底。

## Plan Mode

开启 `/plan` 后，单条用户消息会从规划阶段开始：

1. 只暴露只读工具与两个特殊交互工具：`ask_user` 和 `present_plan`。
2. 模型可以读取/搜索项目，并通过澄清面板向用户提多选问题。
3. 模型提交计划时，完整计划会先作为普通助手消息进入聊天记录。
4. 用户选择“开始执行”后，本轮进入执行阶段并开放全部工具。
5. 执行阶段的写文件、改文件、运行命令仍走权限系统逐个判断；只有命中 allow 规则或选择“本会话放行”才免确认。
6. 用户选择“暂不执行”时，本轮以“计划未执行”停止，不再让模型继续推进。

Plan Mode 开关会保持开启；下一条用户消息会重新从规划阶段开始。Plan Mode 与权限模式正交——前者管「先规划后执行」的阶段节奏，后者管「规则未命中」的兜底强度。

## 安全边界

- 权限决定由工具层代码强制，不由模型/prompt 决定（可抵抗 prompt 注入）。
- 危险命令黑名单与路径沙箱是最靠前、不可被规则或权限模式放开的硬防线。
- 沙箱是应用层前缀校验，管得住文件工具，但管不住 `run_command` 跑起来的脚本自己用代码 open 的文件（已知边界，OS 级沙箱留待后续）。
- `config.yaml` 可能包含真实 API Key，请勿提交到版本库；可用 `deny Read(config.yaml)` 规则进一步阻止模型读取。

## 项目结构

```text
rhinecode/
├── __main__.py          # CLI 入口
├── config.py            # YAML 配置加载与校验
├── conversation.py      # 对话管理、斜杠命令、权限引擎构建、Agent 回调封装
├── agent/
│   ├── events.py        # AgentEvent / StopReason / ConfirmDecision 等事件类型
│   ├── collector.py     # StreamCollector 双路收集
│   ├── loop.py          # Agent Loop 核心（含权限决策预扫接入点）
│   ├── plan_tools.py    # ask_user / present_plan 特殊工具 schema
│   ├── cache_log.py     # 缓存命中调试日志
│   └── prompt/          # 结构化系统提示模块、环境信息与 system-reminder 注入
├── permission/          # 五层防御权限系统（纯逻辑，与 TUI/Provider 解耦）
│   ├── models.py        # Decision / PermissionMode / Layer / Rule / PermissionRequest 等数据结构
│   ├── matching.py      # 命令拆分、命令/路径模式匹配
│   ├── blacklist.py     # ①危险命令黑名单
│   ├── rules.py         # ③规则 deny 优先求值
│   ├── config.py        # 三层 YAML 加载/容错/回写
│   ├── adapter.py       # 工具调用规范化为 PermissionRequest（收口工具知识）
│   └── engine.py        # PermissionEngine.decide 组装四层管线
├── mcp/                 # MCP 客户端（c7，五层：配置→JSON-RPC→传输→会话→适配→编排）
│   ├── config.py        # 两层 mcp.yaml 加载、${VAR} 展开、容错
│   ├── jsonrpc.py       # JSON-RPC 2.0 构造/解析、id 生成、错误类型（纯数据）
│   ├── transport.py     # Transport 抽象 + StdioTransport + HttpTransport
│   ├── client.py        # MCPClient：initialize / tools/list / tools/call
│   ├── tool_adapter.py  # MCPTool（Tool 子类）+ CallToolResult→ToolResult 转换
│   └── manager.py       # MCPManager：多 Server 连接缓存/隔离/生命周期/状态
├── provider/
│   ├── base.py          # BaseProvider / Message / StreamChunk / ToolCall 抽象
│   ├── anthropic.py     # Anthropic 纯对话实现
│   ├── openai.py        # OpenAI 纯对话实现
│   ├── deepseek.py      # DeepSeek 流式对话、思考与工具调用解析
│   └── factory.py       # Provider 工厂
├── tools/
│   ├── base.py          # Tool / ToolResult 抽象
│   ├── diff.py          # 结构化 diff 构造
│   ├── registry.py      # 工具注册中心
│   ├── path_guard.py    # 项目工作目录路径守卫（沙箱层复用）
│   ├── read_file.py
│   ├── write_file.py
│   ├── edit_file.py
│   ├── run_command.py
│   ├── glob_files.py
│   └── grep_content.py
└── tui/
    ├── app.py           # Textual App 主类
    └── widgets.py       # HistoryView / InputBar / StatusBar / 面板组件
```

## 测试

```bash
python -m compileall rhinecode tests
python -m unittest discover -s tests
```

当前测试覆盖路径越界防护、四态确认回调、本会话放行登记规则、Plan Mode 完整计划展示、拒绝计划停止、计划获批后仍逐项确认，以及权限系统的命令/路径匹配、危险命令黑名单（含复合命令逐段与 fork 炸弹）、deny 优先求值、配置三层加载与容错、工具规范化映射、四层决策管线、loop 决策接入（被拒不停循环、allow 规则免确认）等关键行为（`tests/test_perm_*.py`）。

MCP 客户端部分覆盖两层配置合并与 `${VAR}` 展开、JSON-RPC 消息构造与响应分类、stdio 传输三步会话与按 id 配对（用内置模拟 Server 端到端）、`CallToolResult→ToolResult` 转换（含 `isError` 与非文本占位）、单 Server 失败隔离与工具注册、以及 `other` 分支 fnmatch 通配放行（`tests/test_mcp_*.py`、`tests/test_perm_other_glob.py`）。

## 当前阶段文档

C7 的规格、实现计划、任务拆解和验收清单位于：

- `docs/c7/spec.md`
- `docs/c7/plan.md`
- `docs/c7/task.md`
- `docs/c7/checklist.md`

这些文档描述 MCP 客户端的需求、架构、任务与验收（两种传输、JSON-RPC 按 id 配对、三步会话、工具适配与注册、多 Server 隔离与生命周期、与权限/状态栏的接线）。C6（五层防御权限系统）、C5（结构化系统提示与缓存策略）、C4（Agent Loop 与 Plan Mode）文档仍保留，用于追溯设计来源。

## 后续补齐项

以下问题已在工程审查中确认，但不属于当前阶段开发范围，后续章节再统一设计和实现：

1. API Key 与敏感配置的读取脱敏、环境变量化或工作区外管理。
2. Plan Mode 规划阶段的工具阶段强校验，防止模型同轮夹带副作用工具。
3. `write_file` / `edit_file` 的文件系统级原子写入。
4. OS 级沙箱（Seatbelt / bubblewrap），约束 `run_command` 子进程自身发起的文件/网络访问。
5. 权限系统后续项：网络请求限制、资源配额、审计日志。
6. 开发环境依赖固定与 CI，让 `compileall` / `unittest` 在标准环境稳定运行。
7. MCP 后续项：Server 健康检查与自动重连、资源 / 提示词 / 采样等非工具能力、MCP 工具的细粒度权限映射与执行超时可配置化。

## 扩展新 Provider

1. 在 `rhinecode/provider/` 下新建实现文件，继承 `BaseProvider` 并实现 `stream_chat`。
2. 在 `rhinecode/provider/factory.py` 的 `create_provider` 中添加对应分支。
3. 在 `config.yaml` 中将 `protocol` 改为新值。

如果新 Provider 要支持工具调用，需要参考 `deepseek.py`：

- 把 `tools` schema 传给模型 API。
- 从流式响应中拼接工具调用参数。
- 产出 `StreamChunk(type="tool_call")`。
- 能序列化历史中的 `assistant(tool_calls)` 与 `role="tool"` 消息。

## 扩展新工具

1. 在 `rhinecode/tools/` 下新建工具实现，继承 `Tool`。
2. 声明 `name`、`description`、`parameters`、`read_only`。
3. 在 `ToolRegistry.default()` 中注册工具。
4. 文件类工具必须复用 `path_guard.py` 的路径边界校验。
5. 若要纳入细粒度权限控制，在 `permission/adapter.py` 的 `_TOOL_MAP` 加一行映射（映射到 Bash/Read/Edit/Write 规则名与对应 specifier）；未映射的工具自动落到 `other` 分支（仅按工具名匹配整工具规则 + 走权限模式兜底），不会漏过权限检查。

`read_only=True` 的工具经权限引擎放行后可并发执行；`read_only=False` 的工具串行执行，并按权限系统决策决定是否在执行前请求用户确认。
