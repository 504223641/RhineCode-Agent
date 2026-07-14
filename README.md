# RhineCode

RhineCode 是一个用 Python + Textual 实现的终端 AI 编程助手，交互体验参考 Claude Code。

当前版本以 DeepSeek Provider 为主实现了 C9 阶段能力：在 C8 上下文管理、C7 MCP 客户端、C6 五层防御权限系统、C5 结构化系统提示与 C4 Agent Loop 基础上，加入一套 **记忆系统（项目指令 · 会话存档 · 自动笔记）**——三层 RHINE.md 项目指令（用户级 → 项目 `.rhinecode` → 项目根拼接，支持 `@include` 展开）与两级记忆索引在处理首个请求前注入系统提示；每条消息即时以 JSONL 追加写入 `<项目根>/.rhinecode/sessions/`，`/resume` 弹出交互式会话选择面板（上下键选择、回车载入、Esc 退出），载入后聊天区清空并回放该会话的全部历史，相当于完整切换 session，`rhine --continue` 启动时恢复最近会话并同样回放；Agent Loop 自然停止后异步调一次 LLM 把值得记的内容沉淀为四类笔记（用户偏好 / 纠正反馈 / 项目知识 / 参考资料），索引每次注入、正文按需读取；多实例并发由锁文件防护（原子创建、非阻塞退让、过期自愈）。

在此之下，C8 的 **上下文管理（两层压缩）** 仍在——让对话累积再多也不会因超出上下文窗口而瘫掉：每次 API 请求前，先用「锚点 + 增量」近似估算历史 token 用量；**第一层**零成本地把过大的工具结果存盘、历史只留预览与路径；若仍逼近窗口上限，**第二层**调一次 LLM 把较早的消息压成结构化摘要、近期原文保留。全程幂等、fail-safe，连续摘要失败 3 次熔断，用户原始消息永不被改写。C7 的 **MCP（Model Context Protocol）客户端** 也仍在：启动时按配置连接外部 MCP Server（本地子进程走 stdio、远程走 Streamable HTTP），发现其工具并包装成 RhineCode 已有的 `Tool` 接口注册进工具中心，对 Agent Loop、权限系统、TUI 完全无感。每个工具执行前仍由代码（而非模型/prompt）计算「放行 / 拒绝 / 问用户」，被拒不终止循环、把结构化原因回灌模型。模型可以在一次用户请求中循环读取项目、搜索代码、执行工具（含 MCP 远端工具）、回灌结果并继续下一轮，直到自然完成或命中停止条件。Anthropic / OpenAI Provider 保持纯对话能力，但同样享受 RHINE.md 项目指令注入与会话存档/恢复。

## 功能

