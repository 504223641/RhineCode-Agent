"""
花名册的单测（c15 T12，覆盖 AC1–AC4、AC17–AC19、AC36）。

重点四处：**重名一律失败**（刻意偏离 Claude Code 的 latest wins）、
**待命保管历史 / 终态释放历史**、**N3 降级挑最久未活动的且返回值非空**、
**被降级或被清空的队员一定会被唤醒**（否则它的线程永远等下去）。
"""

from __future__ import annotations

import threading
import time
import unittest

from rhinecode.team.models import MAIN_NAME, MemberState
from rhinecode.team.roster import Roster


class RegisterTest(unittest.TestCase):
    def setUp(self) -> None:
        self.roster = Roster()

    def test_main_exists_from_the_start(self) -> None:
        """主对话也是花名册上的一员——投递路径因此不需要特判它。"""
        self.assertEqual(self.roster.names(), (MAIN_NAME,))
        self.assertTrue(self.roster.get(MAIN_NAME).is_main)

    def test_register_succeeds_and_is_running(self) -> None:
        result = self.roster.register("worker-a", "explorer")
        self.assertTrue(result.ok)
        self.assertEqual(result.name, "worker-a")
        self.assertIs(self.roster.get("worker-a").state, MemberState.RUNNING)

    def test_duplicate_name_fails(self) -> None:
        """
        ⚠ 刻意偏离 Claude Code 的「后来者接管」。

        latest wins 会让一条发给 `worker-a` 的消息静默送到另一个 Agent 手里，
        而两边都不报错。
        """
        self.roster.register("worker-a", "explorer")
        result = self.roster.register("worker-a", "explorer")
        self.assertFalse(result.ok)
        self.assertIn("worker-a", result.reason)

    def test_duplicate_failure_mentions_current_state(self) -> None:
        """失败原因要说清对方现在是什么状态——待命的话该发消息而不是重新委派。"""
        self.roster.register("worker-a", "explorer")
        self.roster.mark_idle("worker-a", [])
        result = self.roster.register("worker-a", "explorer")
        self.assertIn("待命", result.reason)
        self.assertIn("发消息", result.reason)

    def test_duplicate_check_covers_terminal_members(self) -> None:
        """终态队员也占着名字：否则新旧两个同名队员会在记录里混成一个。"""
        self.roster.register("worker-a", "explorer")
        self.roster.mark_terminal("worker-a", MemberState.FAILED)
        self.assertFalse(self.roster.register("worker-a", "explorer").ok)

    def test_main_is_a_reserved_name(self) -> None:
        result = self.roster.register(MAIN_NAME, "explorer")
        self.assertFalse(result.ok)
        self.assertIn(MAIN_NAME, result.reason)

    def test_auto_names_do_not_collide(self) -> None:
        first = self.roster.register(None, "explorer")
        second = self.roster.register(None, "explorer")
        self.assertTrue(first.ok and second.ok)
        self.assertNotEqual(first.name, second.name)
        self.assertEqual(first.name, "explorer-1")
        self.assertEqual(second.name, "explorer-2")

    def test_auto_name_fills_the_gap(self) -> None:
        """
        中间的编号被回收后，新队员补上那个空位而不是让编号无限增长。
        """
        self.roster.register(None, "explorer")   # explorer-1
        self.roster.register(None, "explorer")   # explorer-2
        roster2 = Roster()
        roster2.register("explorer-2", "explorer")
        self.assertEqual(roster2.register(None, "explorer").name, "explorer-1")

    def test_name_is_sanitized(self) -> None:
        """
        ⚠ 引号与尖括号必须去掉：名字会被嵌进注入消息的标记块属性里
        （`<teammate-message from="...">`），一个引号就能把标记块拆坏。
        """
        result = self.roster.register('bad"<name> here', "explorer")
        self.assertTrue(result.ok)
        for char in '"<>\' \t':
            self.assertNotIn(char, result.name)

    def test_name_that_sanitizes_to_nothing_fails(self) -> None:
        result = self.roster.register('"""', "explorer")
        self.assertFalse(result.ok)

    def test_read_only_flag_is_kept(self) -> None:
        """F24 判定用：在委派时算好存下来，避免 team 反向依赖 subagents。"""
        self.roster.register("ro", "explorer", read_only=True)
        self.roster.register("rw", "general", read_only=False)
        self.assertTrue(self.roster.get("ro").read_only)
        self.assertFalse(self.roster.get("rw").read_only)


