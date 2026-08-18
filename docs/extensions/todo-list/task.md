# 主对话的待办清单 Tasks

> 对应 [`spec.md`](spec.md) / [`plan.md`](plan.md)。共 **26 个任务**，分四段。
>
> ⚠ **T1 是闸门**：它的实测结论决定待办块挂在哪，结论没出来之前不要写 T17–T19。
> 其余任务（T2–T16）与它无关，可以先做。

## 文件清单

| 操作 | 文件 | 职责 |
| --- | --- | --- |
| 新建 | `rhinecode/todo/__init__.py` | 对外导出 |
| 新建 | `rhinecode/todo/models.py` | `TodoState`、`TodoItem`、中文标签表 |
| 新建 | `rhinecode/todo/store.py` | `TodoStore`、`ReplaceResult`、校验与上限 |
| 新建 | `rhinecode/todo/render.py` | `TodoView`/`TodoRow`、`build_view`、两个文本函数 |
| 新建 | `rhinecode/tools/todo_write.py` | `TodoWriteTool` |
| 改 | `rhinecode/trace/models.py` | 新增 `TODO_UPDATE` 枚举 |
| 改 | `rhinecode/trace/reader.py` | `SUMMARIZERS` 登记摘要函数 |
| 改 | `rhinecode/subagents/toolset.py` | `GLOBAL_DENIED_TOOLS` 加 `todo_write` |
| 改 | `rhinecode/agent/prompt/modules.py` | `optional_slots` 新增 133 槽 |
| 改 | `rhinecode/agent/prompt/builder.py` | `todo_brief` 参数 + add + `_FILLED` |
| 改 | `rhinecode/conversation.py` | `todo_store` 属性、三个只读方法、两处清空、提示传参 |
| 改 | `rhinecode/bootstrap.py` | 建 store、注册工具 |
| 改 | `rhinecode/tui/widgets.py` | 新增 `TodoPane`；`HistoryView.compose` 多产出一个 |
| 改 | `rhinecode/tui/app.py` | CSS、`_refresh_todo`、`TOOL_RESULT` 触发、复位 |
| 新建 | `tests/test_todo_store.py` | 覆写/校验/上限/版本号/并发/无回调结构护栏 |
| 新建 | `tests/test_todo_render.py` | 显示决策/限高/排序/两个文本函数/同口径 |
| 新建 | `tests/test_todo_tool.py` | 工具属性/参数翻译/拒绝回灌/描述同口径 |
| 新建 | `tests/test_todo_integration.py` | 权限/子 Agent/提示槽/会话切换 |
| 新建 | `tests/test_todo_tui.py` | 可见性/转义/拖选可寻址/全部完成留痕 |
| 改 | `CLAUDE.md` | 成对维护点 + 扩展登记 |
| 改 | `docs/extensions/README.md` | 当前扩展表加一行 |
| 删 | `docs/todo/5-todo-list.md` | 做完即删（该文件夹的约定） |
| 改 | `docs/todo/README.md` | 清单重排 |

---

# 第一段 · 地基（T1–T8）

## T1: 实测 `dock: bottom` 在滚动容器内部成不成立

**文件：** 临时脚本，写在 scratchpad，**不进仓库**
**依赖：** 无
**这是本扩展唯一的未知项**，plan 里已登记退路。

**步骤：**
1. 在 scratchpad 建一个脚本，用 Textual 搭一个最小复现：
   一个 `ScrollableContainer`（带 `border: solid`、`padding: 0 1`），
   内含 `Vertical`（`height: auto; min-height: 100%`）装 40 条 `Static`，
   外加一个 `Static` 子节点声明 `dock: bottom; height: auto`。
   容器 `on_mount` 里调 `anchor()`——**要复刻 `HistoryView` 的全部设定**，
   少一样都可能得出错误结论。
2. 用 `async with app.run_test() as pilot:` 无头启动，终端尺寸固定 80×24。
3. 打印三组数字：滚动容器的 `region`、docked 子节点的 `region`、
   `scroll_y` 与 `max_scroll_y`。
4. 调 `pilot.pause()` 后**向上滚动**（`container.scroll_to(y=0)`），再打印同样三组。
5. 把 docked 子节点从 2 行改成 4 行，再打印一次。

**验证：** 逐条对照三个判据——
- **钉住**：向上滚动前后，docked 子节点的 `region.y` **不变**；
- **占位**：docked 子节点从 2 行变 4 行后，可滚动区域的高度**减少 2**
  （`max_scroll_y` 相应增加）；
