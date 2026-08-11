# TUI 显示体系改造 Plan

> 对应 `spec.md` 的八组 F1–F42 / N1–N8。本文档回答**怎么做**。

## 架构概览

八组改动落在五层，**依赖方向不变**（上层依赖下层，反之不可）：

| 层 | 改什么 | 对应组 |
| --- | --- | --- |
| `tui/widgets.py` | 新增活动区与报告两个组件；改工具行、四个面板、历史区 CSS | A B C D E F H |
| `tui/app.py` | 接线：轮询刷活动区、全局展开开关、分级方法、`Ctrl+C` 计数、数字键 | A C D E F H |
| `commands/` | 报告类命令改走报告通道；控制器协议加两个方法 | C D |
| `subagents/` | 任务记录加两个字段、运行器计数、领域层产出活动快照 | A |
| `memory/` `trace/` | 「笔记 → 记忆」改名 | G |

**没有新增包，没有新增依赖边。** 活动区所需数据由 `conversation.py` 以只读快照
向上提供，与既有的 `running_subagent_count()` / `drain_subagent_notifications()`
同一条通路。

## 核心数据结构

### `TaskRecord` 新增两个字段（`subagents/tasks.py`）

```python
tool_calls: int = 0                    # 已完成的工具调用次数（spec F3）
recent_tools: tuple[str, ...] = ()     # 最近若干次调用的**已渲染短文本**，有界
```

⚠ **`recent_tools` 存的是渲染好的字符串（`"Grep(Layer)"`），不是 `ToolCall` 对象。**
理由与既有的 `worktree_path` 只存字符串完全相同：`TaskRecord` 的读写都在
`TaskManager` 的加锁临界区内，而那里的硬不变量是**只做纯内存读写**。放一个
`ToolCall` 进去，是在诱导后来的人在锁内做参数摘要与 markup 转义。

### `ActivityRow`（新，`subagents/tasks.py`）

给界面的**不可变**只读快照，一次轮询取一份：

```python
@dataclass(frozen=True)
class ActivityRow:
    display_name: str          # 已按 /agents 口径拼好：队员名(角色名) 或 角色名
    status: TaskStatus
    seconds: float             # 运行中=至今；已结束=总耗时
    tokens: int
    tool_calls: int
    recent_tools: tuple[str, ...]
    settled_seconds: float     # 已结束多久（用于 F6 的「留片刻后消失」）；运行中为 0
```

**为什么不把 `TaskRecord` 直接递给界面**：它是可变对象、含两个 `Event`，而界面
每 0.5 秒读一次；给不可变快照可以在领域层一次性完成「名字口径统一」与
「终态停留时长」的计算，界面只负责画。

### `SystemLevel`（新，`commands/models.py`）

```python
class SystemLevel(Enum):
    NOTICE = "notice"     # 暗色、无前缀
    EVENT = "event"       # 正常亮度、无前缀
```

只有两个取值：警告与错误已各有独立通道（`show_warning` / `append_error`），
不重复建模。

### 报告分级（`tui/widgets.py`，纯函数）

```python
class ReportLineKind(Enum):
    TITLE = "title"       # 首行：加粗 + 强调色
    SECTION = "section"   # 段落标题：正常亮度 + 加粗
    ITEM = "item"         # 条目行（以 • 或 - 开头）：正常亮度
    DETAIL = "detail"     # 缩进的次级信息：暗色
    BLANK = "blank"

def classify_report(text: str) -> tuple[tuple[ReportLineKind, str], ...]: ...
```

判定规则（**只看行本身的形状，不认识任何一个具体报告**）：
第一行 → `TITLE`；空行 → `BLANK`；以 `•`/`-`/`·` 开头（允许前导空格）→ `ITEM`；
缩进 ≥ 4 空格 → `DETAIL`；其余无缩进且非空 → `SECTION`。

## 模块设计

### A 组 · 活动区

**`subagents/tasks.py`**
- `TaskManager.note_tool(task_id, rendered: str)`：`tool_calls += 1`，
  `recent_tools` 尾部追加并**裁到上限**（`ACTIVITY_RECENT_LIMIT = 5`）。纯内存写。
