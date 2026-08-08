"""
无人值守轮的单测（c15 T35，覆盖 AC22–AC24）。

重点三处：
- **无人轮里确认面板一次都不弹**（AC22）；
- **正常轮里照常弹**（AC23，上一条的反证——证明改动是局部的）；
- **三条拒绝文案两两不同**（AC24，混用会让模型走岔路）。
"""

from __future__ import annotations

import threading
import unittest

from rhinecode.agent.loop import (
    DENIED_BY_USER_FEEDBACK,
    DENIED_NON_INTERACTIVE_FEEDBACK,
    DENIED_UNATTENDED_FEEDBACK,
    Agent,
    RunOptions,
)
from rhinecode.permission.engine import PermissionEngine
from rhinecode.permission.models import PermissionMode
from rhinecode.permission.rules import RuleSet
from rhinecode.provider.base import StreamChunk, ToolCall
from rhinecode.tools.base import Tool, ToolResult
from rhinecode.tools.registry import ToolRegistry


class _WriteTool(Tool):
    """一个会判 ASK 的假工具（非只读、缺省档下需要确认）。"""

    name = "write_file"
    description = "写文件"
    parameters = {
        "type": "object",
        "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
        "required": ["path"],
    }
    read_only = False
    workspace_aware = True

    def __init__(self) -> None:
        self.calls = 0

    def execute(self, args: dict, cwd=None) -> ToolResult:  # noqa: ARG002
        self.calls += 1
        return ToolResult(ok=True, output="写好了")


class _ToolThenStop:
    """第一轮要求调工具，第二轮收工。"""

    def __init__(self) -> None:
        self.round = 0

    def stream_chat(self, messages, thinking_effort, tools=None, system=None):
        self.round += 1
        if self.round == 1:
            yield StreamChunk(
                type="tool_call",
                tool_call=ToolCall(
                    id="c1",
                    name="write_file",
                    arguments={"path": "a.txt", "content": "x"},
                ),
            )
        else:
            yield StreamChunk(type="text", content="收工")


def _run(unattended: bool, ask_result: bool = True):
    """
    跑一轮真实的 Agent Loop，返回 (工具结果, ask 被调用的次数, 工具执行次数)。
    """
    provider = _ToolThenStop()
    tool = _WriteTool()
    registry = ToolRegistry()
    registry.register(tool)
    agent = Agent(provider, registry)

    ask_calls: list = []

    def ask(tc, t, decision) -> bool:  # noqa: ARG001
        ask_calls.append(tc.name)
        return ask_result

    outputs: list[str] = []
    for event in agent.run(
        [],
        "off",
        False,
        "",
        lambda: "",
        "fake",
        None,
        PermissionEngine(RuleSet(rules=[]), mode=PermissionMode.DEFAULT),
        ask,
        None,
        None,
        threading.Event(),
        None,
        None,
        options=RunOptions(
            interactive=not unattended,
            unattended=unattended,
        ),
    ):
        if event.tool_result is not None:
            outputs.append(event.tool_result.output)
    return outputs, len(ask_calls), tool.calls


