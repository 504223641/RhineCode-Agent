# C15 子 Agent 协作 Plan

> 对应 `spec.md`（26 条 F、9 条 N、45 条 AC）。本文档回答「怎么做」：
> 模块怎么切、数据结构长什么样、接线点在哪、以及每个技术选择的理由。

## 架构概览

本章新增**一个叶子包** `rhinecode/team/`，并在五个既有位置接线。

```
                    ┌──────────────────────────────────────┐
                    │  rhinecode/team/  （新，叶子包）      │
                    │                                      │
                    │   roster    花名册 + 待命 + 信箱持有  │
                    │   board     共享任务清单              │
                    │   mailbox   消息路由与投递            │
                    │   render    注入消息 / 展示文本       │
                    │   service   门面（对外唯一入口）      │
                    └──────────────┬───────────────────────┘
                                   │ 被依赖（单向）
        ┌──────────────┬───────────┴──────┬──────────────┐
        │              │                  │              │
   tools/team_*    subagents/         conversation     tui/app
   （5 个新工具）    runner·service      （接线）      （自动唤起）
                                   │
                              agent/gate.py
                            （CompositeGate）
```

**依赖方向**：`team` 谁都不依赖（除标准库与 `provider.base.Message`）；
`subagents` / `tools` / `conversation` / `tui` 单向依赖它。
因此不产生任何新的包级互依——本项目已有的四组（`tools ↔ skills`、
`tools ↔ mcp`、`tools ↔ web`、`tools ↔ subagents`）不增加第五组。

### 一句话说清运行时形态

- **花名册**是本章的中心数据结构，一个进程内一份，由协调层持有。
  它同时是「名字 → 队员」的电话簿、每个队员的**信箱**、以及待命队员的
  **对话历史保管处**。
- **消息投递**是纯内存的：写进收件人的信箱，然后（在锁外）`set` 它的唤醒事件。
  没有文件、没有文件锁、没有跨进程——spec「不做的事」已把那些砍掉。
- **队员看到消息**的时机由它自己的 Agent Loop 决定：正在跑的走闸门
  （每轮迭代前 `take_pending`），待命的被唤醒事件叫醒后从原历史继续。
- **主对话看到消息**有两条路：正在跑的同样走闸门；空闲的由 TUI 既有的
  0.5 秒轮询发现并触发一次「自动轮」。

---

## 核心数据结构

### `BoardTask` — 共享清单上的一条任务

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `task_id` | `str` | 顺序号（`"1"`、`"2"`…）。**刻意用小整数字符串**而不是随机短串：Claude Code 的清单语义里「优先做 ID 小的任务」是有意义的提示，随机串会让这层顺序信息消失 |
| `subject` | `str` | 标题，祈使句 |
| `description` | `str` | 说明 |
| `state` | `TaskState` | `PENDING` / `IN_PROGRESS` / `COMPLETED` |
| `owner` | `str` | 认领人名字；`""` 表示无人认领 |
| `blocked_by` | `tuple[str, ...]` | 被哪些任务挡着 |
| `blocks` | `tuple[str, ...]` | 挡着哪些任务 |
| `created_at` / `updated_at` | `float` | `time.time()`，展示与排序用 |

`blocked_by` 与 `blocks` **双向冗余存储**，在同一个临界区内成对维护。
只存一边的话，列出清单时要遍历全表反查，而清单是每个队员每轮都可能读的。
⚠ 成对维护点，写进代码注释。

### `TaskState` / `MemberState`

```
TaskState:   PENDING → IN_PROGRESS → COMPLETED     （删除是移除条目，不是状态）
MemberState: RUNNING ⇄ IDLE                        （IDLE 可被唤醒回 RUNNING）
                ↓
             FAILED / CANCELLED / RETIRED           （终态，不可唤醒）
```

`RETIRED` 是 N3 的降级去向：待命队员数超上限时，最久未活动的那个被降级，
历史释放，之后向它发消息按「不可唤醒」失败。

### `Envelope` — 一条消息

