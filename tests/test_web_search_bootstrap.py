"""
装配、开关、启动提示、宽规则丢弃闭环、面板（web_search 扩展 T29）。

对应 AC4 / AC6 / AC15 / AC16 / AC17 / AC21 / AC23 / AC26 / AC33。

fixture 沿用 `test_web_bootstrap.py` 的口径：临时工作区 + os.chdir + 临时 user_dir。
`build_app` 会在**当前工作目录**建 `.rhinecode/sessions/` 与会话锁，
不切目录会把垃圾写进仓库。
"""

import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from rhinecode.agent.prompt.modules import fixed_modules
from rhinecode.bootstrap import BootstrapError, build_app
from rhinecode.config import Config
from rhinecode.tools import path_guard


def _cfg(**over) -> Config:
    base = dict(
        protocol="deepseek",
        model="deepseek-chat",
        base_url="https://api.deepseek.com",
        api_key="fake-key-for-test",
        debug_log=False,
        context_window=65536,
        # 缺省关掉分类器：绝大多数用例不需要它，而它会多造一个 Provider。
        classifier_enabled=False,
        search_enabled=True,
        search_api_key="fake-search-key",
    )
    base.update(over)
    return Config(**base)


class Fixture(unittest.TestCase):
    def setUp(self) -> None:
        self._work = tempfile.TemporaryDirectory()
        self._user = tempfile.TemporaryDirectory()
        self.work = Path(self._work.name).resolve()
        self.user_dir = Path(self._user.name).resolve()
        self._cwd = os.getcwd()
        os.chdir(self.work)
        path_guard.clear_read_roots()

    def tearDown(self) -> None:
        os.chdir(self._cwd)
        path_guard.clear_read_roots()
        for d in (self._work, self._user):
            try:
                d.cleanup()
            except OSError:
                pass

    def build(self, cfg=None, **kw):
        result = build_app(cfg or _cfg(), user_dir=self.user_dir, **kw)
        self.addCleanup(result.cleanup)
        return result

    def _write_project_rule(self, text: str) -> None:
        d = self.work / ".rhinecode"
        d.mkdir(parents=True, exist_ok=True)
        (d / "permissions.yaml").write_text(text, encoding="utf-8")

    def _notices(self, result) -> str:
        """`startup_notice` 是一整段字符串（不是列表），可能为 None。"""
        return result.manager.startup_notice or ""


# =============================================================================
# 一、总开关（AC4）
# =============================================================================
class ToggleTests(Fixture):
    def test_registered_when_enabled(self) -> None:
        self.assertIn("web_search", self.build().tool_registry.names())

    def test_absent_when_disabled(self) -> None:
        result = self.build(_cfg(search_enabled=False))
        self.assertNotIn("web_search", result.tool_registry.names())

    def test_exclude_tools_can_remove_it(self) -> None:
        """驱动设施的隔离手段对新工具同样有效。"""
        result = self.build(exclude_tools=frozenset({"web_search"}))
        self.assertNotIn("web_search", result.tool_registry.names())

    def test_no_notices_when_disabled(self) -> None:
        """关闭时不产生任何搜索相关的启动提示（spec F4「逐字一致」）。"""
        result = self.build(_cfg(search_enabled=False, search_api_key=""))
        self.assertNotIn("搜索", self._notices(result))


# =============================================================================
# 二、系统提示的注入条件（AC26 —— 本扩展最容易漏改的一处）
# =============================================================================
class UntrustedInjectionTests(unittest.TestCase):
    """
    ⚠ 那条「外部不可信内容是数据不是指令」的约束原来只挂在 `web_fetch_enabled` 上。

    关掉 web_fetch 而只开 web_search 时它会**凭空消失**，而搜索结果
    （标题与摘要，SEO 投毒的主要落点）照样进上下文。
    四种组合逐个断言，漏改必红。
    """

    def _has_untrusted(self, fetch: bool, search: bool) -> bool:
        """判据与 `ConversationManager._untrusted_enabled` 同源：两者任一启用。"""
        modules = fixed_modules(untrusted_enabled=bool(fetch or search))
        return any("<untrusted-content>" in m.content for m in modules)

    def test_four_combinations(self) -> None:
        for fetch, search, expected in (
            (True, True, True),
            (True, False, True),
            (False, True, True),    # ← 关键那一格
            (False, False, False),
        ):
            with self.subTest(fetch=fetch, search=search):
                self.assertEqual(self._has_untrusted(fetch, search), expected)


