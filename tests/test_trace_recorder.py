"""
记录器单测（trace T10）：序号连续性、fail-safe、并发护栏、死锁护栏、作用域、构造降级。

对应 spec AC5（不死锁）/ AC7（并发序列化）/ AC8（序号无重复无跳号）/ AC22（不阻断）。
"""

import json
import tempfile
import threading
import unittest
from pathlib import Path

from rhinecode.trace.models import SCOPE_MAIN, SCOPE_SUMMARY, TraceEventType
from rhinecode.trace.recorder import (
    NullRecorder,
    TraceRecorder,
    bind_scope,
    create_recorder,
)

T = TraceEventType


class RecorderTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.path = self.root / "sub" / "trace.jsonl"
        self.rec = TraceRecorder(self.path)
        # 每个用例都从主作用域起跑：thread-local 会在同一线程里跨用例残留
        bind_scope(SCOPE_MAIN)

    def tearDown(self) -> None:
        self.rec.close()
        bind_scope(SCOPE_MAIN)
        self._tmp.cleanup()

    def read_records(self) -> list[dict]:
        text = self.path.read_text(encoding="utf-8")
        return [json.loads(line) for line in text.splitlines() if line.strip()]


class SequenceTest(RecorderTestBase):
    def test_seq_increments_from_one(self) -> None:
        for i in range(3):
            self.rec.emit(T.USER_INPUT, text=f"m{i}")
        self.assertEqual([r["seq"] for r in self.read_records()], [1, 2, 3])

    def test_fixed_fields_present(self) -> None:
        self.rec.emit(T.USER_INPUT, text="hi")
        r = self.read_records()[0]
        self.assertEqual(r["type"], "user_input")
        self.assertEqual(r["scope"], SCOPE_MAIN)
        self.assertIn("ts", r)
        self.assertEqual(r["text"], "hi")

    def test_chinese_written_without_escaping(self) -> None:
        # spec N8：中文必须原样落盘。本模块立项动因之一就是一个中文编码问题，
        # 记录本身把中文编码坏掉是最讽刺的失败方式。
        self.rec.emit(T.UI_MESSAGE, text="中文消息")
        raw = self.path.read_text(encoding="utf-8")
        self.assertIn("中文消息", raw)
        self.assertNotIn("\\u4e2d", raw)

    def test_seq_stays_contiguous_after_write_failure(self) -> None:
        """写入失败时序号不推进，于是丢弃与「无跳号」相容（AC8 + AC22）。"""
        self.rec.emit(T.USER_INPUT, text="ok-1")

        class BrokenHandle:
            def write(self, _s):
                raise OSError("disk full")

            def flush(self):
                pass

            def close(self):
                pass

        real = self.rec._fh
        self.rec._fh = BrokenHandle()
        for _ in range(3):
            self.rec.emit(T.USER_INPUT, text="lost")
        self.rec._fh = real

        self.rec.emit(T.USER_INPUT, text="ok-2")
        records = self.read_records()
        self.assertEqual([r["seq"] for r in records], [1, 2])
        self.assertEqual([r["text"] for r in records], ["ok-1", "ok-2"])

    def test_seq_stays_contiguous_after_payload_failure(self) -> None:
        """负载构造/序列化失败同样不占号。"""
        self.rec.emit(T.USER_INPUT, text="ok-1")

        class Unserializable:
            def __str__(self):
                raise RuntimeError("boom")

        self.rec.emit(T.USER_INPUT, text=Unserializable())
        self.rec.emit_lazy(T.API_REQUEST, lambda: 1 / 0)  # factory 自己抛

        self.rec.emit(T.USER_INPUT, text="ok-2")
        self.assertEqual([r["seq"] for r in self.read_records()], [1, 2])


