# 测试覆盖清单

> 本文件是 `CLAUDE.md` 的分册，**按需读取**，不随会话自动注入。
> 主文件只保留索引与「必须不请自来」的内容，细节在这里。

> 逐层记录「测过什么、哪条护栏钉住了什么」。其中夹着若干**「这条护栏为什么
> 不能简化」**的说明——想动某条测试之前先在这里搜一下它，很多看起来啰嗦的写法
> 是踩过坑之后刻意保留的。

开发完成后优先运行：

```bash
python -m compileall rhinecode tests
python -m unittest discover -s tests
```

## 覆盖概览（按层）

下表这几层的测试是**成体系的行为枚举**——覆盖面广但每条都直白，需要细节直接读
测试文件，文件名已列在表里。**下面「逐条留存」那几节不同**，那里夹着「这条护栏
为什么不能简化」的说明，删不得也压不得。

| 层 | 测试文件 | 覆盖要点 | 留作手测的部分 |
| --- | --- | --- | --- |
| 权限系统 | `test_perm_*.py`、`test_review_fixes.py` | 命令/路径匹配、危险命令黑名单（复合命令逐段与 fork 炸弹）、deny 优先求值、三层配置加载与容错、工具规范化映射、四层决策管线、被拒不停循环、`grep_content`/`glob_files` 遵守 `Read(...)` deny、大文件范围读取、损坏本地配置不被覆盖 | — |
| Plan Mode | `test_perm_*.py`、`test_plan_stage_guard.py` | 完整计划展示、拒绝计划停止、获批后仍逐项确认；**规划阶段夹带副作用工具被拦**（含放行模式下也挡得住、获批后放行两条反证） | — |
| MCP 客户端 | `test_mcp_*.py`、`test_mcp_auto_config.py`、`test_perm_other_glob.py` | 两层配置合并与 `${VAR}` 展开、JSON-RPC 消息构造与响应分类、stdio 三步会话与按 id 配对（起真实子进程）、stderr drain 防阻塞、非法远端名规范化但仍调原名、`CallToolResult→ToolResult` 转换、单 Server 失败隔离、运行时重载、URL/NPM 自动解析、YAML 安全写入、Windows `npx.cmd`、`other` 分支 fnmatch 通配 | HTTP 传输与真实 Server 端到端（`docs/c7/checklist.md`） |
| 上下文管理 | `test_context_*.py` | 近似估算（无锚点/有锚点/越界兜底）、第一层存盘（挑大先存、user 不动、幂等、写盘失败保留原文）、第二层纯逻辑（边界 snap 到 user 不拆 tool 对、草稿丢弃、重构结构）、编排（摘要成功失效锚点、连续失败熔断与复位、`manual_compact` 无阈值）、**保留区与余量随窗口缩放**（含 64K 零回归与小窗口端到端判据） | 真实 LLM 摘要与 TUI 渲染 5 场景（`docs/c8/checklist.md`） |
| 记忆系统 | `test_memory_*.py` | 锁原语（原子互斥/过期接管/touch 保鲜）、RHINE.md 三层加载与 `@include`（嵌套上限/防环/越界拦截/围栏代码块）、笔记 frontmatter 往返与索引双截断、会话存档（惰性建档/容错载入丢组/锁标记/过期清理）、编排（笔记请求 `tools=None`/锁被占跳过/高水位增量/`--continue` 顺延被锁会话）、沙箱只读白名单（读放行而写仍拒） | 真实 LLM 笔记质量与 TUI 6 场景（`docs/c9/checklist.md`） |
| `/resume` 与回放 | `test_resume_replay.py` | `build_replay_items` 纯函数（配对、空 content 跳过、缺结果兜底、结果首行截断、双内容 user 优先 `display_content`）、领域入口三态、成功事件流首个为 `HISTORY` 且快照与存档一致 | SessionPanel 交互与回放视觉 |
| 网络访问（web_fetch 扩展） | `test_perm_match_domain.py`、`test_perm_network_layer.py`、`test_perm_allow_rule.py`、`test_perm_rule_loading.py`、`test_web_*.py` | 域名通配四语义（含 `example.*` 不跨点的反证）、硬校验（协议/凭据/CGNAT/组播/6to4/IPv4-mapped 四类漏网）、白名单**来源层区分**（session/local 不建立白名单）、放行档例外与写文件对照组、**判定顺序护栏**、deny 写坏降级为整工具拒绝、`_load_layer` 三处整层降级的反证、字符集推断链（GBK 页面）、HTML 转换（script 的 CDATA 语义 vs svg 嵌套）、逐跳硬校验与同主机三元组、抽取预算随窗口缩放、抽取答案上限、降级路径同样被不可信标记包裹、`tools=None`、trace 作用域包住整个迭代、面板完整地址不截断且方括号真过 Textual 解析、开关两条传递链 | 真实模型下的注入抵抗（`docs/extensions/web-fetch/checklist.md` 场景 5） |
| TUI 渲染 | `test_tui_markup_escape.py`、`test_tui_tool_pending.py` | markup 转义（**落单 `[` 必须转义**，含真实崩溃现场重演与「旧口径确实会崩」的反证）；工具行两阶段（Provider **在流中途**播报 `tool_pending`——判据是「拿到播报时上游被拉了几个块」而不是事件先后，后者在缺陷复现时照样会绿；收集器不把播报累积进 `tool_calls`；pending 行转执行态**复用同一行且重置计时**；`TOOL_RESULT` 用 `pop` 摘除，**反证：改回 `get` 会让绿色「完成」被收尾逻辑覆写成「未执行」**；取消时留下的行被收尾；拒绝路径仍补得上参数） | 真实模型下长文件生成时的观感 |
| Hook 系统 | `test_hook_*.py`（8 个文件） | 条件求值四形态（含「`git *` 不命中 `github-cli`」证明确实复用了命令匹配算法、通用 glob 用 `fnmatchcase` 保证跨平台确定、**缺失字段时反向匹配也不成立**）、七项加载校验与两处整层降级、四种动作（**三条安全反证**：危险命令被①黑名单拦下且子进程未启动 / `{"decision":"allow"}` 不产生放行语义 / 四类禁止地址请求未发出）、编排（零命中不构造负载、`once` 键用 `source#index` 而非 `name`、结论按最严合并、fail-closed 仅在 `pre_tool_use`、**加锁不变量用两线程+完成计数构造**）、Agent Loop 集成（**四条安全反证**：ASK 翻不过黑名单/沙箱、Hook 判 DENY 时 `engine.decide` 调用数为 0、不表态时事件流与无 Hook 逐字一致、六种「没执行」分支逐一钉住）、十二个分发点的结构护栏 + 真实 `build_app` 读真实 `hooks.yaml`、`/hooks` 报告（含 `[` 的命令串**真走一次 Textual 布局计算**）、缺省零行为（含四个纯模块的**导入级** I/O 禁令） | 真实模型下的拦截反应质量与注入生效（`docs/c12/checklist.md` 场景 9/10） |
| 斜杠命令 | `test_command_*.py`、`test_tui_keybindings.py` | 解析器（分类/首空白切分/参数原样保留/大小写不敏感）、注册表（五类冲突、`register_many` 原子性、隐藏命令可执行不可发现、候选只含规范名）、分发器（回显恰好一次、未知命令不进 AI、必需参数校验、处理异常不降级）、14 条规范命令与 8 别名、启动接线冲突退出码 1、Pilot 键盘（Tab 补全/菜单回车/参数区不拦截/高亮） | — |