| 字段 | 说明 |
| --- | --- |
| `sender` | 发件人名字（队员名或 `"main"`） |
| `recipient` | 收件人名字 |
| `summary` | 一句话摘要，界面单行展示用 |
| `body` | 正文 |
| `sent_at` | 落盘时间戳，**由系统补，不由模型给** |
| `read` | 是否已被注入过收件人的历史。缺省 `False` |

`sent_at` 与 `read` 由系统填充，是 spec 原始需求里「落盘时自动补时间戳、
默认未读」那条**唯一保留下来的部分**（文件与锁已随「不做的事」砍掉）。

### `MemberEntry` — 花名册上的一位队员

| 字段 | 说明 |
| --- | --- |
| `name` | 唯一名字 |
| `task_id` | 关联的 C13 `TaskRecord` 标识（用于 `/agents` 对齐展示） |
| `state` | `MemberState` |
| `inbox` | `list[Envelope]`，未读在前 |
| `history` | 待命时保留的完整对话历史；`RETIRED` 后置空 |
| `wake_event` | `threading.Event`，唤醒信号 |
| `last_active` | 最近一次运行结束的时刻，N3 的降级依据 |
| `read_only` | 该队员的最终工具集是否全只读，**F24 判定用**。委派时算好存下来，避免 Plan Mode 判定时反向依赖 `subagents` |

`main` 是花名册里一个**特殊条目**：`state` 恒为 `RUNNING`，
没有 `history`（主历史由协调层持有）、没有 `wake_event`（它的唤醒走 TUI 轮询）。
把它放进花名册而不是特判，是为了让 `send_message` 的路由只有一条代码路径。

---

## 模块设计

### `team/models.py`

**职责**：上面四个数据结构 + 常量表。零 IO、零依赖（只用标准库）。

**常量**：
- `MAIN_NAME = "main"`
- `MAX_IDLE_MEMBERS = 5` — 待命队员上限（N3）
- `MAX_AUTO_WAKE_CHAIN = 5` — 自动唤起连锁上限（F20）
- `STATE_LABELS` — 中文展示名（与枚举值分开，理由同 C13：枚举值是落盘/记录里的稳定标识，不该跟界面措辞走）

### `team/board.py` — 共享任务清单

**职责**：一份加锁的任务表，提供增删查改与依赖判定。

**对外接口**（全部是纯内存操作，全部返回结构化结果而非抛异常）：

| 方法 | 说明 |
| --- | --- |
| `create(subject, description) -> BoardTask` | 分配下一个顺序号 |
| `snapshot() -> tuple[BoardTask, ...]` | 按 ID 升序的只读快照 |
| `get(task_id) -> Optional[BoardTask]` | 单条 |
| `update(task_id, **fields) -> UpdateResult` | 改状态 / 标题 / 说明 |
| `claim(task_id, owner) -> ClaimResult` | **原子认领**，见下 |
| `add_dependency(task_id, blocked_by) -> DepResult` | 双向维护 + 环检测 |
| `remove(task_id) -> bool` | 删除，并从相关任务的依赖里摘除 |
| `is_blocked(task_id) -> tuple[str, ...]` | 返回**尚未完成**的前置任务 ID |

**`claim` 是本模块唯一有并发语义的操作**（F8/AC10）。在同一个临界区内完成
三件事：① 检查任务存在 ② 检查未被挡住 ③ 检查 `owner == ""` 后写入。
三者分开做就有 TOCTOU 窗口，两个队员会同时认领成功。
返回值区分四种结果（成功 / 不存在 / 被挡住并列出挡它的 / 已被某某认领），
调用方据此组织回灌文案。

**环检测**：`add_dependency` 时从新前置沿 `blocked_by` 深搜，
遇到自己即拒绝。spec「不做的事」排除的是「环检测之外的图算法」，
环检测本身必须做——否则两条任务互相挡着，谁都认领不了而清单上看不出原因。

**⚠ N2 加锁不变量**：临界区只做纯内存读写，不调任何回调。
本类**刻意不持有任何回调**，与 C13 `TaskManager` 同一条结构性约定。

### `team/mailbox.py` — 消息路由与投递

