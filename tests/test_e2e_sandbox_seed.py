"""
沙箱、预置与指纹测试（P1a T13）。

覆盖：
- **AC18** 可丢弃校验的两面（临时目录通过 / 项目根拒绝 / **非空临时目录仍通过**）
- 清理三步的必要性（Windows 下 cwd 在待删目录内时直接 rmtree 会失败）
- 六个预置函数各自的落盘位置与内容
- 指纹的稳定性与「mtime 变了就变」
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import yaml

from tests.e2e import fingerprint, sandbox, seeding
from tests.e2e.sandbox import NotDisposableError


REPO_ROOT = Path(__file__).resolve().parent.parent


class DisposableCheckTest(unittest.TestCase):
    """AC18：两条判据一起验，且与「目录是否为空」解耦。"""

    def test_fresh_workspace_passes(self):
        ws = sandbox.create_workspace()
        self.addCleanup(sandbox.force_rmtree, ws)
        sandbox.assert_disposable(ws)  # 不抛即通过

    def test_fresh_user_dir_passes(self):
        ud = sandbox.create_user_dir()
        self.addCleanup(sandbox.force_rmtree, ud)
        sandbox.assert_disposable(ud)

    def test_repo_root_is_rejected(self):
        # 判据①：项目根不在系统临时目录之下 —— 这是 rmtree 之前唯一的闸门
        with self.assertRaises(NotDisposableError) as ctx:
            sandbox.assert_disposable(REPO_ROOT)
        self.assertIn("判据①", str(ctx.exception))

    def test_temp_dir_without_marker_is_rejected(self):
        # 判据②：临时目录里没有标记文件，说明不是本设施造的，同样拒绝
        with tempfile.TemporaryDirectory(prefix="rhine_e2e_notours_") as raw:
            with self.assertRaises(NotDisposableError) as ctx:
                sandbox.assert_disposable(raw)
            self.assertIn("判据②", str(ctx.exception))

    def test_non_empty_workspace_still_passes(self):
        """
        **AC18 的另一半**：预置之后工作区非空是常态，
        「可丢弃」的判据与「目录是否为空」完全无关。
        """
        ws = sandbox.create_workspace()
        self.addCleanup(sandbox.force_rmtree, ws)
        seeding.seed_files(ws, {"src/a.py": "print(1)\n", "RHINE.md": "# 项目指令\n"})
        seeding.seed_project_skill(ws, "demo", {"description": "演示"}, "步骤一")
        self.assertTrue(any(ws.iterdir()))
        sandbox.assert_disposable(ws)


class ForceRmtreeGuardTest(unittest.TestCase):
    """
    `force_rmtree` 的闸门护栏。

    **背景是一次真实事故**：本函数最初「不做可丢弃校验」，调用方从 JSON 抽路径的
    `sed` 因反斜杠失败、变量成了空串，于是它收到 `""`——`Path("")` 解析成 `"."`
    即**当前工作目录**，`rmtree` 把整个代码仓库删光（靠远端仓库才恢复）。

    教训不是「调用方要小心」而是**闸门不该有旁路**。下面每一条都对应那次事故里
    本该拦住它的一环。
    """

    def test_empty_string_is_rejected(self):
        """**引信本身**：空串 → `Path("")` → `"."` → 当前工作目录。"""
        with self.assertRaises(NotDisposableError) as ctx:
            sandbox.force_rmtree("")
        self.assertIn("当前工作目录", str(ctx.exception))

    def test_relative_path_is_rejected(self):
        for candidate in (".", "..", "rhinecode", "./tests"):
            with self.assertRaises(NotDisposableError):
                sandbox.force_rmtree(candidate)

    def test_repo_root_is_rejected(self):
        # 绝对路径但不在临时目录下 —— 判据①拦下
        with self.assertRaises(NotDisposableError) as ctx:
            sandbox.force_rmtree(REPO_ROOT)
        self.assertIn("判据①", str(ctx.exception))
        self.assertTrue(REPO_ROOT.exists(), "仓库必须完好无损")

    def test_temp_dir_without_marker_is_rejected(self):
        # 在临时目录下但不是本设施造的 —— 判据②拦下
        with tempfile.TemporaryDirectory(prefix="rhine_e2e_notours_") as raw:
            with self.assertRaises(NotDisposableError) as ctx:
                sandbox.force_rmtree(raw)
            self.assertIn("判据②", str(ctx.exception))
            self.assertTrue(Path(raw).exists(), "不该被删掉")

    def test_real_sandbox_is_deleted(self):
        # 正常路径仍然能删（闸门不能把正事挡住）
        ws = sandbox.create_workspace()
        seeding.seed_files(ws, {"a/b.txt": "x"})
        sandbox.force_rmtree(ws)
        self.assertFalse(ws.exists())

    def test_missing_path_is_idempotent(self):
        ws = sandbox.create_workspace()
        sandbox.force_rmtree(ws)
        sandbox.force_rmtree(ws)  # 第二次：已不存在，静默返回而不是抛错


class CleanupOrderTest(unittest.TestCase):
    """清理三步：第②步（先 chdir 出来）不是保险，是必需。"""

    def test_cleanup_from_inside_workspace(self):
        previous = Path.cwd()
        ws = sandbox.create_workspace()
        os.chdir(ws)
        try:
            sandbox.cleanup_workspace(ws, previous_cwd=previous)
        finally:
            # cleanup_workspace 正常时已经切回去了；失败时这里兜底
            os.chdir(previous)
        self.assertFalse(ws.exists())
        self.assertEqual(Path.cwd().resolve(), previous.resolve())

    @unittest.skipUnless(sys.platform == "win32", "WinError 32 是 Windows 特有行为")
    def test_direct_rmtree_from_inside_fails_on_windows(self):
        """
        反证第②步的必要性：cwd 位于待删目录内时直接 rmtree 会抛
        `PermissionError [WinError 32] 另一个程序正在使用此文件`——
        那个「另一个程序」就是本进程自己。
        """
        previous = Path.cwd()
        ws = sandbox.create_workspace()
        os.chdir(ws)
        try:
            with self.assertRaises(PermissionError):
                shutil.rmtree(ws)
        finally:
            os.chdir(previous)
            # ⚠️ 这里**不能**用 `sandbox.force_rmtree`：上面那次直接 `rmtree` 会在
            # 撞上 cwd 锁之前先把标记文件删掉，于是目录已经不满足「可丢弃」判据②，
            # 闸门会（正确地）拒绝它。本用例是刻意把目录搞成半删状态的，
            # 收尾只能用裸 `rmtree`——这是全项目**唯一**该这么写的地方。
            shutil.rmtree(ws, ignore_errors=True)

    def test_cleanup_refuses_non_disposable(self):
        # 清理前永远先校验：传一个不可丢弃的路径必须抛错而不是开删
        previous = Path.cwd()
        with self.assertRaises(NotDisposableError):
            sandbox.cleanup_workspace(REPO_ROOT, previous_cwd=previous)
        self.assertTrue(REPO_ROOT.exists())


class SeedingTest(unittest.TestCase):
    def setUp(self):
        self.ws = sandbox.create_workspace()
        self.addCleanup(sandbox.force_rmtree, self.ws)
        self.user = sandbox.create_user_dir()
        self.addCleanup(sandbox.force_rmtree, self.user)

    def test_seed_files_creates_parents_and_utf8(self):
        seeding.seed_files(self.ws, {"a/b/c.txt": "中文内容 [dim]标记[/dim]"})
        target = self.ws / "a" / "b" / "c.txt"
        self.assertTrue(target.is_file())
        self.assertEqual(target.read_text(encoding="utf-8"), "中文内容 [dim]标记[/dim]")

    def test_seed_project_skill_location_and_frontmatter(self):
        path = seeding.seed_project_skill(
            self.ws, "review", {"description": "代码审阅", "allowed_tools": ["read_file", "glob_files"]},
            "第一步：读代码\n\n$ARGUMENTS",
        )
        self.assertEqual(path, self.ws / ".rhinecode" / "skills" / "review.md")
        text = path.read_text(encoding="utf-8")
        self.assertTrue(text.startswith("---\n"))
        self.assertIn("name: review", text)
        self.assertIn("代码审阅", text)
        self.assertIn("$ARGUMENTS", text)

    def test_seed_user_skill_location(self):
        # 用户级路径规则与项目级不同：直接在 user_dir 下，没有 .rhinecode 这一层
        path = seeding.seed_user_skill(self.user, "mine", {"description": "个人"}, "正文")
        self.assertEqual(path, self.user / "skills" / "mine.md")
        self.assertIn("name: mine", path.read_text(encoding="utf-8"))

    def test_seed_permissions_takes_final_dir(self):
        # 项目级：调用方自己拼 .rhinecode
        path = seeding.seed_permissions(
            self.ws / ".rhinecode", allow=["Read(*)"], deny=["Bash(rm *)"]
        )
        self.assertEqual(path, self.ws / ".rhinecode" / "permissions.yaml")
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        self.assertEqual(data["allow"], ["Read(*)"])
        self.assertEqual(data["deny"], ["Bash(rm *)"])

        # 用户级：直接就是 user_dir
        upath = seeding.seed_permissions(self.user, deny=["Read(secret.txt)"])
        self.assertEqual(upath, self.user / "permissions.yaml")

    def test_seed_rhine_md(self):
        path = seeding.seed_rhine_md(self.ws, "# 本项目\n\n用中文回答。\n")
        self.assertEqual(path, self.ws / "RHINE.md")
        self.assertIn("用中文回答", path.read_text(encoding="utf-8"))

    def test_seed_git_repo_creates_history(self):
        try:
            seeding.seed_git_repo(
                self.ws,
                [
                    {"message": "初始提交", "files": {"main.py": "print('hi')\n"}},
                    {"message": "修复登录超时", "files": {"auth.py": "TIMEOUT = 30\n"}},
                ],
            )
        except seeding.GitUnavailableError as e:
            # 环境前置已登记进 checklist；缺 git 时明确说明而不是假装通过
            self.skipTest(f"本机没有 git，跳过（这是已登记的环境前置）：{e}")

        out = subprocess.run(
            ["git", "log", "--oneline"],
            cwd=str(self.ws),
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            check=True,
        ).stdout
        self.assertIn("初始提交", out)
        self.assertIn("修复登录超时", out)
        self.assertTrue((self.ws / "auth.py").is_file())


class FingerprintTest(unittest.TestCase):
    def test_stable_across_calls(self):
        pkg = REPO_ROOT / "rhinecode"
        self.assertEqual(fingerprint.compute(pkg), fingerprint.compute(pkg))
        self.assertEqual(len(fingerprint.compute(pkg)), 12)

    def test_changes_when_mtime_changes(self):
        """改了文件就必须变——这是本模块唯一需要保证的性质。"""
        with tempfile.TemporaryDirectory(prefix="rhine_e2e_fp_") as raw:
            pkg = Path(raw)
            (pkg / "sub").mkdir()
            (pkg / "sub" / "a.py").write_text("x = 1\n", encoding="utf-8")
            before = fingerprint.compute(pkg)

            # 改内容（连带 size 与 mtime 都会变）
            (pkg / "sub" / "a.py").write_text("x = 22\n", encoding="utf-8")
            self.assertNotEqual(before, fingerprint.compute(pkg))

    def test_pycache_is_ignored(self):
        with tempfile.TemporaryDirectory(prefix="rhine_e2e_fp2_") as raw:
            pkg = Path(raw)
            (pkg / "a.py").write_text("x = 1\n", encoding="utf-8")
            before = fingerprint.compute(pkg)
            cache = pkg / "__pycache__"
            cache.mkdir()
            (cache / "a.cpython-311.py").write_text("junk\n", encoding="utf-8")
            self.assertEqual(before, fingerprint.compute(pkg), "编译缓存不该影响指纹")

    def test_missing_dir_returns_unknown(self):
        # 宿主不该因为算不出指纹而起不来
        self.assertEqual(fingerprint.compute(REPO_ROOT / "no_such_pkg"), "unknown")


if __name__ == "__main__":
    unittest.main()
