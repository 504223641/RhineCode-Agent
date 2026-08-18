"""
待办清单显示逻辑的单测（todo-list 扩展 T7，覆盖 AC10/AC12/AC15/AC21 的逻辑半边）。

这一层是**纯函数**，因此三件最容易做错的事在这里可以脱离 Textual 断言：
什么时候不该显示、超过 5 条时留哪 5 条、省略摘要怎么说。

⚠ 本文件里分辨力最强的两条：
- **窗口锚在第一条未完成的条目上**（`WindowTest`）——简单地取前 5 条时它会红，
  而别的用例照样绿。那种实现下，一份做到第 8 条的清单在屏幕上**一条还没做的
  都看不见**，而这块界面存在的全部理由就是回答「还剩什么」；
- **已完成的留在原位**（`ExecutionOrderTest`）——它钉住的是一次**口径反转**：
  初版按状态重排（已完成推到最后），真机反馈后改成一律按执行顺序。
  没有这条的话，后来的人看到「已完成排在待办前面」会以为是 bug 顺手改回去。
"""

from __future__ import annotations

import unittest

from rhinecode.todo.models import TodoItem, TodoState
from rhinecode.todo.render import (
    DISPLAY_LIMIT,
    REMINDER_TITLE_LIMIT,
    build_view,
    render_all_done_text,
    render_todo_brief,
    render_todo_reminder,
)


def item(title: str, state: TodoState = TodoState.PENDING) -> TodoItem:
    return TodoItem(title=title, state=state)


class VisibilityTest(unittest.TestCase):
    """
    AC10 / AC15 的逻辑半边：**什么时候整块不该显示**。

    这是「待办块自动收起」的唯一实现点，界面层只是照着做。
    """

    def test_empty_list_is_not_displayed(self) -> None:
        self.assertIsNone(build_view([]))

    def test_all_completed_is_not_displayed(self) -> None:
        view = build_view(
            [item("a", TodoState.COMPLETED), item("b", TodoState.COMPLETED)]
        )
        self.assertIsNone(view)

    def test_one_unfinished_item_keeps_it_displayed(self) -> None:
        """反证：只要还剩一条没做完，就必须显示——否则进度会提前消失。"""
        view = build_view(
            [item("a", TodoState.COMPLETED), item("b", TodoState.PENDING)]
        )
        self.assertIsNotNone(view)

    def test_in_progress_only_is_displayed(self) -> None:
        self.assertIsNotNone(build_view([item("a", TodoState.IN_PROGRESS)]))


class HeaderTest(unittest.TestCase):
    def test_header_counts_completed_over_total(self) -> None:
        view = build_view(
            [
                item("a", TodoState.COMPLETED),
                item("b", TodoState.COMPLETED),
                item("c", TodoState.IN_PROGRESS),
                item("d"),
            ]
        )
        assert view is not None
        self.assertEqual(view.header, "待办 (2/4)")

    def test_header_counts_the_whole_list_not_just_visible_rows(self) -> None:
        """
        ⚠ 表头的分母是**总条数**，不是显示出来的行数。
        写成后者的话，一份 15 条的清单会显示成「待办 (0/5)」——
        用户以为总共就 5 件事。
        """
        items = [item(f"t{i}") for i in range(15)]
        view = build_view(items)
        assert view is not None
        self.assertEqual(view.header, "待办 (0/15)")


