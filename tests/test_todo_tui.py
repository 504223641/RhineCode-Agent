"""
待办块的界面行为（todo-list 扩展 T22，覆盖 AC9–AC11、AC13–AC15、AC17）。

⚠ **本文件抓不到布局问题。** 「钉不钉得住」「挤没挤到历史内容」只有真机
（或起一个真实 Textual app 量几何）才看得出来——那部分由 T1 的实测结论
与 `checklist.md` 第五节承担。这里验的是**组件自己的行为**：
可见性、渲染内容、转义、拖选可寻址、以及「全部完成」那一行的触发时机。

本项目的历史：tui-activity-fold 那一轮真机跑出 24 条问题而单测事前 0 条。
所以这个文件的定位是「便宜的回归网」，不是「界面对不对」的答案。
"""

from __future__ import annotations

import unittest

from textual.app import App, ComposeResult

from rhinecode.todo.models import TodoItem, TodoState
from rhinecode.todo.render import build_view
from rhinecode.tui.widgets import TodoPane


def item(title: str, state: TodoState = TodoState.PENDING) -> TodoItem:
    return TodoItem(title=title, state=state)


class _PaneApp(App):
    """只装一个待办块的最小宿主——起真实 Textual app 才问得出字符偏移。"""

    def compose(self) -> ComposeResult:
        yield TodoPane()


class VisibilityTest(unittest.IsolatedAsyncioTestCase):
    """AC10 / AC11：该显示就显示，不该显示就整块不占地方。"""

    async def test_none_hides_the_pane(self) -> None:
        async with _PaneApp().run_test(size=(80, 24)) as pilot:
            pane = pilot.app.query_one(TodoPane)
            pane.update_view(None)
            await pilot.pause()
            self.assertFalse(pane.display)

    async def test_a_view_shows_it(self) -> None:
        async with _PaneApp().run_test(size=(80, 24)) as pilot:
            pane = pilot.app.query_one(TodoPane)
            pane.update_view(build_view([item("改 login 接口", TodoState.IN_PROGRESS)]))
            await pilot.pause()
            self.assertTrue(pane.display)

    async def test_hiding_after_showing_takes_the_space_back(self) -> None:
        """
        由显示转隐藏时高度必须归零——否则「全部完成后收起」只是内容清空，
        而那几行空白还占着历史区。
        """
        async with _PaneApp().run_test(size=(80, 24)) as pilot:
            pane = pilot.app.query_one(TodoPane)
            pane.update_view(build_view([item("a"), item("b")]))
            await pilot.pause()
            self.assertGreater(pane.region.height, 0)

            pane.update_view(None)
            await pilot.pause()
            self.assertEqual(pane.region.height, 0)


class RenderTest(unittest.IsolatedAsyncioTestCase):
    """AC13 / AC14：内容画对了，且三档脱离颜色也认得出。"""

    async def _painted(self, view) -> str:
        async with _PaneApp().run_test(size=(80, 24)) as pilot:
            pane = pilot.app.query_one(TodoPane)
            pane.update_view(view)
            await pilot.pause()
            return pane.plain_text()

    async def test_header_and_titles_are_rendered(self) -> None:
        text = await self._painted(
            build_view(
                [
                    item("读现有实现", TodoState.COMPLETED),
                    item("改 login 接口", TodoState.IN_PROGRESS),
                    item("跑测试"),
                ]
            )
        )
        self.assertIn("待办 (1/3)", text)
        for title in ("读现有实现", "改 login 接口", "跑测试"):
            self.assertIn(title, text)

    async def test_all_three_labels_are_distinguishable_without_colour(self) -> None:
        """
        ⚠ **判据刻意只看文字，不看颜色**（AC13）。

        单色终端、截图、以及色觉障碍用户那里颜色全都会丢失，
        而文字标签不会——这正是 spec F14 要求「颜色 + 文字」两重的理由。
        只靠颜色区分的实现会让这条红。
        """
        text = await self._painted(
            build_view(
                [
                    item("甲", TodoState.IN_PROGRESS),
                    item("乙", TodoState.PENDING),
                    item("丙", TodoState.COMPLETED),
                ]
            )
        )
        for label in ("进行中", "待办", "已完成"):
            self.assertIn(label, text)

    async def test_overflow_line_is_rendered(self) -> None:
        text = await self._painted(build_view([item(f"t{i}") for i in range(12)]))
        self.assertIn("还有 7 条", text)

    async def test_only_whitelisted_symbols_appear(self) -> None:
        """
        AC14：待办块上只出现符号白名单里的那个圆点。

        ⚠ 白名单扫描（`tests/test_tui_symbols.py`）扫的是**源码**，
        而这里看的是**画出来的字**——两者互补：源码里可能拼出一个
        运行期才合成的符号。
        """
        text = await self._painted(
            build_view([item("甲", TodoState.IN_PROGRESS), item("乙")])
        )
        allowed = set("●·…")
        for ch in text:
            if ch.isascii() or ch.isspace():
                continue
            # 中日韩文字与全角括号照常出现，只挑「图形符号」区间检查
            if "←" <= ch <= "⯿" or "■" <= ch <= "◿":
                self.assertIn(ch, allowed, f"待办块上出现了白名单外的符号：{ch!r}")


