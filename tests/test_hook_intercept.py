"""
Agent Loop 与 Hook 前置层的集成测试（c12 T29，对应 checklist 第二、六、八节 /
spec AC3、AC4、AC14、AC15、AC16）。

含四条**安全性反证**——它们防的都是「结果碰巧对、实现其实错」：
- Hook 判 ASK 但权限判 DENY → 仍是 DENY，面板不弹（Hook 不能放宽）
- Hook 判 DENY 时 `engine.decide` 一次都没被调用（防「先跑引擎再看 hook」的错序）
- 六种「没执行」的分支都不产生 `post_tool_use*`
- 危险命令 + Hook 判 ASK → 仍被①黑名单拦下
"""

import threading
import unittest
from unittest import mock

from rhinecode.agent.events import AgentEventType, StopReason
from rhinecode.agent.loop import Agent
from rhinecode.hooks import HookManager
from rhinecode.hooks import manager as hook_manager
from rhinecode.hooks.models import (
    ActionOutcome,
    CommandAction,
    HookDecision,
    HookEventType,
    HookRule,
)
from rhinecode.permission.engine import PermissionEngine
from rhinecode.permission.models import Layer, PermissionMode
from rhinecode.permission.rules import RuleSet
from rhinecode.provider.base import BaseProvider, Message, StreamChunk, ToolCall
from rhinecode.tools.base import Tool, ToolResult
from rhinecode.tools.registry import ToolRegistry

PRE = HookEventType.PRE_TOOL_USE
POST = HookEventType.POST_TOOL_USE
FAIL = HookEventType.POST_TOOL_USE_FAILURE


# --------------------------------------------------------------------------- #
# 台架
# --------------------------------------------------------------------------- #
class FakeWriteTool(Tool):
    """有副作用的假工具。"""

    name = "run_command"
    description = "fake"
    parameters = {"type": "object", "properties": {"command": {"type": "string"}}}
    read_only = False

    def __init__(self, ok: bool = True) -> None:
        self.executed = 0
        self._ok = ok

    def execute(self, args: dict) -> ToolResult:
        self.executed += 1
        return ToolResult(ok=self._ok, output="ran" if self._ok else "boom",
                          summary="ran" if self._ok else "失败了")


class FakeReadTool(Tool):
    """只读假工具——它会走并发桶。"""

    name = "read_file"
    description = "fake"
    parameters = {"type": "object", "properties": {"path": {"type": "string"}}}
    read_only = True

    def __init__(self) -> None:
        self.executed = 0

    def execute(self, args: dict) -> ToolResult:
        self.executed += 1
        return ToolResult(ok=True, output="content", summary="read")


class FakeSystemTool(Tool):
    """系统级串行工具（形态同 load_skill）。"""

    name = "load_skill"
    description = "fake"
    parameters = {"type": "object", "properties": {"name": {"type": "string"}}}
    read_only = True
    system_serial = True

    def __init__(self) -> None:
        self.executed = 0

    def execute(self, args: dict) -> ToolResult:
        self.executed += 1
        return ToolResult(ok=True, output="loaded", summary="loaded")


class ScriptedProvider(BaseProvider):
    """第一轮发出预设的工具调用，之后自然完成。"""

    def __init__(self, calls: list[ToolCall]) -> None:
        self._calls = calls
        self.rounds = 0

    def stream_chat(self, messages, thinking_effort="off", tools=None, system=None):
        self.rounds += 1
        if self.rounds == 1:
            for tc in self._calls:
                yield StreamChunk(type="tool_call", tool_call=tc)
            yield StreamChunk(type="done")
            return
        yield StreamChunk(type="text", content="done")
        yield StreamChunk(type="done")


class _CountingEngine(PermissionEngine):
    """记录 `decide` 被调用了几次的权限引擎。"""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.decide_calls = 0

    def decide(self, request):
        self.decide_calls += 1
        return super().decide(request)


def _rule(name="规则", event=PRE, command="x", index=0, source="user"):
    return HookRule(name, source, index, event, None, CommandAction(command, 5))


def _install_outcomes(test, mapping, default=None):
    """把 `run_action` 换成按命令串给结果的假实现。"""
    default = default or ActionOutcome(ok=True)

    def fake(action, payload, client_factory=None):
        key = getattr(action, "command", "")
        return mapping.get(key, default)

    patcher = mock.patch.object(hook_manager, "run_action", side_effect=fake)
    patcher.start()
    test.addCleanup(patcher.stop)


