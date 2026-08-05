# C12 Hook 系统 —— 文档导航

**在生命周期的固定节点上挂用户声明的自动化动作。**
一条规则 = **事件**（何时）+ **条件**（可省，省略即无条件）+ **动作**（做什么），
从两层 YAML（用户级 / 项目级）声明式加载。

## 四份文档

| 文档 | 回答什么 | 状态 |
| --- | --- | --- |
| [`spec.md`](spec.md) | 做什么：十二个事件、四种动作、拦截与失败语义、27 条验收标准 | 已批准 |
| [`plan.md`](plan.md) | 怎么做：`hooks/` 七模块、依赖方向、决策管线接入点、十二个分发点 | 已批准 |
| [`task.md`](task.md) | 按什么顺序做：43 个任务、八个阶段、执行顺序图 | 已批准 |
| [`checklist.md`](checklist.md) | 做对了没：十三节约 83 项，78 项无头可验 / 3 项需人眼 / 2 项需真实模型 | 已批准 |
| [`acceptance.md`](acceptance.md) | **验收报告**：逐节结果与证据、验收期修掉的两处观测缺口、留给用户的三项人眼检查 | 已完成 |

## ⚠ 读 spec 时必须注意的一处勘误

**`spec.md` 的 F2 边界第 2 条带一个勘误块，读那一条时必须连勘误一起读。**

原文写的是「压根没执行的分支**一个都不触发任何工具级事件**」，并把「权限管线判 DENY」
与「用户在确认面板选拒绝」也列了进去。这与 F6 的管线位置**自相矛盾**：
`pre_tool_use` 按决策 1A 排在五层权限管线**之前**，在跑权限判定之前根本无从知道
这次调用会不会被 DENY——结构上做不到。

实际实现按「本条只管**后置**事件」执行（这也正是该条给出的理由所指向的语义）：

| 分支 | `pre_tool_use` | `post_tool_use*` |
| --- | --- | --- |
| 未知工具 / 参数非法 / `out_of_scope` / `plan_blocked` | 不触发 | 不触发 |
| 权限管线判 DENY / 用户在面板拒绝 / Hook 自己拦下 | **触发** | 不触发 |
| 真的执行了 | 触发 | 触发 |

护栏见 `tests/test_hook_intercept.py::NoExecutionBranchesTest`（六条逐一钉住）。

## 四个已确认的设计决策

开工前与用户确认过，四份文档全部建立在它们之上，**改动任一条都要重走 spec**：

1. **Hook 只能收紧，不能放宽**（决策 1A）。`pre_tool_use` 的结论只有 deny / ask / 不表态，
   **没有 allow**；`ask` 也只把 ALLOW 升级为 ASK，绝不降级 DENY。
   这是整章安全论证的全部依据——Hook 加进来后「能通过的调用集合」只会变小，
   C6 的五层不变量原样成立，一个字都不用重新论证。
2. **支持项目级 `hooks.yaml`**（决策 2C），对冲手段是启动时**逐条列出**事件与动作原文。
   风险与对冲的完整说明见 `CLAUDE.md`「安全边界」的 Hook 一节第 ② 条。
3. **拦截类 fail-closed，其余 fail-open**（决策 3A）。`pre_tool_use` 上 Hook 自身跑失败
   即按拦截处理——写坏的安全 Hook 必须**可见地**坏掉，而不是静默失效。
4. **上下文只经 stdin JSON 传递**（决策 3B），配置里不做任何字符串插值。
   这条直接把命令注入面整个关掉了。

## 实现期发生的三处与文档不同的取舍

都在代码注释与 spec 勘误里有记载，这里只做索引：

