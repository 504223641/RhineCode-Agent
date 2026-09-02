"""
启动入口分支的护栏（first-run-setup 扩展 T15/T16，spec F1/F2/F5 / AC1–AC6）。

**本文件断言的是 `run_setup` 的调用次数，不是「最终跑起来没有」。**
只断言后者的话，「没弹向导」与「弹了但立刻返回」给出的结果相同，
而那正是要区分的两种情况——这条写法抄自 C16 那批「分类器有没有被调用」的护栏。

⚠ **不要用 `HOME` 环境变量去伪造用户目录，Windows 上不生效。**
`Path.home()` 在 Windows 上读的是 `USERPROFILE`。实测踩过：设了 `HOME`
之后进程照样读到真实的 `~/.rhinecode/config.yaml`，于是「非交互不弹向导」
那条用例实际上把真的 TUI 起了起来、一直挂到超时。本文件因此**直接 patch
`user_config_path`**，不碰环境变量。
"""

import sys
import tempfile
import unittest
import unittest.mock as mock
from pathlib import Path

from rhinecode.config import PLACEHOLDER_API_KEY, _CONFIG_TEMPLATE
from rhinecode.setup.models import SetupAction, SetupOutcome

_GOOD = (
    "protocol: deepseek\n"
    "model: deepseek-v4-flash\n"
    "base_url: https://api.deepseek.com\n"
    "api_key: sk-real-looking-key\n"
)