- **不遮挡**：docked 子节点的 `region` 与 `#history-messages` 的可见 `region`
  **不重叠**。

三条全中 → 照 plan 的主方案实施。任何一条不中 → 走 plan 的**方案 B**
（`TodoPane` 提到 `#stage` 当 `HistoryView` 的兄弟、base 层 dock、
自画左右下框线、`HistoryView` 可见时挂一个去掉下边框的 class）。

**产出：** 把三组数字与结论写进 `docs/extensions/todo-list/plan.md` 末尾新增的
「T1 实测结论」一节。⚠ **必须写下实际数字**，不能只写「验证通过」——
后来的人要能据此判断这个结论在别的终端尺寸下还成不成立。

## T2: `todo/models.py`

**文件：** `rhinecode/todo/models.py`（新建）
**依赖：** 无

**步骤：**
1. 写模块 docstring：说明这是主对话的**私有进度笔记**，与 C15 的共享清单
   （多方协商的媒介）是两个东西，并写明「刻意不复用 `team` 的枚举」的理由。
2. 定义 `TodoState(str, Enum)`，三个取值 `pending` / `in_progress` / `completed`。
   注释写明字符串取值对齐 Claude Code 的 `TodoWrite`，是模型在参数里写的东西。
3. 定义 `TODO_STATE_LABELS: dict[TodoState, str]` = 待办 / 进行中 / 已完成。
   注释写明它**只用于显示**，且文字标签不可省——脱离颜色也要认得出（spec F14）。
4. 定义 `@dataclass(frozen=True) class TodoItem`：`title: str`、`state: TodoState`。
   docstring 写明「只有两个字段」是刻意的，以及 `frozen` 与 C15 `BoardTask`
   可变刻意相反的理由。
5. 定义 `parse_state(raw) -> Optional[TodoState]`：认字符串（大小写不敏感、
   连字符与下划线都认），认不出返回 `None`。

**验证：** `python -c "from rhinecode.todo.models import TodoState, TodoItem, parse_state; print(parse_state('IN-PROGRESS'), TodoItem('x', TodoState.PENDING))"`
打印出 `TodoState.IN_PROGRESS` 与一个 `TodoItem`。

## T3: `todo/store.py`

**文件：** `rhinecode/todo/store.py`（新建）
**依赖：** T2

**步骤：**
1. 定义 `MAX_ITEMS = 30`，常量上方注释写明它是**拒绝线不是截断线**
   （截断会让模型以为写进去了，而清单少了几条，spec F6）。
2. 定义 `@dataclass(frozen=True) class ReplaceResult`：`ok: bool`、`reason: str = ""`。
   docstring 写明「对外返回结构化结果不抛异常」的理由（调用方是工具）。
3. 定义 `class TodoStore`：`__init__(self, recorder=None)`，持
   `threading.Lock`、`_items: tuple[TodoItem, ...] = ()`、`_version: int = 0`。
   类 docstring 写明加锁不变量与「刻意不持有任何回调」。
   ⚠ `recorder` 是 trace 记录器，**它不是回调**——只在锁外调用，
   注释里要点明这个区别，免得结构护栏被误读成「不能有 recorder」。
4. 写私有纯函数 `_parse(raw) -> tuple[list[TodoItem], str]`（在**锁外**跑）：
   逐条校验，返回 `(items, "")` 或 `([], 原因)`。校验顺序与文案：
   - 顶层不是列表 → 说明期望的形状
   - 条数 > `MAX_ITEMS` → 给出当前条数与上限
   - 某条不是对象 / 无 `title` / strip 后为空 → **指明是第几条**
   - `state` 认不出 → 指明第几条、给的是什么、合法取值有哪三个
   （缺省 `state` 按 `pending` 处理）
5. `replace(raw) -> ReplaceResult`：先 `_parse`（锁外），失败**直接返回、不进锁**；
   成功则在锁内整表替换 + `_version += 1`；**出锁之后**再埋点，最后返回。
   docstring 写明「校验必须在写入之前全部跑完」的理由（spec F5）。
6. `snapshot()` / `version()` / `counts()` / `all_completed()` / `clear()`。
   `snapshot` 直接返回内部元组（`TodoItem` 不可变，无需复制）。
   `all_completed()` 在清单为空时返回 `False`——空清单不是「全做完了」。
7. 埋点方法 `_emit(ok, reason, items)`：整段包 `try/except` 后吞掉异常
   （观测设施绝不能反过来打断被观测的系统）。

