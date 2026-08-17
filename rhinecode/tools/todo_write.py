"""
待办清单的写入工具（todo-list 扩展 T10，spec F3/F5/F7/F8）。

## 它是清单的唯一写入口

用户改不了（本扩展刻意不提供命令层的写路径，spec「不做的事」），
子 Agent 拿不到（`subagents/toolset.py` 的 `GLOBAL_DENIED_TOOLS`）。
于是「谁改的」这个问题只有一个答案：主 Agent 自己。

**这正是选整表覆写的前提**——单一写入方没有 lost update 的风险，
而 C15 的共享清单有 N 个并发写入方、不得不做成增量。

## 不弹确认面板，但**仍然过一次完整的权限判定**

`system_serial = True` 精确地意味着两件事，一件都不多：

① 强制串行执行，不进只读并发桶；
② 权限引擎判 **ASK 时按 ALLOW 处理**——即不弹确认面板，
   并且对第④层（权限档兜底）整层免疫。

⚠ 它**不**意味着「不进权限管线」。本工具照常过 `engine.decide`，
①②③层的 DENY 照常生效——用户写一条**不带括号**的
`deny: todo_write` 真的关得掉它。

（必须不带括号：本工具落 `other` 分支，那个分支只认空模式，
`deny: todo_write(*)` **不命中**。这是 c7 起的既有语义，非本扩展引入。）

为什么给它这条豁免：它不读写文件、不执行命令、不联网，副作用限于改
本进程内存里的一份清单，**没有可映射的 Bash / Read / Edit / Write 语义**。
为一份进度笔记每次弹面板，会让这个功能立刻变成负担。

## ⚠ 成对维护点：本文件的 `description` ↔ `todo/render.py` 的 `render_todo_brief`

两处必须同口径。模型在**两个不同时刻**读到同一条约定：决定要不要列待办时
读系统提示那段，真正调用时读这里——一处强一处弱等于白改。

这是同一个坑的**第五次**（C11 Skill 清单、C13 角色清单、C14 交付信息、
C15 消息标记块），**前四次全都是真实模型实测才发现的**。
护栏见 `tests/test_todo_tool.py::SameVoiceTest`。
"""

from __future__ import annotations

from rhinecode.todo.models import TODO_STATE_LABELS, TodoState
from rhinecode.todo.store import MAX_ITEMS, TodoStore
from rhinecode.tools.base import Tool, ToolResult