- `TaskManager.activity_rows() -> tuple[ActivityRow, ...]`：只取
  「运行中」+「终态且结束不足 `ACTIVITY_LINGER_SECONDS`（5 秒）」的任务。

**`subagents/runner.py`**
- 事件消费循环加一支：`elif event.type is AgentEventType.TOOL_RESULT:`
  → 渲染短文本（复用 B 组的纯函数）→ `tasks.note_tool(...)`。
  ⚠ 用 `TOOL_RESULT` 而不是 `TOOL_START`：口径是「已完成的调用」，且被权限拒绝的
  调用**只产 TOOL_RESULT**，那些同样消耗了一轮，该计入。

**`conversation.py`**
- `subagent_activity() -> tuple[ActivityRow, ...]`：转调服务层；未启用时返回空元组。

**`tui/widgets.py` · `ActivityView(Vertical)`**
- `update_rows(rows, expanded)`：整块重绘（条数 ≤ 5，重绘比 diff 便宜且不会错）。
  无行时 `display = False`（F1 的「不占布局」）。
- 每行 `● <名字> <状态> (<耗时> · ↑<token> · <次数> 次调用)`；
  `expanded` 为真时在其下逐条画 `⎿ <recent_tool>`。
- ⚠ 名字与工具文本一律经本模块 `escape`。

**`tui/app.py`**
- `compose()` 在 `HistoryView` 之后、`CommandPanel` 之前 `yield ActivityView()`。
- `_poll_subagents()` 末尾调 `_refresh_activity()`（**复用既有 0.5 秒节拍，不新增
  定时器**，spec F4）。
- 历史区留痕**复用现有的完成通知那一行**（`drain_subagent_notifications` 的循环），
  只把文案换成带成本的版本。
  ⚠ **F6 的「历史永久痕」与 F7 的「完成通知」是同一行，不要写成两行**——
  写两行会让每个任务在历史区留下重复的两条。

### B 组 · 工具行

**`tools/base.py`**：`Tool.primary_arg: str = ""`（类属性，缺省空 = 未声明）。
各工具按 spec 的主参数表声明。

**`tui/widgets.py`**（纯函数，可单测）
```python
def resolve_call_title(tool_call, primary_args: dict[str, str]) -> tuple[str, str]:
    """→ (标签, 括号内文本)，两者都已转义"""
```
三条分支，顺序固定：
1. `run_agent` 特例 → 标签取 `arguments["name"]`、括号取 `arguments["task"]`；
2. `primary_args` 里有该工具且参数含该键 → 标签取 `_TOOL_LABELS`、括号取该键的值；
3. 兜底 → 标签取 `_TOOL_LABELS`、括号取现有的 `summarize_args`。

**耗时**：`ToolCallWidget.finish` 里 `elapsed >= 1` 才拼 `(Ns)`（F13）。
执行中的 `_render_running` 一字不动。

**`tui/app.py`**：`on_mount` 时从工具注册表建一次 `{name: primary_arg}` 传给
`HistoryView`（工具集启动后不变）。取不到注册表（非 DeepSeek Provider）时传空字典
→ 全部走兜底分支，零回归。

### C 组 · 报告

- `commands/models.py` 的 `CommandController` 协议加 `show_report(text: str)`。
- `commands/builtins.py` **9 处**报告调用点由 `show_message` 改为 `show_report`
  （`/mcp` `/hooks` `/tasks` `/agents` `/context` `/memory` `/skills`
  `/skills prompt` `/help`）。其余 `show_message` 调用点**一处不动**。
- `tui/app.py` 实现 `show_report`：埋点仍记 `source="system"`
  （**刻意不新增 trace 的 source 取值**，与 `show_warning` 同口径），
  渲染交给 `HistoryView.append_report`。
- `HistoryView.append_report(text)` → `classify_report` → 按级别拼 markup →
  一个 `Static`。

### D 组 · 分级

现有通道两条（`show_message` 暗色 / `show_warning` 橙色）+ 错误（`append_error`）。
**只新增一个**：`show_event`（正常亮度、无前缀）。