**验证：** `python -m compileall rhinecode/todo` 通过，且
`python -c "from rhinecode.todo.store import TodoStore; s=TodoStore(); print(s.replace([{'title':'a'}]), s.version(), s.replace('x').reason)"`
打印成功结果、版本号 1、以及一句可读的中文拒绝原因。

## T4: `tests/test_todo_store.py`

**文件：** `tests/test_todo_store.py`（新建）
**依赖：** T3

**步骤：** 写这些用例——
1. 覆写语义：先写 3 条再写 2 条，结果**恰好是那 2 条**（AC1）。
2. 版本号：成功一次加一；**失败不加**（含反证）。
3. 校验四类各一条，断言原因里**含条目序号**。
4. 被拒时清单**逐字未变**（AC4）——先写一份合法的，再提交非法的，比对快照。
5. 上限：31 条被拒且**不是被截成 30 条**（AC5，含「快照仍是上一份」的断言）。
6. 两条同时 `in_progress` **提交成功**（AC3）。
7. `all_completed()` 空清单返回 `False`。
8. `clear()` 之后快照为空、`all_completed()` 为 `False`。
9. 并发：20 个线程各覆写一份不同的清单，结束后快照**恰好等于其中某一份**
   （不会是几份的混合），且版本号 == 20。
10. **结构护栏**：遍历实例属性，断言除 `_recorder` 外不存在 callable 成员
    （对齐 `tests/test_team_board.py` 那条）。

**验证：** `python -m unittest tests.test_todo_store -v` 全绿。

## T5: `todo/render.py` 的 `build_view`

**文件：** `rhinecode/todo/render.py`（新建）
**依赖：** T2

**步骤：**
1. 定义 `@dataclass(frozen=True) class TodoRow`：`title`、`state`。
2. 定义 `@dataclass(frozen=True) class TodoView`：`header: str`、
   `rows: tuple[TodoRow, ...]`、`overflow: str`。
   docstring 写明「这是界面该画成什么样的完整描述，但不含颜色与标记语言」。
3. 定义 `DISPLAY_LIMIT = 5`。
4. `build_view(items, limit=DISPLAY_LIMIT) -> Optional[TodoView]`：
   - 清单为空 → `None`
   - 全部 `COMPLETED` → `None`（spec F11）
   - `header` = `f"待办 ({已完成数}/{总数})"`
   - 排序：`IN_PROGRESS` → `PENDING` → `COMPLETED`，**同优先级内保持原序**
     （用 `sorted(..., key=优先级)`，Python 的 sort 是稳定的，注释点明这一条
     正是「保持原序」的实现依据）
   - 取前 `limit` 条
   - 被省掉的条数 > 0 时 `overflow` = `f"……还有 N 条，已完成 M 条"`，
     其中 M 是**被省掉的那些里**已完成的条数
5. 模块 docstring 写明：**排序只影响显示**，`store.snapshot()` 永远返回原序，
   理由是原序是模型表达的执行次序。

**验证：** `python -c` 造一份 15 条（3 完成、1 进行中、11 待办）的清单，
打印 `build_view` 的结果，肉眼确认进行中排第一、只有 5 行、overflow 文案正确。

## T6: `todo/render.py` 的两个文本函数

**文件：** `rhinecode/todo/render.py`（续）
**依赖：** T5

**步骤：**
1. `render_all_done_text(total) -> str` → `f"待办 {total}/{total} 全部完成"`。
2. `render_todo_brief() -> str` —— 系统提示里那段恒定文本（spec F19）。
   写清四层意思：
   - **多步任务开工前先列出待办**，做的过程中随时更新
   - **每次传完整清单**（不是增量）
   - **状态如实反映实际执行情况**：真在跑才标进行中，并行做多件事时
     ⚠ 已于 2026-08-18 推翻，现为「同一时刻只能有一条进行中」，见 spec F4 的勘误块
   - **下限可数**：少于三步的活、以及纯聊天式的问答，不必列
3. 函数 docstring 里写明**口径取「积极使用」一侧**及其理由
   （与委派刻意相反：委派要开一整条子对话是贵的路径，维护待办近乎零成本），
   并写明「下限必须可数」这条经验的来源（委派触发口径的两次反转）。
4. ⚠ docstring 里明确指认成对维护点：**本函数 ↔ `tools/todo_write.py`
   的 `description`**，两处必须同口径，这是同一个坑的第五次。

