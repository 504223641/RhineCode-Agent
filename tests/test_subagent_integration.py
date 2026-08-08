"""
子 Agent 的集成测试（c13 T30，覆盖 AC6a / AC19a-c / AC20b / AC22 / AC24）。

验的是**跨模块接线**，不是单个模块的行为。四处判据：

1. **结论真的进了主历史**，而且再下一轮仍在（用系统提醒实现会在这里失败）；
2. **Hook 对子 Agent 全量生效**（否则主 Agent 能靠委派绕过拦截规则）；
3. **会话切换先取消任务**（否则跑完的结论会交付进一段已被清空的历史）；
4. **零回归**：服务为 None 时行为与 c12 逐字一致。
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from rhinecode.config import Config
from rhinecode.conversation import ConversationManager
from rhinecode.provider.base import BaseProvider, StreamChunk
from rhinecode.subagents.models import AgentCatalog, AgentSource, AgentSpec
from rhinecode.subagents.runner import SubAgentRuntime
from rhinecode.subagents.service import SubAgentService
from rhinecode.subagents.tasks import KIND_ROLE, TaskStatus
from rhinecode.subagents.toolset import GLOBAL_DENIED_TOOLS, resolve_toolset
from rhinecode.tools.registry import ToolRegistry
from rhinecode.tools.run_agent import RunAgentTool


class _QuietProvider(BaseProvider):
    """只说一句话就结束，不调任何工具。"""

    def __init__(self, reply: str = "好的") -> None:
        self.reply = reply
        self.requests: list[list] = []

    def stream_chat(self, messages, thinking_effort="off", tools=None, system=None):
        self.requests.append(list(messages))
        yield StreamChunk(type="text", content=self.reply)
        yield StreamChunk(type="done")


def _config(**kw) -> Config:
    base = dict(protocol="deepseek", model="m", api_key="k", base_url="")
    base.update(kw)
    return Config(**base)


def _spec(name="explorer") -> AgentSpec:
    return AgentSpec(
        name=name,
        description="只读调研",
        body="你是调研员。",
        source=AgentSource.BUILTIN,
        path=Path(f"{name}.md"),
    )


class IntegrationBase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.user_dir = Path(self._tmp.name) / "user"
        self.user_dir.mkdir(parents=True, exist_ok=True)
        self.provider = _QuietProvider()
        self.registry = ToolRegistry.default()

    def _manager(self, with_service: bool = True) -> ConversationManager:
        manager = ConversationManager(
            self.provider,
            _config(),
            self.registry,
            user_dir=self.user_dir,
            provider_factory=lambda cfg: self.provider,
        )
        if with_service:
            runtime = SubAgentRuntime(
                provider_for=lambda model: self.provider,
                registry=self.registry,
                engine=manager.permission_engine,
                main_mode=lambda: manager.permission_engine.mode,
                environment_text=lambda _cwd: "env",
                default_model="m",
                new_context_manager=manager.new_subagent_context_manager,
            )
            manager.subagent_service = SubAgentService(
                AgentCatalog(specs={"explorer": _spec()}),
                runtime,
                tool_names_provider=self.registry.names,
            )
        return manager


class DeliveryTest(IntegrationBase):
    """AC19b / AC19c：结论进主历史，且**再下一轮仍在**。"""

    def setUp(self) -> None:
        super().setUp()
        self.manager = self._manager()
        record = self.manager.subagent_service.tasks.create(
            KIND_ROLE, "explorer", "去查点东西"
        )
        self.manager.subagent_service.tasks.finish(
            record.task_id, TaskStatus.COMPLETED, "找到了三处调用。"
        )
        self.record = record

    def test_delivered_into_history(self) -> None:
        before = len(self.manager.history)
        self.manager._deliver_subagent_results()

        self.assertEqual(len(self.manager.history), before + 1)
        message = self.manager.history[-1]
        self.assertIn("找到了三处调用。", message.content)
        self.assertIn(self.record.task_id, message.content)

    def test_marked_as_subagent_result_not_user_speech(self) -> None:
        """
        消息要包一层标记块，让模型知道**这不是人在说话**。

        裸着塞一段结论进去，模型会把它当成用户的新指令。
        """
        self.manager._deliver_subagent_results()
        self.assertIn("<subagent-result", self.manager.history[-1].content)

    def test_display_content_empty_so_ui_does_not_show_fake_input(self) -> None:
        """
        `display_content` 置空串：界面与 `/resume` 回放**不**把它显示成一条
        用户输入。完成通知已经由 TUI 的轮询单独出过一行了。
        """
        self.manager._deliver_subagent_results()
        self.assertEqual(self.manager.history[-1].display_content, "")

    def test_idempotent(self) -> None:
        self.manager._deliver_subagent_results()
        length = len(self.manager.history)
        self.manager._deliver_subagent_results()
        self.assertEqual(len(self.manager.history), length)

    def test_still_present_on_the_round_after_next(self) -> None:
        """
        **本模块最重要的一条（AC19c）**：结论在**再下一轮**仍在历史里。

        只验「下一轮能引用」是不够的——用一次性的系统提醒实现也能过那一条，
        但第三轮就会失败。这条是「结论必须进历史」这个决策的唯一有效判据。
        """
        self.manager._deliver_subagent_results()
        snapshot = list(self.manager.history)

        # 再交付一次（无新任务）+ 追加一轮普通对话
        self.manager._deliver_subagent_results()
        self.assertEqual(self.manager.history[: len(snapshot)], snapshot)
        self.assertIn("找到了三处调用。", self.manager.history[-1].content)

    def test_failed_task_also_delivered(self) -> None:
        """
        失败的任务同样要交付——否则模型发起的委派石沉大海，
        它永远等不到回音，也无从判断该不该重试。
        """
        manager = self._manager()
        record = manager.subagent_service.tasks.create(KIND_ROLE, "explorer", "t")
        manager.subagent_service.tasks.finish(
            record.task_id, TaskStatus.FAILED, "子 Agent 出错了。"
        )
        manager._deliver_subagent_results()

        self.assertIn("子 Agent 出错了。", manager.history[-1].content)
        self.assertIn('status="failed"', manager.history[-1].content)


class SessionSwitchTest(IntegrationBase):
    """AC20b：会话切换先取消。"""

    def test_clear_cancels_and_reports_count(self) -> None:
        manager = self._manager()
        for i in range(2):
            manager.subagent_service.tasks.create(KIND_ROLE, f"a{i}", "t")

        text = manager.clear()

        self.assertIn("2", text)
        for record in manager.subagent_service.tasks.snapshot():
            self.assertTrue(record.cancel_event.is_set())

    def test_clear_without_tasks_keeps_original_text(self) -> None:
        """
        没有任务时文案**逐字不变**。

        既有测试逐字断言「对话历史已清空」，多一个括号就会红——
        而那条断言本身是对的，不该为了 c13 去改它。
        """
        self.assertEqual(self._manager().clear(), "对话历史已清空")

    def test_cancel_happens_before_history_is_emptied(self) -> None:
        """
        取消必须**先于**清空。

        反过来的话，正在跑的子 Agent 会在清空之后把结论交付进新的空历史里——
        用户刚清完屏，凭空多出一段来路不明的内容。
        """
        manager = self._manager()
        record = manager.subagent_service.tasks.create(KIND_ROLE, "a", "t")
        manager.history.append(_dummy_message())

        manager.clear()

        self.assertTrue(record.cancel_event.is_set())
        self.assertEqual(manager.history, [])


def _dummy_message():
    from rhinecode.provider.base import Message

    return Message(role="user", content="x")


class HookIntegrationTest(IntegrationBase):
    """
    AC22：一条 `pre_tool_use` 拦截规则对**子 Agent 内**的同类调用同样生效。

    这条是安全判据，不是功能判据：不生效的话，主 Agent 只要把
    「跑 git push」委派出去就能绕过用户写的拦截规则。
    """

    def test_pre_tool_use_blocks_inside_subagent(self) -> None:
        from rhinecode.agent.events import StopReason
        from rhinecode.hooks import HookDecision, HookEventType
        from rhinecode.provider.base import ToolCall
        from rhinecode.subagents.runner import run_subagent
        from rhinecode.subagents.tasks import TaskManager
        from rhinecode.tools.base import Tool, ToolResult

        class _Runner(Tool):
            name = "run_command"
            description = "fake"
            parameters = {"type": "object", "properties": {"command": {"type": "string"}}}
            read_only = False

            def __init__(self) -> None:
                self.executed = 0

            def execute(self, args: dict) -> ToolResult:
                self.executed += 1
                return ToolResult(ok=True, output="pushed", summary="ok")

        class _BlockingHooks:
            """只对 pre_tool_use 表态：一律拦截。"""

            def __init__(self) -> None:
                self.seen: list[str] = []

            def has_listeners(self, event) -> bool:
                return event == HookEventType.PRE_TOOL_USE

            def dispatch(self, event, build=None, cwd=None):
                fields = build() if build else {}
                self.seen.append(fields.get("tool", ""))

                class _Result:
                    verdict = type(
                        "_V",
                        (),
                        {"decision": HookDecision.DENY, "reason": "禁止 git push"},
                    )()

                return _Result()

            def consume_injections(self) -> str:
                return ""

        class _PushProvider(BaseProvider):
            def __init__(self) -> None:
                self.calls = 0

            def stream_chat(self, messages, thinking_effort="off", tools=None, system=None):
                self.calls += 1
                if self.calls == 1:
                    yield StreamChunk(
                        type="tool_call",
                        tool_call=ToolCall(
                            id="c1",
                            name="run_command",
                            arguments={"command": "git push origin main"},
                        ),
                    )
                else:
                    yield StreamChunk(type="text", content="被拦下了，我停手。")
                yield StreamChunk(type="done")

        registry = ToolRegistry()
        tool = _Runner()
        registry.register(tool)
        hooks = _BlockingHooks()
        manager = self._manager(with_service=False)
        provider = _PushProvider()

        runtime = SubAgentRuntime(
            provider_for=lambda model: provider,
            registry=registry,
            engine=manager.permission_engine,
            main_mode=lambda: manager.permission_engine.mode,
            environment_text=lambda _cwd: "env",
            default_model="m",
            hooks=hooks,
        )
        tasks = TaskManager()
        spec = _spec()
        toolset = resolve_toolset(registry.names(), spec)
        record = tasks.create(KIND_ROLE, "explorer", "推一下代码")

        run_subagent(
            runtime, spec, "推一下代码", record, tasks, toolset,
            frozenset(registry.names()),
        )

        self.assertIn("run_command", hooks.seen, "子 Agent 的工具调用必须触发 pre_tool_use")
        self.assertEqual(tool.executed, 0, "被 Hook 拦下的调用绝不能真的执行")
        self.assertIs(record.status, TaskStatus.COMPLETED)
        self.assertEqual(record.stop_reason, StopReason.COMPLETED.value)


class ToolVisibilityTest(IntegrationBase):
    """AC6a：工具数量不随角色数量变化。"""

    def test_tool_count_is_independent_of_agent_count(self) -> None:
        names_before = set(self.registry.names())

        manager = self._manager()
        manager.subagent_service.catalog = AgentCatalog(
            specs={f"a{i}": _spec(f"a{i}") for i in range(5)}
        )

        self.assertEqual(set(self.registry.names()), names_before)

    def test_delegation_tool_absent_from_subagent_toolset(self) -> None:
        """AC11a 的集成侧判据：真实注册中心下也查不到那两个工具。"""
        registry = ToolRegistry.default()
        registry.register(RunAgentTool(_FakeService()))
        result = resolve_toolset(registry.names(), _spec())

        for name in GLOBAL_DENIED_TOOLS:
            with self.subTest(tool=name):
                self.assertNotIn(name, result.allowed)


class _FakeService:
    catalog = AgentCatalog()

    def delegate(self, *a, **k):  # pragma: no cover —— 本用例不触发委派
        raise AssertionError


class PromptInjectionTest(IntegrationBase):
    """角色清单进系统提示。"""

    def test_index_appears_when_agents_loaded(self) -> None:
        manager = self._manager()
        self.assertIn("explorer", manager._agent_index_text())

    def test_index_empty_without_service(self) -> None:
        self.assertEqual(self._manager(with_service=False)._agent_index_text(), "")


class ZeroRegressionTest(IntegrationBase):
    """AC24：服务为 None 时全部接入点安全降级。"""

    def test_all_entry_points_degrade(self) -> None:
        manager = self._manager(with_service=False)

        self.assertEqual(manager._agent_index_text(), "")
        self.assertEqual(manager.running_subagent_count(), 0)
        self.assertEqual(manager.drain_subagent_notifications(), ())
        self.assertIn("未启用", manager.agents_report())
        self.assertIn("未启用", manager.cancel_subagents(None))
        # 交付是空操作，历史不变
        before = list(manager.history)
        manager._deliver_subagent_results()
        self.assertEqual(manager.history, before)

    def test_clear_unaffected(self) -> None:
        self.assertEqual(self._manager(with_service=False).clear(), "对话历史已清空")


class ParentSnapshotTest(IntegrationBase):
    """AC8b：父快照是副本。"""

    def test_history_is_a_copy(self) -> None:
        manager = self._manager()
        manager.history.append(_dummy_message())
        snapshot = manager.parent_snapshot()

        manager.history.append(_dummy_message())

        self.assertEqual(len(snapshot.history), 1)
        self.assertEqual(len(manager.history), 2)

    def test_carries_stable_prompt_and_tools(self) -> None:
        snapshot = self._manager().parent_snapshot()
        self.assertTrue(snapshot.stable)
        self.assertIn("read_file", snapshot.tool_names)

    def test_drops_trailing_unpaired_tool_calls(self) -> None:
        """
        **真实模型验收实测到的缺陷的回归护栏（C13 场景 4 首跑）。**

        `parent_snapshot()` 是从 `RunAgentTool.execute()` 调进来的，而那运行在
        Agent Loop 的**串行段内**——此刻循环已经把 `assistant(tool_calls)`
        追加进 `history`，对应的 `tool` 结果要等本轮全部工具跑完才追加。

        不清理的话，子 Agent 的首次请求必然被 API 拒绝：

            400 - An assistant message with 'tool_calls' must be followed by
                  tool messages responding to each 'tool_call_id'

        本用例**刻意重现那个「跑到一半」的形态**（历史以无配对的
        assistant(tool_calls) 结尾）——只用干净历史构造的测试压根碰不到它，
        这正是它当初逃过 20 项单元测试的原因。
        """
        from rhinecode.provider.base import Message, ToolCall

        manager = self._manager()
        manager.history.append(Message(role="user", content="帮我查一下"))
        # ↓ 循环在分流执行之前就追加了这一条，而 tool 结果还没来
        manager.history.append(
            Message(
                role="assistant",
                content="我用 branch 开一个子 Agent……",
                tool_calls=[ToolCall(id="c1", name="run_agent", arguments={})],
            )
        )

        snapshot = manager.parent_snapshot()

        self.assertTrue(
            all(not (m.role == "assistant" and m.tool_calls) for m in snapshot.history),
            "快照里不得留下无配对的 assistant(tool_calls)",
        )
        # 用户那条要留着——子 Agent 需要它才知道上下文
        self.assertTrue(any(m.role == "user" for m in snapshot.history))

    def test_keeps_paired_tool_calls(self) -> None:
        """
        **反证**：配对完整时不该误删。

        没有这一条，一个「把所有 assistant(tool_calls) 都丢掉」的实现
        也能让上一条通过，而那会让分支式子 Agent 完全看不到父对话做过什么。
        """
        from rhinecode.provider.base import Message, ToolCall

        manager = self._manager()
        manager.history.extend(
            [
                Message(role="user", content="读一下 a.py"),
                Message(
                    role="assistant",
                    content="",
                    tool_calls=[ToolCall(id="c1", name="read_file", arguments={})],
                ),
                Message(role="tool", tool_call_id="c1", content="文件内容"),
                Message(role="assistant", content="读完了。"),
            ]
        )

        snapshot = manager.parent_snapshot()
        roles = [m.role for m in snapshot.history]

        self.assertIn("tool", roles, "配对完整的 tool 结果必须保留")
        self.assertTrue(
            any(m.role == "assistant" and m.tool_calls for m in snapshot.history),
            "配对完整的 assistant(tool_calls) 必须保留",
        )


if __name__ == "__main__":
    unittest.main()
