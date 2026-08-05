"""
条件求值层的单元测试（c12 T6，对应 checklist 第三节 / spec AC5、AC6）。

覆盖四种匹配形态、`all`/`any` 组合、缺失字段语义、类型归一化，以及
「glob 形态确实复用了权限规则的匹配算法」这条容易退化的性质。

本文件**零 I/O**：不建临时目录、不起子进程、不发网络请求（spec N5 / AC26）。
"""

import unittest

from rhinecode.hooks.conditions import evaluate, parse_matcher, stringify
from rhinecode.hooks.models import (
    COMBINE_ALL,
    COMBINE_ANY,
    MATCHER_EXACT,
    MATCHER_GLOB,
    MATCHER_REGEX,
    Condition,
)


def _m(field: str, raw):
    """解析一个匹配项，断言解析成功后返回它（测试内的书写糖）。"""
    matcher, err = parse_matcher(field, raw)
    assert err is None, f"预期解析成功，实际报错：{err}"
    assert matcher is not None
    return matcher


def _cond(combine: str, *pairs):
    """用若干 (字段, 模式) 构造一个 Condition。"""
    return Condition(combine, tuple(_m(f, p) for f, p in pairs))


class ParseMatcherTest(unittest.TestCase):
    """匹配形态识别（T4）。"""

    def test_exact_form(self):
        m = _m("tool", "run_command")
        self.assertEqual(m.kind, MATCHER_EXACT)
        self.assertFalse(m.negated)
        self.assertEqual(m.pattern, "run_command")

    def test_negated_prefix_stripped_once(self):
        m = _m("tool", "!run_command")
        self.assertTrue(m.negated)
        self.assertEqual(m.pattern, "run_command")

    def test_double_bang_keeps_literal(self):
        """`!!x` = 反向匹配字面量 `!x`——用户仍有办法匹配以 `!` 开头的值。"""
        m = _m("text", "!!urgent")
        self.assertTrue(m.negated)
        self.assertEqual(m.pattern, "!urgent")

    def test_glob_form(self):
        m = _m("command", "git *")
        self.assertEqual(m.kind, MATCHER_GLOB)

    def test_regex_form_compiled_at_load(self):
        m = _m("command", "/^git (push|reset)/")
        self.assertEqual(m.kind, MATCHER_REGEX)
        self.assertIsNotNone(m.regex, "正则必须在加载期编译好")
        self.assertEqual(m.pattern, "^git (push|reset)", "包裹的 `/` 必须被剥掉")

    def test_negated_regex(self):
        m = _m("command", "!/^git/")
        self.assertTrue(m.negated)
        self.assertEqual(m.kind, MATCHER_REGEX)

    def test_bad_regex_returns_message_not_exception(self):
        """
        正则编译失败必须走返回值的错误分支。

        加载期的错误必须是**可收集的警告**，不能是异常——否则一条写坏的规则会
        阻断整份配置的加载，与 spec F8「丢弃该条、其余照常」冲突。
        """
        matcher, err = parse_matcher("command", "/[/")
        self.assertIsNone(matcher)
        self.assertIsInstance(err, str)
        self.assertIn("正则", err)

    def test_empty_patterns_rejected(self):
        for raw in ("", "   ", "!", "//"):
            with self.subTest(raw=raw):
                matcher, err = parse_matcher("tool", raw)
                self.assertIsNone(matcher, f"{raw!r} 应被拒绝")
                self.assertIsInstance(err, str)

    def test_single_slash_is_not_regex(self):
        """单个 `/` 不该被当成空正则——它是一个合法的精确模式（根路径）。"""
        m = _m("file_path", "/")
        self.assertEqual(m.kind, MATCHER_EXACT)


class StringifyTest(unittest.TestCase):
    """类型归一化：模式侧与字段值侧必须同口径。"""

    def test_bool_lowercased(self):
        """
        `str(True)` 是 `"True"`，而 YAML 里写的是 `true`。不特判的话
        `is_read_only: true` 永远匹配不上，且**不报错**——规则悄悄永不命中。
        """
        self.assertEqual(stringify(True), "true")
        self.assertEqual(stringify(False), "false")

    def test_number_and_str(self):
        self.assertEqual(stringify(120), "120")
        self.assertEqual(stringify("x"), "x")


class MatchFormTest(unittest.TestCase):
    """四种形态各一条命中、一条不命中（AC5）。"""

    def test_exact(self):
        c = _cond(COMBINE_ALL, ("tool", "run_command"))
        self.assertTrue(evaluate(c, {"tool": "run_command"}))
        self.assertFalse(evaluate(c, {"tool": "read_file"}))

    def test_exact_is_case_sensitive(self):
        c = _cond(COMBINE_ALL, ("tool", "run_command"))
        self.assertFalse(evaluate(c, {"tool": "RUN_COMMAND"}))

    def test_glob(self):
        c = _cond(COMBINE_ALL, ("command", "git *"))
        self.assertTrue(evaluate(c, {"command": "git status"}))
        self.assertFalse(evaluate(c, {"command": "npm test"}))

    def test_regex(self):
        c = _cond(COMBINE_ALL, ("command", "/^git (push|reset)/"))
        self.assertTrue(evaluate(c, {"command": "git push origin main"}))
        self.assertFalse(evaluate(c, {"command": "git status"}))

    def test_regex_is_unanchored(self):
        """spec F3.3：正则非锚定，要整串匹配由用户自己写 `^...$`。"""
        c = _cond(COMBINE_ALL, ("text", "/urgent/"))
        self.assertTrue(evaluate(c, {"text": "this is urgent, please"}))

    def test_negated(self):
        c = _cond(COMBINE_ALL, ("tool", "!run_command"))
        self.assertFalse(evaluate(c, {"tool": "run_command"}))
        self.assertTrue(evaluate(c, {"tool": "read_file"}))


