"""
历史区布局的实测护栏（tui-display 扩展 T11，spec F39/F40 / AC29/AC30）。

## 这条测试在防什么

改造前历史区的内容**贴着视口底部往上长**：只有两条消息时，它们出现在最后两行，
上方一大片空白。根因是 `HistoryView.on_mount` 的 `anchor()`——内容短于视口时
它产生一个**负的滚动偏移**把内容整个推到底部。实测（视口 13 行、内容 2 行）：

    HistoryView       region=(y=0, height=13)   scroll_y=-9   max_scroll_y=0
    #history-messages region=(y=10, height=2)   ← 两条消息落在第 10、11 行

但 `anchor()` **不能简单删掉**：它本身也是带实测证据加进去的（不用它时
`scroll_end()` 取到的是加新组件**之前**的 `max_scroll_y`，于是每次都停在
「差最后一条消息」的位置，用户得自己拨滚轮才看得到刚发的内容）。

两个需求必须同时满足，实现是**一行 CSS**：`HistoryView > Vertical` 上的
`min-height: 100%`。本文件用真实坐标同时钉住两侧，缺任何一侧都能让另一侧
「看起来是对的」。

⚠ **CSS 取自 `RhineApp.CSS` 本身**，不在这里抄一份。抄一份的话测的是副本，
产品里那行被删掉照样全绿。
"""

from __future__ import annotations

import unittest

from textual.app import App, ComposeResult

from rhinecode.tui.app import RhineApp
from rhinecode.tui.widgets import HistoryView


class _LayoutHarness(App):
    """
    只挂历史区的最小应用，但**用产品那份 CSS**。

    RhineApp 需要一整个 ConversationManager 才起得来，而本文件要验的是布局，
    与协调层无关。直接引用 `RhineApp.CSS` 既避开了装配成本，又保证验的是
    产品里真实生效的那份样式（其余选择器匹配不到任何组件，Textual 忽略即可）。
    """

    CSS = RhineApp.CSS

    def compose(self) -> ComposeResult:
        yield HistoryView()


# 视口高度取一个足够小的值，让「两条消息」必然短于视口。
_SIZE = (80, 16)


class TopAlignedTest(unittest.IsolatedAsyncioTestCase):
    """AC29：内容短于视口时贴顶，且滚动偏移不为负。"""

    async def test_two_messages_sit_at_the_top(self) -> None:
        app = _LayoutHarness()
        async with app.run_test(size=_SIZE) as pilot:
            view = app.query_one(HistoryView)
            view.append_system("第一条")
            view.append_system("第二条")
            await pilot.pause()

            self.assertEqual(
                view.scroll_offset.y,
                0,
                "滚动偏移必须为 0；为负说明 anchor 又把短内容推到底部了",
            )

    async def test_content_container_is_flush_with_the_viewport_top(self) -> None:
        """
        比「偏移为 0」更直接的判据：内容容器的**实际坐标**必须贴着视口顶部。

        单看 `scroll_offset` 是不够的——容器被布局排在半空中时偏移同样可以是 0。
        """
        app = _LayoutHarness()
        async with app.run_test(size=_SIZE) as pilot:
            view = app.query_one(HistoryView)
            view.append_system("唯一一条")
            await pilot.pause()

            container = view.query_one("#history-messages")
            # 历史区有 1 格边框，故内容容器的 y 应为「视口 y + 1」
            self.assertLessEqual(
                container.region.y - view.region.y,
                1,
                f"内容容器应贴着视口顶部，实际 container.y={container.region.y} "
                f"view.y={view.region.y}",
            )

    async def test_container_is_stretched_to_fill_the_viewport(self) -> None:
        """
        `min-height: 100%` 的**直接证据**：内容只有一行时，容器仍被撑到满高。

        这条是三者中唯一能区分「贴顶是因为这行 CSS」与「贴顶是因为别的巧合」的
        ——没有它，把 CSS 换成任何一种碰巧也让偏移为 0 的写法都能蒙混过关。
        """
        app = _LayoutHarness()
        async with app.run_test(size=_SIZE) as pilot:
            view = app.query_one(HistoryView)
            view.append_system("一行")
            await pilot.pause()

            container = view.query_one("#history-messages")
            self.assertGreater(
                container.region.height,
                1,
                "容器高度应被 min-height 撑到视口高，而不是只有内容那一行",
            )


class FollowLatestTest(unittest.IsolatedAsyncioTestCase):
    """AC30：内容超出视口后仍自动跟随最新，且不强行拽回正在往上翻的用户。"""

    async def test_long_content_still_follows_the_latest(self) -> None:
        app = _LayoutHarness()
        async with app.run_test(size=_SIZE) as pilot:
            view = app.query_one(HistoryView)
            for i in range(40):
                view.append_system(f"第 {i} 条")
            await pilot.pause()

            self.assertGreater(view.max_scroll_y, 0, "40 条必须已经超出视口，用例才有效")
            self.assertEqual(
                view.scroll_offset.y,
                view.max_scroll_y,
                "内容超出视口后必须停在底部（跟随最新）",
            )

    async def test_the_last_message_is_inside_the_viewport(self) -> None:
        """
        比「偏移等于最大值」更贴近用户感受的判据：最后一条**看得见**。

        改造前那个缺口正是「偏移到了最大值，但最大值本身是过期的」，
        于是最后一条恰好落在视口外一行。
        """
        app = _LayoutHarness()
        async with app.run_test(size=_SIZE) as pilot:
            view = app.query_one(HistoryView)
            for i in range(40):
                view.append_system(f"第 {i} 条")
            await pilot.pause()

            container = view.query_one("#history-messages")
            last = list(container.children)[-1]
            viewport_bottom = view.region.y + view.region.height
            self.assertLessEqual(
                last.region.y + last.region.height,
                viewport_bottom,
                "最后一条消息必须完整落在视口内",
            )

    async def test_manual_scroll_up_is_not_yanked_back(self) -> None:
        """
        AC30b：用户手动往上翻之后，再来一条消息不得把他硬拽回底部。

        Textual 在用户手动滚动时会松开锚点，这条钉住那个行为没被本轮改动破坏。
        """
        app = _LayoutHarness()
        async with app.run_test(size=_SIZE) as pilot:
            view = app.query_one(HistoryView)
            for i in range(40):
                view.append_system(f"第 {i} 条")
            await pilot.pause()

            view.scroll_to(y=0, animate=False, immediate=True)
            await pilot.pause()
            self.assertEqual(view.scroll_offset.y, 0, "手动滚到顶必须真的滚上去了")

            # 直接挂一个组件，绕开 `_scroll_to_latest`（那是「有新消息就带回底部」
            # 的刻意行为）。这里验的是「布局本身不会把人拽回去」。
            container = view.query_one("#history-messages")
            from textual.widgets import Static

            container.mount(Static("又来一条"))
            await pilot.pause()

            self.assertEqual(
                view.scroll_offset.y, 0, "正在看历史时不得被布局强行拽回底部"
            )


if __name__ == "__main__":
    unittest.main()
