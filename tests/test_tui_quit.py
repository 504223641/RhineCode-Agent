"""
连按两次 `Ctrl+C` 退出（tui-display 扩展 T38/T40，spec F31/F32 / AC20/AC21）。

## 这组测试与 C2 那条老护栏的关系

`docs/c2/spec.md` 的 AC9 定下过一条：**「`Ctrl+C` 用于复制场景，不应触发退出」**，
对应的护栏是「`ctrl+c` 不得绑定到退出动作」。

本轮把退出改成**连按两次** `Ctrl+C`。这不是推翻那条约束，而是**兑现它的本意**
——单次按下依然不退出（复制场景安全），两次连按才退。因此护栏**改写而不是删除**
（见 `test_tui_keybindings.py`），而本文件补上行为侧的判据。

## 最要紧的两条

1. **单次按下在任何状态下都不退出**（AC21）——空闲 / 流式运行中 /
   面板挂起中各验一次。这三种状态走的是不同的键位分支，只验一种证明不了另外两种；
2. **有选中文本时按下算复制，不计数**——连续复制五次一次都不会靠近退出。
   反过来（复制也计入）会让「连按两次复制」意外退出程序，那个方向更糟。
"""

from __future__ import annotations

import unittest

from rhinecode.tui.app import RhineApp
from tests.test_command_tui import _make_app


class SingleCtrlCTest(unittest.IsolatedAsyncioTestCase):
    """AC21：单次 `Ctrl+C` 在任何状态下都不退出。"""

    async def test_idle(self) -> None:
        app, _ = _make_app()
        async with app.run_test() as pilot:
            await pilot.press("ctrl+c")
            await pilot.pause()
            self.assertTrue(app.is_running)

    async def test_while_streaming(self) -> None:
        """流式运行中同样不退出——那时用户最可能想复制一段报错。"""
        app, _ = _make_app()
        async with app.run_test() as pilot:
            app._stream_active = True
            await pilot.press("ctrl+c")
            await pilot.pause()
            self.assertTrue(app.is_running)

    async def test_while_a_panel_is_pending(self) -> None:
        """面板挂起中同样不退出。"""
        import threading

        app, _ = _make_app()
        async with app.run_test() as pilot:
            app._pending_interaction = {
                "event": threading.Event(),
                "result": None,
                "kind": "confirm",
                "source": "human",
            }
            await pilot.press("ctrl+c")
            await pilot.pause()
            self.assertTrue(app.is_running)

    async def test_first_press_shows_a_hint(self) -> None:
        """
        AC20b：第一次按下给出提示——提示挂在**状态栏**上。

        ⚠ 文案必须写明是**退出**——`Esc` 才是取消当前回合，两者不能混淆。
        """
        from rhinecode.tui.widgets import QUIT_HINT_TEXT, StatusBar

        app, _ = _make_app()
        async with app.run_test() as pilot:
            await pilot.press("ctrl+c")
            await pilot.pause()
            self.assertIn(QUIT_HINT_TEXT, app.query_one(StatusBar).render().markup)

    async def test_the_hint_stays_out_of_the_chat_area(self) -> None:
        """
        **本条的分辨力所在**：提示**不进聊天区**。

        它是个只活两秒的瞬时状态，不是对话内容。留在历史里的话每次误按都攒一条
        永久噪音，而两秒之后那句话已经不成立了（那时按一次并不会退出）
        ——历史区里躺着一句错的提示，比没有提示更糟。
        """
        from tests.test_command_tui import _history_text

        from rhinecode.tui.widgets import QUIT_HINT_TEXT

        app, _ = _make_app()
        async with app.run_test() as pilot:
            await pilot.press("ctrl+c")
            await pilot.pause()
            self.assertNotIn(QUIT_HINT_TEXT, _history_text(app))


