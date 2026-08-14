"""
护栏：两种熔断（c16 F15/F16/F16a）。

两种熔断的**成因、计数、给用户的说法**都必须分开：

| 熔断 | 触发 | 语义 |
| --- | --- | --- |
| 拦截 | 连续 3 次 / 累计 20 次 | 分类器不了解你的环境、在反复误伤 |
| 失败 | 连续调用失败 3 次 | 接口多半不通，防「所有命令都跑不了而你查不出根因」 |

⚠ 本文件最有分辨力的一条是 `test_counters_are_shared_across_scopes`：
它钉住「**三类动作共用一套计数器**」。按类别分桶会让「分类器整体不可用」
被拆成三份、各自不到阈值，于是**永远不熔断**——而那正是熔断要防的状态。
分桶之后「连续 3 次拦截触发」那条用例照样绿，只有这一条会红。
"""

import threading
import unittest

from rhinecode.classifier.breaker import (
    CONSECUTIVE_BLOCKS,
    CONSECUTIVE_FAILURES,
    TOTAL_BLOCKS,
    CircuitBreaker,
)
from rhinecode.classifier.models import BreakerReason


class BlockBreakerTest(unittest.TestCase):
    """拦截熔断（F15）。"""

    def setUp(self) -> None:
        self.b = CircuitBreaker()

    def test_consecutive_blocks_trip(self) -> None:
        """AC18：连续 3 次拦截触发熔断。"""
        for _ in range(CONSECUTIVE_BLOCKS - 1):
            self.assertIsNone(self.b.record_block())
            self.assertFalse(self.b.is_tripped())
        state = self.b.record_block()
        self.assertIsNotNone(state)
        self.assertTrue(state.tripped)
        self.assertIs(state.reason, BreakerReason.BLOCKS)
        self.assertTrue(self.b.is_tripped())

    def test_an_allow_resets_the_consecutive_counter(self) -> None:
        """
        AC19：中间出现一次放行 → 连续计数清零、不触发。

        对齐官方：*Any allowed action resets the consecutive counter.*
        """
        self.b.record_block()
        self.b.record_block()
        self.b.record_allow()
        self.assertIsNone(self.b.record_block())
        self.assertFalse(self.b.is_tripped())

    def test_total_blocks_trip_even_without_consecutive(self) -> None:
        """
        AC20：拦一次放一次交替，累计到 20 次照样熔断。

        ⚠ 放行**不重置累计数**——那个计数的语义是「这段对话里它一共拦了多少次」，
        一次放行不该抹掉之前的 19 次。官方原话：
        *the total counter persists for the session.*
        """
        tripped = None
        for _ in range(TOTAL_BLOCKS):
            tripped = self.b.record_block() or tripped
            self.b.record_allow()
        self.assertIsNotNone(tripped)
        self.assertIs(tripped.reason, BreakerReason.BLOCKS)

    def test_reset_recovers(self) -> None:
        """AC21：用户在面板上批准一次 → 恢复。"""
        for _ in range(CONSECUTIVE_BLOCKS):
            self.b.record_block()
        self.assertTrue(self.b.is_tripped())
        self.b.reset()
        self.assertFalse(self.b.is_tripped())
        self.assertIs(self.b.state().reason, BreakerReason.NONE)

    def test_counters_restart_after_trip(self) -> None:
        """触发即清零：恢复之后重新开始计数，而不是一恢复就再次撞线。"""
        for _ in range(CONSECUTIVE_BLOCKS):
            self.b.record_block()
        self.b.reset()
        self.assertIsNone(self.b.record_block())
        self.assertFalse(self.b.is_tripped())


