"""笔记与索引纯逻辑单测（c9 T6 / AC15 相关）：frontmatter 往返、宽松容错、索引截断。"""

import unittest

from rhinecode.memory.memories import (
    Memory,
    parse_memory,
    render_memory,
    rebuild_index,
    truncate_index,
    INDEX_MAX_LINES,
    INDEX_MAX_BYTES,
)


class NotesTest(unittest.TestCase):
    def _note(self) -> Memory:
        return Memory(
            filename="prefer-cn.md",
            name="prefer-cn",
            summary="注释一律用中文",
            category="preference",
            body="用户在 2026-07-12 明确要求。",
        )

    def test_render_parse_roundtrip(self) -> None:
        """render 后 parse 得到等价 Note（往返一致）。"""
        n = self._note()
        parsed = parse_memory(render_memory(n), filename=n.filename)
        self.assertIsNotNone(parsed)
        self.assertEqual(parsed.name, n.name)
        self.assertEqual(parsed.summary, n.summary)
        self.assertEqual(parsed.category, n.category)
        self.assertEqual(parsed.body, n.body)

    def test_unknown_fields_ignored(self) -> None:
        """frontmatter 里的未知字段被忽略而不报错（N5 向前兼容）。"""
        text = (
            "---\n"
            "name: x\n"
            "summary: y\n"
            "category: project\n"
            "future_field: whatever\n"
            "---\n"
            "body\n"
        )
        parsed = parse_memory(text)
        self.assertIsNotNone(parsed)
        self.assertEqual(parsed.name, "x")

    def test_bad_frontmatter_returns_none(self) -> None:
        """无 frontmatter / 未闭合 / 缺必填字段 / 非法分类 → None。"""
        self.assertIsNone(parse_memory("没有 frontmatter 的普通文本"))
        self.assertIsNone(parse_memory("---\nname: x\nsummary: y\ncategory: project\n"))  # 未闭合
        self.assertIsNone(parse_memory("---\nname: x\ncategory: project\n---\n"))  # 缺 summary
        self.assertIsNone(parse_memory("---\nname: x\nsummary: y\ncategory: 不存在的类\n---\n"))

    def test_rebuild_index_line_format(self) -> None:
        text = rebuild_index([self._note()])
        self.assertIn("# 记忆索引", text)
        self.assertIn("- prefer-cn（prefer-cn.md）[用户偏好] — 注释一律用中文", text)

    def test_truncate_by_lines(self) -> None:
        """201 行内容截到 200 行。"""
        text = "\n".join(f"line-{i}" for i in range(INDEX_MAX_LINES + 1))
        out = truncate_index(text)
        self.assertEqual(len(out.splitlines()), INDEX_MAX_LINES)

    def test_truncate_by_bytes_valid_utf8(self) -> None:
        """>25KB 中文内容截到 ≤25KB，且不产生非法 UTF-8（整行回退）。"""
        # 每行约 300 字节的中文，100 行 ≈ 30KB > 25KB
        text = "\n".join("中" * 100 for _ in range(100))
        out = truncate_index(text)
        encoded = out.encode("utf-8")  # 能无异常编码即合法
        self.assertLessEqual(len(encoded), INDEX_MAX_BYTES)
        # 每行都是完整的（没有被切成半个字符的行）
        for line in out.splitlines():
            self.assertEqual(line, "中" * 100)


if __name__ == "__main__":
    unittest.main()
