"""
界面内容的**可复制性**（真机反馈）。

用户原话：「整个聊天窗口其实都无法复制」。

## 根因不是「Textual 不支持选择」

Textual 8.x 原生支持文本选择，项目也早就接了拖选 + `Ctrl+C` 复制
（`app._copy_selection_if_any`）。真正的问题是 **`Widget.get_selection` 的默认
实现只认 `Text` 与 `Content` 两种渲染对象，别的一律返回 None**——

而工具行的终态是 `RichGroup`（标题行 + 结果块／差异块），于是**整行内容在
选中复制时凭空消失**：实测「全选」拿到的文本里，用户消息、系统提示、状态栏
都在，唯独工具行一个字都没有。

这是 tui-display 引入 `RichGroup` 时就埋下的，但 tui-activity-fold 让它更要命
——本轮刚把结果原文做得可以展开，展开看到了却复制不走。

## 为什么不改渲染结构

把 `RichGroup` 拆成单个 `Text` 做不到：差异块要按**渲染期才知道的宽度**给
整行补背景，那是 `Text` 表达不了的。因此保留视觉，在 `get_selection` 里补上
选择支持，纯文本与渲染**同源**（都走 `_DiffBlock._rows`）。
"""

import unittest

from textual.app import App, ComposeResult

from rhinecode.provider.base import ToolCall
from rhinecode.tools.diff import MARK_ADD, DiffRow, DiffView
from rhinecode.tui.widgets import DETAIL_FULL, DETAIL_ITEMS, HistoryView


class _Harness(App):
    def compose(self) -> ComposeResult:
        yield HistoryView()


FOLD = {"read_file": ("read", "读取 {n} 个文件")}
PRIMARY = {"read_file": "path", "write_file": "path"}


async def select_all(app, pilot) -> str:
    """全选并取出屏幕上所有可选中的文本。"""
    app.screen.text_select_all()
    await pilot.pause()
    return app.screen.get_selected_text() or ""


class ToolRowSelectionTest(unittest.IsolatedAsyncioTestCase):
    """工具行的内容必须可复制——那是本轮改造最要紧的产出。"""

    async def test_finished_tool_row_is_selectable(self) -> None:
        """
        **本文件最要紧的一条。**

        终态工具行用 `RichGroup` 渲染，而 Textual 的默认 `get_selection`
        对它返回 None。没有本组件自己的实现，这一行在复制时整个消失，
        **而屏幕上明明看得见**——这类「看得见拿不走」的缺陷没人会想到去测。
        """
        app = _Harness()
        async with app.run_test(size=(70, 24)) as pilot:
            view = app.query_one(HistoryView)
            view.set_primary_args(PRIMARY)
            widget = view.add_tool_widget(
                ToolCall(id="c1", name="read_file", arguments={"path": "a.py"})
            )
            await pilot.pause()
            widget.finish(True, "读取 12 行")
            await pilot.pause()

            got = await select_all(app, pilot)
            self.assertIn("a.py", got, "工具行的参数必须可复制")
            self.assertIn("读取 12 行", got, "结果摘要必须可复制")

    async def test_running_tool_row_is_selectable(self) -> None:
        """运行态用 markup 字符串渲染，本来就能选中——这条防的是改法把它弄坏。"""
        app = _Harness()
        async with app.run_test(size=(70, 24)) as pilot:
            view = app.query_one(HistoryView)
            view.set_primary_args(PRIMARY)
            view.add_tool_widget(
                ToolCall(id="c1", name="read_file", arguments={"path": "a.py"})
            )
            await pilot.pause()

            got = await select_all(app, pilot)
            self.assertIn("a.py", got)

    async def test_diff_block_is_selectable(self) -> None:
        """
        差异块的每一行也要能复制。

        它是自定义 renderable（要按渲染期宽度给整行补背景），纯文本由
        `_DiffBlock.plain_text()` 与渲染**同源**产出——各拼一遍的话，
        复制出来的内容会与屏幕上看到的悄悄不一致，而那种不一致极难发现：
        两边单看都是对的。
        """
        app = _Harness()
        async with app.run_test(size=(70, 24)) as pilot:
            view = app.query_one(HistoryView)
            view.set_primary_args(PRIMARY)
            widget = view.add_tool_widget(
                ToolCall(id="c1", name="write_file", arguments={"path": "b.py"})
            )
            await pilot.pause()
            widget.finish(
                True,
                "1 处替换",
                DiffView(
                    op="Update",
                    path="b.py",
                    added=1,
                    removed=0,
                    truncated=False,
                    rows=[
                        DiffRow(marker=MARK_ADD, old_no=None, new_no=1, text="新增的一行")
                    ],
                ),
            )
            await pilot.pause()

            got = await select_all(app, pilot)
            self.assertIn("Update", got, "差异块的标题要可复制")
            self.assertIn("新增的一行", got, "差异块的内容行要可复制")


