"""
退出前结算待决交互（C10-a）的护栏。

## 这条缺口的实测后果比原报告严重一档

原报告说后果是「线程停到天亮」。R3 实跑出来的是**整个进程退不掉**：

    ⚠ app.run() 在 25 秒后仍未返回 —— 退出被那个 wait() 挡住了
    ThreadPoolExecutor 线程 daemon = False

链条：`run_worker(thread=True)` 走的是 **asyncio 的默认线程池**，它的线程
**不是 daemon**；`asyncio.run()` 收尾时调 `loop.shutdown_default_executor()`，
而 **Python 3.11 的这个方法没有 timeout 参数**（3.12 才加），于是它**无限期
等待**那个停在 `Event.wait()` 上的线程。结果是 `app.run()` 永不返回 →
`__main__.py` 的 `finally: result.cleanup()` 永不执行 → MCP 子进程不回收、
会话锁不释放、trace 句柄不关闭。用户看到的是「按了退出，程序卡死了」。

## ⚠ 判据为什么不是「起一个真 app 然后按两次 Ctrl+C」

e2e 宿主**复现不了这一条**——它走 `app.run_test()`，收尾语义与 `app.run()`
不同，R3 实测宿主干净退出了。而起一个真 `app.run()` 再让它挂住，本身就是一条
「不修好就会挂住 25 秒」的用例。

所以这里钉的是**因果链上那一环**：退出分支必须先走结算、结算必须 `set()` 那个
事件。两条合起来就是「wait 会返回」。
"""

import threading
import unittest
from unittest.mock import MagicMock

from rhinecode.tui.app import RhineApp


def _shell() -> RhineApp:
    """造一个不挂载的 app 壳子，只为调那几个方法。"""
    return RhineApp.__new__(RhineApp)


class SettleBeforeQuitTest(unittest.TestCase):
    def setUp(self) -> None:
        self.app = _shell()
        self.app._pending_interaction = None
        self.app._session_panel_active = False
        self.app._resolve_interaction = MagicMock()
        self.app._settle_session = MagicMock()

    def test_pending_confirm_is_settled_with_none(self) -> None:
        """
        面板挂着时，退出前用 `None` 结算它。

        `None` 在三类面板上的语义都已经是「取消 / 拒绝 / 跳过」——**这个方向
        是本条的要害**，见下面那条反证。
        """
        self.app._pending_interaction = {"kind": "confirm"}
        self.app._settle_pending_before_quit()

        self.app._resolve_interaction.assert_called_once()
        args, kwargs = self.app._resolve_interaction.call_args
        self.assertIsNone(args[0])
        self.assertEqual(kwargs.get("source"), "shutdown")

    def test_session_panel_is_settled_too(self) -> None:
        """
        ⚠ **会话选择面板走的是另一条结算路径，只修一条是半个修复。**

        它由主线程发起、没有 Worker 在阻塞等待，所以埋点只能在结算处做，
        而 `_settle_session` 是它**唯一**的入口（走别处会丢埋点与幂等守卫）。
        """
        self.app._session_panel_active = True
        self.app._settle_pending_before_quit()

        self.app._settle_session.assert_called_once()
        args, kwargs = self.app._settle_session.call_args
        self.assertIsNone(args[0])
        self.assertEqual(kwargs.get("source"), "shutdown")

    def test_nothing_pending_settles_nothing(self) -> None:
        """
        没有面板挂着时一条都不结算——否则每次正常退出都会凭空多埋一条交互事件，
        破坏 trace「四类面板各产出恰好一条」的口径（trace AC16）。
        """
        self.app._settle_pending_before_quit()
        self.app._resolve_interaction.assert_not_called()
        self.app._settle_session.assert_not_called()

    def test_quit_path_calls_it_before_exit(self) -> None:
        """
        ⚠ **顺序判据：结算必须发生在 `exit()` 之前。**

        反过来（先 exit 再结算）**在单测里看起来一样**——两个 mock 都被调到了
        ——但真实运行时 `exit()` 之后的代码未必还跑得到，而这条缺口的整个后果
        就发生在那之后。所以判据必须是**顺序**，不能只是「两个都调了」。
        """
        calls: list[str] = []
        app = _shell()
        app._copy_selection_if_any = lambda: False
        app._last_quit_request = 1e9  # 让「距上次按下 ≤ 阈值」成立
        app._settle_pending_before_quit = lambda: calls.append("settle")
        app.exit = lambda: calls.append("exit")

        from time import monotonic

        app._last_quit_request = monotonic()
        app.action_request_quit()

        self.assertEqual(calls, ["settle", "exit"])

    def test_first_press_settles_nothing(self) -> None:
        """
        反证：**第一次**按 Ctrl+C 只是挂提示，不许结算任何面板。

        少了这条反证，一个「每次按 Ctrl+C 都结算」的实现会全绿——而它的效果是
        用户按一下 Ctrl+C 就把面板给取消了，那是个新 bug。
        """
        calls: list[str] = []
        app = _shell()
        app._copy_selection_if_any = lambda: False
        app._last_quit_request = 0.0  # 上次按下在很久以前
        app._settle_pending_before_quit = lambda: calls.append("settle")
        app.exit = lambda: calls.append("exit")
        app._arm_quit_hint = lambda: calls.append("hint")

        app.action_request_quit()
        self.assertEqual(calls, ["hint"])


