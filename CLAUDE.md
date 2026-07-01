# RhineCode

RhineCode 是一个用 Python + Textual 实现的终端 AI 编程助手，交互体验参考 Claude Code。

当前版本以 DeepSeek Provider 为主实现了 C7 阶段能力：在 C6 五层防御权限系统、C5 结构化系统提示与 C4 Agent Loop 基础上，加入一个 **MCP 客户端**——启动时按两层配置连接外部 MCP Server（本地子进程走 stdio、远程走 Streamable HTTP），发现其工具并包装成已有的 `Tool` 接口注册进工具中心，对 Agent Loop / 权限系统 / TUI 完全无感。其下每个工具执行前仍由代码（而非模型/prompt）计算「放行 / 拒绝 / 问用户」，被拒不终止循环、把结构化原因回灌模型。模型可以在一次用户请求中循环读取项目、搜索代码、执行工具（含 MCP 远端工具）、回灌结果并继续下一轮，直到自然完成或命中停止条件。Anthropic / OpenAI Provider 目前保持纯对话能力。

## 语言
中文回答

## 技术栈

- Python 3.11+
- [Textual](https://textual.textualize.io/) — TUI 框架（流式渲染基于 Worker + `call_from_thread`）
- `anthropic` / `openai` SDK，`pyyaml` 配置
- `httpx` — MCP Streamable HTTP 传输的 SSE 流式读取（c7）
- 依赖与入口定义在 `pyproject.toml`，控制台脚本 `rhinecode`

## 当前能力

- **ReAct Agent Loop**：自动执行“调用模型 → 执行工具 → 回灌结果 → 再调用模型”的多轮循环。
- **流式输出**：正文与思考内容逐块渲染，后台 Worker 不阻塞 TUI 主线程。
- **DeepSeek 工具系统**：支持读文件、glob 找文件、grep 搜内容、写文件、精确编辑文件、运行命令。
- **MCP 客户端**（c7）：启动时按两层 `mcp.yaml` 连接外部 MCP Server（stdio / Streamable HTTP），走 JSON-RPC 2.0（请求带 id、响应按 id 配对）完成 `initialize → tools/list → tools/call` 三步，把远端工具包装成 `mcp__<server>__<tool>` 注册进工具中心；多 Server 连接缓存与隔离（单个失败不影响其它），退出统一回收；MCP 工具一律非只读默认走确认，`/mcp` 命令与状态栏展示连接状态。仅接工具能力，不做资源/提示词/采样与健康检查/自动重连。
- **Plan Mode**：`/plan` 开启后先只允许只读调研和需求澄清，完整计划进入聊天记录，经用户批准后才进入执行阶段。与权限模式正交。
- **五层防御权限系统**：每个工具执行前由 `permission/` 包的纯逻辑引擎按固定顺序计算决定——①危险命令黑名单（不可被任何配置/模式放开）→②路径沙箱→③可配置规则（`Tool(模式)`，deny 永远优先）→④权限模式（严格/默认/放行，`/perm` 切换）→⑤人在回路（确认面板四选项：本次/本会话/永久/拒绝）。被拒不终止循环，结构化原因回灌模型让其调整策略。
- **可配置权限规则**：三层 YAML（用户级 `~/.rhinecode/`、项目级 `<根>/.rhinecode/`、本地级 `*.local.yaml` 不提交）声明 allow/deny；跨层合并后 deny 优先求值。
- **明确停止原因**：支持自然完成、迭代上限、用户取消、计划拒绝、连续未知工具、流错误等停止路径。
- **结构化系统提示**：七个固定提示模块走稳定可缓存通道，环境信息与 Plan Mode 提醒通过 `<system-reminder>` 作为动态补充注入。

工具调用、Plan Mode 与权限系统目前仅在 `protocol: deepseek` 且启用默认工具注册中心时可用；MCP 工具随内置工具一同仅在该模式下暴露。

## 架构

当前核心分层如下，上层尽量不感知下层具体实现，通过抽象接口和事件流解耦：

- **TUI 层**（`rhinecode/tui/`）— `app.py` 是 Textual App 主类，用 Worker 消费 AgentEvent 并逐块渲染；`widgets.py` 提供 HistoryView / InputBar / StatusBar / 工具行 / diff / 确认和澄清面板。
- **协调层**（`rhinecode/conversation.py`）— `ConversationManager` 是 TUI 与 Agent / Provider 之间的中转点，维护对话历史、解析斜杠命令（含 `/perm` 切换权限模式、`/mcp` 查看 MCP 状态）、管理思考模式和 Plan Mode、构建权限引擎并封装 ask（四态人工确认）/澄清/计划审批回调；持有 `MCPManager` 引用仅用于 `/mcp` 与状态栏（`mcp_status_line`），MCP 工具本身已注册进 registry、与此引用解耦。
- **Agent 层**（`rhinecode/agent/`）— `loop.py` 实现 ReAct 循环，并在 `_execute` 单点接入权限决策预扫；`events.py` 定义 AgentEvent、停止原因和四态确认决策；`collector.py` 收集流式正文、思考与工具调用；`plan_tools.py` 支撑 Plan Mode 特殊工具；`prompt/` 负责结构化系统提示、环境信息与动态 reminder。
- **Permission 层**（`rhinecode/permission/`）— 五层防御权限系统，纯逻辑、与 TUI/Provider 解耦：`models.py` 数据结构与枚举；`matching.py` 命令/路径匹配与命令拆分；`blacklist.py` 危险命令黑名单；`rules.py` deny 优先求值；`config.py` 三层 YAML 加载/容错/回写；`adapter.py` 把工具调用规范化为权限请求（收口工具知识）；`engine.py` 的 `PermissionEngine.decide` 组装四层管线。
- **MCP 层**（`rhinecode/mcp/`）— MCP 客户端，五层从下到上、下层不感知上层：`config.py` 两层 `mcp.yaml` 加载/`${VAR}` 展开/容错；`jsonrpc.py` JSON-RPC 2.0 消息构造/分类/id 生成（纯数据，无 I/O）；`transport.py` `Transport` 抽象 + `StdioTransport`（子进程 + 后台 reader 线程按 id 派发）+ `HttpTransport`（Streamable HTTP + SSE，同步阻塞）；`client.py` `MCPClient` 封装 `initialize/list_tools/call_tool` 三步；`tool_adapter.py` `MCPTool(Tool)` + `CallToolResult→ToolResult` 转换；`manager.py` `MCPManager` 编排多 Server 的连接缓存/单点隔离/生命周期/状态汇总。同步线程模型（不引入 asyncio）以契合现有 Textual Worker 同步执行模型。在 `__main__.py` 启动时 `connect_all` 注册工具、`finally` 里 `close_all` 回收。
- **Provider 层**（`rhinecode/provider/`）— `base.py` 定义 `BaseProvider`/`Message`/`StreamChunk` 抽象；`anthropic.py`、`openai.py`、`deepseek.py` 为具体实现；`factory.py` 的 `create_provider` 按 `protocol` 分发。
- **Tools 层**（`rhinecode/tools/`）— `base.py` 定义 Tool / ToolResult 抽象；`registry.py` 注册默认工具；`path_guard.py` 负责路径边界（`resolve_in_workspace` 抛异常版、`is_within_workspace` 布尔版供权限引擎②沙箱层复用）；`read_file.py`、`write_file.py`、`edit_file.py`、`run_command.py`、`glob_files.py`、`grep_content.py` 是当前 6 个核心工具。

新增 Provider：在 `provider/` 下继承 `BaseProvider` 实现 `stream_chat`，再到 `factory.py` 添加 `elif` 分支，配置中 `protocol` 改为新值即可。

如果新 Provider 要支持工具调用，需要参考 `deepseek.py`：

- 把 `tools` schema 传给模型 API。
- 从流式响应中拼接工具调用参数。
- 产出 `StreamChunk(type="tool_call")`。
- 能序列化历史中的 `assistant(tool_calls)` 与 `role="tool"` 消息。

新增工具：在 `tools/` 下继承 `Tool`，声明 `name`、`description`、`parameters`、`read_only`，实现 `execute`，再到 `ToolRegistry.default()` 注册。文件类工具必须复用 `path_guard.py` 的路径边界校验。若新工具要纳入细粒度权限控制（映射到 Bash/Read/Edit/Write 规则名与对应 specifier），在 `permission/adapter.py` 的 `_TOOL_MAP` 加一行映射即可；未映射的工具自动落到 `other` 分支（仅按工具名匹配整工具规则 + 走权限模式兜底），不会漏过权限检查。`other` 分支的工具名用 `fnmatch` 通配匹配（c7 决策 A）：无 `*` 时等价精确匹配（向后兼容），故 MCP 工具可用 `allow: mcp__<server>__*` 一次放行整个 Server。

新增 MCP Server：无需改代码，用户在 `~/.rhinecode/mcp.yaml` 或 `<项目根>/.rhinecode/mcp.yaml` 声明即可（stdio 填 `command/args/env`，http 填 `url/headers`，值支持 `${VAR}`）。若要新增**传输方式**（stdio/HTTP 之外），在 `mcp/transport.py` 继承 `Transport` 实现 `start/request/notify/close`，再到 `manager.py` 的 `_build_transport` 加分支即可，`client.py` 以上无感。

新增斜杠命令：必须**成对维护**两处，缺一会出现「命令能用但补全列表看不到」或反之——① 在 `conversation.py` 的 `handle_input` 加分支实现命令逻辑；② 在 `tui/widgets.py` 的 `CommandPanel.COMMANDS` 注册表追加 `(命令文本, 简要描述)`，输入 `/` 才会在补全面板列出。若命令带选项面板/回调（如确认四态），还需同步 `tui/app.py` 的事件处理与回调注入。

> 「成对维护点」备忘（改一处常需同步另一处，避免遗漏）：
> - 新增工具 → `tools/registry.py`（注册）+ `permission/adapter.py`（权限映射，按需）
> - 新增 MCP 传输方式 → `mcp/transport.py`（`Transport` 子类）+ `mcp/manager.py` `_build_transport`（按 `kind` 分支）
> - 新增斜杠命令 → `conversation.py`（逻辑）+ `tui/widgets.py` `CommandPanel.COMMANDS`（补全列表）+（若改了状态栏可见状态）`tui/app.py` 提交处理里 `if text in (...)` 的状态栏刷新白名单
> - 新增状态栏展示字段 → `tui/widgets.py` `StatusBar.update_status`（渲染）+ `tui/app.py` `_refresh_status`（取值传入）+ 触发刷新的命令需在上面那个白名单里
> - 新增确认/交互态 → `agent/events.py`（枚举）+ `tui/widgets.py`（面板选项 id）+ `tui/app.py`（id→枚举映射）+ `conversation.py`（回调闭包处理）
> - 状态栏/历史区文本含字面 `[`（如 `[provider]`）→ 必须转义为 `\[`，否则被 Textual markup 当标签吞掉

## 常用命令

```bash
pip install -e .                          # 安装（开发模式）
cp config.example.yaml config.yaml        # 创建配置后填入真实 api_key
python -m rhinecode --config config.yaml  # 启动
rhinecode --config config.yaml            # 安装后也可以用控制台脚本启动
```

运行时斜杠命令：

- `/think`：在 off / high / max 间循环切换思考模式（Anthropic / DeepSeek 生效）。
- `/plan`：切换 Plan Mode，先规划、澄清和审批，再执行（DeepSeek 工具模式生效）。
- `/perm`：在 默认 / 严格 / 放行 间循环切换权限模式，只影响「规则未命中」的灰色地带兜底（DeepSeek 工具模式生效）。
- `/mcp`：查看各 MCP Server 的连接状态、传输类型、注册工具数与失败原因（纯只读，不改状态）。
- `/clear`：清空当前对话历史。
- `/exit`：退出程序。

运行中按 `Esc` 会请求取消当前 Agent Loop；如果正在等待确认或澄清，则由当前面板处理取消。

## 配置

`config.yaml`（git 忽略，从 `config.example.yaml` 复制）字段：`protocol`（anthropic/openai/deepseek）、`model`、`base_url`、`api_key`。可选字段 `debug_log` 控制是否写入 `.rhinecode_debug.log` 缓存命中调试日志，默认开启。

权限规则配置（c6，可选，从 `permissions.example.yaml` 复制）：三层 YAML，`allow` / `deny` 列表，每条写成 `Tool(模式)`（如 `Bash(git *)`、`Read(config.yaml)`）。位置与优先语义——

- 用户级 `~/.rhinecode/permissions.yaml`（跨项目默认）
- 项目级 `<项目根>/.rhinecode/permissions.yaml`（随仓库走、可提交）
- 本地级 `<项目根>/.rhinecode/permissions.local.yaml`（git 忽略；「永久放行」自动写这里）

三层合并后按 **deny 永远优先**求值（不按层级覆盖）；命令用前缀+glob（`npm:*` 带词边界），文件用 gitignore 风格。危险命令黑名单与路径沙箱是更靠前、不可被规则放开的硬防线。

MCP Server 配置（c7，可选，从 `mcp.example.yaml` 复制）：两层 YAML，顶层键 `mcpServers`（`name → 条目`），stdio 型填 `command/args/env`、http 型填 `url/headers`，`env`/`headers` 值支持 `${VAR}` 展开。位置——用户级 `~/.rhinecode/mcp.yaml`、项目级 `<项目根>/.rhinecode/mcp.yaml`，按名字合并、项目级覆盖用户级。容错 fail-safe：缺失/坏配置跳过并记错误，不阻断启动。

## Spec 驱动开发

开发新功能/章节前使用 `/spec` 技能，协作澄清需求后依次生成 `docs/<章节>/` 下的 `spec.md → plan.md → task.md → checklist.md`，再据此开发与验收。当前主线章节为 `docs/c7/`。

C7 文档描述 MCP 客户端的需求、架构、任务与验收（两种传输、JSON-RPC 按 id 配对、三步会话、工具适配与注册、多 Server 隔离与生命周期、与权限/状态栏接线）：

- `docs/c7/spec.md`
- `docs/c7/plan.md`
- `docs/c7/task.md`
- `docs/c7/checklist.md`

C6（五层防御权限系统）、C5（结构化系统提示与缓存策略）、C4（Agent Loop 与 Plan Mode）文档仍保留，用于追溯设计来源。

## 测试

开发完成后优先运行：

```bash
python -m compileall rhinecode tests
python -m unittest discover -s tests
```

当前测试覆盖路径越界防护、四态确认回调、本会话放行登记规则、Plan Mode 完整计划展示、拒绝计划停止、计划获批后仍逐项确认，以及权限系统的命令/路径匹配、危险命令黑名单（含复合命令逐段与 fork 炸弹）、deny 优先求值、配置三层加载与容错、工具规范化映射、四层决策管线、loop 决策接入（被拒不停循环、allow 规则免确认）等关键行为（`tests/test_perm_*.py`）。

MCP 客户端测试（`tests/test_mcp_*.py`、`tests/test_perm_other_glob.py`）：两层配置合并与 `${VAR}` 展开、JSON-RPC 消息构造与响应分类、stdio 三步会话与按 id 配对（用 `tests/fixtures/mock_mcp_server.py` 端到端起真实子进程）、`CallToolResult→ToolResult` 转换（含 `isError` 与非文本占位、异常兜底不外抛）、单 Server 失败隔离与工具注册进 registry、`other` 分支 fnmatch 通配放行。HTTP 传输与真实 Server 端到端留作手测（见 `docs/c7/checklist.md` 场景）。

涉及 TUI 行为时，再用 tmux 或真实终端做端到端测试：

1. 在 tmux 中启动 RhineCode
2. 输入一段真实的对话请求
3. 观察 RhineCode 是否正确调用工具、生成回复
4. 对照对应章节的 `checklist.md` 逐项验收

## 安全边界

权限决定由工具层代码强制，不由模型/prompt 决定（可抵抗 prompt 注入）。每个工具执行前过五层决策管线：

1. **危险命令黑名单**（`permission/blacklist.py`）：正则拦截 `rm -rf` / `git push --force` / fork 炸弹 / `format`、`Remove-Item -Recurse -Force` 等已知高危命令，复合命令逐段+整条双重检查；**不可被任何配置或权限模式放开**。
2. **路径沙箱**（复用 `path_guard`）：文件、glob、grep 工具以启动时的当前工作目录为项目根；拒绝含 `..`、解析后越界的绝对路径、指向项目外的符号链接。
3. **可配置规则**：三层 YAML 的 allow/deny，deny 永远优先。
4. **权限模式**（`/perm`）：严格/默认/放行，只兜底「规则未命中」的灰色地带，翻不了①②③的 deny。
5. **人在回路**：判定为「问用户」时弹确认面板，四选项（本次/本会话/永久/拒绝）。

被拒不终止 Agent Loop，结构化拒绝原因回灌模型。其它注意：

- 沙箱是应用层前缀校验，管得住文件工具，但管不住 `run_command` 跑起来的脚本自己用代码 open 的文件（已知边界，OS 级沙箱留待后续）。
- `config.yaml` 可能包含真实 API Key，请勿提交到版本库；可用 `deny Read(config.yaml)` 规则进一步阻止模型读取。
- MCP 工具（c7）：远端 Server 是外部程序、不可信，故 MCP 工具一律 `read_only=False`，默认权限模式下每次调用都经人在回路确认；`kind="other"` 会跳过①黑名单与②沙箱（它们针对本地命令/路径），但仍走③规则（`allow: mcp__server__*` 可放行）与④模式兜底。stdio Server 的子进程行为不受路径沙箱约束（与 `run_command` 同属已知边界）；`mcp.yaml` 的 `env`/`headers` 可能含密钥（如 `${API_KEY}`），同样勿提交真实值。

## 已知后续工程项

以下问题已完成工程审查确认，但不属于当前阶段开发范围。后续章节会集中补齐；在当前阶段不要把它们视为阻塞项，除非用户明确要求处理：

1. API Key 与敏感配置的读取脱敏、环境变量化或工作区外管理。
2. Plan Mode 规划阶段的工具阶段强校验，防止模型同轮夹带副作用工具。
3. `write_file` / `edit_file` 的文件系统级原子写入。
4. OS 级沙箱（Seatbelt / bubblewrap），约束 `run_command` 子进程自身发起的文件/网络访问——C6 的应用层黑名单+路径沙箱已覆盖命令与文件工具的常见高危场景，但管不住子进程内部的间接访问。
5. 权限系统后续项：网络请求限制、资源配额、审计日志（C6 spec 明确不做，留待后续章节）。
6. 开发环境依赖固定与 CI，让 `compileall` / `unittest` 在标准环境稳定运行。
7. MCP 后续项（C7 spec 明确不做）：Server 健康检查与自动重连、资源/提示词/采样等非工具能力、MCP 工具的细粒度权限映射与执行超时可配置化、stdio 之外的旧版 HTTP+SSE 传输、MCP 工具结果里图片/二进制内容的实际渲染。

## 代码注释规范

为了降低项目理解成本，所有新增或修改的代码都必须包含充分、清晰、准确的中文注释。注释目标是：让第一次接触本项目的开发者，仅通过阅读代码和注释，就能理解代码的设计意图、执行流程、关键边界条件，并能够复现或安全修改相关逻辑。

### 基本要求

1. **注释语言**
   - 注释主体必须使用中文。
   - 技术关键词、框架名、协议名、变量名、函数名、类型名、设计模式名等可以保留英文，例如：`React`、`hook`、`middleware`、`cache`、`token`、`retry`、`Promise`、`DTO`、`Repository`。
   - 不要为了中文化而强行翻译通用技术词汇，避免造成理解歧义。

2. **注释粒度**
   - 关键模块、核心函数、复杂逻辑、边界处理、异常处理、数据转换、状态变更、异步流程、缓存策略、权限判断等必须写注释。
   - 简单自解释代码不需要机械式注释，例如 `count += 1` 不需要写“计数加一”。
   - 注释应解释“为什么这么做”和“这段代码承担什么职责”，而不是重复代码表面含义。

3. **可复现性要求**
   - 对于核心业务流程，注释需要说明输入来源、处理步骤、输出结果、关键依赖和副作用。
   - 第一次看项目的人应能根据注释理解该逻辑如何运行，并能在相同输入条件下复现代码行为。
   - 如果代码依赖特定配置、环境变量、外部服务、数据库结构或第三方 API，必须在注释中说明。

4. **函数/方法注释**
   - 复杂函数必须说明：
     - 函数用途
     - 参数含义
     - 返回值含义
     - 主要执行步骤
     - 可能抛出的异常或失败情况
     - 是否存在副作用，例如写数据库、发请求、修改全局状态、写缓存等

   示例：

   ```ts
   /**
    * 根据用户 ID 获取用户的完整资料。
    *
    * 执行流程：
    * 1. 先从 cache 中读取用户资料，减少数据库查询压力。
    * 2. 如果 cache 未命中，则查询 database。
    * 3. 查询成功后会将结果写回 cache，供后续请求复用。
    *
    * @param userId 用户唯一标识，必须是已登录用户的 ID。
    * @returns 用户完整资料；如果用户不存在，则返回 null。
    *
    * 副作用：
    * - cache 未命中时会访问 database。
    * - 查询成功后会写入 cache。
    */
   async function getUserProfile(userId: string): Promise<UserProfile | null> {
     // 优先读取 cache，避免高频请求直接打到 database
     const cachedProfile = await cache.get(userId)

     if (cachedProfile) {
       return cachedProfile
     }

     // cache 未命中时再查询 database，保证数据仍然可以被正确获取
     const profile = await userRepository.findById(userId)

     if (!profile) {
       return null
     }

     // 将查询结果写回 cache，提高后续相同用户请求的响应速度
     await cache.set(userId, profile)

     return profile
   }
   ```

## 文档搜索
在参考任何文档之前请确保文档是否是最新版本

## 学习与解释要求

我是第一次独立完成这类项目，可能对项目中的部分技术概念、架构设计、工具链、代码写法或最佳实践不熟悉。

在协助我开发时，请遵守以下要求：

1. **不要默认我已经理解相关技术背景**

   * 如果涉及新的技术概念、框架、库、设计模式或工程实践，请先用清楚、通俗的中文解释它是什么、为什么要用、解决了什么问题。

2. **解释代码修改的原因**

   * 不只是直接给出代码，还需要说明为什么要这样改。
   * 如果有多种实现方式，请简单说明当前方案的优点，以及为什么更适合这个项目。

3. **使用适合初学者理解的说明方式**

   * 解释时尽量避免只堆砌专业术语。
   * 必要时可以使用类比、步骤拆解或简单示例帮助理解。
   * 专业关键词可以保留英文，但需要配合中文解释。

4. **指出我需要重点理解的知识点**

   * 如果某段代码或某个设计背后涉及重要知识点，请明确指出。
   * 例如：异步处理、事件循环、状态管理、配置加载、异常处理、UI 渲染、Provider 抽象等。

5. **避免只给结论**

   * 对于关键修改，请说明：

     * 问题是什么
     * 为什么会出现这个问题
     * 应该如何解决
     * 修改后会带来什么效果

6. **保持教学式协作**

   * 这个项目不仅是为了完成代码，也是为了让我理解项目是如何搭建和演进的。
   * 因此，请在保证代码质量的同时，帮助我逐步建立对项目结构、技术选型和实现细节的理解。