class DisplayLimitTest(unittest.TestCase):
    """AC12：限高与省略摘要。"""

    def test_at_most_five_rows(self) -> None:
        view = build_view([item(f"t{i}") for i in range(15)])
        assert view is not None
        self.assertEqual(len(view.rows), DISPLAY_LIMIT)

    def test_overflow_reports_remaining_and_completed_among_them(self) -> None:
        """
        省略摘要要说两个数字。⚠ 第二个不可省：没有它的话
        「5 条待办 + 10 条已完成」会显示成「还有 10 条」，
        读起来像还有 10 件事要做。
        """
        items = [item(f"待办{i}") for i in range(5)] + [
            item(f"完成{i}", TodoState.COMPLETED) for i in range(10)
        ]
        view = build_view(items)
        assert view is not None
        self.assertEqual(view.overflow, "……还有 10 条，已完成 10 条")

    def test_no_overflow_line_when_everything_fits(self) -> None:
        view = build_view([item("a"), item("b")])
        assert view is not None
        self.assertEqual(view.overflow, "")

    def test_exactly_at_limit_has_no_overflow(self) -> None:
        """边界：正好 5 条时不该出现省略行。"""
        view = build_view([item(f"t{i}") for i in range(DISPLAY_LIMIT)])
        assert view is not None
        self.assertEqual(len(view.rows), DISPLAY_LIMIT)
        self.assertEqual(view.overflow, "")


class ExecutionOrderTest(unittest.TestCase):
    """
    ⚠ **顺序一律是执行顺序，先做的在前**（真机反馈后改，别改回按状态排）。

    初版按 `进行中 → 待办 → 已完成` 重排，把已完成的推到最后。用户看过真机
    之后否掉了：那样清单读起来不像一份计划，而像一个按状态分的堆——
    没法从上往下看出「这件事分几步、走到哪一步了」。

    顺序既然固定，限高就只剩「取哪一段」这一个自由度，见 `WindowTest`。
    """

    def test_order_is_exactly_as_given(self) -> None:
        view = build_view(
            [
                item("先做的", TodoState.COMPLETED),
                item("在做的", TodoState.IN_PROGRESS),
                item("待做的", TodoState.PENDING),
            ]
        )
        assert view is not None
        self.assertEqual([r.title for r in view.rows], ["先做的", "在做的", "待做的"])

    def test_completed_stays_where_it_was(self) -> None:
        """
        **反证**：已完成的**不会**被挪到末尾。

        这条与初版的断言正好相反，是刻意的——它钉住的正是那次口径反转，
        免得后来的人看到「已完成的排在待办前面」以为是 bug 顺手改回去。
        """
        view = build_view(
            [
                item("第一步", TodoState.COMPLETED),
                item("第二步", TodoState.PENDING),
                item("第三步", TodoState.COMPLETED),
            ]
        )
        assert view is not None
        self.assertEqual([r.title for r in view.rows], ["第一步", "第二步", "第三步"])

    def test_same_state_keeps_input_order(self) -> None:
        view = build_view([item("先"), item("中"), item("后")])
        assert view is not None
        self.assertEqual([r.title for r in view.rows], ["先", "中", "后"])

    def test_swapping_input_swaps_output(self) -> None:
        """顺序完全跟着输入走。"""
        view = build_view([item("后"), item("先")])
        assert view is not None
        self.assertEqual([r.title for r in view.rows], ["后", "先"])


class WindowTest(unittest.TestCase):
    """
    限高时取哪一段：**窗口锚在第一条未完成的条目上**。

    ⚠ 这一组是「保持执行顺序」之后唯一还需要判断的地方，两条边界各一个用例。
    """

    def test_window_starts_at_the_first_unfinished_item(self) -> None:
        """
        ⚠ **本文件分辨力最强的一条。**

        15 条做到第 8 条时，简单地取前 5 条会全是已完成的——屏幕上一条
        **还没做的**都看不见，而这块界面存在的全部理由就是回答「还剩什么」。
        """
        items = [
            item(f"第{i}步", TodoState.COMPLETED if i < 8 else TodoState.PENDING)
            for i in range(1, 16)
        ]
        view = build_view(items)
        assert view is not None
        self.assertEqual(view.rows[0].title, "第8步")
        self.assertEqual(len(view.rows), DISPLAY_LIMIT)

    def test_window_is_pulled_back_at_the_tail(self) -> None:
        """
        做到倒数第二条时窗口要往前收，保证是满的——否则末尾只显示一两条、
        上面白留一片。
        """
        items = [
            item(f"第{i}步", TodoState.COMPLETED if i < 14 else TodoState.PENDING)
            for i in range(1, 16)
        ]
        view = build_view(items)
        assert view is not None
        self.assertEqual(len(view.rows), DISPLAY_LIMIT)
        self.assertEqual(view.rows[-1].title, "第15步")

    def test_short_list_shows_everything_from_the_top(self) -> None:
        """条数不超限时整份显示，窗口逻辑不该有任何可见影响。"""
        view = build_view([item("甲", TodoState.COMPLETED), item("乙"), item("丙")])
        assert view is not None
        self.assertEqual([r.title for r in view.rows], ["甲", "乙", "丙"])
        self.assertEqual(view.overflow, "")


