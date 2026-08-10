"""
后台任务表的单测（c13 T14，覆盖 AC16 / AC20c / AC25d）。

重点三处：**两条消费线互不影响**、**并发下不出竞态**、
**结构上不可能违反加锁不变量**（不持有回调）。
"""

from __future__ import annotations

import threading
import unittest

from rhinecode.subagents.tasks import (
    ACTIVITY_RECENT_LIMIT,
    BRANCH_AGENT_NAME,
    KIND_BRANCH,
    KIND_ROLE,
    TaskManager,
    TaskStatus,
)


class CreateTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tm = TaskManager()

    def test_ids_are_unique(self) -> None:
        ids = {self.tm.create(KIND_ROLE, "explorer", "t").task_id for _ in range(200)}
        self.assertEqual(len(ids), 200)

    def test_new_task_is_running(self) -> None:
        record = self.tm.create(KIND_ROLE, "explorer", "找点东西")
        self.assertIs(record.status, TaskStatus.RUNNING)
        self.assertFalse(record.done_event.is_set())
        self.assertFalse(record.cancel_event.is_set())
        self.assertEqual(record.turns, 0)

    def test_label_format(self) -> None:
        record = self.tm.create(KIND_BRANCH, BRANCH_AGENT_NAME, "t")
        self.assertEqual(record.label, f"{BRANCH_AGENT_NAME}[{record.task_id}]")


class BumpAndFinishTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tm = TaskManager()
        self.record = self.tm.create(KIND_ROLE, "explorer", "t")

    def test_turns_is_absolute_tokens_is_incremental(self) -> None:
        """
        轮次传新值、用量传增量——两者语义不同是刻意的。

        运行器直接知道跑到第几轮，而用量是每次请求返回一点、要累加。
        """
        self.tm.bump(self.record.task_id, turns=3, tokens=100)
        self.tm.bump(self.record.task_id, turns=4, tokens=50)

        self.assertEqual(self.record.turns, 4)
        self.assertEqual(self.record.usage_tokens, 150)

    def test_bump_on_unknown_id_is_silent(self) -> None:
        """未知标识静默忽略——抛异常会让运行器在收尾路径上炸掉。"""
        self.tm.bump("deadbe", turns=1)  # 不该抛

    def test_finish_sets_done_event(self) -> None:
        self.tm.finish(self.record.task_id, TaskStatus.COMPLETED, "结论")

        self.assertIs(self.record.status, TaskStatus.COMPLETED)
        self.assertEqual(self.record.conclusion, "结论")
        self.assertTrue(self.record.done_event.is_set())
        self.assertIsNotNone(self.record.finished_at)

    def test_finish_is_idempotent(self) -> None:
        """
        重复 `finish` 不覆盖。

        运行器的 `finally` 与异常兜底可能都会调到它——不幂等的话，
        第二次会把结论覆盖成空串，用户拿到一个「完成了但什么都没说」的任务。
        """
        self.tm.finish(self.record.task_id, TaskStatus.COMPLETED, "真结论")
        self.tm.finish(self.record.task_id, TaskStatus.FAILED, "")

        self.assertIs(self.record.status, TaskStatus.COMPLETED)
        self.assertEqual(self.record.conclusion, "真结论")

    def test_bump_after_finish_is_ignored(self) -> None:
        self.tm.finish(self.record.task_id, TaskStatus.COMPLETED, "x")
        self.tm.bump(self.record.task_id, turns=99)
        self.assertNotEqual(self.record.turns, 99)

    def test_duration_grows_then_freezes(self) -> None:
        running = self.record.duration_seconds
        self.assertGreaterEqual(running, 0.0)
        self.tm.finish(self.record.task_id, TaskStatus.COMPLETED, "x")
        frozen = self.record.duration_seconds
        self.assertEqual(frozen, self.record.duration_seconds)


class ConsumptionLinesTest(unittest.TestCase):
    """两条消费线：交付给模型、通知给用户，互不影响。"""

    def setUp(self) -> None:
        self.tm = TaskManager()
        self.record = self.tm.create(KIND_ROLE, "explorer", "t")
        self.tm.finish(self.record.task_id, TaskStatus.COMPLETED, "结论")

    def test_deliverables_are_idempotent(self) -> None:
        self.assertEqual(len(self.tm.take_deliverables()), 1)
        self.assertEqual(self.tm.take_deliverables(), ())

    def test_notifications_are_idempotent(self) -> None:
        self.assertEqual(len(self.tm.drain_notifications()), 1)
        self.assertEqual(self.tm.drain_notifications(), ())

    def test_two_lines_are_independent(self) -> None:
        """
        **本类最重要的一条**：先 drain 再 take，两者都拿得到。

        合成一条标志位的话，用户看到通知的那一刻结论就被标记成「已交付」，
        模型再也拿不到它——现象是「界面上说完成了，但 AI 完全不知道这回事」。
        """
        self.assertEqual(len(self.tm.drain_notifications()), 1)
        self.assertEqual(len(self.tm.take_deliverables()), 1)

    def test_running_task_is_in_neither_line(self) -> None:
        tm = TaskManager()
        tm.create(KIND_ROLE, "explorer", "t")
        self.assertEqual(tm.take_deliverables(), ())
        self.assertEqual(tm.drain_notifications(), ())

    def test_cancelled_and_failed_also_delivered(self) -> None:
        """
        失败与取消的任务**也要交付**。

        它们同样是终态、同样有一段说明文本。不交付的话，模型发起的委派
        会石沉大海——它永远等不到任何回音，也无从判断该不该重试。
        """
        for status in (TaskStatus.FAILED, TaskStatus.CANCELLED):
            with self.subTest(status=status):
                tm = TaskManager()
                r = tm.create(KIND_ROLE, "x", "t")
                tm.finish(r.task_id, status, "说明")
                self.assertEqual(len(tm.take_deliverables()), 1)


class QueryTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tm = TaskManager()

    def test_running_count(self) -> None:
        a = self.tm.create(KIND_ROLE, "a", "t")
        self.tm.create(KIND_ROLE, "b", "t")
        self.assertEqual(self.tm.running_count(), 2)

        self.tm.finish(a.task_id, TaskStatus.COMPLETED, "x")
        self.assertEqual(self.tm.running_count(), 1)

    def test_running_brief_lists_labels(self) -> None:
        a = self.tm.create(KIND_ROLE, "explorer", "t")
        brief = self.tm.running_brief()
        self.assertIn("explorer", brief)
        self.assertIn(a.task_id, brief)

    def test_running_brief_empty_when_idle(self) -> None:
        self.assertEqual(self.tm.running_brief(), "")

    def test_snapshot_preserves_creation_order(self) -> None:
        names = ["a", "b", "c"]
        for n in names:
            self.tm.create(KIND_ROLE, n, "t")
        self.assertEqual([r.agent_name for r in self.tm.snapshot()], names)


class CancelTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tm = TaskManager()
        self.record = self.tm.create(KIND_ROLE, "explorer", "t")

    def test_cancel_sets_signal_but_not_status(self) -> None:
        """
        取消只**置信号**，不直接改状态。

        真正的收尾由运行器做（它要在安全点停下、写结论、埋 trace）。
        这里直接标成已取消的话，运行器随后还会 finish 一次，
        同一个任务的结论会被写两遍。
        """
        self.assertTrue(self.tm.cancel(self.record.task_id))
        self.assertTrue(self.record.cancel_event.is_set())
        self.assertIs(self.record.status, TaskStatus.RUNNING)

    def test_cancel_unknown_or_finished_returns_false(self) -> None:
        self.assertFalse(self.tm.cancel("deadbe"))
        self.tm.finish(self.record.task_id, TaskStatus.COMPLETED, "x")
        self.assertFalse(self.tm.cancel(self.record.task_id))

    def test_cancel_all_counts_only_running(self) -> None:
        b = self.tm.create(KIND_ROLE, "b", "t")
        self.tm.finish(b.task_id, TaskStatus.COMPLETED, "x")
        self.tm.create(KIND_ROLE, "c", "t")

        self.assertEqual(self.tm.cancel_all(), 2)

    def test_cancel_all_on_empty(self) -> None:
        self.assertEqual(TaskManager().cancel_all(), 0)


class AwaitedFlagTest(unittest.TestCase):
    """
    `awaited` 标记：模型是否声明「这次我要这个结果」（c13 修订）。

    它替代了初版的 `backgrounded`。语义变了：那个是「等待方不等了」的**结果**，
    这个是模型在委派时的**声明**——决定 Agent Loop 收工前要不要停下来等它。
    """

    def setUp(self) -> None:
        self.tm = TaskManager()

    def test_defaults_to_awaited(self) -> None:
        """
        缺省为真。理由：模型不写 `background` 就是「我要这个结果」——
        缺省不等的话，一次普通委派的结论会石沉大海到下一条用户消息。
        """
        self.assertTrue(self.tm.create(KIND_ROLE, "a", "t").awaited)

    def test_can_be_cleared(self) -> None:
        record = self.tm.create(KIND_ROLE, "a", "t")
        record.awaited = False
        self.assertFalse(record.awaited)