**职责**：把一条消息送进收件人的信箱，并唤醒它。

**对外接口**：

| 方法 | 说明 |
| --- | --- |
| `send(sender, recipient, body, summary) -> SendResult` | 投递；收件人不存在 / 不可唤醒时明确失败 |
| `take_unread(name) -> tuple[Envelope, ...]` | 取走某人的未读，**取走即置位**（幂等） |
| `has_unread(name) -> bool` | 只读探测，TUI 轮询用 |

**投递的三段式**（N2 的具体落实）：

```
① 加锁：校验收件人存在且可收 → 追加进 inbox → 记下要唤醒谁
② 出锁
③ 锁外：wake_event.set()
```

⚠ `Event.set()` 唤醒等待线程，属跨线程调度，**必须在锁外**。
这与 C13 `TaskManager` 的 `finish` / `cancel` 是同一条，
违反会与 Textual 阻塞式 `call_from_thread` 组成确定性死锁。

**不可收的三种情形**（F10/F14）：收件人不在花名册（列出全部名字）、
收件人处于终态（说明是失败 / 取消 / 已退休的哪一种）、发给自己。

### `team/roster.py` — 花名册

**职责**：名字的唯一性、队员状态机、待命历史的保管与降级。

| 方法 | 说明 |
| --- | --- |
| `register(name, task_id, read_only) -> RegisterResult` | 占名字。**已存在即失败**（F2） |
| `suggest_name(role) -> str` | 自动生成不冲突的名字：`<角色>-1`、`-2`… |
| `mark_idle(name, history)` | 转待命并保管历史；可能触发降级 |
| `mark_terminal(name, state)` | 转终态，释放历史 |
| `wake(name) -> Optional[list[Message]]` | 队员线程调用，取回历史准备续跑 |
| `snapshot() -> tuple[MemberEntry, ...]` | `/agents` 与工具展示用 |
| `clear()` | `/clear` / `/resume` / 退出（F25） |

**N3 的降级**在 `mark_idle` 里判定：待命数超过 `MAX_IDLE_MEMBERS` 时，
把 `last_active` 最早的那个转成 `RETIRED` 并释放历史，
**返回被降级的名字**让调用方去通知界面。
⚠ 降级必须看得见——返回值不能被丢弃，否则就成了 spec N3 明令禁止的静默降级。

### `team/render.py` — 文本

| 函数 | 产出 |
| --- | --- |
| `render_incoming(envelopes) -> Message` | 把一批未读渲染成一条注入历史的消息 |
| `render_board(tasks) -> str` | 清单的文本展示，`/tasks` 与 `task_list` 工具共用 |
| `render_roster(members) -> str` | 花名册文本，`/agents` 用 |

**`render_incoming` 是 F12 的全部落实**，格式：

```
<teammate-message from="worker-a" at="14:32:07">
（摘要：接口要改成异步的）
正文……
</teammate-message>
```

三条要求写死在这里：
1. `role="user"` —— 与 C13 的子 Agent 结论同理，它与任何 `tool_call_id` 都不配对，
   当成工具结果回灌会破坏消息协议；
2. **标记块名字与 `<subagent-result>` 刻意不同** —— 模型要能区分「队友主动说话」
   与「我委派出去的活回来了」，两者的后续动作完全不同；
3. `display_content=""` —— 界面不把它显示成一条用户输入（通知另有一行）。

⚠ **成对维护点**：本函数与 `send_message` 工具的 `description` 必须同口径。
这是 C11「Skill 清单表头 ↔ `load_skill.description`」、C13「角色清单 ↔
`run_agent.description`」、C14「交付信息 ↔ 工具描述」的**第四次**——
模型在两个不同时刻读到同一条约定（发消息前读工具描述、收消息时读标记块），
一处强一处弱等于白改。

### `team/service.py` — 门面

**职责**：把 board / mailbox / roster 组合起来，是**包外唯一入口**。
工具、协调层、TUI、命令层都只跟它打交道。

