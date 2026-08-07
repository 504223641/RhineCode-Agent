"""
Plan Mode 规划阶段的委派约束（c13 F19a）。

## 这一章在解决什么

Plan Mode 的承诺是「**批准前不动手**」。规划恰恰是最需要把调研赶出主上下文的
场景（规划要读很多东西，而那些内容要一路背到执行阶段），所以委派工具在规划阶段
仍然开放——但**只能委派给最终工具集全只读的角色**。

## 顺带堵掉的一个洞

规划阶段守卫原本写的是 `planning and not read_only and not system_serial`。
那条 `system_serial` 豁免是为 `load_skill` 写的，而 `load_skill` 是
`read_only=True`、本来就被前一个条件挡在外面，于是豁免长期空转。

C13 的 `run_agent` 恰好把它激活了：`system_serial=True` **且** `read_only=False`。
后果实测过——规划阶段模型硬造一个 `run_agent` 调用，它既没被守卫挡下、
又因为 `system_serial` 不进权限管线，**直接执行了**。

豁免条件已改成 `plan_safe`：一个工具必须**明确声明**自己规划期安全才被放行。
本模块的 `PlanGuardTest` 用两个只差这一个标志的假工具把这条钉死。
"""

from __future__ import annotations

import threading
import unittest
from pathlib import Path

from rhinecode.agent.loop import Agent, RunOptions
from rhinecode.permission.engine import PermissionEngine
from rhinecode.permission.models import PermissionMode
from rhinecode.permission.rules import RuleSet
from rhinecode.provider.base import BaseProvider, Message, StreamChunk, ToolCall
from rhinecode.subagents.models import AgentCatalog, AgentSource, AgentSpec
from rhinecode.subagents.runner import SubAgentRuntime
from rhinecode.subagents.service import SubAgentService
from rhinecode.subagents.tasks import KIND_BRANCH, KIND_ROLE
from rhinecode.tools.base import Tool, ToolResult
from rhinecode.tools.registry import ToolRegistry
from rhinecode.tools.run_agent import RunAgentTool


# --------------------------------------------------------------------------- #
# 测试替身
# --------------------------------------------------------------------------- #


class _Reader(Tool):
    name = "read_file"
    description = "fake"
    parameters = {"type": "object", "properties": {}}
    read_only = True

    def execute(self, args: dict) -> ToolResult:
        return ToolResult(ok=True, output="x", summary="x")


class _Searcher(Tool):
    name = "grep_content"
    description = "fake"
    parameters = {"type": "object", "properties": {}}
    read_only = True

    def execute(self, args: dict) -> ToolResult:
        return ToolResult(ok=True, output="x", summary="x")


class _Writer(Tool):
    name = "write_file"
    description = "fake"
    parameters = {"type": "object", "properties": {}}
    read_only = False

    def __init__(self) -> None:
        self.executed = 0

    def execute(self, args: dict) -> ToolResult:
        self.executed += 1
        return ToolResult(ok=True, output="written", summary="written")


class _SerialNoPlanSafe(Tool):
    """
    `system_serial=True` + `read_only=False`，但**不**声明 `plan_safe`。

    这正是修复前 `run_agent` 的形态——它当时能在规划阶段执行。
    """

    name = "sneaky"
    description = "fake"
    parameters = {"type": "object", "properties": {}}
    read_only = False
    system_serial = True

    def __init__(self) -> None:
        self.executed = 0

    def execute(self, args: dict) -> ToolResult:
        self.executed += 1
        return ToolResult(ok=True, output="做了副作用的事", summary="oops")


class _PlanSafeTool(Tool):
    """与上一个只差 `plan_safe = True`——它应当被放行，并收到阶段。"""

    name = "declared"
    description = "fake"
    parameters = {"type": "object", "properties": {}}
    read_only = False
    system_serial = True
    plan_safe = True

    def __init__(self) -> None:
        self.executed = 0
        self.stages: list[bool] = []

    def execute(self, args: dict, plan_stage: bool = False) -> ToolResult:
        self.executed += 1
        self.stages.append(plan_stage)
        return ToolResult(ok=True, output="ok", summary="ok")


class _CallsOnce(BaseProvider):
    """第一轮硬造一个调用，之后说话。"""

    def __init__(self, tool_name: str) -> None:
        self.tool_name = tool_name
        self.turns = 0
        self.schemas: list[list[str]] = []

    def stream_chat(self, messages, thinking_effort="off", tools=None, system=None):
        self.turns += 1
        self.schemas.append(sorted(t["function"]["name"] for t in (tools or [])))
        if self.turns == 1:
            yield StreamChunk(
                type="tool_call",
                tool_call=ToolCall(id="x1", name=self.tool_name, arguments={}),
            )
        else:
            yield StreamChunk(type="text", content="好的。")
        yield StreamChunk(type="done")


