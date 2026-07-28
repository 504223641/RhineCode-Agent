"""
Skill 文本解析单测（c11 T8）。

覆盖 spec AC1（六个 frontmatter 字段被正确读取、缺省值正确）、
AC4（名字规则与保留词）、以及 F5 的各类解析失败原因可读。

全部用字符串字面量驱动，不造临时目录——parse_skill 是纯函数，这正是把
「读文件」留给 discovery 的收益。
"""

import unittest
from pathlib import Path

from rhinecode.skills.models import SkillMode, SkillSource
from rhinecode.skills.parser import parse_skill


def _parse(text: str, source: SkillSource = SkillSource.USER):
    """便捷包装：固定 path 与 source，返回 parse_skill 的三元组。"""
    return parse_skill(text, Path("/tmp/x.md"), source)


class ParseSuccessTest(unittest.TestCase):
    """解析成功路径：字段读取与缺省值。"""

    def test_all_six_fields(self) -> None:
        """六个 frontmatter 字段全部写明时，各字段值与声明一致（AC1）。"""
        text = (
            "---\n"
            "name: deploy-check\n"
            "description: 部署前的检查清单\n"
            "allowed_tools: [read_file, run_command]\n"
            "mode: isolated\n"
            "history_messages: 5\n"
            "model: deepseek-reasoner\n"
            "---\n"
            "第一步：跑测试。\n"
        )
        spec, reason, warnings = _parse(text)
        self.assertIsNone(reason)
        self.assertIsNotNone(spec)
        self.assertEqual(spec.name, "deploy-check")
        self.assertEqual(spec.description, "部署前的检查清单")
        self.assertEqual(spec.allowed_tools, ("read_file", "run_command"))
        self.assertIs(spec.mode, SkillMode.ISOLATED)
        self.assertEqual(spec.history_messages, 5)
        self.assertEqual(spec.model, "deepseek-reasoner")
        self.assertEqual(spec.body.strip(), "第一步：跑测试。")
        self.assertEqual(warnings, [])

    def test_only_required_fields_take_defaults(self) -> None:
        """只写两个必填字段时，其余取缺省值（AC1）。"""
        text = "---\nname: a\ndescription: b\n---\nSOP 正文\n"
        spec, reason, _ = _parse(text)
        self.assertIsNone(reason)
        # allowed_tools 的缺省是 None（未声明 = 不收窄），不是空 tuple。
        self.assertIsNone(spec.allowed_tools)
        self.assertIs(spec.mode, SkillMode.SHARED)
        self.assertEqual(spec.history_messages, 0)
        self.assertIsNone(spec.model)

    def test_unknown_keys_ignored(self) -> None:
        """未知键被忽略，既不失败也不警告（向前兼容）。"""
        text = (
            "---\n"
            "name: a\n"
            "description: b\n"
            "future_field: 未来版本才有的字段\n"
            "---\n"
            "正文\n"
        )
        spec, reason, warnings = _parse(text)
        self.assertIsNone(reason)
        self.assertIsNotNone(spec)
        self.assertEqual(warnings, [])

    def test_allowed_tools_deduped_preserving_order(self) -> None:
        """allowed_tools 含重复项时去重且保序。"""
        text = (
            "---\n"
            "name: a\n"
            "description: b\n"
            "allowed_tools: [run_command, read_file, run_command, glob_files]\n"
            "---\n"
            "正文\n"
        )
        spec, reason, _ = _parse(text)
        self.assertIsNone(reason)
        self.assertEqual(spec.allowed_tools, ("run_command", "read_file", "glob_files"))

    def test_leading_blank_lines_before_frontmatter(self) -> None:
        """frontmatter 之前的空白行被容忍（某些编辑器会留一行）。"""
        text = "\n\n---\nname: a\ndescription: b\n---\n正文\n"
        spec, reason, _ = _parse(text)
        self.assertIsNone(reason)
        self.assertEqual(spec.name, "a")

    def test_body_preserved_verbatim(self) -> None:
        """正文原样保留（缩进与空行即语义，它是要发给模型的 SOP）。"""
        text = "---\nname: a\ndescription: b\n---\n步骤：\n\n  1. 缩进项\n"
        spec, _, _ = _parse(text)
        self.assertIn("  1. 缩进项", spec.body)
        self.assertIn("步骤：\n\n", spec.body)

    def test_shared_mode_with_model_warns_but_loads(self) -> None:
        """共享模式声明 model → 加载成功但产出警告（F22）。"""
        text = "---\nname: a\ndescription: b\nmode: shared\nmodel: x-model\n---\n正文\n"
        spec, reason, warnings = _parse(text)
        self.assertIsNone(reason)
        self.assertIsNotNone(spec)
        self.assertEqual(len(warnings), 1)
        self.assertIn("共享模式", warnings[0])
        self.assertIn("x-model", warnings[0])


