"""
`read_file` 输出预算的护栏（2026-09-17）。

## 这组用例钉的是什么

在此之前 `read_file` 的输出**实际上没有体量上限**：整文件读只在超过 1 MiB 时
才拒绝，于是一个 900 KB 的文件会被整个塞进历史（约 30 万 token）；范围读只限
行数、不限行长。

那个洞原先由 c8 第一层存盘兜着——塞进去、下一轮立刻换成占位。第一层已删
（上游两家都没有「事后删历史」这种机制，一律在产出那一刻限量），因此闸门收到
了工具自己这里。

四个要点，每条对应一种改坏了却不报错的形态：

1. 正常大小的文件一个字都不许少（别把限额调得太紧，那会复刻原来的病）；
2. 超预算的文件要截，且**必须告诉模型从第几行接着读**——只说「已截断」会让
   它要么拿半个文件当全文继续推理，要么猜一个范围重试；
3. 被截掉的内容必须进 `full_output`（那是一条已登记的成对维护点：工具主动裁剪
   了 output 就必须另存完整原文，否则那段内容在行为记录里也永久消失）；
4. 超长单行**不能**给 `start_line` 建议——被切掉的是这一行的后半截，
   而 `start_line=N+1` 正好跳过它。给一条走不通的建议比不给更糟。
"""

import tempfile
import unittest
from pathlib import Path

from rhinecode.tools.read_file import (
    LONG_LINE_HINT,
    READ_OUTPUT_MAX_CHARS,
    ReadFileTool,
)


