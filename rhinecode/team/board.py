"""
共享任务清单（c15 T3–T6，spec F4–F8）。

## 它是什么

一块**所有人都能读写的看板**：主 Agent 与全部队员共用同一份，
清单不属于任何一个 Agent。每条任务带**认领人**与**依赖关系**，
于是队员能自我调度——甲做完 T1 把它标完成，看板上 T2 自动解锁，
乙自己认领 T2 开工，**全程不需要主 Agent 当交通警察**。

这是 C13 完全没有的东西：那时任务是主 Agent 塞给某个子 Agent 的一段话，
别人看不见，进度只能靠各自回流的结论去拼。

对齐 Claude Code 的 `TaskCreate` / `TaskList` / `TaskGet` / `TaskUpdate`
（字段 `owner` / `blockedBy` / `blocks`，语义「被挡住的任务不能被认领」）。

## ⚠ 加锁不变量（与 C11 `SkillManager`、C12 `HookManager`、C13 `TaskManager` 同一条）

**临界区只做纯内存读写**，一切回调、埋点、跨线程调度在锁外。

本类**刻意不持有任何回调**，从结构上杜绝违反——没有可调的东西，
就不可能在持锁时调它。`tests/test_team_board.py` 有一条结构护栏遍历
实例属性，断言不存在 callable 成员。

违反的后果不是「偶尔慢一点」：一个在锁内触发的回调若走到 Textual 的
阻塞式 `call_from_thread`，会与主线程组成**确定性死锁，整个 TUI 冻结**，
而调用栈上没有任何线索。

## 对外一律返回结构化结果，不抛异常

`create` 之外的每个改动方法都返回一个带 `ok` / `reason` 的结果对象。
理由：这些方法的直接调用方是**工具**，而工具的契约是「不得向上抛异常，
否则 Agent Loop 会把它变成一条『工具执行异常』，丢掉这里已经组织好的
可读原因」——而可读原因正是模型自我纠正的唯一依据。
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, replace
from typing import Optional

from rhinecode.team.models import BoardTask, TaskState


# ---------------------------------------------------------------------------
# 结果类型
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class UpdateResult:
    """
    一次更新的结果。

    :param ok: 是否成功
    :param reason: 失败原因（可读中文，直接回灌给模型）
    :param task: 更新后的任务副本；失败时为 `None`
    """

    ok: bool
    reason: str = ""
    task: Optional[BoardTask] = None


@dataclass(frozen=True)
class DepResult:
    """
    一次依赖声明的结果。

    :param ok: 是否成功
    :param reason: 失败原因；成环时**含完整的成环路径**，
        使模型能看懂自己把哪几条任务连成了圈
    """

    ok: bool
    reason: str = ""


@dataclass(frozen=True)
class ClaimResult:
    """
    一次认领的结果。**四种结果必须能被调用方区分**——
    每一种对应完全不同的下一步动作。

    :param ok: 是否认领成功
    :param reason: 可读原因
    :param blocked_by: 被挡住时，尚未完成的前置任务 ID。
        **必须给出来**：只说「被挡住了」的话，模型只能猜是哪几条，
        而它下一步该做的正是去看那几条的进度（spec F7 / AC8）
    :param current_owner: 已被认领时，当前认领人是谁。
        同理——模型据此决定是去找那个人协调，还是换一条任务做
    """

    ok: bool
    reason: str = ""
    blocked_by: tuple[str, ...] = ()
    current_owner: str = ""


# ---------------------------------------------------------------------------
# 清单
# ---------------------------------------------------------------------------


class TaskBoard:
    """
    共享任务清单。一个会话一份，由 `TeamService` 持有。

    线程模型：内部一把 `threading.Lock`，全部公开方法自己加锁。
    调用方（工具、协调层、界面）**不需要**也**不应该**在外面再加锁。

    `get` / `snapshot` 返回的是**副本**：调用方在锁外拿到的对象不会被
    别的线程改到，改动它也回不到板内。这让「读一份数据慢慢渲染」变得安全，
    代价是每次读都要复制——清单是几十条量级的东西，这个代价可以忽略。
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._tasks: dict[str, BoardTask] = {}
        # 下一个要分配的顺序号。**只增不减**——删掉 3 号之后新建的仍是 4 号，
        # 复用编号会让「刚才说的 3 号」在两个人嘴里指两条不同的任务。
        self._next_id = 1

    # ------------------------------------------------------------------ #
    # 读
    # ------------------------------------------------------------------ #

    def get(self, task_id: str) -> Optional[BoardTask]:
        """
        取单条任务。

        :param task_id: 任务标识
        :returns: 任务的**副本**；不存在时 `None`

        副作用：无。
        """
        with self._lock:
            task = self._tasks.get(str(task_id))
            return replace(task) if task is not None else None

    def snapshot(self) -> tuple[BoardTask, ...]:
        """
        全部任务的只读快照，按 ID 升序。

        :returns: 任务副本的元组

        升序而不是建立顺序：两者在正常情况下一致，但删除再新建之后
        字典顺序会与编号脱节，而用户和模型都是按编号指代任务的。

        副作用：无。
        """
        with self._lock:
            return tuple(
                replace(t) for t in sorted(self._tasks.values(), key=lambda t: t.sort_key)
            )

    def count(self) -> int:
        """当前任务条数（展示与空态判断用）。"""
        with self._lock:
            return len(self._tasks)

    def is_blocked(self, task_id: str) -> tuple[str, ...]:
        """
        算出一条任务当前被哪些**尚未完成**的前置任务挡着（spec F7）。

        :param task_id: 任务标识
        :returns: 未完成的前置任务 ID 元组；没被挡住（或任务不存在）时为空元组

        注意判据是「前置**未完成**」而不是「存在前置」：依赖一旦满足，
        `blocked_by` 字段仍然留着（它是历史事实），但阻塞效果消失。

        副作用：无。
        """
        with self._lock:
            return self._blockers_locked(str(task_id))

    # ------------------------------------------------------------------ #
    # 写
    # ------------------------------------------------------------------ #

    def create(self, subject: str, description: str = "") -> BoardTask:
        """
        新建一条任务（spec F6）。

        :param subject: 标题
        :param description: 说明
        :returns: 新任务的副本（含分配到的 `task_id`）

        新任务一律 `PENDING`、无认领人、无依赖——依赖由调用方随后用
        `add_dependency` 声明。**刻意不在这里一步到位**：依赖要做环检测，
        而环检测失败时若任务已经建好，就会留下一条半成品，
        调用方还得自己收拾。

        副作用：改内部状态。
        """
        with self._lock:
            task_id = str(self._next_id)
            self._next_id += 1
            task = BoardTask(
                task_id=task_id,
                subject=(subject or "").strip(),
                description=(description or "").strip(),
            )
            self._tasks[task_id] = task
            return replace(task)

    def update(
        self,
        task_id: str,
        *,
        state: Optional[TaskState] = None,
        subject: Optional[str] = None,
        description: Optional[str] = None,
        owner: Optional[str] = None,
    ) -> UpdateResult:
        """
        逐字段更新一条任务（spec F6）。

        :param task_id: 任务标识
        :param state: 新状态
        :param subject: 新标题
        :param description: 新说明
        :param owner: 新认领人。

            ⚠ **这条通路刻意只用于「放手」与「改派」，不用于抢占式认领。**
            要认领请用 `claim`——它在同一个临界区里做完「没人认领 → 归我」
            的检查与写入，而这里不做那个检查。两个队员同时走这条路
            会双双「认领成功」，而彼此都以为任务归自己。
        :returns: `UpdateResult`

        全部参数都是可选的，`None` 表示不动那个字段。
        任何一个字段变了就刷新 `updated_at`。

        副作用：改内部状态。
        """
        with self._lock:
            task = self._tasks.get(str(task_id))
            if task is None:
                return UpdateResult(ok=False, reason=self._not_found_locked(task_id))

            changed = False
            if state is not None and state is not task.state:
                task.state = state
                changed = True
                # 完成时自动清空认领人：任务已经交付，占着名字没有意义，
                # 而留着会让 `/tasks` 上「已完成 · 认领人 worker-a」这种行
                # 看起来像是它还在做。
                if state is TaskState.COMPLETED:
                    task.owner = ""
            if subject is not None and subject.strip() != task.subject:
                task.subject = subject.strip()
                changed = True
            if description is not None and description.strip() != task.description:
                task.description = description.strip()
                changed = True
            if owner is not None and owner.strip() != task.owner:
                task.owner = owner.strip()
                changed = True

            if changed:
                task.updated_at = time.time()
            return UpdateResult(ok=True, task=replace(task))

    def claim(self, task_id: str, owner: str) -> ClaimResult:
        """
        **原子认领**一条任务（spec F8，AC10）。

        :param task_id: 任务标识
        :param owner: 认领人名字
        :returns: `ClaimResult`，四种结果可区分

        ## ⚠ 三步必须在同一个临界区内

        ① 任务存在 ② 没被前置挡住 ③ 当前无人认领 —— 三者检查完**立即**写入。

        分开做（比如先 `is_blocked()` 再 `update(owner=...)`）会留下
        TOCTOU 窗口：两个队员几乎同时读到「没人认领」，然后双双写入，
        **双方都拿到成功**，各自以为任务归自己，于是同一件事被做两遍
        （或者更糟：两人改同一个文件互相覆盖）。

        这条正是 AC10 钉住的：20 个线程同时认领，必须**恰好一个**成功。

        副作用：成功时改内部状态。
        """
        owner = (owner or "").strip()
        if not owner:
            return ClaimResult(ok=False, reason="认领人名字不能为空。")

        with self._lock:
            task = self._tasks.get(str(task_id))
            if task is None:
                return ClaimResult(ok=False, reason=self._not_found_locked(task_id))

            blockers = self._blockers_locked(task.task_id)
            if blockers:
                return ClaimResult(
                    ok=False,
                    reason=(
                        f"任务 {task.task_id} 现在不能认领——它被这些尚未完成的"
                        f"任务挡着：{'、'.join(blockers)}。"
                        "请先做那几条，或者换一条没被挡住的任务。"
                    ),
                    blocked_by=blockers,
                )

            if task.owner and task.owner != owner:
                return ClaimResult(
                    ok=False,
                    reason=(
                        f"任务 {task.task_id} 已经被 {task.owner} 认领了。"
                        "换一条没人认领的任务，或者给 ta 发条消息协调一下。"
                    ),
                    current_owner=task.owner,
                )

            task.owner = owner
            task.state = TaskState.IN_PROGRESS
            task.updated_at = time.time()
            return ClaimResult(ok=True, current_owner=owner)

    def add_dependency(self, task_id: str, blocked_by_id: str) -> DepResult:
        """
        声明「`task_id` 被 `blocked_by_id` 挡着」（spec F7）。

        :param task_id: 被挡住的那条
        :param blocked_by_id: 挡路的那条
        :returns: `DepResult`；成环时 `reason` 含完整路径

        ## ⚠ 成对维护点：双向写入

        `task.blocked_by` 与 `other.blocks` **必须在同一个临界区内成对更新**。
        只存一边的话，每次列出清单都要遍历全表反查「谁挡着我」，
        而清单是每个队员每一轮都可能读的高频操作。

        ## 为什么必须做环检测

        spec「不做的事」排除的是**环检测之外**的图算法（优先级、时限、
        子任务树）。环检测本身必须做：两条任务互相挡着的话，
        **谁都认领不了，而清单上完全看不出原因**——用户看到的是
        「两条任务永远停在待办」，而每一条单独看都很正常。

        副作用：成功时改内部状态；**失败时一个字节都不改**（AC 明确要求
        被拒时状态没有被写脏）。
        """
        task_id = str(task_id)
        blocked_by_id = str(blocked_by_id)

        if task_id == blocked_by_id:
            return DepResult(
                ok=False, reason=f"任务 {task_id} 不能依赖它自己。"
            )

        with self._lock:
            task = self._tasks.get(task_id)
            if task is None:
                return DepResult(ok=False, reason=self._not_found_locked(task_id))
            other = self._tasks.get(blocked_by_id)
            if other is None:
                return DepResult(ok=False, reason=self._not_found_locked(blocked_by_id))

            if blocked_by_id in task.blocked_by:
                # 重复声明按成功处理（幂等）：模型重试同一个调用不该报错，
                # 那只会让它以为出了问题、再去做多余的补救。
                return DepResult(ok=True)

            cycle = self._find_cycle_locked(blocked_by_id, task_id)
            if cycle:
                return DepResult(
                    ok=False,
                    reason=(
                        "这条依赖会形成循环："
                        f"{' → '.join(cycle)}。"
                        "循环依赖会让圈里的每一条任务都永远认领不了。"
                        "请重新安排先后顺序。"
                    ),
                )

            task.blocked_by = task.blocked_by + (blocked_by_id,)
            other.blocks = other.blocks + (task_id,)
            now = time.time()
            task.updated_at = now
            other.updated_at = now
            return DepResult(ok=True)

    def remove(self, task_id: str) -> bool:
        """
        删除一条任务（spec F6）。

        :param task_id: 任务标识
        :returns: 是否真的删掉了（不存在时 `False`）

        ## ⚠ 必须摘除反向引用

        删掉 A 之后，凡是 `blocked_by` 里含 A 的任务都要把它摘掉。
        不摘的话会留下**指向不存在任务的悬空依赖**，而 `is_blocked` 会把
        「查不到的前置」当成「尚未完成」——那条任务**再也认领不了**，
        且清单上显示的阻塞来源是一个查无此条的编号，无从排查。

        副作用：改内部状态（可能改多条任务）。
        """
        task_id = str(task_id)
        with self._lock:
            if task_id not in self._tasks:
                return False
            del self._tasks[task_id]
            now = time.time()
            for other in self._tasks.values():
                if task_id in other.blocked_by:
                    other.blocked_by = tuple(
                        x for x in other.blocked_by if x != task_id
                    )
                    other.updated_at = now
                if task_id in other.blocks:
                    other.blocks = tuple(x for x in other.blocks if x != task_id)
                    other.updated_at = now
            return True

    def clear(self) -> None:
        """
        清空整份清单（spec F25：`/clear` / `/resume` / 退出）。

        编号也复位——新会话的第一条任务应该是 1 号。

        副作用：改内部状态。
        """
        with self._lock:
            self._tasks.clear()
            self._next_id = 1

    # ------------------------------------------------------------------ #
    # 内部（**全部要求调用方已持锁**）
    # ------------------------------------------------------------------ #

    def _blockers_locked(self, task_id: str) -> tuple[str, ...]:
        """
        算未完成的前置任务。**调用方必须已持锁。**

        查不到的前置**按「未完成」处理**（偏严）：宁可多挡一次，
        也不要因为一条查不到的依赖把任务放出去。正常情况下 `remove`
        已经摘干净了，走到这个分支说明有别的地方出了问题。
        """
        task = self._tasks.get(task_id)
        if task is None:
            return ()
        out: list[str] = []
        for dep_id in task.blocked_by:
            dep = self._tasks.get(dep_id)
            if dep is None or dep.state is not TaskState.COMPLETED:
                out.append(dep_id)
        return tuple(out)

    def _find_cycle_locked(self, start: str, target: str) -> tuple[str, ...]:
        """
        从 `start` 沿 `blocked_by` 深搜，看能不能走到 `target`。
        **调用方必须已持锁。**

        :param start: 起点（准备成为新前置的那条）
        :param target: 终点（准备被挡住的那条）
        :returns: 找到时返回路径 `(target, start, ..., target)` 便于诊断；
                  没找到时空元组

        语义：要给「`target` 被 `start` 挡着」加一条边之前，先看 `start`
        是不是（直接或间接）已经被 `target` 挡着。是的话就成环。

        用迭代式深搜而不是递归：清单条数虽小，但递归深度受栈限制，
        而这里没有任何理由承担那个风险。`seen` 同时防止已有环
        （理论上不该存在）把本函数拖进死循环。
        """
        stack: list[tuple[str, tuple[str, ...]]] = [(start, (start,))]
        seen: set[str] = set()
        while stack:
            node, path = stack.pop()
            if node == target:
                # 拼成「target → start → … → target」，读起来就是那个圈
                return (target,) + path
            if node in seen:
                continue
            seen.add(node)
            current = self._tasks.get(node)
            if current is None:
                continue
            for dep_id in current.blocked_by:
                stack.append((dep_id, path + (dep_id,)))
        return ()

    def _not_found_locked(self, task_id: str) -> str:
        """
        「任务不存在」的可读原因。**调用方必须已持锁。**

        **必须列出现有编号**：模型据此能自我纠正（用对的编号重试），
        只说「不存在」的话它只能猜——这与 C13 `_unknown_agent_text`
        是同一条经验。
        """
        existing = "、".join(
            t.task_id for t in sorted(self._tasks.values(), key=lambda t: t.sort_key)
        )
        tail = f"当前清单上的任务编号：{existing}。" if existing else "当前清单是空的。"
        return f"没有编号为 {task_id} 的任务。{tail}"


__all__ = ["ClaimResult", "DepResult", "TaskBoard", "UpdateResult"]
