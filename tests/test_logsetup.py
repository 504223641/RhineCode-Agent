"""
日志设施（C5）的护栏。

## 为什么这几条值得写

日志的失败形态全都是**静默**的：handler 没装上、装到了 stderr 上、路径不可写
时把启动搞挂——三种都不会报错，只表现为「我开了 --log-file 却什么都没有」
或「界面突然花了」。因此每条用例钉的都是一个具体的静默形态。

⚠ 每条用例都必须 `reset_for_test()`，否则一条用例装的 FileHandler 会留在根
logger 上，后面每条用例的日志都往那个已被删掉的临时文件里写。
"""

import logging
import tempfile
import unittest
from pathlib import Path

from rhinecode import logsetup


class ConfigureTest(unittest.TestCase):
    """`configure` 的三种入参与三种结局。"""

    def setUp(self) -> None:
        logsetup.reset_for_test()
        self._tmp = tempfile.TemporaryDirectory()
        # ⚠ 注册顺序不能反：addCleanup 是 **LIFO**，后注册的先跑。
        # 反过来的话临时目录会在 handler 还开着文件时被删——Windows 上直接
        # PermissionError（WinError 32：文件被另一进程占用），而那与被测行为无关。
        self.addCleanup(self._tmp.cleanup)
        self.addCleanup(logsetup.reset_for_test)
        self.root = Path(self._tmp.name)

    def test_none_means_off(self) -> None:
        """不给路径 = 不开日志：不装 handler、不建目录、返回 None。"""
        before = len(logging.getLogger().handlers)
        self.assertIsNone(logsetup.configure(None))
        self.assertIsNone(logsetup.log_path())
        self.assertEqual(len(logging.getLogger().handlers), before)

    def test_writes_to_the_given_file(self) -> None:
        """给了路径就真的往那个文件里写，且父目录会被建出来。"""
        target = self.root / "nested" / "run.log"
        self.assertEqual(logsetup.configure(target), target)
        self.assertEqual(logsetup.log_path(), target)

        logging.getLogger("rhinecode.test").info("一条测试日志")
        for handler in logging.getLogger().handlers:
            handler.flush()

        text = target.read_text(encoding="utf-8")
        self.assertIn("一条测试日志", text)
        # 级别与 logger 名要在行里——排查时「谁说的」和「多严重」缺一不可
        self.assertIn("INFO", text)
        self.assertIn("rhinecode.test", text)

    def test_unwritable_path_does_not_raise(self) -> None:
        """
        ⚠ 路径不可写时**必须照常启动**，只提示一行。

        观测设施绝不能反过来阻断被观测的系统——与 `trace.create_recorder`
        同一条纪律。用一个**已存在的文件**当父目录来构造这个情形：
        `mkdir` 会在它上面抛 OSError（跨平台一致，比只读目录可靠）。
        """
        blocker = self.root / "iam-a-file"
        blocker.write_text("x", encoding="utf-8")

        self.assertIsNone(logsetup.configure(blocker / "sub" / "run.log"))
        self.assertIsNone(logsetup.log_path())

    def test_never_attaches_a_stderr_handler(self) -> None:
        """
        ⚠ **本条是这份文件里最要紧的一条。**

        Textual 全程持有终端（备用屏幕缓冲 + raw mode）。往 stderr 挂
        `StreamHandler` 会把日志行直接画在界面上，而用户看到的是「界面花了」
        ——完全不会想到是日志干的。`logging.basicConfig()` 的缺省行为正是挂
        stderr，所以本模块刻意不用它；这条用例钉住的就是「别有人图省事换回
        basicConfig」。

        判据写成「装上的每一个 handler 都是 FileHandler」而不是「没有
        StreamHandler」——后者挡不住 `logging.StreamHandler(sys.stdout)`。
        ⚠ FileHandler 是 StreamHandler 的子类，所以顺序不能反。
        """
        logsetup.configure(self.root / "run.log")
        added = [
            h
            for h in logging.getLogger().handlers
            if not isinstance(h, logging.FileHandler)
        ]
        self.assertEqual(added, [], f"根 logger 上出现了非文件 handler：{added}")


class DefaultPathTest(unittest.TestCase):
    """缺省路径的形态。"""

    def test_shape_and_no_side_effect(self) -> None:
        """
        形如 `<项目根>/.rhinecode/logs/<时间戳>.log`，且**不创建任何目录**。

        建目录是 `configure` 的事（它才知道要不要真写）。这里保持纯计算，
        否则每次只是想算个路径都会在用户项目里留下空目录。
        """
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = logsetup.default_log_path(root)
            self.assertEqual(path.parent, root / ".rhinecode" / "logs")
            self.assertEqual(path.suffix, ".log")
            self.assertFalse(path.parent.exists(), "default_log_path 不该有副作用")

    def test_timestamp_carries_milliseconds(self) -> None:
        """
        时间戳**必须含毫秒**，否则同一秒内启动的两次运行会写进同一个文件，
        两段日志互相穿插、读的人分不开（与 `trace.default_trace_path` 同一条理由）。

        ⚠ 判据钉的是**形状**（`<日期>-<时分秒>-<毫秒>.log`），不是「连算两次
        必然不同」。后者在 Windows 上会假红：系统时钟的实际分辨率约 15 毫秒，
        连算 50 次全部落在同一毫秒是常态——**那不是精度不足，是它跑得太快**。
        实测踩过。
        """
        name = logsetup.default_log_path(Path("/tmp/whatever")).name
        self.assertRegex(name, r"^\d{8}-\d{6}-\d{3}\.log$", f"时间戳形状不对：{name}")


if __name__ == "__main__":
    unittest.main()
