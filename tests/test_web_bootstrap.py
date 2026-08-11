"""
装配、开关传递链、系统提示模块、确认面板（web_fetch 扩展 T21/T25/T26/T28）。

fixture 沿用 `test_bootstrap.py` 的口径：临时工作区 + os.chdir + 临时 user_dir +
finally 里 cleanup。`build_app` 会在**当前工作目录**建 `.rhinecode/sessions/` 与
会话锁，不切目录会把垃圾写进仓库。
"""

import os
import tempfile
import unittest
from pathlib import Path

from rhinecode.agent.prompt.builder import build_default_prompt
from rhinecode.agent.prompt.environment import EnvironmentInfo
from rhinecode.agent.prompt.modules import fixed_modules
from rhinecode.bootstrap import build_app
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
    )
    base.update(over)
    return Config(**base)


def _env() -> EnvironmentInfo:
    return EnvironmentInfo(
        working_dir="/tmp/x",
        platform="win32",
        date="2026-07-29",
        git_branch="main",
        model="deepseek-chat",
        protocol="deepseek",
    )


# =============================================================================
# T21：不可信内容的系统提示模块
# =============================================================================
class UntrustedPromptModuleTests(unittest.TestCase):
    def test_disabled_by_default_is_byte_identical(self) -> None:
        """
        缺省不注入，输出与本扩展之前**逐字一致**（spec F4）。

        `fixed_modules()` 无参调用是既有全部调用点的形态。

        ⚠ 判据是「开关关闭时这个模块不在」+ 下一条的「开关是二者唯一差异」，
        **刻意不断言模块总数**：写死个数表达的是同一件事，但任何一次无关的
        新增模块都会把它撞碎（prompt-hardening 加「交付标准」时真撞了），
        而那种失败读起来像是不可信内容模块出了问题，指向完全错误的方向。
        """
        names = [m.name for m in fixed_modules()]
        self.assertNotIn("外部不可信内容", names)

    def test_enabled_inserts_only_that_module(self) -> None:
        """开关打开时**恰好**多出「外部不可信内容」一个模块，其余一字不动。"""
        off = [m.name for m in sorted(fixed_modules(), key=lambda m: m.priority)]
        on = [m.name for m in sorted(fixed_modules(True), key=lambda m: m.priority)]
        self.assertIn("外部不可信内容", on)
        # 把新增项摘掉之后，两份列表必须逐项相等（顺序也不能变）
        self.assertEqual([n for n in on if n != "外部不可信内容"], off)

    def test_position_between_constraints_and_task_mode(self) -> None:
        """
        排在「系统约束」20 与「任务模式」30 之间。

        两条讲的是同一件事的两面（什么算系统指令 / 什么不算），相邻便于模型建立对照。
        """
        modules = sorted(fixed_modules(True), key=lambda m: m.priority)
        names = [m.name for m in modules]
        self.assertEqual(
            names[names.index("系统约束") + 1],
            "外部不可信内容",
        )
        self.assertEqual(names[names.index("外部不可信内容") + 1], "任务模式")

    def test_goes_to_cacheable_channel(self) -> None:
        """文案逐轮逐字节一致，进动态通道会白白浪费前缀缓存。"""
        mod = next(m for m in fixed_modules(True) if m.name == "外部不可信内容")
        self.assertTrue(mod.cacheable)

    def test_text_states_data_not_instructions(self) -> None:
        mod = next(m for m in fixed_modules(True) if m.name == "外部不可信内容")
        self.assertIn("<untrusted-content>", mod.content)
        self.assertIn("数据", mod.content)
        self.assertIn("不得执行", mod.content)

    def test_builder_default_is_off(self) -> None:
        assembled = build_default_prompt(_env())
        self.assertNotIn("<untrusted-content>", assembled.stable)

    def test_builder_on(self) -> None:
        assembled = build_default_prompt(_env(), untrusted_enabled=True)
        self.assertIn("<untrusted-content>", assembled.stable)

    def test_switch_off_output_identical_to_no_param(self) -> None:
        a = build_default_prompt(_env())
        b = build_default_prompt(_env(), untrusted_enabled=False)
        self.assertEqual(a.stable, b.stable)


