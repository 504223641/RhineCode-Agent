"""
澄清提问面板的渲染与交互（ask-user 扩展 F13–F16，spec AC12–AC14）。

面板是本扩展里**用户唯一直接看到的东西**，因此这组护栏盯的是「屏幕上到底
画了什么」而不是「内部状态对不对」——判据一律从 `OptionList` 的选项文本上读。

⚠ 其中 `CheckboxSurvivesNavigationTest` 是本文件最要紧的一条：
它钉的那个 bug **只在「勾选之后再移动光标」时出现**，一次不移动光标的
手测完全看不到（详见 `ClarifyPanel.toggle_check` 的说明）。
"""

from __future__ import annotations

import unittest

from rhinecode.agent.events import ClarifyOption, ClarifyQuestion
from rhinecode.tui.widgets import ClarifyPanel


def _question(*, header="", multi=False, labels=("甲", "乙"), descriptions=None):
    descriptions = descriptions or ("",) * len(labels)
    return ClarifyQuestion(
        question="放哪一层？",
        options=tuple(
            ClarifyOption(label=a, description=b) for a, b in zip(labels, descriptions)
        ),
        header=header,
        multi_select=multi,
    )


def _panel(question=None, index=0, total=1) -> ClarifyPanel:
    panel = ClarifyPanel()
    panel.show_question(question or _question(), index, total)
    return panel


def _prompts(panel) -> list[str]:
    """面板上每一行的原始文本（含 markup），按显示顺序。"""
    return [str(panel.get_option_at_index(i).prompt) for i in range(panel.option_count)]


def _selectable_ids(panel) -> list:
    """可选项的 id，按显示顺序（跳过 disabled 的表头 / 说明 / 提示行）。"""
    out = []
    for i in range(panel.option_count):
        option = panel.get_option_at_index(i)
        if not getattr(option, "disabled", False):
            out.append(option.id)
    return out


class HeaderTest(unittest.TestCase):
    """F13：徽章与进度都是「没有就不出现」，不是「显示空的」。"""

    def test_badge_is_shown_when_given(self) -> None:
        head = _prompts(_panel(_question(header="配置位置")))[0]
        self.assertIn("配置位置", head)

    def test_no_empty_brackets_without_a_badge(self) -> None:
        """
        ⚠ 反证：没徽章时不许留下一对空方括号。

        每个没写 header 的问题上面挂一枚空徽章，比不做还难看。
        """
        head = _prompts(_panel(_question(header="")))[0]
        self.assertNotIn("\\[]", head)

    def test_progress_hidden_for_a_single_question(self) -> None:
        head = _prompts(_panel(index=0, total=1))[0]
        self.assertNotIn("问题 1/1", head)

    def test_progress_shown_for_multiple_questions(self) -> None:
        head = _prompts(_panel(index=1, total=3))[0]
        self.assertIn("问题 2/3", head)


class OtherChoiceTest(unittest.TestCase):
    """F8：「其它…」由界面无条件追加，不来自模型。"""

    def test_appended_after_the_model_options(self) -> None:
        panel = _panel(_question(labels=("甲", "乙", "丙")))
        self.assertEqual(
            _selectable_ids(panel), ["0", "1", "2", ClarifyPanel.OTHER_ID]
        )

    def test_it_is_not_in_the_input(self) -> None:
        """反证：模型给的数据里没有任何一项叫「其它」——它确实是界面补的。"""
        question = _question(labels=("甲", "乙"))
        self.assertNotIn("其它", [o.label for o in question.options])
        self.assertIn(ClarifyPanel.OTHER_ID, _selectable_ids(_panel(question)))


class DescriptionTest(unittest.TestCase):
    """说明行 disabled、不占序号（沿用 C4 以来的结构）。"""

    def test_description_lines_are_not_selectable(self) -> None:
        panel = _panel(_question(descriptions=("跟着仓库走", "只对你生效")))
        self.assertEqual(_selectable_ids(panel), ["0", "1", ClarifyPanel.OTHER_ID])

    def test_number_keys_skip_description_lines(self) -> None:
        """按 2 必须落到第二个**选项**上，而不是一行说明文字。"""
        panel = _panel(_question(descriptions=("跟着仓库走", "只对你生效")))
        index = panel.choice_index(2)
        self.assertEqual(panel.get_option_at_index(index).id, "1")


