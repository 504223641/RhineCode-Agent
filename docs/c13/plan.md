# C13 子 Agent 系统 Plan

> 本文对应已批准的 `spec.md`。每个模块段末标注它兑现哪几条 F 需求。

## 架构概览

新增一个叶子偏上的包 `rhinecode/subagents/`，加一个工具、一条 TUI 呈现链、一个斜杠命令。

```
                    ┌──────────────────────────────────────┐
   模型调用委派工具  │  tools/run_agent.py（RunAgentTool）  │
      ────────────▶ │  system_serial=True，串行段执行       │
                    └───────────────┬──────────────────────┘
                                    │ delegate()
                    ┌───────────────▼──────────────────────┐
                    │  subagents/service.py（SubAgentService）│
                    │  组合：目录 + 任务表 + 运行器          │
                    └──┬──────────┬──────────┬─────────────┘
                       │          │          │
        ┌──────────────▼──┐ ┌─────▼──────┐ ┌─▼────────────────┐
        │ discovery/parser │ │  tasks.py  │ │    runner.py     │
        │ 三层扫描→AgentCatalog│ │ TaskManager│ │ 独立线程跑 Agent │
        └──────────────────┘ └─────┬──────┘ └─┬────────────────┘
                                   │          │ 复用
                                   │      ┌───▼──────────────────┐
                                   │      │ agent.loop.Agent.run │
                                   │      │ permission / hooks /  │
                                   │      │ context / trace       │
                                   │      └───────────────────────┘
                                   │
        ┌──────────────────────────▼────────────────────────────┐
        │ TUI 定时轮询（set_interval，主线程）                    │
        │ ① 完成通知行 ② 状态栏运行数 ③ 无任何跨线程 widget 写入 │
        └───────────────────────────────────────────────────────┘
```

**包名用 `subagents`（复数）而不是 `agents`**：`rhinecode/agent/`（单数）已经是 Agent Loop 引擎包，
两者只差一个字母会长期造成误读，`from rhinecode.agent import ...` 与
`from rhinecode.agents import ...` 在 review 时几乎分辨不出来。

**依赖方向**：`subagents` 依赖 `agent` / `provider` / `permission` / `context` / `hooks` / `trace` / `tools`；
`tools/run_agent.py` 反过来依赖 `subagents`。这形成 `tools ↔ subagents` 的**包级互相依赖**，
与既有的 `tools ↔ skills`、`tools ↔ mcp` 同型——**不成环的唯一依靠仍是
`rhinecode/tools/__init__.py` 保持为空**。本章因此多出第四个依赖方。

---

## 核心数据结构

### AgentSpec（`subagents/models.py`）

一个角色定义的解析结果，不可变。

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `name` | str | 角色标识。frontmatter 的 `name`，缺省取文件名（去扩展名） |
| `description` | str | 必填。主 Agent 选择角色的唯一依据 |
| `body` | str | 正文，即该角色的系统提示 |
| `tools` | `Optional[tuple[str, ...]]` | 白名单。`None` = 未声明 = 继承主对话工具集 |
| `disallowed_tools` | `tuple[str, ...]` | 黑名单 |
| `model` | `Optional[str]` | `None` 或 `"inherit"` 归一为 `None` |
| `max_turns` | int | 缺省 `DEFAULT_MAX_TURNS`，夹在 `[1, HARD_MAX_TURNS]` |
| `permission_mode` | `Optional[PermissionMode]` | `None` = inherit |
| `source` | `AgentSource` | PROJECT / USER / BUILTIN |
| `path` | Path | 定义文件路径，报告用 |
| `warnings` | `tuple[str, ...]` | 未支持字段、越界被夹的数值等 |

常量：`DEFAULT_MAX_TURNS = 15`（与 `SKILL_MAX_ITERATIONS` 同值，同一个理由：
子任务该聚焦）、`HARD_MAX_TURNS = 25`（不得超过主对话的 `MAX_ITERATIONS`）、
`MAX_CONCURRENT = 3`、`FOREGROUND_TIMEOUT = 60.0`。

`UNSUPPORTED_FIELDS`：`skills` / `memory` / `isolation` / `color` / `hooks` /
`mcp_servers` / `background` / `effort` —— 登记表，遇到就产生一条**具名**警告
（照 C11 `UNSUPPORTED_FIELDS` 的先例，而不是笼统地说「有未知字段」）。

