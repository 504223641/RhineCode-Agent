import os
import tempfile
import unittest
from pathlib import Path

from rhinecode.conversation import ConversationManager
from rhinecode.provider.base import BaseProvider, Message, StreamChunk, ToolCall
from rhinecode.agent.events import ConfirmDecision, StopReason
from rhinecode.tools.base import Tool, ToolResult
from rhinecode.tools.edit_file import EditFileTool
from rhinecode.tools.glob_files import GlobTool
from rhinecode.tools.grep_content import GrepTool
from rhinecode.tools.read_file import ReadFileTool
from rhinecode.tools.registry import ToolRegistry
from rhinecode.tools.write_file import WriteFileTool

from rhinecode.config import Config

try:
    from rhinecode.config import load
except ModuleNotFoundError:
    load = None


class TempWorkspaceTest(unittest.TestCase):
    def setUp(self) -> None:
        self._old_cwd = os.getcwd()
        self._workspace = tempfile.TemporaryDirectory()
        self._outside = tempfile.TemporaryDirectory()
        os.chdir(self._workspace.name)

    def tearDown(self) -> None:
        os.chdir(self._old_cwd)
        self._workspace.cleanup()
        self._outside.cleanup()

    def outside_path(self, name: str) -> Path:
        return Path(self._outside.name) / name


class PathGuardTests(TempWorkspaceTest):
    def test_file_tools_reject_paths_outside_workspace(self) -> None:
        Path("inside.txt").write_text("hello\n", encoding="utf-8")
        outside = self.outside_path("outside.txt")
        outside.write_text("secret\n", encoding="utf-8")

        self.assertTrue(ReadFileTool().execute({"path": "inside.txt"}).ok)

        read_result = ReadFileTool().execute({"path": str(outside)})
        self.assertFalse(read_result.ok)
        self.assertEqual(read_result.summary, "路径越界")

        write_result = WriteFileTool().execute({"path": str(outside), "content": "changed"})
        self.assertFalse(write_result.ok)
        self.assertEqual(outside.read_text(encoding="utf-8"), "secret\n")

        edit_result = EditFileTool().execute({
            "path": str(outside),
            "old_string": "secret",
            "new_string": "changed",
        })
        self.assertFalse(edit_result.ok)
        self.assertEqual(edit_result.summary, "路径越界")

    def test_parent_references_are_rejected(self) -> None:
        result = ReadFileTool().execute({"path": "../outside.txt"})
        self.assertFalse(result.ok)
        self.assertEqual(result.summary, "路径越界")

    def test_glob_and_grep_reject_external_inputs(self) -> None:
        outside = self.outside_path("outside.txt")
        outside.write_text("needle\n", encoding="utf-8")

        glob_result = GlobTool().execute({"pattern": "../*"})
        self.assertFalse(glob_result.ok)
        self.assertEqual(glob_result.summary, "路径越界")

        grep_result = GrepTool().execute({"pattern": "needle", "path": str(outside)})
        self.assertFalse(grep_result.ok)
        self.assertEqual(grep_result.summary, "路径越界")

    def test_glob_skips_symlinks_to_external_files(self) -> None:
        outside = self.outside_path("outside.txt")
        outside.write_text("secret\n", encoding="utf-8")
        link = Path("external_link.txt")
        try:
            link.symlink_to(outside)
        except (OSError, NotImplementedError):
            self.skipTest("symlink creation is not available in this environment")

        result = GlobTool().execute({"pattern": "**/*"})
        self.assertTrue(result.ok)
        self.assertNotIn("external_link.txt", result.output)

        read_result = ReadFileTool().execute({"path": "external_link.txt"})
        self.assertFalse(read_result.ok)
        self.assertEqual(read_result.summary, "路径越界")

    def test_large_read_file_requires_explicit_range(self) -> None:
        lines = [f"line {i}\n" for i in range(1, 140000)]
        Path("large.txt").write_text("".join(lines), encoding="utf-8")

        full = ReadFileTool().execute({"path": "large.txt"})
        self.assertFalse(full.ok)
        self.assertEqual(full.summary, "文件过大")

        ranged = ReadFileTool().execute({"path": "large.txt", "start_line": 10, "max_lines": 3})
        self.assertTrue(ranged.ok)
        self.assertIn("10│ line 10", ranged.output)
        self.assertIn("12│ line 12", ranged.output)
        self.assertNotIn("13│ line 13", ranged.output)
        self.assertIn("范围读取 3 行", ranged.summary)


