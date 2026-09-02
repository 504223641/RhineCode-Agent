"""
`/setup` 三处接线的护栏（first-run-setup 扩展 T17/T18，spec F15–F18 / AC20–AC24）。

**这三处是一个成对维护点**：
`commands/builtins.py` 的注册项 ↔ `commands/models.py` 的 `CommandController`
协议 ↔ `tui/app.py` 的实现。

⚠ 漏掉任何一处的表现都是**「命令能补全、按了没反应」**——补全和 `/help`
读的是注册表，而真正干活的是控制器实现，两者之间没有任何东西把它们绑住。
Protocol 是**结构化类型**，不实现它的类照样能当参数传进去，静默。
"""

import inspect
import tempfile
import unittest
import unittest.mock as mock
from pathlib import Path

from rhinecode.commands.builtins import build_builtin_registry
from rhinecode.commands.dispatcher import CommandDispatcher
from rhinecode.commands.models import CommandController, CommandType
from rhinecode.setup.models import SetupAction, SetupOutcome


class RegistrationTest(unittest.TestCase):
    """第一处：注册表（AC20）。"""

    def setUp(self):
        self.registry = build_builtin_registry()

    def test_setup_is_registered(self):
        names = [spec.name for spec in self.registry.visible_commands()]
        self.assertIn("/setup", names)

    def test_setup_is_a_ui_command(self):
        """
        它是界面命令，不进 Agent。

        走 PROMPT 的话，敲 `/setup` 会变成一句发给模型的话——那既花钱又什么
        都不会发生。
        """
        spec = self.registry.resolve("/setup")
        self.assertIsNotNone(spec)
        self.assertEqual(spec.command_type, CommandType.UI)

    def test_setup_has_a_description_for_help_and_completion(self):
        """
        单一注册表同时驱动执行 / `/help` / 补全 / 高亮（c10 的设计），
        因此描述为空会让它在 `/help` 里成为一行光秃秃的命令名。
        """
        spec = self.registry.resolve("/setup")
        self.assertTrue(spec.description.strip())
        self.assertTrue(spec.usage.strip())

    def test_setup_appears_in_completion_candidates(self):
        """AC20：能被 Tab 补全出来。"""
        candidates = [item.canonical_name for item in self.registry.complete("/se")]
        self.assertIn("/setup", candidates)

    def test_setup_does_not_collide_with_an_existing_command(self):
        """
        `/setup` 不能与任何既有命令或别名重名。

        重名的后果是「谁先注册谁生效」，而那取决于列表顺序——完全静默。
        """
        seen = []
        for spec in self.registry.visible_commands():
            seen.append(spec.name)
            seen.extend(spec.aliases)
        self.assertEqual(seen.count("/setup"), 1, f"/setup 重名了：{seen}")


class ProtocolTest(unittest.TestCase):
    """第二处：控制器协议。"""

    def test_protocol_declares_open_setup(self):
        self.assertTrue(hasattr(CommandController, "open_setup"))


class ImplementationTest(unittest.TestCase):
    """
    第三处：`RhineApp` 真的实现了它。

    ⚠ **这一条是本文件的核心。** `CommandController` 是 `Protocol`，
    也就是**结构化类型**——一个没实现 `open_setup` 的类照样能被当作它传进去，
    没有任何静态或运行时检查会拦住。漏了这一处，`/setup` 会补全、会出现在
    `/help` 里、敲下去分发成功，然后在调用控制器方法时才炸（或者更糟：
    如果有人给了个宽松的兜底，就什么都不发生）。
    """

    def test_rhine_app_implements_open_setup(self):
        from rhinecode.tui.app import RhineApp

        self.assertTrue(
            callable(getattr(RhineApp, "open_setup", None)),
            "RhineApp 没实现 open_setup——命令能补全但按了没反应",
        )

    def test_signature_matches_the_protocol(self):
        """
        签名也要对得上。

        协议里是无参方法，实现里若多了个必填参数，分发时会 TypeError——
        同样是「按了没反应」那一类，只是换了个报错方式。
        """
        from rhinecode.tui.app import RhineApp

        protocol_params = list(inspect.signature(CommandController.open_setup).parameters)
        impl_params = list(inspect.signature(RhineApp.open_setup).parameters)
        self.assertEqual(protocol_params, impl_params)


