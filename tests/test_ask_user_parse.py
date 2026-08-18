"""
澄清提问的解析护栏（ask-user 扩展 F10 / spec AC9）。

本文件逐行覆盖 spec F10 那张「模型给的形状有瑕疵时怎么办」的表。
被测对象是 `rhinecode/agent/clarify.py` 的纯函数——不起线程、不发请求、
不碰界面，因此这一整个文件跑起来是毫秒级的。

## 为什么这些用例值得单独一个文件

模型给的参数形状是**本扩展唯一不受我们控制的输入**。工具描述可以写得再清楚，
它仍然可能把 `options` 写成字符串、把四个问题写成八个、或者把 `label` 写成
`summary`。这里每一条都对应一种真实可能出现的写法，而共同的判据只有两条：
**一律不抛异常**，以及**被我们动过手脚的地方必须写进回灌文本**。
"""

import unittest

from rhinecode.agent.clarify import (
    MAX_HEADER,
    MAX_OPTIONS,
    MAX_QUESTIONS,
    parse_questions,
)


def _q(question="问题", options=None, **extra):
    """造一个最小可用的问题字典。"""
    payload = {"question": question, "options": options if options is not None else [{"label": "甲"}]}
    payload.update(extra)
    return payload


class NeverRaisesTest(unittest.TestCase):
    """
    ⚠ **最要紧的一条：任何输入都不抛异常。**

    `parse_questions` 跑在 Agent 循环的工具执行路径上，抛出去会把
    「模型参数写歪了」变成「整轮工具执行炸掉」——而后者会被当成
    「你的工具坏了」回灌给模型（`CLAUDE.md`：Agent Loop 是唯一会把异常
    变成工具结果的地方）。
    """

    def test_any_shape_returns_a_pair(self) -> None:
        weird = [
            None, "", "questions", 0, 1.5, True, {}, {"questions": []},
            [], [None], [0], ["x"], [[]], [{"question": None}],
            [{"question": "q", "options": "不是数组"}],
            [{"question": "q", "options": [None, 3, "x"]}],
            [{"question": "q", "options": [{}]}],
        ]
        for raw in weird:
            with self.subTest(raw=repr(raw)[:40]):
                questions, notes = parse_questions(raw)
                self.assertIsInstance(questions, list)
                self.assertIsInstance(notes, list)


class NotAnArrayTest(unittest.TestCase):
    """F10 第一行：`questions` 不是数组 → 整次调用没法进行。"""

    def test_returns_nothing_but_explains_why(self) -> None:
        questions, notes = parse_questions("放哪一层？")
        self.assertEqual(questions, [])
        self.assertTrue(notes, "没有问题时必须给出至少一条原因，否则模型不知道该怎么改")
        self.assertIn("questions", notes[0])


class NoUsableQuestionTest(unittest.TestCase):
    """F10 第二行：一个可用问题都没有。"""

    def test_missing_question_text_is_skipped(self) -> None:
        questions, notes = parse_questions([{"options": [{"label": "甲"}]}])
        self.assertEqual(questions, [])
        self.assertIn("跳过", "".join(notes))

    def test_missing_options_is_skipped(self) -> None:
        questions, notes = parse_questions([{"question": "放哪一层？"}])
        self.assertEqual(questions, [])
        self.assertIn("跳过", "".join(notes))

    def test_a_bad_question_does_not_kill_the_good_ones(self) -> None:
        """
        ⚠ **反向约束：某一题坏掉不该毁掉整次提问。**

        这是 F10 里「跳过该问题」与「整次失败」两种处理的分界。
        写成「有一个坏的就整次打回」会让模型为了一个笔误重来一轮，
        而用户那边什么都没看到。
        """
        questions, notes = parse_questions([
            _q("好问题一"),
            {"question": "坏问题"},          # 没有候选项
            _q("好问题二"),
        ])
        self.assertEqual([q.question for q in questions], ["好问题一", "好问题二"])
        self.assertIn("1 个", "".join(notes))


class TooManyQuestionsTest(unittest.TestCase):
    """F10 第三行：问题超上限 → 截断，**并且在回灌里说破**。"""

    def test_clamped_to_the_limit(self) -> None:
        questions, _notes = parse_questions([_q(f"问题{i}") for i in range(7)])
        self.assertEqual(len(questions), MAX_QUESTIONS)
        self.assertEqual(questions[0].question, "问题0")

    def test_the_truncation_is_stated_in_the_notes(self) -> None:
        """
        ⚠ **这条是「截断而不是拒绝」这个决定的全部依据。**

        spec F10 里那条判据是「被截掉的事实，模型下一轮能不能自己发现」。
        它能发现，**前提是我们说了**。说明没了的话，截断就退化成
        todo 清单那边明令禁止的形态——模型以为全问过了，
        而它下一轮的判断全建立在一个不存在的答案上。
        """
        _questions, notes = parse_questions([_q(f"问题{i}") for i in range(7)])
        joined = "".join(notes)
        self.assertIn("7", joined, "要告诉模型它一共提了几个")
        self.assertIn(str(MAX_QUESTIONS), joined, "要告诉模型上限是几个")
        self.assertIn("没有提问", joined, "要说清楚其余的压根没问，不是问了没答")


