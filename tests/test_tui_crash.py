"""
崩溃处理（C6）的护栏。

## 本条的来历，以及原报告错在哪

`README.md` 的 C6 原文是「`app.run()` 没有兜底，崩溃可能把用户终端搞坏」，
建议「包一层 catch-all」。R3 实跑把这条**推翻了两半**：

- **终端不会坏。** `App.run_async` 的 `finally: await asyncio.shield(app._shutdown())`
  无条件执行，两种崩溃形态下备用屏幕缓冲与 raw mode 都复位了。
- **catch-all 兜不住主要形态。** 绝大多数崩溃发生在消息处理器与事件回调里，
  Textual 的 `_handle_exception` 会接住它、把 app 关掉，`app.run()` **正常返回**。

真缺口换成两条，本文件各钉一组：**崩溃后进程退出码是 0**（`ExitCodeTest`）、
**带 locals 的完整回溯甩给用户且没有日志**（`CrashRenderingTest`）。
"""

import logging
import tempfile
import unittest
from pathlib import Path

from textual.app import App

from rhinecode import logsetup


class TextualHookSurfaceTest(unittest.TestCase):
    """
    ⚠ **本组钉的是「我们的覆写还接在上游的那个点上」。**

    `RhineApp` 覆写了 `_handle_exception` 与 `_fatal_error` 两个方法。它们是
    Textual 的私有名字——上游哪天改名，我们的覆写就变成**谁也不调的死代码**，
    而后果是**掩码静默消失**：崩溃时又开始把每一帧的 locals（可能含明文
    `api_key`）打给用户，且没有任何东西报错。

    这正是本项目最忌讳的形态，所以两个名字都要有一条断言钉着，
    textual 升级掉它们时当场红。
    """

    def test_base_class_still_has_the_hooks_we_override(self) -> None:
        for name in ("_handle_exception", "_fatal_error"):
            with self.subTest(hook=name):
                self.assertTrue(
                    hasattr(App, name),
                    f"textual 的 App 上没有 {name} 了——RhineApp 那个覆写已经失效，"
                    f"崩溃展示会退回带 locals 的完整回溯（可能含明文 api_key）",
                )

    def test_panic_is_still_available(self) -> None:
        """
        `_fatal_error` 的覆写只用公开 API `panic()` 做展示，不碰任何私有字段
        （`_exit_renderables` / `_close_messages_no_wait`）。这条钉住那个依赖。
        """
        self.assertTrue(callable(getattr(App, "panic", None)))


