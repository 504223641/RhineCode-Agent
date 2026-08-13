"""
三级信息密度的单测（tui-activity-fold 扩展 T24，AC11–AC15）。

`Ctrl+O` 从两态开关改成**三档循环**：折叠 → 逐条 → 全文 → 折叠。

## 三档各自回答什么

| 档 | 回答 |
| --- | --- |
| 折叠 | 这一轮**大概**干了什么（一行聚合语） |
| 逐条 | 它**在找的东西对不对**（每次调用的主参数值 + 结果规模） |
| 全文 | 它**到底**做了什么、拿到了什么（完整参数 + 输出原文） |

## ⚠ 本文件最要紧的一条

`FullTitleTest.test_items_level_still_truncates` —— **逐条档与全文档的标题
必须不同**。改造前展开态沿用构造时算好的截断标题，于是「展开」了却看不到
被截掉的部分。没有这条反证，把两档做成同一个样子照样全绿。
"""

import unittest

from textual.app import App, ComposeResult

from rhinecode.provider.base import ToolCall
from rhinecode.tui.widgets import (
    DETAIL_CYCLE,
    DETAIL_FOLDED,
    DETAIL_FULL,
    DETAIL_ITEMS,
    NEXT_LEVEL_HINT,
    HistoryView,
    ToolBatchWidget,
)

FOLD = {
    "read_file": ("read", "读取 {n} 个文件"),
    "grep_content": ("grep", "搜索内容 {n} 次"),
}
PRIMARY = {"read_file": "path", "grep_content": "pattern", "run_command": "command"}

# 一条足够长、会被折叠档截断的路径
LONG_PATH = "rhinecode/tui/very/deeply/nested/directory/structure/widgets_module.py"


class _Harness(App):
    def compose(self) -> ComposeResult:
        yield HistoryView()


def call(tid: str, name: str, **arguments):
    return ToolCall(id=tid, name=name, arguments=arguments or {})


def text_of(widget) -> str:
    """
    取组件当前展示的纯文本。

    ⚠ **必须走 `plain_text()`**，别读 `widget.content`：终态行的内容是
    Rich 渲染对象，要在渲染期按实时宽度转成 `Content` 才成立（那是选区
    功能的前提，见 `content_from_rich`），`content` 里留的是上一次 markup。
    """
    if hasattr(widget, "plain_text"):
        return widget.plain_text()
    content = widget.content
    return content if isinstance(content, str) else str(content)


async def prepared(app: App) -> HistoryView:
    view = app.query_one(HistoryView)
    view.set_primary_args(PRIMARY)
    view.set_fold_groups(FOLD)
    return view


class CycleTest(unittest.IsolatedAsyncioTestCase):
    """AC11：三档循环闭合。"""

    def test_cycle_has_exactly_three_levels(self) -> None:
        self.assertEqual(DETAIL_CYCLE, (DETAIL_FOLDED, DETAIL_ITEMS, DETAIL_FULL))

    def test_every_level_has_a_hint(self) -> None:
        """
        AC12：每一档都要有「按下去会到哪」的提示。

        漏一档不报错，只是那一档的聚合行末尾空着——而三态循环**全靠这句话**
        让用户不必记忆。
        """
        for level in DETAIL_CYCLE:
            with self.subTest(level=level):
                self.assertIn(level, NEXT_LEVEL_HINT)
                self.assertTrue(NEXT_LEVEL_HINT[level].strip())

    def test_hints_point_forward_not_at_current_state(self) -> None:
        """
        提示写的是**动作**（下一步会发生什么），不是当前状态编号。

        「展开 / 看全文 / 收起」读起来是「按了会怎样」；
        写成「档 1 / 档 2 / 档 3」的话用户还得先知道自己在第几档。
        """
        self.assertIn("展开", NEXT_LEVEL_HINT[DETAIL_FOLDED])
        self.assertIn("全文", NEXT_LEVEL_HINT[DETAIL_ITEMS])
        self.assertIn("收起", NEXT_LEVEL_HINT[DETAIL_FULL])


