# 主对话的待办清单 Plan

> 对应 [`spec.md`](spec.md)。本文回答「怎么做」，不重复「做什么」。

## 架构概览

四层，自下而上，依赖方向单向：

```
  rhinecode/todo/            ← 叶子包：数据 + 校验 + 纯渲染逻辑
        ↑                       只依赖标准库，不 import 任何本项目模块
  rhinecode/tools/todo_write.py   ← 工具：翻译模型参数 → 调 store
        ↑
  rhinecode/conversation.py  ← 协调层：持有 store，向界面暴露只读视图
        ↑
  rhinecode/tui/             ← 界面：TodoPane 组件 + 刷新时机
```

外加两处横向接线：**系统提示新增一个槽位**（模型怎么知道该用它）、
**行为记录新增一个事件类型**（每次覆写留痕）。

### 为什么 `todo` 是叶子包

与 `team` / `trace` / `skills` 同一条：只依赖标准库，谁都可以 import 它，
它谁也不 import。这让它可以被纯逻辑单测完整覆盖——**限高取哪 5 条、
什么时候该显示、覆写合法不合法**，这三件最容易出错的事全部在这一层，
且验证它们**不需要起 Textual**。

⚠ **`todo` 不 import `team`。** 状态枚举各写各的（spec N2）。

## 核心数据结构

### `TodoState`（枚举，`todo/models.py`）

三个取值：`PENDING` / `IN_PROGRESS` / `COMPLETED`。

对外的字符串取值取 `pending` / `in_progress` / `completed`——这是模型在工具
参数里写的东西，对齐 Claude Code 的 `TodoWrite`，让从那边迁移过来的用法直接可用。
中文标签（「待办」/「进行中」/「已完成」）单独一张表，只用于显示。

### `TodoItem`（不可变数据类，`todo/models.py`）

```
title: str          # 标题，已 strip
state: TodoState
```

**只有两个字段**（spec F2）。没有标识——整表覆写下不存在「引用某一条」的场景。

⚠ **`frozen=True`。** 与 C15 的 `BoardTask`（可变）刻意相反：那边要在锁内
逐字段改状态，这边一次性整表替换，不可变省掉一整类「拿到快照之后被别人改掉」
的问题，也让 `snapshot()` 不需要复制。

### `ReplaceResult`（不可变数据类，`todo/store.py`）

```
ok: bool
reason: str      # 失败时的可读中文原因，直接回灌模型
```

与 C15 `UpdateResult` 同一条理由：调用方是工具，而工具的契约是不得向上抛异常
——抛出去会被 Agent Loop 变成一条「工具执行异常」，丢掉这里组织好的可读原因，
而可读原因正是模型自我纠正的唯一依据。

### `TodoView` / `TodoRow`（不可变数据类，`todo/render.py`）

```
TodoRow:  title: str, state: TodoState
TodoView: header: str            # "待办 (2/4)"
          rows: tuple[TodoRow, ...]
          overflow: str          # "" 或 "……还有 6 条，已完成 3 条"
```

**这是「界面该画成什么样」的完整描述，但不含任何颜色与标记语言。**
界面拿到它只负责上色和转义。

### `TodoStore`（`todo/store.py`）

```
replace(raw_items) -> ReplaceResult    # 唯一的写入口
snapshot() -> tuple[TodoItem, ...]
version() -> int
counts() -> (已完成数, 总数)
all_completed() -> bool
clear() -> None
```

线程模型：内部一把 `threading.Lock`，全部公开方法自己加锁。

⚠ **本类刻意不持有任何回调**（spec N3）——没有可调的东西，就不可能在持锁时调它。
与 C15 的 `TaskBoard` 同一条结构护栏（遍历实例属性断言无 callable 成员）。

### `version`：界面刷新的唯一依据

每次**成功**的覆写让 `version` 加一，失败不加。

界面侧记住上次画的版本号，只有版本号变了才重绘。这样做有三个好处：

1. **工作线程侧零成本地判断「要不要通知界面」**——读一个整数，不变就不发起
   任何跨线程调用。
