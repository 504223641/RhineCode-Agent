# 工具活动归并与回合状态行 Tasks

> 对应 [`spec.md`](spec.md) 的 21 条 F 需求与 [`plan.md`](plan.md) 的 6 个模块。
> 共 **38 个任务**，分五段：地基 → A 组归并 → B 组密度 → C 组状态行 → 收尾。

## 文件清单

| 操作 | 文件 | 职责 |
| --- | --- | --- |
| 改 | `rhinecode/tools/display.py` | `FOLD_GROUPS` 表 + 四个纯函数（归并判定、聚合语、进行时文案、完整参数标题） |
| 改 | `rhinecode/tui/widgets.py` | `ToolBatchWidget`（新）、`StatusLine`（新）、`ToolCallWidget`（5 处）、`HistoryView`（3 处）、档位常量、spinner 帧表 |
| 改 | `rhinecode/tui/app.py` | `_detail_level`、`compose`、CSS、`USAGE` 分支、`_set_streaming` 联动、结果取值拆分、清空复位 |
| 改 | `rhinecode/conversation.py` | `fold_group_map()`（与既有 `primary_arg_map()` 并列） |
| 改 | `rhinecode/trace/models.py` | 两个新事件类型 |
| 改 | `rhinecode/trace/reader.py` | 两条摘要函数（**成对维护点，漏了显示成「未登记类型」**） |
| 新 | `tests/test_tui_batch.py` | 批次形成 / 封闭 / 聚合语 / 失败计数 |
| 新 | `tests/test_tui_detail_level.py` | 三档循环与广播 |
| 新 | `tests/test_tui_status_line.py` | 状态行生命周期、等宽护栏 |
| 改 | `tests/test_tool_display.py` | 分组表与四个纯函数 |
| 改 | `tests/test_tui_symbols.py` | 白名单增加四个字形 |
| 改 | `tests/test_trace_reader.py` | 两个新事件的摘要 |

---

## 第一段 · 地基（纯函数，零 IO，可独立单测）

### T1: 归并分组表与判定函数

**文件：** `rhinecode/tools/display.py`
**依赖：** 无
**步骤：**
1. 新增模块级常量 `FOLD_GROUPS: dict[str, tuple[str, str]]`，四项：
   `glob_files → ("glob", "查找文件 {n} 次")`、`grep_content → ("grep", "搜索内容 {n} 次")`、
   `read_file → ("read", "读取 {n} 个文件")`、`web_fetch → ("fetch", "抓取 {n} 个网页")`。
2. 表上方写注释说明三件事：**白名单与分组表刻意合一**（分开必漂移）；
   **刻意不按 `read_only` 派生**（语义不等价，`load_skill` 是反例）；
   **未登记即不参与归并**是偏严方向。
3. 新增 `is_foldable(tool_name: str) -> bool`，即 `tool_name in FOLD_GROUPS`。

**验证：** `python -m compileall rhinecode/tools/display.py` 通过；
Python 交互式调用 `is_foldable("read_file")` 为 True、`is_foldable("write_file")` 为 False。

### T2: 聚合语组装函数

**文件：** `rhinecode/tools/display.py`
**依赖：** T1
**步骤：**
1. 新增 `compose_batch_summary(entries: list[tuple[str, bool | None]]) -> str`。
   入参是按发生时序排列的 `(工具名, 是否成功)`，`None` 表示尚未完成。
2. 按组累计次数，**组的先后取该组第一次出现的时序**（F3）。
3. 各段用行内分隔符 `·` 连接（该符号已在白名单内）。
4. 失败计数（`ok is False` 的条目数）大于零时，在**末尾**追加「N 个失败」（F6）。
   ⚠ **本步已于 2026-09-17 撤销**（见 spec 里 F6 的勘误块）：聚合语不再写失败
   个数。计数逻辑挪进同模块的 `count_batch_failures`，供行为记录使用。
5. 未登记的工具名**跳过不计**（防御性——正常路径不会传进来）。

**验证：** 单测断言
`[("grep",T),("grep",T),("read",T)] → "搜索内容 2 次 · 读取 1 个文件"`；
含一个失败时末尾出现「· 1 个失败」（⚠ 已反转，现在的判据是**一个失败字样都
不出现**，见上一条勘误）；顺序按首次出现而非字母序。

