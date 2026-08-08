# C15 子 Agent 协作 Tasks

> 对应 `spec.md`（26 F / 9 N / 45 AC）与 `plan.md`（新叶子包 + 五处接线）。
> 共 **48 个任务，分五阶段**。每个任务自包含，带明确的验证方式。
>
> **每完成一个任务或一组逻辑相关的任务就提交一次**（`CLAUDE.md` 的既定约定）。

## 文件清单

| 操作 | 文件 | 职责 |
| --- | --- | --- |
| 新建 | `rhinecode/team/__init__.py` | 门面导出 |
| 新建 | `rhinecode/team/models.py` | 数据结构、枚举、常量 |
| 新建 | `rhinecode/team/board.py` | 共享任务清单（加锁 / 原子认领 / 环检测） |
| 新建 | `rhinecode/team/roster.py` | 花名册、待命历史保管、N3 降级 |
| 新建 | `rhinecode/team/mailbox.py` | 消息路由与投递 |
| 新建 | `rhinecode/team/gate.py` | `TeamGate`（实现 `SubAgentGateProtocol`） |
| 新建 | `rhinecode/team/render.py` | 注入消息标记块 / 清单文本 / 花名册文本 |
| 新建 | `rhinecode/team/service.py` | 门面 |
| 新建 | `rhinecode/tools/team_tasks.py` | 四个任务清单工具 |
| 新建 | `rhinecode/tools/send_message.py` | 发消息工具 |
| 修改 | `rhinecode/agent/gate.py` | `CompositeGate` |
| 修改 | `rhinecode/agent/loop.py` | 第三条拒绝文案 + `unattended` 参数 |
| 修改 | `rhinecode/subagents/runner.py` | 待命循环、传 `TeamGate` |
| 修改 | `rhinecode/subagents/service.py` | `name` 参数、重名校验、并发上限 |
| 修改 | `rhinecode/subagents/toolset.py` | 护栏注释（协作工具不入禁表） |
| 修改 | `rhinecode/subagents/report.py` | `/agents` 展示名字与待命 |
| 修改 | `rhinecode/tools/run_agent.py` | `name` 参数与描述 |
| 修改 | `rhinecode/tools/registry.py` | 注册五个新工具 |
| 修改 | `rhinecode/conversation.py` | 接线、`run_auto_wake`、清空、计数复位 |
| 修改 | `rhinecode/tui/app.py` | 自动唤起触发与展示 |
| 修改 | `rhinecode/commands/builtins.py` | `/tasks` 命令 |
| 修改 | `rhinecode/trace/models.py` | 四个新事件类型 |
| 修改 | `rhinecode/trace/reader.py` | 对应 `SUMMARIZERS` 表项 |
| 修改 | `rhinecode/bootstrap.py` | 构造 `TeamService` 并注入 |
| 修改 | `CLAUDE.md` | 能力表、架构表、成对维护点、安全边界 |
| 新建 | `tests/test_team_board.py` 等 8 个 | 见各阶段 |

---

# 阶段一：`team` 包地基（T1–T17）

> 全部是纯内存、零 IO、零外部依赖的代码，可独立测试。
> 这一阶段结束时 `team` 包能单独跑通全部单元测试，但还没接进任何地方。

## T1: 枚举与常量

**文件：** `rhinecode/team/models.py`
**依赖：** 无
**步骤：**
1. 建 `rhinecode/team/` 目录与空的 `__init__.py`（内容留到 T17）。
2. 定义 `TaskState`：`PENDING` / `IN_PROGRESS` / `COMPLETED`（值用小写字符串）。
3. 定义 `MemberState`：`RUNNING` / `IDLE` / `FAILED` / `CANCELLED` / `RETIRED`。
   加一个 `is_wakeable` 属性——**只有 `IDLE` 为真**。
4. 定义常量：`MAIN_NAME = "main"`、`MAX_IDLE_MEMBERS = 5`、
   `MAX_AUTO_WAKE_CHAIN = 5`。
5. 定义 `TASK_STATE_LABELS` / `MEMBER_STATE_LABELS` 两张中文展示名表。
   模块 docstring 里写明**为什么标签与枚举值分开**（枚举值同时是记录里的
   稳定标识，不该跟界面措辞变——与 C13 `STATUS_LABELS` 同理由）。

**验证：** `python -c "from rhinecode.team.models import MemberState; print(MemberState.IDLE.is_wakeable, MemberState.FAILED.is_wakeable)"` 输出 `True False`。

## T2: 四个数据类

**文件：** `rhinecode/team/models.py`
**依赖：** T1
**步骤：**
1. `BoardTask`（可变 dataclass）：`task_id: str`、`subject`、`description`、
   `state: TaskState`、`owner: str = ""`、`blocked_by: tuple[str,...] = ()`、
   `blocks: tuple[str,...] = ()`、`created_at` / `updated_at: float`。
   字段注释里写明 `task_id` **用小整数顺序号字符串**及其理由（plan 决策 9）。
2. `Envelope`（frozen dataclass）：`sender`、`recipient`、`summary`、`body`、
   `sent_at: float`、`read: bool = False`。注释写明 `sent_at` 与 `read`
   **由系统填充、不由模型给**。
3. `MemberEntry`（可变 dataclass）：`name`、`task_id`、`state: MemberState`、
   `inbox: list[Envelope]`、`history: list`（待命时的对话历史）、
   `wake_event: threading.Event`、`last_active: float`、`read_only: bool`。
   `read_only` 的注释要写明它**在委派时算好存下来**，为的是 F24 判定时
   不必反向依赖 `subagents`。
4. `MemberEntry` 加一个 `unread_count` 只读属性。

**验证：** `python -m compileall rhinecode/team` 通过；
构造一个 `MemberEntry` 并断言 `unread_count == 0`。

## T3: 清单骨架与基础读写