2. **界面层不需要认识工具名**。「谁改的」与界面无关，将来若多出第二条写路径
   （目前 spec 明确不做），刷新逻辑一个字都不用改。
3. 重绘天然幂等。

## 模块设计

### 模块一：`rhinecode/todo/`（叶子包）

**职责：** 清单的数据、校验、以及「该画成什么样」的纯逻辑。
**依赖：** 仅标准库。

#### `models.py`
`TodoState` 枚举、`TodoItem`、中文标签表。

#### `store.py`
`TodoStore` 与 `ReplaceResult`。校验在 `replace` 内完成：

| 校验 | 拒绝时的原因要说清 |
| --- | --- |
| 顶层不是列表 | 期望的形状 |
| 条数 > 上限（`MAX_ITEMS = 30`） | 当前条数与上限 |
| 某条不是对象 / 缺 `title` / `title` strip 后为空 | **是第几条** |
| `state` 不认识 | 是第几条、给的是什么、合法取值有哪三个 |

⚠ **校验必须在写入之前全部跑完**（spec F5「被拒时一个字节都不改」）：
先把整份输入解析成 `TodoItem` 列表，全部通过才在锁内一次性替换。
边解析边写的话，一份「前三条合法、第四条非法」的输入会留下半张表。

⚠ **`MAX_ITEMS` 是拒绝线不是截断线**（spec F6）。截断会让模型以为写进去了，
而清单上少了几条，它下一轮据此做的判断全是错的。

#### `render.py`
两个纯函数：

- `build_view(items, limit=5) -> Optional[TodoView]`
  —— **显示决策 + 限高 + 排序全在这里**。返回 `None` 表示「不该显示」
  （清单为空，或全部已完成）。

  取哪 5 条的规则（spec F13）：先按 `IN_PROGRESS` → `PENDING` → `COMPLETED`
  的优先级排，**同一优先级内保持模型给的原始顺序**（那是模型表达的执行次序，
  重排会让清单读起来不像一份计划）。取前 `limit` 条。
  被省掉的条数与其中已完成的条数写进 `overflow`。

  ⚠ 排序**只影响显示**，`store.snapshot()` 永远返回模型给的原始顺序。

- `render_todo_brief() -> str`
  —— 系统提示里那一段恒定文本（spec F19）。与 `team/render.py` 的
  `render_team_brief` 同一形态、同一位置。

- `render_all_done_text(total) -> str`
  —— 「待办 N/N 全部完成」那一行的文本（spec F15）。放在这里而不是界面层，
  是为了让它可被纯逻辑单测断言。

### 模块二：`rhinecode/tools/todo_write.py`

**职责：** 把模型给的参数交给 store，把结果翻译成 `ToolResult`。
**依赖：** `todo`（构造时注入 store 实例）、`tools.base`。

类属性：

| 属性 | 值 | 理由 |
| --- | --- | --- |
| `name` | `todo_write` | 对齐 Claude Code 的 `TodoWrite`，蛇形化 |
| `read_only` | `False` | 它改状态 |
| `system_serial` | `True` | spec F7：不弹面板，但仍过引擎、对④层免疫 |
| `plan_safe` | `True` | spec F8：规划阶段可用（纯内存，无外部副作用） |
| `workspace_aware` | `False` | 不碰路径 |
| `classifier_scope` | **不声明** | 不跑命令 / 不联网 / 不发消息 |

⚠ **`plan_safe=True` 要求 `execute` 接受 `plan_stage` 关键字参数**
（`tools/base.py` 的硬约定，循环会传）。本工具在两个阶段行为**完全相同**
——它不产生任何外部副作用，因此那个参数只是接住、不据它分支。

参数 schema：

```
{"todos": [{"title": <字符串>, "state": "pending"|"in_progress"|"completed"}]}
```

**成功时的 `output`** 要回一份当前清单的文字版（含每条的状态），
让模型下一轮不必再猜自己刚写了什么。**`summary`** 给规模描述（「4 条待办，
已完成 2 条」），供折叠档的工具行显示。

⚠ **工具描述里必须同时写清两件事**（这是模型读到的第二处文本，
与系统提示那段同口径，见「成对维护点」）：**每次传完整清单**（不是增量），
以及**状态如实反映实际执行情况**（真在跑才标进行中，并行时可以多条）。

