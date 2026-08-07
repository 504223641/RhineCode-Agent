"""
角色定义解析器的单测（c13 T3，覆盖 AC1a / AC1b / AC1c）。

本模块钉住 spec F1 的两类分界：

- **抛错**（这份定义根本不能用）：缺 frontmatter / YAML 坏 / 缺 description / 名字非法；
- **记警告**（能用，只是有地方不如预期）：未支持字段 / 轮次越界 / 权限档位写错。

判据是「照着用户写的跑下去，结果会不会与他的意图相反」。
"""

from __future__ import annotations

import unittest
from pathlib import Path

from rhinecode.permission.models import PermissionMode
from rhinecode.subagents.models import (
    DEFAULT_MAX_TURNS,
    HARD_MAX_TURNS,
    UNSUPPORTED_FIELDS,
    AgentSource,
)
from rhinecode.subagents.parser import AgentParseError, parse_agent


def _parse(front: str, body: str = "你是一个测试角色。", name: str = "demo"):
    text = f"---\n{front}\n---\n{body}"
    return parse_agent(text, Path(f"{name}.md"), AgentSource.USER, name)


class MinimalDefinitionTest(unittest.TestCase):
    """AC1a：只写 description 的定义能用。"""

    def test_only_description_is_enough(self) -> None:
        spec = _parse("description: 做点什么")

        self.assertEqual(spec.description, "做点什么")
        self.assertEqual(spec.name, "demo", "name 缺省应回落到文件名")
        self.assertEqual(spec.max_turns, DEFAULT_MAX_TURNS)
        self.assertIsNone(spec.model)
        self.assertIsNone(spec.permission_mode)
        self.assertEqual(spec.warnings, ())

    def test_tools_none_means_inherit(self) -> None:
        """
        未声明 `tools` 时是 `None`（继承），**不是空元组**。

        这个区分不能省：`None` = 「给我主对话有的全部工具」，
        `()` = 「一个都不给」（会让委派立即失败）。混同的话，
        一个没写 tools 的角色会变成零工具、启动即失败。
        """
        self.assertIsNone(_parse("description: x").tools)

    def test_declared_empty_tools_is_empty_tuple(self) -> None:
        """声明了但为空 → `()`，与未声明区分开。"""
        self.assertEqual(_parse("description: x\ntools: []").tools, ())

    def test_empty_body_is_allowed(self) -> None:
        """
        正文可以为空（与 C11 的 Skill 刻意不同）。

        一个只靠工具白名单收窄行为的角色是合法的；硬性要求正文会挡掉这种用法。
        """
        self.assertEqual(_parse("description: x", body="").body, "")


class KeyNormalizationTest(unittest.TestCase):
    """AC1b：连字符与下划线两种写法等价。"""

    def test_hyphen_and_underscore_equivalent(self) -> None:
        a = _parse("description: x\ndisallowed-tools: run_command")
        b = _parse("description: x\ndisallowed_tools: run_command")
        self.assertEqual(a, b)

    def test_max_turns_both_spellings(self) -> None:
        a = _parse("description: x\nmax-turns: 7")
        b = _parse("description: x\nmax_turns: 7")
        self.assertEqual(a.max_turns, b.max_turns)
        self.assertEqual(a.max_turns, 7)

    def test_permission_mode_both_spellings(self) -> None:
        a = _parse("description: x\npermission-mode: strict")
        b = _parse("description: x\npermission_mode: strict")
        self.assertEqual(a.permission_mode, b.permission_mode)
        self.assertIs(a.permission_mode, PermissionMode.STRICT)

    def test_hyphen_wins_when_both_present(self) -> None:
        """两种写法并存时以连字符版为准（那是 Claude Code 的标准写法）。"""
        spec = _parse(
            "description: x\ndisallowed-tools: aaa\ndisallowed_tools: bbb"
        )
        self.assertEqual(spec.disallowed_tools, ("aaa",))

    def test_key_case_insensitive(self) -> None:
        self.assertEqual(_parse("Description: x").description, "x")


