"""
Skill 的 TUI 集成测试（c11 T56）。

覆盖 spec AC24（Skill 短命令进补全并正确执行）、AC34（状态栏 Skill 段与
激活通知）、AC35（提交守卫给出可见提示而不是静默无反应）。

复用 c10 的 Fake Manager 与 Pilot 脚手架：不构造 Provider、不发网络请求。
"""

import unittest

from rhinecode.commands import build_builtin_registry
from rhinecode.commands.skill_commands import build_skill_command_specs
from rhinecode.skills.models import SkillCommandInfo, SkillMode
from rhinecode.tui.app import RhineApp
from rhinecode.tui.widgets import CommandPanel, InputBar, compose_status_text
from tests.test_command_tui import _history_text, _make_app


def _registry_with_skills(*names):
    """造一个内置命令 + 若干 Skill 短命令的注册表。"""
    registry = build_builtin_registry()
    registry.replace_skill_commands(
        build_skill_command_specs(
            [
                SkillCommandInfo(
                    name=n, description=f"{n} 的说明", mode=SkillMode.SHARED
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
            plan_mode=True,
            permission_mode="default",
            mcp_status="MCP：已连接 1/1",
            context_status="上下文：19%",
            skill_status="Skill:2",
        )
        self.assertIn("Skill:2", text)
        # 其它字段一个不少。
        self.assertIn("PLAN", text)
        self.assertIn("权限模式", text)
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