class OptionsTest(unittest.TestCase):
    """F10 第四、五行：候选项的数量边界。"""

    def test_more_than_the_limit_is_clamped_and_stated(self) -> None:
        questions, notes = parse_questions([
            _q(options=[{"label": f"选项{i}"} for i in range(6)])
        ])
        self.assertEqual(len(questions[0].options), MAX_OPTIONS)
        self.assertIn(str(MAX_OPTIONS), "".join(notes))

    def test_exactly_one_option_is_allowed(self) -> None:
        """
        F10：**恰好一个候选项要放行**，不要为难模型。

        界面会另外无条件追加一项「其它…」，所以用户实际看到的仍是两个可选项，
        而「在这一个做法与自己另说之间选」是个完全成立的提问。
        """
        questions, _notes = parse_questions([_q(options=[{"label": "唯一解"}])])
        self.assertEqual(len(questions), 1)
        self.assertEqual(questions[0].options[0].label, "唯一解")

    def test_unusable_entries_inside_options_are_dropped(self) -> None:
        questions, _notes = parse_questions([
            _q(options=[None, {"label": ""}, {"description": "只有说明"}, {"label": "好的"}])
        ])
        self.assertEqual([o.label for o in questions[0].options], ["好的"])

    def test_legacy_field_names_still_parse(self) -> None:
        """
        ⚠ 旧字段名（`summary` / `detail`）也要认。

        新 schema 只教模型写 `label` / `description`，但它可能凭训练先验
        写出旧的那套——本项目已多次实测到「凭先验硬造调用」的形态。
        认两套的成本是两行，认不出的代价是**整题被丢掉、白烧一轮**。
        """
        questions, _notes = parse_questions([
            {"question": "q", "options": [{"summary": "甲", "detail": "说明"}]}
        ])
        self.assertEqual(questions[0].options[0].label, "甲")
        self.assertEqual(questions[0].options[0].description, "说明")


class HeaderTest(unittest.TestCase):
    """F10 第六行：徽章超长截断，缺省为空。"""

    def test_long_header_is_truncated(self) -> None:
        questions, _notes = parse_questions([_q(header="一二三四五六七八九十甲乙丙丁")])
        self.assertLessEqual(len(questions[0].header), MAX_HEADER)

    def test_missing_header_is_empty_not_placeholder(self) -> None:
        """
        空徽章必须是**空串**，不是「（无）」之类的占位。

        面板据此决定整个徽章出不出现（spec F13）——给一个占位字符串的话，
        每个没写 header 的问题上面都会挂一枚写着「（无）」的徽章。
        """
        questions, _notes = parse_questions([_q()])
        self.assertEqual(questions[0].header, "")


class MultiSelectTest(unittest.TestCase):
    """F10 第七行：`multiSelect` 的类型容忍。"""

    def test_both_spellings_are_accepted(self) -> None:
        """连字符与下划线两种写法都认（与 C13 角色定义解析同口径）。"""
        for key in ("multiSelect", "multi_select"):
            with self.subTest(key=key):
                questions, _notes = parse_questions([_q(**{key: True})])
                self.assertTrue(questions[0].multi_select)

    def test_non_boolean_falls_back_to_single_select(self) -> None:
        """
        ⚠ 偏严方向：认不出就当单选。

        反过来（认不出就当多选）会让一次本该单选的提问变成多选面板，
        而用户可能一项都不勾就提交——那与「跳过」的语义混在一起。
        """
        for value in ("true", 1, "yes", [], {}):
            with self.subTest(value=repr(value)):
                questions, _notes = parse_questions([_q(multiSelect=value)])
                self.assertFalse(questions[0].multi_select)


class OptionsAreImmutableTest(unittest.TestCase):
    """
    `options` 必须是 tuple。

    它由 Agent 线程构造、主线程读来渲染。不可变能从结构上杜绝
    「界面渲染到一半被改」——这是本项目对跨线程数据的一贯要求。
    """

    def test_options_is_a_tuple(self) -> None:
        questions, _notes = parse_questions([_q()])
        self.assertIsInstance(questions[0].options, tuple)


if __name__ == "__main__":
    unittest.main()
