# 澄清提问面板：从「Plan Mode 专属」到「随时可用」 Plan

> 对应 [`spec.md`](spec.md)。本文回答「怎么做」——组件怎么分、数据长什么样、
> 谁调谁、以及每个技术决策为什么这么选。

---

## 架构概览

改动分布在**五层**，其中只有一层是新增文件：

```
                    ┌──────────────────────────────────────────┐
   提示词层          │ plan_tools.AskUserTool.description  ①    │  三处同口径
   （模型读的）      │ texts/task_mode.TASK_MODE           ②    │  （F19/F20）
                    │ texts/plan.PLAN_FULL                ③    │
                    └──────────────────────────────────────────┘
                                     │  模型据此决定要不要调
                                     ▼
   ┌────────────────────────────────────────────────────────────┐
   │ Agent 循环层                                                │
   │  _schema_for      可见性判据：阶段 → 「有没有人可问」（F1）  │
   │  _run_special     多问题串行 + 三种「问不了人」 + 熔断       │
   │      │                                                      │
   │      └─→ agent/clarify.py  ★新增：纯函数（解析 + 文案）      │
   └────────────────────────────────────────────────────────────┘
                                     │  ClarifyFn（每题一次）
                                     ▼
   ┌────────────────────────────────────────────────────────────┐
   │ 协调层  conversation.py                                     │
   │   无人值守轮传 clarify=None（F3 第一道）                     │
   └────────────────────────────────────────────────────────────┘
                                     │
                                     ▼
   ┌────────────────────────────────────────────────────────────┐
   │ 界面层                                                      │
   │  app._clarify        一题一次 _interact（机制零改动）        │
   │  app 状态机           选项列表态 ⇄ 自由输入态                │
   │  widgets.ClarifyPanel 徽章 / 进度 / 多选 / 其它… / 提示态     │
   └────────────────────────────────────────────────────────────┘
                                     │
                                     ▼
   ┌────────────────────────────────────────────────────────────┐
   │ 驱动设施  tests/e2e/{protocol,control}.py                   │
   │   四种应答取值 + 放开 via=keys                               │
   └────────────────────────────────────────────────────────────┘
```

**新增文件只有一个**：`rhinecode/agent/clarify.py`（纯函数、零 IO、只依赖
`agent/events.py` 的数据类）。把解析与文案从 `loop.py` 里分出来的理由有两条：
`loop.py` 已经近 2000 行；而 F10 那张瑕疵表与 F12 的回灌格式都是**纯输入输出**，
放在纯函数模块里可以脱离整个 Agent 循环单测。

---

## 核心数据结构

全部放在 `rhinecode/agent/events.py`（界面层已经从那里 import `ClarifyOption`，
不新增跨层依赖）。

### `ClarifyOption`（改字段名）

```python
@dataclass
class ClarifyOption:
    label: str                 # 原 summary
    description: str = ""      # 原 detail
```

⚠ **改名是 spec F7「对齐官方」的一部分，不是洁癖。** 影响面：`loop._parse_options`、
`widgets.ClarifyPanel`、`app.on_option_list_option_selected`、
`tests/e2e/control.settlement_for`。四处都在本次改动范围内，且改错会**当场
`AttributeError`**（不是静默失效），所以不需要额外护栏。

### `ClarifyQuestion`（新增）

```python
@dataclass
class ClarifyQuestion:
    question: str                        # 问题原文（必填，空则整题跳过）
    options: tuple[ClarifyOption, ...]   # 1–4 项（已夹取，界面另加「其它…」）
    header: str = ""                     # ≤12 字符；空则不显示徽章
    multi_select: bool = False
```

⚠ `options` 用 `tuple` 而不是 `list`：它跨线程传给界面层（Agent 线程构造、
主线程读），不可变能从结构上杜绝「界面渲染到一半被改」。

### `ClarifyReply`（新增）

一题的作答结果。**`None` 表示跳过**（用户按了 Esc），与「多选一项都没勾」
是两回事（spec F11）。

