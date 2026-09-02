"""
四屏向导界面的护栏（first-run-setup 扩展 T11–T13 / AC7、AC11–AC15、AC21）。

**两次网络请求全部注入假实现，一次都不联网。**

跑法是把 `SetupScreen` 推进一个最小宿主 App 里，用 `run_test()` 驱动。
每条用例一个干净的 app 实例（照本项目 TUI 测试一贯的做法）。
"""

import tempfile
import unittest
from pathlib import Path

from textual.app import App, ComposeResult
from textual.widgets import Button, Input, OptionList, Static

from rhinecode.config import DEFAULT_MODEL, _CONFIG_TEMPLATE, load
from rhinecode.setup.models import (
    ModelListResult,
    ModelOption,
    ProbeFailure,
    ProbeResult,
    SetupAction,
    SetupDraft,
    SetupMode,
)
from rhinecode.tui.setup_screen import SetupScreen

_OK_LIST = ModelListResult(
    options=(
        ModelOption("srv-flash", "服务端给的", True),
        ModelOption("srv-pro", "另一个", False),
    ),
    from_fallback=False,
)

_FALLBACK_LIST = ModelListResult(
    options=(ModelOption(DEFAULT_MODEL, "兜底那个", True),),
    from_fallback=True,
    error="连不上服务器。",
)

_OK_PROBE = ProbeResult(ok=True, kind=None, detail="连上了。", elapsed_ms=42)
_AUTH_FAIL = ProbeResult(
    ok=False, kind=ProbeFailure.AUTH, detail="密钥无效或已失效，回上一屏换一个试试。",
    elapsed_ms=10,
)


class _Host(App):
    """把 Screen 推上去的最小宿主，等价于真实的两个入口。"""

    def __init__(self, screen: SetupScreen) -> None:
        super().__init__()
        self._screen = screen
        self.outcome = None

    def compose(self) -> ComposeResult:
        return []

    def on_mount(self) -> None:
        self.push_screen(self._screen, self._done)

    def _done(self, outcome) -> None:
        self.outcome = outcome


