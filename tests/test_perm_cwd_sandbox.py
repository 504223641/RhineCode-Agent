"""
c14 T17：权限管线第②层按调用者的工作目录判定（spec F2 / F5 / N1 / N2 / N3）。

**这是隔离的物理实现所在。** 隔离子 Agent 出不去，不是因为它"守规矩"，
而是因为它每一次路径请求都在这一层被以它自己的工作区为界量过。

本文件的用例分四组：

1. 同一个请求换个 cwd 就换个结论；
2. 隔离子 Agent 的三种越界写法（相对上级 / 绝对路径 / 符号链接）全被拒；
3. cwd 缺失时**拒绝**而不是按主项目根放行（N2 的反证）；
4. 管线层序与既有承诺一字未动（N1/N3）。
"""

import unittest
from pathlib import Path

from rhinecode.permission import Decision, PermissionEngine, PermissionMode
from rhinecode.permission.models import Layer, PermissionRequest, Rule
from rhinecode.permission.rules import RuleSet
from rhinecode.tools.path_guard import (
    clear_read_roots,
    main_project_root,
    register_read_root,
)
from tests.worktree_support import cleanup, make_plain_dir


def _engine(rules=None, mode=PermissionMode.DEFAULT) -> PermissionEngine:
    return PermissionEngine(RuleSet(rules or []), mode=mode)


def _req(kind: str, specifier: str, cwd, read_only=None, mode=PermissionMode.DEFAULT):
    """构造一次路径类请求。`read_only` 缺省按 kind 推断。"""
    if read_only is None:
        read_only = kind == "read_path"
    names = {
        "read_path": ("read_file", "Read"),
        "write_path": ("write_file", "Write"),
        "glob": ("glob_files", "Glob"),
    }
    tool_name, rule_name = names[kind]
    return PermissionRequest(
        tool_name=tool_name,
        rule_name=rule_name,
        specifier=specifier,
        kind=kind,
        is_read_only=read_only,
        mode=mode,
        cwd=cwd,
    )


class SandboxFollowsCwdTest(unittest.TestCase):
    """同一个请求，换个 cwd 就换个结论——这就是隔离生效的样子。"""

    def setUp(self):
        self.main = make_plain_dir(prefix="c14-main-")
        self.addCleanup(cleanup, self.main)
        # 隔离工作区就落在主项目根内部，与产品里的位置一致。
        self.wt = self.main / ".rhinecode" / "worktrees" / "w1"
        self.wt.mkdir(parents=True)

    def test_relative_write_allowed_in_both(self):
        """相对路径在各自的根内都合法——隔离不是"什么都不让干"。"""
        eng = _engine(mode=PermissionMode.PERMISSIVE)
        for root in (self.main, self.wt):
            r = eng.decide(_req("write_path", "a.py", root, mode=PermissionMode.PERMISSIVE))
            self.assertIs(r.decision, Decision.ALLOW, f"{root} 下应放行")

    def test_absolute_path_into_main_root_is_denied_for_isolated(self):
        """
        ⚠ **AC2 的核心形态。**

        隔离子 Agent 用绝对路径去够主项目根的文件，必须在第②层被拒。
        这是它"出不去"的物理保证。
        """
        eng = _engine(mode=PermissionMode.PERMISSIVE)
        target = str(self.main / "secret.py")

        allowed = eng.decide(
            _req("write_path", target, self.main, mode=PermissionMode.PERMISSIVE)
        )
        denied = eng.decide(
            _req("write_path", target, self.wt, mode=PermissionMode.PERMISSIVE)
        )

        self.assertIs(allowed.decision, Decision.ALLOW)
        self.assertIs(denied.decision, Decision.DENY)
        self.assertIs(denied.layer, Layer.SANDBOX)

    def test_parent_traversal_is_denied_for_isolated(self):
        """相对上级引用：`../../secret.py` 从工作区跳回主项目根。"""
        eng = _engine(mode=PermissionMode.PERMISSIVE)
        r = eng.decide(_req("write_path", "../../secret.py", self.wt))
        self.assertIs(r.decision, Decision.DENY)
        self.assertIs(r.layer, Layer.SANDBOX)

    def test_symlink_escaping_worktree_is_denied(self):
        """符号链接：在工作区里建一个指向主项目根的链接。"""
        outside = self.main / "outside.py"
        outside.write_text("x\n", encoding="utf-8")
        link = self.wt / "shortcut.py"
        try:
            link.symlink_to(outside)
        except (OSError, NotImplementedError):
            self.skipTest("本平台不支持建立符号链接")

        eng = _engine(mode=PermissionMode.PERMISSIVE)
        r = eng.decide(_req("write_path", str(link), self.wt))
        self.assertIs(r.decision, Decision.DENY)
        self.assertIs(r.layer, Layer.SANDBOX)

    def test_read_is_bounded_too(self):
        """读同样按 cwd 收边界——只有白名单是例外（见下一组）。"""
        eng = _engine()
        target = str(self.main / "config.yaml")
        self.assertIs(
            eng.decide(_req("read_path", target, self.wt)).decision, Decision.DENY
        )

    def test_glob_is_bounded_too(self):
        eng = _engine(mode=PermissionMode.PERMISSIVE)
        r = eng.decide(_req("glob", str(self.main / "**/*.py"), self.wt))
        self.assertIs(r.decision, Decision.DENY)
        self.assertIs(r.layer, Layer.SANDBOX)