```python
@dataclass
class ClarifyReply:
    kind: str                      # "option" | "multi" | "free_text"
    labels: tuple[str, ...] = ()   # 选中的选项名
    text: str = ""                 # 自由输入的原文
```

| 情形 | `kind` | `labels` | `text` |
| --- | --- | --- | --- |
| 单选选了「摘要」 | `option` | `("摘要",)` | `""` |
| 多选勾了两项 | `multi` | `("概述", "结论建议")` | `""` |
| 多选一项都没勾 | `multi` | `()` | `""` |
| 自己打了字 | `free_text` | `()` | `"放到 docs/ 下面"` |
| 跳过 | —— | 整个 reply 是 `None` | |

### `ClarifyFn`（回调签名改动）

```python
# 旧：Callable[[str, list[ClarifyOption]], Optional[str]]
# 新：
ClarifyFn = Callable[[ClarifyQuestion, int, int], Optional[ClarifyReply]]
#                     问题             第几题  共几题     None = 跳过
```

⚠ **一题一次调用，不是一次传全部**。理由见下面的技术决策 D2。
`(第几题, 共几题)` 两个参数同时喂三件事：面板上的进度指示（F13）、
界面判断「答完这题要不要收面板」（F14）、以及记录里的定位（F22）。

---

## 模块设计

### 新增：`rhinecode/agent/clarify.py`

**职责：** 把模型给的原始参数解析成问题列表，以及把作答结果渲染成回灌文本。
**纯函数、零 IO、不 import 任何 `tui` / `permission` / `tools`。**

```python
MAX_QUESTIONS = 4      # spec F7
MAX_OPTIONS   = 4      # spec F8
MAX_HEADER    = 12     # spec F9
SKIP_LIMIT    = 2      # spec F17 熔断阈值

def parse_questions(raw) -> tuple[list[ClarifyQuestion], list[str]]:
    """返回（可用问题, 瑕疵说明）。任何输入都不抛异常（spec N1/F10）。"""

def render_answers(
    pairs: list[tuple[ClarifyQuestion, Optional[ClarifyReply]]],
    notes: list[str],
) -> str:
    """spec F12 的那份清单，末尾附瑕疵说明。"""

def render_unavailable(*, interactive: bool, unattended: bool) -> str:
    """spec F4 的三条文案，按原因分。"""

NO_QUESTIONS_FEEDBACK   # 一个可用问题都没有
SKIP_CIRCUIT_FEEDBACK   # spec F17 熔断后自动跳过时回灌的
```

**`parse_questions` 的处理顺序**（顺序本身是需求，见 F10）：

```
raw 不是 list ────────────────→ ([], ["questions 必须是一个数组"])
   │
   ├─ 逐项解析，跳过：不是对象 / 问题原文为空 / 没有一个可用候选项
   │      每跳一个记一条说明
   ├─ 候选项超 4 → 取前 4，记说明
   ├─ header 超 12 字符 → 截断加省略号（不记说明，界面上看得出来）
   ├─ multi_select 非布尔 → False（不记说明）
   │
   └─ 可用问题超 4 → 取前 4，记「你提了 N 个，只呈现了前 4 个」
```

⚠ **`render_answers` 是「问题原文 → 答案」的清单，不是 JSON。** spec 明确不做
结构化回传：模型读的是文本，一份带 `- 问 → 答` 的清单比一段 JSON 更省 token
也更不容易被它误当成要照抄的格式。

### 改：`rhinecode/agent/plan_tools.py`

- `AskUserTool.parameters` 换成 `questions` 数组形态（F7）。
- `AskUserTool.description` 重写：五层意思 + 四正四负八个示例（F19/F20）。
- **`plan_schemas()` 拆成两个**：

```python
def ask_schemas() -> list[dict]:      # 只有 ask_user——「有人可问」时拼
def plan_schemas() -> list[dict]:     # 只有 present_plan——规划阶段才拼
```

