"""
`tui/widgets.escape` 的护栏：**落单的 `[` 必须被转义**。

## 这条测试在防什么（一次真实崩溃）

现象：Rhine 在模型调用 `edit_file` 时**整个退出**，终端里留下一段 Textual 的
`MarkupError: Expected markup value`。偶发、无规律，长期未归因。

根因链条四步，每一步单独看都对：

1. `summarize_args` **先截断、后转义**：参数值超 30 字符即截断，于是
   `allowed_tools: [read_file, glob_files, grep_content, run_command]`
   被切成 `allowed_tools: [read_file, glo…` —— `[` 还在，配对的 `]` 没了；
2. `rich.markup.escape` 的正则 `(\\*)(\[[a-z#/@][^[]*?])` **要求闭合的 `]`**
   才认定这是标签，括号被切断后它整个放过，`[` 原样留在 markup 串里；
3. **Textual 的 Content markup 比 Rich 严格**：Rich 把落单 `[` 当普通文本，
   Textual 认定它是标签开头并去解析样式值，抛 `MarkupError`；
4. 抛出点在 `OptionList.get_content_height` —— **布局阶段的主线程**，
   不在 `show_for` 的调用栈上，没有任何 try/except 兜得住，Textual 直接拆掉 app。

触发条件很窄（某个值的前 30 字符内有 `[`、配对 `]` 在 30 字符之外），
所以它表现为「偶尔莫名其妙退出」。

## 为什么护栏要写成这个形状

**必须真的走一次 Textual 的布局计算**，不能只断言 `escape()` 的返回值。
崩溃发生在渲染期而不是构造期——只断言字符串的话，将来有人把 `escape` 换回
`rich.markup.escape`，字符串断言会失败没错，但如果有人改的是**调用点**
（比如新增一处忘了转义），字符串断言完全拦不住。
"""

from __future__ import annotations

import unittest

from rich.markup import escape as rich_escape
from textual.app import App, ComposeResult

from rhinecode.provider.base import ToolCall
from rhinecode.tui.widgets import ConfirmPanel, ToolCallWidget, escape, summarize_args


# 真实崩溃现场的原始参数（取自 G:\Rhine-test\c11-p1a 的会话存档）。
# 关键特征：`[` 在第 15 个字符、配对的 `]` 在第 30 个字符之外。
_OLD = "allowed_tools: [read_file, glob_files, grep_content, run_command]"
_NEW = "allowed_tools: [read_file, glob_files, grep_content, run_command, write_file, edit_file]"
_CRASH_ARGS = {
    "path": r"G:\Rhine-test\c11-p1a\.rhinecode\skills\frontend-design\SKILL.md",
    "old_string": _OLD,
    "new_string": _NEW,
}


class _Harness(App):
    """只挂一个确认面板的最小应用。"""

    def compose(self) -> ComposeResult:
        yield ConfirmPanel()


class EscapeSupersetTest(unittest.TestCase):
    """本模块的 escape 必须是 `rich.markup.escape` 的**严格超集**。"""

    def test_matches_rich_on_normal_inputs(self) -> None:
        """常规输入上与 rich 逐字相同——换掉 escape 不该改变既有观感。"""
        for text in (
            "普通文本",
            "[red]完整标签[/red]",
            r"路径 C:\a\b",
            "尾部反斜杠\\",
            "a[b]c",
            "",
        ):
            with self.subTest(text=text):
                self.assertEqual(escape(text), rich_escape(text))

    def test_escapes_unclosed_bracket_where_rich_does_not(self) -> None:
        """
        **本次修复的实质**：括号未闭合时 rich 放过、我们必须转义。

        这条是正向证据；反证是下面那条「rich 版本确实会崩」。
        """
        truncated = "allowed_tools: [read_file, glo…"
        self.assertEqual(rich_escape(truncated), truncated)      # rich 原样放过
        self.assertEqual(escape(truncated), "allowed_tools: \\[read_file, glo…")

    def test_non_string_input(self) -> None:
        """非字符串先 str()——调用点大量传 Path / 枚举 / None。"""
        self.assertEqual(escape(None), "None")
        self.assertEqual(escape(123), "123")


