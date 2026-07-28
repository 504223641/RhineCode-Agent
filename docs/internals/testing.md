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

当前测试覆盖路径越界防护、四态确认回调、本会话放行登记规则、Plan Mode 完整计划展示、拒绝计划停止、计划获批后仍逐项确认，以及权限系统的命令/路径匹配、危险命令黑名单（含复合命令逐段与 fork 炸弹）、deny 优先求值、配置三层加载与容错、工具规范化映射、四层决策管线、loop 决策接入（被拒不停循环、allow 规则免确认）、`grep_content` / `glob_files` 遵守 `Read(...)` deny、大文件范围读取、损坏本地权限配置不被覆盖等关键行为（`tests/test_perm_*.py`、`tests/test_review_fixes.py`）。

MCP 客户端测试（`tests/test_mcp_*.py`、`tests/test_mcp_auto_config.py`、`tests/test_perm_other_glob.py`）：两层配置合并与 `${VAR}` 展开、JSON-RPC 消息构造与响应分类、stdio 三步会话与按 id 配对（用 `tests/fixtures/mock_mcp_server.py` 端到端起真实子进程）、stderr drain 防阻塞、非法远端工具名规范化且仍调用原始远端名、`CallToolResult→ToolResult` 转换（含 `isError` 与非文本占位、异常兜底不外抛）、单 Server 失败隔离与工具注册进 registry、运行时单 Server 重载、URL/NPM 自动解析、歧义候选处理、YAML 安全写入、Windows `npx.cmd` 兼容、`other` 分支 fnmatch 通配放行。HTTP 传输与真实 Server 端到端留作手测（见 `docs/c7/checklist.md` 场景）。

记忆系统测试（`tests/test_memory_*.py`，c9）：锁原语（原子互斥、释放重取、过期接管、touch 保鲜、父目录缺失 fail-safe）、RHINE.md 加载（三层顺序与来源标注、缺层跳过、include 展开/相对引用文件目录/5 层不展开/循环终止/越界拦截/围栏代码块保留/目标缺失容错、用户层边界）、笔记纯逻辑（frontmatter 往返、未知字段忽略、坏格式返 None、索引行格式、行数与字节双截断不产非法 UTF-8）、会话存档（ID 格式、惰性建档、行级 ts、载入往返含 tool_calls、坏行跳过、结尾/中间不成对丢组、孤儿 tool 丢弃、列表标题截断与锁标记、新鲜锁拒接管/过期锁接管后续写、过期清理跳过锁保护并清孤儿锁、/clear 开新档）、编排（假 provider 断言笔记请求 tools=None、落盘+索引重建+notify、目录锁被占跳过不等待、provider 异常静默记录且 in-flight 清除、in-flight 跳过本轮、高水位只发新增段、notes_enabled=False 不调 LLM、--continue 顺延被锁会话、resume 编号定位+时间跨度提醒取走即清、被锁会话拒绝恢复、/memory 报告字段、索引注入截断、`list_resume_sessions` 结构化列表/全量/编号缓存、`session_id` 属性）、沙箱白名单（注册目录可读、未注册工作区外仍拒、`..` 仍拒、read_file 可读白名单、引擎读放行写仍拒、原工作区语义不变）。真实 LLM 笔记质量与 TUI 端到端 6 场景留作手测（见 `docs/c9/checklist.md`）。

/resume 交互化与历史回放测试（`tests/test_resume_replay.py`）：`build_replay_items` 纯函数（user/assistant/tool 配对、空 content assistant 跳过、缺结果防御兜底、结果首行截断、未知 role 跳过、c10 双内容 user 优先展示非空 `display_content`）、conversation 层领域入口（`resume(None)` 空档返回提示 / 有档返回 `SessionListRequest` / 全部锁定或仅当前会话返回提示、`resume(key)` 成功事件流首个为 `HISTORY` 且快照与存档一致、失败不产 `HISTORY`）。SessionPanel 面板交互与回放渲染的视觉效果留 TUI 手测。