⚠ 拆函数而不是给 `plan_schemas()` 加参数：两个工具的可见性判据从此**不同**
（F1），用一个函数带布尔开关表达「有时给一个、有时给两个」，
下一个人读到调用点时看不出判据分了家。

### 改：`rhinecode/agent/loop.py`

**① `_schema_for` 的判据换掉。** 新增入参 `can_ask_user: bool`：

```python
if planning:
    base = base + plan_schemas()      # present_plan：判据不变
if can_ask_user:
    base = base + ask_schemas()       # ask_user：判据换成「有没有人可问」
```

调用点传 `can_ask_user=clarify is not None`。

⚠ **F2（子 Agent 拿不到）由此结构性成立**，不靠另立禁用清单：子 Agent 那条运行链
传的澄清回调本来就是 `None`。同理 F3 第一道（无人值守轮）——协调层把回调置空，
这里自然就不发。

**② `_run_special` 的 `ask_user` 分支重写。** 伪代码：

```
questions, notes = clarify.parse_questions(args.get("questions"))

if clarify is None:                       # F3 第二道 / F4
    → ok=False, output=render_unavailable(interactive, unattended)
    → 不置 cancelled、不置 user_denied、循环继续

if not questions:                          # F10 第一行
    → ok=False, output=NO_QUESTIONS_FEEDBACK

if skips_so_far >= SKIP_LIMIT:             # F17 熔断
    → ok=True, output=SKIP_CIRCUIT_FEEDBACK，一次面板都不弹
    → 界面上说明一次

pairs = []
for i, q in enumerate(questions):
    reply = clarify(q, i, len(questions))
    pairs.append((q, reply))
    if reply is None:                      # 跳过 = 跳过剩余全部
        ctx.clarify_skipped += 1
        if planning:                       # F17：规划阶段维持现状
            ctx.cancelled = True
            → ok=False, output="用户取消了澄清。"
            return
        break                              # 非规划阶段：继续往下走

→ ok=True, output=render_answers(pairs, notes)
```

**③ 熔断计数的存放位置。** 照抄 `consecutive_unknown` 那条现成的路子：
`_RoundContext` 加 `clarify_skipped`（本轮跳过次数），`run()` 里一个局部量累加，
再作为 `_execute(..., clarify_skips=...)` 传下去。

⚠ **判据要用「已累计 + 本轮已跳过」**，不能只用传进来的那个数——模型可能在
**同一轮**里调两次 `ask_user`，只看跨轮的累计值会让熔断晚一次生效。

**④ `_run_special` 新增三个入参**：`planning`（Esc 语义分岔）、
`interactive` / `unattended`（三条文案分岔）、`clarify_skips`（熔断）。

### 改：`rhinecode/conversation.py`

一处：无人值守轮不给澄清回调（F3 第一道）。

```python
ask     = None if unattended else self._build_ask()
clarify = None if unattended else self.clarify_callback     # ← 新增这一行
```

⚠ **与紧邻的 `ask` 那行写成同一形态是刻意的**：那行上面有一段注释解释
「无人轮不弹面板」，两行并排的话，下一个改动的人不可能只看到一半。

### 改：`rhinecode/tui/widgets.py` —— `ClarifyPanel`

```python
class ClarifyPanel(NumberedPanel):
    OTHER_ID = "other"                    # 「其它…」那一项的标识

    def show_question(self, q: ClarifyQuestion, index: int, total: int) -> None
    def show_free_text(self, q: ClarifyQuestion) -> None    # F16 提示态
    def toggle_check(self, option_index: int) -> None       # F15 空格
    def checked_labels(self) -> tuple[str, ...]
    def action_toggle_check(self) -> None                   # Binding: space
    def action_cancel(self) -> None                         # Binding: escape（不变）
```

**渲染结构**（`show_question`）：

