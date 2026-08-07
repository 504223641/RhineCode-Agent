# C13 子 Agent 系统 Tasks

> ⚠ **本文是当初的执行计划，按 34 个任务逐条完成后**又经历了两次设计修订
> （发起与等待分离、去掉项目级角色的启动提示），因此下面若干处已与实现不符：
>
> - T15/T17 的前台阻塞等待 → 已改成「委派永不阻塞，等待交给 Agent Loop」；
> - T28/T29 的 `Ctrl+B` 与项目级启动提示 → 已删除；
> - 新增了 `agent/gate.py`（协议）与 `subagents/gate.py`（实现）两个文件。
>
> **刻意不逐条改写**：它记录的是「当初打算怎么做」，改写等于把这段过程抹掉。
> 以 `spec.md` / `plan.md` 为准，两处修订的来龙去脉见
> [`README.md`](README.md) 的「开发期的两次设计修订」。


> 对应已批准的 `spec.md` / `plan.md`。共 34 个任务。
> 每个任务自包含，验证方式写死为可运行的命令或可观察的输出。

## 文件清单

| 操作 | 文件 | 职责 |
| --- | --- | --- |
| 新建 | `rhinecode/subagents/__init__.py` | 对外导出 |
| 新建 | `rhinecode/subagents/models.py` | `AgentSpec` / `AgentCatalog` / `AgentSource` / `AgentLoadError` / 常量表 / `builtin_agents_dir` |
| 新建 | `rhinecode/subagents/parser.py` | frontmatter 解析、字段归一与夹取 |
| 新建 | `rhinecode/subagents/discovery.py` | 三层扫描与同名覆盖 |
| 新建 | `rhinecode/subagents/toolset.py` | 分层工具过滤（纯函数） |
| 新建 | `rhinecode/subagents/tasks.py` | `TaskRecord` / `TaskStatus` / `TaskManager` |
| 新建 | `rhinecode/subagents/runner.py` | `SubAgentRuntime` / `run_subagent` |
| 新建 | `rhinecode/subagents/service.py` | `SubAgentService` 门面 |
| 新建 | `rhinecode/subagents/render.py` | 角色清单注入文本 |
| 新建 | `rhinecode/subagents/report.py` | `/agents` 报告 |
| 新建 | `rhinecode/subagents/builtin/explorer.md` | 内置只读调研角色 |
| 新建 | `rhinecode/tools/run_agent.py` | 委派工具 |
| 修改 | `rhinecode/permission/engine.py` | 新增 `derive()` |
| 修改 | `rhinecode/agent/loop.py` | `RunOptions.interactive`、非交互拒绝分支与文案、新 outcome |
| 修改 | `rhinecode/trace/models.py` | 两个新事件类型 |
| 修改 | `rhinecode/trace/recorder.py` | `subagent_scope()` |
| 修改 | `rhinecode/trace/__init__.py` | 导出新符号 |
| 修改 | `rhinecode/trace/reader.py` | `SUMMARIZERS` 登记两个摘要函数 |
| 修改 | `rhinecode/commands/models.py` | `ReportTarget.AGENTS` |
| 修改 | `rhinecode/commands/builtins.py` | `/agents` 的 `CommandSpec` 与处理函数 |
| 修改 | `rhinecode/commands/controller.py` | `CommandController` 协议新增取消方法 |
| 修改 | `rhinecode/conversation.py` | 服务构造、清单注入、结论交付、清空时取消、`_provider_for` 加锁 |
| 修改 | `rhinecode/bootstrap.py` | 装配 `SubAgentService`、注册工具、项目级角色提示 |
| 修改 | `rhinecode/tui/app.py` | 轮询定时器、`Ctrl+B`、状态栏取值、`query_report` 分支、取消方法 |
| 修改 | `rhinecode/tui/widgets.py` | 状态栏渲染新增字段 |
| 修改 | `CLAUDE.md` | 能力表、架构表、成对维护点、安全边界、常用命令 |
| 新建 | `docs/c13/README.md` | 本章导航 |
| 新建 | `tests/test_subagent_*.py` | 10 个测试文件（见各任务） |

---

## T1: 角色定义的数据结构与常量表

**文件：** `rhinecode/subagents/models.py`（新建）、`rhinecode/subagents/__init__.py`（新建，先留空导出）
**依赖：** 无

**步骤：**
1. 定义 `AgentSource` 枚举：`PROJECT` / `USER` / `BUILTIN`，并给每个值配一个中文展示名的映射 `SOURCE_LABELS`（`/agents` 报告用）。
2. 定义冻结数据类 `AgentSpec`，字段照 plan「核心数据结构」那张表：`name`、`description`、`body`、`tools`（`Optional[tuple[str, ...]]`）、`disallowed_tools`、`model`、`max_turns`、`permission_mode`、`source`、`path`、`warnings`。
3. 定义冻结数据类 `AgentLoadError`：`path`、`source`、`message`。
4. 定义冻结数据类 `AgentCatalog`：`specs`（`dict[str, AgentSpec]`）、`errors`、`shadowed`（三元组序列：名字、来源、路径）。
5. 定义常量：`DEFAULT_MAX_TURNS = 15`、`HARD_MAX_TURNS = 25`、`MAX_CONCURRENT = 3`、`FOREGROUND_TIMEOUT = 60.0`、`ENTRY_SUFFIX = ".md"`。
6. 定义 `UNSUPPORTED_FIELDS`：`skills` / `memory` / `isolation` / `color` / `hooks` / `mcp_servers` / `background` / `effort`，值为该字段的中文说明（警告文案要具名，不能笼统说「未知字段」）。
7. 定义 `builtin_agents_dir() -> Path`：返回本模块同级的 `builtin/` 目录（照 `skills/models.py` 的 `builtin_skills_dir()` 写法）。
8. 在 `HARD_MAX_TURNS` 处写注释说明它必须 ≤ `agent/loop.py` 的 `MAX_ITERATIONS`，并说明为什么不直接 import 那个常量（避免 `subagents` 在数据层就依赖 `agent` 包）。