> ⚠ **成对维护点**：新增支持的 frontmatter 字段 → `models.py` 的 `AgentSpec` 字段
> +（若无对应能力）`UNSUPPORTED_FIELDS` + `parser.py` 的读取与归一 + `report.py` 的展示。

### AgentCatalog（`subagents/models.py`）

`specs: dict[str, AgentSpec]`（生效的角色，键为 `name`）、
`errors: tuple[AgentLoadError, ...]`、`shadowed: tuple[tuple[str, AgentSource, Path], ...]`
（被覆盖或同层重名而未生效的定义，供 `/agents` 展示——不静默丢弃，见 spec F3）。

### TaskRecord / TaskStatus（`subagents/tasks.py`）

```
TaskStatus = RUNNING | COMPLETED | FAILED | CANCELLED
```

| 字段 | 说明 |
| --- | --- |
| `task_id` | 短标识（6 位十六进制），模型与用户都用它指代任务 |
| `kind` | `role` / `branch` |
| `agent_name` | 角色名；分支式为 `"(branch)"` |
| `task_text` | 任务描述原文 |
| `status` | 上表四态 |
| `started_at` / `finished_at` | 时间戳 |
| `turns` | 已用轮次（运行中也在更新） |
| `usage_tokens` | 累计 token |
| `conclusion` | 结论文本 |
| `stop_reason` | 结束原因 |
| `delivered` | 结论是否已交付主历史（F21 用） |
| `cancel_event` | `threading.Event`，取消信号 |
| `done_event` | `threading.Event`，前台等待用 |
| `backgrounded` | 是否已转后台（前台等待方置位，用于「转后台后不要再回填工具结果」） |

### SubAgentRuntime（`subagents/runner.py`）

运行一个子 Agent 所需的全部外部依赖，由协调层一次性构造后注入。
**做成显式数据类而不是让 runner 反向依赖 `conversation`**，否则 `subagents` 会依赖协调层，
破坏「上层可依赖下层，反之不可」。

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `provider_for` | `Callable[[Optional[str]], BaseProvider]` | 模型名 → Provider，复用协调层已有的缓存 |
| `registry` | `ToolRegistry` | 共享 |
| `engine` | `PermissionEngine` | **主引擎**，运行器据它派生子引擎 |
| `hooks` | HookManager | 共享 |
| `recorder` | TraceRecorder | 共享（写入端已加锁，作用域是 `threading.local`） |
| `new_context_manager` | `Callable[[], ContextManager]` | **每个子 Agent 一个新实例**，理由见「技术决策」 |
| `environment_text` | `Callable[[], str]` | 环境信息段 |
| `untrusted_section` | str | 「外部不可信内容」段原文（F7 的例外用） |
| `parent_snapshot` | `Callable[[], ParentSnapshot]` | 分支式用：父历史副本 + 父 stable 提示 |
| `default_model` | str | 主对话模型名 |

---

## 模块设计

### `subagents/parser.py` —— 单文件解析（F1）

**职责**：文本 → `AgentSpec`。复用 C11 的 frontmatter 切分思路，
键名归一（连字符 → 下划线、大小写不敏感），逐字段校验与夹取。

**对外接口**：`parse_agent(text, path, source, fallback_name) -> AgentSpec`，
解析失败抛 `AgentParseError`（由 discovery 转成 `AgentLoadError`，不外泄）。

要点：
- `description` 缺失 → 解析失败（spec F1 唯一必填项）；
- `name` 缺失 → 用 `fallback_name`（文件名）；`name` 含 `/`、`\`、空白或为空 → 失败；
- `tools` / `disallowed_tools` 支持逗号分隔字符串与 YAML 列表两种写法；
- `max_turns` 非整数或越界 → 夹到合法区间并记警告，不失败；
- `permission_mode` 不认识的取值 → 忽略并记警告，按 inherit 处理；
- 出现在 `UNSUPPORTED_FIELDS` 里的键 → 具名警告。

**依赖**：`models.py`、`yaml`。

### `subagents/discovery.py` —— 三层扫描（F2/F3）

**职责**：三个目录 → `AgentCatalog`。全程 fail-safe。

**对外接口**：`discover_agents(project_dir, user_dir, builtin_dir) -> AgentCatalog`。

- 只认 `*.md`，其它文件静默跳过（同 C11：目录里放 README 不该刷错误）；
- `sorted(iterdir())` 保证扫描顺序确定 → 同层重名时「字典序靠前者生效」是确定行为；
- 层内先合并（重名记 `shadowed` + 一条警告），再按 项目 > 用户 > 内置 覆盖
  （被覆盖者也进 `shadowed`）；
- 目录不存在 = 空层，不是错误。

**依赖**：`parser.py`、`models.py`。

### `subagents/toolset.py` —— 分层工具过滤（F13/F14）

**职责**：纯函数，无 IO。

**对外接口**：

```
GLOBAL_DENIED_TOOLS: frozenset[str]      # 委派工具、Skill 加载工具
BACKGROUND_DENIED_TOOLS: frozenset[str]  # 本章为空集（spec F13 第 3 层）

