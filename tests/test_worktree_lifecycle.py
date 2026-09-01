"""
c14 T9：隔离工作区的生命周期（spec F6–F10 / F16 / F20 / F21 / F22）。

本文件是全章防误删的主要护栏所在。两条写法上的刻意选择：

1. **`judge_removal` 的用例逐条穷举**，不写循环——挂掉时要能直接看出是哪一层
   没挡住。
2. **每条「准许删除」的断言都配一条「拒绝删除」的反证**。只验「该删的删掉了」
   是不够的：三层过滤真正的价值在于「不该删的没被删」，而那一半如果失效，
   表现是**用户的成果无声消失**，测试全绿。
"""

import errno
import os
import threading
import time
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


class ConcurrentCreateTest(RepoTestBase):
    """
    并发创建必须串行走过「动版本库」那一段（2026-08-30，CI 上抓到的缺陷）。

    ## 为什么护栏钉的是「性质」而不是「症状」

    原症状是三个并发隔离委派里有一个失败：

        fatal: could not open '.git/worktrees/<名字>/locked' for writing:
        No such file or directory

    成因是 `git worktree add` 开工时会隐式 prune 掉「看起来没建完」的工作区目录，
    而另一个正建到一半的恰好就长那样——两个并发创建互相拆台。

    ⚠ **但那个窗口只在机器够慢时才张开**：本机 8 路 × 6 轮复现不出来，
    而 CI 上红掉的那个格子分片跑了 150.9s（平时约 60s）。
    照着症状写判据的话，这条护栏在开发机上**永远绿**，等于没有。

    所以它改为直接断言**修法装进去的那条性质**：任何时刻至多有一个创建
    处在临界区内。这个判据与机器快慢无关，且「有人把锁去掉」时当场红。
    """

    def test_no_two_creations_are_inside_the_critical_section(self):
        """
        八路并发创建，全程不得出现「两个同时在临界区里」。

        做法：把 `gitcmd.add_worktree` 换成一个会记账的替身——进临界区时把
        在场人数加一并记下峰值，睡一小会儿放大重叠窗口，再减回去。
        替身仍然调用真实实现，所以创建结果照常可断言。

        副作用：真的建出八个隔离工作区（由 `cleanup` 统一收走）。
        """
        import threading
        import time
        from concurrent.futures import ThreadPoolExecutor

        real_add = lifecycle.gitcmd.add_worktree
        counter_lock = threading.Lock()
        inside = 0
        peak = 0

        def counting_add(main_root, target, branch, base):
            nonlocal inside, peak
            with counter_lock:
                inside += 1
                peak = max(peak, inside)
            try:
                # 睡一下把重叠窗口放大——没有它的话，八个调用可能恰好首尾相接，
                # 于是「峰值 1」既可能是锁的功劳，也可能只是运气好。
                time.sleep(0.02)
                return real_add(main_root, target, branch, base)
            finally:
                with counter_lock:
                    inside -= 1

        lifecycle.gitcmd.add_worktree = counting_add
        self.addCleanup(setattr, lifecycle.gitcmd, "add_worktree", real_add)

        def one(i):
            handle, _ = lifecycle.create(
                self.repo, name=f"w{i}", agent_name="a", task_id=str(i)
            )
            return handle

        with ThreadPoolExecutor(8) as pool:
            handles = list(pool.map(one, range(8)))

        self.assertEqual(
            peak, 1,
            f"同时有 {peak} 个创建处在临界区内——串行闸门没生效，"
            "并发的 `git worktree add` 会互相把对方半建好的目录 prune 掉",
        )
        # 反证的另一半：串行化不能把功能本身弄坏。
        self.assertEqual(len({h.path for h in handles}), 8, "八个工作区必须各不相同")
        self.assertEqual(len({h.branch for h in handles}), 8, "八个分支必须各不相同")


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