def run_agent(provider, tools, engine, ask, hooks=None, plan_mode=False):
    registry = ToolRegistry()
    for tool in tools:
        registry.register(tool)
    agent = Agent(provider, registry, hooks=hooks)
    history: list[Message] = [Message(role="user", content="go")]
    return list(
        agent.run(
            history, "off", plan_mode, "", lambda: "", "model", None,
            engine, ask, None, lambda _plan: True, threading.Event(),
        )
    )


def _results(events):
    return [e for e in events if e.type == AgentEventType.TOOL_RESULT]


# --------------------------------------------------------------------------- #
# 拦截
# --------------------------------------------------------------------------- #
class InterceptTest(unittest.TestCase):
    """Hook 判 DENY（AC14）。"""

    def setUp(self):
        self.tool = FakeWriteTool()
        self.engine = _CountingEngine(RuleSet([]), mode=PermissionMode.PERMISSIVE)
        self.ask_calls = 0

    def _ask(self, *_a):
        self.ask_calls += 1
        return True

    def _hooks(self, verdict, reason="不许"):
        _install_outcomes(self, {"x": ActionOutcome(ok=True, verdict=verdict, reason=reason)})
        return HookManager([_rule()])

    def test_deny_blocks_tool_and_loop_continues(self):
        events = run_agent(
            ScriptedProvider([ToolCall("c1", "run_command", {"command": "git push"})]),
            [self.tool], self.engine, self._ask, self._hooks(HookDecision.DENY),
        )
        self.assertEqual(self.tool.executed, 0, "工具不该执行")
        self.assertEqual(self.ask_calls, 0, "拦截不弹面板")
        results = _results(events)
        self.assertEqual(len(results), 1)
        self.assertFalse(results[0].tool_result.ok)
        self.assertIn("Hook 拦截", results[0].tool_result.output)
        self.assertIn("不许", results[0].tool_result.output)
        # 循环没有异常停止，而是走到自然完成（spec F6.1）
        self.assertEqual(events[-1].stop_reason, StopReason.COMPLETED)

    def test_deny_does_not_call_permission_engine(self):
        """
        ⚠ 反证：Hook 判 DENY 时权限引擎**一次都不该被调用**。

        防的是「先跑 engine.decide 再看 hook」这种把顺序写反、但因为最终
        都拦下了所以结果碰巧正确的实现——那样 Hook 就不再是「排在五层之前」，
        而 spec F6 的管线图和整章的安全论证都建立在这个位置上。
        """
        run_agent(
            ScriptedProvider([ToolCall("c1", "run_command", {"command": "git push"})]),
            [self.tool], self.engine, self._ask, self._hooks(HookDecision.DENY),
        )
        self.assertEqual(self.engine.decide_calls, 0)

    def test_deny_feedback_tells_model_not_to_work_around(self):
        events = run_agent(
            ScriptedProvider([ToolCall("c1", "run_command", {"command": "git push"})]),
            [self.tool], self.engine, self._ask, self._hooks(HookDecision.DENY),
        )
        output = _results(events)[0].tool_result.output
        self.assertIn("不是技术故障", output)
        self.assertIn("绕过", output)

    def test_no_verdict_behaves_exactly_like_no_hook(self):
        """不表态时行为与无 Hook 逐字一致（AC14）。"""
        with_hook = run_agent(
            ScriptedProvider([ToolCall("c1", "run_command", {"command": "ls"})]),
            [FakeWriteTool()], _CountingEngine(RuleSet([]), mode=PermissionMode.PERMISSIVE),
            self._ask, self._hooks(HookDecision.NONE),
        )
        without = run_agent(
            ScriptedProvider([ToolCall("c1", "run_command", {"command": "ls"})]),
            [FakeWriteTool()], _CountingEngine(RuleSet([]), mode=PermissionMode.PERMISSIVE),
            self._ask, None,
        )
        self.assertEqual(
            [(e.type, getattr(e.tool_result, "ok", None)) for e in _results(with_hook)],
            [(e.type, getattr(e.tool_result, "ok", None)) for e in _results(without)],
        )