它额外持有两件全局状态：
- `auto_wake_chain: int` — 自动唤起连锁计数（F20），用户提交消息时清零
- `plan_stage_gate(name) -> bool` — F24 的判定：收件人是不是 `main` 或全只读队员

---

## 接线点（五处）

### ① `agent/gate.py` 新增 `CompositeGate`

C13 的闸门协议有四个方法，C15 的消息注入**完全适配它的 `take_pending`**——
两者在循环里的处理逐字相同（追加进 history + 存档）。

因此不改 `agent/loop.py` 的消息注入逻辑，改为提供一个组合器：

```
CompositeGate(gates: Sequence[SubAgentGateProtocol])
  take_pending()     → 各 gate 的结果按顺序拼接
  has_awaited()      → any(...)
  wait_any(ev)       → 任一 gate 返回 True 即 True
  describe_awaited() → 各 gate 的描述拼接
```

- **主对话**传 `CompositeGate([SubAgentGate, TeamGate("main")])`
- **子 Agent**传 `TeamGate(<它自己的名字>)` —— C13 时子 Agent 用的是
  `NullGate`（`agent.run` 根本没传 gate），现在要传了

`TeamGate` 放在 `team/gate.py`，实现同一个协议：
`take_pending` 取未读并渲染；`has_awaited` 恒为 `False`
（**队员不因为「可能有人给我发消息」而赖着不收工**——那会让每个队员永不结束）。

⚠ **`has_awaited` 返回 `False` 是关键决定**。返回 `True` 会让循环在收工时
调 `wait_any` 等一条可能永远不来的消息，队员再也停不下来。
「等消息」这件事发生在**待命状态**，不在 Agent Loop 里。

### ② `subagents/runner.py` 的待命循环

`run_subagent` 现在跑完一次 `agent.run` 就结束。改成外层循环：

```
while True:
    跑一次 agent.run(history, ..., subagent_gate=TeamGate(name))
    取结论 → tasks.bump / 记录

    if 停止原因不是「自然完成」:        # 失败 / 取消 / 耗尽轮次
        roster.mark_terminal(...)
        break

    roster.mark_idle(name, history)     # 保留历史
    通知主对话「我空闲了」（复用 C13 的完成通知通道）

    等 wake_event（可被 cancel_event 打断）
    if 被取消: mark_terminal; break

    history = roster.wake(name)          # 取回历史
    未读消息由下一轮的 TeamGate.take_pending 注入
```

**F16 的轮次重置**天然成立：每次 `agent.run` 都是一次新的调用，
`max_iterations` 重新计数，而 `history` 是累积的。

⚠ **`TaskStatus` 要不要加 `IDLE`？答案是不加。** 这是本章最容易埋雷的地方：

C13 的闸门判「还要不要等」用的是 `r.awaited and not r.status.is_terminal`。
如果给 `TaskStatus` 加一个 `is_terminal == False` 的 `IDLE`，
**主 Agent 每次收工都会去等一个已经待命的队员，永远等不完**。

所以：C13 的 `TaskStatus` **一个成员都不加**，任务在队员自然停止时照常置
`COMPLETED`（对「这次委派的结论」而言它确实完成了）；
「待命 / 可唤醒」是**花名册**的状态，两套状态各管各的维度。
`/agents` 展示时把两者并起来显示。

### ③ `conversation.py`

| 改动 | 说明 |
| --- | --- |
| 构造 `TeamService` 并持有 | 装配层注入 |
| `subagent_gate()` 改为返回 `CompositeGate` | 主对话侧 |
| `SubAgentRuntime` 增加 `team` 字段 | 运行器要用花名册与信箱 |
| 新增 `run_auto_wake()` | 自动轮的入口，见下 |
| `clear()` / `_resume_stream` 增加 `team.clear()` | F25 |
| 用户提交消息时 `auto_wake_chain = 0` | F20 的计数复位 |

**`run_auto_wake()` 与 `_run()` 的差别只有四处**（其余完全复用）：

1. 不追加用户消息（历史从上次结束的地方接着走）；
2. `RunOptions.interactive = False` —— F18。**这不是新机制**，
   C13 的子 Agent 已经在用同一个开关；
