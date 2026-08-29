"""
共享任务清单的单测（c15 T7/T8，覆盖 AC5–AC11）。

重点四处：**副本隔离**、**依赖强制阻断认领**、**环检测且失败不写脏**、
**并发认领恰好一个成功**。最后一条是本文件里最要紧的——它钉住的是
一个分开写就会静默出错的实现（TOCTOU 窗口下两个队员双双认领成功，
而彼此都以为任务归自己）。
"""

from __future__ import annotations

import threading
import unittest

from rhinecode.team.board import TaskBoard
from rhinecode.team.models import TaskState


class CreateAndReadTest(unittest.TestCase):
    def setUp(self) -> None:
        self.board = TaskBoard()

    def test_ids_are_sequential_from_one(self) -> None:
        """编号从 1 开始顺序分配——「优先做 ID 小的」这层提示依赖它。"""
        for subject in ("设计数据表", "写后端接口", "写前端页面"):
            self.board.create(subject)
        self.assertEqual([t.task_id for t in self.board.snapshot()], ["1", "2", "3"])

    def test_new_task_is_pending_and_unclaimed(self) -> None:
        task = self.board.create("做点事", "说明")
        self.assertIs(task.state, TaskState.PENDING)
        self.assertEqual(task.owner, "")
        self.assertEqual(task.blocked_by, ())
        self.assertEqual(task.blocks, ())

    def test_get_returns_a_copy(self) -> None:
        """
        `get` 返回副本：调用方改它改不到板内。

        没有这条隔离，一个在锁外慢慢渲染清单的调用方就能不经锁改到共享状态。
        """
        self.board.create("原标题")
        borrowed = self.board.get("1")
        borrowed.subject = "被改过的标题"
        self.assertEqual(self.board.get("1").subject, "原标题")

    def test_snapshot_returns_copies(self) -> None:
        self.board.create("原标题")
        for task in self.board.snapshot():
            task.subject = "改了"
        self.assertEqual(self.board.get("1").subject, "原标题")

    def test_get_missing_returns_none(self) -> None:
        self.assertIsNone(self.board.get("99"))

    def test_snapshot_sorted_by_number_not_insertion(self) -> None:
        """按编号排序，不是按字典插入顺序——删掉再新建之后两者会脱节。"""
        for i in range(12):
            self.board.create(f"t{i}")
        ids = [t.task_id for t in self.board.snapshot()]
        self.assertEqual(ids, [str(i) for i in range(1, 13)])


class UpdateAndRemoveTest(unittest.TestCase):
    def setUp(self) -> None:
        self.board = TaskBoard()
        self.board.create("甲")
        self.board.create("乙")

    def test_update_each_field(self) -> None:
        result = self.board.update(
            "1", subject="新标题", description="新说明", state=TaskState.IN_PROGRESS
        )
        self.assertTrue(result.ok)
        task = self.board.get("1")
        self.assertEqual(task.subject, "新标题")
        self.assertEqual(task.description, "新说明")
        self.assertIs(task.state, TaskState.IN_PROGRESS)

    def test_update_missing_task_lists_existing_ids(self) -> None:
        """
        失败原因必须列出现有编号——模型据此自我纠正。
        只说「不存在」的话它只能猜（与 C13 的 `_unknown_agent_text` 同一条经验）。
        """
        result = self.board.update("99", subject="x")
        self.assertFalse(result.ok)
        self.assertIn("1", result.reason)
        self.assertIn("2", result.reason)

    def test_completing_clears_owner(self) -> None:
        """完成即释放认领人，否则 `/tasks` 上会显示成「已完成 · 某某还在做」。"""
        self.board.claim("1", "worker-a")
        self.board.update("1", state=TaskState.COMPLETED)
        self.assertEqual(self.board.get("1").owner, "")

    def test_remove_strips_reverse_references(self) -> None:
        """
        删掉挡路的任务后，被挡的那条要能被认领。

        不摘除的话会留下指向不存在任务的悬空依赖，而 `is_blocked` 把
        「查不到的前置」按未完成处理——那条任务再也认领不了，
        且清单上显示的阻塞来源是一个查无此条的编号。
        """
        self.board.add_dependency("2", "1")
        self.assertEqual(self.board.get("2").blocked_by, ("1",))
        self.assertTrue(self.board.remove("1"))
        self.assertEqual(self.board.get("2").blocked_by, ())
        self.assertTrue(self.board.claim("2", "worker-a").ok)

    def test_remove_missing_returns_false(self) -> None:
        self.assertFalse(self.board.remove("99"))

    def test_ids_are_not_reused_after_removal(self) -> None:
        """
        编号只增不减。复用会让「刚才说的 2 号」在两个人嘴里指两条不同的任务。
        """
        self.board.remove("2")
        self.assertEqual(self.board.create("丙").task_id, "3")

    def test_clear_resets_numbering(self) -> None:
        self.board.clear()
        self.assertEqual(self.board.count(), 0)
        self.assertEqual(self.board.create("新的").task_id, "1")


