# 工具活动归并与回合状态行 Plan

> 对应 [`spec.md`](spec.md) 的 21 条功能需求。本文档定架构、数据结构与接口；
> 具体文件与执行顺序在 [`task.md`](task.md)。

## 架构概览

改动集中在 **TUI 层**，外加 `tools/display.py`（纯数据表）与 `trace/`（两个新事件）。
**Agent Loop、权限管线、Provider、子 Agent 一律不动。**

```
                     ┌─────────────────────────────────────────┐
  协调层             │ ConversationManager                     │
  （已有）           │   primary_arg_map()   ← 既有通道         │
                     │   fold_group_map()    ← 新增，同构       │
                     └────────────────┬────────────────────────┘
                                      │ 启动时注入一次
                     ┌────────────────▼────────────────────────┐
  TUI 装配           │ RhineApp.on_mount                       │
                     └────────────────┬────────────────────────┘
                                      │
        ┌─────────────────────────────┼──────────────────────────┐
        │                             │                          │
  ┌─────▼──────────┐        ┌─────────▼─────────┐      ┌─────────▼────────┐
  │ HistoryView    │        │ RhineApp          │      │ StatusLine       │
  │ （A/B 组）      │        │ （档位与状态行）    │      │ （C 组，新组件）  │
  │                │        │                   │      │                  │
  │ _current_batch │        │ _detail_level 0/1/2│      │ spinner 帧循环    │
  │ _fold_groups   │        │ action_toggle_    │      │ 耗时 / token      │
  │ _mount_widget  │        │   expand（三态）   │      │ 阶段词 / 中断提示  │
  └─────┬──────────┘        └─────────┬─────────┘      └──────────────────┘
        │                             │
  ┌─────▼───────────────┐             │ 广播档位
  │ ToolBatchWidget     │◄────────────┘
  │ （新组件，容器）      │
  │  ├ 聚合行 Static     │
  │  └ ToolCallWidget×N │
  └─────────────────────┘
```

三组的落点互不重叠，可并行开发：

| 组 | 落点 | 与其它组的耦合 |
| --- | --- | --- |
| A 归并 | `ToolBatchWidget`（新）+ `HistoryView` + `tools/display.py` | 无 |
| B 三级密度 | `RhineApp._detail_level` + 三个组件的 `set_detail_level` | 依赖 A 的容器存在 |
| C 状态行 | `StatusLine`（新）+ `compose` + CSS + `_do_stream` 的 USAGE 分支 | 仅与 A 共享「工具行不再自持定时器」这一条 |

---

## 核心数据结构

### 归并分组表（`tools/display.py`）

**白名单与分组表合一**——这是本设计最关键的一条：在表里的工具参与归并，
不在表里的独立成行。**两张表分开写必然漂移**（新增一个检索工具时只加了分组、
忘了加白名单，或反过来）。

```python
# 工具名 → (组标识, 量词模板)
FOLD_GROUPS: dict[str, tuple[str, str]] = {
    "glob_files":   ("glob",  "查找文件 {n} 次"),
    "grep_content": ("grep",  "搜索内容 {n} 次"),
    "read_file":    ("read",  "读取 {n} 个文件"),
    "web_fetch":    ("fetch", "抓取 {n} 个网页"),
}
```

⚠ **刻意不按 `Tool.read_only` 派生**。`read_only` 的语义是「无副作用、可并发」，
与「是一次检索操作」并不等价：`load_skill` 不改文件却会改变会话可用能力，
MCP 工具语义完全未知。显式表是偏严方向——**未登记的一律独立成行**，
遗漏的代价只是少折叠一行，反过来则会让一个有副作用的工具被静默藏起来。

配套的纯函数（零 IO，可单测）：

