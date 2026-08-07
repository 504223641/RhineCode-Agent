"""
c14 T14：路径边界判定按调用者的工作目录进行（spec F2 / F5 / N2 / AC2 / AC3 / AC5）。

c14 之前，`path_guard` 把「项目根」写死成进程的当前工作目录，全进程只有一个边界。
现在边界由调用方显式给出——**这是隔离的物理实现**，所以本文件的用例分三组：

1. 同一个路径在不同 root 下得到不同结论（隔离真的生效）；
2. root 缺失/非法时**拒绝**，而不是回退到主项目根（N2，最容易写错的一处）；
3. 只读白名单与 root **正交**（F5，隔离子 Agent 仍能读用户级记忆）。
"""

import unittest
from pathlib import Path

from rhinecode.tools import path_guard
from rhinecode.tools.path_guard import (
    PathGuardError,
    clear_read_roots,
    is_inside,
    is_readable_path,
    is_within_workspace,
    main_project_root,
    register_read_root,
    require_cwd,
    resolve_in_workspace,
    resolve_readable,
    worktrees_dir_of,
)
from tests.worktree_support import cleanup, make_plain_dir


class RootIsPerCallTest(unittest.TestCase):
    """同一个路径，换个 root 就换个结论——这就是隔离。"""

    def setUp(self):
        self.a = make_plain_dir(prefix="c14-roota-")
        self.addCleanup(cleanup, self.a)
        self.b = make_plain_dir(prefix="c14-rootb-")
        self.addCleanup(cleanup, self.b)

    def test_relative_path_resolves_against_given_root(self):
        self.assertEqual(resolve_in_workspace("x.py", self.a), self.a / "x.py")
        self.assertEqual(resolve_in_workspace("x.py", self.b), self.b / "x.py")

    def test_absolute_path_inside_one_root_is_outside_the_other(self):
        """
        AC2 的核心形态：隔离子 Agent 用绝对路径去够主项目根的文件，必须被拒。
        """
        target = str(self.a / "secret.txt")
        self.assertTrue(is_within_workspace(target, self.a))
        self.assertFalse(is_within_workspace(target, self.b))

    def test_resolve_raises_when_outside(self):
        with self.assertRaises(PathGuardError):
            resolve_in_workspace(str(self.a / "x.py"), self.b)

    def test_parent_ref_rejected_under_any_root(self):
        for root in (self.a, self.b):
            self.assertFalse(is_within_workspace("../x", root))
            with self.assertRaises(PathGuardError):
                resolve_in_workspace("../x", root)

    def test_symlink_escaping_root_is_rejected(self):
        """指向 root 之外的符号链接必须被拒（`resolve` 会跟随它）。"""
        outside = self.b / "target.txt"
        outside.write_text("x\n", encoding="utf-8")
        link = self.a / "escape.txt"
        try:
            link.symlink_to(outside)
        except (OSError, NotImplementedError):
            self.skipTest("本平台不支持建立符号链接")
        self.assertFalse(is_within_workspace(str(link), self.a))


class MissingRootIsRejectedTest(unittest.TestCase):
    """
    ⚠ **spec N2 的反证组，本文件最重要的部分。**

    root 缺失或非法时必须**拒绝**。若实现里写成「回退到主项目根」，
    下面每一条都会变成放行——而那正是「一次隔离故障静默变成一次越权」：
    隔离子 Agent 的路径突然按主项目根判定，它就能读写整个项目，
    调用栈上没有任何线索。
    """

    def test_none_root_is_rejected(self):
        self.assertFalse(is_within_workspace("CLAUDE.md", None))
        self.assertFalse(is_readable_path("CLAUDE.md", None))

    def test_empty_root_is_rejected(self):
        for bad in ("", "   "):
            self.assertFalse(is_within_workspace("CLAUDE.md", bad))
            self.assertFalse(is_readable_path("CLAUDE.md", bad))

    def test_resolve_raises_on_missing_root(self):
        with self.assertRaises(PathGuardError):
            resolve_in_workspace("CLAUDE.md", None)
        with self.assertRaises(PathGuardError):
            resolve_readable("CLAUDE.md", "")

    def test_require_cwd_raises_instead_of_falling_back(self):
        """
        `require_cwd` 是各工具取 cwd 的唯一入口。它若回退到主项目根，
        会造成「引擎按隔离工作区批准、工具却写到主项目根」的静默串写。
        """
        with self.assertRaises(PathGuardError):
            require_cwd(None)
        with self.assertRaises(PathGuardError):
            require_cwd("")

    def test_require_cwd_accepts_valid_root(self):
        root = make_plain_dir(prefix="c14-req-")
        self.addCleanup(cleanup, root)
        self.assertEqual(require_cwd(root), root.resolve())
        self.assertEqual(require_cwd(str(root)), root.resolve())

    def test_missing_root_does_not_silently_use_process_cwd(self):
        """
        直白版：一个**确实存在于进程当前工作目录里**的文件，
        在 root 缺失时也必须被拒。若实现回退到主项目根，这条会红。
        """
        self.assertTrue(is_within_workspace("CLAUDE.md", main_project_root()))
        self.assertFalse(is_within_workspace("CLAUDE.md", None))


