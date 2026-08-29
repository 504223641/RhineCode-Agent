"""
Skill 的 TUI 集成测试（c11 T56）。

覆盖 spec AC24（Skill 短命令进补全并正确执行）、AC34（状态栏 Skill 段与
激活通知）、AC35（提交守卫给出可见提示而不是静默无反应）。

复用 c10 的 Fake Manager 与 Pilot 脚手架：不构造 Provider、不发网络请求。
"""

import unittest

from rhinecode.commands import build_builtin_registry
from rhinecode.commands.skill_commands import build_skill_command_specs
from rhinecode.skills.models import SkillCommandInfo
from rhinecode.tui.widgets import CommandPanel, InputBar, compose_status_text
from tests.test_command_tui import _history_text, _make_app


def _registry_with_skills(*names):
    """造一个内置命令 + 若干 Skill 短命令的注册表。"""
    registry = build_builtin_registry()
    registry.replace_skill_commands(
        build_skill_command_specs(
            [
                SkillCommandInfo(
                    name=n, description=f"{n} 的说明", forked=False
                )
                for n in names
            ]
        )
    )
    return registry


class StatusBarSkillSegmentTests(unittest.TestCase):
    """AC34 纯逻辑侧：状态栏 Skill 段的有无。"""

    def test_absent_when_none_active(self) -> None:
        """无激活时不渲染该段——没用 Skill 的用户状态栏与 c10 完全一致。"""
        text = compose_status_text("deepseek", "m", "off")
        self.assertNotIn("Skill:", text)

    def test_present_and_other_fields_kept(self) -> None:
        text = compose_status_text(
            "deepseek",
            "m",
            "high",
            preset="plan",
            mcp_status="MCP：已连接 1/1",
            context_status="上下文：19%",
            skill_status="Skill:2",
        )
        self.assertIn("Skill:2", text)
        # 其它字段一个不少。
        # auto-plan 扩展：「权限模式」段已整段删除（档位恒为放行、与 [PLAN]/[AUTO]
        # 标记重复），这里不再断言它。反证在
        # `test_command_tui.py::test_permission_mode_segment_is_gone`。
        self.assertIn("PLAN", text)
        self.assertIn("MCP", text)
        self.assertIn("上下文", text)

    def test_no_square_brackets_to_avoid_markup_trap(self) -> None:
        """
        Skill 段刻意不含方括号。

        Textual 会把 `[...]` 当 markup 标签吞掉，项目里凡含字面 `[` 的状态栏
        文本都得转义。不用方括号就绕开了这个坑。
        """
        text = compose_status_text("deepseek", "m", "off", skill_status="Skill:1")
        self.assertIn("Skill:1", text)
        self.assertNotIn("[Skill", text)


class SkillCommandPilotTests(unittest.IsolatedAsyncioTestCase):
    """AC24：Skill 短命令的补全与执行。"""

    async def test_skill_command_appears_in_completion(self) -> None:
        app, _ = _make_app(registry=_registry_with_skills("deploy"))
        async with app.run_test() as pilot:
            await pilot.press(*"/dep")
            await pilot.press("tab")
            self.assertEqual(app.query_one(InputBar).value, "/deploy ")

    async def test_skill_command_coexists_with_builtin_in_menu(self) -> None:
        """与内置命令共存于候选菜单，且内置在前（顺序稳定）。"""
        app, _ = _make_app(registry=_registry_with_skills("commit"))
        async with app.run_test() as pilot:
            await pilot.press(*"/co")
            await pilot.press("tab")
            panel = app.query_one(CommandPanel)
            ids = [panel.get_option_at_index(i).id for i in range(panel.option_count)]
            self.assertIn("/commit", ids)
            self.assertIn("/context", ids)
            self.assertLess(ids.index("/context"), ids.index("/commit"))

    async def test_executing_skill_command_passes_three_arguments(self) -> None:
        """执行 Skill 短命令 → run_skill 收到 name / arguments / display 三项。"""
        app, manager = _make_app(registry=_registry_with_skills("commit"))
        async with app.run_test() as pilot:
            await pilot.press(*"/commit 修复超时")
            await pilot.press("enter")
            await pilot.pause()
            self.assertEqual(
                manager.skills_ran, [("commit", "修复超时", "/commit 修复超时")]
            )

    async def test_skills_subcommands_reach_controller(self) -> None:
        """/skills reload 与 /skills off 分别调到对应控制器方法。"""
        app, manager = _make_app()
        async with app.run_test() as pilot:
            await pilot.press(*"/skills reload")
            await pilot.press("enter")
            await pilot.pause()
            self.assertIn("Skill 已重新加载", _history_text(app))

            await pilot.press(*"/skills off commit")
            await pilot.press("enter")
            await pilot.pause()
            self.assertEqual(manager.deactivated, ["commit"])


