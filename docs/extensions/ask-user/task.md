# 澄清提问面板：从「Plan Mode 专属」到「随时可用」 Tasks

> 对应 [`spec.md`](spec.md) 与 [`plan.md`](plan.md)。共 **35 个任务**，分七组。
>
> **执行顺序的三条硬依赖**（plan.md 末节）：数据类 → 纯函数 → 循环；
> 驱动设施必须最后改（它读产品侧私有属性）。提示词那一组（T7–T10）
> 与面板那一组（T17–T21）互不相干，可并行。

---

## 文件清单

| 操作 | 文件 | 职责 |
| --- | --- | --- |
| 新建 | `rhinecode/agent/clarify.py` | 解析模型给的问题 + 回灌文案（纯函数、零 IO） |
| 修改 | `rhinecode/agent/events.py` | `ClarifyOption` 改字段；新增 `ClarifyQuestion` / `ClarifyReply` |
| 修改 | `rhinecode/agent/plan_tools.py` | schema 换形状；description 重写；拆 `ask_schemas` |
| 修改 | `rhinecode/agent/loop.py` | 可见性判据 / `_run_special` 重写 / 熔断计数 / Esc 分岔 |
| 修改 | `rhinecode/agent/prompt/texts/task_mode.py` | 同口径文本② |
| 修改 | `rhinecode/agent/prompt/texts/plan.py` | 同口径文本③；删「一次一个问题」 |
| 修改 | `rhinecode/conversation.py` | 无人值守轮 `clarify=None` |
| 修改 | `rhinecode/tui/widgets.py` | `NumberedPanel.activate_choice`；`ClarifyPanel` 重写 |
| 修改 | `rhinecode/tui/app.py` | `_clarify` 换签名 / `_interact` 加两参 / 状态机 / 两条按键岔路 |
| 修改 | `rhinecode/trace/reader.py` | 交互事件摘要行 |
| 修改 | `rhinecode/skills/manager.py` | `/skills` 报告里那句过期说明 |
| 修改 | `tests/e2e/protocol.py` | 四种应答取值；放开 `via=keys` |
| 修改 | `tests/e2e/control.py` | `settlement_for` / `_answer_by_keys` |
| 新建 | `tests/test_ask_user_parse.py` | F10 瑕疵表逐行 |
| 新建 | `tests/test_ask_user_trigger.py` | 三处同口径 + 八示例 |
| 新建 | `tests/test_ask_user_loop.py` | 可见性 / 三条文案 / Esc 分岔 / 熔断 |
| 新建 | `tests/test_ask_user_panel.py` | 面板渲染 / 多选 / 自由输入态 |
| 新建 | `tests/test_e2e_ask_user.py` | 三条无头驱动路径 |
| 修改 | `tests/test_tui_symbols.py` | 「刻意不新增符号」的护栏 |
| 修改 | `CLAUDE.md` | 安全边界 F6a；成对维护点两条 |

---

## 第一组：地基（数据类与纯函数）

### T1: 新字段名撞不撞 Textual 内部字段

**文件：** 无（一次性预检）
**依赖：** 无

**步骤：**
1. 列出本次要新增的实例字段：`_checked`（ClarifyPanel）、
   `_clarify_question` / `_clarify_free_text`（App）。
2. 在一个真实实例上逐个 `hasattr` 查一遍。

**验证：**
```bash
python -c "
from textual.widgets import Static, OptionList
from textual.app import App
for name in ('_checked','_clarify_question','_clarify_free_text'):
    print(name, hasattr(Static('x'), name), hasattr(OptionList(), name), hasattr(App(), name))
"
```
期望：九个都是 `False`。任何一个 `True` 就换名字。

⚠ `vars(cls)` 查不出来——那些是 `__init__` 里设的**实例属性**，必须在实例上查
（`CLAUDE.md` 成对维护点，tui-activity-fold 一轮撞了三个）。

---

### T2: 三个数据类

**文件：** `rhinecode/agent/events.py`
**依赖：** T1

**步骤：**
1. `ClarifyOption` 的 `summary` → `label`、`detail` → `description`，
   docstring 说明改名理由（对齐官方，spec F7）。
