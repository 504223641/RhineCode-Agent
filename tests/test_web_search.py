"""搜索纯逻辑单测（web_search 扩展 T4，spec F1/F17/F18 · AC1/AC23）。

全程零 IO：本文件不发请求、不做域名解析、不读文件。
"""

import unittest

from rhinecode.web.search import (
    BRAVE,
    DEFAULT_COUNT,
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


class ProviderTableTests(unittest.TestCase):
    def test_only_one_provider_implemented(self) -> None:
        """
        spec「不做的事」：**只实现一家**。

        这条不是限制未来，而是钉住「没有第二个真实实现来检验的抽象容易设计成
        只适配想象」这个判断——加第二家时改它，顺便被迫回头看一眼那个接缝。
        """
        self.assertEqual(sorted(PROVIDERS), ["brave"])

    def test_provider_fields_present(self) -> None:
        self.assertTrue(BRAVE.endpoint.startswith("https://"))
        self.assertTrue(BRAVE.auth_header)
        self.assertIsNone(check_endpoint(BRAVE.endpoint))


if __name__ == "__main__":
    unittest.main()
