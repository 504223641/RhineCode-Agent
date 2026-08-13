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


class ClipboardTest(unittest.TestCase):
    """
    系统剪贴板写入（验收期新增）。

    Textual 的 `copy_to_clipboard` 走 **OSC 52 转义序列**（由终端代为写剪贴板），
    好处是天然支持 SSH，代价是**很多终端出于安全默认关闭它**——而应用这一端
    只是往标准输出写了几个字节，**成没成功它根本不知道**。用户侧的表现就是
    「选中了、按了 Ctrl+C、什么也没发生」，且无任何报错。

    因此再直接调一次操作系统的剪贴板，两条路一起走。
    """

    def test_round_trip(self) -> None:
        """
        写进去能原样读回来——含中文、符号与换行。

        ⚠ 实现期在这里踩过一个坑：Win32 那条路要**显式声明 `ctypes` 的返回
        类型**。默认返回类型是 32 位 `int`，而那几个函数返回的是 64 位句柄，
        高位被直接截断，于是后续每一步都在操作垃圾地址——表现是「函数都调了、
        全都返回失败」，而且不抛异常。实测不声明时本条必红。
        """
        import ctypes
        import sys

        from rhinecode.tui.clipboard import copy_text

        if sys.platform != "win32":
            self.skipTest("读回验证只在 Windows 上做（其余平台要装外部工具）")

        text = "RhineCode 剪贴板 ✓\n第二行 with ascii"
        self.assertTrue(copy_text(text), "写入应当成功")

        user32, kernel32 = ctypes.windll.user32, ctypes.windll.kernel32
        user32.GetClipboardData.restype = ctypes.c_void_p
        user32.GetClipboardData.argtypes = [ctypes.c_uint]
        kernel32.GlobalLock.restype = ctypes.c_void_p
        kernel32.GlobalLock.argtypes = [ctypes.c_void_p]
        user32.OpenClipboard(None)
        try:
            handle = user32.GetClipboardData(13)  # CF_UNICODETEXT
            pointer = kernel32.GlobalLock(handle)
            got = ctypes.wstring_at(pointer)
            kernel32.GlobalUnlock(handle)
        finally:
            user32.CloseClipboard()

        self.assertEqual(got, text)

    def test_empty_text_is_a_no_op(self) -> None:
        """没什么可复制时直接返回 False，不去动系统剪贴板。"""
        from rhinecode.tui.clipboard import copy_text

        self.assertFalse(copy_text(""))


class DragDoesNotToggleTest(unittest.IsolatedAsyncioTestCase):
    """
    **拖选不得触发展开**（真机反馈）。

    用户原话：「我选择以后会自动展开」，而展开会让原本折叠的子行变可见、
    落进已经画好的选区，于是**复制到的内容比选中的多**。

    ## 根因：Textual 判「是不是点击」不看鼠标动没动

    `app.py` 里那段判定是 `if mouse_up_widget is mouse_down_widget` ——
    只要按下与抬起落在**同一个组件**就发 `Click`。在一行之内拖着选文字，
    抬手时那两个当然是同一个组件，于是照样发 Click。

    （我在实现期的注释里写过「拖拽不会触发 Click」，**那是错的**，
    读了 Textual 源码才发现。）

    判据用 `screen.selections`：单纯点击时它是空的，拖选过就非空。
    """

    async def test_click_still_toggles(self) -> None:
        """**正向**：没拖选时点击照常展开——别把功能一起挡掉了。"""
        from rhinecode.provider.base import ToolCall
        from rhinecode.tui.widgets import DETAIL_FOLDED, ToolBatchWidget

        app = _Harness()
        async with app.run_test(size=(80, 24)) as pilot:
            view = app.query_one(HistoryView)
            view.set_fold_groups(FOLD)
            widget = view.add_tool_widget(
                ToolCall(id="c1", name="read_file", arguments={"path": "a.py"})
            )
            await pilot.pause()
            widget.finish(True, "读取 3 行")
            view.append_system("封闭批次")
            await pilot.pause()

            batch = view.query_one(ToolBatchWidget)
            self.assertEqual(batch._detail_level, DETAIL_FOLDED)

            await pilot.click(batch)
            await pilot.pause()
            self.assertNotEqual(
                batch._detail_level, DETAIL_FOLDED, "单纯点击必须照常展开"
            )

    async def test_drag_select_does_not_toggle(self) -> None:
        """
        **反证**：有选中内容时点击不展开。

        这里直接把 `screen.selections` 造出来再发 Click——因为
        `Pilot` 的鼠标序列不走 App 那段 Click 判定，模拟不出真机的时序。
        判据本身是一样的：**有选区时那次 Click 是拖选的尾巴，不是点击**。

        ⚠ 这里**直接调 `on_click`** 而不是 `pilot.click`：后者会先发 MouseDown，
        而 MouseDown 会**清空选区**——于是造好的「刚拖选过」状态在 Click 到达
        之前就没了，反证根本构造不出来。
        """
        from textual.selection import Selection
        from rhinecode.provider.base import ToolCall
        from rhinecode.tui.widgets import DETAIL_FOLDED, ToolBatchWidget

        class _FakeClick:
            """只需要一个 `stop()`——`on_click` 用到的就这一个方法。"""

            def stop(self) -> None:
                pass

        app = _Harness()
        async with app.run_test(size=(80, 24)) as pilot:
            view = app.query_one(HistoryView)
            view.set_fold_groups(FOLD)
            widget = view.add_tool_widget(
                ToolCall(id="c1", name="read_file", arguments={"path": "a.py"})
            )
            await pilot.pause()
            widget.finish(True, "读取 3 行")
            view.append_system("封闭批次")
            await pilot.pause()

            batch = view.query_one(ToolBatchWidget)
            # 造一个「刚拖选过」的状态
            app.screen.selections = {batch: Selection(None, None)}

            batch.on_click(_FakeClick())
            await pilot.pause()
            self.assertEqual(
                batch._detail_level,
                DETAIL_FOLDED,
                "拖选之后那次 Click 不该展开——展开会让子行落进已画好的选区，"
                "复制到的内容就比选中的多",
            )

            # 反过来：清掉选区之后，同样一次 Click 必须照常展开
            app.screen.selections = {}
            batch.on_click(_FakeClick())
            await pilot.pause()
            self.assertNotEqual(
                batch._detail_level, DETAIL_FOLDED, "没有选区时点击照常展开"
            )