class DependencyTest(unittest.TestCase):
    def setUp(self) -> None:
        self.board = TaskBoard()
        for name in ("甲", "乙", "丙"):
            self.board.create(name)

    def test_dependency_is_stored_both_ways(self) -> None:
        """
        ⚠ 成对维护点：`blocked_by` 与 `blocks` 双向冗余。
        只存一边的话每次列清单都要遍历全表反查，而清单是高频读。
        """
        self.assertTrue(self.board.add_dependency("2", "1").ok)
        self.assertEqual(self.board.get("2").blocked_by, ("1",))
        self.assertEqual(self.board.get("1").blocks, ("2",))

    def test_self_dependency_rejected(self) -> None:
        self.assertFalse(self.board.add_dependency("1", "1").ok)

    def test_repeated_declaration_is_idempotent(self) -> None:
        """重复声明按成功处理：模型重试同一个调用不该拿到错误。"""
        self.assertTrue(self.board.add_dependency("2", "1").ok)
        self.assertTrue(self.board.add_dependency("2", "1").ok)
        self.assertEqual(self.board.get("2").blocked_by, ("1",))

    def test_two_hop_cycle_rejected(self) -> None:
        self.board.add_dependency("2", "1")
        result = self.board.add_dependency("1", "2")
        self.assertFalse(result.ok)
        self.assertIn("循环", result.reason)

    def test_three_hop_cycle_rejected_with_path(self) -> None:
        """成环时必须给出路径，否则模型看不懂自己把哪几条连成了圈。"""
        self.board.add_dependency("2", "1")
        self.board.add_dependency("3", "2")
        result = self.board.add_dependency("1", "3")
        self.assertFalse(result.ok)
        self.assertIn("→", result.reason)
        for task_id in ("1", "2", "3"):
            self.assertIn(task_id, result.reason)

    def test_rejected_cycle_leaves_state_untouched(self) -> None:
        """
        ⚠ 被拒时**一个字节都不能改**。

        写了一半再回滚是另一种实现，但那要求回滚路径本身没有 bug；
        「检查通过才写」从结构上不需要回滚。
        """
        self.board.add_dependency("2", "1")
        self.board.add_dependency("3", "2")
        before = [(t.task_id, t.blocked_by, t.blocks) for t in self.board.snapshot()]
        self.board.add_dependency("1", "3")
        after = [(t.task_id, t.blocked_by, t.blocks) for t in self.board.snapshot()]
        self.assertEqual(before, after)

    def test_dependency_on_missing_task_rejected(self) -> None:
        self.assertFalse(self.board.add_dependency("1", "99").ok)
        self.assertFalse(self.board.add_dependency("99", "1").ok)


class BlockingAndClaimTest(unittest.TestCase):
    def setUp(self) -> None:
        self.board = TaskBoard()
        self.board.create("前置")
        self.board.create("后续")
        self.board.add_dependency("2", "1")

    def test_blocked_until_prerequisite_completed(self) -> None:
        """spec F7 / AC8：依赖是强制的，不只是提示。"""
        self.assertEqual(self.board.is_blocked("2"), ("1",))
        result = self.board.claim("2", "worker-a")
        self.assertFalse(result.ok)
        self.assertEqual(result.blocked_by, ("1",))
        self.assertIn("1", result.reason)

    def test_unblocked_after_prerequisite_completed(self) -> None:
        self.board.update("1", state=TaskState.COMPLETED)
        self.assertEqual(self.board.is_blocked("2"), ())
        self.assertTrue(self.board.claim("2", "worker-a").ok)

    def test_claim_sets_owner_and_in_progress(self) -> None:
        result = self.board.claim("1", "worker-a")
        self.assertTrue(result.ok)
        task = self.board.get("1")
        self.assertEqual(task.owner, "worker-a")
        self.assertIs(task.state, TaskState.IN_PROGRESS)

    def test_second_claim_reports_current_owner(self) -> None:
        """失败方必须知道是谁认领的——它据此决定去协调还是换一条做。"""
        self.board.claim("1", "worker-a")
        result = self.board.claim("1", "worker-b")
        self.assertFalse(result.ok)
        self.assertEqual(result.current_owner, "worker-a")
        self.assertIn("worker-a", result.reason)

    def test_reclaim_by_same_owner_succeeds(self) -> None:
        """同一个人重复认领是幂等的，不该报错。"""
        self.assertTrue(self.board.claim("1", "worker-a").ok)
        self.assertTrue(self.board.claim("1", "worker-a").ok)

    def test_claim_missing_task_lists_existing(self) -> None:
        result = self.board.claim("99", "worker-a")
        self.assertFalse(result.ok)
        self.assertIn("1", result.reason)

    def test_claim_requires_owner_name(self) -> None:
        self.assertFalse(self.board.claim("1", "  ").ok)

    def test_missing_prerequisite_blocks_conservatively(self) -> None:
        """
        查不到的前置按「未完成」处理（偏严）。

        正常情况下 `remove` 已经摘干净，走到这个分支说明别处出了问题——
        那时宁可多挡一次，也不要把任务放出去。
        """
        board = TaskBoard()
        board.create("孤儿")
        # 直接构造一个悬空依赖（绕过 add_dependency 的校验，模拟异常状态）
        with board._lock:  # noqa: SLF001 —— 刻意构造异常状态
            board._tasks["1"].blocked_by = ("404",)
        self.assertEqual(board.is_blocked("1"), ("404",))
        self.assertFalse(board.claim("1", "worker-a").ok)