### 模块三：`rhinecode/conversation.py`（协调层，改动）

新增一个可选属性 `todo_store`，由装配层注入（与 `team_service` / `classifier`
同一先例）。新增三个只读方法供界面调用：

- `todo_version() -> int` —— 没有 store 时返回 0
- `todo_view() -> Optional[TodoView]` —— 没有 store 时返回 None
- `todo_all_done_text() -> str`

以及在**会话切换**时清空：`clear()` 与 `_resume_stream` 两条路径都要调
`todo_store.clear()`。

⚠ 这两处**必须与既有的 Skill 激活态清理写在一起**——那里已经是「会话切换要
复位什么」的收口点，另起一处必然出现「清空能复位、恢复不能」。

### 模块四：`rhinecode/tui/`（界面，改动）

#### `widgets.py` 新增 `TodoPane`

一个 `Static` 子类，挂在 `HistoryView` 内部、`dock: bottom`。

```
update_view(view: Optional[TodoView]) -> None
```

`view` 为 `None` 时 `display = False`（整块不占布局空间，spec F11/N5）。

渲染形态：

```
● 待办 (2/4)
  ● 读现有实现        已完成
  ● 改 login 接口     进行中
  ● 改三处调用方      待办
  ……还有 6 条，已完成 3 条
```

- 圆点前缀 + 颜色 + 文字标签（spec F14）。颜色表**与活动区同源取值**：
  进行中 `#FFA500`、已完成 `#5FD75F`、待办用次级灰。
- 标题与状态标签之间按 `cell_len` 补齐对齐（与确认面板四个选项同一做法）。
- ⚠ **一切文本经 `tui/widgets.py` 自己的 `escape`**，绝不用 rich 那版
  （spec N4：落单的方括号会在布局阶段抛错，整个应用退出）。
- ⚠ **内容经 `set_rich` / `set_markup` 而不是 `update()`**，否则这块内容
  拖选时既不高亮也问不出字符偏移（tui-activity-fold 第 6 轮的教训）。
- ⚠ **新字段名先在 `Static` 实例上 `hasattr` 查一遍**（`_render` / `_closed` /
  `_running` 都撞过，撞上不报错、只表现为界面上东西凭空少了）。

#### `HistoryView` 改动

`compose` 多产出一个 `TodoPane`：

```
yield Vertical(id="history-messages")
yield TodoPane()
```

`clear_all()` **不动**——它只清 `#history-messages` 的子节点，天然不碰待办块。

CSS（与 `#panel-dock` 同一套手法）：

```
TodoPane {
    dock: bottom;
    height: auto;
    display: none;
    border: none;
    border-top: tall #808080 60%;   /* 灰：它是观测区不是交互区，与四个青色面板分开 */
}
```

⚠ **`border-top` 用灰不用主题青**：与活动区同一条理由——它是**观测区**，
下面那几个等着人应答的面板才用青色，两者必须在视觉上分得开。

#### `app.py` 改动

三处：

1. **刷新方法 `_refresh_todo()`**（主线程）：
   ```
   version = manager.todo_version()
   if version == self._todo_version: return
   self._todo_version = version
   view = manager.todo_view()
   if 之前显示过 and view is None and 清单非空且全完成:
       show_event(manager.todo_all_done_text())     # spec F15
   pane.update_view(view)
   self._todo_shown = view is not None
   ```

2. **触发点在 `_do_stream` 的 `TOOL_RESULT` 分支**：先在工作线程读一次
   `todo_version()`（一个整数，不加任何跨线程成本），**变了才**
   `call_from_thread(self._refresh_todo)`。

   ⚠ **刻意不搭 `_poll_subagents` 的 0.5 秒定时器**：那个定时器**只在子 Agent
   服务启用时才注册**（`on_mount` 里有 `if ... is not None`），搭它会让待办块
   在关掉子 Agent 的配置下**整个不刷新**——而配置和界面上都看不出异常。

   ⚠ 也**不新增定时器**：空闲会话的开销必须与改造前一致（与活动区同一条约束）。

