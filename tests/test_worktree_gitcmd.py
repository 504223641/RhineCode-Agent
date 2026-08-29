"""
c14 T5：git 子进程封装（spec F8 / F11 / F16 / F20②）。

这些用例跑在**真实的临时 Git 仓库**上，不打桩。理由是本模块的全部价值就在于
「正确地解析 git 的输出」——用假输出测它，测的是我们自己写的字符串，
真正会变的东西（git 版本差异、porcelain 格式、Windows 路径形态）一条都碰不到。
"""

import unittest

from rhinecode.worktree import gitcmd
from rhinecode.worktree.models import GitCommandFailed, NotARepository
from tests.worktree_support import cleanup, commit_all, git, make_plain_dir, make_repo


class RepositoryProbeTest(unittest.TestCase):
    """仓库探测：`rhine` 可以在任意目录启动，非仓库是常见情形。"""

    def setUp(self):
        self.repo = make_repo()
        self.addCleanup(cleanup, self.repo)

    def test_inside_repository(self):
        gitcmd.ensure_repository(self.repo)  # 不抛即通过

    def test_outside_repository(self):
        outside = self.repo / "not-a-repo"
        outside.mkdir()
        # 注意：它在 repo 内部，所以仍是仓库的一部分——要真正跳出去才行。
        gitcmd.ensure_repository(outside)

    def test_temp_dir_without_repo(self):
        plain = make_plain_dir()
        self.addCleanup(cleanup, plain)
        with self.assertRaises(NotARepository):
            gitcmd.ensure_repository(plain)

    def test_head_commit_is_short_hash(self):
        head = gitcmd.head_commit(self.repo)
        self.assertTrue(head)
        self.assertLessEqual(len(head), 12)
        self.assertTrue(all(c in "0123456789abcdef" for c in head))


class WorktreeLifecycleTest(unittest.TestCase):
    """建/列/删工作目录。"""

    def setUp(self):
        self.repo = make_repo()
        self.addCleanup(cleanup, self.repo)
        self.base = gitcmd.head_commit(self.repo)
        self.wt = self.repo / ".rhinecode" / "worktrees" / "w1"
        self.wt.parent.mkdir(parents=True, exist_ok=True)

    def test_add_then_list(self):
        before = gitcmd.list_worktrees(self.repo)
        gitcmd.add_worktree(self.repo, self.wt, "agent/w1", self.base)
        after = gitcmd.list_worktrees(self.repo)
        self.assertEqual(len(after), len(before) + 1)
        self.assertIn(self.wt.resolve(), after)

    def test_added_worktree_has_git_pointer_file(self):
        """
        隔离工作区的 `.git` 是**文件**不是目录——这是「快速恢复不调 git」
        的物理依据（spec F9），也是「共享版本库开销为零」的直接证据。
        """
        gitcmd.add_worktree(self.repo, self.wt, "agent/w1", self.base)
        marker = self.wt / ".git"
        self.assertTrue(marker.is_file())
        content = marker.read_text(encoding="utf-8").strip()
        self.assertTrue(content.startswith("gitdir:"))
        self.assertIn("worktrees", content)

    def test_branch_exists(self):
        self.assertFalse(gitcmd.branch_exists(self.repo, "agent/w1"))
        gitcmd.add_worktree(self.repo, self.wt, "agent/w1", self.base)
        self.assertTrue(gitcmd.branch_exists(self.repo, "agent/w1"))

    def test_remove_then_prune(self):
        gitcmd.add_worktree(self.repo, self.wt, "agent/w1", self.base)
        gitcmd.remove_worktree(self.repo, self.wt)
        gitcmd.prune_worktrees(self.repo)
        self.assertNotIn(self.wt.resolve(), gitcmd.list_worktrees(self.repo))
        self.assertFalse(self.wt.exists())

    def test_delete_branch(self):
        gitcmd.add_worktree(self.repo, self.wt, "agent/w1", self.base)
        gitcmd.remove_worktree(self.repo, self.wt)
        gitcmd.delete_branch(self.repo, "agent/w1")
        self.assertFalse(gitcmd.branch_exists(self.repo, "agent/w1"))

    def test_add_to_existing_path_fails(self):
        gitcmd.add_worktree(self.repo, self.wt, "agent/w1", self.base)
        with self.assertRaises(GitCommandFailed):
            gitcmd.add_worktree(self.repo, self.wt, "agent/w2", self.base)