### T3: 进行时文案与完整参数标题

**文件：** `rhinecode/tools/display.py`
**依赖：** T1
**步骤：**
1. 新增 `running_verb(tool_name: str) -> str`，产出「查找中… / 搜索中… / 读取中… / 抓取中…」；
   未登记时回退到通用的「执行中…」。
2. 新增 `resolve_full_title(tool_call) -> tuple[str, str]`——与既有
   `resolve_call_parts` 同形，但**每个参数值都不截断**，且列出**全部**参数
   而非只列主参数（F11）。产出仍是**纯文本、未转义**（转义是渲染方的事，
   这是本模块既有约定）。

**验证：** 单测断言 `running_verb("grep_content") == "搜索中…"`；
`resolve_full_title` 对一个含长路径的调用返回的字符串里**不含省略号**、
且包含全部参数名。

### T4: 地基单测

**文件：** `tests/test_tool_display.py`
**依赖：** T1–T3
**步骤：** 为上述四个函数补测试类，覆盖：分组表的每一项、聚合语的顺序与失败段、
未登记工具的回退、完整标题不截断。

**验证：** `python -m unittest tests.test_tool_display` 全绿。

---

## 第二段 · A 组 · 批次归并

### T5: 档位常量与 spinner 帧表

**文件：** `rhinecode/tui/widgets.py`
**依赖：** 无
**步骤：**
1. 模块级新增 `DETAIL_FOLDED = 0` / `DETAIL_ITEMS = 1` / `DETAIL_FULL = 2` /
   `DETAIL_CYCLE = (0, 1, 2)` / `NEXT_LEVEL_HINT`（三档各一句提示）。
2. 模块级新增 `SPINNER_FRAMES = ("◇", "◈", "◆", "◈")` 与 `SPINNER_INTERVAL = 0.15`。
3. 帧表上方注释记下 spec F16 的**已知风险**（模糊宽度，Rich 算 1 格而终端
   在 CJK 环境下可能画 2 格）与**现成退路**（切到全中性宽度的
   `⬩⟐✥⟐` 或 `✢✣✤✥`，换字形只需替换这张表、不动任何结构）。

**验证：** `python -m compileall rhinecode/tui/widgets.py` 通过。

### T6: `ToolBatchWidget` 骨架

**文件：** `rhinecode/tui/widgets.py`
**依赖：** T5
**步骤：**
1. 新增 `class ToolBatchWidget(Vertical)`，构造参数 `detail_level`。
2. 内部状态：`_entries: list[tuple[str, bool | None]]`、`_closed: bool`、
   `_running_verb: str`、`_running_arg: str`、`_detail_level: int`。
3. `compose` 产出一个 `Static`（聚合行，`id="batch-summary"`）。
4. `attach(widget, tool_name)`：把工具行 mount 进本容器、登记 entry、
   调 `widget.set_batch(self)`。
5. `closed` 只读属性。

**验证：** `python -m compileall` 通过；能实例化且 `closed` 为 False。

### T7: 批次的两态渲染

**文件：** `rhinecode/tui/widgets.py`
**依赖：** T6, T2, T3
**步骤：**
1. 新增 `_render()`，按 `_closed` 分两支：
   - **未封闭**：`● {_running_verb}` + 从属行 `⎿ {_running_arg}`（F5）
   - **已封闭**：`● {compose_batch_summary(_entries)}` + 档位提示（F10）；
     `len(_entries) == 1` 时额外保留一条从属行放该次调用的主参数值（F4）
2. 有失败时状态点用失败色、整行按失败态着色（F6，沿用既有配色常量）。
3. ⚠ 一切嵌入的纯文本（主参数值、聚合语）**必须过 `tui/widgets.py` 自己的
   `escape`**，不得用第三方库的同名实现（N2）。

**验证：** 单测构造一个批次，断言未封闭时文本含「中…」、封闭后含聚合语；
主参数值含未闭合方括号时渲染不抛异常。

### T8: 批次的三个状态变更方法

**文件：** `rhinecode/tui/widgets.py`
**依赖：** T7
**步骤：**
1. `note_running(tool_name, primary_value)`：更新 `_running_verb`（取 `running_verb`）
   与 `_running_arg`，重绘。并发时**后到的覆盖先到的**（F5「最近开始的那一个」）。