class LifecycleTest(unittest.TestCase):
    def setUp(self) -> None:
        self.roster = Roster()
        self.roster.register("worker-a", "explorer")

    def test_idle_keeps_history(self) -> None:
        """待命保管历史——这是「叫醒就能接着干」的物理前提。"""
        self.roster.mark_idle("worker-a", ["m1", "m2"])
        entry = self.roster.get("worker-a")
        self.assertIs(entry.state, MemberState.IDLE)
        self.assertTrue(entry.state.is_wakeable)
        self.assertEqual(entry.history, ["m1", "m2"])

    def test_wake_returns_history_and_resumes_running(self) -> None:
        self.roster.mark_idle("worker-a", ["m1", "m2"])
        history = self.roster.wake("worker-a")
        self.assertEqual(history, ["m1", "m2"])
        self.assertIs(self.roster.get("worker-a").state, MemberState.RUNNING)

    def test_wake_clears_the_event(self) -> None:
        """
        ⚠ 醒来必须清掉事件，否则下一次待命会立刻返回、队员进入空转。
        """
        self.roster.mark_idle("worker-a", [])
        self.roster.get("worker-a").wake_event.set()
        self.roster.wake("worker-a")
        self.assertFalse(self.roster.get("worker-a").wake_event.is_set())

    def test_terminal_releases_history_and_blocks_wake(self) -> None:
        self.roster.mark_idle("worker-a", ["m1"])
        self.roster.mark_terminal("worker-a", MemberState.CANCELLED)
        entry = self.roster.get("worker-a")
        self.assertEqual(entry.history, [])
        self.assertFalse(entry.state.is_wakeable)
        self.assertIsNone(self.roster.wake("worker-a"))

    def test_terminal_wakes_a_waiting_member(self) -> None:
        """
        ⚠ 待命线程阻塞在自己的事件上。转终态若不唤醒它，那个线程会一直等下去。
        """
        self.roster.mark_idle("worker-a", [])
        event = self.roster.get("worker-a").wake_event
        self.assertFalse(event.is_set())
        self.roster.mark_terminal("worker-a", MemberState.CANCELLED)
        self.assertTrue(event.is_set())

    def test_wake_unknown_returns_none(self) -> None:
        self.assertIsNone(self.roster.wake("查无此人"))

    def test_running_member_is_not_wakeable(self) -> None:
        """
        运行中的队员不该走唤醒路径——它的消息会经闸门在下一轮迭代注入。
        两条路径混淆会让一条消息被注入两遍。
        """
        self.assertIsNone(self.roster.wake("worker-a"))

    def test_main_is_never_marked_idle_or_terminal(self) -> None:
        """主对话的生命周期不归花名册管。"""
        self.roster.mark_idle(MAIN_NAME, ["x"])
        self.assertIs(self.roster.get(MAIN_NAME).state, MemberState.RUNNING)
        self.roster.mark_terminal(MAIN_NAME, MemberState.FAILED)
        self.assertIs(self.roster.get(MAIN_NAME).state, MemberState.RUNNING)

    def test_mark_terminal_rejects_non_terminal_state(self) -> None:
        with self.assertRaises(AssertionError):
            self.roster.mark_terminal("worker-a", MemberState.IDLE)


