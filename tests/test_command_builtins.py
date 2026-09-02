"""
内置命令测试（c10 T20–T22）：元数据完整性、Fake Controller 行为与 /init 双内容。

build_builtin_registry() 是无导入副作用的纯构造，测试不依赖真实界面或网络。
"""

import unittest

from rhinecode.commands import (
    CommandDispatcher,
    CommandType,
    ModeTarget,
    ReportTarget,
    build_builtin_registry,
)
from rhinecode.commands.builtins import INIT_PROMPT

from tests.test_command_dispatcher import FakeController


# plan 第 7 节批准的登记表：规范名 → (别名集合, 类型)
EXPECTED_TABLE = {
    "/help": ({"/h"}, CommandType.LOCAL),
    "/think": (set(), CommandType.UI),
    "/mode": ({"/plan"}, CommandType.UI),
    "/mcp": (set(), CommandType.LOCAL),
    "/context": ({"/ctx"}, CommandType.LOCAL),
    "/compact": (set(), CommandType.LOCAL),
    "/memory": (set(), CommandType.LOCAL),
    "/resume": ({"/continue"}, CommandType.UI),
    "/init": (set(), CommandType.PROMPT),
    # c11 新增：Skill 管理命令，无别名、本地类型。
    "/skills": (set(), CommandType.LOCAL),
    # c12 新增：Hook 只读报告，无别名、本地类型。
    "/hooks": (set(), CommandType.LOCAL),
    # c13 新增：子 Agent 角色与任务。无别名、本地类型。
    # **不是纯只读**（有 cancel 子命令）——一个跑偏的后台子 Agent 若没有取消入口，
    # 用户只能退出整个程序。命令类型仍是 LOCAL：它不进 AI、不改界面模式。
    "/agents": (set(), CommandType.LOCAL),
    # c15 新增：共享任务清单的只读视图。别名 /board，本地类型。
    # **纯只读、无子命令**——任务的增删改由模型通过工具做，不由用户敲命令做，
    # 否则会出现两条并行的写路径，而命令层那条绕开了「谁改的」这个记录。
    "/tasks": ({"/board"}, CommandType.LOCAL),
    # first-run-setup 扩展新增：重跑配置向导。无别名、界面类型。
    # **UI 而不是 LOCAL**：它推一个 ModalScreen 上来，属于「改变界面状态」；
    # 走 PROMPT 的话敲一次会变成一句发给模型的话，既花钱又什么都不会发生。
    "/setup": (set(), CommandType.UI),
    "/clear": ({"/reset", "/new"}, CommandType.UI),
    "/exit": ({"/quit"}, CommandType.UI),
}


class BuiltinMetadataTests(unittest.TestCase):
    """T20：内置命令元数据与批准表格一致。"""

    def setUp(self) -> None:
        self.registry = build_builtin_registry()

    def test_exactly_sixteen_canonical_commands(self) -> None:
        """
        内置命令恰好十六条（C10 的十二条 + c11 的 /skills + c12 的 /hooks
        + c13 的 /agents + c15 的 /tasks，减去 auto-plan 扩展合并掉的一条
        ——`/plan` 与 `/perm` 合并为 `/mode`，`/plan` 降为别名；
        再加 first-run-setup 扩展的 `/setup`）。

        这条 len 断言是「批准表」的护栏——它保证任何人新增命令时必须
        显式更新 EXPECTED_TABLE 并同步这个数字，而不能悄悄加进去。
        **绝不能因为它变红就删掉它**，那等于让护栏永久失效。
        ⚠ 它变红时正确的做法是**先确认这条命令是不是真的被批准了**，
        再改这里的数字——本次 `/setup` 出自 first-run-setup 扩展的 spec F15。
        """
        names = [s.name for s in self.registry.visible_commands()]
        self.assertEqual(set(names), set(EXPECTED_TABLE))
        self.assertEqual(len(names), 16)

    def test_alias_mapping(self) -> None:
        """全部首批别名映射正确（spec F10/AC5）。"""
        expected_alias_map = {
            "/quit": "/exit",
            "/continue": "/resume",
            "/plan": "/mode",
            "/ctx": "/context",
            "/h": "/help",
            "/reset": "/clear",
            "/new": "/clear",
        }
        for alias, canonical in expected_alias_map.items():
            self.assertEqual(self.registry.resolve(alias).name, canonical, alias)

    def test_mixed_case_aliases_resolve(self) -> None:
        """混合大小写别名生效（spec F6/C15）。"""
        self.assertEqual(self.registry.resolve("/QuIt").name, "/exit")
        self.assertEqual(self.registry.resolve("/CtX").name, "/context")
        self.assertEqual(self.registry.resolve("/NeW").name, "/clear")

    def test_command_types_match_approved_table(self) -> None:
        for spec in self.registry.visible_commands():
            aliases, command_type = EXPECTED_TABLE[spec.name]
            self.assertEqual(set(spec.aliases), aliases, spec.name)
            self.assertEqual(spec.command_type, command_type, spec.name)

    def test_description_and_usage_non_empty(self) -> None:
        for spec in self.registry.visible_commands():
            self.assertTrue(spec.description.strip(), spec.name)
            self.assertTrue(spec.usage.strip(), spec.name)

    def test_argument_hints_and_requires_argument(self) -> None:
        """
        /resume 与 /skills 各有其参数提示，其余为 None；
        全部内置命令 requires_argument=False（参数缺失由各自处理函数给用法提示）。
        """
        expected_hints = {"/resume": "[编号或ID]", "/skills": "[子命令]"}
        for spec in self.registry.visible_commands():
            self.assertFalse(spec.requires_argument, spec.name)
            self.assertEqual(
                spec.argument_hint, expected_hints.get(spec.name), spec.name
            )


