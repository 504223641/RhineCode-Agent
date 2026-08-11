"""
命令报告分级的纯函数单测（tui-display 扩展 T7/T9，spec F16/F17/AC13）。

本文件覆盖两个纯函数：

- `classify_report`：把一段报告逐行判定层级。它是 C 组的全部判定逻辑——
  spec F16 要求**报告的产出函数一字不改**，因此分级只能在展示层靠「行的形状」
  推断。判错的代价只是某一行的亮度不对，不会出功能问题。
- `numbered_prompt`：面板选项的「序号 + 高亮指示符」拼装（E 组 F23/F24）。

⚠ 判定顺序是本文件最要紧的部分：**条目判定必须排在缩进判定之前**。
反过来的话，一个缩进 6 格的条目会被判成 DETAIL、整段条目变暗，
而这在单看某一份报告时未必看得出来。下面有一条专门的反证用例钉住它。
"""

import unittest

from rhinecode.tui.widgets import (
    BRANCH_MARK,
    ERROR_PREFIX,
    WARNING_PREFIX,
    BRANCH_PREFIX,
    SECONDARY_COLOR,
    ReportLineKind,
    ToolCallWidget,
    classify_report,
    numbered_prompt,
)


# 一份形态与真实 `/agents` 报告完全一致的样本（取自 subagents/report.py 的
# 实际拼法：标题 → 空行 → 段落标题 → `  • 条目` → `    详情`）。
# 刻意用真实形态而不是自造的极简字符串——自造的样本证明不了「对真实报告有效」。
AGENTS_REPORT = """子 Agent 角色与任务

已加载角色（3 个）：
  • explorer（内置）
    说明：只读调研：在代码库里找东西、读文件、回答「现在是什么样」
    工具：read_file、glob_files、grep_content
    模型：继承主对话 · 轮次上限：15 · 权限：严格（实际生效：默认）
  • planner（内置）
    说明：只读方案：回答「接下来该怎么做」，不动手

本次运行的任务（1 个）：
  • a3f1c9 · explorer · 已完成 · 14 轮 · 28500 token · 72.3s
    权限层的判定顺序是①黑名单②沙箱

项目级目录：G:\\RhineCode-Agent\\.rhinecode\\agents
改动角色定义后需重启生效（本章不提供 reload）。"""


def kinds_of(text: str) -> list[ReportLineKind]:
    return [kind for kind, _ in classify_report(text)]


class ClassifyReportTest(unittest.TestCase):
    def test_real_agents_report_gets_four_levels(self) -> None:
        """真实报告必须分出四种层级——只要有一种没出现，分级就没起作用。"""
        kinds = set(kinds_of(AGENTS_REPORT))
        self.assertEqual(
            kinds,
            {
                ReportLineKind.TITLE,
                ReportLineKind.SECTION,
                ReportLineKind.ITEM,
                ReportLineKind.DETAIL,
                ReportLineKind.BLANK,
            },
        )

    def test_line_by_line(self) -> None:
        """逐行核对前八行，这是判定规则的正面样本。"""
        pairs = classify_report(AGENTS_REPORT)
        self.assertEqual(pairs[0][0], ReportLineKind.TITLE)
        self.assertEqual(pairs[0][1], "子 Agent 角色与任务")
        self.assertEqual(pairs[1][0], ReportLineKind.BLANK)
        self.assertEqual(pairs[2][0], ReportLineKind.SECTION)
        self.assertEqual(pairs[3][0], ReportLineKind.ITEM)
        self.assertEqual(pairs[4][0], ReportLineKind.DETAIL)
        self.assertEqual(pairs[5][0], ReportLineKind.DETAIL)
        self.assertEqual(pairs[6][0], ReportLineKind.DETAIL)
        self.assertEqual(pairs[7][0], ReportLineKind.ITEM)

    def test_returns_original_line_untouched(self) -> None:
        """
        返回的是**原始行**，不做任何转义或裁剪。

        转义必须留给渲染方：本函数不知道产出会被拼进 markup 还是纯文本，
        在这里转会让同一段文本被转两次（用户看到字面的 `\\[`）。
        """
        text = "标题\n  • 含 [方括号] 的条目"
        pairs = classify_report(text)
        self.assertEqual(pairs[1][1], "  • 含 [方括号] 的条目")

    def test_bullet_wins_over_indent(self) -> None:
        """
        **反证**：条目判定必须排在缩进判定之前。

        缩进 6 格的 `•` 行仍是条目。若顺序反了它会被判成 DETAIL，
        整段条目跟着变暗——而那在单看一份报告时很难察觉。
        """
        self.assertEqual(kinds_of("标题\n      • 深缩进的条目")[1], ReportLineKind.ITEM)

    def test_three_bullet_shapes(self) -> None:
        """三种项目符号都算条目（各报告用的不是同一个）。"""
        for bullet in ("•", "-", "·"):
            with self.subTest(bullet=bullet):
                self.assertEqual(
                    kinds_of(f"标题\n  {bullet} 一条")[1], ReportLineKind.ITEM
                )

    def test_indent_boundary_is_four(self) -> None:
        """3 格缩进还不算详情，4 格才算——边界值两侧各验一次。"""
        self.assertEqual(kinds_of("标题\n   三格")[1], ReportLineKind.SECTION)
        self.assertEqual(kinds_of("标题\n    四格")[1], ReportLineKind.DETAIL)

    def test_whitespace_only_line_is_blank(self) -> None:
        """纯空白行按空行处理，不能因为「缩进够深」被判成详情。"""
        self.assertEqual(kinds_of("标题\n        ")[1], ReportLineKind.BLANK)

    def test_empty_text(self) -> None:
        self.assertEqual(classify_report(""), ())

    def test_blank_first_line_is_not_title(self) -> None:
        """首行为空时不硬套 TITLE——那会让一个空行被加粗成标题。"""
        self.assertEqual(kinds_of("\n真正的标题")[0], ReportLineKind.BLANK)

    def test_single_line_report(self) -> None:
        """只有一行的报告，那一行就是标题。"""
        self.assertEqual(kinds_of("未启用协作。"), [ReportLineKind.TITLE])