class MissingCwdIsDeniedTest(unittest.TestCase):
    """
    ⚠ **spec N2 / AC3 的反证组。**

    cwd 缺失时必须**拒绝**。若实现里写成「回退到主项目根」，下面每条都会变成
    放行——那就是「一次隔离故障静默变成一次越权」。
    """

    def test_none_cwd_denies_write(self):
        eng = _engine(mode=PermissionMode.PERMISSIVE)
        r = eng.decide(_req("write_path", "a.py", None))
        self.assertIs(r.decision, Decision.DENY)
        self.assertIs(r.layer, Layer.SANDBOX)

    def test_none_cwd_denies_read(self):
        r = _engine().decide(_req("read_path", "a.py", None))
        self.assertIs(r.decision, Decision.DENY)

    def test_none_cwd_denies_glob(self):
        r = _engine(mode=PermissionMode.PERMISSIVE).decide(_req("glob", "*.py", None))
        self.assertIs(r.decision, Decision.DENY)

    def test_empty_cwd_denies(self):
        r = _engine(mode=PermissionMode.PERMISSIVE).decide(_req("write_path", "a.py", ""))
        self.assertIs(r.decision, Decision.DENY)

    def test_a_file_that_really_exists_is_still_denied(self):
        """
        直白版：一个**确实存在于主项目根**的文件，cwd 缺失时也必须被拒。
        若实现回退到主项目根，这条会红。
        """
        eng = _engine(mode=PermissionMode.PERMISSIVE)
        self.assertIs(
            eng.decide(_req("read_path", "CLAUDE.md", main_project_root())).decision,
            Decision.ALLOW,
        )
        self.assertIs(
            eng.decide(_req("read_path", "CLAUDE.md", None)).decision, Decision.DENY
        )


class ReadWhitelistUnderIsolationTest(unittest.TestCase):
    """
    F5 / AC5：只读白名单在隔离下**照常生效**，写类面完全不动。

    它表达的是「这些位置在任何工作目录下都允许只读」（用户级记忆、Skill 目录），
    因此隔离子 Agent 同样能读——这与「隔离只收紧不放宽」不矛盾：
    主对话本来就能读它们，子 Agent 没有多拿到任何东西。
    """

    def setUp(self):
        clear_read_roots()
        self.addCleanup(clear_read_roots)
        self.wt = make_plain_dir(prefix="c14-wt-")
        self.addCleanup(cleanup, self.wt)
        self.memory = make_plain_dir(prefix="c14-mem-")
        self.addCleanup(cleanup, self.memory)
        self.note = self.memory / "note.md"
        self.note.write_text("note\n", encoding="utf-8")
        register_read_root(self.memory)

    def test_read_allowed_from_isolated_cwd(self):
        r = _engine().decide(_req("read_path", str(self.note), self.wt))
        self.assertIsNot(r.decision, Decision.DENY)

    def test_write_still_denied_from_isolated_cwd(self):
        eng = _engine(mode=PermissionMode.PERMISSIVE)
        r = eng.decide(_req("write_path", str(self.note), self.wt))
        self.assertIs(r.decision, Decision.DENY)
        self.assertIs(r.layer, Layer.SANDBOX)

    def test_glob_face_untouched(self):
        eng = _engine(mode=PermissionMode.PERMISSIVE)
        r = eng.decide(_req("glob", str(self.memory / "*.md"), self.wt))
        self.assertIs(r.decision, Decision.DENY)


class PipelineUnchangedTest(unittest.TestCase):
    """
    N1 / N3：c14 不新增层、不改层序，既有承诺一字未动。
    """

    def setUp(self):
        self.wt = make_plain_dir(prefix="c14-wt-")
        self.addCleanup(cleanup, self.wt)

    def test_blacklist_still_beats_everything_under_isolation(self):
        """
        ①黑名单对隔离子 Agent 照常生效，且**放行档也翻不过它**。
        隔离没有给子 Agent 任何新的能力。
        """
        eng = _engine([Rule("allow", "Bash", "rm *", "user")], PermissionMode.PERMISSIVE)
        req = PermissionRequest(
            tool_name="run_command",
            rule_name="Bash",
            specifier="rm -rf /",
            kind="command",
            is_read_only=False,
            mode=PermissionMode.PERMISSIVE,
            cwd=self.wt,
        )
        r = eng.decide(req)
        self.assertIs(r.decision, Decision.DENY)
        self.assertIs(r.layer, Layer.BLACKLIST)

    def test_sandbox_beats_allow_rule_under_isolation(self):
        """
        ②沙箱排在③规则之前：一条 `allow Write(*)` 也翻不过越界。
        这是「隔离只收紧不放宽」的直接体现。
        """
        eng = _engine([Rule("allow", "Write", "*", "user")], PermissionMode.PERMISSIVE)
        outside = make_plain_dir(prefix="c14-out-")
        self.addCleanup(cleanup, outside)

        r = eng.decide(_req("write_path", str(outside / "x.py"), self.wt))

        self.assertIs(r.decision, Decision.DENY)
        self.assertIs(r.layer, Layer.SANDBOX)

    def test_deny_rule_still_applies_under_isolation(self):
        """③规则层在隔离下照常求值，deny 仍然优先。"""
        eng = _engine([Rule("deny", "Write", "secret.py", "user")], PermissionMode.PERMISSIVE)
        r = eng.decide(_req("write_path", "secret.py", self.wt))
        self.assertIs(r.decision, Decision.DENY)
        self.assertIs(r.layer, Layer.RULE)

    def test_main_conversation_behaviour_is_unchanged(self):
        """
        N3：主对话传主项目根时，结论与 c14 之前逐字一致。
        """
        eng = _engine()
        root = main_project_root()
        self.assertIs(
            eng.decide(_req("read_path", "CLAUDE.md", root)).decision, Decision.ALLOW
        )
        self.assertIs(
            eng.decide(_req("write_path", "/etc/passwd", root)).decision, Decision.DENY
        )
        self.assertIs(
            eng.decide(_req("write_path", "out.txt", root)).decision, Decision.ASK
        )


if __name__ == "__main__":
    unittest.main()
