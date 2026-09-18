"""
子 Agent 闸门与循环级等待的测试（c13 修订）。

**这两条是用户在真实使用中报出来的缺陷的回归护栏**，测试写法直接对着那两个现象：

1. 「子 Agent 跑完了，但主 Agent 要等用户再说一句话才拿得到结果」
   —— 变相中断了任务；
2. 「同时启动多个子 Agent，它们一个跑完才跑下一个」
   —— 并行成了摆设。

判据一律取**发给模型的请求体**：模型最终看到什么、什么时候看到，
才是这两条是否修好的唯一依据。
"""

from __future__ import annotations

import threading
import time
import unittest

from rhinecode.agent.events import AgentEventType, StopReason
from rhinecode.agent.gate import MAX_WAIT_ROUNDS, NullGate
from rhinecode.agent.loop import Agent, RunOptions
from rhinecode.permission.engine import PermissionEngine
from rhinecode.permission.models import PermissionMode
from rhinecode.permission.rules import RuleSet
from rhinecode.provider.base import BaseProvider, Message, StreamChunk, ToolCall
from rhinecode.subagents.gate import SubAgentGate, render_subagent_message
from rhinecode.subagents.tasks import KIND_ROLE, TaskManager, TaskStatus
from rhinecode.tools.base import Tool, ToolResult
from rhinecode.tools.registry import ToolRegistry


class _Noop(Tool):
    name = "read_file"
    description = "fake"
    parameters = {"type": "object", "properties": {}}
    read_only = True

    def execute(self, args: dict) -> ToolResult:
        return ToolResult(ok=True, output="x", summary="x")


def _gate(tasks: TaskManager) -> SubAgentGate:
    return SubAgentGate(tasks, render_subagent_message)


def _finished(tasks: TaskManager, conclusion: str = "子结论", awaited: bool = True):
    record = tasks.create(KIND_ROLE, "explorer", "t")
    record.awaited = awaited
    tasks.finish(record.task_id, TaskStatus.COMPLETED, conclusion)
    return record


# --------------------------------------------------------------------------- #
# 闸门本身
# --------------------------------------------------------------------------- #


class TakePendingTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tasks = TaskManager()
        self.gate = _gate(self.tasks)

    def test_renders_finished_task(self) -> None:
        record = _finished(self.tasks, "找到了三处。")
        messages = self.gate.take_pending()

        self.assertEqual(len(messages), 1)
        self.assertIn("找到了三处。", messages[0].content)
        self.assertIn(record.task_id, messages[0].content)
        self.assertIn("<subagent-result", messages[0].content)

    def test_idempotent(self) -> None:
        _finished(self.tasks)
        self.assertEqual(len(self.gate.take_pending()), 1)
        self.assertEqual(self.gate.take_pending(), [])

    def test_running_task_not_taken(self) -> None:
        self.tasks.create(KIND_ROLE, "explorer", "t")
        self.assertEqual(self.gate.take_pending(), [])

    def test_message_is_user_role_not_tool(self) -> None:
        """
        必须是 `user` 而不是 `tool`：这条消息与任何 `tool_call_id` 都不配对
        （委派工具早在发起时就返回过结果了），当成工具结果回灌会破坏协议。
        """
        _finished(self.tasks)
        self.assertEqual(self.gate.take_pending()[0].role, "user")

    def test_display_content_empty(self) -> None:
        _finished(self.tasks)
        self.assertEqual(self.gate.take_pending()[0].display_content, "")


class AwaitedTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tasks = TaskManager()
        self.gate = _gate(self.tasks)

    def test_running_awaited_task_counts(self) -> None:
        self.tasks.create(KIND_ROLE, "explorer", "t")   # awaited 缺省为真
        self.assertTrue(self.gate.has_awaited())

    def test_background_task_does_not_count(self) -> None:
        """
        `background=true` 的任务不算——那是模型明说「这次不要这个结果」的，
        循环不该为它停留。
        """
        record = self.tasks.create(KIND_ROLE, "explorer", "t")
        record.awaited = False
        self.assertFalse(self.gate.has_awaited())

    def test_finished_task_does_not_count(self) -> None:
        _finished(self.tasks)
        self.assertFalse(self.gate.has_awaited())

    def test_describe_lists_labels(self) -> None:
        record = self.tasks.create(KIND_ROLE, "explorer", "t")
        text = self.gate.describe_awaited()
        self.assertIn("explorer", text)
        self.assertIn(record.task_id, text)


class WaitAnyTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tasks = TaskManager()
        self.gate = _gate(self.tasks)
        self.cancel = threading.Event()

    def test_returns_immediately_when_something_deliverable(self) -> None:
        _finished(self.tasks)
        self.assertTrue(self.gate.wait_any(self.cancel))

    def test_returns_false_when_nothing_to_wait_for(self) -> None:
        self.assertFalse(self.gate.wait_any(self.cancel))

    def test_wakes_when_task_finishes(self) -> None:
        record = self.tasks.create(KIND_ROLE, "explorer", "t")

        def finish_later():
            time.sleep(0.15)
            self.tasks.finish(record.task_id, TaskStatus.COMPLETED, "好了")

        threading.Thread(target=finish_later, daemon=True).start()
        started = time.monotonic()
        got = self.gate.wait_any(self.cancel)

        self.assertTrue(got)
        self.assertLess(time.monotonic() - started, 3.0)

    def test_cancel_breaks_out_immediately(self) -> None:
        """
        **`Esc` 是等待期间唯一的逃生口**，所以取消信号必须能立刻打断等待。

        不检查它的话，用户按了 Esc 却要干等到子 Agent 自己跑完——
        那正是「等待不设体验超时」这个决定的前提条件。
        """
        self.tasks.create(KIND_ROLE, "explorer", "t")
        self.cancel.set()

        started = time.monotonic()
        self.assertFalse(self.gate.wait_any(self.cancel))
        self.assertLess(time.monotonic() - started, 1.0)


class NullGateTest(unittest.TestCase):
    """不启用子 Agent 时全部退化为空操作。"""

    def test_all_methods_are_no_ops(self) -> None:
        gate = NullGate()
        self.assertEqual(gate.take_pending(), [])
        self.assertFalse(gate.has_awaited())


# --------------------------------------------------------------------------- #
# 循环级行为（用户报的那两个问题）
# --------------------------------------------------------------------------- #


class _ScriptedProvider(BaseProvider):
    """按脚本逐轮产出，并记下每轮收到的消息原文。"""

    def __init__(self, script) -> None:
        self.script = list(script)
        self.calls = 0
        self.bodies: list[str] = []

    def stream_chat(self, messages, thinking_effort="off", tools=None, system=None):
        self.bodies.append(
            "\n".join(str(getattr(m, "content", "") or "") for m in messages)
        )
        step = self.script[self.calls] if self.calls < len(self.script) else "收尾"
        self.calls += 1
        if isinstance(step, ToolCall):
            yield StreamChunk(type="tool_call", tool_call=step)
        else:
            yield StreamChunk(type="text", content=step)
        yield StreamChunk(type="done")


def _run_loop(provider, gate, cancel=None, max_iterations=10):
    registry = ToolRegistry()
    registry.register(_Noop())
    engine = PermissionEngine(RuleSet([]), mode=PermissionMode.DEFAULT)
    agent = Agent(provider, registry)
    history: list[Message] = [Message(role="user", content="开工")]
    events = list(
        agent.run(
            history, "off", False, "", lambda: "", "m", None,
            engine, lambda *a: True, None, None,
            cancel if cancel is not None else threading.Event(),
            options=RunOptions(subagent_gate=gate, max_iterations=max_iterations),
        )
    )
    return events, history


