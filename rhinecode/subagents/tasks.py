"""
后台任务表（c13 T12/T13，spec F18/F20/F22）。

**职责**：追踪每个子 Agent 任务的状态、结果与用量，是任务信息的唯一持有者。
主对话、后台运行器、TUI 轮询、`/agents` 报告全都通过它观察任务。

## ⚠ 加锁不变量（与 C11 `SkillManager`、C12 `HookManager` 同一条）

**临界区只做纯内存读写**，一切回调、埋点、跨线程调度在锁外。

本类**刻意不持有任何回调**，从结构上杜绝违反——没有可调的东西，就不可能
在持锁时调它。`tests/test_subagent_tasks.py` 有一条结构护栏遍历实例属性，
断言不存在 callable 成员。

唯一需要小心的是 `threading.Event.set()`：它会唤醒等待线程，属于跨线程调度，
因此 `finish` / `cancel` 里的 `set()` 都放在**锁外**。

## 两条独立的消费线

`delivered`（交付给模型）与 `notified`（通知给用户）各有各的标志位，
**刻意不合成一条**：

- 通知在任务完成的**那一刻**发生（用户当时在做什么都不影响）；
- 交付要等到**下一轮迭代**组装请求之前（c13 修订：交付点已从「每条用户消息一次」
  下移到迭代级，见 `agent/gate.py`）。

合成一条会导致「用户还没看到通知，模型已经引用了结论」或反之。
"""

from __future__ import annotations

import secrets
import threading
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class TaskStatus(Enum):
    """一个子 Agent 任务的四种状态。三个终态之间互不转换。"""

    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"

    @property
    def is_terminal(self) -> bool:
        return self is not TaskStatus.RUNNING


STATUS_LABELS = {
    TaskStatus.RUNNING: "运行中",
    TaskStatus.COMPLETED: "已完成",
    TaskStatus.FAILED: "失败",
    TaskStatus.CANCELLED: "已取消",
}

# 委派的两种类型。用字符串常量而不是枚举：它直接来自模型给的工具参数，
# 也直接进 trace 负载，保持字符串省掉两次转换。
KIND_ROLE = "role"
KIND_BRANCH = "branch"

# 分支式委派没有角色名，展示与作用域都用这个占位。
BRANCH_AGENT_NAME = "(branch)"


@dataclass
class TaskRecord:
    """
    一个子 Agent 任务的全部状态。

    **可变**（与包内其它数据类不同）：运行器会在跑的过程中更新轮次与用量，
    TUI 轮询与 `/agents` 会读它。所有读写都经 `TaskManager` 的锁。

    :param task_id: 短标识，模型与用户都用它指代任务
    :param kind: `role` / `branch`
    :param agent_name: 角色名；分支式为 `(branch)`
    :param task_text: 任务描述原文
    :param status: 四态之一
    :param started_at: 起始时间（`time.monotonic`，只用于算耗时）
    :param finished_at: 结束时间；未结束时 `None`
    :param turns: 已用轮次，运行中也在更新
    :param usage_tokens: 累计 token
    :param conclusion: 结论文本
    :param stop_reason: 结束原因（Agent Loop 的 `StopReason` 值，或异常摘要）
    :param delivered: 结论是否已追加进主历史
    :param notified: 完成通知是否已出现在界面上
    :param awaited: 模型是否声明「这次我要这个结果」。见字段处的注释
    :param worktree_path: 隔离工作区路径（c14 F23）。非隔离任务为空串。
    :param worktree_branch: 隔离工作区的分支名。非隔离任务为空串。

        ⚠ **这里只存两个字符串，不存 `WorktreeHandle` 对象。**
        `TaskRecord` 的读写都在 `TaskManager` 的加锁临界区内，而临界区的既有
        硬不变量是「只做纯内存读写」。放一个能调 git 的对象进去，是在给后来者
        挖坑——他会很自然地写出 `record.worktree.inspect()`，于是一次 git
        子进程调用跑在锁里，整个 manager 被一条卡住的命令锁死。
    :param cancel_event: 取消信号，运行器在安全点轮询
    :param done_event: 完成信号。**c13 修订后已无前台等待方**，保留它是因为
        运行器的收尾仍靠它表达「这条真的结束了」，且测试用它做同步点
    """

    task_id: str
    kind: str
    agent_name: str
    task_text: str
    status: TaskStatus = TaskStatus.RUNNING
    started_at: float = field(default_factory=time.monotonic)
    finished_at: Optional[float] = None
    turns: int = 0
    usage_tokens: int = 0
    conclusion: str = ""
    stop_reason: str = ""
    delivered: bool = False
    notified: bool = False
    worktree_path: str = ""
    worktree_branch: str = ""
    # 模型委派时是否声明「这次我要这个结果」（`background=false`，缺省）。
    # 为真时 Agent Loop 在准备自然结束前会停下来等它（见 agent/gate.py）；
    # `background=true` 置假——那是模型明说过不等的，循环不该为它停留。
    awaited: bool = True
    cancel_event: threading.Event = field(default_factory=threading.Event)
    done_event: threading.Event = field(default_factory=threading.Event)

    @property
    def duration_seconds(self) -> float:
        """已运行/已耗时秒数。未结束时按当前时间算。"""
        end = self.finished_at if self.finished_at is not None else time.monotonic()
        return max(0.0, end - self.started_at)

    @property
    def label(self) -> str:
        """展示用的短标签，如 `explorer[a3f1c9]`。"""
        return f"{self.agent_name}[{self.task_id}]"


