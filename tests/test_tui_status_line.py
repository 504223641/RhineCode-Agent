"""
回合状态行的单测（tui-activity-fold 扩展 T33，AC16–AC21）。

状态行是**本回合的活体态**：还在跑吗、跑了多久、烧了多少。它只在一次运行
进行中存在，运行一结束就整个隐藏且**不进历史区**——「跑了 12 秒」这条信息
几秒后就过期，写进历史等于往对话里灌过期数据。

## ⚠ 本文件里分量最重的两条

- `SpinnerWidthTest` —— **等宽护栏，带反证**。旋转标记各帧的显示宽度必须
  两两相等，不等的话每换一帧就把整行文字左右推一下，整行看起来在抖，
  比没有动画更糟。反证那条（换成已知不等宽的字符时护栏必须失败）是这条
  护栏**有效性的唯一证据**——只断言「当前帧表通过」证明不了它拦得住什么。
- `TimerConvergenceTest` —— 全界面同时至多**一个**动画定时器。
  这是 F19 的落点：改造前每个工具行各持一个，并发时五个数字同时跳。

⚠ **有一条只能真机验、不在这里**（AC18a）：Rich 与终端**各自计算**字符宽度，
而本轮选用的字形属 Unicode「模糊宽度」——Rich 两侧算出来都是 1 格，
**单元测试永远看不到终端把它画成 2 格这件事**。那条判据写在 checklist 的
「真机验收」一节。
"""

import unittest

from rich.cells import cell_len
from textual.app import App, ComposeResult

from rhinecode.provider.base import ToolCall
from rhinecode.tui.widgets import (
    SPINNER_FRAMES,
    SPINNER_INTERVAL,
    HistoryView,
    StatusLine,
)


class _Harness(App):
    def compose(self) -> ComposeResult:
        yield HistoryView()
        yield StatusLine()


class SpinnerWidthTest(unittest.TestCase):
    """AC18：旋转标记各帧必须等宽。"""

    def test_all_frames_have_the_same_width(self) -> None:
        """
        不等宽的话每换一帧就把整行文字左右推一下——整行看起来在抖，
        比没有动画更糟。
        """
        widths = {frame: cell_len(frame) for frame in SPINNER_FRAMES}
        self.assertEqual(
            len(set(widths.values())),
            1,
            f"各帧宽度必须相等，实际：{widths}",
        )

    def test_the_guard_actually_catches_unequal_widths(self) -> None:
        """
        **反证：这条护栏真的拦得住东西。**

        只断言「当前帧表通过」证明不了任何事——把帧表换成一组已知不等宽的
        字符，上面那条判据必须失败。没有这条反证，将来有人把护栏写成
        恒真表达式也不会有人发现。

        ## ⚠ 选反例时撞出的一条事实，正好解释了为什么还需要真机复核

        第一版反例取的是 `◇`（模糊宽度）与 `a`（半角），**结果没通过**——
        **Rich 认为这两个等宽（都算 1 格）**。因为 Rich 对
        「模糊宽度」（East Asian Width = Ambiguous）字符一律按 **1 格**处理，
        而终端在中日韩环境下**可能画成 2 格**。

        这正是 AC18a 必须**真机验**的原因：本文件用的 `cell_len` 是 Rich 的
        那一套，它**从定义上就看不见模糊宽度的风险**。本条护栏拦得住的是
        「混进一个 Rich 也认作宽字符的字形」，拦不住「模糊宽度在终端里变成 2 格」。

        因此反例改用真正的宽字符（East Asian Width = W）。
        """
        bad_frames = ("a", "中")
        widths = {frame: cell_len(frame) for frame in bad_frames}
        self.assertGreater(
            len(set(widths.values())),
            1,
            "反例构造失败：这两个字符在 Rich 眼里居然等宽，需要换一组反例",
        )

    def test_rich_cannot_see_ambiguous_width_risk(self) -> None:
        """
        **把上面那条发现固化成判据**：Rich 对模糊宽度字符按 1 格算。

        这条不是在测本项目的代码，是在**钉住一个外部事实**——因为整个
        「真机复核」环节的必要性建立在它之上。哪天 Rich 改了这个行为
        （模糊宽度改按 2 格或可配置），这条会红，那时应当重新评估
        AC18a 还要不要人工验。
        """
        self.assertEqual(cell_len("◇"), 1, "Rich 对模糊宽度按 1 格算")
        self.assertEqual(cell_len("中"), 2, "真正的宽字符才是 2 格")

    def test_frame_table_is_not_empty(self) -> None:
        """空表会让 `_current_frame` 除零——那是启动即崩，但只在运行时才暴露。"""
        self.assertGreater(len(SPINNER_FRAMES), 1, "至少要两帧才动得起来")

    def test_interval_is_a_sane_constant(self) -> None:
        """
        节奏必须是常量（F16a：本轮不引入新配置面），且落在「明显在动但不烦躁」
        的区间里。太快让人眼疲劳，太慢则失去「还活着」的提示作用。
        """
        self.assertIsInstance(SPINNER_INTERVAL, float)
        self.assertGreater(SPINNER_INTERVAL, 0.05)
        self.assertLess(SPINNER_INTERVAL, 0.5)