class EscapeTest(unittest.IsolatedAsyncioTestCase):
    """
    AC17：标题里含方括号不能让程序退出。

    ⚠ 这是本项目踩过的**最致命**的一类：落单的 `[` 会被 Textual 的
    Content markup 当成标签开头并抛 `MarkupError`，抛出点在**布局阶段的
    主线程**，业务栈上没有任何线索，**没有任何 try/except 兜得住，
    Textual 直接拆掉整个 app、程序退出**。

    而待办标题正是最典型的高危来源——它是模型给的自由文本。
    """

    async def test_unbalanced_bracket_in_a_title_does_not_crash(self) -> None:
        async with _PaneApp().run_test(size=(80, 24)) as pilot:
            pane = pilot.app.query_one(TodoPane)
            pane.update_view(
                build_view(
                    [
                        item("修 [WIP 的解析器"),
                        item("处理 allowed_tools: [read_file, glo"),
                    ]
                )
            )
            await pilot.pause()
            self.assertTrue(pane.display)
            self.assertIn("修 [WIP 的解析器", pane.plain_text())

    async def test_markup_like_text_is_shown_literally(self) -> None:
        """
        反证：合法的 markup 标签也必须被当成**字面文本**显示，
        而不是被解释成样式——否则模型写一句 `[red]紧急[/red]` 就能改界面配色。
        """
        async with _PaneApp().run_test(size=(80, 24)) as pilot:
            pane = pilot.app.query_one(TodoPane)
            pane.update_view(build_view([item("[red]紧急[/red] 修 bug")]))
            await pilot.pause()
            self.assertIn("[red]紧急[/red]", pane.plain_text())


class SelectionTest(unittest.IsolatedAsyncioTestCase):
    """
    待办块的文字必须能被拖选。

    ## ⚠ 判据必须是「问不问得出字符偏移」

    tui-activity-fold 第 6 轮的教训：Textual 有两套渲染对象，选区功能对
    Rich 那套**三处一起失效**（不画高亮 / 问不出偏移 / 取不到选中内容）。
    **只补第三处是个陷阱**——补完之后「全选 + 复制」通了、测试全绿，
    而用户看到的仍是「连选都选不了」。真实反馈来了两轮才定位到前两条。

    因此这里判的是**第二处**：`get_widget_and_offset_at` 能不能返回偏移。
    """

    async def test_the_pane_is_drag_addressable(self) -> None:
        async with _PaneApp().run_test(size=(80, 24)) as pilot:
            pane = pilot.app.query_one(TodoPane)
            pane.update_view(
                build_view([item("改 login 接口", TodoState.IN_PROGRESS), item("跑测试")])
            )
            await pilot.pause()

            widget, offset = pilot.app.screen.get_widget_and_offset_at(
                pane.region.x + 1, pane.region.y + 1
            )
            self.assertIs(widget, pane)
            self.assertIsNotNone(
                offset,
                "问不出字符偏移就没有「从这个字选到那个字」——只测「能不能复制」"
                "在坏实现下照样全绿",
            )


