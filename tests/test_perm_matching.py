"""matching 模块单测：命令拆分、命令模式匹配、路径模式匹配（c6 T2）。"""

import unittest

from rhinecode.permission.matching import (
    match_command,
    match_command_deep,
    match_path,
    split_commands,
)


class SplitCommandsTests(unittest.TestCase):
    def test_splits_on_all_separators(self) -> None:
        self.assertEqual(split_commands("git status && rm -rf /"), ["git status", "rm -rf /"])
        self.assertEqual(split_commands("a || b ; c | d"), ["a", "b", "c", "d"])
        self.assertEqual(split_commands("a\nb"), ["a", "b"])

    def test_empty_and_blank(self) -> None:
        self.assertEqual(split_commands(""), [])
        self.assertEqual(split_commands("   "), [])


class MatchCommandTests(unittest.TestCase):
    def test_word_boundary_prefix(self) -> None:
        # npm:* / git * 形式带词边界，不误伤相邻词
        self.assertFalse(match_command("npm:*", "npmx"))
        self.assertTrue(match_command("npm:*", "npm install"))
        self.assertTrue(match_command("npm:*", "npm"))
        self.assertFalse(match_command("ls *", "lsof"))
        self.assertTrue(match_command("ls *", "ls -la"))

    def test_interior_and_exact(self) -> None:
        self.assertTrue(match_command("git *", "git push origin main"))
        self.assertTrue(match_command("git * main", "git checkout main"))
        self.assertFalse(match_command("git * main", "git checkout dev"))
        self.assertTrue(match_command("npm run build", "npm run build"))
        self.assertFalse(match_command("npm run build", "npm run test"))

    def test_empty_pattern_matches_all(self) -> None:
        self.assertTrue(match_command("", "anything goes"))


class MatchCommandDeepTests(unittest.TestCase):
    """
    「整条 + 逐段」双重检查的共用实现（perm-compound-command）。

    这里只验判定形态本身；「谁该用它、谁不该用」在
    `test_perm_rules.py::CompoundCommandTest` 与 `test_hook_conditions.py` 里验。
    """

    @staticmethod
    def _hits(pattern: str, command: str) -> bool:
        return match_command_deep(command, lambda one: match_command(pattern, one))

    def test_matches_segment_behind_every_separator(self) -> None:
        # 分隔符矩阵：`&&`、`||`、`;`、`|`、`&`、`|&`、换行，任一段命中即为命中。
        for compound in (
            "git status && git push origin main",
            "git status || git push origin main",
            "git status ; git push origin main",
            "git status | git push origin main",
            "git status & git push origin main",
            "git status |& git push origin main",
            "git status\ngit push origin main",
            "git status\r\ngit push origin main",
            # 真实模型实测产出的那一条（C12 场景 9 首跑）。
            'git add auth.py && git commit -m "x" && echo "=====PUSH=====" && git push origin main',
        ):
            with self.subTest(compound=compound):
                self.assertTrue(self._hits("git push *", compound))

    def test_whole_string_half_still_works(self) -> None:
        # 「整条」那一半不能省：跨参数的模式只有对整条求值才有意义，
        # 拆成 ["git checkout main"] 之后它照样命中，但若实现改成「只逐段」，
        # 像 fork 炸弹那种「分隔符本身是语法一部分」的结构就会被拆碎到谁都匹配不到。
        self.assertTrue(self._hits("git * main", "git checkout main"))
        self.assertTrue(match_command_deep(":(){ :|:& };:", lambda one: one == ":(){ :|:& };:"))

    def test_word_boundary_survives_splitting(self) -> None:
        # 拆段之后每段仍走 match_command 的词边界语义——不会因为拆过就退化成子串匹配。
        self.assertFalse(self._hits("git *", "echo hi && github-cli status"))
        self.assertFalse(self._hits("ls *", "echo hi && lsof -i"))
        self.assertTrue(self._hits("git *", "echo hi && git status"))

    def test_single_segment_behaves_exactly_like_match_command(self) -> None:
        # 非复合命令必须逐字不变（这是「只放宽复合命令这一种形态」的依据）。
        for pattern, command in (
            ("git push *", "git push origin main"),
            ("git push *", "git status"),
            ("npm run build", "npm run build"),
            ("npm run build", "npm run test"),
            ("", "anything goes"),
        ):
            with self.subTest(pattern=pattern, command=command):
                self.assertEqual(
                    self._hits(pattern, command), match_command(pattern, command)
                )

    def test_predicate_is_not_called_again_for_single_segment(self) -> None:
        # 单段时不重复求值：既是省一次调用，也保证「非复合命令」路径上
        # predicate 恰好被调用一次（有副作用的 predicate 才不会被坑）。
        calls: list[str] = []

        def predicate(one: str) -> bool:
            calls.append(one)
            return False

        self.assertFalse(match_command_deep("git status", predicate))
        self.assertEqual(calls, ["git status"])


class MatchPathTests(unittest.TestCase):
    def test_bare_name_any_depth(self) -> None:
        self.assertTrue(match_path(".env", "src/.env"))
        self.assertTrue(match_path(".env", ".env"))
        self.assertFalse(match_path(".env", "src/app.py"))
        self.assertTrue(match_path("*.env", "a/b/c.env"))

    def test_slash_patterns(self) -> None:
        self.assertTrue(match_path("src/**", "src/a/b.py"))
        self.assertTrue(match_path("**/.env", "a/b/.env"))
        self.assertTrue(match_path("**/.env", ".env"))
        self.assertFalse(match_path("src/**", "lib/a.py"))

    def test_windows_separator_and_case(self) -> None:
        # 反斜杠归一化 + 大小写不敏感（spec N8）
        self.assertTrue(match_path("config.yaml", "Config.YAML"))
        self.assertTrue(match_path("src/**", "src\\a\\b.py"))


if __name__ == "__main__":
    unittest.main()