- **ReAct Agent Loop**：自动执行“调用模型 → 执行工具 → 回灌结果 → 再调用模型”的多轮循环。
- **流式输出**：正文与思考内容逐块渲染，后台 Worker 不阻塞 TUI 主线程。
- **DeepSeek 工具系统**：支持读文件、glob 找文件、grep 搜内容、写文件、精确编辑文件、运行命令；大文件读取需要显式行范围，文件发现类工具会逐文件尊重 `Read(...)` deny 规则。
- **MCP 客户端**：启动时按配置连接外部 MCP Server（stdio 子进程 / Streamable HTTP），自动发现并注册其工具（以 `mcp__<server>__<tool>` 为基础命名，必要时规范化为安全 function name），Agent 调用时无感；也支持用户直接说“帮我添加 context7 MCP”，由 Agent 解析候选、写入配置并在当前会话中重载；多 Server 连接缓存与隔离，单个挂掉不影响其它；底部状态栏显示连接状态，`/mcp` 查看明细。
- **记忆系统**：三层 RHINE.md 项目指令（用户级 → 项目 `.rhinecode` → 项目根，支持 `@include` 展开，`/init` 可让 Agent 探索项目自动生成）；每条消息即时 JSONL 存档，`/resume` 弹出交互式会话选择面板（上下键/回车/Esc，锁定会话与当前会话置灰跳过），载入后聊天区清空并回放全部历史、后续消息追加进该会话，`rhine --continue` 启动恢复同样回放；Agent Loop 自然停止后异步生成四类笔记（用户偏好/纠正反馈/项目知识/参考资料），索引注入系统提示、正文按需读取；多实例锁文件防护；`/memory` 查看全部状态。RHINE.md 与会话存档/恢复对所有 Provider 生效，自动笔记与 `/init` 仅 DeepSeek 工具模式。
- **上下文管理（两层压缩）**：每轮请求前近似估算历史用量（锚点+增量）；第一层把过大的工具结果存盘、历史只留预览与路径；第二层在逼近窗口时调 LLM 把较早消息压成五段式结构化摘要、近期原文保留，并提示模型细节需重读文件；连续失败 3 次熔断，用户原文永不改写；`/context` 查用量、`/compact` 手动压缩，状态栏常驻用量指示。窗口大小由 `context_window` 配置（默认 65536）。
- **五层防御权限系统**：每个工具执行前由 `permission/` 包的纯逻辑引擎按固定顺序计算决定——①危险命令黑名单（不可被任何配置/模式放开）→ ②路径沙箱 → ③可配置规则（`Tool(模式)`，deny 永远优先）→ ④权限模式（严格/默认/放行，`/perm` 切换）→ ⑤人在回路（确认面板四选项）。
- **可配置权限规则**：三层 YAML（用户级、项目级、本地级）声明 allow/deny；跨层合并后 deny 优先求值。
- **Plan Mode**：`/plan` 开启后先只允许只读调研和需求澄清，完整计划进入聊天记录，经用户批准后才进入执行阶段。与权限模式正交。
- **人在回路确认**：判定为「问用户」时弹出内联确认面板，四选项——本次放行 / 本会话放行 / 永久放行 / 拒绝；计划获批不等于免确认。
- **明确停止原因**：支持自然完成、迭代上限、用户取消、计划拒绝、连续未知工具、流错误等停止路径。
- **Textual TUI**：历史区（含会话历史回放）、命令提示、工具行、彩色 diff、确认/澄清/会话选择面板、输入框和状态栏组合成终端界面。
- **结构化系统提示**：全局提示按身份、约束、任务模式、工具使用等模块拼装，稳定内容走可缓存通道，环境信息与 Plan Mode 提醒走 `<system-reminder>` 动态注入。

> 工具调用、Plan Mode 与权限系统目前仅在 `protocol: deepseek` 且启用默认工具注册中心时可用；记忆系统的 RHINE.md 注入与会话存档/恢复对所有 Provider 生效。

## 安装

要求：Python 3.11+

```bash
git clone <repo-url>
cd RhineCode-Agent
pip install -e .
```

## 配置

安装后**首次运行 `rhine`**会在 `~/.rhinecode/` 下自动生成三份配置模板，并提示你填入真实 `api_key`：

```bash
rhine
# → 已在 ~/.rhinecode 生成配置模板（config.yaml / permissions.yaml / mcp.yaml），请在 config.yaml 填入真实 api_key 后重新运行 rhine。
```

- `config.yaml`：主配置（**必需**），含 `api_key`。填好后即可在**任意目录**运行 `rhine`，不必再 `cd` 回源码目录、也不必每次带 `--config`（若仍是占位符 `YOUR_API_KEY`，会被拦下并提示）。
- `permissions.yaml` / `mcp.yaml`：可选增强，生成的模板**内容全是注释、默认不生效**（等价于无文件，行为不变）；想用时取消注释即可，无需从示例文件复制。

这三份是**用户级全局配置**，在任意目录运行 `rhine` 都会读到。

你也可以手动从示例文件复制一份到项目内使用，并用 `--config` 显式指定：

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
| `context_window` | 上下文窗口上限（token），作为「历史是否逼近溢出、何时压缩」的判断基准；缺省 / 非法都回退默认 65536。只影响 RhineCode 的压缩时机，不改变模型真实上限，应贴近所用模型的实际上下文长度。首次生成的模板不含此项，想调整手动加一行即可 |

## 启动

安装后在**任意目录**直接运行（读 `~/.rhinecode/config.yaml`，当前目录即 AI 操作的项目根）：

```bash
rhine
```

要接着上一次的对话继续，加 `--continue`（恢复最近一个未被其它实例占用的会话，并把历史回放到聊天区）：