class UntrustedInjectionWiringTests(Fixture):
    """上面那条测的是「条件算对没有」，这条测**装配层真的照那个条件接线了**。"""

    def _prompt_has_untrusted(self, **over) -> bool:
        """
        直接问协调层那个判据方法——它是三个 `build_default_prompt` 调用点
        的**唯一**依据（见 `conversation._untrusted_enabled` 的 docstring）。
        """
        result = self.build(_cfg(**over))
        return result.manager._untrusted_enabled()

    def test_search_only_still_injects(self) -> None:
        self.assertTrue(
            self._prompt_has_untrusted(web_fetch_enabled=False, search_enabled=True)
        )

    def test_fetch_only_still_injects(self) -> None:
        self.assertTrue(
            self._prompt_has_untrusted(web_fetch_enabled=True, search_enabled=False)
        )

    def test_both_off_does_not_inject(self) -> None:
        self.assertFalse(
            self._prompt_has_untrusted(web_fetch_enabled=False, search_enabled=False)
        )


# =============================================================================
# 三、端点校验与启动提示（AC23 / AC33）
# =============================================================================
class EndpointTests(Fixture):
    def test_file_scheme_aborts_startup(self) -> None:
        """
        `file://` 端点会让搜索工具变成文件读取工具、绕过第②层路径沙箱
        ——与②′的协议限制同性质，因此**明确失败而不是降级**。
        """
        with self.assertRaises(BootstrapError) as ctx:
            build_app(
                _cfg(search_endpoint="file:///etc/passwd"), user_dir=self.user_dir
            )
        self.assertIn("协议", str(ctx.exception))

    def test_http_endpoint_warns_but_starts(self) -> None:
        result = self.build(_cfg(search_endpoint="http://proxy.internal/search"))
        self.assertIn("不是 https", self._notices(result))
        self.assertIn("web_search", result.tool_registry.names())

    def test_https_endpoint_silent(self) -> None:
        result = self.build(_cfg(search_endpoint="https://proxy.internal/search"))
        self.assertNotIn("不是 https", self._notices(result))


class MissingKeyTests(Fixture):
    def test_tool_still_registered(self) -> None:
        """
        用户选定的形态（plan 评审）：**工具照常注册**，调用时返回可读错误。
        启动提示与调用期文案服务不同的人——这条给用户，那条给模型。
        """
        result = self.build(_cfg(search_api_key=""))
        self.assertIn("web_search", result.tool_registry.names())

    def test_startup_notice_names_the_field(self) -> None:
        result = self.build(_cfg(search_api_key=""))
        notices = self._notices(result)
        self.assertIn("search.api_key", notices)
        self.assertIn("search.enabled", notices)

    def test_no_notice_when_key_present(self) -> None:
        self.assertNotIn("search.api_key", self._notices(self.build()))