class MultiSelectTest(unittest.TestCase):
    """F15（含修订）：勾选框、回车勾选并前进、数字键、提交行。"""

    def setUp(self) -> None:
        self.panel = _panel(_question(multi=True, labels=("甲", "乙", "丙")))

    def test_checkboxes_are_ascii(self) -> None:
        """
        ⚠ 勾选框必须是 ASCII 方括号（转义后的）。

        用图形符号就要往界面符号白名单里加一项，而那张表越短越有用。
        这条与 `test_tui_symbols.py` 里那条「刻意不新增」互为两半。
        """
        prompts = "".join(_prompts(self.panel))
        self.assertIn("\\[ ]", prompts)
        for glyph in ("☑", "☐", "✓", "✔", "○"):
            self.assertNotIn(glyph, prompts)

    def test_enter_toggles_the_highlighted_row(self) -> None:
        """回车 = 勾选（F15 修订：初版是空格，两个键太多了）。"""
        self.panel.action_select()
        self.assertEqual(self.panel.checked_labels(), ("甲",))
        # 光标已经前进到「乙」，要再切「甲」得先移回去
        self.panel.highlighted = self.panel._choices[0][0]
        self.panel.action_select()
        self.assertEqual(self.panel.checked_labels(), ())

    def test_enter_advances_to_the_next_option(self) -> None:
        """勾完一项，光标自动落到下一项——用户不必手动按下箭头。"""
        self.panel.action_select()
        self.assertEqual(self.panel.choice_position(self.panel.highlighted), 1)
        self.panel.action_select()
        self.assertEqual(self.panel.choice_position(self.panel.highlighted), 2)

    def test_last_option_jumps_over_other_straight_to_submit(self) -> None:
        """
        ⚠ 勾完**最后一个**候选项，光标要跳过「其它…」直达「提交」。

        不跳的话用户想交卷、手却停在「其它…」上，再按一下回车就被拉进
        自由输入态——那与他正在做的事完全相反，而且他多半已经在按了。
        """
        for _ in range(3):
            self.panel.action_select()
        landed = self.panel.get_option_at_index(self.panel.highlighted)
        self.assertEqual(landed.id, ClarifyPanel.SUBMIT_ID)

    def test_submit_row_only_exists_for_multi_select(self) -> None:
        """反证：单选题不该多出一行「提交」——按一次回车就结束了。"""
        self.assertIn(ClarifyPanel.SUBMIT_ID, _selectable_ids(self.panel))
        self.assertNotIn(
            ClarifyPanel.SUBMIT_ID, _selectable_ids(_panel(_question(multi=False)))
        )

    def test_checked_marks_show_on_screen(self) -> None:
        self.panel.toggle_check(1)
        row = _prompts(self.panel)[self.panel._choices[1][0]]
        self.assertIn("\\[x]", row)

    def test_digit_key_toggles_instead_of_submitting(self) -> None:
        """
        ⚠ 多选下数字键 = 切换勾选，**不是**提交（F15）。

        断言「勾上了」而不是「没提交」——后者没法直接观测，而前者一旦成立，
        提交就不可能同时发生（`action_select` 的两条分支互斥）。

        ⚠ 数字键与回车走的是**同一个** `action_select`（基类的
        `activate_choice` 只负责先把光标移过去），因此两条路不可能分叉。
        """
        index = self.panel.choice_index(3)
        self.panel.activate_choice(index)
        self.assertEqual(self.panel.checked_labels(), ("丙",))

    def test_other_row_does_not_join_the_plain_toggle_set(self) -> None:
        """
        「其它…」的勾选状态**来自它那段文本**，不进 `_checked`（F15 二次修订）。

        ⚠ 两份状态表达同一件事必然分叉（勾上了但文本是空的，或反过来），
        而那种不一致在界面上看不出来，只表现为「提交行的条数不对」。

        ⚠ 本条此前读的是 `_choices[-1]`——加了「提交」行之后那已经是**提交行**了，
        于是它变成「断言提交行没有勾选框」，**照样全绿而验错了对象**。
        这类静默漂移正是「加一行就要回头看谁在用下标取行」的理由。
        """
        other_position = len(self.panel._question.options)
        self.panel.toggle_check(other_position)
        self.assertEqual(self.panel.checked_labels(), (), "它不该进 `_checked`")
        self.assertEqual(self.panel.checked_count(), 0, "也不该被算进条数")

    def test_submit_row_never_gets_a_checkbox(self) -> None:
        """反证：提交行是个动作不是候选项，不许长出勾选框。"""
        row = _prompts(self.panel)[self.panel._choices[-1][0]]
        self.assertNotIn("\\[x]", row)
        self.assertNotIn("\\[ ]", row)

    def _submit_row(self) -> str:
        return _prompts(self.panel)[self.panel._choices[-1][0]]

    def test_submit_row_counts_the_checked_ones(self) -> None:
        """已选条数摆在用户正要按的那一行上，他不必自己数、也不必挪视线。"""
        self.assertIn("一项都不选", self._submit_row())
        self.panel.toggle_check(0)
        self.panel.toggle_check(2)
        self.assertIn("已选 2 项", self._submit_row())

    def test_submit_row_count_survives_moving_the_cursor(self) -> None:
        """
        ⚠ **与勾选框同一个坑**：提交行的条数也存在 `_choices` 里，
        只改屏幕不改它的话，用户按一下方向键条数就退回改之前那个数字。

        它比勾选框那条更阴——勾选框全没了一眼就看得出来，
        而一个「已选 1 项」旁边明明勾着两个，只有仔细数才发现。
        """
        self.panel.toggle_check(0)
        self.panel.toggle_check(2)
        self.panel.highlighted = self.panel._choices[1][0]
        self.assertIn("已选 2 项", self._submit_row())

    def test_single_select_has_no_checkboxes(self) -> None:
        """反证：单选面板一个勾选框都不该出现。"""
        prompts = "".join(_prompts(_panel(_question(multi=False))))
        self.assertNotIn("\\[x]", prompts)
        self.assertNotIn("\\[ ]", prompts)


