"""
②″保护路径层的判定测试（protected-paths 扩展）。

分两批：本文件前半是**判定层**（`permission/protected.py` 的纯函数），
后半是**升级语义**（`engine.decide` 的收紧器）。

⚠ 本文件里有五条**反证**，它们各自钉住一种「实现写错但正向判据照样通过」的形态：

| 反证 | 改坏什么会让它变红 |
| --- | --- |
| `ExcludeBeatsProtectTest` | 把 `inspect` 里「先查排除」与「先查保护」两步对调 |
| `CwdTest` | 判定基准从 `request.cwd` 退回主项目根（坑 1） |
| `PipelineOrderGuardTest` | 收紧器被写成「②之后③之前」的短路站（allow 规则能消解它） |
| `NoDowngradeTest` | 同上的另一半：短路站会把③的 deny 与④严格档的 DENY 一起吞掉 |
| `UpgradeTest` 里的默认档那条 | 收紧器只处理 ALLOW（默认档那次 `ASK @ MODE` 层没被换掉） |
"""

import os
import shutil
import tempfile
import unittest
from pathlib import Path

from rhinecode.permission import protected
from rhinecode.permission.models import PermissionMode, PermissionRequest


def _request(specifier: str, cwd, *, kind: str = "write_path") -> PermissionRequest:
    """造一个写入类权限请求（第二批用；这里先给判定层的用例共用一个工作目录约定）。"""
    return PermissionRequest(
        tool_name="write_file",
        rule_name="Write",
        specifier=specifier,
        kind=kind,
        is_read_only=False,
        mode=PermissionMode.DEFAULT,
        cwd=Path(cwd),
    )


class _Workspace(unittest.TestCase):
    """给判定用例准备一个真实的临时工作目录（符号链接那条需要真文件系统）。"""

    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="rhine-protected-")).resolve()
        self.addCleanup(shutil.rmtree, self.root, True)


class ScopeTest(_Workspace):
    """AC4：保护范围与排除范围各自覆盖了哪些路径。"""

    # 逐条列出而不是只测一两个代表：这张表就是「保护清单」本身，
    # 将来有人从 `PROTECTED_RELATIVE` 里摘掉一项时，这里会当场红。
    PROTECTED = (
        ".rhinecode/permissions.yaml",
        ".rhinecode/permissions.local.yaml",
        ".rhinecode/hooks.yaml",
        ".rhinecode/mcp.yaml",
        ".rhinecode/agents/reviewer.md",
        ".rhinecode/skills/commit/SKILL.md",
        ".rhinecode/memory/project.md",
        ".rhinecode/worktrees/review/a.py",
        ".rhinecode/whatever-new-config.yaml",  # 黑名单式：将来新增的自动被保护
        ".git/config",
        ".git/hooks/pre-commit",
    )

    # ⚠ 这三项是**刻意排除**的，理由统一是「纯机器副本，改了不影响以后会发生什么」。
    # 它们与 `path_guard._RUNTIME_ARTIFACT_RELATIVE` 取值相同但两张表刻意不合一。
    EXCLUDED = (
        ".rhinecode/sessions/2026-01-01.jsonl",
        ".rhinecode/context/tool-1.txt",
        ".rhinecode/traces/run.jsonl",
    )

    ORDINARY = (
        "rhinecode/agent/loop.py",
        "README.md",
        "docs/extensions/protected-paths/spec.md",
        "tests/test_perm_protected.py",
    )

    def test_protected_paths_are_hit(self) -> None:
        for rel in self.PROTECTED:
            with self.subTest(path=rel):
                self.assertIsNotNone(protected.inspect(rel, self.root))

    def test_runtime_artifacts_are_not_hit(self) -> None:
        for rel in self.EXCLUDED:
            with self.subTest(path=rel):
                self.assertIsNone(protected.inspect(rel, self.root))

    def test_ordinary_paths_are_not_hit(self) -> None:
        for rel in self.ORDINARY:
            with self.subTest(path=rel):
                self.assertIsNone(protected.inspect(rel, self.root))

    def test_reason_says_why_this_file_is_special(self) -> None:
        """
        AC10：原因文本必须说清**这个文件为什么特殊**，不能只说「需要确认」。

        面板上那一行是用户决定放不放行的唯一依据。只写「需要确认」的话，
        用户看到的是一个没有信息量的打断，几次之后就会条件反射地按放行。
        """
        hit = protected.inspect(".rhinecode/hooks.yaml", self.root)
        self.assertIsNotNone(hit)
        self.assertIn("Hook", hit.reason)
        self.assertIn("不经权限管线", hit.reason)

        hit = protected.inspect(".git/hooks/pre-commit", self.root)
        self.assertIn("版本库", hit.reason)

        # 未单列的配置文件走 `.rhinecode` 那条兜底，说法仍要成立。
        hit = protected.inspect(".rhinecode/permissions.yaml", self.root)
        self.assertIn("配置目录", hit.reason)


