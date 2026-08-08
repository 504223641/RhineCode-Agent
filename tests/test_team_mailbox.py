"""
消息投递的单测（c15 T15，覆盖 AC12–AC16 的数据面、AC35 的结构面）。

重点两处：**取走即置位的幂等性**（没有它，同一条消息会在收件人的每一轮
迭代里被注入一遍）、以及 **`Event.set()` 确实发生在锁外**——后者是本项目
第四次面对同一类死锁风险（C11 `SkillManager` / C12 `HookManager` /
C13 `TaskManager` 各踩过一次）。
"""

from __future__ import annotations

import threading
import time
import unittest

from rhinecode.team.mailbox import Mailbox
from rhinecode.team.models import MAIN_NAME, MemberState
from rhinecode.team.roster import Roster


def _build() -> tuple[Roster, Mailbox]:
    roster = Roster()
    roster.register("worker-a", "explorer")
    roster.register("worker-b", "explorer")
    return roster, Mailbox(roster)


class SendTest(unittest.TestCase):
    def setUp(self) -> None:
        self.roster, self.mailbox = _build()

    def test_message_reaches_recipient(self) -> None:
        result = self.mailbox.send("worker-a", "worker-b", "接口要改成异步的", "接口改异步")
        self.assertTrue(result.ok)
        received = self.mailbox.take_unread("worker-b")
        self.assertEqual(len(received), 1)
        self.assertEqual(received[0].sender, "worker-a")
        self.assertEqual(received[0].body, "接口要改成异步的")
        self.assertEqual(received[0].summary, "接口改异步")

    def test_can_send_to_main(self) -> None:
        self.assertTrue(self.mailbox.send("worker-a", MAIN_NAME, "需要你决定").ok)
        self.assertTrue(self.mailbox.has_unread(MAIN_NAME))

    def test_empty_body_rejected(self) -> None:
        self.assertFalse(self.mailbox.send("worker-a", "worker-b", "   ").ok)

    def test_sending_to_self_rejected(self) -> None:
        """通常是模型把「记笔记」误当成「发消息」，所以文案要指路去任务清单。"""
        result = self.mailbox.send("worker-a", "worker-a", "备忘")
        self.assertFalse(result.ok)
        self.assertIn("任务清单", result.reason)

    def test_unknown_recipient_lists_the_roster(self) -> None:
        """
        ⚠ 必须列出全部名字：模型据此用对的名字重试。
        只说「查无此人」的话它只能猜（与 C13 `_unknown_agent_text` 同一条经验）。
        """
        result = self.mailbox.send("worker-a", "查无此人", "喂")
        self.assertFalse(result.ok)
        for name in (MAIN_NAME, "worker-a", "worker-b"):
            self.assertIn(name, result.reason)

    def test_terminal_recipient_says_which_kind(self) -> None:
        """
        失败 / 取消 / 已退休是三种不同的处境，模型的下一步也不同。
        含糊成「对方不在了」会让它无从判断该不该改派。
        """
        self.roster.mark_terminal("worker-b", MemberState.RETIRED)
        result = self.mailbox.send("worker-a", "worker-b", "喂")
        self.assertFalse(result.ok)
        self.assertIn("已退休", result.reason)
        self.assertIn("委派", result.reason, "要提示改为委派一个新队员")

    def test_failed_recipient_reports_failure_label(self) -> None:
        self.roster.mark_terminal("worker-b", MemberState.FAILED)
        self.assertIn("失败", self.mailbox.send("worker-a", "worker-b", "喂").reason)

    def test_system_fills_timestamp_and_unread_flag(self) -> None:
        """
        ⚠ `sent_at` 由系统补、`read` 缺省假 —— 让模型给时间戳等于让它
        有机会给出一个假的顺序，而信箱是按到达顺序排的。
        """
        before = time.time()
        envelope = self.mailbox.send("worker-a", "worker-b", "x").envelope
        self.assertGreaterEqual(envelope.sent_at, before)
        self.assertFalse(envelope.read)


