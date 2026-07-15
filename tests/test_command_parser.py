"""
命令解析器测试（c10 T4–T5）：输入分类、首空白切分、大小写与参数边界。

parse_input 是无副作用纯函数，测试不需要任何界面、网络或磁盘依赖（spec N8）。
"""

import unittest

from rhinecode.commands import InputKind, parse_input


class ParserClassificationTests(unittest.TestCase):
    """T4：基本分类——空输入、普通消息、首位斜杠。"""

    def test_empty_string_is_empty(self) -> None:
        parsed = parse_input("")
        self.assertEqual(parsed.kind, InputKind.EMPTY)

    def test_whitespace_only_is_empty(self) -> None:
        parsed = parse_input("   \t \n ")
        self.assertEqual(parsed.kind, InputKind.EMPTY)

    def test_plain_message(self) -> None:
        parsed = parse_input("你好，介绍一下项目")
        self.assertEqual(parsed.kind, InputKind.MESSAGE)
        self.assertEqual(parsed.raw_text, "你好，介绍一下项目")

    def test_slash_in_body_is_message(self) -> None:
        """正文中的 /plan 不触发命令（spec F7）。"""
        parsed = parse_input("请解释 /plan 的作用")
        self.assertEqual(parsed.kind, InputKind.MESSAGE)
        self.assertIsNone(parsed.command_token)

    def test_leading_slash_is_slash(self) -> None:
        parsed = parse_input("/plan")
        self.assertEqual(parsed.kind, InputKind.SLASH)
        self.assertEqual(parsed.command_token, "/plan")
        self.assertEqual(parsed.arguments, "")

    def test_leading_whitespace_before_slash_still_slash(self) -> None:
        """两端空白去除后首字符为 / 才进入命令解析（spec F5）。"""
        parsed = parse_input("   /plan  ")
        self.assertEqual(parsed.kind, InputKind.SLASH)
        self.assertEqual(parsed.command_token, "/plan")

    def test_command_token_preserves_case(self) -> None:
        """命令字段保留用户原始大小写（casefold 在注册表查询时才做，spec F6）。"""
        parsed = parse_input("/PLAN")
        self.assertEqual(parsed.command_token, "/PLAN")

    def test_argument_split(self) -> None:
        parsed = parse_input("/resume ABC-123")
        self.assertEqual(parsed.command_token, "/resume")
        self.assertEqual(parsed.arguments, "ABC-123")


class ParserArgumentBoundaryTests(unittest.TestCase):
    """T5：空白分隔符与参数边界。"""

    def test_tab_as_separator(self) -> None:
        parsed = parse_input("/resume\tABC")
        self.assertEqual(parsed.command_token, "/resume")
        self.assertEqual(parsed.arguments, "ABC")

    def test_newline_as_separator(self) -> None:
        parsed = parse_input("/resume\nABC")
        self.assertEqual(parsed.command_token, "/resume")
        self.assertEqual(parsed.arguments, "ABC")

    def test_consecutive_separators_not_in_arguments(self) -> None:
        """命令名与参数之间连续分隔空白不属于参数（spec F5）。"""
        parsed = parse_input("/PLAN    Now Please ")
        self.assertEqual(parsed.command_token, "/PLAN")
        self.assertEqual(parsed.arguments, "Now Please")

    def test_argument_outer_whitespace_stripped_inner_preserved(self) -> None:
        """参数去两端空白，内部连续空白、引号、斜杠与大小写保持原样（spec F6/F11）。"""
        parsed = parse_input('/cmd   "a b";  x|y C:\\tmp  MiXeD  ')
        self.assertEqual(parsed.arguments, '"a b";  x|y C:\\tmp  MiXeD')

    def test_mixed_case_command_with_argument(self) -> None:
        parsed = parse_input("/ReSuMe AbC123")
        self.assertEqual(parsed.command_token, "/ReSuMe")
        self.assertEqual(parsed.arguments, "AbC123")

    def test_bare_slash_is_slash_input(self) -> None:
        """只有 "/" 的输入仍是斜杠输入，由注册表决定是否未知（T5 步骤 4）。"""
        parsed = parse_input("/")
        self.assertEqual(parsed.kind, InputKind.SLASH)
        self.assertEqual(parsed.command_token, "/")


if __name__ == "__main__":
    unittest.main()