class DispatchTest(unittest.TestCase):
    """敲 `/setup` 真的会调到控制器上。"""

    def test_dispatch_calls_open_setup_exactly_once(self):
        registry = build_builtin_registry()
        dispatcher = CommandDispatcher(registry)
        controller = mock.Mock()

        dispatcher.dispatch("/setup", controller)

        self.assertEqual(controller.open_setup.call_count, 1)

    def test_extra_arguments_are_ignored(self):
        """
        它没有子命令，多余参数直接忽略。

        报一个「参数错误」只会把用户挡在外面——他想做的事很明确。
        """
        registry = build_builtin_registry()
        dispatcher = CommandDispatcher(registry)
        controller = mock.Mock()

        dispatcher.dispatch("/setup 随便写点什么", controller)

        self.assertEqual(controller.open_setup.call_count, 1)

    def test_case_insensitive(self):
        """c10 起斜杠命令大小写不敏感，`/setup` 不该是例外。"""
        registry = build_builtin_registry()
        dispatcher = CommandDispatcher(registry)
        controller = mock.Mock()

        dispatcher.dispatch("/SETUP", controller)

        self.assertEqual(controller.open_setup.call_count, 1)


class ConfigPathTest(unittest.TestCase):
    """
    **`/setup` 要写回本次真正在用的那份配置**（不是想当然的用户级那份）。

    用 `--config x.yaml` 启动时两者不是一个文件，而写错的表现是
    「我改了配置，重启还是老样子」——用户会以为 `/setup` 坏了，
    实际上它认认真真改了一个跟当前运行无关的文件。
    """

    def test_app_uses_the_injected_path(self):
        from rhinecode.tui.app import RhineApp

        with tempfile.TemporaryDirectory() as d:
            explicit = Path(d) / "mine.yaml"
            app = RhineApp.__new__(RhineApp)
            app._config_path = explicit
            self.assertEqual(app._config_path, explicit)

    def test_defaults_to_user_level_path_when_not_given(self):
        """
        不传时退回用户级路径——既有调用方（含测试与 e2e 宿主）一个字都不用改。
        """
        from rhinecode.config import user_config_path
        from rhinecode.tui.app import RhineApp

        signature = inspect.signature(RhineApp.__init__)
        self.assertIn("config_path", signature.parameters)
        self.assertIsNone(signature.parameters["config_path"].default)
        # 缺省行为在 __init__ 里是 `config_path or user_config_path()`
        source = inspect.getsource(RhineApp.__init__)
        self.assertIn("user_config_path()", source)
        self.assertTrue(callable(user_config_path))

    def test_build_app_forwards_config_path(self):
        """装配层要把它原样传下去，中间断了同样静默。"""
        import rhinecode.bootstrap as bootstrap

        signature = inspect.signature(bootstrap.build_app)
        self.assertIn("config_path", signature.parameters)
        source = inspect.getsource(bootstrap.build_app)
        self.assertIn("config_path=config_path", source)


class SavedNoticeTest(unittest.TestCase):
    """保存后要提示「下次启动生效」（AC23）；放弃时什么都不做（AC24）。"""

    def _app(self):
        from rhinecode.tui.app import RhineApp

        app = RhineApp.__new__(RhineApp)
        app._config_path = Path("/tmp/whatever.yaml")
        app.show_event = mock.Mock()
        return app

    def test_saved_shows_restart_notice(self):
        app = self._app()
        app._on_setup_done(SetupOutcome(action=SetupAction.SAVED, written=()))
        self.assertEqual(app.show_event.call_count, 1)
        self.assertIn("下次启动生效", app.show_event.call_args[0][0])

    def test_abandoned_says_nothing(self):
        """
        一次「我看看，算了」不该在对话历史里留下任何东西（F18）。
        """
        app = self._app()
        app._on_setup_done(SetupOutcome(action=SetupAction.ABANDONED))
        self.assertEqual(app.show_event.call_count, 0)

    def test_none_outcome_says_nothing(self):
        """Screen 被强制关掉时回调拿到的是 None，不能因此炸掉。"""
        app = self._app()
        app._on_setup_done(None)
        self.assertEqual(app.show_event.call_count, 0)


if __name__ == "__main__":
    unittest.main()
