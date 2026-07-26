"""
命令分发器测试（c10 T13–T14）：普通消息、空输入、未知命令、别名、参数校验与异常隔离。

用记录方法调用的 Fake Controller 验证控制器调用序列（spec N8/C25），
不启动真实界面、Provider 或网络。
"""

import unittest

from rhinecode.commands import (
    CommandDispatcher,
    CommandRegistry,
    CommandSpec,
    CommandType,
    DispatchKind,
)


class FakeController:
    """记录全部控制器调用的测试替身（CommandController 协议的鸭子实现）。"""

    def __init__(self, tools_enabled: bool = True) -> None:
        self._tools_enabled = tools_enabled
        self.calls: list[tuple] = []

    @property
    def tools_enabled(self) -> bool:
        return self._tools_enabled

    def show_user_input(self, text: str) -> None:
        self.calls.append(("show_user_input", text))

    def show_message(self, text: str) -> None:
        self.calls.append(("show_message", text))

    def send_user_message(self, content: str, display_content=None) -> None:
        self.calls.append(("send_user_message", content, display_content))

    def switch_mode(self, target) -> str:
        self.calls.append(("switch_mode", target))
        return f"mode:{target.value}"

    def query_report(self, target) -> str:
        self.calls.append(("query_report", target))
        return f"report:{target.value}"

    def refresh_status(self) -> None:
        self.calls.append(("refresh_status",))

    def clear_conversation(self) -> None:
        self.calls.append(("clear_conversation",))

    def compact_context(self) -> None:
        self.calls.append(("compact_context",))

    def resume_session(self, key) -> None:
        self.calls.append(("resume_session", key))

    def exit_application(self) -> None:
        self.calls.append(("exit_application",))

    # ---- c11 新增的三个控制器方法 ----

    def run_skill(self, name: str, arguments: str, display: str) -> None:
        self.calls.append(("run_skill", name, arguments, display))

    def reload_skills(self) -> str:
        self.calls.append(("reload_skills",))
        return "reloaded"

    def deactivate_skill(self, name) -> str:
        self.calls.append(("deactivate_skill", name))
        return f"off:{name}"

    def names(self) -> list[str]:
        return [c[0] for c in self.calls]


def _noop(invocation, controller) -> None:
    pass


def make_spec(name: str, aliases: tuple = (), handler=_noop, **kwargs) -> CommandSpec:
    return CommandSpec(
        name=name,
        aliases=aliases,
        description=kwargs.pop("description", "描述"),
        usage=kwargs.pop("usage", name),
        command_type=kwargs.pop("command_type", CommandType.LOCAL),
        handler=handler,
        **kwargs,
    )


class DispatcherBasicTests(unittest.TestCase):
    """T13：空输入、普通消息与未知命令。"""

    def setUp(self) -> None:
        self.registry = CommandRegistry()
        self.registry.register(make_spec("/ping"))
        self.dispatcher = CommandDispatcher(self.registry)
        self.controller = FakeController()

    def test_empty_input_no_side_effects(self) -> None:
        """空输入：无任何控制器调用（spec F4/C09）。"""
        for text in ("", "   ", "\t\n"):
            result = self.dispatcher.dispatch(text, self.controller)
            self.assertEqual(result.kind, DispatchKind.EMPTY)
        self.assertEqual(self.controller.calls, [])

    def test_plain_message_echo_then_send(self) -> None:
        """普通消息：按「回显 → 发送」顺序各调用一次（plan 8.1）。"""
        result = self.dispatcher.dispatch("请解释 /plan 的用途", self.controller)
        self.assertEqual(result.kind, DispatchKind.MESSAGE)
        self.assertEqual(
            self.controller.calls,
            [
                ("show_user_input", "请解释 /plan 的用途"),
                ("send_user_message", "请解释 /plan 的用途", None),
            ],
        )

    def test_unknown_command_shows_help_hint(self) -> None:
        """未知命令：回显 + /help 引导，绝不 send_user_message（spec F8）。"""
        result = self.dispatcher.dispatch("/does-not-exist", self.controller)
        self.assertEqual(result.kind, DispatchKind.UNKNOWN)
        self.assertEqual(self.controller.names(), ["show_user_input", "show_message"])
        message = self.controller.calls[1][1]
        self.assertIn("/does-not-exist", message)
        self.assertIn("/help", message)
        self.assertNotIn("send_user_message", self.controller.names())

    def test_known_command_echoes_once(self) -> None:
        result = self.dispatcher.dispatch("/ping", self.controller)
        self.assertEqual(result.kind, DispatchKind.COMMAND)
        self.assertEqual(result.command_name, "/ping")
        self.assertEqual(
            self.controller.names().count("show_user_input"), 1
        )