# =============================================================================
# T25：确认面板的 URL 展示
# =============================================================================
class ConfirmPanelUrlTests(unittest.TestCase):
    """
    面板要展示**完整地址（不截断）**、主机名、判定层。

    现状（改动前）走 summarize_args，它把每个参数值截到 30 字符——
    用户正是靠面板上那个地址决定放不放行的，被截断意味着攻击者只要把恶意部分
    放在第 31 个字符之后，人在回路这层就形同虚设。
    """

    def _panel(self):
        from rhinecode.tui.widgets import ConfirmPanel

        return ConfirmPanel.__new__(ConfirmPanel)  # 只调纯函数，不需要 Textual 挂载

    def _decision(self, **kw):
        from rhinecode.permission.models import Decision, DecisionResult, Layer

        base = dict(decision=Decision.ASK, layer=Layer.MODE, reason="r", kind="url", host="a.test")
        base.update(kw)
        return DecisionResult(**base)

    def _call(self, url, decision):
        from rhinecode.provider.base import ToolCall

        tc = ToolCall(id="1", name="web_fetch", arguments={"url": url, "prompt": "q"})
        return self._panel()._url_detail_lines(tc, decision)

    def test_long_url_not_truncated(self) -> None:
        url = "https://docs.example.com/reference/v2/very/long/path?token=abcdefghijklmnop&x=1"
        self.assertGreater(len(url), 60)
        lines = self._call(url, self._decision(host="docs.example.com"))
        joined = "\n".join(lines)
        self.assertIn(url, joined)
        self.assertNotIn("…", joined)

    def test_shows_host_and_layer(self) -> None:
        from rhinecode.permission.models import Layer

        lines = self._call(
            "https://a.test/x", self._decision(host="a.test", layer=Layer.NETWORK)
        )
        joined = "\n".join(lines)
        self.assertIn("a.test", joined)
        self.assertIn("②′网络边界", joined)

    def test_brackets_escaped_no_markup_error(self) -> None:
        """
        ⚠ URL 天然含 `[`（IPv6 字面量、含 `[` 的查询串）。

        用错 escape（rich 那版只转义「看起来像完整标签」的 `[...]`）会让落单的 `[`
        在 Textual 布局阶段抛 MarkupError——**没有任何 try/except 兜得住，
        整个 app 退出**。这里直接调文本构造函数，断言不抛且已转义。
        """
        from textual.content import Content

        url = "http://[::1]:8080/a?x=[1&y=[2"
        lines = self._call(url, self._decision(host="::1"))
        joined = "\n".join(lines)
        self.assertIn(r"\[", joined, "落单的方括号必须被转义")
        # 真正过一遍 Textual 的 markup 解析：不抛才算数。
        for line in lines:
            Content.from_markup(line)

    def test_layer_labels_cover_every_layer(self) -> None:
        """
        面板的层名表要覆盖 Layer 的全部取值。

        它与 `trace/reader.py` 的 `_LAYER_NAMES` 是同一批取值，两处刻意不合一
        （让只依赖标准库的 trace 叶子包反向依赖 permission 会破坏架构不变量），
        靠这类断言钉住不漏行。
        """
        from rhinecode.permission.models import Layer
        from rhinecode.tui.widgets import ConfirmPanel

        for member in Layer:
            self.assertIn(member.value, ConfirmPanel._LAYER_LABELS, member.name)


# =============================================================================
# T26/T28：开关传递链与装配
# =============================================================================
class BootstrapFixture(unittest.TestCase):
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


class ToolRegistrationTests(BootstrapFixture):
    def test_registered_when_enabled(self) -> None:
        self.assertIn("web_fetch", self.build().tool_registry.names())

    def test_absent_when_disabled(self) -> None:
        result = self.build(_cfg(web_fetch_enabled=False))
        self.assertNotIn("web_fetch", result.tool_registry.names())

    def test_exclude_tools_can_remove_it(self) -> None:
        """驱动设施的隔离手段对新工具同样有效。"""
        result = self.build(exclude_tools=frozenset({"web_fetch"}))
        self.assertNotIn("web_fetch", result.tool_registry.names())

    def test_injected_client_factory_is_used(self) -> None:
        """
        两个注入口一路透传到工具（spec N5：端到端场景也要能离线跑）。

        注意 `fetch()` 里的顺序是「先造客户端、再逐跳解析」——所以 factory
        不能直接抛，否则 resolver 永远轮不到，这条用例会以「resolver 没被用上」
        的形式误报。
        """
        used = {"factory": False, "resolver": False}

        class _NoopClient:
            def stream(self, *a, **kw):
                raise AssertionError("不该真的发出请求")

            def close(self):
                pass

        def factory():
            used["factory"] = True
            return _NoopClient()

        def resolver(host):
            used["resolver"] = True
            return ["93.184.216.34"]

        result = self.build(web_client_factory=factory, web_resolver=resolver)
        tool = result.tool_registry.get("web_fetch")
        tool.execute({"url": "https://a.test/x", "prompt": "q"})
        self.assertTrue(used["factory"], "client_factory 没被用上")
        self.assertTrue(used["resolver"], "resolver 没被用上")