class TaskManager:
    """
    任务表，线程安全。

    并发场景：多个后台子 Agent 线程同时 `bump` / `finish`，
    TUI 主线程每 0.5 秒 `drain_notifications`，主对话线程 `take_deliverables`，
    用户随时 `cancel`。全部经同一把锁。

    副作用：只改自身内存状态。**不做任何 IO、不调任何回调。**
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._tasks: dict[str, TaskRecord] = {}

    # ------------------------------------------------------------------ #
    # 创建与更新
    # ------------------------------------------------------------------ #

    def create(self, kind: str, agent_name: str, task_text: str) -> TaskRecord:
        """
        登记一个新任务，状态为运行中。

        :param kind: `role` / `branch`
        :param agent_name: 角色名；分支式传 `BRANCH_AGENT_NAME`
        :param task_text: 任务描述原文
        :returns: 新建的记录（调用方拿它去起线程）

        标识用 `secrets.token_hex(3)` 生成 6 位十六进制。冲突时重生成——
        6 位有 1600 万种可能，一次运行里最多几十个任务，循环几乎不会执行到第二轮，
        但写上它比「假设不会撞」可靠。

        副作用：写入内部字典。
        """
        with self._lock:
            while True:
                task_id = secrets.token_hex(3)
                if task_id not in self._tasks:
                    break
            record = TaskRecord(
                task_id=task_id,
                kind=kind,
                agent_name=agent_name,
                task_text=task_text,
            )
            self._tasks[task_id] = record
            return record

    def bump(
        self,
        task_id: str,
        turns: Optional[int] = None,
        tokens: Optional[int] = None,
    ) -> None:
        """
        更新运行中的计数。

        :param task_id: 任务标识
        :param turns: 已用轮次的**新值**（不是增量）
        :param tokens: token 用量的**增量**

        两个参数语义不同是刻意的：轮次由运行器直接数得到（它知道跑到第几轮），
        用量则是每次请求返回一点、需要累加。

        未知标识静默忽略——任务可能已被清理，为此抛异常会让运行器
        在收尾路径上炸掉。

        副作用：改记录字段。
        """
        with self._lock:
            record = self._tasks.get(task_id)
            if record is None or record.status.is_terminal:
                return
            if turns is not None:
                record.turns = turns
            if tokens:
                record.usage_tokens += tokens

    def finish(
        self,
        task_id: str,
        status: TaskStatus,
        conclusion: str,
        stop_reason: str = "",
    ) -> None:
        """
        把任务置为终态并唤醒等待方。

        :param task_id: 任务标识
        :param status: 三个终态之一
        :param conclusion: 结论文本（失败时是可读的失败说明）
        :param stop_reason: 结束原因，进 trace 与报告

        **幂等**：已是终态的任务再调一次不改变任何东西。运行器的 `finally`
        与异常兜底可能都会调到它，不幂等的话结论会被后一次覆盖成空串。

        `done_event.set()` 放在**锁外**：它会唤醒等待线程，属于跨线程调度，
        持锁时做这件事违反本模块的加锁不变量。

        副作用：改记录字段；置 `done_event`。
        """
        with self._lock:
            record = self._tasks.get(task_id)
            if record is None or record.status.is_terminal:
                record = None
            else:
                record.status = status
                record.conclusion = conclusion
                record.stop_reason = stop_reason
                record.finished_at = time.monotonic()

        # ── 锁外 ──
        if record is not None:
            record.done_event.set()

    # ------------------------------------------------------------------ #
    # 查询
    # ------------------------------------------------------------------ #

    def get(self, task_id: str) -> Optional[TaskRecord]:
        with self._lock:
            return self._tasks.get(task_id)

    def snapshot(self) -> tuple[TaskRecord, ...]:
        """
        取全部任务的只读快照，按创建顺序。

        :returns: 记录元组

        返回的是**记录对象本身**而不是深拷贝：调用方（报告、轮询）只读，
        深拷贝一份含两个 Event 的对象既贵又没必要。元组本身是在锁内构造的，
        因此不会在遍历途中被并发的 `create` 改变长度。
        """
        with self._lock:
            return tuple(self._tasks.values())

    def running_count(self) -> int:
        """当前运行中的任务数（并发上限的判据，spec F20）。"""
        with self._lock:
            return sum(
                1 for r in self._tasks.values() if r.status is TaskStatus.RUNNING
            )

    def running_brief(self) -> str:
        """
        当前在跑的任务简述，超并发上限时回灌给模型。

        :returns: 形如 `explorer[a3f1c9]、reviewer[7b2e10]`；无任务时空串

        为什么要具体列出而不只说「已达上限」：模型据此能判断
        「我要等的那个是不是已经在跑了」，而不是盲目重试。
        """
        with self._lock:
            names = [
                r.label
                for r in self._tasks.values()
                if r.status is TaskStatus.RUNNING
            ]
        return "、".join(names)

    # ------------------------------------------------------------------ #
    # 两条独立的消费线
    # ------------------------------------------------------------------ #

    def take_deliverables(self) -> tuple[TaskRecord, ...]:
        """
        取走「已完成且尚未交付主历史」的任务（spec F21 第 3 步）。

        :returns: 本次取到的记录；调用方负责把结论追加进主历史

        **取走即置位**，因此重复调用是幂等的——第二次返回空。
        由主对话在**组装请求之前**调用，不能在一次正在运行的循环中途调
        （那会破坏消息协议顺序）。
        """
        with self._lock:
            taken = tuple(
                r
                for r in self._tasks.values()
                if r.status.is_terminal and not r.delivered
            )
            for r in taken:
                r.delivered = True
            return taken

    def drain_notifications(self) -> tuple[TaskRecord, ...]:
        """
        取走「已结束且尚未通知用户」的任务（spec F21 第 1 步）。

        :returns: 本次取到的记录；调用方负责在界面上出通知行

        由 TUI 的定时轮询在**主线程**调用。与 `take_deliverables` 是
        两条独立的消费线，理由见模块 docstring。
        """
        with self._lock:
            taken = tuple(
                r
                for r in self._tasks.values()
                if r.status.is_terminal and not r.notified
            )
            for r in taken:
                r.notified = True
            return taken

    # ------------------------------------------------------------------ #
    # 取消
    # ------------------------------------------------------------------ #

    def cancel(self, task_id: str) -> bool:
        """
        取消单个任务。

        :param task_id: 任务标识
        :returns: 是否真的取消了（标识不存在或已是终态时为假）

        只**置信号**，不直接改状态——真正的收尾由运行器做（它要在安全点
        停下来、写结论、埋 trace）。这里直接标成已取消的话，运行器随后
        还会调一次 `finish`，两处对同一个任务的结论各写一遍。

        `cancel_event.set()` 在锁外，理由同 `finish`。
        """
        with self._lock:
            record = self._tasks.get(task_id)
            if record is None or record.status.is_terminal:
                record = None

        if record is None:
            return False
        record.cancel_event.set()
        return True

    def cancel_all(self) -> int:
        """
        取消全部未完成任务（`/clear`、`/resume`、退出、`/agents cancel all`）。

        :returns: 被取消的任务数
        """
        with self._lock:
            records = [
                r for r in self._tasks.values() if r.status is TaskStatus.RUNNING
            ]

        for record in records:
            record.cancel_event.set()
        return len(records)


__all__ = [
    "TaskStatus",
    "STATUS_LABELS",
    "TaskRecord",
    "TaskManager",
    "KIND_ROLE",
    "KIND_BRANCH",
    "BRANCH_AGENT_NAME",
]