class NumberedPromptTest(unittest.TestCase):
    """E 组 F23/F24：序号 + 非颜色的高亮指示符。"""

    def test_selected_uses_arrow(self) -> None:
        self.assertTrue(numbered_prompt(1, "本次放行", True).startswith("> 1. "))

    def test_unselected_uses_spaces(self) -> None:
        self.assertTrue(numbered_prompt(2, "本会话放行", False).startswith("  2. "))

    def test_prefix_width_is_equal(self) -> None:
        """
        选中与未选中的前缀**必须等宽**，否则高亮在选项间移动时整列文字会左右抖。

        这是 `>` 加一个空格、对上两个空格的全部理由。
        """
        selected = numbered_prompt(1, "文本", True)
        plain = numbered_prompt(1, "文本", False)
        self.assertEqual(len(selected), len(plain))

    def test_text_is_preserved(self) -> None:
        self.assertIn("本会话放行", numbered_prompt(2, "本会话放行", False))

    def test_does_not_escape(self) -> None:
        """
        与 `classify_report` 同口径：转义留给调用方。

        三个面板传进来的文本有的已是 markup（含 `[dim]…[/dim]`），
        在这里转义会把那些样式标签打成字面量。
        """
        self.assertIn("[dim]说明[/dim]", numbered_prompt(1, "放行 [dim]说明[/dim]", False))


class SingleSourceTest(unittest.TestCase):
    """
    **结构护栏**（AC12）：分支符号与次级配色只有一处定义。

    改造前工具行、回放行、diff 块各写了一遍 `"  ⎿  "` 与 `#808080`。
    本轮新增活动区与报告两个使用方之后，散着写必然分叉——而分叉**不报错**，
    只是三处的灰度慢慢对不上，最后没人说得清哪个才是对的。
    """

    def test_tool_widget_branch_color_points_at_the_shared_constant(self) -> None:
        self.assertIs(ToolCallWidget._COLOR_BRANCH, SECONDARY_COLOR)

    def test_branch_prefix_is_built_from_the_mark(self) -> None:
        self.assertIn(BRANCH_MARK, BRANCH_PREFIX)

    def test_no_literal_branch_prefix_left_in_source(self) -> None:
        """
        源码里不得再出现字面的 `"  ⎿  "`——它必须走 `BRANCH_PREFIX`。

        这条比「断言两个常量相等」有分辨力：后者拦不住有人在新代码里
        又手写一遍那个前缀。
        """
        from pathlib import Path

        import rhinecode.tui.widgets as widgets_module

        source = Path(widgets_module.__file__).read_text(encoding="utf-8")
        # 常量自身那一行用 f-string 由 BRANCH_MARK 拼出，不含这个字面量
        self.assertNotIn('"  ⎿  "', source)
        self.assertNotIn('f"  ⎿  ', source)