```
第 0 行  disabled  [青色][徽章]  问题原文            （问题 2/3）
第 1 行  可选      1. 选项名（推荐）        ← 多选时前面多一个 [x] / [ ]
第 2 行  disabled       选项说明（dim）
 …
第 n 行  可选      3. 其它…（自己打字）      ← 界面无条件追加（F8）
末行     disabled  提示行（dim）：随单选/多选变化
```

- 徽章为空时整个 `[…]` 不出现；进度为空（单问题）时那一段不出现。**两处都是
  「不出现」而不是「显示空的」**——`[]` 和 `（问题 1/1）` 都是噪声。
- **勾选框用转义后的 ASCII**：写进 markup 的是 `\[x]` / `\[ ]`。
  ⚠ 落单的 `[` 会在**布局阶段主线程**抛 `MarkupError` 并拆掉整个应用，
  没有任何 try/except 兜得住（`CLAUDE.md` 那条）。这里的字符串是常量、
  不经用户输入，但仍然必须写成转义形态——**给下一个人看的示范也算价值**。

**多选状态怎么存**：`self._checked: set[int]`（存的是**选项下标**，不是 OptionList 下标）。
切换时重新拼那一行的 markup 并**同时更新 `self._choices` 里存的那份**，
再 `replace_option_prompt_at_index`。

⚠ **必须同时更新 `_choices`**：基类的 `watch_highlighted` 会拿 `_choices` 里存的
markup 把每一行重画一遍。只改屏幕不改 `_choices` 的话，**用户按一下方向键，
所有勾选就全没了**——而这个 bug 只在「勾选之后再移动光标」时出现，
一次不移动光标的手测完全看不到。

**`show_free_text`（F16 提示态）**：只留两行——问题那一行 + 一行
`⎿ 直接在下面打字，回车提交 · Esc 退回选项`。**没有任何可选项**，
因此数字键在这个态下天然落进输入框（`choice_index` 返回 `None`，
`_handle_digit_choice` 不拦截），不必新增判断。

### 改：`rhinecode/tui/widgets.py` —— `NumberedPanel` 加一个可覆写点

```python
def activate_choice(self, index: int) -> None:
    """数字键命中一项时做什么。缺省 = 移过去并选中（与回车同一条结算路径）。"""
    self.highlighted = index
    self.action_select()
```

`ClarifyPanel` 覆写它：**多选态下、且命中的不是「其它…」时，改为切换勾选**（F15）。

⚠ 这不新增结算路径（`CLAUDE.md` 里 `_handle_digit_choice` 那条注释的约束仍然成立）
——切换勾选**不是结算**。真正的结算仍然只有 `action_select()` 那一条。

### 改：`rhinecode/tui/app.py`

**① `_clarify` 换签名，一题一次 `_interact`：**

```python
def _clarify(self, question, index, total) -> Optional[ClarifyReply]:
    self._clarify_question = question          # 驱动设施读它（见下）
    self._clarify_free_text = False
    return self._interact(
        "clarify",
        lambda: self._show_clarify_panel(question, index, total),
        None,                                   # 默认值 = 跳过
        display=...,
        keep_panel=(index < total - 1),         # F14：不是最后一题就别收面板
        extra={"question_index": index, "question_total": total,
               "multi_select": question.multi_select},   # F22
    )
```

**② `_interact` 加两个可选入参**：`keep_panel: bool = False` 与
`extra: dict | None = None`。前者进待决盒、后者并进记录负载。
**跨线程机制一个字不改**（spec N2）——「登记待决盒 + 改状态行 + 弹面板」
仍在同一个主线程回合内完成。

⚠ **`extra` 里新增的字段必须同步进 `trace/reader.py` 的交互事件摘要函数**
（`CLAUDE.md` 成对维护点：「trace 埋点新增字段 → 同步阅读器的摘要函数」）。
**漏改不报错**，只是那几个字段等于白记——读时间线的人看不见它们，
要 `--seq` 展开才发现「原来早就记了」。判据是「排查时第一眼要不要看到它」：
`（问题 2/3）` 与答案类型要进摘要行，`multi_select` 留在负载里即可。