**验证：** `python -c "from rhinecode.todo.render import render_todo_brief; print(render_todo_brief())"`
输出的文本里能逐条找到上面四层意思。

## T7: `tests/test_todo_render.py`

**文件：** `tests/test_todo_render.py`（新建）
**依赖：** T5、T6

**步骤：** 写这些用例——
1. 空清单 → `None`（AC10 的逻辑半边）。
2. 全部完成 → `None`（AC15 的逻辑半边）。
3. 部分完成 → `header` 是 `待办 (2/4)`。
4. 15 条时 `rows` 恰好 5 条，`overflow` 文案含剩余条数与其中已完成条数（AC12）。
5. **排序**：进行中的排在待办之前；已完成的**不占名额**——造一份
   「5 完成 + 2 待办」的清单，断言显示出来的 5 条里那 2 条待办**都在**。
6. **稳定性反证**：两条同为 `PENDING` 时保持原序（换一下输入顺序，输出跟着换）。
7. 已完成条数不足以填满时（如只有 3 条且全非完成），`overflow` 为空串。
8. `render_all_done_text(4)` 含 `4/4`。
9. `render_todo_brief()` 的四层意思逐条断言（**含反证**：断言它**不含**
   「拿不准就列」这类无下限的措辞——那是委派那边刚反转掉的形态）。

**验证：** `python -m unittest tests.test_todo_render -v` 全绿。

## T8: `todo/__init__.py`

**文件：** `rhinecode/todo/__init__.py`（新建）
**依赖：** T2、T3、T5、T6

**步骤：**
1. 包 docstring：一句话说明它是叶子包、只依赖标准库与 `trace`。
2. re-export：`TodoState`、`TodoItem`、`TODO_STATE_LABELS`、`TodoStore`、
   `ReplaceResult`、`TodoView`、`TodoRow`、`build_view`、`render_todo_brief`、
   `render_all_done_text`、`DISPLAY_LIMIT`、`MAX_ITEMS`。
3. 定义 `__all__`。

**验证：** `python -c "import rhinecode.todo as t; print(sorted(t.__all__))"`
列出全部导出名，且 `python -c "import rhinecode.todo"` 不触发任何本项目其他模块的导入
（用 `python -X importtime -c "import rhinecode.todo" 2>&1 | grep -o "rhinecode\.[a-z_.]*" | sort -u`
确认只出现 `todo` 自身与 `trace` 两族）。

---

# 第二段 · 工具与接线（T9–T16）

## T9: trace 新增事件类型

**文件：** `rhinecode/trace/models.py`、`rhinecode/trace/reader.py`
**依赖：** 无

**步骤：**
1. `models.py` 的 `TraceEventType` 加 `TODO_UPDATE = "todo_update"`，
   行尾注释写明用途（「待办清单的一次覆写」）。
2. `reader.py` 新增 `_s_todo_update(r) -> str`：摘要行给出
   `成功/被拒` + `总数/已完成/进行中` + 被拒时的原因首句。
3. 把它登记进 `SUMMARIZERS`。

**验证：** `python -m unittest tests.test_trace_reader -v` 全绿
（该文件里有一条遍历全部枚举值断言都已登记的护栏，漏登记会当场红）。

## T10: `tools/todo_write.py`

**文件：** `rhinecode/tools/todo_write.py`（新建）
**依赖：** T8

**步骤：**
1. 模块 docstring：说明它是唯一的写入口、不弹面板但仍过管线、
   以及 `system_serial=True` 精确意味着哪两件事。
2. `class TodoWriteTool(Tool)`，`__init__(self, store)`。
3. 类属性：`name = "todo_write"`、`read_only = False`、`system_serial = True`、
   `plan_safe = True`。⚠ **不声明** `classifier_scope` 与 `workspace_aware`，
   并在注释里写明「刻意不声明」的理由（不落分类器那三类；不碰路径）。
4. `description`：与 `render_todo_brief()` **同口径**的四层意思，
   ⚠ 注释里指认成对维护点。
5. `parameters`：`{"type":"object","properties":{"todos":{"type":"array",
   "items":{...title/state...}}},"required":["todos"]}`。
   `state` 用 `enum` 列出三个取值，`description` 写清语义。