class EveryRowKindIsSelectableTest(unittest.IsolatedAsyncioTestCase):
    """
    **历史区的每一种行都要能复制**（真机反馈后补的全覆盖扫描）。

    前几轮是发现一处补一处：先是工具行，再是 AI 正文与回放，最后这一遍
    扫描又抓出**思考块**——它建行时只有一个 `✻ ` 前缀、内容全靠流式灌进去，
    而 `update_widget` 当时没同步纯文本缓存，于是拖选整段思考只能复制到
    那个孤零零的前缀。

    ⚠ 这条用例的价值在于**遍历**而不是逐个断言：将来新增一种行类型，
    只要它复制不了，这里就当场红——不必等用户再报一次。
    """

    async def test_sweep_all_kinds(self) -> None:
        from textual.selection import Selection
        from rhinecode.provider.base import ToolCall

        app = _Harness()
        async with app.run_test(size=(80, 30)) as pilot:
            view = app.query_one(HistoryView)
            view.set_primary_args(PRIMARY)

            view.append_user("用户消息")
            thinking = view.begin_thinking_turn()
            view.update_widget(thinking, "[dim italic]✻ 思考的内容[/dim italic]")
            assistant = view.begin_assistant_turn()
            view.update_ai_widget(assistant, "AI 正文")
            view.append_system("提示级消息")
            tool = view.add_tool_widget(
                ToolCall(id="c1", name="read_file", arguments={"path": "a.py"})
            )
            await pilot.pause()
            tool.finish(True, "读取 3 行")
            await pilot.pause()

            whole = Selection(None, None)
            unreachable = []
            for child in view.query_one("#history-messages").children:
                got = child.get_selection(whole)
                if got is None or not got[0].strip():
                    unreachable.append(type(child).__name__)

            self.assertEqual(
                unreachable, [], f"这些行的内容复制不走：{unreachable}"
            )

    async def test_thinking_content_survives_streaming(self) -> None:
        """
        思考块的**内容**（不只是前缀）要能复制。

        它是流式灌进去的：建行时只有 `✻ `，内容靠 `update_widget` 反复替换。
        纯文本缓存不跟着更新的话，复制到的就是建行那一瞬的样子。
        """
        from textual.selection import Selection

        app = _Harness()
        async with app.run_test(size=(80, 24)) as pilot:
            view = app.query_one(HistoryView)
            thinking = view.begin_thinking_turn()
            view.update_widget(thinking, "[dim italic]✻ 我在想这个问题[/dim italic]")
            await pilot.pause()

            got = thinking.get_selection(Selection(None, None))
            self.assertIsNotNone(got)
            self.assertIn("我在想这个问题", got[0], "思考的内容必须可复制，不能只有前缀")