**③ `_resolve_interaction` 的收尾改成有条件：**

```python
keep = bool(box.get("keep_panel")) and result is not None
if not keep:
    hide 两个面板；InputBar 解禁并还焦
self._clarify_free_text = False
```

⚠ **`and result is not None` 不可省**：用户在第 2 题（共 3 题）按 Esc 时
`keep_panel` 是真，但循环马上就要 break——不加这个条件，
**面板会永远挂在屏幕上**，而输入框还是禁用的。

**④ 选择消息的分支重写**（`on_option_list_option_selected` 的 clarify 分支）：

```
option.id == OTHER_ID   → 进自由输入态（换面板、解禁输入框、聚焦）；不结算
多选态                   → 结算为 ClarifyReply(kind="multi", labels=已勾选)
单选态                   → 结算为 ClarifyReply(kind="option", labels=(该项 label,))
```

⚠ 三条分支的**顺序**是需求：多选态下高亮停在「其它…」时按回车，
要进自由输入而不是提交勾选结果。

**⑤ 自由输入态的两条新按键路径：**

| 按键 | 在哪拦 | 做什么 |
| --- | --- | --- |
| 回车 | `on_input_bar_input_submitted` 顶部 | 非空 → 结算为 `free_text`；空 → 什么都不做（停在该态） |
| `Esc` | `on_key`，**排在数字键与「有待决交互就 return」两条之前** | 退回选项列表态；**不结算** |

⚠ `Esc` 那条**必须排在最前**：现在 `on_key` 在有待决交互时直接 `return`
（把 Esc 让给被聚焦面板的绑定），而自由输入态下焦点在输入框上、
**面板的绑定收不到**——不加这条分支，Esc 会变成一个什么都不做的键。

⚠ 自由输入态下**不做斜杠命令解析、不弹补全面板**（F16）。落点在提交那条岔路里
（直接结算，压根不进 `parse_input` / `dispatcher.dispatch`），
以及输入时的补全触发点要按该状态短路。

### 改：`tests/e2e/{protocol,control}.py`

**应答取值从「纯数字」扩成四种**（F21）：

| 取值 | 意思 | 结算值 |
| --- | --- | --- |
| `2` | 选第 2 项（0 起） | `ClarifyReply(kind="option", …)` |
| `0,2` | 多选勾第 0 与第 2 项 | `ClarifyReply(kind="multi", …)` |
| `other:放到 docs 下面` | 自由输入 | `ClarifyReply(kind="free_text", text=…)` |
| `skip` | 等价于按 Esc | `None` |

`settlement_for` 改读 `app._clarify_question`（**仍然刻意读私有属性**，
理由不变：与产品侧走同一份计算，照抄一份等价实现会在产品改算法时静默分叉）。

**放开 `clarify` + `via=keys`。** 量过之后发现当初禁它的理由**已经不成立**：
`_answer_by_keys` 算步数用的是 `extract_panel` 过滤掉 disabled 之后的
**可选项序列**，详情行本来就不参与计数，而 `ClarifyPanel` 的初始高亮
正是第一个可选项（序列下标 0）——与 `ConfirmPanel` 完全同构。

按键路径支持三种取值：单选序号、多选（在每个目标上按 `space`、最后 `enter`）、
`skip`（按 `escape`）。**`other:` 只支持 channel**，协议层给明确错误——
自由输入的键盘全链路改用「`keys` 移到「其它…」按回车 + `send` 打字」验，
那条路走的是真人提交入口，比让驱动器逐字模拟更接近真实。

---

## 模块交互

### 正常一次三问（含跳过与自由输入）

