"""
子 Agent 协作的数据结构与常量表（c15 T1/T2）。

本模块是 `team` 包的最底层：只定义「一条共享任务长什么样」「一条消息长什么样」
「花名册上一位队员长什么样」，**不做任何 IO、不依赖包内其它模块**。
清单逻辑在 `board.py`，花名册在 `roster.py`，消息路由在 `mailbox.py`。

## 术语（第一次接触本章时先看这里）

C13 的子 Agent 是**一次性临时工**：干完汇报一句结论就消失，彼此不认识，
所有信息经主 Agent 中转。C15 把它们变成**能互相协作的队员**：

- **队员（member）**：一个有**名字**的子 Agent。名字是消息投递与任务认领的
  唯一标识，且在它干完之后**依然有效**——再给它发一条消息就能把它从
  原来的上下文唤醒继续干，不必重新委派、不必重新交代背景。
- **共享任务清单（board）**：一块所有人都能读写的看板。每条任务带
  **认领人**与**依赖关系**，队员据此自我调度，不需要主 Agent 当交通警察。
- **信箱（inbox）**：每位队员的未读消息队列。**投递是推送式的**——
  收件人不需要主动查收，正在跑的在下一轮迭代开头就看到它。
- **主对话（`main`）**：它也是花名册上的一员，队员可以直接给它发消息。

对应 spec 条款见 `docs/c15/spec.md`：F4/F5（清单构成）、F9（消息构成）、
F13（待命）、F15（待命不占并发名额）、F20（自动唤起连锁上限）。

## ⚠ 与 C13 `TaskStatus` 的关系（本章最容易埋的雷）

**本模块的 `MemberState` 与 C13 的 `TaskStatus` 是两个维度，刻意不合并。**

C13 的 `TaskStatus` 回答「**这次委派的结论产出了没有**」，
Agent Loop 的闸门据它判断「还要不要为这个子 Agent 停留」
（`agent/gate.py` 的 `has_awaited` 用的是 `not status.is_terminal`）。

本模块的 `MemberState` 回答「**这个人还在不在场、叫不叫得醒**」。

队员自然停止时：`TaskStatus` 照常置 `COMPLETED`（结论确实产出了），
`MemberState` 置 `IDLE`（人还在，叫得醒）。

⚠ **如果反过来给 `TaskStatus` 加一个 `is_terminal` 为假的 `IDLE`，
主 Agent 每次准备收工都会去等一个已经待命的队员，而那个队员正等着
主 Agent 给它发消息——双方互等，永远结束不了。** 这条见 plan 决策 2。
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover —— 仅类型检查期
    # 只在类型检查期引入，**运行期不产生任何依赖**——`team` 是叶子包，
    # 真去 import `provider` 会让它背上一条本不需要的运行时依赖。
    from rhinecode.provider.base import Message


# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

# 主对话在花名册里的名字。
#
# 它是一个**保留名**：队员不能叫这个名字（`Roster.register` 会拒绝），
# 否则一条 `to: "main"` 的消息会被送到某个队员手里，而两边都不报错。
#
# 对齐 Claude Code 的 `SendMessage` 语义（「`"main"` = The main conversation」）。
MAIN_NAME = "main"

# 同时保留完整对话历史的待命队员上限（spec N3）。
#
# 待命队员的历史**不会自动释放**——那正是「叫醒它就能接着干」的物理前提。
# 因此必须有个上限，否则一次长会话里反复委派会让内存单调增长。
#
# 超限时由 `Roster.mark_idle` 把**最久未活动**的那个降级为 `RETIRED`
# 并释放历史。⚠ 降级必须**看得见**（返回值要被调用方消费并告知用户）——
# 静默降级会让用户遇到「同一个名字昨天叫得醒、今天叫不醒」而毫无线索。
MAX_IDLE_MEMBERS = 5

# 「消息 → 自动唤起主对话 → 回消息 → 对方又发 → 又被唤起」这条链的连续次数上限
#（spec F20）。
#
# 没有它，两个 Agent 能互相唤醒到天亮、把用户的额度烧光，
# 而界面上看起来只是「一直在动」。
#
# 计数在**用户提交任何一条消息时清零**——用户回来了，链条就重新开始算。
MAX_AUTO_WAKE_CHAIN = 5


# ---------------------------------------------------------------------------
# 枚举
# ---------------------------------------------------------------------------


class TaskState(Enum):
    """
    共享清单上一条任务的三种状态（spec F5）。

    对齐 Claude Code 的 `pending` / `in_progress` / `completed`。
    **删除不是一种状态**——它是把条目从清单里移除（见 `board.remove`）。
    """

    PENDING = "pending"          # 待办：没人认领，或认领人放手了
    IN_PROGRESS = "in_progress"  # 进行中：已被某人认领
    COMPLETED = "completed"      # 已完成


class MemberState(Enum):
    """
    花名册上一位队员的五种状态（spec F13/F14）。

    ```
    RUNNING ⇄ IDLE                                  （IDLE 可被消息唤醒回 RUNNING）
       ↓
    DONE / FAILED / CANCELLED / RETIRED              （终态，不可唤醒）
    ```

    四种终态**刻意分开**而不是合成一个「结束了」：
    向一个终态队员发消息时，用户与模型需要知道**是哪一种**——
    干完收工了、跑挂了可以改派、被取消是人的决定、已退休则是本次会话
    待命的人太多被系统降级的（那不是谁的错，重新委派一个即可）。

    `DONE` 与 `IDLE` 的差别只有一条：**还叫不叫得醒**。
    两者都是「自然干完了」，但 `DONE` 的上下文已经不保留了
    （目前唯一的成因是隔离委派——它与待命互斥，理由见 `runner.py`）。
    合成一个状态的话，用户会对着一个「待命」的名字发消息却石沉大海。
    """

    RUNNING = "running"      # 正在跑
    IDLE = "idle"            # 待命：自然停止、历史保留、叫得醒
    DONE = "done"            # 自然完成但不保留上下文（隔离委派），叫不醒
    FAILED = "failed"        # 跑挂了
    CANCELLED = "cancelled"  # 被取消（`Esc` / `/agents cancel` / 会话切换）
    RETIRED = "retired"      # 待命超上限被降级，历史已释放

    @property
    def is_wakeable(self) -> bool:
        """
        这个状态下还能不能被消息唤醒。

        :returns: **只有 `IDLE` 为真**

        `RUNNING` 也返回假：它已经在跑了，消息会经闸门在下一轮迭代注入，
        不需要（也不能）再「唤醒」一次。两条路径混淆会让一条消息被注入两遍。
        """
        return self is MemberState.IDLE

    @property
    def is_terminal(self) -> bool:
        """
        是不是终态（再也回不到 `RUNNING`）。

        ⚠ **这与 C13 `TaskStatus.is_terminal` 不是一回事**，两者服务于
        不同的判断，见模块 docstring 的「⚠ 与 C13 `TaskStatus` 的关系」。
        """
        return self in (
            MemberState.DONE,
            MemberState.FAILED,
            MemberState.CANCELLED,
            MemberState.RETIRED,
        )


# 中文展示名。
#
# **单独两张表而不是塞进枚举值**，理由与 C13 的 `STATUS_LABELS` 相同：
# 枚举值同时是行为记录（trace）里的稳定标识，不该跟着界面措辞变。
# 改一句中文不应该让历史记录文件里的字段跟着变。
TASK_STATE_LABELS = {
    TaskState.PENDING: "待办",
    TaskState.IN_PROGRESS: "进行中",
    TaskState.COMPLETED: "已完成",
}

MEMBER_STATE_LABELS = {
    MemberState.RUNNING: "运行中",
    MemberState.IDLE: "待命",
    MemberState.DONE: "已完成",
    MemberState.FAILED: "失败",
    MemberState.CANCELLED: "已取消",
    MemberState.RETIRED: "已退休",
}


# ---------------------------------------------------------------------------
# 数据结构
# ---------------------------------------------------------------------------


@dataclass
class BoardTask:
    """
    共享清单上的一条任务（spec F5）。

    **可变**：状态、认领人、依赖都会在运行期被改。所有读写都经 `TaskBoard`
    的锁，`board.get()` / `board.snapshot()` 对外返回的是**副本**，
    使调用方在锁外拿到的东西不会被别的线程改到，也改不回内部状态。

    :param task_id: 任务标识。**用小整数顺序号的字符串**（`"1"`、`"2"`…），
        不用随机短串。

        理由：Claude Code 的清单语义里「优先做 ID 小的任务」是一条有效提示
        （早建的任务往往为后建的铺垫上下文），随机串会让这层顺序信息消失。
        用字符串而不是 int，是为了与模型给的工具参数形态一致——
        模型传回来的就是字符串，少一次两边都要记得做的类型转换。
    :param subject: 标题，祈使句（「修复登录接口的空指针」）。
    :param description: 说明：要做什么、做到什么程度算完。
    :param state: 三态之一。
    :param owner: 认领人的名字；`""` 表示无人认领。
        **认领必须走 `board.claim`**（原子操作），不要直接改这个字段——
        直接改会让两个队员同时认领成功，而双方都以为任务归自己。
    :param blocked_by: 被哪些任务挡着（这些任务全部完成前，本任务不能被认领）。
    :param blocks: 本任务挡着哪些任务。

        ⚠ **成对维护点**：`blocked_by` 与 `blocks` 是**双向冗余存储**，
        必须在同一个临界区内成对更新（见 `board.add_dependency`）。
        只存一边的话，每次列出清单都要遍历全表反查，而清单是每个队员
        每一轮都可能读的高频操作。
    :param created_at: 建立时刻（`time.time()`）。
    :param updated_at: 最近一次变更时刻，展示与排序用。
    """

    task_id: str
    subject: str
    description: str = ""
    state: TaskState = TaskState.PENDING
    owner: str = ""
    blocked_by: tuple[str, ...] = ()
    blocks: tuple[str, ...] = ()
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)

    @property
    def sort_key(self) -> int:
        """
        排序用的数字键。

        :returns: `task_id` 的整数形式；不是纯数字时返回一个极大值，
                  使它排在最后而不是抛异常

        容错而不是抛异常：`task_id` 由本包生成、正常情况下必是数字串，
        但排序是展示路径上的操作，**展示绝不该因为一条脏数据整个崩掉**。
        """
        try:
            return int(self.task_id)
        except (TypeError, ValueError):
            return 1 << 30


@dataclass(frozen=True)
class Envelope:
    """
    一条点对点消息（spec F9）。

    frozen=True：消息一旦发出就不该被改写。唯一的「变更」是标记已读，
    而那通过替换整个对象完成（见 `mailbox.take_unread`），
    不是原地改字段——这让「同一条消息被两个线程同时标已读」不可能发生。

    :param sender: 发件人名字（某个队员，或 `main`）。
    :param recipient: 收件人名字。
    :param summary: 一句话摘要，5–10 个词，**界面单行展示用**。
        它不是可选的装饰：用户在界面上看到的是这一行，正文只有模型会读。
    :param body: 正文，纯文本。
    :param sent_at: 发送时刻。

        ⚠ **由系统填充，不由模型给。** 让模型给时间戳等于让它有机会给出
        一个假的顺序，而信箱是按到达顺序排的。
    :param read: 是否已被注入过收件人的历史。**缺省未读。**

        「取走即置位」是幂等性的全部依据（见 `mailbox.take_unread`）——
        没有它，同一条消息会在收件人的每一轮迭代里都被注入一遍。
    """

    sender: str
    recipient: str
    summary: str
    body: str
    sent_at: float = field(default_factory=time.time)
    read: bool = False


@dataclass
class MemberEntry:
    """
    花名册上的一位队员（spec F1/F3/F13）。

    **可变**：状态、信箱、历史、活动时间都在运行期变化。
    所有读写都经 `Roster` 的锁。

    :param name: 唯一名字。委派时指定，或由 `Roster.suggest_name` 生成。
    :param task_id: 关联的 C13 `TaskRecord` 标识，用于 `/agents` 把
        「花名册状态」与「任务状态」两个维度对齐展示。`main` 为空串。
    :param state: 五态之一，见 `MemberState`。
    :param read_only: 该队员的**最终工具集是否全只读**。

        **在委派时算好存下来**，而不是用时现算——现算需要从 `team` 反向
        依赖 `subagents` 的工具集逻辑，那会破坏「`team` 是叶子包」这条
        架构不变量。它服务于 spec F24：Plan Mode 的规划阶段只允许给
        `main` 或全只读队员发消息（给一个能写文件的待命队员发消息会把它
        唤醒去动手，直接绕过「批准前不动手」的承诺）。
    :param inbox: 消息队列，按到达顺序追加。已读的**不立即删除**——
        保留它们使 `/agents` 能显示「收到过几条」，清理由 `RETIRED` 时
        整条释放承担。
    :param history: 待命期间保管的完整对话历史。

        这是「叫醒就能接着干」的物理前提：唤醒时把它交回运行器，
        运行器据此再跑一次 Agent Loop，于是队员带着全部上下文继续，
        不需要重新交代背景。转终态时**必须清空**（释放内存）。
    :param wake_event: 唤醒信号。队员线程待命时阻塞在它上面。

        ⚠ **`set()` 必须在锁外调用**——它唤醒等待线程，属跨线程调度。
        落进临界区会与 Textual 阻塞式 `call_from_thread` 组成确定性死锁
        （C11 `SkillManager`、C12 `HookManager`、C13 `TaskManager`
        三次同源事故）。`main` 没有这个字段的实际用途（它的唤醒走 TUI
        轮询），但仍然建一个，使路由代码不必特判。
    :param last_active: 最近一次运行结束的时刻。

        spec N3 降级的依据：待命的人超过上限时，挑**这个值最小**的
        （最久没干活的）降级。
    """

    name: str
    task_id: str = ""
    state: MemberState = MemberState.RUNNING
    read_only: bool = False
    inbox: list[Envelope] = field(default_factory=list)
    history: list["Message"] = field(default_factory=list)
    wake_event: threading.Event = field(default_factory=threading.Event)
    last_active: float = field(default_factory=time.time)

    @property
    def unread_count(self) -> int:
        """未读消息条数，`/agents` 与花名册展示用。"""
        return sum(1 for env in self.inbox if not env.read)

    @property
    def is_main(self) -> bool:
        """是不是主对话那个特殊条目。"""
        return self.name == MAIN_NAME


__all__ = [
    "MAIN_NAME",
    "MAX_AUTO_WAKE_CHAIN",
    "MAX_IDLE_MEMBERS",
    "MEMBER_STATE_LABELS",
    "TASK_STATE_LABELS",
    "BoardTask",
    "Envelope",
    "MemberEntry",
    "MemberState",
    "TaskState",
]