class QuitHintLifetimeTest(unittest.IsolatedAsyncioTestCase):
    """
    提示的存续期**就是**连按有效期：看得见 = 现在按第二下能退，看不见 = 得重新按。

    没有这组的话，「提示挂上去之后再也不撤」同样能让上面那条通过——而那正是
    用户要的反面：他要的是超时之后提示自己消失。
    """

    @staticmethod
    def _status_text(app) -> str:
        from rhinecode.tui.widgets import StatusBar

        return app.query_one(StatusBar).render().markup

    async def test_hint_disappears_when_the_window_expires(self) -> None:
        """
        超时后提示消失。

        直接调到期回调，等价于「定时器烧完了」——真等两秒会让整份测试慢一倍，
        而验的东西一模一样（`test_timeout_resets_the_counter` 同款手法）。
        """
        from rhinecode.tui.widgets import QUIT_HINT_TEXT

        app, _ = _make_app()
        async with app.run_test() as pilot:
            await pilot.press("ctrl+c")
            await pilot.pause()
            self.assertIn(QUIT_HINT_TEXT, self._status_text(app))

            app._expire_quit_hint()
            await pilot.pause()
            self.assertNotIn(QUIT_HINT_TEXT, self._status_text(app))
            self.assertTrue(app.is_running, "撤提示不该顺手把程序也退了")

    async def test_pressing_again_restarts_the_window(self) -> None:
        """
        **重复按下要重开窗口，不能沿用上一个定时器**。

        沿用的话，第一次按下起的那个定时器会在第二次按下之后不久把提示清掉
        ——用户刚按过一下，提示却提前消失，看上去像窗口被缩短了。
        """
        from rhinecode.tui.widgets import QUIT_HINT_TEXT

        app, _ = _make_app()
        async with app.run_test() as pilot:
            await pilot.press("ctrl+c")
            await pilot.pause()
            first_timer = app._quit_hint_timer

            # 把时间戳前拨，让下一次按下同样走「第一次」那一支（不退出）
            app._last_quit_request -= RhineApp.QUIT_CONFIRM_SECONDS + 1
            await pilot.press("ctrl+c")
            await pilot.pause()

            self.assertTrue(app.is_running)
            self.assertIsNot(app._quit_hint_timer, first_timer, "应当换了一个新定时器")
            self.assertIn(QUIT_HINT_TEXT, self._status_text(app))

    def test_the_hint_is_dim_and_leftmost(self) -> None:
        """
        提示是**灰色**、且排在**最左**（对齐 Claude Code）。

        两条都容易在后续改动里静默漂走，而且都不报错：

        - **灰色**：状态栏其余高亮段用的是橘色（放行档 / 上下文预警），
          顺手统一过去看着更「一致」，实际是把语义搞混了——橘色的意思是
          「你没做什么但情况变了」，而这条是用户刚按下一个键的直接回应。
          醒目的东西越多，醒目就越不值钱；
        - **最左**：它是瞬时的，接在尾部会被按需出现的 MCP / 上下文 / Skill /
          子 Agent 各段挤出视线——恰恰在它唯一有用的那两秒里看不见。
        """
        from rhinecode.tui.widgets import QUIT_HINT_TEXT, compose_status_text

        markup = compose_status_text("deepseek", "deepseek-chat", "off", quit_hint=True)

        self.assertIn(f"[dim]{QUIT_HINT_TEXT}[/dim]", markup)
        self.assertNotIn("#FFA500", markup, "刻意不用橘色——那是「需要留意」的专用色")
        self.assertLess(
            markup.index(QUIT_HINT_TEXT),
            markup.index("deepseek"),
            "提示必须排在第一段（provider）之前",
        )

    def test_no_hint_means_the_bar_is_byte_for_byte_unchanged(self) -> None:
        """
        **反证**：不在有效期内时，状态栏与加这个字段之前逐字一致。

        没有这条，「提示段永远挂着、只是内容为空」也能让上面那条通过——而那会
        在每一行状态栏最左边留一个多余的 ` | `，用户天天看得见。
        """
        args = ("deepseek", "deepseek-chat", "off")
        from rhinecode.tui.widgets import QUIT_HINT_TEXT, compose_status_text

        plain = compose_status_text(*args)
        self.assertEqual(compose_status_text(*args, quit_hint=False), plain)
        self.assertNotIn(QUIT_HINT_TEXT, plain)

    async def test_copying_does_not_show_the_hint(self) -> None:
        """有选中文本时那一下是复制，状态栏不该挂出退出提示。"""
        from unittest import mock

        from rhinecode.tui.widgets import QUIT_HINT_TEXT

        app, _ = _make_app()
        async with app.run_test() as pilot:
            with mock.patch.object(
                type(app.screen), "get_selected_text", lambda self: "一段文本"
            ), mock.patch.object(app, "copy_to_clipboard"):
                await pilot.press("ctrl+c")
                await pilot.pause()
                self.assertNotIn(QUIT_HINT_TEXT, self._status_text(app))