## 逐条留存

以下三节**刻意保留原有粒度**。判据是警告密度：上表那几层的描述里没有一处
「为什么不能这么改」，而下面这三节里有几十处——那些句子是踩过坑之后写下的，
压缩掉就等于把教训丢了。

Skill 系统测试（`tests/test_skill_*.py` + `tests/test_perm_turn_grant.py`）：解析器（全部字段可选、无 frontmatter 的纯正文也是合法 Skill、两种键名写法等价、**只有下划线写法触发语义变更告知**、六个无能力字段逐条告知且说清「本版本实际会怎么做」、未登记的未知键静默忽略）、三层扫描（**命令名来自路径**而非 frontmatter、含大写超长的名字不再失败、保留子命令词仍失败、跨层整份覆盖、坏样本不阻断其余）、渲染（清单含 `when_to_use`、`$ARGUMENTS` 全量替换、TRUNCATED 与 DROPPED 可区分、字节截断不产生非法 UTF-8）、**预授权**（标准词汇恒等映射、`Glob`/`Grep` 归 `Read`、内部工具名也收、MCP 名原样放行、无法识别项只警告、**永不产出 deny**；授予/撤销/幂等/叠加；**三条安全护栏**：声明「放行全部命令」仍被第①层黑名单拒、「放行全部写入」仍被第②层沙箱拒、配置里的 deny 仍压过预授权）、可调用性（fork 缺省可被模型发起且工具结果是子对话结论、没注入回调时明确说明而不假装成功、`disable-model-invocation` 挡模型但挡不到用户、`user-invocable: false` 不进菜单但仍在清单里）、编排（幂等激活位置不动、热更新、项目提示零状态、三个内置样板开箱可见，以及**加锁不变量的跨线程死锁护栏**——回调桩另起 daemon 线程读状态并 `join(timeout)`，用完成计数而非布尔标志，同线程版本在 `RLock` 下会静默通过故不可简化）、循环（不传 `options` 与改造前一致、`excluded_tools` 双重生效、**跨轮端到端护栏**：第 1 轮调 `load_skill`、断言第 1 轮 reminder 不含 SOP 而第 2 轮含）、启动接线（**白名单笔误不再让启动失败**——这是本次改造在启动路径上最重要的行为反转，进程内与子进程各有一条护栏钉着）。

