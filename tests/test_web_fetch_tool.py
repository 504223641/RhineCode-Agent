"""工具层单测（web_fetch 扩展 T22，spec F1/F2/F3 / AC2/AC3）。"""

import unittest

from rhinecode.tools.web_fetch import WebFetchTool


class _StubManager:
    def __init__(self, result=(True, "输出正文", "摘要"), raises=None):
        self.result = result
        self.raises = raises
        self.calls: list[tuple[str, str]] = []

    def fetch_and_extract(self, url, ask):
        self.calls.append((url, ask))
        if self.raises is not None:
            raise self.raises
        return self.result


class SchemaTests(unittest.TestCase):
    def test_only_url_and_prompt(self) -> None:
        """
        **只取不发**（spec F2 / AC2）：参数表里不存在请求方法、请求体、请求头三类入口。

        这条钉住的是「模型能主动往外发送的数据」的上界。
        """
        props = WebFetchTool.parameters["properties"]
        self.assertEqual(set(props), {"url", "prompt"})
        for forbidden in ("method", "body", "data", "headers", "cookies", "auth", "json"):
            self.assertNotIn(forbidden, props, forbidden)

    def test_both_required(self) -> None:
        self.assertEqual(set(WebFetchTool.parameters["required"]), {"url", "prompt"})

    def test_not_read_only(self) -> None:
        """
        声明为非只读（spec F3）。

        它会向外部主机发起真实请求，该请求本身即是一次对外可观测的行为，
        不满足「只读工具默认放行」的前提。改成只读会让它在放行档下被直接放行，
        与 F7 的整个设计冲突。
        """
        self.assertFalse(WebFetchTool.read_only)

    def test_description_states_key_constraints(self) -> None:
        d = WebFetchTool.description
        self.assertIn("GET only", d)
        self.assertIn("UNTRUSTED", d)

    def test_excluded_from_readonly_schemas(self) -> None:
        """
        非只读 ⇒ Plan Mode 规划阶段看不到它（spec AC3）。

        连带后果是「规划期查不了在线文档」，这是被接受的取舍。
        """
        from rhinecode.tools.registry import ToolRegistry

        reg = ToolRegistry()
        reg.register(WebFetchTool(_StubManager()))
        names = [s["function"]["name"] for s in reg.readonly_schemas()]
        self.assertNotIn("web_fetch", names)
        self.assertIn("web_fetch", [s["function"]["name"] for s in reg.schemas()])


class ExecuteTests(unittest.TestCase):
    def test_happy_path(self) -> None:
        m = _StubManager((True, "正文", "抓取 a.test · 1.0K · 抽取成功"))
        r = WebFetchTool(m).execute({"url": "https://a.test/x", "prompt": "讲了什么"})
        self.assertTrue(r.ok)
        self.assertEqual(r.output, "正文")
        self.assertEqual(r.summary, "抓取 a.test · 1.0K · 抽取成功")
        self.assertEqual(m.calls, [("https://a.test/x", "讲了什么")])

    def test_missing_url(self) -> None:
        r = WebFetchTool(_StubManager()).execute({"prompt": "q"})
        self.assertFalse(r.ok)
        self.assertIn("url", r.output)

    def test_missing_prompt(self) -> None:
        r = WebFetchTool(_StubManager()).execute({"url": "https://a.test/x"})
        self.assertFalse(r.ok)
        self.assertIn("prompt", r.output)

    def test_blank_values_rejected(self) -> None:
        r = WebFetchTool(_StubManager()).execute({"url": "   ", "prompt": "  "})
        self.assertFalse(r.ok)

    def test_extra_params_have_no_effect(self) -> None:
        """
        传入多余参数不产生任何效果（spec AC2 后半句）。

        模型可能凭先验塞一个 method/headers 进来——它们必须被静默忽略，
        而不是意外地被透传出去。
        """
        m = _StubManager()
        r = WebFetchTool(m).execute(
            {
                "url": "https://a.test/x",
                "prompt": "q",
                "method": "POST",
                "headers": {"Authorization": "Bearer secret"},
                "body": "payload",
            }
        )
        self.assertTrue(r.ok)
        # manager 只收到 (url, ask) 两项，多余参数在工具层就被丢掉了。
        self.assertEqual(m.calls, [("https://a.test/x", "q")])

    def test_manager_exception_becomes_failed_result(self) -> None:
        m = _StubManager(raises=RuntimeError("炸了"))
        r = WebFetchTool(m).execute({"url": "https://a.test/x", "prompt": "q"})
        self.assertFalse(r.ok)
        self.assertIn("炸了", r.output)

    def test_non_dict_args(self) -> None:
        r = WebFetchTool(_StubManager()).execute(None)  # type: ignore[arg-type]
        self.assertFalse(r.ok)


class ResultOkReflectsFetchOutcomeTests(unittest.TestCase):
    """
    `ToolResult.ok` 必须反映「这次抓取有没有拿到内容」，不是「函数有没有抛异常」。

    ⚠ **这条是真实模型端到端跑场景 6 时发现的缺陷。** 当时那台机器的 DNS 把公网域名
    解析成 10.x 内网地址，连接期守卫正确拦下了——**而工具层报了 ok=True**，
    TUI 会把一次失败的抓取显示成绿色成功、只是正文里写着「抓取失败」。界面在撒谎。
    """

    def test_failed_fetch_reports_not_ok(self) -> None:
        m = _StubManager((False, "[web_fetch] 抓取失败：…", "抓取 x.test 失败"))
        r = WebFetchTool(m).execute({"url": "https://x.test/a", "prompt": "q"})
        self.assertFalse(r.ok, "抓取失败时 ToolResult.ok 必须是 False")

    def test_cross_host_redirect_reports_not_ok(self) -> None:
        # 跨主机重定向本次确实没抓到内容，模型需要对新地址再发一次。
        m = _StubManager((False, "[web_fetch] 未抓取：… 重定向到了另一个主机", "…未抓取"))
        r = WebFetchTool(m).execute({"url": "https://a.test/1", "prompt": "q"})
        self.assertFalse(r.ok)

    def test_successful_fetch_reports_ok(self) -> None:
        m = _StubManager((True, "正文", "摘要"))
        self.assertTrue(WebFetchTool(m).execute({"url": "https://a.test/x", "prompt": "q"}).ok)


if __name__ == "__main__":
    unittest.main()
