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

import io
from types import SimpleNamespace
import unittest

from rich.console import Console
from textual.app import App, ComposeResult

from rhinecode.provider.base import ToolCall
from rhinecode.tools.diff import MARK_ADD, DiffRow, DiffView
from rhinecode.tui.app import RhineApp
from rhinecode.tui.widgets import (
    BRANCH_LINE_LIMIT,
    DIFF_ROW_LIMIT,
    HistoryView,
    render_diff_block,
)


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


class _ToolHarness(App):
    """只挂历史区的最小应用，用于让工具行真的经历挂载与渲染。"""

    def compose(self) -> ComposeResult:
        yield HistoryView()


def _text_of(widget) -> str:
    """
    取出工具行当前展示的纯文本。

    终态传给 `Static.update` 的是 `RichGroup`，直接 `str()` 只会得到对象表示，
    必须逐个取子元素的 `.plain`。与 `test_tui_tool_pending.py` 里那份同口径
    ——那边验的是「建行/定色」的时序，这边验的是折叠，共用一个辅助函数会让
    两个文件互相牵制，故各留一份（十行的辅助函数，不值得抽公共模块）。
    """
    content = widget.content
    if isinstance(content, str):
        return content
    renderables = getattr(content, "renderables", None) or [content]
    parts = []
    for item in renderables:
        plain = getattr(item, "plain", None)
        parts.append(plain if plain is not None else str(item))
    return "\n".join(parts)


class BranchFoldTest(unittest.IsolatedAsyncioTestCase):
    """
    AC31a/AC31b：长结果被折叠并标出剩余行数，展开后能看到全文。

    ## 这条测试在防什么

    改造前工具结果**只取首行、截到 80 字符**。一次 `grep` 命中 23 处，
    用户只看得到第一处，而且**没有任何迹象表明还有别的**——既不知道被省了
    什么，也没法展开。「还有 N 行」这个数字本身就是那个迹象。
    """

    async def test_folded_shows_limit_lines_and_the_remainder(self) -> None:
        app = _ToolHarness()
        async with app.run_test(size=(120, 40)) as pilot:
            view = app.query_one(HistoryView)
            widget = view.add_tool_widget(ToolCall(id="c1", name="grep_content", arguments={}))
            await pilot.pause()
            widget.finish(True, "\n".join(f"命中第 {i} 处" for i in range(20)))
            await pilot.pause()

            text = _text_of(widget)
            self.assertIn("命中第 0 处", text)
            self.assertIn(f"命中第 {BRANCH_LINE_LIMIT - 1} 处", text)
            self.assertNotIn(f"命中第 {BRANCH_LINE_LIMIT} 处", text, "超限的行不该出现")
            self.assertIn(f"+{20 - BRANCH_LINE_LIMIT} 行", text)
            self.assertIn("Ctrl+O", text, "必须写明怎么展开，否则用户只知道被省了")

    async def test_expanded_shows_everything(self) -> None:
        app = _ToolHarness()
        async with app.run_test(size=(120, 40)) as pilot:
            view = app.query_one(HistoryView)
            widget = view.add_tool_widget(ToolCall(id="c1", name="grep_content", arguments={}))
            await pilot.pause()
            widget.finish(True, "\n".join(f"命中第 {i} 处" for i in range(20)))
            widget.set_expanded(True)
            await pilot.pause()

            text = _text_of(widget)
            for i in range(20):
                self.assertIn(f"命中第 {i} 处", text)
            self.assertNotIn("+15 行", text, "展开后不该还留着折叠提示")

    async def test_collapse_again(self) -> None:
        """再按一次收回——展开是可逆的。"""
        app = _ToolHarness()
        async with app.run_test(size=(120, 40)) as pilot:
            view = app.query_one(HistoryView)
            widget = view.add_tool_widget(ToolCall(id="c1", name="grep_content", arguments={}))
            await pilot.pause()
            widget.finish(True, "\n".join(f"第 {i} 行" for i in range(20)))
            widget.set_expanded(True)
            widget.set_expanded(False)
            await pilot.pause()

            self.assertIn("+15 行", _text_of(widget))

    async def test_short_result_has_no_fold_hint(self) -> None:
        """
        不超限时**不得**出现折叠提示。

        绝大多数工具结果只有一行；无条件挂一句「+0 行」是纯噪音，
        且会让用户以为有东西被藏起来了。
        """
        app = _ToolHarness()
        async with app.run_test(size=(120, 40)) as pilot:
            view = app.query_one(HistoryView)
            widget = view.add_tool_widget(ToolCall(id="c1", name="read_file", arguments={}))
            await pilot.pause()
            widget.finish(True, "读取 412 行")
            await pilot.pause()

            text = _text_of(widget)
            self.assertIn("读取 412 行", text)
            self.assertNotIn("Ctrl+O", text)

    async def test_exactly_at_the_limit_is_not_folded(self) -> None:
        """边界值：恰好等于上限时不折叠（折了只会多一行「+0 行」）。"""
        app = _ToolHarness()
        async with app.run_test(size=(120, 40)) as pilot:
            view = app.query_one(HistoryView)
            widget = view.add_tool_widget(ToolCall(id="c1", name="run_command", arguments={}))
            await pilot.pause()
            widget.finish(True, "\n".join(f"第 {i} 行" for i in range(BRANCH_LINE_LIMIT)))
            await pilot.pause()

            self.assertNotIn("Ctrl+O", _text_of(widget))

    async def test_trailing_blank_lines_do_not_eat_the_budget(self) -> None:
        """
        命令输出几乎都以换行结尾。尾部空行留着会白占折叠额度，
        让一个三行的结果显示成「+1 行」。
        """
        app = _ToolHarness()
        async with app.run_test(size=(120, 40)) as pilot:
            view = app.query_one(HistoryView)
            widget = view.add_tool_widget(ToolCall(id="c1", name="run_command", arguments={}))
            await pilot.pause()
            widget.finish(True, "a\nb\nc\n\n\n")
            await pilot.pause()

            self.assertNotIn("Ctrl+O", _text_of(widget))

    async def test_full_text_is_kept_not_truncated_at_finish(self) -> None:
        """
        **组件保留全文**：折叠只发生在渲染那一刻。

        存半截的话展开就没得展了——而那正是改造前「只取首行截到 80 字符」
        的问题所在。这条比「展开后能看到 20 行」更靠前一层：即使
        `set_expanded` 写错了，只要全文还在就救得回来。
        """
        app = _ToolHarness()
        async with app.run_test(size=(120, 40)) as pilot:
            view = app.query_one(HistoryView)
            widget = view.add_tool_widget(ToolCall(id="c1", name="grep_content", arguments={}))
            await pilot.pause()
            widget.finish(True, "\n".join(f"第 {i} 行" for i in range(20)))
            await pilot.pause()

            self.assertEqual(len(widget._summary.split("\n")), 20)


