"""
独立模式子对话单测（c11 T51）。

覆盖 spec AC18–AC23（子对话隔离、结论回流、配对结构、工具集排除 load_skill）
与 AC33（子对话独立迭代预算）。按 T49a/b/c 三段分组，便于定位失败。

用假 Provider 与真实 SkillManager；不触任何网络。
"""

import os
import tempfile
import unittest
from pathlib import Path

from rhinecode.config import Config
from rhinecode.conversation import ConversationManager
from rhinecode.agent.events import AgentEventType, StopReason
from rhinecode.provider.base import Message, StreamChunk, ToolCall
from rhinecode.skills.manager import SkillManager
from rhinecode.tools.base import Tool, ToolResult
from rhinecode.tools.registry import ToolRegistry


class ScriptedProvider:
    """
    按脚本逐轮响应的假 Provider，并记录每轮收到的 messages / system。

    :param scripts: 每轮的 chunk 列表；轮数超出时重复最后一项。
    """

    def __init__(self, scripts=None) -> None:
        self.scripts = scripts or [[StreamChunk(type="text", content="结论内容"),
                                    StreamChunk(type="done")]]
        self.calls: list[dict] = []

    def stream_chat(self, messages, thinking_effort, tools=None, system=None):
        idx = min(len(self.calls), len(self.scripts) - 1)
        self.calls.append(
            {"messages": list(messages), "tools": tools, "system": system}
        )
        yield from self.scripts[idx]


class EchoTool(Tool):
    name = "echo_tool"
    description = "回显"
    parameters = {"type": "object", "properties": {}}
    read_only = True

    def __init__(self, output: str = "工具输出") -> None:
        self._output = output

    def execute(self, args: dict) -> ToolResult:
        return ToolResult(ok=True, output=self._output)


def _config(model: str = "main-model") -> Config:
    return Config(
        protocol="deepseek",
        model=model,
        base_url="http://test",
        api_key="k",
        debug_log=False,
    )


# 测试用的已知工具名全集（含 Plan Mode 的两个特殊工具）。
_KNOWN_TOOLS = frozenset(
    {"echo_tool", "load_skill", "ask_user", "present_plan"}
)


def _text(content: str = "结论内容"):
    return [StreamChunk(type="text", content=content), StreamChunk(type="done")]


def _tool_call(name: str, call_id: str = "c1", args=None):
    return [
        StreamChunk(
            type="tool_call",
            tool_call=ToolCall(id=call_id, name=name, arguments=args or {}),
        ),
        StreamChunk(type="done"),
    ]


class IsolatedTestBase(unittest.TestCase):
    """临时工作区 + 一个含独立模式 Skill 的 SkillManager。"""

    SKILL_BODY = "审查步骤：先看 diff。"

    def setUp(self) -> None:
        self._old_cwd = os.getcwd()
        self._ws = tempfile.TemporaryDirectory()
        os.chdir(self._ws.name)
        self._home = tempfile.TemporaryDirectory()
        self.user_dir = Path(self._home.name)
        self.skills_dir = self.user_dir / "skills"
        self.skills_dir.mkdir(parents=True)
        # 已创建的 manager，_write_skill 会让它们重新扫盘——
        # 这样测试里「先建 manager 再写 Skill 文件」的自然写法不会踩空 catalog。
        self._managers: list[ConversationManager] = []

    def tearDown(self) -> None:
        os.chdir(self._old_cwd)
        self._ws.cleanup()
        self._home.cleanup()

    def _write_skill(self, name: str = "rev", **extra) -> None:
        lines = [f"name: {name}", "description: 审查改动", "context: fork"]
        lines.extend(f"{k}: {v}" for k, v in extra.items())
        (self.skills_dir / f"{name}.md").write_text(
            "---\n" + "\n".join(lines) + "\n---\n" + self.SKILL_BODY + "\n",
            encoding="utf-8",
        )
        # 让已创建的 manager 重新扫盘，这样测试里「先建 manager 再写 Skill 文件」
        # 的自然写法不会踩到一个空 catalog。
        for mgr in self._managers:
            mgr.skill_manager.startup()

    def _manager(self, provider=None, registry=None, tools=()) -> ConversationManager:
        provider = provider or ScriptedProvider()
        if registry is None:
            registry = ToolRegistry()
            for t in tools:
                registry.register(t)
        sm = SkillManager(
            project_root=None,
            user_dir=self.user_dir,
            builtin_dir=None,
            has_short_command=lambda _n: True,
        )
        sm.startup()
        mgr = ConversationManager(provider, _config(), registry, skill_manager=sm)
        mgr.approve_plan_callback = lambda _p: True
        # 默认关掉 c9 的自动记忆钩子：它在自然停止时起一个 daemon 线程，
        # 用**同一个**假 Provider 再发一次 tools=None 的请求。那条请求会混进
        # provider.calls，让「断言每轮请求都带工具」之类的用例随线程调度时快时慢
        # 地失败（单跑绿、全量跑红）。需要验证钩子的用例自己覆盖回去。
        mgr.memory_manager.on_natural_stop = lambda _history: None
        self._managers.append(mgr)
        return mgr