**文件：** `rhinecode/team/board.py`
**依赖：** T2
**步骤：**
1. 建 `TaskBoard` 类，持有 `threading.Lock`、`dict[str, BoardTask]`、
   `_next_id: int`。
2. 模块 docstring 写清 **N2 加锁不变量**：临界区只做纯内存读写，
   本类**刻意不持有任何回调**（与 C13 `TaskManager` 同一条结构性约定）。
3. `create(subject, description) -> BoardTask`：分配 `str(_next_id)` 并自增。
4. `get(task_id) -> Optional[BoardTask]`：返回**副本**（`dataclasses.replace`），
   避免调用方在锁外改到内部状态。
5. `snapshot() -> tuple[BoardTask, ...]`：按 `int(task_id)` 升序的副本元组。

**验证：** 建三条任务，`snapshot()` 返回的 `task_id` 依次是 `"1"`、`"2"`、`"3"`。

## T4: 清单的更新与删除

**文件：** `rhinecode/team/board.py`
**依赖：** T3
**步骤：**
1. 定义 `UpdateResult`（frozen dataclass）：`ok: bool`、`reason: str`、
   `task: Optional[BoardTask]`。
2. `update(task_id, *, state=None, subject=None, description=None, owner=None)`：
   逐字段可选更新，刷新 `updated_at`；任务不存在时返回 `ok=False` 并说明。
3. `remove(task_id) -> bool`：删除条目，**并从其它任务的 `blocked_by` /
   `blocks` 里摘除对它的引用**（否则会留下指向不存在任务的悬空依赖，
   而 `is_blocked` 会把它当成「永远未完成」，那条任务再也认领不了）。

**验证：** A 挡着 B，删掉 A 之后 `board.get("B").blocked_by` 为空。

## T5: 依赖与环检测

**文件：** `rhinecode/team/board.py`
**依赖：** T4
**步骤：**
1. 定义 `DepResult`（`ok` / `reason`）。
2. `add_dependency(task_id, blocked_by_id) -> DepResult`：
   在同一临界区内**双向**写入（`task.blocked_by` 与 `other.blocks`）。
   注释标注这是**成对维护点**，并写明为什么双向冗余（plan 决策 10）。
3. 环检测：写入前从 `blocked_by_id` 沿 `blocked_by` 深度优先搜索，
   若能到达 `task_id` 则拒绝，`reason` 里给出成环的路径。
   自己依赖自己也在此拒绝。
4. 注释写明：spec「不做的事」排除的是**环检测之外**的图算法，
   环检测本身必须做——否则两条任务互相挡着、谁都认领不了，
   而清单上看不出原因。

**验证：** `A→B`、`B→C` 后再加 `C→A`：返回 `ok=False` 且 `reason` 含成环路径；
此时 `A.blocked_by` **没有**被写脏。

## T6: 阻塞判定与原子认领

**文件：** `rhinecode/team/board.py`
**依赖：** T5
**步骤：**
1. `is_blocked(task_id) -> tuple[str, ...]`：返回 `blocked_by` 里**尚未完成**
   的任务 ID（全部完成时返回空元组）。
2. 定义 `ClaimResult`：`ok`、`reason`、`blocked_by: tuple[str,...]`、
   `current_owner: str` —— 四种结果要能被调用方区分（成功 / 不存在 /
   被挡住 / 已被某某认领）。
3. `claim(task_id, owner) -> ClaimResult`：**在同一个临界区内**依次做
   ① 存在性 ② `is_blocked` 为空 ③ `owner == ""`，全过才写入
   `owner` 与 `state = IN_PROGRESS`。
4. 注释写明：三步分开做会有 TOCTOU 窗口，**两个队员会同时认领成功**
   （AC10 就是钉这条的）。

**验证：** 起 20 个线程同时 `claim` 同一条任务，
断言**恰好 1 个** `ok=True`，其余 `current_owner` 都等于那个成功者。

## T7: 清单测试（基础）

**文件：** `tests/test_team_board.py`
**依赖：** T6
**步骤：**
1. `create` / `get` / `snapshot` 的顺序与内容。
2. `update` 各字段；不存在的 ID 返回 `ok=False`。
3. `remove` 会摘除反向引用（T4 那条）。
4. `get` 返回副本：改动返回值不影响板内状态。

**验证：** `python -m unittest tests.test_team_board` 全绿。

## T8: 清单测试（依赖 / 环 / 并发）

**文件：** `tests/test_team_board.py`
**依赖：** T7
**步骤：**
1. `add_dependency` 双向写入。
2. 环检测三例：直接自依赖、两跳环、三跳环；**并断言被拒时状态没被写脏**。
3. `is_blocked` 随前置任务完成而变空。
4. `claim` 四种结果各一条用例。
5. **并发认领**：20 线程同时 claim，恰好一个成功（AC10）。
6. **结构护栏**：遍历 `TaskBoard` 实例属性，断言不存在 callable 成员
   （N2 的结构性保证，与 C13 `TaskManager` 同形）。

**验证：** `python -m unittest tests.test_team_board` 全绿；
并发用例重复跑 10 次不出现偶发失败。

## T9: 花名册骨架与命名

**文件：** `rhinecode/team/roster.py`
**依赖：** T2
**步骤：**
1. `Roster` 类，持有 `threading.Lock` 与 `dict[str, MemberEntry]`。
   模块 docstring 同样写明 N2 加锁不变量。
2. 构造时自动放入 `main` 特殊条目（`state=RUNNING`、无 `wake_event`）。
   注释写明**为什么把 main 放进花名册而不特判**（plan 决策 12：
   让 `send_message` 的路由只有一条代码路径）。
3. 定义 `RegisterResult`（`ok` / `reason` / `name`）。
4. `register(name, task_id, read_only) -> RegisterResult`：
   名字已存在（**含终态与 `main`**）即失败，`reason` 说明冲突对象的当前状态。
5. `suggest_name(role) -> str`：`<role>-1`、`-2`… 取第一个不冲突的；
   对名字做基本清洗（去空白、非法字符）。