class ElapsedSuffixTest(unittest.IsolatedAsyncioTestCase):
    """
    AC11：终态耗时不足一秒时**整个括号不出现**（F13）。

    改造前每一行都挂着 `(0s)`——绝大多数工具调用是毫秒级的（实测 write_file
    从 tool_start 到 tool_result 只隔 2 毫秒），那个恒为零的括号是纯噪音，
    还会把真正跑了很久的那几行淹掉：一屏十个 `(0s)` 里夹着一个 `(43s)`，
    反而不显眼了。
    """

    async def test_sub_second_call_has_no_parenthesis(self) -> None:
        app = _ToolHarness()
        async with app.run_test(size=(120, 40)) as pilot:
            view = app.query_one(HistoryView)
            widget = view.add_tool_widget(ToolCall(id="c1", name="read_file", arguments={"path": "a"}))
            await pilot.pause()
            widget.finish(True, "读取 1 行")
            await pilot.pause()

            text = _text_of(widget)
            # tui-activity-fold F7 起**成功态不再写「完成」二字**（绿色已经把状态
            # 说完了）。断言换成「标签仍在」，本用例量的东西不变——它验的是
            # 「不足一秒时没有那个括号」，与状态词无关。
            self.assertIn("Read", text)
            self.assertNotIn("完成", text)
            self.assertNotIn("(0s)", text)
            self.assertNotIn("0s", text)

    async def test_slow_call_still_shows_the_seconds(self) -> None:
        """超过一秒的照常显示——那正是这个数字有信息量的场合。"""
        app = _ToolHarness()
        async with app.run_test(size=(120, 40)) as pilot:
            view = app.query_one(HistoryView)
            widget = view.add_tool_widget(ToolCall(id="c1", name="run_command", arguments={"command": "x"}))
            await pilot.pause()
            # 把起点往前推 3 秒，等价于「这次调用跑了 3 秒」
            widget._start -= 3.0
            widget.finish(True, "跑完了")
            await pilot.pause()

            self.assertIn("(3s)", _text_of(widget))

    async def test_pending_phase_is_still_visible(self) -> None:
        """
        **反证（tui-activity-fold 起改写，不是删除）**：参数生成期必须仍有活体信号。

        ## 这条需求的来历与它现在的承载者

        原判据是「工具行显示 `参数生成中… 0s`，秒数在涨」。理由是：那是模型
        生成参数（写文件类调用可能吐几十秒）/ 等确认面板的那段时间里，
        **界面上唯一的活体信号**——没有它，用户看到的是一个完全静止的窗口，
        无从判断程序是在干活还是卡住了。

        tui-activity-fold F19 把**运行中的秒数**从工具行撤走了，因为并发执行
        五个只读工具时屏幕上会有五个数字各自在跳。但**需求本身没有变**，
        只是承载者换成了底部的回合状态行（一处总耗时 + 旋转标记）。

        因此这条护栏改写为两半：
        1. 工具行仍然明确写出**它处在哪个阶段**（这里）；
        2. 活体信号（会动的东西）由 `test_tui_status_line.py` 钉住。

        ⚠ **不要把这条删掉**：删了之后「参数生成中」这个阶段词消失也没人发现，
        而那正是 tool_pending 那条护栏当初要解决的问题。
        """
        app = _ToolHarness()
        async with app.run_test(size=(120, 40)) as pilot:
            view = app.query_one(HistoryView)
            widget = view.add_tool_widget(
                ToolCall(id="c1", name="write_file", arguments=None), pending=True
            )
            await pilot.pause()

            text = _text_of(widget)
            self.assertIn("参数生成中", text)
            # F19/AC20：运行中的秒数已收敛到状态行，工具行上不该再有
            self.assertNotIn("0s", text)
            self.assertIsNone(widget._timer, "工具行不得再自持每秒刷新的定时器")