2. `note_finished(tool_name, ok)`：把对应 entry 的成败落定，重绘。
3. `close()`：置 `_closed = True`、重绘、产出 `ui_tool_batch` 埋点（埋点在 T31 接）。
   已封闭时重复调用**幂等**。

**验证：** 单测断言 `note_finished` 后聚合语计数变化；`close()` 后再 `close()` 不报错、
文本不变。

### T9: `ToolCallWidget` 挂钩批次

**文件：** `rhinecode/tui/widgets.py`
**依赖：** T8
**步骤：**
1. 新增 `set_batch(batch)`，存 `self._batch`。
2. `begin_running` 末尾：`if self._batch: self._batch.note_running(self._name, self._args_summary)`。
3. `finish` 末尾：`if self._batch: self._batch.note_finished(self._name, ok)`。
4. ⚠ 注释写明**这两处回调都在主线程内直接调用**（`finish` 本身就是经
   `call_from_thread` 到主线程的），**不新增任何跨线程通道**（N1）。

**验证：** 单测：把一个 `ToolCallWidget` attach 进批次，调 `finish(True, ...)`，
断言批次的聚合语随之更新。

### T10: `HistoryView` 的批次封闭点

**文件：** `rhinecode/tui/widgets.py`
**依赖：** T8
**步骤：**
1. 新增 `_current_batch: ToolBatchWidget | None = None`（实例属性）。
2. 新增 `_close_batch()`：非空则 `close()` 并置 None。幂等。
3. 在 `_mount_widget` 开头判断：**挂载的不是 `ToolCallWidget` 且不是
   `ToolBatchWidget` 时**先 `_close_batch()`。
4. ⚠ 注释写明为什么判定放这里：它是历史区一切内容的**必经之路**，
   规则是「历史区里出现了别的东西 = 这批工具调用结束了」，简单到不可能漏；
   监听事件的写法要在 app 层枚举所有断开时机，漏一处就会出现
   「一个批次跨越了中间那段正文」。

**验证：** 单测：建批次 → 调 `append_system("x")` → 断言批次已封闭。

### T11: `HistoryView.add_tool_widget` 分流

**文件：** `rhinecode/tui/widgets.py`
**依赖：** T10, T1
**步骤：**
1. 新增实例属性 `_fold_groups: dict`（由 T13 注入）与 `set_fold_groups(mapping)`。
2. `add_tool_widget` 改为：
   - 该工具**可归并** → `_current_batch` 为空或已封闭时新建一个并 `_mount_widget(batch)`；
     然后 `batch.attach(widget, name)`；返回 widget
   - **不可归并** → 先 `_close_batch()`，再按现状 `_mount_widget(widget)`
3. 两条路径都要沿用既有那行 `widget.set_expanded(...)` 的语义（改为按当前档位）。

**验证：** 单测：连续 add 两个 `read_file` → 历史区只多一个批次容器；
中间插一个 `write_file` → 产生「批次 / 独立行 / 新批次」三个挂载物。

### T12: 协调层提供归并表

**文件：** `rhinecode/conversation.py`
**依赖：** T1
**步骤：** 新增 `fold_group_map() -> dict`，与既有 `primary_arg_map()` **并列且同构**——
从工具注册中心取当前工具名集合，与 `FOLD_GROUPS` 求交后返回。

**验证：** 单测断言返回值只含已注册且在 `FOLD_GROUPS` 内的工具名。

### T13: 启动注入

**文件：** `rhinecode/tui/app.py`
**依赖：** T12, T11
**步骤：** 在 `on_mount` 里紧挨着既有 `set_primary_args` 之后，加一行
`self.query_one(HistoryView).set_fold_groups(self._manager.fold_group_map())`。
⚠ 位置与 `primary_args` 同理，**必须排在 `render_history` 之前**——
`--continue` 恢复的历史里有工具行，晚一步会让首屏那批用空表画成独立行、
与其后新产生的形态不一致。

**验证：** 启动一次真实 TUI（或 e2e 宿主），无异常。

### T14: 工具行去定时器

**文件：** `rhinecode/tui/widgets.py`
**依赖：** **T28**（状态行必须已经在跑）
⚠ **本条依赖是执行期修正的，原写「无依赖、可与 T6–T13 并行」是错的。**