class CrashRenderingTest(unittest.TestCase):
    """崩溃时终端上留下什么、日志里留下什么。"""

    def setUp(self) -> None:
        logsetup.reset_for_test()
        self._tmp = tempfile.TemporaryDirectory()
        # ⚠ LIFO：临时目录必须后清，否则 handler 还开着文件就被删（Windows 上直接报错）
        self.addCleanup(self._tmp.cleanup)
        self.addCleanup(logsetup.reset_for_test)
        self.root = Path(self._tmp.name)

    def _make_app(self):
        """造一个不挂载的 RhineApp 壳子，只为调它那两个覆写。"""
        from rhinecode.tui.app import RhineApp

        return RhineApp.__new__(RhineApp)

    def test_traceback_goes_to_the_log_not_the_screen(self) -> None:
        """
        `_handle_exception` 把**完整堆栈**写进日志文件，并**照常交给基类**。

        ⚠ 这条**真的调产品那个覆写**，而不是在用例里把同一段 logging 再写一遍
        ——后者只能证明「logging 能用」，证明不了产品接对了线。代价是要给
        `__new__` 出来的壳子补上基类簿记要读的四个字段，那几行就是本条的成本。

        判据有两半，缺一不可：
        ① 日志里有异常类型、异常消息与我们埋的那句中文（`%s` 忘了填就红）；
        ② `_return_code` 被基类置成 1——那是 C6 缺口一的**数据来源**，
           `super()` 被谁顺手删掉时这一半会红。
        """
        log = self.root / "run.log"
        logsetup.configure(log)

        app = self._make_app()
        # 基类 `_handle_exception` 要读的四个字段，`__new__` 的壳子上没有。
        app._return_code = 0
        app._exception = None
        app._exception_event = type("_E", (), {"set": lambda self: None})()
        app.bell = lambda: None
        app.panic = lambda *args: None

        try:
            raise RuntimeError("模拟一次未捕获异常")
        except RuntimeError as e:
            app._handle_exception(e)

        for handler in logging.getLogger().handlers:
            handler.flush()

        text = log.read_text(encoding="utf-8")
        self.assertIn("未捕获异常", text)
        self.assertIn("RuntimeError", text)
        self.assertIn("模拟一次未捕获异常", text)
        # ② 基类簿记照常发生——退出码那条缺口全靠它
        self.assertEqual(app._return_code, 1, "super()._handle_exception 没被调到")

    def test_screen_message_points_at_the_log_when_there_is_one(self) -> None:
        """
        开了 `--log-file` 时，终端那句话必须**报出路径**——否则「完整堆栈见 xxx」
        等于没说，用户不知道去哪找。
        """
        log = self.root / "run.log"
        logsetup.configure(log)

        captured: list[str] = []
        app = self._make_app()
        app.bell = lambda: None
        app.panic = lambda *args: captured.extend(str(a) for a in args)
        app._fatal_error()

        self.assertEqual(len(captured), 1)
        self.assertIn(str(log), captured[0])

    def test_screen_message_tells_you_how_to_get_one_when_there_is_no_log(self) -> None:
        """
        ⚠ **反证：没开日志时不能只说「完整堆栈见 None」。**

        缺省就是没开日志，所以这条才是绝大多数用户实际会看到的那句。它必须给出
        「下一步做什么」——这是 C7 那张错误分类表贯穿全表的原则，同样适用于此。
        """
        captured: list[str] = []
        app = self._make_app()
        app.bell = lambda: None
        app.panic = lambda *args: captured.extend(str(a) for a in args)
        app._fatal_error()

        self.assertEqual(len(captured), 1)
        self.assertNotIn("None", captured[0])
        self.assertIn("--log-file", captured[0])

    def test_message_carries_the_text_prefix_not_a_symbol(self) -> None:
        """
        文案靠「错误：」**文字前缀**辨认，不引入新的图形符号
        （tui-display F29 的符号白名单）。崩溃退出时颜色未必还在，
        文字前缀是它唯一的依靠。
        """
        captured: list[str] = []
        app = self._make_app()
        app.bell = lambda: None
        app.panic = lambda *args: captured.extend(str(a) for a in args)
        app._fatal_error()

        self.assertTrue(captured[0].startswith("错误："), captured[0])


class ExitCodeTest(unittest.TestCase):
    """
    C6 缺口一：崩溃后进程退出码必须非 0。

    ⚠ 这里**不起真进程**（起一个真 TUI 再让它崩，代价与 flaky 都不划算），
    钉的是入口那段代码的**结构**：`sys.exit` 必须排在 `finally: cleanup()`
    之后，且读的是 `app.return_code`。

    结构断言在这里是够用的，因为这条缺口的本质就是「那两行代码不存在」——
    R3 实测的 `app.return_code = 1` 而进程退出码 0，成因正是入口从不读它。
    """

    def test_entry_reads_return_code_after_cleanup(self) -> None:
        source = (
            Path(__file__).resolve().parents[1] / "rhinecode" / "__main__.py"
        ).read_text(encoding="utf-8")

        self.assertIn("result.app.return_code", source, "入口没有读 app.return_code")

        cleanup_at = source.index("finally:\n        result.cleanup()")
        exit_at = source.index("sys.exit(result.app.return_code)")
        self.assertLess(
            cleanup_at,
            exit_at,
            "sys.exit 必须排在 finally: result.cleanup() **之后**——"
            "SystemExit 会跳过清理，MCP 子进程、会话锁、trace 句柄全都留着",
        )


if __name__ == "__main__":
    unittest.main()
