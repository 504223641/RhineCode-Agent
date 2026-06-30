"""matching 模块单测：命令拆分、命令模式匹配、路径模式匹配（c6 T2）。"""

import unittest

from rhinecode.permission.matching import split_commands, match_command, match_path


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