发现经过：先做了 T14，`test_tui_layout.py::test_running_timer_is_untouched`
当场红——那是 tui-display 刻意留下的**反证护栏**，保护的正是这里要去掉的东西：

> 那是模型生成参数 / 等确认面板的那段时间里界面上唯一的活体信号，
> 从 0s 开始涨正是它的价值所在。

去掉秒数的前提是**有替代品**，而替代品（状态行的 spinner + 总耗时）在第四段
才做。更要命的是它不只是「中间状态难看几分钟」：`write_file` 的参数生成期
（模型吐整份文件内容，可能几十秒）正是那条护栏的原始场景，
**而写文件是不可归并的独立行、折叠不了**——批次归并救不了它。

因此 T14 必须排在 T28 之后，且完成时要**同步改写**那条既有护栏
（改写而非删除：它保护的需求仍然成立，只是承载者从工具行换成了状态行）。
**步骤：**
1. `ToolCallWidget.on_mount` 删掉 `self._timer = self.set_interval(...)`。
2. `_render_running` 不再显示秒数。
3. ⚠ **`_start` 与 `finish` 里的耗时计算保留**——终态的 `(Ns)` 不依赖定时器，
   `finish` 时一次性算出即可。`_timer` 相关的 stop 逻辑改为容错（可能为 None）。

**验证：** 单测断言 `on_mount` 后 `_timer is None`；`finish` 后终态文本仍含耗时
（构造一个 ≥1 秒的场景）。

### T15: 成功态去掉「完成」

**文件：** `rhinecode/tui/widgets.py`
**依赖：** T14
**步骤：** `_render_finished` 里成功分支不再拼「完成」二字；**失败分支保留「失败」**
（脱离颜色也能辨认的唯一依靠，F7）。diff 分支同步。

**验证：** 单测断言成功态文本**不含**「完成」、失败态**含**「失败」。

### T16: 批次的完整单测

**文件：** `tests/test_tui_batch.py`（新）
**依赖：** T6–T15
**步骤：** 覆盖 AC1–AC10：批次形成、正文断开、确认面板**不**断开、
写文件独立成行、聚合语顺序、单次仍用聚合语且带从属行、运行中形态、
运行中无耗时数字、失败计数与红点、成功态无「完成」。

**验证：** `python -m unittest tests.test_tui_batch` 全绿。

---

## 第三段 · B 组 · 三级密度

### T17: `finish` 收两份结果文本

**文件：** `rhinecode/tui/widgets.py`
**依赖：** T15
**步骤：** `finish` 签名加 `detail: str = ""`，存 `self._detail`。
docstring 写明 `summary` 服务档 0/1、`detail` 服务档 2、为空时档 2 回退显示 summary。

**验证：** `compileall` 通过；既有调用点（只传三个参数）不报错。

### T18: app 侧拆分结果取值

**文件：** `rhinecode/tui/app.py`
**依赖：** T17
**步骤：**
1. 把 `_summarize_result` 拆成 `_result_summary(res)`（取 `res.summary`，
   缺失回退 `res.output`）与 `_result_detail(res)`（取 `res.output` 原文）。
2. `TOOL_RESULT` 分支改为 `widget.finish(res.ok, _result_summary(res), diff, _result_detail(res))`。
3. ⚠ `_result_detail` **取 `output` 而非 `full_output`**（F12）：后者会让一次
   测试套件输出撑爆历史区。
4. **裁剪说明就地拼进返回值，不另传标志**：`_result_detail` 检测到
   `res.full_output` 非空（即工具主动裁剪过）时，在返回字符串**末尾追加一行**
   注明「输出已由工具裁剪，完整原文见行为记录」。
   ⚠ 这样组件侧**不需要多一个参数**——判定与措辞都收在这一个函数里，
   `ToolCallWidget` 只管把拿到的文本画出来。多一个布尔参数意味着多一处
   「传了但没用」或「用了但没传」的可能。

**验证：** 单测：构造一个带 summary 与 output 的假结果，断言两个函数各取其一。

### T19: 工具行按档位渲染标题

**文件：** `rhinecode/tui/widgets.py`
**依赖：** T17, T3
**步骤：**
1. 构造时保留 `self._tool_call = tool_call`（`begin_running` 时更新）。
2. 新增 `set_detail_level(level)`：存档位，已定色则重绘。
3. `_render_finished` 里标题按档位选：档 2 用 `resolve_full_title`（完整参数、
   不截断），档 0/1 用现有 `resolve_call_title`。
