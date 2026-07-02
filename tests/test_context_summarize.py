"""第二层纯逻辑单测（c8 T14 / AC9/AC10/AC11）：保留边界、草稿丢弃、重构结构。"""

import unittest

from rhinecode.provider.base import Message, ToolCall
from rhinecode.context.summarize import (
    BOUNDARY_MESSAGE,
    MIN_RETAIN_MESSAGES,
    SUMMARY_MARKER,
    compute_retain_index,
    parse_summary,
    reconstruct,
    render_transcript,
)


class RetainIndexTest(unittest.TestCase):
    def _big(self, role: str, tid: str = None) -> Message:
        # 每条 ~2000 字符 ≈ 667 token，多条即可越过 RETAIN_TOKENS
        return Message(role=role, content="X" * 2000, tool_call_id=tid)

    def test_snaps_to_user_boundary(self) -> None:
        # user/assistant 交替、内容够大 → 边界落在 user 且 > 0
        msgs = []
        for _ in range(10):
            msgs.append(self._big("user"))
            msgs.append(self._big("assistant"))
        idx = compute_retain_index(msgs)
        self.assertGreater(idx, 0)
        self.assertEqual(msgs[idx].role, "user")

    def test_does_not_split_tool_pair(self) -> None:
        # 构造 assistant(tool_calls) + tool 结果紧邻，边界回退到 user 不应落在 tool 上
        msgs = [Message(role="user", content="U" * 2000)]
        for i in range(8):
            msgs.append(
                Message(role="assistant", content="A" * 2000,
                        tool_calls=[ToolCall(id=f"t{i}", name="x", arguments={})])
            )
            msgs.append(Message(role="tool", content="R" * 2000, tool_call_id=f"t{i}"))
            msgs.append(Message(role="user", content="U" * 2000))
        idx = compute_retain_index(msgs)
        self.assertEqual(msgs[idx].role, "user")

    def test_small_history_retains_all(self) -> None:
        # 全部很小（< RETAIN_TOKENS）→ 保留全部，无早段可摘要（idx==0）
        msgs = [Message(role="user", content="hi"), Message(role="assistant", content="yo")]
        self.assertEqual(compute_retain_index(msgs), 0)

    def test_min_retain_messages(self) -> None:
        # 即便按 token 想少留，也至少保留 MIN_RETAIN_MESSAGES 条 → idx <= n - 5
        msgs = []
        for _ in range(10):
            msgs.append(self._big("user"))
            msgs.append(self._big("assistant"))
        idx = compute_retain_index(msgs)
        self.assertLessEqual(idx, len(msgs) - MIN_RETAIN_MESSAGES)


class ParseSummaryTest(unittest.TestCase):
    def test_drops_draft_before_marker(self) -> None:
        text = "这是分析草稿，随便写\n" + SUMMARY_MARKER + "\n① 任务目标：X"
        self.assertEqual(parse_summary(text), "① 任务目标：X")

    def test_no_marker_uses_full_text(self) -> None:
        self.assertEqual(parse_summary("   只有正文  "), "只有正文")

    def test_empty_returns_none(self) -> None:
        self.assertIsNone(parse_summary(SUMMARY_MARKER + "   "))
        self.assertIsNone(parse_summary(""))
        self.assertIsNone(parse_summary(None))

    def test_last_marker_wins(self) -> None:
        # 草稿里也提到标记时，取最后一次标记之后的内容
        text = f"draft {SUMMARY_MARKER} 假摘要 {SUMMARY_MARKER} 真摘要"
        self.assertEqual(parse_summary(text), "真摘要")


class ReconstructTest(unittest.TestCase):
    def test_structure(self) -> None:
        retained = [Message(role="user", content="keep"), Message(role="assistant", content="ok")]
        rebuilt = reconstruct("SUMMARY", retained)
        self.assertEqual([m.role for m in rebuilt], ["user", "assistant", "user", "assistant"])
        # 第一条含摘要正文，第二条是边界提示
        self.assertIn("SUMMARY", rebuilt[0].content)
        self.assertEqual(rebuilt[1].content, BOUNDARY_MESSAGE)
        # 保留区原文在尾部
        self.assertEqual(rebuilt[2].content, "keep")


class RenderTranscriptTest(unittest.TestCase):
    def test_roles_rendered(self) -> None:
        msgs = [
            Message(role="user", content="问题"),
            Message(role="assistant", content="", tool_calls=[ToolCall(id="t1", name="grep", arguments={})]),
            Message(role="tool", content="结果", tool_call_id="t1"),
        ]
        out = render_transcript(msgs)
        self.assertIn("【用户】问题", out)
        self.assertIn("grep(t1)", out)
        self.assertIn("【工具结果·t1】结果", out)


if __name__ == "__main__":
    unittest.main()
