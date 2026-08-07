"""
c14 T3：隔离工作区名字的安全校验（spec F7 / AC8 / AC9）。

**这是本章唯一挡在「模型给的字符串」与「拼进文件系统路径」之间的闸门**，
因此这里的用例刻意写成穷举式的：每个非法构造一条独立断言，而不是塞进一个
`for` 循环里一次性判完。理由是循环里挂掉时只知道「有一条没过」，
而逐条写能直接看出**是哪一类**绕过没被挡住。
"""

import unittest

from rhinecode.worktree.models import MAX_NAME_LENGTH, WorktreeNameError
from rhinecode.worktree.naming import generate_name, validate_name


class ValidNameTest(unittest.TestCase):
    """合法名字必须原样通过——挡太宽和挡太松一样是 bug。"""

    def test_simple(self):
        self.assertEqual(validate_name("fix-toolset"), "fix-toolset")

    def test_nested(self):
        """`/` 做嵌套是明确允许的（spec F7）。"""
        self.assertEqual(validate_name("feature/login"), "feature/login")

    def test_deep_nested(self):
        self.assertEqual(validate_name("a/b/c"), "a/b/c")

    def test_all_allowed_chars(self):
        self.assertEqual(validate_name("A_z-0.9"), "A_z-0.9")

    def test_dot_inside_segment_is_fine(self):
        """段**内**的点合法，只有整段等于 '.' 或 '..' 才非法。"""
        self.assertEqual(validate_name("v1.2.3"), "v1.2.3")
        self.assertEqual(validate_name("...x"), "...x")

    def test_exactly_max_length(self):
        name = "a" * MAX_NAME_LENGTH
        self.assertEqual(validate_name(name), name)

    def test_strips_surrounding_whitespace(self):
        self.assertEqual(validate_name("  fix  "), "fix")


class PathTraversalTest(unittest.TestCase):
    """
    路径遍历构造穷举。

    每一条都能通过「只看有没有奇怪字符」的粗校验，所以一条都不能省。
    """

    def _reject(self, raw: str):
        with self.assertRaises(WorktreeNameError, msg=f"未拒绝: {raw!r}"):
            validate_name(raw)

    def test_bare_parent(self):
        self._reject("..")

    def test_parent_in_middle(self):
        self._reject("a/../b")

    def test_parent_at_front(self):
        self._reject("../a")

    def test_parent_at_end(self):
        self._reject("a/..")

    def test_bare_dot(self):
        self._reject(".")

    def test_dot_segment_in_middle(self):
        self._reject("a/./b")

    def test_posix_absolute(self):
        self._reject("/etc/passwd")

    def test_windows_drive(self):
        """盘符形式的绝对路径靠「禁止冒号」挡下。"""
        self._reject("C:/Windows")

    def test_backslash(self):
        """Windows 上反斜杠是路径分隔符——放行等于开了第二条遍历通道。"""
        self._reject("a\\b")

    def test_backslash_parent(self):
        self._reject("..\\..\\Windows")

    def test_trailing_slash(self):
        self._reject("a/")

    def test_double_slash(self):
        self._reject("a//b")


class MalformedNameTest(unittest.TestCase):
    """其它形态的非法输入。"""

    def _reject(self, raw):
        with self.assertRaises(WorktreeNameError, msg=f"未拒绝: {raw!r}"):
            validate_name(raw)

    def test_empty(self):
        self._reject("")

    def test_whitespace_only(self):
        self._reject("   ")

    def test_too_long(self):
        self._reject("a" * (MAX_NAME_LENGTH + 1))

    def test_space_inside(self):
        self._reject("my agent")

    def test_shell_metachars(self):
        """即使 gitcmd 用 shell=False，名字里也不该出现这些。"""
        for raw in ("a;b", "a&&b", "a|b", "a$b", "a`b`", "a>b"):
            self._reject(raw)

    def test_non_ascii(self):
        self._reject("角色")

    def test_not_a_string(self):
        self._reject(None)
        self._reject(123)


class GenerateNameTest(unittest.TestCase):
    """
    生成的名字**必然**通过校验（spec AC9）。

    这条断言不是走过场：清洗规则与校验规则是两段独立的代码，将来任何一边改动
    都可能让它们错位。让生成路径也过一遍校验，错位会在开发期暴露，
    而不是等到某个角色名恰好触发时才出现一次莫名其妙的创建失败。
    """

    def test_plain(self):
        name = generate_name("reviewer", "abc123")
        self.assertEqual(validate_name(name), name)
        self.assertIn("reviewer", name)

    def test_agent_name_with_illegal_chars(self):
        for raw in ("my agent!!", "../evil", "C:\\x", "角色名", "  ", "..", "."):
            name = generate_name(raw, "task01")
            self.assertEqual(validate_name(name), name, f"来自 {raw!r} 的名字非法")

    def test_empty_inputs(self):
        name = generate_name("", "")
        self.assertEqual(validate_name(name), name)

    def test_long_inputs_are_truncated(self):
        name = generate_name("x" * 200, "y" * 200)
        self.assertLessEqual(len(name), MAX_NAME_LENGTH)
        self.assertEqual(validate_name(name), name)

    def test_task_id_distinguishes_concurrent_tasks(self):
        a = generate_name("reviewer", "task-aaa")
        b = generate_name("reviewer", "task-bbb")
        self.assertNotEqual(a, b)


if __name__ == "__main__":
    unittest.main()
