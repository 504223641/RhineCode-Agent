"""
子 Agent 运行器的单测（c13 T16，覆盖 AC7a/AC7b/AC9b/AC10/AC13a/AC14b/AC15/AC22b/AC25c）。

## 三条反证是本模块的重点

plan 里点名的三处「漏了不报错、界面上看不出来」的坑，各有一条反证：

1. **主引擎档位跑完后没变**——只断言「子 Agent 用对了档位」证明不了主引擎没被改；
2. **回合级预授权没被继承**——它绑在某一次执行上，不该跟着委派跑出去；
3. **Hook 注入队列跑完后仍非空**——那是个会被取走的队列，子 Agent 消费它
   会让主对话的注入型 Hook 凭空消失。

## 为什么用假 Provider 而不是驱动设施

运行器的判据全是「它给 Agent Loop 传了什么、从子历史里取出了什么」，
不涉及界面。假 Provider 能精确控制「模型说了什么、调了什么工具」，
而真实模型每次跑的都不一样。
"""

from __future__ import annotations

import threading
import unittest
from pathlib import Path

from rhinecode.agent.events import StopReason
from rhinecode.permission.engine import PermissionEngine
from rhinecode.permission.models import PermissionMode, Rule
from rhinecode.permission.rules import RuleSet
from rhinecode.provider.base import BaseProvider, Message, StreamChunk, ToolCall
from rhinecode.subagents.models import AgentSource, AgentSpec
from rhinecode.subagents.runner import (
    ParentSnapshot,
    SubAgentRuntime,
    run_subagent,
)
from rhinecode.subagents.tasks import TaskManager, TaskStatus
from rhinecode.subagents.toolset import resolve_toolset
from rhinecode.tools.base import Tool, ToolResult
from rhinecode.tools.registry import ToolRegistry


# --------------------------------------------------------------------------- #
# 测试替身
# --------------------------------------------------------------------------- #


class _Reader(Tool):
    name = "read_file"
    description = "fake"
    parameters = {"type": "object", "properties": {"path": {"type": "string"}}}
    read_only = True

    def execute(self, args: dict) -> ToolResult:
        return ToolResult(ok=True, output="文件内容", summary="read")


class _Writer(Tool):
    """非只读 → 默认档下落进灰色地带、判 ASK。"""

    name = "write_file"
    description = "fake"
    parameters = {"type": "object", "properties": {"path": {"type": "string"}}}
    read_only = False

    def __init__(self) -> None:
        self.executed = 0

    def execute(self, args: dict) -> ToolResult:
        self.executed += 1
        return ToolResult(ok=True, output="written", summary="written")


class _Fetcher(Tool):
    name = "web_fetch"
    description = "fake"
    parameters = {"type": "object", "properties": {"url": {"type": "string"}}}
    read_only = True

    def execute(self, args: dict) -> ToolResult:
        return ToolResult(ok=True, output="页面", summary="fetched")


class _SayProvider(BaseProvider):
    """按脚本逐轮产出：字符串 = 说一句话；ToolCall = 调一次工具。"""

    def __init__(self, script) -> None:
        self.script = list(script)
        self.calls = 0
        self.stables: list[str] = []
        self.messages_per_turn: list[str] = []
        self.tool_names_per_turn: list[list[str]] = []

    def stream_chat(self, messages, thinking_effort="off", tools=None, system=None):
        self.stables.append(system or "")
        # dynamic 段（环境信息、不可信内容约束）进的是 <system-reminder>，
        # 混在 messages 里而不是 system 参数里——验它必须看这份原文。
        self.messages_per_turn.append(
            "\n".join(str(getattr(m, "content", "") or "") for m in messages)
        )
        self.tool_names_per_turn.append(
            sorted(t["function"]["name"] for t in (tools or []))
        )
        step = self.script[self.calls] if self.calls < len(self.script) else "收尾结论"
        self.calls += 1
        if isinstance(step, ToolCall):
            yield StreamChunk(type="tool_call", tool_call=step)
        else:
            yield StreamChunk(type="text", content=step)
        yield StreamChunk(type="done")


