"""
命令系统 TUI 测试（c10 T41/T49/T50）。

两组测试：
1. 纯逻辑：CommandHighlighter 着色规则、compose_status_text 模式标记（无需启动 App）；
2. Textual 集成：用 App.run_test() / Pilot 驱动真实按键——Tab 单候选补全、
   多候选菜单、参数区 Tab 不改写、菜单 Enter 执行高亮项、未知命令本地提示、
   /mode（别名 /plan）与 Shift+Tab 切换状态栏 [AUTO]/[PLAN]、/init 双内容提交。

集成测试用 Fake ConversationManager（鸭子类型替身）：不构造 Provider、
不发网络请求，只记录领域方法调用（spec N8/C25/C26）。
"""

import unittest

from rich.text import Text as RichText

from rhinecode.config import Config
from rhinecode.commands import CommandSpec, CommandType, build_builtin_registry
from rhinecode.commands.builtins import INIT_PROMPT
from rhinecode.agent.events import AgentEvent, AgentEventType, StopReason
from rhinecode.tui.app import RhineApp
from rhinecode.tui.widgets import (
    CommandHighlighter,
    CommandPanel,
    InputBar,
    StatusBar,
    compose_status_text,
)


# ---------------------------------------------------------------------- #
# 纯逻辑：命令字段高亮（T41）
# ---------------------------------------------------------------------- #
class HighlighterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.highlighter = CommandHighlighter(build_builtin_registry())

    def _spans(self, value: str) -> list:
        text = RichText(value)
        self.highlighter.highlight(text)
        return [s for s in text.spans if s.style == CommandHighlighter.COMMAND_STYLE]

    def test_full_canonical_command_highlights_field_only(self) -> None:
        """/resume 3：只有命令字段着色，参数保持普通样式（spec F24）。"""
        spans = self._spans("/resume 3")
        self.assertEqual(len(spans), 1)
        self.assertEqual((spans[0].start, spans[0].end), (0, len("/resume")))

    def test_full_alias_highlights(self) -> None:
        spans = self._spans("/continue abc")
        self.assertEqual((spans[0].start, spans[0].end), (0, len("/continue")))

    def test_uppercase_command_highlights_preserving_text(self) -> None:
        text = RichText("/PLAN")
        self.highlighter.highlight(text)
        self.assertEqual(text.plain, "/PLAN")  # 显示保留用户输入形式
        spans = [s for s in text.spans if s.style == CommandHighlighter.COMMAND_STYLE]
        self.assertEqual(len(spans), 1)

    def test_partial_prefix_not_highlighted(self) -> None:
        self.assertEqual(self._spans("/res"), [])

    def test_slash_in_body_not_highlighted(self) -> None:
        self.assertEqual(self._spans("解释 /plan"), [])

    def test_unknown_command_not_highlighted(self) -> None:
        self.assertEqual(self._spans("/nope"), [])


# ---------------------------------------------------------------------- #
# 纯逻辑：状态栏模式标记（T41，spec F29–F30/N9）
# ---------------------------------------------------------------------- #
class StatusTextTests(unittest.TestCase):
    def test_auto_mode_marker(self) -> None:
        text = compose_status_text("deepseek", "m", "off", preset="auto")
        self.assertIn("\\[AUTO]", text)
        self.assertIn("[dim]", text)
        self.assertNotIn("计划模式", text)

    def test_plan_mode_marker_differs_in_style(self) -> None:
        auto_text = compose_status_text("deepseek", "m", "off", preset="auto")
        plan_text = compose_status_text("deepseek", "m", "off", preset="plan")
        self.assertIn("\\[PLAN]", plan_text)
        self.assertIn("bold", plan_text)  # PLAN 使用醒目加粗样式，与 dim 的 AUTO 不同
        self.assertNotEqual(auto_text, plan_text)
        # 忽略样式后仍能靠文字区分（N9）
        self.assertNotIn("\\[PLAN]", auto_text)
        self.assertNotIn("\\[AUTO]", plan_text)

    def test_no_marker_when_tools_unavailable(self) -> None:
        """
        auto-plan 扩展：`preset` 为 None（无工具能力的 Provider）时整段不出现。

        那些 Provider 既没有受控工具也没有 Plan Mode，显示一个模式标记只会误导。
        """
        text = compose_status_text("anthropic", "m", "off")
        self.assertNotIn("AUTO", text)
        self.assertNotIn("PLAN", text)

    def test_permission_mode_segment_is_gone(self) -> None:
        """
        auto-plan 扩展 F15（**反证**）：独立的「权限模式」段已整段删除。

        `/perm` 删掉之后主对话档位恒为放行档，一个恒定不变的段是纯噪音，
        且与 [AUTO] 标记重复。连带那段的橘色也不该再出现——橘色在本项目里
        专指「需要用户留意」，常年亮着会稀释它在别处（上下文逼近上限、
        确认面板）的分量。
        """
        for preset in ("auto", "plan", None):
            with self.subTest(preset=preset):
                text = compose_status_text("deepseek", "m", "off", preset=preset)
                self.assertNotIn("权限模式", text)
                self.assertNotIn("#FFA500", text)

    def test_other_fields_preserved(self) -> None:
        text = compose_status_text(
            "deepseek", "model-x", "high",
            preset="auto",
            mcp_status="MCP：已连接 1/1 · 工具 3",
            context_status="上下文：19% · 12.3K/64K",
        )
        for expected in ("deepseek", "model-x", "思考模式：高效",
                         "MCP：已连接 1/1", "上下文：19%"):
            self.assertIn(expected, text)