6. `execute(self, args, plan_stage: bool = False) -> ToolResult`：
   取 `args.get("todos")` 交给 `store.replace`；
   - 失败 → `ToolResult(ok=False, output=result.reason, summary="待办未更新")`
   - 成功 → `output` 是当前清单的文字版（逐行「序号. 标题 —— 状态」），
     `summary` 是 `f"{总数} 条待办，已完成 {N} 条"`
   ⚠ `plan_stage` 只接住不分支，注释写明「两个阶段行为完全相同」的理由。

**验证：** `python -c` 造一个 store 与工具，调 `execute({"todos":[{"title":"a","state":"in_progress"}]})`，
打印 `ok` / `output` / `summary`；再调一次非法参数，确认 `ok=False` 且 `output` 是可读中文。

## T11: `tests/test_todo_tool.py`

**文件：** `tests/test_todo_tool.py`（新建）
**依赖：** T10

**步骤：** 写这些用例——
1. 四个类属性取值逐个断言（`read_only=False` / `system_serial=True` /
   `plan_safe=True` / 无 `classifier_scope`）。
2. `execute` **接受** `plan_stage` 关键字参数（用 `inspect.signature` 断言，
   这是 `plan_safe=True` 的硬约定，漏了会在规划阶段抛 `TypeError`）。
3. 成功路径：`ok=True`，`output` 含每条标题与中文状态，`summary` 含条数。
4. 失败路径：`ok=False`，`output` 是 store 给的可读原因，且**store 未被改动**。
5. `parameters` 的 `state` 枚举取值与 `TodoState` 的取值集合**逐字相等**
   （漏改一处会让模型写出一个工具认不出的状态）。
6. **同口径护栏 `SameVoiceTest`**：`description` 与 `render_todo_brief()`
   都含那四层意思；**含反证**——把其中一处的某层意思去掉，断言会红。

**验证：** `python -m unittest tests.test_todo_tool -v` 全绿。

## T12: 子 Agent 禁用

**文件：** `rhinecode/subagents/toolset.py`
**依赖：** 无

**步骤：**
1. `GLOBAL_DENIED_TOOLS` 加 `"todo_write"`，上方注释写明理由：
   子 Agent 的进度已有活动区，再来一份会让屏幕上出现两处对不上的进度数字
   （spec F9）。
2. 注释里点明它排在角色白名单**之前**，所以一条 `tools: [todo_write]`
   的角色定义也拿不到它。

**验证：** `python -m unittest tests.test_subagent_toolset -v` 全绿
（该文件有遍历该集合逐个断言的护栏，新增项自动被覆盖）。

## T13: 系统提示槽位

**文件：** `rhinecode/agent/prompt/modules.py`、`rhinecode/agent/prompt/builder.py`
**依赖：** 无

**步骤：**
1. `modules.py` 的 `optional_slots()` 加
   `PromptModule(name="待办清单", priority=133, cacheable=True, content="")`，
   并在函数 docstring 的槽位清单里加一行，写明**为什么是 133**
   （恒定文本、缓存上与 134 等价；语义上先管好自己再找别人）。
2. `builder.py` 的 `build_default_prompt` 加参数 `todo_brief: str = ""`，
   加 `builder.add(PromptModule(name="待办清单", priority=133, cacheable=True, content=todo_brief))`，
   并把 `"待办清单"` 加进 `_FILLED`。
3. docstring 的 `:param todo_brief:` 写明它只由主对话传（子 Agent 拿不到这个工具）。

**验证：** `python -c` 调 `build_default_prompt(env, todo_brief="XYZ")`，
断言 `stable` 里 `XYZ` **恰好出现一次**（漏改 `_FILLED` 会让槽位加两次）；
再调一次不传该参数，断言输出与改动前逐字一致。

## T14: 协调层

**文件：** `rhinecode/conversation.py`
**依赖：** T8

**步骤：**
1. `__init__` 加 `todo_store=None` 参数并存为属性（与 `team_service` 同位置、
   同注释形态：由装配层注入）。
2. 加三个只读方法：`todo_version()`（无 store 返回 0）、
   `todo_view()`（无 store 返回 `None`，否则 `build_view(store.snapshot())`）、
   `todo_all_done_text()`（用 `store.counts()` 的总数）。
3. 两处 `build_default_prompt` 调用点（主对话 `_run` 与 fork 子对话
   `_run_forked_skill`）：**只在主对话那处**传
   `todo_brief=render_todo_brief() if self.todo_store else ""`。
   ⚠ 注释写明 fork 子对话不传的理由。
4. `clear()` 与 `_resume_stream` 两处：加 `if self.todo_store: self.todo_store.clear()`，
   **与既有的 Skill 激活态清理写在一起**（那里已经是会话切换的复位收口点）。