2. 新增 `ClarifyQuestion`：`question` / `options`（`tuple`）/ `header` / `multi_select`。
   docstring 写明 `options` 用 `tuple` 是因为它跨线程传给界面层。
3. 新增 `ClarifyReply`：`kind` / `labels` / `text`，docstring 带 plan.md 那张
   「五种情形对应什么」的表，**并写明 `None` 表示跳过、与「多选零勾选」是两回事**。

**验证：** `python -c "from rhinecode.agent.events import ClarifyQuestion, ClarifyReply, ClarifyOption; print(ClarifyOption('a','b'))"`

---

### T3: `clarify.py` 的常量与解析

**文件：** `rhinecode/agent/clarify.py`（新建）
**依赖：** T2

**步骤：**
1. 模块 docstring：本模块为什么是纯函数零 IO、为什么不放在 `loop.py` 里。
2. 四个常量 `MAX_QUESTIONS=4` / `MAX_OPTIONS=4` / `MAX_HEADER=12` / `SKIP_LIMIT=2`，
   每个上方注明对应的 spec 条目。
3. `parse_questions(raw) -> tuple[list[ClarifyQuestion], list[str]]`，
   严格按 plan.md 那个处理顺序实现，**任何输入都不抛异常**。

**验证：**
```bash
python -c "
from rhinecode.agent.clarify import parse_questions
for raw in (None, [], 'x', [{}], [{'question':'q'}], [{'question':'q','options':[{'label':'a'}]}]):
    print(repr(raw)[:40], '->', parse_questions(raw))
"
```
期望：六种输入全部返回二元组、一个异常都不抛。

---

### T4: `clarify.py` 的文案与回灌渲染

**文件：** `rhinecode/agent/clarify.py`
**依赖：** T3

**步骤：**
1. `render_answers(pairs, notes)`：spec F12 那份清单，四种答案类型各有措辞；
   末尾附瑕疵说明。
2. `render_unavailable(*, interactive, unattended)`：spec F4 三条文案。
   ⚠ 三条各自要说清「你现在该做什么」，不是三种说法同一件事。
3. `NO_QUESTIONS_FEEDBACK`、`SKIP_FEEDBACK`、`SKIP_CIRCUIT_FEEDBACK` 三个常量。
   ⚠ `SKIP_FEEDBACK` **必须同时含两层意思**：按最佳判断继续 + 不要为同一件事
   再问一次（spec F17，少了第二层模型下一轮会原样再问）。

**验证：**
```bash
python -c "
from rhinecode.agent.clarify import render_unavailable, SKIP_FEEDBACK
a=render_unavailable(interactive=False, unattended=False)
b=render_unavailable(interactive=True, unattended=True)
c=render_unavailable(interactive=True, unattended=False)
assert len({a,b,c})==3, '三条文案必须各不相同'
print(a); print(b); print(c); print(SKIP_FEEDBACK)
"
```

---

### T5: 解析的护栏

**文件：** `tests/test_ask_user_parse.py`（新建）
**依赖：** T4

**步骤：** 按 spec F10 那张表逐行写用例，七种情形各一条：
`questions` 非数组 / 无可用问题 / 问题超 4 / 某题缺原文 / 某题无候选项 /
候选项超 4 / 候选项恰好 1 个；外加 `header` 超长与 `multi_select` 类型不对。
再加一条：**被截掉的事实必须出现在 `notes` 里**（spec F10 的判据）。

**验证：** `python -m unittest tests.test_ask_user_parse -v`

---

## 第二组：提示词三处同口径（可与第三、四组并行）

### T6: 工具 schema 换形状

**文件：** `rhinecode/agent/plan_tools.py`
**依赖：** T2

**步骤：**
1. `AskUserTool.parameters` 改成 `questions` 数组，字段名对齐官方
   （`question` / `header` / `options[].label` / `options[].description` / `multiSelect`）。
2. 每个字段的 `description` 里写清约束（1–4 个问题、2–4 个选项、header ≤12 字符）。
3. 把 `plan_schemas()` 拆成 `ask_schemas()`（只含 `ask_user`）与
   `plan_schemas()`（只含 `present_plan`），并在两者的 docstring 里
   **写明判据从此不同**。