```
模型 ── ask_user{questions:[Q1,Q2,Q3]} ──→ loop._execute
                                              │ 分流到 special（无条件，既有）
                                              ▼
                                        loop._run_special
                                              │ clarify.parse_questions
                                              │
        ┌── clarify(Q1, 0, 3) ────────────────┤
        │      keep_panel=True                │
        ▼                                     │
   app._clarify → _interact ─call_from_thread→ 主线程：登记 + 状态行 + 弹面板
        │                                     │        （同一个回合内）
        │  Agent 线程 block 在 Event.wait()   │
        │                                     │
        │  ←── 用户按 2 ──── OptionSelected ──┘
        │      _resolve_interaction(reply)
        │      keep_panel 且 reply 非空 → 面板不收、焦点不动
        ▼
   返回 ClarifyReply(option, ("摘要",))
        │
        ├── clarify(Q2, 1, 3) → 用户选「其它…」→ 自由输入态 → 打字回车
        │      → ClarifyReply(free_text, text="放到 docs 下面")
        │
        └── clarify(Q3, 2, 3) → 用户按 Esc
               → None → ctx.clarify_skipped += 1
               → 非规划阶段：break，面板收起（result is None）
                                              │
                                              ▼
                              render_answers([...]) → ToolResult(ok=True)
                                              │
                                              ▼
                                   回灌历史，循环继续
```

### 自由输入态的状态机（F16）

```
                    选中「其它…」
   ┌─────────────┐ ─────────────→ ┌─────────────┐
   │ 选项列表态   │                │ 自由输入态   │
   │             │ ←───────────── │             │
   │ 面板可选     │      Esc       │ 面板=提示态  │
   │ 输入框禁用   │                │ 输入框可用   │
   └──────┬──────┘                └──────┬──────┘
          │ 回车/数字键                    │ 回车（非空）
          │ Esc（=跳过）                   │ 回车（空）→ 原地不动
          ▼                               ▼
      结算本题 ─────────────────────── 结算本题
```

**两个态下各按键的归属**（这张表就是 AC14/AC17 的判据）：

| 按键 | 选项列表态 | 自由输入态 |
| --- | --- | --- |
| ↑↓ | 面板导航 | 输入框光标 |
| 数字 | 选中 / 切换勾选 | **落进输入框**（面板无可选项，天然如此） |
| 空格 | 多选时切换勾选 | 输入一个空格 |
| 回车 | 结算本题 | 非空则结算；空则原地不动 |
| `Esc` | 跳过（本题及剩余全部） | **退回选项列表** |
| `Ctrl+C` ×2 | 退出程序（既有行为，不新增例外） | 同左 |

---

## 文件组织

```
rhinecode/
├── agent/
│   ├── clarify.py          ★新增 —— 解析 + 回灌文案（纯函数、零 IO）
│   ├── events.py           改 —— ClarifyOption 改字段；新增 ClarifyQuestion / ClarifyReply
│   ├── loop.py             改 —— _schema_for 判据 / _run_special 重写 / 熔断计数
│   ├── plan_tools.py       改 —— schema 换形状；description 重写；拆 ask_schemas
│   └── prompt/texts/
│       ├── task_mode.py    改 —— 五层意思（同口径②）
│       └── plan.py         改 —— 五层意思（同口径③）；删「一次一个问题」
├── conversation.py         改 —— 无人值守轮 clarify=None
├── skills/manager.py       改 —— /skills 报告里那句过期说明
├── trace/reader.py         改 —— 交互事件摘要行补进度与答案类型（成对维护点）
└── tui/
    ├── app.py              改 —— _clarify 换签名 / _interact 加两参 / 状态机 / 两条按键岔路
    └── widgets.py          改 —— ClarifyPanel 重写 show_*；NumberedPanel 加 activate_choice

tests/
├── e2e/protocol.py         改 —— 四种取值 + 放开 keys
├── e2e/control.py          改 —— settlement_for / _answer_by_keys
├── test_ask_user_parse.py      ★新增 —— F10 那张瑕疵表逐行
├── test_ask_user_trigger.py    ★新增 —— 三处同口径 + 八示例（F19/F20）
├── test_ask_user_loop.py       ★新增 —— 可见性判据 / 三条文案 / Esc 分岔 / 熔断
├── test_ask_user_panel.py      ★新增 —— 面板渲染 + 多选 + 自由输入态
└── test_e2e_ask_user.py        ★新增 —— 三条无头驱动路径
```