斜杠命令系统测试（`tests/test_command_*.py`，c10）：解析器（空输入/普通消息/首位斜杠分类、正文斜杠不触发、首空白切分与 Tab/换行分隔符、参数外层去空白内部原样含引号/管道/反斜杠、命令字段保留大小写、裸 `/` 仍是斜杠输入）、注册表（规范名/别名/大小写不敏感解析、名称/名称·名称/别名·别名/别名·仅大小写·同命令重复别名五类冲突、`register_many` 原子性失败不留半成品、隐藏命令可执行不可发现、候选顺序稳定且只含规范名（别名不参与补全）、帮助含描述/用法/类型/参数提示）、分发器（空输入零副作用、普通消息回显→发送各一次、未知命令 `/help` 引导且不发 AI、别名与大写命中同一 spec、`CommandInvocation` 字段、必需参数缺失显示用法、处理异常单次本地错误不降级、失败后普通输入仍可用、重复分发确定性）、内置命令（12 条规范名与 8 别名映射、三类分类与批准表一致、Fake Controller 行为——报告/模式刷新/`/clear extra` 仍清空/`/resume`·`/continue` 参数透传/`/exit` 只调退出、`/init` 双内容与工具关闭提示、`/help` 别名等价）、启动接线（同一注册表实例注入 App、冲突退出码 1 且 Provider/工具/MCP/Manager/App 均未创建）、TUI（`CommandHighlighter` 完整命中才着色/参数不着色/正文斜杠不着色、`compose_status_text` 的 `[DEFAULT]`/`[PLAN]` 标记与其它字段保留、Pilot 键盘——Tab 单候选直补/别名前缀补为规范名/带参数提示留空格/多候选稳定菜单/参数区 Tab 不改写/隐藏命令不进菜单仍可执行/菜单回车执行高亮项/未知命令本地提示/`/plan` 状态栏标记切换/`/init` 双内容提交/Esc 关菜单保输入，用 Fake Manager 不触真实 Provider）。旧 TUI 结构回归（`tests/test_tui_keybindings.py`）新增：静态 COMMANDS 已删、提交入口 dispatcher 单入口、命令字符串白名单已删、SessionPanel 直调 `resume_session`、控制器方法面完整。

上下文管理测试（`tests/test_context_*.py`，用假 provider 断言摘要请求不带工具）：近似估算（无锚点全量、有锚点=锚点+增量、越界兜底）、第一层存盘（单结果 / 聚合挑大先存 / user 不动 / 幂等 / 写盘失败保留原文）、第二层纯逻辑（保留边界 snap 到 user 不拆 tool 对、草稿丢弃、重构结构与角色交替、转录渲染）、编排（摘要成功重构并失效锚点、连续失败熔断与复位、`manual_compact` 无阈值——小历史 noop「无可摘要」且不调模型 / 大历史无视余量直接摘要、`before_request` 先 offload 降估算、`status_line` 格式与高亮/熔断标记）。真实 LLM 摘要与 TUI 渲染的端到端 5 场景留作手测（见 `docs/c8/checklist.md`）。