def _run_planning(tool: Tool, plan_mode: bool = True):
    """跑一轮 Plan Mode 的规划阶段（不批准计划，因此始终停在规划阶段）。"""
    registry = ToolRegistry()
    registry.register(tool)
    provider = _CallsOnce(tool.name)
    engine = PermissionEngine(RuleSet([]), mode=PermissionMode.DEFAULT)
    agent = Agent(provider, registry)
    events = list(
        agent.run(
            [Message(role="user", content="改点东西")],
            "off",
            plan_mode,
            "", lambda: "", "m", None,
            engine,
            lambda *a: True,          # ask 一律同意：模拟最宽松的用户
            None, None, threading.Event(),
            options=RunOptions(max_iterations=3),
        )
    )
    return provider, events


# --------------------------------------------------------------------------- #
# 循环层：规划阶段守卫
# --------------------------------------------------------------------------- #


class PlanGuardTest(unittest.TestCase):
    """
    豁免条件必须是 `plan_safe`，不是 `system_serial`。

    两个假工具只差这一个标志，行为必须相反——这是本模块最重要的一对判据。
    """

    def test_plain_side_effect_tool_is_blocked(self) -> None:
        """对照组：普通副作用工具照旧被挡（既有行为，零回归）。"""
        tool = _Writer()
        provider, _ = _run_planning(tool)

        self.assertNotIn("write_file", provider.schemas[0])
        self.assertEqual(tool.executed, 0)

    def test_system_serial_without_plan_safe_is_blocked(self) -> None:
        """
        **修复前的洞**：`system_serial=True` + `read_only=False` 且未声明
        `plan_safe` 的工具，现在必须被挡下。

        修复前这个用例会失败——那个工具既不在 schema 里、又能被硬造出来执行，
        而且因为 `system_serial` 连权限管线都不进。
        """
        tool = _SerialNoPlanSafe()
        provider, _ = _run_planning(tool)

        self.assertNotIn("sneaky", provider.schemas[0])
        self.assertEqual(tool.executed, 0, "未声明 plan_safe 的副作用工具绝不能在规划阶段执行")

    def test_plan_safe_tool_is_offered_and_told_the_stage(self) -> None:
        """
        **反证**：只把 `plan_safe` 加上，同一个工具就该被放行。

        没有这一条，一个「把所有非只读工具都挡死」的实现也能过上面两条，
        而那样委派在规划阶段就彻底用不了了。
        """
        tool = _PlanSafeTool()
        provider, _ = _run_planning(tool)

        self.assertIn("declared", provider.schemas[0], "plan_safe 工具应出现在规划阶段的 schema 里")
        self.assertEqual(tool.executed, 1)
        self.assertEqual(tool.stages, [True], "循环必须把「当前是规划阶段」告诉它")

    def test_normal_mode_passes_false(self) -> None:
        """
        普通模式（没开 Plan Mode）下 `plan_stage` 必须是 False。

        传错的话，委派会在普通对话里也被限制成只读角色——用户完全看不出为什么。
        """
        tool = _PlanSafeTool()
        _run_planning(tool, plan_mode=False)
        self.assertEqual(tool.stages, [False])


class RealToolDeclaresPlanSafeTest(unittest.TestCase):
    """结构护栏：真实的委派工具必须声明 `plan_safe`。"""

    def test_run_agent_is_plan_safe(self) -> None:
        self.assertTrue(RunAgentTool.plan_safe)

    def test_load_skill_is_not_plan_safe(self) -> None:
        """
        `load_skill` **不该**声明它——它靠 `read_only=True` 通过，
        声明 `plan_safe` 会让人误以为那是它进规划阶段的原因。
        """
        from rhinecode.tools.load_skill import LoadSkillTool

        self.assertFalse(LoadSkillTool.plan_safe)
        self.assertTrue(LoadSkillTool.read_only)

    def test_planning_schemas_includes_both_kinds(self) -> None:
        registry = ToolRegistry()
        registry.register(_Reader())
        registry.register(_Writer())
        registry.register(_PlanSafeTool())

        names = {s["function"]["name"] for s in registry.planning_schemas()}
        self.assertEqual(names, {"read_file", "declared"})


# --------------------------------------------------------------------------- #
# 服务层：规划阶段只许委派全只读角色
# --------------------------------------------------------------------------- #


def _spec(name, tools=None) -> AgentSpec:
    return AgentSpec(
        name=name,
        description="x",
        body="",
        source=AgentSource.BUILTIN,
        path=Path(f"{name}.md"),
        tools=tools,
    )