class LifecycleTest(unittest.IsolatedAsyncioTestCase):
    """AC16/AC17：只在运行中存在，结束即隐藏且不留痕。"""

    async def test_hidden_before_any_run(self) -> None:
        """
        AC24 零回归：空闲时它不占布局，界面与改造前逐字一致。
        """
        app = _Harness()
        async with app.run_test(size=(120, 40)) as pilot:
            line = app.query_one(StatusLine)
            self.assertFalse(line.display)

    async def test_start_shows_it_and_stop_hides_it(self) -> None:
        app = _Harness()
        async with app.run_test(size=(120, 40)) as pilot:
            line = app.query_one(StatusLine)
            line.start()
            await pilot.pause()
            self.assertTrue(line.display)

            line.stop()
            await pilot.pause()
            self.assertFalse(line.display, "运行结束后必须整个隐藏、不占布局")

    async def test_start_and_stop_are_idempotent(self) -> None:
        """异常路径上 `_set_streaming` 可能被调两次，重复调用不得出错。"""
        app = _Harness()
        async with app.run_test(size=(120, 40)) as pilot:
            line = app.query_one(StatusLine)
            line.start()
            line.start()
            await pilot.pause()
            self.assertTrue(line.display)
            line.stop()
            line.stop()
            await pilot.pause()
            self.assertFalse(line.display)

    async def test_nothing_lands_in_history(self) -> None:
        """
        AC17：状态行**不进历史区**。

        「跑了 12 秒」几秒后就过期，写进历史等于往对话里灌过期数据。
        """
        app = _Harness()
        async with app.run_test(size=(120, 40)) as pilot:
            view = app.query_one(HistoryView)
            line = app.query_one(StatusLine)
            line.start()
            await pilot.pause()

            container = view.query_one("#history-messages")
            self.assertEqual(len(container.children), 0)