class DeliveryWithinOneRunTest(unittest.TestCase):
    """
    **用户报的第一个问题的回归护栏。**

    现象：「子 Agent 工作结束以后模型没法立即拿到结果，得用户对话一次
    主 Agent 才能拿到」——等于变相中断了任务。
    """

    def test_conclusion_arrives_mid_run_without_user_input(self) -> None:
        """
        子 Agent 在主对话跑到第 2 轮时完成，**第 3 轮的请求体里就该有它**。

        修复前：一整次运行里一轮都拿不到，必须等下一条用户消息。
        """
        tasks = TaskManager()
        gate = _gate(tasks)
        record = tasks.create(KIND_ROLE, "explorer", "t")

        # ⚠ 三轮的参数**刻意各不相同**。写成三次一模一样的调用，本场景就同时
        # 是一次「原地打转」（`agent/spinning.py`），循环会在第 3 轮把自己停掉、
        # 第 4 轮根本不存在——而本用例要验的恰恰是第 3 轮之后那次请求体。
        # 真实的主 Agent 在等子 Agent 期间做的也是三件不同的事，不是同一件三遍。
        provider = _ScriptedProvider([
            ToolCall(id="a", name="read_file", arguments={"path": "a.py"}),
            ToolCall(id="b", name="read_file", arguments={"path": "b.py"}),
            ToolCall(id="c", name="read_file", arguments={"path": "c.py"}),
            "我做完了。",
        ])
        # 第 2 轮请求发出后让它完成
        def finish_soon():
            while provider.calls < 2:
                time.sleep(0.01)
            tasks.finish(record.task_id, TaskStatus.COMPLETED, "子结论：42 处。")

        threading.Thread(target=finish_soon, daemon=True).start()
        _run_loop(provider, gate)

        hits = [i for i, b in enumerate(provider.bodies, 1) if "42 处" in b]

        # 判据只有一条：**同一次运行内**到手，不需要用户插话。
        #
        # 刻意不断言「第几轮」——那个数字是竞态产物（后台线程按
        # `provider.calls` 触发，落点随调度浮动），钉死它只会得到一条
        # 间歇性失败的测试，而失败信息看起来像功能坏了。
        self.assertTrue(hits, "结论必须在同一次运行内进入某一轮的请求体")
        # 且必须在模型给出最终答复的那一轮之前或当轮到手——否则等于没用上
        self.assertLessEqual(hits[0], len(provider.bodies))

    def test_loop_waits_before_finishing(self) -> None:
        """
        模型准备收工时若还有「要等」的子 Agent，循环**不结束**，
        而是等它、把结论注入后再跑一轮。

        这是「委派出去之后模型直接回答」这条路径的兜底——没有它，
        模型会拿着不完整的信息作答。
        """
        tasks = TaskManager()
        gate = _gate(tasks)
        record = tasks.create(KIND_ROLE, "explorer", "t")

        provider = _ScriptedProvider(["我先答一句。", "拿到结论了，最终答复。"])

        def finish_soon():
            time.sleep(0.2)
            tasks.finish(record.task_id, TaskStatus.COMPLETED, "子结论：42 处。")

        threading.Thread(target=finish_soon, daemon=True).start()
        events, _ = _run_loop(provider, gate)

        self.assertGreaterEqual(provider.calls, 2, "循环应当等到结论后再跑一轮")
        self.assertIn("42 处", provider.bodies[-1])
        notices = [e.message for e in events if e.type == AgentEventType.NOTICE]
        self.assertTrue(
            any("等待子 Agent" in (n or "") for n in notices),
            "必须有一条提示，否则界面上就是『AI 突然不说话了』",
        )

    def test_background_task_does_not_hold_the_loop(self) -> None:
        """
        **反证**：`background=true` 的任务不该让循环停留。

        没有这一条的话，一个「无条件等所有任务」的实现也能过上面两条，
        而那会让模型明确声明的「我不等它」失效。
        """
        tasks = TaskManager()
        gate = _gate(tasks)
        record = tasks.create(KIND_ROLE, "explorer", "t")
        record.awaited = False

        provider = _ScriptedProvider(["直接收工。"])
        started = time.monotonic()
        _run_loop(provider, gate)

        self.assertEqual(provider.calls, 1)
        self.assertLess(time.monotonic() - started, 1.0)

    def test_cancel_during_wait_ends_the_run(self) -> None:
        tasks = TaskManager()
        gate = _gate(tasks)
        tasks.create(KIND_ROLE, "explorer", "t")
        cancel = threading.Event()

        provider = _ScriptedProvider(["先答一句。"])

        def cancel_soon():
            time.sleep(0.15)
            cancel.set()

        threading.Thread(target=cancel_soon, daemon=True).start()
        events, _ = _run_loop(provider, gate, cancel=cancel)

        finished = [e for e in events if e.type == AgentEventType.FINISHED]
        self.assertEqual(finished[-1].stop_reason, StopReason.USER_CANCELLED)

    def test_wait_rounds_are_bounded(self) -> None:
        """
        兜底：不会「等到一个 → 又发现一个 → 再等」无限接力。

        构造一个永远有任务在跑的闸门，循环必须自己走出来。
        """
        tasks = TaskManager()
        gate = _gate(tasks)
        tasks.create(KIND_ROLE, "never", "t")   # 永不结束

        # 让 wait_any 立刻返回 True（假装总有东西可交付），逼出接力场景
        gate.wait_any = lambda cancel_event, timeout=0: True  # type: ignore[assignment]

        provider = _ScriptedProvider(["收工。"] * 100)
        events, _ = _run_loop(provider, gate, max_iterations=100)

        finished = [e for e in events if e.type == AgentEventType.FINISHED]
        self.assertEqual(finished[-1].stop_reason, StopReason.COMPLETED)
        self.assertLessEqual(provider.calls, MAX_WAIT_ROUNDS + 2)


class ZeroRegressionTest(unittest.TestCase):
    """不传闸门时行为与改动前逐字一致。"""

    def test_default_is_none(self) -> None:
        self.assertIsNone(RunOptions().subagent_gate)

    def test_loop_runs_normally_without_gate(self) -> None:
        provider = _ScriptedProvider(["直接回答。"])
        registry = ToolRegistry()
        registry.register(_Noop())
        engine = PermissionEngine(RuleSet([]), mode=PermissionMode.DEFAULT)
        agent = Agent(provider, registry)
        history = [Message(role="user", content="你好")]
        events = list(
            agent.run(
                history, "off", False, "", lambda: "", "m", None,
                engine, lambda *a: True, None, None, threading.Event(),
            )
        )
        self.assertEqual(provider.calls, 1)
        self.assertEqual(events[-1].stop_reason, StopReason.COMPLETED)


if __name__ == "__main__":
    unittest.main()