```python
def is_foldable(tool_name: str) -> bool: ...

def compose_batch_summary(
    entries: list[tuple[str, bool]],   # [(工具名, 是否成功), ...] 按发生时序
) -> str:
    """产出聚合语，如「查找文件 1 次 · 搜索内容 3 次 · 读取 8 个文件 · 1 个失败」。

    分组顺序 = 各组**第一次出现**的时序（F3）；失败段恒在末尾（F6）。
    """

def running_verb(tool_name: str) -> str:
    """运行中的进行时文案，如「搜索中…」（F5）。"""

def resolve_full_title(tool_call) -> tuple[str, str]:
    """
    档 2 专用的完整标题（F11）：与既有 `resolve_call_parts` 同形，但
    **每个值都不截断**，且列出**全部**参数而非只列主参数。
    产出仍是纯文本、未转义（转义是渲染方的事，本模块的既有约定）。
    """
```

### 详细度档位（`tui/widgets.py` 模块级常量）

```python
DETAIL_FOLDED = 0   # 批次只显示聚合行
DETAIL_ITEMS  = 1   # 批次展开为逐条；单条仍受行数上限
DETAIL_FULL   = 2   # 单条显示完整参数与结果原文
DETAIL_CYCLE  = (DETAIL_FOLDED, DETAIL_ITEMS, DETAIL_FULL)

# 聚合行末尾的档位提示，指向**按下去会到哪一档**（F10）
NEXT_LEVEL_HINT = {
    DETAIL_FOLDED: "（Ctrl+O 展开）",
    DETAIL_ITEMS:  "（Ctrl+O 看全文）",
    DETAIL_FULL:   "（Ctrl+O 收起）",
}
```

⚠ 用整数档位而不是两个布尔。两个布尔能表达 4 种状态，其中一种非法
（「不展开批次但展开单条」），而非法状态迟早会被某条路径构造出来。

### Spinner 帧表（`tui/widgets.py`）

```python
SPINNER_FRAMES = ("◇", "◈", "◆", "◈")   # F16
SPINNER_INTERVAL = 0.15                  # 秒/帧（F16a，常量不可配置）
```

---

## 模块设计

### 1. `ToolBatchWidget`（新，`tui/widgets.py`）

**职责**：装一批连续的可归并工具行，按档位决定露多少。

**结构**：`Vertical` 容器，内含一个聚合行 `Static` + N 个 `ToolCallWidget`。

**对外接口**：

```python
class ToolBatchWidget(Vertical):
    def __init__(self, detail_level: int) -> None: ...

    def attach(self, widget: ToolCallWidget, tool_name: str) -> None:
        """把一条工具行纳入本批次，并登记它的工具名用于聚合计数。"""

    def note_running(self, tool_name: str, primary_value: str) -> None:
        """某次调用进入执行态：更新进行时文案与从属行（F5）。"""

    def note_finished(self, tool_name: str, ok: bool) -> None:
        """某次调用定色：重算聚合语（含失败计数）。"""

    def close(self) -> None:
        """封闭本批次：形态从进行时切到完成时（F5），此后不再接受新行。"""

    def set_detail_level(self, level: int) -> None:
        """档位广播：档 0 隐藏全部子行、档 1/2 显示，并逐个转发给子行。"""

    @property
    def closed(self) -> bool: ...
```

**内部状态**：`_entries: list[tuple[str, bool | None]]`（工具名 + 成败，None 为未完成）、
`_closed: bool`、`_running_label: str`、`_running_arg: str`。

**渲染分两态**：

- 未封闭 → `● {running_verb}…` + 从属行 `⎿ {当前主参数值}`
- 已封闭 → `● {compose_batch_summary(entries)}` + 档位提示；
  单次调用时额外保留一条从属行放主参数值（F4）

⚠ **聚合行的一切文本必须经项目自有的 `escape`**（N2）：主参数值来自工具参数，
含方括号是常态，落单的 `[` 会在布局阶段的主线程抛错、没有任何异常处理兜得住。

### 2. `ToolCallWidget`（改，`tui/widgets.py`）

四处改动，其余逐字不变：

