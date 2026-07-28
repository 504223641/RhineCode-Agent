"""
SkillManager 编排单测（c11 T27/T29）。

覆盖 spec AC11（激活列表与注入）、AC14/AC17（工具策略四分支）、
AC27（热更新与「下次启动会失败」告知）、AC32（幂等激活）、AC38（零状态项目提示），
以及 plan A1 的**加锁不变量**——这一章唯一的确定性死锁风险，
它的自动化护栏就在本文件里。
"""

import tempfile
import threading
import unittest
from pathlib import Path

from rhinecode.skills import discovery as discovery_module
from rhinecode.skills.manager import SkillManager
from rhinecode.skills.models import (
    ActivationStatus,
    DegradeKind,
    LOAD_SKILL_TOOL,
    SkillSource,
    builtin_skills_dir,
)
from rhinecode.skills.discovery import discover

ALL_TOOLS = frozenset(
    {"read_file", "write_file", "run_command", "glob_files", LOAD_SKILL_TOOL}
)
KNOWN = ALL_TOOLS | {"ask_user", "present_plan"}


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _skill_text(name: str, description: str = "说明", body: str = None, **extra) -> str:
    """
    造一份 Skill 文本。

    ⚠️ 对齐改造后**命令名来自文件路径**，frontmatter 的 `name` 只做显示。
    各用例一律把文件命名成 `<name>.md`，两者保持一致，断言才好读。
    """
    lines = [f"name: {name}", f"description: {description}"]
    lines.extend(f"{k}: {v}" for k, v in extra.items())
    return "---\n" + "\n".join(lines) + "\n---\n" + (body or f"{name} 的 SOP\n")


