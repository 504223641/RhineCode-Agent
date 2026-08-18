"""
待办写入工具的单测（todo-list 扩展 T11）。

⚠ 本文件里最要紧的两条**都不是在测功能，是在测约定**：

- `PlanStageContractTest` —— `plan_safe=True` 的工具**必须**能接住循环传进来的
  `plan_stage`。漏了不会在这里报错，而是等到有人在 Plan Mode 的规划阶段
  真的调它时抛 `TypeError`，那时它已经被包装成一条「工具执行异常」了。
- `SameVoiceTest` —— 工具描述与系统提示那段必须同口径。这是同一个坑的
  **第五次**（C11 Skill 清单、C13 角色清单、C14 交付信息、C15 消息标记块），
  前四次全都是真实模型实测才发现的。
- `ManyShotExamplesTest` —— 描述里那八个正反示例**是本工具唯一有效的触发手段**，
  不是可有可无的装饰。真机验收下静态规则三个杠杆全部失效（各 0 次调用），
  对齐 Claude Code 补上示例才是第四个杠杆。护栏钉住正反两侧的数量，
  因为「顺手删几个例子省 token」在测试上完全看不出来。
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
    #
    # ⚠ **第五层「时点锚点」是真机复测逼出来的（2026-08-18）。** 补齐八个示例
    # 之后 flash 4/4 通过，而 pro 的 B1 仍然 0 次——它三条触发条件全中
    # （改 3 个文件 / 4 步 / 6 处重复应用），正文里连规划都没提，直接开改。
    # 加的不是推力（那会把 flash 推成过触发），是一个**可匹配的时点**：
    # 「第一次调用 edit_file / write_file / run_command 之前」。
    # 「开始动手之前」是抽象判断，模型对它系统性偷懒；三个工具名是可匹配项。
    #
    # ⚠ **第六、七层是 2026-08-18 为治「中途不更新、最后一次性全标完」加的，
    # 它们互相咬合、不许单独删。** 原文写着「允许多条同时是 in_progress」，
    # 那句话被模型当成免责条款用：开工时把几条全标 in_progress 之后，
    # 清单**从此永远合法**，再没有任何东西迫使它开口。
    # 「同一时刻只有一条」是台发动机（每做完一件事，清单就变得不合规，
    # 而修好它的唯一办法就是再调一次工具）；「不许跳步」是它的锁
    # （少了它，剩下的 pending 仍可被一次性直接标成 completed，绕开发动机）。
    _LAYERS = (
        "三步",
        "完整清单",
        "如实",
        "每做完一步",
        "第一次调用",
        "同一时刻只能有一条 in_progress",
        "不许从 pending 直接跳到 completed",
    )

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

    def test_the_guard_covers_seven_layers(self) -> None:
        """
        ⚠ **护栏自身的护栏。**

        上面那条遍历 `_LAYERS`，因此**把某一层从表里删掉**会让它静默变弱：
        测试照样全绿，而两处文本从此可以在那一层上自由分叉——
        这正是「同一个坑的第五次」里，前四次共同的失效方式
        （护栏在，但它管的东西被悄悄缩小了）。

        钉住数量之后，缩表就必须在这里改，改的时候人会读到这段说明。

        ⚠ **从四层涨到五层是 2026-08-18 真机复测的结果**，理由写在
        `_LAYERS` 上方。涨表要在这里改是**刻意的**——它逼着加层的人
        顺手确认「新加的这层两处都写了」。

        ⚠ **同日又从五层涨到七层**（进度纪律那两条），理由同样写在
        `_LAYERS` 上方。那两条**互相咬合**，删掉任一条另一条即失效。
        """
        self.assertEqual(len(self._LAYERS), 7)
        self.assertEqual(len(set(self._LAYERS)), 7, "七层意思不能有重复")

    def test_both_say_three_steps_or_more(self) -> None:
        """第一层：**下限可数**。「三步」在两处都要出现。"""
        self.assertIn("三步", self.description)
        self.assertIn("三步", self.brief)

    def test_both_say_pass_the_whole_list(self) -> None:
        """第二层：每次传完整清单，不是增量。"""
        self.assertIn("完整清单", self.description)
        self.assertIn("完整清单", self.brief)

    def test_both_say_state_must_be_truthful(self) -> None:
        """第三层：状态如实反映实际执行情况。"""
        for text in (self.description, self.brief):
            self.assertIn("如实", text)
            self.assertIn("in_progress", text)

    def test_both_say_update_as_you_go(self) -> None:
        """第四层：每完成一步就更新，不要攒到最后。"""
        for text in (self.description, self.brief):
            self.assertIn("每做完一步", text)

    def test_both_say_exactly_one_in_progress(self) -> None:
        """
        第六层：同一时刻只能有一条 in_progress。

        ⚠ **这条是「中途不更新」那个病的主药**，理由见 `_LAYERS` 上方。
        """
        for text in (self.description, self.brief):
            self.assertIn("同一时刻只能有一条 in_progress", text)

    def test_neither_permits_multiple_in_progress(self) -> None:
        """
        ⚠ **反证：旧口径不得残留。**

        原文那句「允许多条同时是 in_progress」在**字面上仍然像句好话**
        （它讲的是「别说假话」），因此最可能的退化方式不是删掉新规矩，
        而是「顺手把旧那句加回来当补充说明」——两句并存时模型听哪句
        没有定论，而界面上完全看不出来。

        没有这一条的话，上面那条正向断言在两句并存时**照样通过**。
        """
        for text in (self.description, self.brief):
            self.assertNotIn("允许多条", text)
            self.assertNotIn("多条同时是 in_progress", text)

    def test_both_forbid_jumping_from_pending_to_completed(self) -> None:
        """
        第七层：不许从 pending 直接跳到 completed。

        ⚠ **它是第六层的锁。** 少了它，模型仍可把剩下的 pending 一次性
        直接标成 completed——那正是要治的症状本身，而第六层管不到
        （那一刻清单上确实只有零条 in_progress，形式上合规）。
        """
        for text in (self.description, self.brief):
            self.assertIn("不许从 pending 直接跳到 completed", text)

    def test_both_anchor_the_update_moment_to_concrete_tools(self) -> None:
        """
        更新的时点必须挂在**可匹配的动作**上，不是「做完一步」这种抽象判断。

        ⚠ 判据取「三个工具名同时出现在更新那句话附近」不现实（措辞会变），
        故退一步钉两件事：两处都点名了那三个工具，且都说了「之前」。
        依据与第五层同源——模型对有具体可匹配项的指令遵循得好，
        对抽象判断系统性偷懒。
        """
        for text in (self.description, self.brief):
            for tool_name in ("edit_file", "write_file", "run_command"):
                self.assertIn(tool_name, text)
            self.assertIn("之前", text)

    def test_both_tell_how_to_mark_parallel_work(self) -> None:
        """
        ⚠ **「只能一条」必须配一个出口，否则模型遇到并行会自己发明标法。**

        两种真实情形各要有做法：多个子 Agent 同时跑（清单上算一条）、
        某条卡住了要先做后面的（重排清单，不是多标一条 in_progress）。

        没有出口的规矩会被绕过而不是被遵守——这与「负例是边界唯一的
        形状来源」是同一条经验。
        """
        for text in (self.description, self.brief):
            self.assertIn("子 Agent", text)
            self.assertIn("重排清单", text)

    def test_both_say_when_not_to_use_it(self) -> None:
        """
        两处都必须写「什么时候不必用」。

        ⚠ 只写该用的一侧就是**单向推力**，而那正是委派那边
        2026-08-10 整个反转掉的形态（用户实测「一个非常简单的任务
        都要让子 Agent 去做」）。
        """
        self.assertIn("不用", self.description)
        self.assertIn("不必列", self.brief)

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


class ManyShotExamplesTest(unittest.TestCase):
    """
    ⚠ **描述里的八个示例是本工具唯一有效的触发手段，护栏钉住它们还在。**

    背景（`docs/extensions/todo-list/acceptance-live.md`）：静态规则那条路
    走到头了——能力描述 → 带可匹配条件的指令 → 每轮 `<system-reminder>`，
    三个杠杆逐个加上，两个模型在一个明确五步的任务上仍然**各 0 次**调用；
    而明确命令它用，一次就用对。差距查到是 Claude Code 那份描述有
    **八个带 reasoning 的正反示例**，占了三分之二篇幅，我们此前**全是规则**。

    因此「顺手删几个例子省 token」是这段文本最可能的退化方式，
    而它在其它任何测试里都看不出来（功能全对、同口径全对）。

    ⚠ **负例数量单独钉一条**：只留正例就是单向推力，而那正是 C13 委派触发
    口径 2026-08-10 整个反转掉的形态（用户实测「一个非常简单的任务都要让
    子 Agent 去做」）。负例是「不该列」那条边界唯一的形状来源。
    """

    # 三段的小标题。⚠ 判定靠它们切段，改标题要同步改这里。
    _POSITIVE_HEADING = "## 该用的例子"
    _NEGATIVE_HEADING = "## 不该用的例子"
    # ⚠ 第三段（2026-08-18 加）：它的例子回答「已经决定要列了，遇到并行怎么标」，
    # **与触发无关**。不单独切出来的话，它们会被算进负例段，
    # 于是「负例恰好 4 个」那条钉子失效——而负例数量正是本类最要紧的一条。
    _PROGRESS_HEADING = "## 进度怎么推"

    def setUp(self) -> None:
        tool, _ = make_tool()
        self.description = tool.description

    def _split(self) -> tuple[str, str]:
        """
        把描述切成「正例段」与「负例段」两半。

        ⚠ 负例段**到进度段为止**——进度段里的例子不是负例。
        """
        self.assertIn(self._POSITIVE_HEADING, self.description)
        self.assertIn(self._NEGATIVE_HEADING, self.description)
        self.assertIn(self._PROGRESS_HEADING, self.description)
        head, _, tail = self.description.partition(self._NEGATIVE_HEADING)
        _, _, positive = head.partition(self._POSITIVE_HEADING)
        negative, _, _ = tail.partition(self._PROGRESS_HEADING)
        return positive, negative

    def _progress_section(self) -> str:
        """「进度怎么推」那一段的正文。"""
        _, _, section = self.description.partition(self._PROGRESS_HEADING)
        return section

    def test_four_positive_and_four_negative_examples(self) -> None:
        """
        四正四负，逐侧钉数量。

        对齐 Claude Code 的 TodoWrite（4 个 `Examples of When to Use` +
        4 个 `Examples of When NOT to Use`）。
        """
        positive, negative = self._split()
        self.assertEqual(positive.count("<例子>"), 4, "正例不是 4 个")
        self.assertEqual(negative.count("<例子>"), 4, "负例不是 4 个")

    def test_every_example_carries_its_reasoning(self) -> None:
        """
        ⚠ **每个例子都必须带一段「为什么」**——这一条比数量更要紧。

        光给「用户说 X，你就调工具」是让模型背答案，只有说清判据
        （命中了哪一条、为什么这算三步）才迁移得到没见过的场景上。
        Claude Code 那八个例子**每一个**都有 `<reasoning>` 块。

        ⚠ 总数 **10 = 触发 8 + 进度 2**。分开数是刻意的：只钉总数的话，
        「删掉一个负例、补一个进度例」会静默通过，而那恰好是最坏的退化
        （单向推力回来了，测试还是绿的）。
        """
        positive, negative = self._split()
        self.assertEqual(positive.count("<例子>") + negative.count("<例子>"), 8)
        self.assertEqual(self._progress_section().count("<例子>"), 2)

        self.assertEqual(self.description.count("<例子>"), 10)
        self.assertEqual(self.description.count("</例子>"), 10, "有例子没闭合")
        self.assertEqual(
            self.description.count("<为什么>"), 10, "有例子没写判据"
        )
        self.assertEqual(self.description.count("</为什么>"), 10)

    def test_a_two_step_case_is_shown_as_not_listing(self) -> None:
        """
        ⚠ **临界负例不能少：恰好两步的活要示范成「不列」。**

        「一步」和「纯问答」那两个负例太好判了，模型本来就不会误触发
        （B2/B3/B4 三条真机全过正说明这一点）。真正需要示范的是**贴着
        下限的那一档**——有两件事、听起来像多步、但只有两步。

        没有它的话「三步」这个下限只是一句规则，模型仍会凭「有好几件事」
        的感觉往上凑，而那是过触发的起点。
        """
        _positive, negative = self._split()
        self.assertIn("两步", negative)
        self.assertIn("三步", negative)

    def test_has_no_unbounded_push(self) -> None:
        """
        ⚠ **反证，与 `test_todo_render.BriefTest` 那条成对。**

        此前只有系统提示那一侧钉了这条，工具描述这一侧是空的——**成对维护点
        少了一半护栏**，描述可以单方面滑向无下限推力而没人发现。
        补示例这一轮顺手补齐（我自己就差点写下「拿不准就数步数」）。

        禁的是这类**无下限**开头。委派那边 2026-08-10 整个反转掉的正是它：
        为治欠触发写下四条单向推力，结果是「一个非常简单的任务都要让
        子 Agent 去做」。待办成本远低于委派、口径可以更积极，
        **但下限必须在**。
        """
        for banned in ("拿不准就", "一律先列", "总是先列", "任何任务都"):
            with self.subTest(banned=banned):
                self.assertNotIn(banned, self.description)

    def test_the_examples_do_not_use_square_brackets(self) -> None:
        """
        ⚠ 示例里不许出现字面 `[`。

        这段文本会随工具描述流到界面上的若干处（`/help` 类报告、
        确认面板的参数摘要）。Textual 的 Content markup 比 Rich 严格，
        **落单的 `[` 在布局阶段主线程抛 `MarkupError`，没有任何 try/except
        兜得住、整个 app 直接退出**（见 CLAUDE.md 的 markup 转义那条）。

        描述是我们自己写死的常量，最省事的办法就是压根不写它。
        """
        self.assertNotIn("[", self.description)


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