class ParseFailureTest(unittest.TestCase):
    """解析失败路径：每种失败都要给出可读原因（F5）。"""

    def test_no_frontmatter(self) -> None:
        spec, reason, warnings = _parse("直接就是正文，没有 frontmatter\n")
        self.assertIsNone(spec)
        self.assertIn("frontmatter", reason)
        self.assertEqual(warnings, [])

    def test_unclosed_frontmatter(self) -> None:
        spec, reason, _ = _parse("---\nname: a\ndescription: b\n没有闭合\n")
        self.assertIsNone(spec)
        self.assertIn("未闭合", reason)

    def test_bad_yaml(self) -> None:
        spec, reason, _ = _parse("---\nname: [未闭合的列表\n---\n正文\n")
        self.assertIsNone(spec)
        self.assertIn("解析失败", reason)

    def test_frontmatter_not_mapping(self) -> None:
        """frontmatter 是列表而非映射 → 明确报错。"""
        spec, reason, _ = _parse("---\n- a\n- b\n---\n正文\n")
        self.assertIsNone(spec)
        self.assertIn("键值映射", reason)

    def test_empty_frontmatter(self) -> None:
        """空 frontmatter（safe_load 返回 None）走「顶层必须是映射」分支。"""
        spec, reason, _ = _parse("---\n---\n正文\n")
        self.assertIsNone(spec)
        self.assertIn("键值映射", reason)

    def test_empty_body(self) -> None:
        spec, reason, _ = _parse("---\nname: a\ndescription: b\n---\n   \n\n")
        self.assertIsNone(spec)
        self.assertIn("正文为空", reason)

    def test_missing_name(self) -> None:
        spec, reason, _ = _parse("---\ndescription: b\n---\n正文\n")
        self.assertIsNone(spec)
        self.assertIn("缺少必填字段 name", reason)

    def test_yaml_boolean_name_gets_quoting_hint(self) -> None:
        """
        `name: off` 被 YAML 1.1 解析成布尔 False。

        此时报「缺少 name」是误导（用户明明写了），必须报类型错并提示加引号。
        同类裸词还有 on/yes/no/true/false。
        """
        for raw in ("off", "on", "yes", "no", "true", "false"):
            with self.subTest(raw=raw):
                spec, reason, _ = _parse(
                    f"---\nname: {raw}\ndescription: b\n---\n正文\n"
                )
                self.assertIsNone(spec)
                self.assertIn("加引号", reason)
                self.assertNotIn("缺少必填字段", reason)

    def test_missing_description(self) -> None:
        spec, reason, _ = _parse("---\nname: a\n---\n正文\n")
        self.assertIsNone(spec)
        self.assertIn("description", reason)

    def test_allowed_tools_as_string(self) -> None:
        """常见笔误：写成逗号分隔的字符串而非列表（AC1）。"""
        spec, reason, _ = _parse(
            "---\nname: a\ndescription: b\nallowed_tools: read_file, run_command\n---\n正文\n"
        )
        self.assertIsNone(spec)
        self.assertIn("字符串列表", reason)

    def test_bad_mode(self) -> None:
        spec, reason, _ = _parse("---\nname: a\ndescription: b\nmode: parallel\n---\n正文\n")
        self.assertIsNone(spec)
        self.assertIn("shared", reason)

    def test_negative_history_messages(self) -> None:
        spec, reason, _ = _parse(
            "---\nname: a\ndescription: b\nhistory_messages: -1\n---\n正文\n"
        )
        self.assertIsNone(spec)
        self.assertIn("非负整数", reason)

    def test_boolean_history_messages_rejected(self) -> None:
        """`history_messages: yes` 被 YAML 解析成 True；不排除 bool 会当成 1 悄悄生效。"""
        spec, reason, _ = _parse(
            "---\nname: a\ndescription: b\nhistory_messages: yes\n---\n正文\n"
        )
        self.assertIsNone(spec)
        self.assertIn("非负整数", reason)


class NameRuleTest(unittest.TestCase):
    """名字规则与保留词（AC4）。"""

    def _reason_for_name(self, name: str) -> str:
        # 名字统一加引号写入 YAML，确保 parser 收到的确实是字符串——
        # 否则像 off 这类裸词会先被 YAML 变成布尔值，测到的就不是名字规则了
        # （那条路径由 test_yaml_boolean_name_gets_quoting_hint 单独覆盖）。
        spec, reason, _ = _parse(f'---\nname: "{name}"\ndescription: b\n---\n正文\n')
        self.assertIsNone(spec, f"名字 {name!r} 本应被拒绝")
        return reason

    def test_illegal_names_rejected(self) -> None:
        """大写 / 空格 / 斜杠 / 下划线开头 / 数字开头 / 超长，各自失败。"""
        for name in ("Deploy", "my skill", "a/b", "_x", "1x", "a" * 33):
            with self.subTest(name=name):
                self.assertIn("不合法", self._reason_for_name(name))

    def test_boundary_length_accepted(self) -> None:
        """恰好 32 字符合法（边界的另一侧，防止把 <= 写成 <）。"""
        spec, reason, _ = _parse(
            f"---\nname: {'a' * 32}\ndescription: b\n---\n正文\n"
        )
        self.assertIsNone(reason)
        self.assertEqual(len(spec.name), 32)

    def test_reserved_subcommands_rejected(self) -> None:
        """四个 /skills 子命令词不能作为 Skill 名（AC4）。"""
        for name in ("reload", "off", "prompt", "run"):
            with self.subTest(name=name):
                self.assertIn("保留子命令词", self._reason_for_name(name))


if __name__ == "__main__":
    unittest.main()
