"""
委派工具与角色清单的单测（c13 T24，覆盖 AC6a）。

两块内容：

- **工具本身**：结构标志、参数校验、异常不外抛；
- **同口径护栏**：工具描述与清单表头是模型决定「要不要委派」时读到的
  唯一两处文本，一处强一处弱等于白改。这是 C11 原样踩过的坑。
"""

from __future__ import annotations

import unittest
from pathlib import Path

from rhinecode.subagents.models import AgentCatalog, AgentSource, AgentSpec
from rhinecode.subagents.render import _INDEX_HEADER, render_agent_index
from rhinecode.subagents.service import DelegateOutcome
from rhinecode.subagents.tasks import KIND_BRANCH, KIND_ROLE
from rhinecode.tools.run_agent import RunAgentTool


class _FakeService:
    """记下 delegate 收到了什么，并按预设返回。"""

    def __init__(self, outcome: DelegateOutcome = None, raises: bool = False) -> None:
        self.calls: list[tuple] = []
        self.plan_stages: list[bool] = []
        self._outcome = outcome or DelegateOutcome(ok=True, text="结论", task_id="a3f1c9")
        self._raises = raises

    def delegate(
        self, kind, agent_name, task_text, background=False, parent=None,
        plan_stage=False,
    ):
        self.calls.append((kind, agent_name, task_text, background, parent))
        self.plan_stages.append(plan_stage)
        if self._raises:
            raise RuntimeError("服务炸了")
        return self._outcome


def _spec(name="explorer", description="调研用") -> AgentSpec:
    return AgentSpec(
        name=name,
        description=description,
        body="",
        source=AgentSource.BUILTIN,
        path=Path(f"{name}.md"),
    )


class ToolShapeTest(unittest.TestCase):
    """结构护栏：改动这两个标志会当场红。"""

    def test_system_serial_and_not_read_only(self) -> None:
        """
        `system_serial=True`：它会开一整条子对话，放进只读并发桶会占着槽位干等，
        把同轮其它只读工具一起堵住。
        `read_only=False`：它确实会产生副作用（子 Agent 可能写文件）。
        """
        self.assertTrue(RunAgentTool.system_serial)
        self.assertFalse(RunAgentTool.read_only)

    def test_schema_requires_type_and_task(self) -> None:
        required = RunAgentTool.parameters["required"]
        self.assertIn("type", required)
        self.assertIn("task", required)
        self.assertNotIn("agent", required, "分支式不需要角色名")

    def test_type_enum_lists_both_kinds(self) -> None:
        enum = RunAgentTool.parameters["properties"]["type"]["enum"]
        self.assertEqual(set(enum), {KIND_ROLE, KIND_BRANCH})


class ExecuteTest(unittest.TestCase):
    def setUp(self) -> None:
        self.service = _FakeService()
        self.tool = RunAgentTool(self.service)

    def test_forwards_arguments(self) -> None:
        self.tool.execute(
            {"type": "role", "agent": "explorer", "task": "去查", "background": True}
        )
        kind, name, task, background, _ = self.service.calls[0]
        self.assertEqual((kind, name, task, background), ("role", "explorer", "去查", True))

    def test_background_defaults_to_false(self) -> None:
        self.tool.execute({"type": "role", "agent": "x", "task": "t"})
        self.assertFalse(self.service.calls[0][3])

    def test_success_result(self) -> None:
        result = self.tool.execute({"type": "role", "agent": "explorer", "task": "t"})
        self.assertTrue(result.ok)
        self.assertEqual(result.output, "结论")

    def test_failure_passes_through_service_text(self) -> None:
        """
        服务层已经把失败原因组织好了（列出可用角色、说明哪个工具名写错了），
        工具不得把它换成一句笼统的错误——那正是模型自我纠正所需的信息。
        """
        service = _FakeService(DelegateOutcome(ok=False, text="没有名为 nope 的角色。当前可用：explorer"))
        result = RunAgentTool(service).execute({"type": "role", "agent": "nope", "task": "t"})

        self.assertFalse(result.ok)
        self.assertIn("explorer", result.output)

    def test_summary_carries_task_id(self) -> None:
        """
        工具行上要带任务标识，用户才能把这一行与后来的完成通知对上。
        同时跑两个子 Agent 时，没有标识的话那两条通知无从区分。
        """
        result = self.tool.execute({"type": "role", "agent": "explorer", "task": "t"})
        self.assertIn("a3f1c9", result.summary)

    def test_backgrounded_summary_says_so(self) -> None:
        service = _FakeService(
            DelegateOutcome(ok=True, text="x", task_id="ff00aa", backgrounded=True)
        )
        result = RunAgentTool(service).execute({"type": "role", "agent": "e", "task": "t"})
        self.assertIn("后台", result.summary)

    def test_exception_does_not_escape(self) -> None:
        """
        `Tool` 契约要求实现不得向上抛：抛出去会被 Agent Loop 变成一条
        「工具执行异常」，丢掉服务层组织好的可读原因。
        """
        result = RunAgentTool(_FakeService(raises=True)).execute(
            {"type": "role", "agent": "e", "task": "t"}
        )
        self.assertFalse(result.ok)
        self.assertIn("委派失败", result.output)

    def test_missing_arguments_are_normalized_not_crashed(self) -> None:
        result = self.tool.execute({})
        self.assertIsNotNone(result)
        self.assertEqual(self.service.calls[0][:3], ("", "", ""))


