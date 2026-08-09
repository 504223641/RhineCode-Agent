"""
历史区的两条护栏：**用户消息整行灰底** 与 **新内容必须自己滚到底**。

## 这两条各在防什么（都是用户报的真实现象）

### 一、「发完消息还得手动拨滚轮才看得到」

根因不在业务代码，在 Textual 的一个时序细节：`mount()` 只是把组件放进 DOM，
**它的高度要等下一次布局才算得出来**，而 `scroll_end()` 取的是**当前**的
`max_scroll_y`——也就是加新组件之前的值。于是每次都停在「差最后一条消息」的位置，
消息占几行就差几行。改之前实测（80×24 终端、历史已铺满时）：

    append_user 之前   scroll_y=38  max=38
    append_user 之后   scroll_y=38  max=41   ← 新消息整条在视口外

修法是打开 Textual 的**滚动锚点**（`HistoryView.on_mount` 里的 `anchor()`）：
锚点由合成器在**每次排布时**把组件按到底部，与新组件的高度在同一次布局里算出，
因此不存在「用了过期的 max_scroll_y」这回事。

⚠️ **判据必须是 `scroll_offset.y == max_scroll_y`，不能是「调用过 scroll_end」。**
后者在修复前**照样通过**——那正是这个 bug 的形态：调用发生了，滚到的位置是错的。

⚠️ **`scroll_end` 那一处不能删。** 用户手动往上翻时 Textual 会自动松开锚点
（不把正在读历史的人硬拽回底部），松开之后只有 `scroll_end` 能重新按住它。
`test_new_message_pulls_a_scrolled_up_user_back` 就是钉这一半的。

### 二、用户消息要一眼能扫出来

markup 里的背景色只覆盖**字符所在的格子**——一句短消息只有几格变灰，
看起来像「选中了几个字」。要铺满整行必须让背景落在 widget 区域上，即走 CSS，
所以有了 `UserMessageWidget`。判据取「离文字很远的那一列也是灰的」，
因为**只有铺满才会**如此；只断言「用了 UserMessageWidget」的话，
将来有人把 CSS 里的 `width: 1fr` 删掉，测试照样绿。
"""

from __future__ import annotations

import unittest

from textual.app import App, ComposeResult

from rhinecode.provider.base import Message
from rhinecode.tui.widgets import HistoryView, UserMessageWidget


class _Harness(App):
    """只挂一个历史区的最小应用，CSS 与产品侧 `RhineApp` 的历史区部分一致。"""

    CSS = """
    HistoryView { height: 1fr; border: solid #7AEEFF 60%; padding: 0 1; }
    HistoryView > Vertical { height: auto; }
    """

    def compose(self) -> ComposeResult:
        yield HistoryView()


def _fill(view: HistoryView, rows: int = 60) -> None:
    """铺满历史区，制造「必须滚动才看得到底」的前提条件。"""
    for i in range(rows):
        view.append_system(f"历史第 {i} 行")


