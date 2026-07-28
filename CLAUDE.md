# RhineCode

RhineCode 是一个用 Python + Textual 实现的终端 AI 编程助手，交互体验参考 Claude Code。

当前版本以 DeepSeek Provider 为主实现了 C11 阶段能力：在 C10 斜杠命令系统、C9 记忆系统、C8 上下文管理、C7 MCP 客户端、C6 五层防御权限系统、C5 结构化系统提示与 C4 Agent Loop 基础上，加入一套 **Skill 系统（可复用 AI 操作的两阶段加载与两种执行模式）**——把重复输入的提示词封装成带 YAML frontmatter 的独立 Markdown 文件，三级存放（项目 > 用户 > 内置）同名覆盖、单文件解析失败不阻断其余；**两阶段加载**让启动时只把「名字 + 一句话说明」注入稳定通道，模型判断要用时再调系统级 `load_skill` 工具把完整 SOP 拉进动态槽位（每轮重建、多个可同时激活）；**两种执行模式**——共享模式留在主对话，独立模式开一条子对话跑完只回流结论（主历史恰好新增两条配对消息，可配置带入多少历史、可指定模型）；`allowed-tools` **预授权**——列出的操作在本次执行内免于人工确认（标准语义，**不限制**模型能调用什么）；每个 Skill 自动注册成斜杠短命令（与内置命令重名则跳过并提示 `/skills run`），`/skills reload` 热更新（连同短命令一并重新注册），`/clear` 与 `/resume` 一并清空激活态；内置 commit / review / test 三个样板。其下 C10 的 **斜杠命令注册与分发系统** 仍在——独立 `commands/` 层以单一 `CommandSpec` 注册表统一管理 12 条规范命令与 8 个别名（执行、`/help` 帮助、补全候选、输入高亮共享同一份事实来源）；用户输入先经 `CommandDispatcher` 分流，本地/界面命令绕过 Agent（不耗 Token、不入模型历史），未知命令只给本地 `/help` 引导；命令名与别名大小写不敏感、参数原样保留；启动早期（Provider/MCP 之前）批量原子注册并做名称冲突 fail-fast；提示词命令（`/init`）采用双内容模型（`Message.content` 给模型、`display_content` 给界面与回放）；Tab 单候选直补/多候选菜单、菜单回车执行高亮项、完整命中的命令字段青色高亮；状态栏以 `[DEFAULT]`/`[PLAN]` 标记模式。其下 C9 的 **记忆系统（项目指令 · 会话存档 · 自动笔记）** 仍在——三层 RHINE.md 项目指令（用户级→项目 .rhinecode→项目根拼接、支持 @include 展开）与两级记忆索引在处理首个请求前注入系统提示预留槽位；每条消息即时以 JSONL 追加写入 `<项目根>/.rhinecode/sessions/`，`/resume`、`rhine --continue` 可容错恢复（坏行跳过、不成对工具调用丢组、超窗先压缩、超 24h 插时间跨度提醒）；Agent Loop 自然停止后异步调一次 LLM 把值得记的内容沉淀为四类笔记（用户偏好/纠正反馈/项目知识/参考资料，用户级与项目级分开存），索引每次注入、正文按需读取；多实例并发由锁文件防护（原子创建、非阻塞退让、过期自愈）。其下 C8 的 **上下文管理（两层压缩）** 仍在——每次 API 请求前，先用「锚点 + 增量」近似估算当前历史 token 用量；**第一层**零成本地把过大的工具结果存盘、历史里只留预览与路径占位；若估算仍逼近窗口上限，**第二层**调一次 LLM 把较早的消息压成结构化摘要、近期原文保留，并补一条「要细节请重读文件、勿照摘要脑补」的边界消息。全程幂等、fail-safe，连续摘要失败 3 次熔断，用户原始消息永不被改写；`/context` 查看用量、`/compact` 手动压缩，底部状态栏常驻用量指示。其下 C7 的 MCP 客户端仍在：启动时按两层配置连接外部 MCP Server（本地子进程走 stdio、远程走 Streamable HTTP），发现其工具并包装成已有的 `Tool` 接口注册进工具中心，对 Agent Loop / 权限系统 / TUI 完全无感。每个工具执行前仍由代码（而非模型/prompt）计算「放行 / 拒绝 / 问用户」，被拒不终止循环、把结构化原因回灌模型。模型可以在一次用户请求中循环读取项目、搜索代码、执行工具（含 MCP 远端工具）、回灌结果并继续下一轮，直到自然完成或命中停止条件。Anthropic / OpenAI Provider 目前保持纯对话能力。另有一套**跨阶段的 Trace 行为记录设施**（`--trace`，缺省关闭、不占章节号）——把运行过程中「实际发生了什么」按时间顺序写成十五类结构化事件的 JSONL，配一个只读 CLI 阅读器，用于验收既有能力与排查那类「界面上看不出、但行为确实不对」的问题；关闭时链路上不存在任何中间层。

## 语言
中文回答

## 技术栈

