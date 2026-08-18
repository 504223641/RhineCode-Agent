"""
主对话待办清单的数据模型（todo-list 扩展 T2，spec F2/F4/F14）。

## 它是什么

主 Agent 的**私有进度笔记**：一个多步任务开工前它列几条，做的过程中
逐条更新，用户随时能在历史区底部看到「还剩什么」。

## ⚠ 它与 C15 共享任务清单（`team/board.py`）是两个东西

| | C15 共享清单 | 本模块 |
| --- | --- | --- |
| 写入方 | 主 Agent + 全部队员，**并发** | 主 Agent 一个，**串行** |
| 存在理由 | 「**谁**来做这条」——多方抢活的协商媒介 | 「**做到哪了**」——单一执行者的进度 |
| 认领人 / 依赖 | 核心字段 | **不存在** |

一份数据结构里一半字段永远不用、一半方法永远不调，通常说明它不是同一个东西。
更要紧的是**混表的后果**：两者进同一张表之后，用户会同时看到自己的待办
与队员的工单，而**没有任何线索分辨哪条是谁的**。

## ⚠ 为什么连状态枚举都不复用 `team.models.TaskState`

复用它要让 `todo` 与 `team` 两个**叶子包互相知道对方**——省下的是十几行，
换来的是一条会长期误导人的跨包依赖。这与本项目里
「三份 `Layer` 名字表刻意不合一」（`trace` 叶子包不能反向依赖 `permission`）、
「C13 的 `TaskStatus` 与 C15 的 `MemberState` 刻意分开」是同一条经验。

两者的演化方向也不同：C15 那份将来可能因认领语义新增状态，
本模块的三态是**封闭**的（spec F4 明确不加「已取消」「已阻塞」）。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Optional


class TodoState(str, Enum):
    """
    一条待办的状态。**三个取值，封闭集合。**

    ⚠ **刻意不设「已取消」「已阻塞」**（spec F4）：那是多方协作才需要的，
    单一执行者用不上——一条不做了，下次覆写时不带它即可
    （整表覆写语义让「删除」变成了免费操作，见 `store.TodoStore.replace`）。

    ## 字符串取值为什么是这三个英文词

    它们是**模型在工具参数里写的东西**，对齐 Claude Code 的 `TodoWrite`
    （`pending` / `in_progress` / `completed`），让从那边迁移过来的用法
    与写法直接可用——与 C13「角色 `name` 缺省取文件名」是同一个考虑：
    生态兼容优先于内部整齐。

    中文标签另有一张表（`TODO_STATE_LABELS`），**只用于显示**。
    """

    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"


# 状态 → 中文标签。**只用于显示，不参与任何判定。**
#
# ⚠ **文字标签不可省**（spec F14）：待办块同时用颜色和文字表达状态，
# 而颜色在单色终端、截图、以及色觉障碍用户那里都可能丢失——
# **文字是脱离颜色之后唯一还认得出的东西**。这与本项目「警告与错误靠
# 文字前缀而不是图形」是同一条理由。
TODO_STATE_LABELS: dict[TodoState, str] = {
    TodoState.PENDING: "待办",
    TodoState.IN_PROGRESS: "进行中",
    TodoState.COMPLETED: "已完成",
}


@dataclass(frozen=True)
class TodoItem:
    """
    清单上的一条待办。

    :param title: 标题，祈使句（「改 login 接口」）。已 strip。
    :param state: 三态之一。

    ## ⚠ 只有两个字段，这是刻意的（spec F2）

    没有标识、没有认领人、没有依赖、没有说明、没有优先级、没有时限。

    **没有标识**是整表覆写换来的直接简化：模型每次传完整列表
    （见 `store.TodoStore.replace`），因此不存在「引用某一条」的场景，
    也就不需要一个稳定标识。展示时的序号是**位置**而不是身份。

    ## ⚠ `frozen=True`，与 C15 的 `BoardTask`（可变）刻意相反

    那边要在锁内逐字段改状态（认领、改派、标完成），所以必须可变，
    代价是 `snapshot()` 每次都要 `replace()` 出一份副本，
    免得调用方在锁外拿到的对象被别的线程改到。

    这边一次性整表替换，没有「改一个字段」的场景。不可变于是同时买到两样：
    快照可以**直接返回内部元组、零复制**，且从结构上不存在
    「拿到快照之后被别人改掉」这类竞态。
    """

    title: str
    state: TodoState


def parse_state(raw: object) -> Optional[TodoState]:
    """
    把模型给的状态值解析成 `TodoState`。

    :param raw: 模型在工具参数里写的东西。缺省（`None` 或空串）按
        `PENDING` 处理——**新列出来的待办本来就是待办**，
        强制模型每条都写一遍 `"state": "pending"` 只是徒增出错的机会。
    :returns: 解析成功的枚举值；认不出时 `None`（由调用方转成可读拒绝原因）

    ## 容错口径：大小写不敏感 + 连字符与下划线都认

    与 C13 角色 frontmatter 的字段名归一同一条理由——模型写
    `"In-Progress"` 或 `"IN_PROGRESS"` 时，报一个「状态认不出」的错
    对用户毫无价值，而它表达的意思没有任何歧义。

    ⚠ **只在这里容错，不要往枚举里加别名成员**：`TodoState` 的取值集合
    要与工具 `parameters` 里的 `enum` **逐字相等**（有护栏钉着），
    加别名会让那条断言失去意义。

    副作用：无。
    """
    if raw is None:
        return TodoState.PENDING
    if not isinstance(raw, str):
        return None
    normalized = raw.strip().lower().replace("-", "_")
    if not normalized:
        return TodoState.PENDING
    for state in TodoState:
        if state.value == normalized:
            return state
    return None


__all__ = ["TODO_STATE_LABELS", "TodoItem", "TodoState", "parse_state"]