3. **`_reset_display_state()` 里收起待办块**并把版本号与显示标记复位
   （spec F17 的界面半边；数据半边由协调层清空 store）。

### 模块五：系统提示（`agent/prompt/`，改动）

新增槽位 **133「待办清单」**，`cacheable=True`。

- `modules.py` 的 `optional_slots()` 加一条
- `builder.py` 的 `build_default_prompt` 加一个 `todo_brief` 参数、加一次
  `builder.add(...)`、并把「待办清单」加进 `_FILLED` 元组

⚠ **漏改 `_FILLED` 不报错**，只是那个槽位被添加两次（一次填了内容、一次空槽）。

**为什么是 133、排在「组队协作」(134) 之前：** 两段都是恒定文本，缓存上等价；
排序按语义——**先说「自己怎么管进度」，再说「什么时候找别人」**。
先总后分的次序与 134 → 135 的既有理由一脉相承。

**只有主对话传这个参数。** 定义式子 Agent 不走这条链，分支式子 Agent 与
fork 子对话拿不到这个工具（spec F9），传了等于告诉它们去用一个看不见的工具。

### 模块六：子 Agent 工具过滤（`subagents/toolset.py`，改动）

`GLOBAL_DENIED_TOOLS` 加入 `todo_write`（spec F9）。

⚠ 那一层排在角色白名单**之前**，因此一条 `tools: [todo_write]` 的角色定义
也拿不到它。该集合已有遍历式护栏，新增项自动被覆盖。

### 模块七：行为记录（`trace/`，改动）

新增事件类型 `TODO_UPDATE = "todo_update"`，负载含：
`ok` / `reason`（被拒时）/ `total` / `completed` / `in_progress` / `version`。

⚠ **同时要在 `trace/reader.py` 的 `SUMMARIZERS` 里登记摘要函数**——
漏了不报错，只会在阅读器里显示成「（未登记类型）」。

⚠ **埋点在 store 的锁外**（spec N3）：`replace` 里锁内算结果、出锁、埋点、返回。
与 `SkillManager.deactivate` 同一形态。

### 模块八：装配（`bootstrap.py`，改动）

在既有的 team 服务附近，同一条件分支内（DeepSeek 工具模式 + 默认注册中心）：

```
todo_store = TodoStore(recorder=recorder)
manager.todo_store = todo_store
tool_registry.register(TodoWriteTool(todo_store))
```

⚠ **必须在 `exclude_tools` 摘除与 `session_start` 快照之间的既有位置之后**，
不进那个窄窗口——它对本扩展没有约束，照着 team 的位置放即可。

不注册时 `manager.todo_store` 为 `None`，界面拿到的 view 恒为 `None`，
待办块永不显示——**这就是 spec N5「零回归」的实现方式**，不需要额外开关。

## 模块交互

一次成功覆写的完整链路：

```
模型发起 todo_write(todos=[...])
      │
      ▼
Agent Loop 决策预扫 ── engine.decide ──→ ALLOW（④层免疫，不弹面板）
      │
      ▼
TodoWriteTool.execute(args, plan_stage=…)
      │
      ▼
TodoStore.replace(raw)
      ├─ 锁外：全量校验 → 解析成 TodoItem 列表（失败即返回，不进锁）
      ├─ 锁内：整表替换 + version += 1        ← 纯内存读写
      └─ 锁外：trace 埋点 → 返回 ReplaceResult
      │
      ▼
ToolResult(ok=True, output=清单文字版, summary="4 条待办，已完成 2 条")
      │
      ▼
Agent Loop 播报 TOOL_RESULT
      │
      ▼
TUI 工作线程：todo_version() 变了？
      │ 是
      ▼
call_from_thread(_refresh_todo)          ← 唯一的跨线程边，且不在任何锁内
      │
      ▼
主线程：build_view(snapshot) → TodoPane.update_view(view)
```

会话切换：

```
/clear 或 /resume
  ├─ conversation：todo_store.clear()      （数据）
  └─ app._reset_display_state()：pane 收起 + 版本号复位   （界面）
```

## 文件组织