class AutoScrollTest(unittest.IsolatedAsyncioTestCase):
    """新内容出现后，视口必须已经在底部——不需要用户动手。"""

    async def test_new_user_message_is_visible_without_scrolling(self) -> None:
        """发一条消息后立刻处于底部（改之前差的正好是新消息那几行）。"""
        app = _Harness()
        async with app.run_test(size=(80, 24)) as pilot:
            view = app.query_one(HistoryView)
            _fill(view)
            await pilot.pause()

            view.append_user("刚发的这条必须整条可见")
            await pilot.pause()

            self.assertEqual(view.scroll_offset.y, view.max_scroll_y)

    async def test_multi_line_message_is_fully_visible(self) -> None:
        """
        长消息（换行成多行）同样要整条落进视口。

        单独立一条是因为「差一条消息的高度」这个 bug 的严重程度与消息行数成正比：
        单行消息只是被切掉一行，容易被当成显示误差；多行消息则是整段看不见。
        """
        app = _Harness()
        async with app.run_test(size=(80, 24)) as pilot:
            view = app.query_one(HistoryView)
            _fill(view)
            await pilot.pause()

            view.append_user("很长的一条消息，" + "需要折行的内容 " * 20)
            await pilot.pause()

            self.assertEqual(view.scroll_offset.y, view.max_scroll_y)

    async def test_new_message_pulls_a_scrolled_up_user_back(self) -> None:
        """
        用户翻上去之后再发消息，必须被带回底部。

        这一条钉的是 `_scroll_to_latest` 里那次 `scroll_end`：用户手动滚动会让
        Textual 松开锚点，只靠 `anchor()` 就再也回不到底部了（删掉那行会红）。
        """
        app = _Harness()
        async with app.run_test(size=(80, 24)) as pilot:
            view = app.query_one(HistoryView)
            _fill(view)
            await pilot.pause()

            view.scroll_to(y=0, animate=False)
            await pilot.pause()
            self.assertEqual(view.scroll_offset.y, 0, "前提没成立：没能翻到顶")

            view.append_user("翻上去之后发的这条")
            await pilot.pause()

            self.assertEqual(view.scroll_offset.y, view.max_scroll_y)

    async def test_streaming_reply_keeps_the_bottom(self) -> None:
        """流式 AI 回复逐块变长的过程中，视口一路跟到底。"""
        app = _Harness()
        async with app.run_test(size=(80, 24)) as pilot:
            view = app.query_one(HistoryView)
            _fill(view)
            await pilot.pause()

            widget = view.begin_assistant_turn()
            accumulated = ""
            for i in range(40):
                accumulated += f"第 {i} 段流式内容。"
                view.update_ai_widget(widget, accumulated)
            await pilot.pause()

            self.assertEqual(view.scroll_offset.y, view.max_scroll_y)

    async def test_history_replay_lands_at_the_bottom(self) -> None:
        """`/resume` 回放一整段历史后停在最新一条，而不是最早那条。"""
        app = _Harness()
        async with app.run_test(size=(80, 24)) as pilot:
            view = app.query_one(HistoryView)
            view.render_history(
                [Message(role="user", content=f"历史提问 {i}") for i in range(40)]
            )
            await pilot.pause()

            self.assertEqual(view.scroll_offset.y, view.max_scroll_y)


class UserRowBackgroundTest(unittest.IsolatedAsyncioTestCase):
    """用户消息行：独立组件 + 灰底铺满整行。"""

    async def test_live_echo_and_replay_use_the_same_widget(self) -> None:
        """
        实时回显与 `/resume` 回放必须产出同一种组件。

        两处各拼一次的话，回放出来的用户消息会与刚发的那条长得不一样，
        而这**不报错**——历史区里两种样式混着出现，只有人眼看得出来。
        """
        app = _Harness()
        async with app.run_test(size=(80, 24)) as pilot:
            view = app.query_one(HistoryView)
            view.append_user("实时回显的一条")
            await pilot.pause()
            live = view.query(UserMessageWidget)
            self.assertEqual(len(live), 1)

            view.render_history([Message(role="user", content="回放出来的一条")])
            await pilot.pause()
            replayed = view.query(UserMessageWidget)
            self.assertEqual(len(replayed), 1)

    async def test_background_fills_the_whole_row(self) -> None:
        """
        短消息也要整行灰底——判据取「离文字很远的那一列」。

        取样方式：直接问屏幕某个格子被画成了什么底色（`Screen.get_style_at`），
        这是唯一能分辨「整行铺满」与「只有字底下变灰」的办法。对照组是紧邻的
        系统提示行，它必须仍是页面底色；没有这个对照，「所有行都是灰的」
        （比如有人把背景写到了 `HistoryView` 上）也会通过。
        """
        app = _Harness()
        async with app.run_test(size=(60, 14)) as pilot:
            view = app.query_one(HistoryView)
            view.append_system("上一条系统提示")
            view.append_user("短消息")
            await pilot.pause()

            row = view.query_one(UserMessageWidget).region.y
            # 内容区最右端（留出右边框）与最左端，两处底色必须相同 = 铺满整行
            left = app.screen.get_style_at(2, row).bgcolor
            right = app.screen.get_style_at(56, row).bgcolor
            self.assertIsNotNone(left)
            self.assertEqual(left, right, "整行没有铺满：行首行尾底色不一致")

            # 对照组：普通消息行仍是页面底色，说明变灰的只有用户消息这一行
            plain_row = app.screen.get_style_at(2, row - 2).bgcolor
            self.assertNotEqual(
                left, plain_row, "用户行与普通行底色相同，等于没有区分"
            )


if __name__ == "__main__":
    unittest.main()