resolve_toolset(all_tools, spec, is_background) -> ToolsetResult
```

`ToolsetResult` = `(allowed: frozenset[str], unresolved: tuple[str, ...], reason: str)`。
`unresolved` 是 `tools` 里没有对应工具的名字（F14 的失败说明要用它）。

顺序固定：**全局禁止 → 角色白名单交集 → 角色黑名单 → 后台附加层**。

Plan Mode 的两个特殊工具（提问 / 提交计划）不在注册中心里，而是循环按 `plan_mode`
注入的——因此不靠本函数排除，靠**运行器一律传 `plan_mode=False`**（见 runner）。

> ⚠ **成对维护点**：新增「任何子 Agent 都不该看到」的工具 → 加进 `GLOBAL_DENIED_TOOLS`。
> 漏改不报错，只会让子 Agent 多出一个能力，且界面上完全看不出来。

### `subagents/tasks.py` —— 后台任务管理器（F18/F20/F22）

**职责**：任务表的唯一持有者，线程安全。

**对外接口**：

| 方法 | 说明 |
| --- | --- |
| `create(kind, agent_name, task_text) -> TaskRecord` | 分配 id 并登记为 RUNNING |
| `running_count() -> int` | 并发上限判据（F20） |
| `running_brief() -> str` | 超限时回灌用：当前在跑的是哪几个 |
| `finish(task_id, status, conclusion, stop_reason)` | 运行器收尾调用 |
| `bump(task_id, turns=None, tokens=None)` | 运行中更新计数 |
| `snapshot() -> tuple[TaskRecord, ...]` | 只读快照，供 `/agents` 与轮询 |
| `take_deliverables() -> tuple[TaskRecord, ...]` | 取走「已完成且未交付」的，置 `delivered` |
| `drain_notifications() -> tuple[TaskRecord, ...]` | 取走「已完成且未通知」的，供 TUI 出通知行 |
| `cancel(task_id) -> bool` / `cancel_all() -> int` | 置取消信号 |

> ⚠ **加锁不变量（与 C11 `SkillManager`、C12 `HookManager` 同一条）**：
> 临界区**只做纯内存读写**，一切回调、埋点、跨线程调度在锁外。
> 本类刻意不持有任何回调，从结构上杜绝违反。

`take_deliverables` 与 `drain_notifications` 是**两条独立的消费线**（各有各的标志位）：
通知给用户看、交付给模型看，两者时机不同——通知在完成的那一刻，交付要等到主对话下一次发请求。
合成一条会导致「用户还没看到通知，模型已经引用了结论」或反之。

### `subagents/runner.py` —— 子 Agent 运行器（F7/F8/F10/F11/F12/F15/F16/F17/F26）

**职责**：在**独立线程**里跑完一个子 Agent。

**对外接口**：`run_subagent(runtime, spec_or_branch, task_text, record) -> None`
（无返回值，结果全部写进 `record`）。

执行步骤：

1. `recorder.bind_scope(subagent_scope(name))`——本线程从现在起属于该作用域。
   作用域是 `threading.local`，因此并发的多个子 Agent 互不干扰。
2. 埋 `subagent_start`。
3. **组装系统提示**（F7）：
   - 定义式：`stable = 角色正文`；`dynamic = 环境信息`（+ 工具集含网络访问工具时追加不可信内容段）。
   - 分支式：`stable` 与历史都取父快照。
4. **派生权限引擎**（F16/F17）：`engine.derive(mode=min(主档, 角色档))`——
   新实例共享 `file_ruleset` / `policy_ruleset` / `session_rules`（同一个列表对象，只读使用），
   **`turn_rules` 为空**。绝不修改主引擎的 `mode`。
5. **非交互 ask 回调**（F15）：恒返回 False。配合 `RunOptions.interactive=False`
   让循环使用子 Agent 专用的回灌文案且**不触发 `deny_cooldown`**。
6. 构造 `Agent(sub_provider, registry, recorder=runtime.recorder, hooks=runtime.hooks)`——
   **`hooks` 必须传**，这是 F25 的全部实现：工具级三事件由 `Agent` 内部分发，
   传了就自动对子 Agent 生效。漏传不报错，只是用户的 `pre_tool_use` 拦截规则
   对子 Agent 静默失效，而主 Agent 可以靠委派绕过它。
7. 调 `Agent.run(...)`，参数要点：
   - `plan_mode=False`（子 Agent 不进 Plan Mode，无人审批）；
   - `debug_log_path=None`、`recorder=None`（不写会话存档，N6）；
   - `clarify=None` / `approve_plan=None`；
   - `context_manager=runtime.new_context_manager()`（**每次新建**）；
   - `options=RunOptions(max_iterations=spec.max_turns, record_usage=False,
     allow_summary=False, excluded_tools=<toolset 的补集>, interactive=False)`。
8. 消费事件流：**不转发给 TUI**（F23），只用于累计轮次/用量、捕获 FINISHED 的 stop_reason。
9. 取结论：子历史里最后一条非空 assistant 正文；
   `stop_reason != COMPLETED` 时**一律**替换为说明文本（F11 的反例：
   计划前言被当成结论）。
10. 埋 `subagent_end`，`tasks.finish(...)`，置 `done_event`。

整段包 `try/except BaseException`：**后台线程的异常无人接管**，逃逸出去会让任务永远停在
RUNNING、前台等待方永远等不到 `done_event`。异常一律转成 FAILED + 可读结论。

### `subagents/service.py` —— 组合层（F6/F19/F20）

**职责**：把目录、任务表、运行器组合成一个对外门面，供工具与协调层调用。

**对外接口**：

| 方法 | 说明 |
| --- | --- |
| `delegate(kind, agent_name, task_text, background) -> DelegateOutcome` | 委派主流程 |
| `catalog` | 只读目录 |
| `index_text() -> str` | 角色清单注入文本（F9） |
| `tasks` | 任务管理器 |
| `report(...) -> str` | `/agents` 报告 |
| `request_background(task_id)` | 手动切后台（Ctrl+B 用） |

`delegate` 流程：校验 → 查角色 → `resolve_toolset` → 空集则失败（F14）→
并发上限（F20）→ 建任务 → 起 `threading.Thread(daemon=True)` → 分流：

- 后台（显式 / 分支式）→ 立即返回「已转入后台，标识 X」；
- 前台 → `record.done_event.wait(FOREGROUND_TIMEOUT)`；
  - 返回 True → 结论作为工具结果；
  - 返回 False（或期间被 `request_background` 置位）→ 置 `backgrounded`，
    返回「已转入后台」。

### `subagents/render.py` / `report.py` —— 文本产出（F9/F24）

`render.py`：角色清单注入文本。表头**指令式**，与委派工具的 `description` **同口径**：
「命中就委派 / 用它替代你自己直接做 / 用户不必点名 / 拿不准就委派」。

> ⚠ **成对维护点**：改动清单表头或委派工具描述 → 两处必须同改。
> 它们是模型决定「要不要委派」时读到的唯一两处文本，一处强一处弱等于白改。
> 这条与 C11 的 `_INDEX_HEADER` ↔ `load_skill.description` 是同一个坑。

`report.py`：`/agents` 三形态的文本。角色段展示来源层、description、最终工具集、
模型、轮次上限、**权限档位的声明值与实际生效值**（F16 要求两者都可见）、
未支持字段提示；错误段；被覆盖定义段；任务段。

### `tools/run_agent.py` —— 委派工具（F6）

```
name = "run_agent"
read_only = False
system_serial = True
```

`system_serial=True` 与 `LoadSkillTool` 同理由、同先例：它会开一整条子对话，
放进只读并发桶意味着子对话的执行会从线程池工作线程里发生。
**该标志同时意味着这次工具调用本身不进权限管线**——安全论证：
委派这个动作本身不产生任何副作用，副作用全部来自子 Agent 调用的工具，
而那些调用**逐个**过完整五层管线加 Hook 拦截（F25）。

参数 schema：`type`（枚举 `role` / `branch`）、`agent`、`task`、`background`。

**依赖**：`subagents.service`（构造时注入实例）。

### 协调层接线（`conversation.py`）

1. 构造 `SubAgentService`，把 `_provider_for`、`_registry`、`_engine`、`_hooks`、
   `_recorder`、上下文管理器工厂、环境信息取值函数打包成 `SubAgentRuntime`。
2. `_provider_for` 的缓存字典加锁——它现在会被多个后台线程并发调用。
3. 主对话的 `dynamic()` 里追加角色清单（F9）。
4. **每次发起主对话请求之前**调 `tasks.take_deliverables()`，
   把结论追加进主历史并写存档（F21 第 3 步）。
5. `clear()` / `resume` 前先 `cancel_all()` 并把取消数量告知用户（F22）。
6. **项目级角色的启动提示**（F4）：装配完成后取 `catalog` 里 `source == PROJECT`
   的角色名列表，经与 C11「发现 N 个项目级 Skill」同一条醒目通道提示，
   并附一句「随代码仓库分发，评审应与评审代码同等对待」。
   **刻意不做持久化**——有状态的话 `git pull` 新拉进来的角色会被静默吞掉。

> ⚠ **子 Agent 绝不调用 `hooks.consume_injections()`**。那是一个**会被取走**的队列，
> 后台子 Agent 去消费它，会让一条挂在 `turn_start` 上的注入型 Hook 从主对话里凭空消失。
> 这是本章最隐蔽的一处坑：不报错、不留痕，只是用户的 Hook 偶尔不生效。

### TUI 接线（`tui/app.py`）

- `on_mount` 起 `set_interval(0.5, self._poll_subagents)`：
  主线程读 `drain_notifications()` → 写通知行；读 `running_count()` → 刷状态栏。
  **全程主线程，零跨线程 widget 写入**——从结构上避开与 Textual 阻塞式
  `call_from_thread` 组成死锁的那一类问题（C11 已踩过）。
- `on_key` 增加 `Ctrl+B`：有前台等待中的子 Agent 时调 `request_background`。
- 状态栏新增「子Agent: N」字段。

---

## 模块交互

**一次前台委派（超时转后台）的完整链路**：

```
模型 → run_agent(type=role, agent=explorer, task=...)
  └─ Agent Loop 串行段（system_serial，跳过权限管线）
      └─ SubAgentService.delegate()
          ├─ resolve_toolset() → 非空
          ├─ TaskManager.create() → task_id=a3f1c9
          ├─ Thread(run_subagent) ──────────────┐
          └─ done_event.wait(60) ── 超时 ───┐   │（独立线程）
                                            │   ├ bind_scope("subagent:explorer")
   工具结果「已转入后台，标识 a3f1c9」 ◀────┘   ├ engine.derive(min 档)
   主对话继续                                    ├ Agent.run(...)  ← 每次工具调用
                                                 │   仍过 五层管线 + Hook
                                                 ├ tasks.finish(COMPLETED, 结论)
                                                 └ done_event.set()
                                                       │
   TUI 定时器（0.5s，主线程）◀──────────────────────────┘
     └ drain_notifications() → 聊天区出通知行
                                                       │
   主对话下一次请求组装前 ◀─────────────────────────────┘
     └ take_deliverables() → 结论作为一条消息追加进主历史 → 模型看到
