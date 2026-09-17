"""
`grep_content` 单行长度上限的护栏（2026-09-17）。

`MAX_MATCHES` 限的是**条数**，单行长度此前不限：搜一个压缩过的 JS 或单行 JSON，
200 条命中每条几万字符，一次搜索就能产出上百万字符。

这在 c8 第一层存盘还在的时候由那一层兜着。第一层已整层删除（上游两家都在工具
**产出的那一刻**限量、进了历史就不再动），所以每个可能产出大结果的工具都要自己
把住这一关——这是同一件事在搜索工具上的落点，与 `read_file` /`run_command`
那两个字符预算同源。
"""

import tempfile
import unittest
from pathlib import Path

from rhinecode.tools.grep_content import MAX_LINE_CHARS, GrepTool


class GrepLineBudgetTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.cwd = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)
        self.tool = GrepTool()

    def _grep(self, pattern: str):
        return self.tool.execute({"pattern": pattern}, cwd=self.cwd)

    def test_an_ordinary_source_line_is_untouched(self) -> None:
        """正常长度的命中行一个字都不许少。"""
        line = "def find_me(arg):  # " + "x" * 80
        (self.cwd / "a.py").write_text(line, encoding="utf-8")

        res = self._grep("find_me")
        self.assertTrue(res.ok, res.output)
        self.assertIn(line, res.output)
        self.assertNotIn("本行过长", res.output)

    def test_a_minified_bundle_line_is_clipped_in_place(self) -> None:
        """
        超长单行截断并**就地标注**。

        ⚠ 标注不可省：模型看不出这行还有后半截，会拿着半行代码去推理，
        而它看不出有什么不对（同 `read_file` 那条超长单行的教训）。
        """
        (self.cwd / "bundle.js").write_text(
            "find_me=" + "Z" * (MAX_LINE_CHARS * 20), encoding="utf-8"
        )

        res = self._grep("find_me")
        self.assertTrue(res.ok, res.output)
        self.assertIn("本行过长", res.output)
        self.assertLess(len(res.output), MAX_LINE_CHARS * 3, "整体输出没被收住")

    def test_the_whole_result_stays_bounded_at_the_match_cap(self) -> None:
        """
        ⚠ **反证：条数上限与单行上限必须同时生效。**

        少了单行上限，本条会产出 `50 × MAX_LINE_CHARS × 20` 量级的输出而其余
        用例照样全绿——因为它们各自只看一条命中行。真正的危害恰恰是两者相乘。
        """
        blob = "find_me=" + "Z" * (MAX_LINE_CHARS * 20)
        for i in range(50):
            (self.cwd / f"b{i}.js").write_text(blob, encoding="utf-8")

        res = self._grep("find_me")
        self.assertTrue(res.ok, res.output)
        # 50 条命中 × 每条最多 MAX_LINE_CHARS，再给表头与文件名留一倍余量
        self.assertLess(len(res.output), 50 * MAX_LINE_CHARS * 2)


if __name__ == "__main__":
    unittest.main()
