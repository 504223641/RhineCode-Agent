"""
待办写入工具的单测（todo-list 扩展 T11）。

⚠ 本文件里最要紧的两条**都不是在测功能，是在测约定**：

- `PlanStageContractTest` —— `plan_safe=True` 的工具**必须**能接住循环传进来的
  `plan_stage`。漏了不会在这里报错，而是等到有人在 Plan Mode 的规划阶段
  真的调它时抛 `TypeError`，那时它已经被包装成一条「工具执行异常」了。
- `SameVoiceTest` —— 工具描述与系统提示那段必须同口径。这是同一个坑的
  **第五次**（C11 Skill 清单、C13 角色清单、C14 交付信息、C15 消息标记块），
  前四次全都是真实模型实测才发现的。
"""

from __future__ import annotations

import inspect
import unittest

from rhinecode.todo.models import TodoState
from rhinecode.todo.render import render_todo_brief
from rhinecode.todo.store import MAX_ITEMS, TodoStore
from rhinecode.tools.todo_write import TodoWriteTool


def make_tool() -> tuple[TodoWriteTool, TodoStore]:
    store = TodoStore()
    return TodoWriteTool(store), store


class DeclarationTest(unittest.TestCase):
    """类属性的取值逐个钉住——它们决定这个工具在管线里怎么被对待。"""

    def setUp(self) -> None:
        self.tool, _ = make_tool()

    def test_name(self) -> None:
        self.assertEqual(self.tool.name, "todo_write")

    def test_not_read_only(self) -> None:
        self.assertFalse(self.tool.read_only)

    def test_system_serial(self) -> None:
        """
        不弹确认面板（判 ASK 按 ALLOW），且对第④层免疫。
        ⚠ 它**不**意味着不进管线——`deny: todo_write` 仍然拦得住，
        那一条由 `test_todo_integration.py` 钉住。
        """
        self.assertTrue(self.tool.system_serial)

    def test_plan_safe(self) -> None:
        self.assertTrue(self.tool.plan_safe)

    def test_not_workspace_aware(self) -> None:
        """刻意不声明：本工具不碰任何路径。"""
        self.assertFalse(self.tool.workspace_aware)

    def test_no_classifier_scope(self) -> None:
        """
        ⚠ 刻意**不**进分类器：C16 只审三类动作（跑命令 / 访问网络 /
        给队友发消息），本工具一类都不落。硬塞进去会让每次更新待办
        都多一次模型调用，纯粹是成本。

        这条用例同时是那个「刻意」的书面记录，免得后来的人当成漏做补上。
        """
        self.assertEqual(self.tool.classifier_scope, "")


class PlanStageContractTest(unittest.TestCase):
    """
    ⚠ `plan_safe=True` 的硬约定：`execute` 必须接受 `plan_stage`。

    漏了不会在单测里自然暴露——只有在 Plan Mode 规划阶段真的调它时才抛
    `TypeError`，而那时它已被包装成「工具执行异常」回灌给模型了。
    """

    def test_execute_accepts_plan_stage_keyword(self) -> None:
        tool, _ = make_tool()
        params = inspect.signature(tool.execute).parameters
        self.assertIn("plan_stage", params)

    def test_calling_with_plan_stage_works(self) -> None:
        tool, store = make_tool()
        result = tool.execute({"todos": [{"title": "a"}]}, plan_stage=True)
        self.assertTrue(result.ok, result.output)
        self.assertEqual(len(store.snapshot()), 1)

    def test_plan_stage_does_not_change_behaviour(self) -> None:
        """
        两个阶段行为**完全相同**——本工具不产生任何外部副作用，
        因此那个参数只是接住、不据它分支。
        """
        tool_a, store_a = make_tool()
        tool_b, store_b = make_tool()
        payload = {"todos": [{"title": "a", "state": "in_progress"}]}
        tool_a.execute(payload, plan_stage=False)
        tool_b.execute(payload, plan_stage=True)
        self.assertEqual(store_a.snapshot(), store_b.snapshot())


class SchemaTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tool, _ = make_tool()

    def test_state_enum_matches_the_internal_states_exactly(self) -> None:
        """
        ⚠ schema 里的三个取值必须与 `TodoState` **逐字相等**。

        不等的话模型会照着 schema 写出一个工具认不出的状态——
        而它做得完全正确，错的是我们给它的说明书。
        """
        declared = set(
            self.tool.parameters["properties"]["todos"]["items"]["properties"][
                "state"
            ]["enum"]
        )
        self.assertEqual(declared, {s.value for s in TodoState})

    def test_todos_is_required(self) -> None:
        self.assertIn("todos", self.tool.parameters["required"])

    def test_title_is_required_per_item(self) -> None:
        item_schema = self.tool.parameters["properties"]["todos"]["items"]
        self.assertEqual(item_schema["required"], ["title"])

    def test_todos_description_says_it_is_a_full_replacement(self) -> None:
        """参数说明里也要写清覆写语义——模型未必读完整段工具描述。"""
        desc = self.tool.parameters["properties"]["todos"]["description"]
        self.assertIn("完整", desc)


class ExecuteTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tool, self.store = make_tool()

    def test_success_returns_the_rendered_list(self) -> None:
        result = self.tool.execute(
            {
                "todos": [
                    {"title": "读现有实现", "state": "completed"},
                    {"title": "改 login 接口", "state": "in_progress"},
                    {"title": "跑测试"},
                ]
            }
        )
        self.assertTrue(result.ok)
        for expected in ("读现有实现", "已完成", "改 login 接口", "进行中", "跑测试", "待办"):
            self.assertIn(expected, result.output)

    def test_success_summary_reports_scale(self) -> None:
        self.tool.execute(
            {"todos": [{"title": "a", "state": "completed"}, {"title": "b"}]}
        )
        result = self.tool.execute(
            {"todos": [{"title": "a", "state": "completed"}, {"title": "b"}]}
        )
        self.assertIn("2", result.summary)
        self.assertIn("1", result.summary)

    def test_empty_list_reports_cleared(self) -> None:
        self.tool.execute({"todos": [{"title": "a"}]})
        result = self.tool.execute({"todos": []})
        self.assertTrue(result.ok)
        self.assertIn("清空", result.summary)

    def test_failure_passes_the_readable_reason_to_the_model(self) -> None:
        result = self.tool.execute({"todos": [{"title": "ok"}, {"title": "  "}]})
        self.assertFalse(result.ok)
        self.assertIn("第 2 条", result.output)

    def test_failure_has_its_own_summary(self) -> None:
        """
        ⚠ 失败时 `summary` 必须单独给。不给的话 TUI 会回退到取 `output`
        首行，而 `output` 是一段写给模型的完整解释——整段说教会糊在工具行上。
        （tui-activity-fold 验收第 22 条踩过同一个坑。）
        """
        result = self.tool.execute({"todos": "坏的"})
        self.assertFalse(result.ok)
        self.assertTrue(result.summary)
        self.assertNotEqual(result.summary, result.output)
        self.assertLess(len(result.summary), 20)

    def test_failure_leaves_the_store_untouched(self) -> None:
        self.tool.execute({"todos": [{"title": "原有"}]})
        before = self.store.snapshot()
        self.tool.execute({"todos": [{"title": "新的"}, {"title": ""}]})
        self.assertEqual(self.store.snapshot(), before)

    def test_missing_todos_key_is_a_readable_failure_not_a_crash(self) -> None:
        """模型漏传参数是常见形态，必须给可读原因而不是异常。"""
        result = self.tool.execute({})
        self.assertFalse(result.ok)
        self.assertIn("todos", result.output)

    def test_broken_store_is_reported_not_raised(self) -> None:
        """
        `Tool` 契约：`execute` 不得向上抛异常，否则 Agent Loop 会把它变成
        一条「工具执行异常」，丢掉这里组织好的可读原因。
        """

        class Exploding:
            def replace(self, raw):
                raise RuntimeError("炸了")

        tool = TodoWriteTool(Exploding())  # type: ignore[arg-type]
        result = tool.execute({"todos": []})
        self.assertFalse(result.ok)
        self.assertIn("炸了", result.output)