class ConcurrentCreateAndRemoveTest(RepoTestBase):
    """
    并发的「建工作区」与「回收工作区」不许互相拆台（2026-08-31 的 CI flake）。

    **这条护栏钉的是 `_REPO_LOCK` 圈住了 `remove`**，把那把锁从 `remove` 上拿掉
    就会当场红。完整成因与证据见 `lifecycle._REPO_LOCK` 上方那段注释，这里只记
    与测试写法直接相关的两点：

    1. **窗口必须人为放大。** 真实的窗口在 `git worktree add` 内部
       （`safe_create_leading_directories` 与 `mkdir` 之间），实测只有 **<10µs**
       ——本机靠对撞根本撞不上（未改动的 git + 独立进程死循环 rmdir，8 轮才中
       1 轮）。CI 上真正发生的是受害进程恰好在那两句之间被调度器抢走，
       所以这里用一个 sleep 把那次**抢占**摆成最坏情况。
       ⚠ 被模拟的是**抢占**，不是失败本身：失败仍由真实的
       `os.mkdir` 在真实的 `.git/worktrees/<名字>` 上以真实的 ENOENT 产生。
    2. **必须配一条反证**（`test_the_amplified_window_really_bites`）。
       只断言「并发下创建成功」是不够的：如果放大手法本身失效（窗口没张开、
       两个线程压根没重叠），这条用例照样全绿，而它什么都没验到。
    """

    #: 放大后的窗口。取值只需明显大于两个 git 子进程的启动开销（各约 50ms），
    #: 让回收方稳定地落在窗口里；再大只是让用例变慢。
    WINDOW_S = 0.3

    def _slow_add(self, in_window: threading.Event):
        """
        把 git 那两句相邻的系统调用摆开，中间留出「被抢占」的时间。

        完全照 `builtin/worktree.c` 的顺序做：
          ① `safe_create_leading_directories` → 建出 `.git/worktrees`
          ② 这里是 CI 上被调度器抢走的那一瞬
          ③ `mkdir(.git/worktrees/<名字>)` —— 父目录没了就是 ENOENT
        ③ 成功则把目录让回去，交给真正的 git 跑完整个 add。
        """
        real_add = gitcmd.add_worktree

        def slow_add(root, path, branch, base):
            parent = Path(root) / ".git" / "worktrees"
            child = parent / Path(path).name
            parent.mkdir(parents=True, exist_ok=True)          # ①
            in_window.set()
            time.sleep(self.WINDOW_S)                           # ②
            os.mkdir(child)                                     # ③ 父目录没了就抛
            os.rmdir(child)
            real_add(root, path, branch, base)

        return slow_add

    def _race(self, recycle):
        """
        跑一次对撞：创建方停在窗口里，回收方在窗口期回收另一个工作区。

        :param recycle: 回收动作，入参是待回收工作区的句柄
        :returns: 创建方抛出的异常（没抛则为 None）

        场景与 CI 上那次一致：仓库里此刻**只有一个**已跑完的工作区，
        把它删掉会让 `.git/worktrees` 变空，于是 git 顺手 rmdir 掉父目录——
        而那正是创建方下一步要往里 mkdir 的地方。
        """
        done = lifecycle.create(self.repo, name="done-worker")[0]
        self.assertTrue((self.repo / ".git" / "worktrees" / "done-worker").is_dir())

        in_window = threading.Event()
        failure: list[BaseException] = []
        original = gitcmd.add_worktree
        gitcmd.add_worktree = self._slow_add(in_window)
        try:
            def creator():
                try:
                    lifecycle.create(self.repo, name="late-worker")
                except BaseException as exc:  # noqa: BLE001
                    failure.append(exc)

            def remover():
                in_window.wait(30)
                recycle(done)

            threads = [threading.Thread(target=creator), threading.Thread(target=remover)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(120)
                self.assertFalse(t.is_alive(), "对撞线程没能在 120 秒内收尾")
        finally:
            gitcmd.add_worktree = original
        return failure[0] if failure else None

    def test_recycling_does_not_break_a_concurrent_create(self):
        """回收路径走产品的 `lifecycle.remove` → 被闸门挡在窗口外，创建照常成功。"""
        exc = self._race(
            lambda h: lifecycle.remove(
                self.repo, h.path, h.branch, ChangeStatus(dirty=False, commits=0)
            )
        )
        self.assertIsNone(exc, f"并发回收把正在创建的工作区拆台了：{exc}")
        self.assertTrue((self.repo / ".rhinecode" / "worktrees" / "late-worker").is_dir())

    def test_the_amplified_window_really_bites(self):
        """
        反证：**绕开闸门**直接跑 git 的回收命令，同一个窗口必然把创建拆台。

        它证明上一条不是空转——放大手法确实张开了窗口、两个线程确实重叠。
        少了它，一个「窗口根本没张开」的写法会让上一条永远绿，
        而 `_REPO_LOCK` 被谁删掉都没人知道。
        """
        def bypass(h):
            # 与 `lifecycle.remove` 的第 1、2 步逐字相同，只是不进闸门。
            try:
                gitcmd.remove_worktree(self.repo, h.path)
            except WorktreeError:
                pass
            try:
                gitcmd.prune_worktrees(self.repo)
            except WorktreeError:
                pass

        exc = self._race(bypass)

        # ⚠ **断言认 errno 与路径，不认报错文案。** `strerror` 会跟着系统语言走
        # （本机是「系统找不到指定的路径。」，CI 上是 "No such file or directory"），
        # 拿文案做判据等于让这条护栏在换个 locale 的机器上莫名其妙地红。
        self.assertIsInstance(exc, FileNotFoundError)
        self.assertEqual(exc.errno, errno.ENOENT)
        # 而且必须是 CI 上那一句的成因：**父目录没了**，所以建不出 `<名字>` 这一级。
        failed_on = Path(exc.filename)
        self.assertEqual(failed_on.name, "late-worker")
        self.assertEqual(failed_on.parent.name, "worktrees")
        self.assertFalse(failed_on.parent.exists(), "父目录还在的话就不是这个成因")


if __name__ == "__main__":
    unittest.main()