**验证：** `python -m unittest tests.test_conversation tests.test_clear_stale_deliverables -v` 全绿
（确认没碰坏既有的会话切换路径）。

## T15: 装配

**文件：** `rhinecode/bootstrap.py`
**依赖：** T10、T14

**步骤：**
1. 在建 `TeamService` 的同一条件分支内，建
   `todo_store = TodoStore(recorder=recorder)`，赋给 `manager.todo_store`。
2. `tool_registry.register(TodoWriteTool(todo_store))`。
3. 注释写明：不注册时 `manager.todo_store` 为 `None`、view 恒为 `None`、
   待办块永不显示——**这就是零回归的实现方式**，不需要额外开关（spec N5）。

**验证：** `python -m unittest tests.test_bootstrap -v` 全绿；
另跑 `python -m unittest tests.test_web_bootstrap -v`（它数 `build_default_prompt`
的调用点次数，确认没有意外新增）。

## T16: `tests/test_todo_integration.py`

**文件：** `tests/test_todo_integration.py`（新建）
**依赖：** T12、T13、T14、T15

**步骤：** 写这些用例——
1. **不弹面板**：走权限引擎判定该工具，断言结论不是 ASK（AC6 前半）。
2. **deny 生效**：配一条不带括号的 `deny: todo_write` 规则，断言结论是 DENY
   （AC6 后半）。⚠ 用**不带括号**的整工具形式——它落 `other` 分支。
3. **子 Agent 拿不到**：用一个 `tools: ["todo_write"]` 的角色跑一次工具集解析，
   断言最终集合里没有它（AC8）。
4. **提示槽位**：`build_default_prompt(..., todo_brief=render_todo_brief())`
   的 `stable` 段里含那四层意思之一，且该文本**恰好出现一次**（AC21）。
5. **规划阶段不可用**（⚠ 2026-08-18 反转，见 spec F8 勘误块）：`plan_safe` 为假，规划阶段守卫拦下它，获批后放行（AC7）。
6. **会话切换清空**：往 store 写 3 条，调协调层的 `clear()`，断言快照为空（AC18）。

**验证：** `python -m unittest tests.test_todo_integration -v` 全绿。

---

# 第三段 · 界面（T17–T22）

> ⚠ **本段全部依赖 T1 的实测结论。** 结论若是「走方案 B」，
> T18/T19 的步骤按 plan 的方案 B 改写后再执行。

## T17: `TodoPane` 组件

**文件：** `rhinecode/tui/widgets.py`
**依赖：** T5、T1

**步骤：**
1. **先做撞名预检**：对每个准备用的实例字段名跑
   `hasattr(Static("x"), name)`，撞上就换名。⚠ 本项目已撞过三次
   （`_render` / `_closed` / `_running`），**一律不报错、只表现为界面上
   东西凭空少了**。把预检结果写进类注释。
2. `class TodoPane(Static)`，`DEFAULT_CSS = "TodoPane { display: none; }"`
   （与四个面板同一做法：不依赖外部 CSS 也能初始隐藏）。
3. 颜色表：`{IN_PROGRESS: "#FFA500", COMPLETED: "#5FD75F", PENDING: 次级灰}`，
   注释写明**取值与活动区同源**。
4. `update_view(self, view)`：
   - `view is None` → `self.display = False` 并返回
   - 否则拼 markup：表头一行 `● 待办 (2/4)`，每条一行
     `  ● {标题}{补齐空格}{中文标签}`，有 `overflow` 时末尾一行
   - 标题与标签之间按 `cell_len` 补齐（与确认面板四个选项同一做法）
   - ⚠ **一切文本经本模块的 `escape`**，绝不用 rich 那版
   - ⚠ 内容经 `set_markup`（而非 `update()`），使这块内容可拖选、可寻址
   - 最后 `self.display = True`
5. docstring 写明它是**观测区不是交互区**（区内没有任何操作入口）。

**验证：** `python -m unittest tests.test_tui_symbols -v` 全绿
（符号白名单扫描，确认没引入新符号）。

## T18: `HistoryView` 挂载待办块

**文件：** `rhinecode/tui/widgets.py`
**依赖：** T17

**步骤：**
1. `HistoryView.compose` 在 `yield Vertical(id="history-messages")` 之后
   多 `yield TodoPane()`。