**验证：**
```bash
python -c "
from rhinecode.agent.plan_tools import ask_schemas, plan_schemas
print([s['function']['name'] for s in ask_schemas()])
print([s['function']['name'] for s in plan_schemas()])
"
```
期望：`['ask_user']` 与 `['present_plan']`。

---

### T7: 工具描述重写（五层 + 八示例）

**文件：** `rhinecode/agent/plan_tools.py`
**依赖：** T6

**步骤：**
1. 写五层意思，锚点词 `拍板` / `一两次` / `默认做法` / `已经说过` / `2 到 4 个`
   必须逐字出现。
2. 写**四个正例 + 四个负例**，每个都带一句判据（为什么该问 / 为什么不该问）。
3. ⚠ 负例里**必须有一个贴着下限的临界情形**——听起来像该问、实际不该问的那种
   （例如「这个函数放哪个文件」，答案在代码结构里）。「用户刚说过的」
   那类模型本来就不会误判，放进去等于白占篇幅。
4. 在常量上方写一段注释：这八个示例是本工具**唯一有效**的触发手段，
   依据是 todo-list 2026-08-18 的真机复测，**别当装饰删掉省 token**。

**验证：** `python -c "from rhinecode.agent.plan_tools import AskUserTool; d=AskUserTool.description; [print(w, w in d) for w in ('拍板','一两次','默认做法','已经说过','2 到 4 个')]"`

---

### T8: 任务模式那段

**文件：** `rhinecode/agent/prompt/texts/task_mode.py`
**依赖：** 无（可与 T7 并行）

**步骤：** 改写「区分两种提问」那一条：把「停下来什么都不交付、等用户回答」
换成「用 `ask_user` 给出 2 到 4 个选项让他点一下」，并补齐五层意思的锚点词。
⚠ 篇幅要能被**扫读**（它每轮都发），不要照抄工具描述那份说明书。

**验证：** `python -c "from rhinecode.agent.prompt.texts.task_mode import TASK_MODE; [print(w, w in TASK_MODE) for w in ('拍板','一两次','默认做法','已经说过','2 到 4 个')]"`

---

### T9: 计划模式那段

**文件：** `rhinecode/agent/prompt/texts/plan.py`
**依赖：** 无（可与 T7/T8 并行）

**步骤：**
1. `PLAN_FULL` 第 2 步改写：**删掉「一次一个问题」**（F7 落地后它是错的），
   改成「一次最多 4 个」；字段名从 `summary` / `detail` 改成 `label` / `description`。
2. 补齐五层锚点词。

**验证：** `python -c "from rhinecode.agent.prompt.texts.plan import PLAN_FULL; assert '一次一个问题' not in PLAN_FULL; print('ok')"`

---

### T10: 三处同口径的护栏

**文件：** `tests/test_ask_user_trigger.py`（新建）
**依赖：** T7, T8, T9

**步骤：** 照抄 `tests/test_todo_tool.py::SameVoiceTest` 的形态，但**三处不是两处**：
1. `_LAYERS` 五个锚点在三处文本里逐个断言。
2. **护栏自身的护栏**：`assertEqual(len(_LAYERS), 5)` + 无重复，
   docstring 写明「缩表是前五次共同的失效方式」。
3. 三处文本两两不相等（同口径 ≠ 同一份文本）。
4. 反证：`PLAN_FULL` 不含「一次一个问题」。
5. `ManyShotExamplesTest`：正反各 4 个、每个带判据、临界负例在、
   描述里不含无下限的推力词。

**验证：** `python -m unittest tests.test_ask_user_trigger -v`

---

## 第三组：Agent 循环

### T11: 可见性判据

**文件：** `rhinecode/agent/loop.py`
**依赖：** T6

**步骤：**
1. `_schema_for` 加入参 `can_ask_user: bool`；`planning` 只再管 `plan_schemas()`。
2. 调用点传 `can_ask_user=clarify is not None`。
3. 在 `_schema_for` 的 docstring 里写明**两个特殊工具的判据从此不同**，
   以及「F2 由此结构性成立，不靠禁用清单」。