Skill 召回率测试（R 系列）：`test_skill_render.py` 钉住**清单表头的三个要件**（动手前先扫清单 / 命中则替代默认做法 / 代价不对称）与「用户不必点名」，另有三条钉住**超预算降级**（每个名字都存活 / 内置先于项目被削 / 降级提示要告诉模型「名字看着相关就直接加载试试」）；`test_skill_manager.py` 钉住 `load_skill` 的工具描述**不得再声称 fork Skill 只能由用户触发**（那句过期说法等于让模型以为一半的 Skill 碰不了）且含与表头同口径的四句。**给一段提示词文本加测试是刻意的**——它是模型决定「要不要用 Skill」时读的唯一两处文本，写坏了编译不报错、测试全绿、界面正常，只是模型的行为悄悄少了一半。

Skill 体检测试（`tests/test_skill_audit.py`，作者期扩展，48 条）：八项检查各自的命中、不命中与边界（阈值取等号时**不**命中、`description` 按字符数不按字节数）；**四条反向用例**，它们比正向的更容易在日后被「顺手补全」掉——① 裸写只读类别**不报**（反了的话三个既有内置样板立刻集体触发）、② 无 frontmatter 的纯正文 Skill **不报任何建议**（那是对齐改造刻意支持的用法，报了会淹掉真正要紧的几条）、③ 预授权部分认得部分认不出时**不报**「全军覆没」、④ 项目级盖用户级**不报**「覆盖内置」；两条措辞护栏（说明过长的建议必须含「不会省下清单预算」、缺占位符的建议必须含「不会丢」——这两句缺了会让用户按建议改完发现没用、或把「位置不对」误读成「功能失效」）；MCP 专项（具体工具名**不报**、带通配符才报、且建议**必须**含「不要加参数模式」——引擎对 `other` 分支要求 pattern 为空，照「加模式」改会让预授权永远匹配不上，那是**建议本身把用户配置改坏**）；注入上限量的是**渲染后的整段**（有一条专门造「正文未达阈值 + 满额资源清单后越线」的目录型 Skill）；纯函数护栏（`entry_path` 指向不存在的文件仍跑得通 = 零 IO 的直接证据、重复调用结果全等）。**全程不创建任何文件**，定义一律用字符串字面量构造。

配套地，`tests/test_skill_parser.py` 有一条**关键反向用例**：作者显式写下的 `description` **恰好与正文第一段逐字相同**时，`description_explicit` 仍须为真。它钉住的是「**不许**用『重新提取正文第一段再比对』代替这个标记」——那种启发式在此场景下会把一份完全正确的 Skill 误判成「作者没写说明」。**没有这条用例，把标记删掉改成比对，测试会全绿。**