```
rhinecode/
├── todo/                        ← 新建，叶子包
│   ├── __init__.py              — 对外导出 TodoStore / build_view / render_* 
│   ├── models.py                — TodoState、TodoItem、中文标签表
│   ├── store.py                 — TodoStore、ReplaceResult、校验与上限
│   └── render.py                — TodoView/TodoRow、build_view、两个文本渲染函数
├── tools/
│   └── todo_write.py            ← 新建 — TodoWriteTool
├── tui/
│   ├── widgets.py               ← 改 — 新增 TodoPane；HistoryView.compose 多产出一个
│   └── app.py                   ← 改 — CSS、_refresh_todo、TOOL_RESULT 触发、复位
├── agent/prompt/
│   ├── modules.py               ← 改 — optional_slots 新增 133 槽
│   └── builder.py               ← 改 — todo_brief 参数 + add + _FILLED
├── subagents/toolset.py         ← 改 — GLOBAL_DENIED_TOOLS 加 todo_write
├── trace/
│   ├── models.py                ← 改 — TODO_UPDATE 枚举
│   └── reader.py                ← 改 — SUMMARIZERS 登记摘要函数
├── conversation.py              ← 改 — todo_store 属性、三个只读方法、两处清空
└── bootstrap.py                 ← 改 — 建 store、注册工具

tests/
├── test_todo_store.py           ← 新建 — 覆写、校验、上限、版本号、并发、无回调结构护栏
├── test_todo_render.py          ← 新建 — build_view 的显示决策/限高/排序、两个文本函数
├── test_todo_tool.py            ← 新建 — 工具属性、参数翻译、拒绝回灌、描述同口径
├── test_todo_integration.py     ← 新建 — 权限（不弹面板/deny 生效）、子 Agent 拿不到、
│                                        提示槽位、会话切换清空
└── test_todo_tui.py             ← 新建 — TodoPane 可见性/转义/拖选可寻址/全部完成留痕
```

## 技术决策

| 决策点 | 选择 | 理由 |
| --- | --- | --- |
| 数据结构来源 | 全新叶子包 `todo/`，连状态枚举都不复用 `team` | 复用要让两个叶子包互相依赖，省十几行换一条长期误导人的依赖（spec N2） |
| `TodoItem` 可变性 | 不可变 | 整表替换语义下没有「改一个字段」的场景；省掉快照复制与一整类竞态 |
| 写入接口 | 整表覆写 | 单一写入方，无 lost update 风险；模型不必维护标识与状态机（spec F3） |
| 界面刷新触发 | 工作线程读版本号，变了才跨线程通知 | 不新增定时器；不搭 `_poll_subagents`（它只在子 Agent 服务启用时注册）；界面层不必认识工具名 |
| 待办块的位置 | `HistoryView` 内部 `dock: bottom` | 框线天然连续（HistoryView 自己那圈围住它）；`clear_all` 只清 `#history-messages`，不必改 |
| 占位 vs 浮层 | 占位（base 层，挤压滚动区） | spec F12 已拍板：历史内容一个字不能被盖住 |
| 限高的实现位置 | 叶子包的纯函数 `build_view` | 「取哪 5 条」是最容易错的一处，放在纯逻辑层可脱离 Textual 断言 |
| 排序范围 | 只影响显示，`snapshot` 保持原序 | 原序是模型表达的执行次序 |
| 提示槽位号 | 133 | 恒定文本、缓存上与 134 等价；语义上「先管好自己」在「找别人」之前 |
| 是否加配置开关 | 不加 | 未注册工具时 store 为 `None`、view 恒为 `None`，零回归自动成立 |
| 全部完成的留痕通道 | 历史区**事件级**（正常亮度、无前缀） | 「真的发生了一件事」，与子 Agent 跑完留痕同级；提示级会太暗 |

## ⚠ 本扩展新增的成对维护点（要补进 `CLAUDE.md`）