class DoublePressTest(unittest.IsolatedAsyncioTestCase):
    """AC20a / AC20c：连按退出；超时后复位。"""

    async def test_two_presses_quit(self) -> None:
        app, _ = _make_app()
        async with app.run_test() as pilot:
            await pilot.press("ctrl+c")
            await pilot.press("ctrl+c")
            await pilot.pause()
            self.assertFalse(app.is_running)

    async def test_timeout_resets_the_counter(self) -> None:
        """
        AC20c：间隔超过窗口就重新计数。

        直接把「上次按下的时刻」往前拨，等价于「等了很久」——真等两秒会让
        整份测试慢一倍，而验的东西一模一样。
        """
        app, _ = _make_app()
        async with app.run_test() as pilot:
            await pilot.press("ctrl+c")
            await pilot.pause()
            app._last_quit_request -= RhineApp.QUIT_CONFIRM_SECONDS + 1

            await pilot.press("ctrl+c")
            await pilot.pause()
            self.assertTrue(app.is_running, "超时之后那一下应当只是「第一次」")

            # 复位之后再连按两次仍然退得掉
            await pilot.press("ctrl+c")
            await pilot.pause()
            self.assertFalse(app.is_running)


class CopyTakesPrecedenceTest(unittest.IsolatedAsyncioTestCase):
    """
    **本文件的核心反证**：有选中文本时 `Ctrl+C` 是复制，且**不计数**。

    这是 C2 那条老护栏（「`Ctrl+C` 用于复制场景」）在新键位下的落点。
    Textual 里 `Screen` 与 `Input` 各有一条 `ctrl+c → copy`，两条都是
    `priority=False`——**都会被我们的 priority 绑定盖掉**。不做分流的话，
    「Ctrl+C 复制」这个今天真实可用的功能会整个消失。
    """

    async def test_two_presses_with_a_selection_do_not_quit(self) -> None:
        from unittest import mock

        app, _ = _make_app()
        async with app.run_test() as pilot:
            with mock.patch.object(
                type(app.screen), "get_selected_text", lambda self: "选中的一段报错"
            ), mock.patch.object(app, "copy_to_clipboard") as copied:
                await pilot.press("ctrl+c")
                await pilot.press("ctrl+c")
                await pilot.pause()

                self.assertTrue(app.is_running, "有选中内容时连按两次也不该退出")
                self.assertEqual(copied.call_count, 2, "两次都该是复制")

    async def test_selection_in_the_focused_widget_also_counts(self) -> None:
        """
        选中来源有**两处**，缺一不可（实测确认是两套独立机制）：
        鼠标在历史区拖选走 `screen.get_selected_text()`，
        输入框内 Shift+方向键选中走焦点组件的 `selected_text`。
        """
        from unittest import mock

        from rhinecode.tui.widgets import InputBar

        app, _ = _make_app()
        async with app.run_test() as pilot:
            bar = app.query_one(InputBar)
            bar.focus()
            await pilot.pause()
            with mock.patch.object(
                type(bar), "selected_text", property(lambda self: "我自己打的字")
            ), mock.patch.object(app, "copy_to_clipboard") as copied:
                await pilot.press("ctrl+c")
                await pilot.pause()

                self.assertTrue(app.is_running)
                copied.assert_called_once_with("我自己打的字")

    async def test_copying_does_not_arm_the_quit(self) -> None:
        """
        **「不计数」的分辨力所在**：复制一次之后**紧接着**按一次（无选中）
        必须只是「第一次」，不能退出。

        没有这条，把复制分支写成「复制并且照常计数」也能让上面两条通过。
        """
        from unittest import mock

        app, _ = _make_app()
        async with app.run_test() as pilot:
            with mock.patch.object(
                type(app.screen), "get_selected_text", lambda self: "一段文本"
            ), mock.patch.object(app, "copy_to_clipboard"):
                await pilot.press("ctrl+c")
                await pilot.pause()

            await pilot.press("ctrl+c")
            await pilot.pause()
            self.assertTrue(app.is_running, "复制不该给退出「上膛」")


