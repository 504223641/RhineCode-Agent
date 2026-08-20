"""搜索工具层单测（web_search 扩展 T12，spec F1/F2/F3/F5/F21 · AC1/AC2/AC3/AC5/AC22）。"""

import unittest

from rhinecode.classifier.models import SCOPE_SEARCH
from rhinecode.tools.display import TOOL_LABELS
from rhinecode.tools.web_search import WebSearchTool
from rhinecode.web.models import SearchOutcome, SearchResult
from rhinecode.web.search import FAILURE_NO_KEY, FAILURE_QUOTA, FAILURE_SERVICE


class _StubManager:
    """记录入参、按预置结果作答的 manager 替身。"""

    def __init__(self, outcome=None, raises=None) -> None:
        self._outcome = outcome or SearchOutcome(
            ok=True,
            query="q",
            provider="brave",
            results=(SearchResult("T", "https://a.test/1", "D"),),
            used=1,
            limit=50,
        )
        self._raises = raises
        self.calls: list = []

    def search(self, query, count=None):
        self.calls.append((query, count))
        if self._raises is not None:
            raise self._raises
        return self._outcome


class DeclarationTests(unittest.TestCase):
    """类属性是本工具的安全声明，每一条都有 spec 依据。"""

    def test_read_only_is_false(self) -> None:
        """
        ⚠ spec F3 的硬约束。

        改成 True 会让它在第③层未命中时直接放行、**根本不进第④层**，
        而 C16 分类器的触发条件正是「结论来自第④层」——净效果是
        **分类器对搜索完全失效，且没有任何声音**（配置、界面、测试三处都看不出，
        因为结果确实是放行）。这条断言是那个无声缺陷的唯一护栏。
        """
        self.assertIs(WebSearchTool.read_only, False)

    def test_classifier_scope_matches_constant(self) -> None:
        """
        工具里写的是字面量（避免把整个 classifier 包拉进工具层的导入图），
        因此需要这条断言把它与真正的常量钉在一起——两边分叉时它会红。
        """
        self.assertEqual(WebSearchTool.classifier_scope, SCOPE_SEARCH)

    def test_primary_arg_is_query(self) -> None:
        """界面上要显示的是查询词——它决定「发出去了什么」（spec F7）。"""
        self.assertEqual(WebSearchTool.primary_arg, "query")

    def test_tool_label_registered(self) -> None:
        self.assertEqual(TOOL_LABELS["web_search"], "WebSearch")

    def test_parameters_exactly_two(self) -> None:
        """spec F2/AC2：没有请求头、请求体、语言 / 地区 / 时间范围任何入口。"""
        props = WebSearchTool.parameters["properties"]
        self.assertEqual(sorted(props), ["count", "query"])
        self.assertEqual(WebSearchTool.parameters["required"], ["query"])

    def test_description_warns_about_third_party(self) -> None:
        """spec F5/AC5：描述里要有「发给第三方」与「别放内部标识符 / 密钥」两层意思。"""
        text = WebSearchTool.description.lower()
        self.assertIn("third-party", text)
        self.assertIn("verbatim", text)
        self.assertIn("credentials", text)
        self.assertIn("internal project", text)

    def test_description_warns_results_untrusted(self) -> None:
        text = WebSearchTool.description
        self.assertIn("UNTRUSTED", text)
        self.assertIn("<untrusted-content>", text)
        # 标题与摘要同样不可信——SEO 投毒的落点就在那里。
        self.assertIn("Titles and snippets", text)

    def test_description_does_not_claim_cannot_send_data(self) -> None:
        """
        ⚠ **反证**：`web_fetch` 的「只取不发」在这里是错的。

        查询词本身就是发出去的数据。谁要顺手把那句话抄过来，这条会红。
        """
        text = WebSearchTool.description.lower()
        self.assertNotIn("cannot be used to submit data", text)


