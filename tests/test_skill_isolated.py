"""
独立模式子对话单测（c11 T51）。

覆盖 spec AC18–AC23（子对话隔离、结论回流、配对结构、工具集排除 load_skill）
与 AC33（子对话独立迭代预算）。按 T49a/b/c 三段分组，便于定位失败。

另有 `HookDispatchTest`（审查报告 B3）：钉住「fork 子对话里工具级三个 Hook
事件照常分发」。它不属于 c11 的验收范围——那是 c12 的一条安全承诺在这条
路径上的落点，而这里是唯一能构造出那条路径的地方。

用假 Provider 与真实 SkillManager；不触任何网络。
"""

import os
import tempfile
import unittest
from pathlib import Path

from rhinecode.config import Config
from rhinecode.conversation import ConversationManager
from rhinecode.hooks import NullHookManager
from rhinecode.hooks.models import (
    NO_VERDICT,
    DispatchResult,
    HookDecision,
    HookEventType,
    HookVerdict,
)
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


# 工具级三事件。它们**全部由 `Agent` 内部分发**，因此是「fork 子对话有没有
# 拿到 hooks」这件事唯一可观测的证据（回合级事件由协调层分发，不经 Agent）。
_TOOL_EVENTS = frozenset(
    {
        HookEventType.PRE_TOOL_USE,
        HookEventType.POST_TOOL_USE,
        HookEventType.POST_TOOL_USE_FAILURE,
    }
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

    def _manager(
        self, provider=None, registry=None, tools=(), hook_manager=None
    ) -> ConversationManager:
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
        # `hook_manager` 缺省仍是 None（协调层用 `NullHookManager()` 兜底），
        # 只有 HookDispatchTest 会传一个记录型的假实现进来。
        mgr = ConversationManager(
            provider,
            _config(),
            registry,
            skill_manager=sm,
            hook_manager=hook_manager,
        )
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
        大工具结果在子历史中被替换为「预览 + 来源」占位，且**未**调用摘要 LLM。

        这是「子对话共享 ContextManager 但只开第一层」这个决策的唯一收益点，
        不验就不知道有没有真的接上。

        ⚠ 这条断言原先是 `assertIn(".rhinecode", ...)`——占位里当时确实写着存盘
        文件的路径，而那正是 2026-09-17 修掉的死循环的诱因（见
        `context/offload.py` 模块 docstring）。现在改成断言「**有**来源、
        **没有**路径」，两句缺一不可：只断言有来源的话，把路径原样加回去照样绿。
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
        self.assertIn("已存盘", tool_msgs[0].content, "占位标记不见了")
        self.assertIn("来源：echo_tool", tool_msgs[0].content, "占位没写清这条结果是谁产生的")
        self.assertNotIn(
            ".rhinecode", tool_msgs[0].content, "占位里又出现了存盘路径（死循环的诱因）"
        )
        # 存盘文件确实生成了（人工排查仍取得到，只是不再告诉模型路径）。
        offload_dir = Path(".rhinecode") / "context"
        self.assertTrue(list(offload_dir.glob("*.txt")))

        # 第二层未被调用：所有请求都带着 tools（摘要请求会强制 tools=None）。
        for call in provider.calls:
            self.assertIsNotNone(call["tools"], "出现了摘要请求（第二层被调用了）")


class _RecordingHooks(NullHookManager):
    """
    只对三个**工具级**事件表态的假 Hook 编排者，用来观测「分发到底有没有发生」。

    继承 `NullHookManager` 而不是从零手写一个类，理由有两条：
    ① 协调层与上下文管理器还会调 `bind_context` / `consume_injections` /
       `warnings` 等方法，逐个补全等于把接口抄一遍，将来 `HookManager`
       加方法时这里会 `AttributeError`；
    ② 空对象的语义正是「什么都不做」，本类只覆盖需要观测的那两个方法，
       读的人一眼就知道其余行为与真实的「没配 Hook」逐字一致。

    :param verdict: `pre_tool_use` 要返回的结论。缺省 `NO_VERDICT`（不表态），
                    传 DENY 时表示「用户写了一条拦截规则」。
    """

    def __init__(self, verdict: HookVerdict = NO_VERDICT) -> None:
        self._verdict = verdict
        # 记录 (事件, 工具名)，测试据此断言「哪个事件在子对话里真的分发过」。
        self.seen: list[tuple[HookEventType, str]] = []

    def has_listeners(self, event) -> bool:
        # 只认工具级三事件：回合级 / 会话级事件在子对话里走的是另一条通道
        # （协调层直接分发，不经 Agent），混进来会让断言分不清是哪一半在起作用。
        return event in _TOOL_EVENTS

    def dispatch(self, event, payload_factory=None, cwd=None):
        # 负载工厂必须真的调用一次——`Agent._tool_fields` 里出的错（比如将来给
        # 工具级负载加字段时写错取值）只有在这里被调用时才暴露得出来。
        fields = payload_factory() if payload_factory else {}
        self.seen.append((event, fields.get("tool", "")))
        if event is HookEventType.PRE_TOOL_USE:
            return DispatchResult(verdict=self._verdict, matched=1, executed=1)
        return DispatchResult(matched=1, executed=1)

    def events_for(self, tool_name: str) -> list:
        """取某个工具上分发过的事件列表（顺序保留）。"""
        return [ev for ev, name in self.seen if name == tool_name]


class _FakeCommandTool(Tool):
    """
    冒充 `run_command` 的假工具：非只读、命令类，因此走串行桶、过完整权限管线。

    刻意用 `run_command` 这个名字而不是自造一个：`permission/adapter.py` 按
    工具名映射 `kind`，换个名字会落进 `other` 分支，判定路径与真实场景不同。

    :param ok: 执行结果的成败。为 False 时用来触发 `post_tool_use_failure`。
    """

    name = "run_command"
    description = "假的命令执行器"
    parameters = {"type": "object", "properties": {"command": {"type": "string"}}}
    read_only = False

    def __init__(self, ok: bool = True) -> None:
        self._ok = ok
        self.executed = 0

    def execute(self, args: dict) -> ToolResult:
        self.executed += 1
        if self._ok:
            return ToolResult(ok=True, output="done", summary="ok")
        return ToolResult(ok=False, output="boom", summary="命令失败")


class HookDispatchTest(IsolatedTestBase):
    """
    `context: fork` 的 Skill 子对话里，**工具级三个 Hook 事件照常分发**。

    ## 违反会发生什么（这是安全判据，不是功能判据）

    `pre_tool_use` / `post_tool_use` / `post_tool_use_failure` 三个事件
    **全部由 `Agent` 内部分发**，而 `Agent.__init__` 的 `hooks` 缺省是
    `NullHookManager()`。`conversation.py` 的 `_run_forked_skill` 一旦漏传
    `hooks=self._hooks`（**它真的漏过，见审查报告 B3**），这条子对话里那三个
    事件一次都不分发，且**不报错、不告警、其余测试全绿**。

    用户看到的形态是：他写了一条
    `pre_tool_use` + `command contains "git push"` → `deny` 的规则，
    在主对话里试过、确实拦住了；之后模型加载一个 `context: fork` 的 Skill
    （或用户自己敲那个 Skill 的短命令），正文里那句 `git push` **直接跑掉**，
    而 `/hooks` 报告显示这条规则「触发 0 次」。他会去改 `if:` 条件，
    而根因在一个跟条件毫无关系的地方。

    这与 C13「Hook 对子 Agent 全量生效」是**同一条安全承诺的两个落点**：
    委派那条路由 `tests/test_subagent_integration.py::HookIntegrationTest`
    钉着，Skill 这条路此前没有任何东西钉。而 Skill 更容易走到——
    一次 `load_skill` 就够，不需要写角色定义文件。

    ## 为什么这条不能简化

    ① **不能只断言 `pre_tool_use`。** 三个事件走的是 `Agent` 里**两个不同的**
       方法（`_dispatch_pre_tool` / `_dispatch_post_tool`）。只验前置的话，
       一个「前置接上了、后置没接」的实现照样全绿，而用户拿 `post_tool_use`
       做的审计日志会在 Skill 子对话里静默漏记。
    ② **不能只断言「事件被分发过」，必须同时断言 DENY 真的拦住了。**
       只看 `seen` 的话，一个「分发了但把结论丢掉」的实现（求值完不用
       `hook_verdict`）照样全绿，而那正是安全边界失效的形态。
    ③ **不能拿只读工具代替。** 只读工具走并发桶那个 `_dispatch_post_tool`
       调用点，非只读走串行桶那个；用非只读的命令类工具才与「用户想拦
       `git push`」这个真实场景同形。
    ④ **假 Hook 必须真的调一次 `payload_factory`。** 不调的话，负载构造里的
       错误（`_tool_fields` 将来加字段写错取值）在这里永远暴露不出来。
    """

    def _run_with(self, hooks: "_RecordingHooks", tool: "_FakeCommandTool"):
        """
        跑一次「Skill 子对话里调一次命令工具」的完整流程。

        执行流程：第一轮模型发起 `run_command`，第二轮收口成一段文本结论。

        :returns: 那个假 Provider，便于需要时检查逐轮请求

        副作用：`hooks.seen` 被填上本次分发过的事件。
        """
        provider = ScriptedProvider(
            [
                _tool_call("run_command", args={"command": "git push origin main"}),
                _text("我停手了。"),
            ]
        )
        mgr = self._manager(provider, tools=[tool], hook_manager=hooks)
        self._write_skill()
        list(mgr.run_skill("rev", "", "/rev"))
        return provider

    def test_pre_tool_use_deny_blocks_the_command_inside_a_forked_skill(self) -> None:
        """
        一条 `pre_tool_use` 的 deny 规则在 fork 子对话里**同样拦得住**。

        两个断言缺一不可：事件确实分发过（否则规则「触发 0 次」），
        且工具**一次都没执行**（否则规则形同虚设）。
        """
        hooks = _RecordingHooks(
            HookVerdict(HookDecision.DENY, "禁止 git push", "no-push")
        )
        tool = _FakeCommandTool()

        self._run_with(hooks, tool)

        self.assertIn(
            HookEventType.PRE_TOOL_USE,
            hooks.events_for("run_command"),
            "fork 子对话里的工具调用必须触发 pre_tool_use"
            "——不触发的话用户写的拦截规则在这条路径上等于不存在",
        )
        self.assertEqual(tool.executed, 0, "被 Hook 拦下的命令绝不能真的执行")

    def test_post_tool_use_fires_after_a_successful_call(self) -> None:
        """
        不表态时工具照常执行，且**执行之后**分发 `post_tool_use`。

        这条同时反证了上一条：DENY 那次「没执行」确实是 Hook 拦的，
        不是这条链路本来就跑不通。
        """
        hooks = _RecordingHooks()
        tool = _FakeCommandTool(ok=True)

        self._run_with(hooks, tool)

        events = hooks.events_for("run_command")
        self.assertEqual(tool.executed, 1, "不表态时命令应照常执行")
        self.assertIn(HookEventType.PRE_TOOL_USE, events)
        self.assertIn(HookEventType.POST_TOOL_USE, events)
        self.assertNotIn(HookEventType.POST_TOOL_USE_FAILURE, events)

    def test_post_tool_use_failure_fires_when_the_call_fails(self) -> None:
        """
        工具返回 `ok=False` 时分发的是 `post_tool_use_failure` 而不是 `post_tool_use`。

        两个事件的分工是「跑了且成功」与「跑了但失败」，混用会让
        「统计工具失败率」这类 Hook 用途在 Skill 子对话里直接失真。
        """
        hooks = _RecordingHooks()
        tool = _FakeCommandTool(ok=False)

        self._run_with(hooks, tool)

        events = hooks.events_for("run_command")
        self.assertEqual(tool.executed, 1)
        self.assertIn(HookEventType.POST_TOOL_USE_FAILURE, events)
        self.assertNotIn(HookEventType.POST_TOOL_USE, events)


class _FakeDelegateTool(Tool):
    """
    假的委派工具：名字、`system_serial`、`read_only` 三项与真 `RunAgentTool` 一致。

    ⚠ **`system_serial = True` 不是抄参数抄顺手了，它是判据的一部分。**
    循环里系统级工具走的是另一条预扫分支（判 ASK 按 ALLOW 处理、不弹面板），
    若「本轮没发给你」那道兜底判定排在它**之后**，一次硬造的委派就会照常执行。
    用一个 `system_serial=False` 的假工具来验，等于把这条最可能出问题的路
    绕开了——那种护栏挡不住它本该挡的东西。

    真 `RunAgentTool` 要一整套子 Agent 服务才造得出来，而这里要验的是
    **循环层的过滤**，与它执行什么无关，故用假的。
    """

    name = "run_agent"
    description = "委派"
    parameters = {
        "type": "object",
        "properties": {"agent": {"type": "string"}, "task": {"type": "string"}},
    }
    read_only = False
    system_serial = True
    plan_safe = True

    def __init__(self) -> None:
        self.executed = 0

    def execute(self, args: dict, **kwargs) -> ToolResult:
        self.executed += 1
        return ToolResult(ok=True, output="已委派", summary="ok")


class ForkDelegationTest(IsolatedTestBase):
    """
    **`context: fork` 的 Skill 不能派活给子 Agent**（2026-09-01 定的语义）。

    ## 钉的是语义，不是某一行代码

    定的产品语义是「已经是一层子对话的东西，不许再往下开一层」。子 Agent
    那一侧从 C13 起就是这么做的（`GLOBAL_DENIED_TOOLS` 同时挡 `run_agent`
    与 `load_skill`，后者的注释逐字写着「一个 `context: fork` 的 Skill 会再开
    一层子对话」）；fork 这一侧却只挡了 `load_skill`，于是同一条不变量在两条
    路上说法不一样，而**界面上完全看不出来**。

    ## 为什么必须是两条断言，缺一条防线就不成立

    ① **排除只是「不把 schema 发给模型」。** 模型仍会凭训练先验硬造出调用
       ——C11 场景 10 实测撞到过：那一轮 `tool_names` 里没有 `run_command`，
       DeepSeek 照样调了出来，参数名还全对。
    ② **兜底判定只是「调了就拒」。** 少了排除，模型每一轮都看得见这个工具，
       于是会反复去调、反复被拒，把子对话那 15 轮预算烧在互相拉扯上。

    只验其中一条的话，另一条被删掉时这个文件照样全绿。

    ## ⚠ 另一半语义：闸门**刻意没有**补上

    别看到「fork 子对话拿不到子 Agent 结论」就去给 `RunOptions` 补
    `subagent_gate`——那两种修法只能落地一种，理由写在
    `skills/manager.py` 的 `fork_excluded_tools()` docstring 里（要点：
    `TaskManager.take_deliverables()` 不按发起方分桶，把主对话那个闸门交给
    fork 会让主对话委派出去的结论被注入到一份**用完即弃**的历史里并标成
    「已交付」，那是**丢结果**）。`NoGateOnTheForkPathTest` 从另一侧钉住这条。
    """

    def _run_fork_with_delegate(self, provider: ScriptedProvider) -> _FakeDelegateTool:
        """
        跑一条 fork 子对话，注册中心里**真的有**一个叫 `run_agent` 的工具。

        「真的注册了」是必要条件：没注册的话调用会落进「未知工具」分支，
        那条路无论排不排除都会拒绝，护栏就验不到本条语义了。

        :returns: 那个假委派工具，供调用方检查它执行过几次
        """
        tool = _FakeDelegateTool()
        mgr = self._manager(provider, tools=[EchoTool(), tool])
        self._write_skill()
        list(mgr.run_skill("rev", "", "/rev"))
        return tool

    def test_run_agent_schema_is_not_sent_to_the_fork_sub_conversation(self) -> None:
        """
        第一道：fork 子对话每一轮的工具集里都没有 `run_agent`。

        `echo_tool` 的在场是对照组——少了它，一个「工具集整个是空的」的
        实现（比如排除集算错成全集）也会让上面那条断言通过。
        """
        provider = ScriptedProvider([_text("审查完了")])
        self._run_fork_with_delegate(provider)

        self.assertTrue(provider.calls, "子对话至少要发起一次请求")
        for i, call in enumerate(provider.calls):
            names = {t["function"]["name"] for t in (call["tools"] or [])}
            self.assertNotIn(
                "run_agent",
                names,
                f"第 {i + 1} 轮把委派工具发给了 fork 子对话——"
                "分身 Skill 本身就是一层子对话，不该再往下开一层",
            )
            self.assertIn("echo_tool", names, "对照组：普通工具必须照常可见")

    def test_a_fabricated_delegation_is_refused_and_never_executes(self) -> None:
        """
        第二道：模型硬造一次 `run_agent` 调用，工具**一次都不执行**。

        两个断言分工不同：`executed == 0` 说明没跑，回灌文案里那句
        「本轮未提供给你」说明**是被这条判定拒的**——少了后者，一个
        「工具压根没注册」的假绿（落进未知工具分支）看起来一模一样。
        """
        provider = ScriptedProvider(
            [
                _tool_call("run_agent", args={"agent": "explorer", "task": "查一下"}),
                _text("我自己查完了。"),
            ]
        )
        tool = self._run_fork_with_delegate(provider)

        self.assertEqual(
            tool.executed,
            0,
            "fork 子对话里硬造出来的委派**执行了**——"
            "排除只是不发 schema，拦住它的是循环层那道兜底判定，两者缺一防线不成立",
        )
        # 回灌给模型的那条结果必须说清「为什么没执行」，否则它会换个工具名再试。
        second_round = provider.calls[1]["messages"]
        feedback = " ".join(m.content or "" for m in second_round)
        self.assertIn("本轮未提供给你", feedback)

    def test_the_two_tables_agree_on_which_tools_open_a_layer(self) -> None:
        """
        **成对维护点的护栏**：两张表对「哪些工具会再开一层子对话」说法一致。

        `subagents/toolset.py` 的 `GLOBAL_DENIED_TOOLS`（子 Agent 那一侧）与
        `skills/manager.py` 的 `fork_excluded_tools()`（fork 那一侧）是同一条
        不变量的两个落点。**只改一处不报错**——另一条路上那个工具照常可见、
        照常能调，两条路的行为从此不一样，而配置和界面上都看不出异常。
        这正是 `run_agent` 从 C13 一直漏到 2026-09-01 的形态。

        ⚠ 顺带钉住**表长**：把某一项从 `_LAYER_OPENING_TOOLS` 里删掉是这类
        遍历式护栏共同的失效方式——遍历一张空表永远绿。
        """
        from rhinecode.subagents.toolset import GLOBAL_DENIED_TOOLS

        # 「会再开一层子对话」的工具。委派开的是子 Agent，加载开的是
        # 另一条 `context: fork` 的子对话——两者都是「一层」。
        layer_opening = {"run_agent", "load_skill"}
        self.assertEqual(
            len(layer_opening), 2, "表长变了就说明这条不变量的覆盖面变了，请一并复核"
        )

        mgr = self._manager()
        fork_excluded = mgr.skill_manager.fork_excluded_tools()
        for name in sorted(layer_opening):
            self.assertIn(
                name,
                GLOBAL_DENIED_TOOLS,
                f"子 Agent 那一侧漏了 {name}——一条 `tools: [{name}]` 的角色定义就能往下开一层",
            )
            self.assertIn(
                name,
                fork_excluded,
                f"fork 那一侧漏了 {name}——两张表说法不一样，而界面上看不出来",
            )


class NoGateOnTheForkPathTest(IsolatedTestBase):
    """
    fork 子对话**刻意不接**子 Agent 闸门——钉住「别两个都做」。

    `ForkDelegationTest` 定的语义是「fork 不能派活」，那么再给
    `RunOptions` 补一个 `subagent_gate` 就等于留下一段**永远走不到**的代码：
    没有任何工具能在这条路上创建出子 Agent，闸门取到的只会是主对话委派的
    那些任务——而取走它们正是本轮拒绝的那个丢结果的形态。

    ⚠ 这条护栏的失败方式很特别：它**不测行为，测的是接线**。之所以值得单列，
    是因为「顺手把闸门也接上，反正多接一根线不会错」看起来永远像个改进。
    """

    def test_run_options_carry_no_subagent_gate_on_this_path(self) -> None:
        """
        fork 侧那次 `agent.run(...)` 既不去**取**闸门，也不把它放进 `RunOptions`。

        ## ⚠ 只断言 `options.subagent_gate is None` 是不够的（变异实测证实）

        `ConversationManager.subagent_gate()` 在两个服务都没启用时返回 `None`，
        而单测里的管理器正是这种。于是「真的把 `subagent_gate=self.subagent_gate()`
        加到 fork 路径上」这个变异**照样全绿**——护栏挡不住它本该挡的那一行。

        所以这里把工厂替换成一个返回哨兵的桩：**取没取过**才是可判定的信号。
        两条断言分工不同——工厂没被调用说明接线上压根没这一步，
        `RunOptions` 里为空说明就算将来换个取法也传不进去。
        """
        captured: list = []
        asked: list = []

        class _SentinelGate:
            """
            行为等价于 `NullGate` 的哨兵闸门。

            ⚠ **必须实现完整协议、不能用裸 `object()`**：接线一旦真的被改回去，
            循环会立刻调 `take_pending()`，裸对象会抛 `AttributeError` 把整条
            子对话炸掉——测试确实会红，但红在一个与判据无关的地方，
            下一个人看到的是一条堆栈而不是「你不该接这个闸门」。
            """

            def take_pending(self):
                return []

            def has_awaited(self) -> bool:
                return False

            def wait_any(self, cancel_event, timeout: float = 0.0) -> bool:
                return False

            def describe_awaited(self) -> str:
                return ""

        sentinel = _SentinelGate()

        provider = ScriptedProvider([_text("结论")])
        mgr = self._manager(provider)
        self._write_skill()

        # 桩：真实环境里两个服务都启用时这里会返回一个真闸门。
        # 单测的管理器没启用服务、原方法恒返回 None，那会让本条护栏形同虚设。
        def fake_gate():
            asked.append(True)
            return sentinel

        mgr.subagent_gate = fake_gate

        from rhinecode.agent.loop import Agent

        original = Agent.run

        def spy(self, *args, **kwargs):
            captured.append(kwargs.get("options"))
            return original(self, *args, **kwargs)

        Agent.run = spy
        try:
            list(mgr.run_skill("rev", "", "/rev"))
        finally:
            Agent.run = original

        self.assertTrue(captured, "fork 路径必须真的跑过一次 Agent.run")
        self.assertEqual(
            asked,
            [],
            "fork 路径去取了子 Agent 闸门——两种语义只能落地一种，"
            "既排除了委派工具又接闸门的话，那个闸门永远走不到，"
            "而它取走的会是主对话委派出去的结论（丢结果）",
        )
        options = captured[0]
        self.assertIsNotNone(options, "fork 路径的 RunOptions 不该是 None")
        self.assertIsNot(
            getattr(options, "subagent_gate", None),
            sentinel,
            "闸门被放进了 fork 路径的 RunOptions",
        )


if __name__ == "__main__":
    unittest.main()