class PathFormTest(_Workspace):
    """AC5：换一种写法指向同一个文件，结论必须一样。"""

    def test_relative_dot_prefix_and_absolute_all_hit(self) -> None:
        target = self.root / ".rhinecode" / "hooks.yaml"
        for spec in (
            ".rhinecode/hooks.yaml",
            "./.rhinecode/hooks.yaml",
            str(target),
        ):
            with self.subTest(form=spec):
                hit = protected.inspect(spec, self.root)
                self.assertIsNotNone(hit)
                # 解析后的路径必须一致——豁免集合用它做键，
                # 三种写法算出三个不同的键的话，「本会话放行」就只对其中一种生效。
                self.assertEqual(hit.path, target)

    def test_case_variant_hits_on_case_insensitive_filesystems(self) -> None:
        """
        Windows 上 `.RHINECODE\\hooks.yaml` 与 `.rhinecode\\hooks.yaml` 是同一个文件。

        逐字符比较路径的话，一个大小写变体就能静默跳过整层保护，而它写到的
        还是那份真正的配置。POSIX 上它确实是另一个文件，因此那里不该命中。
        """
        hit = protected.inspect(".RHINECODE/hooks.yaml", self.root)
        if os.path.normcase("A") == "a":  # 大小写不敏感的平台（Windows）
            self.assertIsNotNone(hit)
        else:
            self.assertIsNone(hit)

    def test_symlink_into_protected_dir_is_hit(self) -> None:
        """
        **在工作区内建一条指向 `.rhinecode/hooks.yaml` 的软链再写它**同样命中。

        这条依赖 `resolve_in_workspace` 会跟符号链接。若哪天有人把解析换成
        「自己拼 Path」，这条会红——而那正是它存在的理由。
        """
        (self.root / ".rhinecode").mkdir()
        real = self.root / ".rhinecode" / "hooks.yaml"
        real.write_text("- {}\n", encoding="utf-8")
        link = self.root / "innocent.yaml"
        try:
            os.symlink(real, link)
        except (OSError, NotImplementedError) as exc:
            self.skipTest(f"本平台建不了符号链接（Windows 需要开发者模式或管理员）：{exc}")
        hit = protected.inspect("innocent.yaml", self.root)
        self.assertIsNotNone(hit)
        self.assertEqual(hit.path, real.resolve())