class RecordingTool(Tool):
    name = "danger"
    description = "records execution"
    parameters = {"type": "object", "properties": {}}
    read_only = False

    def __init__(self) -> None:
        self.executed = False

    def execute(self, args: dict) -> ToolResult:
        self.executed = True
        return ToolResult(ok=True, output="executed", summary="executed")


class ToolCallingProvider(BaseProvider):
    def __init__(self, second_error: bool = False) -> None:
        self.calls = 0
        self.second_error = second_error

    def stream_chat(
        self,
        messages: list[Message],
        thinking_effort: str = "off",
        tools: list[dict] | None = None,
        system: str | None = None,
    ):
        self.calls += 1
        if self.calls == 1:
            yield StreamChunk(
                type="tool_call",
                tool_call=ToolCall(id="call-1", name="danger", arguments={}),
            )
            yield StreamChunk(type="done")
            return

        if self.second_error:
            yield StreamChunk(type="text", content="partial")
            yield StreamChunk(type="error", content="boom")
            return

        yield StreamChunk(type="text", content="final")
        yield StreamChunk(type="done")


class PlanProvider(BaseProvider):
    def __init__(self, plan: str, call_danger_after_plan: bool = False) -> None:
        self.plan = plan
        self.call_danger_after_plan = call_danger_after_plan
        self.calls = 0

    def stream_chat(
        self,
        messages: list[Message],
        thinking_effort: str = "off",
        tools: list[dict] | None = None,
        system: str | None = None,
    ):
        self.calls += 1
        if self.calls == 1:
            yield StreamChunk(
                type="tool_call",
                tool_call=ToolCall(
                    id="plan-1",
                    name="present_plan",
                    arguments={"plan": self.plan},
                ),
            )
            yield StreamChunk(type="done")
            return

        if self.call_danger_after_plan and self.calls == 2:
            yield StreamChunk(
                type="tool_call",
                tool_call=ToolCall(id="danger-1", name="danger", arguments={}),
            )
            yield StreamChunk(type="done")
            return

        yield StreamChunk(type="text", content="final")
        yield StreamChunk(type="done")


def manager_with_tool(provider: BaseProvider, tool: RecordingTool) -> ConversationManager:
    registry = ToolRegistry()
    registry.register(tool)
    # c5：ConversationManager 改为接收整份 Config。测试里 debug_log=False，避免在临时工作区写日志文件。
    config = Config(
        protocol="deepseek",
        model="test-model",
        base_url="http://test",
        api_key="test-key",
        debug_log=False,
    )
    return ConversationManager(provider, config, registry)


class PermissionFilterTests(TempWorkspaceTest):
    def _manager_with_default_tools(self) -> tuple[ConversationManager, ToolRegistry]:
        Path(".rhinecode").mkdir(exist_ok=True)
        Path(".rhinecode/permissions.yaml").write_text(
            'deny:\n  - "Read(config.yaml)"\n',
            encoding="utf-8",
        )
        registry = ToolRegistry.default()
        config = Config(
            protocol="deepseek",
            model="test-model",
            base_url="http://test",
            api_key="test-key",
            debug_log=False,
        )
        return ConversationManager(ToolCallingProvider(), config, registry), registry

    def test_grep_skips_files_denied_by_read_rules(self) -> None:
        Path("config.yaml").write_text("api_key: SECRET\n", encoding="utf-8")
        Path("visible.txt").write_text("api_key: visible\n", encoding="utf-8")
        _manager, registry = self._manager_with_default_tools()

        result = registry.get("grep_content").execute({"pattern": "api_key", "path": "."})

        self.assertTrue(result.ok)
        self.assertIn("visible.txt", result.output)
        self.assertIn("visible", result.output)
        self.assertNotIn("SECRET", result.output)
        self.assertNotIn("config.yaml", result.output)
        self.assertIn("跳过 1 个", result.summary)

    def test_glob_skips_files_denied_by_read_rules(self) -> None:
        Path("config.yaml").write_text("api_key: SECRET\n", encoding="utf-8")
        Path("visible.txt").write_text("ok\n", encoding="utf-8")
        _manager, registry = self._manager_with_default_tools()

        result = registry.get("glob_files").execute({"pattern": "*"})

        self.assertTrue(result.ok)
        self.assertIn("visible.txt", result.output)
        self.assertNotIn("config.yaml", result.output)
        self.assertIn("跳过 1 个", result.summary)


