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

## ⚠ 排序只影响显示，不影响数据

`TodoStore.snapshot()` 永远返回模型给的原始顺序（那是它表达的执行次序，
重排会让清单读起来不像一份计划）。本模块的优先级排序**只发生在这里**，
每次现算，不写回。
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

# 显示优先级：**用户要看的是「还剩什么」**，所以已完成的让位。
#
# ⚠ 数值越小越靠前。这张表同时是「已完成不占名额」的实现——它排在最后，
# 于是只要还有进行中或待办，前 5 个名额就轮不到它。
_DISPLAY_PRIORITY: dict[TodoState, int] = {
    TodoState.IN_PROGRESS: 0,
    TodoState.PENDING: 1,
    TodoState.COMPLETED: 2,
}


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

    ## 取哪几条（spec F13）

    按 `进行中 → 待办 → 已完成` 排序，**同一档内保持模型给的原序**，
    然后取前 `limit` 条。

    ⚠ 「同档保持原序」靠的是 **Python `sorted` 的稳定性**——它是语言保证，
    不是巧合。改成别的排序方式（比如自己分桶再拼）时必须自己保证这一点，
    否则同为「待办」的几条会莫名其妙地换位置，用户看到的是清单在自己抖。

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

    # sorted 是稳定排序 → 同优先级内保持原序（见上面那段说明）
    ordered = sorted(items, key=lambda item: _DISPLAY_PRIORITY[item.state])
    shown = ordered[:limit]
    hidden = ordered[limit:]

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
        "## 待办清单\n"
        "\n"
        "你有一份待办清单（`todo_write`），用户能在界面上一直看到它。"
        "它回答的是「**还剩什么**」——这是用户在一个多步任务进行中"
        "唯一看得到的进度来源。\n"
        "\n"
        "**要用它的时候：**\n"
        "\n"
        "- 一件事需要**三步或更多**才能做完 —— 动手之前先把这几步列出来。\n"
        "- 用户一次给了**多件事** —— 每件一条。\n"
        "- 干到一半发现还要多做几步 —— 把新的步骤加进去，别留在心里。\n"
        "- **每完成一步就更新一次**，不要攒到最后一起标完成。"
        "攒着等于用户全程看不到进度，这份清单也就失去了意义。\n"
        "\n"
        "**不必用它的时候：**\n"
        "\n"
        "- 一两步就能做完的活（读一个文件、答一个问题、改一处笔误）。\n"
        "- 纯聊天式的问答，没有要执行的动作。\n"
        "\n"
        "**怎么用：**\n"
        "\n"
        "- **每次都传完整清单**，不是只传改动的那几条。"
        "你传什么，清单就变成什么——没带上的条目会消失，这是删除一条的正常方式。\n"
        "- 状态**如实反映实际情况**：真的正在做才标 `in_progress`。"
        "如果你同时在推进好几件事，那就允许多条同时是 `in_progress`——"
        "**不要为了「看起来整齐」而说假话**。\n"
        "- 标题写成祈使句，一句话说清做什么（「改 login 接口」而不是「登录相关」）。"
    )


__all__ = [
    "DISPLAY_LIMIT",
    "TodoRow",
    "TodoView",
    "build_view",
    "render_all_done_text",
    "render_todo_brief",
]
