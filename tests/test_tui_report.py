"""
命令报告分级的纯函数单测（tui-display 扩展 T7/T9，spec F16/F17/AC13）。

本文件覆盖报告分级的纯函数：

- `classify_report`：把一段报告逐行判定层级。它是 C 组的全部判定逻辑——
  spec F16 要求**报告的产出函数一字不改**，因此分级只能在展示层靠「行的形状」
  推断。判错的代价只是某一行的亮度不对，不会出功能问题。

⚠ 判定顺序是本文件最要紧的部分：**条目判定必须排在缩进判定之前**。
反过来的话，一个缩进 6 格的条目会被判成 DETAIL、整段条目变暗，
而这在单看某一份报告时未必看得出来。下面有一条专门的反证用例钉住它。
"""

import unittest

from rhinecode.tui.widgets import (
    BRANCH_MARK,
    BRANCH_PREFIX,
    SECONDARY_COLOR,
    ReportLineKind,
    ToolCallWidget,
    classify_report,
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


if __name__ == "__main__":
    unittest.main()