# ---------------------------------------------------------------------- #
# Textual 集成：Fake Manager + Pilot（T49/T50）
# ---------------------------------------------------------------------- #
class FakeMemoryManager:
    """RhineApp.on_mount 只用到 notify 赋值与 touch_session_lock 心跳。"""

    def __init__(self) -> None:
        self.notify = None

    def touch_session_lock(self) -> None:
        pass


class FakeSkillManager:
    """SkillManager 的最小替身：只提供 App 会用到的几个接触点。"""

    def __init__(self) -> None:
        self.notify_activation = None
        self.active_count = 0
        # c11：RhineApp.reload_skills 会拿它去重建 Skill 短命令。
        self.infos: tuple = ()

    def status_segment(self):
        return f"Skill:{self.active_count}" if self.active_count else None

    def command_infos(self):
        return self.infos


class FakeManager:
    """ConversationManager 的鸭子替身：记录领域方法调用，不触碰 Provider/网络。"""

    def __init__(self) -> None:
        self.history: list = []
        self.startup_notice = None
        # c12：App 在 on_mount 里单独取项目级 Hook 提示，走醒目通道展示
        # （与 startup_notice 分开——那条是 dim 的信息，这条是警告）。
        self.hooks_project_notice_value = None
        self.memory_manager = FakeMemoryManager()
        self.thinking_effort = "off"
        self.plan_mode = False
        self.confirm_callback = None
        self.clarify_callback = None
        self.approve_plan_callback = None
        self.submitted: list[tuple] = []
        self.resumed: list = []
        self.cleared = 0
        # c11：App 会在 on_mount 注入激活通知、在刷新状态栏时取 Skill 段。
        self.skill_manager = FakeSkillManager()
        self.skills_ran: list[tuple] = []
        self.deactivated: list = []

    @property
    def permission_mode_value(self):
        # 真实档位。auto-plan 扩展起它**不再进状态栏**（只服务 trace 启动快照），
        # 但仍是协调层的公开属性，替身照样提供。
        return "permissive"

    @property
    def preset(self):
        from rhinecode.presets import preset_of

        return preset_of(self.plan_mode)

    @property
    def preset_value(self):
        """
        状态栏取的就是这个（auto-plan 扩展）。

        ⚠ 刻意**从 `plan_mode` 推导**而不是写死返回 "auto"：写死的话，
        `/mode` 与 `Shift+Tab` 切换之后状态栏在替身上永远不变，
        而那正是几条集成用例要看的东西。
        """
        from rhinecode.presets import preset_of

        return preset_of(self.plan_mode).value

    def cycle_preset(self) -> str:
        """auto-plan 扩展：取代改造前的 toggle_plan / cycle_permission。"""
        from rhinecode.presets import axes_of, next_preset

        target = next_preset(self.preset)
        _mode, planning = axes_of(target)
        self.plan_mode = planning
        return f"模式：{target.value}"

    @property
    def tools_enabled(self) -> bool:
        return True

    def mcp_status_line(self):
        return None

    def context_status_line(self):
        return None

    def request_cancel(self) -> None:
        pass

    def submit_user_message(self, content, display_content=None):
        self.submitted.append((content, display_content))

        def gen():
            yield AgentEvent(type=AgentEventType.FINISHED, stop_reason=StopReason.COMPLETED)

        return gen()

    def cycle_thinking(self) -> str:
        return "思考模式：高效（high）"

    # auto-plan 扩展：`toggle_plan` 与 `cycle_permission` 已被 `cycle_preset`
    # 取代（定义在上面靠近 preset 属性处，与它们读写的是同一条轴）。

    def mcp_report(self) -> str:
        return "MCP 报告"

    def context_report(self) -> str:
        return "上下文报告"

    def memory_report(self) -> str:
        return "记忆报告"

    def subagent_activity(self) -> tuple:
        """
        活动区快照（tui-display 扩展 F1/F4）。

        返回空元组 = **未启用子 Agent** 那一支（与本桩件的
        `subagent_service = None` 同一个前提）：活动区整块隐藏、不占布局空间。
        """
        return ()

    def primary_arg_map(self) -> dict:
        """
        工具行主参数映射（tui-display 扩展 F12）。

        返回空字典 = **未启用工具能力**那一支，工具行全部走键值对摘要兜底
        ——与本桩件其余部分（`subagent_service = None` 等）保持同一个前提：
        这里模拟的是最小配置下的 App。
        """
        return {}

    def fold_group_map(self) -> dict:
        """
        批次归并分组表（tui-activity-fold F2）。

        返回空字典 = **未启用工具能力**那一支：一个工具都不可归并，
        每次调用照旧独立成行，形态与改造前逐字一致——与 `primary_arg_map`
        同一个前提（这里模拟的是最小配置下的 App）。
        """
        return {}

    # ---- c11 Skill 相关 ----

    def skill_status_segment(self):
        return self.skill_manager.status_segment()

    def skills_report(self) -> str:
        return "Skill 报告"

    def skills_prompt_report(self) -> str:
        return "Skill 注入报告"

    def hooks_report(self) -> str:
        return "Hook 报告"

    # ---- c13 子 Agent 相关 ----
    # `subagent_service = None` 让 App 走「未启用」分支：不注册轮询定时器，
    # 状态栏不显示子 Agent 段——与真实的非工具模式一致。

    subagent_service = None

    def running_subagent_count(self) -> int:
        return 0

    def drain_subagent_notifications(self) -> tuple:
        return ()

    # c15：协作能力未启用时的取值。真实 `ConversationManager` 在
    # `team_service is None` 时返回的正是这些，因此替身照抄即可——
    # 界面层不该为「有没有启用协作」写两套分支。
    def team_drain_notices(self) -> tuple:
        return ()

    def team_has_unread_for_main(self) -> bool:
        return False

    def team_can_auto_wake(self) -> bool:
        return False

    def team_bump_auto_wake(self) -> int:
        return 0

    def team_reset_auto_wake(self) -> None:
        return None

    def team_auto_wake_limit(self) -> int:
        return 0

    def team_board_text(self) -> str:
        return "当前未启用子 Agent 协作。"

    def team_roster_text(self) -> str:
        return ""

    def agents_report(self) -> str:
        return "子 Agent 报告"

    def cancel_subagents(self, target) -> str:
        return "已取消"

    def hooks_project_notice(self):
        """项目级 Hook 提示；缺省 None（多数用例没有项目级规则）。"""
        return self.hooks_project_notice_value

    def reload_skills(self) -> str:
        return "Skill 已重新加载"

    def deactivate_skill(self, name) -> str:
        self.deactivated.append(name)
        return f"已卸载 {name}"

    def run_skill(self, name, arguments, display):
        self.skills_ran.append((name, arguments, display))

        def gen():
            yield AgentEvent(
                type=AgentEventType.FINISHED, stop_reason=StopReason.COMPLETED
            )

        return gen()

    def manual_compact(self):
        return "无可摘要的早段"

    def resume(self, key=None):
        self.resumed.append(key)
        return "找不到会话"

    def clear(self) -> str:
        self.cleared += 1
        self.history = []
        return "对话历史已清空"


