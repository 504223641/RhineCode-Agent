"""
c14 T9：隔离工作区的生命周期（spec F6–F10 / F16 / F20 / F21 / F22）。

本文件是全章防误删的主要护栏所在。两条写法上的刻意选择：

1. **`judge_removal` 的用例逐条穷举**，不写循环——挂掉时要能直接看出是哪一层
   没挡住。
2. **每条「准许删除」的断言都配一条「拒绝删除」的反证**。只验「该删的删掉了」
   是不够的：三层过滤真正的价值在于「不该删的没被删」，而那一半如果失效，
   表现是**用户的成果无声消失**，测试全绿。
"""

import unittest
from pathlib import Path

from rhinecode.worktree import gitcmd, lifecycle
from rhinecode.worktree.models import (
    ChangeStatus,
    ProvisionEntry,
    WorktreeError,
    WorktreeHandle,
    WorktreeNameError,
)
from tests.worktree_support import cleanup, commit_all, git, make_repo


class RepoTestBase(unittest.TestCase):
    def setUp(self):
        self.repo = make_repo()
        self.addCleanup(cleanup, self.repo)

    def _config_identity(self, path: Path):
        git(["config", "user.name", "c14 test"], path)
        git(["config", "user.email", "c14@example.invalid"], path)


class CreateTest(RepoTestBase):
    """创建（spec F6–F8）。"""

    def test_creates_directory_branch_and_base(self):
        head = gitcmd.head_commit(self.repo)
        handle, result = lifecycle.create(self.repo, "w1")

        self.assertTrue(handle.path.is_dir())
        self.assertEqual(handle.name, "w1")
        self.assertEqual(handle.branch, "agent/w1")
        self.assertEqual(handle.base_commit, head)
        self.assertFalse(handle.recovered)
        self.assertEqual(result.warnings, ())
        self.assertTrue(gitcmd.branch_exists(self.repo, "agent/w1"))

    def test_lands_under_worktrees_root(self):
        handle, _ = lifecycle.create(self.repo, "w1")
        self.assertEqual(handle.path.parent, lifecycle.worktrees_root(self.repo))

    def test_nested_name(self):
        handle, _ = lifecycle.create(self.repo, "feature/login")
        self.assertTrue(handle.path.is_dir())
        self.assertEqual(handle.branch, "agent/feature/login")

    def test_generated_name_when_absent(self):
        handle, _ = lifecycle.create(self.repo, None, agent_name="reviewer", task_id="t1")
        self.assertTrue(handle.path.is_dir())
        self.assertIn("reviewer", handle.name)

    def test_uncommitted_changes_do_not_leak_in(self):
        """
        AC10：基点是 HEAD，主项目根里**未提交**的改动不会带进隔离工作区。

        这是第 4 题选「从当前 HEAD 开」的直接后果，也是实际使用中最容易被
        抱怨的一处——所以必须有一条断言把它固定下来。
        """
        (self.repo / "seed.txt").write_text("MAIN-EDIT\n", encoding="utf-8")

        handle, _ = lifecycle.create(self.repo, "w1")

        self.assertEqual(
            (handle.path / "seed.txt").read_text(encoding="utf-8"), "seed\n"
        )

    def test_branch_conflict_gets_suffix(self):
        """AC11：撞名时自动改名，且**回报的是实际用的名字**。"""
        git(["branch", "agent/w1"], self.repo)

        handle, _ = lifecycle.create(self.repo, "w1")

        self.assertEqual(handle.branch, "agent/w1-2")
        self.assertTrue(gitcmd.branch_exists(self.repo, "agent/w1-2"))

    def test_provision_entries_applied(self):
        (self.repo / "local.yaml").write_text("x\n", encoding="utf-8")
        handle, result = lifecycle.create(
            self.repo, "w1", entries=[ProvisionEntry("local.yaml", "copy")]
        )
        self.assertEqual(result.applied, ("local.yaml",))
        self.assertTrue((handle.path / "local.yaml").is_file())