| 改动 | 需求 | 说明 |
| --- | --- | --- |
| 去掉 `on_mount` 里的 `set_interval` | F19 | 运行中不再每秒重绘；**终态耗时照常计算**（`finish` 时算 `monotonic() - _start`，不依赖定时器） |
| `_render_running` 不再显示秒数 | F19/AC20 | 运行中的时间统一由状态行承担 |
| `_render_finished` 成功态去掉「完成」 | F7 | 失败态保留「失败」——它是脱离颜色也能辨认的唯一依靠 |
| 保留 `tool_call` 引用，标题按档位重算 | F11 | 档 2 用完整参数，其余用截断版 |

新增：

```python
def set_detail_level(self, level: int) -> None:
    """替代原 set_expanded。档 2 → 完整参数 + 结果原文；档 0/1 → 现有折叠形态。"""

def set_batch(self, batch: "ToolBatchWidget | None") -> None:
    """登记所属批次。finish/begin_running 时回调它重算聚合行。"""
```

#### ⚠ `finish` 必须收两份结果文本，不能只收一份

**这是自检时发现的一处实质缺口。** 现状 `app._summarize_result(res)` 返回
**一个**字符串，且**优先取 `res.summary`**——也就是说 `res.output` 的原文
**组件根本拿不到**。照现状实现的话，档 2 展开出来的仍是那句规模描述
（「读取 1902 行 · 78.4 KB」），F12 无从兑现。

改法是让 `finish` 收两份，各服务一个档位：

```python
def finish(self, ok: bool, summary: str, diff=None, detail: str = "") -> None:
    """
    :param summary: **规模描述**（工具自报的那句），档 0/1 显示
    :param detail:  **输出原文**，仅档 2 显示；为空时档 2 退回显示 summary
    """
```

`app.py` 侧相应拆成两个取值函数：

| 函数 | 取什么 | 服务档位 |
| --- | --- | --- |
| `_result_summary(res)` | `res.summary`，缺失时回退 `res.output` | 档 0 / 1 |
| `_result_detail(res)` | `res.output` 原文 | 档 2 |

⚠ **档 2 显示 `output` 而不是 `full_output`**（spec F12 的明确要求）。
`run_command` 会主动把输出裁成前 30 + 后 10 行、完整原文另存 `full_output`；
展开成完整原文会让一次测试套件输出撑爆历史区。因此判据是：
**`res.full_output` 非空即说明被裁过**，此时在结果块末行如实注明
还有多少行、以及完整原文可从行为记录（trace）取得。

⚠ 回调**只在主线程内直接调用**（`finish` 本身就是经 `call_from_thread` 到主线程的），
不新增任何跨线程通道（N1）。

⚠ `set_expanded` **保留为薄封装**（`set_expanded(True)` → `set_detail_level(DETAIL_FULL)`）：
`build_replay_items` 的回放路径与既有测试都在调它，一次性全改会把改动面无谓地扩大。

### 3. `HistoryView`（改，`tui/widgets.py`）

**新增状态**：`_current_batch: ToolBatchWidget | None`、`_fold_groups: dict`、
`_detail_level: int`。

**核心改动只有两处**：

```python
def add_tool_widget(self, tool_call, pending=False) -> ToolCallWidget:
    """
    新增分流：可归并 → 进当前批次（没有就新建）；不可归并 → 先封闭当前批次，
    再按现状直接挂进历史区。
    """

def _mount_widget(self, widget):
    """
    ⚠ **批次封闭的唯一判定点**：挂载任何**非工具行**内容前先 `_close_batch()`。

    为什么选这里而不是监听 TEXT 事件：这里是历史区一切内容的**必经之路**，
    正文、思考、系统行、通知全都走它。规则因此简单到不可能漏——
    「历史区里出现了别的东西」就是「这批工具调用结束了」。
    监听事件的写法要在 app 层枚举所有断开时机，漏一处就会出现
    「一个批次跨越了中间那段正文」，而那在界面上表现为顺序错乱。
    """
```