class PlanStageForwardingTest(unittest.TestCase):
    """`plan_stage` 必须如实转发给服务层——它是 Plan Mode 承诺的最后一环。"""

    def test_defaults_to_false(self) -> None:
        """
        缺省 False：不经循环的调用（测试、将来的其它调用方）按普通模式处理。
        缺省成 True 的话，普通对话里的委派会莫名其妙只能用只读角色。
        """
        service = _FakeService()
        RunAgentTool(service).execute({"type": "role", "agent": "e", "task": "t"})
        self.assertEqual(service.plan_stages, [False])

    def test_forwarded_when_given(self) -> None:
        service = _FakeService()
        RunAgentTool(service).execute(
            {"type": "role", "agent": "e", "task": "t"}, plan_stage=True
        )
        self.assertEqual(service.plan_stages, [True])


class BranchSnapshotTest(unittest.TestCase):
    def test_snapshot_callback_used_for_branch(self) -> None:
        service = _FakeService()
        sentinel = object()
        tool = RunAgentTool(service, parent_snapshot=lambda: sentinel)

        tool.execute({"type": "branch", "task": "t"})
        self.assertIs(service.calls[0][4], sentinel)

    def test_snapshot_not_used_for_role(self) -> None:
        service = _FakeService()
        tool = RunAgentTool(service, parent_snapshot=lambda: object())

        tool.execute({"type": "role", "agent": "e", "task": "t"})
        self.assertIsNone(service.calls[0][4])

    def test_no_snapshot_callback_is_constructible(self) -> None:
        """没有协调层的环境（测试）里也能构造——分支式会由服务层给出提示。"""
        tool = RunAgentTool(_FakeService())
        self.assertIsNotNone(tool.execute({"type": "branch", "task": "t"}))


class SameVoiceTest(unittest.TestCase):
    """
    ⚠ **成对维护点的护栏**：工具描述与清单表头必须同口径。

    这两处是模型决定「要不要委派」时读到的唯一两处文本。C11 已经踩过一次：
    一处写成公告式、另一处写成指令式，模型按弱的那份行事，系统性欠触发。

    断言的是**四层意思**而不是逐字固化——措辞可以打磨，这四层不能丢。
    """

    def setUp(self) -> None:
        self.header = "\n".join(_INDEX_HEADER)
        self.desc = RunAgentTool.description

    def test_both_say_delegate_instead_of_doing_it_yourself(self) -> None:
        """①「用它替代你自己动手」——最关键的一句，少了它模型会把委派当可选项。"""
        for text, label in ((self.header, "清单表头"), (self.desc, "工具描述")):
            with self.subTest(where=label):
                self.assertIn("而不是自己动手做", text)

    def test_both_say_user_need_not_name_it(self) -> None:
        """②「用户不必点名」。"""
        for text, label in ((self.header, "清单表头"), (self.desc, "工具描述")):
            with self.subTest(where=label):
                self.assertIn("不必明确说", text)

    def test_both_say_when_unsure_delegate(self) -> None:
        """③「拿不准就委派」。"""
        for text, label in ((self.header, "清单表头"), (self.desc, "工具描述")):
            with self.subTest(where=label):
                self.assertIn("倾向委派", text)

    def test_both_explain_the_context_cost(self) -> None:
        """
        ④ 说明**上下文成本**——那是委派唯一真正的收益。

        不讲清楚的话模型无从权衡「多一次调用」与「省下的上下文」，
        默认会选自己动手（少一次调用）。
        """
        for text, label in ((self.header, "清单表头"), (self.desc, "工具描述")):
            with self.subTest(where=label):
                self.assertIn("上下文", text)


class IndexRenderTest(unittest.TestCase):
    def test_empty_catalog_renders_nothing(self) -> None:
        """
        无角色时返回**空串**，不是一个空清单。

        空清单会让模型看到「你有委派能力，但一个角色都没有」，
        进而可能反复尝试 run_agent 去试探。
        """
        self.assertEqual(render_agent_index(AgentCatalog()), "")

    def test_lists_name_and_description(self) -> None:
        catalog = AgentCatalog(specs={"explorer": _spec()})
        text = render_agent_index(catalog)
        self.assertIn("explorer", text)
        self.assertIn("调研用", text)

    def test_order_follows_catalog(self) -> None:
        catalog = AgentCatalog(
            specs={"a": _spec("a"), "b": _spec("b"), "c": _spec("c")}
        )
        text = render_agent_index(catalog)
        self.assertLess(text.index("**a**"), text.index("**b**"))
        self.assertLess(text.index("**b**"), text.index("**c**"))

    def test_over_budget_keeps_names_drops_descriptions(self) -> None:
        """
        超预算时**保名字只砍描述**（照 C11 作者期扩展的结论）。

        名字是模型发起委派的必需品，描述只影响它选得准不准。
        砍名字等于让那个角色消失，砍描述只是让选择变粗。
        """
        catalog = AgentCatalog(
            specs={
                "explorer": _spec("explorer", "很长的说明" * 50),
                "reviewer": _spec("reviewer", "同样很长的说明" * 50),
            }
        )
        degraded = render_agent_index(catalog, budget=800)

        self.assertIn("explorer", degraded)
        self.assertIn("reviewer", degraded)
        self.assertNotIn("很长的说明", degraded)

    def test_within_budget_keeps_descriptions(self) -> None:
        catalog = AgentCatalog(specs={"explorer": _spec()})
        self.assertIn("调研用", render_agent_index(catalog, budget=100000))


if __name__ == "__main__":
    unittest.main()