class ResolveActuallyWakesTheWaiterTest(unittest.TestCase):
    """
    因果链的另一环：`_resolve_interaction` 真的会 `set()` 那个事件。

    ⚠ 这条**必须用真线程**，不能只断言「`event.set` 被调过」。
    停在 `Event.wait()` 上的是**另一个**线程，而「set 被调了」与「那个线程真的
    醒了」是两件事——本项目在死锁护栏上踩过同型的坑（成对维护点里记着
    「死锁护栏必须用完成计数而不是布尔标志，同线程版本在 `RLock` 下会静默通过」）。
    """

    def test_a_blocked_thread_is_released(self) -> None:
        app = _shell()
        box = {"kind": "confirm", "event": threading.Event(), "result": "未结算"}
        app._pending_interaction = box
        app._clarify_free_text = False

        # `_resolve_interaction` 的收尾会去 query 面板；壳子上没有 DOM，
        # 用假的顶掉——本条只验「事件被 set、结果被写入」这一段。
        app.query_one = MagicMock()
        app._recorder = MagicMock()

        woke = threading.Event()

        def waiter() -> None:
            box["event"].wait()
            woke.set()

        t = threading.Thread(target=waiter, daemon=True)
        t.start()

        app._resolve_interaction(None, source="shutdown")

        self.assertTrue(
            woke.wait(timeout=5.0),
            "结算之后被阻塞的线程仍然没醒——这正是让整个进程退不掉的那一环",
        )
        self.assertIsNone(box["result"])
        self.assertEqual(box["source"], "shutdown")


class SourceValueIsRegisteredTest(unittest.TestCase):
    """
    ⚠ **成对维护点：新增结算来源取值要同步五处。**

    `paired-maintenance` 里那条写着「产品侧交互结算点新增来源取值 →
    `tui/app.py` 三处 + `tests/e2e/protocol.py` 的取值集合 + 断言词汇」。
    漏掉驱动设施那一侧的表现是「驱动设施认不出这个来源」，而那不报错。
    """

    def test_shutdown_is_documented_on_both_sides(self) -> None:
        import pathlib

        root = pathlib.Path(__file__).resolve().parents[1]
        product = (root / "rhinecode" / "tui" / "app.py").read_text(encoding="utf-8")
        driver = (root / "tests" / "e2e" / "protocol.py").read_text(encoding="utf-8")

        self.assertIn("`shutdown`", product, "产品侧的取值集合没登记 shutdown")
        self.assertIn("shutdown", driver, "驱动设施那一侧的取值集合没登记 shutdown")

    def test_shutdown_is_not_merged_with_driver_forced(self) -> None:
        """
        反证：不许图省事复用 `driver_forced`。

        那条是**外部驱动者**掐掉的，这条是**用户按了两次 Ctrl+C**。合并之后，
        一次真人退出会在审计记录里显示成「测试设施干的」。
        """
        import inspect

        source = inspect.getsource(RhineApp._settle_pending_before_quit)
        self.assertIn('source="shutdown"', source)
        self.assertNotIn("driver_forced", source)


if __name__ == "__main__":
    unittest.main()