**验证：** 同一个名字注册两次，第二次 `ok=False`；
`suggest_name("explorer")` 连续调用（配合注册）得到 `explorer-1`、`explorer-2`。

## T10: 待命、终态与唤醒取历史

**文件：** `rhinecode/team/roster.py`
**依赖：** T9
**步骤：**
1. `mark_idle(name, history) -> tuple[str, ...]`：置 `IDLE`、存 `history`、
   刷新 `last_active`；返回**因此被降级的名字**（T11 实现，先返回空元组）。
2. `mark_terminal(name, state)`：置终态并**清空 `history`**（释放内存）。
   断言传入的 state 不是 `RUNNING`/`IDLE`。
3. `wake(name) -> Optional[list]`：置回 `RUNNING`、返回保管的历史；
   不可唤醒时返回 `None`。
4. `snapshot()` / `get(name)`：返回副本供展示。
5. `clear()`：清空全部条目并重建 `main`（F25）。

**验证：** 队员 `mark_idle` 后 `state.is_wakeable` 为真、`wake()` 拿回同一份历史；
`mark_terminal` 之后 `wake()` 返回 `None` 且 `history` 已空。

## T11: N3 降级

**文件：** `rhinecode/team/roster.py`
**依赖：** T10
**步骤：**
1. 在 `mark_idle` 末尾：若 `IDLE` 数量超过 `MAX_IDLE_MEMBERS`，
   把 `last_active` 最早的若干个转成 `RETIRED` 并清空历史。
2. 把被降级的名字**作为返回值**交给调用方。
3. 注释写明 ⚠ **返回值不能被丢弃**——spec N3 明令降级必须看得见，
   丢掉返回值就成了它禁止的静默降级。

**验证：** 连续 `mark_idle` 6 个队员（上限 5），第 6 次返回值含第 1 个的名字，
且那个队员 `state == RETIRED`、`history` 为空。

## T12: 花名册测试

**文件：** `tests/test_team_roster.py`
**依赖：** T11
**步骤：**
1. 注册、重名失败（含与 `main` 重名）、自动命名不冲突。
2. 待命 / 终态 / 唤醒的状态流转与历史保管释放。
3. N3 降级：上限、挑中的是最久未活动的那个、返回值非空。
4. `clear()` 之后只剩 `main`。
5. 结构护栏：实例属性无 callable 成员。

**验证：** `python -m unittest tests.test_team_roster` 全绿。

## T13: 消息投递

**文件：** `rhinecode/team/mailbox.py`
**依赖：** T11
**步骤：**
1. `Mailbox` 类，持有 `Roster` 引用（信箱数据存在 `MemberEntry.inbox` 里，
   本类只做路由，不另存一份）。
2. 定义 `SendResult`（`ok` / `reason`）。
3. `send(sender, recipient, body, summary) -> SendResult` 按**三段式**实现：
   - **锁内**：校验收件人存在且 `state` 可收、追加 `Envelope`
     （补 `sent_at`、`read=False`）、记下要唤醒的 `Event`；
   - **出锁**；
   - **锁外**：`event.set()`。
4. 在 `set()` 那一行上方写死注释：⚠ **必须在锁外**——它唤醒等待线程属跨线程
   调度，落进锁内会与 Textual 阻塞式 `call_from_thread` 组成确定性死锁
   （C11/C12/C13 三次同源事故）。
5. 三种失败：收件人不在花名册（**列出全部名字**）、收件人处于终态
   （说明是失败 / 取消 / 已退休哪一种）、发给自己。

**验证：** 向不存在的名字发送返回 `ok=False` 且 `reason` 含现有名字列表；
向 `RETIRED` 队员发送失败并指明「已退休」。

## T14: 未读的取走与探测

**文件：** `rhinecode/team/mailbox.py`
**依赖：** T13
**步骤：**
1. `take_unread(name) -> tuple[Envelope, ...]`：取走未读并**就地置 `read=True`**
   （取走即置位 → 重复调用幂等，同一条消息不会注入两遍）。
2. `has_unread(name) -> bool`：只读探测，供 TUI 轮询用（**不改状态**）。
3. 注释写明幂等性的重要性：与 C13 `take_deliverables` 同一条契约。

**验证：** 连发 3 条，`take_unread` 第一次拿到 3 条、第二次拿到 0 条；
其间 `has_unread` 由 `True` 变 `False`。

## T15: 信箱测试

**文件：** `tests/test_team_mailbox.py`
**依赖：** T14
**步骤：**
1. 投递成功路径与三种失败路径。
2. `take_unread` 幂等；`has_unread` 不改状态。
3. `sent_at` 被系统填充、`read` 默认假。
4. **锁外唤醒的结构护栏**：用一个会在 `set()` 里回调进 `Mailbox` 的假 Event
   （或用可重入探测）证明 `set()` 发生时**锁已释放**。
   ⚠ 按 `docs/internals/testing.md` 的记载，死锁类护栏**必须用完成计数
   而不是布尔标志**——同线程版本在 `RLock` 下会静默通过。

**验证：** `python -m unittest tests.test_team_mailbox` 全绿。

## T16: 渲染

**文件：** `rhinecode/team/render.py`
**依赖：** T14
**步骤：**
1. `render_incoming(envelopes) -> Message`：
   - `role="user"`、`display_content=""`；
   - 正文包在 `<teammate-message from="..." at="...">` 标记块里，含摘要与正文；
   - 多条未读合并进一条消息（每条一个标记块）。
2. 在函数 docstring 里写死三条要求及理由（plan「`render_incoming` 是 F12 的
   全部落实」那一段），并标注 ⚠ **成对维护点：本函数与 `send_message` 工具的
   `description` 必须同口径**，说明这是本项目第四次踩同一个坑。
3. `render_board(tasks) -> str`：ID / 状态 / 标题 / 认领人 / 阻塞来源，
   被挡住的明确标出。