- `HistoryView.append_event(text)`：正常亮度、无前缀符号。
- `append_warning` / `append_error` 的文案前缀改为「警告：」「错误：」，
  并**去掉 `append_error` 现有的 `●`**（`●` 按 F29 专属工具行与活动行）。
- 逐个调用点指派级别（T 阶段逐条列出），**不留默认兜底**（F20）。

### E 组 · 四个面板

**`tui/widgets.py` 新增共用帮助函数**
```python
def numbered_prompt(index: int, text: str, selected: bool) -> str:
    """→ '> 1. 文本' / '  2. 文本'（序号只由调用方对可选项递增）"""
```

- 三个面板（Confirm / Clarify / Session）在 `show_*` 里对**可选项**递增编号；
  表头与详情行（`disabled=True`）不占号。
- **高亮跟随**：各面板实现 `watch_highlighted`，用
  `replace_option_prompt_at_index` 只改前缀的 `> ` / `  `。
  ⚠ 该方法不改 `highlighted`，不会自激。
- **数字键**：在 `tui/app.py` 的 `on_key` 里新增一段，位置**必须在既有的
  「交互待决 → return」守卫之前**；命中 `1`–`9` 且当前有面板挂起时，
  映射到第 N 个可选项并走既有结算路径（`_resolve_interaction` / `_settle_session`）。
  Esc、上下键、回车的处理顺序一字不动（N8）。

### F 组 · 去表情与退出键位

**表情替换**：按 spec 的清单逐文件改，纯文本改动。

**键位**（`tui/app.py` 新增 `BINDINGS`）
```python
BINDINGS = [
    Binding("ctrl+q", "noop", "", show=False, priority=True),      # 取消 Textual 的退出
    Binding("ctrl+c", "request_quit", "", show=False, priority=True),
    Binding("ctrl+o", "toggle_expand", "", show=False),
]
```
⚠ 两条 `priority=True` 是**实测确认必需**的：Textual 8.2.7 的 `ctrl+q → quit`
本身就是 priority 绑定（退出行为来自 Textual，不是项目代码）；而 `Input` 自带
`ctrl+c → copy`，不用 priority 的话输入框聚焦时我们的动作根本不触发。

**`action_request_quit`（双击）**
1. **有选中文本** → 执行复制，**不计数、不提示**；
2. 否则：距上次按下 ≤ `QUIT_CONFIRM_SECONDS`（2 秒）→ `self.exit()`；
3. 否则 → 记时间戳 + 在**状态栏最左侧**挂出「再按一次 Ctrl+C 退出」，
   并起一个 `QUIT_CONFIRM_SECONDS` 后触发的定时器把它撤下
   （`_arm_quit_hint` / `_expire_quit_hint`，实际使用后修订：原为聊天区的一行提示级消息）。

⚠ **「有选中文本」必须同时查两处，缺一不可**（实测确认是两套独立机制）：

| 用户操作 | 判定来源 | 现有绑定 |
| --- | --- | --- |
| 鼠标在历史区拖选一段（抄报错 / 路径） | `screen.get_selected_text()` | `Screen.BINDINGS`: `ctrl+c → screen.copy_text` |
| 输入框内 Shift+方向键选中自己打的字 | 焦点组件的 `selected_text` | `Input.BINDINGS`: `ctrl+c → copy` |

两条都是 `priority=False`，因此**都会被我们的 `priority=True` 绑定盖掉**——
不做分流的话，「Ctrl+C 复制」这个今天真实可用的功能会整个消失，
而那正是 C2 护栏当年写下「Ctrl+C 用于复制场景」时指的东西。

⚠ **「不计数」是这条设计的要害**：连续复制五次一次都不会靠近退出。
反过来（复制也计入双击）会让「连按两次复制」意外退出程序，那个更糟。

**已知代价（接受）**：屏幕上有选中内容时按两次 `Ctrl+C` 得到的是「复制两次」，
不会退出；想退出需先清掉选中。相比「复制两次就退出」，这个方向更安全。
另：第一次按下的提示文案必须写明是**退出**而不是取消——`Esc` 才是取消当前回合，
两者不能让用户混淆。