⚠️ **内置样板零建议刻意不建自动化断言**（spec F6）。硬判据会让日后内置样板的正常调整（改一句说明、加一条预授权）被一条与该次改动毫无关系的测试拦住，而人被拦住时的第一反应是改判据而不是改样板——护栏于是既碍事又无效。实现时逐一跑过一遍并记进验收记录即可；`BuiltinSamplesTest` 只钉「有哪几个样板、各自的执行模式、`skill-creator` 是目录型且手册不在正文里」，不钉建议数。

Trace 记录器测试（`tests/test_trace_*.py` + `tests/test_bootstrap.py`，共 7 个文件 134 条）：纯函数（十五枚举取值为小写下划线、`clip` 未截断返回裸串 / 截断返回三字段 / 中文按字符切不产乱码、`redact_config` 掩码且容忍字段增减、`agent_event_payload` 有 `text_length` 无 `text`、有 `tool_name` 无 `arguments`、None 键不写入、`default_trace_path` 含毫秒且不建目录）；记录器（序号自增连续、**写入失败与负载异常后序号仍连续**、三类失败均不抛、`close` 幂等、中文不转义落盘、**并发护栏**起 8 线程各 40 条断言行数与序号集合精确、**跨线程死锁护栏**用完成计数而非布尔标志、作用域嵌套恢复与 thread-local 隔离、`create_recorder` 对「父路径是文件」降级为 Null 且反证直接构造确实抛 `OSError`）；装饰器（成对请求响应、**消费方 break 后仍结算且顺序正确**、`tool_names` 与传入 schema 一致、畸形 schema 不搞挂请求、轮次按作用域各自计数、转发的 chunk 与内层**逐个 `is` 相等**、参数原样透传）；装配（进程内可装配、参数全可选、`cleanup` 幂等且 `session_end` 恰好一条、关闭时协调层 provider **不是** `TracingProvider`、三类致命错误文案逐字一致、白名单笔误时 `connect_all` 未被调用、AST 断言工厂内零 `sys.exit`、**四类用户级内容隔离**逐项验证、同进程连续装配不泄漏只读根、子进程实跑三种 `--trace` 形态含不可写路径不阻断）；埋点（六种 `outcome` 各一条 + `out_of_scope` 的物证与两处反证 + `ask_user`/`present_plan` 不产 `tool_execute`、权限决策 `layer`/`decision`、两层压缩字段、四种作用域与轮次不混、`command_dispatch` 恰好四条且 `seq` 小于对应 `ui_message`、`agent_event` 不带重负载、十五类总清点、**AC6 护栏**：用一个 `emit` 必抛的记录器跑含只读并发工具的循环，断言 `ToolResult.ok is True` 且 output 不含「工具执行异常」）；零回归（稳定段结构基线**不逐字固化**——文案微调不是回归、假警报会让人无视断言、开关双跑对比请求/事件流/历史三者逐字节相等、关闭时不产 `traces` 目录、四固定字段顺序契约）；阅读器（五种模式下**文件哈希不变**、十五类摘要函数无遗漏的正向守卫、未登记类型输出显式标记、坏行跳过计数、`[project.scripts]` 只有 `rhine`）。真实 LLM 下的记录质量与 TUI 端到端 9 场景留作手测（见 `docs/c11/testing/p0-trace/checklist.md`）。