class ToolListFormatTest(unittest.TestCase):
    """逗号分隔字符串与 YAML 列表两种写法都要认。"""

    def test_comma_string_and_yaml_list_equivalent(self) -> None:
        a = _parse("description: x\ntools: read_file, glob_files")
        b = _parse("description: x\ntools:\n  - read_file\n  - glob_files")
        self.assertEqual(a.tools, b.tools)
        self.assertEqual(a.tools, ("read_file", "glob_files"))

    def test_only_recognizing_one_form_would_silently_break(self) -> None:
        """
        **反证式说明**：只认 YAML 列表的话，逗号写法会变成**一个**名字叫
        「read_file, glob_files」的工具——它匹配不到任何东西，最终工具集为空，
        用户看到的是「委派怎么直接失败了」而不是「我的写法不对」。

        断言长度为 2 就是在钉这条。
        """
        self.assertEqual(len(_parse("description: x\ntools: a, b").tools), 2)

    def test_blank_items_dropped(self) -> None:
        self.assertEqual(_parse("description: x\ntools: a, , b,").tools, ("a", "b"))


class FatalErrorTest(unittest.TestCase):
    """抛错的四种情形。"""

    def test_missing_frontmatter(self) -> None:
        with self.assertRaises(AgentParseError) as cm:
            parse_agent("只有正文", Path("x.md"), AgentSource.USER, "x")
        self.assertIn("frontmatter", str(cm.exception))

    def test_unclosed_frontmatter(self) -> None:
        with self.assertRaises(AgentParseError) as cm:
            parse_agent("---\ndescription: x\n正文", Path("x.md"), AgentSource.USER, "x")
        self.assertIn("未闭合", str(cm.exception))

    def test_broken_yaml(self) -> None:
        with self.assertRaises(AgentParseError):
            _parse("description: [unclosed")

    def test_frontmatter_not_a_mapping(self) -> None:
        with self.assertRaises(AgentParseError) as cm:
            parse_agent("---\n- a\n- b\n---\nx", Path("x.md"), AgentSource.USER, "x")
        self.assertIn("映射", str(cm.exception))

    def test_missing_description(self) -> None:
        with self.assertRaises(AgentParseError) as cm:
            _parse("name: demo")
        self.assertIn("description", str(cm.exception))

    def test_blank_description(self) -> None:
        with self.assertRaises(AgentParseError):
            _parse('description: "   "')

    def test_illegal_name_characters(self) -> None:
        # 用 YAML 单引号：双引号里 `\b` 会被当成退格转义，传进去的就不是反斜杠了
        for bad in ("a/b", "a\\b", "a:b", "a b"):
            with self.subTest(name=bad):
                with self.assertRaises(AgentParseError):
                    _parse(f"description: x\nname: '{bad}'")

    def test_empty_name(self) -> None:
        with self.assertRaises(AgentParseError):
            _parse('description: x\nname: "  "')


class MaxTurnsTest(unittest.TestCase):
    """轮次上限：夹取而非报错，但必须告知夹到了多少。"""

    def test_within_range_kept(self) -> None:
        spec = _parse("description: x\nmax_turns: 7")
        self.assertEqual(spec.max_turns, 7)
        self.assertEqual(spec.warnings, ())

    def test_above_hard_cap_clamped_with_number_in_warning(self) -> None:
        spec = _parse(f"description: x\nmax_turns: {HARD_MAX_TURNS + 100}")
        self.assertEqual(spec.max_turns, HARD_MAX_TURNS)
        # 警告里必须出现夹到的数字——否则用户会以为真能跑那么多轮
        self.assertTrue(any(str(HARD_MAX_TURNS) in w for w in spec.warnings))

    def test_below_one_raised(self) -> None:
        spec = _parse("description: x\nmax_turns: 0")
        self.assertEqual(spec.max_turns, 1)
        self.assertTrue(spec.warnings)

    def test_non_integer_falls_back(self) -> None:
        spec = _parse("description: x\nmax_turns: 很多")
        self.assertEqual(spec.max_turns, DEFAULT_MAX_TURNS)
        self.assertTrue(spec.warnings)

    def test_bool_is_not_an_integer(self) -> None:
        """
        `max_turns: true` 必须被当成非法值。

        Python 里 `bool` 是 `int` 的子类，`int(True)` == 1——不单独挡的话，
        这个写法会静默变成「只跑一轮」，而用户完全无从察觉。
        """
        spec = _parse("description: x\nmax_turns: true")
        self.assertEqual(spec.max_turns, DEFAULT_MAX_TURNS)
        self.assertTrue(spec.warnings)

    def test_hard_cap_not_above_loop_limit(self) -> None:
        """
        `HARD_MAX_TURNS` 不得超过 Agent Loop 的迭代上限。

        两个常量刻意不 import 互相依赖（理由见 `models.py` 的注释），
        代价是要人工对齐——这条断言就是那份对齐的护栏，改大了当场红。
        """
        from rhinecode.agent.loop import MAX_ITERATIONS

        self.assertLessEqual(HARD_MAX_TURNS, MAX_ITERATIONS)