class StatusRefreshTests(unittest.IsolatedAsyncioTestCase):
    """AC34：模型激活 Skill 后状态栏立刻更新。"""

    async def test_activation_notify_refreshes_status_bar(self) -> None:
        app, manager = _make_app()
        async with app.run_test() as pilot:
            # on_mount 应已把回调注入 SkillManager。
            self.assertIsNotNone(manager.skill_manager.notify_activation)

            manager.skill_manager.active_count = 1
            app._refresh_status()
            await pilot.pause()
            from rhinecode.tui.widgets import StatusBar

            self.assertIn("Skill:1", app.query_one(StatusBar).render().markup)

            manager.skill_manager.active_count = 0
            app._refresh_status()
            await pilot.pause()
            self.assertNotIn("Skill:", app.query_one(StatusBar).render().markup)


class SubmitGuardHintTests(unittest.IsolatedAsyncioTestCase):
    """
    AC35：忙碌时提交给出**可见提示**，而不是静默无反应。

    静默 return 会让用户以为界面卡死了——尤其在确认面板期间，输入框并没有
    被禁用，用户点回去敲回车是很自然的动作。
    """

    async def test_streaming_submit_shows_hint_once(self) -> None:
        app, _ = _make_app()
        async with app.run_test() as pilot:
            app._set_streaming(True)
            await pilot.press(*"你好")
            await pilot.press("enter")
            await pilot.pause()
            text = _history_text(app)
            self.assertIn("正在运行中", text)

            # 同一次流式内连按两次只提示一次，避免刷屏。
            await pilot.press("enter")
            await pilot.pause()
            self.assertEqual(_history_text(app).count("正在运行中"), 1)

    async def test_hint_reset_on_new_stream(self) -> None:
        """进入新一轮流式后可以再提示一次。"""
        app, _ = _make_app()
        async with app.run_test() as pilot:
            app._set_streaming(True)
            # 空输入不会触发提交事件，必须先敲点内容。
            await pilot.press(*"甲")
            await pilot.press("enter")
            await pilot.pause()
            app._set_streaming(False)
            app._set_streaming(True)
            await pilot.press(*"乙")
            await pilot.press("enter")
            await pilot.pause()
            self.assertEqual(_history_text(app).count("正在运行中"), 2)

    async def test_pending_interaction_submit_shows_hint(self) -> None:
        """
        确认面板期间提交 → 出现「等待确认」提示。

        **本分支可达且最常见**：只有澄清面板会禁用 InputBar，
        确认面板与计划审批面板都不禁用。
        """
        app, _ = _make_app()
        async with app.run_test() as pilot:
            app._pending_interaction = {"event": None, "result": None, "kind": "confirm"}
            app._busy_hint_shown = False
            await pilot.press(*"想插一句")
            await pilot.press("enter")
            await pilot.pause()
            self.assertIn("等待你的确认", _history_text(app))

    async def test_normal_submit_unaffected(self) -> None:
        """不忙碌时提交照常走分发器，不产生任何提示。"""
        app, manager = _make_app()
        async with app.run_test() as pilot:
            await pilot.press(*"普通消息")
            await pilot.press("enter")
            await pilot.pause()
            self.assertEqual(len(manager.submitted), 1)
            self.assertNotIn("正在运行中", _history_text(app))


if __name__ == "__main__":
    unittest.main()