4. `render_roster(members) -> str`：名字 / 状态 / 角色 / 未读数。

**验证：** 渲染一条消息，断言输出含 `<teammate-message`、发件人名字、
且 `display_content == ""`；标记块名字与 `<subagent-result>` **不同**。

## T17: 门面

**文件：** `rhinecode/team/service.py`、`rhinecode/team/__init__.py`
**依赖：** T16
**步骤：**
1. `TeamService`：构造 `Roster` / `TaskBoard` / `Mailbox` 并暴露它们；
   额外持有 `auto_wake_chain: int`。
2. 方法：`send(...)`、`take_unread(...)`、`has_unread(...)`、
   `register_member(...)`、`board` 属性、`clear()`、
   `bump_auto_wake() -> int`、`reset_auto_wake()`、
   `can_auto_wake() -> bool`（对比 `MAX_AUTO_WAKE_CHAIN`）。
3. `can_send_in_plan_stage(recipient) -> tuple[bool, str]`：F24 判定
   ——收件人是 `main` 或 `read_only` 队员时放行，否则给出可发送对象列表。
4. `__init__.py` 导出 `TeamService` 与少量类型；**不导出内部模块**。

**验证：** `python -c "from rhinecode.team import TeamService; s=TeamService(); print(s.can_auto_wake())"` 输出 `True`。

---

# 阶段二：闸门与工具（T18–T26）

## T18: `CompositeGate`

**文件：** `rhinecode/agent/gate.py`
**依赖：** 无（只依赖既有协议）
**步骤：**
1. 新增 `CompositeGate(gates)`：`take_pending` 顺序拼接、`has_awaited` 取 `any`、
   `wait_any` 任一为真即真、`describe_awaited` 拼接非空描述。
2. `wait_any` 的实现要**逐个短路**：第一个返回 `True` 就返回，
   不要等所有 gate。
3. docstring 写明：本类**留在 `agent/` 一侧**的理由与协议相同
   ——`agent` 不得依赖 `subagents` / `team`，否则撞循环导入（C13 实测过）。
4. 加进 `__all__`。

**验证：** 用两个假 gate 构造，断言 `take_pending` 返回两者结果的拼接、
`has_awaited` 在任一为真时为真。

## T19: `TeamGate`

**文件：** `rhinecode/team/gate.py`
**依赖：** T17、T18
**步骤：**
1. `TeamGate(service, name)`：
   - `take_pending()` → `service.take_unread(name)` 非空时返回
     `[render_incoming(...)]`，否则空列表；
   - `has_awaited()` → **恒 `False`**；
   - `wait_any()` → **恒 `False`**；
   - `describe_awaited()` → `""`。
2. 在 `has_awaited` 上方写死注释：⚠ 返回 `True` 会让队员为「可能有人给我
   发消息」赖着不收工、永远停不下来。**等消息发生在待命状态，不在 Agent Loop
   里**（plan 决策 4）。

**验证：** `isinstance(TeamGate(...), SubAgentGateProtocol)` 为真；
无未读时 `take_pending()` 返回空列表。

## T20: 闸门测试

**文件：** `tests/test_team_gate.py`
**依赖：** T19
**步骤：**
1. `TeamGate` 取未读并渲染；幂等（第二次为空）。
2. `TeamGate.has_awaited` 恒假 —— **单独一条用例并写明理由**，
   防止后来的人「顺手修正」它。
3. `CompositeGate` 的四个方法。
4. `CompositeGate([SubAgentGate, TeamGate])` 组合行为：
   子 Agent 结论与队友消息**都能注入**，且顺序稳定。

**验证：** `python -m unittest tests.test_team_gate` 全绿。

## T21: 任务工具（建 / 列）

**文件：** `rhinecode/tools/team_tasks.py`
**依赖：** T17
**步骤：**
1. `TaskCreateTool`：`read_only=False`、`system_serial=True`、`plan_safe=True`。
   参数 `subject`（必填）、`description`（必填）。
2. `TaskListTool`：`read_only=True`、`system_serial=False`、`plan_safe=True`，
   无参数，返回 `render_board` 的文本。
3. 两者构造时接 `TeamService` 与一个 `whoami` 回调（取当前 Agent 名字，
   用于 `owner` 缺省值）。
4. 模块 docstring 写明：**不进权限管线**的论证（与 `run_agent` / `load_skill`
   同先例）与 `plan_safe=True` 的兑现方式。

**验证：** 构造工具、`execute` 建两条任务再列出，输出含两条的 ID 与标题。

## T22: 任务工具（查 / 改）

**文件：** `rhinecode/tools/team_tasks.py`
**依赖：** T21
**步骤：**
1. `TaskGetTool`：`read_only=True`，参数 `task_id`。
2. `TaskUpdateTool`：`read_only=False`、`system_serial=True`，参数
   `task_id`、可选 `status` / `subject` / `description` / `owner` /
   `add_blocked_by`（数组）。`status: "deleted"` 走删除（对齐 Claude Code）。
3. **认领走 `board.claim`**：`owner` 字段被设置时用原子认领，
   四种结果各自转成可读文案；被挡住时**列出挡它的任务 ID**（F7/AC8）。
4. 工具描述里写明「优先做 ID 小的、没被挡住的任务」（对齐 Claude Code 的提示）。

**验证：** 认领一条被挡住的任务，`ToolResult.ok=False` 且 output 含阻塞来源；
前置完成后再认领成功。

## T23: 发消息工具

**文件：** `rhinecode/tools/send_message.py`
**依赖：** T17
**步骤：**
1. `SendMessageTool`：`read_only=False`、`system_serial=True`、`plan_safe=True`。
   参数 `to`（必填）、`message`（必填）、`summary`（必填，5–10 词）。
2. `execute` 调 `service.send(whoami(), to, message, summary)`，
   失败原样回灌 `reason`。