class ChangeDetectionTest(unittest.TestCase):
    """变更检测——它决定「保留还是删除」，判错就是丢数据。"""

    def setUp(self):
        self.repo = make_repo()
        self.addCleanup(cleanup, self.repo)
        self.base = gitcmd.head_commit(self.repo)
        self.wt = self.repo / ".rhinecode" / "worktrees" / "w1"
        self.wt.parent.mkdir(parents=True, exist_ok=True)
        gitcmd.add_worktree(self.repo, self.wt, "agent/w1", self.base)
        git(["config", "user.name", "c14 test"], self.wt)
        git(["config", "user.email", "c14@example.invalid"], self.wt)

    def test_clean_worktree(self):
        self.assertEqual(gitcmd.status(self.wt), (False, ()))

    def test_modified_tracked_file(self):
        (self.wt / "seed.txt").write_text("changed\n", encoding="utf-8")
        dirty, files = gitcmd.status(self.wt)
        self.assertTrue(dirty)
        self.assertIn("seed.txt", files)

    def test_untracked_file_counts_as_dirty(self):
        """
        ⚠ 这条是本文件最重要的一条。

        子 Agent 新建的文件默认是**未跟踪**的。如果 status 只看已跟踪文件的修改，
        一个「刚写完新文件还没 add」的工作区会被判成干净而被自动删掉，
        成果直接消失且不报错。
        """
        (self.wt / "brand-new.py").write_text("x = 1\n", encoding="utf-8")
        dirty, files = gitcmd.status(self.wt)
        self.assertTrue(dirty, "未跟踪文件必须算作有变更")
        self.assertIn("brand-new.py", files)

    def test_no_commits_since_base(self):
        self.assertEqual(gitcmd.commits_since(self.wt, self.base), (0, ()))

    def test_commits_since_base(self):
        (self.wt / "added.py").write_text("y = 2\n", encoding="utf-8")
        commit_all(self.wt, "add file")
        count, files = gitcmd.commits_since(self.wt, self.base)
        self.assertEqual(count, 1)
        self.assertIn("added.py", files)

    def test_commit_leaves_worktree_clean(self):
        (self.wt / "added.py").write_text("y = 2\n", encoding="utf-8")
        commit_all(self.wt, "add file")
        self.assertEqual(gitcmd.status(self.wt), (False, ()))

    def test_unknown_base_returns_zero(self):
        """基点不存在时按「无新增提交」处理，不抛异常。"""
        self.assertEqual(gitcmd.commits_since(self.wt, "deadbeef"), (0, ()))


class BranchCommitCountTest(unittest.TestCase):
    """
    `commits_on_branch`——启动清理**唯一**能算出「这个分支上有几个提交」的办法。

    清理面对的是上次运行留下的目录，创建时那个基点无从得知，只能用 merge-base
    把分叉点算回来。判错的方向很要命：把「有提交」误判成「没有」，
    清理就会连分支一起删掉，成果直接消失。
    """

    def setUp(self):
        self.repo = make_repo()
        self.addCleanup(cleanup, self.repo)
        self.base = gitcmd.head_commit(self.repo)
        self.wt = self.repo / ".rhinecode" / "worktrees" / "w1"
        self.wt.parent.mkdir(parents=True, exist_ok=True)
        gitcmd.add_worktree(self.repo, self.wt, "agent/w1", self.base)
        git(["config", "user.name", "c14 test"], self.wt)
        git(["config", "user.email", "c14@example.invalid"], self.wt)

    def test_no_commits(self):
        self.assertEqual(gitcmd.commits_on_branch(self.repo, "agent/w1"), 0)

    def test_counts_commits_without_knowing_base(self):
        (self.wt / "a.py").write_text("a\n", encoding="utf-8")
        commit_all(self.wt, "one")
        (self.wt / "b.py").write_text("b\n", encoding="utf-8")
        commit_all(self.wt, "two")

        self.assertEqual(gitcmd.commits_on_branch(self.repo, "agent/w1"), 2)

    def test_main_moving_forward_does_not_confuse_it(self):
        """
        主分支后来又提交了，分叉点仍应是当初那个点——
        否则一个没改任何东西的分支会被算成「落后 N 个提交」而误判。
        """
        (self.wt / "a.py").write_text("a\n", encoding="utf-8")
        commit_all(self.wt, "sub work")

        (self.repo / "main-side.txt").write_text("m\n", encoding="utf-8")
        commit_all(self.repo, "main work")

        self.assertEqual(gitcmd.commits_on_branch(self.repo, "agent/w1"), 1)

    def test_unknown_branch_returns_minus_one(self):
        """
        ⚠ 算不出来必须返回 -1，**不能返回 0**。
        0 的语义是「确定没有提交，可以连分支一起删」，而「算不出来」
        被当成「确定没有」就会删掉可能装着成果的分支。
        """
        self.assertEqual(gitcmd.commits_on_branch(self.repo, "agent/nope"), -1)


class HooksSharedTest(unittest.TestCase):
    """
    spec F11 的可运行证据：git 钩子在主仓库与隔离工作区之间**天然共享**。

    本章因此对 git hooks **什么都不做**。留这条护栏是为了让那个「不做」的决定
    有依据钉着——将来 git 若改变这个行为，这里会当场红，而不是让用户先撞上。
    """

    def test_hooks_path_is_shared(self):
        repo = make_repo()
        self.addCleanup(cleanup, repo)
        base = gitcmd.head_commit(repo)
        wt = repo / ".rhinecode" / "worktrees" / "w1"
        wt.parent.mkdir(parents=True, exist_ok=True)
        gitcmd.add_worktree(repo, wt, "agent/w1", base)

        self.assertEqual(gitcmd.hooks_path(repo), gitcmd.hooks_path(wt))


if __name__ == "__main__":
    unittest.main()
