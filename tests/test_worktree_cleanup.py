"""
c14 T11：启动清理（spec F19 / F21 / AC25–AC30）。

清理是本章唯一会**自动删东西**的路径，所以这里的用例分两类，缺一不可：

- 「该删的删掉了」——否则清理是死代码（CLAUDE.md 里已有 `dropped_fatal` 这个
  「写了但永远进不去的分支」被当技术债登记的先例）；
- 「不该删的一根汗毛都没动」——这一半如果失效，表现是用户的成果无声消失。

用「把目录 mtime 调老」来构造过期，而不是等真实时间——测试必须是确定性的。
"""

import os
import time
import unittest

from rhinecode.worktree import cleanup as cleanup_mod
from rhinecode.worktree import gitcmd, lifecycle
from tests.worktree_support import cleanup, commit_all, git, make_repo

# 比缺省阈值（7 天）明显更老，避免边界抖动。
_OLD = 30 * 86400


def _age(path, seconds: float = _OLD) -> None:
    """把一个目录树里全部文件的修改时间往前拨，构造「过期」。"""
    stamp = time.time() - seconds
    for root, _dirs, files in os.walk(path):
        for name in files:
            target = os.path.join(root, name)
            try:
                os.utime(target, (stamp, stamp))
            except OSError:
                pass
    try:
        os.utime(path, (stamp, stamp))
    except OSError:
        pass


class NoDirectoryTest(unittest.TestCase):
    """根目录不存在是最常见的情形（绝大多数项目从没用过隔离）。"""

    def test_missing_root_returns_empty_report(self):
        repo = make_repo()
        self.addCleanup(cleanup, repo)
        report = cleanup_mod.scan_and_clean(repo, 7)
        self.assertTrue(report.is_empty)
        self.assertEqual(report.scanned, 0)


class CleanupTestBase(unittest.TestCase):
    def setUp(self):
        self.repo = make_repo()
        self.addCleanup(cleanup, self.repo)

    def _make(self, name: str):
        handle, _ = lifecycle.create(self.repo, name)
        git(["config", "user.name", "c14 test"], handle.path)
        git(["config", "user.email", "c14@example.invalid"], handle.path)
        return handle


class AgeThresholdTest(CleanupTestBase):
    """过期判定（AC25）。"""

    def test_fresh_worktree_is_untouched(self):
        handle = self._make("fresh")
        report = cleanup_mod.scan_and_clean(self.repo, 7)
        self.assertTrue(handle.path.is_dir())
        self.assertEqual(report.scanned, 0)
        self.assertTrue(report.is_empty)

    def test_expired_clean_worktree_is_removed(self):
        handle = self._make("stale")
        _age(handle.path)

        report = cleanup_mod.scan_and_clean(self.repo, 7)

        self.assertFalse(handle.path.exists())
        self.assertEqual([n for n, _ in report.removed], ["stale"])
        self.assertFalse(gitcmd.branch_exists(self.repo, handle.branch))

    def test_zero_threshold_disables_cleanup(self):
        """阈值 ≤ 0 视为用户关闭了本功能——一个都不动。"""
        handle = self._make("stale")
        _age(handle.path)

        report = cleanup_mod.scan_and_clean(self.repo, 0)

        self.assertTrue(handle.path.is_dir())
        self.assertTrue(report.is_empty)

    def test_mtime_uses_newest_file_not_creation(self):
        """
        一个**持续在用**的工作区不该被判过期。
        把整棵树拨老、再单独摸新一个文件，它就应该活下来。
        """
        handle = self._make("busy")
        _age(handle.path)
        fresh = handle.path / "just-touched.txt"
        fresh.write_text("now\n", encoding="utf-8")

        report = cleanup_mod.scan_and_clean(self.repo, 7)

        self.assertTrue(handle.path.is_dir())
        self.assertTrue(report.is_empty)


