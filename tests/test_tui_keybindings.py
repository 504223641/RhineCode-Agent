import ast
import unittest
from pathlib import Path


APP_SOURCE = Path(__file__).resolve().parents[1] / "rhinecode" / "tui" / "app.py"


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


if __name__ == "__main__":
    unittest.main()