class CheckboxSurvivesNavigationTest(unittest.TestCase):
    """
    ⚠⚠ **本文件最要紧的一条。**

    基类的 `watch_highlighted` 会拿 `_choices` 里存的文本把每一行重画一遍。
    `toggle_check` 若只改屏幕、不同步 `_choices`，**用户按一下方向键
    所有勾选就凭空全没**。

    而这个 bug 只在「勾选之后再移动光标」时出现——一次不移动光标的手测
    完全看不到它，这正是它需要一条专用护栏的原因。
    """

    def test_checks_survive_moving_the_cursor(self) -> None:
        panel = _panel(_question(multi=True, labels=("甲", "乙", "丙")))
        panel.toggle_check(0)
        panel.toggle_check(2)

        # 移动光标 —— 这会触发 watch_highlighted 重画所有行
        panel.highlighted = panel._choices[1][0]

        self.assertEqual(
            panel.checked_labels(), ("甲", "丙"), "内部状态不该被重画影响"
        )
        prompts = _prompts(panel)
        self.assertIn("\\[x]", prompts[panel._choices[0][0]], "屏幕上第 1 项的勾选没了")
        self.assertIn("\\[x]", prompts[panel._choices[2][0]], "屏幕上第 3 项的勾选没了")


class FreeTextStateTest(unittest.TestCase):
    """F16：自由输入提示态。"""

    def setUp(self) -> None:
        self.panel = _panel(_question(header="配置位置"))
        self.panel.show_free_text()

    def test_panel_stays_visible(self) -> None:
        """
        ⚠ 面板**不收起**——「现在在干什么」必须一直看得见。

        收起来的话用户会以为提问已经结束，而回调其实还阻塞着。
        """
        self.assertTrue(self.panel.display)

    def test_question_is_still_on_screen(self) -> None:
        head = _prompts(self.panel)[0]
        self.assertIn("放哪一层？", head)
        self.assertIn("配置位置", head)

    def test_it_tells_the_user_what_to_do(self) -> None:
        hint = _prompts(self.panel)[-1]
        self.assertIn("打字", hint)
        self.assertIn("Esc", hint)

    def test_no_selectable_rows(self) -> None:
        self.assertEqual(_selectable_ids(self.panel), [])

    def test_digit_keys_fall_through_to_the_input_box(self) -> None:
        """
        ⚠ 本态下没有任何可选项，因此数字键**天然**落进输入框。

        这是「不加可选项」白捡的好处，不必为它新增判断——但它得有条护栏，
        否则将来有人往提示态里加一个可选项时，用户输入的每个数字
        都会变成一次误选。
        """
        for number in (1, 2, 3):
            self.assertIsNone(self.panel.choice_index(number))


