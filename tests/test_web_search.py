"""搜索纯逻辑单测（web_search 扩展 T4，spec F1/F17/F18 · AC1/AC23）。

全程零 IO：本文件不发请求、不做域名解析、不读文件。
"""

import unittest

from rhinecode.web.search import (
    BOCHA,
    BRAVE,
    DEFAULT_COUNT,
    DEFAULT_PROVIDER,
    MAX_COUNT,
    MIN_COUNT,
    PROVIDERS,
    check_endpoint,
    normalize_count,
)


class NormalizeCountTests(unittest.TestCase):
    """条数规范化：越界只夹取、非法只回退，**任何输入都不抛异常**（spec F1）。"""

    def test_missing_takes_default(self) -> None:
        self.assertEqual(normalize_count(None), DEFAULT_COUNT)

    def test_below_range_clamped_to_min(self) -> None:
        self.assertEqual(normalize_count(0), MIN_COUNT)
        self.assertEqual(normalize_count(-1), MIN_COUNT)

    def test_above_range_clamped_to_max(self) -> None:
        self.assertEqual(normalize_count(99), MAX_COUNT)

    def test_numeric_string_accepted(self) -> None:
        self.assertEqual(normalize_count("3"), 3)

    def test_float_truncated_into_range(self) -> None:
        self.assertEqual(normalize_count(3.7), 3)

    def test_garbage_takes_default(self) -> None:
        self.assertEqual(normalize_count("x"), DEFAULT_COUNT)
        self.assertEqual(normalize_count([1, 2]), DEFAULT_COUNT)

    def test_bool_takes_default_not_one(self) -> None:
        """
        ⚠ `bool` 是 `int` 的子类，夹取会把 `True` 变成 1。

        那会让模型拿到一条结果、还以为自己要到的就是一条——传布尔进来是明显的
        类型错误，退回缺省比夹成 1 更诚实。这条断言正面钉住那个选择。
        """
        self.assertEqual(normalize_count(True), DEFAULT_COUNT)
        self.assertEqual(normalize_count(False), DEFAULT_COUNT)

    def test_custom_default_used(self) -> None:
        """装配层会把配置里的 `search.max_results` 作为 default 传进来。"""
        self.assertEqual(normalize_count(None, default=8), 8)

    def test_out_of_range_default_is_also_clamped(self) -> None:
        """
        ⚠ **回退值也要夹取。** 实现期实测撞到的一个真缺口：

        `default` 来自配置里的 `search.max_results`，而那一项走「非法值回退默认」
        的宽松口径——用户完全可以写 `max_results: 99`。不夹的话，模型**不指定
        条数**时（最常见的情况）反而会把 99 原样发给服务商：一条越界值从
        「用户指定」这条路被挡住，却从「缺省」这条路溜了出去。
        """
        self.assertEqual(normalize_count(None, default=99), MAX_COUNT)
        self.assertEqual(normalize_count(None, default=0), MIN_COUNT)
        self.assertEqual(normalize_count(None, default=-5), MIN_COUNT)
        self.assertEqual(normalize_count("x", default=99), MAX_COUNT)
        self.assertEqual(normalize_count(True, default=99), MAX_COUNT)

    def test_garbage_default_falls_back_to_builtin(self) -> None:
        self.assertEqual(normalize_count(None, default="x"), DEFAULT_COUNT)


class CheckEndpointTests(unittest.TestCase):
    """端点校验：只判协议与主机名，**不判地址范围**（spec F11/F17）。"""

    def test_https_ok(self) -> None:
        self.assertIsNone(check_endpoint("https://api.example.com/v1/search"))

    def test_http_ok(self) -> None:
        """非 https 是合法的——内网搜索代理常常是 http，警告由装配层出。"""
        self.assertIsNone(check_endpoint("http://搜索代理.internal/v1"))

    def test_private_address_ok(self) -> None:
        """
        ⚠ 端点**刻意不过②′层的地址范围校验**（spec F11）。

        它是用户在配置里写死的，不是模型指定的地址；过②′的后果是
        「用户配一个内网搜索代理就用不了」。这条是那个决定的正面反证——
        谁日后把 `is_forbidden_address` 加进来，它会红。
        """
        self.assertIsNone(check_endpoint("http://127.0.0.1:8080/search"))
        self.assertIsNone(check_endpoint("https://10.1.2.3/search"))

    def test_file_scheme_rejected(self) -> None:
        reason = check_endpoint("file:///etc/passwd")
        self.assertIsNotNone(reason)
        self.assertIn("协议", reason)

    def test_other_scheme_rejected(self) -> None:
        self.assertIsNotNone(check_endpoint("ftp://a.example.com/x"))

    def test_missing_host_rejected(self) -> None:
        self.assertIsNotNone(check_endpoint("https:///only/path"))

    def test_empty_rejected(self) -> None:
        self.assertIsNotNone(check_endpoint(""))
        self.assertIsNotNone(check_endpoint(None))