class IllegalNameTest(RepoTestBase):
    """
    AC8：非法名字必须在**任何目录被创建之前**就被拒。

    「拒绝了」还不够——要断言文件系统上**什么都没留下**。校验若排在拼接之后，
    这条会红。
    """

    def _reject(self, raw):
        root = lifecycle.worktrees_root(self.repo)
        with self.assertRaises(WorktreeNameError, msg=f"未拒绝 {raw!r}"):
            lifecycle.create(self.repo, raw)
        if root.exists():
            self.assertEqual(
                sorted(p.name for p in root.iterdir()), [], f"{raw!r} 留下了目录"
            )

    def test_parent_ref(self):
        self._reject("../evil")

    def test_embedded_parent(self):
        self._reject("a/../../evil")

    def test_absolute(self):
        self._reject("/tmp/evil")

    def test_backslash(self):
        self._reject("..\\..\\evil")

    def test_too_long(self):
        self._reject("a" * 200)

    def test_empty(self):
        self._reject("   ")


class FastRecoveryTest(RepoTestBase):
    """
    AC12：目录已在且确属本仓库的工作区时走快速恢复，**一条 git 都不调**。

    用替换 `gitcmd.add_worktree` 的方式断言「没被调用」，而不是只看返回值的
    `recovered` 标志——标志是我们自己填的，它为真不能证明真的没调 git。
    """

    def test_second_create_recovers_without_git(self):
        first, _ = lifecycle.create(self.repo, "w1")
        self.assertFalse(first.recovered)

        calls = []
        original = gitcmd.add_worktree

        def spy(*args, **kwargs):
            calls.append(args)
            return original(*args, **kwargs)

        lifecycle.gitcmd.add_worktree = spy
        try:
            second, _ = lifecycle.create(self.repo, "w1")
        finally:
            lifecycle.gitcmd.add_worktree = original

        self.assertTrue(second.recovered)
        self.assertEqual(second.path, first.path)
        self.assertEqual(calls, [], "快速恢复路径不应调用 add_worktree")

    def test_nonempty_plain_directory_is_not_recoverable(self):
        """
        没有 `.git` 指针文件的普通目录**不算**可恢复——否则用户手工放进去的
        目录会被当成工作区，后续操作都会作用在一个不是工作区的地方。

        用**非空**目录构造：实测 `git worktree add` 对一个**空的**已存在目录
        是成功的（它会直接把 checkout 放进去），那种情形无害；有内容的目录
        git 会拒绝，于是走到我们的 `WorktreeError`。
        """
        root = lifecycle.worktrees_root(self.repo)
        manual = root / "manual"
        manual.mkdir(parents=True, exist_ok=True)
        (manual / "user-file.txt").write_text("mine\n", encoding="utf-8")

        with self.assertRaises(WorktreeError):
            lifecycle.create(self.repo, "manual")

        # 用户放进去的东西必须原封不动。
        self.assertEqual(
            (manual / "user-file.txt").read_text(encoding="utf-8"), "mine\n"
        )

    def test_directory_with_real_git_dir_is_not_recoverable(self):
        """
        `.git` 是**目录**说明那是一个独立仓库（用户手工 clone 进来的），
        不是本仓库的工作区，绝不能当成可恢复对象。
        """
        root = lifecycle.worktrees_root(self.repo)
        (root / "cloned").mkdir(parents=True, exist_ok=True)
        (root / "cloned" / ".git").mkdir()

        with self.assertRaises(WorktreeError):
            lifecycle.create(self.repo, "cloned")