**护栏改写**（`tests/test_tui_keybindings.py`）
- `test_ctrl_c_is_not_bound_to_quit` → `test_single_ctrl_c_never_quits`：
  断言 `ctrl+c` 不直接绑定 `quit` 动作，且**单次按下后应用仍在运行**。
- `test_placeholder_points_to_ctrl_q_for_quit` → 断言占位符写「连按两次 Ctrl+C 退出」
  且不含「Ctrl+Q 退出」。
- 同步 `docs/c2/checklist.md` 的 AC9。

### G 组 · 「笔记 → 记忆」

分两步，**顺序不能反**（每步都要能跑通全量测试）：

1. **标识符与文件改名**（纯机械）：`memory/notes.py` → `memories.py`、
   `note_updater.py` → `memory_updater.py`，以及 spec 表里的九组标识符。
2. **字符串与文档改口**：界面 4 处、发给模型 4 处、文档 194 处、测试 58 处。

**trace 作用域**（数据契约，四处同改）：`SCOPE_NOTES` 取值 `"notes"` → `"memory"`；
`trace/models.py`、`trace/__init__.py`、`CLAUDE.md` 的命令示例、
`docs/c11/testing/p0-trace/` 两份文档。

⚠ **`notetaker` 一个字都不许动**（`tests/e2e/align_scenarios.py`、
`docs/c11/acceptance/skills-align-live.md`）——它是 C11 权限验收里测试夹具 Skill
的名字，只是恰好含 `note`。**改名一律用带词边界的精确匹配，不用裸替换。**

### H 组 · 布局与体量

**F39 + F40 是一行 CSS**（已实测）：
```css
HistoryView > Vertical { height: auto; min-height: 100%; }
```
实测数据：短内容 `region.y=1` / `scroll_y=0`（贴顶，负偏移消失）；
长内容 `scroll_y=28 == max_scroll_y`、最后一条落在视口内（跟随最新仍成立）。
**`anchor()` 一个字都不用动**，它那段带实测证据的注释原样保留。

**F41 折叠**：`ToolCallWidget.finish` 的分支行支持多行，超过
`BRANCH_LINE_LIMIT`（5 行）时截断并追加 `… +N 行（Ctrl+O 展开）`；
组件保留完整文本，展开时改画全文。diff 块同理，上限
`DIFF_ROW_LIMIT`。

**全局展开开关**：`RhineApp._expanded: bool`，`Ctrl+O` 切换，同时广播给
活动区与历史区所有工具行（对齐 Claude Code 的全局 verbose 语义，
**不为工具行另立一个键**）。

**F42 删轮次行**：`_do_stream` 的 `PROGRESS` 分支去掉 `append_system` 与那条
`_trace_ui_message`，**保留 `reset_text_widgets()`**（它负责让新一轮文本另起一块，
删掉会让相邻两轮的正文粘在一起）。

## 模块交互

**活动区的数据流（全程主线程）**
```
子 Agent 线程                          主线程（每 0.5 秒）
  runner 消费事件
    └ TOOL_RESULT → tasks.note_tool()      _poll_subagents()
        （加锁，纯内存写）                     └ conversation.subagent_activity()
                                                  └ TaskManager.activity_rows()（加锁，纯内存读）
                                              └ ActivityView.update_rows()
```
**没有任何一条边是从子 Agent 线程指向界面的**（N1）。

**工具行标题的解析**
```
_do_stream(TOOL_START) → HistoryView.add_tool_widget(tc)
                            └ ToolCallWidget(tc, primary_args)
                                 └ resolve_call_title()  ← 纯函数，可单测
```

**报告**
```
/agents → builtins → controller.show_report(text)
                        └ app.show_report → HistoryView.append_report
                                               └ classify_report()  ← 纯函数，可单测
```

## 文件组织