class _ExplodingProvider(BaseProvider):
    def stream_chat(self, messages, thinking_effort="off", tools=None, system=None):
        raise RuntimeError("模型炸了")
        yield  # pragma: no cover —— 让它是生成器


class _HookSpy:
    """
    假 Hook 管理器：`consume_injections` 有一次性内容，被取走就空了。

    子 Agent **绝不能**碰它——那会让主对话的注入型 Hook 凭空消失。
    """

    def __init__(self) -> None:
        self.injections = "来自 turn_start 的注入内容"

    def consume_injections(self) -> str:
        text, self.injections = self.injections, ""
        return text

    def has_listeners(self, event) -> bool:
        return False

    def dispatch(self, event, build=None):  # pragma: no cover —— 无监听者不会走到
        raise AssertionError("不该被调用")


class _EngineSpy:
    """
    包住真引擎，记下每次 `derive` 出来的实例，让测试能看到子 Agent 实际用了什么。
    """

    def __init__(self, engine: PermissionEngine) -> None:
        self._engine = engine
        self.derived: list[PermissionEngine] = []

    def derive(self, mode):
        sub = self._engine.derive(mode)
        self.derived.append(sub)
        return sub

    def __getattr__(self, item):
        return getattr(self._engine, item)


# --------------------------------------------------------------------------- #
# 夹具
# --------------------------------------------------------------------------- #


def _spec(**kw) -> AgentSpec:
    base = dict(
        name="explorer",
        description="x",
        body="你是一个只读调研员。",
        source=AgentSource.BUILTIN,
        path=Path("explorer.md"),
    )
    base.update(kw)
    return AgentSpec(**base)


class RunnerBase(unittest.TestCase):
    def setUp(self) -> None:
        self.registry = ToolRegistry()
        self.writer = _Writer()
        for tool in (_Reader(), self.writer, _Fetcher()):
            self.registry.register(tool)
        self.all_names = frozenset(self.registry.names())
        self.engine = PermissionEngine(RuleSet([]), mode=PermissionMode.DEFAULT)
        self.tasks = TaskManager()
        self.hooks = _HookSpy()

    def _runtime(self, provider, engine=None, mode=None, untrusted="") -> SubAgentRuntime:
        return SubAgentRuntime(
            provider_for=lambda name: provider,
            registry=self.registry,
            engine=engine if engine is not None else self.engine,
            main_mode=lambda: mode if mode is not None else self.engine.mode,
            environment_text=lambda: "工作目录：/tmp",
            default_model="m",
            hooks=self.hooks,
            untrusted_section=untrusted,
        )

    def _run(self, provider, spec=_spec(), task="去查点东西", parent=None, **kw):
        runtime = kw.pop("runtime", None) or self._runtime(provider, **kw)
        toolset = resolve_toolset(self.all_names, spec)
        record = self.tasks.create(
            "role" if spec else "branch", spec.name if spec else "(branch)", task
        )
        run_subagent(
            runtime, spec, task, record, self.tasks, toolset, self.all_names, parent
        )
        return record


# --------------------------------------------------------------------------- #
# 正常路径
# --------------------------------------------------------------------------- #


class HappyPathTest(RunnerBase):
    def test_conclusion_is_last_assistant_text(self) -> None:
        record = self._run(_SayProvider(["我找到了三处调用。"]))

        self.assertIs(record.status, TaskStatus.COMPLETED)
        self.assertEqual(record.conclusion, "我找到了三处调用。")
        self.assertEqual(record.stop_reason, StopReason.COMPLETED.value)
        self.assertTrue(record.done_event.is_set())

    def test_tool_use_then_conclusion(self) -> None:
        provider = _SayProvider(
            [ToolCall(id="c1", name="read_file", arguments={"path": "a.py"}), "读完了，结论如下。"]
        )
        record = self._run(provider)

        self.assertIs(record.status, TaskStatus.COMPLETED)
        self.assertEqual(record.conclusion, "读完了，结论如下。")
        self.assertGreaterEqual(provider.calls, 2)

    def test_turns_and_usage_recorded(self) -> None:
        provider = _SayProvider(
            [ToolCall(id="c1", name="read_file", arguments={"path": "a"}), "好了"]
        )
        record = self._run(provider)
        self.assertGreaterEqual(record.turns, 1)


