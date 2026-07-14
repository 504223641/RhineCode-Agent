"""
TUI 结构回归测试（键位 + c10 命令接线的静态断言）。

对 app.py / widgets.py 源码做结构检查：不启动真实终端，验证
- Ctrl+C 未绑定退出（历史回归）
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
    def test_ctrl_c_is_not_bound_to_quit(self):
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

    def test_placeholder_points_to_ctrl_q_for_quit(self):
        source = APP_SOURCE.read_text(encoding="utf-8")

        self.assertIn("Ctrl+Q 退出", source)
        self.assertNotIn("Ctrl+C 退出", source)


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
        """SessionPanel 选中路径直调 resume_session，不拼接 "/resume ..." 文本。"""
        selected = self._method_source("on_option_list_option_selected")
        self.assertIn("resume_session", selected)
        self.assertNotIn('f"/resume', selected)
        self.assertNotIn("'/resume", selected)

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