class ArgumentTests(unittest.TestCase):
    def test_missing_query(self) -> None:
        result = WebSearchTool(_StubManager()).execute({})
        self.assertFalse(result.ok)
        self.assertIn("query", result.output)

    def test_blank_query(self) -> None:
        self.assertFalse(WebSearchTool(_StubManager()).execute({"query": "   "}).ok)

    def test_non_dict_args(self) -> None:
        self.assertFalse(WebSearchTool(_StubManager()).execute(None).ok)

    def test_count_passed_through_verbatim(self) -> None:
        """夹取由 manager 统一做——工具层多判一次会让口径分两处。"""
        manager = _StubManager()
        WebSearchTool(manager).execute({"query": "q", "count": 99})
        self.assertEqual(manager.calls, [("q", 99)])

    def test_count_absent_passes_none(self) -> None:
        manager = _StubManager()
        WebSearchTool(manager).execute({"query": "q"})
        self.assertEqual(manager.calls, [("q", None)])

    def test_extra_args_ignored(self) -> None:
        """spec F2/AC2：多余参数不产生任何效果——两次调用逐字相同。"""
        plain = _StubManager()
        extra = _StubManager()
        a = WebSearchTool(plain).execute({"query": "q"})
        b = WebSearchTool(extra).execute(
            {"query": "q", "headers": {"X": "1"}, "lang": "zh", "body": "x"}
        )
        self.assertEqual(plain.calls, extra.calls)
        self.assertEqual(a.output, b.output)


class OkFlagTests(unittest.TestCase):
    """
    spec F21/AC22 的那张表。

    这是照抄 `web_fetch` 验收期修掉的真实缺陷：`ok` 若不反映「有没有拿到内容」，
    **TUI 会把一次失败显示成绿色成功**、只是正文里写着失败。
    那个缺陷**离线单测发现不了**——只有把真实链路跑起来才会觉得不对，
    所以这里逐条钉死。
    """

    def _run(self, outcome) -> bool:
        return WebSearchTool(_StubManager(outcome)).execute({"query": "q"}).ok

    def test_with_results_is_ok(self) -> None:
        self.assertTrue(
            self._run(
                SearchOutcome(
                    ok=True, query="q", provider="brave",
                    results=(SearchResult("T", "https://a", "D"),),
                )
            )
        )

    def test_zero_results_is_still_ok(self) -> None:
        """搜到 0 条也是成功——我们如实回报了「这个词搜不到东西」，那是有效信息。"""
        self.assertTrue(self._run(SearchOutcome(ok=True, query="q", provider="brave")))

    def test_three_failures_are_not_ok(self) -> None:
        for failure in (FAILURE_NO_KEY, FAILURE_QUOTA, FAILURE_SERVICE):
            with self.subTest(failure=failure):
                self.assertFalse(
                    self._run(
                        SearchOutcome(ok=False, query="q", provider="brave", failure=failure)
                    )
                )


class ExceptionTests(unittest.TestCase):
    def test_ordinary_exception_becomes_result(self) -> None:
        result = WebSearchTool(_StubManager(raises=RuntimeError("炸了"))).execute({"query": "q"})
        self.assertFalse(result.ok)
        self.assertIn("炸了", result.output)

    def test_keyboard_interrupt_propagates(self) -> None:
        """⚠ `BaseException` 刻意不吞——用户按 Esc / Ctrl-C 要能中断一次卡住的搜索。"""
        tool = WebSearchTool(_StubManager(raises=KeyboardInterrupt()))
        with self.assertRaises(KeyboardInterrupt):
            tool.execute({"query": "q"})


class OutputTests(unittest.TestCase):
    def test_output_and_summary_both_filled(self) -> None:
        result = WebSearchTool(_StubManager()).execute({"query": "q"})
        self.assertIn("<untrusted-content", result.output)
        self.assertIn("搜索", result.summary)


if __name__ == "__main__":
    unittest.main()