⚠ **确认面板为什么不断开批次（AC2）**：四个交互面板都是 `compose` 里的独立组件，
弹出时**不往历史区挂任何东西**，因此不经过 `_mount_widget`、不触发封闭。
这不是特意为 AC2 加的判断，是既有布局结构的自然结果——**没有代码需要为它写**，
但需要一条护栏钉住，免得将来有人给面板加一条「已批准 xxx」的历史行时
无声地破坏它。

⚠ **批次能跨轮**。`PROGRESS`（新一轮）与 `THINKING` 都**不**直接断开批次——
但思考块本身走 `_mount_widget`，所以开启思考模式时批次实际会按轮切分。
**这是接受的**：思考块是一段独立呈现的内容，让它插进批次中间会让时序错乱。
关闭思考模式时（默认）批次可自然跨越多轮，这正是「读项目」那类场景的主形态。

### 4. `StatusLine`（新，`tui/widgets.py`）

**职责**：本回合的活体状态行。**只在运行中存在，不进历史区。**

```python
class StatusLine(Static):
    def start(self) -> None:
        """开始一次运行：记起点、清零 token、启动帧定时器、显示自身。"""

    def stop(self) -> None:
        """运行结束：停定时器、隐藏自身（display=False，不占布局）。"""

    def add_tokens(self, count: int) -> None:
        """累加本回合 token（每轮末尾跳变一次，F17）。"""

    def set_phase(self, phase: str, interruptible: bool = True) -> None:
        """切换阶段词；等待确认期间 interruptible=False（不显示中断提示，F18）。"""
```

**唯一的定时器**：`set_interval(SPINNER_INTERVAL, self._tick)`，主线程内推进帧序号并重绘。
耗时由 `monotonic() - _start` 实时算出——**F18 要求面板期间继续累计**，
因此起点不因阶段切换而重置。

### 5. `RhineApp`（改，`tui/app.py`）

| 改动 | 需求 |
| --- | --- |
| `_expanded: bool` → `_detail_level: int`，`action_toggle_expand` 改为循环 `DETAIL_CYCLE` | F9 |
| `compose` 在 `InputBar` 之前 yield `StatusLine()` | F14 |
| `on_mount` 注入 `fold_group_map()`（与既有 `primary_arg_map()` 并列） | F2 |
| `_set_streaming(True/False)` 联动 `StatusLine.start()/stop()` | F14 |
| `_do_stream` 新增 `USAGE` 分支 → `call_from_thread(status_line.add_tokens, ...)` | F15 |
| `_interact` 前后切换阶段词 | F18 |
| `/clear`、`/resume` 后复位档位与批次 | F20 |

**CSS 新增**：

```css
StatusLine {
    height: auto;
    display: none;   /* 缺省不占布局，start() 时置为 block */
    padding: 0 1;
}
```

⚠ **不动 `#status-row`**。那一行是配置态（provider / 模型 / 权限档），
`StatusLine` 是回合活体态，两者语义不同、生命周期不同。合并会让
「常驻状态」与「瞬时状态」争同一块地方，而 tui-display F31 已经为
左右两区的分工写过一次判据。

### 6. `trace`（改）

新增两个事件类型，**同步登记进阅读器的摘要表**（既有成对维护点，
漏了只会显示成「（未登记类型）」）：

| 事件 | 何时产出 | 负载 |
| --- | --- | --- |
| `ui_tool_batch` | 批次封闭时 | 各组计数、失败数、调用总数 |
| `ui_detail_level` | 档位切换时 | 新档位 |

---

## 模块交互

### 数据流 1 · 一次可归并调用的完整生命周期

