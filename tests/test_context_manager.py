"""ContextManager 编排单测（c8 T14 / AC3/AC8/AC12）：摘要重构、锚点失效、熔断、manual、offload 先行。"""

import tempfile
import unittest
from pathlib import Path

from rhinecode.provider.base import Message, StreamChunk, ToolCall
from rhinecode.agent.events import Usage
from rhinecode.context.manager import ContextManager, MAX_SUMMARY_FAILURES
from rhinecode.context.offload import SINGLE_RESULT_TOKENS
from rhinecode.context.estimate import CHARS_PER_TOKEN


class FakeProvider:
    """可控假 provider：按预置 chunk 列表产出流；断言摘要请求不带工具。"""

    def __init__(self, chunks: list) -> None:
        self.chunks = chunks
        self.calls = 0

    def stream_chat(self, messages, thinking_effort="off", tools=None, system=None):
        self.calls += 1
        assert tools is None, "摘要请求禁止携带工具 schema（F11）"
        for c in self.chunks:
            yield c


def _big_history(n_pairs: int = 10) -> list:
    """构造 user/assistant 交替、每条 ~2000 字符的大历史（越过 RETAIN_TOKENS）。"""
    msgs = []
    for _ in range(n_pairs):
        msgs.append(Message(role="user", content="U" * 2000))
        msgs.append(Message(role="assistant", content="A" * 2000))
    return msgs


_OK_CHUNKS = [
    StreamChunk(type="text", content="分析草稿"),
    StreamChunk(type="text", content="<<<正式摘要>>> ① 任务目标：X"),
    StreamChunk(type="done"),
]
_ERR_CHUNKS = [StreamChunk(type="error", content="boom")]


class SummaryReconstructTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.store = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_success_reconstructs_and_invalidates_anchor(self) -> None:
        prov = FakeProvider(_OK_CHUNKS)
        cm = ContextManager(prov, "m", window=1000, store_dir=self.store)
        cm.record_usage(Usage(prompt_tokens=999), 3)  # 先埋一个锚点
        history = _big_history()
        before = len(history)
        notice = cm._do_summary(history)

        self.assertEqual(notice.kind, "summary")
        # 重构：早段被压成 [摘要, 边界]，尾部保留原文，总长应缩短
        self.assertLess(len(history), before)
        self.assertEqual(history[0].role, "user")
        self.assertEqual(history[1].role, "assistant")
        # 锚点失效（历史索引已变）
        self.assertIsNone(cm._anchor_tokens)

    def test_noop_when_nothing_to_summarize(self) -> None:
        prov = FakeProvider(_OK_CHUNKS)
        cm = ContextManager(prov, "m", window=1000, store_dir=self.store)
        small = [Message(role="user", content="hi"), Message(role="assistant", content="yo")]
        notice = cm._do_summary(small)
        self.assertEqual(notice.kind, "noop")
        self.assertEqual(prov.calls, 0)  # 没有可摘要早段，不该调用模型


class CircuitBreakerTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.store = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_three_failures_trips_breaker(self) -> None:
        prov = FakeProvider(_ERR_CHUNKS)
        cm = ContextManager(prov, "m", window=1000, store_dir=self.store)
        kinds = [cm._do_summary(_big_history()).kind for _ in range(MAX_SUMMARY_FAILURES)]
        self.assertEqual(kinds[-1], "circuit_break")
        self.assertTrue(cm._circuit_broken)

    def test_broken_skips_auto_summary(self) -> None:
        prov = FakeProvider(_ERR_CHUNKS)
        cm = ContextManager(prov, "m", window=1000, store_dir=self.store, auto_margin=100)
        for _ in range(MAX_SUMMARY_FAILURES):
            cm._do_summary(_big_history())
        self.assertTrue(cm._circuit_broken)
        calls_before = prov.calls
        # 已熔断：before_request 不应再触发摘要（不再新增 provider 调用）
        cm.before_request(_big_history())
        self.assertEqual(prov.calls, calls_before)

    def test_reset_clears_breaker(self) -> None:
        prov = FakeProvider(_ERR_CHUNKS)
        cm = ContextManager(prov, "m", window=1000, store_dir=self.store)
        for _ in range(MAX_SUMMARY_FAILURES):
            cm._do_summary(_big_history())
        cm.reset()
        self.assertFalse(cm._circuit_broken)
        self.assertEqual(cm._summary_failures, 0)
        self.assertIsNone(cm._anchor_tokens)


class ManualCompactTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.store = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_noop_when_lean(self) -> None:
        # 窗口极大 → 估算远低于「窗口 - 3K」→ noop
        cm = ContextManager(FakeProvider(_OK_CHUNKS), "m", window=1_000_000, store_dir=self.store)
        notice = cm.manual_compact(_big_history())
        self.assertEqual(notice.kind, "noop")
        self.assertIn("宽裕", notice.message)

    def test_compacts_when_over_manual_threshold(self) -> None:
        # 窗口小到估算超过「窗口 - 3K」→ 触发摘要
        cm = ContextManager(FakeProvider(_OK_CHUNKS), "m", window=3200, store_dir=self.store, manual_margin=3000)
        notice = cm.manual_compact(_big_history())
        self.assertEqual(notice.kind, "summary")


class OffloadBeforeSummaryTest(unittest.TestCase):
    """AC3：before_request 先 offload，降低估算，可能因此免去/减轻第二层摘要。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.store = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_offload_runs_and_lowers_estimate(self) -> None:
        prov = FakeProvider(_OK_CHUNKS)
        cm = ContextManager(prov, "m", window=100_000, store_dir=self.store)
        big_tool = Message(
            role="tool",
            content="B" * int((SINGLE_RESULT_TOKENS + 2000) * CHARS_PER_TOKEN),
            tool_call_id="c1",
        )
        history = [
            Message(role="user", content="q"),
            Message(role="assistant", content="", tool_calls=[ToolCall(id="c1", name="read_file", arguments={})]),
            big_tool,
        ]
        est_before = cm._estimate(history)
        notices = cm.before_request(history)
        est_after = cm._estimate(history)

        self.assertTrue(any(n.kind == "offload" for n in notices))
        self.assertLess(est_after, est_before)  # 存盘后估算下降
        self.assertTrue(history[2].content.startswith("[大型工具结果已存盘"))


class UsageReportTest(unittest.TestCase):
    def test_report_contains_fields(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            cm = ContextManager(FakeProvider(_OK_CHUNKS), "m", window=65536, store_dir=Path(d))
            report = cm.usage_report([Message(role="user", content="hi")])
            self.assertIn("估算", report)
            self.assertIn("65536", report)
            self.assertIn("余量", report)


if __name__ == "__main__":
    unittest.main()
