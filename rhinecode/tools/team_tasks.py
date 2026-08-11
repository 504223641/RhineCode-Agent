"""
共享任务清单的四个工具（c15 T21/T22，spec F6）。

## 为什么是四个独立工具，不是一个带 `action` 参数的工具

C13 把委派做成一个工具带 `type` 参数，理由是「不论加载了多少角色，
模型看到的工具数量始终不变」。**那条理由在这里不适用**——任务工具的数量
本来就固定，不随清单长度变化。

分开则每个工具的参数 schema 更精确（`task_list` 压根没有参数，
`task_get` 只有一个），模型少犯参数错误。这也与 Claude Code 的
`TaskCreate` / `TaskList` / `TaskGet` / `TaskUpdate` 四件套一致。

## 都不弹确认面板（写入的两个是 `system_serial=True`）

与 `run_agent`、`load_skill` 同先例。论证：这四个工具**不读写文件、
不执行命令**，副作用限于「在本进程内存里改一份清单」，没有可映射的
Bash / Read / Edit / Write 语义。

`system_serial=True` 的含义是「**判 ASK 时按 ALLOW 处理**」——不是「不进管线」。
它们**照常过一次 `engine.decide`**，因此 `deny: task_create` / `deny: task_update`
（不带括号的整工具规则）**确实拦得住**；只是③层未命中时直接放行而不弹面板。

⚠ 这里一度写着「`deny` 规则对它们无效」，那是 C15 验收期实测确认的**真实缺陷**
（预扫直接给 ALLOW、根本不调引擎），已于 perm-system-serial-bypass 修掉。
Hook 的 `pre_tool_use` 依然是另一条独立且更早的收窄手段。

## `plan_safe` 的兑现

`task_create` 与 `task_update` 声明了 `plan_safe=True`（`read_only` 的
另两个本来就在规划阶段可用，不需要声明）。

声明它等于承诺「规划阶段不产生副作用」。这两个工具的兑现方式是
**无条件放行**：拆任务本就是规划的一部分，而改的是内存里的清单，
不碰文件、不起进程。它们仍**必须接受** `plan_stage` 关键字参数——
那是 `Tool.plan_safe` 的契约，循环一定会传。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from rhinecode.team.identity import current_identity
from rhinecode.team.models import TASK_STATE_LABELS, TaskState
from rhinecode.tools.base import Tool, ToolResult
from rhinecode.trace import TraceEventType

if TYPE_CHECKING:  # pragma: no cover —— 仅类型检查期
    from rhinecode.team.service import TeamService


# 模型给的状态字符串 → 枚举。
#
# `deleted` **不是**一种状态而是一次删除（对齐 Claude Code：
# 「Setting status to `deleted` permanently removes the task」），
# 因此它不在这张表里，由 `task_update` 单独分流。
_STATE_BY_NAME = {state.value: state for state in TaskState}

# 共用的一段说明，四个工具的描述都会引用它，保证口径一致。
_BOARD_NOTE = (
    "共享任务清单是你和队友**共用同一份**的看板：你建的任务队友看得到，"
    "队友建的你也看得到。"
)


class _BoardTool(Tool):
    """
    四个任务工具的共同基类：持有服务门面，并提供统一的异常兜底。

    :param service: 协作服务门面

    `Tool` 契约要求 `execute` **不得向上抛异常**，否则 Agent Loop 会把它
    变成一条「工具执行异常」，丢掉这里已经组织好的可读原因——而可读原因
    正是模型自我纠正的唯一依据。因此每个子类的 `execute` 都包一层
    `_guard`。
    """

    def __init__(self, service: "TeamService") -> None:
        self._service = service

    @property
    def board(self):
        return self._service.board

    def _trace(self, action: str, task_id: str, detail: str = "") -> None:
        """
        记一次清单变更（c15 T42）。

        **带上 actor**：清单是所有人共用的一份，一条任务莫名其妙变了状态时，
        没有这个字段就无从追是谁改的。
        """
        self._service._emit(  # noqa: SLF001 —— 埋点漏斗，刻意不做成公开 API
            TraceEventType.TEAM_TASK,
            action=action,
            task_id=task_id,
            actor=current_identity(),
            detail=detail,
        )

    @staticmethod
    def _guard(fn):
        """跑一段逻辑并把任何异常转成 `ok=False`。"""
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001 —— 契约要求不外抛
            return ToolResult(ok=False, output=f"任务清单操作失败：{exc}", summary="清单操作失败")


class TaskCreateTool(_BoardTool):
    """在共享清单上新建一条任务。"""

    name = "task_create"
    read_only = False
    # 显示标题而不是 description：标题就是那一句「要做什么」
    primary_arg = "subject"
    system_serial = True
    plan_safe = True

    description = (
        "在共享任务清单上新建一条任务。\n"
        "\n"
        f"{_BOARD_NOTE}\n"
        "\n"
        "**把一个多步骤的目标拆成任务写进清单，然后派队员去做**——"
        "队员会自己认领没被挡住的任务、做完标完成、解锁后续任务，"
        "整个过程不需要你逐条派发。\n"
        "\n"
        "任务之间有先后顺序时，建完之后用 `task_update` 的 `add_blocked_by` "
        "声明依赖。被挡住的任务**谁都认领不了**，直到挡它的那些全部完成。\n"
        "\n"
        "标题写成祈使句（「修复登录接口的空指针」），说明里写清楚"
        "**做到什么程度算完**——认领它的队员看不到你和用户的对话。"
    )

    parameters = {
        "type": "object",
        "properties": {
            "subject": {
                "type": "string",
                "description": "任务标题，祈使句，一句话说清要做什么。",
            },
            "description": {
                "type": "string",
                "description": (
                    "任务说明：背景、要做什么、做到什么程度算完。"
                    "认领它的队员看不到你和用户的对话，所以这里要自包含。"
                ),
            },
        },
        "required": ["subject", "description"],
    }

    def execute(self, args: dict, plan_stage: bool = False) -> ToolResult:
        """
        新建一条任务。

        :param args: `subject` 与 `description`
        :param plan_stage: 是否处于规划阶段。**无条件放行**——拆任务本就是
            规划的一部分，且改的是内存里的清单，不碰文件、不起进程
        :returns: 含新任务编号的结果

        副作用：改共享清单。
        """
        def run() -> ToolResult:
            subject = str(args.get("subject") or "").strip()
            if not subject:
                return ToolResult(
                    ok=False, output="subject 不能为空。", summary="缺少标题"
                )
            task = self.board.create(subject, str(args.get("description") or ""))
            self._trace("create", task.task_id, subject[:40])
            return ToolResult(
                ok=True,
                output=(
                    f"已新建任务 {task.task_id}：{task.subject}\n"
                    f"当前状态：待办、无人认领。\n"
                    f"有先后顺序的话，用 task_update 的 add_blocked_by 声明依赖。"
                ),
                summary=f"新建任务 [{task.task_id}] {subject[:20]}",
            )

        return self._guard(run)


class TaskListTool(_BoardTool):
    """列出共享清单。只读。"""

    name = "task_list"
    read_only = True
    # 无主参数：它就是「列一下」，没有哪个参数值得进标题
    primary_arg = ""

    description = (
        "列出共享任务清单：每条任务的编号、状态、标题、认领人、以及"
        "**它现在被哪些未完成的任务挡着**。\n"
        "\n"
        f"{_BOARD_NOTE}\n"
        "\n"
        "**做完手上的任务之后先看一眼这里**，认领下一条没被挡住的——"
        "优先做编号小的（早建的任务往往为后建的铺垫上下文）。\n"
        "\n"
        "认领请用 `task_update` 把 `owner` 填成你自己的名字。"
    )

    parameters = {"type": "object", "properties": {}}

    def execute(self, args: dict) -> ToolResult:
        """
        列出清单。

        :param args: 无参数
        :returns: 清单的文本形态

        副作用：无。
        """
        def run() -> ToolResult:
            text = self._service.board_text()
            count = self.board.count()
            return ToolResult(
                ok=True, output=text, summary=f"共享清单 · {count} 条任务"
            )

        return self._guard(run)


class TaskGetTool(_BoardTool):
    """查看单条任务的完整信息。只读。"""

    name = "task_get"
    read_only = True
    primary_arg = "task_id"

    description = (
        "查看共享清单上某一条任务的完整信息："
        "标题、说明、状态、认领人、两个方向的依赖关系。\n"
        "\n"
        "认领一条任务之前先看它的说明——`task_list` 只给标题。"
    )

    parameters = {
        "type": "object",
        "properties": {
            "task_id": {"type": "string", "description": "任务编号，如 \"3\"。"},
        },
        "required": ["task_id"],
    }

    def execute(self, args: dict) -> ToolResult:
        """
        查看单条任务。

        :param args: `task_id`
        :returns: 该任务的完整信息；不存在时失败并列出现有编号

        副作用：无。
        """
        def run() -> ToolResult:
            task_id = str(args.get("task_id") or "").strip()
            task = self.board.get(task_id)
            if task is None:
                existing = "、".join(t.task_id for t in self.board.snapshot())
                tail = f"当前清单上的编号：{existing}。" if existing else "当前清单是空的。"
                return ToolResult(
                    ok=False,
                    output=f"没有编号为 {task_id!r} 的任务。{tail}",
                    summary="任务不存在",
                )

            blockers = self.board.is_blocked(task_id)
            lines = [
                f"任务 {task.task_id}：{task.subject}",
                f"状态：{TASK_STATE_LABELS.get(task.state, task.state.value)}",
                f"认领人：{task.owner or '（无人认领）'}",
            ]
            if task.description:
                lines.append(f"说明：{task.description}")
            if blockers:
                lines.append(f"现在被这些未完成的任务挡着：{'、'.join(blockers)}")
            elif task.blocked_by:
                lines.append(f"依赖：{'、'.join(task.blocked_by)}（已全部完成）")
            else:
                lines.append("没有前置依赖。")
            if task.blocks:
                lines.append(f"它挡着：{'、'.join(task.blocks)}")
            return ToolResult(
                ok=True, output="\n".join(lines), summary=f"查看任务 [{task.task_id}]"
            )

        return self._guard(run)


class TaskUpdateTool(_BoardTool):
    """更新一条任务：改状态、认领、改说明、加依赖、删除。"""

    name = "task_update"
    read_only = False
    # 显示编号而不是改了什么：一次调用可能同时改状态、认领人、依赖
    primary_arg = "task_id"
    system_serial = True
    plan_safe = True

    description = (
        "更新共享清单上的一条任务：改状态、**认领**、改标题说明、"
        "加依赖、删除。\n"
        "\n"
        "**认领**：把 `owner` 填成你自己的名字。认领是原子的——"
        "同一条任务只会有一个人认到，抢输了会明确告诉你是谁认走了。"
        "被未完成的前置任务挡着时认领会被拒绝，并告诉你在等哪几条。\n"
        "\n"
        "**做完一条就立刻把它标成 `completed`**：清单是队友判断"
        "「下一步能做什么」的唯一依据，你不标，被它挡着的任务就一直没人能动。\n"
        "\n"
        "**没真做完就别标完成**——测试还红着、实现只写了一半、"
        "遇到解决不了的问题，都应该保持 `in_progress` 并把情况"
        "发消息告诉相关的人。\n"
        "\n"
        "`add_blocked_by` 声明「这条任务被哪些任务挡着」。"
        "会自动拒绝形成循环的依赖。\n"
        "\n"
        "把 `status` 设成 `deleted` 会**删除**这条任务（不是一种状态）。"
    )

    parameters = {
        "type": "object",
        "properties": {
            "task_id": {"type": "string", "description": "要更新的任务编号。"},
            "status": {
                "type": "string",
                "enum": ["pending", "in_progress", "completed", "deleted"],
                "description": (
                    "新状态。`deleted` 表示删除这条任务（不是一种状态）。"
                ),
            },
            "owner": {
                "type": "string",
                "description": (
                    "认领人。填你自己的名字即认领这条任务，"
                    "**不确定自己叫什么就填 `me`**；"
                    "填空串表示放手，让别人可以认领。"
                ),
            },
            "subject": {"type": "string", "description": "新标题。"},
            "description": {"type": "string", "description": "新说明。"},
            "add_blocked_by": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "声明这条任务被哪些任务挡着（填它们的编号）。"
                    "被挡住的任务在前置全部完成前谁都认领不了。"
                ),
            },
        },
        "required": ["task_id"],
    }

    def execute(self, args: dict, plan_stage: bool = False) -> ToolResult:
        """
        更新一条任务。

        :param args: 见 `parameters`
        :param plan_stage: 是否处于规划阶段。**无条件放行**，理由同 `task_create`
        :returns: 结果；认领冲突 / 被挡住 / 成环各有专门的可读原因

        执行顺序有讲究：**先加依赖、再认领、最后改状态**。
        反过来的话，一次「声明依赖 + 顺手认领」的调用会先认领成功、
        再加上一条挡住自己的依赖，得到一个「已认领但被挡着」的怪状态。

        副作用：改共享清单。
        """
        def run() -> ToolResult:
            task_id = str(args.get("task_id") or "").strip()
            if not task_id:
                return ToolResult(ok=False, output="task_id 不能为空。", summary="缺少编号")

            status = str(args.get("status") or "").strip().lower()

            # 删除单独分流：它不是一种状态（对齐 Claude Code）。
            if status == "deleted":
                if self.board.remove(task_id):
                    self._trace("delete", task_id)
                    return ToolResult(
                        ok=True,
                        output=f"已删除任务 {task_id}，相关的依赖引用也一并摘除了。",
                        summary=f"删除任务 [{task_id}]",
                    )
                return ToolResult(
                    ok=False, output=f"没有编号为 {task_id!r} 的任务。", summary="任务不存在"
                )

            notes: list[str] = []

            # ① 依赖：必须在认领之前
            for blocker in args.get("add_blocked_by") or []:
                dep = self.board.add_dependency(task_id, str(blocker).strip())
                if not dep.ok:
                    return ToolResult(ok=False, output=dep.reason, summary="依赖声明失败")
                notes.append(f"已声明：被 {blocker} 挡着")

            # ② 认领：走原子路径，不走 update(owner=...)
            owner = args.get("owner")
            if owner is not None and str(owner).strip():
                claim = self.board.claim(task_id, self._resolve_owner(str(owner)))
                if not claim.ok:
                    return ToolResult(ok=False, output=claim.reason, summary="认领失败")
                notes.append(f"已认领（{claim.current_owner}）")
                self._trace("claim", task_id, claim.current_owner)
            elif owner is not None:
                # 显式传空串 = 放手
                released = self.board.update(task_id, owner="")
                if not released.ok:
                    return ToolResult(ok=False, output=released.reason, summary="更新失败")
                notes.append("已放手，别人可以认领了")

            # ③ 其余字段
            state = _STATE_BY_NAME.get(status) if status else None
            if status and state is None:
                return ToolResult(
                    ok=False,
                    output=(
                        f"status 只能是 {'、'.join(_STATE_BY_NAME)} 或 deleted，"
                        f"收到的是 {status!r}。"
                    ),
                    summary="状态非法",
                )

            result = self.board.update(
                task_id,
                state=state,
                subject=args.get("subject"),
                description=args.get("description"),
            )
            if not result.ok:
                return ToolResult(ok=False, output=result.reason, summary="更新失败")
            if state is not None:
                notes.append(f"状态 → {TASK_STATE_LABELS.get(state, state.value)}")
                self._trace("status", task_id, state.value)

            if not notes:
                notes.append("没有任何字段发生变化")

            unblocked = self._newly_claimable(task_id, state)
            tail = f"\n现在可以认领：{'、'.join(unblocked)}。" if unblocked else ""
            return ToolResult(
                ok=True,
                output=f"任务 {task_id}：{'；'.join(notes)}。{tail}",
                summary=f"更新任务 [{task_id}] · {notes[0]}",
            )

        return self._guard(run)

    @staticmethod
    def _resolve_owner(owner: str) -> str:
        """
        把 `owner` 解析成真正的认领人名字。

        :param owner: 模型给的值
        :returns: 认领人名字

        `me` / `self` / `我` 一律解析成**当前调用者自己**。

        这是一条**容错**：认领的常态就是「我来做这件事」，而一个子 Agent
        未必总记得住自己叫什么（名字在它的系统提示里，但那是几十轮之前的
        文本了）。它写 `me` 时意图非常明确，因为一个歧义的名字把它挡在
        门外是没道理的。

        其余值原样透传——**代别人认领是允许的**（对齐 Claude Code：
        `owner` 是任意 agent name），主 Agent 有时确实要替队员先占住一条任务。
        """
        text = owner.strip()
        if text.lower() in ("me", "self", "myself") or text == "我":
            return current_identity()
        return text

    def _newly_claimable(self, task_id: str, state) -> list[str]:
        """
        本次改动之后，哪些任务变得可以认领了。

        只在把任务标成**已完成**时才算——那是唯一会解锁别人的动作。
        把它写进回灌文本，是为了让刚做完一件事的队员**当场知道下一步能做什么**，
        而不必再调一次 `task_list`（少一轮往返，也少一次「它忘了看清单」的机会）。
        """
        if state is not TaskState.COMPLETED:
            return []
        out = []
        for task in self.board.snapshot():
            if (
                task.task_id != task_id
                and task.state is TaskState.PENDING
                and not task.owner
                and not self.board.is_blocked(task.task_id)
                and task_id in task.blocked_by
            ):
                out.append(task.task_id)
        return out


def build_board_tools(service: "TeamService") -> list[Tool]:
    """
    造出四个任务工具。

    :param service: 协作服务门面
    :returns: 四个工具实例，注册顺序即它们在工具清单里的顺序

    顺序刻意是「建 → 列 → 查 → 改」：模型读工具清单时，
    这个顺序本身就是一次使用流程的提示。
    """
    return [
        TaskCreateTool(service),
        TaskListTool(service),
        TaskGetTool(service),
        TaskUpdateTool(service),
    ]


__all__ = [
    "TaskCreateTool",
    "TaskGetTool",
    "TaskListTool",
    "TaskUpdateTool",
    "build_board_tools",
]