```
Worker 线程                          主线程
────────────────────────────────────────────────────────────────
TOOL_PENDING
  └ call_from_thread ────────────► HistoryView.add_tool_widget(tc, pending=True)
                                     ├ is_foldable("read_file") → True
                                     ├ _current_batch 为空 → 新建 ToolBatchWidget
                                     │   └ _mount_widget(batch)   ← 批次本身也走它
                                     ├ batch.attach(widget, "read_file")
                                     └ 返回 widget（Worker 持引用，口径不变）

TOOL_START
  └ call_from_thread ────────────► widget.begin_running(tc)
                                     └ 回调 batch.note_running(...) → 重绘聚合行

TOOL_RESULT
  └ tool_widgets.pop(tc.id)  ← 既有不变量：pop 而非 get
  └ call_from_thread ────────────► widget.finish(ok, summary, diff)
                                     └ 回调 batch.note_finished(...) → 重算聚合语

模型开始输出正文
  └ call_from_thread ────────────► HistoryView.begin_assistant_turn()
                                     └ _mount_widget(正文组件)
                                         └ _close_batch() → batch.close()
                                             └ 形态切到完成态 + 产出 ui_tool_batch
```

### 数据流 2 · 档位切换

```
Ctrl+O → action_toggle_expand
           ├ _detail_level = 下一档（DETAIL_CYCLE 循环）
           ├ _refresh_activity()            ← 活动区：档 0 折叠，档 1/2 均展开（F13）
           ├ HistoryView.set_detail_level() ← 广播给全部批次与独立工具行
           └ 产出 ui_detail_level
```

⚠ 广播必须经 `HistoryView` 自己记档位再下发，**不要在 app 层遍历组件**——
那样只覆盖此刻挂着的，之后新建的行又会回到默认档（既有注释已记过这个坑）。

### 数据流 3 · 状态行

```
_set_streaming(True)  → StatusLine.start()
                          └ set_interval(0.15) → 每帧推进 spinner + 重算耗时

USAGE 事件（每轮末尾）→ add_tokens(n)          ← 跳变式更新（F17）
确认面板弹出         → set_phase("等待确认", interruptible=False)
面板结算             → set_phase("处理中")
_set_streaming(False) → StatusLine.stop()
```

---

## 文件组织

```
rhinecode/
├── tools/display.py       改：FOLD_GROUPS 表 + 三个纯函数
├── tui/widgets.py         改：ToolBatchWidget（新）、StatusLine（新）、
│                             ToolCallWidget（4 处）、HistoryView（2 处）、
│                             档位常量与 spinner 帧表
├── tui/app.py             改：_detail_level、compose、CSS、USAGE 分支、
│                             _set_streaming 联动、清空复位
├── conversation.py        改：fold_group_map()（与 primary_arg_map 并列）
└── trace/
    ├── models.py          改：两个新事件类型
    └── reader.py          改：两条摘要函数（成对维护点）

tests/
├── test_tool_display.py       改：分组表与三个纯函数
├── test_tui_batch.py          新：批次形成/封闭/聚合语/失败计数
├── test_tui_detail_level.py   新：三档循环与广播
├── test_tui_status_line.py    新：状态行生命周期与等宽护栏
├── test_tui_symbols.py        改：白名单增加四个字形
└── test_trace_reader.py       改：两个新事件的摘要
```

---

## 技术决策