class AskUpgradeTest(unittest.TestCase):
    """Hook 判 ASK（AC14、AC15）。"""

    def setUp(self):
        self.seen_decisions = []

    def _ask(self, _tc, _tool, decision):
        self.seen_decisions.append(decision)
        return True

    def _hooks(self, reason="要你确认"):
        _install_outcomes(
            self, {"x": ActionOutcome(ok=True, verdict=HookDecision.ASK, reason=reason)}
        )
        return HookManager([_rule()])

    def test_allow_is_upgraded_to_ask(self):
        tool = FakeWriteTool()
        engine = PermissionEngine(RuleSet([]), mode=PermissionMode.PERMISSIVE)
        run_agent(
            ScriptedProvider([ToolCall("c1", "run_command", {"command": "ls"})]),
            [tool], engine, self._ask, self._hooks(),
        )
        self.assertEqual(len(self.seen_decisions), 1, "放行档下本该直接放行，现在要弹面板")
        self.assertEqual(self.seen_decisions[0].layer, Layer.HOOK)
        self.assertIn("要你确认", self.seen_decisions[0].reason)
        self.assertEqual(tool.executed, 1, "用户同意后照常执行")

    def test_readonly_tool_leaves_the_concurrent_bucket_when_upgraded(self):
        """升级后必须走串行桶——并发桶里没有弹面板的路径。"""
        tool = FakeReadTool()
        engine = PermissionEngine(RuleSet([]), mode=PermissionMode.PERMISSIVE)
        run_agent(
            ScriptedProvider([ToolCall("c1", "read_file", {"path": "a.py"})]),
            [tool], engine, self._ask, self._hooks(),
        )
        self.assertEqual(len(self.seen_decisions), 1)

    def test_ask_cannot_downgrade_a_blacklist_deny(self):
        """
        ⚠ 安全反证（AC15）：Hook 判 ASK + ①黑名单判 DENY → **仍是 DENY**。

        若实现写成「命中 ASK 就置为 ASK」，一条 Hook 就能把黑名单的硬拒绝
        变成一次可以点「同意」的确认面板——Hook 于是获得了放宽权限的能力，
        而「Hook 只能收紧」是本章全部安全论证的依据。
        """
        tool = FakeWriteTool()
        engine = PermissionEngine(RuleSet([]), mode=PermissionMode.PERMISSIVE)
        events = run_agent(
            ScriptedProvider([ToolCall("c1", "run_command", {"command": "rm -rf /"})]),
            [tool], engine, self._ask, self._hooks(),
        )
        self.assertEqual(self.seen_decisions, [], "面板绝不能弹")
        self.assertEqual(tool.executed, 0)
        self.assertIn("[权限拒绝", _results(events)[0].tool_result.output)

    def test_ask_cannot_downgrade_a_sandbox_deny(self):
        """同上，②路径沙箱那一层。"""
        tool = FakeReadTool()
        engine = PermissionEngine(RuleSet([]), mode=PermissionMode.PERMISSIVE)
        events = run_agent(
            ScriptedProvider([ToolCall("c1", "read_file", {"path": "/etc/passwd"})]),
            [tool], engine, self._ask, self._hooks(),
        )
        self.assertEqual(self.seen_decisions, [])
        self.assertEqual(tool.executed, 0)
        self.assertIn("[权限拒绝", _results(events)[0].tool_result.output)


class TraceFidelityTest(unittest.TestCase):
    """
    `permission_decision` 记的必须是**生效的**那个结论（端到端场景 2 实测踩过）。

    埋点若排在 Hook 升级之前，一次「权限判 ALLOW、Hook 升级为 ASK」的调用会在
    记录里留下 `decision=allow`，而用户实际看到的是一个确认面板——
    **观测设施撒谎且不报错**，排查的人会据此断定「Hook 没生效」。
    """

    class _Rec:
        enabled = True

        def __init__(self):
            self.events: list[tuple] = []

        def emit(self, event_type, **payload):
            self.events.append((getattr(event_type, "value", event_type), payload))

        def emit_lazy(self, event_type, factory):
            self.emit(event_type, **factory())

        def current_scope(self):
            return "main"

        def bind_scope(self, name):
            pass

    def test_records_the_effective_decision_after_upgrade(self):
        _install_outcomes(
            self, {"x": ActionOutcome(ok=True, verdict=HookDecision.ASK, reason="要确认")}
        )
        rec = self._Rec()
        registry = ToolRegistry()
        registry.register(FakeReadTool())
        agent = Agent(
            ScriptedProvider([ToolCall("c1", "read_file", {"path": "a.py"})]),
            registry,
            recorder=rec,
            hooks=HookManager([_rule()]),
        )
        list(agent.run(
            [Message(role="user", content="go")], "off", False, "", lambda: "", "m", None,
            PermissionEngine(RuleSet([]), mode=PermissionMode.PERMISSIVE),
            lambda *a: True, None, None, threading.Event(),
        ))

        decisions = [p for name, p in rec.events if name == "permission_decision"]
        self.assertEqual(len(decisions), 1)
        self.assertEqual(decisions[0]["decision"], "ask", "记的必须是升级后的结论")
        self.assertEqual(decisions[0]["layer"], "hook")

    def test_records_allow_when_hook_says_nothing(self):
        _install_outcomes(self, {"x": ActionOutcome(ok=True)})
        rec = self._Rec()
        registry = ToolRegistry()
        registry.register(FakeReadTool())
        agent = Agent(
            ScriptedProvider([ToolCall("c1", "read_file", {"path": "a.py"})]),
            registry,
            recorder=rec,
            hooks=HookManager([_rule()]),
        )
        list(agent.run(
            [Message(role="user", content="go")], "off", False, "", lambda: "", "m", None,
            PermissionEngine(RuleSet([]), mode=PermissionMode.PERMISSIVE),
            lambda *a: True, None, None, threading.Event(),
        ))
        decisions = [p for name, p in rec.events if name == "permission_decision"]
        self.assertEqual(decisions[0]["decision"], "allow", "不表态时不该被改写")