class SummarizeResultTest(unittest.TestCase):
    """
    T14：`_result_summary` 交出全文，把「省略」整个交给展示层。

    职责因此分成两层——**这里负责取内容，组件负责决定画多少**。改造前
    两件事挤在一处：取首行、截到 80 字符，于是内容在到达组件之前就没了，
    展开无从谈起。
    """

    @staticmethod
    def _res(output: str = "", summary: str = "", ok: bool = True):
        return SimpleNamespace(output=output, summary=summary, ok=ok)

    def test_tool_provided_summary_still_wins(self) -> None:
        """工具自带的 summary 优先级不变——那是作者亲手写的概括。"""
        self.assertEqual(
            RhineApp._result_summary(self._res(output="一大堆", summary="命中 23 处")),
            "命中 23 处",
        )

    def test_full_output_is_handed_over_intact(self) -> None:
        text = "\n".join(f"第 {i} 行" for i in range(30))
        self.assertEqual(RhineApp._result_summary(self._res(output=text)), text)

    def test_long_single_line_is_not_clipped_at_80(self) -> None:
        """改造前这里截到 80 字符，组件那边再想展开也没有内容可展。"""
        line = "x" * 500
        self.assertEqual(RhineApp._result_summary(self._res(output=line)), line)

    def test_empty_output_wording_depends_on_success(self) -> None:
        self.assertEqual(RhineApp._result_summary(self._res(ok=True)), "（无输出）")
        self.assertEqual(RhineApp._result_summary(self._res(ok=False)), "（无错误信息）")