class _QuietProvider(BaseProvider):
    def __init__(self) -> None:
        self.calls = 0

    def stream_chat(self, messages, thinking_effort="off", tools=None, system=None):
        self.calls += 1
        yield StreamChunk(type="text", content="结论")
        yield StreamChunk(type="done")


class DelegateInPlanStageTest(unittest.TestCase):
    def setUp(self) -> None:
        self.registry = ToolRegistry()
        for tool in (_Reader(), _Searcher(), _Writer()):
            self.registry.register(tool)
        self.engine = PermissionEngine(RuleSet([]), mode=PermissionMode.DEFAULT)
        self.provider = _QuietProvider()

        runtime = SubAgentRuntime(
            provider_for=lambda name: self.provider,
            registry=self.registry,
            engine=self.engine,
            main_mode=lambda: self.engine.mode,
            environment_text=lambda: "env",
            default_model="m",
        )
        self.service = SubAgentService(
            AgentCatalog(
                specs={
                    # 全只读 → 规划阶段可用
                    "reader": _spec("reader", tools=("read_file", "grep_content")),
                    # 含写工具 → 规划阶段不可用
                    "worker": _spec("worker", tools=("read_file", "write_file")),
                    # 不声明白名单 = 继承全部（含写工具）→ 规划阶段不可用
                    "generalist": _spec("generalist"),
                }
            ),
            runtime,
            tool_names_provider=self.registry.names,
        )

    def _settle(self) -> None:
        import time

        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if all(r.status.is_terminal for r in self.service.tasks.snapshot()):
                return
            time.sleep(0.01)

    def test_read_only_role_allowed(self) -> None:
        outcome = self.service.delegate(
            KIND_ROLE, "reader", "去查点东西", plan_stage=True
        )
        self.assertTrue(outcome.ok, outcome.text)
        self._settle()

    def test_role_with_write_tools_rejected(self) -> None:
        outcome = self.service.delegate(
            KIND_ROLE, "worker", "去改点东西", plan_stage=True
        )

        self.assertFalse(outcome.ok)
        self.assertIn("规划阶段", outcome.text)
        self.assertIn("write_file", outcome.text, "要指名道姓说出是哪个工具挡住了它")

    def test_rejection_lists_usable_read_only_roles(self) -> None:
        """
        **必须列出「现在能用哪些角色」**。

        只说「不许」的话模型只能猜，或者干脆放弃委派、把调研全做在主对话里
        ——那正是本功能要避免的事。
        """
        outcome = self.service.delegate(KIND_ROLE, "worker", "t", plan_stage=True)

        self.assertIn("reader", outcome.text)
        self.assertNotIn("可用的只读角色：worker", outcome.text)

    def test_rejection_does_not_start_anything(self) -> None:
        """被拒时**不起线程、不发 API**——与 F14/F20 的失败路径同口径。"""
        self.service.delegate(KIND_ROLE, "worker", "t", plan_stage=True)

        self.assertEqual(self.provider.calls, 0)
        self.assertEqual(self.service.tasks.snapshot(), ())

    def test_inheriting_role_rejected_in_plan_stage(self) -> None:
        """不声明白名单的角色继承了写工具，规划阶段同样不可用。"""
        outcome = self.service.delegate(KIND_ROLE, "generalist", "t", plan_stage=True)
        self.assertFalse(outcome.ok)

    def test_branch_rejected_in_plan_stage(self) -> None:
        """
        分支式继承主对话的完整工具集（含写工具），因此规划阶段一律不可用。
        """
        from rhinecode.subagents.runner import ParentSnapshot

        outcome = self.service.delegate(
            KIND_BRANCH, "", "t",
            parent=ParentSnapshot(history=(), stable="s", tool_names=()),
            plan_stage=True,
        )
        self.assertFalse(outcome.ok)
        self.assertIn("规划阶段", outcome.text)

    def test_no_restriction_outside_plan_stage(self) -> None:
        """
        **反证**：不在规划阶段时，含写工具的角色照常可委派。

        没有这一条，一个「无条件只许只读角色」的实现也能过上面几条，
        而那会让 general-purpose 在任何时候都用不了。
        """
        outcome = self.service.delegate(KIND_ROLE, "worker", "t")
        self.assertTrue(outcome.ok, outcome.text)
        self._settle()

    def test_unknown_tool_name_counts_as_writable(self) -> None:
        """
        注册中心里查不到的工具名按**有副作用**处理。

        宁可多挡一次，也不要因为一个查不到的名字把「批准前不动手」的承诺放过去。
        """
        writable = self.service._writable_tools(frozenset({"read_file", "不存在的工具"}))
        self.assertIn("不存在的工具", writable)
        self.assertNotIn("read_file", writable)


if __name__ == "__main__":
    unittest.main()