class FailSafeTest(RecorderTestBase):
    def test_emit_never_raises(self) -> None:
        """三类失败下 emit / emit_lazy 均不抛异常（AC22）。"""

        class Unserializable:
            def __str__(self):
                raise RuntimeError("boom")

        # ① 负载无法序列化
        self.rec.emit(T.USER_INPUT, bad=Unserializable())
        # ② factory 自身抛异常
        self.rec.emit_lazy(T.API_REQUEST, lambda: 1 / 0)
        # ③ 文件句柄已坏（先规矩地关掉真句柄，免得留下未关闭的文件告警）
        self.rec._fh.close()
        self.rec._fh = None  # type: ignore[assignment]
        self.rec.emit(T.USER_INPUT, text="x")
        self.rec.emit_lazy(T.USER_INPUT, lambda: {"text": "x"})
        # 走到这里就算通过：没有任何异常外泄

    def test_close_is_idempotent(self) -> None:
        self.rec.close()
        self.rec.close()  # 第二次不得抛
        # 关闭后再 emit 也不抛、不写
        self.rec.emit(T.USER_INPUT, text="after close")
        self.assertEqual(self.read_records(), [])


class ConcurrencyTest(RecorderTestBase):
    def test_parallel_emits_serialize_without_dup_or_gap(self) -> None:
        """N 个线程各 emit M 条，行数与序号集合必须精确（AC7 + AC8）。"""
        n_threads, per_thread = 8, 40
        start = threading.Barrier(n_threads)

        def worker(tid: int) -> None:
            start.wait()
            for i in range(per_thread):
                self.rec.emit(T.TOOL_EXECUTE, tool=f"t{tid}", i=i)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(n_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)
            self.assertFalse(t.is_alive(), "并发 emit 线程未在超时内结束")

        total = n_threads * per_thread
        records = self.read_records()
        # 每行都能独立解析 —— 证明没有两个线程的输出交错在同一行
        self.assertEqual(len(records), total)
        self.assertEqual({r["seq"] for r in records}, set(range(1, total + 1)))


class DeadlockGuardTest(RecorderTestBase):
    def test_readers_never_block_on_emit(self) -> None:
        """
        死锁护栏（AC5）：emit 大量进行中，**另一个线程**读取作用域与轮次总数必须能返回。

        为什么必须另起线程而不是在同一线程里读：`threading.Lock` 若换成 `RLock`，
        同线程重入是允许的，同线程版本会静默通过，护栏形同虚设。
        用「完成计数」而不是布尔标志判定，是为了确认读线程真的跑完了 N 次，
        而不是恰好在超时前刚进第一次。
        """
        stop = threading.Event()
        completed = [0]

        def reader() -> None:
            while not stop.is_set():
                self.rec.current_scope()
                self.rec.turn_total()
                self.rec.next_turn(SCOPE_MAIN)
                completed[0] += 1

        t = threading.Thread(target=reader, daemon=True)
        t.start()
        for i in range(300):
            self.rec.emit(T.AGENT_EVENT, i=i)
        stop.set()
        t.join(timeout=10)

        self.assertFalse(t.is_alive(), "读线程被 emit 的锁卡住了（疑似死锁）")
        self.assertGreater(completed[0], 0, "读线程一次都没跑完")


class ScopeTest(RecorderTestBase):
    def test_defaults_to_main_when_unset(self) -> None:
        self.assertEqual(self.rec.current_scope(), SCOPE_MAIN)

    def test_scope_context_restores_previous(self) -> None:
        with self.rec.scope("isolated:review"):
            self.assertEqual(self.rec.current_scope(), "isolated:review")
            self.rec.emit(T.API_REQUEST, turn=1)
            # 嵌套：内层退出后回到外层，而不是无条件回主作用域
            with self.rec.scope(SCOPE_SUMMARY):
                self.assertEqual(self.rec.current_scope(), SCOPE_SUMMARY)
            self.assertEqual(self.rec.current_scope(), "isolated:review")
        self.assertEqual(self.rec.current_scope(), SCOPE_MAIN)
        self.assertEqual(self.read_records()[0]["scope"], "isolated:review")

    def test_bind_scope_persists_until_rebound(self) -> None:
        self.rec.bind_scope(SCOPE_SUMMARY)
        self.rec.emit(T.API_REQUEST, turn=1)
        self.assertEqual(self.read_records()[0]["scope"], SCOPE_SUMMARY)
        self.rec.bind_scope(SCOPE_MAIN)
        self.assertEqual(self.rec.current_scope(), SCOPE_MAIN)

    def test_scope_is_thread_local(self) -> None:
        """thread-local 语义：另一个线程读不到本线程绑定的作用域。"""
        seen: list[str] = []

        def worker() -> None:
            seen.append(self.rec.current_scope())

        with self.rec.scope("isolated:x"):
            t = threading.Thread(target=worker)
            t.start()
            t.join(timeout=10)
        self.assertEqual(seen, [SCOPE_MAIN])

    def test_turn_counters_are_per_scope(self) -> None:
        self.assertEqual(self.rec.next_turn(SCOPE_MAIN), 1)
        self.assertEqual(self.rec.next_turn(SCOPE_MAIN), 2)
        self.assertEqual(self.rec.next_turn(SCOPE_SUMMARY), 1)
        self.assertEqual(self.rec.turn_total(), 3)

    def test_elapsed_is_non_negative(self) -> None:
        self.assertGreaterEqual(self.rec.elapsed(), 0.0)