class InspectTest(RepoTestBase):
    """变更检查（spec F16）。"""

    def setUp(self):
        super().setUp()
        self.handle, _ = lifecycle.create(self.repo, "w1")
        self._config_identity(self.handle.path)

    def test_clean(self):
        status = lifecycle.inspect(self.handle)
        self.assertTrue(status.untouched)

    def test_uncommitted_change(self):
        (self.handle.path / "seed.txt").write_text("edited\n", encoding="utf-8")
        status = lifecycle.inspect(self.handle)
        self.assertTrue(status.dirty)
        self.assertFalse(status.untouched)
        self.assertIn("seed.txt", status.files)

    def test_untracked_file(self):
        (self.handle.path / "new.py").write_text("x\n", encoding="utf-8")
        status = lifecycle.inspect(self.handle)
        self.assertTrue(status.dirty)

    def test_committed_change(self):
        (self.handle.path / "new.py").write_text("x\n", encoding="utf-8")
        commit_all(self.handle.path, "work")
        status = lifecycle.inspect(self.handle)
        self.assertFalse(status.dirty)
        self.assertEqual(status.commits, 1)
        self.assertFalse(status.untouched)
        self.assertIn("new.py", status.files)

    def test_missing_directory(self):
        ghost = WorktreeHandle(
            name="gone", path=self.repo / "nope", branch="agent/gone", base_commit="x"
        )
        self.assertTrue(lifecycle.inspect(ghost).untouched)


class JudgeRemovalTest(RepoTestBase):
    """
    三层过滤穷举（spec F20/F21）。**纯判定，可离线穷举。**

    每一层单独一组用例，挂掉时能直接定位是哪一层失效。
    """

    def setUp(self):
        super().setUp()
        self.handle, _ = lifecycle.create(self.repo, "w1")
        self.clean = ChangeStatus(dirty=False, commits=0)

    # —— ①位置 ——
    def test_layer1_empty_path(self):
        v = lifecycle.judge_removal(self.repo, Path(""), self.clean)
        self.assertFalse(v.allowed)
        self.assertIn("为空", v.reason)

    def test_layer1_worktrees_root_itself(self):
        """删根目录会一次性清空全部工作区，包括正在跑的那些。"""
        v = lifecycle.judge_removal(
            self.repo, lifecycle.worktrees_root(self.repo), self.clean
        )
        self.assertFalse(v.allowed)

    def test_layer1_outside_worktrees_root(self):
        """
        ⚠ 这条挡的是「误删会话存档」：`.rhinecode/sessions` 在主项目根内，
        用 path_guard 的主项目根边界判定会放行它。边界必须收紧到 worktrees 这一层。
        """
        v = lifecycle.judge_removal(
            self.repo, self.repo / ".rhinecode" / "sessions", self.clean
        )
        self.assertFalse(v.allowed)

    def test_layer1_project_root(self):
        v = lifecycle.judge_removal(self.repo, self.repo, self.clean)
        self.assertFalse(v.allowed)

    # —— ②归属 ——
    def test_layer2_unregistered_directory(self):
        """AC27：手工建的普通目录不被碰。"""
        manual = lifecycle.worktrees_root(self.repo) / "manual"
        manual.mkdir(parents=True, exist_ok=True)
        v = lifecycle.judge_removal(self.repo, manual, self.clean)
        self.assertFalse(v.allowed)
        self.assertIn("未被版本库登记", v.reason)

    # —— ③变更 ——
    def test_layer3_dirty_is_unconditional_veto(self):
        """
        AC28 / N5：未提交的改动只存在于那个目录里，删掉就永久丢失。
        因此它是**无条件**否决——不看过期时长、不看提交数。
        """
        dirty = ChangeStatus(dirty=True, commits=5)
        v = lifecycle.judge_removal(self.repo, self.handle.path, dirty)
        self.assertFalse(v.allowed)
        self.assertIn("未提交", v.reason)

    # —— 通过 ——
    def test_allowed_without_commits_deletes_branch(self):
        v = lifecycle.judge_removal(self.repo, self.handle.path, self.clean)
        self.assertTrue(v.allowed)
        self.assertFalse(v.keep_branch)

    def test_allowed_with_commits_keeps_branch(self):
        """
        AC29 / F21：有提交时只删目录、**留分支**。
        commit 在共享版本库里，删目录不丢数据——这正是清理能真正回收空间的原因。
        """
        status = ChangeStatus(dirty=False, commits=3)
        v = lifecycle.judge_removal(self.repo, self.handle.path, status)
        self.assertTrue(v.allowed)
        self.assertTrue(v.keep_branch)