4. `set_expanded(expanded)` 保留为薄封装 → `set_detail_level(DETAIL_FULL if expanded else DETAIL_FOLDED)`。

**验证：** 单测：同一个调用在档 1 与档 2 下的标题不同，且档 2 的**不含省略号**。

### T20: 结果块按档位渲染

**文件：** `rhinecode/tui/widgets.py`
**依赖：** T19, T18
**步骤：**
1. `_branch_block` 按档位取源：档 2 用 `_detail`（为空回退 `_summary`）、
   档 0/1 用 `_summary`。
2. 档 2 **不受 `BRANCH_LINE_LIMIT` 约束**；档 0/1 维持现有折叠与「… +N 行」提示。
3. 裁剪说明**不在本任务处理**——它已由 T18 拼进 `detail` 的末尾，
   本任务只负责把拿到的文本原样画出来。

**验证：** 单测：一个 20 行的 output，档 1 显示 5 行 + 提示、档 2 显示 20 行。

### T21: 批次的档位广播

**文件：** `rhinecode/tui/widgets.py`
**依赖：** T20, T8
**步骤：** `ToolBatchWidget.set_detail_level(level)`：
档 0 → 子工具行全部 `display = False`；档 1/2 → 显示，并逐个转发 `set_detail_level`。
自身聚合行的档位提示随之更新。

**验证：** 单测：档 0 下子行不可见、档 1 下可见。

### T22: `HistoryView` 的档位广播

**文件：** `rhinecode/tui/widgets.py`
**依赖：** T21
**步骤：** `set_expanded(bool)` 改造为 `set_detail_level(int)`（保留前者为薄封装）。
遍历已挂载的批次与独立工具行下发；**自身记住档位**，供 `add_tool_widget` 给新行用。
⚠ 注释保留既有那条警告：**不要在 app 层遍历组件**，否则展开后新产生的行又是折叠的。

**验证：** 单测：设档位后再 add 一个工具行，断言新行的档位与全局一致。

### T23: app 的三态循环

**文件：** `rhinecode/tui/app.py`
**依赖：** T22
**步骤：**
1. `self._expanded: bool` → `self._detail_level: int = DETAIL_FOLDED`。
2. `action_toggle_expand` 改为在 `DETAIL_CYCLE` 上循环。
3. 转发：`_refresh_activity()`（活动区按 F13 映射：档 0 折叠、档 1/2 均展开）
   + `HistoryView.set_detail_level(...)`。
4. 产出 `ui_detail_level` 埋点（T31 接）。

**验证：** 单测：连按三次动作，档位序列为 0→1→2→0；活动区在档 1 与档 2 下入参一致。

### T24: 档位单测

**文件：** `tests/test_tui_detail_level.py`（新）
**依赖：** T19–T23
**步骤：** 覆盖 AC11–AC15：三态循环、档位提示文案与实际效果一致、
档 2 标题完整、档 2 结果原文与裁剪注明、活动区无第三档。

**验证：** `python -m unittest tests.test_tui_detail_level` 全绿。

---

## 第四段 · C 组 · 回合状态行

### T25: `StatusLine` 组件

**文件：** `rhinecode/tui/widgets.py`
**依赖：** T5
**步骤：**
1. 新增 `class StatusLine(Static)`，状态：`_start`、`_tokens`、`_frame`、
   `_phase`、`_interruptible`、`_timer`。
2. `start()`：记起点、清零 token、置帧 0、`display = True`、
   `set_interval(SPINNER_INTERVAL, self._tick)`。
3. `stop()`：停定时器、`display = False`。
4. `_tick()`：推进帧序号、重绘。
5. `_render()`：`{帧} {阶段词} ({耗时}s · ↑ {token} · esc 中断)`，
   各段无数据时整段隐藏；`_interruptible` 为假时不出中断提示（F18）。
   ⚠ 颜色用主题青（与历史区/输入框边框同色常量）。

**验证：** 单测：`start()` 后 `display` 为真且 `_timer` 非空；`stop()` 后反之。

### T26: token 与阶段词

