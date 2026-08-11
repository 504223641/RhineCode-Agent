"""
子 Agent 活动区的单测（tui-display 扩展 T20/T23，spec F1–F6 / AC1/AC2/AC22/AC25）。

## 这个区域解决的问题

改造前，子 Agent 派出去之后是几分钟的静默——唯一的活体信号是状态栏角落那个
`子Agent:2` 计数。用户无从判断它是在干活还是卡住了。

## 本文件的三个重点

1. **无行时整块隐藏**（F1/F9）：不使用子 Agent 的用户永远走这一支，
   界面表现必须与改造前逐字一致；
2. **成本数字只有一处渲染**（F6）：终态行与历史留痕共用 `format_activity_cost`，
   各拼一次的话一次改动只改一处不报错，用户会看到同一个任务的成本在两处对不上；
3. **转义**（AC22/N2）：队员名来自模型给的参数、角色名来自用户写的角色定义
   文件、工具文本里含路径与命令——全是可能含字面 `[` 的自由文本。
"""

from __future__ import annotations

import unittest

from textual.app import App, ComposeResult

from rhinecode.subagents.tasks import ActivityRow, TaskStatus
from rhinecode.tui.widgets import (
    ActivityView,
    format_activity_cost,
    format_duration,
    format_tokens,
)


def row(
    name: str = "explorer",
    status: TaskStatus = TaskStatus.RUNNING,
    seconds: float = 23.0,
    tokens: int = 3100,
    tool_calls: int = 8,
    recent=(),
    settled: float = 0.0,
) -> ActivityRow:
    return ActivityRow(
        display_name=name,
        status=status,
        seconds=seconds,
        tokens=tokens,
        tool_calls=tool_calls,
        recent_tools=tuple(recent),
        settled_seconds=settled,
    )


class _Harness(App):
    def compose(self) -> ComposeResult:
        yield ActivityView()


def _text_of(view: ActivityView) -> str:
    """把活动区当前挂着的那个 Static 的 markup 原样取出来。"""
    children = list(view.children)
    if not children:
        return ""
    content = children[0].content
    return content if isinstance(content, str) else str(content)


# --------------------------------------------------------------------------- #
# 成本数字的渲染（纯函数）
# --------------------------------------------------------------------------- #


class CostFormatTest(unittest.TestCase):
    def test_duration_under_a_minute(self) -> None:
        self.assertEqual(format_duration(43.7), "43s")

    def test_duration_over_a_minute(self) -> None:
        """`0m 43s` 里那个零毫无信息量，所以不足一分钟只写秒。"""
        self.assertEqual(format_duration(72), "1m 12s")
        self.assertEqual(format_duration(59), "59s")

    def test_tokens_switch_to_k(self) -> None:
        """活动行横向空间很紧，而这个数字要的是量级不是精确值。"""
        self.assertEqual(format_tokens(820), "820 tokens")
        self.assertEqual(format_tokens(28500), "28.5k tokens")

    def test_running_puts_elapsed_first(self) -> None:
        """
        运行中把耗时排最前——它是唯一每秒都在跳的数字，
        用户扫一眼就是想确认「它还活着」。
        """
        text = format_activity_cost(row())
        self.assertTrue(text.startswith("23s"))
        self.assertIn("↑3.1k tokens", text)
        self.assertIn("8 次调用", text)

    def test_terminal_puts_calls_first(self) -> None:
        """终态时耗时已不重要，用户要的是「这次委派花了多少」。"""
        text = format_activity_cost(
            row(status=TaskStatus.COMPLETED, seconds=72, tokens=28500, tool_calls=14)
        )
        self.assertTrue(text.startswith("14 次调用"))
        self.assertIn("28.5k tokens", text)
        self.assertIn("1m 12s", text)

    def test_running_marks_tokens_with_the_arrow(self) -> None:
        """`↑` 在符号白名单里，专表 token 计数。"""
        self.assertIn("↑", format_activity_cost(row()))
        self.assertNotIn("↑", format_activity_cost(row(status=TaskStatus.COMPLETED)))

    def test_only_whitelisted_symbols(self) -> None:
        """
        产出里只许出现白名单内的符号（F29）。

        没有这条，下一个人加一句「⏱ 43s」是很自然的事，而符号一多每个的语义
        就都记不住了。
        """
        text = format_activity_cost(row()) + format_activity_cost(
            row(status=TaskStatus.FAILED)
        )
        for banned in "◆▲×✅❌🔒⏱⚠":
            self.assertNotIn(banned, text)


# --------------------------------------------------------------------------- #
# 组件行为
# --------------------------------------------------------------------------- #