2. 在 `clear_all` 的 docstring 里补一句：它只清 `#history-messages` 的子节点，
   **天然不碰待办块**（待办的清空走协调层，见 spec F17）——
   写下来是为了防止后来的人「顺手」改成 `self.remove_children()`。
3. 加一个 `todo_pane()` 便捷取值方法（`query_one(TodoPane)`），供 `app.py` 调用。

**验证：** `python -m unittest tests.test_tui_selection tests.test_tui_tool_pending -v` 全绿
（确认没碰坏历史区既有行为）。

## T19: CSS

**文件：** `rhinecode/tui/app.py`
**依赖：** T18

**步骤：**
1. 在 `HistoryView > Vertical` 那段之后新增：
   ```
   TodoPane {
       dock: bottom;
       height: auto;
       display: none;
       border: none;
       border-top: tall #808080 60%;
   }
   ```
2. 注释写明三件事：**灰色分隔线**（观测区，与四个青色交互面板分开，
   与活动区同一条理由）；**不加左右 padding**（`HistoryView` 自带
   `padding: 0 1`，再加一层会把框线内侧擦出空白——这是第 19 条验收
   已经踩过的坑）；**`display: none` 是缺省态**，可见性由 `update_view` 切换。
3. 把 T1 实测的三组数字摘一句进注释（「实测 80×24：待办块 4 行时
   可滚动区高度由 X 变为 X-4，滚动前后 `region.y` 不变」）。

**验证：** 启动一次真实界面（`python -m rhinecode --config ...`）
或用 e2e 宿主起一个，肉眼确认：无待办时布局与改造前一致、框线完整。

## T20: `_refresh_todo` 与触发点

**文件：** `rhinecode/tui/app.py`
**依赖：** T19、T14

**步骤：**
1. `__init__` 加两个状态字段：`self._todo_version = 0`、`self._todo_shown = False`。
2. 加 `_refresh_todo(self)`（**主线程**）：
   - 读 `self._manager.todo_version()`，与 `self._todo_version` 相同则直接返回
   - 记下新版本号，取 `view = self._manager.todo_view()`
   - **全部完成的转移检测**：`self._todo_shown and view is None and
     self._manager.todo_all_done()` → `self.show_event(self._manager.todo_all_done_text())`
   - `self.query_one(HistoryView).todo_pane().update_view(view)`
   - `self._todo_shown = view is not None`
   - 整段包 `try/except`（观测设施不得反过来打断界面）
3. `_do_stream` 的 `TOOL_RESULT` 分支末尾（`widget.finish` 之后）加：
   在**工作线程**读 `self._manager.todo_version()`，与 `self._todo_version`
   不同才 `self.call_from_thread(self._refresh_todo)`。
   ⚠ 注释写明**为什么不搭 `_poll_subagents` 的定时器**（它只在子 Agent 服务
   启用时注册，搭它会让待办块在关掉子 Agent 时整个不刷新，而配置和界面上
   都看不出异常），以及**为什么不新增定时器**（空闲开销必须与改造前一致）。
4. 协调层补一个 `todo_all_done()`（`store` 存在且 `all_completed()`）。

**验证：** 用 e2e 宿主 + 脚本化 Provider 跑一个「调 `todo_write` 三次、
最后一次全部完成」的剧本，`screen --selector "#history-messages"`
确认出现「全部完成」那一行且**只出现一次**。

## T21: 会话切换的界面复位

**文件：** `rhinecode/tui/app.py`
**依赖：** T20

**步骤：**
1. `_reset_display_state()` 里加三行：待办块 `update_view(None)`、
   `self._todo_version = 0`、`self._todo_shown = False`。
2. 注释写明版本号也要复位的理由：新会话的 store 从 0 重新计数，
   不复位的话第一次覆写（版本号 1）在旧值是 5 的情况下**照样会被认成变化**
   ——碰巧对，但依据是错的；而若旧值恰好是 1 就会**漏一次刷新**。
   ⚠ 这一条不复位的后果是「换了会话之后第一次列待办不显示」，
   而那看起来像功能坏了。

**验证：** `python -m unittest tests.test_tui_keybindings -v` 全绿；
另用 e2e 宿主跑「列待办 → `/clear` → 看界面」，确认待办块收起。

## T22: `tests/test_todo_tui.py`

**文件：** `tests/test_todo_tui.py`（新建）
**依赖：** T17–T21