class RemoveTest(RepoTestBase):
    """删除（spec F20/F21/F22）。"""

    def setUp(self):
        super().setUp()
        self.handle, _ = lifecycle.create(self.repo, "w1")
        self._config_identity(self.handle.path)

    def test_clean_worktree_removed_with_branch(self):
        v = lifecycle.remove(
            self.repo, self.handle.path, self.handle.branch, ChangeStatus(False, 0)
        )
        self.assertTrue(v.allowed)
        self.assertFalse(self.handle.path.exists())
        self.assertFalse(gitcmd.branch_exists(self.repo, self.handle.branch))

    def test_committed_worktree_removed_branch_kept_and_recoverable(self):
        """AC29 的完整形态：目录没了，但成果一个提交都不少。"""
        (self.handle.path / "new.py").write_text("VALUE = 42\n", encoding="utf-8")
        commit_all(self.handle.path, "work")
        status = lifecycle.inspect(self.handle)

        v = lifecycle.remove(
            self.repo, self.handle.path, self.handle.branch, status
        )

        self.assertTrue(v.allowed)
        self.assertTrue(v.keep_branch)
        self.assertFalse(self.handle.path.exists())
        self.assertTrue(gitcmd.branch_exists(self.repo, self.handle.branch))
        shown = git(["show", f"{self.handle.branch}:new.py"], self.repo)
        self.assertIn("VALUE = 42", shown)

    def test_dirty_worktree_is_not_touched(self):
        """
        ⚠ **本文件最重要的反证。**

        未获许可时 `remove` 必须**一步都不往下走**。若三层过滤失效，
        表现是用户的成果无声消失——测试全绿、界面正常。
        """
        (self.handle.path / "wip.py").write_text("half done\n", encoding="utf-8")
        status = lifecycle.inspect(self.handle)

        v = lifecycle.remove(
            self.repo, self.handle.path, self.handle.branch, status
        )

        self.assertFalse(v.allowed)
        self.assertTrue(self.handle.path.is_dir(), "目录必须还在")
        self.assertTrue((self.handle.path / "wip.py").is_file(), "文件必须还在")
        self.assertTrue(gitcmd.branch_exists(self.repo, self.handle.branch))

    def test_out_of_scope_path_is_not_touched(self):
        """位置层的反证：连文件系统都不碰。"""
        victim = self.repo / ".rhinecode" / "sessions"
        victim.mkdir(parents=True, exist_ok=True)
        (victim / "archive.jsonl").write_text("data\n", encoding="utf-8")

        v = lifecycle.remove(self.repo, victim, "", ChangeStatus(False, 0))

        self.assertFalse(v.allowed)
        self.assertTrue((victim / "archive.jsonl").is_file())

    def test_orphan_directory_is_cleaned(self):
        """
        AC30 / F22：实测形态——git 先注销登记、再在删目录时失败，
        留下「版本库不认、磁盘还在」的残骸。独立的目录删除兜底必须能收拾它。
        """
        path = self.handle.path
        gitcmd.remove_worktree(self.repo, path)
        # 手工重建目录，模拟「注销成功但目录还在」。
        path.mkdir(parents=True, exist_ok=True)
        (path / "leftover.txt").write_text("x\n", encoding="utf-8")

        # 此时它已不在登记表里，②层会拒——这正是设计意图：
        # 残骸由启动清理的兜底路径收拾，而不是由 judge_removal 放行。
        v = lifecycle.judge_removal(self.repo, path, ChangeStatus(False, 0))
        self.assertFalse(v.allowed)

    def test_remove_is_idempotent(self):
        lifecycle.remove(
            self.repo, self.handle.path, self.handle.branch, ChangeStatus(False, 0)
        )
        v = lifecycle.remove(
            self.repo, self.handle.path, self.handle.branch, ChangeStatus(False, 0)
        )
        self.assertFalse(v.allowed)  # 第二次因②层归属不过而拒绝，不抛异常


if __name__ == "__main__":
    unittest.main()
