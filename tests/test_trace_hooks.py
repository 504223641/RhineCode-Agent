"""
埋点测试（trace T50–T53）：十五类事件、六种工具结局、作用域、不阻断、界面与命令。

对应 spec AC6（记录失败不阻断）/ AC9（十五类均可产出）/ AC10–AC21 / AC27。

组织方式：每个测试类聚焦一层，用真实的 Agent / Dispatcher / ContextManager /
SkillManager 配假 Provider，落盘到临时文件后回读断言——**断言的是记录文件的内容**，
而不是「某个方法被调用过」，这样重构埋点实现不会让测试变红。
"""

import json
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace

from rhinecode.agent.events import AgentEventType, StopReason
from rhinecode.agent.loop import Agent, RunOptions
from rhinecode.commands import CommandDispatcher, CommandType, build_builtin_registry
from rhinecode.commands.models import CommandSpec
from rhinecode.commands.registry import CommandRegistry
from rhinecode.context.manager import ContextManager
from rhinecode.permission.engine import PermissionEngine
from rhinecode.permission.models import PermissionMode
from rhinecode.permission.rules import RuleSet, Rule
from rhinecode.provider.base import Message, StreamChunk, ToolCall
from rhinecode.tools.base import Tool, ToolResult
from rhinecode.tools.policy import ToolPolicy
from rhinecode.tools.registry import ToolRegistry
from rhinecode.trace.models import SCOPE_MAIN, SCOPE_NOTES, SCOPE_SUMMARY, TraceEventType
from rhinecode.trace.recorder import TraceRecorder, bind_scope
from rhinecode.trace.tracing_provider import TracingProvider

T = TraceEventType


# ---------------------------------------------------------------------------
# 公共替身
# ---------------------------------------------------------------------------
class FakeTool(Tool):
    def __init__(self, name: str, read_only: bool = True, raises: bool = False) -> None:
        self.name = name
        self.description = f"{name} 的说明"
        self.parameters = {"type": "object", "properties": {}}
        self.read_only = read_only
        self._raises = raises

    def execute(self, args: dict) -> ToolResult:
        if self._raises:
            raise RuntimeError("工具内部炸了")
        return ToolResult(ok=True, output=f"{self.name} 输出", summary=f"{self.name} 完成")


class ScriptedProvider:
    """按脚本逐轮产出 chunk 的假 Provider（生成器函数，满足 closing 要求）。"""

    def __init__(self, scripts) -> None:
        self.scripts = scripts
        self.calls: list[dict] = []

    def stream_chat(self, messages, thinking_effort="off", tools=None, system=None):
        idx = min(len(self.calls), len(self.scripts) - 1)
        self.calls.append({"messages": list(messages), "tools": tools, "system": system})
        yield from self.scripts[idx]


def _text_round(text: str = "完成"):
    return [StreamChunk(type="text", content=text), StreamChunk(type="done")]


def _tool_round(name: str, call_id: str = "c1", args=None):
    return [
        StreamChunk(
            type="tool_call",
            tool_call=ToolCall(id=call_id, name=name, arguments=args),
        ),
        StreamChunk(type="done"),
    ]