**步骤：** 写这些用例——
1. `update_view(None)` → `display` 为 `False`（AC10）。
2. `update_view(有内容)` → `display` 为 `True`，且渲染文本含表头与各条标题。
3. **转义**：标题含 `[` 的待办能正常渲染，**不抛异常**（AC17，含反证：
   断言用 rich 那版 `escape` 会漏掉落单的 `[`）。
4. **三档可区分**：渲染出的纯文本里三个中文标签都在（AC13，
   判据刻意**不看颜色**——那正是「脱离颜色也认得出」的意思）。
5. **拖选可寻址**：对该组件调 `get_widget_and_offset_at`，断言返回的
   offset **不是 `None`**（tui-activity-fold 第 6 轮的教训：只判「能不能复制」
   在旧实现下照样全绿）。
6. **全部完成留痕恰好一次**：模拟版本号连续变化两次（第二次全部完成），
   断言 `show_event` 被调用**恰好一次**（AC15）。
7. **复位**：调 `_reset_display_state` 后待办块隐藏且版本号归零。

**验证：** `python -m unittest tests.test_todo_tui -v` 全绿。

---

# 第四段 · 收尾与验收（T23–T26）

## T23: `CLAUDE.md` 登记

**文件：** `CLAUDE.md`
**依赖：** T22

**步骤：**
1. 「已实现的扩展」小节加一段：待办清单，一句话说明 + 指向
   `docs/extensions/todo-list/spec.md`。
2. 「成对维护点」加一条：**系统提示那段 ↔ 工具描述**，写明这是**同一个坑的
   第五次**（列出前四次），并指向护栏 `test_todo_tool.py::SameVoiceTest`。
3. 架构表的 TUI 行补一句待办块；新增一行 `Todo | todo/ | ...`
   （⚠ 致命不变量那一列写：不 import `team`；加锁临界区只做纯内存读写；
   `MAX_ITEMS` 是拒绝线不是截断线）。
4. 「运行时斜杠命令」**不动**（本扩展不加命令）。

**验证：** 通读改动段落，确认没有与既有条目矛盾的说法。

## T24: 扩展索引登记

**文件：** `docs/extensions/README.md`
**依赖：** T23

**步骤：** 「当前扩展」表加一行，格式与既有五行一致
（扩展名 / 状态含任务数与测试数 / 一句话起点与做法）。

**验证：** 表格渲染正常，链接可达。

## T25: 清理 todo 文件夹

**文件：** `docs/todo/5-todo-list.md`（删）、`docs/todo/README.md`（改）
**依赖：** T24

**步骤：**
1. `git rm docs/todo/5-todo-list.md`（该文件夹的约定：做完即删）。
2. `README.md` 的清单表删掉第 5 行；**按命名规则重排序号**——
   原 `6-ask-user-panel.md` 前移为 `5-`（用 `git mv` 保住历史）。
3. `README.md` 抬头的「上次改动」加一条，写明本次删了哪个、重排了什么。
4. 「已完成（已从本文件夹移除）」小节加一条，写明与原 todo 的**分歧**：
   原文倾向的三处显示（历史区块 / 状态行 `N/M` / 只读命令）**评审时砍到只剩一处**
   ——状态行那段被判为冗余（待办块本身常驻可见，同一个数字同屏出现两次），
   只读命令同理（清单没有「藏起来需要翻出来看」的状态）。

**验证：** `ls docs/todo/` 显示序号连续无空号；`README.md` 表格行数与文件数一致。

## T26: 全量验证

**文件：** 无
**依赖：** T25

**步骤：**
1. `python -m compileall rhinecode tests`
2. `python -m unittest discover -s tests`
3. 逐条跑 `checklist.md`。

**验证：** 编译无错；全量测试全绿（skipped 仍是 4 项）；checklist 逐条有证据。

---

## 执行顺序

```
T1（闸门，实测）─────────────────────────┐
                                        │
T2 → T3 → T4                            │
  ↘  T5 → T6 → T7                       │
        ↘ T8 ─┬─→ T10 → T11             │
              │                         │
T9 ───────────┤                         │
T12 ──────────┤                         │
T13 ──────────┼─→ T14 → T15 → T16       │
              │                         │
              └─────────────────────────┴─→ T17 → T18 → T19 → T20 → T21 → T22
                                                                            │
                                                          T23 → T24 → T25 → T26
```

**可并行的三组**：`T9`（trace）、`T12`（子 Agent 禁表）、`T13`（提示槽）
互不相干，与第一段也不相干。

**T1 可以最先做也可以与第一段并行**——它只产出一个结论，不产出代码；
但**第三段动手之前必须有结论**。
