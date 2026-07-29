# 当前能力细节

> 本文件是 `CLAUDE.md` 的分册，**按需读取**，不随会话自动注入。
> 主文件只保留索引与「必须不请自来」的内容，细节在这里。

> 逐条描述各章能力的**实际行为与边界**（阈值、降级路径、哪些 Provider 生效）。
> 主文件的能力表只回答「有什么」，这里回答「具体怎么表现」。

- **Skill 系统**（c11，已对齐 **Agent Skills 开放标准**）：独立 `skills/` 层（对标 permission/context/memory/commands：纯逻辑 + 单点接入，不依赖 Textual）。单个 Skill = 可选的 YAML frontmatter（`name`/`description`/`when_to_use`/`allowed-tools`/`context`/`disable-model-invocation`/`user-invocable`/`model`，**全部可选**）+ Markdown SOP 正文；一份只有正文的 `.md` 也是合法 Skill，说明从正文第一段提取。连字符与下划线两种键名写法等价。支持单文件型（`x.md`）与目录型（含 `SKILL.md` 入口 + 随附资源）。

  **命令名来自文件系统路径**（目录名 / 去扩展名的文件名），`name` 只是显示标签——这是「外部 Skill 原样可用」的地基：从 Claude Code 或 Codex 拉一个目录丢进 `.rhinecode/skills/` 就能用，不必检查也不必修改 frontmatter。**三级存放**：项目 > 用户 > 内置，同名整份覆盖。

  **两阶段加载**：启动时清单（命令名 + `description` + `when_to_use`）进 priority 140 稳定槽位；模型调 `load_skill` 后完整正文进 priority 120 动态槽位，**每轮重建**、多个可同时激活、重复激活幂等。正文超单体上限截断（TRUNCATED）、超总量上限整段丢弃（DROPPED），措辞不同且对用户可见。

  **「在哪执行」与「谁能触发」是正交两维**（C11 曾把它们捆在一起）：`context: fork` 开子对话跑完只回流结论（主历史**恰好新增两条配对消息**）；`disable-model-invocation` 决定模型能否自行发起；`user-invocable: false` 则不进斜杠菜单但模型仍可发起。子对话固定只带那条自包含调用消息，工具集排除 `load_skill` 防嵌套。

  **`allowed-tools` 是预授权，不是收窄**：列出的操作在**本次执行内**免于人工确认，**不限制**模型能调用什么。取值词汇与 `permissions.yaml` 的规则名一致（`Bash(git *)` / `Read` / `Write` / `Edit`，标准里的 `Glob`/`Grep` 映射到 `Read`），实现为权限引擎第③层的 `turn_rules`，排在①黑名单②沙箱**之后**——因此翻不过前两层。用户发出下一条消息即失效。无法识别的项跳过 + 警告，**不 fail-fast**（外部 Skill 里出现 `Task` / `TodoWrite` 这类名字是正常现象；`WebFetch` 自 web_fetch 扩展起是**真工具**，可以写，但括号里必须是 `domain:` 前缀）。

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
- **结构化系统提示**：八个固定提示模块走稳定可缓存通道（第八个「外部不可信内容」随 web_fetch 扩展加入、按 `web_fetch_enabled` 开关注入，关闭时输出与加入前逐字一致），环境信息与 Plan Mode 提醒通过 `<system-reminder>` 作为动态补充注入。
- **网络访问**（web_fetch 扩展，不占章节号）：给一个 http/https 地址与一段「要提取什么」的说明，取回正文并按提问抽取要点。**实际行为与边界**：

  **权限**：新增**②′网络边界层**，排在②路径沙箱之后、③可配置规则**之前**。它做两件事——
  ① **结构性硬校验**（不可被任何配置或权限模式放开）：协议只许 http/https（`file://` 会绕过路径沙箱）、拒绝内嵌凭据、拒绝非全局可路由地址（判据是**白名单式**的 `not is_global` + 显式还原 IPv4-mapped/6to4/NAT64 + 补判组播与保留段——黑名单式枚举实测漏 CGNAT `100.64.0.1`、组播 `224.0.0.1`、6to4 封环回 `2002:7f00:1::` 三类）；② **域名策略求值**：deny 优先 → allow 命中放行 → **白名单已建立但未命中即拒**。

  **规则语法**：`WebFetch(domain:模式)`，与 `Bash(...)`/`Read(...)` 写在同一份 YAML 的同一组 allow/deny。`example.com` 不含子域；`*.example.com` 任意深度子域但不含裸域；`*` 全匹配；**非前导位置的 `*` 不跨点**（`example.*` 匹配 `example.org` 但不匹配 `example.evil.com`——跨点会让一条本意放行「各国域名」的规则连带放行攻击者可注册的域名）。写坏时按效果分两支、**两支都偏严**：allow 整条丢弃、deny **降级为整工具拒绝**。

  **⚠ 白名单只由「用户级 / 项目级」两层建立。** 在那两处写下任何一条 `allow: WebFetch(domain:...)`，就等于声明「只许访问这些」——此后未列出的域名一律被直接拒绝，`/perm` 切到放行档也翻不过来。而**本地级**（确认面板选「永久放行」自动写入的那份）、会话级、Skill 预授权**只放行、不建立白名单**。这是本项目对「层级不决定优先级」的**唯一一处例外**，理由：不区分的话，用户点一次「永久放行」就等于建立了只含一个域名的白名单，其它域名从「弹确认」变成「硬拒且永不再问」且界面上无从恢复；且 Skill 的 `allowed-tools` 会反向**收紧**，违反「只放宽从不收紧」这条既有承诺。
  **必须知道的一处不直觉**：本地级也是可手编的个人策略文件。同一条规则写进 `permissions.yaml` 会建立白名单、写进 `permissions.local.yaml` 不会。**要建立白名单，请写用户级或项目级。**

  **权限模式的例外**：放行档对 URL 类请求**不生效**（未建立白名单时仍弹确认）。理由是放行档对文件工具的后果被②沙箱兜住，而 URL 类在未建白名单时没有等价边界。连带后果：**无人值守场景下没有免确认的网络访问路径**，除非写文件级域名规则。

  **抓取行为**：只 GET（无请求体、无自定义头、无认证）；跨主机重定向**不跟随**（返回目标地址由模型再发一次，使每一跳都过完整判定），「同主机」判据是 `(scheme, host, port)` 三元组全等；**每一跳都重跑完整硬校验 + 域名解析结果校验**（否则 `https://a/x → http://user:pass@a/` 这类跳转会让协议与凭据限制对新地址失效）；单跳超时 30 秒、同主机跳数上限 5（即最多 6 次请求、上界约 180 秒——但**域名解析不在该上界内**，标准库解析函数无超时参数、`Esc` 也打断不了）；响应体上限 5 MiB；二进制类型**先看响应头就不读正文**，只回报类型与体量；字符集推断链为「响应头 → 文档内 `<meta>`（只扫前 2KB）→ 兜底 UTF-8」。

  **抽取与降级**：抓回正文截断到 `window // 4` 字符（夹在 4 000–100 000，**随 `context_window` 缩放**）后，发一次**独立的**模型请求按提问摘要（强制 `tools=None`、不进主历史、trace 作用域 `web_extract`）。抽取答案另有 8 000 字符上限（防被注入的页面诱导超长输出绕过前两道体量约束）。抽取超时/异常/空返回一律**降级**为「本地转换后的正文节选」（8 000 字符，折算后低于 C8 的单结果存盘线），并在结果里如实注明「这是原文节选而不是回答」。

  **不可信标注**：工具结果的正文——**无论抽取成功还是降级**——一律包在 `<untrusted-content source="...">` 里，元信息（来源/最终地址/内容类型/两类截断标志/是否降级）在标记**外**。「抽取不是消毒」：抽取模型读的是同一张可能被注入的页面，它完全可能把伪装成指令的文本如实转述出来。

  **总开关**：`config.yaml` 的 `web_fetch_enabled`（缺省 true）。关闭后工具不注册、系统提示不含那条约束、域名语法不校验也不产生警告——行为与本扩展之前逐字一致。

工具调用、Plan Mode 与权限系统目前仅在 `protocol: deepseek` 且启用默认工具注册中心时可用；MCP 工具随内置工具一同仅在该模式下暴露。
