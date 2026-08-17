"""
待办清单显示逻辑的单测（todo-list 扩展 T7，覆盖 AC10/AC12/AC15/AC21 的逻辑半边）。

这一层是**纯函数**，因此三件最容易做错的事在这里可以脱离 Textual 断言：
什么时候不该显示、超过 5 条时留哪 5 条、省略摘要怎么说。

⚠ 本文件里分辨力最强的两条：
- **已完成不占名额**（`test_completed_never_steals_a_slot`）——把优先级表
  写反或干脆不排序时，它会红而别的用例照样绿；
- **同档保持原序**（`StableOrderTest`）——它钉住的是 `sorted` 的稳定性。
  改成「分桶再拼」的实现很容易丢掉这条，症状是同为待办的几条莫名换位置，
  用户看到的是清单在自己抖。
"""

from __future__ import annotations

import unittest

from rhinecode.todo.models import TodoItem, TodoState
from rhinecode.todo.render import (
    DISPLAY_LIMIT,
    build_view,
    render_all_done_text,
    render_todo_brief,
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


class PriorityTest(unittest.TestCase):
    """AC12 后半：进行中 > 待办 > 已完成。"""

    def test_in_progress_comes_first(self) -> None:
        view = build_view(
            [
                item("已完成的", TodoState.COMPLETED),
                item("待办的", TodoState.PENDING),
                item("在做的", TodoState.IN_PROGRESS),
            ]
        )
        assert view is not None
        self.assertEqual([r.title for r in view.rows], ["在做的", "待办的", "已完成的"])

    def test_completed_never_steals_a_slot(self) -> None:
        """
        ⚠ **本文件分辨力最强的一条。**

        5 条已完成 + 2 条待办：那 2 条待办**都必须在**显示出来的 5 条里。
        不排序（或把优先级写反）的话，5 个名额会被已完成的占满，
        屏幕上显示「待办 (5/7)」却一条没做完的都看不见——
        而这正是这块界面存在的全部意义。
        """
        items = [item(f"完成{i}", TodoState.COMPLETED) for i in range(5)] + [
            item("还没做A"),
            item("还没做B"),
        ]
        view = build_view(items)
        assert view is not None
        shown = [r.title for r in view.rows]
        self.assertIn("还没做A", shown)
        self.assertIn("还没做B", shown)
        self.assertEqual(shown[:2], ["还没做A", "还没做B"])

    def test_completed_shows_up_when_there_is_room(self) -> None:
        """反证：名额有富余时已完成的照常显示——优先级不是「隐藏已完成」。"""
        view = build_view([item("完成", TodoState.COMPLETED), item("待办")])
        assert view is not None
        self.assertEqual([r.title for r in view.rows], ["待办", "完成"])


class StableOrderTest(unittest.TestCase):
    """
    ⚠ 同一档内**保持模型给的原序**（靠 `sorted` 的稳定性）。

    丢掉这条的症状是：同为「待办」的几条莫名其妙换位置，
    用户看到的是清单在自己抖，而每一帧单独看都「没错」。
    """

    def test_same_state_keeps_input_order(self) -> None:
        view = build_view([item("先"), item("中"), item("后")])
        assert view is not None
        self.assertEqual([r.title for r in view.rows], ["先", "中", "后"])

    def test_swapping_input_swaps_output(self) -> None:
        """反证：换一下输入顺序，输出必须跟着换——否则「保持原序」只是碰巧。"""
        view = build_view([item("后"), item("先")])
        assert view is not None
        self.assertEqual([r.title for r in view.rows], ["后", "先"])

    def test_stability_holds_across_states(self) -> None:
        """两档混排时，各档内部同样保持原序。"""
        view = build_view(
            [
                item("待办1"),
                item("在做1", TodoState.IN_PROGRESS),
                item("待办2"),
                item("在做2", TodoState.IN_PROGRESS),
            ]
        )
        assert view is not None
        self.assertEqual(
            [r.title for r in view.rows], ["在做1", "在做2", "待办1", "待办2"]
        )


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
        """状态如实反映实际执行情况，且允许多条同时进行中。"""
        self.assertIn("如实", self.brief)
        self.assertIn("in_progress", self.brief)

    def test_says_update_as_you_go(self) -> None:
        """每完成一步就更新，不要攒到最后——攒着等于用户全程看不到进度。"""
        self.assertIn("每完成一步", self.brief)

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
        self.assertIn("不必用它", self.brief)


if __name__ == "__main__":
    unittest.main()