class RetirementTest(unittest.TestCase):
    """spec N3 / AC36：待命队员有上限，超限时最久未活动的被降级。"""

    def _fill(self, roster: Roster, count: int) -> list[tuple[str, ...]]:
        returned = []
        for index in range(count):
            name = f"w{index}"
            roster.register(name, "explorer")
            time.sleep(0.002)  # 拉开 last_active，使「最久未活动」可判定
            returned.append(roster.mark_idle(name, [f"h{index}"]))
        return returned

    def test_under_limit_retires_nobody(self) -> None:
        roster = Roster(max_idle=3)
        self.assertEqual(self._fill(roster, 3), [(), (), ()])
        self.assertEqual(roster.idle_count(), 3)

    def test_over_limit_retires_least_recently_active(self) -> None:
        roster = Roster(max_idle=3)
        returned = self._fill(roster, 5)
        self.assertEqual(returned[3], ("w0",))
        self.assertEqual(returned[4], ("w1",))
        self.assertIs(roster.get("w0").state, MemberState.RETIRED)
        self.assertIs(roster.get("w1").state, MemberState.RETIRED)
        self.assertEqual(roster.idle_count(), 3, "待命人数维持在上限")

    def test_retired_member_loses_history_and_cannot_wake(self) -> None:
        roster = Roster(max_idle=1)
        self._fill(roster, 2)
        self.assertEqual(roster.get("w0").history, [])
        self.assertIsNone(roster.wake("w0"))

    def test_retired_member_is_woken_so_its_thread_can_exit(self) -> None:
        """
        ⚠ 被降级的队员正阻塞在事件上。不唤醒它，那个 daemon 线程要到
        进程退出才结束。醒来后它调 `wake()` 拿到 `None`，据此干净退出。
        """
        roster = Roster(max_idle=1)
        roster.register("w0", "explorer")
        roster.mark_idle("w0", [])
        event = roster.get("w0").wake_event
        roster.register("w1", "explorer")
        roster.mark_idle("w1", [])
        self.assertTrue(event.is_set())

    def test_retirement_result_is_not_silent(self) -> None:
        """
        spec N3 明令降级必须看得见：方法把名字**返回**给调用方去通知用户。
        这条用例钉住「返回值非空」，调用方是否真的展示由集成测试覆盖。
        """
        roster = Roster(max_idle=1)
        returned = self._fill(roster, 2)
        self.assertTrue(returned[-1], "超限时必须返回被降级的名字")


class ClearTest(unittest.TestCase):
    def test_clear_leaves_only_main(self) -> None:
        roster = Roster()
        roster.register("worker-a", "explorer")
        roster.register("worker-b", "explorer")
        roster.clear()
        self.assertEqual(roster.names(), (MAIN_NAME,))

    def test_clear_wakes_idle_members(self) -> None:
        """
        ⚠ 抹掉册子不会让待命线程醒来。不唤醒它们，`/clear` 之后那些线程
        会挂在那儿直到进程退出。
        """
        roster = Roster()
        roster.register("worker-a", "explorer")
        roster.mark_idle("worker-a", [])
        event = roster.get("worker-a").wake_event
        roster.clear()
        self.assertTrue(event.is_set())


class SnapshotTest(unittest.TestCase):
    def test_main_comes_first(self) -> None:
        roster = Roster()
        roster.register("worker-a", "explorer")
        self.assertEqual(roster.snapshot()[0].name, MAIN_NAME)

    def test_snapshot_returns_copies(self) -> None:
        roster = Roster()
        roster.register("worker-a", "explorer")
        for entry in roster.snapshot():
            entry.history.append("污染")
        self.assertEqual(roster.get("worker-a").history, [])


class ConcurrencyTest(unittest.TestCase):
    def test_only_one_thread_gets_a_contested_name(self) -> None:
        roster = Roster()
        barrier = threading.Barrier(16)
        results = []
        guard = threading.Lock()

        def worker() -> None:
            barrier.wait()
            result = roster.register("抢名字", "explorer")
            with guard:
                results.append(result)

        threads = [threading.Thread(target=worker) for _ in range(16)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertEqual(len([r for r in results if r.ok]), 1)

    def test_auto_naming_never_collides_under_concurrency(self) -> None:
        """
        ⚠ 生成候选名与占用它必须在同一个临界区内。

        分两步做（先 `suggest_name` 再 `register`）会让两个并发委派拿到
        同一个候选名，然后一个失败——而它本来只是想要「一个不冲突的名字」。
        """
        roster = Roster()
        barrier = threading.Barrier(16)
        names = []
        guard = threading.Lock()

        def worker() -> None:
            barrier.wait()
            result = roster.register(None, "explorer")
            with guard:
                names.append(result.name if result.ok else None)

        threads = [threading.Thread(target=worker) for _ in range(16)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertNotIn(None, names, "自动命名不该失败")
        self.assertEqual(len(set(names)), 16, "16 个名字必须互不相同")


class LockInvariantTest(unittest.TestCase):
    """N2 结构护栏：不持有回调 → 不可能在持锁时调它（与 C13 同形）。"""

    def test_roster_holds_no_callables(self) -> None:
        roster = Roster()
        roster.register("worker-a", "explorer")
        for name, value in vars(roster).items():
            self.assertFalse(
                callable(value),
                f"Roster 不得持有可调用成员，但 {name} 是 {type(value).__name__}",
            )


if __name__ == "__main__":
    unittest.main()