---

## 技术决策

| # | 决策点 | 选择 | 理由 |
| --- | --- | --- | --- |
| D1 | 可见性怎么控 | **`clarify is not None`** | F2（子 Agent）与 F3（无人轮）由同一个判据结构性兑现，不必各立一张禁用清单。「能不能问」= 「有没有人可问」，语义上就该是这个 |
| D2 | 多问题串行放在哪 | **循环侧**（每题一次回调） | 界面侧串行做不到：`_interact` 一次只能等一个 Event，主线程又不能阻塞。放循环侧则跨线程机制**一个字不改**（spec N2）。代价是每题一条记录、一次通知——与「每弹一次面板一次通知」的现状同口径 |
| D3 | 多题之间面板收不收 | **不收**，靠待决盒里的 `keep_panel` | 收了会经历 hide → 还焦输入框 → 再 disable → 再聚焦面板，用户看到焦点跳动。代价是 `_resolve_interaction` 多一个条件分支 |
| D4 | 自由输入在哪打字 | **主输入框**，面板保留为提示态 | 面板内嵌 `Input` 要新增子组件，撞 Textual 内部字段名的风险实测过三次（`_render` / `_closed` / `_running`，一律不报错、只表现为「东西凭空少了」）。收起面板则造出静默中间态 |
| D5 | 勾选框用什么符号 | **转义后的 ASCII `\[x]` / `\[ ]`** | 不动界面符号白名单（`☑ ☐ ✓` 都在扫描区间内）。白名单那张表越短越有用 |
| D6 | 多选的勾选状态存哪 | `set[int]` + **同步更新 `_choices` 里的 markup** | 基类 `watch_highlighted` 会拿 `_choices` 重画所有行；只改屏幕的话「勾完再按方向键，勾选全没」，而不移动光标的手测看不出来 |
| D7 | 数字键在多选下的行为 | **切换勾选**，经 `activate_choice` 覆写点 | 直接在 `_handle_digit_choice` 里 `isinstance` 判面板类型会把面板知识漏进 App；覆写点让每个面板自己回答「数字键命中我时该干嘛」。且切换不是结算，不违反「不新增结算路径」 |
| D8 | 熔断计数存哪 | `_RoundContext` 计本轮 + `run()` 局部量累加 | 与 `consecutive_unknown` 完全同构，不新增状态容器。⚠ 判据取「累计 + 本轮」，否则同一轮里两次 `ask_user` 会让熔断晚一次生效 |
| D9 | 「问不了人」回灌几条文案 | **三条**（子 Agent / 无人轮 / 兜底） | 三种情形下模型该做的事不同。一句通用的「不支持澄清」会让子 Agent 以为是故障而重试 |
| D10 | 回灌用什么格式 | **人读的清单**，不是 JSON | 省 token；且 JSON 容易被模型误当成「答案要照这个格式回」 |
| D11 | 解析瑕疵怎么处理 | **能救就救 + 在回灌里说破** | 与 todo-list 那条「拒绝线不是截断线」方向相反，判据是「被截掉的事实模型下一轮能不能自己发现」——这里能（答案按问题原文列出，少了哪个看得见），那里不能 |
| D12 | `ClarifyOption` 改不改字段名 | **改**（`label` / `description`） | F7 对齐官方的一部分。四处调用点改错会当场 `AttributeError`，不是静默失效，不需要额外护栏 |
| D13 | 驱动设施要不要放开 `keys` | **放开** | 量过之后发现原禁令的理由不成立：按键步数本来就按「过滤掉 disabled 的可选项序列」算。多选的核心交互是按空格，不放开就没法验 |
| D14 | 自由输入的键盘全链路怎么验 | `keys` 移动 + `send` 打字 | `send` 走真人提交入口，正好压到 F18 那条新岔路上；让驱动器逐字模拟反而绕开了要验的东西 |