Skill 系统测试（`tests/test_skill_*.py` + `tests/test_perm_turn_grant.py`）：解析器（全部字段可选、无 frontmatter 的纯正文也是合法 Skill、两种键名写法等价、**只有下划线写法触发语义变更告知**、六个无能力字段逐条告知且说清「本版本实际会怎么做」、未登记的未知键静默忽略）、三层扫描（**命令名来自路径**而非 frontmatter、含大写超长的名字不再失败、保留子命令词仍失败、跨层整份覆盖、坏样本不阻断其余）、渲染（清单含 `when_to_use`、`$ARGUMENTS` 全量替换、TRUNCATED 与 DROPPED 可区分、字节截断不产生非法 UTF-8）、**预授权**（标准词汇恒等映射、`Glob`/`Grep` 归 `Read`、内部工具名也收、MCP 名原样放行、无法识别项只警告、**永不产出 deny**；授予/撤销/幂等/叠加；**三条安全护栏**：声明「放行全部命令」仍被第①层黑名单拒、「放行全部写入」仍被第②层沙箱拒、配置里的 deny 仍压过预授权）、可调用性（fork 缺省可被模型发起且工具结果是子对话结论、没注入回调时明确说明而不假装成功、`disable-model-invocation` 挡模型但挡不到用户、`user-invocable: false` 不进菜单但仍在清单里）、编排（幂等激活位置不动、热更新、项目提示零状态、三个内置样板开箱可见，以及**加锁不变量的跨线程死锁护栏**——回调桩另起 daemon 线程读状态并 `join(timeout)`，用完成计数而非布尔标志，同线程版本在 `RLock` 下会静默通过故不可简化）、循环（不传 `options` 与改造前一致、`excluded_tools` 双重生效、**跨轮端到端护栏**：第 1 轮调 `load_skill`、断言第 1 轮 reminder 不含 SOP 而第 2 轮含）、启动接线（**白名单笔误不再让启动失败**——这是本次改造在启动路径上最重要的行为反转，进程内与子进程各有一条护栏钉着）。

Trace 记录器测试（`tests/test_trace_*.py` + `tests/test_bootstrap.py`，共 7 个文件 134 条）：纯函数（十五枚举取值为小写下划线、`clip` 未截断返回裸串 / 截断返回三字段 / 中文按字符切不产乱码、`redact_config` 掩码且容忍字段增减、`agent_event_payload` 有 `text_length` 无 `text`、有 `tool_name` 无 `arguments`、None 键不写入、`default_trace_path` 含毫秒且不建目录）；记录器（序号自增连续、**写入失败与负载异常后序号仍连续**、三类失败均不抛、`close` 幂等、中文不转义落盘、**并发护栏**起 8 线程各 40 条断言行数与序号集合精确、**跨线程死锁护栏**用完成计数而非布尔标志、作用域嵌套恢复与 thread-local 隔离、`create_recorder` 对「父路径是文件」降级为 Null 且反证直接构造确实抛 `OSError`）；装饰器（成对请求响应、**消费方 break 后仍结算且顺序正确**、`tool_names` 与传入 schema 一致、畸形 schema 不搞挂请求、轮次按作用域各自计数、转发的 chunk 与内层**逐个 `is` 相等**、参数原样透传）；装配（进程内可装配、参数全可选、`cleanup` 幂等且 `session_end` 恰好一条、关闭时协调层 provider **不是** `TracingProvider`、三类致命错误文案逐字一致、白名单笔误时 `connect_all` 未被调用、AST 断言工厂内零 `sys.exit`、**四类用户级内容隔离**逐项验证、同进程连续装配不泄漏只读根、子进程实跑三种 `--trace` 形态含不可写路径不阻断）；埋点（六种 `outcome` 各一条 + `out_of_scope` 的物证与两处反证 + `ask_user`/`present_plan` 不产 `tool_execute`、权限决策 `layer`/`decision`、两层压缩字段、四种作用域与轮次不混、`command_dispatch` 恰好四条且 `seq` 小于对应 `ui_message`、`agent_event` 不带重负载、十五类总清点、**AC6 护栏**：用一个 `emit` 必抛的记录器跑含只读并发工具的循环，断言 `ToolResult.ok is True` 且 output 不含「工具执行异常」）；零回归（稳定段结构基线**不逐字固化**——文案微调不是回归、假警报会让人无视断言、开关双跑对比请求/事件流/历史三者逐字节相等、关闭时不产 `traces` 目录、四固定字段顺序契约）；阅读器（五种模式下**文件哈希不变**、十五类摘要函数无遗漏的正向守卫、未登记类型输出显式标记、坏行跳过计数、`[project.scripts]` 只有 `rhine`）。真实 LLM 下的记录质量与 TUI 端到端 9 场景留作手测（见 `docs/c11/testing/p0-trace/checklist.md`）。

