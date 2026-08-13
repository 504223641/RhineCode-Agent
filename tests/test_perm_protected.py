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
from rhinecode.permission.engine import PermissionEngine
from rhinecode.permission.models import (
    Decision,
    Layer,
    PermissionMode,
    PermissionRequest,
    Rule,
)
from rhinecode.permission.rules import RuleSet


def _request(
    specifier: str,
    cwd,
    *,
    kind: str = "write_path",
    mode: PermissionMode = PermissionMode.DEFAULT,
    rule_name: str = "Write",
    tool_name: str = "write_file",
    read_only: bool = False,
) -> PermissionRequest:
    """造一个权限请求。缺省是写入类（本层唯一生效的种类）。"""
    return PermissionRequest(
        tool_name=tool_name,
        rule_name=rule_name,
        specifier=specifier,
        kind=kind,
        is_read_only=read_only,
        mode=mode,
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


# ===========================================================================
# 第二批：升级语义（`engine.decide` 的收紧器）
# ===========================================================================


class _EngineCase(_Workspace):
    """给引擎用例准备工作目录与一个构造引擎的小工具。"""

    def engine(
        self,
        *rules: Rule,
        mode: PermissionMode = PermissionMode.DEFAULT,
    ) -> PermissionEngine:
        return PermissionEngine(RuleSet(list(rules)), mode=mode)

    def write(self, specifier: str, *, mode: PermissionMode = PermissionMode.DEFAULT):
        return _request(specifier, self.root, mode=mode)


class PipelineOrderGuardTest(_EngineCase):
    """
    AC6 / N6：**一条宽 allow 规则盖不过本层。**

    ⚠ **这条必须用「宽 allow + 保护路径」构造，不能用「③层没命中」的形态。**

    理由与②′网络边界层那条一字不差（`test_perm_network_layer.py` 的
    `PipelineOrderGuardTests` 里写过一次）：用「③层没命中」构造的话，
    实现即便把收紧器写成「②之后③之前的短路站」，判据也**照样通过**——
    因为那种形态下③层本来就不说话，两种实现结论相同。发现不了顺序错误。

    本用例额外做一件事来堵死「护栏退化」：先用 `_decide_core` 断言
    **不加收紧器时它确实是 ALLOW**。少了这一步，哪天有人把 `.rhinecode/**`
    写成一个匹配不上的模式，这条护栏会静默退化成「③层没命中」的形态而仍然全绿。
    """

    RULE = Rule("allow", "Write", ".rhinecode/**", "user")

    def test_wide_allow_is_really_a_hit_at_layer_three(self) -> None:
        """前提校验：不加收紧器时，那条 allow 真的在③层命中并放行。"""
        engine = self.engine(self.RULE)
        core = engine._decide_core(self.write(".rhinecode/hooks.yaml"))
        self.assertIs(core.decision, Decision.ALLOW)
        self.assertIs(core.layer, Layer.RULE)

    def test_wide_allow_cannot_dissolve_the_protected_layer(self) -> None:
        engine = self.engine(self.RULE)
        result = engine.decide(self.write(".rhinecode/hooks.yaml"))
        self.assertIs(result.decision, Decision.ASK)
        self.assertIs(result.layer, Layer.PROTECTED)

    def test_wide_allow_still_works_for_the_excluded_dirs(self) -> None:
        """对照：同一条 allow 对排除目录仍然照常放行（本层没有波及无关路径）。"""
        engine = self.engine(self.RULE)
        result = engine.decide(self.write(".rhinecode/traces/run.jsonl"))
        self.assertIs(result.decision, Decision.ALLOW)
        self.assertIs(result.layer, Layer.RULE)


class NoDowngradeTest(_EngineCase):
    """
    AC7 / AC8 / N1：**任何 DENY 都不被降级为 ASK。**

    这三条是「收紧器 vs 短路站」的另一半反证。把实现改成「命中保护路径即短路
    返回 ASK」，三条会同时变红——而 `PipelineOrderGuardTest` 在那种实现下仍是绿的，
    所以两组缺一不可。
    """

    def test_layer_three_deny_survives(self) -> None:
        """用户明确写下的禁止，不能被本层降级成「问一下」。"""
        engine = self.engine(Rule("deny", "Write", ".rhinecode/hooks.yaml", "user"))
        result = engine.decide(self.write(".rhinecode/hooks.yaml"))
        self.assertIs(result.decision, Decision.DENY)
        self.assertIs(result.layer, Layer.RULE)

    def test_strict_mode_deny_survives(self) -> None:
        """严格档的「灰色地带一律拒绝」不能被本层降级。"""
        engine = self.engine(mode=PermissionMode.STRICT)
        result = engine.decide(
            self.write(".rhinecode/hooks.yaml", mode=PermissionMode.STRICT)
        )
        self.assertIs(result.decision, Decision.DENY)
        self.assertIs(result.layer, Layer.MODE)

    def test_sandbox_deny_survives(self) -> None:
        """②层路径沙箱的越界拒绝同样原样保留（层仍是 SANDBOX，不是 PROTECTED）。"""
        engine = self.engine()
        result = engine.decide(self.write("../outside/.rhinecode/hooks.yaml"))
        self.assertIs(result.decision, Decision.DENY)
        self.assertIs(result.layer, Layer.SANDBOX)


class UpgradeTest(_EngineCase):
    """AC9：三种「本该放行或本该只是普通确认」的情形都被换成本层的 ASK。"""

    def test_permissive_mode_is_upgraded(self) -> None:
        """本扩展要解决的主场景：放行档下写配置不再直接落盘。"""
        engine = self.engine(mode=PermissionMode.PERMISSIVE)
        result = engine.decide(
            self.write(".rhinecode/hooks.yaml", mode=PermissionMode.PERMISSIVE)
        )
        self.assertIs(result.decision, Decision.ASK)
        self.assertIs(result.layer, Layer.PROTECTED)

    def test_default_mode_ask_has_its_layer_replaced(self) -> None:
        """
        ⚠ **默认档下结论本来就是 ASK，但层必须被换成 PROTECTED。**

        把收紧器写成「只处理 ALLOW」的话，这一支会原样返回 `ASK @ MODE`——
        确认面板据 layer 判断要不要给「永久放行」，于是它照常显示四个选项。
        用户点下去写出一条③层 allow 规则，而下一次那条规则又会被本层升级回 ASK。
        **骗人的按钮原样存在，只是换了个入口。**

        这条是 plan 阶段那处修正的唯一护栏。
        """
        engine = self.engine()
        result = engine.decide(self.write(".rhinecode/hooks.yaml"))
        self.assertIs(result.decision, Decision.ASK)
        self.assertIs(result.layer, Layer.PROTECTED)

    def test_turn_grant_cannot_dissolve_the_protected_layer(self) -> None:
        """Skill 的 `allowed-tools` 预授权（本次执行级 allow）同样盖不过本层。"""
        engine = self.engine()
        engine.grant_turn_rules([Rule("allow", "Write", ".rhinecode/**", "skill")])
        result = engine.decide(self.write(".rhinecode/hooks.yaml"))
        self.assertIs(result.decision, Decision.ASK)
        self.assertIs(result.layer, Layer.PROTECTED)

    def test_edit_tool_is_covered_too(self) -> None:
        """`edit_file` 走的也是 write_path，不能只覆盖 `write_file`。"""
        engine = self.engine(mode=PermissionMode.PERMISSIVE)
        request = _request(
            ".rhinecode/hooks.yaml",
            self.root,
            mode=PermissionMode.PERMISSIVE,
            rule_name="Edit",
            tool_name="edit_file",
        )
        self.assertIs(engine.decide(request).layer, Layer.PROTECTED)


class ExemptTest(_EngineCase):
    """AC12 / AC13：本会话豁免的语义边界。"""

    def test_exempt_stops_the_upgrade_and_is_recorded(self) -> None:
        engine = self.engine(mode=PermissionMode.PERMISSIVE)
        request = self.write(".rhinecode/hooks.yaml", mode=PermissionMode.PERMISSIVE)

        self.assertIs(engine.decide(request).layer, Layer.PROTECTED)

        self.assertTrue(engine.grant_protected_exemption(request))
        result = engine.decide(request)
        self.assertIs(result.decision, Decision.ALLOW)
        self.assertIs(result.layer, Layer.MODE)
        # 不记这个标记的话，时间线上只剩一条 `allow（④模式）`，
        # 读的人会以为用户切到了放行档。
        self.assertTrue(result.protected_exempt)

    def test_exemption_does_not_spread_to_siblings(self) -> None:
        """
        **反证**：豁免精确到单个文件，不扩展到目录。

        写成目录级豁免的话，用户为 `hooks.yaml` 点一次「本会话放行」，
        就等于把整个 `.rhinecode/` 交出去了。
        """
        engine = self.engine(mode=PermissionMode.PERMISSIVE)
        engine.grant_protected_exemption(
            self.write(".rhinecode/hooks.yaml", mode=PermissionMode.PERMISSIVE)
        )
        other = self.write(".rhinecode/mcp.yaml", mode=PermissionMode.PERMISSIVE)
        self.assertIs(engine.decide(other).layer, Layer.PROTECTED)

    def test_exemption_does_not_dissolve_a_deny_rule(self) -> None:
        """豁免**只解除本层的升级**，其余各层照常——deny 规则仍然拦得住。"""
        engine = self.engine(Rule("deny", "Write", ".rhinecode/hooks.yaml", "user"))
        request = self.write(".rhinecode/hooks.yaml")
        engine.grant_protected_exemption(request)
        result = engine.decide(request)
        self.assertIs(result.decision, Decision.DENY)
        self.assertIs(result.layer, Layer.RULE)

    def test_granting_on_a_non_protected_path_is_a_no_op(self) -> None:
        engine = self.engine()
        self.assertFalse(engine.grant_protected_exemption(self.write("src/main.py")))
        self.assertEqual(engine.protected_exemptions, set())

    def test_exemption_key_is_the_resolved_path(self) -> None:
        """
        三种写法指向同一个文件时，豁免必须都认。

        豁免键取的是解析后的绝对路径，所以为相对写法登记一次之后，
        模型改用绝对路径写同一个文件也不该再被拦。
        """
        engine = self.engine(mode=PermissionMode.PERMISSIVE)
        engine.grant_protected_exemption(
            self.write(".rhinecode/hooks.yaml", mode=PermissionMode.PERMISSIVE)
        )
        absolute = _request(
            str(self.root / ".rhinecode" / "hooks.yaml"),
            self.root,
            mode=PermissionMode.PERMISSIVE,
        )
        self.assertTrue(engine.decide(absolute).protected_exempt)

    def test_is_protected_exempt_reads_the_same_set(self) -> None:
        engine = self.engine()
        target = (self.root / ".rhinecode" / "hooks.yaml").resolve()
        self.assertFalse(engine.is_protected_exempt(target))
        engine.grant_protected_exemption(self.write(".rhinecode/hooks.yaml"))
        self.assertTrue(engine.is_protected_exempt(target))


class DeriveTest(_EngineCase):
    """
    AC14：派生给子 Agent 的引擎视图**共享同一个豁免集合**。

    与 `session_rules` 同口径：用户在确认面板上对某个文件的明确授予理应对子 Agent
    也生效。不共享的话会出现「主对话能写、子 Agent 写不了」，而两边配置看起来
    一模一样——那类不一致极难解释。

    ⚠ 与 `load_errors` 是同一个坑：构造函数会给派生实例新建一个空 `set()`，
    `derive` 里不显式赋值的话共享**静默地不成立**。
    """

    def test_derived_engine_sees_the_main_exemption(self) -> None:
        engine = self.engine(mode=PermissionMode.PERMISSIVE)
        request = self.write(".rhinecode/hooks.yaml", mode=PermissionMode.PERMISSIVE)
        engine.grant_protected_exemption(request)

        derived = engine.derive(PermissionMode.PERMISSIVE)
        self.assertTrue(derived.decide(request).protected_exempt)

    def test_the_set_is_the_same_object_in_both_directions(self) -> None:
        engine = self.engine()
        derived = engine.derive(PermissionMode.DEFAULT)
        self.assertIs(derived.protected_exemptions, engine.protected_exemptions)

    def test_derived_engine_still_upgrades_without_an_exemption(self) -> None:
        """反向对照：没有豁免时子 Agent 照样被拦（它非交互，判 ASK 即自动拒绝）。"""
        engine = self.engine(mode=PermissionMode.PERMISSIVE)
        derived = engine.derive(PermissionMode.PERMISSIVE)
        result = derived.decide(
            self.write(".rhinecode/hooks.yaml", mode=PermissionMode.PERMISSIVE)
        )
        self.assertIs(result.layer, Layer.PROTECTED)


class IsolatedAgentEngineTest(_EngineCase):
    """
    坑 1 在**引擎层**的护栏——`CwdTest` 只覆盖到纯函数，覆盖不到这条接线。

    ⚠ **这个类是实现期做变异测试才补上的。** 当时把 `_apply_protected` 里的
    `request.cwd` 换成主项目根，红的是三条**豁免**用例（它们靠「豁免键算出来对不上」
    偶然抓到），`CwdTest` 全绿。也就是说：改一下豁免实现，坑 1 在引擎层就一条护栏
    都没有了。

    而坑 1 的**真实形态**比那个变异更隐蔽：路径按 `cwd` 正确解析，但拿去与
    **主项目根**拼出的保护根比较。那时 `<主根>/.rhinecode/worktrees/w/a.py` 落在
    `<主根>/.rhinecode` 内——**隔离子 Agent 的每一次写入都命中本层**，而它非交互、
    判 ASK 自动拒绝，于是隔离委派全部静默失败，界面上只看到「子 Agent 什么都没
    做出来」。下面第一条用例专治这个形态。
    """

    def setUp(self) -> None:
        super().setUp()
        self.worktree = self.root / ".rhinecode" / "worktrees" / "review"
        self.worktree.mkdir(parents=True)

    def test_isolated_agent_ordinary_writes_are_not_upgraded(self) -> None:
        engine = self.engine(mode=PermissionMode.PERMISSIVE)
        for rel in ("a.py", "src/main.py", "docs/readme.md"):
            with self.subTest(path=rel):
                result = engine.decide(
                    _request(rel, self.worktree, mode=PermissionMode.PERMISSIVE)
                )
                self.assertIs(result.decision, Decision.ALLOW)
                self.assertIsNot(result.layer, Layer.PROTECTED)

    def test_the_layer_is_asked_about_the_callers_cwd(self) -> None:
        """
        **结构判据**：引擎交给判定层的必须是 `request.cwd`，不是主项目根。

        ⚠ **这条刻意用结构判据而不是行为判据，理由是实测出来的。**

        坑 1 的行为症状（隔离子 Agent 的写入被误拦）只在「工作区确实坐落在
        主项目根的 `.rhinecode/` 之下」时才出现。而单测用的是系统临时目录，
        它不在真实仓库根内——于是把 `request.cwd` 换成 `main_project_root()`
        之后，本类下面那两条**行为**用例照样全绿（实现期变异测试实测）。

        也就是说：这条接线的行为判据在单测环境里**天生没有分辨力**。
        直接断言「问的是谁的工作目录」才咬得住。
        """
        seen: list[tuple[str, object]] = []
        original = protected.inspect

        def spy(specifier, cwd):
            seen.append((specifier, cwd))
            return original(specifier, cwd)

        engine = self.engine(mode=PermissionMode.PERMISSIVE)
        protected.inspect = spy
        try:
            engine.decide(_request("a.py", self.worktree, mode=PermissionMode.PERMISSIVE))
        finally:
            protected.inspect = original

        self.assertEqual(seen, [("a.py", self.worktree)])

    def test_main_conversation_writing_into_a_worktree_is_upgraded(self) -> None:
        """
        镜像对照：**同一个绝对目标**，主对话去写就要过人眼。

        两条合起来才构成「结论取决于谁在写」这个判据；只留任一条都可以被
        「本层整个不生效」或「本层无条件生效」蒙混过去。
        """
        engine = self.engine(mode=PermissionMode.PERMISSIVE)
        result = engine.decide(
            _request(
                str(self.worktree / "a.py"), self.root, mode=PermissionMode.PERMISSIVE
            )
        )
        self.assertIs(result.decision, Decision.ASK)
        self.assertIs(result.layer, Layer.PROTECTED)


class OtherKindsUnchangedTest(_EngineCase):
    """
    AC1 / N4：**其余种类的请求逐字不变。**

    判据是「`decide` 与 `_decide_core` 的结果完全相等」——比逐条断言具体结论强，
    因为它不依赖我对既有行为的记忆，直接拿实现自己当基准。

    ⚠ 每个用例的 specifier 都刻意指向**保护路径**：若收紧器漏判了 kind，
    这些请求就会被误升级，判据当场红。用普通路径构造的话测不到这一点。
    """

    def _same(self, request: PermissionRequest, engine: PermissionEngine) -> None:
        self.assertEqual(engine.decide(request), engine._decide_core(request))

    def test_command_kind_untouched(self) -> None:
        engine = self.engine(mode=PermissionMode.PERMISSIVE)
        self._same(
            _request(
                "cat .rhinecode/hooks.yaml",
                self.root,
                kind="command",
                mode=PermissionMode.PERMISSIVE,
                rule_name="Bash",
                tool_name="run_command",
            ),
            engine,
        )

    def test_read_kind_untouched(self) -> None:
        engine = self.engine()
        self._same(
            _request(
                ".rhinecode/hooks.yaml",
                self.root,
                kind="read_path",
                rule_name="Read",
                tool_name="read_file",
                read_only=True,
            ),
            engine,
        )

    def test_glob_kind_untouched(self) -> None:
        engine = self.engine()
        self._same(
            _request(
                ".rhinecode/**",
                self.root,
                kind="glob",
                rule_name="Read",
                tool_name="glob_files",
                read_only=True,
            ),
            engine,
        )

    def test_url_kind_untouched(self) -> None:
        engine = self.engine(mode=PermissionMode.PERMISSIVE)
        self._same(
            _request(
                "https://example.com/.rhinecode/hooks.yaml",
                self.root,
                kind="url",
                mode=PermissionMode.PERMISSIVE,
                rule_name="WebFetch",
                tool_name="web_fetch",
            ),
            engine,
        )

    def test_other_kind_untouched(self) -> None:
        engine = self.engine(mode=PermissionMode.PERMISSIVE)
        self._same(
            _request(
                "",
                self.root,
                kind="other",
                mode=PermissionMode.PERMISSIVE,
                rule_name="send_message",
                tool_name="send_message",
            ),
            engine,
        )

    def test_ordinary_write_untouched(self) -> None:
        """工作区内的普通写入：本层不该给日常改代码多加一次面板。"""
        engine = self.engine(mode=PermissionMode.PERMISSIVE)
        self._same(self.write("src/main.py", mode=PermissionMode.PERMISSIVE), engine)


class VerdictFieldsTest(_EngineCase):
    """
    `_apply_protected` 是 `DecisionResult` 的**第二个构造出口**，
    `_decide_core` 里的 `_verdict` 闭包管不到它——kind/host 必须显式填。

    本层当前只对 write_path 生效、host 恒为空，因此这条现在测不出用户可见差别。
    留着是因为那条不变量的价值恰恰在于「不留特例」（CLAUDE.md 成对维护点）：
    将来有人扩大本层的适用种类时，漏填会在这里当场红，而不是等到真机弹面板才发现。
    """

    def test_upgraded_result_carries_kind(self) -> None:
        engine = self.engine(mode=PermissionMode.PERMISSIVE)
        request = self.write(".rhinecode/hooks.yaml", mode=PermissionMode.PERMISSIVE)
        result = engine.decide(request)
        self.assertEqual(result.kind, request.kind)
        self.assertEqual(result.host, request.host)

    def test_exempt_result_keeps_the_original_fields(self) -> None:
        engine = self.engine(mode=PermissionMode.PERMISSIVE)
        request = self.write(".rhinecode/hooks.yaml", mode=PermissionMode.PERMISSIVE)
        engine.grant_protected_exemption(request)
        core = engine._decide_core(request)
        result = engine.decide(request)
        self.assertEqual(
            (result.decision, result.layer, result.reason, result.kind, result.host),
            (core.decision, core.layer, core.reason, core.kind, core.host),
        )


if __name__ == "__main__":
    unittest.main()