def _make_app(registry=None) -> tuple[RhineApp, FakeManager]:
    config = Config(
        protocol="deepseek",
        model="test-model",
        base_url="http://test",
        api_key="test-key",
        debug_log=False,
    )
    manager = FakeManager()
    app = RhineApp(manager, config, registry or build_builtin_registry())
    return app, manager


def _history_text(app: RhineApp) -> str:
    """把聊天区全部 Static 行的可见文本拼接，供内容断言。"""
    from textual.widgets import Static as _Static

    parts = []
    for widget in app.query("#history-messages > *"):
        if isinstance(widget, _Static):
            try:
                parts.append(widget.render().plain)
            except Exception:
                parts.append(str(widget.render()))
    return "\n".join(parts)


class CompletionInteractionTests(unittest.IsolatedAsyncioTestCase):
    """T49：单候选、多候选与参数区 Tab。"""

    async def test_single_candidate_tab_completes(self) -> None:
        app, _ = _make_app()
        async with app.run_test() as pilot:
            await pilot.press(*"/compa")
            await pilot.press("tab")
            self.assertEqual(app.query_one(InputBar).value, "/compact")
            # 单候选补全不弹「多选菜单」：面板与输入时一致，至多显示当前唯一命中项
            panel = app.query_one(CommandPanel)
            ids = [panel.get_option_at_index(i).id for i in range(panel.option_count)]
            self.assertEqual(ids, ["/compact"])

    async def test_single_candidate_with_hint_keeps_trailing_space(self) -> None:
        app, _ = _make_app()
        async with app.run_test() as pilot:
            await pilot.press(*"/resu")
            await pilot.press("tab")
            # /resume 有参数提示 → 末尾保留一个空格供继续输入
            self.assertEqual(app.query_one(InputBar).value, "/resume ")

    async def test_multi_candidate_tab_opens_stable_menu(self) -> None:
        app, _ = _make_app()
        async with app.run_test() as pilot:
            await pilot.press(*"/c")
            await pilot.press("tab")
            panel = app.query_one(CommandPanel)
            self.assertTrue(panel.display)
            # 候选只含规范名（别名 /ctx、/continue 不出现），顺序为注册顺序
            ids = [panel.get_option_at_index(i).id for i in range(panel.option_count)]
            self.assertEqual(ids, ["/context", "/compact", "/clear"])

    async def test_alias_prefix_tab_completes_canonical_only(self) -> None:
        """别名不参与补全：/cont 只剩规范名 /context 一个候选，Tab 直补。"""
        app, _ = _make_app()
        async with app.run_test() as pilot:
            await pilot.press(*"/cont")
            await pilot.press("tab")
            self.assertEqual(app.query_one(InputBar).value, "/context")

    async def test_tab_in_argument_area_untouched(self) -> None:
        app, _ = _make_app()
        async with app.run_test() as pilot:
            bar = app.query_one(InputBar)
            bar.value = "/resume 12"
            bar.cursor_position = len("/resume 12")
            await pilot.press("tab")
            self.assertEqual(bar.value, "/resume 12")

    async def test_hidden_command_not_in_menu(self) -> None:
        registry = build_builtin_registry()
        registry.register(
            CommandSpec(
                name="/hiddencmd",
                aliases=(),
                description="隐藏测试命令",
                usage="/hiddencmd",
                command_type=CommandType.LOCAL,
                handler=lambda inv, ctrl: ctrl.show_message("hidden ok"),
                hidden=True,
            )
        )
        app, _ = _make_app(registry)
        async with app.run_test() as pilot:
            await pilot.press(*"/h")
            panel = app.query_one(CommandPanel)
            ids = [panel.get_option_at_index(i).id for i in range(panel.option_count)]
            self.assertNotIn("/hiddencmd", ids)
            # 但直接输入完整名称仍可执行（spec F20）
            bar = app.query_one(InputBar)
            bar.value = "/hiddencmd"
            await pilot.press("enter")
            await pilot.pause()
            self.assertIn("hidden ok", _history_text(app))