class RestoreOptionsTest(unittest.TestCase):
    """F16 的 Esc 分支：从自由输入态退回选项列表态。"""

    def test_options_come_back(self) -> None:
        panel = _panel(_question(labels=("甲", "乙")))
        panel.show_free_text()
        panel.restore_options()
        self.assertEqual(_selectable_ids(panel), ["0", "1", ClarifyPanel.OTHER_ID])

    def test_checked_items_survive_the_round_trip(self) -> None:
        """
        ⚠ 退回来时勾选**必须还在**。

        用户勾了两项、又去看了看「其它…」、然后按 Esc 退回来——勾选还在
        才是对的。用 `show_question` 复原会把它们清空（那是「换了一道题」
        才该做的事），所以这里刻意有 `restore_options` 这个第二个入口。
        """
        panel = _panel(_question(multi=True, labels=("甲", "乙", "丙")))
        panel.toggle_check(0)
        panel.toggle_check(1)
        panel.show_free_text()
        panel.restore_options()
        self.assertEqual(panel.checked_labels(), ("甲", "乙"))

    def test_a_new_question_does_reset_the_checks(self) -> None:
        """反证：换一道题时勾选必须清空，否则上一题的勾选会串进来。"""
        panel = _panel(_question(multi=True, labels=("甲", "乙")))
        panel.toggle_check(0)
        panel.show_question(_question(multi=True, labels=("丙", "丁")), 1, 2)
        self.assertEqual(panel.checked_labels(), ())


class MarkupSafetyTest(unittest.TestCase):
    """
    ⚠ 模型给的自由文本里可能有字面方括号。

    落单的左方括号会在**布局阶段主线程**抛 MarkupError 并拆掉整个应用，
    **没有任何 try/except 兜得住**（`CLAUDE.md` 那条，真实崩溃过一次）。
    """

    _ALLOWED_TAGS = ("dim", "bold")

    def test_brackets_in_model_text_are_escaped(self) -> None:
        question = ClarifyQuestion(
            question="用 list[str] 还是 tuple？",
            options=(
                ClarifyOption(label="list[str]", description="可变，见 typing[docs"),
                ClarifyOption(label="tuple[str, ...]", description="不可变"),
            ),
            header="类型[写法",
        )
        panel = ClarifyPanel()
        panel.show_question(question)
        for row in _prompts(panel):
            # 去掉已转义的那些，剩下的左方括号只允许来自 markup 标签
            stripped = row.replace("\\[", "")
            for chunk in stripped.split("[")[1:]:
                tag = chunk.split("]")[0]
                self.assertTrue(
                    tag.startswith("/")
                    or tag.startswith("#")
                    or tag in self._ALLOWED_TAGS,
                    f"这一行里有个未转义的字面方括号：{row!r}",
                )


if __name__ == "__main__":
    unittest.main()