class AllDoneTextTest(unittest.TestCase):
    def test_text_contains_the_ratio(self) -> None:
        self.assertIn("4/4", render_all_done_text(4))


class BriefTest(unittest.TestCase):
    """
    AC21：系统提示那段的口径。

    ⚠ 它决定「模型要不要用这个工具」。C15 的教训：协作能力一个字没进
    系统提示，真实模型下 0 次使用——工具描述只在模型已经想到要用它
    之后才起作用。
    """

    def setUp(self) -> None:
        self.brief = render_todo_brief()

    def test_mentions_the_tool_name(self) -> None:
        self.assertIn("todo_write", self.brief)

    def test_lower_bound_is_countable(self) -> None:
        """
        ⚠ 下限必须**可数**（「三步」），不能只写「简单的任务」。

        经验来源是委派触发口径的两次反转：模型对有具体可匹配项的指令
        遵循得好，对抽象判断系统性偷懒。
        """
        self.assertIn("三步", self.brief)

    def test_says_replace_the_whole_list(self) -> None:
        self.assertIn("完整清单", self.brief)

    def test_says_state_must_be_truthful(self) -> None:
        """状态如实反映实际执行情况。"""
        self.assertIn("如实", self.brief)
        self.assertIn("in_progress", self.brief)

    def test_says_update_as_you_go(self) -> None:
        """每完成一步就更新，不要攒到最后——攒着等于用户全程看不到进度。"""
        self.assertIn("每做完一步", self.brief)

    def test_has_no_unbounded_push(self) -> None:
        """
        ⚠ **反证。** 这一段刻意**不含**「拿不准就列」这类无下限措辞。

        那正是委派那边 2026-08-10 整个反转掉的形态：为治欠触发写下的
        四条单向推力导致了过触发（用户实测「一个非常简单的任务都要让
        子 Agent 去做」）。待办的成本远低于委派，所以口径可以更积极，
        **但仍然要有下限**。
        """
        for banned in ("拿不准就", "一律先列", "总是先列", "任何任务都"):
            self.assertNotIn(banned, self.brief)

    def test_says_when_not_to_use_it(self) -> None:
        """必须写「什么时候不必用」——只写该用的一侧就是单向推力。"""
        self.assertIn("不必列", self.brief)