class SameVoiceTest(unittest.TestCase):
    """
    ⚠ **成对维护点的护栏**：工具描述 ↔ 系统提示那段，必须同口径。

    模型在**两个不同时刻**读到同一条约定：决定要不要列待办时读系统提示，
    真正调用时读工具描述——一处强一处弱等于白改。

    这是同一个坑的**第五次**：
    C11「Skill 清单表头 ↔ `load_skill.description`」、
    C13「角色清单 ↔ `run_agent.description`」、
    C14「交付信息 ↔ 委派工具描述」、
    C15「消息标记块 ↔ `send_message.description`」。
    **前四次全都是真实模型实测才发现的。**
    """

    # 两处必须共同承载的四层意思。
    #
    # ⚠ **这张表本身是护栏的一部分**：下面 `test_the_guard_covers_four_layers`
    # 钉住它的长度。没有那一条的话，「把某一层从表里删掉」会让护栏静默变弱
    # ——测试照样全绿，而两处文本从此可以自由分叉。
    _LAYERS = ("三步", "完整清单", "如实", "每完成一步")

    def setUp(self) -> None:
        tool, _ = make_tool()
        self.description = tool.description
        self.brief = render_todo_brief()

    def test_every_layer_appears_in_both_texts(self) -> None:
        """四层意思逐个在两处都要出现——这是同口径的完整定义。"""
        for layer in self._LAYERS:
            with self.subTest(layer=layer):
                self.assertIn(layer, self.description, "工具描述缺了这一层")
                self.assertIn(layer, self.brief, "系统提示那段缺了这一层")

    def test_the_guard_covers_four_layers(self) -> None:
        """
        ⚠ **护栏自身的护栏。**

        上面那条遍历 `_LAYERS`，因此**把某一层从表里删掉**会让它静默变弱：
        测试照样全绿，而两处文本从此可以在那一层上自由分叉——
        这正是「同一个坑的第五次」里，前四次共同的失效方式
        （护栏在，但它管的东西被悄悄缩小了）。

        钉住数量之后，缩表就必须在这里改，改的时候人会读到这段说明。
        """
        self.assertEqual(len(self._LAYERS), 4)
        self.assertEqual(len(set(self._LAYERS)), 4, "四层意思不能有重复")

    def test_both_say_three_steps_or_more(self) -> None:
        """第一层：**下限可数**。「三步」在两处都要出现。"""
        self.assertIn("三步", self.description)
        self.assertIn("三步", self.brief)

    def test_both_say_pass_the_whole_list(self) -> None:
        """第二层：每次传完整清单，不是增量。"""
        self.assertIn("完整清单", self.description)
        self.assertIn("完整清单", self.brief)

    def test_both_say_state_must_be_truthful(self) -> None:
        """第三层：状态如实反映实际执行情况，允许多条同时进行中。"""
        for text in (self.description, self.brief):
            self.assertIn("如实", text)
            self.assertIn("in_progress", text)

    def test_both_say_update_as_you_go(self) -> None:
        """第四层：每完成一步就更新，不要攒到最后。"""
        for text in (self.description, self.brief):
            self.assertIn("每完成一步", text)

    def test_both_say_when_not_to_use_it(self) -> None:
        """
        两处都必须写「什么时候不必用」。

        ⚠ 只写该用的一侧就是**单向推力**，而那正是委派那边
        2026-08-10 整个反转掉的形态（用户实测「一个非常简单的任务
        都要让子 Agent 去做」）。
        """
        self.assertIn("不用", self.description)
        self.assertIn("不必用", self.brief)

    def test_the_two_texts_are_not_the_same_string(self) -> None:
        """
        ⚠ **反向约束：同口径不等于同一份文本，两处刻意各写各的。**

        合成一份共享常量看起来更「不会漏改」，实际会毁掉这条护栏的用途：
        两处的**受众时刻不同**（决定要不要用 / 正在用），因此篇幅、语气、
        举例都该不一样——系统提示那段要能被扫读，工具描述要能被当说明书查。

        真正要一致的是**那四层意思**，不是字面。这条钉住「别偷懒合并」。
        """
        self.assertNotEqual(self.description, self.brief)
        # 工具描述里不该出现系统提示那段的 Markdown 标题
        self.assertNotIn("## 待办清单", self.description)


class DescriptionMentionsTheLimitTest(unittest.TestCase):
    def test_description_states_the_cap_and_that_it_rejects(self) -> None:
        """
        ⚠ 描述里要写清上限**以及「超出会被拒绝而不是截断」**。

        只写数字不写行为的话，模型撞上限时会以为自己写进去了一部分——
        而那正是选择「拒绝」而非「截断」要避免的认知错位。
        """
        tool, _ = make_tool()
        self.assertIn(str(MAX_ITEMS), tool.description)
        self.assertIn("拒绝", tool.description)
        self.assertIn("截断", tool.description)


if __name__ == "__main__":
    unittest.main()