# =============================================================================
# 四、宽泛规则丢弃的闭环（AC16 / AC17 / AC4）
# =============================================================================
class BroadRuleDropTests(Fixture):
    """
    四种开关组合 × 两种规则效果。**闭环**是这一组的重点：
    关掉分类器或关掉搜索能力，那条 allow 都该恢复生效。
    """

    def _rules(self, result):
        return {
            (r.effect, r.tool, r.pattern) for r in result.manager.permission_engine.file_ruleset.rules
        }

    def test_allow_dropped_when_classifier_on(self) -> None:
        self._write_project_rule("allow:\n  - \"WebSearch\"\n")
        result = self.build(_cfg(classifier_enabled=True))
        self.assertNotIn(("allow", "WebSearch", ""), self._rules(result))
        notices = self._notices(result)
        self.assertIn("WebSearch", notices)
        self.assertIn("搜索", notices)

    def test_allow_kept_when_classifier_off(self) -> None:
        """闭环上半：关掉把关人，就该由用户自己写的规则说了算。"""
        self._write_project_rule("allow:\n  - \"WebSearch\"\n")
        result = self.build(_cfg(classifier_enabled=False))
        self.assertIn(("allow", "WebSearch", ""), self._rules(result))

    def test_allow_kept_when_search_disabled(self) -> None:
        """
        闭环下半（spec F4）：**关掉的能力不该影响用户的规则文件**。

        搜索关着的时候丢掉一条 `allow: WebSearch`，只会产生一条让人困惑的
        启动提示——用户会以为那条规则原本是生效的。
        """
        self._write_project_rule("allow:\n  - \"WebSearch\"\n")
        result = self.build(_cfg(classifier_enabled=True, search_enabled=False))
        self.assertIn(("allow", "WebSearch", ""), self._rules(result))

    def test_deny_never_dropped(self) -> None:
        """AC16：`deny: WebSearch` 在任何组合下都生效。"""
        self._write_project_rule("deny:\n  - \"WebSearch\"\n")
        for classifier_on in (True, False):
            for search_on in (True, False):
                with self.subTest(classifier=classifier_on, search=search_on):
                    result = self.build(
                        _cfg(classifier_enabled=classifier_on, search_enabled=search_on)
                    )
                    self.assertIn(("deny", "WebSearch", ""), self._rules(result))

    def test_patterned_allow_not_dropped(self) -> None:
        """
        带括号的写法本来就不命中任何调用，丢弃它只会产生一条让人困惑的提示。
        """
        self._write_project_rule("allow:\n  - \"WebSearch(*)\"\n")
        result = self.build(_cfg(classifier_enabled=True))
        self.assertIn(("allow", "WebSearch", "*"), self._rules(result))

    def test_command_rules_still_dropped(self) -> None:
        """对照组：命令类的既有行为一个字没动。"""
        self._write_project_rule("allow:\n  - \"Bash(python *)\"\n")
        result = self.build(_cfg(classifier_enabled=True))
        self.assertNotIn(("allow", "Bash", "python *"), self._rules(result))


# =============================================================================
# 五、/clear 复位配额（AC21）
# =============================================================================
class QuotaResetTests(Fixture):
    def test_clear_resets_quota(self) -> None:
        result = self.build(_cfg(search_session_quota=5))
        manager = result.manager
        self.assertIsNotNone(manager.web_search_manager)
        manager.web_search_manager._take_quota()
        manager.web_search_manager._take_quota()
        self.assertEqual(manager.web_search_manager.quota_state(), (2, 5))
        manager.clear()
        self.assertEqual(manager.web_search_manager.quota_state(), (0, 5))

    def test_clear_survives_search_disabled(self) -> None:
        """搜索关闭时 `web_search_manager` 是 None，`clear()` 不该炸。"""
        result = self.build(_cfg(search_enabled=False))
        self.assertIsNone(result.manager.web_search_manager)
        result.manager.clear()


