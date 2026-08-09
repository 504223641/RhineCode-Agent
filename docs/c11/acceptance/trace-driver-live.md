# Trace 与驱动设施的真实模型验收（2026-08-09）

> **这次验的是什么**：不是某个产品功能，而是**两套测试设施自身**——
> Trace 行为记录器与端到端驱动设施。它们是 C2–C15 全部验收的依据，
> 依据本身不可信的话，上面所有结论都要打折。
>
> 触发点是本轮的两项改造：① trace 删掉全部截断阈值；② 驱动新增
> `wait --until quiescent` / `keys` / `screen` 三项能力与 C15 场景。

## 方法

- **驱动**：`tests/e2e/host.py --mode live` 常驻宿主 + `client.py` 瘦客户端，
  经本机回环通道驱动真实界面。输入走真人提交入口，面板应答走第⑤层人在回路。
- **模型**：`deepseek-v4-flash`，真实凭据，`context_window=1000000`。
- **预置**：`tests.e2e.c15_scenarios:seed_team`（两个协作角色 + 两个源文件 + allow 规则）。
- **取证**：判据一律引用 trace 事件原文，或控制通道返回的结构化数据。
- 共起 4 台宿主（其中两台是为载入修复而重启），全程零残留。

每条判据分两栏：**机器判到了什么**（可复核的事实）与**据此做的判断**。

---

## 一、Trace 完整性（本轮改造的核心）

| # | 判据 | 机器判到了什么 | 判断 |
| --- | --- | --- | --- |
| T1 | 系统提示不截断 | `api_request.system` 类型为 `str`（不是截断对象）、长度 **5033 字符** | ✅ |
| T2 | **旧阈值确实会切掉要害内容** | 同一份提示里：组队协作 @2038、角色清单 @3430 → 旧阈值下保留；**Skill 清单 @4392 → 旧阈值下被整段切掉**。尾部丢弃 1033 字符 = 全文 21% | ✅ **这是本轮改造的直接证据** |
| T3 | 消息历史不限条数 | `messages` 类型为 `list`，全程无 `{items, truncated}` 形态 | ✅ |
| T4 | 全文件零截断对象 | 两份产物（121 条 / 205 条事件）遍历所有字段，`truncated` 出现 **0 次** | ✅ |
| T5 | 命令输出完整留痕 | 一条产出 200 行的命令：`tool_execute.output` **203 行**（含 L0000/L0035/L0100/L0199）；`model_output` **44 行**、含「省略中间」、L0035 不在 | ✅ **159 行此前在任何地方都不存在** |
| T6 | 裁剪行为本身没变 | `model_output` 仍是前 30 + 后 10 行，token 预算逐字不变 | ✅ |

> ⚠️ **T2 值得单独说**：这个临时工作区**连 RHINE.md 都没有**、没有记忆索引，
> 系统提示已经 5033 字符。真实项目里 RHINE.md 动辄数百行——旧阈值下
> C11/C13/C15 三段清单会**全部**消失，而那正是排查「模型为什么不用这个能力」
> 时唯一的证据（已知项 #17 就是这类问题）。

## 二、Trace 覆盖面

| # | 判据 | 机器判到了什么 | 判断 |
| --- | --- | --- | --- |
| T7 | **每条 tool_execute 都有判定** | 两份产物分别 17/17、12/12 全中 | ✅ 改造前对七个 `system_serial` 工具恒假，这条护栏写不出来 |
| T8 | `system_serial` 绕过引擎可见 | 10 条 `bypassed_engine=True`，工具为 `run_agent` / `send_message` / `task_create` / `task_update`；阅读器标 `⚠绕过引擎` | ✅ 已知项 #18 现在可从记录复核 |
| T9 | 走引擎的照常走 | `edit_file` / `read_file` / `task_get` / `task_list` 命中 `Read(*)` / `Edit(*)` / 只读放行 | ✅ 判定行为一字未改 |
| T10 | `cwd` 进记录 | `permission_decision` 17/17、`tool_execute` 17/17 全带 | ✅ C14 的隔离故障从此可复核 |
| T11 | `subagent_start` 带运行条件 | `worker/greet_worker · isolated=False · mode=default · cwd=<工作区>` | ✅ 实际生效的权限档此前一处都没记 |
| T12 | 作用域不串味 | `main` / `subagent:greet_worker` / `subagent:shout_worker` / `subagent:alice` / `notes` 各自独立计轮 | ✅ |
| T13 | 事件类型覆盖 | 两份产物合计出现 16 类（含 `team_task` / `team_member` / `team_message` / `auto_wake` / `skill_state` / `interaction`） | ✅ |

## 三、C15 协作机制（真实模型，非剧本）

| # | 判据 | 机器判到了什么 | 判断 |
| --- | --- | --- | --- |
| C1 | 共享清单 | `team_task create [1] by main` / `create [2] by main`，两个队员各 `claim` 自己那条、置 `in_progress` → `completed`，**零冲突** | ✅ |
| C2 | 并行委派 | 两条 `subagent_start` 相隔 2 ms，两个队员的 `edit_file` 分别落在 `src/app.py` 与 `src/util.py` | ✅ |
| C3 | 点对点消息 | `greet_worker → main`、`shout_worker → main` 各一条，`ok=True` | ✅ |
| C4 | 待命 | 两个队员跑完 `team_member · idle`，`status.background.idle_members = 2` | ✅ |
| C5 | **唤醒续跑保留上下文** | alice 被唤醒后 turn 4 携带 **8 条历史**（turn 3 是 6 条），其中含首轮任务关键词 `farewell` 与 `teammate-message` 标记块 | ✅ 不是重新起一条对话 |
| C6 | **自动唤起** | `auto_wake 第 1/5 次 · 来自 main`，随后 `ui_message` 出现「⟳ 自动唤起（第 1/5 次）——队友发来了消息，主对话在你不在场时自行处理。」 | ✅ 用户不在场时程序自己跑了一轮 |