class SummaryTest(unittest.TestCase):
    def setUp(self) -> None:
        self.roster, self.mailbox = _build()

    def test_missing_summary_falls_back_to_first_line(self) -> None:
        """
        摘要是用户在界面上唯一看得到的一行。留空会让通知退化成
        「worker-a 发来一条消息」这种什么都没说的提示。
        """
        envelope = self.mailbox.send("worker-a", "worker-b", "第一行\n第二行").envelope
        self.assertEqual(envelope.summary, "第一行")

    def test_summary_is_flattened_to_one_line(self) -> None:
        """多行摘要会把界面的单行布局撑破。"""
        envelope = self.mailbox.send(
            "worker-a", "worker-b", "正文", "第一行\n第二行"
        ).envelope
        self.assertNotIn("\n", envelope.summary)

    def test_summary_is_truncated(self) -> None:
        envelope = self.mailbox.send("worker-a", "worker-b", "正文", "很长" * 200).envelope
        self.assertLessEqual(len(envelope.summary), 80)


class UnreadTest(unittest.TestCase):
    def setUp(self) -> None:
        self.roster, self.mailbox = _build()

    def test_take_unread_is_idempotent(self) -> None:
        """
        ⚠ 取走即置位。没有它，同一条消息会在收件人的**每一轮迭代**里
        都被注入一遍（与 C13 `take_deliverables` 同一条契约）。
        """
        for index in range(3):
            self.mailbox.send("worker-a", "worker-b", f"第 {index} 条")
        self.assertEqual(len(self.mailbox.take_unread("worker-b")), 3)
        self.assertEqual(self.mailbox.take_unread("worker-b"), ())

    def test_has_unread_does_not_consume(self) -> None:
        """
        ⚠ 只读：TUI 每 0.5 秒调它一次，有副作用的话消息会被轮询吃掉，
        而收件人永远看不到。
        """
        self.mailbox.send("worker-a", "worker-b", "x")
        for _ in range(5):
            self.assertTrue(self.mailbox.has_unread("worker-b"))
        self.assertEqual(len(self.mailbox.take_unread("worker-b")), 1)

    def test_unread_preserves_arrival_order(self) -> None:
        for index in range(5):
            self.mailbox.send("worker-a", "worker-b", f"m{index}")
        bodies = [env.body for env in self.mailbox.take_unread("worker-b")]
        self.assertEqual(bodies, [f"m{i}" for i in range(5)])

    def test_unknown_recipient_has_no_unread(self) -> None:
        self.assertFalse(self.mailbox.has_unread("查无此人"))
        self.assertEqual(self.mailbox.take_unread("查无此人"), ())

    def test_new_message_after_reading_is_unread_again(self) -> None:
        self.mailbox.send("worker-a", "worker-b", "第一条")
        self.mailbox.take_unread("worker-b")
        self.mailbox.send("worker-a", "worker-b", "第二条")
        received = self.mailbox.take_unread("worker-b")
        self.assertEqual([e.body for e in received], ["第二条"])


class WakeTest(unittest.TestCase):
    def setUp(self) -> None:
        self.roster, self.mailbox = _build()

    def test_idle_recipient_is_woken(self) -> None:
        self.roster.mark_idle("worker-b", [])
        event = self.roster.get("worker-b").wake_event
        self.assertFalse(event.is_set())
        self.mailbox.send("worker-a", "worker-b", "醒醒")
        self.assertTrue(event.is_set())

    def test_running_recipient_is_not_woken(self) -> None:
        """
        ⚠ 运行中的收件人走闸门（下一轮迭代注入），不走唤醒。
        两条路径混淆会让同一条消息被处理两遍。
        """
        event = self.roster.get("worker-b").wake_event
        self.mailbox.send("worker-a", "worker-b", "hi")
        self.assertFalse(event.is_set())


