"""
Agent 循环的工具收窄与 RunOptions 单测（c11 T36）。

覆盖 spec AC8、AC14（逐轮收窄）、AC15（Plan Mode 交互）、AC23（子对话排除
load_skill）、AC29（动态段零回归）、AC33（子对话独立迭代预算），
以及**改造点 1 的核心回归**——dynamic 每轮求值、且第 N 轮激活的 Skill
在第 N+1 轮真的出现在提醒里。
"""

import threading
import unittest
from pathlib import Path

from rhinecode.agent.loop import Agent, RunOptions
from rhinecode.agent.events import AgentEventType, StopReason
from rhinecode.permission.engine import PermissionEngine
from rhinecode.permission.models import PermissionMode
from rhinecode.permission.rules import RuleSet
from rhinecode.provider.base import Message, StreamChunk, ToolCall
from rhinecode.tools.base import Tool, ToolResult
from rhinecode.tools.policy import ToolPolicy
from rhinecode.tools.registry import ToolRegistry


class FakeTool(Tool):
    """一个什么都不做的工具，只为出现在 schema 列表里。"""

    def __init__(self, name: str, read_only: bool = True) -> None:
        self.name = name
        self.description = f"{name} 的说明"
        self.parameters = {"type": "object", "properties": {}}
        self.read_only = read_only

    def execute(self, args: dict) -> ToolResult:
        return ToolResult(ok=True, output="ok")


class RecordingProvider:
    """
    记录每一轮实际发出的 messages 与 tools，用于断言「模型看到了什么」。

    :param scripts: 每轮的响应脚本，每项是 list[StreamChunk]；
                    轮数超出脚本时重复最后一项。
    """

    def __init__(self, scripts) -> None:
        self.scripts = scripts
        self.calls: list[dict] = []

    def stream_chat(self, messages, thinking_effort, tools=None, system=None):
        idx = min(len(self.calls), len(self.scripts) - 1)
        self.calls.append(
            {"messages": list(messages), "tools": tools, "system": system}
        )
        yield from self.scripts[idx]


def _text_round(text: str = "完成"):
    return [StreamChunk(type="text", content=text), StreamChunk(type="done")]


def _tool_round(name: str, call_id: str = "c1", args=None):
    return [
        StreamChunk(
            type="tool_call",
            tool_call=ToolCall(id=call_id, name=name, arguments=args or {}),
        ),
        StreamChunk(type="done"),
    ]


def _engine() -> PermissionEngine:
    # PERMISSIVE：本文件测的是「工具集怎么收窄」，不是权限，
    # 用放行模式避免权限层干扰。
    return PermissionEngine(RuleSet([]), mode=PermissionMode.PERMISSIVE)


def _run(agent, provider, history=None, options=RunOptions(), dynamic=None,
         plan_mode=False):
    return list(
        agent.run(
            history if history is not None else [Message(role="user", content="go")],
            "off",
            plan_mode,
            "",
            dynamic or (lambda: ""),
            "model",
            None,
            _engine(),
            lambda *a: True,
            None,
            lambda _p: True,
            threading.Event(),
            options=options,
        )
    )


def _tool_names(call) -> set:
    return {t["function"]["name"] for t in (call["tools"] or [])}


class DefaultBehaviourTest(unittest.TestCase):
    """不传 options 时行为与 C10 一致（N3 零回归）。"""

    def test_no_options_means_no_narrowing(self) -> None:
        registry = ToolRegistry()
        registry.register(FakeTool("read_file"))
        registry.register(FakeTool("write_file", read_only=False))
        provider = RecordingProvider([_text_round()])
        _run(Agent(provider, registry), provider)
        self.assertEqual(_tool_names(provider.calls[0]), {"read_file", "write_file"})

    def test_dynamic_zero_regression_when_nothing_active(self) -> None:
        """
        未激活任何 Skill 时，dynamic 的输出与 C10 口径逐字节相等（AC29）。

        这是 N3 零回归的硬护栏：Skill 系统装上之后，没用它的人不该看到
        系统提示有任何变化。
        """
        registry = ToolRegistry()
        provider = RecordingProvider([_text_round()])
        base = "环境信息：工作区 /x"
        _run(Agent(provider, registry), provider, dynamic=lambda: base)
        reminder = provider.calls[0]["messages"][-1]
        self.assertEqual(reminder.role, "system")
        self.assertIn(base, reminder.content)