class ManagerTestBase(unittest.TestCase):
    """提供一个带临时用户级目录的 SkillManager。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.project_root = root / "proj"
        self.user_dir = root / "user"
        self.user_skills = self.user_dir / "skills"
        self.short_commands: set[str] = set()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _manager(self, **kwargs) -> SkillManager:
        m = SkillManager(
            project_root=self.project_root,
            user_dir=self.user_dir,
            builtin_dir=None,
            has_short_command=lambda n: n in self.short_commands,
            **kwargs,
        )
        m.startup()
        return m


class EmptyManagerTest(unittest.TestCase):
    """Null Object：不扫盘、各查询方法都能安全调用。"""

    def test_empty_does_not_touch_filesystem(self) -> None:
        """
        `empty()` 不得调 discover。

        它是协调层的缺省注入对象；若它扫盘，构造 ConversationManager 就会
        隐式读用户主目录，给既有整套测试引入 IO（违背 N1）。
        """
        calls = []
        original = discovery_module.discover
        discovery_module.discover = lambda *a, **k: calls.append(a) or original(*a, **k)
        try:
            m = SkillManager.empty()
        finally:
            discovery_module.discover = original
        self.assertEqual(calls, [])
        self.assertIsNone(m.get("x"))
        self.assertEqual(m.command_infos(), ())
        self.assertIsNone(m.status_segment())
        self.assertEqual(m.index_text(), "")
        self.assertEqual(m.active_text(), "")
        self.assertIsNone(m.project_skill_notice())


class ActivateTest(ManagerTestBase):
    """激活三态与幂等（AC11/AC32）。"""

    def test_activate_shared(self) -> None:
        _write(self.user_skills / "a.md", _skill_text("a"))
        m = self._manager()
        result = m.activate("a", "参数X")
        self.assertIs(result.status, ActivationStatus.ACTIVATED)
        self.assertIn("a 的 SOP", m.active_text())
        self.assertEqual(m.status_segment(), "Skill:1")

    def test_not_found_returns_available_names(self) -> None:
        """名字不存在 → 附可用名字列表，让模型下一轮能自我纠正而不是反复猜。"""
        _write(self.user_skills / "alpha.md", _skill_text("alpha"))
        m = self._manager()
        result = m.activate("alfa", "")
        self.assertIs(result.status, ActivationStatus.NOT_FOUND)
        self.assertEqual(result.available_names, ("alpha",))

    def test_isolated_hint_when_short_command_available(self) -> None:
        _write(self.user_skills / "rev.md", _skill_text("rev", context="fork"))
        self.short_commands.add("rev")
        m = self._manager()
        result = m.activate("rev", "")
        self.assertIs(result.status, ActivationStatus.FORKED)
        self.assertEqual(result.entry_hint, "/rev")

    def test_isolated_hint_falls_back_when_no_short_command(self) -> None:
        """短命令因重名未注册时，提示必须指向 /skills run，绝不能指向不存在的命令。"""
        _write(self.user_skills / "rev.md", _skill_text("rev", context="fork"))
        m = self._manager()
        result = m.activate("rev", "")
        self.assertEqual(result.entry_hint, "/skills run rev")

    def test_idempotent_activation_keeps_position_updates_arguments(self) -> None:
        """连续两次激活 → 一条记录、位置为首次、参数为第二次（AC32/F30）。"""
        _write(self.user_skills / "a.md", _skill_text("a"))
        _write(self.user_skills / "b.md", _skill_text("b"))
        m = self._manager()
        m.activate("a", "第一次")
        m.activate("b", "")
        m.activate("a", "第二次")

        self.assertEqual(m.status_segment(), "Skill:2")
        text = m.active_text()
        # 位置不动：a 仍排在 b 前面。
        self.assertLess(text.index("Skill 指令开始：a"), text.index("Skill 指令开始：b"))
        # 参数更新为第二次。
        self.assertIn("第二次", text)
        self.assertNotIn("第一次", text)

    def test_deactivate_single_and_all(self) -> None:
        _write(self.user_skills / "a.md", _skill_text("a"))
        _write(self.user_skills / "b.md", _skill_text("b"))
        m = self._manager()
        m.activate("a", "")
        m.activate("b", "")

        self.assertIn("已卸载", m.deactivate("a"))
        self.assertEqual(m.status_segment(), "Skill:1")
        self.assertIn("未激活", m.deactivate("a"))
        self.assertIn("全部", m.deactivate(None))
        self.assertIsNone(m.status_segment())
        self.assertIn("没有已激活", m.deactivate(None))

    def test_clear_active(self) -> None:
        _write(self.user_skills / "a.md", _skill_text("a"))
        m = self._manager()
        m.activate("a", "")
        m.clear_active()
        self.assertIsNone(m.status_segment())
        self.assertEqual(m.active_text(), "")

    def test_notify_callback_failure_does_not_break_activation(self) -> None:
        """回调抛异常 → 激活仍算成功（激活本身已完成，不该报告成失败）。"""
        _write(self.user_skills / "a.md", _skill_text("a"))

        def boom() -> None:
            raise RuntimeError("界面正在退出")

        m = self._manager(notify_activation=boom)
        self.assertIs(m.activate("a", "").status, ActivationStatus.ACTIVATED)
        self.assertEqual(m.status_segment(), "Skill:1")


class LockInvariantTest(ManagerTestBase):
    """
    加锁不变量回归（plan A1）——本章唯一确定性死锁的自动化护栏。

    死锁链路：activate() 持锁 → notify_activation → Textual 的 call_from_thread
    （阻塞）→ 主线程 _refresh_status → status_segment() 申请同一把锁 → 双向死等。
    后果是整个 TUI 冻结、连 Esc 都不响应，而触发条件只是模型成功调一次 load_skill。
    """

    def _probe_from_other_thread(self, fn) -> tuple[bool, int]:
        """
        造一个回调桩：桩内**另起一个 daemon 线程**去读 Skill 状态，并带超时等它。

        :returns: `(状态字典, 回调桩)`。状态字典有两个计数：
                  `calls`（桩被调用次数）与 `done`（子线程在超时内跑完的次数）。

        **用计数而不是布尔标志**：布尔标志会被后续一次成功的调用覆盖——
        若实现里存在「锁内调一次、锁外又调一次」的情形，第一次被阻塞的事实
        会被第二次成功悄悄抹掉，护栏静默失效。计数则要求**每一次**调用都畅通。

        **为什么必须跨线程、不能在桩自己线程内直接调**：
        若同线程调用，一旦将来有人把 `Lock` 换成 `RLock`，同线程重入会被放行、
        测试照样绿；而生产环境的死锁是**跨线程**的（activate 在 Worker 线程持锁，
        call_from_thread 让主线程去读同一份状态），RLock 对跨线程毫无帮助——
        死锁依旧。跨线程版本对任何锁实现都成立，且不必去断言 `_lock` 的具体类型
        （那是实现细节，将来换成队列或别的原语时断言就失效了）。

        **子线程必须 daemon=True 且用 join(timeout=...)**：
        真出死锁时不能把整个测试进程一起挂死。
        """
        state = {"done": 0, "calls": 0}

        def probe() -> None:
            # 用 try/finally 而不是「调完再计数」：fn() 抛异常时也要计数，
            # 否则会把一个普通异常误报成死锁。真死锁时执行卡在 fn() 里面，
            # 根本到不了 finally，语义仍然正确。
            try:
                fn()
            finally:
                state["done"] += 1

        def stub() -> None:
            state["calls"] += 1
            t = threading.Thread(target=probe, daemon=True)
            t.start()
            t.join(timeout=2)

        return state, stub

    def test_notify_activation_happens_outside_the_lock(self) -> None:
        _write(self.user_skills / "a.md", _skill_text("a"))
        m_holder: dict = {}
        state, stub = self._probe_from_other_thread(
            lambda: m_holder["m"].status_segment()
        )
        m = self._manager(notify_activation=stub)
        m_holder["m"] = m

        m.activate("a", "")

        # ① 桩确实被调用过。否则走了 NOT_FOUND / ISOLATED 早返回时断言一次都
        #    不执行，这条护栏会**空过**——看起来是绿的，实际什么都没验。
        self.assertGreaterEqual(state["calls"], 1, "notify_activation 桩根本没被调用")
        # ② **每一次**调用的子线程都在超时内完成 → 证明回调发生时锁已释放。
        self.assertEqual(
            state["done"], state["calls"],
            "回调期间读状态被阻塞：notify_activation 在锁内触发了",
        )

    def test_has_short_command_is_called_outside_the_lock(self) -> None:
        """
        `has_short_command` 是跨层回调（调进 CommandRegistry），同样不得在锁内。

        用同样的跨线程探针：回调发生时若锁还被持有，子线程读状态就会超时。
        """
        _write(self.user_skills / "rev.md", _skill_text("rev", context="fork"))
        m_holder: dict = {}
        state, stub_body = self._probe_from_other_thread(
            lambda: m_holder["m"].status_segment()
        )

        def has_cmd(_name: str) -> bool:
            stub_body()
            return False

        m = SkillManager(
            project_root=self.project_root,
            user_dir=self.user_dir,
            builtin_dir=None,
            has_short_command=has_cmd,
        )
        m.startup()
        m_holder["m"] = m

        m.activate("rev", "")
        self.assertGreaterEqual(state["calls"], 1, "has_short_command 桩根本没被调用")
        self.assertEqual(
            state["done"], state["calls"], "has_short_command 在锁内被调用了"
        )

    def test_concurrent_activation_is_idempotent(self) -> None:
        """
        并发激活同一 Skill 只留一条记录。

        这正是需要锁的原因：load_skill 是只读工具、落进并发桶，模型同一轮
        发两次调用时两个线程会并发跑「查在不在 → 在则改、不在则 append」，
        GIL 保证不了这个复合操作的原子性。
        """
        _write(self.user_skills / "a.md", _skill_text("a"))
        m = self._manager()

        barrier = threading.Barrier(8)

        def worker(i: int) -> None:
            barrier.wait()
            m.activate("a", f"参数{i}")

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5)

        self.assertEqual(m.status_segment(), "Skill:1")


class ReloadTest(ManagerTestBase):
    """热更新（AC27/F26/F27）。"""

    def test_added_and_removed(self) -> None:
        _write(self.user_skills / "a.md", _skill_text("a"))
        m = self._manager()
        _write(self.user_skills / "b.md", _skill_text("b"))
        (self.user_skills / "a.md").unlink()

        outcome = m.reload()
        self.assertEqual(outcome.added, ("b",))
        self.assertEqual(outcome.removed, ("a",))

    def test_removed_active_skill_auto_deactivated(self) -> None:
        """已激活但文件被删 → 自动卸载并报告（F27）。"""
        _write(self.user_skills / "a.md", _skill_text("a"))
        m = self._manager()
        m.activate("a", "")
        (self.user_skills / "a.md").unlink()

        outcome = m.reload()
        self.assertEqual(outcome.auto_deactivated, ("a",))
        self.assertIsNone(m.status_segment())

    def test_body_update_takes_effect_next_render(self) -> None:
        """
        改正文后 reload → 下一次 active_text() 自动是新正文。

        这正是「ActiveSkill 不存正文、每轮从 catalog 现取」的收益：
        无需任何同步逻辑。
        """
        _write(self.user_skills / "a.md", _skill_text("a", body="旧正文\n"))
        m = self._manager()
        m.activate("a", "")
        self.assertIn("旧正文", m.active_text())

        _write(self.user_skills / "a.md", _skill_text("a", body="新正文\n"))
        m.reload()
        text = m.active_text()
        self.assertIn("新正文", text)
        self.assertNotIn("旧正文", text)

    def test_report_lists_source_mode_and_activation(self) -> None:
        _write(self.user_skills / "a.md", _skill_text("a", "甲说明"))
        _write(self.project_root / ".rhinecode" / "skills" / "p.md",
               _skill_text("p", "乙说明", context="fork"))
        m = self._manager()
        m.activate("a", "")

        report = m.report()
        self.assertIn("a", report)
        self.assertIn("甲说明", report)
        self.assertIn("用户级", report)
        self.assertIn("已激活", report)
        self.assertIn("项目级", report)
        self.assertIn("子对话", report)

    def test_report_shows_load_errors_and_warnings(self) -> None:
        _write(self.user_skills / "bad.md", "---\nname: [坏\n---\n正文\n")
        _write(self.user_skills / "w.md",
               _skill_text("w", model="m"))
        m = self._manager()
        report = m.report()
        self.assertIn("加载失败", report)
        self.assertIn("字段提示", report)

    def test_prompt_report_has_three_sections(self) -> None:
        _write(self.user_skills / "a.md",
               _skill_text("a", allowed_tools="[read_file]"))
        m = self._manager()
        m.activate("a", "")
        report = m.prompt_report(ALL_TOOLS)
        self.assertIn("第一阶段清单", report)
        self.assertIn("已激活 Skill 正文", report)
        self.assertIn("当前可见工具集", report)
        self.assertIn("read_file", report)

    def test_prompt_report_when_nothing_active(self) -> None:
        m = self._manager()
        report = m.prompt_report(ALL_TOOLS)
        self.assertIn("Skill 不再收窄工具集", report)

    def test_prompt_report_lists_only_tools_actually_injected(self) -> None:
        """
        可见工具集只列**真的会发给模型**的工具（AC26：所见即实际注入）。

        `policy.exempt` 里除 `load_skill` 外还有 `ask_user` / `present_plan`，
        但这两个不在注册中心里——它们由 `_schema_for` 在过滤之后经
        `plan_schemas()` 单独拼接，且只在 Plan Mode 的规划阶段拼。
        直接把 exempt 全列出来会在普通模式下多报两个模型压根看不到的工具，
        让这份「排查为什么模型没按 Skill 做」的报告本身变成误导来源。
        """
        _write(self.user_skills / "a.md",
               _skill_text("a", allowed_tools="[read_file]"))
        m = self._manager()
        m.activate("a", "")

        # 只看工具清单那一行：该段落还有一句说明 Plan Mode 附加工具的注脚，
        # 那句里天然含 ask_user / present_plan 字样，混进来会让断言失去意义。
        section = m.prompt_report(ALL_TOOLS).split("【当前可见工具集】", 1)[1]
        listed = next(line for line in section.splitlines() if line.strip())

        self.assertIn("read_file", listed)
        self.assertIn(LOAD_SKILL_TOOL, listed)
        self.assertNotIn("ask_user", listed)
        self.assertNotIn("present_plan", listed)
        # 未声明的普通工具当然也不该出现。
        self.assertIn("write_file", listed)  # 不再收窄：全部工具都可见

    def test_degrade_shows_in_report_with_distinct_wording(self) -> None:
        """两种降级在报告里措辞不同（AC13）。"""
        from rhinecode.skills.models import BODY_MAX_LINES

        big = "\n".join(f"行{i}" for i in range(BODY_MAX_LINES + 100))
        _write(self.user_skills / "t.md", _skill_text("t", body=big))
        m = self._manager()
        m.activate("t", "")
        m.active_text()  # 触发降级计算
        self.assertIn("截断", m.report())

    def test_degrade_of_deactivated_skill_leaves_no_ghost(self) -> None:
        """卸载后再渲染 → _degrades 不留幽灵键。"""
        from rhinecode.skills.models import BODY_MAX_LINES

        big = "\n".join(f"行{i}" for i in range(BODY_MAX_LINES + 100))
        _write(self.user_skills / "t.md", _skill_text("t", body=big))
        _write(self.user_skills / "s.md", _skill_text("s"))
        m = self._manager()
        m.activate("t", "")
        m.activate("s", "")
        m.active_text()
        m.deactivate("t")
        m.active_text()
        self.assertNotIn("截断", m.report())


class ProjectNoticeTest(ManagerTestBase):
    """项目级 Skill 提示的零状态语义（AC38/N8）。"""

    def test_notice_contains_names_and_location(self) -> None:
        _write(self.project_root / ".rhinecode" / "skills" / "p.md", _skill_text("p"))
        m = self._manager()
        notice = m.project_skill_notice()
        self.assertIsNotNone(notice)
        self.assertIn("p", notice)
        self.assertIn("skills", notice)

    def test_notice_is_stateless_and_repeats(self) -> None:
        """
        连续调用两次都非空——**不做「只提示一次」的持久化**（AC38）。

        项目级 Skill 来自代码仓库，git pull 后可能凭空多出几个。
        若做了「已确认过就不再提示」的状态，新增时状态不失效，新来的就被静默吞掉。
        """
        _write(self.project_root / ".rhinecode" / "skills" / "p.md", _skill_text("p"))
        m = self._manager()
        self.assertIsNotNone(m.project_skill_notice())
        self.assertIsNotNone(m.project_skill_notice())

    def test_no_project_skill_means_no_notice(self) -> None:
        _write(self.user_skills / "u.md", _skill_text("u"))
        m = self._manager()
        self.assertIsNone(m.project_skill_notice())


class BuiltinSamplesTest(unittest.TestCase):
    """三个内置样板开箱可见（AC28）。"""

    def test_three_builtin_skills_discovered(self) -> None:
        catalog = discover(None, None, builtin_skills_dir())
        names = sorted(s.command_name for s in catalog.skills)
        self.assertEqual(names, ["commit", "review", "test"])
        for spec in catalog.skills:
            self.assertIs(spec.source, SkillSource.BUILTIN)
        self.assertEqual(catalog.errors, ())

    def test_builtin_modes_cover_both_paths(self) -> None:
        """样板要同时覆盖共享与独立两条执行路径（F28）。"""
        catalog = discover(None, None, builtin_skills_dir())
        modes = {s.command_name: s.forked for s in catalog.skills}
        self.assertIs(modes["commit"], False)
        self.assertIs(modes["review"], True)
        self.assertIs(modes["test"], False)


if __name__ == "__main__":
    unittest.main()


class LoadSkillToolTest(ManagerTestBase):
    """
    load_skill 工具的三态输出与权限语义（c11 T29，AC7/AC8）。

    放在本文件而不是单独一个测试文件：它是 SkillManager 的薄封装，
    验证它就是在验证 manager 的对外契约。
    """

    def _tool(self, manager=None):
        from rhinecode.tools.load_skill import LoadSkillTool

        return LoadSkillTool(manager or self._manager())

    def test_read_only_is_true(self) -> None:
        """
        read_only=True 是本工具免确认的唯一前提，也是它进并发桶、
        进而要求 SkillManager 加锁的原因。改这个标志前必须同时评估两处。
        """
        self.assertTrue(self._tool().read_only)

    def test_permission_engine_allows_without_asking(self) -> None:
        """
        用**真实**权限引擎在 DEFAULT 模式下判定 → ALLOW 而非 ASK（AC8）。

        这是 read_only 不被误改的回归护栏：若有人改成 False，
        这条会立刻变红并指出后果（每加载一个 Skill 都要用户按一次确认）。
        """
        from rhinecode.permission.adapter import to_request
        from rhinecode.permission.engine import PermissionEngine
        from rhinecode.permission.models import Decision, PermissionMode
        from rhinecode.permission.rules import RuleSet

        tool = self._tool()
        engine = PermissionEngine(RuleSet([]), mode=PermissionMode.DEFAULT)
        request = to_request(tool, {"name": "a"}, PermissionMode.DEFAULT)
        self.assertIs(engine.decide(request).decision, Decision.ALLOW)

    def test_activated_returns_short_confirmation_not_the_body(self) -> None:
        """
        成功时只回一句简短确认，**不回灌正文**（AC7）。

        正文已由系统提示的槽位注入且每轮都在；工具结果里再来一份是纯浪费，
        而且工具结果会随上下文压缩被挪走，系统提示槽位才是持久的那份。
        """
        _write(self.user_skills / "a.md",
               _skill_text("a", body="这是绝不该出现在工具结果里的正文XYZ\n"))
        m = self._manager()
        result = self._tool(m).execute({"name": "a", "arguments": "p"})
        self.assertTrue(result.ok)
        self.assertIn("已激活", result.output)
        self.assertNotIn("绝不该出现", result.output)
        # 但正文确实进了注入槽位。
        self.assertIn("绝不该出现", m.active_text())

    def test_not_found_lists_available_names(self) -> None:
        _write(self.user_skills / "alpha.md", _skill_text("alpha"))
        result = self._tool().execute({"name": "alfa"})
        self.assertFalse(result.ok)
        self.assertIn("alpha", result.output)

    def test_isolated_points_at_a_real_command(self) -> None:
        """独立模式失败提示必须指向真实存在的入口，短命令不可用时用 /skills run。"""
        _write(self.user_skills / "rev.md", _skill_text("rev", context="fork"))
        result = self._tool().execute({"name": "rev"})
        self.assertFalse(result.ok)
        self.assertIn("/skills run rev", result.output)

    def test_isolated_uses_short_command_when_registered(self) -> None:
        _write(self.user_skills / "rev.md", _skill_text("rev", context="fork"))
        self.short_commands.add("rev")
        result = self._tool().execute({"name": "rev"})
        self.assertIn("/rev", result.output)
        self.assertNotIn("/skills run", result.output)

    def test_degrade_note_appended_on_activation(self) -> None:
        """激活时若已处于降级状态，结果里要附上说明（F9 的可见性要求）。"""
        from rhinecode.skills.models import BODY_MAX_LINES

        big = "\n".join(f"行{i}" for i in range(BODY_MAX_LINES + 100))
        _write(self.user_skills / "t.md", _skill_text("t", body=big))
        m = self._manager()
        tool = self._tool(m)
        tool.execute({"name": "t"})
        m.active_text()  # 触发降级计算
        result = tool.execute({"name": "t"})  # 幂等重激活，此时能读到降级标记
        self.assertTrue(result.ok)
        self.assertIn("截断", result.output)

    def test_missing_name_argument(self) -> None:
        result = self._tool().execute({})
        self.assertFalse(result.ok)
        self.assertIn("name", result.output)

    def test_exception_is_swallowed_into_failed_result(self) -> None:
        """Tool 契约：绝不向上抛异常，一律转 ok=False。"""

        class Boom:
            def activate(self, *a, **k):
                raise RuntimeError("炸了")

        from rhinecode.tools.load_skill import LoadSkillTool

        result = LoadSkillTool(Boom()).execute({"name": "x"})
        self.assertFalse(result.ok)
        self.assertIn("炸了", result.output)