```bash
rhine --continue
```

需要临时使用其它配置文件时用 `--config` 覆盖：

```bash
rhine --config config.yaml
```

未安装或开发调试时，也可用等价的模块入口（需在源码目录）：

```bash
python -m rhinecode --config config.yaml
```

## 斜杠命令

C10 起所有斜杠命令由**单一命令注册中心**统一管理：执行、`/help` 帮助、自动补全共享同一份登记信息；命令名与别名**大小写不敏感**（`/ReSuMe 3` 等价 `/resume 3`，参数原样保留）；未知命令（如 `/plna`）不会被发送给 AI，只在本地提示「未知命令，输入 `/help` 查看可用命令」。

| 命令 | 别名 | 类型 | 说明 |
|------|------|------|------|
| `/help` | `/h` | 本地 | 按稳定顺序列出全部命令的名称、别名、描述、用法与参数提示 |
| `/think` | — | 界面 | 在 off / high / max 间循环切换思考模式（Anthropic / DeepSeek 生效） |
| `/plan` | — | 界面 | 切换 Plan Mode：先规划、澄清和审批，再执行（DeepSeek 工具模式生效） |
| `/perm` | `/permissions`、`/allowed-tools` | 界面 | 在 默认 / 严格 / 放行 间循环切换权限模式，只影响「规则未命中」的灰色地带兜底（DeepSeek 工具模式生效） |
| `/mcp` | — | 本地 | 查看各 MCP Server 的连接状态、传输类型、注册工具数与失败原因 |
| `/context` | `/ctx` | 本地 | 查看当前上下文近似用量（估算 token / 窗口上限 / 余量 / 已存盘结果数 / 是否熔断），只读（DeepSeek 工具模式生效） |
| `/compact` | — | 本地 | 手动触发第二层 LLM 摘要压缩，无余量阈值；无够旧早段时回「无可摘要的早段」（DeepSeek 工具模式生效） |
| `/resume` | `/continue` | 界面 | 无参弹出交互式会话选择面板（上下键选择、回车载入、Esc 退出，锁定/当前会话置灰跳过）；`/resume <编号或ID>` 直接载入。载入后聊天区清空并回放该会话全部历史，后续消息追加进该会话（所有 Provider 生效） |
| `/memory` | — | 本地 | 查看记忆系统状态：RHINE.md 各层加载、两级笔记数量与索引、最近笔记更新结果、当前会话与写锁状态，只读（所有 Provider 生效） |
| `/init` | — | 提示词 | 让 Agent 探索项目并生成项目根 `RHINE.md`；已存在时不覆盖、只输出改进建议，写盘走完整权限管线（DeepSeek 工具模式生效）。界面与恢复回放只显示 `/init`，模型收到完整内置提示词 |
| `/clear` | `/reset`、`/new` | 界面 | 清空当前对话历史（并复位上下文压缩状态；会话存档开新档、旧档保留） |
| `/exit` | `/quit` | 界面 | 退出程序 |

三种类型的含义：**本地**命令直接执行固定逻辑、不进入 Agent Loop（`/compact` 的专用摘要调用是明确例外）；**界面**命令改变会话或界面状态、同样不进入 Agent；**提示词**命令把内置预设提示词作为用户请求交给 AI。本地与界面命令不消耗对话 Token、不写入模型历史。无参数命令会忽略多余参数（`/clear now` 仍执行清空）。

补全与高亮：

- 输入 `/` 前缀实时弹出候选菜单，规范名与别名都参与匹配（别名候选会标注其规范命令）；隐藏命令不出现在帮助或补全中，但直接输入完整名称仍可执行。
- **Tab 补全**：唯一匹配时直接补全命令字段（如 `/compa` → `/compact`）；多个匹配时展示稳定排序的候选菜单。光标已进入参数区时 Tab 不改动参数。
- **菜单回车**：候选菜单可见时按回车执行当前高亮项，即使只输入了部分前缀（如 `/co` 回车执行高亮的命令）。
- **命令字段高亮**：输入框中完整命中规范名或别名的命令字段以青色加粗显示（`/resume 3` 只高亮 `/resume`）；未完成的前缀（`/res`）与正文中的斜杠不高亮。