class OtherIsACheckboxInMultiSelectTest(unittest.TestCase):
    """
    ⚠ **多选题里「其它…」是第 N 个勾选项，勾上它的方式是打一段字**
    （F15 二次修订）。

    改之前它是「进去打字 → 当场交卷」，于是用户想「先补一条自己的，
    再回去把剩下几项勾上」这个再自然不过的意图**做不到**，
    而且是交出去之后才发现。
    """

    def setUp(self) -> None:
        self.panel = _panel(_question(multi=True, labels=("甲", "乙")))

    def _other_row(self) -> str:
        return _prompts(self.panel)[self.panel._choices[-2][0]]

    def _submit_row(self) -> str:
        return _prompts(self.panel)[self.panel._choices[-1][0]]

    def test_other_row_shows_an_empty_checkbox(self) -> None:
        self.assertIn("\[ ]", self._other_row())

    def test_typing_checks_it_and_shows_what_you_typed(self) -> None:
        """
        回到列表后那一行要显示打的内容——只显示一个 `[x] 其它…` 的话，
        用户想确认自己打了什么就只能再进去一次。
        """
        self.panel.set_custom_text("还要一份 README")
        row = self._other_row()
        self.assertIn("\[x]", row)
        self.assertIn("还要一份 README", row)

    def test_it_counts_toward_the_submit_row(self) -> None:
        self.panel.toggle_check(0)
        self.assertIn("已选 1 项", self._submit_row())
        self.panel.set_custom_text("再加一条")
        self.assertIn("已选 2 项", self._submit_row())

    def test_enter_on_a_checked_other_clears_it(self) -> None:
        """
        ⚠ 它的回车语义必须与其余勾选项一致——**按一下切换**。

        做成「已勾时再进去改」的话，用户就**没有任何办法把它取消掉**了。
        """
        self.panel.set_custom_text("写错了")
        self.panel.highlighted = self.panel._choices[-2][0]
        self.panel.action_select()
        self.assertEqual(self.panel.custom_text(), "")
        self.assertIn("\[ ]", self._other_row())
        self.assertEqual(
            self.panel.get_option_at_index(self.panel.highlighted).id,
            ClarifyPanel.SUBMIT_ID,
            "取消之后光标照常推到提交行",
        )

    def test_checks_survive_entering_free_text(self) -> None:
        """进自由输入态不许清掉已勾的项——回来还要接着挑。"""
        self.panel.toggle_check(0)
        self.panel.show_free_text()
        self.assertEqual(self.panel.checked_labels(), ("甲",))

    def test_free_text_hint_differs_between_the_two_kinds(self) -> None:
        """
        ⚠ 两种题的回车含义不同，提示行是用户唯一的依据：
        单选题打完就交卷，多选题打完只是记下来、回到勾选界面接着挑。
        """
        self.panel.show_free_text()
        self.assertIn("回到勾选", "".join(_prompts(self.panel)))

        single = _panel(_question(multi=False))
        single.show_free_text()
        text = "".join(_prompts(single))
        self.assertIn("回车提交", text)
        self.assertNotIn("回到勾选", text)

    def test_single_select_other_row_has_no_checkbox(self) -> None:
        """反证：单选题的「其它…」不该长出勾选框。"""
        single = _panel(_question(multi=False))
        row = _prompts(single)[single._choices[-1][0]]
        self.assertNotIn("\[ ]", row)
        self.assertNotIn("\[x]", row)


class TextualClickRoutingTest(unittest.TestCase):
    """
    ⚠ **钉住一条上游事实：`OptionList` 的鼠标点击经过 `action_select`。**

    本扩展把多选的勾选逻辑放在 `action_select` 上，理由正是「它是回车、
    数字键、鼠标点击三条路唯一的汇合点」。这个理由**整个依赖上游实现**
    ——实测 Textual 8.2.7 的 `_on_click` 就是
    `self.highlighted = clicked_option; self.action_select()`。

    上游哪天改成直接 `post_message(OptionSelected(...))`，鼠标点击就会绕开
    我们的覆写、在多选题里**当场提交**，而键盘按同一项只是勾选——
    同一个动作两种结果，且只有用鼠标的人撞得到，测试全绿。

    这条红了不代表有 bug，代表**那个判断要重新做一遍**（`app.py`
    `_settle_clarify` 的分支 3 是为此留的兜底）。
    """

    def test_click_goes_through_action_select(self) -> None:
        import inspect

        from textual.widgets import OptionList

        source = inspect.getsource(OptionList._on_click)
        self.assertIn(
            "action_select",
            source,
            "Textual 改了点击的路由：鼠标点击不再经过 action_select。"
            "多选的勾选逻辑挂在那里，必须重新确认点击在多选题里的行为。",
        )