端到端驱动设施测试（`tests/test_e2e_*.py`，P1a，共 9 个文件 151 条）：协议（编解码往返含中文与字面 markup、越界错误码抛 `ValueError`、四种面板 choice 校验、`clarify`+`keys` 被拒、非法 JSON 不吞）；发现（名片往返、`unpublish` 幂等、坏名片跳过、`resolve_host` 在 0/1/多个时的三种文案、**连不上立即报陈旧**（阈值按实测校准：Windows 上 OS 自己就要 ~2.03 秒才返回 `ConnectionRefusedError`，判据是「不等满调用方给的超时」）、pid 不符识破端口复用）；沙箱与预置（可丢弃校验两条判据 + **非空目录仍通过**、项目根被拒、清理三步与「cwd 在待删目录内时直接 rmtree 在 Windows 必失败」的反证、六个预置函数落盘位置、指纹稳定性与 mtime 敏感性）；假模型（四类块、按轮次、耗尽兜底不抛错不挂起、`tool_names` 与 `dynamic_reminder` 两个取值口径、8 线程×40 次并发计数精确）；断言层（十一项词汇各「通过一次失败一次」、失败诊断含证据序号与**邻域整行**且确实走 `render_timeline`、坏行计数与 reader 一致、**源码护栏**禁止绕过 reader 自行 `json.loads`）；驱动内核（`run_on_main` 超时真生效、三态判据含「面板挂着时忙碌态仍为真但必须判 PENDING」、两终态与超时诊断字段齐备、取消后以 `user_cancelled` 结束且仍可再 send、轮次预算拦截、四类面板各应答一次并断言循环得以继续、`channel`/`keys` 两种来源、跨线程死锁护栏用完成计数、**`shutdown_on_main` 的就地验证 + 反证**（跳过强制结算就卡住）、**最后一段 AI 正文必被记进 `ui_message` 且工具执行时不重复**（P1a 补掉的 P0 缺口，同样有反证）、**笔记更新通知必被记进 `ui_message`**（全阶段复测 O5 补掉的缺口：原先 `_notify_memory` 绕过埋点，使 c9 AC19 在任何 trace 验收里都是盲区）——三条护栏：正向产出、那一行确实出现在历史区、以及**顺序护栏**「渲染失败时不得留下记录」（mock `call_from_thread` 抛异常模拟退出竞态；**前两条只覆盖正常路径，把埋点挪到渲染之前照样全绿**，两个方向都已实测））；宿主（**完整闭环全程不重启**且历史条数取自会话存档、真人提交入口产 `user_input`/`command_dispatch`、`observe` 给出遍历控件取不到的完整 payload、忙碌期查询 <1 秒、挂着面板 quit 也干净退出、空闲超时自退且末条为 `session_end`、装配期致命错误经通道回报文案逐字一致、名片一步连上、陈旧立即失败、指纹随代码变、**四档确认各走一次且 permanent 真写本地配置**、**危险命令即使驱动者放行也在第①层被拦**、被摘工具在两处清单都不见且一致、用户目录在临时目录下、MCP 状态为空、**指定了 `model:` 的 fork Skill 仍走假模型且作用域为 `isolated:*`**、预置的 Skill 与 git 历史真实可见、Skill 激活「第 N 轮激活第 N+1 轮生效」、流错误停止、兜底可识别、中文与字面 `[` 全链路不乱码、无残留、进程内白名单与 MCP 连接复位）。瘦客户端（`test_e2e_client.py`，全阶段复测缺陷 D1 的护栏）：`send_command` 把「宿主没了」的**两种 TCP 形态**（正常退出的 EOF、被强杀的 RST）翻译成同一条可读提示，含 `ConnectionAbortedError`、**收到半截数据后才 RST 仍不得退化成 `JSONDecodeError`**、以及正常路径分块到达不受影响。**反证是结构性的**：断言 `RuntimeError`，而 `ConnectionResetError` 继承 `OSError`——删掉 `except` 当场红。这类缺陷单测里天然不可见（只在真人强杀时出现），不钉住就要等下一次全阶段复测才发现。真实模式 2 条默认 skip（需 `RHINE_E2E_LIVE=1` 与有效凭据）、「连续起停」慢速专项默认 skip（需 `RHINE_E2E_SLOW=1`）。**环境前置：本机需装 git**（`seed_git_repo` 依赖它，缺失时明确抛错而非静默跳过——静默跳过会让依赖提交历史的场景假绿）。

涉及 TUI 行为时，再用 tmux 或真实终端做端到端测试：

1. 在 tmux 中启动 RhineCode
2. 输入一段真实的对话请求
3. 观察 RhineCode 是否正确调用工具、生成回复
4. 对照对应章节的 `checklist.md` 逐项验收