class LockInvariantTest(unittest.TestCase):
    """
    ⚠ 本类是本文件最要紧的护栏：证明 `Event.set()` 发生在**锁外**。

    落进临界区会与 Textual 阻塞式 `call_from_thread` 组成确定性死锁、
    整个 TUI 冻结，而调用栈上没有任何线索。本项目已在 C11/C12/C13
    踩过三次同源事故。

    ## 为什么同线程探测在这里是有效的

    `docs/internals/testing.md` 记着一条经验：死锁护栏「必须用完成计数
    而不是布尔标志，同线程版本在 `RLock` 下会静默通过」。

    那条经验的前提是 **`RLock` 可重入**——同一个线程持锁时再 acquire
    会直接成功，于是探测不到。本模块用的是**不可重入的 `threading.Lock`**，
    同线程持锁时 `acquire(blocking=False)` 返回 `False`，探测有效。

    `test_roster_lock_is_not_reentrant` 专门钉住这个前提：
    将来有人把 `Lock` 换成 `RLock`，那条会先红，
    提醒他本类的探测方式随之失效、必须改写。
    """

    def test_roster_lock_is_not_reentrant(self) -> None:
        roster = Roster()
        lock = roster._lock  # noqa: SLF001 —— 刻意探测内部同步原语
        self.assertTrue(lock.acquire(blocking=False))
        try:
            self.assertFalse(
                lock.acquire(blocking=False),
                "Roster 必须用不可重入的 Lock —— 换成 RLock 会让本文件的"
                "锁外探测静默失效",
            )
        finally:
            lock.release()

    def _probe(self, roster: Roster, name: str) -> list[bool]:
        """
        把某个队员的唤醒事件换成会记录「set 时锁是否空闲」的探针。

        :returns: 一个列表，每次 `set()` 追加一条「当时拿到锁了吗」

        拿得到锁 = 那一刻没人持锁 = `set()` 发生在临界区之外。
        """
        observations: list[bool] = []

        class ProbeEvent(threading.Event):
            def set(self_inner) -> None:  # noqa: N805
                acquired = roster._lock.acquire(blocking=False)  # noqa: SLF001
                observations.append(acquired)
                if acquired:
                    roster._lock.release()  # noqa: SLF001
                super().set()

        with roster._lock:  # noqa: SLF001
            roster._members[name].wake_event = ProbeEvent()  # noqa: SLF001
        return observations

    def test_deliver_sets_event_outside_the_lock(self) -> None:
        roster, mailbox = _build()
        roster.mark_idle("worker-b", [])
        observations = self._probe(roster, "worker-b")
        mailbox.send("worker-a", "worker-b", "醒醒")
        self.assertEqual(observations, [True], "投递的唤醒必须发生在锁外")

    def test_mark_terminal_sets_event_outside_the_lock(self) -> None:
        roster, _ = _build()
        roster.mark_idle("worker-b", [])
        observations = self._probe(roster, "worker-b")
        roster.mark_terminal("worker-b", MemberState.CANCELLED)
        self.assertEqual(observations, [True], "转终态的唤醒必须发生在锁外")

    def test_clear_sets_events_outside_the_lock(self) -> None:
        roster, _ = _build()
        roster.mark_idle("worker-b", [])
        observations = self._probe(roster, "worker-b")
        roster.clear()
        self.assertEqual(observations, [True], "清空时的唤醒必须发生在锁外")

    def test_retirement_sets_event_outside_the_lock(self) -> None:
        roster = Roster(max_idle=1)
        roster.register("w0", "explorer")
        roster.mark_idle("w0", [])
        observations = self._probe(roster, "w0")
        roster.register("w1", "explorer")
        roster.mark_idle("w1", [])
        self.assertEqual(observations, [True], "N3 降级的唤醒必须发生在锁外")

    def test_mailbox_holds_no_mutable_state(self) -> None:
        """
        `Mailbox` 自己不存数据（全部落在花名册里），因此不需要自己的锁，
        也就不可能违反加锁不变量。这条钉住那个结构选择。
        """
        _, mailbox = _build()
        for name, value in vars(mailbox).items():
            self.assertIsInstance(
                value, Roster, f"Mailbox 只应持有花名册，但 {name} 是 {type(value).__name__}"
            )


class ConcurrencyTest(unittest.TestCase):
    def test_concurrent_sends_lose_nothing(self) -> None:
        roster, mailbox = _build()
        barrier = threading.Barrier(8)

        def worker(index: int) -> None:
            barrier.wait()
            for seq in range(25):
                mailbox.send("worker-a", "worker-b", f"{index}-{seq}")

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        received = mailbox.take_unread("worker-b")
        self.assertEqual(len(received), 200)
        self.assertEqual(len({e.body for e in received}), 200, "没有丢失或重复")

    def test_concurrent_take_unread_delivers_each_message_once(self) -> None:
        """
        两个消费者同时取未读时，每条消息**只能被取走一次**——
        否则同一条消息会被注入历史两遍。
        """
        roster, mailbox = _build()
        for index in range(200):
            mailbox.send("worker-a", "worker-b", f"m{index}")

        barrier = threading.Barrier(8)
        collected: list[str] = []
        guard = threading.Lock()

        def consumer() -> None:
            barrier.wait()
            for _ in range(10):
                got = mailbox.take_unread("worker-b")
                with guard:
                    collected.extend(e.body for e in got)

        threads = [threading.Thread(target=consumer) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertEqual(len(collected), 200)
        self.assertEqual(len(set(collected)), 200, "每条消息只被取走一次")


if __name__ == "__main__":
    unittest.main()