class SystemPromptTest(RunnerBase):
    """AC7a / AC7b：系统提示只给角色正文，不含主对话八模块。"""

    def test_stable_is_exactly_the_role_body(self) -> None:
        provider = _SayProvider(["done"])
        self._run(provider, spec=_spec(body="你是一个只读调研员。"))

        self.assertEqual(provider.stables[0], "你是一个只读调研员。")

    def test_environment_info_is_injected(self) -> None:
        """环境信息段确实进了请求（它是子 Agent 唯一的「我在哪」来源）。"""
        provider = _SayProvider(["done"])
        self._run(provider)
        self.assertIn("工作目录：/tmp", provider.messages_per_turn[0])

    def test_untrusted_section_present_with_network_tool(self) -> None:
        """
        工具集含网络访问工具时，「外部不可信内容」段必须出现。

        这条是 web_fetch 扩展那个坑的护栏：漏注入的表现是「子 Agent 能抓到
        投毒页面却没有那道约束」，界面上完全看不出来。
        """
        marker = "<<不可信内容约束>>"
        provider = _SayProvider(["done"])
        self._run(
            provider, spec=_spec(tools=("read_file", "web_fetch")), untrusted=marker
        )

        self.assertIn(marker, provider.messages_per_turn[0])

    def test_untrusted_section_absent_without_network_tool(self) -> None:
        """
        **反证**：不含网络访问工具时该段不出现。

        没有这一条，「无条件注入」也能让上一条通过——而那会给每个只读角色
        白白多塞一段与它无关的约束。
        """
        marker = "<<不可信内容约束>>"
        provider = _SayProvider(["done"])
        self._run(provider, spec=_spec(tools=("read_file",)), untrusted=marker)

        self.assertNotIn(marker, provider.messages_per_turn[0])

    def test_untrusted_section_follows_final_toolset_not_declaration(self) -> None:
        """
        判据是**最终**工具集，不是角色声明。

        角色声明了 web_fetch 但被黑名单减掉时，那段不该出现——
        它此刻确实拿不到网络。
        """
        marker = "<<不可信内容约束>>"
        provider = _SayProvider(["done"])
        self._run(
            provider,
            spec=_spec(tools=("read_file", "web_fetch"), disallowed_tools=("web_fetch",)),
            untrusted=marker,
        )

        self.assertNotIn(marker, provider.messages_per_turn[0])


class ToolExclusionTest(RunnerBase):
    """AC11b：白名单之外的工具既不发给模型，调用了也拒绝。"""

    def test_only_whitelisted_tools_sent(self) -> None:
        provider = _SayProvider(["done"])
        self._run(provider, spec=_spec(tools=("read_file",)))

        self.assertEqual(provider.tool_names_per_turn[0], ["read_file"])

    def test_calling_excluded_tool_is_refused(self) -> None:
        provider = _SayProvider(
            [ToolCall(id="c1", name="write_file", arguments={"path": "x"}), "好吧"]
        )
        self._run(provider, spec=_spec(tools=("read_file",)))

        self.assertEqual(self.writer.executed, 0, "白名单外的工具绝不能真的执行")


# --------------------------------------------------------------------------- #
# 三条反证
# --------------------------------------------------------------------------- #