class ConcurrencyTest(unittest.TestCase):
    """
    ⚠ 本类是整份清单里最要紧的护栏。

    `claim` 的三步检查（存在 / 未被挡住 / 无人认领）若不在同一个临界区内完成，
    就会留下 TOCTOU 窗口：两个队员几乎同时读到「没人认领」，然后双双写入，
    **双方都拿到成功**，各自以为任务归自己，于是同一件事被做两遍——
    或者更糟，两人改同一个文件互相覆盖。

    用 `Barrier` 让所有线程尽可能同时冲进 `claim`，放大那个窗口。
    """

    def test_exactly_one_claimer_wins(self) -> None:
        for _ in range(10):  # 重复 10 轮，压掉偶发性
            board = TaskBoard()
            board.create("抢它")
            barrier = threading.Barrier(20)
            results = []
            guard = threading.Lock()

            def worker(index: int) -> None:
                barrier.wait()
                result = board.claim("1", f"worker-{index}")
                with guard:
                    results.append(result)

            threads = [threading.Thread(target=worker, args=(i,)) for i in range(20)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()

            winners = [r for r in results if r.ok]
            self.assertEqual(len(winners), 1, "恰好一个线程能认领成功")
            owner = board.get("1").owner
            self.assertEqual(winners[0].current_owner, owner)
            for loser in (r for r in results if not r.ok):
                self.assertEqual(
                    loser.current_owner, owner, "失败方看到的认领人必须是真正的赢家"
                )

    def test_concurrent_writes_keep_board_consistent(self) -> None:
        """并发建任务 + 改状态之后，清单内容完整、编号无重复无丢失（AC11）。"""
        board = TaskBoard()
        barrier = threading.Barrier(10)

        def worker() -> None:
            barrier.wait()
            for _ in range(20):
                task = board.create("并发建的")
                board.update(task.task_id, state=TaskState.COMPLETED)

        threads = [threading.Thread(target=worker) for _ in range(10)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        tasks = board.snapshot()
        self.assertEqual(len(tasks), 200)
        self.assertEqual(len({t.task_id for t in tasks}), 200, "编号不重复")
        self.assertTrue(all(t.state is TaskState.COMPLETED for t in tasks))


class LockInvariantTest(unittest.TestCase):
    """
    N2 加锁不变量的**结构护栏**（与 C13 `TaskManager` 同形）。

    临界区只做纯内存读写，一切回调与跨线程调度在锁外。本类**刻意不持有
    任何回调**——没有可调的东西，就不可能在持锁时调它。

    违反的后果不是「偶尔慢一点」：锁内触发的回调若走到 Textual 的阻塞式
    `call_from_thread`，会与主线程组成确定性死锁、整个 TUI 冻结，
    而调用栈上没有任何线索（C11/C12/C13 三次同源事故）。
    """

    def test_board_holds_no_callables(self) -> None:
        board = TaskBoard()
        board.create("随便")
        for name, value in vars(board).items():
            self.assertFalse(
                callable(value),
                f"TaskBoard 不得持有可调用成员，但 {name} 是 {type(value).__name__}",
            )


if __name__ == "__main__":
    unittest.main()