**文件：** `rhinecode/tui/widgets.py`
**依赖：** T25
**步骤：**
1. `add_tokens(n)`：累加并重绘。
2. `set_phase(phase, interruptible=True)`：切换阶段词与中断提示可见性、重绘。
   ⚠ **不重置 `_start`**——F18 要求面板等待期间耗时继续累计。

**验证：** 单测：`set_phase("等待确认", False)` 后文本含「等待确认」、**不含**「esc」；
且此前累计的耗时起点未变。

### T27: 挂载与样式

**文件：** `rhinecode/tui/app.py`
**依赖：** T25
**步骤：**
1. `compose` 在 `SessionPanel()` 之后、`InputBar(...)` 之前 yield `StatusLine()`。
2. CSS 新增 `StatusLine { height: auto; display: none; padding: 0 1; }`。
3. ⚠ 注释写明**与四个交互面板同构**（按需出现、不占布局），
   是第五个同类组件而非新的布局形态；`HistoryView` 与 `#status-row` 样式一行不动。

**验证：** 启动 e2e 宿主，`screen` 导出整屏文本，与改造前对比**逐字一致**（缺省隐藏）。

### T28: 运行生命周期联动

**文件：** `rhinecode/tui/app.py`
**依赖：** T27
**步骤：** `_set_streaming(True)` → `StatusLine.start()`；`_set_streaming(False)` → `stop()`。
⚠ `_set_streaming` 是既有的唯一状态复位点，挂在它上面可保证异常路径也会收尾。

**验证：** e2e：`send` 一条消息 → `status` 期间整屏含状态行；`wait` 到 idle 后不含。

### T29: USAGE 分支

**文件：** `rhinecode/tui/app.py`
**依赖：** T28
**步骤：** `_do_stream` 新增 `elif etype == AgentEventType.USAGE:` 分支，
取本轮用量的总 token，`call_from_thread(status_line.add_tokens, n)`。
⚠ 该事件**每轮末尾**才到（Provider 协议限制），因此数字是跳变式更新（F17）。

**验证：** 用脚本化 Provider 跑一轮，断言 token 段出现且数值等于剧本给的用量。

### T30: 面板期间的阶段词

**文件：** `rhinecode/tui/app.py`
**依赖：** T26, T28
**步骤：** `_interact` 弹面板前 `set_phase("等待确认", interruptible=False)`，
结算后恢复 `set_phase("处理中")`。
⚠ 三类交互（确认 / 澄清 / 审批）**共用 `_interact` 这一个入口**，改一处即可。

**验证：** e2e：脚本化剧本走到确认面板，`screen` 断言状态行含「等待确认」且不含「esc」。

### T31: trace 两个新事件

**文件：** `rhinecode/trace/models.py`、`rhinecode/trace/reader.py`
**依赖：** T8, T23
**步骤：**
1. `TraceEventType` 增 `UI_TOOL_BATCH` 与 `UI_DETAIL_LEVEL`。
2. `reader.SUMMARIZERS` **同步登记两条摘要函数**（⚠ 成对维护点，
   漏了只会显示成「（未登记类型）」）。
3. 埋点接入：`ToolBatchWidget.close()` 产出前者（各组计数 / 失败数 / 调用总数）；
   `action_toggle_expand` 产出后者（新档位）。

**验证：** `python -m unittest tests.test_trace_reader` 全绿；
跑一次带 `--trace` 的 e2e 后用阅读器读，两类事件都有摘要行、无「未登记类型」。

### T32: 会话切换复位

**文件：** `rhinecode/tui/app.py`
**依赖：** T23, T28
**步骤：** `/clear` 与 `/resume` 的既有路径上补两件事：
`StatusLine.stop()`、档位复位为 `DETAIL_FOLDED`（并下发）。
`HistoryView.clear()` 的 `remove_children` 会连带删除批次容器，
只需把 `_current_batch` 置 None。

**验证：** e2e：跑一轮 → `/clear` → 断言整屏不含状态行、档位回到默认。

### T33: 状态行单测

**文件：** `tests/test_tui_status_line.py`（新）
**依赖：** T25–T32
**步骤：** 覆盖 AC16–AC21（AC18a 除外，那条只能真机）：
生命周期、耗时实时 / token 跳变、结束不留痕、面板期间形态、
并发时只有一处时间数字、清空复位。
**另加一条等宽护栏**：断言 `SPINNER_FRAMES` 各帧的计算宽度两两相等
（换成不等宽字形时当场失败，AC18）。

