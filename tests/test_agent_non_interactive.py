"""
非交互执行模式的单测（c13 T10，覆盖 AC13a / AC13c）。

## 这条开关在解决什么

C13 的子 Agent 跑在非交互环境里（可能在后台，用户正在跟主对话说话），
确认面板压根弹不出来。最省事的做法是「让 ask 回调恒返回 False」——
**但那是错的**，它会走进既有的「用户拒绝」路径，而那条路径做了两件对
子 Agent 完全不适用的事：

1. 回灌 `DENIED_BY_USER_FEEDBACK`，文案写的是「**这是用户的决定**，
   不要重试、不要绕，去问用户为什么」——可子 Agent 面前没有用户可问；
2. 置 `_RoundContext.user_denied`，让**下一轮 `tools=None`**。
   子 Agent 从此手里没有任何工具，只能交一段「我做不了」的结论。

正确语义是「**没有人能做决定**，换条路继续」。因此新增 `RunOptions.interactive`
开关，走一条独立分支。

## 本模块的判据结构

正面判据（新行为对）+ **两条反证**（旧行为确实没被误触发 / 旧路径确实会那样）。
只验前者是不够的：`ask` 恒返回 False 也能让「调用被拒」这条断言通过。
"""

from __future__ import annotations

import threading
import unittest

from rhinecode.agent.loop import (
    DENIED_BY_USER_FEEDBACK,
    DENIED_NON_INTERACTIVE_FEEDBACK,
    OUTCOME_DENIED_NON_INTERACTIVE,
    Agent,
    RunOptions,
)
from rhinecode.permission.engine import PermissionEngine
from rhinecode.permission.models import PermissionMode
from rhinecode.permission.rules import RuleSet
from rhinecode.provider.base import BaseProvider, Message, StreamChunk, ToolCall
from rhinecode.tools.base import Tool, ToolResult
from rhinecode.tools.registry import ToolRegistry


class _Writer(Tool):
    """非只读工具 → 默认档下必然落进灰色地带、判 ASK。"""

    name = "write_file"
    description = "fake"
    parameters = {"type": "object", "properties": {"path": {"type": "string"}}}
    read_only = False

    def __init__(self) -> None:
        self.executed = 0

    def execute(self, args: dict) -> ToolResult:
        self.executed += 1
        return ToolResult(ok=True, output="written", summary="written")


class _RetryHappyProvider(BaseProvider):
    """
    只要手里有工具就一直发 write_file 的假模型。

    与 `test_perm_deny_stops_retry.py` 里那个是同一个角色：它不看回灌文案。
    正因为它不讲道理，才验得出「下一轮还有没有工具」这件事——
    有工具它就会再发一次，没有它就只能说话。
    """

    def __init__(self) -> None:
        self.tools_per_turn: list[object] = []
        self.calls = 0

    def stream_chat(self, messages, thinking_effort="off", tools=None, system=None):
        self.calls += 1
        self.tools_per_turn.append(tools)
        if tools:
            yield StreamChunk(
                type="tool_call",
                tool_call=ToolCall(
                    id=f"c{self.calls}",
                    name="write_file",
                    arguments={"path": f"attempt{self.calls}.txt"},
                ),
            )
            yield StreamChunk(type="done")
            return
        yield StreamChunk(type="text", content="我没有工具可用了。")
        yield StreamChunk(type="done")


class _AskSpy:
    """记录 ask 回调被调用了几次；恒返回传入的答复。"""

    def __init__(self, approve: bool = False) -> None:
        self.calls = 0
        self._approve = approve

    def __call__(self, tc, tool, decision) -> bool:
        self.calls += 1
        return self._approve


def _run(provider, tool, ask, *, interactive: bool, max_iterations: int = 25):
    registry = ToolRegistry()
    registry.register(tool)
    engine = PermissionEngine(RuleSet([]), mode=PermissionMode.DEFAULT)
    agent = Agent(provider, registry)
    history: list[Message] = [Message(role="user", content="写个文件")]
    return list(
        agent.run(
            history, "off", False, "", lambda: "", "model", None,
            engine, ask, None, None, threading.Event(),
            options=RunOptions(
                interactive=interactive, max_iterations=max_iterations
            ),
        )
    )


class FeedbackTextTest(unittest.TestCase):
    """文案的四层意思，按语义断言而不逐字固化（沿用既有测试的口径）。"""

    def test_names_the_tool(self) -> None:
        text = DENIED_NON_INTERACTIVE_FEEDBACK.format(name="write_file")
        self.assertIn("write_file", text)

    def test_says_nobody_can_answer(self) -> None:
        """① 说清这是环境问题，不是有人拒绝、也不是工具坏了。"""
        text = DENIED_NON_INTERACTIVE_FEEDBACK.format(name="write_file")
        self.assertIn("非交互", text)
        self.assertIn("不是有人拒绝", text)

    def test_forbids_identical_retry(self) -> None:
        """② 原样重试无意义（但**不禁止换条路**——这是与旧文案的关键差别）。"""
        text = DENIED_NON_INTERACTIVE_FEEDBACK.format(name="write_file")
        self.assertIn("重试", text)

    def test_says_other_write_tools_fail_too(self) -> None:
        """
        **真实模型验收后加的一条**：光说「别原样重试」不够。

        实测：general-purpose 被拒之后依次试了 `edit_file` → `edit_file` →
        `write_file` → `run_command` 才放弃——它遵守了「不原样重试」，
        但不知道**一个写工具被拒意味着同类工具都会被拒**。
        缺的是那条规则，不是劝告。
        """
        text = DENIED_NON_INTERACTIVE_FEEDBACK.format(name="write_file")
        self.assertIn("换一个工具也没用", text)

    def test_points_to_readonly_and_conclusion(self) -> None:
        """③④ 引导改用只读方式，或把这一步写进结论交给主对话。"""
        text = DENIED_NON_INTERACTIVE_FEEDBACK.format(name="write_file")
        self.assertIn("只读", text)
        self.assertIn("结论", text)

    def test_differs_from_user_denial_text(self) -> None:
        """
        **反证**：两段文案必须真的不同。

        它们解决的是相反的问题——一条要求「停止推进、去问用户」，
        另一条要求「换条路继续」。将来若有人图省事把两处合并，这条当场红。
        """
        a = DENIED_NON_INTERACTIVE_FEEDBACK.format(name="x")
        b = DENIED_BY_USER_FEEDBACK.format(name="x")
        self.assertNotEqual(a, b)
        # 旧文案要求「停止推进」，新文案不该有这个意思
        self.assertIn("停止本次任务的推进", b)
        self.assertNotIn("停止本次任务的推进", a)


