# 项目结构

> RhineCode 用户手册 · [返回手册目录](README.md) · [返回项目 README](../../README.md)

## 项目结构

```text
rhinecode/
├── __main__.py          # CLI 入口（只管命令行：参数、模板生成、配置加载、记录器构造）
├── bootstrap.py         # 装配层：build_app 按固定顺序组装应用，致命错误抛 BootstrapError
├── config.py            # YAML 配置加载与校验
├── conversation.py      # 对话管理、领域方法、权限引擎构建、Agent 回调封装、上下文/记忆/Skill 接线
├── presets.py           # auto / plan 两个运行模式预设（「权限档 × 规划阶段」的固定组合）
├── agent/
│   ├── events.py        # AgentEvent / StopReason / ConfirmDecision 等事件类型
│   ├── collector.py     # StreamCollector 双路收集
│   ├── loop.py          # Agent Loop 核心（含权限决策预扫接入点）
│   ├── plan_tools.py    # ask_user / present_plan 特殊工具 schema
│   ├── clarify.py       # 澄清提问的问卷数据结构与可见性判据（ask-user 扩展）
│   ├── gate.py          # 子 Agent 等待闸门的**协议**与 NullGate（c13）
│   ├── cache_log.py     # 缓存命中调试日志
│   └── prompt/          # 结构化系统提示：模块拼装、环境信息、system-reminder 注入
├── classifier/          # 命令与网络的分类器审查（c16，叶子包）
│   ├── models.py        # 审查请求 / 结论 / 作用域等值对象
│   ├── prompt.py        # 两阶段提示词（一个词的快速过滤 + 带理由的复核）
│   ├── parse.py         # 分类器输出解析（解析不出来一律按拒绝）
│   ├── breaker.py       # 两种熔断：拦截熔断与失败熔断（计数器**刻意不按 scope 分桶**）
│   ├── cache.py         # 同一动作的判定缓存
│   ├── broad.py         # 过宽命令放行规则的识别（入参收成两个字符串以保住叶子性质）
│   ├── render.py        # 给用户的完整理由 vs 给模型的固定文案
│   ├── session.py       # 一次会话内的审查状态
│   └── service.py       # 门面（review **绝不外抛异常**——它跑在决策预扫里）
├── todo/                # 主对话的待办清单（todo-list 扩展，叶子包）
│   ├── models.py        # 待办项与状态枚举（**刻意不复用 team 的那份**）
│   ├── store.py         # 整表覆写；校验在写入之前全部跑完，绝不留半张表
│   └── render.py        # 显示决策：钉在历史区内部底端、占位不浮起
├── team/                # 子 Agent 协作（c15，叶子包）
│   ├── models.py        # 看板任务 / 消息 / 花名册条目 / 两个枚举 / 三个上限
│   ├── board.py         # 共享任务看板（原子认领、依赖强制、环检测）
│   ├── roster.py        # 花名册、待命历史保管、超限回收、信箱原语
│   ├── mailbox.py       # 消息路由与投递（锁内写、锁外唤醒）
│   ├── gate.py          # TeamGate —— 队友消息的注入闸门
│   ├── identity.py      # 线程本地的「现在是谁在调用」
│   ├── render.py        # 注入消息标记块 / 看板文本 / 组队说明
│   └── service.py       # 门面（协作能力的唯一入口）
├── trace/               # 行为记录（跨阶段测试设施，叶子包、只依赖标准库）
│   ├── models.py        # 三十二类事件枚举、六种作用域、脱敏/白名单纯函数（**无截断**）
│   ├── recorder.py      # TraceRecorder（锁内序列化+落盘+序号）/ NullRecorder / create_recorder
│   ├── tracing_provider.py  # Provider 装饰器：记录每次请求与响应（唯一分层例外）
│   └── reader.py        # 只读 CLI 阅读器（python -m rhinecode.trace.reader）
├── permission/          # 五层防御权限系统（纯逻辑，与 TUI/Provider 解耦）
│   ├── models.py        # Decision / PermissionMode / Layer / Rule / PermissionRequest 等数据结构
│   ├── matching.py      # 命令拆分、命令/路径模式匹配
│   ├── blacklist.py     # ①危险命令黑名单
│   ├── rules.py         # ③规则 deny 优先求值
│   ├── config.py        # 三层 YAML 加载/容错/回写
│   ├── adapter.py       # 工具调用规范化为 PermissionRequest（收口工具知识）
│   ├── network.py       # ②′网络边界层：硬校验 + 域名策略（判定期与连接期共用同一份实现）
│   ├── protected.py     # ②″保护路径：写配置类文件必须过人眼（纯函数，基准是调用者的 cwd）
│   ├── render.py        # 判定结果与拒绝理由的文本渲染
│   └── engine.py        # PermissionEngine.decide 组装决策管线（含④层分类器接入点）
├── web/                 # 网络抓取、抽取与搜索（web-fetch / web-search 扩展）
│   ├── models.py        # FetchOutcome / ExtractOutcome / 搜索结果值对象
│   ├── decode.py        # 字节→文本：响应头 → 文档内 <meta> → 兜底 UTF-8
│   ├── convert.py       # HTML→纯文本（标准库 HTMLParser，不引入新依赖）、截断
│   ├── fetcher.py       # 抓取 + 逐跳硬校验 + 重定向控制 + 体量/时长上限
│   ├── extract.py       # 预算计算（随 context_window 缩放）、抽取提示、结果解析
│   ├── render.py        # 不可信标记 + 元信息渲染 + TUI 摘要
│   ├── manager.py       # WebFetchManager：本包唯一持 provider、唯一编排副作用
│   ├── search.py        # 搜索服务商适配（PROVIDERS 表）
│   ├── search_manager.py # 会话级配额（缺省 50 次，/clear 复位）与调用编排
│   └── search_render.py # 搜索结果渲染与「完整不截断」的确认面板文案
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
│   ├── memories.py      # 记忆 frontmatter 解析 / 渲染 / 索引重建与截断
│   ├── memory_updater.py # 记忆 LLM 的 Prompt 与 JSON 响应解析（文件名白名单）
│   └── manager.py       # MemoryManager：启动 / 注入 / 存档 / 异步记忆 / resume 编排
├── hooks/               # Hook 系统（c12）：事件 + 条件 + 动作
│   ├── models.py        # 十二个事件枚举 / 两张字段表 / 全部数据类（零 I/O）
│   ├── conditions.py    # 四种匹配形态 + all/any 组合（零 I/O）
│   ├── parser.py        # YAML → HookRule + 七项集中校验（零 I/O）
│   ├── config.py        # 两层文件定位、加载、模板生成
│   ├── actions.py       # 四种动作执行器（命令过①黑名单，HTTP 过②′硬校验）
│   ├── manager.py       # 分发 / once / 结论合并 / 注入队列 / 统计 / 埋点
│   └── report.py        # /hooks 报告与项目级启动提示（零 I/O）
├── subagents/           # 子 Agent 系统（c13）：角色 + 任务表 + 运行器
│   ├── models.py        # AgentSpec / AgentCatalog / 常量表 / builtin_agents_dir
│   ├── parser.py        # frontmatter → AgentSpec（纯函数，不碰文件系统）
│   ├── discovery.py     # 三层扫描与同名覆盖，全程 fail-safe
│   ├── toolset.py       # 分层工具过滤（安全边界排在用户配置之前）
│   ├── tasks.py         # TaskManager（线程安全；刻意不持有任何回调）
│   ├── runner.py        # 在独立线程里跑完一个子 Agent
│   ├── service.py       # 委派门面（永不阻塞 → 多个委派天然并行）
│   ├── gate.py          # 等待闸门的实现（协议在 agent/gate.py）
│   ├── render.py        # 角色清单注入文本
│   ├── report.py        # /agents 报告
│   └── builtin/         # 内置角色（explorer / planner / general-purpose）
├── worktree/            # 隔离工作区（c14，叶子包；**全项目唯一执行 git 的地方**）
│   ├── models.py        # WorktreeHandle / ChangeStatus / ProvisionEntry 等值对象
│   ├── naming.py        # 名字校验与生成（**校验必须先于任何路径拼接**）
│   ├── gitcmd.py        # git 子进程封装（建 / 查 / 删 / prune / merge-base）
│   ├── provision.py     # 环境初始化：copy / link 清单，单条失败只警告不中断
│   ├── lifecycle.py     # 创建、快速恢复、三层过滤的删除判定与执行
│   ├── cleanup.py       # 启动时扫过期工作区，三种结局
│   └── render.py        # 交付信息段与启动清理提示
├── skills/              # Skill 系统（c11，纯逻辑 + 单点接入）
│   ├── models.py        # 枚举 / frozen 数据类 / 常量 / builtin_skills_dir
│   ├── parser.py        # 单份文本 → SkillSpec（纯函数，不碰文件系统）
│   ├── discovery.py     # 三层扫描 + 层内去重 + 跨层整份覆盖
│   ├── render.py        # 全部「给模型看的文本」：清单/参数替换/正文/资源清单
│   ├── validation.py    # allowed-tools 声明 → 权限规则（预授权，纯函数）
│   ├── audit.py         # Skill 体检：七项检查、纯函数零 IO，只观测不改判定
│   ├── manager.py       # SkillManager：状态与副作用编排（四段式加锁纪律）
│   └── builtin/         # 随包分发的样板：commit.md / review.md / test.md / skill-creator/
├── commands/            # 斜杠命令注册与分发（c10）
│   ├── models.py        # 枚举 / CommandSpec / CommandController 协议
│   ├── parser.py        # 输入分类与首空白切分（纯函数）
│   ├── registry.py      # CommandRegistry：索引 / 冲突校验 / 补全 / 帮助
│   ├── dispatcher.py    # CommandDispatcher：分流、回显、未知命令引导
│   ├── skill_commands.py # SkillCommandInfo → CommandSpec（c11）
│   └── builtins.py      # 15 条内置命令 + 别名 + INIT_PROMPT
├── context/             # 上下文两层压缩（c8，纯逻辑 + 单点接入）
│   ├── models.py        # CompactionNotice / ContextStats 数据类
│   ├── estimate.py      # 近似估算纯函数（锚点 + 增量）
│   ├── offload.py       # Offloader：第一层工具结果存盘（单结果/聚合、幂等）
│   ├── summarize.py     # 第二层纯逻辑：保留边界/转录/摘要 Prompt/解析/重构
│   └── manager.py       # ContextManager：编排两层压缩、锚点、熔断、可观测
├── provider/
│   ├── base.py          # BaseProvider / Message / StreamChunk / ToolCall 抽象
│   ├── deepseek.py      # DeepSeek 流式对话、思考与工具调用解析（走 OpenAI 兼容协议）
│   └── factory.py       # Provider 工厂（只认 deepseek，旧协议值给迁移提示）
├── tools/               # ⚠ tools/__init__.py **必须保持为空**，否则包级互依会成环
│   ├── base.py          # Tool / ToolResult 抽象
│   ├── diff.py          # 结构化 diff 构造
│   ├── display.py       # 工具调用在界面上的摘要行渲染
│   ├── registry.py      # 工具注册中心
│   ├── path_guard.py    # 路径守卫（沙箱层复用）。**c14 起 root 必传、无默认值**，
│   │                    #   边界按调用者的工作目录算 —— 这是隔离的物理实现
│   ├── read_file.py     # 读文件（大文件必须显式行范围）
│   ├── write_file.py    # 新建或覆盖，生成 diff
│   ├── edit_file.py     # 用唯一匹配的原文片段精确替换
│   ├── run_command.py   # 执行 shell 命令（子进程按黑名单过滤敏感环境变量）
│   ├── glob_files.py    # glob 找文件（排除隔离工作区与运行期产物）
│   ├── grep_content.py  # 正则搜内容（同上）
│   ├── load_skill.py    # 两阶段加载的第二阶段（system_serial 强制串行）
│   ├── run_agent.py     # 委派工具（system_serial；plan_safe，规划阶段只许全只读角色）
│   ├── send_message.py  # 点对点消息（c15）
│   ├── team_tasks.py    # 共享任务看板四件套（c15）
│   ├── todo_write.py    # 主对话待办清单的整表覆写（**子 Agent 拿不到这个工具**）
│   ├── web_fetch.py     # 网络抓取（委托 web/manager.py）
│   ├── web_search.py    # 网络搜索（委托 web/search_manager.py）
│   └── mcp_config.py    # mcp_resolve_server / mcp_add_server 内置工具
└── tui/
    ├── app.py           # Textual App 主类
    ├── clipboard.py     # 选中文本的复制（有选区时 Ctrl+C 是复制，且不计入退出连按）
    └── widgets.py       # HistoryView / InputBar / StatusBar / StatusLine / ActivityView /
                         #   ToolBatchWidget / 确认·澄清·会话选择面板。
                         #   ⚠ markup 转义必须用本文件的 escape，绝不用 rich 那版
```

