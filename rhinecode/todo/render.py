"""
待办清单的显示决策与文本渲染（todo-list 扩展 T5/T6，spec F11/F13/F15/F19）。

## 这一层承担什么

**「界面该画成什么样」的全部判断，但不含任何颜色与标记语言。**

三件最容易做错的事都在这里，而它们在这一层可以**脱离 Textual 断言**：

1. **什么时候不该显示**（清单为空、或全部已完成）
2. **超过 5 条时留哪 5 条**（进行中 > 待办 > 已完成）
3. **省略摘要怎么说**（还剩几条、其中已完成几条）

界面层（`tui/widgets.py` 的 `TodoPane`）拿到 `TodoView` 之后只负责
上色与转义——它**不做任何判断**。

## ⚠ 顺序一律是「执行顺序」，本模块不重排

`TodoStore.snapshot()` 返回模型给的原始顺序，本模块**原样用它**。
初版曾按状态重排（已完成的推到最后），真机反馈后撤销——
清单要读起来像一份计划，而不是按状态分的堆。

限高时**只挑一段**（窗口锚在第一条未完成的条目上），不改变相对次序。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Sequence

from rhinecode.todo.models import TodoItem, TodoState

# 待办块最多显示几条（spec F13）。
#
# 5 这个数的依据：待办块是**占位**的（它挤压历史区的可用高度），
# 因此它的高度直接换算成「用户少看见几行对话」。5 条 + 表头 + 省略行 + 分隔线
# = 8 行，在 24 行的终端上占三分之一，是「看得清进度」与「还看得见对话」
# 之间的平衡点。
DISPLAY_LIMIT = 5



@dataclass(frozen=True)
class TodoRow:
    """待办块上的一行。"""

    title: str
    state: TodoState


@dataclass(frozen=True)
class TodoView:
    """
    待办块该画成什么样的**完整描述**。

    :param header: 表头，形如 `待办 (2/4)`
    :param rows: 要显示的行（已按优先级排过序、已限高）
    :param overflow: 省略摘要，形如 `……还有 6 条，已完成 3 条`；
        没有被省掉的条目时为空串

    ⚠ **不含颜色、不含标记语言、不含任何 Textual 的东西。**
    这正是它能被纯逻辑单测完整覆盖的原因。
    """

    header: str
    rows: tuple[TodoRow, ...]
    overflow: str


def build_view(
    items: Sequence[TodoItem], limit: int = DISPLAY_LIMIT
) -> Optional[TodoView]:
    """
    把一份清单算成「界面该画成什么样」。

    :param items: 清单快照（模型给的原始顺序）
    :param limit: 最多显示几行
    :returns: `TodoView`；**返回 `None` 表示这块整个不该显示**

    ## 什么时候返回 None（spec F11）

    - **清单为空**——从没列过待办，或刚被清空
    - **全部已完成**——活干完了，「还剩什么」这个问题不再有意义

    ⚠ 后一条是「全部完成后待办块自动收起」的**唯一**实现点。
    界面层只是照着做，不重复判断。

    ## 顺序：**一律按执行顺序，先做的在前**（真机反馈后改）

    **不排序**，原样保持模型给的次序——那正是它安排的执行顺序。

    ⚠ 初版按 `进行中 → 待办 → 已完成` 重排过，把已完成的推到最后。
    那是错的：清单读起来不再像一份计划，而像一个按状态分的堆，
    用户没法从上往下看出「这件事分几步、走到哪一步了」。
    用户原话：「任务列表应该按照先后执行顺序排列，先执行的靠前」。

    ## 取哪几条（spec F13）

    顺序既然固定了，限高就只剩「取哪一段」这一个自由度。
    **窗口锚在第一条未完成的条目上**：从它开始往后取 `limit` 条。

    ⚠ 不能简单地取前 `limit` 条：一份 15 条的清单做到第 8 条时，
    前 5 条全是已完成的——屏幕上一条**还没做的**都看不见，
    而这块界面存在的全部理由就是回答「还剩什么」。

    ⚠ 也不能让窗口越过末尾：全部做完之前的最后几条时，
    `start` 要往前收，保证窗口始终是满的（否则末尾只显示一两条，
    上面白留一片）。

    ## 省略摘要说什么

    被省掉的条数，以及**被省掉的那些里**有几条已完成。第二个数字不可省：
    没有它的话，一份「5 条待办 + 10 条已完成」的清单会显示成
    「……还有 10 条」，读起来像还有 10 件事要做。

    副作用：无。
    """
    if not items:
        return None
    if all(item.state is TodoState.COMPLETED for item in items):
        return None

    total = len(items)
    completed = sum(1 for item in items if item.state is TodoState.COMPLETED)
    header = f"待办 ({completed}/{total})"

    # **不排序**：原序就是执行顺序（见上面那段说明）。
    ordered = list(items)

    # 窗口锚在第一条未完成的条目上；全部完成时不会走到这里（上面已返回 None）。
    first_unfinished = next(
        (i for i, item in enumerate(ordered) if item.state is not TodoState.COMPLETED),
        0,
    )
    # 往前收，保证窗口是满的（末尾不足 limit 条时不要露出空档）
    start = max(0, min(first_unfinished, len(ordered) - limit))
    shown = ordered[start:start + limit]
    hidden = ordered[:start] + ordered[start + limit:]

    if hidden:
        hidden_done = sum(1 for item in hidden if item.state is TodoState.COMPLETED)
        overflow = f"……还有 {len(hidden)} 条，已完成 {hidden_done} 条"
    else:
        overflow = ""

    return TodoView(
        header=header,
        rows=tuple(TodoRow(title=item.title, state=item.state) for item in shown),
        overflow=overflow,
    )


def render_all_done_text(total: int) -> str:
    """
    「全部完成」那行记录的文本（spec F15）。

    :param total: 清单总条数
    :returns: 形如 `待办 4/4 全部完成`

    ## 为什么要有这一行

    待办块在全部完成的那一刻自动收起（`build_view` 返回 `None`）。
    不留记录的话，用户只要那一刻没盯着屏幕，就**永远不知道它列过哪几条**
    ——而收起是必然的。这与子 Agent 跑完在历史区留一行终态是同一个做法。

    ⚠ 它走历史区的**事件级**通道（正常亮度、无前缀），不是提示级：
    「一件事完成了」该看得见，而提示级那档是留给「记忆已更新」这类
    误读代价为零的消息的。

    副作用：无。
    """
    return f"待办 {total}/{total} 全部完成"


def render_todo_brief() -> str:
    """
    系统提示里的「待办清单」段（spec F19，槽位 133）。

    :returns: 一段**恒定不变**的文本（因此进稳定通道、吃前缀缓存）

    ## ⚠ 这一段不能省，理由有先例

    C15 的协作能力当初一个字都没进系统提示，真实模型验收下的结果是
    **0 次使用**——工具描述只在「模型已经想到要用这个工具」之后才起作用，
    它决定不了「要不要用」。同一个坑在 C13 委派上也踩过。

    ## ⚠ 口径取「积极使用」一侧，且这与委派**刻意相反**

    | | 委派（`run_agent`） | 待办（本工具） |
    | --- | --- | --- |
    | 成本 | 起一整条子对话 + 冷启动，**贵** | 改一份内存清单，**近乎零** |
    | 失败模式 | 白烧一轮 token，还要解释一遍 | 多了一块清单 |
    | 口径 | 默认不委派、用户开口才是主路径 | **多步任务默认就列** |

    因此本段与 `subagents/render.py` 的口径**不同是刻意的**，别顺手统一
    ——那正是 Claude Code 自己的分法（Skill 工具写 `call this tool first`，
    Agent 工具写 `do not spawn unless the user asks`）。

    ## ⚠ 下限必须**可数**

    写「少于三步的活不必列」而不是「简单的任务不必列」。
    这条经验来自委派触发口径的两次反转：**模型对有具体可匹配项的指令
    遵循得好，对抽象判断系统性偷懒**。第一次为治欠触发写了四条无下限的
    推力，结果是「一个非常简单的任务都要让子 Agent 去做」。

    ## ⚠ 成对维护点：本函数 ↔ `tools/todo_write.py` 的 `description`

    两处必须同口径（**多步才列 / 每次传完整清单 / 状态如实反映 /
    随时更新**）。它们是模型在**两个不同时刻**读到的同一条约定：
    决定要不要列时读系统提示，真正调用时读工具描述——一处强一处弱等于白改。

    这是同一个坑的**第五次**：C11「Skill 清单表头 ↔ `load_skill.description`」、
    C13「角色清单 ↔ `run_agent.description`」、C14「交付信息 ↔ 委派工具描述」、
    C15「消息标记块 ↔ `send_message.description`」——**前四次全都是真实模型
    实测才发现的**。护栏见 `tests/test_todo_tool.py::SameVoiceTest`。

    副作用：无。
    """
    return (
        "## 待办清单（开工前的第一件事）\n"
        "\n"
        "`todo_write` 维护一份用户**一直看得到**的待办清单。它回答「**还剩什么**」——在一个多步任务进行中，这是用户唯一看得到的进度来源。\n"
        "\n"
        "**只要接下来要做的事满足下面任意一条，就先调 `todo_write` 把步骤列出来，再开始动手。这不是可选项。**\n"
        "\n"
        "- 要**改 2 个以上文件**；\n"
        "- 要做的事有**三步或更多**；\n"
        "- 用户一次给了**多件事**；\n"
        "- 同一种修改要在**多处**重复应用（比如「把 X 都改成 Y」）。\n"
        "\n"
        "然后**每做完一步就再调一次**，把它标成 completed、把下一步标成 in_progress。攒到最后一次性标完成等于用户全程看不到进度，这份清单也就白列了。\n"
        "\n"
        "**该列的样子。** 用户说「把 a.py、b.py、c.py 里的裸 except 都改掉，改完跑测试」——这是四步，动手前先列出来，然后每改完一个文件就更新一次。\n"
        "\n"
        "**不该列的样子。** 用户说「加个 parse_date 函数，再给它补个测试」——确实是两件事，但只有两步，**不列**。别因为「有两件事」就往上凑。\n"
        "\n"
        "**下面两种不必列**（列了反而是噪音）：\n"
        "\n"
        "- 一两步就能做完的活：读一个文件、改一处笔误、答一个问题。\n"
        "- 纯聊天式的问答，没有要执行的动作。\n"
        "\n"
        "**判据只有一条：数得出三步就列，数不出来就别列。** 更多正反例子见 `todo_write` 的工具描述。\n"
        "\n"
        "**两条使用约定：**\n"
        "\n"
        "- **每次都传完整清单**，不是只传改动的那几条。你传什么，清单就变成什么——没带上的条目会消失，这是删除一条的正常方式。\n"
        "- 状态**如实反映实际情况**：真的正在做才写 in_progress。同时推进好几件事时，允许多条同时是 in_progress——**不要为了看起来整齐而说假话**。"
    )


def render_todo_reminder(item_count: int, all_done: bool) -> str:
    """
    每轮注入 `<system-reminder>` 的待办提醒（真机验收后加）。

    :param item_count: 当前清单条数
    :param all_done: 是否全部完成
    :returns: 一段提醒；不需要提醒时返回空串

    ## ⚠ 为什么非要有这个（静态提示词已经证明不够）

    两轮真实模型验收（`deepseek-v4-flash` 与 `deepseek-v4-pro` 各一轮）：
    一个明确五步的任务（三个文件各改两处 + 跑测试），两个模型**都是
    0 次 `todo_write`**，13 轮 / 11 轮里做了 23 / 16 次工具调用，
    正文里连「先规划」都没提过。

    排查确认**不是接线问题**：系统提示里那段在（`## 待办清单` 完整出现）、
    工具 schema 完整送到了（含全部描述文本）。把提示词从「能力描述」改写成
    带可匹配条件的**指令**（「这不是可选项」「要改 2 个以上文件」）之后
    **仍然 0 次**。

    结论：**静态提示词这条路走到头了。** 这与 C11「模型欠触发 Skill」
    是同一类系统性偏差，而 Claude Code 真正让它触发的机制不是描述，
    是**每轮注入的 `<system-reminder>`**。本函数就是那一条。

    ## 三条措辞要求

    ① **明说「别跟用户提这条提醒」**——否则模型会把它当成用户说的话，
       在正文里回一句「好的，我会维护待办清单」，那是纯噪音。
    ② **重复那条可数的下限**，不要在这里放宽——提醒的作用是「让它想起来」，
       不是「让它无条件列」。放宽会把欠触发直接推成过触发
       （委派那次就是这么翻车的）。
    ③ **清单非空时改口**：这时该提醒的是「记得更新」而不是「记得创建」，
       两者混成一句会让它在已有清单时又新建一份。

    副作用：无。
    """
    if all_done:
        # 全部完成：界面上那块已经收起，再提醒等于催它做已经做完的事。
        return ""
    if item_count == 0:
        return (
        "这是一条系统提醒，**不要在回复里向用户提起它**。\n"
        "\n"
        "你的待办清单目前是空的。如果接下来要做的事满足下面任意一条，\n"
        "**先调 `todo_write` 把步骤列出来再动手**：要改 2 个以上文件、\n"
        "要做的事有三步或更多、用户一次给了多件事、同一种修改要在多处重复应用。\n"
        "\n"
        "一两步就能做完的活，或纯聊天式的问答，**不必列**。"
        )
    return (
        "这是一条系统提醒，**不要在回复里向用户提起它**。\n"
        "\n"
        "你的待办清单上还有没做完的条目。做完一步就立刻调 `todo_write` 把它标成\n"
        "completed、把下一步标成 in_progress——**每次都传完整清单**。\n"
        "攒到最后一起标完成等于用户全程看不到进度。"
    )


__all__ = [
    "DISPLAY_LIMIT",
    "TodoRow",
    "TodoView",
    "build_view",
    "render_all_done_text",
    "render_todo_reminder",
    "render_todo_brief",
]