**验证：** `python -m unittest tests.test_agent_loop -v`（既有用例先全绿）

---

### T12: `_run_special` 的 ask 分支重写

**文件：** `rhinecode/agent/loop.py`
**依赖：** T4, T11

**步骤：**
1. `_run_special` 加入参 `planning` / `interactive` / `unattended` / `clarify_skips`。
2. 按 plan.md 那段伪代码重写 ask 分支：解析 → 三条「问不了人」文案 →
   无可用问题 → 熔断 → 多问题串行。
3. 三条「问不了人」与熔断路径都**不置 `cancelled`、不置 `user_denied`**，
   注释写明理由（没有任何用户做过决定）。

**验证：** `python -m compileall rhinecode/agent && python -m unittest tests.test_agent_loop tests.test_plan_mode -v`

---

### T13: 熔断计数

**文件：** `rhinecode/agent/loop.py`
**依赖：** T12

**步骤：**
1. `_RoundContext` 加 `clarify_skipped: int = 0`。
2. `run()` 里一个局部累加量，照抄 `consecutive_unknown` 的位置与写法。
3. `_execute` 加入参 `clarify_skips` 并透传给 `_run_special`。
4. ⚠ 判据写成 **「累计 + 本轮已跳过」**，注释说明：模型可能在同一轮里调两次
   `ask_user`，只看跨轮累计会让熔断晚一次生效。

**验证：** 新增用例见 T16。先跑 `python -m unittest tests.test_agent_loop`

---

### T14: Esc 语义分岔

**文件：** `rhinecode/agent/loop.py`
**依赖：** T12

**步骤：** 在 ask 分支里按 `planning` 分两条：规划阶段置 `ctx.cancelled`（现状不变），
非规划阶段回灌 `SKIP_FEEDBACK` 并 `break`。注释写明这是**两种行为**，
不是一种行为的两个措辞。

**验证：** `python -m unittest tests.test_plan_mode -v`（Plan Mode 既有行为必须全绿）

---

### T15: 无人值守轮不给澄清回调

**文件：** `rhinecode/conversation.py`
**依赖：** 无

**步骤：** 在 `ask = None if unattended else ...` 紧邻处加
`clarify = None if unattended else self.clarify_callback`，把它传给 `run()`；
注释接着上面那段「纵深防御」写，说明两行是同一件事的两半。

**验证：** `grep -n "clarify = None if unattended" rhinecode/conversation.py`

---

### T16: 循环侧的护栏

**文件：** `tests/test_ask_user_loop.py`（新建）
**依赖：** T13, T14, T15

**步骤：**
1. **可见性**：`clarify` 非空 → schema 里有 `ask_user`；为空 → 没有。
   `present_plan` 只在规划阶段出现（两条反证）。
2. **三条文案**：子 Agent / 无人轮 / 兜底各回灌不同文本，且三者都不终止循环。
3. **Esc 分岔**：规划阶段 → `USER_CANCELLED` 收尾；非规划阶段 → 循环继续，
   回灌含两层意思。
4. **熔断**：跳过 2 次后第 3 次**不调 clarify 回调**
   （⚠ 断言的是**调用次数**，不是结果——只断言「结果是跳过」的话，
   「熔断了」与「又问了一次而用户又跳过」看不出区别）。
5. **同一轮两次调用**也计入熔断。

**验证：** `python -m unittest tests.test_ask_user_loop -v`

---

## 第四组：面板（可与第二、三组并行）

### T17: `NumberedPanel` 的覆写点

**文件：** `rhinecode/tui/widgets.py`
**依赖：** T1

**步骤：** 加 `activate_choice(index)`，缺省实现 = `highlighted = index; action_select()`；
docstring 写明它存在的理由（让每个面板自己回答「数字键命中我时该干嘛」，
而不是让 App 去 `isinstance` 判面板类型）。

**验证：** `python -m unittest tests.test_tui_panels -v`（既有面板用例必须全绿——
这一步只加一个缺省实现等价于现状的覆写点，任何一条红都说明改错了）

---

### T18: `ClarifyPanel.show_question`