---

## 三处同口径文本的护栏形态

照抄 `tests/test_todo_tool.py::SameVoiceTest` 那一套（它是本项目第五次踩同一个坑
之后沉淀下来的形态），但**这次是三处不是两处**：

```python
_LAYERS = ("拍板", "一两次", "默认做法", "已经说过", "2 到 4 个")

def test_every_layer_appears_in_all_three(self):
    for layer in self._LAYERS:
        for name, text in self._TEXTS.items():   # 工具描述 / 任务模式 / 计划模式
            self.assertIn(layer, text, f"{name} 缺了这一层")

def test_the_guard_covers_five_layers(self):
    """护栏自身的护栏：缩表会让上面那条静默变弱——前五次共同的失效方式。"""
    self.assertEqual(len(self._LAYERS), 5)

def test_the_three_texts_are_not_the_same_string(self):
    """同口径 ≠ 同一份文本。受众时刻不同，篇幅语气该不一样。"""

def test_plan_text_no_longer_says_one_question_at_a_time(self):
    """反证：F7 落地后「一次一个问题」是错的，它留在那儿会与工具描述打架。"""
```

**八示例的护栏**（照抄 `ManyShotExamplesTest`）：正例 4 个、负例 4 个、
每个都带判据、负例里有一个**贴着下限的临界情形**，且描述里不含无下限的推力词。

⚠ 五个锚点词的选法有讲究：它们要能**自然地**出现在三处文本里。
「一两次」是可数下限那一层的锚（对应 spec F19 第 2 层），
「2 到 4 个」是「怎么用」那一层的锚——两个都带数字，正是已知项 #17
留下的那条线索（模型对有具体可匹配项的指令遵循得好）。

---

## 与既有不变量的关系（逐条确认没破）

| 不变量 | 本次是否触碰 | 说明 |
| --- | --- | --- |
| markup 转义必须用 `tui/widgets.py` 的 `escape` | **触碰** | 徽章、问题、选项名、说明四处新的自由文本入口，全部走它；勾选框常量写成转义形态 |
| 活动区数据一律主线程轮询、不新增跨线程推送 | 不触碰 | 自由输入态只是主线程内的状态切换 |
| 新增 TUI 组件字段名先在 `Static` 实例上 `hasattr` 查 | **触碰** | 新增 `_checked` / `_clarify_free_text` / `_clarify_question` 三个字段，开工第一步就查 |
| `_settle_session` 是会话结算的唯一入口 | 不触碰 | —— |
| Hook 前置层的分发点位置「两头都不能挪」 | 不触碰 | F6a 明确不接进去 |
| 五层权限管线的层序 | 不触碰 | `ask_user` 不进管线（F6） |
| `_interact` 的「登记 + 状态行 + 弹面板」同回合完成 | 不触碰 | 只加两个可选入参，`_arm` 内部一字不改 |
| 界面符号白名单 | 不触碰 | D5，且要加护栏钉住这个「刻意」 |
| Hook 通知种类取值集合 | 不触碰 | 仍是「等待澄清」那一种 |

---

## 实现顺序的约束（给 task.md 的输入）

三条硬依赖，顺序不可颠倒：

1. **数据类先改**（`events.py`）——四个模块都依赖它，先改能让后面每一步的
   `AttributeError` 立刻暴露在正确的位置。
2. **纯函数模块（`clarify.py`）先于循环改造**——循环那步要拿它的解析结果做分支，
   而它可以脱离整个 Agent 单测，先绿了后面才好排查。
3. **驱动设施最后改**——它读产品侧的私有属性，产品没定型之前改它必然返工。

两条可以并行：**提示词三处文本**与**面板渲染**互不相干。
