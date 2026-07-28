# 当前能力细节

> 本文件是 `CLAUDE.md` 的分册，**按需读取**，不随会话自动注入。
> 主文件只保留索引与「必须不请自来」的内容，细节在这里。

> 逐条描述各章能力的**实际行为与边界**（阈值、降级路径、哪些 Provider 生效）。
> 主文件的能力表只回答「有什么」，这里回答「具体怎么表现」。

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