class SwitchChainTests(BootstrapFixture):
    """
    F4 的两条开关传递链（T26）。

    `bootstrap.py` 对权限 `load_all` 与 `fixed_modules` 都是零调用，
    两条链都必须穿过协调层。
    """

    def test_all_prompt_call_sites_receive_the_switch(self) -> None:
        """
        链路②的调用点（c13 起**三个**）：

        1. 主对话（`_run`）；
        2. fork 子对话（`_run_forked_skill`）；
        3. **分支式子 Agent 的父快照**（`parent_snapshot`，c13 F8）——
           它继承父对话的稳定提示，漏传会让分支式子 Agent 失去不可信约束。

        漏传任一处的表现都是「主对话有不可信约束、某条子对话没有」——
        **界面上完全看不出来**，只有被注入的页面恰好走进那条子对话时才显形。
        所以这里直接查源码里的调用点，而不是只跑一条主对话。

        **定义式子 Agent 不在此列**：它按 spec F7 只拿角色正文 + 环境信息，
        不走 `build_default_prompt` 的八模块；那道约束由 `runner._build_prompts`
        在「最终工具集含网络访问工具」时单独追加，护栏见
        `tests/test_subagent_runner.py::SystemPromptTest`。
        """
        import inspect

        import rhinecode.conversation as conv

        source = inspect.getsource(conv)
        occurrences = source.count("untrusted_enabled=self._config.web_fetch_enabled")
        self.assertEqual(
            occurrences, 3,
            "build_default_prompt 的三个调用点都要传开关；"
            "漏一处会让对应的子对话失去不可信约束，且界面上看不出来",
        )

    def test_disabled_skips_domain_validation(self) -> None:
        """
        链路①：关闭时写坏的域名规则**零警告**（spec F4「逐字一致」）。

        多出一条警告就不叫逐字一致了。
        """
        self._write_project_rule('allow:\n  - "WebFetch(github.com)"\n')
        result = self.build(_cfg(web_fetch_enabled=False))
        self.assertEqual(result.manager._engine.load_errors, [])

    def test_enabled_produces_warning(self) -> None:
        self._write_project_rule('allow:\n  - "WebFetch(github.com)"\n')
        result = self.build()
        self.assertEqual(len(result.manager._engine.load_errors), 1)

    def test_warning_reaches_startup_notice(self) -> None:
        """
        链路：加载警告要**用户在界面上实际看得到**（spec F25/AC33）。

        `load_errors` 此前全仓零消费者，警告收集完就死在那里。
        """
        self._write_project_rule('allow:\n  - "WebFetch(github.com)"\n')
        notice = self.build().manager.startup_notice
        self.assertIsNotNone(notice)
        self.assertIn("WebFetch(github.com)", notice)

    def test_no_warning_no_extra_notice(self) -> None:
        """无警告时启动提示不该凭空多出内容。"""
        notice = self.build().manager.startup_notice
        if notice is not None:
            self.assertNotIn("权限规则加载提示", notice)

    def test_policy_ruleset_only_user_and_project(self) -> None:
        """
        白名单只由用户级/项目级建立（spec F6a）。

        本地级是「永久放行」自动写入的授权记录，算进来会让用户点一次
        「永久放行」就把自己锁死。
        """
        d = self.work / ".rhinecode"
        d.mkdir(parents=True, exist_ok=True)
        (d / "permissions.local.yaml").write_text(
            'allow:\n  - "WebFetch(domain:a.test)"\n', encoding="utf-8"
        )
        engine = self.build().manager._engine
        self.assertTrue(engine.file_ruleset.has_allow_for("WebFetch"), "全量规则里该有它")
        self.assertFalse(
            engine.policy_ruleset.has_allow_for("WebFetch"),
            "本地级不该建立白名单",
        )


if __name__ == "__main__":
    unittest.main()