class CwdTest(_Workspace):
    """
    AC2 / 坑 1：判定基准必须是**本次调用的工作目录**，不是主项目根。

    ⚠ **判据必须用「同一个绝对目标 + 两个 cwd」构造。**

    写成「同一个相对路径 `.rhinecode/hooks.yaml` 在两个 cwd 下结论相反」是错的：
    以隔离工作区为 cwd 时它指的是 `<工作区>/.rhinecode/hooks.yaml`，落在那个工作区
    自己的保护根内，**照样命中**（spec F14 写明这是期望行为）。用那个写法建护栏，
    在「判定基准退回主项目根」时**照样通过**——坑 1 就漏掉了。

    漏掉的后果：隔离子 Agent 的每一次写入都会命中本层，而它非交互、判 ASK
    自动拒绝，于是隔离委派**全部静默失败**，界面上只看到「子 Agent 什么都没做出来」。
    """

    def setUp(self) -> None:
        super().setUp()
        self.worktree = self.root / ".rhinecode" / "worktrees" / "review"
        self.worktree.mkdir(parents=True)

    def test_same_target_two_verdicts(self) -> None:
        target = self.worktree / "a.py"

        # 主对话去写别人的隔离工作区 → 命中（C14：成果应经分支交付）
        self.assertIsNotNone(protected.inspect(str(target), self.root))

        # 隔离子 Agent 写自己工作区里的业务文件 → 不命中
        self.assertIsNone(protected.inspect("a.py", self.worktree))

    def test_isolated_agent_ordinary_writes_are_never_hit(self) -> None:
        """隔离工作区里没有 `.rhinecode/`，所以它的日常写入一条都命中不了。"""
        for rel in ("a.py", "src/main.py", "docs/readme.md", "tests/test_x.py"):
            with self.subTest(path=rel):
                self.assertIsNone(protected.inspect(rel, self.worktree))

    def test_isolated_agent_writing_its_own_git_is_still_hit(self) -> None:
        """
        它写自己工作区里的 `.git` 仍命中——**期望行为，不作特例**（spec F14）。

        在 git worktree 里 `.git` 是个文件而不是目录，但保护按路径前缀判定，
        因此写它本身照样被拦下。
        """
        self.assertIsNotNone(protected.inspect(".git", self.worktree))


class FailSafeTest(_Workspace):
    """AC3 / N3：判定失败一律偏严，且绝不抛异常。"""

    def test_missing_or_invalid_cwd_is_treated_as_hit(self) -> None:
        for bad in (None, "", "   "):
            with self.subTest(cwd=repr(bad)):
                hit = protected.inspect("a.py", bad)
                self.assertIsNotNone(hit)
                self.assertIn("无法解析", hit.reason)

    def test_parent_reference_is_treated_as_hit(self) -> None:
        """含 `..` 的路径解析会被 path_guard 拒绝 → 按命中处理。"""
        hit = protected.inspect("../outside.py", self.root)
        self.assertIsNotNone(hit)

    def test_unresolvable_path_can_never_match_an_exemption(self) -> None:
        """
        解析失败那一支填的 `path` **不是绝对路径**，因此永远匹配不上豁免集合。

        豁免集合里存的都是解析后的绝对路径。这是安全的方向：解析不了的路径
        豁免不掉。
        """
        hit = protected.inspect("a.py", None)
        self.assertFalse(hit.path.is_absolute())


class ExcludeBeatsProtectTest(_Workspace):
    """
    **反证**：排除必须查在保护之前。

    排除项（`.rhinecode/traces` 等）是保护项（`.rhinecode`）的**真子集**。
    两步顺序对调的话，`.rhinecode/traces/x.jsonl` 会先命中 `.rhinecode` 那条保护根，
    排除**永远不生效**——而正向用例（「保护项命中」）在那种实现下照样全绿。
    """

    def test_excluded_paths_sit_inside_a_protected_root(self) -> None:
        # 先证明前提：排除项确实落在某个保护根内部（否则这条反证就没有分辨力）
        traces = self.root / ".rhinecode" / "traces" / "run.jsonl"
        roots = protected.protected_roots_of(self.root)
        self.assertTrue(
            any(str(traces).startswith(str(r)) for r in roots),
            "排除项本该落在保护根内部，否则本反证测不到顺序",
        )
        # 再断言结论：不命中
        self.assertIsNone(protected.inspect(".rhinecode/traces/run.jsonl", self.root))

    def test_every_excluded_entry_is_a_subset_of_some_protected_entry(self) -> None:
        """遍历常量表：新增排除项时自动被覆盖，不必回来改这条用例。"""
        for parts in protected.EXCLUDED_RELATIVE:
            with self.subTest(excluded=parts):
                self.assertTrue(
                    any(parts[: len(p)] == p for p in protected.PROTECTED_RELATIVE),
                    f"排除项 {parts} 不在任何保护范围内——那它根本不需要被排除",
                )


if __name__ == "__main__":
    unittest.main()