class _EntryCase(unittest.TestCase):
    """
    公共夹具：临时用户目录 + 把 `build_app` 与向导都换成假的。

    `build_app` 必须换掉——真跑装配会连 MCP、起 Provider、开会话存档，
    而本文件要验的只是「入口的分支走对没有」。
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.home = Path(self._tmp.name)
        self.config_path = self.home / ".rhinecode" / "config.yaml"

        self.run_setup = mock.Mock(
            return_value=SetupOutcome(action=SetupAction.SAVED, written=())
        )
        self.build_app = mock.Mock()
        self.build_app.return_value.app.return_code = 0

        self._patchers = [
            mock.patch("rhinecode.__main__.user_config_path", return_value=self.config_path),
            mock.patch("rhinecode.__main__.run_setup", self.run_setup),
            mock.patch("rhinecode.__main__.build_app", self.build_app),
            # 三份可选模板落到临时目录里，别污染真实用户目录
            mock.patch(
                "rhinecode.permission.config.user_config_path",
                return_value=self.home / ".rhinecode" / "permissions.yaml",
            ),
            mock.patch(
                "rhinecode.mcp.config.user_config_path",
                return_value=self.home / ".rhinecode" / "mcp.yaml",
            ),
            mock.patch(
                "rhinecode.hooks.config.user_config_path",
                return_value=self.home / ".rhinecode" / "hooks.yaml",
            ),
        ]
        for patcher in self._patchers:
            patcher.start()

    def tearDown(self):
        for patcher in self._patchers:
            patcher.stop()
        self._tmp.cleanup()

    def run_main(self, *, interactive: bool, argv=("rhine",)):
        """
        跑一次 `main()`，返回退出码（正常返回记 0）。

        `_stdin_is_interactive` 被直接替换，而不是去伪造 `sys.stdin`——
        那个函数本身另有专门的用例。
        """
        from rhinecode.__main__ import main

        with mock.patch("rhinecode.__main__._stdin_is_interactive", return_value=interactive):
            with mock.patch.object(sys, "argv", list(argv)):
                try:
                    main()
                except SystemExit as exc:
                    return exc.code if exc.code is not None else 0
        return 0

    def write_config(self, text: str):
        self.config_path.parent.mkdir(parents=True, exist_ok=True)
        self.config_path.write_text(text, encoding="utf-8")


class InteractiveTest(_EntryCase):
    """有终端时该弹向导（AC1–AC4）。"""

    def test_missing_config_opens_wizard(self):
        """AC1：配置不存在 → 向导被调用**恰好一次**。"""
        self.run_main(interactive=True)
        self.assertEqual(self.run_setup.call_count, 1)

    def test_placeholder_key_opens_wizard(self):
        """AC2：占位符 key → 同样弹向导，而不是打印一句话退出。"""
        self.write_config(_GOOD.replace("sk-real-looking-key", PLACEHOLDER_API_KEY))
        self.run_main(interactive=True)
        self.assertEqual(self.run_setup.call_count, 1)

    def test_broken_config_opens_wizard(self):
        """AC3：坏 YAML → 同样弹向导。"""
        self.write_config("[unclosed\n :: :\n")
        self.run_main(interactive=True)
        self.assertEqual(self.run_setup.call_count, 1)

    def test_good_config_does_not_open_wizard(self):
        """
        AC4：配置可用 → 向导**零次调用**，直接装配。

        这是「不打扰用户」那一半，与上面三条同等重要。
        """
        self.write_config(_GOOD)
        self.run_main(interactive=True)
        self.assertEqual(self.run_setup.call_count, 0)
        self.assertEqual(self.build_app.call_count, 1)

    def test_saved_continues_into_the_app(self):
        """
        AC9：向导保存后**不退出进程**，直接往下装配。

        这是本扩展相对现状最大的行为改变。
        """

        def _save_a_real_config(path, *args, **kwargs):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(_GOOD, encoding="utf-8")
            return SetupOutcome(action=SetupAction.SAVED, written=(path,))

        self.run_setup.side_effect = _save_a_real_config
        code = self.run_main(interactive=True)
        self.assertEqual(code, 0)
        self.assertEqual(self.build_app.call_count, 1, "保存之后应当继续装配")

    def test_abandoned_exits_zero_without_starting_app(self):
        """AC7：放弃 → 退出码 0，**不装配**。"""
        self.run_setup.return_value = SetupOutcome(action=SetupAction.ABANDONED)
        code = self.run_main(interactive=True)
        self.assertEqual(code, 0)
        self.assertEqual(self.build_app.call_count, 0)

    def test_wizard_output_is_still_validated_by_load(self):
        """
        向导写完之后**照常走 `load()`**，不跳过校验。

        向导不做「我写的一定对」的假设。这里让它写一份缺字段的配置，
        入口应当按配置错误退出，而不是带着半份配置去装配。
        """

        def _save_a_broken_config(path, *args, **kwargs):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("protocol: deepseek\n", encoding="utf-8")
            return SetupOutcome(action=SetupAction.SAVED, written=(path,))

        self.run_setup.side_effect = _save_a_broken_config
        code = self.run_main(interactive=True)
        self.assertEqual(code, 1)
        self.assertEqual(self.build_app.call_count, 0)


class NonInteractiveTest(_EntryCase):
    """
    **没有终端时一次都不许弹向导**（AC5/AC6，spec F2）。

    破了这条，CI、管道、`&&` 链、任何包装脚本都会断——它们没有终端可弹向导，
    只会永远挂在那儿。
    """

    def test_missing_config_does_not_open_wizard(self):
        code = self.run_main(interactive=False)
        self.assertEqual(self.run_setup.call_count, 0, "非交互环境下弹了向导")
        self.assertEqual(code, 0, "老行为是提示一句、退出码 0")

    def test_missing_config_still_writes_templates(self):
        """模板照常生成——这一半行为一个字没改。"""
        self.run_main(interactive=False)
        for name in ("config.yaml", "permissions.yaml", "mcp.yaml", "hooks.yaml"):
            self.assertTrue((self.home / ".rhinecode" / name).exists(), name)

    def test_placeholder_key_exits_one(self):
        """
        已有模板但没填 key：老行为是**退出码 1**（与「刚生成模板」的 0 不同）。

        两个退出码的区别是老代码里就有的，本扩展不许把它们抹平。
        """
        self.write_config(_CONFIG_TEMPLATE)
        code = self.run_main(interactive=False)
        self.assertEqual(self.run_setup.call_count, 0)
        self.assertEqual(code, 1)

    def test_broken_config_exits_one(self):
        self.write_config("[unclosed\n :: :\n")
        code = self.run_main(interactive=False)
        self.assertEqual(self.run_setup.call_count, 0)
        self.assertEqual(code, 1)

    def test_good_config_starts_normally(self):
        """非交互 + 配置可用 = 照常跑，这是 CI 里最常见的形态。"""
        self.write_config(_GOOD)
        self.run_main(interactive=False)
        self.assertEqual(self.run_setup.call_count, 0)
        self.assertEqual(self.build_app.call_count, 1)


class ExplicitConfigTest(_EntryCase):
    """
    **显式 `--config` 一律不弹向导**（AC6）。

    用户点名了一个文件，那就以它为准；指到一个不存在的文件是错误，
    不是「该配置一下了」——擅自造文件或弹向导都会让 `--config` 这个开关
    失去意义。
    """

    def test_missing_explicit_config_is_an_error(self):
        missing = self.home / "nope.yaml"
        code = self.run_main(interactive=True, argv=("rhine", "--config", str(missing)))
        self.assertEqual(self.run_setup.call_count, 0, "显式 --config 时弹了向导")
        self.assertEqual(code, 1)
        self.assertFalse(missing.exists(), "不该擅自造文件")

    def test_explicit_good_config_starts_normally(self):
        path = self.home / "mine.yaml"
        path.write_text(_GOOD, encoding="utf-8")
        self.run_main(interactive=True, argv=("rhine", "--config", str(path)))
        self.assertEqual(self.run_setup.call_count, 0)
        self.assertEqual(self.build_app.call_count, 1)


class StdinDetectionTest(unittest.TestCase):
    """
    `_stdin_is_interactive` 自身。

    它是「该不该弹向导」的唯一闸门，因此判不准时必须选那条**逐字保持
    老行为**的路（返回假）。
    """

    def test_tty(self):
        from rhinecode.__main__ import _stdin_is_interactive

        with mock.patch.object(sys, "stdin", mock.Mock(isatty=lambda: True)):
            self.assertTrue(_stdin_is_interactive())

    def test_not_tty(self):
        from rhinecode.__main__ import _stdin_is_interactive

        with mock.patch.object(sys, "stdin", mock.Mock(isatty=lambda: False)):
            self.assertFalse(_stdin_is_interactive())

    def test_stdin_is_none(self):
        """Windows 上用 pythonw 之类的无控制台方式启动时 `sys.stdin` 是 None。"""
        from rhinecode.__main__ import _stdin_is_interactive

        with mock.patch.object(sys, "stdin", None):
            self.assertFalse(_stdin_is_interactive())

    def test_isatty_raises(self):
        """已关闭的流上 `isatty()` 会抛 ValueError。"""
        from rhinecode.__main__ import _stdin_is_interactive

        broken = mock.Mock()
        broken.isatty.side_effect = ValueError("I/O operation on closed file")
        with mock.patch.object(sys, "stdin", broken):
            self.assertFalse(_stdin_is_interactive())


class NoReverseDependencyTest(unittest.TestCase):
    """
    **`setup/` 不许反向依赖上层**（spec N3）。

    它跑在装配之前，依赖 `bootstrap` / `conversation` / `tui` / `provider`
    会成环。

    ⚠ 判据用 `ast` 解析真实的 import 语句，**不是 grep**——包的 docstring 里
    就写着「绝不 import bootstrap / conversation / tui / provider」这句话，
    一条朴素的文本搜索会被它自己的说明文字判红。
    """

    def test_setup_package_imports_nothing_from_upper_layers(self):
        import ast

        forbidden = {"bootstrap", "conversation", "tui", "provider", "agent", "commands"}
        offenders = []
        for path in Path("rhinecode/setup").glob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                names = []
                if isinstance(node, ast.Import):
                    names = [alias.name for alias in node.names]
                elif isinstance(node, ast.ImportFrom) and node.module:
                    names = [node.module]
                for name in names:
                    parts = name.split(".")
                    if parts[:1] == ["rhinecode"] and len(parts) > 1 and parts[1] in forbidden:
                        offenders.append(f"{path.name}: {name}")
        self.assertEqual(offenders, [], f"setup/ 反向依赖了上层：{offenders}")


if __name__ == "__main__":
    unittest.main()