class ReadWhitelistIsOrthogonalTest(unittest.TestCase):
    """
    F5 / AC5：只读白名单与 root **正交**。

    它表达的是「这些位置在任何工作目录下都允许只读访问」（用户级记忆、Skill 目录），
    因此隔离子 Agent 同样能读；而写类判定完全不受它影响。
    """

    def setUp(self):
        clear_read_roots()
        self.addCleanup(clear_read_roots)
        self.workspace = make_plain_dir(prefix="c14-ws-")
        self.addCleanup(cleanup, self.workspace)
        self.memory = make_plain_dir(prefix="c14-mem-")
        self.addCleanup(cleanup, self.memory)
        self.note = self.memory / "note.md"
        self.note.write_text("note\n", encoding="utf-8")
        register_read_root(self.memory)

    def test_readable_from_any_root(self):
        self.assertTrue(is_readable_path(str(self.note), self.workspace))
        self.assertTrue(is_readable_path(str(self.note), main_project_root()))

    def test_not_writable_from_any_root(self):
        self.assertFalse(is_within_workspace(str(self.note), self.workspace))
        self.assertFalse(is_within_workspace(str(self.note), main_project_root()))

    def test_whitelist_still_rejects_parent_refs(self):
        """白名单不是免检通道：`..` 仍然被拒。"""
        self.assertFalse(
            is_readable_path(str(self.memory / ".." / "x"), self.workspace)
        )

    def test_whitelist_needs_no_root_but_root_must_be_valid(self):
        """
        白名单分支不看 root 的**内容**，但 root 本身仍须有效——
        否则「不传 root 就能读白名单」会成为一条绕过 N2 的旁路。
        """
        self.assertFalse(is_readable_path(str(self.note), None))


class WorktreeHelpersTest(unittest.TestCase):
    """F18 用到的两个小工具。"""

    def test_worktrees_dir_of(self):
        root = Path("/tmp/proj")
        self.assertEqual(
            worktrees_dir_of(root), Path("/tmp/proj/.rhinecode/worktrees")
        )

    def test_is_inside_matches_by_path_not_name(self):
        """
        ⚠ 按**路径**判断，不是按目录名——否则会误伤用户自己叫 `worktrees`
        的业务目录。
        """
        base = make_plain_dir(prefix="c14-base-")
        self.addCleanup(cleanup, base)
        wt = base / ".rhinecode" / "worktrees"
        wt.mkdir(parents=True)
        decoy = base / "src" / "worktrees"
        decoy.mkdir(parents=True)

        self.assertTrue(is_inside(wt / "a" / "x.py", wt))
        self.assertTrue(is_inside(wt, wt))
        self.assertFalse(is_inside(decoy / "x.py", wt))

    def test_is_inside_is_failsafe(self):
        self.assertFalse(is_inside("", "/definitely/not/here"))


class RenameGuardTest(unittest.TestCase):
    """
    `workspace_root` 已改名为 `main_project_root`（c14 D3）。

    改名是**刻意**的：15 个既有调用点里 12 处要主项目根、3 处要调用者的工作目录，
    不改名就没法强制逐个复核，而这两者混淆一次就是隔离失效。
    留这条护栏防止有人为了「兼容」把旧名字加回去。
    """

    def test_old_name_is_gone(self):
        self.assertFalse(
            hasattr(path_guard, "workspace_root"),
            "workspace_root 不应再存在——它的两种语义已被刻意拆开",
        )

    def test_new_name_returns_process_cwd(self):
        self.assertEqual(main_project_root(), Path.cwd().resolve())


if __name__ == "__main__":
    unittest.main()