class ReadFileBudgetTest(unittest.TestCase):
    """读取结果按字符预算分页。"""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.cwd = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)
        self.tool = ReadFileTool()

    def _write(self, name: str, text: str) -> str:
        (self.cwd / name).write_text(text, encoding="utf-8")
        return name

    @staticmethod
    def _source_lines(count: int) -> str:
        """造一段像源码的文本，每行约 80 字符。"""
        return "\n".join(
            f"def func_{i}(arg):  # {'x' * 55}" for i in range(count)
        )

    # ------------------------------------------------------------------ #
    # 1. 正常大小的文件不许动
    # ------------------------------------------------------------------ #
    def test_an_ordinary_source_file_is_returned_whole(self) -> None:
        """
        一千行的源码文件（约 80 KB）必须完整返回。

        ⚠ 这条是「别把限额调紧」的护栏。本项目自己最大的源文件
        `agent/loop.py` 是 2300 多行——限额必须容得下真实世界的文件，
        否则就是换一种方式复刻「模型拿不到它要的东西」那个病。
        """
        name = self._write("ordinary.py", self._source_lines(1000))
        res = self.tool.execute({"path": name}, cwd=self.cwd)

        self.assertTrue(res.ok, res.output)
        self.assertNotIn("只显示到第", res.output, "一千行的普通源文件不该被分页")
        self.assertIsNone(res.full_output, "没截断时不该另存完整版")
        self.assertIn("func_0(", res.output)
        self.assertIn("func_999(", res.output)

    # ------------------------------------------------------------------ #
    # 2. 超预算要截，且要说清怎么接着读
    # ------------------------------------------------------------------ #
    def test_a_huge_file_is_paged_with_an_actionable_hint(self) -> None:
        """超预算时给第一页 + 一个**能直接用**的续读参数。"""
        total = READ_OUTPUT_MAX_CHARS // 80 + 2000
        name = self._write("huge.py", self._source_lines(total))
        res = self.tool.execute({"path": name}, cwd=self.cwd)

        self.assertTrue(res.ok, res.output)
        # 给模型的那份收在预算附近（表头与提示各占几十字）
        self.assertLess(len(res.output), READ_OUTPUT_MAX_CHARS + 2000)
        self.assertIn("func_0(", res.output, "第一页必须从文件开头给起")

        # ⚠ 关键：提示里必须有能直接照做的 start_line
        self.assertIn("start_line=", res.output)
        # 那个数字必须真的能接上——照它再读一次，拿到的第一行正好是断点的下一行
        after = res.output.split("start_line=")[1].split(" ")[0].rstrip("）")
        follow = self.tool.execute(
            {"path": name, "start_line": int(after), "max_lines": 5}, cwd=self.cwd
        )
        self.assertTrue(follow.ok, follow.output)
        self.assertIn(f"{after}│", follow.output, "续读没有从提示说的那一行开始")

    def test_the_clipped_content_is_kept_for_the_trace(self) -> None:
        """
        被截掉的内容进 `full_output`。

        这是一条**已登记的成对维护点**：工具主动裁剪 `output` 就必须同时填
        `full_output`，否则那段内容在行为记录里也一并消失——而记录看起来仍然
        是完整的，因为被裁掉的地方连痕迹都没有。
        """
        total = READ_OUTPUT_MAX_CHARS // 80 + 2000
        name = self._write("huge.py", self._source_lines(total))
        res = self.tool.execute({"path": name}, cwd=self.cwd)

        self.assertIsNotNone(res.full_output)
        assert res.full_output is not None  # 给类型检查看的
        self.assertIn(f"func_{total - 1}(", res.full_output, "完整版里应当有最后一行")
        self.assertNotIn("只显示到第", res.full_output, "完整版不该带分页提示")

    def test_a_range_read_is_also_capped(self) -> None:
        """
        范围读同样受字符预算约束——它只限行数、不限行长。

        ⚠ **`full_output` 那一半必须单独断言。** 两条读取路径各填各的，
        只断言整文件那条时，把范围读这条的 `full_output` 改成 `None`
        照样全绿（变异实测确认过）。
        """
        total = READ_OUTPUT_MAX_CHARS // 80 + 2000
        name = self._write("huge.py", self._source_lines(total))
        res = self.tool.execute(
            {"path": name, "start_line": 1, "max_lines": 2000}, cwd=self.cwd
        )

        self.assertTrue(res.ok, res.output)
        self.assertLess(len(res.output), READ_OUTPUT_MAX_CHARS + 2000)
        self.assertIn("start_line=", res.output)

        self.assertIsNotNone(res.full_output, "范围读被截断时也要另存完整原文")
        assert res.full_output is not None  # 给类型检查看的
        self.assertGreater(len(res.full_output), len(res.output))

    def test_a_range_read_that_fits_leaves_full_output_unset(self) -> None:
        """范围读没被截断时不另存——两份一模一样只会让记录文件白白翻倍。"""
        name = self._write("ordinary.py", self._source_lines(1000))
        res = self.tool.execute(
            {"path": name, "start_line": 1, "max_lines": 20}, cwd=self.cwd
        )

        self.assertTrue(res.ok, res.output)
        self.assertIsNone(res.full_output)

    def test_hitting_the_line_cap_also_says_how_to_continue(self) -> None:
        """
        行数上限先到时也要给续读参数。

        原先那句只说「后续内容未显示」，不说从哪儿接着读——同一条教训
        （拒绝/截断的文案要说清「怎么办」）。
        """
        name = self._write("ordinary.py", self._source_lines(100))
        res = self.tool.execute(
            {"path": name, "start_line": 1, "max_lines": 10}, cwd=self.cwd
        )

        self.assertTrue(res.ok, res.output)
        self.assertIn("start_line=11", res.output)

    # ------------------------------------------------------------------ #
    # 3. 反证：超长单行不能给 start_line
    # ------------------------------------------------------------------ #
    def test_a_single_overlong_line_does_not_suggest_start_line(self) -> None:
        """
        一行就超过整份预算时，**不许**建议 `start_line` 续读。

        ⚠ **这条是反证。** 把两种情况合并成一句提示看起来是「简化」，而且
        其余用例照样全绿——但那条建议在这里是错的：被切掉的是第 1 行的后半截，
        `start_line=2` 会从第 2 行开始，正好跳过它。模型会照做，然后拿着一份
        缺了一大块的内容继续推理，而它看不出有什么不对。
        """
        name = self._write("bundle.js", "Z" * (READ_OUTPUT_MAX_CHARS * 3))
        res = self.tool.execute({"path": name}, cwd=self.cwd)

        self.assertTrue(res.ok, res.output)
        self.assertNotIn("start_line=", res.output, "超长单行不该给续读参数")
        self.assertIn(LONG_LINE_HINT.strip(), res.output)

    def test_a_single_overlong_line_still_shows_real_content(self) -> None:
        """超长单行截完必须仍有实际内容，不能只剩一句提示。"""
        name = self._write("bundle.js", "Z" * (READ_OUTPUT_MAX_CHARS * 3))
        res = self.tool.execute({"path": name}, cwd=self.cwd)

        self.assertGreater(res.output.count("Z"), READ_OUTPUT_MAX_CHARS // 2)
        self.assertIn("本行过长", res.output, "行内截断必须就地说明")


if __name__ == "__main__":
    unittest.main()