class RenderReportTest(unittest.IsolatedAsyncioTestCase):
    """
    AC13a：`/agents` 那类报告呈现出分级，整段不再是同一个暗色。

    判据取 **markup 原文**而不是渲染出来的纯文本：分级的全部内容就是样式，
    看纯文本的话四种级别长得一模一样，用例会在「分级完全失效」时照样通过。
    """

    async def _render(self, text: str) -> str:
        from textual.app import App, ComposeResult

        from rhinecode.tui.widgets import HistoryView

        class _Harness(App):
            def compose(self) -> ComposeResult:
                yield HistoryView()

        app = _Harness()
        async with app.run_test() as pilot:
            view = app.query_one(HistoryView)
            view.append_report(text)
            await pilot.pause()
            child = list(view.query_one("#history-messages").children)[-1]
            content = child.content
            return content if isinstance(content, str) else str(content)

    async def test_at_least_three_distinct_styles(self) -> None:
        markup = await self._render(AGENTS_REPORT)
        self.assertIn("bold #7AEEFF", markup, "首行要加粗 + 强调色")
        self.assertIn("[bold]", markup, "段落标题要加粗")
        self.assertIn(SECONDARY_COLOR, markup, "次级信息要暗")

    async def test_no_bullet_symbol_on_the_title(self) -> None:
        """
        F17：首行**不发前缀符号**。

        按 F29 收敛后的词汇表，`●` 专属于工具行与活动行；为报告再造一个图形
        会让符号表重新变杂，而符号一多每个的语义就都记不住了。
        """
        markup = await self._render(AGENTS_REPORT)
        self.assertNotIn("● 子 Agent 角色与任务", markup)

    async def test_report_text_is_escaped(self) -> None:
        """
        报告里嵌着路径、错误消息、任务标题与模型产出的结论——全是可能含字面
        `[` 的自由文本。
        """
        markup = await self._render("标题\n  • 任务 [未闭合括号")
        self.assertIn("\\[未闭合", markup)

    async def test_whole_report_is_not_one_dim_block(self) -> None:
        """
        **反证**：改造前整段被包进一个 `[dim]`。

        没有这条，把 `append_report` 写成 `append_system` 的别名也能让上面
        那些「有某某样式」的断言部分通过。
        """
        markup = await self._render(AGENTS_REPORT)
        self.assertFalse(
            markup.startswith("[dim]"), f"整段不该再是一个暗色块：{markup[:60]}"
        )


class SystemLevelTest(unittest.IsolatedAsyncioTestCase):
    """
    D 组：四条系统行通道各不相同（tui-display 扩展 F19/F21，AC14/AC15）。

    改造前「子 Agent 完成」与「记忆已更新」走同一条 `[dim]` 通道，
    于是一屏里最要紧的那条和最可忽略的那条长得一模一样。
    """

    async def _markups(self) -> dict:
        from textual.app import App, ComposeResult

        from rhinecode.tui.widgets import HistoryView

        class _Harness(App):
            def compose(self) -> ComposeResult:
                yield HistoryView()

        out = {}
        app = _Harness()
        async with app.run_test() as pilot:
            view = app.query_one(HistoryView)
            view.append_system("记忆已更新（3 条）")
            view.append_event("explorer 已完成")
            view.append_warning("仍有 2 个子 Agent 在后台运行")
            view.append_error("连接超时，已重试 2 次")
            await pilot.pause()
            children = list(view.query_one("#history-messages").children)
            for name, child in zip(("notice", "event", "warning", "error"), children):
                content = child.content
                out[name] = content if isinstance(content, str) else str(content)
        return out

    async def test_four_channels_are_all_different(self) -> None:
        markups = await self._markups()
        self.assertEqual(
            len(set(m.split("]")[0] for m in markups.values())),
            4,
            f"四条通道的样式必须互不相同：{markups}",
        )

    async def test_warning_and_error_carry_text_prefixes(self) -> None:
        """
        AC15：去掉颜色之后**警告与错误**仍可辨认。

        这是 F21 推翻「每级各发一个图形」之后剩下的那道保证——
        截图、配色异常的终端、端到端驱动抓到的纯文本里颜色都可能丢失。
        """
        markups = await self._markups()
        self.assertIn(WARNING_PREFIX, markups["warning"])
        self.assertIn(ERROR_PREFIX, markups["error"])

    async def test_notice_and_event_differ_only_in_brightness(self) -> None:
        """
        F21：提示级与事件级之间**只差亮度**，这是刻意的。

        误读这两者的代价为零——把一条「记忆已更新」当成事件，不会导致任何
        错误决策。真正会让人做错决定的是漏看警告与错误，那两级由文字承担。
        """
        markups = await self._markups()
        self.assertTrue(markups["notice"].startswith("[dim]"))
        self.assertFalse(markups["event"].startswith("["), "事件级不该带任何样式包裹")

    async def test_no_invented_level_glyphs(self) -> None:
        """
        F21/F29：不存在为分级发明的专属图形。

        本条曾设计成 `·` / `◆` / `▲` / `×` 四个前缀符号，已推翻——
        Claude Code 不给严重级别发图形，自创图形是「符号越加越杂」的来源。
        `●` 也不行：它专属于工具行与活动行。
        """
        markups = await self._markups()
        for name, markup in markups.items():
            for glyph in ("◆", "▲", "×", "●"):
                with self.subTest(channel=name, glyph=glyph):
                    self.assertNotIn(glyph, markup)


