"""ContextManager 编排单测（c8 T14 / AC8/AC12）：摘要重构、锚点失效、熔断、manual、不到线不动历史。"""

import tempfile
import unittest
from pathlib import Path

from rhinecode.provider.base import Message, StreamChunk, ToolCall
from rhinecode.agent.events import Usage
from rhinecode.context.manager import ContextManager, MAX_SUMMARY_FAILURES
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
        cm = ContextManager(prov, "m", window=1000)
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
        cm = ContextManager(prov, "m", window=1000)
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
        cm = ContextManager(prov, "m", window=1000)
        kinds = [cm._do_summary(_big_history()).kind for _ in range(MAX_SUMMARY_FAILURES)]
        self.assertEqual(kinds[-1], "circuit_break")
        self.assertTrue(cm._circuit_broken)

    def test_broken_skips_auto_summary(self) -> None:
        prov = FakeProvider(_ERR_CHUNKS)
        cm = ContextManager(prov, "m", window=1000, auto_margin=100)
        for _ in range(MAX_SUMMARY_FAILURES):
            cm._do_summary(_big_history())
        self.assertTrue(cm._circuit_broken)
        calls_before = prov.calls
        # 已熔断：before_request 不应再触发摘要（不再新增 provider 调用）
        cm.before_request(_big_history())
        self.assertEqual(prov.calls, calls_before)

    def test_reset_clears_breaker(self) -> None:
        prov = FakeProvider(_ERR_CHUNKS)
        cm = ContextManager(prov, "m", window=1000)
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

    def test_noop_when_nothing_to_summarize(self) -> None:
        # 手动已无余量阈值：noop 只在「没有够旧的早段可摘要」时出现（物理约束，非拒绝）。
        # 小历史全部落在保留区 → _do_summary 直接 noop，且不调用模型。
        prov = FakeProvider(_OK_CHUNKS)
        cm = ContextManager(prov, "m", window=1_000_000)
        small = [Message(role="user", content="hi"), Message(role="assistant", content="yo")]
        notice = cm.manual_compact(small)
        self.assertEqual(notice.kind, "noop")
        self.assertIn("无可摘要", notice.message)
        self.assertEqual(prov.calls, 0)

    def test_compacts_regardless_of_headroom(self) -> None:
        # 阈值已移除：即便窗口极大、余量宽裕（旧逻辑会 noop），只要有够旧的早段就直接摘要。
        cm = ContextManager(FakeProvider(_OK_CHUNKS), "m", window=1_000_000)
        notice = cm.manual_compact(_big_history())
        self.assertEqual(notice.kind, "summary")


class NothingIsTouchedBelowTheLineTest(unittest.TestCase):
    """
    ⚠ **这一组取代了原来的 `OffloadBeforeSummaryTest`**（c8 第一层存盘，
    2026-09-17 整层删除）。

    原用例断言「一条超大工具结果会被就地换成占位、估算随之下降」。那个行为正是
    这次要去掉的东西：它按绝对阈值触发（合计 16 000 token），在 1 000 000 的窗口
    下相当于**用量到 1.6% 就开始删历史**，真实 trace 里模型因此连续十几轮拿不到
    自己刚读的文件。

    现在钉住的是反过来的不变量：**没到摘要触发线之前，历史一个字都不许动。**
    """

    def test_a_huge_tool_result_is_left_alone(self) -> None:
        """远低于触发线时，超大工具结果原样留在历史里。"""
        prov = FakeProvider(_OK_CHUNKS)
        cm = ContextManager(prov, "m", window=1_000_000)
        big = "B" * 200_000          # 约 67K token，远超旧的单条 4000 token 线
        history = [
            Message(role="user", content="q"),
            Message(
                role="assistant",
                content="",
                tool_calls=[ToolCall(id="c1", name="read_file", arguments={})],
            ),
            Message(role="tool", content=big, tool_call_id="c1"),
        ]
        est_before = cm._estimate(history)
        notices = cm.before_request(history)
        est_after = cm._estimate(history)

        self.assertEqual(notices, [], "没逼近窗口就不该有任何压缩动作")
        self.assertEqual(history[2].content, big, "工具结果被动过了")
        self.assertEqual(est_after, est_before, "估算不该因为压缩而变化")

    def test_history_is_untouched_at_half_the_window(self) -> None:
        """
        ⚠ **反证。** 用量到窗口一半时也一个字不许动。

        少了这条，把摘要触发线写成「窗口的 1%」照样能让上面那条通过
        （那条用的历史只有几万 token，1% 的线在 1M 窗口下是 10K——刚好还够不着）。
        """
        prov = FakeProvider(_OK_CHUNKS)
        cm = ContextManager(prov, "m", window=100_000)
        history = [Message(role="user", content="q")]
        for i in range(20):
            history.append(
                Message(
                    role="assistant",
                    content="",
                    tool_calls=[ToolCall(id=f"c{i}", name="read_file", arguments={})],
                )
            )
            history.append(Message(role="tool", content="B" * 7_500, tool_call_id=f"c{i}"))
        before = [m.content for m in history]

        self.assertGreater(cm._estimate(history), 40_000, "构造的历史应当到窗口一半左右")
        notices = cm.before_request(history)

        self.assertEqual(notices, [])
        self.assertEqual([m.content for m in history], before, "历史被改写了")


class UsageReportTest(unittest.TestCase):
    def test_report_contains_fields(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            cm = ContextManager(FakeProvider(_OK_CHUNKS), "m", window=65536)
            report = cm.usage_report([Message(role="user", content="hi")])
            self.assertIn("估算", report)
            self.assertIn("65536", report)
            self.assertIn("余量", report)


class StatusLineTest(unittest.TestCase):
    """状态栏一行摘要（c8 UI）：格式、低用量不高亮、越阈值高亮、熔断标记。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.store = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_lean_history_no_warn(self) -> None:
        cm = ContextManager(FakeProvider(_OK_CHUNKS), "m", window=65536)
        text, warn = cm.status_line([Message(role="user", content="hi")])
        self.assertTrue(text.startswith("上下文："))
        self.assertIn("%", text)
        self.assertIn("/", text)  # 形如 x/64K
        self.assertFalse(warn)

    def test_over_threshold_warns(self) -> None:
        # 窗口很小 → 大历史轻易越过 window*0.8 → 高亮
        cm = ContextManager(FakeProvider(_OK_CHUNKS), "m", window=500)
        _, warn = cm.status_line(_big_history())
        self.assertTrue(warn)

    def test_circuit_broken_marks_and_warns(self) -> None:
        prov = FakeProvider(_ERR_CHUNKS)
        cm = ContextManager(prov, "m", window=1_000_000)
        for _ in range(MAX_SUMMARY_FAILURES):
            cm._do_summary(_big_history())
        self.assertTrue(cm._circuit_broken)
        # 即便窗口极大（用量占比很低），熔断本身也应高亮并带上标记。
        # ⚠ 标记由 `⚠` 改成文字「已熔断」（tui-display 扩展 F28/F30）：
        # 状态栏是单色单行，一个符号说不清「熔断」是什么意思。
        text, warn = cm.status_line([Message(role="user", content="hi")])
        self.assertTrue(warn)
        self.assertIn("已熔断", text)


if __name__ == "__main__":
    unittest.main()