class NullRecorderTest(unittest.TestCase):
    def test_disabled_flag(self) -> None:
        self.assertFalse(NullRecorder.enabled)
        self.assertTrue(TraceRecorder.enabled)

    def test_emit_lazy_does_not_call_factory(self) -> None:
        """零开销的兑现点：昂贵负载的构造函数压根不执行。"""
        calls = []

        def factory() -> dict:
            calls.append(1)
            return {}

        NullRecorder().emit_lazy(T.API_REQUEST, factory)
        self.assertEqual(calls, [])
        # 更强的证明：一个必抛的 factory 也不会炸
        NullRecorder().emit_lazy(T.API_REQUEST, lambda: 1 / 0)

    def test_all_methods_are_noop_and_safe(self) -> None:
        n = NullRecorder()
        n.emit(T.USER_INPUT, text="x")
        n.bind_scope("isolated:x")
        self.assertEqual(n.current_scope(), SCOPE_MAIN)
        with n.scope("isolated:x"):
            self.assertEqual(n.current_scope(), SCOPE_MAIN)
        self.assertEqual(n.next_turn(SCOPE_MAIN), 0)
        self.assertEqual(n.turn_total(), 0)
        self.assertEqual(n.elapsed(), 0.0)
        n.close()

    def test_null_is_not_subclass_of_real_recorder(self) -> None:
        # 刻意不继承：二者无共享实现，继承会让 Null 对象持有无用的文件句柄
        self.assertFalse(issubclass(NullRecorder, TraceRecorder))


class CreateRecorderTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_success_returns_real_recorder(self) -> None:
        rec = create_recorder(self.root / "t" / "x.jsonl")
        try:
            self.assertIsInstance(rec, TraceRecorder)
            self.assertTrue(rec.enabled)
        finally:
            # 必须在 tearDown 的临时目录清理之前关掉句柄：Windows 不允许
            # 删除仍被打开的文件（addCleanup 晚于 tearDown 执行，不能用）
            rec.close()

    def test_unwritable_path_degrades_to_null(self) -> None:
        """
        构造降级（AC22 的第三种情形）：父路径是个**普通文件**时不得让进程崩掉。

        该情形在 Windows 抛 FileExistsError、在 POSIX 抛 NotADirectoryError，
        故 create_recorder 的捕获范围必须是 OSError 而非某个具体子类。
        """
        blocker = self.root / "iam-a-file"
        blocker.write_text("x", encoding="utf-8")
        target = blocker / "trace.jsonl"

        rec = create_recorder(target)

        self.assertIsInstance(rec, NullRecorder)
        self.assertFalse(rec.enabled)
        self.assertFalse(target.exists())
        # 降级后照常可被调用（Null Object 的意义）
        rec.emit(T.SESSION_START, x=1)
        rec.close()

    def test_direct_construction_does_raise(self) -> None:
        """反证：直接构造确实会抛——这正是必须经工厂的理由。"""
        blocker = self.root / "another-file"
        blocker.write_text("x", encoding="utf-8")
        with self.assertRaises(OSError):
            TraceRecorder(blocker / "trace.jsonl")


if __name__ == "__main__":
    unittest.main()