class _ScreenCase(unittest.IsolatedAsyncioTestCase):
    """公共夹具：临时配置目录 + 造 Screen 的工厂。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "config.yaml"

    def tearDown(self):
        self._tmp.cleanup()

    def make(
        self,
        *,
        mode=SetupMode.FIRST_RUN,
        prefill=None,
        models=_OK_LIST,
        probe_result=_OK_PROBE,
    ) -> SetupScreen:
        return SetupScreen(
            self.path,
            prefill=prefill,
            mode=mode,
            list_models_fn=lambda *a, **k: models,
            verify_fn=lambda *a, **k: probe_result,
        )

    def text_of(self, screen: SetupScreen, widget_id: str) -> str:
        """
        读一个 Static 上现在显示的文本。

        ⚠ 走 `render()` 而不是 `.renderable`——Textual 8.x 的 `Static` 已经
        没有 `renderable` 这个属性了（内容存在私有的 `_Static__content` 里，
        对外的读法是 `render()` 返回的 `Content`）。
        """
        return str(screen.query_one(widget_id, Static).render())

    async def fill_credentials(self, pilot, screen, key="sk-test", url=None):
        """走完第一、二屏。"""
        screen.query_one("#btn-primary", Button).press()
        await pilot.pause()
        screen.query_one("#setup-key", Input).value = key
        if url is not None:
            screen.query_one("#setup-url", Input).value = url
        screen.query_one("#btn-primary", Button).press()
        await pilot.pause()
        await pilot.pause()


class IntroTest(_ScreenCase):
    """第一屏（AC10）。"""

    async def test_shows_config_path(self):
        screen = self.make()
        app = _Host(screen)
        async with app.run_test() as pilot:
            await pilot.pause()
            self.assertIn(str(self.path), self.text_of(screen, "#setup-body"))

    async def test_escape_abandons_without_writing(self):
        """AC7：按 Esc 交回 ABANDONED，且**没写任何文件**。"""
        screen = self.make()
        app = _Host(screen)
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("escape")
            await pilot.pause()
        self.assertEqual(app.outcome.action, SetupAction.ABANDONED)
        self.assertEqual(app.outcome.written, ())
        self.assertFalse(self.path.exists(), "放弃时不该写任何文件")

    async def test_manual_button_also_abandons(self):
        screen = self.make()
        app = _Host(screen)
        async with app.run_test() as pilot:
            await pilot.pause()
            screen.query_one("#btn-secondary", Button).press()
            await pilot.pause()
        self.assertEqual(app.outcome.action, SetupAction.ABANDONED)


class CredentialsTest(_ScreenCase):
    """第二屏（AC11、AC21）。"""

    async def test_base_url_prefilled_with_official_default(self):
        screen = self.make()
        app = _Host(screen)
        async with app.run_test() as pilot:
            await pilot.pause()
            self.assertEqual(
                screen.query_one("#setup-url", Input).value, "https://api.deepseek.com"
            )

    async def test_empty_key_blocks_progress_on_first_run(self):
        """AC11：首次配置时 key 为空不能前进。"""
        screen = self.make()
        app = _Host(screen)
        async with app.run_test() as pilot:
            await pilot.pause()
            screen.query_one("#btn-primary", Button).press()
            await pilot.pause()
            # 现在在第二屏，不填 key 直接下一步
            screen.query_one("#btn-primary", Button).press()
            await pilot.pause()
            self.assertTrue(
                screen.query_one("#setup-key", Input).display, "仍应停在第二屏"
            )

    async def test_rerun_allows_empty_key(self):
        """
        AC21：`/setup` 重跑时 key 留空表示「不改」，因此**允许**为空。

        这条与上一条是一对：同一个输入框，两种模式下的必填性相反。
        """
        prefill = SetupDraft(
            api_key="", base_url="https://api.deepseek.com", model="m", context_window=1
        )
        self.path.write_text(
            "protocol: deepseek\nmodel: m\nbase_url: https://api.deepseek.com\n"
            "api_key: sk-existing\n",
            encoding="utf-8",
        )
        screen = self.make(mode=SetupMode.RERUN, prefill=prefill)
        app = _Host(screen)
        async with app.run_test() as pilot:
            await pilot.pause()
            screen.query_one("#btn-primary", Button).press()
            await pilot.pause()
            screen.query_one("#btn-primary", Button).press()
            await pilot.pause()
            await pilot.pause()
            self.assertTrue(
                screen.query_one("#setup-models").display, "应当已经走到第三屏"
            )

    async def test_rerun_key_placeholder_says_it_can_be_left_blank(self):
        """AC21：留空的语义必须**写在界面上**，不能指望用户猜。"""
        screen = self.make(mode=SetupMode.RERUN)
        app = _Host(screen)
        async with app.run_test() as pilot:
            await pilot.pause()
            screen.query_one("#btn-primary", Button).press()
            await pilot.pause()
            self.assertIn("不改", screen.query_one("#setup-key", Input).placeholder)


class ModelStepTest(_ScreenCase):
    """第三屏（AC12、AC13）。"""

    async def test_options_come_from_server(self):
        """AC12：选项来自服务端返回，不是兜底清单。"""
        screen = self.make(models=_OK_LIST)
        app = _Host(screen)
        async with app.run_test() as pilot:
            await self.fill_credentials(pilot, screen)
            option_list = screen.query_one("#setup-models", OptionList)
            labels = [str(option_list.get_option_at_index(i).prompt) for i in range(option_list.option_count)]
            self.assertTrue(any("srv-flash" in x for x in labels), labels)
            self.assertTrue(any("srv-pro" in x for x in labels), labels)

    async def test_fallback_is_announced(self):
        """
        AC13：兜底必须**说出来**。

        静默退回内置清单等于把「清单会过期」这个问题原样搬回来了，
        还多骗用户一次。
        """
        screen = self.make(models=_FALLBACK_LIST)
        app = _Host(screen)
        async with app.run_test() as pilot:
            await self.fill_credentials(pilot, screen)
            notice = self.text_of(screen, "#setup-fallback")
            self.assertIn("兜底", notice)
            self.assertIn("过期", notice)
            self.assertIn("连不上服务器", notice, "失败原因也要说")

    async def test_manual_input_wins_over_selection(self):
        """
        手输框非空时以它为准——用户特意打了字，那就是他的意思。
        """
        screen = self.make(models=_OK_LIST)
        app = _Host(screen)
        async with app.run_test() as pilot:
            await self.fill_credentials(pilot, screen)
            screen.query_one("#setup-manual", Input).value = "my-own-model"
            screen.query_one("#btn-primary", Button).press()
            await pilot.pause()
            await pilot.pause()
            self.assertEqual(load(str(self.path)).model, "my-own-model")

    async def test_context_window_follows_selected_model(self):
        """
        AC17 的一半：窗口跟着模型走（spec F12）。

        认得出的模型写它的真实窗口，认不出的退回缺省值——`window_for` 已经
        单独测过，这里验的是**这条线真的接上了**。
        """
        screen = self.make(models=_OK_LIST)
        app = _Host(screen)
        async with app.run_test() as pilot:
            await self.fill_credentials(pilot, screen)
            screen.query_one("#setup-manual", Input).value = DEFAULT_MODEL
            screen.query_one("#btn-primary", Button).press()
            await pilot.pause()
            await pilot.pause()
            self.assertEqual(load(str(self.path)).context_window, 1_000_000)


class VerifyStepTest(_ScreenCase):
    """第四屏（AC8、AC14、AC15、AC17）。"""

    async def _run_to_end(self, pilot, screen):
        await self.fill_credentials(pilot, screen)
        screen.query_one("#btn-primary", Button).press()
        await pilot.pause()
        await pilot.pause()

    async def test_success_writes_and_lists_files(self):
        """AC15/AC17：成功时写盘，并把实际写了哪些文件列出来。"""
        screen = self.make()
        app = _Host(screen)
        async with app.run_test() as pilot:
            await self._run_to_end(pilot, screen)
            status = self.text_of(screen, "#setup-status")
            self.assertIn("连上了", status)
            self.assertIn(str(self.path), status)
            screen.query_one("#btn-primary", Button).press()
            await pilot.pause()

        self.assertEqual(app.outcome.action, SetupAction.SAVED)
        self.assertIn(self.path, app.outcome.written)
        cfg = load(str(self.path))
        self.assertEqual(cfg.api_key, "sk-test")
        self.assertEqual(cfg.model, "srv-flash")

    async def test_failure_shows_readable_reason_and_three_exits(self):
        """AC14/AC8：可读原因 + 三个出口。"""
        screen = self.make(probe_result=_AUTH_FAIL)
        app = _Host(screen)
        async with app.run_test() as pilot:
            await self._run_to_end(pilot, screen)
            self.assertIn("密钥无效", self.text_of(screen, "#setup-status"))
            for widget_id in ("#btn-primary", "#btn-secondary", "#btn-tertiary"):
                self.assertTrue(
                    screen.query_one(widget_id, Button).display,
                    f"{widget_id} 应当可见——校验失败要给三个出口",
                )

    async def test_save_anyway_writes_and_exits(self):
        """
        AC8：「仍然保存并继续」。

        断网、代理抽风、服务端 5xx 都不该把人锁死在向导里。
        """
        screen = self.make(probe_result=_AUTH_FAIL)
        app = _Host(screen)
        async with app.run_test() as pilot:
            await self._run_to_end(pilot, screen)
            screen.query_one("#btn-secondary", Button).press()
            await pilot.pause()
        self.assertEqual(app.outcome.action, SetupAction.SAVED)
        self.assertTrue(self.path.exists())
        self.assertEqual(load(str(self.path)).api_key, "sk-test")

    async def test_retry_goes_back_to_credentials(self):
        screen = self.make(probe_result=_AUTH_FAIL)
        app = _Host(screen)
        async with app.run_test() as pilot:
            await self._run_to_end(pilot, screen)
            screen.query_one("#btn-primary", Button).press()
            await pilot.pause()
            self.assertTrue(screen.query_one("#setup-key", Input).display)

    async def test_escape_during_wait_still_abandons(self):
        """
        **等待期间按 Esc 必须能出来**（spec N4）。

        网络卡住时出不去的话，用户只能杀进程——而那时配置还没写，
        下次启动又是同一个卡住的向导。
        """
        import threading

        release = threading.Event()

        def _slow_verify(*args, **kwargs):
            release.wait(timeout=5)
            return _OK_PROBE

        screen = SetupScreen(
            self.path,
            mode=SetupMode.FIRST_RUN,
            list_models_fn=lambda *a, **k: _OK_LIST,
            verify_fn=_slow_verify,
        )
        app = _Host(screen)
        try:
            async with app.run_test() as pilot:
                await self.fill_credentials(pilot, screen)
                screen.query_one("#btn-primary", Button).press()
                await pilot.pause()
                await pilot.press("escape")
                await pilot.pause()
            self.assertEqual(app.outcome.action, SetupAction.ABANDONED)
            self.assertFalse(self.path.exists(), "等待中放弃同样不该写文件")
        finally:
            release.set()


class EscapeMarkupTest(_ScreenCase):
    """
    **外部文本必须过 `escape`。**

    落单的 `[` 会在布局阶段的主线程抛 `MarkupError`，**没有任何 try/except
    兜得住，整个 app 退出**。错误消息里带方括号是常见形态，Windows 路径
    更是天天见。这条用例就是拿一个带方括号的服务端响应去撞它。
    """

    async def test_bracket_in_model_name_does_not_crash(self):
        nasty = ModelListResult(
            options=(ModelOption("model[unclosed", "说明 [也带一个", True),),
            from_fallback=False,
        )
        screen = self.make(models=nasty)
        app = _Host(screen)
        async with app.run_test() as pilot:
            await self.fill_credentials(pilot, screen)
            self.assertEqual(
                screen.query_one("#setup-models", OptionList).option_count, 1
            )

    async def test_bracket_in_error_detail_does_not_crash(self):
        nasty = ModelListResult(
            options=(ModelOption("m", "", True),),
            from_fallback=True,
            error="服务端说：[Errno 11001] getaddrinfo failed",
        )
        screen = self.make(models=nasty)
        app = _Host(screen)
        async with app.run_test() as pilot:
            await self.fill_credentials(pilot, screen)
            self.assertIn("getaddrinfo", self.text_of(screen, "#setup-fallback"))

    async def test_bracket_in_probe_detail_does_not_crash(self):
        bad = ProbeResult(
            ok=False,
            kind=ProbeFailure.NETWORK,
            detail="连不上。[WinError 10061] 由于目标计算机积极拒绝",
            elapsed_ms=3,
        )
        screen = self.make(probe_result=bad)
        app = _Host(screen)
        async with app.run_test() as pilot:
            await self.fill_credentials(pilot, screen)
            screen.query_one("#btn-primary", Button).press()
            await pilot.pause()
            await pilot.pause()
            self.assertIn("WinError", self.text_of(screen, "#setup-status"))


class NoTraceInstrumentationTest(unittest.TestCase):
    """
    **本文件一个 trace 埋点都不加**（plan「密钥不外泄」落点 3）。

    这里没有值得观测的东西，而它经手的恰恰是最敏感的字段。`/setup` 那次
    跑在记录器已经存在之后，加了埋点就会写进产物。
    """

    def test_screen_module_has_no_recorder_usage(self):
        source = Path("rhinecode/tui/setup_screen.py").read_text(encoding="utf-8")
        for needle in ("recorder", "_safe_emit", "from rhinecode.trace"):
            self.assertNotIn(needle, source, f"向导界面里出现了 {needle}")


class PreservesUserSectionsTest(_ScreenCase):
    """
    `/setup` 走一遍不许毁掉用户手写的段落，也不许清空密钥。

    这是端到端形态的 AC22——`writer` 那侧已经单独测过，这里验界面真的
    把空 key 原样传下去了。
    """

    async def test_rerun_with_blank_key_keeps_key_and_sections(self):
        self.path.write_text(
            _CONFIG_TEMPLATE.replace("api_key: YOUR_API_KEY", "api_key: sk-original")
            + "\nworktree:\n  cleanup_days: 3\n",
            encoding="utf-8",
        )
        screen = self.make(mode=SetupMode.RERUN)
        app = _Host(screen)
        async with app.run_test() as pilot:
            await pilot.pause()
            screen.query_one("#btn-primary", Button).press()
            await pilot.pause()
            # key 留空，直接下一步
            screen.query_one("#btn-primary", Button).press()
            await pilot.pause()
            await pilot.pause()
            screen.query_one("#btn-primary", Button).press()
            await pilot.pause()
            await pilot.pause()
            screen.query_one("#btn-primary", Button).press()
            await pilot.pause()

        text = self.path.read_text(encoding="utf-8")
        self.assertIn("cleanup_days: 3", text, "用户手写的段落被抹掉了")
        self.assertEqual(load(str(self.path)).api_key, "sk-original", "密钥被清空了")


if __name__ == "__main__":
    unittest.main()