class PermissionIsolationTest(RunnerBase):
    """AC14b / AC15 / AC15b：权限的三条隔离判据。"""

    def test_effective_mode_is_the_narrower_one(self) -> None:
        self.engine.mode = PermissionMode.PERMISSIVE
        spy = _EngineSpy(self.engine)
        provider = _SayProvider(["done"])
        self._run(
            provider,
            spec=_spec(permission_mode=PermissionMode.STRICT),
            runtime=self._runtime(provider, engine=spy, mode=PermissionMode.PERMISSIVE),
        )

        self.assertEqual(len(spy.derived), 1)
        self.assertIs(spy.derived[0].mode, PermissionMode.STRICT)

    def test_declared_permissive_does_not_escalate(self) -> None:
        """角色声明放行、主对话默认档 → 实际生效仍是默认档（spec F16）。"""
        spy = _EngineSpy(self.engine)
        provider = _SayProvider(["done"])
        self._run(
            provider,
            spec=_spec(permission_mode=PermissionMode.PERMISSIVE),
            runtime=self._runtime(provider, engine=spy, mode=PermissionMode.DEFAULT),
        )

        self.assertIs(spy.derived[0].mode, PermissionMode.DEFAULT)

    def test_main_engine_mode_unchanged(self) -> None:
        """
        **反证**：跑完之后主引擎的档位没变。

        只断言「子 Agent 用对了档位」证明不了这一点。若运行器图省事写了
        `engine.mode = ...`，前一条照样过，而主对话的权限档位被一个后台线程
        静默改掉了——界面上完全看不出来。
        """
        self.engine.mode = PermissionMode.DEFAULT
        self._run(
            _SayProvider(["done"]), spec=_spec(permission_mode=PermissionMode.STRICT)
        )

        self.assertIs(self.engine.mode, PermissionMode.DEFAULT)

    def test_turn_rules_not_inherited(self) -> None:
        """
        **反证**：回合级预授权不跟着委派跑出去（spec F17）。

        场景：主对话里一个 Skill 的 `allowed-tools` 授予了放行，
        随后模型在同一次执行里发起委派。
        """
        self.engine.grant_turn_rules(
            [Rule(effect="allow", tool="Write", pattern="*", source="session")]
        )
        spy = _EngineSpy(self.engine)
        provider = _SayProvider(["done"])
        self._run(
            provider,
            runtime=self._runtime(provider, engine=spy),
        )

        self.assertEqual(spy.derived[0].turn_rules, [])
        # 主引擎那条还在——派生不该动它
        self.assertEqual(len(self.engine.turn_rules), 1)

    def test_session_rules_are_inherited(self) -> None:
        """
        与上一条**刻意相反**：会话级放行要继承（AC15b）。

        它是用户在确认面板上明确授予的，理应对子 Agent 生效。
        两条一起写出来，防止后来者把其中一条当 bug 修掉。
        """
        self.engine.session_rules.append(
            Rule(effect="allow", tool="Write", pattern="*", source="session")
        )
        spy = _EngineSpy(self.engine)
        provider = _SayProvider(["done"])
        self._run(provider, runtime=self._runtime(provider, engine=spy))

        self.assertIs(spy.derived[0].session_rules, self.engine.session_rules)


class NonInteractiveTest(RunnerBase):
    """AC13a：判 ASK 直接拒，且拒绝理由是引导性的。"""

    def test_ask_decision_is_auto_denied(self) -> None:
        provider = _SayProvider(
            [ToolCall(id="c1", name="write_file", arguments={"path": "x"}), "我写不了。"]
        )
        record = self._run(provider)

        self.assertEqual(self.writer.executed, 0)
        self.assertIs(record.status, TaskStatus.COMPLETED)

    def test_allow_rule_lets_it_through(self) -> None:
        """AC13b：用户配了 allow 规则后同一任务能成功写入。"""
        engine = PermissionEngine(
            RuleSet([Rule(effect="allow", tool="Write", pattern="*", source="user")]),
            mode=PermissionMode.DEFAULT,
        )
        provider = _SayProvider(
            [ToolCall(id="c1", name="write_file", arguments={"path": "x"}), "写好了。"]
        )
        self._run(provider, runtime=self._runtime(provider, engine=engine))

        self.assertEqual(self.writer.executed, 1)


class HookInjectionTest(RunnerBase):
    """AC22b：子 Agent 不消费主对话的 Hook 注入队列。"""

    def test_injection_queue_untouched(self) -> None:
        """
        **反证**：跑完之后队列**仍然非空**。

        这是本章最隐蔽的一处坑：`consume_injections` 是会被**取走**的队列，
        后台子 Agent 消费它，会让一条挂在 `turn_start` 上的注入型 Hook
        从主对话里凭空消失——不报错、不留痕，用户只会觉得「我的 Hook 偶尔不生效」。
        """
        before = self.hooks.injections
        self._run(_SayProvider(["done"]))

        self.assertEqual(self.hooks.injections, before)
        self.assertTrue(self.hooks.injections)