class PermanentAllowFallbackTests(TempWorkspaceTest):
    def test_broken_local_permissions_file_falls_back_to_session_rule(self) -> None:
        Path(".rhinecode").mkdir(exist_ok=True)
        local_path = Path(".rhinecode/permissions.local.yaml")
        broken = "allow: [unclosed\n"
        local_path.write_text(broken, encoding="utf-8")
        tool = RecordingTool()
        manager = manager_with_tool(ToolCallingProvider(), tool)
        manager.confirm_callback = lambda _tc, _tool, _dec: ConfirmDecision.ALLOW_PERMANENT

        list(manager.handle_input("run it"))

        self.assertTrue(tool.executed)
        self.assertEqual(local_path.read_text(encoding="utf-8"), broken)
        self.assertEqual(len(manager._engine.session_rules), 1)
        self.assertTrue(any("写入本地权限配置失败" in err for err in manager._engine.load_errors))


class ConversationManagerTests(unittest.TestCase):
    """
    c4：handle_input 现在返回 AgentEvent 事件流（不再是 StreamChunk）。
    AgentEventType 继承 str，故 `event.type == "tool_result"` 等字符串比较仍成立。
    """

    def test_side_effect_tool_without_confirm_callback_is_rejected(self) -> None:
        # fail-closed：未注入确认回调时，有副作用工具绝不执行（confirm 闭包对 None 回调返回 False）
        tool = RecordingTool()
        manager = manager_with_tool(ToolCallingProvider(), tool)

        events = list(manager.handle_input("run it"))

        self.assertFalse(tool.executed)
        results = [e for e in events if e.type == "tool_result"]
        self.assertEqual(len(results), 1)
        self.assertFalse(results[0].tool_result.ok)

    def test_confirm_callback_controls_side_effect_execution(self) -> None:
        # c6：副作用工具在默认档判为 ASK，确认回调（四态）决定是否执行；签名为 (tc, tool, decision)
        denied_tool = RecordingTool()
        denied_manager = manager_with_tool(ToolCallingProvider(), denied_tool)
        denied_manager.confirm_callback = lambda _tc, _tool, _dec: ConfirmDecision.DENY
        list(denied_manager.handle_input("run it"))
        self.assertFalse(denied_tool.executed)

        approved_tool = RecordingTool()
        approved_manager = manager_with_tool(ToolCallingProvider(), approved_tool)
        approved_manager.confirm_callback = lambda _tc, _tool, _dec: ConfirmDecision.ALLOW
        list(approved_manager.handle_input("run it"))
        self.assertTrue(approved_tool.executed)

    def test_session_allow_registers_session_rule(self) -> None:
        # c6：选 ALLOW_SESSION 后执行本次，并向引擎登记一条会话级 allow 规则（取代 c5 的一刀切免确认）
        tool = RecordingTool()
        manager = manager_with_tool(ToolCallingProvider(), tool)
        calls = {"n": 0}

        def cb(_tc, _tool, _dec):
            calls["n"] += 1
            return ConfirmDecision.ALLOW_SESSION

        manager.confirm_callback = cb
        list(manager.handle_input("run it"))
        self.assertTrue(tool.executed)
        self.assertEqual(calls["n"], 1)
        # 会话级规则已登记：工具名 danger（未映射 → rule_name 取工具自身名），来源 session
        self.assertEqual(len(manager._engine.session_rules), 1)
        self.assertEqual(manager._engine.session_rules[0].effect, "allow")
        self.assertEqual(manager._engine.session_rules[0].source, "session")

    def test_present_plan_is_emitted_as_full_text_event(self) -> None:
        plan = "1. Read the current code\n2. Update the approval flow\n3. Run regression tests"
        tool = RecordingTool()
        provider = PlanProvider(plan)
        manager = manager_with_tool(provider, tool)
        manager.plan_mode = True
        manager.approve_plan_callback = lambda _plan: False

        events = list(manager.handle_input("make a plan"))

        self.assertIn(plan, [e.text for e in events if e.type == "text"])
        self.assertEqual(provider.calls, 1)
        self.assertEqual(events[-1].type, "finished")
        self.assertEqual(events[-1].stop_reason, StopReason.PLAN_REJECTED)
        self.assertNotIn("final", [e.text for e in events if e.type == "text"])

    def test_rejected_plan_stops_before_followup_tool_calls(self) -> None:
        tool = RecordingTool()
        provider = PlanProvider("Do the thing", call_danger_after_plan=True)
        manager = manager_with_tool(provider, tool)
        manager.plan_mode = True
        manager.approve_plan_callback = lambda _plan: False

        events = list(manager.handle_input("make a plan"))

        self.assertEqual(provider.calls, 1)
        self.assertFalse(tool.executed)
        self.assertEqual(events[-1].type, "finished")
        self.assertEqual(events[-1].stop_reason, StopReason.PLAN_REJECTED)

    def test_plan_approval_does_not_skip_side_effect_confirmation(self) -> None:
        tool = RecordingTool()
        manager = manager_with_tool(PlanProvider("Approved plan", call_danger_after_plan=True), tool)
        manager.plan_mode = True
        manager.approve_plan_callback = lambda _plan: True
        confirm_calls = {"n": 0}

        def deny(_tc, _tool, _dec):
            confirm_calls["n"] += 1
            return ConfirmDecision.DENY

        manager.confirm_callback = deny

        events = list(manager.handle_input("make a plan"))

        self.assertEqual(confirm_calls["n"], 1)
        self.assertFalse(tool.executed)
        danger_results = [
            e for e in events
            if e.type == "tool_result" and e.tool_call and e.tool_call.id == "danger-1"
        ]
        self.assertEqual(len(danger_results), 1)
        self.assertFalse(danger_results[0].tool_result.ok)

    def test_second_round_error_ends_with_finished_and_no_partial_answer(self) -> None:
        # c4：二轮流出错 → 先产出 ERROR，再以 FINISHED(STREAM_ERROR) 收尾；不追加 partial 文本
        tool = RecordingTool()
        manager = manager_with_tool(ToolCallingProvider(second_error=True), tool)
        manager.confirm_callback = lambda _tc, _tool, _dec: ConfirmDecision.ALLOW

        events = list(manager.handle_input("run it"))
        types = [e.type for e in events]

        self.assertIn("error", types)
        self.assertEqual(types[-1], "finished")
        self.assertNotIn("done", types)
        self.assertFalse(
            any(
                msg.role == "assistant" and msg.content == "partial" and not msg.tool_calls
                for msg in manager.history
            )
        )


@unittest.skipIf(load is None, "PyYAML is not installed in this Python environment")
class ConfigLoadTests(unittest.TestCase):
    def write_config(self, text: str) -> str:
        handle = tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False)
        with handle:
            handle.write(text)
        self.addCleanup(lambda: Path(handle.name).unlink(missing_ok=True))
        return handle.name

    def test_empty_config_is_value_error(self) -> None:
        with self.assertRaisesRegex(ValueError, "顶层必须是 YAML 对象"):
            load(self.write_config(""))

    def test_non_mapping_config_is_value_error(self) -> None:
        with self.assertRaisesRegex(ValueError, "顶层必须是 YAML 对象"):
            load(self.write_config("- item\n"))

    def test_invalid_yaml_is_value_error(self) -> None:
        with self.assertRaisesRegex(ValueError, "YAML 解析失败"):
            load(self.write_config("protocol: [unterminated\n"))

    def test_missing_required_field_is_value_error(self) -> None:
        with self.assertRaisesRegex(ValueError, "api_key"):
            load(self.write_config("protocol: deepseek\nmodel: x\nbase_url: https://example.test\n"))