**验证：** `python -m unittest tests.test_tui_status_line` 全绿。

---

## 第五段 · 收尾

### T34: 符号白名单登记

**文件：** `tests/test_tui_symbols.py`、`CLAUDE.md`
**依赖：** T5
**步骤：**
1. 把 `◇ ◈ ◆` 三个字形加进 `WHITELIST`（`◈` 出现两次但只需登记一次）。
2. 确认扫描覆盖的模块清单包含 `widgets.py`（已包含）。
3. `CLAUDE.md` 的六符号表扩为九个，并注明新增三个的用途与**已知宽度风险**。

**验证：** `python -m unittest tests.test_tui_symbols` 全绿。

### T35: 文档同步

**文件：** `CLAUDE.md`、`docs/extensions/README.md`
**依赖：** T34
**步骤：**
1. `CLAUDE.md` 的「成对维护点」补三条：**归并分组表与白名单合一**
   （新增检索工具时改一处即可，但漏改的表现是「新工具永远独立成行」）；
   **`finish` 的 summary/detail 两份文本**（只填一份的表现是档 2 展开无内容）；
   **spinner 帧表与等宽护栏**。
2. 架构表 TUI 行补上批次容器与状态行。
3. `docs/extensions/README.md` 的扩展表新增本扩展一行。

**验证：** 人工通读，与实际实现一致。

### T36: 全量测试

**文件：** —
**依赖：** T1–T35
**步骤：** `python -m compileall rhinecode tests` +
`python -m unittest discover -s tests`。
⚠ 特别确认既有的 `test_tui_tool_pending.py::DoStreamWiringTest` 仍通过——
**它是 plan 里「不破坏 `_do_stream` 不变量」那条论证的证据**。

**验证：** 全绿，skipped 数与改造前一致（4 项）。

### T37: 端到端场景

**文件：** `tests/e2e/scripts.py`（可能新增剧本）
**依赖：** T36
**步骤：** 用脚本化 Provider 编一个剧本：连发 3 个只读检索 → 输出正文 →
再发 1 个写文件 → 结束。驱动它跑完，逐条对照 AC1–AC26 里可机器判定的部分。

**验证：** `client screen` 导出的文本符合预期：一个批次块 + 一段正文 + 一个独立写文件行。

### T38: 真机复核（AC18a / AC18b）

**文件：** —
**依赖：** T37
**步骤：** 在真实终端启动 `rhine`，发一条会触发多次检索的请求，肉眼确认：
1. 状态行的 spinner 每帧切换时**其后的文字不发生左右位移**（AC18a）；
2. 动画节奏「明显在动但不干扰阅读」（AC18b）；
3. 批次块折叠 / 逐条 / 全文三档按 `Ctrl+O` 循环，提示文案与实际一致。

⚠ **这一步不可省也不可自动化**：渲染库两侧算出来都是 1 格，
单元测试永远看不到终端把模糊宽度画成 2 格。若发现位移，
按 T5 注释里记的退路换成全中性宽度字形组，**只需替换那张常量表**。

**验证：** 三条肉眼判据全部通过；若 ① 不通过则执行退路后复验。

---

## 执行顺序

```
第一段（地基，可并行起步）
T1 → T2 → T3 → T4

第二段（A 组）                          T15 ┐（独立，可提前做）
T5 → T6 → T7 → T8 → T9                      │
              └→ T10 → T11 → T13            │
        T12 ──────────┘                      │
                        └────────────────────┴→ T16

⚠ T14（工具行去定时器）已移出本段，改排在 T28 之后——理由见该任务。

第三段（B 组，依赖 A 组的容器）
T17 → T18 → T19 → T20 → T21 → T22 → T23 → T24

第四段（C 组）
T25 → T26 → T27 → T28 → T29 → T30 → T32
              │     T31 ←────────┘
              │      └→ T33
              └→ T14（从第二段移来：状态行跑起来了，工具行的秒数才能撤）

第五段（收尾，严格串行）
T34 → T35 → T36 → T37 → T38
```

**关键路径**：T1 → T5 → T6 → T8 → T10 → T11 → T17 → T19 → T22 → T23 → T36 → T38。
C 组（T25–T33）可在 A 组完成后与 B 组并行。
