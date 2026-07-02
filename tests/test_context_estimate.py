"""上下文估算单测（c8 T14 / AC2）：无锚点全量求和、有锚点=锚点+增量、越界兜底。"""

import unittest

from rhinecode.provider.base import Message, ToolCall
from rhinecode.context.estimate import (
    CHARS_PER_TOKEN,
    MSG_OVERHEAD_TOKENS,
    estimate_message_tokens,
    estimate_tokens,
)


class EstimateMessageTest(unittest.TestCase):
    def test_plain_message(self) -> None:
        # 300 字符 / 3 + 4 开销 = 104
        msg = Message(role="user", content="x" * 300)
        self.assertEqual(estimate_message_tokens(msg), int(300 / CHARS_PER_TOKEN) + MSG_OVERHEAD_TOKENS)

    def test_tool_calls_counted(self) -> None:
        # tool_calls 的函数名与参数 JSON 也计入字符，故比纯空消息大
        empty = Message(role="assistant", content="")
        with_calls = Message(
            role="assistant",
            content="",
            tool_calls=[ToolCall(id="c1", name="read_file", arguments={"path": "a.py"})],
        )
        self.assertGreater(
            estimate_message_tokens(with_calls), estimate_message_tokens(empty)
        )

    def test_none_content_safe(self) -> None:
        # content 为 None 不应崩溃（防御性）
        msg = Message(role="tool", content=None, tool_call_id="c1")
        self.assertEqual(estimate_message_tokens(msg), MSG_OVERHEAD_TOKENS)


class EstimateTokensTest(unittest.TestCase):
    def setUp(self) -> None:
        self.history = [
            Message(role="user", content="a" * 30),
            Message(role="assistant", content="b" * 30),
            Message(role="tool", content="c" * 30, tool_call_id="c1"),
        ]

    def test_no_anchor_sums_all(self) -> None:
        expected = sum(estimate_message_tokens(m) for m in self.history)
        self.assertEqual(estimate_tokens(self.history, None, 0), expected)

    def test_anchor_plus_delta(self) -> None:
        # 锚点覆盖前 1 条，增量为后 2 条
        delta = sum(estimate_message_tokens(m) for m in self.history[1:])
        self.assertEqual(estimate_tokens(self.history, 1000, 1), 1000 + delta)

    def test_anchor_covers_all_no_delta(self) -> None:
        self.assertEqual(estimate_tokens(self.history, 5000, 3), 5000)

    def test_anchor_len_out_of_range_clamped(self) -> None:
        # anchor_len 越界（历史被截短）时按 min 兜底，增量为空 → 只剩锚点
        self.assertEqual(estimate_tokens(self.history, 5000, 99), 5000)


if __name__ == "__main__":
    unittest.main()