class ConcurrencyTest(unittest.TestCase):
    """AC25d：并发下不出竞态。"""

    def test_twenty_threads_create_bump_finish(self) -> None:
        tm = TaskManager()
        errors: list[BaseException] = []
        barrier = threading.Barrier(20)

        def worker(i: int) -> None:
            try:
                barrier.wait()  # 尽量让 20 个线程同时冲进去
                record = tm.create(KIND_ROLE, f"agent{i}", "t")
                for turn in range(1, 6):
                    tm.bump(record.task_id, turns=turn, tokens=10)
                tm.finish(record.task_id, TaskStatus.COMPLETED, f"结论{i}")
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        self.assertEqual(errors, [])
        snapshot = tm.snapshot()
        self.assertEqual(len(snapshot), 20)
        self.assertTrue(all(r.status.is_terminal for r in snapshot))
        self.assertTrue(all(r.turns == 5 for r in snapshot))
        self.assertTrue(all(r.usage_tokens == 50 for r in snapshot))
        self.assertEqual(len({r.task_id for r in snapshot}), 20)

    def test_concurrent_consumption_delivers_each_exactly_once(self) -> None:
        """
        10 个线程同时 `take_deliverables`，每条结论**恰好**被取走一次。

        取重了会让同一段结论在主历史里出现两遍。
        """
        tm = TaskManager()
        for i in range(30):
            r = tm.create(KIND_ROLE, f"a{i}", "t")
            tm.finish(r.task_id, TaskStatus.COMPLETED, "x")

        collected: list[str] = []
        lock = threading.Lock()

        def consumer() -> None:
            taken = tm.take_deliverables()
            with lock:
                collected.extend(r.task_id for r in taken)

        threads = [threading.Thread(target=consumer) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        self.assertEqual(len(collected), 30)
        self.assertEqual(len(set(collected)), 30)


class NoteToolTest(unittest.TestCase):
    """
    `note_tool`：活动区那三个数字里的第三个（tui-display 扩展 F3/F5，AC3/AC25b）。

    ⚠ 它记的是**已完成**的调用，不是发起过的。口径由运行器那边保证
    （消费 `TOOL_RESULT` 而不是 `TOOL_START`），本类只验计数与有界。
    """

    def setUp(self) -> None:
        self.tm = TaskManager()
        self.record = self.tm.create(KIND_ROLE, "explorer", "调研")

    def test_counts_up(self) -> None:
        for i in range(3):
            self.tm.note_tool(self.record.task_id, f"Read(a{i}.py)")
        self.assertEqual(self.record.tool_calls, 3)

    def test_recent_keeps_order(self) -> None:
        self.tm.note_tool(self.record.task_id, "Grep(Layer)")
        self.tm.note_tool(self.record.task_id, "Read(engine.py)")
        self.assertEqual(self.record.recent_tools, ("Grep(Layer)", "Read(engine.py)"))

    def test_recent_is_bounded_but_the_count_is_not(self) -> None:
        """
        **两者刻意不同步**：最近列表有上限（展示用），计数没有（成本用）。

        把计数也截到 5 会让活动行显示「5 次调用」而实际跑了 20 次——
        那个数字的全部意义就在于反映真实规模。
        """
        for i in range(20):
            self.tm.note_tool(self.record.task_id, f"Read(a{i}.py)")
        self.assertEqual(self.record.tool_calls, 20)
        self.assertEqual(len(self.record.recent_tools), ACTIVITY_RECENT_LIMIT)
        # 保留的是**最近**的那几条，不是最早的
        self.assertEqual(self.record.recent_tools[-1], "Read(a19.py)")

    def test_unknown_id_is_ignored(self) -> None:
        """
        未知标识静默忽略，与 `bump` 同口径。

        运行器的事件消费循环可能在任务被清理之后才处理完最后几个事件，
        为此抛异常会让它在收尾路径上炸掉。
        """
        self.tm.note_tool("不存在", "Read(x)")  # 不抛异常即通过

    def test_terminal_task_is_ignored(self) -> None:
        """已结束的任务不再计数——终态的成本数字必须是定死的。"""
        self.tm.finish(self.record.task_id, TaskStatus.COMPLETED, "好了")
        self.tm.note_tool(self.record.task_id, "Read(x)")
        self.assertEqual(self.record.tool_calls, 0)

    def test_stores_strings_not_objects(self) -> None:
        """
        **结构护栏**：`recent_tools` 里只能是字符串。

        存 `ToolCall` 对象会诱导后来的人在**锁内**做参数摘要与 markup 转义
        ——那两件事都要调别处的函数，而本项目已经因为「临界区里做了不该做的事」
        死锁过四次。与 `worktree_path` 只存字符串是同一条理由。
        """
        self.tm.note_tool(self.record.task_id, "Read(a.py)")
        for item in self.record.recent_tools:
            self.assertIsInstance(item, str)


class LockInvariantTest(unittest.TestCase):
    """结构护栏：本类刻意不持有回调，从结构上杜绝「持锁时调回调」。"""

    def test_manager_holds_no_callables(self) -> None:
        """
        遍历实例属性，断言没有 callable 成员。

        本项目已经因为「持锁期间跨线程调度」踩过一次确定性死锁（C11 的
        `SkillManager`）。与其靠代码评审记住这条纪律，不如让它在结构上
        不可能发生——没有可调的东西，就不可能在持锁时调它。

        将来若真需要回调，删这条测试之前先想清楚：它要在锁外调。
        """
        tm = TaskManager()
        tm.create(KIND_ROLE, "x", "t")

        offenders = [
            name
            for name, value in vars(tm).items()
            if callable(value) and not isinstance(value, type)
        ]
        self.assertEqual(offenders, [], f"TaskManager 不该持有回调：{offenders}")


if __name__ == "__main__":
    unittest.main()