状态栏以 `[DEFAULT]`（中性弱化色）/ `[PLAN]`（醒目青色加粗）标记当前模式，取代旧的「计划模式：开/关」文字；执行 `/plan` 后立即刷新。

运行中按 `Esc` 会请求取消当前 Agent Loop；如果正在等待确认或澄清，则由当前面板处理取消。

## 工具能力

DeepSeek 工具模式会向模型暴露以下工具：

| 工具 | 说明 | 只读 |
|------|------|------|
| `read_file` | 读取项目内文本文件，并带行号返回内容；超过 1 MiB 的文件必须用 `start_line` / `max_lines` 分段读取，单次最多 2000 行 | 是 |
| `glob_files` | 按 glob 模式查找项目内文件；命中 `Read(...)` deny 的文件会被跳过并显示跳过数量 | 是 |
| `grep_content` | 在项目内文本文件中按正则搜索内容；命中 `Read(...)` deny 的文件不会被打开，结果会显示跳过数量 | 是 |
| `write_file` | 新建或覆盖项目内文件，并生成 diff | 否 |
| `edit_file` | 用唯一匹配的原文片段精确替换；支持 `edits` 数组批量替换 | 否 |
| `run_command` | 在项目工作目录下执行 shell 命令 | 否 |
| `mcp_resolve_server` | 解析用户给出的 MCP 名称、NPM 包名或 HTTP URL，返回候选配置、来源、置信度和风险提示 | 是 |
| `mcp_add_server` | 将已解析的 MCP 配置写入用户级或项目级 `mcp.yaml`，并只重载目标 Server | 否 |

只读工具经权限引擎放行后可并发执行；有副作用工具串行执行，是否需要确认由权限系统的五层决策管线决定（命中 allow 规则免确认、命中 deny 规则直接拒绝、灰色地带按权限模式兜底）。当决策为「问用户」时弹出确认面板，四选项：本次放行 / 本会话放行 / 永久放行 / 拒绝。

## MCP 客户端