class BraveParamsTests(unittest.TestCase):
    def test_params_shape(self) -> None:
        self.assertEqual(BRAVE.build_params("httpx timeout", 3), {"q": "httpx timeout", "count": 3})

    def test_query_not_rewritten(self) -> None:
        """
        spec F8：**不做任何查询词过滤或改写**。

        一条含项目内部标识符的查询词必须原样进入请求参数——判断「这段文字能不能
        发出去」是 C16 分类器的职责，不是这里。
        """
        query = "RhineCode PermissionEngine decide AttributeError"
        self.assertEqual(BRAVE.build_params(query, 5)["q"], query)


class BraveParseTests(unittest.TestCase):
    """响应解析：防御式 + **三态**（spec F18/F21）。"""

    def test_normal(self) -> None:
        payload = {
            "web": {
                "results": [
                    {"title": "T1", "url": "https://a.test/1", "description": "D1"},
                    {"title": "T2", "url": "https://b.test/2", "description": "D2"},
                    {"title": "T3", "url": "https://c.test/3", "description": "D3"},
                ]
            }
        }
        results = BRAVE.parse(payload)
        self.assertEqual(len(results), 3)
        self.assertEqual(results[0].title, "T1")
        self.assertEqual(results[1].url, "https://b.test/2")
        self.assertEqual(results[2].snippet, "D3")

    def test_missing_optional_fields_kept(self) -> None:
        """只要有地址，一条结果就是有用的路标；标题与摘要缺失只留空。"""
        results = BRAVE.parse({"web": {"results": [{"url": "https://a.test/1"}]}})
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].title, "")
        self.assertEqual(results[0].snippet, "")

    def test_malformed_entries_skipped_not_raised(self) -> None:
        """单条畸形只跳过那一条，其余照常，**绝不抛异常**。"""
        payload = {
            "web": {
                "results": [
                    {"title": "好的", "url": "https://a.test/1", "description": "D"},
                    "不是对象",
                    {"title": "没有地址"},
                    {"url": "   "},
                ]
            }
        }
        results = BRAVE.parse(payload)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].url, "https://a.test/1")

    def test_unknown_structure_returns_none(self) -> None:
        """
        ⚠ **三态反证的上半：结构不认识返回 `None`，不是 `[]`。**

        断言用 `assertIsNone` 而**不是** `assertFalse`——`None` 与 `[]` 都是假值，
        写布尔断言的话这条护栏什么也钉不住，而它正是本扩展最容易写错的地方。
        """
        for payload in (
            "垃圾",
            None,
            123,
            {},
            {"web": "不是对象"},
            {"web": {}},
            {"web": {"results": "不是列表"}},
        ):
            with self.subTest(payload=payload):
                self.assertIsNone(BRAVE.parse(payload))

    def test_真的没搜到_returns_empty_list(self) -> None:
        """
        ⚠ **三态反证的下半：结构认得、`results` 为空 → 返回 `[]`。**

        它与上一条的区别决定了 spec F21 里完全相反的两种判定：
        `None` 是失败、**不计配额**；`[]` 是成功 0 条、**计配额**。
        合并两者的净效果是一次解析故障伪装成「这个词搜不到」——
        模型会去换关键词反复重试，而根因在字段名对不上。
        """
        result = BRAVE.parse({"web": {"results": []}})
        self.assertIsNotNone(result)
        self.assertEqual(result, [])


class BochaParamsTests(unittest.TestCase):
    def test_params_shape(self) -> None:
        """
        博查是 POST + JSON body，字段名与 Brave 完全不同。

        `summary: True` 不可省——不带它只返回一小段 `snippet`，
        而一句话的摘要判断不了「这条值不值得再去抓」。
        """
        body = BOCHA.build_params("httpx 超时", 3)
        self.assertEqual(body["query"], "httpx 超时")
        self.assertEqual(body["count"], 3)
        self.assertIs(body["summary"], True)
        self.assertEqual(body["freshness"], "noLimit")

    def test_query_not_rewritten(self) -> None:
        query = "RhineCode PermissionEngine decide AttributeError"
        self.assertEqual(BOCHA.build_params(query, 5)["query"], query)