class BatchVisibilityTest(unittest.IsolatedAsyncioTestCase):
    """AC11：档位对批次与其工具行的作用。"""

    async def test_folded_shows_only_the_summary(self) -> None:
        app = _Harness()
        async with app.run_test(size=(120, 40)) as pilot:
            view = await prepared(app)
            view.add_tool_widget(call("c1", "read_file", path="a.py"))
            view.add_tool_widget(call("c2", "read_file", path="b.py"))
            await pilot.pause()

            view.set_detail_level(DETAIL_FOLDED)
            await pilot.pause()
            batch = view.query_one(ToolBatchWidget)
            self.assertTrue(batch.display, "聚合行本身任何档位都可见")
            self.assertTrue(
                all(not w.display for w in batch._widgets),
                "折叠档下工具行整体不可见",
            )

    async def test_items_and_full_both_reveal_rows(self) -> None:
        app = _Harness()
        async with app.run_test(size=(120, 40)) as pilot:
            view = await prepared(app)
            view.add_tool_widget(call("c1", "read_file", path="a.py"))
            await pilot.pause()
            batch = view.query_one(ToolBatchWidget)

            for level in (DETAIL_ITEMS, DETAIL_FULL):
                with self.subTest(level=level):
                    view.set_detail_level(level)
                    await pilot.pause()
                    self.assertTrue(all(w.display for w in batch._widgets))


class FullTitleTest(unittest.IsolatedAsyncioTestCase):
    """AC13：全文档的标题列全部参数且不截断。"""

    async def test_full_level_shows_every_argument_untruncated(self) -> None:
        app = _Harness()
        async with app.run_test(size=(120, 40)) as pilot:
            view = await prepared(app)
            widget = view.add_tool_widget(
                call("c1", "grep_content", pattern="def compose_status_text", path=LONG_PATH)
            )
            await pilot.pause()
            widget.finish(True, "共 3 处匹配")

            view.set_detail_level(DETAIL_FULL)
            await pilot.pause()

            text = text_of(widget)
            self.assertIn(LONG_PATH, text, "全文档必须显示完整路径")
            self.assertIn("pattern", text, "全文档必须列出全部参数名")
            self.assertIn("path", text)

    async def test_items_level_still_truncates(self) -> None:
        """
        **本文件最要紧的一条反证。**

        逐条档只显示主参数、且长值截断；全文档才列全。两档做成同一个样子的话
        「展开」就等于没展开——那正是改造前的缺陷（展开态沿用构造时算好的
        截断标题）。没有这条，把两档写成一样照样全绿。
        """
        app = _Harness()
        async with app.run_test(size=(120, 40)) as pilot:
            view = await prepared(app)
            widget = view.add_tool_widget(
                call("c1", "grep_content", pattern="def compose_status_text", path=LONG_PATH)
            )
            await pilot.pause()
            widget.finish(True, "共 3 处匹配")

            view.set_detail_level(DETAIL_ITEMS)
            await pilot.pause()
            items_text = text_of(widget)

            view.set_detail_level(DETAIL_FULL)
            await pilot.pause()
            full_text = text_of(widget)

            self.assertNotEqual(items_text, full_text, "两档的标题必须不同")
            self.assertNotIn(LONG_PATH, items_text, "逐条档不该显示非主参数的长路径")