3. 描述里写清四件事，**与 `render_incoming` 的标记块同口径**（成对维护点）：
   - 你的正文输出**对其它 Agent 不可见**，要沟通必须调本工具；
   - 消息会自动送达，对方不需要查收件箱，你也不要去查；
   - 按**名字**指代对方；名字在对方干完之后**依然有效**，
     再发一条就会把它从原上下文唤醒继续干；
   - `to: "main"` 是发给主对话。
4. 在描述与 docstring 里都写明这条 ⚠ 成对维护点。

**验证：** 发一条消息后，收件人 `has_unread` 为真且 `take_unread` 拿到的
正文与摘要一致。

## T24: 发消息工具的规划阶段分支

**文件：** `rhinecode/tools/send_message.py`
**依赖：** T23
**步骤：**
1. `execute(args, plan_stage=False)`：`plan_stage` 为真时先调
   `service.can_send_in_plan_stage(to)`。
2. 不通过时返回 `ok=False`，文案复用 C13 F19a 的形态：
   说明当前处于规划阶段、为什么不能发给它（唤醒一个能写文件的队员会绕过
   「批准前不动手」的承诺）、**并列出现在能发给谁**。
3. 注释写明：声明 `plan_safe=True` 等于承诺规划阶段不产生副作用，
   本分支就是兑现方式（C13 的既有契约）。

**验证：** `plan_stage=True` 时发给一个非只读队员返回 `ok=False` 且文案含
可发送对象；发给 `main` 成功。

## T25: 工具测试

**文件：** `tests/test_team_tools.py`
**依赖：** T24
**步骤：**
1. 五个工具的成功路径与主要失败路径。
2. 认领被挡住的任务（AC8）、并发认领只有一个成功。
3. `plan_stage` 分支两条（放行 / 拒绝）。
4. **`SameVoiceTest`**：断言 `SendMessageTool.description` 与
   `team/render.py` 的标记块口径一致——至少覆盖「输出对别人不可见 /
   自动送达不必查收 / 按名字指代 / 名字在完成后仍有效」四点。
   参照 `tests/test_subagent_tool.py::SameVoiceTest` 的写法。
5. 五个工具都断言 `read_only` / `system_serial` / `plan_safe` 的取值符合设计。

**验证：** `python -m unittest tests.test_team_tools` 全绿。

## T26: 注册与工具集护栏

**文件：** `rhinecode/tools/registry.py`、`rhinecode/subagents/toolset.py`
**依赖：** T25
**步骤：**
1. 在注册中心登记五个新工具（跟随既有注册方式，接 `TeamService`）。
2. `toolset.py` 的 `GLOBAL_DENIED_TOOLS` **不加**这五个——
   在该常量的注释里补一句：协作工具**刻意不在此表内**（F22 要求它们对
   子 Agent 可见），并说明为什么 `run_agent` / `load_skill` 仍必须留着。
3. 确认 `permission/adapter.py` **不需要**登记（它们落 `other` 分支，
   仍可被 `deny` 规则禁掉）——在 `_TOOL_MAP` 旁补一句说明，免得后来的人以为漏了。

**验证：** 跑既有的 `python -m unittest tests.test_subagent_toolset`：
断言子 Agent 的最终工具集**含**五个协作工具、**不含** `run_agent` 与 `load_skill`。

---

# 阶段三：队员侧的待命与唤醒（T27–T33）

## T27: 委派的 `name` 参数与重名校验

**文件：** `rhinecode/subagents/service.py`
**依赖：** T17
**步骤：**
1. `SubAgentService.__init__` 增加 `team: Optional[TeamService] = None`。
2. `delegate(...)` 增加 `name: Optional[str] = None` 参数。
3. 在**工具集算完之后、并发上限之前**插入命名段：
   - `name` 为空 → `roster.suggest_name(角色名)`；
   - 调 `roster.register(name, task_id?, read_only)`，失败即返回
     `DelegateOutcome(ok=False, ...)`，**不起线程、不建任务记录**（F2/AC3）。
4. `read_only` 由已算好的最终工具集推出（复用既有的 `_writable_tools`：
   空列表即全只读）。
5. ⚠ 顺序注释：命名必须在**工具集算完之后**（要 `read_only`）、
   **建任务记录与建工作区之前**（失败时不该留下任何痕迹）。

**验证：** 用同一个 `name` 委派两次，第二次 `ok=False`；
`tasks.snapshot()` 长度仍为 1、花名册里只有一个该名字的条目。

## T28: 并发上限与花名册联动

**文件：** `rhinecode/subagents/service.py`、`rhinecode/subagents/models.py`
**依赖：** T27
**步骤：**
1. `MAX_CONCURRENT` 由 `3` 改为 `5`，注释写明理由
   （团队场景下一个 Lead 派 4 个人是常见形态；plan 决策 13），
   并写明**待命队员不占名额**（F15）——`running_count()` 只数 `RUNNING`。
2. 委派成功后把名字写进 `TaskRecord`（新增 `member_name: str = ""` 字段），
   供 `/agents` 展示与结算时回查花名册。
3. `cancel_all_for_session_switch` 之后同步调 `team.clear()`
   （或由协调层统一做——**二选一并在注释里写明由谁负责**，避免两处都做或都不做）。

**验证：** 起 5 个队员成功、第 6 个失败；把其中 2 个转待命后再委派 2 个成功
（证明待命不占名额）。

## T29: 委派工具的 `name` 参数

**文件：** `rhinecode/tools/run_agent.py`
**依赖：** T28
**步骤：**
1. `parameters` 增加 `name`（可选字符串）。
2. 描述里说明：给名字之后**可以用 `send_message` 按名字找它**，
   包括它干完之后——再发一条消息就能把它从原上下文唤醒继续干，
   **不必重新委派、不必重新交代背景**。
3. `execute` 把 `name` 透传给 `service.delegate`。
4. ⚠ 这条描述与 `send_message` 的描述、`render_incoming` 的标记块
   构成三处同口径点，在注释里标注。

