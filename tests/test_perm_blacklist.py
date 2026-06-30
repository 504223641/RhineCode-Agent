"""blacklist 模块单测：危险命令拦截 + 复合命令逐段（c6 T3，对应 AC2）。"""

import unittest

from rhinecode.permission.blacklist import check_command


class BlacklistTests(unittest.TestCase):
    def test_recursive_force_delete(self) -> None:
        self.assertIsNotNone(check_command("rm -rf /"))
        self.assertIsNotNone(check_command("rm -fr ."))
        self.assertIsNotNone(check_command("rm -r -f somedir"))

    def test_compound_command_second_segment(self) -> None:
        # 复合命令：前半安全、后半危险 → 整条被拦（AC2）
        self.assertIsNotNone(check_command("echo ok && rm -rf ."))
        self.assertIsNotNone(check_command("git status; rm -rf build"))

    def test_git_destructive(self) -> None:
        self.assertIsNotNone(check_command("git push --force"))
        self.assertIsNotNone(check_command("git reset --hard HEAD~1"))
        self.assertIsNotNone(check_command("git clean -fd"))

    def test_windows_dangerous(self) -> None:
        self.assertIsNotNone(check_command("format C:"))
        self.assertIsNotNone(check_command("rd /s /q build"))
        self.assertIsNotNone(check_command("Remove-Item -Recurse -Force ."))

    def test_disk_and_forkbomb(self) -> None:
        self.assertIsNotNone(check_command("mkfs.ext4 /dev/sda1"))
        self.assertIsNotNone(check_command(":(){ :|:& };:"))

    def test_safe_commands_pass(self) -> None:
        self.assertIsNone(check_command("ls -la"))
        self.assertIsNone(check_command("git status"))
        self.assertIsNone(check_command("npm run build"))
        self.assertIsNone(check_command("rm file.txt"))  # 非递归删单文件不在黑名单


if __name__ == "__main__":
    unittest.main()