# --------------------------------------------------------------------------- #
# 非正常结束
# --------------------------------------------------------------------------- #


class AbnormalEndTest(RunnerBase):
    """AC10 / AC25c。"""

    def test_cancelled_does_not_return_the_preamble(self) -> None:
        """
        **AC10 的核心反例**：被取消时，最后那条 assistant 正文里的「前言」
        绝不能被当成结论回流。

        C11 踩过这个坑——计划被拒时最后一条 assistant 是
        「我打算这样做：……」，按「找最后一条非空 assistant」会把它当结论，
        用户以为任务完成了。
        """
        spec = _spec()
        toolset = resolve_toolset(self.all_names, spec)
        record = self.tasks.create("role", "explorer", "t")
        record.cancel_event.set()   # 开局即取消

        provider = _SayProvider(["我打算这样做：先读文件……"])
        run_subagent(
            self._runtime(provider), spec, "t", record, self.tasks,
            toolset, self.all_names,
        )

        self.assertIs(record.status, TaskStatus.CANCELLED)
        self.assertNotIn("我打算这样做", record.conclusion)
        self.assertIn("取消", record.conclusion)

    def test_max_iterations_message_mentions_budget(self) -> None:
        """轮次用尽的说明里要出现轮数，用户才知道去调 max_turns。"""
        # 一个永远调工具的模型 → 必然撞上限
        provider = _SayProvider(
            [ToolCall(id=f"c{i}", name="read_file", arguments={"path": "a"}) for i in range(50)]
        )
        record = self._run(provider, spec=_spec(max_turns=2))

        self.assertIs(record.status, TaskStatus.FAILED)
        self.assertIn("轮次预算", record.conclusion)

    def test_own_budget_does_not_touch_main(self) -> None:
        """AC9b：子 Agent 用满自己的预算，与主对话的 25 轮无关。"""
        provider = _SayProvider(
            [ToolCall(id=f"c{i}", name="read_file", arguments={"path": "a"}) for i in range(50)]
        )
        self._run(provider, spec=_spec(max_turns=2))
        # 只跑了自己的 2 轮，不是主对话的 25 轮
        self.assertLessEqual(provider.calls, 4)

    def test_provider_exception_becomes_failed_task(self) -> None:
        """
        AC25c：异常不逃逸，任务不会永远停在「运行中」。

        后台线程的异常无人接管——逃逸出去会让前台等待方永远阻塞。
        """
        record = self._run(_ExplodingProvider())

        self.assertIs(record.status, TaskStatus.FAILED)
        self.assertTrue(record.done_event.is_set())
        self.assertIn("出错", record.conclusion)


class BranchDelegationTest(RunnerBase):
    """AC8a / AC8b：分支式继承父历史快照。"""

    def test_parent_history_is_visible(self) -> None:
        parent = ParentSnapshot(
            history=(Message(role="user", content="项目代号叫 Rhine"),),
            stable="父对话的稳定提示",
            tool_names=("read_file",),
        )
        provider = _SayProvider(["代号是 Rhine。"])
        record = self._run(provider, spec=None, task="说出项目代号", parent=parent)

        self.assertEqual(record.conclusion, "代号是 Rhine。")
        self.assertEqual(provider.stables[0], "父对话的稳定提示")

    def test_snapshot_is_a_copy(self) -> None:
        """
        父历史是**快照**：子 Agent 跑起来之后主对话追加的消息不该进入它的视野。

        这里用「运行器不得修改传入的 tuple」作为可测判据——它是不可变的，
        运行器必须自己建列表。
        """
        original = (Message(role="user", content="a"),)
        parent = ParentSnapshot(history=original, stable="s", tool_names=())
        self._run(_SayProvider(["done"]), spec=None, task="t", parent=parent)

        self.assertEqual(len(original), 1, "父快照不得被就地追加")


if __name__ == "__main__":
    unittest.main()
