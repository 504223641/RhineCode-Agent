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
    随时更新 / 列清单的时点 / 同一时刻只有一条 in_progress /
    不许从 pending 跳到 completed**）。它们是模型在**两个不同时刻**
    读到的同一条约定：决定要不要列时读系统提示，真正调用时读工具描述
    ——一处强一处弱等于白改。

    ## ⚠ 后两层是 2026-08-18 加的，它们互相咬合，别单独删掉任一条

    症状是「中途一直不更新，最后一次性全标完成」。根因是原文写着
    「**允许多条同时是 in_progress**」——那句话本意是「别说假话」，
    实际被模型当成了免责条款：开工时把三条全标 in_progress 之后，
    清单**从此永远合法**，再没有任何东西迫使它开口。

    「同一时刻只有一条」是台发动机：每做完一件事，清单就变得不合规
    （零条 in_progress），而修好它的唯一办法就是再调一次工具。
    「不许跳步」是它的锁：少了这条，模型仍可把剩下的 pending 一次性
    直接标成 completed，绕开发动机。**两条缺一不可。**

    ⚠ 代价是牺牲了一点表达力（真有多路并行时只能说得糙一点）。
    换来的是那台发动机。要退很容易——三处各改一句话。
    「并行怎么标」的两种情形写在正文里，别删：它们是「只能一条」
    唯一的出口，删掉之后模型遇到并行会自己发明标法。

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
        "**时点：在你本次任务第一次调用 `edit_file` / `write_file` / `run_command` 之前。** 已经改了两个文件才想起来列，那份清单对用户就没用了——他要的是「还剩什么」，不是「我干过什么」。前面的只读调研（读文件、搜一下有几处）**在列清单之前发生很正常**，那正是你列得出清单的前提。⚠ 这是**时点**，不是新增的触发条件：不满足上面四条的活，照样不列。\n"
        "\n"
        "然后**每做完一步就再调一次**，把它标成 completed、把下一步标成 in_progress。攒到最后一次性标完成等于用户全程看不到进度，这份清单也就白列了。\n"
        "\n"
        "**更新也有时点，而且是同一个：每次你要调 `edit_file` / `write_file` / `run_command` 之前，先回头看清单上那条 in_progress——它做完了吗？做完了就先更新清单再动手。** 「做完一步」要靠你自己判断，容易一路做下去忘了汇报；「我马上要动手了」是个明摆着的事实，而它在一个任务里会反复出现十几次。\n"
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
        "**四条使用约定：**\n"
        "\n"
        "- **每次都传完整清单**，不是只传改动的那几条。你传什么，清单就变成什么——没带上的条目会消失，这是删除一条的正常方式。\n"
        "- **同一时刻只能有一条 in_progress。** 做完它就标 completed，并在同一次调用里把下一条标成 in_progress。`in_progress` 问的不是「有几件事在推进」，是「**你此刻的注意力在哪一条**」——你一次调一个工具、看一个结果，注意力永远只有一处。\n"
        "- **不许从 pending 直接跳到 completed。** 一条要标成 completed，它上一次必须已经是 in_progress。跳过去等于这条从没被汇报过。\n"
        "- 状态**如实反映实际情况**：真的正在做那条才写 in_progress，**不要为了看起来整齐而说假话**。两种「看起来好几件事同时在做」的情形，如实的标法仍然是一条：派了多个子 Agent 同时跑，清单上是**一条**（它们的进度用户已在活动区看到，拆开会出现两块对不上的数字）；某条卡住了要先做后面的，**重排清单**（把它标回 pending 挪到后面），不要靠多标一条 in_progress 来表达。"
    )


#: 提醒里念出条目标题时的长度上限。
#:
#: ⚠ **标题本身没有长度校验**（`store.py` 只限条数不限标题长），因此这里必须
#: 自己截。不截的话，一条被模型写成三百字的「标题」会每轮重发一次。
REMINDER_TITLE_LIMIT = 60