class ResultBodyTest(unittest.IsolatedAsyncioTestCase):
    """AC14：全文档显示输出原文；被裁剪过的要如实说明。"""

    async def test_full_level_shows_the_raw_output(self) -> None:
        app = _Harness()
        async with app.run_test(size=(120, 40)) as pilot:
            view = await prepared(app)
            widget = view.add_tool_widget(call("c1", "read_file", path="a.py"))
            await pilot.pause()
            raw = "\n".join(f"第 {i} 行内容" for i in range(20))
            widget.finish(True, "读取 20 行 · 1.2 KB", None, raw)

            view.set_detail_level(DETAIL_ITEMS)
            await pilot.pause()
            self.assertIn("读取 20 行", text_of(widget), "逐条档显示的是规模描述")
            self.assertNotIn("第 19 行内容", text_of(widget))

            view.set_detail_level(DETAIL_FULL)
            await pilot.pause()
            self.assertIn("第 19 行内容", text_of(widget), "全文档显示的是原文")

    async def test_full_level_falls_back_to_summary_when_no_detail(self) -> None:
        """
        没有原文时（回放路径、脚本化 Provider）全文档回退显示规模描述——
        不能因为拿不到原文就画出一片空白。
        """
        app = _Harness()
        async with app.run_test(size=(120, 40)) as pilot:
            view = await prepared(app)
            widget = view.add_tool_widget(call("c1", "read_file", path="a.py"))
            await pilot.pause()
            widget.finish(True, "读取 20 行")

            view.set_detail_level(DETAIL_FULL)
            await pilot.pause()
            self.assertIn("读取 20 行", text_of(widget))

    async def test_full_level_is_not_line_limited(self) -> None:
        """全文档不受行数上限约束，且不出现「… +N 行」那个折叠提示。"""
        app = _Harness()
        async with app.run_test(size=(120, 40)) as pilot:
            view = await prepared(app)
            widget = view.add_tool_widget(call("c1", "read_file", path="a.py"))
            await pilot.pause()
            raw = "\n".join(f"line {i}" for i in range(30))
            widget.finish(True, "读取 30 行", None, raw)

            view.set_detail_level(DETAIL_FULL)
            await pilot.pause()
            text = text_of(widget)
            self.assertIn("line 29", text)
            self.assertNotIn("+", text.split("line 0")[0], "全文档不该有折叠提示")


class ResultDetailSourceTest(unittest.TestCase):
    """AC14 的取值侧：`_result_detail` 取 output 而非 full_output。"""

    @staticmethod
    def _res(output="", summary="", full_output="", ok=True):
        from types import SimpleNamespace

        return SimpleNamespace(
            ok=ok, output=output, summary=summary, full_output=full_output
        )

    def test_detail_takes_output_not_full_output(self) -> None:
        """
        ⚠ **刻意取裁剪后的那份。** 展开成完整原文会让一次测试套件输出
        撑爆历史区（spec F12 明确否掉）。
        """
        from rhinecode.tui.app import RhineApp

        res = self._res(output="裁剪后的 40 行", full_output="完整的 5000 行")
        detail = RhineApp._result_detail(res)
        self.assertIn("裁剪后的 40 行", detail)
        self.assertNotIn("完整的 5000 行", detail)

    def test_clipped_output_carries_a_note(self) -> None:
        """被裁剪时就地补一句说明——判定与措辞都收在一个函数里。"""
        from rhinecode.tui.app import RhineApp

        detail = RhineApp._result_detail(self._res(output="abc", full_output="abcdef"))
        self.assertIn("裁剪", detail)
        self.assertIn("行为记录", detail)

    def test_unclipped_output_has_no_note(self) -> None:
        from rhinecode.tui.app import RhineApp

        detail = RhineApp._result_detail(self._res(output="abc"))
        self.assertEqual(detail, "abc")

    def test_empty_output_returns_empty(self) -> None:
        """空串让组件回退显示规模描述（见 `finish` 的 detail 形参）。"""
        from rhinecode.tui.app import RhineApp

        self.assertEqual(RhineApp._result_detail(self._res(summary="只有摘要")), "")

    def test_summary_prefers_the_tools_own_wording(self) -> None:
        from rhinecode.tui.app import RhineApp

        res = self._res(output="一大堆原文", summary="命中 23 处")
        self.assertEqual(RhineApp._result_summary(res), "命中 23 处")