class UnattendedRoundTest(unittest.TestCase):
    def test_no_panel_is_raised(self) -> None:
        """
        AC22：无人轮里判 ASK 的操作被自动拒绝，**确认面板一次都不弹**。
        """
        outputs, ask_calls, tool_calls = _run(unattended=True)
        self.assertEqual(ask_calls, 0, "无人轮不该调 ask 回调")
        self.assertEqual(tool_calls, 0, "被拒绝的工具不该执行")
        self.assertTrue(any("用户当前不在场" in o for o in outputs))

    def test_normal_round_still_raises_the_panel(self) -> None:
        """
        AC23：**反证**——同一个操作在正常轮里确认面板照常弹出。

        没有这条，「无人轮不弹面板」可能是因为整条 ASK 路径都坏了，
        而那会悄悄废掉 C6 第⑤层人在回路。
        """
        outputs, ask_calls, tool_calls = _run(unattended=False)
        self.assertEqual(ask_calls, 1, "正常轮必须弹面板")
        self.assertEqual(tool_calls, 1, "用户批准后工具应当执行")
        self.assertTrue(any("写好了" in o for o in outputs))

    def test_normal_round_denial_uses_the_user_wording(self) -> None:
        """正常轮里用户按拒绝 → 走「人做了决定」那条文案。"""
        outputs, _ask_calls, tool_calls = _run(unattended=False, ask_result=False)
        self.assertEqual(tool_calls, 0)
        self.assertTrue(any("用户的决定" in o for o in outputs))

    def test_unattended_keeps_tools_in_the_next_round(self) -> None:
        """
        ⚠ 无人轮的拒绝**不置 `user_denied`**。

        那个标志会让下一轮 `tools=None`，而无人轮的模型应当带着工具继续——
        回消息、读文件、改共享清单都是它现在做得到且该做的事。
        证据：第二轮仍然发生了（provider 被调了两次）。
        """
        provider = _ToolThenStop()
        tool = _WriteTool()
        registry = ToolRegistry()
        registry.register(tool)
        agent = Agent(provider, registry)
        list(
            agent.run(
                [], "off", False, "", lambda: "", "fake", None,
                PermissionEngine(RuleSet(rules=[]), mode=PermissionMode.DEFAULT),
                lambda *a: True, None, None, threading.Event(), None, None,
                options=RunOptions(interactive=False, unattended=True),
            )
        )
        self.assertEqual(provider.round, 2, "拒绝之后应当还有下一轮")


class FeedbackWordingTest(unittest.TestCase):
    """
    AC24：三条拒绝文案**两两不同**。

    混用不报错，只是模型走岔路：
    - 套用「人否决了」→ 主 Agent 以为被拒，直接放弃整件事；
    - 套用「子 Agent 环境」→ 它去「换只读方式达成」，而此刻真正该做的是
      把决定权留给回来的用户、同时别让队友干耗着。
    """

    ALL = (
        DENIED_BY_USER_FEEDBACK,
        DENIED_NON_INTERACTIVE_FEEDBACK,
        DENIED_UNATTENDED_FEEDBACK,
    )

    def test_all_three_are_distinct(self) -> None:
        self.assertEqual(len(set(self.ALL)), 3)

    def test_unattended_does_not_order_a_full_stop(self) -> None:
        """
        「停止推进 / 不要重试」属于**用户否决**那条。无人轮照搬它，
        主 Agent 会误以为被否决而放弃整件事。
        """
        self.assertIn("立即停止", DENIED_BY_USER_FEEDBACK)
        self.assertNotIn("立即停止", DENIED_UNATTENDED_FEEDBACK)

    def test_unattended_says_the_user_is_away_not_that_someone_refused(self) -> None:
        self.assertIn("不在场", DENIED_UNATTENDED_FEEDBACK)
        self.assertIn("不是有人拒绝了你", DENIED_UNATTENDED_FEEDBACK)

    def test_unattended_points_at_messaging_and_leaving_a_note(self) -> None:
        """
        无人轮特有的两条出路：**回消息给等着的队友**、
        **把需要批准的事留给回来的用户**。子 Agent 那条没有这两件事
        （它没有队友要安抚，也不直接面对用户）。
        """
        self.assertIn("回一条消息", DENIED_UNATTENDED_FEEDBACK)
        self.assertIn("用户回来", DENIED_UNATTENDED_FEEDBACK)
        self.assertNotIn("回一条消息", DENIED_NON_INTERACTIVE_FEEDBACK)

    def test_all_three_accept_the_tool_name(self) -> None:
        for template in self.ALL:
            with self.subTest(template=template[:20]):
                self.assertIn("{name}", template)


if __name__ == "__main__":
    unittest.main()
