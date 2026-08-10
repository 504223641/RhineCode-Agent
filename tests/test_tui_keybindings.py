"""
TUI 结构回归测试（键位 + c10 命令接线的静态断言）。

对 app.py / widgets.py 源码做结构检查：不启动真实终端，验证
- 单次 Ctrl+C 不导致退出（历史回归，判据形态已随 tui-display 扩展改写）
- 提交入口只走 CommandDispatcher 单入口（c10 T51）
- 旧的命令字符串状态刷新白名单与静态 COMMANDS 列表已删除
- 运行中 Esc 取消、确认/会话面板优先级守卫仍在
- SessionPanel 选中路径直调 resume_session，不再拼接斜杠文本
"""

import ast
import unittest
from pathlib import Path


APP_SOURCE = Path(__file__).resolve().parents[1] / "rhinecode" / "tui" / "app.py"
WIDGETS_SOURCE = Path(__file__).resolve().parents[1] / "rhinecode" / "tui" / "widgets.py"


class TuiKeybindingTests(unittest.TestCase):
    """
    `Ctrl+C` 的历史约束（c2 AC9）——**判据形态改写，意图原样继承**。

    原判据是「`ctrl+c` 不得绑定到退出动作」，理由记在 `docs/c2/spec.md`：
    **「`Ctrl+C` 用于复制场景，不应触发退出」**。

    tui-display 扩展把退出改成**连按两次** `Ctrl+C`。这不是推翻那条约束，
    而是兑现它的本意——单次按下依然不退出（复制场景安全），两次连按才退。
    因此这里的判据由「不得绑定」改成「**不得直接绑到 `quit`**」，
    行为侧的「单次按下后应用仍在运行」由 `tests/test_tui_quit.py` 承担
    （空闲 / 流式运行中 / 面板挂起中各验一次）。
    """

    def test_single_ctrl_c_never_quits(self):
        """
        `ctrl+c` 不得直接绑到 Textual 的 `quit` 动作。

        它必须走本项目的 `request_quit`——那里面才有「第一次只提示、
        有选中文本就复制且不计数」这几条。直接绑 `quit` 等于一按就退，
        正是 c2 AC9 当年要挡的。
        """
        tree = ast.parse(APP_SOURCE.read_text(encoding="utf-8"))

        ctrl_c_quit_bindings = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            if not isinstance(node.func, ast.Name) or node.func.id != "Binding":
                continue
            args = node.args
            if len(args) >= 2:
                key = args[0].value if isinstance(args[0], ast.Constant) else None
                action = args[1].value if isinstance(args[1], ast.Constant) else None
                if key == "ctrl+c" and action == "quit":
                    ctrl_c_quit_bindings.append(node)

        self.assertEqual(ctrl_c_quit_bindings, [])

    def test_placeholder_points_to_double_ctrl_c_for_quit(self):
        """
        输入框占位符必须写「连按两次 Ctrl+C 退出」，且不得再提 `Ctrl+Q 退出`。

        占位符是用户唯一会读到「怎么退出」的地方。留着旧文案的后果是用户按
        `Ctrl+Q` 什么都不发生（那个键已被 `action_noop` 吃掉），却完全不知道
        该按什么。
        """
        source = APP_SOURCE.read_text(encoding="utf-8")

        self.assertIn("连按两次 Ctrl+C 退出", source)
        self.assertNotIn("Ctrl+Q 退出", source)


class CommandWiringStructureTests(unittest.TestCase):
    """c10 T51：命令系统接线后的源码结构回归。"""

    def setUp(self) -> None:
        self.app_source = APP_SOURCE.read_text(encoding="utf-8")
        self.widgets_source = WIDGETS_SOURCE.read_text(encoding="utf-8")

    def _method_source(self, name: str) -> str:
        """取 app.py 中指定方法的源码段（AST 定位）。"""
        tree = ast.parse(self.app_source)
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == name:
                return ast.get_source_segment(self.app_source, node) or ""
        self.fail(f"app.py 中找不到方法 {name}")

    def test_command_panel_static_list_removed(self) -> None:
        """CommandPanel 不再维护静态 COMMANDS 类属性（候选来自注册表）。"""
        from rhinecode.tui.widgets import CommandPanel

        self.assertFalse(hasattr(CommandPanel, "COMMANDS"))

    def test_submit_entry_uses_dispatcher_only(self) -> None:
        """提交入口只调用 dispatcher.dispatch，不再引用 handle_input。"""
        submit = self._method_source("on_input_bar_input_submitted")
        self.assertIn("dispatch", submit)
        self.assertNotIn("handle_input", submit)

    def test_command_string_whitelist_removed(self) -> None:
        """旧的 `if text in ("/think", ...)` 状态刷新白名单已删除。"""
        self.assertNotIn('text in ("/think"', self.app_source)
        self.assertNotIn('"/think", "/plan", "/perm", "/clear"', self.app_source)

    def test_no_system_exit_handling_in_submit(self) -> None:
        """/exit 经控制器接口退出，提交入口不再捕获 SystemExit。"""
        submit = self._method_source("on_input_bar_input_submitted")
        self.assertNotIn("SystemExit", submit)

    def test_escape_cancel_and_panel_priority_kept(self) -> None:
        """运行中 Esc 取消与确认/会话面板优先级守卫仍存在。"""
        on_key = self._method_source("on_key")
        self.assertIn("request_cancel", on_key)
        self.assertIn("_pending_interaction", on_key)
        self.assertIn("_session_panel_active", on_key)

    def test_session_panel_calls_resume_session_without_slash_text(self) -> None:
        """
        SessionPanel 选中路径直调 resume_session，不拼接 "/resume ..." 文本。

        P1a 起该调用下移了一层：选中分支只调 `_settle_session`，由它统一
        埋点 + 关面板 + 载入（两条结算路径共用同一份实现）。断言随之跟到那一层，
        「不伪造斜杠文本」这条要求本身一字未变。
        """
        selected = self._method_source("on_option_list_option_selected")
        self.assertIn("_settle_session", selected, "选中路径必须走会话结算的唯一入口")
        settle = self._method_source("_settle_session")
        self.assertIn("resume_session", settle)
        for source in (selected, settle):
            self.assertNotIn('f"/resume', source)
            self.assertNotIn("'/resume", source)

    def test_settle_session_is_the_single_entry(self) -> None:
        """
        `_settle_session` 是会话面板结算的**唯一入口**，且带幂等守卫。

        两条既有路径（选中 / Esc 取消）都必须经由它；缺了幂等守卫，
        驱动设施退出时的强制结算会在没有面板时凭空多埋一条交互事件。
        """
        settle = self._method_source("_settle_session")
        self.assertIn("_session_panel_active", settle, "第一行必须是幂等守卫")
        self.assertIn("INTERACTION", settle, "埋点收拢在此")
        self.assertIn("_close_session_panel", settle)
        cancelled = self._method_source("on_session_panel_cancelled")
        self.assertIn("_settle_session", cancelled)

    def test_app_implements_controller_surface(self) -> None:
        """CommandController 协议的关键能力在 App 上都有实现（C79 静态面）。"""
        for method in (
            "show_user_input", "show_message", "send_user_message", "switch_mode",
            "query_report", "refresh_status", "clear_conversation",
            "compact_context", "resume_session", "exit_application",
        ):
            self.assertIn(f"def {method}", self.app_source, method)


if __name__ == "__main__":
    unittest.main()