3. `ask` 回调传 `None`（`interactive=False` 时循环根本不会调它）；
4. 事件流上打一个「自动轮」标记，供 TUI 区分展示（F21）。

### ④ `agent/loop.py` 新增一条回灌文案

C13 已有 `DENIED_NON_INTERACTIVE_FEEDBACK`，它的措辞写死了「（子 Agent）」，
且给出的下一步是「换只读方式把活干完」。主对话无人轮需要另一条：

```
DENIED_UNATTENDED_FEEDBACK：
  这次调用需要人工确认，而**用户当前不在场**（这一轮是队友的消息自动唤起的），
  确认面板弹出来也没人应答，因此被自动拒绝。
  不是有人拒绝了你，原样重试不会有不同结果。
  现在能做的：回消息给队友说明情况、让它先做别的；
  或者把「需要用户批准 X」写出来留在这里，用户回来就能看到。
```

两条文案的选择由 `interactive=False` 之外的一个新参数决定
（`unattended: bool`）。**三条文案的语义必须始终两两不同**：
`DENIED_BY_USER_FEEDBACK`（人否决了 → 停止推进）、
`DENIED_NON_INTERACTIVE_FEEDBACK`（子 Agent 环境 → 换只读方式继续）、
`DENIED_UNATTENDED_FEEDBACK`（用户不在场 → 回消息 + 留言）。
⚠ 与 C13 那条一样，**都不置 `user_denied`**（那个标志会让下一轮 `tools=None`，
对无人轮是错的——它应该带着工具继续做能做的事）。

### ⑤ `tui/app.py` 的自动唤起

**复用既有的 `set_interval(0.5, self._poll_subagents)`，不新增定时器。**
在其中追加一段判定：

```
if 主对话空闲（没有 Worker 在跑）
   and team.has_unread("main")
   and team.auto_wake_chain < MAX_AUTO_WAKE_CHAIN:
       team.auto_wake_chain += 1
       显示「自动唤起（第 N/5 次）」
       run_worker(self._do_stream(self._manager.run_auto_wake()))
elif 达到上限:
       显示一次「已达自动唤起上限，等你回来」（**只显示一次**，用标志位防刷屏）
```

⚠ **必须先判「主对话空闲」**。C13 的 Worker 是 `exclusive=True`，
不判的话新 Worker 会把正在跑的那个挤掉——用户正在等的回答会凭空消失。

---

## 新增工具（5 个）

| 工具 | `read_only` | `system_serial` | `plan_safe` | 说明 |
| --- | --- | --- | --- | --- |
| `task_create` | False | True | True | 建任务 |
| `task_list` | True | False | True | 列清单（只读，进并发桶） |
| `task_get` | True | False | True | 单条详情 |
| `task_update` | False | True | True | 改状态 / 认领 / 依赖 / 删除 |
| `send_message` | False | True | True | 发消息，**规划阶段有额外判定** |

**四个任务工具对齐 Claude Code 的四件套**，不合并成一个带 `action` 参数的工具。
理由：C13 把委派合并成一个是因为「不论加载多少角色，模型看到的工具数不变」，
而任务工具的数量本来就固定，那条理由不适用；分开则每个工具的参数 schema
更精确，模型少犯参数错误。

**都不进权限管线**（`system_serial=True`，与 `run_agent` / `load_skill` 同先例）。
论证已写在 spec 安全边界第 1 条：不读写文件、不执行命令，
没有可映射的 Bash/Read/Edit/Write 语义；仍可被 `deny` 规则整个禁掉（落 `other` 分支）。

**`plan_safe=True` 的兑现**（C13 的契约：声明它就必须自己兑现「规划阶段不产生副作用」）：
- 四个任务工具：改的是内存里的清单，规划阶段本来就该拆任务 → 无条件放行；
- `send_message`：`execute` 的 `plan_stage` 分支里判定收件人，
  非 `main` 且非全只读队员 → 拒绝，文案复用 C13 F19a 那套（列出当前能发给谁）。

---

## 数据流