class DispatcherAliasArgumentTests(unittest.TestCase):
    """T14：别名、参数校验与异常隔离。"""

    def setUp(self) -> None:
        self.registry = CommandRegistry()
        self.invocations: list = []

        def record(invocation, controller) -> None:
            self.invocations.append(invocation)

        self.registry.register(
            make_spec("/context", aliases=("/ctx",), handler=record)
        )

        def boom(invocation, controller) -> None:
            raise RuntimeError("主动失败")

        self.registry.register(make_spec("/boom", handler=boom))
        self.registry.register(
            make_spec(
                "/need-arg",
                handler=record,
                usage="/need-arg <目标>",
                argument_hint="<目标>",
                requires_argument=True,
            )
        )
        self.dispatcher = CommandDispatcher(self.registry)
        self.controller = FakeController()

    def test_canonical_alias_and_case_hit_same_spec(self) -> None:
        """规范名、别名与大写别名命中同一 CommandSpec（spec F6/F10）。"""
        for text in ("/context", "/ctx", "/CTX"):
            self.dispatcher.dispatch(text, self.controller)
        self.assertEqual(len(self.invocations), 3)
        self.assertTrue(all(i.spec.name == "/context" for i in self.invocations))

    def test_invocation_fields(self) -> None:
        self.dispatcher.dispatch("/CTX  AbC 123 ", self.controller)
        inv = self.invocations[0]
        self.assertEqual(inv.typed_name, "/CTX")
        self.assertEqual(inv.matched_name, "/ctx")
        self.assertEqual(inv.spec.name, "/context")
        self.assertEqual(inv.arguments, "AbC 123")

    def test_missing_required_argument_shows_usage(self) -> None:
        """必需参数缺失：显示用法且不调用处理函数、不进入 AI（spec F14）。"""
        result = self.dispatcher.dispatch("/need-arg", self.controller)
        self.assertEqual(result.kind, DispatchKind.ERROR)
        self.assertEqual(self.invocations, [])
        message = self.controller.calls[-1][1]
        self.assertIn("/need-arg <目标>", message)
        self.assertIn("<目标>", message)
        self.assertNotIn("send_user_message", self.controller.names())

    def test_required_argument_provided_executes(self) -> None:
        result = self.dispatcher.dispatch("/need-arg xyz", self.controller)
        self.assertEqual(result.kind, DispatchKind.COMMAND)
        self.assertEqual(self.invocations[0].arguments, "xyz")

    def test_handler_exception_shows_single_local_error(self) -> None:
        """处理函数异常：只显示一次本地错误，不降级为普通消息（spec N5）。"""
        result = self.dispatcher.dispatch("/boom", self.controller)
        self.assertEqual(result.kind, DispatchKind.ERROR)
        self.assertEqual(result.command_name, "/boom")
        self.assertEqual(self.controller.names(), ["show_user_input", "show_message"])
        self.assertIn("主动失败", self.controller.calls[1][1])
        self.assertNotIn("send_user_message", self.controller.names())

    def test_failure_then_normal_message_still_works(self) -> None:
        """命令失败后普通输入仍可正常提交（spec N5/C29）。"""
        self.dispatcher.dispatch("/boom", self.controller)
        result = self.dispatcher.dispatch("正常消息", self.controller)
        self.assertEqual(result.kind, DispatchKind.MESSAGE)
        self.assertIn("send_user_message", self.controller.names())

    def test_extra_arguments_passed_to_handler(self) -> None:
        """多余参数传给处理函数，由无参命令自行忽略（spec F12）。"""
        self.dispatcher.dispatch("/context now", self.controller)
        self.assertEqual(self.invocations[0].arguments, "now")

    def test_deterministic_dispatch(self) -> None:
        """相同输入重复分发，结构化结果一致（spec N3/C20）。"""
        results = [
            self.dispatcher.dispatch("/ctx abc", FakeController())
            for _ in range(3)
        ]
        self.assertEqual(len({(r.kind, r.command_name) for r in results}), 1)


if __name__ == "__main__":
    unittest.main()