class ContentTest(unittest.IsolatedAsyncioTestCase):
    """AC16/AC19：那一行写什么。"""

    async def test_shows_spinner_phase_elapsed_and_interrupt(self) -> None:
        app = _Harness()
        async with app.run_test(size=(120, 40)) as pilot:
            line = app.query_one(StatusLine)
            line.start()
            await pilot.pause()

            text = line.compose_text()
            self.assertIn(SPINNER_FRAMES[0], text)
            self.assertIn("处理中", text)
            self.assertIn("s", text, "耗时段必须在")
            self.assertIn("esc 中断", text)

    async def test_token_segment_hidden_until_there_is_usage(self) -> None:
        """
        无数据的段整段隐藏（与状态栏各段的既有做法一致）——
        不写 `↑0 tokens`，那是纯噪音。
        """
        app = _Harness()
        async with app.run_test(size=(120, 40)) as pilot:
            line = app.query_one(StatusLine)
            line.start()
            await pilot.pause()
            self.assertNotIn("↑", line.compose_text())

            line.add_tokens(2100)
            await pilot.pause()
            self.assertIn("↑", line.compose_text())
            self.assertIn("2.1k", line.compose_text())

    async def test_tokens_accumulate_across_rounds(self) -> None:
        """
        AC16：token 是**跨轮累加**的本回合总量，不是最后一轮的量。

        ⚠ 它**跳变式**更新（每轮流末尾才到一次），与耗时的持续滚动节奏不同
        ——那是 Provider 协议限制，属已知行为而非缺陷。
        """
        app = _Harness()
        async with app.run_test(size=(120, 40)) as pilot:
            line = app.query_one(StatusLine)
            line.start()
            line.add_tokens(1000)
            line.add_tokens(1500)
            await pilot.pause()
            self.assertIn("2.5k", line.compose_text())

    async def test_start_resets_the_counter(self) -> None:
        """新一轮运行必须从零开始算，不能把上一轮的量带过来。"""
        app = _Harness()
        async with app.run_test(size=(120, 40)) as pilot:
            line = app.query_one(StatusLine)
            line.start()
            line.add_tokens(5000)
            line.stop()
            line.start()
            await pilot.pause()
            self.assertNotIn("↑", line.compose_text())

    async def test_waiting_phase_drops_the_interrupt_hint(self) -> None:
        """
        AC19：面板弹出期间阶段词切换，且**不显示中断提示**——
        面板有自己的取消方式，两套提示同屏会误导用户去按 Esc。
        """
        app = _Harness()
        async with app.run_test(size=(120, 40)) as pilot:
            line = app.query_one(StatusLine)
            line.start()
            line.set_phase("等待确认", False)
            await pilot.pause()

            text = line.compose_text()
            self.assertIn("等待确认", text)
            self.assertNotIn("esc", text)

    async def test_elapsed_keeps_counting_across_phase_changes(self) -> None:
        """
        AC19：面板等待期间**耗时继续累计**——那段时间确实在这次回合内，
        用户等了多久就是等了多久。

        判据是「切阶段不重置起点」：拨一下起点再切阶段，读数不得归零。
        """
        app = _Harness()
        async with app.run_test(size=(120, 40)) as pilot:
            line = app.query_one(StatusLine)
            line.start()
            line._start_time -= 42
            line.set_phase("等待确认", False)
            await pilot.pause()
            self.assertIn("42s", line.compose_text())


class SpinTest(unittest.IsolatedAsyncioTestCase):
    """帧推进。"""

    async def test_tick_advances_and_wraps(self) -> None:
        app = _Harness()
        async with app.run_test(size=(120, 40)) as pilot:
            line = app.query_one(StatusLine)
            line.start()
            await pilot.pause()

            seen = []
            for _ in range(len(SPINNER_FRAMES) + 1):
                seen.append(line._current_frame())
                line._tick()
            # 走满一圈必须回到起点——不回的话是取模写错了，动画会在某一帧卡住
            self.assertEqual(seen[0], seen[len(SPINNER_FRAMES)])
            self.assertEqual(len(set(seen)), len(set(SPINNER_FRAMES)))


class TimerConvergenceTest(unittest.IsolatedAsyncioTestCase):
    """
    AC20：**全界面同时至多一个动画定时器。**

    这是 F19 的落点。改造前每个工具行各持一个每秒刷新的定时器，
    并发执行五个只读工具时屏幕上有五个数字各自在跳——它们表达的是同一件事
    （「还在跑」），却占了五份注意力。
    """

    async def test_tool_rows_hold_no_timer(self) -> None:
        app = _Harness()
        async with app.run_test(size=(120, 40)) as pilot:
            view = app.query_one(HistoryView)
            widgets = [
                view.add_tool_widget(
                    ToolCall(id=f"c{i}", name="read_file", arguments={"path": f"{i}.py"})
                )
                for i in range(5)
            ]
            await pilot.pause()

            for widget in widgets:
                self.assertIsNone(
                    widget._timer, "工具行不得再自持每秒刷新的定时器（F19）"
                )

    async def test_no_seconds_on_running_tool_rows(self) -> None:
        """
        并发跑五个工具时，工具行上一个秒数都不该有——
        时间只在状态行那一处显示。
        """
        app = _Harness()
        async with app.run_test(size=(120, 40)) as pilot:
            view = app.query_one(HistoryView)
            widget = view.add_tool_widget(
                ToolCall(id="c1", name="read_file", arguments={"path": "a.py"})
            )
            await pilot.pause()
            widget._start -= 37
            widget._render_running()
            self.assertNotIn("37", str(widget.content))


if __name__ == "__main__":
    unittest.main()