RhineCode 可作为 [MCP](https://modelcontextprotocol.io) 客户端接入外部 MCP Server，把它们提供的工具接进工具中心，无需改动源码。启动时自动完成「连接 → `initialize` 握手 → `tools/list` 发现 → 注册」；之后远端工具与内置工具走同一套 Agent Loop 与权限管线。

- **两种传输**：本地子进程走 stdio 管道，远程走 Streamable HTTP。底层按 JSON-RPC 2.0 收发，请求带 id、响应按 id 配对（stdio 单管道复用靠后台 reader 线程派发）；stdio 的 stderr 会被后台线程持续 drain，避免 Server 大量写错误日志时堵塞握手或工具发现。
- **命名与隔离**：远端工具注册名以 `mcp__<server>__<tool>` 为基础；若远端名字含空格、斜杠等不适合作为 function name 的字符，会规范化为安全名称，`/mcp` 明细会展示 `registered_name <- server/tool`。
- **连接生命周期**：多 Server 连接缓存、单点隔离（某 Server 连接/发现失败只跳过并记录，不影响其它 Server 与启动）；程序退出时统一关闭连接、回收 stdio 子进程。
- **安全默认**：MCP 工具一律视为非只读，默认权限模式下每次调用都经人在回路确认；可用权限规则 `allow: mcp__<server>__*` 一次放行整个 Server（见下）。若工具名被规范化，请以 `/mcp` 显示的注册名写规则。
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

## 记忆系统

让 RhineCode 跨会话「记得住」：项目怎么规范、上次聊到哪、用户有什么偏好。三套机制按依赖分级生效——**RHINE.md 项目指令**与**会话存档/恢复**对所有 Provider 生效，**自动笔记**与 `/init` 仅 DeepSeek 工具模式。

### RHINE.md 项目指令

类似 Claude Code 的 CLAUDE.md：把项目规范写进 RHINE.md，每次对话自动注入系统提示。三层加载、越具体越靠后（利用近因效应）：

| 位置 | 层级 |
|------|------|
| `~/.rhinecode/RHINE.md` | 用户级（跨项目通用偏好） |
| `<项目根>/.rhinecode/RHINE.md` | 项目级 |
| `<项目根>/RHINE.md` | 项目根（最常用，随仓库提交） |

支持 `@相对路径` include 原地展开（相对引用文件所在目录，嵌套上限 4 层、自动防循环，围栏代码块内的 `@` 不展开）。`/init` 可让 Agent 探索项目自动生成项目根 RHINE.md——已存在时不覆盖、只输出改进建议，写盘走完整权限管线。

### 会话存档与恢复

- **即时存档**：每条消息（用户/AI/工具结果）立即以一行 JSON 追加写入 `<项目根>/.rhinecode/sessions/<会话ID>.jsonl`——只追加不回写，崩溃最多丢最后一行；记录的是上下文压缩**前**的原始消息流。`/clear` 开新档，旧档保留；超过 30 天的存档启动时静默清理。
- **`/resume` 交互式恢复**：无参弹出会话选择面板，列出全部会话（编号/ID/时间/消息数/标题），上下键选择、回车载入、Esc 退出；被其它实例占用的会话（🔒）与当前会话置灰、导航自动跳过。载入后聊天区清空并**回放该会话的全部历史**（用户消息、AI 回复、工具调用行），后续新消息追加进该会话——相当于完整切换 session。`/resume <编号或ID>` 直接载入同样回放。`rhine --continue` 启动时恢复最近未锁定会话，同样回放。
- **容错载入**：坏行跳过、不成对的工具调用消息成组丢弃；恢复后若历史已逼近上下文窗口先压缩一次；距上次对话超过 24 小时会给模型注入一次性时间跨度提醒。

### 自动笔记

Agent Loop 自然完成后，后台异步调一次 LLM 审视本轮新增对话，把值得长期记住的内容沉淀为四类笔记：**用户偏好 / 纠正反馈 / 项目知识 / 参考资料**。用户相关存 `~/.rhinecode/memory/`、项目相关存 `<项目根>/.rhinecode/memory/`，每目录一份 `MEMORY.md` 索引。索引每次注入系统提示（截断 200 行 / 25KB），笔记正文由模型按需 `read_file` 读取（用户级目录经只读白名单放行）。安全设计：笔记 LLM 请求禁用全部工具、只产出 JSON 动作，文件名过 `[a-z0-9_-]+\.md` 白名单校验（防路径注入），写盘由程序在锁临界区内完成。

### 多实例并发防护

同机开多个 RhineCode 实例时靠锁文件互斥：`O_CREAT|O_EXCL` 原子创建、拿不到锁立即退让（笔记跳过本轮 / 会话拒绝载入）、锁文件 mtime 超过 10 分钟视为崩溃残留自动清除。会话锁靠追加消息时 touch + TUI 每 2 分钟心跳保鲜。锁只保证「写不坏」，不提供跨实例实时一致性（语义重复的笔记靠 LLM 去重收敛）。

`/memory` 随时查看全部状态：RHINE.md 各层加载与 include 展开、两级笔记数量与索引、最近一次笔记更新结果、当前会话与写锁状态。

> `.rhinecode/sessions/`、`.rhinecode/memory/`、`.rhinecode/context/` 含对话原文与工具输出，已加入 `.gitignore`，勿提交。

## 上下文管理

RhineCode 用两层压缩，让对话累积再多也不会因超出上下文窗口而瘫掉。仅在 DeepSeek 工具模式生效，由 Agent Loop 在**每轮 API 请求前**单点触发：先跑第一层（管单条消息大小），再按估算决定是否第二层（管累积历史长度）。用户的原始消息始终原文保留，不被摘要改写。

### 近似估算

不引入精确 tokenizer，用「**锚点 + 增量**」估算当前用量：

- **锚点**：上一次 API 返回的 `usage.prompt_tokens` 是 API 亲口给的**精确值**，覆盖上次发送过的那段历史。
- **增量**：只有锚点之后新增的少量消息按字符数粗估（`字符数 ÷ 3.0 + 每条 4 token 框架开销`）。

误差被限制在「增量」小段上，比值取偏小以倾向**高估**——宁可压缩提前发生（安全），也不低估导致真溢出（危险）。

### 第一层 · 工具结果存盘（零成本）

Token 大头是工具结果。这层不调模型，把过大的工具结果写到磁盘、历史里只留占位：

- **单结果**：任一工具结果估算 > 4000 token → 存盘。
- **聚合**：剩余工具结果合计 > 16000 token → 按体积从大到小依次存盘，直到降回阈值以下。

存盘到 `<项目根>/.rhinecode/context/<tool_call_id>.txt`，历史里替换为「体量说明 + 前 500 字预览 + 文件路径 + 重读提示」。只作用于工具结果消息，绝不触碰用户消息；以 `tool_call_id` 为键幂等；写盘失败则保留原文、跳过（fail-safe）。

### 第二层 · LLM 结构化摘要（兜底）

当估算逼近窗口（自动触发留 13K 安全余量）时，调一次 LLM 把较早的消息压成摘要、近期原文保留：

- **保留边界**：从尾部保留约 10000 token 或至少 5 条，并回退到最近的 `user` 消息——保证不切碎消息、不拆散 `assistant(工具调用)↔工具结果` 配对，重构后历史对 API 合法。
- **摘要 Prompt**：禁止模型调用任何工具；要求先写分析草稿、再输出分隔标记后写正式摘要（草稿丢弃）；正式摘要按五部分固定组织——①任务目标 ②已完成步骤与结论 ③关键文件与改动 ④当前状态与待办 ⑤重要约束与决策。
- **重构**：新历史 = `[结构化摘要, 边界提示, 近期原文]`。边界提示告诉模型「要具体代码细节会重新读取文件，绝不照摘要臆测」，防止照着摘要脑补代码。
- **熔断**：摘要连续失败 3 次即熔断，自动路径不再尝试，避免死循环；任一次成功清零，`/clear` 复位。

### 手动与可观测

- `/compact`：手动触发第二层摘要，**无余量阈值**——用户主动触发即尝试；历史尚无够旧的早段可摘要时如实回「无可摘要的早段」（这是「确实没得压」，非拒绝）。手动路径只做第二层摘要，第一层存盘由自动路径承担。
- `/context`：只读报告，列出估算 token、窗口上限、余量、已存盘结果数、是否熔断。
- **状态栏**：底部常驻「上下文：19% · 12.3K/64K」，随每轮对话自动刷新；用量达窗口 80% 或已熔断时橘色高亮预警。

### 配置

窗口大小由 `config.yaml` 的可选字段 `context_window` 决定（默认 65536，缺省/非法回退默认）。其余阈值（存盘 4K/16K、保留 10K、余量 13K 等）为模块内常量，当前不可配（YAGNI）。

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
- `Read(...)` deny 会同时约束直接读取和间接发现：例如 `deny: Read(config.yaml)` 会阻止 `read_file config.yaml`，也会让 `grep_content path="."` 跳过该文件、让 `glob_files "**/*"` 不输出该文件。
- MCP 远端工具未做细粒度映射，落到「整工具规则」的 `other` 分支：规则直接写注册后的工具名，且支持 `fnmatch` 通配——`allow: mcp__everything__echo` 精确放行单个工具，`allow: mcp__everything__*` 一次放行整个 Server（无 `*` 时等价精确匹配，向后兼容普通工具名）；如果远端名被规范化，以 `/mcp` 里展示的 `registered_name` 为准。
- 三层合并后按 **deny 永远优先**求值（不按层级覆盖）：任一条 deny 命中即拒绝，deny 不可被 allow 翻案；没有 deny 命中、有 allow 命中则放行；都没命中交给权限模式兜底。
- 「永久放行」写入本地级配置前会先解析已有 `permissions.local.yaml`；如果文件损坏或顶层不是映射，RhineCode 不会覆盖原文件，而是保留本次会话放行并报告/记录写入失败。

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
- `config.yaml` 可能包含真实 API Key，请勿提交到版本库；可用 `deny Read(config.yaml)` 规则进一步阻止模型读取，并阻止 grep/glob 间接泄露该文件内容或路径。
- MCP 远端 Server 是外部程序、不可信：MCP 工具一律视为非只读，默认模式下每次调用都经人在回路确认；stdio 子进程的行为不受路径沙箱约束（与 `run_command` 同属已知边界）；`mcp.yaml` 的 `env`/`headers` 可能含密钥，勿提交真实值。
- 上下文管理的存盘文件（`.rhinecode/context/`）与会话存档（`.rhinecode/sessions/`）都含工具结果和对话原文（可能包括被读过的敏感文件片段），已加入 `.gitignore`，勿提交。
- 记忆系统的笔记写盘是内部可信写盘（不经工具权限管线），但写入路径由代码锁死：LLM 只产出 JSON 动作，文件名过白名单校验，物理上出不了两个 memory 目录；笔记与摘要 LLM 请求均强制禁用工具。

## 项目结构

```text
rhinecode/
├── __main__.py          # CLI 入口
├── config.py            # YAML 配置加载与校验
├── conversation.py      # 对话管理、斜杠命令、权限引擎构建、Agent 回调封装、上下文/记忆接线
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
│   ├── auto_config.py   # MCP 名称/URL 自动解析与 mcp.yaml 安全写入
│   ├── jsonrpc.py       # JSON-RPC 2.0 构造/解析、id 生成、错误类型（纯数据）
│   ├── transport.py     # Transport 抽象 + StdioTransport + HttpTransport
│   ├── client.py        # MCPClient：initialize / tools/list / tools/call
│   ├── tool_adapter.py  # MCPTool（Tool 子类）+ CallToolResult→ToolResult 转换
│   └── manager.py       # MCPManager：多 Server 连接缓存/隔离/生命周期/状态
├── memory/              # 记忆系统（c9，纯逻辑 + 单点接入）
│   ├── lockfile.py      # 锁原语：原子创建 / 释放 / 心跳 / 过期判定
│   ├── instructions.py  # RHINE.md 三层加载 + @include 展开（纯函数）
│   ├── session.py       # SessionStore：JSONL 存档追加 / 扫描 / 容错载入 / 会话锁
│   ├── notes.py         # 笔记 frontmatter 解析 / 渲染 / 索引重建与截断
│   ├── note_updater.py  # 笔记 LLM 的 Prompt 与 JSON 响应解析（文件名白名单）
│   └── manager.py       # MemoryManager：启动 / 注入 / 存档 / 异步笔记 / resume 编排
├── context/             # 上下文两层压缩（c8，纯逻辑 + 单点接入）
│   ├── models.py        # CompactionNotice / ContextStats 数据类
│   ├── estimate.py      # 近似估算纯函数（锚点 + 增量）
│   ├── offload.py       # Offloader：第一层工具结果存盘（单结果/聚合、幂等）
│   ├── summarize.py     # 第二层纯逻辑：保留边界/转录/摘要 Prompt/解析/重构
│   └── manager.py       # ContextManager：编排两层压缩、锚点、熔断、可观测
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
│   ├── mcp_config.py    # mcp_resolve_server / mcp_add_server 内置工具
│   ├── path_guard.py    # 项目工作目录路径守卫（沙箱层复用）
│   ├── read_file.py
│   ├── write_file.py
│   ├── edit_file.py
│   ├── run_command.py
│   ├── glob_files.py
│   └── grep_content.py
└── tui/
    ├── app.py           # Textual App 主类
    └── widgets.py       # HistoryView（含历史回放）/ InputBar / StatusBar / 确认·澄清·会话选择面板
```

## 测试

```bash
python -m compileall rhinecode tests
python -m unittest discover -s tests
```

当前测试覆盖路径越界防护、四态确认回调、本会话放行登记规则、Plan Mode 完整计划展示、拒绝计划停止、计划获批后仍逐项确认，以及权限系统的命令/路径匹配、危险命令黑名单（含复合命令逐段与 fork 炸弹）、deny 优先求值、配置三层加载与容错、工具规范化映射、四层决策管线、loop 决策接入（被拒不停循环、allow 规则免确认）、`grep_content` / `glob_files` 遵守 `Read(...)` deny、大文件范围读取、损坏本地权限配置不被覆盖等关键行为（`tests/test_perm_*.py`、`tests/test_review_fixes.py`）。

MCP 客户端部分覆盖两层配置合并与 `${VAR}` 展开、JSON-RPC 消息构造与响应分类、stdio 传输三步会话与按 id 配对（用内置模拟 Server 端到端）、stderr drain 防阻塞、非法远端工具名规范化且仍调用原始远端名、`CallToolResult→ToolResult` 转换（含 `isError` 与非文本占位）、单 Server 失败隔离与工具注册、运行时单 Server 重载、自动解析/写入 MCP 配置、Windows `npx.cmd` 兼容，以及 `other` 分支 fnmatch 通配放行（`tests/test_mcp_*.py`、`tests/test_mcp_auto_config.py`、`tests/test_perm_other_glob.py`）。

上下文管理部分覆盖近似估算（无锚点全量 / 有锚点=锚点+增量 / 越界兜底）、第一层存盘（单结果 / 聚合挑大先存 / 用户消息不动 / 幂等 / 写盘失败保留原文）、第二层纯逻辑（保留边界回退到 user 不拆工具对 / 草稿丢弃 / 重构结构与角色交替 / 转录渲染）、编排（摘要成功重构并失效锚点 / 连续失败熔断与复位 / `manual_compact` 无阈值 / 存盘先行降估算 / 状态栏摘要格式与高亮）（`tests/test_context_*.py`）。真实 LLM 摘要与 TUI 渲染的端到端场景留作手测。

记忆系统部分覆盖锁原语（原子互斥 / 释放重取 / 过期接管 / touch 保鲜）、RHINE.md 三层加载与 @include 展开（嵌套上限 / 防环 / 越界拦截 / 围栏代码块保留）、笔记纯逻辑（frontmatter 往返 / 索引截断）、会话存档（惰性建档 / 容错载入丢组 / 列表与锁标记 / 过期清理）、MemoryManager 编排（笔记请求禁用工具 / 锁被占跳过 / 高水位增量 / --continue 顺延被锁会话）、`/resume` 交互化（结构化列表与编号缓存 / `build_replay_items` 回放转换 / 无参返回面板信号 / 载入成功事件流携带历史快照、失败不清屏）（`tests/test_memory_*.py`、`tests/test_resume_replay.py`）。面板交互与回放渲染的视觉效果留作 TUI 手测。

## 当前阶段文档

C9 的规格、实现计划、任务拆解和验收清单位于：

- `docs/c9/spec.md`
- `docs/c9/plan.md`
- `docs/c9/task.md`
- `docs/c9/checklist.md`

这些文档描述记忆系统的需求、架构、任务与验收（RHINE.md 三层项目指令与 @include、JSONL 会话存档与容错恢复、四类自动笔记与索引注入、锁文件并发防护、`/resume`·`/memory`·`/init`·`--continue`）。C8（上下文管理）、C7（MCP 客户端）、C6（五层防御权限系统）、C5（结构化系统提示与缓存策略）、C4（Agent Loop 与 Plan Mode）文档仍保留，用于追溯设计来源。

## 后续补齐项

以下问题已在工程审查中确认，但不属于当前阶段开发范围，后续章节再统一设计和实现：

1. API Key 与敏感配置的读取脱敏、环境变量化或工作区外管理。
2. Plan Mode 规划阶段的工具阶段强校验，防止模型同轮夹带副作用工具。
3. `write_file` / `edit_file` 的文件系统级原子写入。
4. OS 级沙箱（Seatbelt / bubblewrap），约束 `run_command` 子进程自身发起的文件/网络访问。
5. 权限系统后续项：网络请求限制、资源配额、审计日志。
6. 开发环境依赖固定与 CI，让 `compileall` / `unittest` 在标准环境稳定运行。
7. MCP 后续项：Server 健康检查与自动重连、资源 / 提示词 / 采样等非工具能力、MCP 工具的细粒度权限映射与执行超时可配置化。
8. 上下文管理后续项：精确 tokenizer（当前仅近似估算）、摘要策略的质量/机器学习优化、存盘文件的清理与生命周期、除窗口大小外其它阈值的可配置化、跨会话摘要持久化。
9. 记忆系统后续项：向量数据库/RAG 语义检索、团队记忆同步/跨机器共享、跨实例实时一致性、笔记自动清理与遗忘机制、各阈值可配置化、存档格式版本迁移与加密存储。

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