**验证：** 既有 `tests/test_subagent_tool.py` 全绿；
新增一条断言 `"name"` 在 `parameters["properties"]` 中。

## T30: 运行器接入 `TeamGate`

**文件：** `rhinecode/subagents/runner.py`
**依赖：** T19、T28
**步骤：**
1. `SubAgentRuntime` 增加 `team: object = None` 字段（带 docstring 说明）。
2. `run_subagent` 里构造 `TeamGate(runtime.team, member_name)`，
   通过 `agent.run(...)` 的 `options.subagent_gate` 传入。
   ⚠ `runtime.team` 为 `None` 时**不传**（保持 `NullGate`）——
   这是 N5 零回归的依据：不启用 team 的环境行为逐字不变。
3. 从 `record.member_name` 取名字。

**验证：** 既有 `tests/test_subagent_*.py` 全绿（它们不构造 team，走 `NullGate`）；
新增一条：构造 team 时，队员在下一轮迭代能看到消息。

## T31: 待命循环骨架

**文件：** `rhinecode/subagents/runner.py`
**依赖：** T30
**步骤：**
1. 把 `run_subagent` 里「跑一次 `agent.run` + 收尾」的部分抽成内部函数
   `_run_once(history) -> (stop_reason, turns, conclusion)`。
2. 外层改成 `while True` 循环，按 plan 的伪代码组织：
   跑一次 → 判停止原因 → 非自然完成则 `mark_terminal` 并 `break`。
3. **`TaskStatus` 一个成员都不加**——任务在自然停止时照常置 `COMPLETED`。
   在此处写死注释说明理由（plan 决策 2）：加一个 `is_terminal` 为假的 `IDLE`
   会让主 Agent 每次收工都去等一个已待命的队员，**永远等不完**。

**验证：** 不发任何消息时，队员跑完即待命，`tasks` 里状态是 `COMPLETED`、
花名册里状态是 `IDLE`；主 Agent 此时能正常收工（不卡在闸门）。

## T32: 唤醒续跑

**文件：** `rhinecode/subagents/runner.py`
**依赖：** T31
**步骤：**
1. 待命后调 `roster.mark_idle(name, history)`，**消费它的返回值**：
   有被降级的名字就通过既有的通知通道告诉用户（N3/AC36）。
2. 通过既有的完成通知通道告诉主对话「我空闲了」（F13 的「通知 Lead」那一半）。
3. `record.cancel_event` 与 `wake_event` **同时等**：
   用 `wake_event.wait(timeout=短)` 循环 + 检查 `cancel_event`，
   或用两个 Event 的等价组合。被取消时 `mark_terminal(CANCELLED)` 并退出。
4. 醒来后 `history = roster.wake(name)`；`None`（已被降级）时按终态退出。
5. 继续下一次 `_run_once`——**未读消息由下一轮 `TeamGate.take_pending` 注入**，
   这里不手工注入（避免两条注入路径导致消息重复）。
6. 每次唤醒 `tasks` 记录的状态回到 `RUNNING`、轮次从 0 重新计（F16）。

**验证：** 队员待命后发一条消息，它继续运行并在新一轮里能引用先前对话的内容；
`max_turns=3` 的队员唤醒后**又能跑满 3 轮**。

## T33: 待命与唤醒测试

**文件：** `tests/test_team_wake.py`
**依赖：** T32
**步骤：**
1. 自然停止 → 待命；失败 / 取消 / 耗尽轮次 → 终态不可唤醒（F14/AC18）。
2. 唤醒续跑保留历史（AC17）。
3. 轮次预算重置（AC20）。
4. **「有待命队员时主 Agent 能正常收工」**——钉住 plan 风险 1，
   用例注释里写明这条为什么不能删。
5. 待命期间被 `Esc` 取消 → 转 `CANCELLED`。
6. N3 降级发生时通知被产出（返回值被消费）。

**验证：** `python -m unittest tests.test_team_wake` 全绿。

---

# 阶段四：主对话侧的自动唤起（T34–T41）

## T34: 第三条拒绝文案

**文件：** `rhinecode/agent/loop.py`
**依赖：** 无
**步骤：**
1. 新增 `DENIED_UNATTENDED_FEEDBACK`，措辞按 plan 接线点 ④：
   用户当前不在场 / 不是有人拒绝你 / 重试无用 /
   **现在能做的是回消息给队友、或把「需要用户批准 X」留在这里**。
2. `RunOptions` 增加 `unattended: bool = False`；`_execute` 与 `_execute_serial`
   透传，在 `decision == ASK and not interactive` 分支按它选文案。
3. ⚠ 与 C13 那条一样**不置 `user_denied`**——在代码旁写明理由
   （那个标志会让下一轮 `tools=None`，对无人轮是错的：它应该带着工具
   继续做能做的事）。
4. 三条文案的语义差异写成一段集中注释（人否决 / 子 Agent 环境 / 用户不在场）。

**验证：** `python -m compileall rhinecode/agent` 通过；
构造 `unattended=True` 的运行，ASK 分支回灌的是新文案。

## T35: 无人轮文案测试

**文件：** `tests/test_team_unattended.py`
**依赖：** T34
**步骤：**
1. 三条文案**两两不同**（AC24），且新文案不含「停止推进 / 不要重试」这类
   属于 `DENIED_BY_USER_FEEDBACK` 的指令词。
2. `unattended=True` 时 ASK 被自动拒绝、`ask` 回调**一次都没被调用**（AC22）。
3. **反证**：`unattended=False`（正常轮）时同一个操作 `ask` **被调用**（AC23）。
4. `unattended=True` 时 `user_denied` **不置位**，下一轮**仍然发工具**。

**验证：** `python -m unittest tests.test_team_unattended` 全绿。

## T36: 协调层接线

**文件：** `rhinecode/conversation.py`
**依赖：** T19、T30
**步骤：**
1. `__init__` 增加 `team_service=None` 参数并持有。
2. `subagent_gate()` 改为：team 存在时返回
   `CompositeGate([SubAgentGate(...), TeamGate(team, MAIN_NAME)])`，
   否则原样返回 `SubAgentGate`（N5 零回归）。