class SigintGuardTest(unittest.IsolatedAsyncioTestCase):
    """
    `SIGINT` 必须走**同一条**连按两次的判定。

    ## 这组测试防的是什么

    `Ctrl+C` 有两条抵达路径：控制台输入模式决定它是一个**按键**（`0x03` 进输入流）
    还是一个**信号**（`CTRL_C_EVENT` → `SIGINT`）。后一条路径上，Python 默认处理器
    会在主线程抛 `KeyboardInterrupt` 把 `app.run()` 掀翻——**一次就退**，
    `request_quit` 里的计数一个字都读不到。

    用户实测反馈的「有时一下就退了」正是这条路径。它在单元测试里不会自己出现
    （测试环境的控制台模式是另一回事），所以必须显式钉住：**装了守卫**，
    且守卫落到 `action_request_quit` 上而不是自己另写一份判定。
    """

    async def test_guard_is_installed_and_restored(self) -> None:
        """挂载时接管 SIGINT，卸载时还原——不把进程级状态留给退出之后的代码。"""
        import signal

        app, _ = _make_app()
        before = signal.getsignal(signal.SIGINT)
        async with app.run_test() as pilot:
            await pilot.pause()
            # ⚠ 用 `assertEqual` 而不是 `assertIs`：每次取 `app._on_sigint` 都会
            # 现造一个新的 bound method 对象，`is` 恒为假。相等性才比的是
            # 「同一个函数 + 同一个实例」。
            self.assertEqual(
                signal.getsignal(signal.SIGINT),
                app._on_sigint,
                "挂载后 SIGINT 应当由本应用接管",
            )
        self.assertIs(signal.getsignal(signal.SIGINT), before, "退出后应当还原")

    async def test_signal_goes_through_the_same_double_press_logic(self) -> None:
        """
        **本组的分辨力所在**：一次 `SIGINT` 只是「第一次」，两次才退出。

        没有这条，把处理器写成「收到信号直接 `self.exit()`」也能让上一条通过
        ——而那正是要防的形态。
        """
        app, _ = _make_app()
        async with app.run_test() as pilot:
            app._on_sigint(2, None)
            await pilot.pause()
            self.assertTrue(app.is_running, "一次信号不该退出")

            app._on_sigint(2, None)
            await pilot.pause()
            self.assertFalse(app.is_running, "两次信号应当退出")

    async def test_signal_and_key_press_share_one_counter(self) -> None:
        """
        两条路径共用同一个计数器：先按键、再来信号，同样退出。

        计数器要是各存一份，「按一下再收一个信号」会两边都停在「第一次」，
        用户按了两下却退不掉——而那种偏差在界面上完全看不出来。
        """
        app, _ = _make_app()
        async with app.run_test() as pilot:
            await pilot.press("ctrl+c")
            await pilot.pause()
            self.assertTrue(app.is_running)

            app._on_sigint(2, None)
            await pilot.pause()
            self.assertFalse(app.is_running)


class CtrlQTest(unittest.IsolatedAsyncioTestCase):
    """AC20d：`Ctrl+Q` 不再退出。"""

    async def test_ctrl_q_does_nothing(self) -> None:
        """
        ⚠ 退出行为来自 **Textual 自带的 priority 绑定**，不是本项目的代码。
        覆盖它的那条也必须是 priority，否则内置那条先赢——这条用例就是那个
        「必须 priority」的实测依据。
        """
        app, _ = _make_app()
        async with app.run_test() as pilot:
            await pilot.press("ctrl+q")
            await pilot.pause()
            self.assertTrue(app.is_running)


if __name__ == "__main__":
    unittest.main()