class BatchSelectionTest(unittest.IsolatedAsyncioTestCase):
    """批次聚合行与展开后的原文同样要可复制。"""

    async def test_summary_line_is_selectable(self) -> None:
        app = _Harness()
        async with app.run_test(size=(70, 24)) as pilot:
            view = app.query_one(HistoryView)
            view.set_primary_args(PRIMARY)
            view.set_fold_groups(FOLD)
            widget = view.add_tool_widget(
                ToolCall(id="c1", name="read_file", arguments={"path": "a.py"})
            )
            await pilot.pause()
            widget.finish(True, "读取 12 行")
            view.append_system("封闭批次")
            await pilot.pause()

            got = await select_all(app, pilot)
            self.assertIn("读取 1 个文件", got, "聚合语必须可复制")

    async def test_expanded_raw_output_is_selectable(self) -> None:
        """
        **展开之后看到的原文必须拿得走。**

        这是本轮改造与这个缺陷叠加出的最坏组合：三级密度让用户终于能看到
        输出原文，而复制不走等于「看得见拿不到」——比看不到更让人恼火。
        """
        app = _Harness()
        async with app.run_test(size=(70, 24)) as pilot:
            view = app.query_one(HistoryView)
            view.set_primary_args(PRIMARY)
            view.set_fold_groups(FOLD)
            widget = view.add_tool_widget(
                ToolCall(id="c1", name="read_file", arguments={"path": "a.py"})
            )
            await pilot.pause()
            widget.finish(True, "读取 2 行", None, "文件的第一行\n文件的第二行")
            view.append_system("封闭批次")
            await pilot.pause()

            view.set_detail_level(DETAIL_FULL)
            await pilot.pause()

            got = await select_all(app, pilot)
            self.assertIn("文件的第二行", got, "展开后的原文必须可复制")

    async def test_folded_rows_are_not_selectable(self) -> None:
        """
        **反证**：折叠起来的行不该被复制走。

        看不见的东西复制出来，与看得见却复制不走同样是「所见非所得」——
        用户全选之后会拿到一堆屏幕上根本没有的内容。

        ⚠ 用例刻意用**两次**调用：只有一次时，聚合行本身会显示那次调用的
        主参数（F4 的设计——单次时那个信息放得下），于是路径出现在屏幕上是
        **正确的**，反证就构造不出来了。
        """
        app = _Harness()
        async with app.run_test(size=(70, 24)) as pilot:
            view = app.query_one(HistoryView)
            view.set_primary_args(PRIMARY)
            view.set_fold_groups(FOLD)
            for i, path in enumerate(("秘密路径.py", "另一个.py")):
                w = view.add_tool_widget(
                    ToolCall(id=f"c{i}", name="read_file", arguments={"path": path})
                )
                await pilot.pause()
                w.finish(True, "读取 2 行")
            view.append_system("封闭批次")
            await pilot.pause()

            got = await select_all(app, pilot)
            self.assertIn("读取 2 个文件", got, "聚合行是可见的，要能复制")
            self.assertNotIn("秘密路径.py", got, "折叠起来的子行不该被复制走")

            view.set_detail_level(DETAIL_ITEMS)
            await pilot.pause()
            got = await select_all(app, pilot)
            self.assertIn("秘密路径.py", got, "展开之后就该能复制了")


class OtherContentSelectionTest(unittest.IsolatedAsyncioTestCase):
    """其余各类内容的可复制性（这些本来就好，防改坏）。"""

    async def test_user_and_system_lines_are_selectable(self) -> None:
        app = _Harness()
        async with app.run_test(size=(70, 24)) as pilot:
            view = app.query_one(HistoryView)
            view.append_user("我说的话")
            view.append_system("系统提示")
            await pilot.pause()

            got = await select_all(app, pilot)
            self.assertIn("我说的话", got)
            self.assertIn("系统提示", got)


if __name__ == "__main__":
    unittest.main()