class ActivityViewTest(unittest.IsolatedAsyncioTestCase):
    async def test_hidden_when_empty(self) -> None:
        """
        AC1/F9：没有任何行时整块隐藏且不占布局空间。

        这是零回归的落点——不使用子 Agent 的用户永远走这一支。
        """
        app = _Harness()
        async with app.run_test() as pilot:
            view = app.query_one(ActivityView)
            view.update_rows(())
            await pilot.pause()

            self.assertFalse(view.display)
            self.assertEqual(view.region.height, 0, "隐藏之后不该再占布局空间")

    async def test_two_rows_show_names_and_numbers(self) -> None:
        app = _Harness()
        async with app.run_test() as pilot:
            view = app.query_one(ActivityView)
            view.update_rows((row("explorer"), row("planner", seconds=12, tokens=1200, tool_calls=3)))
            await pilot.pause()

            text = _text_of(view)
            self.assertTrue(view.display)
            self.assertIn("explorer", text)
            self.assertIn("planner", text)
            self.assertIn("运行中", text)
            self.assertIn("23s", text)
            self.assertIn("8 次调用", text)

    async def test_collapsed_hides_the_recent_calls(self) -> None:
        app = _Harness()
        async with app.run_test() as pilot:
            view = app.query_one(ActivityView)
            view.update_rows((row(recent=("Grep(Layer)", "Read(engine.py)")),), expanded=False)
            await pilot.pause()

            text = _text_of(view)
            self.assertNotIn("Grep(Layer)", text)
            self.assertIn("Ctrl+O 展开", text, "折叠态必须告诉用户还能展开")

    async def test_expanded_lists_the_recent_calls(self) -> None:
        app = _Harness()
        async with app.run_test() as pilot:
            view = app.query_one(ActivityView)
            view.update_rows((row(recent=("Grep(Layer)", "Read(engine.py)")),), expanded=True)
            await pilot.pause()

            text = _text_of(view)
            self.assertIn("Grep(Layer)", text)
            self.assertIn("Read(engine.py)", text)
            self.assertIn("⎿", text, "子行要有从属符号，否则读不出层级")
            self.assertIn("Ctrl+O 收回", text)

    async def test_terminal_row_shows_final_cost(self) -> None:
        app = _Harness()
        async with app.run_test() as pilot:
            view = app.query_one(ActivityView)
            view.update_rows(
                (row(status=TaskStatus.COMPLETED, seconds=72, tokens=28500, tool_calls=14),)
            )
            await pilot.pause()

            text = _text_of(view)
            self.assertIn("已完成", text)
            self.assertIn("14 次调用", text)

    async def test_failed_row_is_distinguishable_without_colour(self) -> None:
        """
        失败与完成必须靠**文字**分得开，不能只差一个颜色。

        与 D 组「警告/错误靠文字前缀」是同一条理由：颜色在截图、
        配色异常的终端、端到端驱动抓到的纯文本里都可能丢失。
        """
        app = _Harness()
        async with app.run_test() as pilot:
            view = app.query_one(ActivityView)
            view.update_rows((row(status=TaskStatus.FAILED),))
            await pilot.pause()
            self.assertIn("失败", _text_of(view))

    async def test_rebuild_replaces_instead_of_appending(self) -> None:
        """
        整块重绘：连调两次不会留下上一次的行。

        增量更新要维护「哪一行对应哪个任务」的映射，那是一类典型的、
        出错后表现为「数字串行」的 bug。行数以并发上限（5）为界，
        重绘一次比算差异便宜，而且不会错。
        """
        app = _Harness()
        async with app.run_test() as pilot:
            view = app.query_one(ActivityView)
            view.update_rows((row("explorer"),))
            await pilot.pause()
            view.update_rows((row("planner"),))
            await pilot.pause()

            text = _text_of(view)
            self.assertIn("planner", text)
            self.assertNotIn("explorer", text)

    async def test_going_empty_hides_it_again(self) -> None:
        """终态行淡出之后整块要重新隐藏，不能留一个空壳占着两行。"""
        app = _Harness()
        async with app.run_test() as pilot:
            view = app.query_one(ActivityView)
            view.update_rows((row(),))
            await pilot.pause()
            view.update_rows(())
            await pilot.pause()

            self.assertFalse(view.display)
            self.assertEqual(_text_of(view), "")


class EscapeTest(unittest.IsolatedAsyncioTestCase):
    """
    AC22/N2：一切嵌入 markup 的自由文本都必须经**本项目的** `escape`。

    ⚠ 判据用「应用没崩」而不是「文本里有反斜杠」：`MarkupError` 抛在
    `get_content_height` 这类**布局阶段的主线程**调用里，不在业务调用栈上，
    没有任何 try/except 兜得住——Textual 直接拆掉整个 app。
    所以真实现场就是「跑着跑着突然退出」。
    """

    async def test_unclosed_bracket_in_member_name(self) -> None:
        """队员名来自模型给的 `run_agent` 参数，完全不可信。"""
        app = _Harness()
        async with app.run_test() as pilot:
            view = app.query_one(ActivityView)
            view.update_rows((row(name="wor[ker1(explorer)"),))
            await pilot.pause()

            self.assertTrue(app.is_running, "应用必须还活着")
            self.assertIn("\\[", _text_of(view))

    async def test_unclosed_bracket_in_recent_tool_text(self) -> None:
        """
        最近调用的文本里含路径与命令，`[` 是家常便饭。

        ⚠ 它在任务表里存的是**纯文本**（运行器刻意不转义），所以转义**必须**
        在这里做——两边都不做就是崩溃，两边都做就是用户看到字面的 `\\[`。
        """
        app = _Harness()
        async with app.run_test() as pilot:
            view = app.query_one(ActivityView)
            view.update_rows((row(recent=("Read(src/[wip.py)",)),), expanded=True)
            await pilot.pause()

            self.assertTrue(app.is_running)
            self.assertIn("\\[wip", _text_of(view))

    async def test_five_rows_with_brackets_all_survive(self) -> None:
        """
        并发跑满上限时每一行都含未闭合括号——真实崩溃需要凑够两个才触发，
        单行的用例可能因为 Textual 的容忍度而假绿。
        """
        app = _Harness()
        async with app.run_test() as pilot:
            view = app.query_one(ActivityView)
            view.update_rows(
                tuple(row(name=f"a[{i}", recent=(f"Read([{i}.py)",)) for i in range(5)),
                expanded=True,
            )
            await pilot.pause()

            self.assertTrue(app.is_running)


if __name__ == "__main__":
    unittest.main()