class GlobAlgorithmTest(unittest.TestCase):
    """glob 形态确实复用了权限规则的匹配算法，而不是退化成通用通配。"""

    def test_command_field_keeps_word_boundary(self):
        """
        `git *` 命中 `git status`，**不**命中 `github-cli`。

        这条是 `FIELD_MATCH_KIND` 有没有生效的判据：若 `command` 字段漏登记、
        退化成 `fnmatch`，`git *` 会连 `github-cli` 一起命中——而配置和界面上
        都看不出任何异常（`FIELD_MATCH_KIND` 那条成对维护点说的就是这个）。
        """
        c = _cond(COMBINE_ALL, ("command", "git *"))
        self.assertTrue(evaluate(c, {"command": "git status"}))
        self.assertTrue(evaluate(c, {"command": "git"}), "词边界前缀应命中裸命令本身")
        self.assertFalse(evaluate(c, {"command": "github-cli list"}))

    def test_path_field_uses_gitignore_style(self):
        """`**/*.py` 跨任意层——通用 fnmatch 做不到（`*` 会跨 `/`，语义不同）。"""
        c = _cond(COMBINE_ALL, ("file_path", "**/*.py"))
        self.assertTrue(evaluate(c, {"file_path": "a/b/c.py"}))
        self.assertTrue(evaluate(c, {"file_path": "x.py"}))
        self.assertFalse(evaluate(c, {"file_path": "a/b/c.md"}))

    def test_plain_field_is_case_sensitive_on_every_platform(self):
        """
        通用分支用 `fnmatchcase` 而不是 `fnmatch`。

        后者会先过 `os.path.normcase`：Windows 上退化成大小写不敏感、POSIX 上保持敏感，
        同一份 hooks.yaml 在两个平台行为不同，而配置和界面上都看不出异常。
        """
        c = _cond(COMBINE_ALL, ("text", "Hello*"))
        self.assertTrue(evaluate(c, {"text": "Hello world"}))
        self.assertFalse(evaluate(c, {"text": "hello world"}))


class CombineTest(unittest.TestCase):
    """`all` 与 `any` 的组合语义（AC5）。"""

    def test_all_requires_every_matcher(self):
        c = _cond(COMBINE_ALL, ("tool", "run_command"), ("command", "git *"))
        self.assertTrue(evaluate(c, {"tool": "run_command", "command": "git push"}))
        self.assertFalse(evaluate(c, {"tool": "run_command", "command": "npm test"}))
        self.assertFalse(evaluate(c, {"tool": "read_file", "command": "git push"}))

    def test_any_requires_one_matcher(self):
        c = _cond(COMBINE_ANY, ("tool", "run_command"), ("tool", "edit_file"))
        self.assertTrue(evaluate(c, {"tool": "run_command"}))
        self.assertTrue(evaluate(c, {"tool": "edit_file"}))
        self.assertFalse(evaluate(c, {"tool": "read_file"}))

    def test_none_condition_is_always_true(self):
        """spec F3.4：省略 `if` = 无条件触发。"""
        self.assertTrue(evaluate(None, {}))
        self.assertTrue(evaluate(None, {"anything": "goes"}))


class MissingFieldTest(unittest.TestCase):
    """
    ⚠ 缺失字段的语义（AC5 的关键一条）：不成立，**反向匹配也不成立**。

    写反的后果不是「少命中几次」，而是一条本意为「除了 git 命令之外」的规则
    在所有**没有 command 字段**的事件上突然成立——也就是几乎所有事件。
    """

    def test_plain_matcher_fails_on_missing_field(self):
        c = _cond(COMBINE_ALL, ("command", "git *"))
        self.assertFalse(evaluate(c, {"tool": "read_file"}))

    def test_negated_matcher_ALSO_fails_on_missing_field(self):
        c = _cond(COMBINE_ALL, ("command", "!git *"))
        self.assertFalse(
            evaluate(c, {"tool": "read_file"}),
            "字段不存在时反向匹配也必须不成立——「字段不存在」≠「不匹配」",
        )

    def test_none_value_treated_as_missing(self):
        c = _cond(COMBINE_ALL, ("command", "git *"))
        self.assertFalse(evaluate(c, {"command": None}))

    def test_negated_matcher_on_none_value(self):
        c = _cond(COMBINE_ALL, ("command", "!git *"))
        self.assertFalse(evaluate(c, {"command": None}))

    def test_any_with_one_missing_still_works(self):
        """缺失只让**该匹配项**不成立，不影响 `any` 里其它项。"""
        c = _cond(COMBINE_ANY, ("command", "git *"), ("tool", "read_file"))
        self.assertTrue(evaluate(c, {"tool": "read_file"}))


class ValueTypeTest(unittest.TestCase):
    """非字符串字段值的匹配（AC5）。"""

    def test_bool_field_matched_by_true(self):
        c = _cond(COMBINE_ALL, ("is_read_only", "true"))
        self.assertTrue(evaluate(c, {"is_read_only": True}))
        self.assertFalse(evaluate(c, {"is_read_only": False}))

    def test_bool_pattern_from_yaml(self):
        """YAML 里 `is_read_only: true` 解析成 Python bool，模式侧同样要归一化。"""
        c = _cond(COMBINE_ALL, ("is_read_only", True))
        self.assertTrue(evaluate(c, {"is_read_only": True}))
        self.assertFalse(evaluate(c, {"is_read_only": False}))

    def test_number_field(self):
        c = _cond(COMBINE_ALL, ("message_count", "120"))
        self.assertTrue(evaluate(c, {"message_count": 120}))
        self.assertFalse(evaluate(c, {"message_count": 121}))


if __name__ == "__main__":
    unittest.main()