| 决策点 | 选择 | 理由 |
| --- | --- | --- |
| 归并白名单从哪来 | **显式表 `FOLD_GROUPS`**，与分组表合一 | 两张表必漂移；`read_only` 语义不等于「检索操作」；未登记即不折叠是偏严方向 |
| 白名单怎么到 TUI | 复用 `primary_arg_map()` 的既有通道 | 已验证的路径，启动时注入一次，不新造机制 |
| 批次封闭判定在哪 | **`_mount_widget` 一处** | 历史区一切内容的必经之路，规则简单到不可能漏；监听事件要在 app 层枚举所有时机，漏一处就顺序错乱 |
| 档位用什么表示 | **整数三档**，不用两个布尔 | 两个布尔能表达一种非法状态（不展开批次却展开单条） |
| 批次能否跨轮 | **能**。`PROGRESS` 不断开 | Claude Code 的 `Searched for 10 patterns` 显然跨轮；按轮切会让归并基本失效 |
| 思考块是否断开批次 | **断开**（经 `_mount_widget` 自然发生） | 它是独立呈现的一段内容，插进批次中间会让时序错乱 |
| 运行中的时间显示 | 全部收敛到状态行，工具行不再自持定时器 | AC20；顺带消掉「并发时 N 个数字同时跳」 |
| 终态耗时是否保留 | **保留**（≥1 秒时） | 去掉定时器不影响它——`finish` 时一次性算出即可 |
| `set_expanded` 是否删除 | **保留为薄封装** | 回放路径与既有测试都在调，一次性全改会无谓扩大改动面 |
| `finish` 收一份还是两份结果文本 | **两份**（`summary` + `detail`） | 现状只传 summary，output 原文组件拿不到，F12 无从兑现（自检发现） |
| 档 2 显示 `output` 还是 `full_output` | **`output`**，并注明剩余行数指向 trace | 完整原文会让一次测试套件输出撑爆历史区（spec F12 明确要求） |
| 状态行放哪 | **新组件**，不并进 `#status-row` | 配置态 vs 回合活体态，语义与生命周期都不同 |
| token 是否实时 | **否**，每轮跳变 | Provider 协议限制（F17），已写进验收标准 |

---

## 两处风险面（spec 审批时点名要求单独论证）

### 风险 1 · `_do_stream` 的事件消费结构

**既有不变量**（`CLAUDE.md` 成对维护点）：`tool_widgets` 表**只装还没定色的行**——
`TOOL_PENDING` 建行、`TOOL_START` 复用、`TOOL_RESULT` **必须 `pop`**、
`finally` 里 `_settle_unfinished_tools` 收尾剩下的。把 `pop` 写成 `get`
会让已定成绿色的行在收尾时被覆写成「失败 · 未执行」。

**本轮为什么不破坏它**：批次容器**不参与 `tool_widgets` 的登记与摘除**。
Worker 拿到的仍是 `ToolCallWidget` 引用、仍按原口径 `pop`；
批次只是改变了那个组件**挂在哪**。因此：

- `_do_stream` 的四个分支**一行都不改**（除新增的 USAGE 分支，它与工具行无关）
- `_settle_unfinished_tools` **一行都不改**，它收尾的行照样在批次里，
  `finish` 照样回调批次重算聚合语

**护栏**：`test_tui_tool_pending.py::DoStreamWiringTest` 是既有的结构护栏
（含「`get` 会覆写」的反证），本轮**不改它**——它继续通过就是这条论证的证据。

### 风险 2 · 新增布局层级

**担心什么**：`compose` 的组件顺序与 CSS 决定了整个界面的排布，
`tui-display` F39/F40 那条 `min-height: 100%` 是带实测证据加进去的，
一次布局改动可能让「内容短时贴顶」或「内容长时跟随最新」失效。

**为什么风险可控**：

1. `StatusLine` 插在 `SessionPanel` 与 `InputBar` 之间，
   **与四个交互面板同构**——它们已经全部是 `height: auto; display: none`
   的「按需出现、不占布局」组件，`StatusLine` 逐字沿用同一套。
   也就是说这不是新的布局形态，是第五个同类组件。
2. `HistoryView` 与 `#status-row` 的样式**一行不动**，
   F39/F40 的 `min-height: 100%` 不在改动面内。
3. 缺省态（`display: none`）下界面与改造前**逐字一致**，
   这正是 AC24 零回归要断言的。

**验证方式**：端到端驱动设施抓 `#history-messages` 与整屏文本对比，
外加 AC18a 的真机复核（那一条**只能真机验**——渲染库两侧算出来都是 1 格，
单元测试永远看不到终端把模糊宽度画成 2 格）。