class ChangeProtectionTest(CleanupTestBase):
    """变更保护——本文件的重点（AC28 / AC29 / N5）。"""

    def test_dirty_worktree_is_kept_with_reason(self):
        """
        AC28：有未提交改动就**无条件**不删，不看它多老。
        那些改动只存在于那个目录里，删掉永久丢失。
        """
        handle = self._make("wip")
        (handle.path / "half-done.py").write_text("TODO\n", encoding="utf-8")
        _age(handle.path)

        report = cleanup_mod.scan_and_clean(self.repo, 7)

        self.assertTrue(handle.path.is_dir(), "有未提交改动的工作区必须保留")
        self.assertTrue((handle.path / "half-done.py").is_file())
        kept = dict(report.kept)
        self.assertIn("wip", kept)
        self.assertIn("未提交", kept["wip"])

    def test_committed_worktree_loses_dir_keeps_branch(self):
        """
        AC29 / F21：有提交时删目录、**留分支**，成果可从版本库取回。
        这是清理能真正回收空间又不丢数据的原因。
        """
        handle = self._make("done")
        (handle.path / "result.py").write_text("ANSWER = 42\n", encoding="utf-8")
        commit_all(handle.path, "work")
        _age(handle.path)

        report = cleanup_mod.scan_and_clean(self.repo, 7)

        self.assertFalse(handle.path.exists(), "目录应被回收")
        self.assertTrue(
            gitcmd.branch_exists(self.repo, handle.branch), "分支必须保留"
        )
        shown = git(["show", f"{handle.branch}:result.py"], self.repo)
        self.assertIn("ANSWER = 42", shown)

        removed = dict(report.removed)
        self.assertEqual(removed.get("done"), handle.branch)

    def test_report_surfaces_kept_branch_name(self):
        """
        用户看到「目录没了」时必须能知道成果在哪个分支上，
        否则他会以为工作丢了。
        """
        handle = self._make("done")
        (handle.path / "r.py").write_text("x\n", encoding="utf-8")
        commit_all(handle.path, "work")
        _age(handle.path)

        report = cleanup_mod.scan_and_clean(self.repo, 7)

        self.assertTrue(any(branch for _, branch in report.removed))


class ScopeTest(CleanupTestBase):
    """清理的作用范围——不该碰的一律不碰。"""

    def test_manual_directory_is_not_removed(self):
        """AC27：手工放进去的普通目录不被版本库登记，②层拒绝。"""
        root = lifecycle.worktrees_root(self.repo)
        manual = root / "my-notes"
        manual.mkdir(parents=True, exist_ok=True)
        (manual / "notes.md").write_text("mine\n", encoding="utf-8")
        _age(manual)

        report = cleanup_mod.scan_and_clean(self.repo, 7)

        self.assertTrue((manual / "notes.md").is_file(), "用户的目录必须原封不动")
        self.assertIn("my-notes", dict(report.kept))

    def test_sessions_directory_is_never_scanned(self):
        """
        清理只看 `.rhinecode/worktrees/` 下面。会话存档、上下文存盘等
        兄弟目录**根本不进入候选**。
        """
        sessions = self.repo / ".rhinecode" / "sessions"
        sessions.mkdir(parents=True, exist_ok=True)
        (sessions / "a.jsonl").write_text("data\n", encoding="utf-8")
        _age(sessions)

        cleanup_mod.scan_and_clean(self.repo, 7)

        self.assertTrue((sessions / "a.jsonl").is_file())

    def test_only_expired_entries_are_scanned(self):
        fresh = self._make("fresh")
        stale = self._make("stale")
        _age(stale.path)

        report = cleanup_mod.scan_and_clean(self.repo, 7)

        self.assertEqual(report.scanned, 1)
        self.assertTrue(fresh.path.is_dir())
        self.assertFalse(stale.path.exists())


class OrphanAndFailureTest(CleanupTestBase):
    """残留与异常（AC30 + fail-safe）。"""

    def test_orphan_directory_is_reported_not_crashed(self):
        """
        AC30 的实测形态：git 先注销登记、再在删目录时失败，
        留下「版本库不认、磁盘还在」的残骸。

        清理面对它时必须**不抛异常**。它会被②层归属拦下并记进 kept——
        这是刻意的保守选择：一个版本库不认的目录，我们没有任何权威依据
        断定它可以删。
        """
        handle = self._make("orphan")
        gitcmd.remove_worktree(self.repo, handle.path)
        handle.path.mkdir(parents=True, exist_ok=True)
        (handle.path / "leftover.txt").write_text("x\n", encoding="utf-8")
        _age(handle.path)

        report = cleanup_mod.scan_and_clean(self.repo, 7)

        self.assertIn("orphan", dict(report.kept))

    def test_failing_entry_does_not_break_the_rest(self):
        """
        一个坏条目不影响其余，且整体不抛——清理绝不能阻断启动。
        """
        good = self._make("good")
        bad = self._make("bad")
        _age(good.path)
        _age(bad.path)

        original = lifecycle.inspect

        def flaky(handle):
            if handle.name == "bad":
                raise RuntimeError("模拟状态查询失败")
            return original(handle)

        cleanup_mod.lifecycle.inspect = flaky
        try:
            report = cleanup_mod.scan_and_clean(self.repo, 7)
        finally:
            cleanup_mod.lifecycle.inspect = original

        self.assertFalse(good.path.exists(), "好条目照常被处理")
        self.assertIn("bad", dict(report.kept))
        self.assertIn("出错", dict(report.kept)["bad"])


if __name__ == "__main__":
    unittest.main()