**验证：** `python -c "from rhinecode.subagents.models import AgentSpec, builtin_agents_dir; print(builtin_agents_dir())"` 打印出以 `subagents/builtin` 结尾的路径。

---

## T2: frontmatter 解析器

**文件：** `rhinecode/subagents/parser.py`（新建）
**依赖：** T1

**步骤：**
1. 定义 `AgentParseError(Exception)`。
2. 实现 `_split_frontmatter(text) -> tuple[dict, str]`：切分 `---` 包裹的 YAML 与正文；无 frontmatter 时抛 `AgentParseError`。
3. 实现 `_normalize_keys(raw) -> dict`：键转小写、连字符转下划线（`disallowed-tools` → `disallowed_tools`）。
4. 实现 `_as_name_list(value) -> tuple[str, ...]`：同时接受逗号分隔字符串与 YAML 列表，逐项 strip 并丢弃空串。
5. 实现 `parse_agent(text, path, source, fallback_name) -> AgentSpec`：
   - `description` 缺失或空白 → 抛 `AgentParseError`；
   - `name` 缺失 → 用 `fallback_name`；含 `/`、`\`、空白或为空 → 抛 `AgentParseError`；
   - `tools` 未声明时置 `None`（**区别于空列表**，`None` 表示继承）；
   - `model` 为 `inherit`（大小写不敏感）或缺失 → 归一为 `None`；
   - `max_turns` 非整数 → 记警告并用默认值；越界 → 夹到 `[1, HARD_MAX_TURNS]` 并记警告（警告里写明被夹到多少）；
   - `permission_mode` 认 `strict` / `default` / `permissive`，其它取值记警告并置 `None`；
   - 命中 `UNSUPPORTED_FIELDS` 的键逐个产出具名警告。
6. 给 `parse_agent` 写完整 docstring：参数、返回、抛出条件、无副作用。

**验证：** `python -m compileall rhinecode/subagents` 通过。

---

## T3: 解析器测试

**文件：** `tests/test_subagent_parser.py`（新建）
**依赖：** T2

**步骤：**
1. 最小合法定义（只有 `description`）能解析，`name` 取 `fallback_name`，`tools is None`。
2. `disallowed-tools` 与 `disallowed_tools` 两种写法产出相同结果（AC1）。
3. `tools: read_file, glob_files` 与 YAML 列表写法产出相同结果。
4. 缺 `description` 抛 `AgentParseError`；`name` 含空白抛 `AgentParseError`。
5. `max_turns: 999` 被夹到 `HARD_MAX_TURNS` 且警告里出现被夹后的数字。
6. `model: inherit` 归一为 `None`。
7. `permission_mode: bogus` 记警告且置 `None`。
8. 每个 `UNSUPPORTED_FIELDS` 的键都产出**含该字段名**的警告（遍历常量表断言，漏登记当场红）。

**验证：** `python -m unittest tests.test_subagent_parser` 全绿。

---

## T4: 三层扫描与覆盖

**文件：** `rhinecode/subagents/discovery.py`（新建）
**依赖：** T2

**步骤：**
1. 实现 `_scan_layer(directory, source) -> tuple[list[AgentSpec], list[AgentLoadError], list[shadowed]]`：
   - `directory` 为 `None` 或不存在 → 空层，不算错误；
   - `sorted(iterdir())` 遍历，只处理 `.md` 文件，其它静默跳过；
   - 读文件或解析失败 → 记一条 `AgentLoadError`，继续下一个；
   - 层内同名 → **保留先出现的（字典序靠前）**，后者进 `shadowed` 并记一条错误级警告。
2. 实现 `discover_agents(project_dir, user_dir, builtin_dir) -> AgentCatalog`：按 项目 → 用户 → 内置 顺序扫描，先到先得（高优先层先扫），被低优先层同名者进 `shadowed`。
3. 整个模块 fail-safe：任何 `OSError` 都转成 `AgentLoadError`，绝不外抛。
4. 模块 docstring 说明「命令名来自 `name` 而非路径」这一点与 C11 刻意不同，并写明理由（spec F2）。

**验证：** `python -m compileall rhinecode/subagents` 通过。

---

## T5: 扫描测试

**文件：** `tests/test_subagent_discovery.py`（新建）
**依赖：** T4

**步骤：**
1. 用 `tempfile.TemporaryDirectory` 造三层目录。
2. 同名角色分别放项目级与用户级 → 生效的是项目级，用户级那份出现在 `shadowed`（AC2）。
3. 放一个坏 YAML 文件 + 一个好文件 → 好的照常可用，坏的进 `errors`（AC3）。
4. 同层放两个 `name` 相同的文件 → 字典序靠前者生效，另一份在 `shadowed` 里可见（AC3）。
5. 目录不存在 → 空目录，`errors` 为空。
6. 放一个 `.txt` 与一个 `README.md`（`README.md` 会被当角色解析并因缺 `description` 报错——断言这条错误存在，说明「只跳过非 `.md`」的口径被准确实现）。

**验证：** `python -m unittest tests.test_subagent_discovery` 全绿。

---

## T6: 内置 explorer 角色

**文件：** `rhinecode/subagents/builtin/explorer.md`（新建）
**依赖：** T1

**步骤：**
1. frontmatter：`name: explorer`、`description`（写成**触发词前置**的形式，照 C11 作者期扩展的教训：先写「什么时候用」再写「是什么」）、`tools: read_file, glob_files, grep_content`、`permission_mode: strict`、`max_turns: 15`。
2. 正文写清三件事：身份（只读调研员）、职责（把散落的信息汇总成结论）、**产出要求**（最后一段必须是自包含的结论，因为只有它会回流主对话）。
3. 正文里明确写「你没有写入能力，遇到需要修改的地方就在结论里指出位置和建议，不要尝试修改」——避免它把轮次浪费在必然被拒的写入上。

**验证：** `python -c "from rhinecode.subagents.discovery import discover_agents; from rhinecode.subagents.models import builtin_agents_dir; c=discover_agents(None,None,builtin_agents_dir()); print(c.specs['explorer'].tools, c.errors)"` 打印出三个工具名且 `errors` 为空。

---

## T7: 分层工具过滤

**文件：** `rhinecode/subagents/toolset.py`（新建）
**依赖：** T1

**步骤：**
1. 定义 `GLOBAL_DENIED_TOOLS = frozenset({"run_agent", "load_skill"})`，注释说明：用字面量而非 import，是为了不让本模块依赖 `tools` 包（`tools ↔ subagents` 已经是包级互依）；并标注这是成对维护点。
2. 定义 `BACKGROUND_DENIED_TOOLS = frozenset()`，注释说明为什么本章为空集（spec F13 的说明块，照抄理由）。
3. 定义冻结数据类 `ToolsetResult`：`allowed`（`frozenset[str]`）、`unresolved`（`tuple[str, ...]`）、`reason`（空集时的可读说明，非空集时为空串）。
4. 实现 `resolve_toolset(all_tools, spec, is_background) -> ToolsetResult`，顺序固定：全局禁止 → 角色白名单交集 → 角色黑名单 → 后台附加层。
5. `spec.tools is None` 时跳过白名单交集（继承）；非 `None` 时记录其中不在 `all_tools` 里的名字进 `unresolved`。
6. 结果为空时组装 `reason`：写明原始工具数、每一层去掉了什么、`unresolved` 有哪些。

**验证：** `python -m compileall rhinecode/subagents` 通过。

---

## T8: 工具过滤测试

**文件：** `tests/test_subagent_toolset.py`（新建）
**依赖：** T7

**步骤：**
1. `tools` 未声明时继承全部工具，但 `run_agent` / `load_skill` **一定不在结果里**（AC11）。
2. 白名单里写了不存在的工具名 → 进 `unresolved`，且不影响存在的那些。
3. 白名单与黑名单同时命中一个工具 → 该工具被排除（黑名单在白名单之后）。
4. 白名单全部写错 → `allowed` 为空且 `reason` 里出现那些错误名字（AC12）。
5. 遍历 `GLOBAL_DENIED_TOOLS`，逐个断言不出现在结果里（新增禁止项时自动被覆盖）。

**验证：** `python -m unittest tests.test_subagent_toolset` 全绿。

---

## T9: 权限引擎派生

**文件：** `rhinecode/permission/engine.py`（修改）、`tests/test_perm_derive.py`（新建）
**依赖：** 无

**步骤：**
1. 在 `PermissionEngine` 上新增 `derive(self, mode) -> PermissionEngine`：
   - 新实例的 `file_ruleset` / `policy_ruleset` **共享同一对象**；
   - `session_rules` **共享同一个列表对象**（用户在主对话授予的「本会话放行」应对子 Agent 生效）；
   - `turn_rules` 为**全新空列表**（spec F17）；
   - `mode` 取传入值；`load_errors` 共享。
2. docstring 写明：这是给子 Agent 用的**只读视图**，调用方不得在派生实例上登记规则；并写明为什么必须派生而不是改 `self.mode`（后台线程改主对话档位，界面看不出来）。
3. 新增模块级函数 `narrower_mode(a, b) -> PermissionMode`：按 STRICT < DEFAULT < PERMISSIVE 取更严的一档，供 F16 使用。
4. 测试：派生实例改 `mode` 不影响原实例；派生实例的 `turn_rules` 为空且往里追加不影响原实例；原实例往 `session_rules` 追加后派生实例能看到；`narrower_mode` 六种组合全覆盖。

**验证：** `python -m unittest tests.test_perm_derive` 全绿；`python -m unittest discover -s tests -p "test_perm*.py"` 全绿（既有权限测试无回归）。

---

## T10: Agent Loop 的非交互模式

**文件：** `rhinecode/agent/loop.py`（修改）、`tests/test_agent_non_interactive.py`（新建）
**依赖：** 无

**步骤：**
1. `RunOptions` 新增 `interactive: bool = True`，docstring 写明**默认值即既有行为，不传等于零回归**。
2. 新增常量 `OUTCOME_DENIED_NON_INTERACTIVE = "denied_non_interactive"`，与既有 `OUTCOME_*` 放在一起。
3. 新增常量 `DENIED_NON_INTERACTIVE_FEEDBACK`：文案要求——说明这是**非交互环境**、没有人能确认，引导「改用只读方式达成」或「在最终结论里写明这一步需要用户授权」，并**明确要求不要重试同一个调用**。在注释里写明它与 `DENIED_BY_USER_FEEDBACK` 的语义差别（那条是「人做了决定」，这条是「没人能做决定」）。
4. 在决策为 `ASK` 的分支里加判断：`options.interactive` 为假时**不调 `ask` 回调**，直接按新 outcome 记录并回灌新文案，且**不置 `user_denied`**（不触发下一轮禁用全部工具的硬约束）。
5. 测试：
   - `interactive=False` 时判 ASK 的调用被拒且 `ask` 回调**一次都没被调用**；
   - 回灌文本是新文案而非 `DENIED_BY_USER_FEEDBACK`；
   - 被拒之后的下一轮**仍然带工具**（反证：`interactive=True` 的同场景下一轮 `tools` 为 `None`）；
   - `interactive=True`（默认）时行为与改动前逐字一致。

**验证：** `python -m unittest tests.test_agent_non_interactive` 全绿；`python -m unittest discover -s tests -p "test_agent*.py" -p "test_perm*.py"` 全绿。

---

## T11: trace 埋点扩展

**文件：** `rhinecode/trace/models.py`、`rhinecode/trace/recorder.py`、`rhinecode/trace/__init__.py`、`rhinecode/trace/reader.py`（均修改）、`tests/test_trace_reader.py`（修改）
**依赖：** 无

**步骤：**
1. `trace/models.py` 的 `TraceEventType` 新增 `SUBAGENT_START = "subagent_start"` 与 `SUBAGENT_END = "subagent_end"`。
2. `trace/recorder.py` 新增 `subagent_scope(name) -> str`，返回 `f"subagent:{name}"`；与既有 `isolated_scope` 放在一起，注释说明为什么不复用（读记录时要能把 Skill 子对话与子 Agent 分开）。
3. `trace/__init__.py` 导出 `subagent_scope` 与两个新事件类型。
4. `trace/reader.py` 的 `SUMMARIZERS` 登记两个摘要函数：start 摘 `kind` / `agent` / `task_id` / 任务描述首行；end 摘 `task_id` / `status` / `turns` / `stop_reason`。
5. `tests/test_trace_reader.py` 里已有的「遍历全部事件类型断言都登记了摘要」那条用例会自动覆盖新增项——确认它确实存在；若不存在则补一条。

**验证：** `python -m unittest tests.test_trace_reader` 全绿。

---

## T12: 任务表数据结构

**文件：** `rhinecode/subagents/tasks.py`（新建，本任务只写数据结构）
**依赖：** T1

**步骤：**
1. 定义 `TaskStatus` 枚举：`RUNNING` / `COMPLETED` / `FAILED` / `CANCELLED`，配 `STATUS_LABELS` 中文展示名。
2. 定义**可变**数据类 `TaskRecord`，字段照 plan 那张表（含 `cancel_event` / `done_event` 两个 `threading.Event`、`delivered` / `notified` / `backgrounded` 三个标志）。
3. `TaskRecord` 加一个 `duration_seconds` 属性（未结束时按当前时间算）。
4. 注释写明：`delivered` 与 `notified` 是**两条独立的消费线**，理由照 plan。

**验证：** `python -m compileall rhinecode/subagents` 通过。

---

## T13: 任务管理器

**文件：** `rhinecode/subagents/tasks.py`（续写）
**依赖：** T12

**步骤：**
1. 实现 `TaskManager`，持一把 `threading.Lock` 与 `dict[str, TaskRecord]`。
2. 实现 plan 里列的九个方法：`create` / `running_count` / `running_brief` / `bump` / `finish` / `snapshot` / `take_deliverables` / `drain_notifications` / `cancel` / `cancel_all`。
3. `create` 用 `secrets.token_hex(3)` 生成 6 位标识，冲突时重生成。
4. **加锁不变量**：每个方法的临界区只做纯内存读写；`finish` 里置 `done_event` 放在**锁外**（`Event.set()` 会唤醒等待线程，属于跨线程调度）。在类 docstring 里把这条写成显式约定，并说明它与 C11 `SkillManager`、C12 `HookManager` 是同一条。
5. `snapshot` 返回 `TaskRecord` 的**浅拷贝元组**，避免调用方在锁外读到半更新的字段。

**验证：** `python -m compileall rhinecode/subagents` 通过。

---

## T14: 任务管理器测试

**文件：** `tests/test_subagent_tasks.py`（新建）
**依赖：** T13

**步骤：**
1. `create` 分配的标识互不重复（造 200 个）。
2. `take_deliverables` 只取「已完成且未交付」的，第二次调用返回空（幂等）。
3. `drain_notifications` 与 `take_deliverables` **互不影响**：先 drain 再 take，两者都能拿到（证明是两条独立消费线）。
4. `cancel` 置位对应任务的 `cancel_event`；`cancel_all` 返回被取消的数量且只算未完成的。
5. **并发压测**：起 20 个线程各自 `create` + `bump` + `finish`，结束后断言 `snapshot()` 恰好 20 条、状态全部为终态、无异常逃逸。
6. **结构护栏**：断言 `TaskManager` 不持有任何 callable 属性（用 `inspect` 遍历实例属性），钉住「本类刻意不持有回调」这条设计约定。

**验证：** `python -m unittest tests.test_subagent_tasks` 全绿。

---

## T15: 子 Agent 运行器

**文件：** `rhinecode/subagents/runner.py`（新建）
**依赖：** T7、T9、T10、T11、T13

**步骤：**
1. 定义冻结数据类 `SubAgentRuntime`，字段照 plan 那张表。
2. 定义冻结数据类 `ParentSnapshot`：`history`（`list[Message]` 的副本）、`stable`（父稳定提示）、`tool_names`（父工具集）。
3. 实现 `run_subagent(runtime, spec, task_text, record, parent=None) -> None`，按 plan 的十步执行。
4. 步骤要点逐条落实：
   - 开头 `recorder.bind_scope(subagent_scope(...))`；
   - 定义式 `stable = spec.body`，`dynamic` 闭包返回 `environment_text()`（工具集含 `web_fetch` 时追加 `untrusted_section`）；**绝不调用 `hooks.consume_injections()`**，并在此处写注释说明为什么（它是会被取走的队列）；
   - 分支式取 `parent.history` 副本与 `parent.stable`；
   - `engine.derive(narrower_mode(主档, 角色档))`；
   - `ask` 回调恒返回 `False`（配合 `interactive=False`，实际不会被调用，留着是防御性的）；
   - `Agent(provider, registry, recorder=..., hooks=runtime.hooks)`——**`hooks` 必须传**，注释写明漏传的后果；
   - `Agent.run(..., plan_mode=False, clarify=None, approve_plan=None, recorder=None, context_manager=runtime.new_context_manager(), options=RunOptions(max_iterations=spec.max_turns, record_usage=False, allow_summary=False, excluded_tools=<全部工具 − allowed>, interactive=False))`。
5. 事件消费：只累计轮次（数 `TOOL_START` 之外的迭代信号或直接数 history 里的 assistant 条数）、捕获 `FINISHED` 的 `stop_reason`，**不 yield 给任何人**。
6. 结论提取：反向找最后一条非空 assistant 正文；`stop_reason != COMPLETED` 时**一律**替换为按停止原因分类的说明文本（定义一张 `_FAILURE_TEXT` 表，照 `conversation.py` 的 `_ISOLATED_FAILURE_TEXT` 先例）。
7. 整段包 `try/except BaseException` 兜底，转成 `FAILED` + 可读结论；`finally` 里保证 `tasks.finish` 一定被调用。

**验证：** `python -m compileall rhinecode/subagents` 通过。

---

## T16: 运行器测试

**文件：** `tests/test_subagent_runner.py`（新建）
**依赖：** T15

**步骤：**
1. 用假 Provider（照既有测试的 stub 写法）跑一个「不调工具直接答」的子 Agent，断言结论 = 那段正文，状态 `COMPLETED`。
2. **权限隔离**：主引擎为 `PERMISSIVE`、角色声明 `strict` → 运行期间实际用的引擎 `mode` 为 `STRICT`，且**主引擎的 `mode` 跑完后仍是 `PERMISSIVE`**（AC14 的核心反证）。
3. **不继承回合级预授权**：主引擎 `grant_turn_rules` 一条放行规则后启动子 Agent，断言子 Agent 用的引擎 `turn_rules` 为空（AC15）。
4. **非交互拒绝**：子 Agent 调一个会判 ASK 的工具 → 被拒且 `ask` 回调未被调用。
5. **结论提取的反例**：子历史最后一条 assistant 是「我打算这样做：……」但 `stop_reason` 是 `CANCELLED` → 回流的是说明文本而不是那段前言（AC10）。
6. **异常兜底**：让 Provider 抛异常 → 任务状态 `FAILED`、`done_event` 已置位、异常不逃逸（用 `threading.excepthook` 或直接同线程调用 `run_subagent` 断言不抛）。
7. **不消费注入队列**：给一个含待取注入的假 Hook 管理器，跑完子 Agent 后断言队列**仍然非空**（钉住 plan 里那条最隐蔽的坑）。

**验证：** `python -m unittest tests.test_subagent_runner` 全绿。

---

## T17: 服务门面

**文件：** `rhinecode/subagents/service.py`（新建）
**依赖：** T4、T13、T15

**步骤：**
1. 定义冻结数据类 `DelegateOutcome`：`ok`、`text`（回灌给模型的文本）、`task_id`（可为 None）。
2. 实现 `SubAgentService.__init__(catalog, runtime)`，内部建 `TaskManager`。
3. 实现 `delegate(kind, agent_name, task_text, background) -> DelegateOutcome`，按 plan 的流程：
   - `kind` 非法 / `task_text` 为空 → 失败并说明；
   - `kind == "role"` 时角色不存在 → 失败并**列出全部可用角色名**（模型据此自我纠正）；
   - `resolve_toolset` 空集 → 失败并回灌 `reason`（F14，**不起线程、不发 API**）；
   - `running_count() >= MAX_CONCURRENT` → 失败并附 `running_brief()`（F20）；
   - `kind == "branch"` 时强制 `background=True`；
   - 建任务 → 起 `threading.Thread(target=..., daemon=True)`；
   - 后台 → 立即返回「已转入后台，标识 X，完成后结果会自动送达」；
   - 前台 → `done_event.wait(FOREGROUND_TIMEOUT)`，超时或 `backgrounded` 已置位则返回转后台文案，否则返回结论。
4. 实现 `request_background(task_id) -> bool`：置 `backgrounded` 并置 `done_event`（让前台等待方立刻返回）。**注意**：`done_event` 被提前置位后，运行器真正完成时不能再依赖它做「是否已交付」判断——用 `status` 判断。
5. 实现 `index_text()`（委托 `render.py`）与 `report(...)`（委托 `report.py`）。
6. 实现 `foreground_task_id()`：当前正在被前台等待的任务标识，供 `Ctrl+B` 使用。

**验证：** `python -m compileall rhinecode/subagents` 通过。

---

## T18: 服务测试

**文件：** `tests/test_subagent_service.py`（新建）
**依赖：** T17

**步骤：**
1. 角色不存在 → 失败文本里出现全部可用角色名。
2. 角色的 `tools` 全写错 → 失败，且**断言没有起过线程、没有创建过任务**（AC12）。
3. 并发上限：塞 3 个假的 RUNNING 任务后第 4 次 `delegate` 失败，文本里出现那 3 个的标识（AC18）。
4. 前台超时：把 `FOREGROUND_TIMEOUT` 打补丁成 0.1 秒，用一个慢子 Agent → 返回转后台文案，且任务仍在 RUNNING（AC17）。
5. `request_background` 能让前台等待方提前返回。
6. `kind == "branch"` 时即使传 `background=False` 也走后台（AC8）。

**验证：** `python -m unittest tests.test_subagent_service` 全绿。

---

## T19: 角色清单注入文本

**文件：** `rhinecode/subagents/render.py`（新建）
**依赖：** T1

**步骤：**
1. 定义 `_INDEX_HEADER` 常量，措辞**指令式**：命中就委派 / 用它替代你自己直接做 / 用户不必点名 / 拿不准就委派。
2. 实现 `render_agent_index(catalog) -> str`：表头 + 每个角色一行「名字 —— description」；无角色时返回空串（**不要产出一个空清单**，那会让模型以为委派能力不存在还占上下文）。
3. 在 `_INDEX_HEADER` 上方写 ⚠ 注释标注成对维护点：它与 `tools/run_agent.py` 的 `description` 必须同口径。
4. 超预算时的降级：角色很多时**保名字只砍描述**（照 C11 作者期扩展的结论）。

**验证：** `python -m compileall rhinecode/subagents` 通过。

---

## T20: /agents 报告

**文件：** `rhinecode/subagents/report.py`（新建）
**依赖：** T13、T19

**步骤：**
1. 实现 `render_report(catalog, tasks, resolve_fn, main_mode) -> str`，分段：
   - **角色段**：名字、来源层、description、最终工具集、模型、轮次上限、**权限档位「声明值 → 实际生效值」**、未支持字段提示；
   - **加载错误段**（无则不出现）；
   - **被覆盖定义段**（无则不出现，含来源与路径）；
   - **任务段**：标识、类型与角色、状态、耗时、轮次、用量、结论首行；无任务时写「本次运行尚未发起过委派」。
2. 所有嵌进文本的用户可控内容（角色名、路径、结论）必须走 `tui/widgets.py` 的 `escape`——**不要用 rich 那版**。在调用处写注释说明理由。
3. 实现 `render_cancel_result(count) -> str`。

**验证：** `python -m compileall rhinecode/subagents` 通过。

---

## T21: 报告测试

**文件：** `tests/test_subagent_report.py`（新建）
**依赖：** T20

**步骤：**
1. 角色声明 `permissive`、主对话 `default` → 报告里同时出现声明值与生效值，且生效值是「默认」（AC14）。
2. 有加载错误时错误段出现，无错误时该段**完全不出现**。
3. 有被覆盖定义时该段出现并含来源层。
4. 任务段展示状态、轮次、用量（AC16）。
5. 角色名含 `[` 时报告里被转义成 `\[`（钉住 markup 转义，防 `MarkupError` 拆掉整个 app）。

**验证：** `python -m unittest tests.test_subagent_report` 全绿。

---

## T22: 包导出

**文件：** `rhinecode/subagents/__init__.py`（补写）
**依赖：** T17、T20

**步骤：**
1. 导出 `AgentSpec` / `AgentCatalog` / `AgentSource` / `SubAgentService` / `SubAgentRuntime` / `ParentSnapshot` / `discover_agents` / `builtin_agents_dir` / 常量。
2. 写 `__all__`。
3. 模块 docstring 说明本包的职责与依赖方向，并写明 `tools ↔ subagents` 是**第四组**包级互依、不成环靠 `tools/__init__.py` 为空。

**验证：** `python -c "import rhinecode.subagents as s; print(len(s.__all__))"` 正常打印。

---

## T23: 委派工具

**文件：** `rhinecode/tools/run_agent.py`（新建）
**依赖：** T17

**步骤：**
1. 定义 `RunAgentTool(Tool)`：`name = "run_agent"`、`read_only = False`、`system_serial = True`。
2. `description` 写成**指令式**，与 `render.py` 的 `_INDEX_HEADER` 同口径；⚠ 注释标注成对维护点。描述里要写清两种 `type` 的差别与各自适用场景。
3. `parameters` JSON Schema：`type`（枚举 `role` / `branch`，必填）、`agent`（字符串）、`task`（字符串，必填）、`background`（布尔，缺省 false）。
4. `__init__(self, service)` 接 `SubAgentService`。
5. `execute(args)`：取参数 → 调 `service.delegate(...)` → 转 `ToolResult`；`ok` 取 `outcome.ok`，`summary` 写角色名与任务标识，`output` 写 `outcome.text`。捕获自身全部异常转 `ok=False`（`Tool` 契约要求不外抛）。
6. 类 docstring 写明 `system_serial=True` 的两条理由（会开一整条子对话 / 因此不进权限管线），并写明安全论证：副作用全部来自子 Agent 的工具调用，那些逐个过完整五层管线加 Hook。

**验证：** `python -m compileall rhinecode/tools` 通过。

---

## T24: 委派工具测试

**文件：** `tests/test_subagent_tool.py`（新建）
**依赖：** T23

**步骤：**
1. `system_serial is True` 且 `read_only is False`（结构护栏，改动会当场红）。
2. `type` 非法 / `task` 为空 → `ok=False` 且文本可读。
3. `type=role` 缺 `agent` → 失败并提示。
4. 成功路径：假 service 返回成功 → `ToolResult.ok` 为真且 `output` 是 service 的文本。
5. service 抛异常 → `ok=False`，异常不外抛。
6. **同口径护栏**：断言 `RunAgentTool.description` 与 `render._INDEX_HEADER` 含同一组关键短语（钉住成对维护点，一处改了另一处不改就红）。

**验证：** `python -m unittest tests.test_subagent_tool` 全绿。

---

## T25: 命令层登记

**文件：** `rhinecode/commands/models.py`、`rhinecode/commands/builtins.py`、`rhinecode/commands/controller.py`（均修改）
**依赖：** 无

**步骤：**
1. `ReportTarget` 新增 `AGENTS = "agents"`（枚举 docstring 的成对维护点说明要一并更新）。
2. `CommandController` 协议新增 `cancel_subagents(target: Optional[str]) -> str`。
3. `builtins.py` 新增 `_handle_agents`：无参走 `query_report(ReportTarget.AGENTS)`；`cancel <标识>` 与 `cancel all` 走 `cancel_subagents`；未知子命令给出用法串。
4. 登记 `CommandSpec`：名字 `agents`、描述、用法 `/agents [cancel <标识|all>]`、非隐藏。
5. 抽出 `_AGENTS_USAGE` 常量（照 `_SKILLS_USAGE` 先例）。

**验证：** `python -m unittest tests.test_commands` 全绿（既有命令测试覆盖注册表一致性）。

---

## T26: 命令测试

**文件：** `tests/test_subagent_commands.py`（新建）
**依赖：** T25

**步骤：**
1. `/agents` 走报告分支；`/agents cancel abc` 与 `/agents cancel all` 走取消分支且参数正确。
2. `/agents bogus` 给出用法串、**不进 AI**。
3. `/AGENTS`（大写）同样命中（C10 大小写不敏感）。
4. `/agents` 出现在 `/help` 输出里。

**验证：** `python -m unittest tests.test_subagent_commands` 全绿。

---

## T27: 协调层接线

**文件：** `rhinecode/conversation.py`（修改）
**依赖：** T17、T19

**步骤：**
1. `__init__` 接一个 `subagent_service` 参数（可为 `None` → 非 DeepSeek 工具模式下不启用，保证 N1）。
2. `_provider_for` 加一把 `threading.Lock` 保护缓存字典，注释写明它现在会被后台线程并发调用。
3. 主对话 `dynamic()` 里追加 `service.index_text()`。
4. 新增 `_deliver_subagent_results()`：取 `take_deliverables()`，把每条结论包成带标记块的 `Message(role="user", content=..., display_content=...)` 追加进 `history` 并 `memory_manager.record_message`。在 `_run` 组装请求**之前**调用。
5. `clear()` 与会话恢复路径里先 `tasks.cancel_all()`，把数量并进返回文案。
6. 新增 `agents_report()` 与 `cancel_subagents(target)` 两个领域方法。
7. 新增 `parent_snapshot()`：返回 `history` 的**副本**、当前 stable 提示与工具名集合，供分支式使用。

**验证：** `python -m compileall rhinecode` 通过；`python -m unittest discover -s tests` 无新增失败。

---

## T28: 装配层

**文件：** `rhinecode/bootstrap.py`（修改）
**依赖：** T22、T23、T27

**步骤：**
1. 在 Skill 第一阶段之后、`connect_all` 之前的同一段窗口里，扫描三层角色目录得到 `AgentCatalog`。
2. 构造 `SubAgentRuntime`（从已有的 provider 工厂、registry、engine、hooks、recorder 组装）与 `SubAgentService`。
3. 注册 `RunAgentTool`——必须在 `session_start` 快照**之前**（否则快照里的工具集与实际不符，与既有 `exclude_tools` 同一条理由）。
4. 把 service 传进 `ConversationManager`。
5. 收集项目级角色名列表，经与 C11「发现 N 个项目级 Skill」同一条醒目通道产出启动提示（F4），并附「随代码仓库分发，评审应与评审代码同等对待」。
6. 在装配顺序注释里补一句：`RunAgentTool` 注册点为什么卡在这个位置。

**验证：** `python -m unittest tests.test_bootstrap tests.test_skill_startup` 全绿。

---

## T29: TUI 接线

**文件：** `rhinecode/tui/app.py`、`rhinecode/tui/widgets.py`（均修改）
**依赖：** T27、T28

**步骤：**
1. `widgets.py` 的 `compose_status_text` 新增「子Agent: N」字段（N 为 0 时**不显示该字段**，避免给常规用户增加噪音）。
2. `app.py` 的 `_refresh_status` 在**同一份参数组**里加上运行数（不要抄第二份清单——那是既有成对维护点）。
3. `on_mount` 起 `self.set_interval(0.5, self._poll_subagents)`。
4. 实现 `_poll_subagents()`：读 `drain_notifications()` → 每条写一行通知（用 `escape` 转义结论首行）；读 `running_count()` → 变化时 `refresh_status()`。整个方法包 try/except，**观测设施绝不能反过来打断界面**。
5. `on_key` 增加 `Ctrl+B` 分支：有前台等待任务时调 `request_background` 并提示，否则给一句「当前没有正在等待的子 Agent」。
6. `query_report` 增加 `ReportTarget.AGENTS` 分支；新增 `cancel_subagents` 实现（转发协调层）。

**验证：** `python -m unittest discover -s tests -p "test_tui*.py"` 全绿。

---

## T30: 集成测试

**文件：** `tests/test_subagent_integration.py`（新建）
**依赖：** T27、T28、T29

**步骤：**
1. **结论交付主历史**：制造一个已完成未交付的任务 → 调 `_deliver_subagent_results()` → `history` 新增一条消息且 `take_deliverables()` 第二次为空；再跑一轮，断言那条消息**仍在** history 里（AC19 的「再下一轮仍能引用」）。
2. **Hook 对子 Agent 生效**：装一个拦截某工具的 `pre_tool_use` 规则，让子 Agent 调该工具 → 被拦（AC22）。
3. **清空时取消**：起两个假 RUNNING 任务 → `clear()` → 两个都变 `CANCELLED` 且返回文案含数量（AC20）。
4. **零回归**：`subagent_service=None` 构造协调层，跑一轮主对话，行为与改动前一致（AC24）。
5. **工具列表稳定**：加载 0 个角色与加载 5 个角色两种情况下，模型可见的工具名集合相同（AC6）。

**验证：** `python -m unittest tests.test_subagent_integration` 全绿。

---

## T31: 内置角色护栏

**文件：** `tests/test_subagent_builtin.py`（新建）
**依赖：** T6

**步骤：**
1. **硬编码内置角色名字清单**（当前只有 `explorer`），断言扫描结果与之完全一致——新增内置角色时这条当场红，逼人来登记（照 C11 `BuiltinSamplesTest` 先例）。
2. `explorer` 的 `tools` 只含只读工具，`permission_mode` 为 `strict`。
3. `explorer` 解析后 `warnings` 为空——**内置样板是用户看到的唯一范例，自己触发警告等于示范了不该学的写法**。
4. 断言 `explorer` 的 `description` 非空且长度在合理区间（防写成一个字）。

**验证：** `python -m unittest tests.test_subagent_builtin` 全绿。

---

## T32: 全量回归

**文件：** 无（只跑）
**依赖：** T1–T31

**步骤：**
1. `python -m compileall rhinecode tests`。
2. `python -m unittest discover -s tests`。
3. 记录新的用例总数与 skipped 数，与改动前对照（改动前 1478 项 / skipped 4）。
4. 有失败就修到全绿再往下走。

**验证：** 全量测试通过，skipped 仍为 4。

---

## T33: 文档回填

**文件：** `CLAUDE.md`（修改）、`docs/c13/README.md`（新建）
**依赖：** T32

**步骤：**
1. `CLAUDE.md` 能力表新增 C13 一行。
2. 架构表新增 `subagents/` 一层，⚠ 列写两条致命不变量：**权限必须派生不得改主引擎**、**加锁临界区只做纯内存读写**。
3. 「成对维护点」新增 4 条（frontmatter 字段 / `GLOBAL_DENIED_TOOLS` / 清单表头 ↔ 工具描述 / trace 事件），以及「`tools ↔ subagents` 是第四组包级互依」。
4. 「安全边界」新增子 Agent 系统的条款：ASK 一律自动拒绝、权限只能收紧、项目级角色的分发面、Hook 对子 Agent 全量生效、结论回流会把子 Agent 读到的内容带进主历史。
5. 「常用命令」与斜杠命令清单新增 `/agents`，按键说明新增 `Ctrl+B`。
6. 「已知后续工程项」新增 C13 spec 明确不做的那批。
7. `docs/c13/README.md`：四份文档导航 + 本章一句话 + 与 C11 `context: fork` 的术语区分说明。

**验证：** 人读一遍；`grep -c "C13" CLAUDE.md` 大于 0。

---

## T34: 端到端手测准备

**文件：** `docs/c13/acceptance/`（新建目录，本任务只建骨架）
**依赖：** T33

**步骤：**
1. 建 `docs/c13/acceptance/README.md`，说明验收记录的两栏格式（机器判到了什么 / 据此做的判断），照 C11 `acceptance/` 先例。
2. 按 `checklist.md` 列出待手测场景的空表，留给阶段六填。

**验证：** 文件存在且格式与 C11 的验收记录一致。

---

## 执行顺序

```
T1 ──┬─ T2 ─ T3
     ├─ T4 ─ T5          （T4 依赖 T2）
     ├─ T6
     ├─ T7 ─ T8
     └─ T12 ─ T13 ─ T14

T9（权限派生）   ─┐
T10（非交互模式） ─┼─▶ T15 ─ T16 ─▶ T17 ─ T18
T11（trace）     ─┘        │
                           ├─▶ T19 ─▶ T20 ─ T21
                           └─▶ T22 ─▶ T23 ─ T24

T25 ─ T26（命令层，可与上面并行）

T22 + T23 + T25 ─▶ T27 ─▶ T28 ─▶ T29 ─▶ T30
                                    T31（可并行）
                                        │
                                        ▼
                                   T32 ─ T33 ─ T34
```

T9 / T10 / T11 / T25 互不依赖，可任意顺序先做；它们都是**对既有代码的小改动**，
先做完能让后面的新模块有稳定地基。