class SkillCommandHotReloadTests(unittest.IsolatedAsyncioTestCase):
    """
    `/skills reload` 之后新增 Skill 的短命令立刻可用（c11 F26）。

    用**真实** SkillManager + 真实 CommandRegistry 端到端验：
    只断言「replace_skill_commands 被调过」证明不了「新命令真的能补全并执行」，
    而后者才是这条链路存在的意义。
    """

    def setUp(self) -> None:
        import tempfile
        from pathlib import Path

        self._home = tempfile.TemporaryDirectory()
        self.skills_dir = Path(self._home.name) / "skills"
        self.skills_dir.mkdir(parents=True)

    def tearDown(self) -> None:
        self._home.cleanup()

    def _write(self, name: str) -> None:
        (self.skills_dir / f"{name}.md").write_text(
            f"---\nname: {name}\ndescription: {name} 的说明\n---\n正文\n",
            encoding="utf-8",
        )

    def _real_skill_manager(self, registry):
        from pathlib import Path
        from rhinecode.skills.manager import SkillManager

        sm = SkillManager(
            project_root=None,
            user_dir=Path(self._home.name),
            builtin_dir=None,
            has_short_command=registry.has_skill_command,
        )
        sm.startup()
        return sm

    def _app_with_real_skills(self):
        """把 FakeManager 的 skill_manager 换成真的，其余保持替身。"""
        registry = build_builtin_registry()
        app, manager = _make_app(registry=registry)
        sm = self._real_skill_manager(registry)
        # 模拟 __main__ 启动时的初次短命令注册，否则「启动时已有的 Skill」
        # 其短命令根本没注册过，测不出「reload 后消失」。
        registry.replace_skill_commands(
            build_skill_command_specs(sm.command_infos())
        )
        manager.skill_manager = sm
        # 领域方法改为委托真实 SkillManager，与生产实现同口径。
        manager.reload_skills = lambda: (
            sm.reload(),
            "Skill 定义已重新加载。",
        )[1]
        manager.skill_status_segment = sm.status_segment
        return app, manager, registry

    async def test_new_skill_short_command_available_after_reload(self) -> None:
        app, _, registry = self._app_with_real_skills()
        async with app.run_test() as pilot:
            # 启动时没有任何 Skill。
            self.assertIsNone(registry.resolve("/fresh"))

            # 模拟用户在外部新建了一个 Skill 文件。
            self._write("fresh")

            await pilot.press(*"/skills reload")
            await pilot.press("enter")
            await pilot.pause()

            # ① 注册表里有了。
            self.assertTrue(registry.has_skill_command("fresh"))
            # ② 补全能补出来——CommandPanel 每次按键现调 registry.complete，
            #    所以不需要刷新任何组件。
            await pilot.press(*"/fre")
            await pilot.press("tab")
            self.assertEqual(app.query_one(InputBar).value, "/fresh ")

    async def test_removed_skill_short_command_disappears(self) -> None:
        """定义被删 → 短命令随之消失，不留一条会报错的僵尸命令。"""
        self._write("gone")
        app, _, registry = self._app_with_real_skills()
        async with app.run_test() as pilot:
            self.assertTrue(registry.has_skill_command("gone"))

            (self.skills_dir / "gone.md").unlink()
            await pilot.press(*"/skills reload")
            await pilot.press("enter")
            await pilot.pause()

            self.assertFalse(registry.has_skill_command("gone"))
            self.assertIsNone(registry.resolve("/gone"))

    async def test_conflicting_new_skill_reports_run_fallback(self) -> None:
        """
        新增的 Skill 与内置命令重名 → 短命令不注册，但报告里给出 /skills run 入口。

        没有这句提示，用户会以为 Skill 根本没加载成功。
        """
        app, _, registry = self._app_with_real_skills()
        async with app.run_test() as pilot:
            self._write("clear")
            await pilot.press(*"/skills reload")
            await pilot.press("enter")
            await pilot.pause()

            text = _history_text(app)
            self.assertIn("/clear", text)
            self.assertIn("/skills run", text)
            # 内置 /clear 完好无损。
            from rhinecode.commands.models import CommandType

            self.assertIs(registry.resolve("/clear").command_type, CommandType.UI)

    async def test_builtin_commands_survive_reload(self) -> None:
        """热更新不得误伤内置命令。"""
        app, _, registry = self._app_with_real_skills()
        async with app.run_test() as pilot:
            before = [s.name for s in registry.visible_commands()]
            self._write("extra")
            await pilot.press(*"/skills reload")
            await pilot.press("enter")
            await pilot.pause()
            after = [s.name for s in registry.visible_commands()]
            for name in before:
                self.assertIn(name, after)
            self.assertIn("/extra", after)