```

## 文件组织

```
rhinecode/
├── subagents/                    ← 新包
│   ├── __init__.py               — 对外导出（AgentSpec / SubAgentService / 常量）
│   ├── models.py                 — AgentSpec、AgentCatalog、TaskRecord、TaskStatus、常量表
│   ├── parser.py                 — 单文件解析与字段归一
│   ├── discovery.py              — 三层扫描与覆盖
│   ├── toolset.py                — 分层工具过滤（纯函数）
│   ├── tasks.py                  — TaskManager（线程安全）
│   ├── runner.py                 — SubAgentRuntime、run_subagent
│   ├── service.py                — SubAgentService 门面
│   ├── render.py                 — 角色清单注入文本
│   ├── report.py                 — /agents 报告
│   └── builtin/
│       └── explorer.md           — 内置只读调研角色
├── tools/run_agent.py            ← 新增：委派工具
├── agent/loop.py                 ← 改：RunOptions.interactive、非交互拒绝分支与文案
├── permission/engine.py          ← 改：derive()
├── trace/models.py               ← 改：subagent_start / subagent_end
├── trace/reader.py               ← 改：两个摘要函数登记进 SUMMARIZERS
├── trace/recorder.py             ← 改：subagent_scope() 辅助
├── commands/builtins.py          ← 改：/agents 的 CommandSpec 与处理函数
├── commands/models.py            ← 改：ReportTarget 新增取值
├── conversation.py               ← 改：服务构造、清单注入、结论交付、清空时取消
├── tui/app.py                    ← 改：轮询定时器、Ctrl+B、状态栏字段
├── tui/widgets.py                ← 改：状态栏渲染新增字段
├── commands/controller.py        ← 改：协议新增取消方法
└── bootstrap.py                  ← 改：装配 SubAgentService、注册工具、项目级角色提示

