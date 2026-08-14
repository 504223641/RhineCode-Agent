"""
护栏：两阶段输出解析，以及**两个阶段的失败语义刻意不同**（c16 F10/F12）。

    parse_stage1 返回 None（看不懂） → 按可疑处理，**进第二阶段**
    parse_stage2 返回 None（看不懂） → 判定失败，未熔断时**拒绝**

⚠ 这不是放宽。放行的路径**只有一条**：`parse_stage1` 明确返回 `False`。
下面 `OnlyOnePathAllowsTest` 就是钉这一条的——它遍历一批「乱七八糟的输出」，
断言没有任何一种能走到放行。
"""

import unittest

from rhinecode.classifier import parse


class Stage1Test(unittest.TestCase):
    """第一阶段：只输出一个词。"""

    def test_clean_words(self) -> None:
        self.assertIs(parse.parse_stage1("pass"), False)
        self.assertIs(parse.parse_stage1("block"), True)

    def test_tolerates_punctuation_and_case_and_markdown(self) -> None:
        """
        解析要宽松。每严格一分，落到「看不懂」的概率就高一分——
        而那条路在第二阶段通往**拒绝一次正常操作**。
        """
        for text in ("PASS", " pass. ", "`pass`", "**pass**", "pass\n"):
            with self.subTest(text=text):
                self.assertIs(parse.parse_stage1(text), False)
        for text in ("BLOCK", "block。", "> block", "*block*"):
            with self.subTest(text=text):
                self.assertIs(parse.parse_stage1(text), True)

    def test_chinese_variants(self) -> None:
        """模型偶尔会自己换成中文。"""
        self.assertIs(parse.parse_stage1("放行"), False)
        self.assertIs(parse.parse_stage1("拦截"), True)

    def test_first_line_wins_not_whole_text(self) -> None:
        """
        ⚠ 只看首行，**不做全文搜索**。

        一段解释性文字里同时出现 pass 与 block 是很可能的，
        全文搜索会让结果取决于「谁先出现」——那是个碰运气的判定。
        """
        self.assertIs(parse.parse_stage1("block\n虽然看起来像是可以 pass 的"), True)

    def test_unparseable_returns_none(self) -> None:
        for text in ("", "   ", "嗯……让我想想", "我不确定这个动作"):
            with self.subTest(text=text):
                self.assertIsNone(parse.parse_stage1(text))


class Stage2Test(unittest.TestCase):
    """第二阶段：结论 + 理由。"""

    def test_standard_format(self) -> None:
        got = parse.parse_stage2("结论: block\n理由: 地址的查询参数里像是一段密钥")
        self.assertEqual(got, (True, "地址的查询参数里像是一段密钥"))

    def test_pass_with_reason(self) -> None:
        got = parse.parse_stage2("结论: pass\n理由: 这是用户明确要求的构建命令")
        self.assertIsNotNone(got)
        self.assertIs(got[0], False)

    def test_tolerates_english_and_markdown(self) -> None:
        for text in (
            "Verdict: block\nReason: exfiltrates a local file",
            "**结论**: block\n**理由**: 往外发文件",
            "结论：block\n理由：往外发文件",
        ):
            with self.subTest(text=text):
                got = parse.parse_stage2(text)
                self.assertIsNotNone(got, text)
                self.assertIs(got[0], True)

    def test_bare_word_falls_back_to_stage1_rules(self) -> None:
        """没写「结论:」前缀时退回第一阶段那套判断——模型偶尔会直接甩一个词。"""
        got = parse.parse_stage2("block")
        self.assertIsNotNone(got)
        self.assertIs(got[0], True)

    def test_missing_reason_is_not_a_failure(self) -> None:
        """
        结论有、理由没有 → **不算失败**，给一句兜底文案。

        判 None 的代价是「一次本该被拦下的动作走了失败路径」——虽然失败也是
        拒绝，但记录里会归错类，排查时会以为是接口出了问题。
        """
        got = parse.parse_stage2("结论: block")
        self.assertIsNotNone(got)
        self.assertIs(got[0], True)
        self.assertTrue(got[1])

    def test_unparseable_returns_none(self) -> None:
        for text in ("", "   ", "我需要更多信息才能判断"):
            with self.subTest(text=text):
                self.assertIsNone(parse.parse_stage2(text))


class OnlyOnePathAllowsTest(unittest.TestCase):
    """
    ⚠ **本文件最要紧的一条**：放行的路径只有「明确说不可疑」这一条。

    遍历一批异常输出，断言没有任何一种能被解析成放行。
    没有这条的话，一次「解析器写宽了」的改动会静默地把方向从收紧变成放宽。
    """

    GARBAGE = [
        "",
        "   \n\t ",
        "我不确定",
        "这个问题很复杂，需要更多上下文",
        "ERROR: rate limited",
        "{}",
        "null",
        "<html><body>502 Bad Gateway</body></html>",
        "抱歉，我无法回答这个问题",
    ]

    def test_no_garbage_parses_as_allow_in_stage1(self) -> None:
        for text in self.GARBAGE:
            with self.subTest(text=text[:24]):
                self.assertIsNot(
                    parse.parse_stage1(text), False,
                    "异常输出绝不能被解析成放行",
                )

    def test_no_garbage_parses_as_allow_in_stage2(self) -> None:
        for text in self.GARBAGE:
            with self.subTest(text=text[:24]):
                got = parse.parse_stage2(text)
                if got is not None:
                    self.assertIs(got[0], True, "异常输出若能解析，也只能是拦截")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