3. 构造 `SubAgentRuntime` 时传 `team=self.team_service`。
4. 新增只读方法供 TUI 用：`team_has_unread_for_main()`、
   `team_can_auto_wake()`、`team_snapshot()`、`board_text()`。

**验证：** 不传 `team_service` 时 `subagent_gate()` 返回的仍是 `SubAgentGate`；
传了则是 `CompositeGate`。

## T37: `run_auto_wake`

**文件：** `rhinecode/conversation.py`
**依赖：** T34、T36
**步骤：**
1. 把 `_run` 里「组装系统提示 / 构造 ask / 调 `Agent.run`」的公共部分抽出，
   使 `_run` 与新方法共用（**不要复制一份**——两份会各自演化，
   而分叉处正好是安全行为）。
2. `run_auto_wake()`：
   - 不追加任何用户消息；
   - `RunOptions(interactive=False, unattended=True)`；
   - `ask` 传 `None`；
   - 其余（历史、系统提示、闸门、上下文管理、存档）与 `_run` 相同。
3. 它照常经过 `_wrap_events`（预授权的授予与撤销必须成对，
   `finally` 里 `restore_turn_rules(token)`——CLAUDE.md 的既有成对维护点）。
4. 事件流首尾各插一个可辨识标记（供 TUI 展示自动轮，F21）。

**验证：** 调 `run_auto_wake()` 产出的事件流能正常跑完；
其间未读消息被注入历史（第一轮 `take_pending`）。

## T38: 清空与计数复位

**文件：** `rhinecode/conversation.py`
**依赖：** T37
**步骤：**
1. `clear()` 与 `_resume_stream` 里增加 `team_service.clear()`
   （**与 Skill 激活态清空、子 Agent 取消放在一起**，CLAUDE.md 已登记
   「`clear()` 与 `_resume_stream` 两处必须清空 Skill 激活态」，本章再加一项）。
2. 用户提交消息的入口调 `team_service.reset_auto_wake()`（F20 的计数复位）。
3. 在两处都补注释说明「漏改的后果」：`clear` 漏了会让上一轮的队员消息
   出现在新对话里；计数漏复位会让自动唤起在用户回来后仍处于停用状态。

**验证：** `/clear` 之后花名册只剩 `main`、清单为空、`can_auto_wake()` 为真。

## T39: TUI 触发自动唤起

**文件：** `rhinecode/tui/app.py`
**依赖：** T37
**步骤：**
1. 在既有的 `_poll_subagents` 里追加一段（**不新增 `set_interval`**，
   在注释里写明这是 N6 的落实方式）。
2. 判定顺序：① 主对话空闲（没有 Worker 在跑）② `team_has_unread_for_main()`
   ③ `team_can_auto_wake()`。
3. ⚠ **必须先判空闲**——`run_worker` 是 `exclusive=True`，
   不判会把正在跑的 Worker 挤掉，用户正在等的回答凭空消失（plan 风险 3）。
   在这一行上方写死注释。
4. 满足则 `bump_auto_wake()` 并
   `run_worker(lambda: self._do_stream(self._manager.run_auto_wake()), thread=True)`。
5. 整段仍在既有的 `try/except` 内——观测与通知设施绝不能打断定时器。

**验证：** 手工构造一条发给 `main` 的未读消息，
在空闲状态下 0.5 秒内观察到自动轮启动。

## T40: 自动轮的展示

**文件：** `rhinecode/tui/app.py`
**依赖：** T39
**步骤：**
1. 自动轮开始时在聊天区插一行醒目提示：
   `自动唤起（第 N/5 次）· 来自 <发件人>`（F21/AC26）。
2. 无人轮里有操作被拒绝时，结束后补一行汇总：
   `本轮有 N 个操作因无人值守被拒绝——你回来后可以让我重试`。
3. 达到连锁上限时提示一次「已达自动唤起上限，等你回来」，
   **用标志位防刷屏**（轮询每 0.5 秒跑一次，不防会刷满屏）。
4. 用户提交消息时清掉该标志位。
5. ⚠ 所有插入聊天区的文本走 `tui/widgets.py` 的 `escape`
   （CLAUDE.md 的硬不变量：绝不用 rich 那版）。

**验证：** 跑一遍自动唤起，界面上能看到起止提示；
达到上限后提示**只出现一次**。

## T41: 自动唤起测试

**文件：** `tests/test_team_auto_wake.py`
**依赖：** T40
**步骤：**
1. 空闲 + 有未读 → 触发（AC21）。
2. **正在跑时不触发**（钉住 plan 风险 3，含反证）。
3. 连锁上限：达到后停止，用户提交消息后复位恢复（AC25）。
4. 无人轮里 ASK 被拒（AC22）与正常轮弹面板（AC23）的对照。
5. 上限提示只出现一次。

**验证：** `python -m unittest tests.test_team_auto_wake` 全绿。

---

# 阶段五：观测、命令与收尾（T42–T48）

## T42: 行为记录事件类型

**文件：** `rhinecode/trace/models.py`
**依赖：** T17
**步骤：**
1. `TraceEventType` 新增四个：`TEAM_MESSAGE`（发/收）、`TEAM_TASK`（清单变更）、
   `TEAM_MEMBER`（注册/待命/唤醒/降级）、`AUTO_WAKE`（自动轮起止与结果）。
2. 在枚举旁标注 ⚠ **成对维护点**：必须同步 `trace/reader.py` 的 `SUMMARIZERS`，
   漏了不报错、只会显示成「（未登记类型）」。

**验证：** `python -c "from rhinecode.trace.models import TraceEventType; print(TraceEventType.AUTO_WAKE)"` 正常。

## T43: 阅读器摘要