**文件：** `rhinecode/tui/widgets.py`
**依赖：** T2, T17

**步骤：**
1. 换签名为 `show_question(question, index, total)`。
2. 表头 = `[徽章]  问题原文  （问题 i/N）`；**徽章为空时整个 `[…]` 不出现，
   单问题时进度整段不出现**。
3. 每个选项一行可选（`label`）+ 一行 disabled 说明（`description`，仍不占序号）。
4. 末尾无条件追加「其它…（自己打字）」，`id=OTHER_ID`。
5. 底部提示行随单选/多选变化。
6. ⚠ 所有模型给的自由文本走本模块的 `escape`。

**验证：** 见 T21。先 `python -m compileall rhinecode/tui`

---

### T19: 多选

**文件：** `rhinecode/tui/widgets.py`
**依赖：** T18

**步骤：**
1. `self._checked: set[int]`（选项下标）。
2. 勾选框写成**转义后的** `\[x]` / `\[ ]`。
3. `toggle_check(option_index)`：重拼该行 markup、**同时更新 `self._choices` 里
   存的那一份**、再 `replace_option_prompt_at_index`。
   ⚠ 注释必须写明：只改屏幕不改 `_choices`，用户按一下方向键勾选就全没了
   （基类 `watch_highlighted` 会拿 `_choices` 重画所有行），
   而不移动光标的手测看不出来。
4. **不另绑键**：勾选走 `OptionList` 自带的 `enter`，由覆写 `action_select` 接管。
5. 覆写 `action_select`：多选态且命中的是候选项时 → `toggle_and_advance`；
   其余（单选、「其它…」、「提交」）落回 `super()` 的结算路径。
   ⚠ 差异必须落在这里而不是 `activate_choice`——回车与数字键的唯一汇合点。
6. 提示行实时显示已选条数。

**验证：** 见 T21

---

### T20: 自由输入提示态

**文件：** `rhinecode/tui/widgets.py`
**依赖：** T18

**步骤：** `show_free_text(question)`：只留问题那一行 + 一行
`⎿ 直接在下面打字，回车提交 · Esc 退回选项`。**不加任何可选项**——
注释写明这带来一个免费的好处：数字键在这个态下天然落进输入框
（`choice_index` 返回 `None`），不必新增判断。

**验证：** 见 T21

---

### T21: 面板的护栏

**文件：** `tests/test_ask_user_panel.py`（新建）
**依赖：** T18, T19, T20

**步骤：**
1. 徽章缺省时表头**不含** `[]`；单问题时**不含**「问题 1/1」。
2. 每题末尾都有「其它…」，且它不来自入参。
3. 多选：`toggle_check` 之后 `checked_labels` 正确；
   **反证**——`toggle_check` 之后再触发一次 `watch_highlighted`，勾选**仍在**
   （这条就是 T19 那个「只改屏幕」bug 的护栏）。
4. `action_select` 在多选的候选项上不结算（只勾选并前进）、在「提交」行与单选下结算；
   勾完末项跳过「其它…」直达「提交」；单选题没有「提交」行（反证）。
5. 自由输入态下 `choice_index(1)` 返回 `None`。
6. 含 `[` 的问题文本与选项名不会产生未转义的 markup。

**验证：** `python -m unittest tests.test_ask_user_panel -v`

---

## 第五组：界面接线

### T22: `_interact` 加两个可选入参

**文件：** `rhinecode/tui/app.py`
**依赖：** T1

**步骤：** 加 `keep_panel: bool = False`（进待决盒）与 `extra: dict | None = None`
（并进记录负载）。⚠ `_arm` 内部**一字不改**——那三件事必须继续在同一个主线程
回合内完成。

**验证：** `python -m unittest tests.test_tui_keybindings -v`

---

### T23: `_clarify` 换签名

**文件：** `rhinecode/tui/app.py`
**依赖：** T2, T18, T22

**步骤：**
1. `_clarify(question, index, total) -> Optional[ClarifyReply]`。
2. 存 `self._clarify_question`（驱动设施读它）；复位 `self._clarify_free_text`。
3. `keep_panel=(index < total - 1)`；`extra` 带进度与 `multi_select`。
4. `_show_clarify_panel` 改为转调 `show_question`，仍禁用输入框。