- Python 3.11+
- [Textual](https://textual.textualize.io/) — TUI 框架（流式渲染基于 Worker + `call_from_thread`）
- `anthropic` / `openai` SDK，`pyyaml` 配置
- `httpx` — MCP Streamable HTTP 传输的 SSE 流式读取（c7）
- 依赖与入口定义在 `pyproject.toml`，控制台脚本 `rhinecode`

## 当前能力

- **Skill 系统**（c11，已对齐 **Agent Skills 开放标准**）：独立 `skills/` 层（对标 permission/context/memory/commands：纯逻辑 + 单点接入，不依赖 Textual）。单个 Skill = 可选的 YAML frontmatter（`name`/`description`/`when_to_use`/`allowed-tools`/`context`/`disable-model-invocation`/`user-invocable`/`model`，**全部可选**）+ Markdown SOP 正文；一份只有正文的 `.md` 也是合法 Skill，说明从正文第一段提取。连字符与下划线两种键名写法等价。支持单文件型（`x.md`）与目录型（含 `SKILL.md` 入口 + 随附资源）。

  **命令名来自文件系统路径**（目录名 / 去扩展名的文件名），`name` 只是显示标签——这是「外部 Skill 原样可用」的地基：从 Claude Code 或 Codex 拉一个目录丢进 `.rhinecode/skills/` 就能用，不必检查也不必修改 frontmatter。**三级存放**：项目 > 用户 > 内置，同名整份覆盖。

  **两阶段加载**：启动时清单（命令名 + `description` + `when_to_use`）进 priority 140 稳定槽位；模型调 `load_skill` 后完整正文进 priority 120 动态槽位，**每轮重建**、多个可同时激活、重复激活幂等。正文超单体上限截断（TRUNCATED）、超总量上限整段丢弃（DROPPED），措辞不同且对用户可见。

  **「在哪执行」与「谁能触发」是正交两维**（C11 曾把它们捆在一起）：`context: fork` 开子对话跑完只回流结论（主历史**恰好新增两条配对消息**）；`disable-model-invocation` 决定模型能否自行发起；`user-invocable: false` 则不进斜杠菜单但模型仍可发起。子对话固定只带那条自包含调用消息，工具集排除 `load_skill` 防嵌套。

  **`allowed-tools` 是预授权，不是收窄**：列出的操作在**本次执行内**免于人工确认，**不限制**模型能调用什么。取值词汇与 `permissions.yaml` 的规则名一致（`Bash(git *)` / `Read` / `Write` / `Edit`，标准里的 `Glob`/`Grep` 映射到 `Read`），实现为权限引擎第③层的 `turn_rules`，排在①黑名单②沙箱**之后**——因此翻不过前两层。用户发出下一条消息即失效。无法识别的项跳过 + 警告，**不 fail-fast**（外部 Skill 里出现 `WebFetch` 这类名字是正常现象）。

  **无对应能力的标准字段逐条告知**（`background`/`agent`/`effort`/`hooks`/`paths`/`shell`），不静默忽略。每个 Skill 自动注册 `/<name>` 短命令（与内置命令冲突则跳过并提示 `/skills run <name>`），`/skills` 五形态，状态栏 `Skill:N`。

- **斜杠命令系统**（c10）：独立 `commands/` 层（对标 permission/context/memory：纯逻辑 + 单点接入，不依赖 Textual）。`CommandSpec` 单一注册来源登记规范名/别名/描述/用法/类型/参数提示/隐藏标记/处理函数；命令分**本地直执行**（`/help`·`/mcp`·`/context`·`/compact`·`/memory`，绕过 Agent；`/compact` 的专用摘要调用是明确例外）、**界面状态**（`/think`·`/plan`·`/perm`·`/resume`·`/clear`·`/exit`）、**提示词**（`/init`，展开静态内置提示词交给 Agent，界面/存档回放显示原命令）三类；处理函数只面向 `CommandController` 协议（RhineApp 实现），可用 Fake 替身独立测试。解析大小写不敏感（casefold）、参数只按首空白切分不做 shell 分词；未知命令本地提示不进 AI；启动早期 `build_builtin_registry()` 原子注册，名称/别名冲突（含仅大小写不同）抛 `CommandRegistrationError` 以退出码 1 fail-fast（先于 Provider/MCP/会话锁创建）。别名：`/h`→`/help`、`/ctx`→`/context`、`/continue`→`/resume`、`/permissions`·`/allowed-tools`→`/perm`、`/reset`·`/new`→`/clear`、`/quit`→`/exit`。Tab 补全（单候选直补、多候选稳定排序菜单、参数区不拦截）、菜单回车执行高亮项、完整命中命令字段高亮（Textual `Input.highlighter` 公开扩展点）、状态栏 `[DEFAULT]`（dim）/`[PLAN]`（加粗青色）模式标记。
- **ReAct Agent Loop**：自动执行“调用模型 → 执行工具 → 回灌结果 → 再调用模型”的多轮循环。
- **流式输出**：正文与思考内容逐块渲染，后台 Worker 不阻塞 TUI 主线程。
- **DeepSeek 工具系统**：支持读文件、glob 找文件、grep 搜内容、写文件、精确编辑文件、运行命令；`read_file` 对大文件强制范围读取，`glob_files` / `grep_content` 会逐文件应用 `Read(...)` deny 过滤。
- **MCP 客户端**（c7）：启动时按两层 `mcp.yaml` 连接外部 MCP Server（stdio / Streamable HTTP），走 JSON-RPC 2.0（请求带 id、响应按 id 配对）完成 `initialize → tools/list → tools/call` 三步，把远端工具包装成 `mcp__<server>__<tool>` 风格的工具注册进工具中心；非法 function name 会规范化，真实远端名仍用于 `tools/call`；stdio stderr 后台 drain 防止 Server 大量写日志时阻塞；多 Server 连接缓存与隔离（单个失败不影响其它），退出统一回收；支持用户通过自然语言添加 MCP，Agent 先用 `mcp_resolve_server` 解析候选，再用 `mcp_add_server` 写入配置并单 Server 重载；MCP 工具一律非只读默认走确认，`/mcp` 命令与状态栏展示连接状态。仅接工具能力，不做资源/提示词/采样与健康检查/自动重连。
- **记忆系统**（c9）：独立 `memory/` 层（对标 permission/mcp/context：纯逻辑 + 单点接入）。三套机制按依赖分级生效——**RHINE.md 项目指令**与**会话存档/恢复**对所有 Provider 生效，**自动笔记**与 `/init` 仅 DeepSeek 工具模式（F21）。① RHINE.md：三层加载（`~/.rhinecode/RHINE.md` → `<项目根>/.rhinecode/RHINE.md` → `<项目根>/RHINE.md`，越具体越靠后利用近因效应），`@相对路径` include 原地展开（相对引用文件所在目录、嵌套上限 4 层、visited 防环、越出宿主层边界不展开、围栏代码块跳过），拼接结果填 c5 预留的「自定义指令」槽位（priority 110、c9 起 cacheable=True 进稳定通道）；`/init` 用内置指令走普通 Agent Loop 探索项目生成项目根 RHINE.md（已存在只提改进建议，写盘走完整权限管线）。② 会话存档：每会话一个 `<项目根>/.rhinecode/sessions/<YYYYMMDD-HHMMSS-xxxx>.jsonl`，惰性建档、逐条追加写（记录 c8 压缩**前**的原始消息流）、无独立 meta 文件（列表信息扫 JSONL 现算）；`/clear` 开新档；`/resume` 弹交互式选择面板（上下键/回车/Esc，锁定与当前项置灰跳过），载入成功产 `HISTORY` 事件、聊天区清空并回放全部历史（切换 session 语义），`rhine --continue` 恢复最近未锁定会话（启动同样回放）；容错载入（坏行跳过、不成对工具调用丢组、恢复后若逼近窗口先跑一次 c8 压缩并 reset 锚点、超 24h 登记一次性时间跨度提醒并入下次 dynamic reminder）；启动时静默清理 30 天前存档。③ 自动笔记：Agent Loop 自然停止（FINISHED=COMPLETED）后异步 daemon 线程调一次 LLM（禁用工具、高水位只审视新增段），LLM 只产出 JSON 动作（filename 白名单 `[a-z0-9_-]+\.md` 防路径注入），程序在锁临界区内写盘并全量重建索引；用户相关存 `~/.rhinecode/memory/`、项目相关存 `<项目根>/.rhinecode/memory/`，每目录一份 `MEMORY.md` 索引（注入截断 200 行/25KB）填「长期记忆」槽位（priority 130）；用户级 memory 目录经 path_guard 只读白名单放行模型按需读取。④ 并发防护：锁文件 `O_CREAT|O_EXCL` 原子创建、拿不到即退让（笔记跳过本轮/会话拒绝载入）、mtime 过期自愈（阈值 600 秒）；会话锁靠追加时 touch + TUI 每 2 分钟心跳保鲜。`/memory` 只读报告全部状态。
- **上下文管理**（c8）：独立 `context/` 层（对标 permission/mcp：纯逻辑 + 单点接入），仅在 DeepSeek 工具模式生效。每轮请求前经 `loop.py` 单点调用两层压缩——**估算**用「锚点（上次 API `usage.prompt_tokens`，精确）+ 增量（锚点后新增消息按字符估）」，误差只积累在增量小段；**第一层预防**把单个 >4K token 或合计 >16K token 的工具结果按大到小存盘到 `<项目根>/.rhinecode/context/<id>.txt`，历史留「预览 + 路径」占位（幂等键 `tool_call_id`，只动 `role="tool"`，写盘失败保留原文）；**第二层兜底**在估算逼近窗口（自动留 13K 余量）时调 LLM 生成五段式结构化摘要，保留边界回退到最近 `user`（不拆散 `assistant(tool_calls)↔tool`），重构为 `[摘要, 边界提示, 近期原文]`。摘要 Prompt 禁用工具、要求先草稿后正文（`<<<正式摘要>>>` 分隔、草稿丢弃）；连续失败 3 次熔断，`/clear` 复位。`/context` 只读报告、`/compact` 手动压缩（**无余量阈值，主动触发即尝试摘要**，无够旧早段时如实回「无可摘要的早段」），状态栏常驻「上下文：19% · 12.3K/64K」（≥80% 或熔断橘色高亮）。窗口大小由 `config.context_window`（默认 65536）配置。
- **Plan Mode**：`/plan` 开启后先只允许只读调研和需求澄清，完整计划进入聊天记录，经用户批准后才进入执行阶段。与权限模式正交。
- **五层防御权限系统**：每个工具执行前由 `permission/` 包的纯逻辑引擎按固定顺序计算决定——①危险命令黑名单（不可被任何配置/模式放开）→②路径沙箱→③可配置规则（`Tool(模式)`，deny 永远优先）→④权限模式（严格/默认/放行，`/perm` 切换）→⑤人在回路（确认面板四选项：本次/本会话/永久/拒绝）。被拒不终止循环，结构化原因回灌模型让其调整策略。
- **可配置权限规则**：三层 YAML（用户级 `~/.rhinecode/`、项目级 `<根>/.rhinecode/`、本地级 `*.local.yaml` 不提交）声明 allow/deny；跨层合并后 deny 优先求值。
- **明确停止原因**：支持自然完成、迭代上限、用户取消、计划拒绝、连续未知工具、流错误等停止路径。
- **结构化系统提示**：七个固定提示模块走稳定可缓存通道，环境信息与 Plan Mode 提醒通过 `<system-reminder>` 作为动态补充注入。

工具调用、Plan Mode 与权限系统目前仅在 `protocol: deepseek` 且启用默认工具注册中心时可用；MCP 工具随内置工具一同仅在该模式下暴露。

## 架构

当前核心分层如下，上层尽量不感知下层具体实现，通过抽象接口和事件流解耦：

- **TUI 层**（`rhinecode/tui/`）— `app.py` 是 Textual App 主类，用 Worker 消费 AgentEvent 并逐块渲染；c10 起实现命令层的 `CommandController` 协议（`tools_enabled` / `show_user_input` / `show_message` / `send_user_message` / `switch_mode` / `query_report` / `refresh_status` / `clear_conversation` / `compact_context` / `resume_session` / `exit_application`，c11 增 `run_skill` / `reload_skills` / `deactivate_skill`），输入提交唯一入口是 `dispatcher.dispatch(text, self)`；c11 起提交守卫从静默 return 改为三分支（确认面板期间与流式运行中各给一条可见提示、同一次忙碌期只提示一次，会话面板分支保留裸 return 且不可达），并把 `_notify_skill_activation` 注入 `SkillManager`（工作线程回调，必须 `call_from_thread` 且包 try/except——它跑在只读并发桶里，抛异常会被 `future.result()` 外层当成「工具执行异常」回灌模型），Manager 三类返回值（str / SessionListRequest / 事件迭代器）经 `_consume_manager_result` 统一消费（迭代器走后台 Worker）；SessionPanel 选中后直调 `resume_session(session_id)`，不再拼接 `/resume <id>` 文本。`widgets.py` 提供 HistoryView / InputBar（接收注册表、装 `CommandHighlighter` 高亮完整命中的命令字段、命令字段内 Tab post `CommandCompletionRequested`）/ StatusBar（`compose_status_text` 纯函数组装，`[DEFAULT]`/`[PLAN]` 模式标记，c11 增 `Skill:N` 段、None 即隐藏且刻意不含方括号）/ CommandPanel（构造时注入注册表、`show_for` 用 `registry.complete` 动态取候选）/ 工具行 / diff / 确认、澄清和会话选择面板（`SessionPanel`），以及历史回放（`build_replay_items` 纯函数——user 消息优先展示非空 `display_content`——+ `HistoryView.render_history` 清屏批量重画，回放工具行用简化静态行、不复用带计时器的 `ToolCallWidget`）。
- **Commands 层**（`rhinecode/commands/`，c10）— 斜杠命令注册与分发，五个模块下层不感知上层：`models.py` 枚举（`CommandType`/`InputKind`/`DispatchKind`/`ModeTarget`/`ReportTarget`）、冻结数据类（`CommandSpec`/`ParsedInput`/`CommandInvocation`/`DispatchResult`/`CompletionItem`）与 `CommandController` 协议（不导入 Textual/Conversation/Provider）；`parser.py` 纯函数 `parse_input`（空输入/普通消息/斜杠分类 + 首空白一次切分，不做 shell 分词）；`registry.py` `CommandRegistry`（casefold 索引、原子 `register_many`、名称/别名/大小写冲突校验抛 `CommandRegistrationError`、`resolve`/`complete`/`render_help`，隐藏命令可执行不可发现；c11 起 `_specs` 拆成 `_builtin_specs` + `_skill_specs` 的只读拼接属性——**写入必须直接操作两个列表之一**，对属性 append 会写进临时对象后被丢弃，不报错也不生效——并新增 `replace_skill_commands`（冲突跳过其余照常、每条先 stage 进独立 probe 字典避免残留幽灵索引项）与 `has_skill_command`（只查 `_skill_specs`，用 `resolve` 会在重名时命中内置命令，把入口提示指向一条存在但错误的命令））；`dispatcher.py` `CommandDispatcher`（分流、统一回显恰好一次、未知命令 `/help` 引导、必需参数校验、处理异常转本地错误不降级发 AI）；`skill_commands.py`（c11）`build_skill_command_specs` 把中立的 `SkillCommandInfo` 转成 `CommandSpec`（handler 必须用工厂函数捕获 info，循环里直接 def 会让全部闭包指向最后一条、静默跑错 Skill）；`builtins.py` 13 条内置命令 + 别名 + 静态 `INIT_PROMPT`（`/help` 闭包捕获注册表；`build_builtin_registry()` 无导入副作用）。依赖方向：commands ← tui/app ← `__main__`；conversation/memory/context/provider 不反向依赖 commands。
- **协调层**（`rhinecode/conversation.py`）— `ConversationManager` 是 TUI 与 Agent / Provider 之间的中转点，维护对话历史、管理思考模式和 Plan Mode、构建权限引擎并封装 ask（四态人工确认）/澄清/计划审批回调。c10 起**不再解析斜杠文本**（旧 `handle_input` 已删除），改为暴露领域方法：`submit_user_message(content, display_content=None)`（普通消息与提示词命令共用入口，追加历史+存档+返回事件流）、`cycle_thinking()`/`toggle_plan()`/`cycle_permission()`（模式切换返回结果文本）、`mcp_report()`/`context_report()`/`memory_report()`（只读报告）、`manual_compact()`（事件流或能力限制提示）、`resume(key=None)`（无参返回 `SessionListRequest`、带 key 走 `_resume_stream` 事件流——载入成功先产 `HISTORY` 事件携带**压缩前**历史快照供 TUI 回放，再 `context_manager.reset()` + `before_request` 补压缩）、`clear()`（返回确认文本；调 `memory_manager.on_clear()` 开新档）与只读 `tools_enabled`。构建权限引擎后会把 `Read(...)` 路径过滤器注入 `glob_files` / `grep_content`，避免只读搜索工具绕过文件级 deny；持有 `MCPManager` 引用仅用于报告与状态栏（`mcp_status_line`），MCP 工具本身已注册进 registry、与此引用解耦；仅在工具模式下构造 `ContextManager`（c8），把它作为参数传入 `agent.run` 实现每轮请求前的两层压缩，并暴露 `context_status_line` 给状态栏（`manual_compact` 走事件流在 Worker 线程执行，因摘要 LLM 调用会阻塞、不能卡 UI 主线程）。c9 接入：构造 `MemoryManager`（所有 Provider）并调 `startup`（可带 `--continue` 的 resume_latest），把用户级 memory 目录注册进只读白名单；`_run()` 把 RHINE.md 与记忆索引填进 `build_default_prompt` 两参数、把一次性 pending 提醒并入 dynamic、把 `record_message` 作为 recorder 传给 `agent.run`；事件流经 `_wrap_events` 包装（FINISHED=COMPLETED 时触发异步笔记钩子）。c11 接入：构造参数增 `skill_manager`（缺省用 `SkillManager.empty()` 这个 Null Object 兜底——协调层**绝不自行扫盘**，它拿不到 `has_short_command`，而且会给既有整套测试引入读用户主目录的隐式 IO），把用户级与内置 skills 目录注册进只读白名单；新增领域方法 `run_skill`/`skills_report`/`skills_prompt_report`/`skill_status_segment`/`reload_skills`/`deactivate_skill`，以及 `_run_forked_skill`（`context: fork` 的子对话，`record` 参数区分用户触发与模型自行发起——后者不写主历史也不写存档）、`run_forked_for_model`（模型经 `load_skill` 发起 fork 的入口）、`_provider_for`（`dataclasses.replace` + `create_provider` 换模型，`BaseProvider` 接口一行不动）、`_build_ask`（从 `_run` 内联提取，供子对话复用同一份确认实现，避免「本会话放行」的规则登记出现两套逻辑）。**两处必须清空激活态**：`clear()`（F11）与 `_resume_stream` 成功分支（N4）——激活态是进程内存状态而 `/resume` 换的是历史，不清空的话会话 A 激活的 Skill 会跟着进会话 B。
- **Agent 层**（`rhinecode/agent/`）— `loop.py` 实现 ReAct 循环，并在 `_execute` 单点接入权限决策预扫，还在每轮请求前单点调用 `context_manager.before_request`（两层压缩，产出 `NOTICE` 事件）、拿到 usage 后 `record_usage` 更新估算锚点（`context_manager` 为 None 时整段跳过，保持 c8 之前行为）；`events.py` 定义 AgentEvent（含 c8 的 `NOTICE` 系统提示事件、c9 的 `HISTORY` 历史快照事件——会话恢复成功后携带压缩前消息列表供 TUI 整体回放）、停止原因和四态确认决策；`collector.py` 收集流式正文、思考与工具调用；`plan_tools.py` 支撑 Plan Mode 特殊工具；`prompt/` 负责结构化系统提示、环境信息与动态 reminder。c11 两处改造：`dynamic` 参数从 `str` 改成 `Callable[[], str]` **每轮求值一次**（模型第 N 轮激活的 Skill，其 SOP 必须从第 N+1 轮起出现在提醒里；取值型会让两阶段加载在本次循环剩余轮次里完全失效）；`RunOptions` 打包四个开关（`max_iterations`/`record_usage`/`allow_summary`/`excluded_tools`）。**`excluded_tools` 是 C11 那个三元组塌缩后的唯一遗存**——只剩子对话防嵌套这一个用户；排除既作用于「发不发 schema」也作用于「调了认不认」，两处缺一防线就不成立。加载工具由 `Tool.system_serial` 标志强制走串行、不进只读并发桶（它现在可能开一整条子对话）。
- **Permission 层**（`rhinecode/permission/`）— 五层防御权限系统，纯逻辑、与 TUI/Provider 解耦：`models.py` 数据结构与枚举；`matching.py` 命令/路径匹配与命令拆分；`blacklist.py` 危险命令黑名单；`rules.py` deny 优先求值；`config.py` 三层 YAML 加载/容错/回写；`adapter.py` 把工具调用规范化为权限请求（收口工具知识）；`engine.py` 的 `PermissionEngine.decide` 组装四层管线。
- **MCP 层**（`rhinecode/mcp/`）— MCP 客户端，五层从下到上、下层不感知上层：`config.py` 两层 `mcp.yaml` 加载/`${VAR}` 展开/容错；`auto_config.py` 负责 URL/NPM MCP 名称解析、候选置信度、`npx.cmd` 平台默认值和 `mcp.yaml` 安全写入；`jsonrpc.py` JSON-RPC 2.0 消息构造/分类/id 生成（纯数据，无 I/O）；`transport.py` `Transport` 抽象 + `StdioTransport`（子进程 + 后台 reader 线程按 id 派发，另有 stderr drain 线程保留最近错误日志）+ `HttpTransport`（Streamable HTTP + SSE，同步阻塞），并在 Windows 下解析裸命令到 `.cmd/.exe/.bat`；`client.py` `MCPClient` 封装 `initialize/list_tools/call_tool` 三步；`tool_adapter.py` `MCPTool(Tool)` + 安全注册名规范化 + `CallToolResult→ToolResult` 转换；`manager.py` `MCPManager` 编排多 Server 的连接缓存/单点隔离/生命周期/状态汇总，并支持按 server 精确 `reload_server`、清理旧工具和旧连接。同步线程模型（不引入 asyncio）以契合现有 Textual Worker 同步执行模型。在 `__main__.py` 启动时 `connect_all` 注册工具、`finally` 里 `close_all` 回收。
- **Memory 层**（`rhinecode/memory/`）— 记忆系统，六个模块下层不感知上层（c9）：`lockfile.py` 锁原语（`O_CREAT|O_EXCL` 原子创建/释放/`touch` 心跳/mtime 过期判定，机制与策略分离——拿不到锁怎么办由使用方定）；`instructions.py` RHINE.md 三层加载 + @include 展开纯函数（返回拼接文本 + 各层加载报告）；`session.py` `SessionStore`（惰性建档、追加写、扫描列表、容错载入 `_drop_unpaired` 丢组、30 天清理、会话锁 attach/接管）；`notes.py` 笔记 frontmatter 宽松解析/渲染、索引全量重建与注入截断（纯逻辑零 IO 决策）；`note_updater.py` 笔记 LLM 的 Prompt 与 JSON 响应解析（filename 白名单防注入，只产意图不写盘）；`manager.py` `MemoryManager` 唯一持 provider 引用与副作用编排（startup/两槽位注入/record_message/on_natural_stop 异步笔记线程/resume/memory_report/close）。写盘权收拢在 manager 的锁临界区内——拿锁的人就是写盘的人。
- **Skills 层**（`rhinecode/skills/`，c11）— Skill 系统，六个模块严格单向依赖、下层不感知上层：`models.py` 三枚举（`SkillSource`（**成员定义顺序即优先级顺序**）/`DegradeKind`/`ActivationStatus`）、frozen 数据类、全部常量与 `builtin_skills_dir()`；`parser.py` 纯函数 `parse_skill`（单份文本 + 调用方算好的命令名 → SkillSpec 或失败原因，不碰文件系统；键名归一**必须在留存原始键名之后**做，否则分不出用户写的是标准的 `allowed-tools` 还是旧的 `allowed_tools`，而那条语义变更告知恰恰依赖这个区分；`_split_outside_parens` 切分声明串时括号内的空格不算分隔符——标准最常见的写法正是 `Bash(git add *) Bash(git commit *)`）；`discovery.py` 三层扫描 + **从路径推导命令名** + 跨层整份覆盖（覆盖不记 error，被覆盖那份的提示也不发出）；`render.py` 全部「给模型看的文本」；`validation.py` **预授权声明 → 权限规则**（纯函数；本系统的规则名 `Bash`/`Read`/`Write`/`Edit` 与标准工具名逐字相同，故不需要翻译层）；`manager.py` `SkillManager` 唯一持可变状态与副作用编排（`turn_grants()` 取本次执行的预授权规则、`fork_excluded_tools()` 给防嵌套用）。**加锁不变量（违反即确定性死锁）**：临界区只做纯内存读写，一切解析、渲染、回调、IO 都在锁外——`activate` 写成四段式只有第③段持锁，因为 `notify_activation` → Textual 阻塞式 `call_from_thread` → 主线程 `status_segment()` 申请同一把锁会双向死锁、整个 TUI 冻结，而触发条件只是模型成功调一次 `load_skill`。依赖方向：`skills` 是叶子包，`tools/load_skill.py`、`commands/skill_commands.py`（经中立的 `SkillCommandInfo`）、`conversation.py` 单向依赖它。
- **Context 层**（`rhinecode/context/`）— 上下文两层压缩，纯逻辑 + 单点接入、与 TUI/Provider 解耦（c8）：`models.py` 两个数据类（`CompactionNotice` 压缩动作通知、`ContextStats` 用量快照）；`estimate.py` 近似估算纯函数（锚点 + 增量，`CHARS_PER_TOKEN=3.0` 偏小以倾向高估求安全）；`offload.py` `Offloader` 第一层存盘（单结果 >4K / 合计 >16K 两趟、幂等键 `tool_call_id`、只动 `role="tool"`、写盘失败保留原文）；`summarize.py` 第二层纯逻辑（`compute_retain_index` 尾部 10K token 或 ≥5 条并 snap 回最近 user、`render_transcript` 把待摘要段渲成一条 user 转录规避裸 tool 缺配对、`SUMMARY_SYSTEM_PROMPT` 五段式禁工具提示、`parse_summary` 丢草稿、`reconstruct` 重构为 `[摘要, 边界, 保留区]`）；`manager.py` `ContextManager` 唯一持 provider 引用与副作用编排（`before_request` 自动路径、`manual_compact` 手动路径无阈值、`record_usage` 更新锚点、熔断计数、`status_line`/`usage_report` 可观测、`reset` 复位）。c11 两处扩展：`before_request` 增 `allow_summary`（必须在 `and` 链最前面短路——`_estimate` 的锚点对应主历史，用它估算子对话那条短历史毫无意义）；`summarize.py` 抽出 `snap_back_to_user` 返回 `Optional[int]`，两个调用方对「找不到 user 边界」的反应恰好相反。仅 DeepSeek 工具模式构造，跨消息长期持有以累积锚点与熔断状态。
- **Trace 层**（`rhinecode/trace/`）— **跨阶段的测试设施，不是产品功能、不占章节号、不隶属 Skill 系统**。它把运行过程中「实际发生了什么」按时间顺序写成结构化事件流（JSONL），服务 C2–C11 已完成能力与未来所有阶段的验收，专治那类「界面上看不出、但行为确实不对」的问题（立项动因之一就是「模型调用了本轮没发给它的工具」这种从界面完全看不见的偏差）。**缺省关闭**，`rhine --trace` 开启。五个模块：`models.py`（十五类事件枚举、四种作用域、`clip` 全项目唯一截断入口——未截断返回裸串、截断返回 `{text, truncated, original_length}`、`redact_config` 白名单式脱敏、`agent_event_payload` 字段白名单刻意排除 `arguments`/`output`/正文、`default_trace_path` 时间戳含毫秒否则同秒两次运行会追加进同一文件）；`recorder.py`（`TraceRecorder` 把序列化+写入+flush+序号推进全放进**同一个临界区**——`seq` 是 JSON 首字段，锁外组装等于锁外读计数、并发必出重号；**序号只在 flush 成功后推进**使「静默丢弃」与「无跳号」相容；临界区禁回调禁跨线程调度，沿用 C11 的死锁教训。`NullRecorder` 不继承真实记录器且 `emit_lazy` 不调 factory，是「关闭时零开销」的兑现点；`create_recorder` 捕获 `OSError` 降级——父路径是文件时 Windows 抛 `FileExistsError`、POSIX 抛 `NotADirectoryError`，故不能捕具体子类。作用域用 `threading.local()`，**不跨线程继承**，线程池入口必须显式重绑）；`tracing_provider.py`（`TracingProvider` 装饰器，是 N4 允许的**唯一分层例外**——`provider/base` 是零副作用纯抽象模块；显式 `contextlib.closing` 让「消费方 break 后何时结算」成为代码事实而非 CPython 引用计数细节）；`reader.py`（只读 CLI 阅读器，`python -m rhinecode.trace.reader`，**刻意不注册控制台入口**）。作用域四种：`main` / `isolated:<skill>` / `summary` / `notes`——后两者必需，因为 `ContextManager` 与 `MemoryManager` 与主对话**共用同一个 Provider 实例**，不区分会污染轮次计数。依赖方向：`trace` 是叶子包（只依赖标准库），被 `agent`/`conversation`/`context`/`memory`/`skills`/`commands`/`tui`/`bootstrap` 单向依赖。
- **驱动设施层**（`tests/e2e/`）— **P0 Trace 设施的续作，同样是跨阶段测试设施：不是产品功能、不占章节号、不属于任何产品章节、不进产品包**（产品代码绝不反向依赖它）。P0 解决「看清实际发生了什么」，P1a 解决「让 Claude 自己把交互跑起来」——起一个**常驻宿主进程**把 RhineCode 完整装配并让界面一直活着，外部经**本机回环控制通道**发指令驱动，形成 `send → wait → status → answer → wait → 再 send` 的闭环，全程不重启。**P1a 的边界是「交互闭环」，P1b（无人值守回归）不在本轮**。十一个模块：`protocol.py`（行分隔 JSON、九个错误码、四种面板的 choice 取值表与 `via` 取值；`err()` 对越界 code 抛 `ValueError`——拼错码字是客户端分支的**静默**失效）；`discovery.py`（名片 `<临时目录>/rhinecode-e2e/host-<pid>.json`，位置与工作区无关以破解「要先找到工作区才能找到端口」的死循环；两级陈旧判定 = 连不上即陈旧 + 连上再校验 pid 防端口复用；**刻意不做进程存活检测**）；`sandbox.py`（可丢弃校验两条判据 = 位于系统临时目录之下 **且** 含本设施标记文件，**与「目录是否为空」解耦**；三步清理 `cleanup()` → `chdir` 回去 → `rmtree`，顺序不可调且 `rmtree` 不吞错）；`seeding.py`（六个预置函数，全显式 UTF-8；git 缺失明确抛错不静默跳过——静默跳过会让依赖提交历史的场景**假绿**）；`fingerprint.py`（路径+size+mtime 的 sha256 前 12 位；已知误报 mtime 变化即报「代码变了」，但**误报只让人多重启一次，漏报会让人以为改动生效了**）；`scripted.py`（`ScriptedProvider` 按轮次作答、耗尽走 `[e2e-fallback]` 兜底不抛错不挂起；`RecordedCall` 原样留存四个入参，其 `dynamic_reminder` 读的是**消息列表末条 system 消息**——已激活 Skill 的 SOP 正文在那里、**不在 `system` 参数里**，混作一谈会写出永远失败的断言）；`assertions.py`（`TraceView` + 十一项断言词汇 + 失败诊断附证据序号邻域整行；读记录一律复用 `trace.reader`，`check_history_len` 是唯一允许 `json.loads` 的地方——它读的是**会话存档**）；`control.py`（`DriverCore` 与**四条不变量**，见下）；`host.py`（常驻宿主 + `ControlServer` + 退出编排，**socket 先于装配**以便装配期致命错误经通道回报）；`client.py`（瘦客户端，无状态无重试，**刻意不 import 任何 `rhinecode`**——宿主挂掉时它仍要能起来报错）；`scripts.py`（现成剧本与预置函数，供手工驱动直接引用）；`p0_scenarios.py`（**用 P1a 验收 P0 那 9 条端到端场景**的剧本与预置——它们当初留作手测只因「没有能驱动界面的东西」，P1a 交付后已实跑 6 条通过。其中场景 2「白名单外调用」尤其值得一提：原始 bug 是模型的**偶发幻觉、不可控**，P0 手测时只能听天由命，而脚本化假模型可以**确定性复现**它——这正是假模型相对真实模型的独特价值。P1b 做无人值守场景时直接复用本模块）。产品侧只动三处、**全部可选且缺省等于现状**：`build_app` 增 `provider_factory` / `exclude_tools`（并把前者透传给协调层），`ConversationManager` 增 `provider_factory`（**换模型旁路**的注入点），`RhineApp` 的交互结算点来源字段可传入 + `_settle_session` 收拢两处会话结算。

  **`control.py` 的四条不变量**（违反的后果分别是确定性死锁、静默失败、随机红）：① **加锁四段式**——临界区内只做纯内存读写，一切跨线程调度必须在锁外（持锁跨线程 → 主线程被慢回调堵住 → 锁被无限占 → 恰恰在最需要取消时取消不了，与 C11 `SkillManager` 完全同型）；② **`wait` 不持驱动锁**（否则 `wait` 期间 `status` 答不了，违反「一秒内返回」）；③ **应答前必须复核「面板已展示且已获得焦点」**，且**面板类型取自待决盒的 `kind`、不能从控件类反推**（`ConfirmPanel` 被 confirm 与 approve 两种交互复用；实测抢跑窗口占比 8869/8870）；④ **跨线程只走 `run_on_main`** 且一律带超时（`call_from_thread` 无超时参数、会阻塞到工作跑完、还拒绝主线程调用）。另有两条实测教训写死在代码里：`send` 必须 `bar.focus()` + `bar.value=` + `await pilot.press("enter")` 三行俱全（只设 value 不按回车 → 模型一次没被调；把 `press` 塞进 lambda → 只造出一个从未被 await 的协程对象、**驱动器返回 ok 而什么都没发生**）；`shutdown_on_main` 的「强制结算」与「等忙碌态转假」必须**交织**在同一个带上限的循环里（不结算则 `asyncio.run` 收尾去 join 阻塞的工作线程，而 Python 3.11 的 `shutdown_default_executor` 无超时、永不返回；分两段则结算后新弹的面板会在第二段再次挂住）。

- **装配层**（`rhinecode/bootstrap.py`）— `build_app(cfg, *, user_dir, resume_latest, recorder, provider_factory, exclude_tools) -> BuildResult`：把原先写在 `__main__.main()` 里的十二步装配整体抽出，**顺序一处不动**（含那段「位置为什么卡在这个窄窗口里 / 两头都不能挪」的 C11 注释，它随代码迁走）。抽出的收益是**测试能在进程内装配一个真实应用**并逐个断言中间组件（原先只能起子进程，什么都断言不了）。三处致命错误从 `print + sys.exit(1)` 改为抛 `BootstrapError`（`args[0]` 即成文的完整 stderr 文案，调用方原样打印、不再拼前缀；三段文案是既有启动测试逐字断言的**不可变契约**），**工厂内零 `sys.exit`**。`cleanup` 闭包五步固定顺序且幂等：`session_end` → `memory_manager.close()` → `mcp_manager.close_all()` → `clear_read_roots()` → `recorder.close()`（第一步必须在最后一步之前；第四步是因为只读白名单是**进程级全局状态**，同进程连续装配不清理会继承上一次注册的目录）。注意 `session_start` **不是记录文件里第一条事件**——`bind_tools` 的 `skill_state` 排在它前面，因为快照必须等 `connect_all` + `bind_tools` 完成才完整；读 trace 时把它当「装配完成」而非「进程起点」。
- **Provider 层**（`rhinecode/provider/`）— `base.py` 定义 `BaseProvider`/`Message`/`StreamChunk` 抽象；`anthropic.py`、`openai.py`、`deepseek.py` 为具体实现；`factory.py` 的 `create_provider` 按 `protocol` 分发。
- **Tools 层**（`rhinecode/tools/`）— `base.py` 定义 Tool / ToolResult 抽象；`registry.py` 注册默认工具，并提供 `unregister` 给 MCP 重载清理旧工具；`mcp_config.py` 暴露 `mcp_resolve_server`（只读解析）和 `mcp_add_server`（写配置并重载，非只读）两个内置工具；`load_skill.py` 两阶段加载的第二阶段入口，四态返回（ACTIVATED 回一句确认 / FORKED 回子对话结论 / NOT_FOUND / NOT_MODEL_INVOCABLE）；它声明 `system_serial = True` 强制串行——**判据做成工具自己的标志而不是循环按名字判断**，否则 agent 层要反向 import skills 层；`path_guard.py` 负责路径边界（`resolve_in_workspace` 抛异常版、`is_within_workspace` 布尔版供权限引擎②沙箱层复用）；`read_file.py` 支持 `start_line` / `max_lines` 并对超过 1 MiB 的文件强制范围读取；`glob_files.py`、`grep_content.py` 支持可注入 `path_filter`，输出前逐文件过滤被 `Read(...)` deny 的路径；`write_file.py`、`edit_file.py`、`run_command.py` 是当前其它核心工具。

新增 Provider：在 `provider/` 下继承 `BaseProvider` 实现 `stream_chat`，再到 `factory.py` 添加 `elif` 分支，配置中 `protocol` 改为新值即可。

如果新 Provider 要支持工具调用，需要参考 `deepseek.py`：

- 把 `tools` schema 传给模型 API。
- 从流式响应中拼接工具调用参数。
- 产出 `StreamChunk(type="tool_call")`。
- 能序列化历史中的 `assistant(tool_calls)` 与 `role="tool"` 消息。

新增工具：在 `tools/` 下继承 `Tool`，声明 `name`、`description`、`parameters`、`read_only`，实现 `execute`，再到 `ToolRegistry.default()` 注册。文件类工具必须复用 `path_guard.py` 的路径边界校验；如果工具会批量枚举、搜索或打开文件，还应提供类似 `path_filter(rel_path) -> bool` 的注入点，并由 `ConversationManager` 用权限引擎补上逐文件 `Read(...)` deny 过滤。若新工具要纳入细粒度权限控制（映射到 Bash/Read/Edit/Write 规则名与对应 specifier），在 `permission/adapter.py` 的 `_TOOL_MAP` 加一行映射即可；未映射的工具自动落到 `other` 分支（仅按工具名匹配整工具规则 + 走权限模式兜底），不会漏过权限检查。`other` 分支的工具名用 `fnmatch` 通配匹配（c7 决策 A）：无 `*` 时等价精确匹配（向后兼容），故 MCP 工具可用 `allow: mcp__<server>__*` 一次放行整个 Server；注意 MCP 注册名可能被规范化，权限规则应使用 `/mcp` 显示的 registered name，实际远端名保存在工具对象里用于 `tools/call`。

新增 MCP Server：优先让 Agent 使用内置工具自动完成，而不是直接手写 YAML。流程是先调用只读的 `mcp_resolve_server` 解析用户输入（URL 直接生成 HTTP 配置；自然语言名称/包名优先走 NPM registry），向用户说明来源、写入位置和将启动的外部命令后，再调用非只读的 `mcp_add_server` 写入 `~/.rhinecode/mcp.yaml` 或 `<项目根>/.rhinecode/mcp.yaml` 并触发单 Server 重载；未明确范围时默认项目级。手动声明仍支持：stdio 填 `command/args/env`，http 填 `url/headers`，值支持 `${VAR}`。若要新增**传输方式**（stdio/HTTP 之外），在 `mcp/transport.py` 继承 `Transport` 实现 `start/request/notify/close`，再到 `manager.py` 的 `_build_transport` 加分支即可，`client.py` 以上无感。

新增斜杠命令（c10 起单一注册，旧的「逻辑 + 补全列表」双维护规则已废除）：在 `commands/builtins.py` 的 `build_builtin_registry()` 登记一条 `CommandSpec`（规范名/别名/描述/用法/类型/参数提示）并实现处理函数（只做「参数解释 + `CommandController` 调用」，需要新领域能力时在 `conversation.py` 加领域方法、`tui/app.py` 的控制器方法里接线），再补一组 `tests/test_command_builtins.py` 测试即可——补全菜单、`/help` 帮助、输入高亮都自动读取注册表，无需再改 `CommandPanel`、状态刷新白名单或任何清单。需要刷新状态栏的命令在处理函数里显式调 `controller.refresh_status()`。若命令带选项面板/回调（如确认四态），仍需同步 `tui/app.py` 的事件处理与回调注入。

新增 Skill：**不写代码**——在 `<项目根>/.rhinecode/skills/` 或 `~/.rhinecode/skills/` 放一个 `<name>.md`（或一个含 `SKILL.md` 的目录）。**命令名就是文件名/目录名**，frontmatter 全部可选。想让模型知道何时该用它，写 `description` + `when_to_use`；想让某些操作免确认，写 `allowed-tools: [Bash(git *), Read]`（**这是预授权，不是限制**）；想开子对话只回流结论，写 `context: fork`。用 `$ARGUMENTS` 承接用户参数。运行中 `/skills reload` 即可生效。

**从 Claude Code 或 Codex 直接搬**：把整个 Skill 目录复制进来即可，不需要改任何东西。无法支持的字段（`background`/`agent`/`effort`/`hooks`/`paths`/`shell`）会在 `/skills` 里逐条告知本版本的实际行为。

> **实测教训（trace 手测场景 2 抓到的）**：声明只读白名单时，`read_file` 几乎总该配上
> `glob_files`。只给 `read_file` 的话，模型面对「审阅 app 目录下的代码」这类任务
> **没有任何办法发现目录里有哪些文件**——实测中它先 `read_file('app')` 得到
> 「路径是目录而非文件」，转而调 `run_command('dir /s /b')` 被白名单挡下，
> 于是开始盲猜文件名（`main.c`、`app.cpp`、`index.html`、`forms.py`、`views.py`、
> `urls.py`……12 次猜、11 次失败），白烧了 8 轮 API 调用。
> **收窄过度比不收窄更糟**：它不会报错，只会让模型退化成穷举，而这在界面上完全看不出来
> （用户只看到「读了几个文件然后给了个回答」）。新增**内置样板**才需要动代码：在 `rhinecode/skills/builtin/` 加 `.md`，`pyproject.toml` 的 package-data 已覆盖 `builtin/*.md` 无需再改，但要确认白名单里的工具名全部真实存在（否则启动 fail-fast）。

> 「成对维护点」备忘（改一处常需同步另一处，避免遗漏）：
> - 新增工具 → `tools/registry.py`（注册）+ `permission/adapter.py`（权限映射，按需）+ 若要在 `allowed-tools` 里可写，还要在 `skills/validation.py` 的 `_TOOL_ALIASES` 加一行
> - 新增 MCP 传输方式 → `mcp/transport.py`（`Transport` 子类）+ `mcp/manager.py` `_build_transport`（按 `kind` 分支）
> - 新增斜杠命令 → 只需 `commands/builtins.py` 登记一条 `CommandSpec` + 处理函数 + 测试（c10 单一注册来源；补全/帮助/高亮自动生效）
> - 新增 `ModeTarget` / `ReportTarget` 枚举值 → `commands/models.py`（枚举）+ `tui/app.py` `switch_mode`/`query_report`（分支，未知值明确抛错）+ `conversation.py`（对应领域方法）
> - 新增状态栏展示字段 → `tui/widgets.py` `compose_status_text`（渲染）+ `tui/app.py` `_refresh_status`（取值传入）；命令触发的刷新由处理函数调 `refresh_status()`，无白名单
> - 新增确认/交互态 → `agent/events.py`（枚举）+ `tui/widgets.py`（面板选项 id）+ `tui/app.py`（id→枚举映射）+ `conversation.py`（回调闭包处理）
> - 新增 RHINE.md 层级或记忆目录 → `memory/instructions.py` / `memory/manager.py`（加载逻辑）+ `/memory` 报告（`memory_report`）+（涉及模型按需读取时）`path_guard` 只读白名单注册（`conversation.py`）
> - 新增 Skill 内置样板 → `rhinecode/skills/builtin/*.md` + 确认 `pyproject.toml` 的 `[tool.setuptools.package-data]` 仍覆盖它（否则 `pip install -e .` 正常但真安装后样板凭空消失且不报错）
> - **预授权的授予与撤销必须成对**，且撤销放在 `finally`：`_wrap_events` 是每一次 Agent 执行的唯一包装点，三条路径（主对话 / 用户触发的子对话 / 模型自行发起的子对话）都经过它。**撤销用 `restore_turn_rules(token)` 回滚而不是 `revoke_turn_rules()` 清空**——授权会嵌套（模型在主对话里发起 fork 时内层若清空，会把外层那次执行的授权也抹掉，外层剩下的轮次突然开始弹本不该弹的确认面板，界面上看不出任何异常）
> - **新增 Skill frontmatter 字段** → `skills/models.py`（`SkillSpec` 字段 + 若无对应能力则登记进 `UNSUPPORTED_FIELDS`）+ `skills/parser.py`（读取与归一）+ `skills/render.py`（若要进清单）；连字符写法要能被 `_normalize_keys` 认出
> - **命令名的来源是文件系统路径，不是 frontmatter** → 改动 `discovery.py` 的推导逻辑时，`跨层覆盖键`、`短命令注册`、`/skills 报告` 三处的「同一个 Skill」判定都跟着它走
> - `rhinecode/tools/__init__.py` **不得 re-export 任何子模块**：`tools ↔ skills` 与 `tools ↔ mcp` 都是包级互相依赖，不成环唯一依靠这个文件是空的
> - `SkillManager` 持锁期间**禁止任何回调与跨线程调度**：违反会与 Textual 阻塞式 `call_from_thread` 组成确定性死锁，整个 TUI 冻结
> - Skill 短命令的两处注册必须同口径：`__main__` 启动时一次、`RhineApp.reload_skills()` 热更新时一次（都走 `build_skill_command_specs` + `replace_skill_commands`，且都要把 skipped 的冲突项提示成 `/skills run`）
> - 启动接线中 `LoadSkillTool` 必须在算 `known_tools` **之前**注册，且整段 Skill 校验必须夹在 `MCPAddServerTool` 注册之后、`connect_all` 之前（两头都不能挪，理由见 `__main__.py` 注释）
> - 状态栏/历史区文本含字面 `[`（如 `[provider]`）→ 必须转义为 `\[`，否则被 Textual markup 当标签吞掉
> - **任何往 markup 串里嵌纯文本的地方，一律用 `tui/widgets.py` 的 `escape`，绝不要 `from rich.markup import escape`**：rich 那版只转义「看起来像完整标签」的 `[...]`（正则要求闭合的 `]`），因此**被截断的括号会被它整个放过**；而 Textual 的 Content markup 比 Rich 严格，会把落单的 `[` 当标签开头并抛 `MarkupError`——抛出点在 `OptionList.get_content_height` 这类**布局阶段的主线程**调用里，不在业务调用栈上，没有任何 try/except 兜得住，**Textual 直接拆掉整个 app、程序退出**。真实现场：`summarize_args` 先截断后转义，把 `allowed_tools: [read_file, glob_files, …]` 切成 `allowed_tools: [read_file, glo…`，`edit_file` 的 `old_string`+`new_string` 天然成对凑够两个未闭合括号（一个不够，实测 Textual 容忍），确认面板一弹就崩。护栏见 `tests/test_tui_markup_escape.py`（含现场重演与「旧口径确实会崩」的反证）
> - 新增 trace 事件类型 → `trace/models.py`（`TraceEventType` 枚举）+ `trace/reader.py` 的 `SUMMARIZERS`「type → 摘要函数」表（**漏了不报错**，只会让新事件在阅读器里显示成「（未登记类型）」——那句话就是为暴露这个遗漏而刻意保留的）
> - `bootstrap.build_app` 的装配顺序 → 那段「位置为什么卡在这个窄窗口里 / 两头都不能挪」的理由注释**必须随代码走**；迁代码留注释等于把知识丢了。**该窗口里现在卡着两件事**：C11 的 Skill 白名单校验，与 P1a 的 `exclude_tools` 摘除——两者理由同型（往前挪会让合法工作区被判笔误而 fail-fast，往后挪会让 `session_start` 快照与实际工具集不符）。重排 bootstrap 时**必须两段一起看**，只看到 Skill 那半段就会以为另一半可以自由移动
> - 新增控制通道指令 → `tests/e2e/protocol.py`（取值/错误码）+ `control.py`（`DriverCore` 方法）+ `host.py` 的 `dispatch` 分支 + `client.py`（子命令）+ `test_e2e_control.py`（**五处齐改，漏一处是静默失效**：客户端能发但宿主不认、或宿主认了但没人调得到）
> - 产品侧交互结算点新增来源取值 → `tui/app.py` 三处（`_interact` 的待决盒 / `_resolve_interaction` / `_settle_session`）+ `tests/e2e/protocol.py` 的取值集合 + 断言词汇
> - `build_app` 新增参数 → `rhinecode/bootstrap.py` + `tests/e2e/host.py` 的装配调用（**宿主是它的第二个真实调用方**，漏改会让驱动设施与真实启动行为分叉，而分叉处恰恰是「验收依据」）
> - **`_settle_session` 是会话面板结算的唯一入口** → 将来任何第三条会话结算路径都必须走它，否则丢埋点、丢幂等守卫（`test_tui_keybindings.py` 有结构护栏钉着）
> - `tests/e2e/assertions.py` 的十一项断言词汇 ↔ spec F25 清单 → 增删要同步 spec / `assertions.py` / `test_e2e_assertions.py` 三处
> - 测试里删沙箱目录 → 一律走 `tests/e2e/sandbox.py` 的 `force_rmtree`，**不要直接写 `shutil.rmtree(path, ignore_errors=True)`**：撞上 git 留下的只读 `.git/objects` 会「删一半」，留下一个只剩空 `.git` 的残骸且**一个错都不报**（实测只在全量测试的并发负载下出现，单跑那条用例必成功，追起来极费劲）
> - 新增状态栏字段 → 除原有两处（`compose_status_text` 渲染 + `_refresh_status` 取值）外，`_refresh_status` 现在把**同一份**参数组同时喂给 `compose_status_text` 做 trace 快照。**保持单一参数组、不要抄第二份清单**——抄了会让维护点从两处涨到三处，而漏改的后果是记录里的状态栏文本与用户实际看到的不一致（观测设施撒谎但不报错）
> - trace 埋点一律走**受保护漏斗**：`agent/loop.py` 的 `_safe_emit` / `_safe_emit_lazy` / `_safe_scope` / `_safe_bind`。Agent Loop 是唯一会把异常变成「工具结果」回灌模型的地方，埋点异常会伪装成「你的工具坏了」
> - `ui_message` 的 AI 正文由 `tui/app.py` `_do_stream` 里的 `reset_text_widgets()` 收尾产出，调用点共**四处**：循环内三处（PROGRESS / TOOL_START / HISTORY）+ **`finally` 里一处**。第四处不可省——前三处都是「靠下一个动作给上一段收尾」，所以一轮运行里的**最后**一段正文没有它就一条事件都不产（P1a 实测发现的 P0 缺口，**界面上完全看不出来**：界面显示得好好的，只是没被记下来）。它在 `finally` 里的位置也定死：`bind_scope(SCOPE_MAIN)` **之后**（该段呈现在主界面上、该记 `main`）、两个 `call_from_thread` **之前**（后者在退出竞态下会抛，放后面等于「出错时不记录」）。护栏见 `tests/test_e2e_control.py::FinalTextRecordedTest`
> - `SkillManager` 的 trace 埋点必须在锁**外**（`deactivate` 为此改成「锁内算结果 → 出锁 → 埋点 → 返回」）；明确**不埋** `tool_policy()`——它每轮被调用，埋进去会淹掉时间线

## 常用命令

```bash
pip install -e .                          # 安装（开发模式），生成全局命令 rhine
rhine                                      # 任意目录启动；首次运行自动生成 ~/.rhinecode/config.yaml 模板并引导填 api_key
rhine --config config.yaml                # 显式指定配置文件覆盖全局配置
rhine --continue                          # 启动时恢复最近一次会话，接着上次继续（c9）
python -m rhinecode --config config.yaml  # 未安装/开发调试时的等价入口（需在源码目录）

# 行为记录（trace，跨阶段测试设施；缺省关闭，不开则一个字节都不写）
rhine --trace                             # 写 <项目根>/.rhinecode/traces/<时间戳>.jsonl
rhine --trace /tmp/x.jsonl                # 指定文件

# 阅读产出（只读；刻意不注册控制台入口，避免给 PATH 多一个命令）
python -m rhinecode.trace.reader <文件>                          # 时间线摘要，每事件一行
python -m rhinecode.trace.reader <文件> --type api_request       # 按类型过滤（逗号分隔多个）
python -m rhinecode.trace.reader <文件> --scope isolated:review  # 按作用域过滤（可与 --type 取交集）
python -m rhinecode.trace.reader <文件> --seq 42                 # 展开单条完整负载（截断字段标原长）

# 端到端驱动设施（P1a，跨阶段测试设施；同样不进产品包、不注册控制台入口）
# ① 起一个常驻宿主（它自己建临时工作区与临时用户目录，事后自动删）
python -m tests.e2e.host --mode scripted --script tests.e2e.scripts:CONFIRM_THEN_DONE
python -m tests.e2e.host --mode scripted --script tests.e2e.scripts:THINK_AND_READ \
                         --seed tests.e2e.scripts:seed_basic --keep-workspace
python -m tests.e2e.host --mode live --idle-timeout 600          # 真实模型（需有效凭据）

# ② 用瘦客户端驱动它（每次调用都是独立进程，无状态、无重试）
python -m tests.e2e.client hosts                 # 列出当前宿主（排障用，不需要宿主活着）
python -m tests.e2e.client status                # 三态 / 面板原文与可选项 / 轮次 / 指纹
python -m tests.e2e.client send "写个文件"        # 走真人提交入口（不是内部方法）
python -m tests.e2e.client wait --timeout 180    # 等到 idle 或 pending 两个终态之一
python -m tests.e2e.client answer once           # 应答面板（--via keys 走模拟按键路径）
python -m tests.e2e.client observe --since 42 --types tool_execute   # 读记录增量
python -m tests.e2e.client cancel                # 等价于真人按 Esc
python -m tests.e2e.client quit                  # 优雅退出并清理临时目录

# ③ 跑它的测试（宿主用例会起真实子进程）
python -m unittest tests.test_e2e_host
RHINE_E2E_SLOW=1 python -m unittest tests.test_e2e_host   # 含「连续起停」慢速专项
RHINE_E2E_LIVE=1 python -m unittest tests.test_e2e_live   # 真实模式（缺省 skip）
```

运行时斜杠命令（c10 起大小写不敏感、支持别名与 Tab 补全；未知命令不进 AI、只提示 `/help`）：

- `/help`（别名 `/h`）：按稳定顺序列出全部非隐藏命令的规范名、别名、描述、用法、类型与参数提示（c10）。
- `/think`：在 off / high / max 间循环切换思考模式（Anthropic / DeepSeek 生效）。
- `/plan`：切换 Plan Mode，先规划、澄清和审批，再执行（DeepSeek 工具模式生效）。
- `/perm`（别名 `/permissions`、`/allowed-tools`）：在 默认 / 严格 / 放行 间循环切换权限模式，只影响「规则未命中」的灰色地带兜底（DeepSeek 工具模式生效）。
- `/mcp`：查看各 MCP Server 的连接状态、传输类型、注册工具数与失败原因（纯只读，不改状态）。
- `/context`（别名 `/ctx`）：查看当前上下文近似用量（估算 token / 窗口上限 / 余量 / 已存盘工具结果数 / 是否熔断），纯只读（DeepSeek 工具模式生效）。
- `/compact`：手动触发第二层 LLM 摘要压缩，无余量阈值——主动触发即尝试；历史尚无够旧的早段可摘要时如实回「无可摘要的早段」（DeepSeek 工具模式生效）。
- `/resume`（别名 `/continue`）：无参弹出交互式会话选择面板（列出全部会话：编号/ID/标题/消息数/时间/锁标记，上下键选择、回车载入、Esc 退出；锁定项与当前会话置灰跳过）；`/resume <编号或ID>` 直接载入。载入成功后聊天区清空并回放该会话全部历史（用户消息/AI 回复/简化工具行），存档指针随之切换（被其它实例新鲜锁占用时拒绝且不清屏；载入后逼近窗口先跑一次 c8 压缩）。`rhine --continue` 启动恢复同样回放历史。所有 Provider 生效（c9）。
- `/memory`：查看记忆系统状态——RHINE.md 各层加载与 include 展开、两级笔记数量与索引超限标记、最近一次自动笔记更新结果、当前会话 ID 与已存档消息数、写锁状态。纯只读（c9）。
- `/init`：用内置指令启动一次 Agent Loop，探索项目生成项目根 `RHINE.md`；已存在时不覆盖、只输出改进建议。写盘走完整权限管线（DeepSeek 工具模式生效，c9）。c10 双内容：界面与恢复回放显示 `/init`，模型历史与存档保留展开后的完整提示词（`Message.display_content`）。
- `/skills`：管理 Skill（c11）。五种形态——无参列出全部 Skill 及其来源层级、模式、激活状态与加载错误；`/skills prompt` 查看当前**实际注入**了什么（第一阶段清单 / 已激活正文 / 当前可见工具集），排查「为什么模型没按我的 Skill 做」用；`/skills reload` 热更新定义（已激活的正文自动换新，定义消失的自动卸载，**斜杠短命令一并重新注册**——新增的立刻可补全可执行、删除的随之消失，白名单笔误只丢弃并警告「下次启动会失败」而不终止进程）；`/skills off [名字]` 卸载指定或全部激活项；`/skills run <名字> [参数]` 执行指定 Skill（独立模式的通用入口，也是短命令被重名跳过时的替代入口）。
- **Skill 短命令**：每个 Skill 自动注册 `/<name>`（如 `/commit`、`/review`），进 Tab 补全与 `/help`；与内置命令或其别名重名时跳过注册并在启动时提示改用 `/skills run <name>`。
- `/clear`（别名 `/reset`、`/new`）：清空当前对话历史（并复位上下文压缩的锚点/熔断/已存盘状态；会话存档开新档、旧档保留，c9；一并卸载全部已激活 Skill，c11）。
- `/exit`（别名 `/quit`）：退出程序。

补全与高亮（c10）：输入 `/` 前缀实时弹候选（只显示规范名，别名不参与补全——仍可直接输入执行、完整命中仍高亮、`/help` 可见；隐藏命令不出现）；Tab 单候选直补（有参数提示的命令末尾留一个空格）、多候选弹稳定排序菜单；菜单可见时回车执行当前高亮项；光标进入参数区后 Tab 不拦截。输入框只在命令字段完整命中规范名或别名时以青色加粗高亮该字段，参数与未完成前缀保持普通样式。无参命令忽略多余参数（`/clear now` 仍清空）。

运行中按 `Esc` 会请求取消当前 Agent Loop；如果正在等待确认或澄清，则由当前面板处理取消。

## 配置

`config.yaml`（git 忽略，从 `config.example.yaml` 复制）字段：`protocol`（anthropic/openai/deepseek）、`model`、`base_url`、`api_key`。可选字段 `debug_log` 控制是否写入 `.rhinecode_debug.log` 缓存命中调试日志，默认开启。可选字段 `context_window`（c8）声明上下文窗口上限（token），作为「历史是否逼近溢出、何时压缩」的判断基准；缺省 / 非法 / 非正值都由 `_parse_int` fail-safe 回退默认 65536（不抛异常），故首次生成的模板**不含**此项——没写即用默认值，想调大/调小手动加一行即可。注意它只影响 RhineCode 的压缩时机，不改变模型真实上限，应贴近所用模型的实际上下文长度。

配置定位（`rhinecode/config.py` + `__main__.py`）：命令**不带 `--config` 时缺省读用户级全局配置 `~/.rhinecode/config.yaml`**，使 `rhine` 在任意工作目录都能读到同一份配置（工作目录本身仍作为 AI 操作的项目根，二者互不影响）。该缺省文件不存在时首次运行会自动写入模板（`scaffold_user_config`，占位 `api_key: YOUR_API_KEY`）并提示后退出；模板占位符会被 `__main__` 单独拦下引导（占位符是非空串、能过 `load()` 校验，不拦会带假 key 启动）。显式 `--config <路径>` 优先且指向不存在的文件时按错误处理（不自动造文件）。

首次运行的模板生成是**三类统一**的（都在缺省流程、仅动用户级 `~/.rhinecode/`）：`__main__` 依次调 `config.scaffold_user_config` / `permission.config.scaffold_user_config` / `mcp.config.scaffold_user_config`。三者语义不同——`config.yaml` 必需，本次才生成时引导填 key 后退出；`permissions.yaml` / `mcp.yaml` 可选、模板**全注释**（`yaml.safe_load` 得 `None`、`_load_layer` 返回空集，与无文件等价），静默生成、**不因它们退出**，老用户下次运行会顺带补上。新增 config 模块要接入首次生成，需在其 `config.py` 加 `_CONFIG_TEMPLATE` + `scaffold_user_config` 并在 `__main__` 那段追加一次调用（**成对维护点**）。

权限规则配置（c6，可选，从 `permissions.example.yaml` 复制）：三层 YAML，`allow` / `deny` 列表，每条写成 `Tool(模式)`（如 `Bash(git *)`、`Read(config.yaml)`）。位置与优先语义——

- 用户级 `~/.rhinecode/permissions.yaml`（跨项目默认；首次运行自动生成全注释模板）
- 项目级 `<项目根>/.rhinecode/permissions.yaml`（随仓库走、可提交）
- 本地级 `<项目根>/.rhinecode/permissions.local.yaml`（git 忽略；「永久放行」自动写这里）

三层合并后按 **deny 永远优先**求值（不按层级覆盖）；命令用前缀+glob（`npm:*` 带词边界），文件用 gitignore 风格。危险命令黑名单与路径沙箱是更靠前、不可被规则放开的硬防线。用户级模板首次运行自动生成（全注释=空、fail-safe 行为不变），不必再手动复制 `permissions.example.yaml`。

Skill 定义（c11，可选）：无需任何 YAML 配置，直接放 Markdown 文件即可——
- 项目级 `<项目根>/.rhinecode/skills/`（随仓库走、可提交、团队共享，优先级最高）
- 用户级 `~/.rhinecode/skills/`（跨项目默认）
- 内置 `rhinecode/skills/builtin/`（随包分发的 commit / review / test 三个样板）

同名按上述优先级**整份覆盖**（不做字段合并）。单文件型直接放 `<name>.md`；目录型放一个含 `SKILL.md` 入口的目录，目录内其余文件作为随附资源（模板/示例/脚本/参考文档），其绝对路径与清单会一并注入，模型按清单用绝对路径 `read_file` 读取（这两个目录在工作区外，不支持 glob/grep 枚举）。

文件级 `Read(...)` deny 不只约束 `read_file`：`ConversationManager` 会把权限过滤器注入 `glob_files` / `grep_content`，所以 `deny: Read(config.yaml)` 会同时阻止直接读取、grep 泄露内容、glob 输出路径。永久放行写入 `<项目根>/.rhinecode/permissions.local.yaml` 前必须先成功解析已有文件；如果 YAML 损坏或顶层不是映射，`append_local_allow` 会抛错且不覆盖原文件，上层保留本会话规则作为可用性 fallback。

MCP Server 配置（c7，可选，从 `mcp.example.yaml` 复制或由 `mcp_add_server` 自动写入）：两层 YAML，顶层键 `mcpServers`（`name → 条目`），stdio 型填 `command/args/env`、http 型填 `url/headers`，`env`/`headers` 值支持 `${VAR}` 展开。位置——用户级 `~/.rhinecode/mcp.yaml`（首次运行自动生成全注释模板）、项目级 `<项目根>/.rhinecode/mcp.yaml`，按名字合并、项目级覆盖用户级；自动添加时 `scope=auto` 默认项目级，只有用户明确说“全局/所有项目/以后都用”才写用户级。容错 fail-safe：启动加载时缺失/坏配置跳过并记错误，不阻断启动；自动写入时若目标 YAML 损坏、顶层结构异常或同名配置冲突，不会静默覆盖原文件。用户级模板首次运行自动生成（全注释=空、行为不变），不必再手动复制 `mcp.example.yaml`。

## Spec 驱动开发

开发新功能/章节前使用 `/spec` 技能，协作澄清需求后依次生成 `docs/<章节>/` 下的 `spec.md → plan.md → task.md → checklist.md`，再据此开发与验收。当前主线章节为 `docs/c11/`。

C11 的全部文档收在 `docs/c11/` 一个目录下，进门先读 **`docs/c11/README.md`**（导航 + 「冲突时以谁为准」）。目录分三块：

- **产品能力（Skill 系统）**——顶层 `spec.md` / `plan.md` / `task.md` / `checklist.md` 是 C11 原始设计；`docs/c11/align/` 下同名四份是**对齐 Agent Skills 开放标准的改造**。**两者冲突时以 `align/` 为准**（`allowed-tools` 的语义、命令名来源、执行模式字段、「谁能触发」是否与「在哪执行」正交、白名单笔误是否 fail-fast，这五处都反过来了）。原始那四份**刻意保留**——它们记录「当初为什么那样设计」，删掉等于把这段历史扔了。
- **跨阶段测试设施**——`docs/c11/testing/`，**不占章节号、不属于 Skill 系统**，服务 C2–C11 与未来所有阶段的验收。`brief.md`（需求交底，同时覆盖两期）+ `p0-trace/`（P0 行为记录器）+ `p1-driver/`（P1a 端到端驱动设施）。两期是**并列**关系不是父子。P1b 尚未开工，范围清单在 `p1-driver/spec.md` 末节。
- **验收记录**——`docs/c11/acceptance/` 下四份实跑报告：`skills-c11-live.md`（C11 原始设计，10 场景 43/43）、`skills-align-live.md`（对齐改造，7 场景 29/29）、`trace-p0-e2e.md`（用 P1a 验 P0）、`driver-p1a.md`（P1a 自身）。每条判据分「机器判到了什么」与「据此做的判断」两栏。

C10（斜杠命令系统）、C9（记忆系统）、C8（上下文管理）、C7（MCP 客户端）、C6（五层防御权限系统）、C5（结构化系统提示与缓存策略）、C4（Agent Loop 与 Plan Mode）文档仍保留，用于追溯设计来源。

## 测试

开发完成后优先运行：

```bash
python -m compileall rhinecode tests
python -m unittest discover -s tests
```

当前测试覆盖路径越界防护、四态确认回调、本会话放行登记规则、Plan Mode 完整计划展示、拒绝计划停止、计划获批后仍逐项确认，以及权限系统的命令/路径匹配、危险命令黑名单（含复合命令逐段与 fork 炸弹）、deny 优先求值、配置三层加载与容错、工具规范化映射、四层决策管线、loop 决策接入（被拒不停循环、allow 规则免确认）、`grep_content` / `glob_files` 遵守 `Read(...)` deny、大文件范围读取、损坏本地权限配置不被覆盖等关键行为（`tests/test_perm_*.py`、`tests/test_review_fixes.py`）。

MCP 客户端测试（`tests/test_mcp_*.py`、`tests/test_mcp_auto_config.py`、`tests/test_perm_other_glob.py`）：两层配置合并与 `${VAR}` 展开、JSON-RPC 消息构造与响应分类、stdio 三步会话与按 id 配对（用 `tests/fixtures/mock_mcp_server.py` 端到端起真实子进程）、stderr drain 防阻塞、非法远端工具名规范化且仍调用原始远端名、`CallToolResult→ToolResult` 转换（含 `isError` 与非文本占位、异常兜底不外抛）、单 Server 失败隔离与工具注册进 registry、运行时单 Server 重载、URL/NPM 自动解析、歧义候选处理、YAML 安全写入、Windows `npx.cmd` 兼容、`other` 分支 fnmatch 通配放行。HTTP 传输与真实 Server 端到端留作手测（见 `docs/c7/checklist.md` 场景）。

记忆系统测试（`tests/test_memory_*.py`，c9）：锁原语（原子互斥、释放重取、过期接管、touch 保鲜、父目录缺失 fail-safe）、RHINE.md 加载（三层顺序与来源标注、缺层跳过、include 展开/相对引用文件目录/5 层不展开/循环终止/越界拦截/围栏代码块保留/目标缺失容错、用户层边界）、笔记纯逻辑（frontmatter 往返、未知字段忽略、坏格式返 None、索引行格式、行数与字节双截断不产非法 UTF-8）、会话存档（ID 格式、惰性建档、行级 ts、载入往返含 tool_calls、坏行跳过、结尾/中间不成对丢组、孤儿 tool 丢弃、列表标题截断与锁标记、新鲜锁拒接管/过期锁接管后续写、过期清理跳过锁保护并清孤儿锁、/clear 开新档）、编排（假 provider 断言笔记请求 tools=None、落盘+索引重建+notify、目录锁被占跳过不等待、provider 异常静默记录且 in-flight 清除、in-flight 跳过本轮、高水位只发新增段、notes_enabled=False 不调 LLM、--continue 顺延被锁会话、resume 编号定位+时间跨度提醒取走即清、被锁会话拒绝恢复、/memory 报告字段、索引注入截断、`list_resume_sessions` 结构化列表/全量/编号缓存、`session_id` 属性）、沙箱白名单（注册目录可读、未注册工作区外仍拒、`..` 仍拒、read_file 可读白名单、引擎读放行写仍拒、原工作区语义不变）。真实 LLM 笔记质量与 TUI 端到端 6 场景留作手测（见 `docs/c9/checklist.md`）。

/resume 交互化与历史回放测试（`tests/test_resume_replay.py`）：`build_replay_items` 纯函数（user/assistant/tool 配对、空 content assistant 跳过、缺结果防御兜底、结果首行截断、未知 role 跳过、c10 双内容 user 优先展示非空 `display_content`）、conversation 层领域入口（`resume(None)` 空档返回提示 / 有档返回 `SessionListRequest` / 全部锁定或仅当前会话返回提示、`resume(key)` 成功事件流首个为 `HISTORY` 且快照与存档一致、失败不产 `HISTORY`）。SessionPanel 面板交互与回放渲染的视觉效果留 TUI 手测。

斜杠命令系统测试（`tests/test_command_*.py`，c10）：解析器（空输入/普通消息/首位斜杠分类、正文斜杠不触发、首空白切分与 Tab/换行分隔符、参数外层去空白内部原样含引号/管道/反斜杠、命令字段保留大小写、裸 `/` 仍是斜杠输入）、注册表（规范名/别名/大小写不敏感解析、名称/名称·名称/别名·别名/别名·仅大小写·同命令重复别名五类冲突、`register_many` 原子性失败不留半成品、隐藏命令可执行不可发现、候选顺序稳定且只含规范名（别名不参与补全）、帮助含描述/用法/类型/参数提示）、分发器（空输入零副作用、普通消息回显→发送各一次、未知命令 `/help` 引导且不发 AI、别名与大写命中同一 spec、`CommandInvocation` 字段、必需参数缺失显示用法、处理异常单次本地错误不降级、失败后普通输入仍可用、重复分发确定性）、内置命令（12 条规范名与 8 别名映射、三类分类与批准表一致、Fake Controller 行为——报告/模式刷新/`/clear extra` 仍清空/`/resume`·`/continue` 参数透传/`/exit` 只调退出、`/init` 双内容与工具关闭提示、`/help` 别名等价）、启动接线（同一注册表实例注入 App、冲突退出码 1 且 Provider/工具/MCP/Manager/App 均未创建）、TUI（`CommandHighlighter` 完整命中才着色/参数不着色/正文斜杠不着色、`compose_status_text` 的 `[DEFAULT]`/`[PLAN]` 标记与其它字段保留、Pilot 键盘——Tab 单候选直补/别名前缀补为规范名/带参数提示留空格/多候选稳定菜单/参数区 Tab 不改写/隐藏命令不进菜单仍可执行/菜单回车执行高亮项/未知命令本地提示/`/plan` 状态栏标记切换/`/init` 双内容提交/Esc 关菜单保输入，用 Fake Manager 不触真实 Provider）。旧 TUI 结构回归（`tests/test_tui_keybindings.py`）新增：静态 COMMANDS 已删、提交入口 dispatcher 单入口、命令字符串白名单已删、SessionPanel 直调 `resume_session`、控制器方法面完整。

上下文管理测试（`tests/test_context_*.py`，用假 provider 断言摘要请求不带工具）：近似估算（无锚点全量、有锚点=锚点+增量、越界兜底）、第一层存盘（单结果 / 聚合挑大先存 / user 不动 / 幂等 / 写盘失败保留原文）、第二层纯逻辑（保留边界 snap 到 user 不拆 tool 对、草稿丢弃、重构结构与角色交替、转录渲染）、编排（摘要成功重构并失效锚点、连续失败熔断与复位、`manual_compact` 无阈值——小历史 noop「无可摘要」且不调模型 / 大历史无视余量直接摘要、`before_request` 先 offload 降估算、`status_line` 格式与高亮/熔断标记）。真实 LLM 摘要与 TUI 渲染的端到端 5 场景留作手测（见 `docs/c8/checklist.md`）。

Skill 系统测试（`tests/test_skill_*.py` + `tests/test_perm_turn_grant.py`）：解析器（全部字段可选、无 frontmatter 的纯正文也是合法 Skill、两种键名写法等价、**只有下划线写法触发语义变更告知**、六个无能力字段逐条告知且说清「本版本实际会怎么做」、未登记的未知键静默忽略）、三层扫描（**命令名来自路径**而非 frontmatter、含大写超长的名字不再失败、保留子命令词仍失败、跨层整份覆盖、坏样本不阻断其余）、渲染（清单含 `when_to_use`、`$ARGUMENTS` 全量替换、TRUNCATED 与 DROPPED 可区分、字节截断不产生非法 UTF-8）、**预授权**（标准词汇恒等映射、`Glob`/`Grep` 归 `Read`、内部工具名也收、MCP 名原样放行、无法识别项只警告、**永不产出 deny**；授予/撤销/幂等/叠加；**三条安全护栏**：声明「放行全部命令」仍被第①层黑名单拒、「放行全部写入」仍被第②层沙箱拒、配置里的 deny 仍压过预授权）、可调用性（fork 缺省可被模型发起且工具结果是子对话结论、没注入回调时明确说明而不假装成功、`disable-model-invocation` 挡模型但挡不到用户、`user-invocable: false` 不进菜单但仍在清单里）、编排（幂等激活位置不动、热更新、项目提示零状态、三个内置样板开箱可见，以及**加锁不变量的跨线程死锁护栏**——回调桩另起 daemon 线程读状态并 `join(timeout)`，用完成计数而非布尔标志，同线程版本在 `RLock` 下会静默通过故不可简化）、循环（不传 `options` 与改造前一致、`excluded_tools` 双重生效、**跨轮端到端护栏**：第 1 轮调 `load_skill`、断言第 1 轮 reminder 不含 SOP 而第 2 轮含）、启动接线（**白名单笔误不再让启动失败**——这是本次改造在启动路径上最重要的行为反转，进程内与子进程各有一条护栏钉着）。

Trace 记录器测试（`tests/test_trace_*.py` + `tests/test_bootstrap.py`，共 6 个文件 127 条）：纯函数（十五枚举取值为小写下划线、`clip` 未截断返回裸串 / 截断返回三字段 / 中文按字符切不产乱码、`redact_config` 掩码且容忍字段增减、`agent_event_payload` 有 `text_length` 无 `text`、有 `tool_name` 无 `arguments`、None 键不写入、`default_trace_path` 含毫秒且不建目录）；记录器（序号自增连续、**写入失败与负载异常后序号仍连续**、三类失败均不抛、`close` 幂等、中文不转义落盘、**并发护栏**起 8 线程各 40 条断言行数与序号集合精确、**跨线程死锁护栏**用完成计数而非布尔标志、作用域嵌套恢复与 thread-local 隔离、`create_recorder` 对「父路径是文件」降级为 Null 且反证直接构造确实抛 `OSError`）；装饰器（成对请求响应、**消费方 break 后仍结算且顺序正确**、`tool_names` 与传入 schema 一致、畸形 schema 不搞挂请求、轮次按作用域各自计数、转发的 chunk 与内层**逐个 `is` 相等**、参数原样透传）；装配（进程内可装配、参数全可选、`cleanup` 幂等且 `session_end` 恰好一条、关闭时协调层 provider **不是** `TracingProvider`、三类致命错误文案逐字一致、白名单笔误时 `connect_all` 未被调用、AST 断言工厂内零 `sys.exit`、**四类用户级内容隔离**逐项验证、同进程连续装配不泄漏只读根、子进程实跑三种 `--trace` 形态含不可写路径不阻断）；埋点（六种 `outcome` 各一条 + `out_of_scope` 的物证与两处反证 + `ask_user`/`present_plan` 不产 `tool_execute`、权限决策 `layer`/`decision`、两层压缩字段、四种作用域与轮次不混、`command_dispatch` 恰好四条且 `seq` 小于对应 `ui_message`、`agent_event` 不带重负载、十五类总清点、**AC6 护栏**：用一个 `emit` 必抛的记录器跑含只读并发工具的循环，断言 `ToolResult.ok is True` 且 output 不含「工具执行异常」）；零回归（稳定段结构基线**不逐字固化**——文案微调不是回归、假警报会让人无视断言、开关双跑对比请求/事件流/历史三者逐字节相等、关闭时不产 `traces` 目录、四固定字段顺序契约）；阅读器（五种模式下**文件哈希不变**、十五类摘要函数无遗漏的正向守卫、未登记类型输出显式标记、坏行跳过计数、`[project.scripts]` 只有 `rhine`）。真实 LLM 下的记录质量与 TUI 端到端 9 场景留作手测（见 `docs/c11/testing/p0-trace/checklist.md`）。

端到端驱动设施测试（`tests/test_e2e_*.py`，P1a，共 8 个文件 135 条）：协议（编解码往返含中文与字面 markup、越界错误码抛 `ValueError`、四种面板 choice 校验、`clarify`+`keys` 被拒、非法 JSON 不吞）；发现（名片往返、`unpublish` 幂等、坏名片跳过、`resolve_host` 在 0/1/多个时的三种文案、**连不上立即报陈旧**（阈值按实测校准：Windows 上 OS 自己就要 ~2.03 秒才返回 `ConnectionRefusedError`，判据是「不等满调用方给的超时」）、pid 不符识破端口复用）；沙箱与预置（可丢弃校验两条判据 + **非空目录仍通过**、项目根被拒、清理三步与「cwd 在待删目录内时直接 rmtree 在 Windows 必失败」的反证、六个预置函数落盘位置、指纹稳定性与 mtime 敏感性）；假模型（四类块、按轮次、耗尽兜底不抛错不挂起、`tool_names` 与 `dynamic_reminder` 两个取值口径、8 线程×40 次并发计数精确）；断言层（十一项词汇各「通过一次失败一次」、失败诊断含证据序号与**邻域整行**且确实走 `render_timeline`、坏行计数与 reader 一致、**源码护栏**禁止绕过 reader 自行 `json.loads`）；驱动内核（`run_on_main` 超时真生效、三态判据含「面板挂着时忙碌态仍为真但必须判 PENDING」、两终态与超时诊断字段齐备、取消后以 `user_cancelled` 结束且仍可再 send、轮次预算拦截、四类面板各应答一次并断言循环得以继续、`channel`/`keys` 两种来源、跨线程死锁护栏用完成计数、**`shutdown_on_main` 的就地验证 + 反证**（跳过强制结算就卡住）、**最后一段 AI 正文必被记进 `ui_message` 且工具执行时不重复**（P1a 补掉的 P0 缺口，同样有反证））；宿主（**完整闭环全程不重启**且历史条数取自会话存档、真人提交入口产 `user_input`/`command_dispatch`、`observe` 给出遍历控件取不到的完整 payload、忙碌期查询 <1 秒、挂着面板 quit 也干净退出、空闲超时自退且末条为 `session_end`、装配期致命错误经通道回报文案逐字一致、名片一步连上、陈旧立即失败、指纹随代码变、**四档确认各走一次且 permanent 真写本地配置**、**危险命令即使驱动者放行也在第①层被拦**、被摘工具在两处清单都不见且一致、用户目录在临时目录下、MCP 状态为空、**指定了 `model:` 的独立模式 Skill 仍走假模型且作用域为 `isolated:*`**、预置的 Skill 与 git 历史真实可见、Skill 激活「第 N 轮激活第 N+1 轮生效」、流错误停止、兜底可识别、中文与字面 `[` 全链路不乱码、无残留、进程内白名单与 MCP 连接复位）。真实模式 2 条默认 skip（需 `RHINE_E2E_LIVE=1` 与有效凭据）、「连续起停」慢速专项默认 skip（需 `RHINE_E2E_SLOW=1`）。**环境前置：本机需装 git**（`seed_git_repo` 依赖它，缺失时明确抛错而非静默跳过——静默跳过会让依赖提交历史的场景假绿）。

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

被拒不终止 Agent Loop，结构化拒绝原因回灌模型。**但「用户在面板里选拒绝」是例外中的例外**：它不是技术失败而是人的决定，回灌文案（`DENIED_BY_USER_FEEDBACK`）明确要求模型停止推进、不要重试/改参数/换工具绕过，转而向用户说明意图并询问拒绝原因；且**下一轮硬性不发任何工具**（`_RoundContext.user_denied` → `tools=None`），使模型在物理上只能产出文本。软硬两道缺一不可——实测只留文案时，一个不听劝的模型会把 25 轮迭代全部烧在重试上（用户要连点 25 次拒绝），加上硬约束后 2 轮结束。护栏见 `tests/test_perm_deny_stops_retry.py`。其它注意：

- 沙箱是应用层前缀校验，管得住文件工具，但管不住 `run_command` 跑起来的脚本自己用代码 open 的文件（已知边界，OS 级沙箱留待后续）。
- `config.yaml` 可能包含真实 API Key，请勿提交到版本库；可用 `deny Read(config.yaml)` 规则进一步阻止模型读取，并阻止 grep/glob 间接泄露该文件内容或路径。
- MCP 工具（c7）：远端 Server 是外部程序、不可信，故 MCP 工具一律 `read_only=False`，默认权限模式下每次调用都经人在回路确认；`kind="other"` 会跳过①黑名单与②沙箱（它们针对本地命令/路径），但仍走③规则（`allow: mcp__server__*` 可放行）与④模式兜底。若工具名被规范化，规则要写注册名而不是远端原名。stdio Server 的子进程行为不受路径沙箱约束（与 `run_command` 同属已知边界）；`mcp_add_server` 会写配置并启动外部 MCP，必须保持 `read_only=False` 且先让用户确认；`mcp.yaml` 的 `env`/`headers` 可能含密钥（如 `${API_KEY}`），同样勿提交真实值，也不要让自动解析逻辑生成真实密钥。
- Skill 系统（c11）三条：① **信任模型**——Skill 正文是「发给模型的文本」，可以指挥模型读写文件、执行命令。项目级 Skill 随代码仓库分发，`git pull` 后可能凭空多出几个，因此每次启动都提示「发现 N 个项目级 Skill」且**刻意不做「只提示一次」的持久化**（有状态的话新增时状态不失效，新来的就被静默吞掉）；评审 `.rhinecode/skills/` 应与评审代码同等对待。② **无权限豁免**——Skill 拿不到任何权限捷径，它指挥的每个工具调用照样过五层管线，正文里写「直接执行 rm -rf /」也只会在第①层黑名单被拦下。③ **`allowed-tools` 不是安全边界**——它是「提升模型选对工具准确率」的收窄手段，会因 MCP 未连接而剪枝、因剔空而降级为不收窄、因任一 Skill 未声明而整体塌缩；要限制模型能做什么请用 `permissions.yaml` 的 deny 规则。另：用户级 `~/.rhinecode/skills/` 与内置目录经 path_guard **只读白名单**放行（目录型 Skill 的随附资源在工作区外，模型需按清单读取），与 c9 的 memory 目录同理——只对 read 类判定生效，write/glob/grep 面完全不动。
- 行为记录（trace，测试设施）：**产物比会话存档更敏感**——里面既有完整的模型请求与响应，也有每次工具执行的参数与**输出原文**（被读过的文件内容、命令输出）。如果模型在对话中读过配置文件，那份内容会原样进入 `tool_execute` 事件，**其中可能含明文 API Key**。三条纪律：① 忽略规则要加在**启动 `rhine` 的那个项目**里——trace 产物落在该项目根的 `.rhinecode/traces/` 下，而本仓库 `.gitignore` 的那行只在开发 RhineCode 时生效；去别的项目跑 trace 前，先给那个项目的 `.gitignore` 补上 `.rhinecode/traces/`（**实测过：不补就会被 `git status` 列出来**）。勿提交、勿外传、勿贴进 issue；② `session_start` 的配置快照里 `api_key` 已被固定掩码替换（`redact_config` 是白名单式逐字段取值，新增含密字段默认不记录），但这**只保证配置快照**——工具输出里的泄漏不在它的职责范围内，由 `.gitignore` 兜底；③ 记录器**不改变任何权限判定**，它只观测；`--trace` 不是权限开关，开启它不会让模型多做任何一件事。另：记录失败一律静默（写盘失败、路径不可写、负载序列化异常全被吞掉），这是**有意的**——观测设施绝不能反过来阻断被观测的系统。
- 端到端驱动设施（P1a，测试设施）四条：① **控制通道不鉴权**——它只绑 `127.0.0.1`、只在宿主活着的这段时间存在，任何能在本机跑程序的人都能连上去驱动它。这是刻意接受的取舍（加鉴权会让一个测试设施凭空多出密钥管理），代价是**驱动期间应把本机视为可信环境**；真实模式尤其要注意，那时宿主进程持有你的真实凭据。② **驱动器不扩大权限面**——它替人应答只是换了第⑤层人在回路的执行者，前四层一字不动：驱动者选「放行」的危险命令照样在第①层黑名单被拦下（`test_e2e_host.py` 有专门护栏钉着这条）。③ **`exclude_tools` 摘掉的两个工具是隔离边界的一部分**：`mcp_add_server` 会写**真实**用户主目录且不吃 `user_dir`，`mcp_resolve_server` 虽是 `read_only=True` 却要访问外部包索引——而只读且被放行的工具**根本不弹面板**，应答者拦不住它。改动这个集合前先想清楚隔离还成不成立。④ **宿主的记录产物与 trace 同等敏感**（它就是 trace），落在临时工作区里、随宿主退出一并删除；用 `--keep-workspace` 保留时请自行按上一条的三条纪律处理。
- 记忆系统（c9）：**会话存档含完整对话原文**（含被读过的敏感文件内容、工具输出），`.rhinecode/sessions/`、`.rhinecode/memory/`、`.rhinecode/context/` 已加入 `.gitignore`，勿提交。笔记落盘与索引重建是**内部可信写盘**（同 c8 存盘先例，不经工具权限管线），但写入路径由代码锁死：LLM 只产出 JSON 动作，`filename` 过 `[a-z0-9_-]+\.md` 白名单校验（防路径注入），物理上出不了两个 memory 目录。用户级 `~/.rhinecode/memory/` 经 path_guard **只读白名单**放行——只对 read 类判定生效（`resolve_readable`/`is_readable_path`），write/glob 判定完全不动，不扩大沙箱其它面。笔记 LLM 请求强制 `tools=None`。锁文件只保证「写不坏」，不提供跨实例实时一致性（语义重复笔记靠 LLM 去重收敛）。/init 生成 RHINE.md 走 `write_file` 完整权限管线（人在回路确认后才落盘）。
- 上下文管理（c8）：第一层存盘写入 `<项目根>/.rhinecode/context/<id>.txt` 属**内部可信写盘**，不经工具权限管线（不是模型发起的工具调用）；存盘内容是工具结果原文，可能含被读过的敏感文件片段，故 `.rhinecode/context/` 应随 `.rhinecode/` 一并 git 忽略、勿提交（已在 `.gitignore`）。第二层摘要会把「较早的对话历史（含工具结果）」作为一条 user 转录发给 LLM——与正常对话同样是把上下文交给模型，无额外外泄面，但若用了 `deny Read(敏感文件)`，注意该文件内容一旦已进入历史仍可能被摘要带走（deny 只挡新读取，挡不住已在历史里的内容）。摘要请求强制 `tools=None`，模型在摘要阶段无法调用任何工具。

## 已知后续工程项

以下问题已完成工程审查确认，但不属于当前阶段开发范围。后续章节会集中补齐；在当前阶段不要把它们视为阻塞项，除非用户明确要求处理：

1. API Key 与敏感配置的读取脱敏、环境变量化或工作区外管理。
2. Plan Mode 规划阶段的工具阶段强校验，防止模型同轮夹带副作用工具。**已有真实观测样本**（C11 场景 10 验收时撞到，见 `docs/c11/acceptance/skills-c11-live.md`）：规划阶段那一轮的 `tool_names` 里没有 `run_command`（`_schema_for` 用 `readonly_schemas()` 滤掉了它），模型仍凭先验调了出来，而 `_execute` 的 `out_of_scope` 判据只查 **Skill 白名单**（`_visible(name, policy)`）、不查规划阶段的只读过滤，于是 `outcome=executed`。**这不是 C11 `out_of_scope` 守卫漏了**——两处过滤职责不同。缓解是五层管线一层没少（照样弹确认面板）；这次夹带的恰好是 `git diff`，但同一路径上完全可能是写命令。

3. `write_file` / `edit_file` 的文件系统级原子写入。
4. OS 级沙箱（Seatbelt / bubblewrap），约束 `run_command` 子进程自身发起的文件/网络访问——C6 的应用层黑名单+路径沙箱已覆盖命令与文件工具的常见高危场景，但管不住子进程内部的间接访问。
5. 权限系统后续项：网络请求限制、资源配额、审计日志（C6 spec 明确不做，留待后续章节）。
6. 开发环境依赖固定与 CI，让 `compileall` / `unittest` 在标准环境稳定运行。
7. MCP 后续项（C7 spec 明确不做）：Server 健康检查与自动重连、资源/提示词/采样等非工具能力、MCP 工具的细粒度权限映射与执行超时可配置化、stdio 之外的旧版 HTTP+SSE 传输、MCP 工具结果里图片/二进制内容的实际渲染。
8. 上下文管理后续项（C8 spec 明确不做）：精确 tokenizer（当前仅「锚点+增量」近似估算）、摘要策略的机器学习/质量优化、存盘文件的清理与生命周期管理（`/clear` 只复位幂等状态、不删磁盘文件）、除窗口大小外其它阈值（存盘/保留/余量等）的可配置化、摘要内容的分段/多轮压缩与跨会话持久化。**其中「保留区阈值」有一处已实测确认的局限**（P1a 验 P0 场景 7 时发现）：`summarize.RETAIN_TOKENS = 10000` 与 `auto_margin = 13000` 都是**固定常量、不随 `context_window` 缩放**，因此配置了小窗口（如 8192）的模型上，保留区比整个窗口还大、早段恒为空，**第二层摘要永远不会真正压缩**（实测连续三次 `no_early_segment`），历史只会一路涨到溢出。大窗口（64K 及以上）不受影响。修法需重新设计「保留多少」的语义（按窗口比例？按绝对条数？），不宜顺手改，故登记在此。
9. Skill 系统后续项（C11 spec 明确不做）：Skill 的市场分发与版本管理、嵌套激活（Skill 里再激活 Skill）、参数 schema 与校验、模板引擎（`$ARGUMENTS` 只做字面替换）、多个 Skill 并行执行、跨会话保持激活态、文件监听式自动热更新（当前需显式 `/skills reload`）。
10. Trace 记录器后续项（spec 明确不做）：TUI 驱动器（P1，用 Pilot 无人驱动界面跑完整场景，本轮只做 P0 记录器）、记录文件的自动清理与轮转（`--trace` 每次运行产一个新文件，攒多了要手工删）、实时流式查看（当前只能事后读文件）、可视化时间线、跨运行对比与差异分析、阈值（字段截断 4000 字符 / 消息条数 400）可配置化、采样与按类型开关（当前只有「全开」与「全关」两态）。
11. 记忆系统后续项（C9 spec 明确不做）：向量数据库/RAG 语义检索（召回只靠索引注入 + 按路径读文件）、团队记忆同步/跨机器共享、跨实例实时一致性（锁只保证「写不坏」，语义重复笔记靠 LLM 去重收敛）、笔记自动清理与遗忘机制、各阈值（24h 提醒/30 天过期/索引 200 行/锁 600 秒等）可配置化、存档格式版本迁移工具、存档加密或压缩存储。

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
