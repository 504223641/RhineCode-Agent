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
        idx = compute_retain_index(msgs, 65536)
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
        idx = compute_retain_index(msgs, 65536)
        self.assertEqual(msgs[idx].role, "user")

    def test_small_history_retains_all(self) -> None:
        # 全部很小（< RETAIN_TOKENS）→ 保留全部，无早段可摘要（idx==0）
        msgs = [Message(role="user", content="hi"), Message(role="assistant", content="yo")]
        self.assertEqual(compute_retain_index(msgs, 65536), 0)

    def test_min_retain_messages(self) -> None:
        # 即便按 token 想少留，也至少保留 MIN_RETAIN_MESSAGES 条 → idx <= n - 5
        msgs = []
        for _ in range(10):
            msgs.append(self._big("user"))
            msgs.append(self._big("assistant"))
        idx = compute_retain_index(msgs, 65536)
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


class RetainScalesWithWindowTest(unittest.TestCase):
    """
    **保留区与触发余量随窗口缩放**（已知项 #8，本轮修复）。

    ## 这组护栏钉的是一个实测缺陷

    `RETAIN_TOKENS` 与 `auto_margin` 原本是固定常量（10000 / 13000），不随
    `config.context_window` 变。后果在小窗口模型上是**第二层摘要完全失效**：

    - 保留区目标 10000 token **比整个 8192 窗口还大** → 早段恒为空 → 无可摘要；
    - 触发线 `window - auto_margin` = 8192 − 13000 = **负数** → 判据恒真
      → 每一次请求都尝试压缩、每一次都白跑。

    两件事叠加的结果是「看起来一直在压缩，实际一次都没压过」，历史一路涨到溢出。
    这在界面上完全看不出来——用户只看到上下文百分比一直涨。
    """

    def test_default_window_behaviour_is_unchanged(self) -> None:
        """
        65536 附近的窗口必须**逐字维持改造前的取值**。

        这是零回归的形式化表达：比例乘出来超过上限，被夹回原来的固定值。

        ⚠ **1000000 已从这条的清单里摘掉**（2026-09-17）：大窗口侧现在由
        `LARGE_WINDOW_MARGIN_RATIO` 主导，见下面那两条。两条规则在
        window ≈ 433 000 处交接，该点以下才谈得上「维持原值」。
        """
        from rhinecode.context.summarize import retain_budget, RETAIN_TOKENS_CAP
        from rhinecode.context.manager import _derive_margin, MARGIN_CAP

        for window in (65536, 131072, 262144):
            with self.subTest(window=window):
                self.assertEqual(retain_budget(window), RETAIN_TOKENS_CAP)
                self.assertEqual(_derive_margin(window), MARGIN_CAP)

    def test_large_window_triggers_near_the_upstream_line(self) -> None:
        """
        大窗口下触发点落在窗口的 97% 左右（对齐 Claude Code）。

        依据：Claude Code 在 1M 上下文的模型上约 967K 触发自动压缩（≈96.7%），
        Codex 的 `auto_compact_token_limit` 是窗口 × 90%。本项目取 97%。

        ⚠ 改造前 1M 窗口的触发点在 **98.7%**（余量停在固定的 13000），
        只剩 1.3% 的空间吸收估算误差——而本层用的是近似估算，误差的**绝对值**
        随历史长度增长。c8 第一层删除之后摘要是唯一的兜底，这个余量必须跟着长。
        """
        from rhinecode.context.manager import _derive_margin

        for window in (500_000, 1_000_000, 2_000_000):
            with self.subTest(window=window):
                trigger = (window - _derive_margin(window)) / window
                self.assertAlmostEqual(trigger, 0.97, places=2)

    def test_the_margin_has_no_upper_cap(self) -> None:
        """
        ⚠ **反证：窗口翻倍，余量必须跟着翻倍。**

        少了这条，给大窗口侧补一个 `min(..., 某个上限)` 照样能让上面那条通过
        ——只要那个上限大于 1M × 3%。而「余量不跟着窗口走」正是这次要改掉的
        缺陷本身（它此前把 1M 窗口的触发点顶到 98.7%），换个更大的数字造回来
        一样是错的。

        同一个形态在本项目已经出现过两次：已知项 #8（`RETAIN_TOKENS` 与
        `auto_margin` 固定值）与 c8 第一层存盘的绝对阈值。
        """
        from rhinecode.context.manager import _derive_margin

        base = _derive_margin(1_000_000)
        self.assertEqual(_derive_margin(2_000_000), base * 2)
        self.assertEqual(_derive_margin(4_000_000), base * 4)

    def test_small_window_scales_down(self) -> None:
        """小窗口下预算必须真的变小，否则早段永远为空。"""
        from rhinecode.context.summarize import retain_budget

        self.assertLess(retain_budget(8192), 8192, "保留预算不得大于整个窗口")
        self.assertLess(retain_budget(8192), retain_budget(65536))

    def test_trigger_line_is_always_positive(self) -> None:
        """
        `window - auto_margin` 必须恒为正。

        它一旦为负，触发判据就恒真——压缩在每一次请求上空转，
        而这**不产生任何错误**，只是悄悄浪费一轮判断。
        """
        from rhinecode.context.manager import _derive_margin

        for window in (1000000, 65536, 8192, 4096, 1024, 100, 2):
            with self.subTest(window=window):
                self.assertGreater(window - _derive_margin(window), 0)

    def test_small_window_actually_yields_an_early_segment(self) -> None:
        """
        **端到端判据**：同一段历史，大窗口下无早段可摘要，小窗口下必须有。

        只断言常量变小是不够的——真正要证明的是「第二层终于能压缩了」。
        """
        msgs = []
        for _ in range(40):
            msgs.append(Message(role="user", content="用户请求" * 60))
            msgs.append(Message(role="assistant", content="回答内容" * 60))

        self.assertEqual(
            compute_retain_index(msgs, 65536), 0,
            "这段历史在 64K 窗口下本就全部落在保留区，无早段——对照组",
        )
        self.assertGreater(
            compute_retain_index(msgs, 8192), 0,
            "同一段历史在 8K 窗口下必须切得出早段，否则修复没生效",
        )

    def test_min_messages_still_floors_it(self) -> None:
        """窗口小到离谱时，`MIN_RETAIN_MESSAGES` 仍保证近期上下文不被压没。"""
        msgs = [Message(role="user", content="x" * 500) for _ in range(20)]
        idx = compute_retain_index(msgs, 100)
        self.assertLessEqual(idx, len(msgs) - MIN_RETAIN_MESSAGES)