class BochaParseTests(unittest.TestCase):
    """
    ⚠ **顶层包装两种形态都认**（`_bocha_parse` 的 docstring 有完整理由）。

    官方文档在飞书需登录，公开渠道拿不到完整的响应示例——只能确认它是 Bing
    兼容形态（`webPages.value[]`），而**有没有一层 `data` 包装没能核实**。
    两种都认是把一个**已知的不确定性**处理掉，不是加料；真正的兜底仍是三态。
    """

    _ENTRY = {
        "name": "HTTPX Timeouts",
        "url": "https://www.python-httpx.org/advanced/timeouts/",
        "summary": "较完整的摘要",
        "snippet": "一小段",
        "siteName": "python-httpx.org",
        "datePublished": "2026-01-01",
    }

    def test_with_data_wrapper(self) -> None:
        payload = {"code": 200, "log_id": "x", "data": {"webPages": {"value": [self._ENTRY]}}}
        results = BOCHA.parse(payload)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].title, "HTTPX Timeouts")
        self.assertEqual(results[0].url, self._ENTRY["url"])

    def test_without_data_wrapper(self) -> None:
        payload = {"_type": "SearchResponse", "webPages": {"value": [self._ENTRY]}}
        self.assertEqual(len(BOCHA.parse(payload)), 1)

    def test_summary_preferred_over_snippet(self) -> None:
        """`summary` 是带 `summary: true` 才有的较完整摘要，优先取长的那个。"""
        self.assertEqual(BOCHA.parse({"webPages": {"value": [self._ENTRY]}})[0].snippet, "较完整的摘要")

    def test_falls_back_to_snippet(self) -> None:
        entry = {k: v for k, v in self._ENTRY.items() if k != "summary"}
        self.assertEqual(BOCHA.parse({"webPages": {"value": [entry]}})[0].snippet, "一小段")

    def test_both_missing_leaves_empty(self) -> None:
        """一条只有标题和地址的结果仍然是有用的路标。"""
        entry = {"name": "T", "url": "https://a.test/1"}
        self.assertEqual(BOCHA.parse({"webPages": {"value": [entry]}})[0].snippet, "")

    def test_malformed_entries_skipped(self) -> None:
        payload = {"webPages": {"value": [self._ENTRY, "不是对象", {"name": "没地址"}, {"url": "  "}]}}
        self.assertEqual(len(BOCHA.parse(payload)), 1)

    def test_unknown_structure_returns_none(self) -> None:
        """三态上半：用 `assertIsNone` 而不是 `assertFalse`（两者都是假值）。"""
        for payload in ("垃圾", None, 123, {}, {"data": "不是对象"},
                        {"webPages": "不是对象"}, {"webPages": {"value": "不是列表"}},
                        {"data": {"webPages": {}}}):
            with self.subTest(payload=payload):
                self.assertIsNone(BOCHA.parse(payload))

    def test_真的没搜到_returns_empty_list(self) -> None:
        """三态下半：结构认得、`value` 为空 → `[]`（成功 0 条、计配额）。"""
        for payload in ({"webPages": {"value": []}}, {"data": {"webPages": {"value": []}}}):
            with self.subTest(payload=payload):
                result = BOCHA.parse(payload)
                self.assertIsNotNone(result)
                self.assertEqual(result, [])


class ProviderTableTests(unittest.TestCase):
    def test_provider_table_matches_config_whitelist(self) -> None:
        """
        ⚠ **成对维护点**：`config._KNOWN_PROVIDERS` ↔ 本模块的 `PROVIDERS`。

        `config.py` 刻意**不 import** 本模块（配置层依赖能力层是层级倒挂），
        代价是那份名单要手工同步。漏改的表现是「配置里写了新服务商，
        启动时说不认识」——会当场报错、不会静默，但这条断言让它在开发期就红。
        """
        from rhinecode.config import _KNOWN_PROVIDERS

        self.assertEqual(sorted(_KNOWN_PROVIDERS), sorted(PROVIDERS))

    def test_default_provider_is_reachable_domestically(self) -> None:
        """
        ⚠ **缺省是博查，不是 Brave**，这是刻意的。

        本项目是中文、只针对 DeepSeek、主力网络环境连不通境外服务
        （web_fetch 验收记录遗留项 #3：本机 DNS 把公网域名重写成 10.x）。
        **把缺省定在一个连不上的服务商上是个坏缺省。**
        """
        self.assertEqual(DEFAULT_PROVIDER, "bocha")
        self.assertIn(DEFAULT_PROVIDER, PROVIDERS)

    def test_all_providers_well_formed(self) -> None:
        for name, spec in PROVIDERS.items():
            with self.subTest(provider=name):
                self.assertEqual(spec.name, name)
                self.assertTrue(spec.endpoint.startswith("https://"))
                self.assertTrue(spec.auth_header)
                self.assertIn(spec.method.upper(), ("GET", "POST"))
                self.assertIsNone(check_endpoint(spec.endpoint))

    def test_two_transports_both_present(self) -> None:
        """
        接第二家时补的那一格。**两种传输都要有真实实例**——
        只剩一种时 `method` 那个字段又会退化成「看不出有没有用」的冗余。
        """
        methods = {spec.method.upper() for spec in PROVIDERS.values()}
        self.assertEqual(methods, {"GET", "POST"})

    def test_bearer_prefix_only_where_needed(self) -> None:
        self.assertEqual(BOCHA.auth_prefix, "Bearer ")
        self.assertEqual(BRAVE.auth_prefix, "")


if __name__ == "__main__":
    unittest.main()