class LevelAssignmentTest(unittest.TestCase):
    """
    F20：每个调用点都有明确级别，**不留兜底**。

    本组的全部价值就在于把「重要的」从「可忽略的」里分出来，留一个
    「认不出就按提示级」的默认分支等于没分。
    """

    def test_finish_reasons_are_split_into_two_levels(self) -> None:
        """
        六种结束原因分两档，判据是「用户看到之后要不要做点什么」。

        - 事件级：「已取消」「计划未执行」是用户自己刚做的决定的回执；
        - 警告级：迭代上限 / 未知工具 / 流错误都是**任务没做完就停了**，
          用户多半要重试或改写请求。漏看这三条会让人以为任务成功了。
        """
        from rhinecode.agent.events import StopReason
        from rhinecode.tui.app import LEVEL_EVENT, LEVEL_WARNING, RhineApp

        expected = {
            StopReason.USER_CANCELLED: LEVEL_EVENT,
            StopReason.PLAN_REJECTED: LEVEL_EVENT,
            StopReason.MAX_ITERATIONS: LEVEL_WARNING,
            StopReason.UNKNOWN_TOOL: LEVEL_WARNING,
            StopReason.STREAM_ERROR: LEVEL_WARNING,
        }
        for reason, level in expected.items():
            with self.subTest(reason=reason):
                got_level, text = RhineApp._finish_line(reason, "")
                self.assertEqual(got_level, level)
                self.assertTrue(text, "非自然结束必须有可展示的文本")

    def test_natural_completion_shows_nothing(self) -> None:
        """自然完成不打扰用户——这条行为改造前后一字不变。"""
        from rhinecode.agent.events import StopReason
        from rhinecode.tui.app import RhineApp

        _, text = RhineApp._finish_line(StopReason.COMPLETED, "")
        self.assertEqual(text, "")

    def test_finish_lines_carry_no_emoji(self) -> None:
        """
        F28：结束提示里不再有 `⏹` / `⚠`。

        警告级由 widget 统一加「警告：」文字前缀，留着 `⚠` 会变成
        「警告：⚠ 已达迭代上限」。
        """
        from rhinecode.agent.events import StopReason
        from rhinecode.tui.app import RhineApp

        for reason in StopReason:
            with self.subTest(reason=reason):
                _, text = RhineApp._finish_line(reason, "")
                for glyph in ("⏹", "⚠", "🔄"):
                    self.assertNotIn(glyph, text)

    def test_unknown_level_raises_instead_of_degrading(self) -> None:
        """
        **反证**：认不出的级别当场炸，不静默退回提示级。

        没有这条，任何一处拼错的级别都会悄悄降到最暗的那一档，
        而界面上只表现为「那条消息不太显眼」——没人会去查。
        """
        from rhinecode.tui.app import RhineApp
        from rhinecode.tui.widgets import HistoryView

        with self.assertRaises(KeyError):
            RhineApp._history_channel(HistoryView(), "拼错的级别")


if __name__ == "__main__":
    unittest.main()