**文件：** `rhinecode/trace/reader.py`
**依赖：** T42
**步骤：**
1. `SUMMARIZERS` 增加四个表项，每条一行可读摘要
   （谁发给谁 / 哪条任务变成什么 / 谁进入什么状态 / 第几次自动唤起及结果）。
2. 在各埋点处经 `_safe_emit` 漏斗产出事件（N7）。

**验证：** 跑一遍协作流程产出 trace，
`python -m rhinecode.trace.reader <文件>` 输出里**没有**「（未登记类型）」。

## T44: `/tasks` 命令

**文件：** `rhinecode/commands/builtins.py`
**依赖：** T36
**步骤：**
1. 登记一条 `CommandSpec`：名字 `tasks`，别名 `board`，纯只读，
   描述与用法齐备（c10 单一注册来源，补全 / 帮助 / 高亮自动生效）。
2. 处理函数调 `manager.board_text()` 展示清单；
   清单为空时给一句明确的空态提示。
3. team 未启用时提示「当前未启用子 Agent 协作」。

**验证：** 启动后输入 `/tasks` 显示清单；`/help` 里能看到它；Tab 能补全。

## T45: `/agents` 扩展

**文件：** `rhinecode/subagents/report.py`
**依赖：** T36
**步骤：**
1. 任务行增加**队员名字**与**花名册状态**（运行中 / 待命 / 已退休…）。
2. 待命队员单独一段列出，标明未读消息数。
3. ⚠ 改动用**带断言的替换**，改完**对着实际渲染输出看一眼**——
   C14 踩过：不带断言的字符串替换没匹配上就静默跳过了，
   单元测试不会替你看那行长什么样。

**验证：** 起一个队员并让它待命，`/agents` 输出里能看到名字与「待命」，
且**打印出来人工确认过**。

## T46: 装配

**文件：** `rhinecode/bootstrap.py`
**依赖：** T26、T36
**步骤：**
1. 构造 `TeamService`，注入协调层与 `SubAgentService`。
2. 注册五个协作工具——位置在 `LoadSkillTool` 注册**之后**、
   `connect_all` **之前**（跟随既有窄窗口的约束）。
3. ⚠ 装配顺序注释必须随代码走（CLAUDE.md 硬要求）：
   写明本章新增的两步为什么卡在这个位置。
4. team 未启用（非 DeepSeek 工具模式）时整段跳过，工具不注册。

**验证：** `rhine` 正常启动；`/tasks` 与 `/agents` 可用；
非工具模式下启动不报错且没有协作工具。

## T47: 端到端集成测试

**文件：** `tests/test_team_integration.py`
**依赖：** T46
**步骤：**
1. **场景一（AC43）**：建 4 条带依赖的任务 → 两个具名队员各自认领 →
   完成解锁后续 → 队员之间发生一次直接消息 → 清单终态与实际一致。
2. **场景二（AC44）**：后台队员发消息给 `main` → 空闲触发自动轮 →
   主对话回消息 → 队员继续并完成。
3. **场景三（AC45）**：队员做完第一件事待命 → 发消息指派第二件 →
   不重新交代背景就接着干，产出里能看到引用了第一件事的上下文。
4. **Hook 集成**：确认工具级三事件对协作工具照常分发
   （参照 `test_subagent_integration.py::HookIntegrationTest`）。

**验证：** `python -m unittest tests.test_team_integration` 全绿。

## T48: 全量回归与文档同步

**文件：** `CLAUDE.md`、`docs/c15/README.md`
**依赖：** T47
**步骤：**
1. `python -m compileall rhinecode tests` + `python -m unittest discover -s tests`
   **全绿**，记下新的测试总数。
2. `CLAUDE.md` 更新四处：
   - 能力表加 C15 一行；
   - 架构表加 `team/` 一层（含 ⚠ 致命不变量：加锁临界区只做纯内存读写、
     `Event.set()` 在锁外；`TeamGate.has_awaited` 恒假）；
   - **成对维护点**新增四条：`send_message` 描述 ↔ `<teammate-message>` 标记块、
     `blocked_by` ↔ `blocks` 双向、trace 事件 ↔ `SUMMARIZERS`、
     `clear()` 与 `_resume_stream` 再加一项清空；
   - 安全边界新增 C15 一组（spec 的 7 条）。
3. 写 `docs/c15/README.md`（导航 + 「零基础版：团队是什么」+ 关键决策清单）。
4. `docs/todo/README.md` 按需登记本章发现的新待办。

**验证：** 全量测试全绿；`git diff` 复核 `CLAUDE.md` 四处都改到了。

---

## 执行顺序

```
阶段一（team 包，可完全独立开发与测试）
  T1 → T2 → ┬→ T3 → T4 → T5 → T6 → T7 → T8      （清单）
            ├→ T9 → T10 → T11 → T12              （花名册）
            │        ↓
            └→      T13 → T14 → T15              （信箱，依赖花名册）
                     ↓
                    T16 → T17                    （渲染 → 门面）

阶段二（闸门与工具）
  T18 ─┐
  T17 ─┴→ T19 → T20
       └→ T21 → T22 ─┐
       └→ T23 → T24 ─┴→ T25 → T26

阶段三（队员侧）
  T17 → T27 → T28 → T29
  T19 ─────────┴→ T30 → T31 → T32 → T33

阶段四（主对话侧）
  T34 → T35                （文案，可与阶段三并行）
  T19+T30 → T36 → T37 → T38
  T37 ────────┴→ T39 → T40 → T41

阶段五（收尾）
  T17 → T42 → T43
  T36 → T44
  T36 → T45
  T26+T36 → T46 → T47 → T48
```

**关键路径**：`T1 → T2 → T9 → T10 → T11 → T13 → T14 → T16 → T17 → T19 →
T30 → T31 → T32 → T36 → T37 → T39 → T46 → T47 → T48`。

**可并行的三条支线**：清单（T3–T8）、文案（T34–T35）、
观测与命令（T42–T45）——它们互不依赖，卡住任一条不影响主线推进。
