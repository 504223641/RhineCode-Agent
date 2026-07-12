"""第一层存盘单测（c8 T14 / AC4/AC5/AC6/AC7）：单结果/聚合存盘、user 不动、幂等。"""

import tempfile
import unittest
from pathlib import Path

from rhinecode.provider.base import Message
from rhinecode.context.estimate import CHARS_PER_TOKEN
from rhinecode.context.offload import (
    Offloader,
    SINGLE_RESULT_TOKENS,
    COMBINED_RESULT_TOKENS,
)


def _chars_for_tokens(tokens: int) -> int:
    """构造「估算约为 tokens」所需的字符数（略放大，确保跨过阈值）。"""
    return int(tokens * CHARS_PER_TOKEN) + 50


class OffloadTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.store = Path(self._tmp.name)
        self.off = Offloader(self.store)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _tool_msg(self, tid: str, tokens: int) -> Message:
        return Message(role="tool", content="A" * _chars_for_tokens(tokens), tool_call_id=tid)

    def test_single_result_offloaded_and_file_written(self) -> None:
        big = self._tool_msg("c1", SINGLE_RESULT_TOKENS + 500)
        original = big.content
        history = [Message(role="user", content="keep me"), big]
        notices = self.off.run(history)

        self.assertEqual([n.kind for n in notices], ["offload"])
        # 历史里该条变为占位（含路径提示），user 原文不变（AC6）
        self.assertTrue(history[1].content.startswith("[大型工具结果已存盘"))
        self.assertEqual(history[0].content, "keep me")
        # 磁盘落了完整原文（AC4）
        f = self.store / "c1.txt"
        self.assertTrue(f.exists())
        self.assertEqual(f.read_text(encoding="utf-8"), original)

    def test_small_result_untouched(self) -> None:
        small = self._tool_msg("c1", 10)
        history = [small]
        self.assertEqual(self.off.run(history), [])
        self.assertTrue(history[0].content.startswith("A"))  # 原文未改

    def test_idempotent(self) -> None:
        big = self._tool_msg("c1", SINGLE_RESULT_TOKENS + 500)
        history = [big]
        self.off.run(history)
        placeholder = history[0].content
        # 二次运行：不再处理、不产生重复文件、count 不增
        self.assertEqual(self.off.run(history), [])
        self.assertEqual(history[0].content, placeholder)
        self.assertEqual(self.off.count, 1)
        self.assertEqual(len(list(self.store.glob("*.txt"))), 1)

    def test_aggregate_offloads_largest_first(self) -> None:
        # 每条各自不超单阈值（<4000），但足够多条使合计超聚合阈值（>16000）；
        # 6 条 × ~3500 ≈ 21000 > 16000，且 3500 < SINGLE_RESULT_TOKENS。
        each = SINGLE_RESULT_TOKENS - 500
        self.assertLess(each, SINGLE_RESULT_TOKENS)
        history = [self._tool_msg(f"c{i}", each - i * 20) for i in range(6)]
        total_before = each * 6
        self.assertGreater(total_before, COMBINED_RESULT_TOKENS)

        notices = self.off.run(history)
        self.assertEqual([n.kind for n in notices], ["offload"])
        # c0 体积最大，应最先被存盘（挑大的先存）
        self.assertTrue(history[0].content.startswith("[大型工具结果已存盘"))
        # 至少存了 1 条，且未把所有条都存（存到合计达标即停）
        self.assertGreaterEqual(self.off.count, 1)
        remaining = [m for m in history if m.content.startswith("A")]
        self.assertTrue(remaining, "聚合存盘应在合计达标后停止，保留部分较小结果原文")

    def test_user_message_never_offloaded(self) -> None:
        huge_user = Message(role="user", content="U" * _chars_for_tokens(SINGLE_RESULT_TOKENS + 5000))
        history = [huge_user]
        self.assertEqual(self.off.run(history), [])
        self.assertTrue(history[0].content.startswith("U"))

    def test_write_failure_keeps_original(self) -> None:
        # store_dir 指向一个「已被占用为文件」的路径，mkdir 会失败 → 保留原文（N2）
        bad_file = Path(self._tmp.name) / "afile"
        bad_file.write_text("x", encoding="utf-8")
        off = Offloader(bad_file / "sub")  # 父级是文件，mkdir 必失败
        big = self._tool_msg("c1", SINGLE_RESULT_TOKENS + 500)
        original = big.content
        history = [big]
        notices = off.run(history)
        self.assertEqual(notices, [])
        self.assertEqual(history[0].content, original)  # 原文保留，不崩溃


if __name__ == "__main__":
    unittest.main()