| 位置 | 取舍 | 理由（摘要） |
| --- | --- | --- |
| `hooks/parser.py` 模块 docstring | spec F8 第 8 项**从加载期移到运行期** | 唯一「会产出决策」的动作是 `command`，而 `post_tool_use` + `command` 正是最常见的正当用法（自动格式化）。照字面实现会给每条格式化 Hook 都挂一条警告，警告区随即失去可读性 |
| `hooks/models.py` 的 `OPEN_INPUT_EVENTS` | 三个工具级事件的**字段集是开放的** | `tool_input` 的键取决于是哪个工具，加载期不可能枚举。代价是那三个事件上的字段笔误加载期发现不了，排查靠 `/hooks` 里触发次数恒为 0 |
| `agent/loop.py` `_execute` | 系统级工具对 Plan Mode 过滤的**隐式豁免写成显式条件** | 为把 `pre_tool_use` 收在单一分发点上，系统级工具的分流必须挪到规划阶段过滤之后；不显式写出豁免的话，今天唯一的系统级工具 `load_skill` 行为会悄悄改变 |
| `hooks/conditions.py` `_match_command_field` | 命令类字段**整条 + 逐段**双重检查（原设计只调 `match_command`） | 真实模型实跑撞出来的：`command: "git push *"` 被 `git add x && git commit && git push origin main` 整个绕过，而那是模型自然写出的形态、不是刻意规避。①黑名单早就是拆的，Hook 侧漏了这一半 |
| `tui/app.py` + `widgets.py` | 项目级提示从 `startup_notice` 摘出，改走**新增的醒目通道** | 人眼评审发现：原来走 `append_system` 的 `[dim]`，也就是比正文还暗；而它是本项目里唯一一段「这些命令会直接执行」的警告。顺带去掉了 Markdown 的 `**`——Textual 不认，只会显示成字面星号 |
| `hooks/manager.py` `BLOCKED_FEEDBACK` | 删去末句「并请他决定是否调整这条规则」 | 人眼评审：它把「要不要削弱这道防线」主动摆上桌面。真实模型实跑中，模型据此给出的选项之一就是「修改 hooks.yaml」 |

## 相关代码

| 位置 | 内容 |
| --- | --- |
| `rhinecode/hooks/` | 本章新增的包，七个模块（`models` / `conditions` / `parser` / `config` / `actions` / `manager` / `report`），其中四个零 I/O |
| `rhinecode/agent/loop.py` | Hook 前置层接入决策预扫；三个工具级事件的分发 |
| `rhinecode/conversation.py` | 回合级两事件（搭 `_wrap_events` 的车）、会话级两处、注入通道、`/hooks` 领域方法 |
| `rhinecode/context/manager.py` | 压缩两事件（`_do_summary` 外壳 + `_summarize` 内层） |
| `rhinecode/tui/app.py` | 消息级两事件、`notification` 四处、`query_report` 分支 |
| `rhinecode/bootstrap.py` | 装配 `HookManager`、会话级两事件 |

## 测试

| 文件 | 覆盖 |
| --- | --- |
| `tests/test_hook_conditions.py` | 四种匹配形态、`all`/`any`、缺失字段语义、类型归一化 |
| `tests/test_hook_parser.py` | 七项校验、两处整层降级、两层加载顺序、模板 |
| `tests/test_hook_actions.py` | 四种动作，含黑名单拦截 / 网络硬校验 / `allow` 被忽略三条安全反证 |
| `tests/test_hook_manager.py` | 分发、`once`、结论合并、失败语义、惰性负载、**加锁不变量** |
| `tests/test_hook_intercept.py` | Agent Loop 集成，含四条安全反证 |
| `tests/test_hook_dispatch_points.py` | 十二个分发点的结构护栏 + 真实 `build_app` 读真实 `hooks.yaml` |
| `tests/test_hook_command.py` | `/hooks` 登记与报告，含 `[` 的命令串真走一次 Textual 布局 |
| `tests/test_hook_zero_regression.py` | 缺省零行为 + 四个纯模块的**导入级** I/O 禁令 |
| `tests/test_e2e_hooks.py` | **起真宿主子进程**的端到端六场景（拦截 / ASK 升级 / 自动化正路 / fail-closed 与 fail-open 成对 / 项目级提示上首屏） |

合计 213 条。真实模型（`deepseek-v4-flash`）的两条判据见
[`acceptance.md`](acceptance.md)「真实模型实跑」一节。