class ToolPolicyFilterTest(unittest.TestCase):
    """按 ToolPolicy 过滤工具集（AC14/AC23）。"""

    def _registry(self) -> ToolRegistry:
        r = ToolRegistry()
        for n in ("read_file", "run_command", "glob_files"):
            r.register(FakeTool(n))
        r.register(FakeTool("load_skill"))
        return r

    def test_allowed_narrows_the_set(self) -> None:
        provider = RecordingProvider([_text_round()])
        policy = ToolPolicy(
            allowed=frozenset({"read_file"}),
            exempt=frozenset({"load_skill"}),
            excluded=frozenset(),
        )
        _run(
            Agent(provider, self._registry()),
            provider,
            options=RunOptions(tool_policy=lambda: policy),
        )
        self.assertEqual(_tool_names(provider.calls[0]), {"read_file", "load_skill"})

    def test_exempt_survives_the_whitelist(self) -> None:
        """load_skill 必须穿透白名单，否则模型激活第一个 Skill 后就加载不了第二个。"""
        provider = RecordingProvider([_text_round()])
        policy = ToolPolicy(
            allowed=frozenset({"glob_files"}),
            exempt=frozenset({"load_skill"}),
            excluded=frozenset(),
        )
        _run(
            Agent(provider, self._registry()),
            provider,
            options=RunOptions(tool_policy=lambda: policy),
        )
        self.assertIn("load_skill", _tool_names(provider.calls[0]))

    def test_call_to_narrowed_out_tool_is_refused_not_executed(self) -> None:
        """
        模型硬调一个**本轮没发给它**的工具 → 不执行，回灌结构化原因。

        这不是假想情况：实测 DeepSeek 在只收到 4 个工具 schema 的情况下，
        凭训练先验造出了一次 `edit_file` 调用，连参数名都猜对了，于是一个
        白名单只含读文件的 Skill 把源码给改了。照常执行等于 allowed_tools
        白声明——收窄了「发什么」却不管「收到什么」。

        判据是行为：工具的 execute 没被调用，且结果里说明了当前可用工具。
        """
        executed: list[str] = []

        class SpyTool(FakeTool):
            def execute(self, args):
                executed.append(self.name)
                return ToolResult(ok=True, output="ok")

        registry = ToolRegistry()
        registry.register(SpyTool("read_file"))
        registry.register(SpyTool("write_file", read_only=False))
        registry.register(SpyTool("load_skill"))

        provider = RecordingProvider([_tool_round("write_file"), _text_round()])
        policy = ToolPolicy(
            allowed=frozenset({"read_file"}),
            exempt=frozenset({"load_skill"}),
            excluded=frozenset(),
        )
        events = _run(
            Agent(provider, registry),
            provider,
            options=RunOptions(tool_policy=lambda: policy),
        )

        self.assertEqual(executed, [])
        outputs = [
            e.tool_result.output
            for e in events
            if e.type is AgentEventType.TOOL_RESULT and e.tool_result is not None
        ]
        self.assertTrue(outputs)
        self.assertIn("write_file", outputs[0])
        self.assertIn("不在当前 Skill 声明的工具集内", outputs[0])
        # 必须告诉模型现在能用什么，否则它只能继续瞎试
        self.assertIn("read_file", outputs[0])
        # 反面：不能把 ask_user / present_plan 这类不在注册中心的工具列给它
        self.assertNotIn("ask_user", outputs[0])
        # 循环不终止：拒绝之后仍继续下一轮
        self.assertEqual(len(provider.calls), 2)

    def test_call_is_executed_normally_when_no_policy(self) -> None:
        """无收窄策略时一切照旧执行（零回归）。"""
        executed: list[str] = []

        class SpyTool(FakeTool):
            def execute(self, args):
                executed.append(self.name)
                return ToolResult(ok=True, output="ok")

        registry = ToolRegistry()
        registry.register(SpyTool("write_file", read_only=False))
        provider = RecordingProvider([_tool_round("write_file"), _text_round()])
        _run(Agent(provider, registry), provider)

        self.assertEqual(executed, ["write_file"])

    def test_excluded_beats_exempt(self) -> None:
        """
        excluded 优先级高于 exempt（AC23）。

        子对话就是靠这条禁止嵌套激活 Skill 的；若 exempt 能翻掉 excluded，
        F23 直接失效。
        """
        provider = RecordingProvider([_text_round()])
        policy = ToolPolicy(
            allowed=None,
            exempt=frozenset({"load_skill"}),
            excluded=frozenset({"load_skill"}),
        )
        _run(
            Agent(provider, self._registry()),
            provider,
            options=RunOptions(tool_policy=lambda: policy),
        )
        self.assertNotIn("load_skill", _tool_names(provider.calls[0]))

    def test_plan_mode_intersects_whitelist_with_readonly(self) -> None:
        """
        Plan Mode 规划阶段 + 白名单 → 「白名单 ∩ 只读」+ ask_user/present_plan（AC15）。

        两个特殊工具是在过滤**之后**才拼上的，所以天然不受白名单影响（F15）。
        """
        r = ToolRegistry()
        r.register(FakeTool("read_file"))
        r.register(FakeTool("glob_files"))
        r.register(FakeTool("run_command", read_only=False))
        provider = RecordingProvider([_text_round()])
        policy = ToolPolicy(
            allowed=frozenset({"read_file", "run_command"}),
            exempt=frozenset(),
            excluded=frozenset(),
        )
        _run(
            Agent(provider, r),
            provider,
            options=RunOptions(tool_policy=lambda: policy),
            plan_mode=True,
        )
        names = _tool_names(provider.calls[0])
        # run_command 虽在白名单里，但规划阶段只给只读工具。
        self.assertEqual(names, {"read_file", "ask_user", "present_plan"})