1. **系统提示那段 ↔ 工具描述**（`todo/render.py` 的 `render_todo_brief`
   ↔ `tools/todo_write.py` 的 `description`）。两处必须同口径：
   **每次传完整清单** / **状态如实反映实际执行** / **少于三步不必列**。
   这与 C11「Skill 清单表头 ↔ `load_skill.description`」、C13「角色清单 ↔
   `run_agent.description`」、C14「交付信息 ↔ 委派工具描述」、C15「消息标记块 ↔
   `send_message.description`」是**同一个坑的第五次**——前四次全都是真实模型
   实测才发现的。护栏：两处都断言含同样四层意思，含反证。

2. **新增 trace 事件类型 → `trace/models.py` 枚举 + `trace/reader.py` 的
   `SUMMARIZERS`**（既有条目，本扩展只是又一个实例）。

3. **新增系统提示槽位 → `optional_slots` + `builder` 的参数与 `_FILLED`**
   （既有条目，同上）。

## ⚠ 一处必须先做实测再往下写的风险

**`dock: bottom` 在 `ScrollableContainer`（`HistoryView`）内部到底成不成立，
本项目没有先例。** 已有的 `#panel-dock` 是在**非滚动**容器 `#stage` 里 dock 的，
证明不了这一条。

可能的三种结果与应对：

| 实测结果 | 应对 |
| --- | --- |
| 正常：待办块固定在底部，滚动区自动缩短 | 照本 plan 实施 |
| 待办块跟着内容滚 | 退回**方案 B**：把 `TodoPane` 提到 `#stage` 里当 `HistoryView` 的兄弟、base 层 `dock: bottom`，并在它可见时给 `HistoryView` 加一个去掉下边框的 class（框线由 `TodoPane` 自己接着画，手法与 `#panel-dock` 完全相同） |
| 滚动区高度不随之变化（内容被盖住） | 同上退回方案 B |

**T1 就是这个实测**，结论写进 `plan.md` 的修订说明再往下走。
这条来自 tui-activity-fold 的教训：那一轮 24 个问题里前两个都是布局假设不成立，
而**单元测试一条都抓不到**。

## spec 覆盖自检

| spec | 落在哪 |
| --- | --- |
| F1 会话内唯一 | `TodoStore` 单实例，装配层建一份 |
| F2 只有标题与状态 | `TodoItem` 两个字段 |
| F3 整表覆写 | `TodoStore.replace` 是唯一写入口 |
| F4 三态、不限进行中条数 | `TodoState`；`replace` 无「进行中至多一条」校验 |
| F5 非法拒绝、一字节不改 | 锁外全量校验，全通过才进锁替换 |
| F6 硬上限 | `MAX_ITEMS = 30`，超出拒绝 |
| F7 不弹面板但过管线 | `system_serial = True` |
| F8 规划阶段可用 | `plan_safe = True` + `execute` 接 `plan_stage` |
| F9 子 Agent 拿不到 | `GLOBAL_DENIED_TOOLS` |
| F10 钉在历史区内部底端 | `TodoPane` + `dock: bottom`（T1 实测确认） |
| F11 空或全完成则整块隐藏 | `build_view` 返回 `None` → `display = False` |
| F12 占位不浮起 | base 层，不进 `panels` 图层 |
| F13 限高 5 条 + 优先级 | `build_view(limit=5)` |
| F14 圆点 + 颜色 + 文字 | `TodoPane` 渲染；符号在白名单内 |
| F15 全部完成留一行 | `_refresh_todo` 的转移检测 + `render_all_done_text` |
| F16 不进状态行、无命令 | 不改 `compose_status_text`，不加 `CommandSpec` |
| F17 会话切换清空 | 协调层清 store + `_reset_display_state` 收界面 |
| F18 行为记录 | `TODO_UPDATE` + `SUMMARIZERS` |
| F19 系统提示 | 133 槽 + `render_todo_brief` |
| N1 不新增底部布局层 | 待办块在 `HistoryView` 之内，底部四层不变 |
| N2 叶子包 | `todo/` 只依赖标准库 |
| N3 加锁不变量 | 锁内纯内存；埋点在锁外；不持有回调 |
| N4 转义 | `TodoPane` 走本模块 `escape` |
| N5 零回归 | 未注册工具时 store 为 `None` |
| N6 不引入线程→界面推送 | 唯一跨线程边是 `_do_stream` 既有的 `call_from_thread`，不在任何锁内 |