class MinimalCrashShapeTest(unittest.TestCase):
    """
    把「什么形状才会崩」这件事钉死——它比想象中窄，这正是本 bug 长期未被归因的原因。

    实测结论（`Content.from_markup` 直接判定）：
    - **一个**未闭合 `[`：Textual 容忍，不抛；
    - **两个**未闭合 `[` 且后面还跟着一个真标签的 `]`：抛
      `MarkupError: Expected markup value`。

    因为要凑齐两个，日常输入几乎撞不上；而 `edit_file` 的
    `old_string` + `new_string` **天然成对**，两个值又常常长得几乎一样——
    于是它成了这个 bug 最稳定的触发器。
    """

    _TWO_UNCLOSED = "old=allowed_tools: [read_file, glo, new=allowed_tools: [read_file, glo"

    def test_rich_escape_lets_the_crash_shape_through(self) -> None:
        """反证：rich 口径下这个形状确实会让 Textual 抛。"""
        from textual.content import Content
        from textual.markup import MarkupError

        with self.assertRaises(MarkupError):
            Content.from_markup(f"[dim]{rich_escape(self._TWO_UNCLOSED)}[/dim]")

    def test_our_escape_neutralises_it(self) -> None:
        """本模块的 escape 把同一形状变成可安全渲染的纯文本。"""
        from textual.content import Content

        content = Content.from_markup(f"[dim]{escape(self._TWO_UNCLOSED)}[/dim]")
        # 括号作为字面量原样呈现给用户，没有被当成标签吞掉
        self.assertIn("[read_file", content.plain)

    def test_single_unclosed_bracket_is_tolerated(self) -> None:
        """
        单个未闭合括号 Textual 本来就容忍——记录在案，免得后人以为漏测了。

        这条也解释了为什么同一次调用里工具行没崩、确认面板崩了：
        工具行用 `max_len=60`，在第二个括号出现前就截断了。
        """
        from textual.content import Content

        Content.from_markup(f"[dim]{rich_escape('我先看下 [read_file')}[/dim]")


class ConfirmPanelRendersTruncatedBracketTest(unittest.IsolatedAsyncioTestCase):
    """确认面板遇上「被截断的方括号」必须能正常渲染，而不是崩掉整个应用。"""

    async def test_panel_survives_layout_pass(self) -> None:
        """
        真实崩溃现场的完整重演：工具行 + 确认面板 + **一次布局计算**。

        布局那一步不可省——原来的 `MarkupError` 正是在
        `OptionList.get_content_height` 里抛出的，构造期一切正常。
        """
        summary = summarize_args(_CRASH_ARGS, max_len=200)
        # 前置断言：确认这份参数确实造出了「括号被截断」的形态，
        # 否则将来 summarize_args 的截断长度一改，这条用例会静默失去意义。
        self.assertIn("\\[read_file", summary)
        self.assertNotIn("]", summary.split("\\[read_file")[1][:20])

        tool_call = ToolCall(id="c1", name="edit_file", arguments=_CRASH_ARGS)
        app = _Harness()
        async with app.run_test() as pilot:
            await app.mount(ToolCallWidget(tool_call))
            await pilot.pause()
            panel = app.query_one(ConfirmPanel)
            panel.show_for(tool_call, None, None)
            await pilot.pause()
            # 强制走一次高度计算：这就是原来抛 MarkupError 的那一步
            panel.get_content_height(panel.styles, 100, 100)
            await pilot.pause()

    async def test_reverse_proof_rich_escape_would_crash(self) -> None:
        """
        **反证**：按修复前的口径（先截断、再用 rich 转义）组装同一条表头，
        Textual 确实会抛 `MarkupError`。

        没有这条，上面那条用例无法区分「修复生效了」与「这个场景本来就不会崩」。

        这里逐字重演修复前的 `summarize_args`——不复用产品函数，
        因为产品函数已经修好了，复用就证明不了任何事。
        """
        from textual.markup import MarkupError
        from textual.widgets.option_list import Option

        def _old_summarize(arguments: dict, max_len: int) -> str:
            """修复前的实现：截断在前、rich 转义在后。"""
            parts = []
            for key, value in arguments.items():
                text = str(value).replace("\n", " ")
                if len(text) > 30:
                    text = text[:30] + "…"
                parts.append(f"{key}={text}")
            summary = ", ".join(parts)
            if len(summary) > max_len:
                summary = summary[:max_len] + "…"
            return rich_escape(summary)

        bad = _old_summarize(_CRASH_ARGS, max_len=200)
        self.assertIn("[read_file", bad)          # rich 确实没转义掉它
        self.assertNotIn("\\[read_file", bad)

        app = _Harness()
        async with app.run_test() as pilot:
            panel = app.query_one(ConfirmPanel)
            panel.clear_options()
            panel.add_option(
                Option(f"[#FFA500]⚠ 确认执行：edit_file({bad})[/#FFA500]", disabled=True)
            )
            with self.assertRaises(MarkupError):
                panel.get_content_height(panel.styles, 100, 100)
            await pilot.pause()


if __name__ == "__main__":
    unittest.main()