class DiffFoldTest(unittest.IsolatedAsyncioTestCase):
    """
    AC31c：diff 块同样受行数上限约束。

    改造前它**完全没有上限**——一次大改动会把整块差异铺进历史区，
    后面的对话全被挤出屏幕。
    """

    @staticmethod
    def _big_diff(rows: int) -> DiffView:
        view = DiffView(op="Update", path="rhinecode/tui/app.py")
        for i in range(rows):
            view.rows.append(DiffRow(MARK_ADD, None, i + 1, f"新增第 {i} 行"))
        view.added = rows
        return view

    @staticmethod
    def _render(view: DiffView, expanded: bool) -> str:
        """把 diff 块渲染成纯文本（走真实的 Rich 渲染协议，不是读内部字段）。"""
        console = Console(width=100, file=io.StringIO())
        with console.capture() as cap:
            console.print(render_diff_block(view, expanded=expanded))
        return cap.get()

    def test_folded_diff_is_capped_and_says_how_many_are_left(self) -> None:
        text = self._render(self._big_diff(40), expanded=False)
        self.assertIn("新增第 0 行", text)
        self.assertIn(f"新增第 {DIFF_ROW_LIMIT - 1} 行", text)
        self.assertNotIn(f"新增第 {DIFF_ROW_LIMIT} 行", text)
        self.assertIn(f"+{40 - DIFF_ROW_LIMIT} 行", text)
        self.assertIn("Ctrl+O", text)

    def test_expanded_diff_shows_every_row(self) -> None:
        text = self._render(self._big_diff(40), expanded=True)
        self.assertIn("新增第 39 行", text)
        self.assertNotIn("Ctrl+O", text)

    def test_small_diff_is_untouched(self) -> None:
        """未超限的 diff 逐字不变——绝大多数改动只有几行，加提示是纯噪音。"""
        text = self._render(self._big_diff(3), expanded=False)
        self.assertNotIn("Ctrl+O", text)
        self.assertIn("新增第 2 行", text)

    def test_generation_truncation_and_display_folding_are_separate(self) -> None:
        """
        **两种截断刻意不合并。**

        `DiffView.truncated` 是 **diff 生成侧**（tools/diff.py）的标记，说的是
        「这份 diff 本身就没算全」；折叠说的是「算全了但没画全」。合并会让
        用户以为按 Ctrl+O 就能看到那些**根本没被生成出来**的行。
        """
        view = self._big_diff(40)
        view.truncated = True
        text = self._render(view, expanded=False)
        self.assertIn("diff 已截断", text)
        self.assertIn("Ctrl+O", text)

        # 展开之后「生成侧截断」那条仍在——它不是展开能解决的
        expanded = self._render(view, expanded=True)
        self.assertIn("diff 已截断", expanded)
        self.assertNotIn("Ctrl+O", expanded)


class GlobalExpandTest(unittest.IsolatedAsyncioTestCase):
    """
    T39：`Ctrl+O` 是**一个**全局键，同时管活动区与历史区。

    对齐 Claude Code 的全局 verbose 语义。两个键会让用户记两套，而这两处
    展开的是同一类东西（「刚才具体做了什么」）。

    ⚠ **tui-activity-fold 起它是三档循环**（折叠 → 逐条 → 全文 → 折叠），
    不再是两态开关。批次归并之后「展开」有了两层含义：把批次摊成逐条、
    把单条摊成原文。**逐条档的单条结果仍受行数上限约束**——那一档要的是
    「这一批都调了什么」，不是每条的内容。
    """

    async def test_cycle_goes_folded_items_full_folded(self) -> None:
        from tests.test_command_tui import _make_app

        app, _ = _make_app()
        async with app.run_test(size=(120, 40)) as pilot:
            view = app.query_one(HistoryView)
            widget = view.add_tool_widget(ToolCall(id="c1", name="grep_content", arguments={}))
            await pilot.pause()
            widget.finish(True, "\n".join(f"第 {i} 行" for i in range(20)))
            await pilot.pause()
            self.assertIn("+15 行", _text_of(widget), "起点是折叠档")

            # 第一下 → 逐条档：批次摊开了，但**单条结果仍受行数上限**
            await pilot.press("ctrl+o")
            await pilot.pause()
            self.assertIn("+15 行", _text_of(widget), "逐条档的单条结果仍要折叠")

            # 第二下 → 全文档：这才看得到原文
            await pilot.press("ctrl+o")
            await pilot.pause()
            self.assertNotIn("+15 行", _text_of(widget))
            self.assertIn("第 19 行", _text_of(widget), "全文档要看得到最后一行")

            # 第三下 → 回到折叠，循环闭合
            await pilot.press("ctrl+o")
            await pilot.pause()
            self.assertIn("+15 行", _text_of(widget), "按满三下必须回到起点")

    async def test_rows_finished_after_the_toggle_respect_it(self) -> None:
        """
        **顺序反证**：先切档、后定色的行，也要按当前档位画。

        只广播给「当前挂着的行」而不记下全局档位的话，切档之后新产生的每一行
        又会是折叠的——用户会以为开关时灵时不灵。
        """
        from tests.test_command_tui import _make_app

        app, _ = _make_app()
        async with app.run_test(size=(120, 40)) as pilot:
            # 按两下到全文档（一下只到逐条档，那一档看不到原文）
            await pilot.press("ctrl+o")
            await pilot.press("ctrl+o")
            await pilot.pause()

            view = app.query_one(HistoryView)
            widget = view.add_tool_widget(ToolCall(id="c1", name="grep_content", arguments={}))
            await pilot.pause()
            widget.finish(True, "\n".join(f"第 {i} 行" for i in range(20)))
            await pilot.pause()

            self.assertIn("第 19 行", _text_of(widget))


if __name__ == "__main__":
    unittest.main()