class TodoWriteTool(Tool):
    """
    用一份完整清单替换当前待办清单。

    :param store: 待办清单的存放层（由装配层注入）
    """

    name = "todo_write"

    # 它改状态，不是只读。
    read_only = False

    # 见模块 docstring：不弹面板 + 对④层免疫，但①②③与 Hook 照常生效。
    system_serial = True

    # spec F8：规划阶段可用。它不产生任何外部副作用（纯内存），
    # 而规划阶段本身可能有多步调研，正需要一份进度笔记。
    #
    # ⚠ 声明它等于承诺「规划阶段不产生副作用」，且 **`execute` 必须接受
    # `plan_stage` 关键字参数**（`tools/base.py` 的硬约定，循环会传）。
    plan_safe = True

    # ⚠ **刻意不声明** `workspace_aware`：本工具不碰任何路径，
    # 声明它只会多出一个用不上的参数。
    #
    # ⚠ **刻意不声明** `classifier_scope`：C16 的分类器只审三类动作
    # （跑命令 / 访问网络 / 给队友发消息），本工具一类都不落。
    # 硬塞进去会让每次更新待办都多一次模型调用，纯粹是成本。

    # 界面上的工具行显示成 `Todo(4 条)` 之类——但待办的主参数是个数组，
    # 没有单个值可显示，故不声明 `primary_arg`，回退到既有的键值对摘要。

    description = (
        "维护你的待办清单。用户在界面上**一直看得到**它，这是他在一个多步任务\n"
        "进行中唯一看得到的进度来源。\n"
        "\n"
        "**只要接下来要做的事满足下面任意一条，就先调本工具把步骤列出来，\n"
        "再开始动手。这不是可选项。**\n"
        "\n"
        "- 要改 2 个以上文件；\n"
        "- 要做的事有**三步或更多**；\n"
        "- 用户一次给了多件事；\n"
        "- 同一种修改要在多处重复应用。\n"
        "\n"
        "然后**每做完一步就再调一次**，把它标成 completed、下一步标成 in_progress。\n"
        "攒到最后一起标完成等于用户全程看不到进度。\n"
        "\n"
        "**什么时候不用：** 一两步就能做完的活，或纯聊天式的问答。\n"
        "\n"
        "**两条约定：**\n"
        "\n"
        "- **每次都传完整清单**，不是只传改动的那几条。你传什么，清单就变成什么\n"
        "——没带上的条目会消失，这就是删除一条的正常方式。\n"
        "- 状态要**如实反映实际情况**：真的正在做才写 in_progress。同时推进好几件事时，\n"
        "**允许多条同时是 in_progress**——不要为了看起来整齐而说假话。\n"
        "\n"
        "条目按**执行顺序**排列，先做的在前。"
        f"\n最多 {MAX_ITEMS} 条。超出会被**拒绝**（不会被截断），"
        "这时请把步骤合并得更粗一些再提交。"
    )

    parameters = {
        "type": "object",
        "properties": {
            "todos": {
                "type": "array",
                "description": (
                    "**完整**的待办清单。整份替换当前清单，不是增量追加——"
                    "没有出现在这个数组里的条目会被删除。"
                ),
                "items": {
                    "type": "object",
                    "properties": {
                        "title": {
                            "type": "string",
                            "description": "这一步要做什么，祈使句一句话。",
                        },
                        "state": {
                            "type": "string",
                            # ⚠ 这三个取值必须与 `TodoState` 逐字相等，
                            # 有护栏钉着（`test_todo_tool.py`）。不等的话
                            # 模型会写出一个工具认不出的状态，而它照着 schema 写的。
                            "enum": [state.value for state in TodoState],
                            "description": (
                                "pending=还没开始，in_progress=正在做，"
                                "completed=已做完。省略按 pending 处理。"
                            ),
                        },
                    },
                    "required": ["title"],
                },
            }
        },
        "required": ["todos"],
    }

    def __init__(self, store: TodoStore) -> None:
        self._store = store

    def execute(self, args: dict, plan_stage: bool = False) -> ToolResult:
        """
        用参数里的清单整体替换当前清单。

        :param args: 模型给的参数，取其中的 `todos`
        :param plan_stage: 当前是否处于 Plan Mode 的规划阶段。

            ⚠ **只接住，不据它分支**——本工具在两个阶段行为**完全相同**，
            因为它不产生任何外部副作用。这个参数存在只是因为 `plan_safe=True`
            的工具必须能接住循环传进来的它（不接会抛 `TypeError`）。
        :returns: `ToolResult`。失败时 `output` 是 store 给的可读中文原因，
            模型据此自我纠正。

        副作用：改清单（进而改界面上的待办块）；产生一条行为记录。
        """
        try:
            result = self._store.replace(args.get("todos"))
        except Exception as exc:  # noqa: BLE001 —— Tool 契约要求不外抛
            return ToolResult(
                ok=False,
                output=f"更新待办清单失败：{exc}",
                summary="待办未更新",
            )

        if not result.ok:
            # ⚠ `summary` 必须单独给。不给的话 TUI 会回退到取 `output` 首行，
            # 而 `output` 是一段写给模型的完整解释——整段说教会糊在工具行上
            # （tui-activity-fold 验收第 22 条踩过同一个坑）。
            return ToolResult(ok=False, output=result.reason, summary="待办未更新")

        items = self._store.snapshot()
        completed, total = self._store.counts()
        if not items:
            return ToolResult(ok=True, output="待办清单已清空。", summary="待办已清空")

        lines = [
            f"{index}. {item.title} —— {TODO_STATE_LABELS[item.state]}"
            for index, item in enumerate(items, start=1)
        ]
        return ToolResult(
            ok=True,
            # 回一份当前清单的文字版：让模型下一轮不必再猜自己刚写了什么，
            # 也让它能直接照着改（下一次覆写要传完整清单）。
            output="当前待办清单：\n" + "\n".join(lines),
            summary=f"{total} 条待办，已完成 {completed} 条",
        )


__all__ = ["TodoWriteTool"]