class BuiltinBehaviorTests(unittest.TestCase):
    """T21：内置命令的控制器行为（Fake Controller 观察调用）。"""

    def setUp(self) -> None:
        self.registry = build_builtin_registry()
        self.dispatcher = CommandDispatcher(self.registry)
        self.controller = FakeController()

    def test_help_shows_registry_content_with_aliases(self) -> None:
        # `/help` 是多行报告，走 `show_report`（tui-display 扩展 F15），
        # 不是 `show_message`
        self.dispatcher.dispatch("/help", self.controller)
        shown = [c[1] for c in self.controller.calls if c[0] == "show_report"]
        self.assertEqual(len(shown), 1)
        self.assertIn("/resume", shown[0])
        self.assertIn("/continue", shown[0])
        self.assertIn("/quit", shown[0])

    def test_help_alias_equivalent(self) -> None:
        """/h 与 /help 输出一致（C32）。"""
        a, b = FakeController(), FakeController()
        self.dispatcher.dispatch("/help", a)
        self.dispatcher.dispatch("/h", b)
        self.assertEqual(a.calls[1][1], b.calls[1][1])

    def test_reports(self) -> None:
        cases = {"/mcp": ReportTarget.MCP, "/context": ReportTarget.CONTEXT, "/memory": ReportTarget.MEMORY}
        for text, target in cases.items():
            controller = FakeController()
            self.dispatcher.dispatch(text, controller)
            self.assertIn(("query_report", target), controller.calls)
            # 报告走专用通道（tui-display 扩展 F15/T28）。⚠ 这里断言的是
            # `show_report` 而不是 `show_message`——两者刻意分开记，
            # 某条报告被误改回普通通道时这条当场红。
            self.assertIn(("show_report", f"report:{target.value}"), controller.calls)

    def test_mode_commands_show_result_and_refresh(self) -> None:
        # auto-plan 扩展：`/plan` 现在是 `/mode` 的别名，两者走同一个目标；
        # 权限档已无运行期切换入口，`ModeTarget.PERMISSION` 随之删除。
        cases = {
            "/think": ModeTarget.THINKING,
            "/mode": ModeTarget.PRESET,
            "/plan": ModeTarget.PRESET,
        }
        for text, target in cases.items():
            controller = FakeController()
            self.dispatcher.dispatch(text, controller)
            self.assertIn(("switch_mode", target), controller.calls)
            self.assertIn(("show_message", f"mode:{target.value}"), controller.calls)
            self.assertIn(("refresh_status",), controller.calls)

    def test_clear_with_extra_argument_still_clears(self) -> None:
        """/clear now 仍执行清空（spec F12/C17）。"""
        self.dispatcher.dispatch("/clear now", self.controller)
        self.assertIn(("clear_conversation",), self.controller.calls)
        self.assertIn(("show_message", "对话历史已清空"), self.controller.calls)
        self.assertIn(("refresh_status",), self.controller.calls)

    def test_compact_with_extra_argument_still_compacts(self) -> None:
        self.dispatcher.dispatch("/compact extra", self.controller)
        self.assertIn(("compact_context",), self.controller.calls)

    def test_resume_argument_passing(self) -> None:
        """/resume 无参传 None、/continue ABC 传 "ABC"（spec F13）。"""
        a, b = FakeController(), FakeController()
        self.dispatcher.dispatch("/resume", a)
        self.assertIn(("resume_session", None), a.calls)
        self.dispatcher.dispatch("/continue ABC", b)
        self.assertIn(("resume_session", "ABC"), b.calls)

    def test_exit_only_calls_exit(self) -> None:
        self.dispatcher.dispatch("/exit", self.controller)
        actions = [c[0] for c in self.controller.calls if c[0] != "show_user_input"]
        self.assertEqual(actions, ["exit_application"])

    def test_quit_alias_exits(self) -> None:
        self.dispatcher.dispatch("/quit", self.controller)
        self.assertIn(("exit_application",), self.controller.calls)


class InitDualContentTests(unittest.TestCase):
    """T22：/init 的双内容行为（spec F25–F27）。"""

    def setUp(self) -> None:
        self.dispatcher = CommandDispatcher(build_builtin_registry())

    def test_tools_disabled_shows_hint_only(self) -> None:
        controller = FakeController(tools_enabled=False)
        self.dispatcher.dispatch("/init", controller)
        self.assertNotIn("send_user_message", controller.names())
        shown = [c[1] for c in controller.calls if c[0] == "show_message"]
        self.assertTrue(any("不支持 /init" in text for text in shown))

    def test_tools_enabled_sends_full_prompt(self) -> None:
        controller = FakeController(tools_enabled=True)
        self.dispatcher.dispatch("/init", controller)
        sends = [c for c in controller.calls if c[0] == "send_user_message"]
        self.assertEqual(len(sends), 1)
        self.assertEqual(sends[0][1], INIT_PROMPT)
        self.assertIn("RHINE.md", sends[0][1])

    def test_display_content_preserves_typed_input(self) -> None:
        """display_content 保留用户实际输入（如 /INIT，T22 步骤 3）。"""
        controller = FakeController(tools_enabled=True)
        self.dispatcher.dispatch("/INIT", controller)
        sends = [c for c in controller.calls if c[0] == "send_user_message"]
        self.assertEqual(sends[0][2], "/INIT")

    def test_no_duplicate_echo(self) -> None:
        """处理函数不重复回显原始命令（回显由分发器统一，恰好一次）。"""
        controller = FakeController(tools_enabled=True)
        self.dispatcher.dispatch("/init", controller)
        self.assertEqual(controller.names().count("show_user_input"), 1)


if __name__ == "__main__":
    unittest.main()