## 四、驱动设施

| # | 判据 | 机器判到了什么 | 判断 |
| --- | --- | --- | --- |
| D1 | `status` 新字段 | `background = {subagents, idle_members, unread_for_main}` + `quiescent` 均在 | ✅ |
| D2 | **待命不阻塞静止** | 两个队员待命时 `quiescent=True`、`wait --until quiescent` 立即返回 | ✅ 与 C15 那条不变量一致；判反了会永远等不到 |
| D3 | `keys` 投递 | `keys slash t a s k s` → 输入框读出 `/tasks`，补全菜单弹出 | ✅ |
| D4 | `screen` 导出菜单 | `CommandPanel` 读出 `/tasks  查看队员共用的共享任务清单（编号/状态/认领人/阻塞来源）` | ✅ **C10 E03 从「验不了」变成可自动判定** |
| D5 | `screen` 导出正文 | 聊天区 14 个控件、4498 字符，含队员名与工具行 | ✅ |
| D6 | 确认面板 + 按键应答 | `answer once --via keys` → `source=human`；`interaction confirm → allow` | ✅ |
| D7 | 用户拒绝 | `answer deny` → `interaction confirm → deny` + `tool_execute run_command · denied_by_user` + 回灌文案 | ✅ |
| D8 | 协议校验挡住错误取值 | `answer no`（approve 面板的取值）在 confirm 面板上被 `bad_request` 拒绝，消息列出合法取值 | ✅ 拼错不会静默生效 |
| D9 | `observe` 增量 | `--since 170` 命中 6 条、`skipped=0`、时间线可读 | ✅ |
| D10 | 退出与清理 | `quit` 后临时工作区 0 个、名片 0 张 | ✅ |

---

## 五、真机抓到的两个缺口（都已修）

**这两条单元测试与脚本化剧本都抓不到**，只有真机跑才暴露——与 C13/C14 验收
得出的结论一致：**真实模型跑一遍能抓到的，和单测能抓到的，是两批不同的问题。**

### ① 唤醒续跑只有 `subagent_end`，没有 `subagent_start`

现场：alice 被唤醒两次，时间线上 **1 条 start 配 3 条 end**。
`_next_round_record` 每轮发新 `task_id`，那两条 end 看起来像凭空出现的。

比「对不上号」更要紧的是：**续跑轮的运行条件一处都没记**——工具集、权限档、
工作目录全都只在首轮那条 start 里。要复核「它被叫醒之后还是那套能力吗」，
除了这条埋点没有别的依据。

修法：补埋点，`kind=wake`；两处调用点抽成共用的 `_emit_start`
（各拼一份的话，将来给 start 加字段必然只加到一处，而**漏的那处不报错**）。
护栏 `WakeTraceTest`，第二条专门钉住「不能只补一条空壳事件让计数配平」。

复验：`wake:worker/bob [716e6a]` ↔ `subagent_end [716e6a]`。

### ② `screen` 读不到 `Input` / `OptionList` 的内容

现场：`keys` 之后补全菜单确实弹出来了（`CommandPanel` 可见本身就是证据
——只有 `/` 开头才弹），但 `screen` 导出的只有一串 `╭───────`。

根因：这两类控件**按行绘制**（实现 `render_line` 而不是 `render()`），
而它们的 `render()` 返回 Panel 外壳——**Rich 捕获会「成功」并给出边框**，
于是原来的 fallback 永远走不到。而 C10 E03「补全菜单里有哪些候选」
正是这个缺口要解决的判据之一。

修法：逐行读 `render_line` 优先，并分出 `text`（画出来的，会折行）与
`content`（逻辑内容，不折行）两份——内容断言必须用后者，
否则会失败在折行位置这种与判据无关的地方。

---

## 六、两条方法论收获

**① 「捕获成功」不等于「拿到了内容」。**
`screen` 那个 bug 最坏的地方不是拿不到文本，而是拿到了
`RichVisual(Static(), <Group object at 0x…>)` ——**一个看起来像内容的字符串**。
判据若是 `assertNotIn`，它会**通过**。观测设施返回 repr 比返回空串危险得多，
所以取不出来时一律返回空串。

**② 设施自身的判据要与产品判据分开写。**
这次先验 `ScopedScriptedProvider` 的分派语义，再验协作行为——
因为分派错了的话剧本会静默走兜底，而**流程仍然跑得通、测试仍然全绿**。
先证明尺子准，再用它量东西。

---

## 七、结论

**两套设施的功能全部正常。** 29 条判据全中，真机额外抓出 2 个缺口并当场修掉、
复验通过。全量单测 **2267 项全绿、skipped 4**，零残留。

本轮改造的核心主张得到实测支持：**旧的 4000 字阈值在一个连 RHINE.md 都没有的
最小工作区里就已经在切掉 21% 的系统提示，而被切掉的正好是三个能力清单。**
