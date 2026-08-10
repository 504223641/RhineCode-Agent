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
        AC20b：第一次按下给出提示。

        ⚠ 文案必须写明是**退出**——`Esc` 才是取消当前回合，两者不能混淆。
        """
        from tests.test_command_tui import _history_text

        app, _ = _make_app()
        async with app.run_test() as pilot:
            await pilot.press("ctrl+c")
            await pilot.pause()
            text = _history_text(app)
            self.assertIn("再按一次 Ctrl+C 退出", text)


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