端到端驱动设施测试（`tests/test_e2e_*.py`，P1a，共 8 个文件 143 条）：协议（编解码往返含中文与字面 markup、越界错误码抛 `ValueError`、四种面板 choice 校验、`clarify`+`keys` 被拒、非法 JSON 不吞）；发现（名片往返、`unpublish` 幂等、坏名片跳过、`resolve_host` 在 0/1/多个时的三种文案、**连不上立即报陈旧**（阈值按实测校准：Windows 上 OS 自己就要 ~2.03 秒才返回 `ConnectionRefusedError`，判据是「不等满调用方给的超时」）、pid 不符识破端口复用）；沙箱与预置（可丢弃校验两条判据 + **非空目录仍通过**、项目根被拒、清理三步与「cwd 在待删目录内时直接 rmtree 在 Windows 必失败」的反证、六个预置函数落盘位置、指纹稳定性与 mtime 敏感性）；假模型（四类块、按轮次、耗尽兜底不抛错不挂起、`tool_names` 与 `dynamic_reminder` 两个取值口径、8 线程×40 次并发计数精确）；断言层（十一项词汇各「通过一次失败一次」、失败诊断含证据序号与**邻域整行**且确实走 `render_timeline`、坏行计数与 reader 一致、**源码护栏**禁止绕过 reader 自行 `json.loads`）；驱动内核（`run_on_main` 超时真生效、三态判据含「面板挂着时忙碌态仍为真但必须判 PENDING」、两终态与超时诊断字段齐备、取消后以 `user_cancelled` 结束且仍可再 send、轮次预算拦截、四类面板各应答一次并断言循环得以继续、`channel`/`keys` 两种来源、跨线程死锁护栏用完成计数、**`shutdown_on_main` 的就地验证 + 反证**（跳过强制结算就卡住）、**最后一段 AI 正文必被记进 `ui_message` 且工具执行时不重复**（P1a 补掉的 P0 缺口，同样有反证））；宿主（**完整闭环全程不重启**且历史条数取自会话存档、真人提交入口产 `user_input`/`command_dispatch`、`observe` 给出遍历控件取不到的完整 payload、忙碌期查询 <1 秒、挂着面板 quit 也干净退出、空闲超时自退且末条为 `session_end`、装配期致命错误经通道回报文案逐字一致、名片一步连上、陈旧立即失败、指纹随代码变、**四档确认各走一次且 permanent 真写本地配置**、**危险命令即使驱动者放行也在第①层被拦**、被摘工具在两处清单都不见且一致、用户目录在临时目录下、MCP 状态为空、**指定了 `model:` 的 fork Skill 仍走假模型且作用域为 `isolated:*`**、预置的 Skill 与 git 历史真实可见、Skill 激活「第 N 轮激活第 N+1 轮生效」、流错误停止、兜底可识别、中文与字面 `[` 全链路不乱码、无残留、进程内白名单与 MCP 连接复位）。真实模式 2 条默认 skip（需 `RHINE_E2E_LIVE=1` 与有效凭据）、「连续起停」慢速专项默认 skip（需 `RHINE_E2E_SLOW=1`）。**环境前置：本机需装 git**（`seed_git_repo` 依赖它，缺失时明确抛错而非静默跳过——静默跳过会让依赖提交历史的场景假绿）。

涉及 TUI 行为时，再用 tmux 或真实终端做端到端测试：

1. 在 tmux 中启动 RhineCode
2. 输入一段真实的对话请求
3. 观察 RhineCode 是否正确调用工具、生成回复
4. 对照对应章节的 `checklist.md` 逐项验收