if __name__ == "__main__":
    unittest.main()


class ClickToExpandTest(unittest.IsolatedAsyncioTestCase):
    """
    鼠标点击展开（验收期新增）。

    `Ctrl+O` 是**全局**档位，管所有批次；鼠标是**单个**的——想看某一批具体
    做了什么，不必把满屏的批次一起摊开。两级配合：点聚合行摊开这一批，
    点其中某一条看它的完整参数与输出原文。
    """

    async def test_clicking_a_batch_toggles_only_that_batch(self) -> None:
        app = _Harness()
        async with app.run_test(size=(120, 40)) as pilot:
            view = await prepared(app)
            view.add_tool_widget(call("c1", "read_file", path="a.py"))
            await pilot.pause()
            view.append_system("断开")
            view.add_tool_widget(call("c2", "read_file", path="b.py"))
            await pilot.pause()

            first, second = list(view.query(ToolBatchWidget))
            self.assertEqual(first._detail_level, DETAIL_FOLDED)

            await pilot.click(first)
            await pilot.pause()

            self.assertEqual(first._detail_level, DETAIL_ITEMS, "点中的那批要摊开")
            self.assertEqual(
                second._detail_level, DETAIL_FOLDED, "**没点的那批不受影响**"
            )

    async def test_clicking_again_folds_it_back(self) -> None:
        app = _Harness()
        async with app.run_test(size=(120, 40)) as pilot:
            view = await prepared(app)
            view.add_tool_widget(call("c1", "read_file", path="a.py"))
            await pilot.pause()
            batch = view.query_one(ToolBatchWidget)

            await pilot.click(batch)
            await pilot.pause()
            self.assertEqual(batch._detail_level, DETAIL_ITEMS)

            await pilot.click(batch)
            await pilot.pause()
            self.assertEqual(batch._detail_level, DETAIL_FOLDED, "再点一次收回")

    async def test_clicking_a_row_toggles_full_detail(self) -> None:
        """点单条 → 逐条 ↔ 全文。折叠档下它不可见，够不着，故不回落到折叠。"""
        app = _Harness()
        async with app.run_test(size=(120, 40)) as pilot:
            view = await prepared(app)
            widget = view.add_tool_widget(call("c1", "read_file", path="a.py"))
            await pilot.pause()
            widget.finish(True, "读取 12 行", None, "行内容")

            view.set_detail_level(DETAIL_ITEMS)
            await pilot.pause()

            await pilot.click(widget)
            await pilot.pause()
            self.assertEqual(widget._detail_level, DETAIL_FULL)

            await pilot.click(widget)
            await pilot.pause()
            self.assertEqual(widget._detail_level, DETAIL_ITEMS, "再点一次回到逐条")

    async def test_global_key_still_overrides_individual_clicks(self) -> None:
        """
        **`Ctrl+O` 仍是全局的**：点开过的单个批次，全局切档时要跟着走。

        没有这条的话，「点开几个之后再按 Ctrl+O」会得到一屏各不相同的状态，
        而那正是全局开关要避免的。
        """
        app = _Harness()
        async with app.run_test(size=(120, 40)) as pilot:
            view = await prepared(app)
            view.add_tool_widget(call("c1", "read_file", path="a.py"))
            await pilot.pause()
            view.append_system("断开")
            view.add_tool_widget(call("c2", "read_file", path="b.py"))
            await pilot.pause()

            first, second = list(view.query(ToolBatchWidget))
            await pilot.click(first)
            await pilot.pause()
            self.assertNotEqual(first._detail_level, second._detail_level)

            view.set_detail_level(DETAIL_FULL)
            await pilot.pause()
            self.assertEqual(first._detail_level, DETAIL_FULL)
            self.assertEqual(second._detail_level, DETAIL_FULL, "全局广播要覆盖单个")