**验证：** `python -m compileall rhinecode/tui`

---

### T24: `_resolve_interaction` 有条件收尾

**文件：** `rhinecode/tui/app.py`
**依赖：** T23

**步骤：** `keep = bool(box.get("keep_panel")) and result is not None`；
为真则跳过「隐藏面板 + 解禁输入框 + 还焦」。一律复位 `_clarify_free_text`。
⚠ 注释写明 `and result is not None` 不可省：中途跳过时面板会永远挂着，
而输入框还是禁用的（界面假死）。

**验证：** `python -m unittest tests.test_tui_keybindings -v`

---

### T25: 选择消息三分支

**文件：** `rhinecode/tui/app.py`
**依赖：** T24

**步骤：** clarify 分支按**顺序**写三条：`OTHER_ID` → 进自由输入态（不结算）；
多选态 → 结算 `kind="multi"`；单选 → 结算 `kind="option"`。
⚠ 顺序是需求：多选态下高亮停在「其它…」按回车要进自由输入，不是提交勾选。

**验证：** 见 T32

---

### T26: 自由输入态的两条按键岔路

**文件：** `rhinecode/tui/app.py`
**依赖：** T25

**步骤：**
1. `on_input_bar_input_submitted` 顶部加岔路：`_clarify_free_text` 为真时，
   非空 → `_resolve_interaction(ClarifyReply(kind="free_text", ...))`；空 → 直接 return。
   **不进 `parse_input`、不分发消息类 Hook、不进 dispatcher**（spec F16）。
2. `on_key` 加一条分支，**排在数字键与「有待决交互就 return」两条之前**：
   `_clarify_free_text` 且 `escape` → 退回选项列表态（重新 `show_question`、
   禁用输入框、聚焦面板），**不结算**。
   ⚠ 注释写明为什么必须排在最前：自由输入态下焦点在输入框上，
   面板自己的 Esc 绑定收不到。
3. 输入时的斜杠补全触发点按该状态短路。
4. `_handle_digit_choice` 改调 `panel.activate_choice(index)`。

**验证：** `python -m unittest tests.test_tui_keybindings tests.test_tui_tool_pending -v`

---

### T27: 记录摘要行

**文件：** `rhinecode/trace/reader.py`
**依赖：** T22

**步骤：** 交互事件的摘要函数补上「第几题/共几题」与答案类型；
`multi_select` 留在负载里不进摘要（判据：排查时第一眼要不要看到它）。

**验证：** `python -m unittest tests.test_trace_reader -v`

---

### T28: `/skills` 报告那句过期说明

**文件：** `rhinecode/skills/manager.py`
**依赖：** T11

**步骤：** 把「Plan Mode 的规划阶段会另外只保留只读工具并附加 ask_user /
present_plan」改成符合新判据的说法（`ask_user` 只要有人可问就在）。

**验证：** `python -m unittest tests.test_skill_manager -v`

---

## 第六组：驱动设施（必须最后改）

### T29: 协议层四种取值

**文件：** `tests/e2e/protocol.py`
**依赖：** T23

**步骤：**
1. 换掉 `_CLARIFY_INDEX`，改成认四种形态：`2` / `0,2` / `other:文本` / `skip`。
2. `KEYS_SUPPORTED_KINDS` 加入 `clarify`；把那段「为什么排除 clarify」的注释
   **改写成「为什么现在可以了」**——量过之后原理由不成立（按键步数本来就按
   过滤掉 disabled 之后的可选项序列算）。
3. `other:` + `keys` 的组合仍然拒绝，错误消息里指明改用 channel、
   或走「`keys` 选中『其它…』+ `send` 打字」。

**验证：** `python -m unittest tests.test_e2e_control -v`

---

### T30: 结算翻译

**文件：** `tests/e2e/control.py`
**依赖：** T29

**步骤：** `settlement_for` 的 clarify 分支改读 `app._clarify_question`，
按四种取值构造 `ClarifyReply` 或 `None`；越界仍抛 `ValueError`。
⚠ 注释保留「刻意读私有属性」那段理由，并把属性名从 `_clarify_options`
更新为 `_clarify_question`。