class ConclusionFlowTest(IsolatedTestBase):
    """T49c：结论提取与回流（AC18/AC19）。"""

    def test_exactly_two_paired_messages_in_main_history(self) -> None:
        """
        主历史新增**恰好两条配对消息**，子对话的工具过程一条都不进来（AC19）。
        """
        provider = ScriptedProvider(
            [_tool_call("echo_tool"), _text("最终审查结论")]
        )
        mgr = self._manager(provider, tools=[EchoTool("大段工具输出XYZ")])
        self._write_skill()

        events = list(mgr.run_skill("rev", "只看 auth", "/rev 只看 auth"))

        self.assertEqual(len(mgr.history), 2)
        self.assertEqual(mgr.history[0].role, "user")
        self.assertEqual(mgr.history[1].role, "assistant")
        self.assertEqual(mgr.history[1].content, "最终审查结论")
        # 工具调用与结果都不在主历史里。
        joined = " ".join(m.content or "" for m in mgr.history)
        self.assertNotIn("大段工具输出XYZ", joined)
        self.assertIsNone(mgr.history[0].tool_calls)
        self.assertIs(events[-1].stop_reason, StopReason.COMPLETED)

    def test_user_message_is_self_contained_with_display_content(self) -> None:
        """
        user 消息的 content 是自包含文本（名字+说明+参数），
        display_content 是用户敲的原始输入（AC24 双内容）。
        """
        mgr = self._manager()
        self._write_skill()
        list(mgr.run_skill("rev", "只看 auth", "/rev 只看 auth"))

        msg = mgr.history[0]
        self.assertIn("rev", msg.content)
        self.assertIn("审查改动", msg.content)
        self.assertIn("只看 auth", msg.content)
        self.assertEqual(msg.display_content, "/rev 只看 auth")

    def test_both_messages_recorded_to_session_archive(self) -> None:
        recorded: list[Message] = []
        mgr = self._manager()
        mgr.memory_manager.record_message = recorded.append
        self._write_skill()
        list(mgr.run_skill("rev", "", "/rev"))
        self.assertEqual([m.role for m in recorded], ["user", "assistant"])

    def test_no_result_path_yields_notice_and_keeps_pairing(self) -> None:
        """
        「未产出结果」分支：助手位是原因文案、配对结构仍成立、且产出一条 NOTICE。

        NOTICE 不可省：外层拦下了全部 FINISHED，而 TUI 对 COMPLETED 的收尾行
        不渲染任何东西；不补这条，用户会觉得界面完全没反应。
        （这里用「一直调工具直到打满迭代上限」触发该分支；
        取消路径由 test_cancel_event_is_rebuilt 从另一侧覆盖。）
        """
        provider = ScriptedProvider([_tool_call("echo_tool")])
        mgr = self._manager(provider, tools=[EchoTool()])
        self._write_skill()

        events = list(mgr.run_skill("rev", "", "/rev"))
        self.assertEqual(len(mgr.history), 2)
        self.assertEqual(mgr.history[0].role, "user")
        self.assertEqual(mgr.history[1].role, "assistant")
        self.assertIn("未产出", mgr.history[1].content)
        notices = [e for e in events if e.type == AgentEventType.NOTICE]
        self.assertTrue(notices)

    def test_max_iterations_reason(self) -> None:
        """子对话打满迭代上限 → 对应原因文案（AC33）。"""
        provider = ScriptedProvider([_tool_call("echo_tool")])
        mgr = self._manager(provider, tools=[EchoTool()])
        self._write_skill()
        list(mgr.run_skill("rev", "", "/rev"))
        self.assertIn("迭代上限", mgr.history[1].content)

    def test_stream_error_reason(self) -> None:
        """
        流错误（含上下文超限）→ 明确文案 + 主历史留可追溯记录。

        这是 F21 说明义务里那条兜底链路的终点。
        """
        provider = ScriptedProvider([[StreamChunk(type="error", content="超窗了")]])
        mgr = self._manager(provider)
        self._write_skill()
        events = list(mgr.run_skill("rev", "", "/rev"))
        self.assertIn("模型请求出错", mgr.history[1].content)
        self.assertTrue([e for e in events if e.type == AgentEventType.ERROR])

    def test_plan_rejected_does_not_return_preamble_as_conclusion(self) -> None:
        """
        计划被拒时不能把前言当结论回流（AC20）。

        子历史最后一条 assistant 带着「我打算这样做……」的非空正文，
        只按「找最后一条非空 assistant」会把它误当结论，用户会以为执行完了。
        """
        provider = ScriptedProvider(
            [
                [
                    StreamChunk(type="text", content="我打算这样做：先读文件"),
                    StreamChunk(
                        type="tool_call",
                        tool_call=ToolCall(
                            id="p1", name="present_plan", arguments={"plan": "步骤一"}
                        ),
                    ),
                    StreamChunk(type="done"),
                ]
            ]
        )
        mgr = self._manager(provider)
        mgr.approve_plan_callback = lambda _p: False  # 拒绝
        mgr.plan_mode = True
        self._write_skill()

        list(mgr.run_skill("rev", "", "/rev"))
        self.assertIn("计划未获批准", mgr.history[1].content)
        self.assertNotIn("我打算这样做", mgr.history[1].content)