class NonInteractiveDenyTest(unittest.TestCase):
    """非交互模式下 ASK 的处理。"""

    def test_ask_callback_never_invoked(self) -> None:
        """
        `ask` **一次都不会被调用**。

        这条比「调用被拒」重要得多：`ask` 在子 Agent 场景下会跨线程弹面板，
        真的走进去就是「后台线程往主界面弹了一个没人预期的面板」。
        判分支的位置必须排在调 `ask` 之前，这条钉住它。
        """
        spy = _AskSpy()
        _run(_RetryHappyProvider(), _Writer(), spy, interactive=False, max_iterations=3)
        self.assertEqual(spy.calls, 0)

    def test_feedback_is_the_non_interactive_text(self) -> None:
        """回灌的是新文案，不是 `DENIED_BY_USER_FEEDBACK`。"""
        events = _run(
            _RetryHappyProvider(), _Writer(), _AskSpy(),
            interactive=False, max_iterations=3,
        )
        results = [e.tool_result for e in events if e.tool_result is not None]
        self.assertTrue(results)
        self.assertIn("非交互", results[0].output)
        self.assertNotIn("这是用户的决定", results[0].output)
        self.assertFalse(results[0].ok)

    def test_tool_never_executes(self) -> None:
        tool = _Writer()
        _run(_RetryHappyProvider(), tool, _AskSpy(), interactive=False, max_iterations=3)
        self.assertEqual(tool.executed, 0)

    def test_next_turn_still_has_tools(self) -> None:
        """
        **本模块最重要的一条**：被拒之后的下一轮**仍然带工具**。

        子 Agent 应当改用只读方式继续干活，剥夺它的工具等于让它当场瘫掉。
        """
        provider = _RetryHappyProvider()
        _run(provider, _Writer(), _AskSpy(), interactive=False, max_iterations=3)

        self.assertGreaterEqual(len(provider.tools_per_turn), 2)
        self.assertTrue(provider.tools_per_turn[0], "第 1 轮本就该带工具")
        self.assertTrue(
            provider.tools_per_turn[1],
            "非交互拒绝**不该**触发 user_denied，下一轮必须仍带工具",
        )


class InteractiveUnchangedTest(unittest.TestCase):
    """反证 + 零回归：`interactive=True`（缺省）时行为逐字不变。"""

    def test_default_is_interactive(self) -> None:
        """缺省值即既有行为——不传这个字段的既有调用点一律不受影响。"""
        self.assertTrue(RunOptions().interactive)

    def test_interactive_true_takes_the_old_path(self) -> None:
        """
        **反证**：同一个场景在 `interactive=True` 下走的是旧路径——
        `ask` 被调用、回灌旧文案、下一轮 `tools` 为空。

        没有这一条，就无法区分「新分支生效了」与「两条路径恰好表现一样」。
        """
        spy = _AskSpy(approve=False)
        provider = _RetryHappyProvider()
        events = _run(provider, _Writer(), spy, interactive=True, max_iterations=3)

        self.assertEqual(spy.calls, 1, "交互模式下 ask 必须被调用")
        results = [e.tool_result for e in events if e.tool_result is not None]
        self.assertIn("这不是技术故障", results[0].output)
        self.assertGreaterEqual(len(provider.tools_per_turn), 2)
        self.assertFalse(
            provider.tools_per_turn[1], "交互模式下被拒的下一轮必须不带工具"
        )


class TraceOutcomeTest(unittest.TestCase):
    """新增的 outcome 取值必须真的被用上，否则读 trace 时分不出这一类。"""

    def test_outcome_constant_value(self) -> None:
        self.assertEqual(OUTCOME_DENIED_NON_INTERACTIVE, "denied_non_interactive")

    def test_outcome_recorded(self) -> None:
        class _Recorder:
            def __init__(self):
                self.outcomes = []

            def emit(self, *a, **k):
                pass

            def emit_lazy(self, event_type, build):
                payload = build()
                if "outcome" in payload:
                    self.outcomes.append(payload["outcome"])

            def scope(self, name):
                from contextlib import nullcontext
                return nullcontext()

            def current_scope(self):
                return "main"

            def bind_scope(self, name):
                pass

            def next_turn(self, scope):
                return 1

        rec = _Recorder()
        registry = ToolRegistry()
        registry.register(_Writer())
        engine = PermissionEngine(RuleSet([]), mode=PermissionMode.DEFAULT)
        agent = Agent(_RetryHappyProvider(), registry, recorder=rec)
        list(
            agent.run(
                [Message(role="user", content="写个文件")],
                "off", False, "", lambda: "", "model", None,
                engine, _AskSpy(), None, None, threading.Event(),
                options=RunOptions(interactive=False, max_iterations=2),
            )
        )
        self.assertIn(OUTCOME_DENIED_NON_INTERACTIVE, rec.outcomes)


if __name__ == "__main__":
    unittest.main()