def _clip_title(title: str) -> str:
    """
    把条目标题截到 `REMINDER_TITLE_LIMIT`，超长时以省略号收尾。

    :param title: 原始标题
    :returns: 可直接嵌进提醒文本的标题

    副作用：无。
    """
    text = title.strip()
    if len(text) <= REMINDER_TITLE_LIMIT:
        return text
    return text[: REMINDER_TITLE_LIMIT - 1] + "…"


def render_todo_reminder(
    item_count: int,
    all_done: bool,
    in_progress: Optional[tuple[int, str]] = None,
) -> str:
    """
    每轮注入 `<system-reminder>` 的待办提醒（真机验收后加）。

    :param item_count: 当前清单条数
    :param all_done: 是否全部完成
    :param in_progress: 当前标着 in_progress 的那条，形如 `(序号从 1 起, 标题)`；
        **一条都没有时传 `None`**——那是个有意义的状态，不是缺省值，见下
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

    ## 四条措辞要求

    ① **明说「别跟用户提这条提醒」**——否则模型会把它当成用户说的话，
       在正文里回一句「好的，我会维护待办清单」，那是纯噪音。
    ② **重复那条可数的下限**，不要在这里放宽——提醒的作用是「让它想起来」，
       不是「让它无条件列」。放宽会把欠触发直接推成过触发
       （委派那次就是这么翻车的）。
    ③ **清单非空时改口**：这时该提醒的是「记得更新」而不是「记得创建」，
       两者混成一句会让它在已有清单时又新建一份。
    ④ **把当前那条 in_progress 的标题念出来**（2026-08-18 加）。原文只说
       「做完一步就立刻更新」——那是一句**泛泛的催促**，模型每轮都读到，
       读多了等于没读。念出标题之后它变成一个**指名道姓的问句**
       （「『改 b.py 里的裸 except』做完了吗」），而那种问题很难当没看见。

    ## ⚠ 「一条 in_progress 都没有」是最该提醒的时刻，不是缺省状态

    这是「同一时刻只能有一条 in_progress」那条规矩的**点火装置**：
    模型做完一件事、还没更新清单时，清单上恰好零条 in_progress——
    此时提醒直接指出这个事实并要它补上，那台发动机才真正转起来。

    把这个分支与「清单为空」合并会毁掉它：前者是「你漏了一次汇报」，
    后者是「你还没列清单」，两句话要模型做的事完全不同。

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
    if in_progress is None:
        # 点火分支：清单上还有没做完的，却一条 in_progress 都没有。
        # 最常见的成因就是「上一条刚做完、还没汇报」——正是要抓的那一刻。
        return (
            "这是一条系统提醒，**不要在回复里向用户提起它**。\n"
            "\n"
            "你的待办清单上还有没做完的条目，但**当前一条 in_progress 都没有**。\n"
            "如果你刚做完一条却还没更新，现在就调 `todo_write` 把它标成 completed、\n"
            "把接下来要做的那条标成 in_progress——**每次都传完整清单**。\n"
            "\n"
            "如果你是停下来了，请向用户说明原因，不要让清单一直空着 in_progress。"
        )
    position, title = in_progress
    return (
        "这是一条系统提醒，**不要在回复里向用户提起它**。\n"
        "\n"
        f"你的待办清单上标着 in_progress 的是第 {position} 条：**{_clip_title(title)}**。\n"
        "\n"
        "**它做完了吗？** 做完了就立刻调 `todo_write` 把它标成 completed、\n"
        "把下一条标成 in_progress——**每次都传完整清单**。\n"
        "还没做完就接着做，不必调本工具。\n"
        "\n"
        "特别是：**在你下一次调 `edit_file` / `write_file` / `run_command` 之前**\n"
        "先回答这个问题。攒到最后一起标完成等于用户全程看不到进度。"
    )


__all__ = [
    "DISPLAY_LIMIT",
    "REMINDER_TITLE_LIMIT",
    "TodoRow",
    "TodoView",
    "build_view",
    "render_all_done_text",
    "render_todo_reminder",
    "render_todo_brief",
]
