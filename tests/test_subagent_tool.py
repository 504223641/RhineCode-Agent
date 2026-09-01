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
        # c14：记录本次调用的 isolation 参数。`None` = 未表态，与显式 False
        # 语义不同（单向加严里两者都不能撤销角色的声明，但要能区分）。
        self.isolations: list = []
        # c15：记录本次调用的 name 参数。`None` = 让系统自动起名。
        self.names: list = []
        self._outcome = outcome or DelegateOutcome(ok=True, text="结论", task_id="a3f1c9")
        self._raises = raises

    def delegate(
        self, kind, agent_name, task_text, background=False, parent=None,
        plan_stage=False, isolation=None, name=None,
    ):
        self.calls.append((kind, agent_name, task_text, background, parent))
        self.plan_stages.append(plan_stage)
        self.isolations.append(isolation)
        self.names.append(name)
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


class NameForwardingTest(unittest.TestCase):
    """
    c15：队员名字必须如实转发给服务层。

    名字是消息投递与任务认领的唯一标识，转丢了的表现是「模型明明起了名字，
    发消息时却说查无此人」。
    """

    def test_absent_name_is_none_not_empty_string(self) -> None:
        """
        ⚠ `None`（让系统起名）与 `""`（模型给了个空名字）语义不同，
        在花名册的 `register` 里走不同分支。用 `args.get` 取原值即可区分。
        """
        service = _FakeService()
        RunAgentTool(service).execute({"type": "role", "agent": "e", "task": "t"})
        self.assertEqual(service.names, [None])

    def test_name_is_forwarded(self) -> None:
        service = _FakeService()
        RunAgentTool(service).execute(
            {"type": "role", "agent": "e", "task": "t", "name": "reviewer"}
        )
        self.assertEqual(service.names, ["reviewer"])

    def test_name_is_declared_in_schema(self) -> None:
        self.assertIn("name", RunAgentTool.parameters["properties"])


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

    这两处是模型决定「要不要委派」时读到的唯一两处文本。一处写得强、
    另一处写得弱，模型会按弱的那份行事。

    ## ⚠ 2026-08-10：这一组断言整个反转过，别照着旧版本推理

    原先钉的是**四条推力**（命中就委派 / 用户不必点名 / 拿不准就委派 /
    不要先看一眼再决定），为的是对抗 C13/C15 验收观测到的**欠触发**。

    **那四条把模型推到了另一个极端**：用户实测「一个非常简单的任务都要让子
    Agent 去做」。⚠ 关键事实是**欠触发与过触发出自同一个模型**
    （`deepseek-v4-flash`），所以问题不在模型强弱，在于那四条**单向**——
    只写了该委派的理由，还点名禁掉了模型自己会用的两个刹车。

    现在钉的是对齐 Claude Code `Agent` 工具的四层意思。措辞可以打磨，
    这四层不能丢。完整论证见 `subagents/render.py` 的模块 docstring。
    """

    def setUp(self) -> None:
        self.header = "\n".join(_INDEX_HEADER)
        self.desc = RunAgentTool.description

    def test_both_default_to_not_delegating(self) -> None:
        """
        ①**默认不委派**——最关键的一条，它决定模型在灰色地带倒向哪边。

        对应 Claude Code 的「Do not spawn agents unless the user asks」。
        """
        for text, label in ((self.header, "清单表头"), (self.desc, "工具描述")):
            with self.subTest(where=label):
                self.assertIn("默认不要委派", text)

    def test_both_name_the_user_asking_as_the_main_trigger(self) -> None:
        """
        ②**用户开口是主要触发路径**。

        与旧版的「用户不必明确说」正好相反，这是本次反转最直接的一处。
        """
        for text, label in ((self.header, "清单表头"), (self.desc, "工具描述")):
            with self.subTest(where=label):
                self.assertIn("用户开口", text)

    def test_both_explain_the_cold_start_cost(self) -> None:
        """
        ③ 说明**冷启动成本**——这是模型算得清的那本账。

        旧版把委派算成「多一次调用」（近乎免费），模型据此永远选委派。
        对应 Claude Code 的「starts cold and re-derives context you already
        have — it's the expensive path」。
        """
        for text, label in ((self.header, "清单表头"), (self.desc, "工具描述")):
            with self.subTest(where=label):
                self.assertIn("冷启动", text)

    def test_both_reject_multi_part_tasks_as_a_signal(self) -> None:
        """
        ④「活分成好几部分 / 用户说了彻底、全面」**不构成**委派信号。

        对应 Claude Code 的「A task with "multiple angles," "thorough," or
        several parts is not a request to spawn; handle it inline」。
        这一条专治本次用户反馈的症状：简单任务被字面匹配成「调研类」派出去。
        """
        for text, label in ((self.header, "清单表头"), (self.desc, "工具描述")):
            with self.subTest(where=label):
                self.assertIn("不构成委派信号", text)
                self.assertIn("彻底", text)

    def test_both_give_a_countable_floor(self) -> None:
        """
        **可数的下限**：一两次工具调用能做完的活自己做。

        `docs/todo/README.md` 的「已取消」一节留下的线索：模型对**有具体可匹配项**
        的指令遵循得好，对抽象判断（「任务之间相不相干」）系统性偷懒。
        所以刹车必须给成可数的，不能只说「简单的活自己做」。
        """
        for text, label in ((self.header, "清单表头"), (self.desc, "工具描述")):
            with self.subTest(where=label):
                self.assertIn("一两次工具调用", text)

    def test_neither_pushes_delegation_anymore(self) -> None:
        """
        **反证**：两处都不得再出现旧版的推力措辞。

        ⚠ 这条反证有具体的现实针对性：`skills/render.py` 的清单表头
        **刻意保持 pushy**（「用户不必明确说」原样留着），措辞几乎一样、
        文件就在隔壁。下一个人极容易「统一口径」把它抄回来。

        两者的成本结构相反：加载 Skill 只是往上下文加一段文本（便宜、可逆），
        委派要起一整条子对话并冷启动（贵）。Claude Code 自己也是这么分的。
        """
        for text, label in ((self.header, "清单表头"), (self.desc, "工具描述")):
            for banned in ("倾向委派", "用户不必明确说", "而不是自己动手做"):
                with self.subTest(where=label, phrase=banned):
                    self.assertNotIn(banned, text)

    def test_neither_anchors_on_a_file_count(self) -> None:
        """
        **反证**：两处都不得出现具体的文件数量锚点。

        这条从旧版**原样保留**，理由在反转后依然成立：给一个具体数字，
        模型就会拿实际数量去比对，从而给自己找到一个与真实成本无关的借口。
        成本要按**机制**说（冷启动、重新推导背景）。
        """
        for text, label in ((self.header, "清单表头"), (self.desc, "工具描述")):
            with self.subTest(where=label):
                self.assertNotIn("二十个文件", text)


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