class FailClosedTest(unittest.TestCase):
    """Hook 自身失败在 pre_tool_use 上即拦截（AC17）。"""

    def test_hook_failure_blocks_the_call(self):
        _install_outcomes(self, {"x": ActionOutcome(ok=False, detail="脚本不存在")})
        tool = FakeWriteTool()
        events = run_agent(
            ScriptedProvider([ToolCall("c1", "run_command", {"command": "ls"})]),
            [tool], PermissionEngine(RuleSet([]), mode=PermissionMode.PERMISSIVE),
            lambda *a: True, HookManager([_rule()]),
        )
        self.assertEqual(tool.executed, 0)
        output = _results(events)[0].tool_result.output
        self.assertIn("Hook 自身执行失败", output)
        self.assertIn("脚本不存在", output)

    def test_hook_failure_on_post_tool_use_does_not_block(self):
        """同一条坏 Hook 挂到 post_tool_use 上，工具必须照常跑完（AC18）。"""
        _install_outcomes(self, {"x": ActionOutcome(ok=False, detail="脚本不存在")})
        tool = FakeWriteTool()
        events = run_agent(
            ScriptedProvider([ToolCall("c1", "run_command", {"command": "ls"})]),
            [tool], PermissionEngine(RuleSet([]), mode=PermissionMode.PERMISSIVE),
            lambda *a: True, HookManager([_rule(event=POST)]),
        )
        self.assertEqual(tool.executed, 1)
        self.assertTrue(_results(events)[0].tool_result.ok)


# --------------------------------------------------------------------------- #
# 事件覆盖与边界
# --------------------------------------------------------------------------- #
class _Recording:
    """记录全部 dispatch 调用的假 manager。"""

    enabled = True
    rules: list = []
    warnings: list = []

    def __init__(self, listen=()):
        self.listen = set(listen)
        self.seen: list[tuple[str, dict]] = []

    def bind_context(self, session_id="", cwd=""):
        pass

    def has_listeners(self, event):
        return event in self.listen

    def dispatch(self, event, payload_factory=None):
        from rhinecode.hooks.models import EMPTY_DISPATCH

        self.seen.append((event.value, payload_factory() if payload_factory else {}))
        return EMPTY_DISPATCH

    def consume_injections(self):
        return ""

    def report(self):
        return ""

    def project_notice(self):
        return None

    def events(self):
        return [name for name, _ in self.seen]


class EventCoverageTest(unittest.TestCase):
    """工具级三事件的触发边界（AC3、AC4）。"""

    ALL = (PRE, POST, FAIL)

    def _run(self, calls, tools, *, engine=None, ask=None, plan_mode=False):
        rec = _Recording(self.ALL)
        engine = engine or PermissionEngine(RuleSet([]), mode=PermissionMode.PERMISSIVE)
        run_agent(ScriptedProvider(calls), tools, engine, ask or (lambda *a: True),
                  rec, plan_mode=plan_mode)
        return rec

    def test_serial_tool_fires_pre_and_post(self):
        rec = self._run([ToolCall("c1", "run_command", {"command": "ls"})], [FakeWriteTool()])
        self.assertEqual(rec.events(), ["pre_tool_use", "post_tool_use"])

    def test_failed_tool_fires_failure_event(self):
        rec = self._run([ToolCall("c1", "run_command", {"command": "ls"})],
                        [FakeWriteTool(ok=False)])
        self.assertEqual(rec.events(), ["pre_tool_use", "post_tool_use_failure"])

    def test_concurrent_readonly_tool_fires_both(self):
        """只读并发工具**也**触发 pre_tool_use（AC4）。"""
        rec = self._run([ToolCall("c1", "read_file", {"path": "a.py"})], [FakeReadTool()])
        self.assertEqual(rec.events(), ["pre_tool_use", "post_tool_use"])

    def test_system_serial_tool_fires_pre(self):
        """系统级串行工具虽然跳过权限引擎，但它确实要执行，所以照样触发（AC4）。"""
        rec = self._run([ToolCall("c1", "load_skill", {"name": "x"})], [FakeSystemTool()])
        self.assertIn("pre_tool_use", rec.events())

    def test_payload_expands_tool_input(self):
        rec = self._run([ToolCall("c1", "run_command", {"command": "git push"})],
                        [FakeWriteTool()])
        _, fields = rec.seen[0]
        self.assertEqual(fields["command"], "git push", "tool_input 逐字展开成顶层字段")
        self.assertEqual(fields["tool"], "run_command")
        self.assertEqual(fields["tool_call_id"], "c1")
        self.assertIs(fields["is_read_only"], False)

    def test_post_payload_carries_output_and_duration(self):
        rec = self._run([ToolCall("c1", "run_command", {"command": "ls"})], [FakeWriteTool()])
        _, fields = rec.seen[1]
        self.assertEqual(fields["tool_output"], "ran")
        self.assertIn("duration_ms", fields)


