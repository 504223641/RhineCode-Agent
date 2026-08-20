"""搜索结果渲染单测（web_search 扩展 T6，spec F19/F21/F23 · AC22/AC25/AC29）。"""

import unittest

from rhinecode.web.models import SearchOutcome, SearchResult
from rhinecode.web.search import FAILURE_NO_KEY, FAILURE_QUOTA, FAILURE_SERVICE
from rhinecode.web.search_render import (
    UNTRUSTED_CLOSE,
    UNTRUSTED_OPEN,
    render,
    summary,
)


def _ok(**kw) -> SearchOutcome:
    """造一个成功的结果，字段可逐个覆盖。"""
    base = dict(
        ok=True,
        query="httpx 超时",
        provider="brave",
        results=(
            SearchResult("HTTPX Timeouts", "https://a.test/1", "几种超时的配法"),
            SearchResult("Stack Overflow", "https://b.test/2", "一个提问"),
        ),
        used=3,
        limit=50,
    )
    base.update(kw)
    return SearchOutcome(**base)


def _fail(failure: str, **kw) -> SearchOutcome:
    base = dict(
        ok=False, query="q", provider="brave", failure=failure, error="连接超时",
        used=50, limit=50,
    )
    base.update(kw)
    return SearchOutcome(**base)


class UntrustedMarkerTests(unittest.TestCase):
    """spec F19/AC25：结果在标记内，元信息在标记外。"""

    def test_results_inside_marker(self) -> None:
        text = render(_ok())
        open_tag = UNTRUSTED_OPEN.format(source="brave-search:httpx 超时")
        self.assertIn(open_tag, text)
        self.assertIn(UNTRUSTED_CLOSE, text)
        body_start = text.index(open_tag)
        body_end = text.index(UNTRUSTED_CLOSE)
        inner = text[body_start:body_end]
        self.assertIn("HTTPX Timeouts", inner)
        self.assertIn("https://a.test/1", inner)
        self.assertIn("几种超时的配法", inner)

    def test_meta_line_outside_marker(self) -> None:
        """
        ⚠ 用**字符串位置**比较，不能只断言「两者都出现了」。

        模型看到「结果：2 条」时必须能确定那是系统说的，而不是某条搜索结果的
        标题里写的——「都出现了」这种断言在元信息被塞进标记里时照样通过。
        """
        text = render(_ok())
        meta_pos = text.index("服务商：brave")
        open_pos = text.index(UNTRUSTED_OPEN.format(source="brave-search:httpx 超时"))
        self.assertLess(meta_pos, open_pos)

    def test_source_carries_provider_and_query(self) -> None:
        text = render(_ok(query="内部系统 排查"))
        self.assertIn('source="brave-search:内部系统 排查"', text)

    def test_zero_results_has_no_marker(self) -> None:
        """没有外部内容就不包标记——包个空壳会让模型以为里面有东西被吞了。"""
        text = render(_ok(results=()))
        self.assertNotIn(UNTRUSTED_CLOSE, text)
        self.assertIn("没有搜到任何结果", text)


class MetaLineTests(unittest.TestCase):
    """spec F23/AC29：四项元信息都读得出来。"""

    def test_all_four_fields(self) -> None:
        text = render(_ok())
        self.assertIn("httpx 超时", text)   # 原始查询词
        self.assertIn("brave", text)        # 服务商
        self.assertIn("2 条", text)         # 返回条数
        self.assertIn("3/50", text)         # 已用 / 上限

    def test_unlimited_quota_shown_as_text(self) -> None:
        self.assertIn("3/不限", render(_ok(limit=0)))

    def test_query_not_truncated_in_output(self) -> None:
        """回灌模型的正文里查询词是完整原文（截短只发生在单行摘要里）。"""
        long_query = "关于超时配置的" * 40
        self.assertIn(long_query, render(_ok(query=long_query)))


class FailureTextTests(unittest.TestCase):
    """spec F21/AC22：四类文案各不相同，且各自指向正确的下一步。"""

    def test_no_key_and_quota_say_do_not_retry(self) -> None:
        self.assertIn("不要重试", render(_fail(FAILURE_QUOTA)))
        # 无密钥那条用的是「重试不会成功」，语义相同、措辞不同（它还要说清是配置问题）。
        no_key = render(_fail(FAILURE_NO_KEY))
        self.assertIn("重试不会成功", no_key)
        self.assertIn("不是临时故障", no_key)

    def test_service_failure_allows_one_retry(self) -> None:
        """
        ⚠ 这一类**与上面两类刻意相反**：它是可能恢复的。

        三条文案如果长得差不多，模型会一律重试——而前两类重试一万次也不会成功，
        只会把剩下的迭代轮次全部烧光。
        """
        text = render(_fail(FAILURE_SERVICE))
        self.assertIn("重试一次", text)
        self.assertNotIn("不要重试", text)

    def test_three_texts_pairwise_different(self) -> None:
        texts = [render(_fail(f)) for f in (FAILURE_NO_KEY, FAILURE_QUOTA, FAILURE_SERVICE)]
        self.assertEqual(len(set(texts)), 3)

    def test_no_key_names_the_config_field(self) -> None:
        self.assertIn("search.api_key", render(_fail(FAILURE_NO_KEY)))

    def test_quota_shows_used_and_limit(self) -> None:
        self.assertIn("50/50", render(_fail(FAILURE_QUOTA)))

    def test_service_failure_carries_reason(self) -> None:
        self.assertIn("连接超时", render(_fail(FAILURE_SERVICE)))

    def test_failure_paths_have_no_untrusted_marker(self) -> None:
        """失败时没有任何外部内容，标记不该出现。"""
        for failure in (FAILURE_NO_KEY, FAILURE_QUOTA, FAILURE_SERVICE):
            with self.subTest(failure=failure):
                self.assertNotIn(UNTRUSTED_CLOSE, render(_fail(failure)))


class SummaryTests(unittest.TestCase):
    def test_success_summary(self) -> None:
        self.assertEqual(summary(_ok()), "搜索「httpx 超时」· 2 条 · 3/50")

    def test_zero_results_still_success_shaped(self) -> None:
        self.assertIn("0 条", summary(_ok(results=())))

    def test_failure_summaries_distinct(self) -> None:
        texts = {summary(_fail(f)) for f in (FAILURE_NO_KEY, FAILURE_QUOTA, FAILURE_SERVICE)}
        self.assertEqual(len(texts), 3)
        for text in texts:
            self.assertIn("失败", text)

    def test_long_query_clipped_in_summary_only(self) -> None:
        """
        单行摘要会截短，而这**不违反 spec F7**——F7 要的是「做决定的地方看得到
        全文」，那是确认面板与 Ctrl+O 展开态。单行摘要只有一行高。
        """
        long_query = "abcdefghij" * 10
        line = summary(_ok(query=long_query))
        self.assertIn("…", line)
        self.assertLess(len(line), len(long_query))

    def test_newline_in_query_folded(self) -> None:
        self.assertNotIn("\n", summary(_ok(query="第一行\n第二行")))


if __name__ == "__main__":
    unittest.main()