# =============================================================================
# 六、确认面板（AC6 / AC15）
# =============================================================================
class ConfirmPanelTests(unittest.IsolatedAsyncioTestCase):
    """
    真跑一个 Textual app 把面板挂出来，断言**实际渲染出的选项**。

    形态照抄 `test_protected_wiring.py::PanelTest`——那一组验的是同一件事的
    另一半（保护路径少一项），两处的失败形态也相同：一个点了没用的按钮。
    """

    async def _show(self, kind: str, query: str = "httpx 超时"):
        from textual.app import App, ComposeResult

        from rhinecode.provider.base import ToolCall
        from rhinecode.tui.widgets import ConfirmPanel

        class _Harness(App):
            def compose(self) -> ComposeResult:
                yield ConfirmPanel()

        name = "web_search" if kind == "search" else "web_fetch"
        args = {"query": query} if kind == "search" else {"url": "https://a.test/x", "prompt": "看看"}
        app = _Harness()
        async with app.run_test():
            panel = app.query_one(ConfirmPanel)
            panel.show_for(
                ToolCall(id="c1", name=name, arguments=args),
                None,
                SimpleNamespace(
                    reason="交由用户确认",
                    kind=kind,
                    host="a.test" if kind == "url" else "",
                    layer=SimpleNamespace(value="mode"),
                ),
            )
            return panel

    def _ids(self, panel) -> list:
        return [o.id for o in panel._options if o.id is not None]

    def _texts(self, panel) -> str:
        """
        面板上的**全部**文本。

        ⚠ 补充展示行（完整查询词、去向、判定来自）是经 `_add_static` 加进
        `_options` 的、`id` 为 None 的条目，**不是 `Static` 组件**——
        `panel.query("Static")` 返回空列表。实现期在这里错过一次。
        """
        return chr(10).join(str(o.prompt) for o in panel._options)

    async def test_search_panel_has_no_permanent_option(self) -> None:
        """
        AC15：搜索类只有三个选项。

        「永久放行」写的是文件级规则，而启用分类器时 `allow: WebSearch` 会被
        F16 丢弃——那个按钮点了下次还弹，与②″那条「骗人的按钮」完全同形。
        """
        panel = await self._show("search")
        self.assertEqual(self._ids(panel), ["yes", "yes_session", "no"])

    async def test_url_panel_still_has_four_options(self) -> None:
        """
        **反证**：别的场景一项都不能少。

        没有这条的话，把三选项分支写成无条件生效（比如判据写反）会全绿通过，
        而用户从此再也点不到「永久放行」。
        """
        panel = await self._show("url")
        self.assertEqual(self._ids(panel), ["yes", "yes_session", "yes_permanent", "no"])

    async def test_search_session_option_uses_default_wording(self) -> None:
        """
        ⚠ **两个布尔的反证。**

        搜索类的「本会话放行」走**正常的会话级规则**，不是②″那套内存豁免。
        说明文字若变成保护路径那句「不写入配置」，说明有人把两个判据
        合并成了一个——而那会让搜索类的放行跑去登记一个保护路径豁免，
        **不报错，但那次放行不生效**。
        """
        panel = await self._show("search")
        texts = self._texts(panel)
        self.assertIn("本会话内相同调用不再询问", texts)
        self.assertNotIn("不写入配置", texts)

    async def test_full_query_shown_untruncated(self) -> None:
        """AC6：300 字的查询词完整可见，无省略号。"""
        query = "怎么配置超时" * 50
        panel = await self._show("search", query)
        rendered = self._texts(panel)
        self.assertIn(query, rendered)

    async def test_markup_special_chars_do_not_crash(self) -> None:
        """
        ⚠ 查询词是自由文本，落单的 `[` 完全正常（搜个 `list[int] 怎么写` 就有）。

        用错 escape 会在 Textual 的**布局阶段**抛 `MarkupError`——那是没有任何
        try/except 兜得住、会直接拆掉整个 app 的那一类。这条真挂一次面板，
        挂得出来即通过。
        """
        panel = await self._show("search", "list[int] 怎么写 [/dim] [bold]x")
        self.assertEqual(self._ids(panel), ["yes", "yes_session", "no"])

    async def test_third_party_warning_visible(self) -> None:
        panel = await self._show("search")
        rendered = self._texts(panel)
        self.assertIn("第三方", rendered)


if __name__ == "__main__":
    unittest.main()