class NoExecutionBranchesTest(unittest.TestCase):
    """
    ⚠ 六种「压根没执行」的分支**一个工具级事件都不该产生**（AC3）。

    把「没跑」混进 `post_tool_use_failure` 会让「统计工具失败率」这类用途
    直接失真，而且不报错——这正是要逐条钉住它的理由。
    """

    ALL = (PRE, POST, FAIL)

    def _events(self, calls, tools, *, engine=None, ask=None, plan_mode=False):
        rec = _Recording(self.ALL)
        engine = engine or PermissionEngine(RuleSet([]), mode=PermissionMode.PERMISSIVE)
        run_agent(ScriptedProvider(calls), tools, engine, ask or (lambda *a: True),
                  rec, plan_mode=plan_mode)
        return rec.events()

    def test_unknown_tool(self):
        self.assertEqual(self._events([ToolCall("c1", "no_such_tool", {})], [FakeWriteTool()]), [])

    def test_invalid_arguments(self):
        events = self._events([ToolCall("c1", "run_command", "不是字典")], [FakeWriteTool()])
        self.assertEqual(events, [])

    def test_permission_deny(self):
        """
        ⚠ 这两条与 spec F2 边界第 2 条的**字面**表述不同，是刻意的。

        那条边界说「权限 DENY」与「用户拒绝」下「一个工具级事件都不触发」，
        但它给的理由只讲后置事件的语义（「跑了但失败了」）；而 `pre_tool_use`
        按决策 1A 排在五层**之前**——在跑权限判定之前根本无从知道会不会 DENY，
        结构上做不到。故：前置照常触发，**后置一个都不产**。
        """
        events = self._events(
            [ToolCall("c1", "run_command", {"command": "rm -rf /"})], [FakeWriteTool()]
        )
        self.assertEqual(events, ["pre_tool_use"])

    def test_user_denied_in_panel(self):
        events = self._events(
            [ToolCall("c1", "run_command", {"command": "ls"})],
            [FakeWriteTool()],
            engine=PermissionEngine(RuleSet([]), mode=PermissionMode.DEFAULT),
            ask=lambda *a: False,
        )
        self.assertEqual(events, ["pre_tool_use"])

    def test_plan_blocked(self):
        events = self._events(
            [ToolCall("c1", "run_command", {"command": "ls"})],
            [FakeWriteTool()],
            plan_mode=True,
        )
        self.assertEqual(events, [], "规划阶段挡下的调用连 pre 都不触发")

    def test_special_tools_fire_nothing(self):
        """`ask_user` / `present_plan` 是与用户交互，不是工具执行（AC4）。"""
        events = self._events(
            [ToolCall("c1", "present_plan", {"plan": "做点事"})], [FakeWriteTool()],
            plan_mode=True,
        )
        self.assertEqual(events, [])


class ZeroRegressionTest(unittest.TestCase):
    """不传 hooks 时零回归（AC24）。"""

    def test_default_manager_never_builds_payload(self):
        tool = FakeWriteTool()
        agent = Agent(ScriptedProvider([]), ToolRegistry())
        # 缺省应当是 NullHookManager，其 has_listeners 恒为 False
        self.assertFalse(agent._hooks.has_listeners(PRE))
        self.assertFalse(agent._hooks.enabled)
        del tool


if __name__ == "__main__":
    unittest.main()
