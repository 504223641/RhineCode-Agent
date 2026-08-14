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
        from rhinecode.tui.widgets import QUIT_HINT_TEXT, StatusHint

        app, _ = _make_app()
        async with app.run_test() as pilot:
            await pilot.press("ctrl+c")
            await pilot.pause()
            self.assertIn(QUIT_HINT_TEXT, app.query_one(StatusHint).render().markup)

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
        from rhinecode.tui.widgets import StatusHint

        return app.query_one(StatusHint).render().markup

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

    async def test_the_hint_is_dim(self) -> None:
        """
        提示是**灰色**（对齐 Claude Code），刻意不用橘色。

        橘色在本项目里有确定语义——「用户没主动做什么、但情况变了」
        （放行档 / 上下文预警 / 确认面板），那些要抢注意力。而这条是用户刚按下
        一个键的直接回应，视线本来就在等它。顺手「统一」成橘色看着更一致，
        实际是把语义搞混，还会让真正该抢注意力的那三处贬值。
        """
        from rhinecode.tui.widgets import QUIT_HINT_TEXT

        app, _ = _make_app()
        async with app.run_test() as pilot:
            await pilot.press("ctrl+c")
            await pilot.pause()
            markup = self._status_text(app)
            self.assertIn(f"[dim]{QUIT_HINT_TEXT}[/dim]", markup)
            self.assertNotIn("#FFA500", markup, "刻意不用橘色——那是「需要留意」的专用色")

    async def test_the_hint_hugs_the_left_edge(self) -> None:
        """
        **本条是用户那句「贴着状态栏最左边」的判据**。

        ⚠ 它防的不是「排在第一段之前」——早先那版就是那么写的，判据过了而屏幕上
        并没有贴左：`StatusBar` 整块 `text-align: right`，拼进那串文本的提示只会落在
        **右对齐块的最左边**，也就是随其余各段的总长度在屏幕中间浮动。
        所以判据必须落在**布局坐标**上：左区的起点 x 必须是 0，且右区排在它右边。
        """
        from rhinecode.tui.widgets import QUIT_HINT_TEXT, StatusBar, StatusHint

        app, _ = _make_app()
        async with app.run_test() as pilot:
            await pilot.press("ctrl+c")
            await pilot.pause()

            hint = app.query_one(StatusHint)
            bar = app.query_one(StatusBar)
            self.assertEqual(hint.region.x, 0, "提示必须贴着状态栏左边缘")
            self.assertGreater(hint.region.width, 0, "有提示时左区不该是零宽")
            self.assertGreaterEqual(bar.region.x, hint.region.right, "右区应排在左区之后")
            self.assertNotIn(
                QUIT_HINT_TEXT,
                bar.render().markup,
                "提示不该同时出现在右区那串里",
            )

            # **合成之后**再验一次：底行第 0 列起就是提示本身。
            #
            # 上面那几条看的是「布局把左区摆在了 x=0」，这条看的是「那一格里
            # 真的画着提示」——两者不等价：给左区来一句 `width: 1fr` 或
            # `text-align: right`，region.x 照样是 0，字却被推到中间去了，
            # 而那正是这次要修的形态。
            bottom = app.screen._compositor.render_strips()[-1].text
            self.assertTrue(
                bottom.startswith(QUIT_HINT_TEXT),
                f"底行应当以提示开头，实际是 {bottom!r}",
            )

    async def test_no_hint_means_the_left_region_takes_no_space(self) -> None:
        """
        **反证**：不在有效期内时左区零宽，右区那串与没有这个功能之前逐字一致。

        没有这条，「左区永远挂着、只是内容为空」也能让上面两条通过——而那会在
        状态栏左边留一块永久的空白，把右区整体往右挤，用户天天看得见。
        """
        from textual.content import Content

        from rhinecode.tui.widgets import StatusBar, StatusHint, compose_status_text

        app, manager = _make_app()
        async with app.run_test() as pilot:
            await pilot.pause()
            self.assertEqual(app.query_one(StatusHint).region.width, 0, "没提示时左区应零宽")
            # ⚠ 比 `.plain` 而不是 `.markup`：`render()` 会把 markup 重新序列化一遍，
            # 途中 `\[` 这类转义写法会被归一（`\[DEFAULT]` → `[DEFAULT]`）。
            # 比 markup 会失败在与判据无关的转义形态上，而用户看到的是 plain。
            expected = Content.from_markup(
                compose_status_text(
                    provider=app._config.protocol,
                    model=app._config.model,
                    thinking_effort=manager.thinking_effort,
                    preset=manager.preset_value,
                    mcp_status=manager.mcp_status_line(),
                    context_status=None,
                    context_warn=False,
                    skill_status=manager.skill_status_segment(),
                    subagent_status=None,
                )
            ).plain
            self.assertEqual(
                app.query_one(StatusBar).render().plain,
                expected,
                "右区那串不该因为这个功能多出任何字符",
            )

    async def test_the_right_region_does_not_move_when_the_hint_appears(self) -> None:
        """
        提示出现与消失时，**右区那串的位置一格都不能动**。

        右区若用 `width: auto`（或让左右两区共同居中之类），提示一挂出来整条
        状态栏就会横向抖一下——每次误按 `Ctrl+C` 抖一次，而这是纯粹的视觉噪音。
        `width: 1fr` + 右对齐让右区的右边缘钉死在屏幕右边，与左区无关。

        ⚠ **终端宽度必须给够**（这里 120 列）。窄到装不下「提示 + 整串状态」时，
        右区被挤窄、那串会被裁掉尾部——那是**已登记的代价**（只持续两秒），
        不是这条判据要管的东西。用缺省 80 列跑会失败在那个无关的边界上。
        """
        from rich.cells import cell_len

        app, _ = _make_app()
        async with app.run_test(size=(120, 24)) as pilot:
            await pilot.pause()
            before = app.screen._compositor.render_strips()[-1].text.rstrip()

            await pilot.press("ctrl+c")
            await pilot.pause()
            after = app.screen._compositor.render_strips()[-1].text.rstrip()

            # ⚠ 必须用 `cell_len` 而不是 `len`：中文是**双宽**字符，一个字占两格。
            # 拿字符数当列号会得出「右端移动了 6 格」这种结论，而屏幕上两次都在
            # 第 119 列——判据本身错了，却看着像发现了一个 bug。
            self.assertEqual(
                cell_len(before),
                cell_len(after),
                "右区右边缘应当钉死在屏幕右边——两次底行的右端必须对齐",
            )

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
