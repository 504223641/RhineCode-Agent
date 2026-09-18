"""
`run_command` 输出预算的护栏（2026-09-17）。

## 这组用例钉的是什么

原先的判据是「前 30 行 + 后 10 行」。真实 trace 实录：模型打印 128 行源码，
拿到 40 行、中间 88 行没了；于是它把切片越切越小，连续十几轮都在重取同一段
内容，最终撞迭代上限、一个文件都没写出来。

判据因此从**行数**换成**字符预算**（30 000 字符，对齐 Claude Code 的 Bash
默认值）。这里的四条分别钉住那次改动的四个要点，**每一条对应一种改坏了却
不报错的形态**：

1. 够小的输出一个字都不许少——这是那次症状的直接反面；
2. 真正的大输出仍然要裁（反证：少了它，把预算设成无穷大也能让第 1 条绿）；
3. stderr 有保底额度，不会被刷屏的 stdout 挤掉；
4. 判据确实是**字符**而不是行数（反证：几千行短输出必须完整保留，
   而少数几行超长输出必须被裁——用行数算的话这两条的结论正好反过来）。
"""

import tempfile
import unittest
from pathlib import Path

from rhinecode.tools.run_command import (
    RUN_OUTPUT_MAX_CHARS,
    RUN_STDERR_MIN_CHARS,
    RunCommandTool,
    _split_budget,
)


class RunCommandBudgetTest(unittest.TestCase):
    """给模型的命令输出按字符预算裁剪。"""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.cwd = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)
        self.tool = RunCommandTool()

    def _run(self, script: str):
        """跑一条 python 单行脚本并返回 ToolResult。

        ⚠ 必须是**单行**：经 `shell=True` 传下去时字面的 `\n` 不是换行，
        python 会直接报语法错，而断言会失败在一个毫不相干的地方。
        """
        return self.tool.execute({"command": f'python -c "{script}"'}, cwd=self.cwd)

    # ------------------------------------------------------------------ #
    # 1. 症状的直接反面
    # ------------------------------------------------------------------ #
    def test_a_hundred_line_source_dump_survives_intact(self) -> None:
        """
        128 行源码规模的输出必须一个字都不少。

        这正是真实 trace 里被砍成 40 行的那一次：128 行、6 322 字符。
        旧判据（30+10 行）下它必然被裁，新判据下它远在预算之内。
        """
        # 每行约 50 字符 × 128 行 ≈ 6 400 字符，稳稳在 30 000 以内
        script = "[print('%03d| some_source_line_that_is_about_fifty_chars' % i) for i in range(128)]"
        res = self._run(script)
        self.assertTrue(res.ok, res.output)

        self.assertNotIn("省略中间", res.output, "128 行的输出不该被裁")
        self.assertIsNone(res.full_output, "没裁剪时不该另存完整版")
        # 首、中、尾三个位置都在
        for probe in ("000|", "064|", "127|"):
            self.assertIn(probe, res.output, f"输出里应当有 {probe}")

    # ------------------------------------------------------------------ #
    # 2. 反证：真的大起来还是要裁
    # ------------------------------------------------------------------ #
    def test_still_clips_a_huge_output(self) -> None:
        """
        远超预算的输出仍然要裁，且完整原文进 `full_output`。

        ⚠ **这条是第 1 条的反证**：没有它的话，把预算改成无穷大
        （或者干脆去掉裁剪）也能让第 1 条通过，而那会让一次
        `pytest` 的输出撑爆上下文。
        """
        lines = RUN_OUTPUT_MAX_CHARS // 7 + 1000   # 每行 `L00042` + 换行 = 7 字符
        res = self._run(f"[print('L%05d' % i) for i in range({lines})]")
        self.assertTrue(res.ok, res.output)

        self.assertIn("省略中间", res.output)
        self.assertIsNotNone(res.full_output)
        assert res.full_output is not None  # 给类型检查看的

        # 给模型的那份确实收在预算附近（留一倍余量给命令回显与两行表头）
        self.assertLess(len(res.output), RUN_OUTPUT_MAX_CHARS * 2)
        # 完整版一行不少
        body = [ln for ln in res.full_output.splitlines() if ln.startswith("L")]
        self.assertEqual(len(body), lines)

    # ------------------------------------------------------------------ #
    # 3. stderr 的保底额度
    # ------------------------------------------------------------------ #
    def test_stderr_keeps_its_floor_when_stdout_is_huge(self) -> None:
        """
        stdout 刷屏时，stderr 里那几行报错必须完整保留。

        这是命令失败时唯一有用的信息。按体量平分预算的话它会被挤掉——
        而失败的命令恰恰常常同时刷一屏 stdout（编译进度、测试进度）。
        """
        lines = RUN_OUTPUT_MAX_CHARS // 7 + 1000
        script = (
            "import sys;"
            f"[print('L%05d' % i) for i in range({lines})];"
            "print('FATAL-marker-line', file=sys.stderr)"
        )
        res = self._run(script)
        self.assertTrue(res.ok, res.output)

        self.assertIn("FATAL-marker-line", res.output, "stderr 的报错行被挤掉了")

    def test_split_budget_gives_stdout_everything_when_stderr_is_empty(self) -> None:
        """stderr 为空时，保底额度不占地方——整份预算归 stdout。"""
        out_budget, err_budget = _split_budget(999_999, 0)
        self.assertEqual(err_budget, 0)
        self.assertEqual(out_budget, RUN_OUTPUT_MAX_CHARS)

    def test_split_budget_honours_the_stderr_floor(self) -> None:
        """stderr 有内容时至少拿到保底额度。"""
        _, err_budget = _split_budget(999_999, 999_999)
        self.assertGreaterEqual(err_budget, RUN_STDERR_MIN_CHARS)

    # ------------------------------------------------------------------ #
    # 4. 反证：判据是字符，不是行数
    # ------------------------------------------------------------------ #
    def test_many_short_lines_are_not_clipped(self) -> None:
        """
        行数远超旧上限、但字符总量在预算内 → 不许裁。

        ⚠ **这条与下一条是一对**，它们钉住「判据换成了字符」这件事本身：
        按行数算的话，本条会被裁（2 000 行 ≫ 40 行）而下一条不会（3 行 < 40 行）
        ——结论正好与现在相反。任何一条单独存在都挡不住把判据改回行数。
        """
        res = self._run("[print(i) for i in range(2000)]")   # 约 8 900 字符
        self.assertTrue(res.ok, res.output)
        self.assertNotIn("省略中间", res.output)

    def test_a_few_very_long_lines_are_clipped(self) -> None:
        """行数只有 3、但字符总量远超预算 → 必须裁，且不能只剩一句省略提示。"""
        res = self._run("[print('X' * 40000) for _ in range(3)]")
        self.assertTrue(res.ok, res.output)

        self.assertIn("省略中间", res.output)
        # ⚠ 超长单行会让「按行保留」的两端双双落空，早期实现在这里
        #    只吐出一句省略提示、一个字内容都没有。必须真的有内容。
        self.assertGreater(res.output.count("X"), 1000, "裁剪后应当仍有实际内容")


if __name__ == "__main__":
    unittest.main()