```
rhinecode/
├── tui/
│   ├── widgets.py     — 新增 ActivityView / classify_report / numbered_prompt /
│   │                     resolve_call_title；改 ToolCallWidget、四个面板、HistoryView
│   └── app.py         — BINDINGS、_refresh_activity、show_report/show_event、
│                         action_request_quit、action_toggle_expand、数字键
├── commands/
│   ├── models.py      — CommandController 加 show_report / show_event
│   └── builtins.py    — 9 处报告调用点改通道
├── subagents/
│   ├── tasks.py       — TaskRecord 两字段、ActivityRow、note_tool、activity_rows
│   └── runner.py      — TOOL_RESULT 计数
├── tools/base.py      — Tool.primary_arg
├── memory/            — G 组改名（notes.py → memories.py 等）
├── trace/models.py    — SCOPE_NOTES 取值改为 "memory"
└── conversation.py    — subagent_activity()
```

## 技术决策

| 决策点 | 选择 | 理由 |
| --- | --- | --- |
| 活动区数据获取 | 主线程轮询，复用既有 0.5 秒节拍 | 本项目已因「加锁临界区内跨线程调度」死锁四次；不新增定时器则空闲开销与改造前一致 |
| `recent_tools` 存什么 | **已渲染的字符串** | 临界区只做纯内存读写；存 `ToolCall` 会诱导后来的人在锁内做摘要与转义（与 `worktree_path` 同一条理由） |
| 工具调用计数取哪个事件 | `TOOL_RESULT` | 口径是「已完成的调用」；被权限拒绝的只产 `TOOL_RESULT`，那些同样烧了预算 |
| 主参数声明在哪 | `Tool.primary_arg` 类属性 + 启动时建一次映射 | 与工具本体同处，改工具时不会漏；界面只拿一份不可变映射，不反向依赖 `tools` |
| 顶部对齐怎么实现 | `min-height: 100%` 一行 CSS | **实测**：短内容贴顶且 `scroll_y=0`，长内容仍 `scroll_y==max`。`anchor()` 完全不动，它那段实测证据不被打扰 |
| 展开用几个键 | **一个** `Ctrl+O`，全局 | 对齐 Claude Code 的全局 verbose 语义；两个键会让用户记两套 |
| 报告分级放哪 | 展示层纯函数，只看行的形状 | spec F16 要求报告产出函数一字不改（它们有大量逐字断言的护栏） |
| 分级新增几个方法 | **一个** `show_event` | 提示（`show_message`）、警告（`show_warning`）、错误（`append_error`）都已存在，只缺中间一级 |
| 面板高亮指示符怎么跟随 | `watch_highlighted` + `replace_option_prompt_at_index` | Textual 原生 API（已验证存在）；该方法不改 `highlighted`，不会自激 |
| `Ctrl+C` 与复制的冲突 | 有选中文本时**先复制、不计数**；选中来源查 `Screen` 与焦点组件**两处** | Textual 8.x 里 `Screen` 与 `Input` 各有一条 `ctrl+c → copy`（都是 priority=False，会被我们盖掉）；直接夺走就是重新踩回 C2 护栏那个坑 |
| `Ctrl+Q` 怎么取消 | `priority=True` 覆盖成空动作 | **实测**：退出行为来自 Textual 自带的 priority 绑定，不覆盖去不掉 |
| 改名怎么做 | 词边界精确匹配，分两步提交 | 裸替换会打中 `notetaker`（C11 验收夹具），让验收记录与代码对不上号 |

## 风险与对策

| 风险 | 对策 |
| --- | --- |
| 数字键与既有键位冲突 | 只在有面板挂起时拦截 `1`–`9`；Esc / 上下键 / 回车的处理顺序不动，护栏断言其行为不变 |
| 全局展开开关让工具行整体重绘卡顿 | 只重绘**当前挂着的**工具行；历史里已定色的行在下次展开时才重画 |
| `min-height: 100%` 影响回放路径 | `render_history` 走同一个容器，一并验；AC29/AC30 各有判据 |
| G 组改名打中无关标识符 | 词边界匹配 + 改完跑全量测试 + AC28 专门钉 `notetaker` |
| 删掉轮次行后 trace 少一类 `ui_message` | 先查有无判据依赖它（T 阶段第一步），有则同步改 |