class PermissionModeTest(unittest.TestCase):
    def test_each_mode_recognized(self) -> None:
        for mode in PermissionMode:
            with self.subTest(mode=mode):
                spec = _parse(f"description: x\npermission_mode: {mode.value}")
                self.assertIs(spec.permission_mode, mode)
                self.assertEqual(spec.warnings, ())

    def test_inherit_normalized_to_none(self) -> None:
        self.assertIsNone(_parse("description: x\npermission_mode: inherit").permission_mode)

    def test_unknown_falls_back_to_inherit_not_strict(self) -> None:
        """
        认不出来时按**继承**处理，不是按最严。

        按最严会让一个拼写错误静默把角色降级成只读，用户看到「我的角色怎么
        什么都干不了」，而定义文件里明明写着别的。继承 + 警告，现象与提示才对得上。
        """
        spec = _parse("description: x\npermission_mode: strick")
        self.assertIsNone(spec.permission_mode)
        self.assertTrue(any("permission_mode" in w for w in spec.warnings))


class ModelFieldTest(unittest.TestCase):
    def test_inherit_and_missing_both_none(self) -> None:
        self.assertIsNone(_parse("description: x").model)
        self.assertIsNone(_parse("description: x\nmodel: inherit").model)
        self.assertIsNone(_parse("description: x\nmodel: INHERIT").model)

    def test_explicit_model_kept(self) -> None:
        self.assertEqual(_parse("description: x\nmodel: deepseek-chat").model, "deepseek-chat")


class UnsupportedFieldsTest(unittest.TestCase):
    """AC1c：未支持字段只警告、不阻断，且警告必须具名。"""

    def test_every_unsupported_field_warns_by_name(self) -> None:
        """
        遍历常量表逐个验证——**新增一项忘了写警告文案时当场红**。

        这条是 `UNSUPPORTED_FIELDS` 这张表的护栏：它同时钉住
        「表里每一项都真的会触发警告」与「警告里出现了字段名」。
        """
        for key in UNSUPPORTED_FIELDS:
            with self.subTest(field=key):
                spec = _parse(f"description: x\n{key}: 随便什么")
                self.assertTrue(
                    any(key in w for w in spec.warnings),
                    f"{key} 应产出含字段名的警告",
                )

    def test_agent_still_usable(self) -> None:
        """带未支持字段的角色**照常可用**——它只是少一个本来就没有的能力。"""
        spec = _parse("description: x\nmemory: user\ncolor: red")
        self.assertEqual(spec.description, "x")
        self.assertEqual(len(spec.warnings), 2)

    def test_isolation_is_no_longer_unsupported(self) -> None:
        """
        c14：`isolation` 已从未支持表里移出——它现在真的生效（spec F13）。

        ⚠ 这条是那条成对维护点的**正面**护栏：如果做了某个字段却忘了从
        `UNSUPPORTED_FIELDS` 里删掉，用户会被告知「本项目不支持该字段，已忽略」，
        而它其实生效了——这种「功能做了却说没做」的错误比漏做更难被发现。
        """
        self.assertNotIn("isolation", UNSUPPORTED_FIELDS)
        spec = _parse("description: x\nisolation: worktree")
        self.assertEqual(spec.isolation, "worktree")
        self.assertEqual(spec.warnings, ())

    def test_truly_unknown_field_is_silent(self) -> None:
        """
        表**之外**的未知键静默忽略。

        理由与 C11 的「目录里放 README 不刷错误」同源：用户可能在 frontmatter 里
        放注释性字段，为它们刷警告只会让 `/agents` 变成噪音。
        表里那八个之所以要报，是因为它们在 Claude Code 里**有效**，
        用户有明确的预期落空。
        """
        self.assertEqual(_parse("description: x\nmy_note: 随手记").warnings, ())


if __name__ == "__main__":
    unittest.main()