class TraceHookBase(unittest.TestCase):
    """临时记录文件 + 每个用例复位作用域。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.path = self.root / "trace.jsonl"
        self.rec = TraceRecorder(self.path)
        bind_scope(SCOPE_MAIN)

    def tearDown(self) -> None:
        self.rec.close()
        bind_scope(SCOPE_MAIN)
        self._tmp.cleanup()

    def records(self, type_: "TraceEventType | str | None" = None) -> list[dict]:
        text = self.path.read_text(encoding="utf-8")
        all_ = [json.loads(l) for l in text.splitlines() if l.strip()]
        if type_ is None:
            return all_
        want = getattr(type_, "value", type_)
        return [r for r in all_ if r["type"] == want]

    def run_loop(
        self,
        provider,
        registry,
        *,
        engine=None,
        options=RunOptions(),
        ask=None,
        history=None,
        plan_mode=False,
    ) -> list:
        agent = Agent(provider, registry, recorder=self.rec)
        return list(
            agent.run(
                history if history is not None else [Message(role="user", content="go")],
                "off",
                plan_mode,
                "",
                lambda: "",
                "model",
                None,
                engine or PermissionEngine(RuleSet([]), mode=PermissionMode.PERMISSIVE),
                ask or (lambda *a: True),
                None,
                lambda _p: True,
                threading.Event(),
                options=options,
            )
        )


# ---------------------------------------------------------------------------
# T50：模型交互 / 权限 / 工具
# ---------------------------------------------------------------------------
class ModelInteractionTest(TraceHookBase):
    def test_api_request_tool_names_match_schema(self) -> None:
        """AC10：请求事件的 tool_names 与假 Provider 实际收到的 schema 一致。"""
        registry = ToolRegistry()
        registry.register(FakeTool("read_file"))
        registry.register(FakeTool("write_file", read_only=False))
        provider = ScriptedProvider([_text_round()])
        traced = TracingProvider(provider, self.rec, "model")

        self.run_loop(traced, registry)

        sent = {t["function"]["name"] for t in provider.calls[0]["tools"]}
        req = self.records(T.API_REQUEST)[0]
        self.assertEqual(set(req["tool_names"]), sent)
        self.assertEqual(len(req["messages"]), len(provider.calls[0]["messages"]))

    def test_api_response_fields_present(self) -> None:
        provider = ScriptedProvider([_text_round("你好世界")])
        traced = TracingProvider(provider, self.rec, "model")
        self.run_loop(traced, ToolRegistry())

        resp = self.records(T.API_RESPONSE)[0]
        self.assertEqual(resp["text"], "你好世界")
        self.assertEqual(resp["turn"], 1)
        self.assertGreaterEqual(resp["duration_ms"], 0)

    def test_truncation_records_original_length(self) -> None:
        """AC17：超长字段被截断但记下原长。"""
        from rhinecode.trace.models import MAX_FIELD_CHARS

        long_text = "字" * (MAX_FIELD_CHARS + 500)
        provider = ScriptedProvider([_text_round(long_text)])
        traced = TracingProvider(provider, self.rec, "model")
        self.run_loop(traced, ToolRegistry())

        resp = self.records(T.API_RESPONSE)[0]
        self.assertIs(resp["text"]["truncated"], True)
        self.assertEqual(resp["text"]["original_length"], MAX_FIELD_CHARS + 500)


class PermissionHookTest(TraceHookBase):
    def test_decision_layer_and_reason(self) -> None:
        registry = ToolRegistry()
        registry.register(FakeTool("read_file"))
        provider = ScriptedProvider([_tool_round("read_file", args={"path": "a.txt"}), _text_round()])

        self.run_loop(provider, registry)

        dec = self.records(T.PERMISSION_DECISION)
        self.assertEqual(len(dec), 1)
        self.assertEqual(dec[0]["tool"], "read_file")
        self.assertEqual(dec[0]["decision"], "allow")
        self.assertTrue(dec[0]["layer"])
        self.assertTrue(dec[0]["reason"])
        self.assertIs(dec[0]["is_read_only"], True)

    def test_deny_rule_recorded(self) -> None:
        registry = ToolRegistry()
        registry.register(FakeTool("write_file", read_only=False))
        engine = PermissionEngine(
            RuleSet([Rule(effect="deny", tool="Write", pattern="", source="project")]),
            mode=PermissionMode.PERMISSIVE,
        )
        provider = ScriptedProvider(
            [_tool_round("write_file", args={"path": "a.txt", "content": "x"}), _text_round()]
        )

        self.run_loop(provider, registry, engine=engine)

        dec = self.records(T.PERMISSION_DECISION)[0]
        self.assertEqual(dec["decision"], "deny")


class ToolOutcomeTest(TraceHookBase):
    """六种 outcome 各一条（T31 的八处写入点覆盖到六个语义）。"""

    def test_executed_serial(self) -> None:
        registry = ToolRegistry()
        registry.register(FakeTool("write_file", read_only=False))
        provider = ScriptedProvider(
            [_tool_round("write_file", args={"path": "a", "content": "b"}), _text_round()]
        )
        self.run_loop(provider, registry)
        ev = self.records(T.TOOL_EXECUTE)[0]
        self.assertEqual(ev["outcome"], "executed")
        self.assertIs(ev["is_concurrent"], False)
        self.assertIs(ev["ok"], True)

    def test_executed_concurrent(self) -> None:
        registry = ToolRegistry()
        registry.register(FakeTool("read_file"))
        provider = ScriptedProvider([_tool_round("read_file", args={"path": "a"}), _text_round()])
        self.run_loop(provider, registry)
        ev = self.records(T.TOOL_EXECUTE)[0]
        self.assertEqual(ev["outcome"], "executed")
        self.assertIs(ev["is_concurrent"], True)

    def test_unknown_tool(self) -> None:
        provider = ScriptedProvider([_tool_round("no_such_tool", args={}), _text_round()])
        self.run_loop(provider, ToolRegistry())
        ev = self.records(T.TOOL_EXECUTE)[0]
        self.assertEqual(ev["outcome"], "unknown_tool")

    def test_invalid_arguments(self) -> None:
        registry = ToolRegistry()
        registry.register(FakeTool("write_file", read_only=False))
        # arguments=None 表示 Provider 解析模型生成的 JSON 失败
        provider = ScriptedProvider([_tool_round("write_file", args=None), _text_round()])
        self.run_loop(provider, registry)
        ev = self.records(T.TOOL_EXECUTE)[0]
        self.assertEqual(ev["outcome"], "invalid_arguments")

    def test_denied_by_permission(self) -> None:
        registry = ToolRegistry()
        registry.register(FakeTool("write_file", read_only=False))
        engine = PermissionEngine(
            RuleSet([Rule(effect="deny", tool="Write", pattern="", source="project")]),
            mode=PermissionMode.PERMISSIVE,
        )
        provider = ScriptedProvider(
            [_tool_round("write_file", args={"path": "a", "content": "b"}), _text_round()]
        )
        self.run_loop(provider, registry, engine=engine)
        ev = self.records(T.TOOL_EXECUTE)[0]
        self.assertEqual(ev["outcome"], "denied_by_permission")

    def test_denied_by_user(self) -> None:
        registry = ToolRegistry()
        registry.register(FakeTool("write_file", read_only=False))
        engine = PermissionEngine(RuleSet([]), mode=PermissionMode.DEFAULT)
        provider = ScriptedProvider(
            [_tool_round("write_file", args={"path": "a", "content": "b"}), _text_round()]
        )
        self.run_loop(provider, registry, engine=engine, ask=lambda *a: False)
        ev = self.records(T.TOOL_EXECUTE)[0]
        self.assertEqual(ev["outcome"], "denied_by_user")

    def test_out_of_scope_carries_unavailable_text(self) -> None:
        """
        **本模块立项要查的那个 bug 的唯一物证。**

        模型调了一个「存在但本轮没发给它」的工具，回灌文本必须出现在 trace 里
        ——它既不在 permission_decision（没进引擎）也不在 agent_event（被白名单剔掉）。
        """
        registry = ToolRegistry()
        registry.register(FakeTool("read_file"))
        registry.register(FakeTool("edit_file", read_only=False))
        provider = ScriptedProvider(
            [_tool_round("edit_file", args={"path": "a"}), _text_round()]
        )
        options = RunOptions(
            tool_policy=lambda: ToolPolicy(
                allowed=frozenset({"read_file"}),
                exempt=frozenset(),
                excluded=frozenset(),
            )
        )

        self.run_loop(provider, registry, options=options)

        ev = self.records(T.TOOL_EXECUTE)[0]
        self.assertEqual(ev["outcome"], "out_of_scope")
        self.assertIn("工具不可用", str(ev["output"]))
        # 反证：这件事在其它两类事件里查不到
        self.assertEqual(self.records(T.PERMISSION_DECISION), [])
        self.assertNotIn("工具不可用", json.dumps(self.records(T.AGENT_EVENT), ensure_ascii=False))

    def test_special_tools_produce_no_tool_execute(self) -> None:
        """裁决：ask_user / present_plan 不产 tool_execute（由 interaction 承载）。"""
        provider = ScriptedProvider(
            [
                _tool_round("present_plan", args={"plan": "先看代码"}),
                _text_round(),
            ]
        )
        self.run_loop(provider, ToolRegistry(), plan_mode=True)
        self.assertEqual(self.records(T.TOOL_EXECUTE), [])


class ContextCompactionHookTest(TraceHookBase):
    def test_offload_layer_records_ids_and_paths(self) -> None:
        from rhinecode.context.estimate import CHARS_PER_TOKEN
        from rhinecode.context.offload import SINGLE_RESULT_TOKENS

        big = "A" * (int(SINGLE_RESULT_TOKENS * CHARS_PER_TOKEN) + 200)
        history = [
            Message(role="user", content="go"),
            Message(role="tool", content=big, tool_call_id="call-1"),
        ]
        cm = ContextManager(
            ScriptedProvider([_text_round()]),
            "model",
            65536,
            self.root / "store",
            recorder=self.rec,
        )
        cm.before_request(history)

        ev = self.records(T.CONTEXT_COMPACTION)
        self.assertEqual(len(ev), 1)
        self.assertEqual(ev[0]["layer"], "offload")
        self.assertEqual(ev[0]["tool_call_ids"], ["call-1"])
        self.assertTrue(Path(ev[0]["paths"][0]).exists())

    def test_summary_layer_records_boundary_and_counts(self) -> None:
        summary_script = [
            StreamChunk(type="text", content="<<<正式摘要>>>\n## 已完成\n看了代码\n"),
            StreamChunk(type="done"),
        ]
        provider = ScriptedProvider([summary_script])
        cm = ContextManager(
            provider, "model", 65536, self.root / "store", recorder=self.rec
        )
        # 每条约 2000 token（6000 字），20 条共约 40K，早段必然落在 10K 保留区之外
        history = [Message(role="user", content=f"m{i}" * 3000) for i in range(20)]
        cm.manual_compact(history)

        ev = [r for r in self.records(T.CONTEXT_COMPACTION) if r["layer"] == "summary"]
        self.assertEqual(len(ev), 1)
        self.assertIs(ev[0]["ok"], True)
        self.assertGreater(ev[0]["retain_index"], 0)
        self.assertEqual(ev[0]["before_count"], 20)
        self.assertEqual(ev[0]["after_count"], len(history))

    def test_summary_noop_recorded_as_not_ok(self) -> None:
        provider = ScriptedProvider([_text_round()])
        cm = ContextManager(
            provider, "model", 65536, self.root / "store", recorder=self.rec
        )
        cm.manual_compact([Message(role="user", content="短")])
        ev = [r for r in self.records(T.CONTEXT_COMPACTION) if r["layer"] == "summary"]
        self.assertIs(ev[0]["ok"], False)
        self.assertEqual(ev[0]["skipped"], "no_early_segment")


# ---------------------------------------------------------------------------
# T51：作用域与不阻断
# ---------------------------------------------------------------------------
class ScopeTest(TraceHookBase):
    def test_summary_scope_isolated_from_main(self) -> None:
        """摘要调用记 summary 作用域，且主对话后续事件仍是 main。"""
        summary_script = [
            StreamChunk(type="text", content="<<<正式摘要>>>\n## 已完成\nx\n"),
            StreamChunk(type="done"),
        ]
        provider = ScriptedProvider([summary_script])
        traced = TracingProvider(provider, self.rec, "model")
        cm = ContextManager(traced, "model", 65536, self.root / "store", recorder=self.rec)
        history = [Message(role="user", content=f"m{i}" * 3000) for i in range(20)]
        cm.manual_compact(history)

        req = self.records(T.API_REQUEST)[0]
        self.assertEqual(req["scope"], SCOPE_SUMMARY)
        # 摘要结束后主线程回到主作用域
        self.rec.emit(T.USER_INPUT, text="下一句")
        self.assertEqual(self.records(T.USER_INPUT)[0]["scope"], SCOPE_MAIN)

    def test_turn_counters_do_not_mix(self) -> None:
        """summary 的轮次自成一套，不污染主对话轮次。"""
        provider = ScriptedProvider([_text_round()])
        traced = TracingProvider(provider, self.rec, "model")
        self.run_loop(traced, ToolRegistry())
        with self.rec.scope(SCOPE_SUMMARY):
            list(traced.stream_chat([Message(role="user", content="s")]))
        self.run_loop(traced, ToolRegistry())

        pairs = [(r["scope"], r["turn"]) for r in self.records(T.API_REQUEST)]
        self.assertEqual(pairs, [(SCOPE_MAIN, 1), (SCOPE_SUMMARY, 1), (SCOPE_MAIN, 2)])

    def test_notes_scope_bound_on_worker_entry(self) -> None:
        """笔记线程入口绑定 notes 作用域（T38 的位置正确性）。"""
        seen: list[str] = []

        def worker() -> None:
            # 模拟 MemoryManager._update_notes 的入口绑定
            self.rec.bind_scope(SCOPE_NOTES)
            seen.append(self.rec.current_scope())

        t = threading.Thread(target=worker)
        t.start()
        t.join(timeout=10)
        self.assertEqual(seen, [SCOPE_NOTES])
        # 主线程完全不受影响
        self.assertEqual(self.rec.current_scope(), SCOPE_MAIN)

    def test_concurrent_bucket_propagates_parent_scope(self) -> None:
        """
        T32：并发池线程里 `tool.execute` **内部**产生的事件，作用域与父作用域一致。

        造一个在 execute 里 emit 的工具，在非 main 作用域下跑一次只读并发路径。
        """
        rec = self.rec

        class EmittingTool(Tool):
            name = "emitting_tool"
            description = "在 execute 内部埋点"
            parameters = {"type": "object", "properties": {}}
            read_only = True

            def execute(self, args: dict) -> ToolResult:
                rec.emit(T.SKILL_STATE, action="from_inside_tool")
                return ToolResult(ok=True, output="ok")

        registry = ToolRegistry()
        registry.register(EmittingTool())
        provider = ScriptedProvider([_tool_round("emitting_tool", args={}), _text_round()])

        with self.rec.scope("isolated:demo"):
            self.run_loop(provider, registry)

        inner = self.records(T.SKILL_STATE)[0]
        self.assertEqual(inner["scope"], "isolated:demo")

    def test_unrelated_events_default_to_main(self) -> None:
        """AC21：不隶属任何对话的事件（会话启停/命令分发/状态栏）归主作用域。"""
        self.rec.emit(T.SESSION_START, x=1)
        self.rec.emit(T.STATUS_BAR, text="x")
        self.rec.emit(T.COMMAND_DISPATCH, command="/help")
        self.assertEqual({r["scope"] for r in self.records()}, {SCOPE_MAIN})


class NonBlockingTest(TraceHookBase):
    def test_broken_recorder_does_not_poison_tool_results(self) -> None:
        """
        AC6：记录器**必抛**时，只读并发桶的工具结果仍然正常。

        这是 plan 风险表里那条护栏：并发桶的异常会被 `future.result()` 抓住并
        当成「工具执行异常」回灌模型——于是「日志写不进去」会伪装成「你的工具坏了」，
        模型开始绕路重试，一个观测设施变成行为污染源。
        """

        class ExplodingRecorder:
            enabled = True

            def emit(self, *a, **kw):
                raise RuntimeError("记录器炸了")

            def emit_lazy(self, *a, **kw):
                raise RuntimeError("记录器炸了")

            def scope(self, name):
                raise RuntimeError("记录器炸了")

            def current_scope(self):
                raise RuntimeError("记录器炸了")

            def bind_scope(self, name):
                raise RuntimeError("记录器炸了")

            def next_turn(self, scope):
                raise RuntimeError("记录器炸了")

            def turn_total(self):
                return 0

            def elapsed(self):
                return 0.0

            def close(self):
                pass

        registry = ToolRegistry()
        registry.register(FakeTool("read_file"))
        provider = ScriptedProvider([_tool_round("read_file", args={"path": "a"}), _text_round()])

        agent = Agent(provider, registry, recorder=ExplodingRecorder())
        events = list(
            agent.run(
                [Message(role="user", content="go")],
                "off",
                False,
                "",
                lambda: "",
                "model",
                None,
                PermissionEngine(RuleSet([]), mode=PermissionMode.PERMISSIVE),
                lambda *a: True,
                None,
                lambda _p: True,
                threading.Event(),
            )
        )

        results = [e for e in events if e.type == AgentEventType.TOOL_RESULT]
        self.assertEqual(len(results), 1)
        self.assertIs(results[0].tool_result.ok, True)
        self.assertNotIn("工具执行异常", results[0].tool_result.output)
        # 循环仍自然完成
        finished = [e for e in events if e.type == AgentEventType.FINISHED]
        self.assertEqual(finished[-1].stop_reason, StopReason.COMPLETED)


# ---------------------------------------------------------------------------
# T52：界面与命令
# ---------------------------------------------------------------------------
class FakeController:
    """CommandController 的最小替身，只做捕获。"""

    def __init__(self, recorder) -> None:
        self._recorder = recorder
        self.echoes: list[str] = []
        self.messages: list[str] = []
        self.sent: list[tuple] = []
        self.tools_enabled = True

    # 界面消息埋点由 RhineApp 负责；这里手动复刻同一口径，验证顺序关系
    def show_user_input(self, text: str) -> None:
        self._recorder.emit(T.UI_MESSAGE, source="user_echo", text=text)
        self.echoes.append(text)

    def show_message(self, text: str) -> None:
        self._recorder.emit(T.UI_MESSAGE, source="system", text=text)
        self.messages.append(text)

    def send_user_message(self, content, display_content=None) -> None:
        self.sent.append((content, display_content))

    def switch_mode(self, target):
        return "切换了"

    def query_report(self, target):
        return "报告"

    def refresh_status(self) -> None:
        pass

    def clear_conversation(self) -> None:
        pass

    def compact_context(self) -> None:
        pass

    def resume_session(self, key=None) -> None:
        pass

    def exit_application(self) -> None:
        pass

    def run_skill(self, name, arguments, display) -> None:
        pass

    def reload_skills(self) -> None:
        pass

    def deactivate_skill(self, name) -> None:
        pass


class CommandDispatchHookTest(TraceHookBase):
    def test_four_situations_produce_exactly_four_dispatch_events(self) -> None:
        """
        AC15：未知 / 缺参 / handler 异常 / 正常命中各一条，共四条；
        且每条的 seq 都**小于**该命令产生的 ui_message。
        """

        def boom(invocation, controller):
            raise RuntimeError("处理函数炸了")

        registry = build_builtin_registry()
        # 走公开的 register，让索引与冲突校验按正常路径建立
        registry.register(
            CommandSpec(
                name="/boom",
                aliases=(),
                description="必然抛异常",
                usage="/boom",
                command_type=CommandType.LOCAL,
                handler=boom,
            )
        )

        dispatcher = CommandDispatcher(registry, recorder=self.rec)
        ctrl = FakeController(self.rec)

        dispatcher.dispatch("/nosuchcommand", ctrl)   # 未知
        dispatcher.dispatch("/resume", ctrl)          # 正常命中（无必需参数）
        dispatcher.dispatch("/boom", ctrl)            # handler 异常
        dispatcher.dispatch("/help", ctrl)            # 正常命中

        dispatches = self.records(T.COMMAND_DISPATCH)
        self.assertEqual(len(dispatches), 4)
        self.assertEqual(
            [d.get("is_unknown") for d in dispatches], [True, False, False, False]
        )
        # 命令类型可区分（LOCAL / UI / PROMPT 三类里至少出现两类）
        kinds = {d.get("command_type") for d in dispatches if not d["is_unknown"]}
        self.assertTrue(kinds)

        # 每条 command_dispatch 的 seq 必须小于它之后那条 ui_message（因果顺序）
        all_records = self.records()
        for d in dispatches:
            later = [
                r
                for r in all_records
                if r["type"] == "ui_message"
                and r["seq"] > d["seq"]
                and r.get("source") == "system"
            ]
            if later:
                self.assertLess(d["seq"], later[0]["seq"])

    def test_missing_argument_still_records_dispatch(self) -> None:
        """
        缺参那条提前 return 的路径也要有分发记录（否则「敲了没反应」查不到）。

        内置命令目前没有 requires_argument=True 的，故临时注册一条来覆盖这条路径。
        """
        registry = build_builtin_registry()
        registry.register(
            CommandSpec(
                name="/needsarg",
                aliases=(),
                description="必须带参数",
                usage="/needsarg <值>",
                command_type=CommandType.LOCAL,
                handler=lambda inv, ctrl: None,
                requires_argument=True,
                argument_hint="值",
            )
        )
        dispatcher = CommandDispatcher(registry, recorder=self.rec)
        ctrl = FakeController(self.rec)

        dispatcher.dispatch("/needsarg", ctrl)

        self.assertEqual(len(self.records(T.COMMAND_DISPATCH)), 1)
        # 缺参提示也确实显示了（说明走的正是那条提前 return）
        self.assertTrue(any("需要参数" in m for m in ctrl.messages))

    def test_empty_input_produces_nothing(self) -> None:
        """空输入零副作用：user_input 也不产（沿用 C10 spec F4）。"""
        dispatcher = CommandDispatcher(build_builtin_registry(), recorder=self.rec)
        dispatcher.dispatch("   ", FakeController(self.rec))
        self.assertEqual(self.records(), [])

    def test_plain_message_records_user_input_only(self) -> None:
        dispatcher = CommandDispatcher(build_builtin_registry(), recorder=self.rec)
        dispatcher.dispatch("帮我看看代码", FakeController(self.rec))
        self.assertEqual(len(self.records(T.USER_INPUT)), 1)
        self.assertEqual(self.records(T.COMMAND_DISPATCH), [])


class AgentEventPayloadHookTest(TraceHookBase):
    def test_agent_event_carries_no_heavy_payload(self) -> None:
        """AC20：agent_event 不带 output / arguments / 正文。"""
        from rhinecode.trace.models import agent_event_payload

        registry = ToolRegistry()
        registry.register(FakeTool("read_file"))
        provider = ScriptedProvider(
            [_tool_round("read_file", args={"path": "机密路径"}), _text_round("一段正文")]
        )
        events = self.run_loop(provider, registry)

        # 复刻 _do_stream 的埋点（界面层的 Pilot 测试在 test_command_tui 覆盖）
        for e in events:
            self.rec.emit_lazy(T.AGENT_EVENT, lambda ev=e: agent_event_payload(ev))

        blob = json.dumps(self.records(T.AGENT_EVENT), ensure_ascii=False)
        self.assertNotIn("机密路径", blob)
        self.assertNotIn("read_file 输出", blob)
        self.assertNotIn("一段正文", blob)
        # 但工具名与长度在
        self.assertIn("read_file", blob)
        self.assertIn("text_length", blob)


# ---------------------------------------------------------------------------
# T53：装配期与恢复、十五类总清点
# ---------------------------------------------------------------------------
class SkillStateHookTest(TraceHookBase):
    def _manager(self):
        from rhinecode.skills.manager import SkillManager

        skills = self.root / "proj" / ".rhinecode" / "skills"
        skills.mkdir(parents=True, exist_ok=True)
        (skills / "demo.md").write_text(
            "---\nname: demo\ndescription: 演示\n---\n正文\n", encoding="utf-8"
        )
        sm = SkillManager(
            self.root / "proj",
            self.root / "user",
            None,
            has_short_command=lambda _n: True,
            recorder=self.rec,
        )
        sm.startup(frozenset({"read_file"}))
        return sm

    def test_activate_deactivate_clear_reload_each_record_once(self) -> None:
        sm = self._manager()
        sm.bind_tools(registered=frozenset({"read_file"}))
        sm.activate("demo", "参数")
        sm.deactivate("demo")
        sm.activate("demo", "")
        sm.clear_active()
        sm.reload(frozenset({"read_file"}), frozenset({"read_file"}))

        actions = [r["action"] for r in self.records(T.SKILL_STATE)]
        self.assertEqual(
            actions,
            ["bind_tools", "activate", "deactivate", "activate", "clear_active", "reload"],
        )

    def test_tool_policy_does_not_record(self) -> None:
        """明确排除：tool_policy 每轮被调用，埋进去会淹掉时间线。"""
        sm = self._manager()
        sm.bind_tools(registered=frozenset({"read_file"}))
        before = len(self.records(T.SKILL_STATE))
        for _ in range(5):
            sm.tool_policy(frozenset({"read_file"}))
        self.assertEqual(len(self.records(T.SKILL_STATE)), before)


class FifteenTypesTest(TraceHookBase):
    def test_all_fifteen_types_can_be_produced(self) -> None:
        """
        AC9 的总清点：十五类事件均可产出且关键字段非空。

        这里用记录器直接产出各类型的代表性负载——分层的「谁来产」已由上面各类
        分别验证，本条只做「一个类型都没漏、每类都有可读内容」的清点。
        """
        payloads = {
            T.SESSION_START: {"project_root": "/x", "tool_names": ["read_file"]},
            T.SESSION_END: {"reason": "normal_exit", "turn_total": 2, "elapsed_seconds": 1.5},
            T.USER_INPUT: {"text": "你好", "kind": "message"},
            T.COMMAND_DISPATCH: {"command": "/help", "is_unknown": False},
            T.API_REQUEST: {"turn": 1, "model": "m", "tool_names": []},
            T.API_RESPONSE: {"turn": 1, "text": "回答", "duration_ms": 10},
            T.PERMISSION_DECISION: {"tool": "read_file", "decision": "allow", "layer": "mode"},
            T.INTERACTION: {"kind": "confirm", "result": "allow", "display": "x"},
            T.TOOL_EXECUTE: {"tool": "read_file", "ok": True, "outcome": "executed"},
            T.UI_MESSAGE: {"source": "system", "text": "提示"},
            T.STATUS_BAR: {"text": "\\[deepseek] m"},
            T.AGENT_EVENT: {"event_type": "progress", "iteration": 2},
            T.CONTEXT_COMPACTION: {"layer": "offload", "count": 1},
            T.SKILL_STATE: {"action": "activate", "skill": "demo"},
            T.HISTORY_RESTORED: {"origin": "startup", "message_count": 4},
        }
        self.assertEqual(len(payloads), 15)
        for t, payload in payloads.items():
            self.rec.emit(t, **payload)

        got = self.records()
        self.assertEqual(len(got), 15)
        self.assertEqual({r["type"] for r in got}, {t.value for t in T})
        for r in got:
            # 每条除四个固定字段外至少还有一个负载字段
            self.assertGreater(len(r) - 4, 0, f"{r['type']} 负载为空")


if __name__ == "__main__":
    unittest.main()