### 场景一：队员 A 给队员 B 发消息（B 正在跑）

```
A 的 Agent Loop
  └─ send_message(to="worker-b", ...)
       └─ TeamService.send()
            ├─ 锁内：校验 B 存在且可收 → 追加进 B 的 inbox
            └─ 锁外：B.wake_event.set()（B 在跑，这次 set 无人等待，无害）

B 的 Agent Loop（下一轮迭代开头）
  └─ CompositeGate.take_pending()
       └─ TeamGate("worker-b").take_pending()
            └─ mailbox.take_unread("worker-b") → render_incoming() → 追加进 B 的 history
```

### 场景二：队员给 `main` 发消息，主对话空闲

```
队员 → send_message(to="main")  →  main 的 inbox

TUI 的 0.5 秒轮询
  └─ 主对话空闲 + has_unread("main") + 未达连锁上限
       └─ run_worker(_do_stream(manager.run_auto_wake()))
            └─ Agent.run(interactive=False, ...)
                 └─ 第一轮迭代开头 take_pending() 注入那条消息
                 └─ 模型处理；撞到 ASK → DENIED_UNATTENDED_FEEDBACK
```

### 场景三：唤醒一个待命的队员

```
main → send_message(to="worker-a")
         ├─ 锁内：追加进 worker-a 的 inbox
         └─ 锁外：worker-a.wake_event.set()

worker-a 的线程（阻塞在 wake_event.wait()）
  └─ 醒来 → roster.wake() 取回历史 → 再跑一次 agent.run(history)
       └─ 第一轮 take_pending() 注入那条消息 → 它带着全部上下文继续干
```

---

## 文件组织

```
rhinecode/team/                      （新包，叶子）
├── __init__.py      — 导出 TeamService 与少量类型
├── models.py        — BoardTask / Envelope / MemberEntry / 枚举 / 常量
├── board.py         — 共享任务清单（加锁，原子认领，环检测）
├── mailbox.py       — 消息路由与投递（三段式：锁内写、锁外唤醒）
├── roster.py        — 花名册、待命历史保管、N3 降级
├── gate.py          — TeamGate（实现 SubAgentGateProtocol）
├── render.py        — 注入消息标记块 / 清单文本 / 花名册文本
└── service.py       — 门面

rhinecode/tools/
├── team_tasks.py    — 四个任务清单工具
└── send_message.py  — 发消息工具

修改：
├── agent/gate.py         — CompositeGate
├── agent/loop.py         — DENIED_UNATTENDED_FEEDBACK + unattended 参数
├── subagents/runner.py   — 待命循环、注册花名册、传 TeamGate
├── subagents/service.py  — name 参数、重名校验、并发上限 3→5
├── subagents/toolset.py  — 确认协作工具不在 GLOBAL_DENIED_TOOLS 内
├── tools/run_agent.py    — name 参数 + 描述补充
├── conversation.py       — 接线、run_auto_wake、clear、连锁计数复位
├── tui/app.py            — 轮询里触发自动唤起、展示待命与自动轮
├── commands/builtins.py  — /tasks 命令
├── trace/models.py       — 四个新事件类型
├── trace/reader.py       — 对应的 SUMMARIZERS 表项
└── bootstrap.py          — 构造 TeamService 并注入

测试（新建）：
├── tests/test_team_board.py         — 清单、原子认领、依赖与环检测
├── tests/test_team_mailbox.py       — 投递、失败分支、锁外唤醒的结构护栏
├── tests/test_team_roster.py        — 命名、重名、待命、N3 降级
├── tests/test_team_gate.py          — TeamGate + CompositeGate
├── tests/test_team_tools.py         — 五个工具的参数与失败文案
├── tests/test_team_wake.py          — 待命循环与唤醒续跑
├── tests/test_team_auto_wake.py     — 自动轮、无人轮拒绝、连锁上限
└── tests/test_team_integration.py   — 端到端三场景
```

---

## 技术决策