class PerRoundEvaluationTest(unittest.TestCase):
    """改造点 1 的核心回归：dynamic 与 tool_policy 每轮求值。"""

    def _registry(self) -> ToolRegistry:
        r = ToolRegistry()
        r.register(FakeTool("read_file"))
        r.register(FakeTool("load_skill"))
        return r

    def test_dynamic_called_once_per_round(self) -> None:
        calls = {"n": 0}

        def dynamic() -> str:
            calls["n"] += 1
            return ""

        provider = RecordingProvider([_tool_round("read_file"), _text_round()])
        _run(Agent(provider, self._registry()), provider, dynamic=dynamic)
        self.assertEqual(calls["n"], 2)

    def test_tool_policy_called_once_per_round(self) -> None:
        calls = {"n": 0}

        def policy_fn():
            calls["n"] += 1
            return None

        provider = RecordingProvider([_tool_round("read_file"), _text_round()])
        _run(
            Agent(provider, self._registry()),
            provider,
            options=RunOptions(tool_policy=policy_fn),
        )
        self.assertEqual(calls["n"], 2)

    def test_skill_activated_in_round_1_is_visible_in_round_2(self) -> None:
        """
        **改造点 1 的要害护栏**（AC9）。

        只验「dynamic 被调了 N 次」证明不了「第 N+1 轮真的看到了正文」——
        实现完全可能每轮都调回调、却把结果丢掉。这里用真实 SkillManager
        端到端跑两轮：第 1 轮模型调 load_skill 激活 Skill，第 2 轮返回文本。

        断言第 1 轮的 <system-reminder> **不含** SOP 正文，第 2 轮的**含**。
        这同时锁住了 spec 审查阶段那条阻塞项：模型激活了 Skill，本次循环剩余
        轮次却完全看不到指令。
        """
        import tempfile

        from rhinecode.skills.manager import SkillManager
        from rhinecode.tools.load_skill import LoadSkillTool

        marker = "这是只有加载后才该出现的SOP正文MARKER"
        with tempfile.TemporaryDirectory() as tmp:
            user_dir = Path(tmp) / "user"
            skills = user_dir / "skills"
            skills.mkdir(parents=True)
            (skills / "s.md").write_text(
                f"---\nname: s\ndescription: 说明\n---\n{marker}\n", encoding="utf-8"
            )

            manager = SkillManager(
                project_root=None,
                user_dir=user_dir,
                builtin_dir=None,
                has_short_command=lambda _n: False,
            )
            manager.startup(frozenset({"load_skill"}))

            registry = ToolRegistry()
            registry.register(LoadSkillTool(manager))

            provider = RecordingProvider(
                [
                    _tool_round("load_skill", args={"name": "s"}),
                    _text_round(),
                ]
            )

            def dynamic() -> str:
                # 与 conversation 层的真实拼法同构：环境信息 + 已激活 Skill 正文。
                parts = ["环境信息"]
                active = manager.active_text()
                if active:
                    parts.append(active)
                return "\n\n".join(parts)

            events = _run(Agent(provider, registry), provider, dynamic=dynamic)

        self.assertEqual(len(provider.calls), 2)
        round1 = provider.calls[0]["messages"][-1].content
        round2 = provider.calls[1]["messages"][-1].content
        self.assertNotIn(marker, round1, "第 1 轮不该看到尚未激活的 SOP")
        self.assertIn(marker, round2, "第 2 轮必须看到刚激活的 SOP（改造点 1 失效）")
        self.assertIs(events[-1].stop_reason, StopReason.COMPLETED)