**验证：** `python -m unittest tests.test_e2e_control -v`

---

### T31: 按键路径支持 clarify

**文件：** `tests/e2e/control.py`
**依赖：** T30

**步骤：** `_answer_by_keys` 支持三种：单选序号（`target_id` 就是序号字符串）、
多选（依次移动 + `enter` 勾选，最后在「提交」行 `enter`）、`skip`（按 `escape`）。
⚠ 多选的移动是**相对**的，要记住当前位置逐步移动，不能每次都从 0 算。

**验证：** 见 T32

---

### T32: 端到端三条路径

**文件：** `tests/test_e2e_ask_user.py`（新建）
**依赖：** T31

**步骤：**
1. 单选：剧本让模型调一次 `ask_user`（一个问题），`answer 1` 应答，
   断言回灌里出现该选项名。
2. 多选走**按键路径**：`answer "0,2" --via keys`，断言两项都在回灌里。
3. 自由输入走**真人路径**：`keys down …` 选中「其它…」→ `send "我的答案"`，
   断言回灌里是那段原文、且**对话历史里没有多出一条用户消息**。
4. 三条都用 `DriverFixture.driving`（`CLAUDE.md`：新写的驱动用例一律走它）。

**验证：** `python -m unittest tests.test_e2e_ask_user -v`

---

## 第七组：登记与收尾

### T33: 「刻意不新增符号」的护栏

**文件：** `tests/test_tui_symbols.py`
**依赖：** T19

**步骤：** 加一条用例断言澄清面板的勾选框是 ASCII、白名单长度未变；
docstring 写明这是**刻意**——`☑ ☐ ✓` 都在扫描区间内，加一个就要动两处表。

**验证：** `python -m unittest tests.test_tui_symbols -v`

---

### T34: `CLAUDE.md`

**文件：** `CLAUDE.md`
**依赖：** T32

**步骤：**
1. 「安全边界」补一段：`ask_user` 不进权限管线的**新论证（两条）**，
   以及 **F6a 已知边界**（没有关掉它的手段；`pre_tool_use` 对它不触发）。
2. 「成对维护点」加两条：
   - 改动澄清提问的口径 → **三处**文本必须同口径（这是同一个坑的第六次）；
   - 驱动设施的 clarify 结算读 `app._clarify_question`，产品改属性名会一起失效。
3. 「已实现的扩展」列表加一行 ask-user，指向本目录。
4. ⚠ **能力表不加行**（这是扩展，判据见 spec 开头）。

**验证：** `grep -n "ask_user" CLAUDE.md | head`

---

### T35: 全量回归

**文件：** 无
**依赖：** 全部

**步骤：** 编译 + 全量测试。

**验证：**
```bash
python -m compileall rhinecode tests
python -m unittest discover -s tests
```
期望：全绿，总数比改动前多（新增五个测试文件），skipped 仍是 4。

---

## 执行顺序

```
T1 ─┬─→ T2 ─→ T3 ─→ T4 ─→ T5                      （地基：数据类 + 纯函数）
    │         └──→ T6 ─→ T7 ─┐
    │                T8 ─────┼─→ T10               （提示词三处同口径）
    │                T9 ─────┘
    │
    │         T6 ─→ T11 ─→ T12 ─→ T13 ─┐
    │                       └─→ T14 ───┼─→ T16      （Agent 循环）
    │                       T15 ───────┘
    │
    └─→ T17 ─→ T18 ─┬─→ T19 ─┐
                    └─→ T20 ─┴─→ T21               （面板）

T22 ─→ T23 ─→ T24 ─→ T25 ─→ T26                    （界面接线，依赖 T18/T21）
                              T27  T28

T29 ─→ T30 ─→ T31 ─→ T32                           （驱动设施，依赖 T23）

T33（依赖 T19）   T34（依赖 T32）   T35（依赖全部）
```

**能并行的两条线**：提示词（T7–T10）与面板（T17–T21）互不相干，
分别只依赖 T6 与 T1。