class TodoReminderTest(unittest.TestCase):
    """
    每轮 `<system-reminder>` 待办提醒的四个分支。

    ⚠ **这个函数此前一条测试都没有**（2026-08-18 补）。它是本扩展**唯一
    被真机证明有效**的杠杆——两轮验收里静态提示词三个杠杆加满仍是各 0 次
    调用，是加了这条提醒才有效果的。**唯一有效的东西没有护栏**，
    意味着任何一次「顺手简化一下措辞」都可能把它改回无效形态而无人察觉。
    """

    def test_all_done_says_nothing(self) -> None:
        """全部完成时不提醒——界面上那块已经收起，再催等于催已做完的事。"""
        self.assertEqual(render_todo_reminder(4, True, None), "")
        self.assertEqual(render_todo_reminder(4, True, (1, "改 a.py")), "")

    def test_empty_list_asks_to_create_one(self) -> None:
        """清单为空时提醒的是「记得创建」，并且要重复那条可数的下限。"""
        text = render_todo_reminder(0, False)
        self.assertIn("空的", text)
        self.assertIn("三步", text)

    def test_empty_list_keeps_the_lower_bound(self) -> None:
        """
        ⚠ **反证：提醒里不许放宽下限。**

        提醒的作用是「让它想起来」，不是「让它无条件列」。放宽会把欠触发
        直接推成过触发——委派那边 2026-08-10 就是这么翻的车。
        """
        text = render_todo_reminder(0, False)
        for banned in ("拿不准就", "一律先列", "总是先列", "任何任务都"):
            self.assertNotIn(banned, text)

    def test_in_progress_item_is_named(self) -> None:
        """
        ⚠ **有 in_progress 时必须把序号与标题念出来。**

        泛泛的「做完一步就更新」每轮都读得到，读多了等于没读；念出标题
        之后它变成一个指名道姓的问句，而那种问题很难当没看见。
        """
        text = render_todo_reminder(4, False, (2, "改 b.py 里的裸 except"))
        self.assertIn("第 2 条", text)
        self.assertIn("改 b.py 里的裸 except", text)

    def test_in_progress_reminder_anchors_to_concrete_tools(self) -> None:
        """提醒也要挂在那三个可匹配的动作上，与两处静态文本同口径。"""
        text = render_todo_reminder(4, False, (1, "改 a.py"))
        for tool_name in ("edit_file", "write_file", "run_command"):
            self.assertIn(tool_name, text)
        self.assertIn("之前", text)

    def test_long_title_is_clipped(self) -> None:
        """
        标题**没有长度校验**（`store.py` 只限条数），所以这里必须自己截。

        不截的话，一条被写成几百字的「标题」会每轮随提醒重发一次。
        """
        text = render_todo_reminder(2, False, (1, "标" * 400))
        self.assertIn("…", text)
        self.assertNotIn("标" * (REMINDER_TITLE_LIMIT + 1), text)

    def test_no_in_progress_is_its_own_branch(self) -> None:
        """
        ⚠ **点火分支：还有没做完的、却零条 in_progress。**

        这是「同一时刻只能有一条 in_progress」那条规矩的发动机点火处——
        模型刚做完一条还没汇报时，清单恰好是这个形状。
        """
        text = render_todo_reminder(4, False, None)
        self.assertIn("一条 in_progress 都没有", text)
        self.assertIn("todo_write", text)

    def test_the_ignition_branch_is_not_the_empty_branch(self) -> None:
        """
        ⚠ **反证：点火分支不得与「清单为空」合并。**

        合并看起来省一个分支，实际把两件不同的事说成了一句话：
        「你漏了一次汇报」与「你还没列清单」要模型做的事完全不同。
        合并之后模型在漏汇报时会被要求「先列清单」——它已经有清单了。
        """
        ignition = render_todo_reminder(4, False, None)
        empty = render_todo_reminder(0, False, None)
        self.assertNotEqual(ignition, empty)
        self.assertNotIn("空的", ignition)
        self.assertNotIn("一条 in_progress 都没有", empty)

    def test_every_branch_tells_the_model_to_keep_quiet(self) -> None:
        """
        四个分支里凡是出声的，都必须写「别跟用户提这条提醒」。

        少了它，模型会把提醒当成用户说的话，在正文里回一句
        「好的，我会维护待办清单」——那是纯噪音。
        """
        for text in (
            render_todo_reminder(0, False),
            render_todo_reminder(4, False, None),
            render_todo_reminder(4, False, (1, "改 a.py")),
        ):
            self.assertIn("不要在回复里向用户提起它", text)


if __name__ == "__main__":
    unittest.main()