class ExecutionInteractionTests(unittest.IsolatedAsyncioTestCase):
    """T50：菜单 Enter 执行高亮项、未知命令、模式状态与 /init 双内容。"""

    async def test_enter_executes_highlighted_candidate(self) -> None:
        app, manager = _make_app()
        async with app.run_test() as pilot:
            await pilot.press(*"/c")   # 菜单出现：/context、/compact、/clear
            await pilot.press("down")  # 高亮移到 /compact
            await pilot.press("down")  # 高亮移到 /clear
            await pilot.press("enter")
            await pilot.pause()
            # 执行的是高亮候选（/clear 行为），而非把前缀发给 Agent
            self.assertEqual(manager.cleared, 1)
            self.assertEqual(manager.submitted, [])

    async def test_unknown_command_local_hint_only(self) -> None:
        app, manager = _make_app()
        async with app.run_test() as pilot:
            bar = app.query_one(InputBar)
            bar.value = "/plna"
            app.query_one(CommandPanel).hide()  # 确保无高亮候选干扰
            await pilot.press("enter")
            await pilot.pause()
            self.assertEqual(manager.submitted, [])
            text = _history_text(app)
            self.assertIn("未知命令", text)
            self.assertIn("/help", text)

    async def test_mode_toggle_switches_status_marker(self) -> None:
        """`/plan`（现在是 `/mode` 的别名）在两个预设间来回切，状态栏跟着变。"""
        app, manager = _make_app()
        async with app.run_test() as pilot:
            bar_widget = app.query_one(StatusBar)
            self.assertIn("[AUTO]", bar_widget.render().plain)
            bar = app.query_one(InputBar)
            bar.value = "/plan"
            app.query_one(CommandPanel).hide()
            await pilot.press("enter")
            await pilot.pause()
            self.assertTrue(manager.plan_mode)
            self.assertIn("[PLAN]", bar_widget.render().plain)
            # 再次执行恢复 [AUTO]
            bar.value = "/plan"
            await pilot.press("enter")
            await pilot.pause()
            self.assertIn("[AUTO]", bar_widget.render().plain)

    async def test_shift_tab_switches_preset(self) -> None:
        """
        auto-plan 扩展 F5：`Shift+Tab` 走**真实按键路径**切换预设。

        ⚠ 这条必须用按键而不是直接调命令——它要验的是 `priority=True` 有没有
        真的从 Textual 手里抢到这个键（`Screen` 自带 `shift+tab → focus_previous`）。
        调命令会绕过整个按键分发，抢没抢到都会绿。

        同时断言**焦点没被夺走**：抢占之后再自己动焦点，等于把被抢掉的行为
        又还回去一半，用户会看到「模式变了、光标也跑了」。
        """
        app, manager = _make_app()
        async with app.run_test() as pilot:
            bar_widget = app.query_one(StatusBar)
            focused_before = app.focused
            self.assertIn("[AUTO]", bar_widget.render().plain)

            await pilot.press("shift+tab")
            await pilot.pause()
            self.assertTrue(manager.plan_mode, "shift+tab 必须真的切到 plan")
            self.assertIn("[PLAN]", bar_widget.render().plain)
            self.assertIs(app.focused, focused_before, "切换不得改变焦点")

            await pilot.press("shift+tab")
            await pilot.pause()
            self.assertFalse(manager.plan_mode)
            self.assertIn("[AUTO]", bar_widget.render().plain)

    async def test_init_dual_content(self) -> None:
        app, manager = _make_app()
        async with app.run_test() as pilot:
            bar = app.query_one(InputBar)
            bar.value = "/init"
            app.query_one(CommandPanel).hide()
            await pilot.press("enter")
            await pilot.pause()
            # 界面只回显一次原命令；Manager 收到完整提示词与显示内容
            self.assertEqual(_history_text(app).count("/init"), 1)
            self.assertEqual(manager.submitted, [(INIT_PROMPT, "/init")])

    async def test_plain_message_submitted_once(self) -> None:
        app, manager = _make_app()
        async with app.run_test() as pilot:
            bar = app.query_one(InputBar)
            bar.value = "请解释 /plan 的用途"
            await pilot.press("enter")
            await pilot.pause()
            self.assertEqual(manager.submitted, [("请解释 /plan 的用途", None)])
            self.assertFalse(manager.plan_mode)  # 正文斜杠不触发命令（C12）

    async def test_escape_closes_menu_keeps_input(self) -> None:
        app, manager = _make_app()
        async with app.run_test() as pilot:
            await pilot.press(*"/cont")
            panel = app.query_one(CommandPanel)
            self.assertTrue(panel.display)
            await pilot.press("escape")
            self.assertFalse(panel.display)
            self.assertEqual(app.query_one(InputBar).value, "/cont")
            self.assertEqual(manager.resumed, [])


if __name__ == "__main__":
    unittest.main()