| # | 决策点 | 选择 | 理由 |
| --- | --- | --- | --- |
| 1 | 新包的依赖方向 | `team` 是叶子，`subagents` 依赖它 | 反过来会让 `team` 背上整个 Agent Loop；且花名册只需存「名字 + 不透明句柄」，不需要认识 `TaskRecord` |
| 2 | 待命状态放哪 | **花名册**里，`TaskStatus` 一个成员都不加 | 加一个 `is_terminal == False` 的 `IDLE` 会让主 Agent 收工时永远等待已待命的队员。两套状态各管一个维度 |
| 3 | 消息注入的通路 | 复用 C13 的闸门协议，加一个 `CompositeGate` | `take_pending` 的语义与位置（每轮迭代前、协议合法）完全适配，`agent/loop.py` 的注入逻辑一行不改 |
| 4 | `TeamGate.has_awaited` | 恒 `False` | 返回 `True` 会让队员为「可能有人发消息」赖着不收工，永远停不下来。等消息发生在待命状态，不在循环里 |
| 5 | 任务工具的粒度 | 四个独立工具，不合并 | 对齐 Claude Code；数量本来固定，C13「合并以固定工具数」的理由不适用；分开则参数 schema 更精确 |
| 6 | 协作工具是否进权限管线 | 不进（`system_serial=True`） | 与 `run_agent` / `load_skill` 同先例：不读写文件、不执行命令。仍可被 `deny` 规则禁掉 |
| 7 | 无人轮的权限处理 | 复用 `interactive=False`，新增一条回灌文案 | C13 已建好整条机制（含「不置 `user_denied`」这个关键细节）。只有文案要新写，因为下一步建议不同 |
| 8 | 自动唤起的触发点 | 复用 TUI 既有的 0.5 秒轮询 | 不新增定时器（见下方「与 spec 的一处调整」）。且轮询在主线程，天然避开 `call_from_thread` 死锁 |
| 9 | 任务 ID 形态 | 小整数顺序号字符串 | Claude Code 的「优先做 ID 小的任务」是有效提示，随机短串会丢掉这层顺序信息 |
| 10 | 依赖双向存储 | `blocked_by` 与 `blocks` 都存 | 只存一边则每次列清单都要全表反查，而清单是高频读 |
| 11 | 环检测 | 做（`add_dependency` 时深搜） | 不做的话两条任务互相挡着、谁都认领不了，而清单上看不出原因。spec 排除的是「环检测之外的图算法」 |
| 12 | `main` 的表示 | 花名册里的特殊条目 | 让 `send_message` 的路由只有一条代码路径，不特判 |
| 13 | 并发上限 | 运行中 3 → **5**，待命另设 5 | 团队场景下 3 个太紧（一个 Lead 派 4 个人是常见形态）。待命不占名额是 spec F15 |
| 14 | 消息标记块名 | `<teammate-message>`，与 `<subagent-result>` 刻意不同 | 模型要能区分「队友主动说话」与「我派出去的活回来了」，后续动作完全不同 |

---

## ⚠ 与 spec 的一处调整（需你确认）

**spec N6 写的是「不引入轮询空转……不得用定时轮询实现」。**

实现时发现本项目**已经存在**两处轮询，都有明确理由：

- `tui/app.py` 的 `set_interval(0.5, _poll_subagents)`（C13）——
  用轮询是因为后台线程更新界面只能走**阻塞式**的 `call_from_thread`，
  推送式会与持锁的后台线程组成确定性死锁；
- `subagents/gate.py` 的 `wait_any`（0.05 秒）——
  用轮询是因为 `TaskManager` **刻意不持有任何回调**（防死锁的结构性约定）。

因此 N6 的落实方式调整为：

- **队员侧走事件驱动**（`threading.Event`，零 CPU 占用）—— 这是 N6 的实质；
- **主对话侧复用既有的 0.5 秒轮询，不新增任何定时器** —— 空闲会话的 CPU 占用
  与 C13/C14 **完全相同**，没有新增。

AC39（「完全空闲的会话没有周期性 CPU 占用」）据此调整为
**「与不使用本章能力时的占用相同」**——绝对意义上的「零轮询」在 C13 就已经
不成立了，把它写进 C15 的验收标准会验出一条必然失败的判据。