class RunOptionsSwitchTest(unittest.TestCase):
    """max_iterations / record_usage / allow_summary 三个开关。"""

    def test_max_iterations_uses_option_value(self) -> None:
        """子对话有独立且更小的迭代预算，超限文案里的数字也要跟着改（AC33/N6）。"""
        r = ToolRegistry()
        r.register(FakeTool("read_file"))
        # 永远只调工具、不结束 → 必然撞上限。
        provider = RecordingProvider([_tool_round("read_file")])
        events = _run(
            Agent(provider, r), provider, options=RunOptions(max_iterations=2)
        )
        self.assertEqual(len(provider.calls), 2)
        finished = events[-1]
        self.assertIs(finished.stop_reason, StopReason.MAX_ITERATIONS)
        self.assertIn("2", finished.message)

    def test_record_usage_false_skips_anchor_update(self) -> None:
        class FakeContext:
            def __init__(self) -> None:
                self.recorded = 0

            def before_request(self, history, allow_summary=True):
                self.recorded_allow = allow_summary
                return []

            def record_usage(self, usage, sent_len):
                self.recorded += 1

        class UsageProvider(RecordingProvider):
            def stream_chat(self, messages, thinking_effort, tools=None, system=None):
                self.calls.append({"messages": list(messages), "tools": tools})
                yield StreamChunk(type="text", content="ok")
                yield StreamChunk(
                    type="usage", usage=type("U", (), {"prompt_tokens": 100})()
                )
                yield StreamChunk(type="done")

        ctx = FakeContext()
        provider = UsageProvider([])
        agent = Agent(provider, ToolRegistry())
        list(
            agent.run(
                [Message(role="user", content="go")],
                "off", False, "", lambda: "", "model", None,
                _engine(), lambda *a: True, None, None, threading.Event(),
                context_manager=ctx,
                options=RunOptions(record_usage=False),
            )
        )
        self.assertEqual(ctx.recorded, 0)

    def test_allow_summary_passed_through_to_context_manager(self) -> None:
        """allow_summary 必须原样透传给 before_request（子对话只跑第一层）。"""

        class FakeContext:
            def __init__(self) -> None:
                self.seen = []

            def before_request(self, history, allow_summary=True):
                self.seen.append(allow_summary)
                return []

            def record_usage(self, usage, sent_len):
                pass

        ctx = FakeContext()
        provider = RecordingProvider([_text_round()])
        agent = Agent(provider, ToolRegistry())
        list(
            agent.run(
                [Message(role="user", content="go")],
                "off", False, "", lambda: "", "model", None,
                _engine(), lambda *a: True, None, None, threading.Event(),
                context_manager=ctx,
                options=RunOptions(allow_summary=False),
            )
        )
        self.assertEqual(ctx.seen, [False])


if __name__ == "__main__":
    unittest.main()