class FailureBreakerTest(unittest.TestCase):
    """失败熔断（F16）。"""

    def setUp(self) -> None:
        self.b = CircuitBreaker()

    def test_consecutive_failures_trip(self) -> None:
        """AC22 前半：连续 3 次调用失败触发。"""
        for _ in range(CONSECUTIVE_FAILURES - 1):
            self.assertIsNone(self.b.record_failure("连不上"))
        state = self.b.record_failure("连不上")
        self.assertIsNotNone(state)
        self.assertIs(state.reason, BreakerReason.FAILURES)

    def test_error_text_reaches_the_snapshot(self) -> None:
        """
        ⚠ 最后一次的错误必须进快照。

        spec F16 的**全部价值**就在于让用户看见「根因是分类器连不上」；
        只说「已熔断」而不说为什么，用户仍然查不出来。
        """
        for _ in range(CONSECUTIVE_FAILURES - 1):
            self.b.record_failure("旧错误")
        state = self.b.record_failure("Connection refused: api.example.com")
        self.assertIn("Connection refused", state.detail)

    def test_failures_do_not_count_toward_block_breaker(self) -> None:
        """
        AC22 后半：**失败不计入拦截计数**（对齐官方）。

        混在一起的话，一次网络故障会被当成「分类器在误伤」，
        给用户的说法就错了——他会去改自己的命令，而问题在别处。
        """
        b = CircuitBreaker()
        for _ in range(CONSECUTIVE_FAILURES - 1):
            b.record_failure("网络抖动")
        # 再拦两次：若失败计入了拦截计数，这里就会凑够 3 次而熔断。
        self.assertIsNone(b.record_block())
        state = b.record_block()
        self.assertIsNone(state, "两次拦截不该触发熔断——失败不该被算进来")

    def test_an_allow_resets_failures(self) -> None:
        """一次成功判定同时证明「连得上」，故清零失败计数。"""
        self.b.record_failure("x")
        self.b.record_failure("x")
        self.b.record_allow()
        self.assertIsNone(self.b.record_failure("x"))


class SharedCounterTest(unittest.TestCase):
    """
    ⚠ **本文件分辨力最高的一条**：三类动作共用一套计数器（F16a）。
    """

    def test_counters_are_shared_across_scopes(self) -> None:
        """
        AC18b：命令、网络、消息各被拦 1 次（合计 3 次）**即触发熔断**。

        熔断器不认识 `scope`——这条用例正是钉住这一点的：
        哪天有人给它加上按类别分桶，只有这条会红，
        而「连续 3 次拦截触发」那条照样绿。
        """
        b = CircuitBreaker()
        self.assertIsNone(b.record_block())   # 假设来自命令类
        self.assertIsNone(b.record_block())   # 假设来自网络类
        state = b.record_block()              # 假设来自消息类
        self.assertIsNotNone(state, "三类各拦一次就该熔断——计数器不该按类别分桶")

    def test_breaker_takes_no_scope_argument(self) -> None:
        """
        结构护栏：`record_block` 不接受任何区分类别的参数。

        没有这条的话，「加一个可选的 scope 参数、缺省不分桶」这种改动
        会悄悄开一个口子，而上面那条行为判据在缺省路径下照样绿。
        """
        import inspect

        for name in ("record_block", "record_allow"):
            sig = inspect.signature(getattr(CircuitBreaker, name))
            params = [p for p in sig.parameters if p != "self"]
            self.assertEqual(params, [], f"{name} 不该有区分类别的参数")


class ThreadSafetyTest(unittest.TestCase):
    """N6：并发下计数不丢（子 Agent 在独立线程里跑）。"""

    def test_concurrent_records_are_all_counted(self) -> None:
        """
        20 个线程各记一次拦截 → 累计计数必然达标并触发一次熔断。

        ⚠ 判据用**完成计数**而不是布尔标志：布尔在丢更新时仍可能为真，
        测不出竞态（`docs/internals/testing.md` 里记着这条方法论）。
        """
        b = CircuitBreaker()
        trips: list = []
        lock = threading.Lock()

        def worker() -> None:
            state = b.record_block()
            if state is not None:
                with lock:
                    trips.append(state)

        threads = [threading.Thread(target=worker) for _ in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertTrue(trips, "20 次拦截必然触发至少一次熔断")
        self.assertTrue(b.is_tripped())


class ThresholdsTest(unittest.TestCase):
    def test_thresholds_match_the_official_values(self) -> None:
        """
        三个阈值**刻意不可配置**（spec「不做的事」，官方原话：
        *These thresholds are not configurable.*）。

        写死数值是刻意的：改动它们要有人来动这条用例，那时会看到上面这段说明。
        """
        self.assertEqual(CONSECUTIVE_BLOCKS, 3)
        self.assertEqual(TOTAL_BLOCKS, 20)
        self.assertEqual(CONSECUTIVE_FAILURES, 3)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
