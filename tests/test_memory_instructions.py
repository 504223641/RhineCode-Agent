"""RHINE.md 三层加载与 @include 展开单测（c9 T4 / AC1/AC2/AC3）。"""

import unittest
import tempfile
from pathlib import Path

from rhinecode.memory.instructions import load_instructions, MAX_INCLUDE_DEPTH


class InstructionsTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        # 模拟用户级目录与项目根（互不包含）
        self.user_dir = root / "home" / ".rhinecode"
        self.project = root / "proj"
        (self.project / ".rhinecode").mkdir(parents=True)
        self.user_dir.mkdir(parents=True)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _write(self, path: Path, text: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")

    def _load(self):
        return load_instructions(self.user_dir, self.project)

    # ------------------------------------------------------------------ #
    # AC1：三层拼接顺序、来源标注、缺层跳过
    # ------------------------------------------------------------------ #
    def test_three_layers_order_and_source(self) -> None:
        self._write(self.user_dir / "RHINE.md", "USER-CONTENT")
        self._write(self.project / ".rhinecode" / "RHINE.md", "PROJ-DOT-CONTENT")
        self._write(self.project / "RHINE.md", "PROJ-ROOT-CONTENT")
        result = self._load()
        # 顺序：用户级 → 项目 .rhinecode → 项目根（越具体越靠后）
        pos_user = result.text.index("USER-CONTENT")
        pos_dot = result.text.index("PROJ-DOT-CONTENT")
        pos_root = result.text.index("PROJ-ROOT-CONTENT")
        self.assertLess(pos_user, pos_dot)
        self.assertLess(pos_dot, pos_root)
        # 各带来源路径标注
        self.assertIn("# 来源：", result.text)
        self.assertIn(str(self.user_dir / "RHINE.md"), result.text)
        # 三层都 loaded
        self.assertEqual([l.loaded for l in result.layers], [True, True, True])

    def test_missing_layers_skipped(self) -> None:
        self._write(self.project / "RHINE.md", "ONLY-ROOT")
        result = self._load()
        self.assertIn("ONLY-ROOT", result.text)
        self.assertNotIn("用户级" + "内容", result.text)
        loaded_flags = [l.loaded for l in result.layers]
        self.assertEqual(loaded_flags, [False, False, True])

    def test_all_missing_empty_text(self) -> None:
        result = self._load()
        self.assertEqual(result.text, "")

    # ------------------------------------------------------------------ #
    # AC2：include 展开 / 深度 / 循环 / 越界 / 代码块
    # ------------------------------------------------------------------ #
    def test_include_expanded(self) -> None:
        self._write(self.project / "RHINE.md", "before @docs/extra.md after")
        self._write(self.project / "docs" / "extra.md", "EXTRA-BODY")
        result = self._load()
        self.assertIn("EXTRA-BODY", result.text)
        self.assertNotIn("@docs/extra.md", result.text)

    def test_include_relative_to_including_file(self) -> None:
        """子文件里的相对引用以子文件所在目录为基准，而非项目根。"""
        self._write(self.project / "RHINE.md", "@docs/a.md")
        self._write(self.project / "docs" / "a.md", "A(@b.md)")  # b.md 相对 docs/
        self._write(self.project / "docs" / "b.md", "B-BODY")
        result = self._load()
        self.assertIn("B-BODY", result.text)

    def test_include_depth_limit(self) -> None:
        """构造 5 层嵌套链：第 5 层保持原文且 errors 有记录。"""
        # RHINE.md → l1 → l2 → l3 → l4 → l5；depth 依次 0..4，depth>=4 时不展开 l5
        self._write(self.project / "RHINE.md", "@l1.md")
        for i in range(1, 5):
            self._write(self.project / f"l{i}.md", f"L{i} @l{i+1}.md")
        self._write(self.project / "l5.md", "L5-BODY")
        result = self._load()
        self.assertIn("L4", result.text)          # 第 4 层已展开
        self.assertNotIn("L5-BODY", result.text)  # 第 5 层未展开
        self.assertIn("@l5.md", result.text)      # 原文保留
        root_layer = result.layers[2]
        self.assertTrue(any("深度" in e for e in root_layer.errors))
        self.assertEqual(MAX_INCLUDE_DEPTH, 4)

    def test_include_cycle_terminates(self) -> None:
        """A 引 B、B 引 A：正常结束不递归爆栈，errors 记录循环。"""
        self._write(self.project / "RHINE.md", "@a.md")
        self._write(self.project / "a.md", "A-BODY @b.md")
        self._write(self.project / "b.md", "B-BODY @a.md")
        result = self._load()
        self.assertIn("A-BODY", result.text)
        self.assertIn("B-BODY", result.text)
        root_layer = result.layers[2]
        self.assertTrue(any("循环" in e for e in root_layer.errors))

    def test_include_out_of_boundary_blocked(self) -> None:
        """解析后越出宿主层边界的引用不展开（越界拦截）。"""
        secret = Path(self._tmp.name) / "secret.md"
        self._write(secret, "TOP-SECRET")
        self._write(self.project / "RHINE.md", "@../secret.md")
        result = self._load()
        self.assertNotIn("TOP-SECRET", result.text)
        self.assertIn("@../secret.md", result.text)  # 原文保留
        self.assertTrue(any("越界" in e for e in result.layers[2].errors))

    def test_fenced_code_block_not_expanded(self) -> None:
        """围栏代码块内的 @路径 保持原文。"""
        self._write(self.project / "docs" / "x.md", "X-BODY")
        self._write(
            self.project / "RHINE.md",
            "outside @docs/x.md\n```\ninside @docs/x.md\n```\n",
        )
        result = self._load()
        # 块外展开、块内保留
        self.assertIn("X-BODY", result.text)
        self.assertIn("inside @docs/x.md", result.text)

    # ------------------------------------------------------------------ #
    # AC3：fail-safe
    # ------------------------------------------------------------------ #
    def test_include_missing_target(self) -> None:
        """include 指向不存在文件：整体不抛异常、原文保留、errors 记录。"""
        self._write(self.project / "RHINE.md", "KEEP @no/such.md")
        result = self._load()
        self.assertIn("KEEP", result.text)
        self.assertIn("@no/such.md", result.text)
        self.assertTrue(any("不存在" in e for e in result.layers[2].errors))

    def test_user_layer_boundary_is_user_dir(self) -> None:
        """用户层的边界是 ~/.rhinecode 自身：引项目里的文件视为越界。"""
        self._write(self.project / "leak.md", "PROJ-LEAK")
        # 用相对路径从 user_dir 爬到项目目录
        rel = "../../proj/leak.md"
        self._write(self.user_dir / "RHINE.md", f"@{rel}")
        result = self._load()
        self.assertNotIn("PROJ-LEAK", result.text)
        self.assertTrue(any("越界" in e for e in result.layers[0].errors))


if __name__ == "__main__":
    unittest.main()