class AllDoneTransitionTest(unittest.TestCase):
    """
    AC15：「全部完成」那一行**只在跃迁的那一刻**留，且只留一次。

    这里不起 Textual app——验的是 `_refresh_todo` 的判定逻辑，
    用一个最小替身把它的三个依赖（版本号 / 视图 / 全完成）喂进去。
    """

    class _Manager:
        def __init__(self) -> None:
            self.version = 0
            self.items: list[TodoItem] = []

        def todo_version(self) -> int:
            return self.version

        def todo_view(self):
            return build_view(self.items)

        def todo_all_done(self) -> bool:
            return bool(self.items) and all(
                i.state is TodoState.COMPLETED for i in self.items
            )

        def todo_all_done_text(self) -> str:
            return f"待办 {len(self.items)}/{len(self.items)} 全部完成"

    class _App:
        """只带 `_refresh_todo` 需要的那几样东西的替身。"""

        def __init__(self, manager) -> None:
            self._manager = manager
            self._todo_version = 0
            self._todo_shown = False
            self.events: list[str] = []
            self.views: list = []

        def show_event(self, text: str) -> None:
            self.events.append(text)

        def query_one(self, _cls):  # noqa: ANN001
            app = self

            class _History:
                @staticmethod
                def todo_pane():
                    class _Pane:
                        @staticmethod
                        def update_view(view):
                            app.views.append(view)

                    return _Pane()

            return _History()

        # 直接借真实实现，保证测的就是产品代码那一份
        from rhinecode.tui.app import RhineApp as _R

        _refresh_todo = _R._refresh_todo

    def _advance(self, app, manager, items) -> None:
        manager.items = list(items)
        manager.version += 1
        app._refresh_todo()

    def test_the_line_appears_exactly_once_on_completion(self) -> None:
        manager = self._Manager()
        app = self._App(manager)

        self._advance(app, manager, [item("甲"), item("乙")])
        self.assertEqual(app.events, [], "还没做完时不该有记录")

        self._advance(
            app, manager, [item("甲", TodoState.COMPLETED), item("乙")]
        )
        self.assertEqual(app.events, [], "做完一半时也不该有")

        self._advance(
            app,
            manager,
            [item("甲", TodoState.COMPLETED), item("乙", TodoState.COMPLETED)],
        )
        self.assertEqual(len(app.events), 1)
        self.assertIn("2/2", app.events[0])
        self.assertIsNone(app.views[-1], "留完记录之后待办块要收起")

    def test_no_line_when_it_was_never_shown(self) -> None:
        """
        ⚠ 反证：一份**一上来就全完成**的清单不该冒出这行。

        判据里「上一次显示过」那个条件就是为它写的——少了它，
        一个从来没显示过待办的会话也会凭空多出一行记录。
        """
        manager = self._Manager()
        app = self._App(manager)
        self._advance(app, manager, [item("甲", TodoState.COMPLETED)])
        self.assertEqual(app.events, [])

    def test_clearing_does_not_look_like_completion(self) -> None:
        """
        ⚠ 反证：`/clear` 造成的「不该显示」不能被误当成「全做完了」。

        判据里第三个条件（清单确实全完成）就是为它写的。
        """
        manager = self._Manager()
        app = self._App(manager)
        self._advance(app, manager, [item("甲"), item("乙")])
        self._advance(app, manager, [])  # 相当于 store.clear()
        self.assertEqual(app.events, [])
        self.assertIsNone(app.views[-1])

    def test_same_version_is_a_no_op(self) -> None:
        """版本号没变就不重绘——重绘幂等，但白跑一趟没有意义。"""
        manager = self._Manager()
        app = self._App(manager)
        self._advance(app, manager, [item("甲")])
        before = len(app.views)
        app._refresh_todo()
        self.assertEqual(len(app.views), before)


if __name__ == "__main__":
    unittest.main()