---

## spec 覆盖对照

| F 需求 | 落在哪 |
| --- | --- |
| F1 具名 / F2 重名失败 / F3 花名册 | `team/roster.py` + `tools/run_agent.py` 的 `name` 参数 |
| F4–F8 共享清单 | `team/board.py` + `tools/team_tasks.py` |
| F9–F12 点对点消息 | `team/mailbox.py` + `team/render.py` + `tools/send_message.py` |
| F13–F16 唤醒续跑 | `team/roster.py` + `subagents/runner.py` 的待命循环 |
| F17 自动唤起 | `tui/app.py` 轮询 + `conversation.run_auto_wake` |
| F18 无人轮不弹面板 | `RunOptions.interactive=False`（复用 C13） |
| F19 拒绝理由区分 | `agent/loop.py` 的 `DENIED_UNATTENDED_FEEDBACK` |
| F20 连锁上限 | `team/service.py` 的 `auto_wake_chain` + TUI 判定 |
| F21 无人轮可辨识 | 事件流上的自动轮标记 + TUI 展示 |
| F22 工具可见 / F23 仍禁委派 | `subagents/toolset.py`（协作工具不入禁表，`run_agent`/`load_skill` 原样在表内） |
| F24 Plan Mode | `send_message.execute` 的 `plan_stage` 分支 |
| F25 会话边界 | `conversation.clear` / `_resume_stream` 调 `team.clear()` |
| F26 展示与记录 | `/tasks` 命令、`/agents` 扩展、`trace` 四个新事件 |

| N 需求 | 落在哪 |
| --- | --- |
| N1 不扩大权限面 | 五个工具均不读写文件 / 不执行命令；队员工具集仍走 C13 三层过滤 |
| N2 加锁不变量 | `board` / `mailbox` / `roster` 三处临界区只做内存读写；`Event.set()` 在锁外 |
| N3 内存上限 | `roster.mark_idle` 的降级，返回值必须被消费 |
| N4 失败明确 | 所有对外方法返回结构化结果，不抛异常、不产生半截副作用 |
| N5 零回归 | 不传 team service 时：`CompositeGate` 退化成原 `SubAgentGate`，子 Agent 传 `NullGate`，行为逐字不变 |
| N6 事件驱动 | 见上「与 spec 的一处调整」 |
| N7 观测不阻断 | trace 埋点走既有的 `_safe_emit` 漏斗 |
| N8 依赖方向 | `team` 是叶子；`agent/gate.py` 只放协议与组合器，不依赖 `team` |
| N9 中文注释 | 每处「刻意这样做」的理由写在代码旁 |

---

## 风险与需要特别小心的地方

1. **`TaskStatus` 加 `IDLE` 是最容易犯的错**（决策 2）。它不报错，
   表现是主 Agent 收工时无限等待一个已经待命的队员。
   护栏：`test_team_wake.py` 里一条「主 Agent 在有待命队员时能正常收工」的用例。
2. **`Event.set()` 落进锁内**（N2）。不报错，只在特定并发时序下死锁。
   护栏：与 C13 同形的结构测试（遍历实例属性断言无 callable 成员 + 完成计数式
   死锁检测；⚠ 按 `docs/internals/testing.md` 的记载，**必须用完成计数而不是
   布尔标志**，同线程版本在 `RLock` 下会静默通过）。
3. **自动唤起挤掉正在跑的 Worker**（接线点 ⑤）。`exclusive=True` 的 Worker
   会被新的挤掉，用户正在等的回答凭空消失。必须先判空闲。
4. **`send_message` 的描述与 `<teammate-message>` 标记块不同口径**。
   这是本项目第四次踩同一个坑，前三次都是真实模型实测才发现的。
   护栏：`test_team_tools.py::SameVoiceTest`。
5. **子 Agent 从 `NullGate` 改成 `TeamGate` 是一处行为变更**。
   要确认 C13/C14 的既有测试不受影响（它们大多不构造 team service，
   届时仍传 `NullGate`）。