class SubHistoryTest(IsolatedTestBase):
    """T49a：子历史构造（AC20/F20）。"""

    def test_sub_history_is_exactly_one_self_contained_message(self) -> None:
        """
        子对话固定只带那条自包含调用消息（对齐改造 F9）。

        标准里的 fork 不提供「从主历史带入尾部若干条」，C11 的 `history_messages`
        随之删除。那条消息是自包含的（含 Skill 名、说明与参数），模型据此就知道
        要做什么——这也正是「自包含调用文本」当初被设计成这样的原因。
        """
        return self._legacy_zero_history_check()

    def _legacy_zero_history_check(self) -> None:
        """
        `history_messages: 0` → 子历史恰为一条 user 消息，内容与主历史那条同源。

        末尾那条追加不可省：否则子对话完全没有用户轮，模型收到一个
        只有系统提示、没有任务陈述的请求。
        """
        provider = ScriptedProvider()
        mgr = self._manager(provider)
        self._write_skill(history_messages=0)
        mgr.history.append(Message(role="user", content="之前的对话"))
        mgr.history.append(Message(role="assistant", content="之前的回复"))

        list(mgr.run_skill("rev", "", "/rev"))

        sent = provider.calls[0]["messages"]
        # 最后一条是循环拼的 <system-reminder>，往前一条才是 user。
        user_msgs = [m for m in sent if m.role == "user"]
        self.assertEqual(len(user_msgs), 1)
        self.assertNotIn("之前的对话", user_msgs[0].content)
        # 与主历史那条同源。
        self.assertEqual(user_msgs[0].content, mgr.history[-2].content)

    def test_contains_this_skill_body_only(self) -> None:
        """子对话系统提示含本 Skill 正文，**不含**主对话其它已激活 Skill 的正文。"""
        provider = ScriptedProvider()
        mgr = self._manager(provider)
        self._write_skill()
        (self.skills_dir / "other.md").write_text(
            "---\nname: other\ndescription: 另一个\n---\n另一个Skill的正文OTHERBODY\n",
            encoding="utf-8",
        )
        mgr.skill_manager.startup()
        mgr.skill_manager.activate("other", "")

        list(mgr.run_skill("rev", "", "/rev"))

        reminder = provider.calls[0]["messages"][-1].content
        self.assertIn(self.SKILL_BODY, reminder)
        self.assertNotIn("OTHERBODY", reminder)

    def test_no_skill_index_in_sub_conversation(self) -> None:
        """子对话不给第一阶段清单——它不许再激活别的 Skill（F23）。"""
        provider = ScriptedProvider()
        mgr = self._manager(provider)
        self._write_skill()
        (self.skills_dir / "other.md").write_text(
            "---\nname: other\ndescription: 清单里才有的说明UNIQUEDESC\n---\n正文\n",
            encoding="utf-8",
        )
        mgr.skill_manager.startup()

        list(mgr.run_skill("rev", "", "/rev"))
        self.assertNotIn("UNIQUEDESC", provider.calls[0]["system"] or "")

    def test_plan_bridge_present_only_in_plan_mode(self) -> None:
        provider = ScriptedProvider()
        mgr = self._manager(provider)
        self._write_skill()
        mgr.plan_mode = True
        list(mgr.run_skill("rev", "", "/rev"))
        self.assertIn("计划模式", provider.calls[0]["messages"][-1].content)

        provider2 = ScriptedProvider()
        mgr2 = self._manager(provider2)
        list(mgr2.run_skill("rev", "", "/rev"))
        self.assertNotIn("计划模式", provider2.calls[0]["messages"][-1].content)