角色目录的推导照 Skill 先例：内置目录由 `subagents/models.py` 的
`builtin_agents_dir()` 给出，项目级与用户级由 `bootstrap.py` 拼装——
`config.py` 不参与（Skill 的三层目录同样不经它）。

tests/
├── test_subagent_parser.py       — 字段解析、归一、夹取、未支持字段警告
├── test_subagent_discovery.py    — 三层覆盖、同层重名、fail-safe
├── test_subagent_toolset.py      — 三层过滤顺序、空集、未解析名字
├── test_subagent_tasks.py        — 并发安全、两条消费线、取消
├── test_subagent_runner.py       — 权限派生、非交互拒绝、结论提取、异常兜底
├── test_subagent_service.py      — 并发上限、前台超时转后台、空工具集失败
├── test_subagent_tool.py         — 参数校验、system_serial、错误回灌文案
├── test_subagent_report.py       — /agents 三形态
├── test_subagent_integration.py  — 结论交付主历史、Hook 生效、清空时取消
└── test_subagent_builtin.py      — explorer 存在且字段合法（硬编码名字清单）
```

## 技术决策

| 决策点 | 选择 | 理由 |
| --- | --- | --- |
| 包名 | `subagents/`（复数） | 与既有 `agent/`（Agent Loop）只差一字母会长期误读 |
| 线程模型 | 子 Agent 一律独立线程，前台=主 Worker 阻塞等待 | 「转后台」退化为「停止等待」，不需要在运行途中移交执行状态——这是三种进后台方式能统一实现的关键 |
| TUI 更新 | 定时轮询（`set_interval`），不做跨线程推送 | 后台线程 `call_from_thread` 是阻塞式的，与持锁组件配合会构成确定性死锁（C11 已踩过）。轮询把全部 widget 写入留在主线程 |
| 权限隔离 | `PermissionEngine.derive()` 派生实例 | 引擎的 `mode` / `turn_rules` 是**可变字段**。子 Agent 若直接改主引擎的 mode，主对话的权限档会被后台线程改掉，且界面上看不出来 |
| `session_rules` 共享 | 按引用共享（只读使用） | 用户在主对话确认面板上选的「本会话放行」应当对子 Agent 生效——那是明确授予。而 `turn_rules` 不共享（spec F17） |
| 上下文管理器 | **每个子 Agent 一个新实例** | 它持有 `_anchor_tokens` / `_circuit_broken` 等可变状态且无锁，并发共享会互相污染估算锚点。新建实例很轻，只共享存盘目录 |
| 非交互拒绝 | 加 `RunOptions.interactive` 开关，改循环 | 现有 ASK 返回 False 的路径会走 `DENIED_BY_USER_FEEDBACK`（「这是用户的决定，别重试」）并**硬性禁掉下一轮的全部工具**。这两条对子 Agent 都是错的：没有用户做过决定，且它应当改用只读方式继续 |
| 结论回流载体 | 追加一条带标记块的消息进主历史 | 动态注入的系统提醒不进历史，模型第三轮就忘了。用 `display_content` 让界面显示成通知行而非伪造的用户输入 |
| 两条消费线 | 通知与交付各有标志位 | 通知在完成瞬间给用户，交付要等主对话下次发请求。合成一条会出现「用户还没看到，模型已经引用了」 |
| 角色身份 | `name` 优先，缺省回落文件名 | spec F2 已定：保证从官方生态复制来的定义原样可用 |
| 委派工具进不进权限管线 | 不进（`system_serial=True`） | 委派本身无副作用；副作用全部来自子 Agent 的工具调用，那些逐个过完整管线。与 `load_skill` 同先例 |
| 后台附加过滤层 | 保留结构位、当前为空集 | 见 spec F13 的说明块：本章全程非交互，前后台约束相同，造人为差异会让同一角色在两种场景下行为不同而配置上看不出来 |
| Plan Mode | 子 Agent 一律 `plan_mode=False` | 它非交互，没有人能审批计划；顺带使两个特殊工具天然不出现 |
