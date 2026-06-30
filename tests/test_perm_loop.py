"""loop 接入决策单测：DENY 不停循环、allow 规则免确认（c6 T9，对应 AC9/AC4）。"""

import threading
import unittest

from rhinecode.agent.loop import Agent
from rhinecode.agent.events import AgentEventType, StopReason
from rhinecode.provider.base import BaseProvider, Message, StreamChunk, ToolCall
from rhinecode.tools.base import Tool, ToolResult
from rhinecode.tools.registry import ToolRegistry
from rhinecode.permission.engine import PermissionEngine
from rhinecode.permission.models import PermissionMode, Rule
from rhinecode.permission.rules import RuleSet


class FakeRunCommand(Tool):
    """名为 run_command 的假工具：不真正跑 shell，只记录是否被执行。"""

    name = "run_command"
    description = "fake"
    parameters = {"type": "object", "properties": {"command": {"type": "string"}}}
    read_only = False

    def __init__(self) -> None:
        self.executed = False

    def execute(self, args: dict) -> ToolResult:
        self.executed = True
        return ToolResult(ok=True, output="ran", summary="ran")


class OneToolProvider(BaseProvider):
    """第一轮发一个 run_command 调用，第二轮自然完成。"""

    def __init__(self, command: str) -> None:
        self.command = command
        self.calls = 0

    def stream_chat(self, messages, thinking_effort="off", tools=None, system=None):
        self.calls += 1
        if self.calls == 1:
            yield StreamChunk(
                type="tool_call",
                tool_call=ToolCall(id="c1", name="run_command", arguments={"command": self.command}),
            )
            yield StreamChunk(type="done")
            return
        yield StreamChunk(type="text", content="done")
        yield StreamChunk(type="done")


def run_agent(provider, tool, engine, ask):
    registry = ToolRegistry()
    registry.register(tool)
    agent = Agent(provider, registry)
    history: list[Message] = [Message(role="user", content="go")]
    return list(agent.run(
        history, "off", False, "", "", "model", None,
        engine, ask, None, None, threading.Event(),
    ))


class LoopPermissionTests(unittest.TestCase):
    def test_blacklist_deny_does_not_stop_loop(self) -> None:
        # AC9：被拒工具回灌 ok=False 结构化结果，循环继续到自然完成；ask 不被调用
        tool = FakeRunCommand()
        engine = PermissionEngine(RuleSet([]), mode=PermissionMode.PERMISSIVE)
        ask_calls = {"n": 0}

        def ask(_tc, _tool, _dec):
            ask_calls["n"] += 1
            return True

        events = run_agent(OneToolProvider("rm -rf /"), tool, engine, ask)

        self.assertFalse(tool.executed)
        self.assertEqual(ask_calls["n"], 0)  # DENY 不弹确认
        results = [e for e in events if e.type == AgentEventType.TOOL_RESULT]
        self.assertEqual(len(results), 1)
        self.assertFalse(results[0].tool_result.ok)
        self.assertIn("[权限拒绝", results[0].tool_result.output)
        # 循环没有异常停止，而是走到自然完成
        self.assertEqual(events[-1].type, AgentEventType.FINISHED)
        self.assertEqual(events[-1].stop_reason, StopReason.COMPLETED)

    def test_allow_rule_executes_without_ask(self) -> None:
        # AC4：命中 allow 规则的副作用工具直接执行，不触发 ask
        tool = FakeRunCommand()
        engine = PermissionEngine(RuleSet([Rule("allow", "Bash", "git *", "project")]))
        ask_calls = {"n": 0}

        def ask(_tc, _tool, _dec):
            ask_calls["n"] += 1
            return True

        run_agent(OneToolProvider("git status"), tool, engine, ask)

        self.assertTrue(tool.executed)
        self.assertEqual(ask_calls["n"], 0)


if __name__ == "__main__":
    unittest.main()