class SubAgentDrivingTest(IsolatedTestBase):
    """T49b：子 Agent 的参数与隔离（AC21/AC23）。"""

    def test_load_skill_excluded_from_sub_conversation_tools(self) -> None:
        """子对话工具集不含 load_skill（AC23，禁止嵌套激活）。"""
        from rhinecode.tools.load_skill import LoadSkillTool

        provider = ScriptedProvider()
        registry = ToolRegistry()
        registry.register(EchoTool())
        mgr = self._manager(provider, registry=registry)
        registry.register(LoadSkillTool(mgr.skill_manager))
        self._write_skill()

        list(mgr.run_skill("rev", "", "/rev"))
        names = {t["function"]["name"] for t in (provider.calls[0]["tools"] or [])}
        self.assertNotIn("load_skill", names)
        self.assertIn("echo_tool", names)

    def test_recorder_not_called_for_sub_conversation_turns(self) -> None:
        """子对话过程不写会话存档——只有主历史那两条会被记录。"""
        recorded: list[Message] = []
        provider = ScriptedProvider([_tool_call("echo_tool"), _text()])
        mgr = self._manager(provider, tools=[EchoTool()])
        mgr.memory_manager.record_message = recorded.append
        self._write_skill()

        list(mgr.run_skill("rev", "", "/rev"))
        # 若子对话也记录，这里会多出 assistant(tool_calls) 与 tool 两条。
        self.assertEqual(len(recorded), 2)

    def test_cancel_event_is_rebuilt(self) -> None:
        """
        先 request_cancel 再跑独立 Skill → 子对话**不**开局即取消。

        `request_cancel()` 置的就是 `self._cancel_event`，而它原本只在 `_run()`
        里重建；不在子对话入口重建，上次运行残留的置位会让它一开局就被杀。
        """
        provider = ScriptedProvider()
        mgr = self._manager(provider)
        self._write_skill()
        mgr.request_cancel()

        list(mgr.run_skill("rev", "", "/rev"))
        self.assertEqual(len(provider.calls), 1, "子对话开局即被取消了")
        self.assertEqual(mgr.history[1].content, "结论内容")

    def test_custom_model_uses_a_different_provider(self) -> None:
        """spec.model 非空 → 用另一个 Provider 实例（F22）。"""
        main_provider = ScriptedProvider()
        mgr = self._manager(main_provider)
        self._write_skill(model="other-model")

        created = {}
        import rhinecode.conversation as conv

        original = conv.create_provider

        def fake_create(config):
            created["model"] = config.model
            return ScriptedProvider()

        conv.create_provider = fake_create
        try:
            list(mgr.run_skill("rev", "", "/rev"))
        finally:
            conv.create_provider = original

        self.assertEqual(created["model"], "other-model")
        # 主 Provider 一次都没被调用。
        self.assertEqual(main_provider.calls, [])

    def test_provider_cache_reuses_instance(self) -> None:
        mgr = self._manager()
        import rhinecode.conversation as conv

        original = conv.create_provider
        conv.create_provider = lambda cfg: ScriptedProvider()
        try:
            a = mgr._provider_for("m1")
            b = mgr._provider_for("m1")
            c = mgr._provider_for("m2")
        finally:
            conv.create_provider = original
        self.assertIs(a, b)
        self.assertIsNot(a, c)

    def test_natural_stop_triggers_memory_hook(self) -> None:
        """事件流经 _wrap_events → 自然完成时触发 c9 记忆钩子（F21）。"""
        hits = []
        mgr = self._manager()
        mgr.memory_manager.on_natural_stop = lambda h: hits.append(len(h))
        self._write_skill()
        list(mgr.run_skill("rev", "", "/rev"))
        self.assertEqual(len(hits), 1)


class ContextLayerTest(IsolatedTestBase):
    """AC21：子对话跑 C8 第一层、不跑第二层。"""

    def test_first_layer_offload_applies_second_layer_does_not(self) -> None:
        """
        大工具结果在子历史中被替换为「预览 + 路径」占位，且**未**调用摘要 LLM。

        这是「子对话共享 ContextManager 但只开第一层」这个决策的唯一收益点，
        不验就不知道有没有真的接上。
        """
        big = "X" * 40000  # 远超第一层单结果阈值
        provider = ScriptedProvider([_tool_call("echo_tool"), _text()])
        mgr = self._manager(provider, tools=[EchoTool(big)])
        self._write_skill()

        list(mgr.run_skill("rev", "", "/rev"))

        # 第二轮发出的消息里，那条 tool 结果应已被替换成占位。
        second_round = provider.calls[1]["messages"]
        tool_msgs = [m for m in second_round if m.role == "tool"]
        self.assertTrue(tool_msgs)
        self.assertLess(len(tool_msgs[0].content), 4000, "第一层存盘没生效")
        self.assertIn(".rhinecode", tool_msgs[0].content)
        # 存盘文件确实生成了。
        offload_dir = Path(".rhinecode") / "context"
        self.assertTrue(list(offload_dir.glob("*.txt")))

        # 第二层未被调用：所有请求都带着 tools（摘要请求会强制 tools=None）。
        for call in provider.calls:
            self.assertIsNotNone(call["tools"], "出现了摘要请求（第二层被调用了）")


if __name__ == "__main__":
    unittest.main()
