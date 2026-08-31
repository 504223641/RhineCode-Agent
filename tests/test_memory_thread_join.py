"""
记忆线程的退出收尾（C10-c）的护栏。

## 这条缺口是什么

`_update_memories` 跑在一个 **daemon 线程**里，而它写盘用的是
`Path.write_text`——先以 `"w"` 打开（**当场截断**）再写。CPython 在解释器终结
开始后，daemon 线程一旦尝试获取 GIL 就会被直接结束，**可以停在这两步之间**。

R3 构造实测（在截断与写入之间插一个 2 毫秒的 sleep 放大窗口）：

    open(mode='w') 之后、还没写入时: ''
    → 只要线程停在 open 与 write 之间，这条记忆就没了
    12 次里有 10 次落在「已截断、未写入」的窗口内

丢的是**持久化数据且不可恢复**。连带的第二个后果更难查：`_apply_actions` 的
`finally: lockfile.release(lock)` 同样跑不到，`.lock` 留在原地，而
`MEMORY_LOCK_STALE = 600.0`——**下一次运行的记忆更新会被挡最多 10 分钟**，
用户看到的现象是「记忆功能好像不工作了，过一会儿又好了」。

⚠ 那 10/12 是**刻意放大过的命中率，不是真实概率**：真实窗口很窄（时间几乎全花在
LLM 调用上，写盘只有毫秒级）。但它是「低概率 × 不可恢复」，与「高概率 × 可恢复」
不是一回事。

## ⚠ 正确的组合是 daemon + join(timeout)，三种写错方式各有一条反证

- 改成**非 daemon** → 一次卡住的 LLM 调用会让程序退不掉（变成 C10-a 那个形态）；
- **join 不给 timeout** → 同上；
- **顺手把子 Agent 线程也改了** → 那里的取舍是**明确记录过**的，且它写的是
  临时状态不是持久化数据。
"""

import threading
import time
import unittest
from unittest.mock import MagicMock

from rhinecode.memory.manager import MemoryManager


def _shell() -> MemoryManager:
    """造一个不初始化的壳子，只为调 `close()`。"""
    manager = MemoryManager.__new__(MemoryManager)
    manager._memory_thread = None
    manager._session = MagicMock()
    return manager


class CloseJoinsTheThreadTest(unittest.TestCase):
    def test_close_waits_for_a_running_memory_thread(self) -> None:
        """
        `close()` 会等还在跑的记忆线程收尾。

        判据用**真线程**：让它睡一小段再置一个标志，然后断言 `close()` 返回时
        标志已经置上了。断言「join 被调过」是不够的——那证明不了它真的等到了。
        """
        manager = _shell()
        finished = threading.Event()

        def body() -> None:
            time.sleep(0.15)
            finished.set()

        thread = threading.Thread(target=body, daemon=True)
        manager._memory_thread = thread
        thread.start()

        manager.close()

        self.assertTrue(finished.is_set(), "close() 没等记忆线程写完就返回了")
        manager._session.release.assert_called_once()

    def test_close_gives_up_after_the_timeout(self) -> None:
        """
        ⚠ **反证：等不到也必须放手，绝不能无限等。**

        `_update_memories` 里那次 LLM 调用可能卡住。无限 join 会让程序退不掉，
        而那正好变成 C10-a 那个形态（用户按了退出、程序卡死）——**用一个缺陷去
        换另一个同型的缺陷**。

        判据同时钉住两件事：① 真的在超时附近返回了（不是无限等）；
        ② 会话锁**照常释放**（超时不该把后面的收尾一起跳过）。
        """
        manager = _shell()
        manager.MEMORY_JOIN_TIMEOUT = 0.2

        stop = threading.Event()
        thread = threading.Thread(target=stop.wait, daemon=True)
        manager._memory_thread = thread
        thread.start()
        self.addCleanup(stop.set)

        started = time.monotonic()
        manager.close()
        elapsed = time.monotonic() - started

        self.assertLess(elapsed, 2.0, f"close() 等了 {elapsed:.1f}s，看起来是无限等")
        self.assertGreaterEqual(elapsed, 0.15, "根本没等，join 可能没接上")
        manager._session.release.assert_called_once()

    def test_close_without_a_thread_is_fine(self) -> None:
        """从没触发过记忆更新时照常退出。"""
        manager = _shell()
        manager.close()
        manager._session.release.assert_called_once()

    def test_timeout_constant_is_bounded(self) -> None:
        """
        `MEMORY_JOIN_TIMEOUT` 必须是个有限的小数值。

        它的依据：真实窗口只有写盘那毫秒级的一段，等它够了；而 LLM 还在跑时
        等 5 秒也等不出结果，等下去只是让用户干瞪眼。
        """
        self.assertIsInstance(MemoryManager.MEMORY_JOIN_TIMEOUT, float)
        self.assertGreater(MemoryManager.MEMORY_JOIN_TIMEOUT, 0)
        self.assertLessEqual(
            MemoryManager.MEMORY_JOIN_TIMEOUT,
            30.0,
            "退出时最多让用户等这么久——再长就该问问它到底在等什么了",
        )


class DaemonStaysDaemonTest(unittest.TestCase):
    """⚠ 三条「别改成那样」的反证。"""

    def _source(self, *parts: str) -> str:
        import pathlib

        root = pathlib.Path(__file__).resolve().parents[1]
        return root.joinpath(*parts).read_text(encoding="utf-8")

    def test_memory_thread_is_still_daemon(self) -> None:
        """
        ⚠ **别把记忆线程改成非 daemon。**

        那会让「记忆线程正在等一个卡住的 LLM 调用」直接演变成「程序退不掉」，
        与 C10-a 同型。正确的组合是 **daemon + join(timeout)**：daemon 保证最坏
        情况下进程仍能退出，join(timeout) 争取那个毫秒级的写盘窗口。
        """
        source = self._source("rhinecode", "memory", "manager.py")
        self.assertIn('name="rhine-memory"', source)
        self.assertIn("daemon=True", source)
        self.assertNotIn("daemon=False", source)

    def test_join_always_has_a_timeout(self) -> None:
        """反证：`join()` 不许出现不带 timeout 的写法。"""
        source = self._source("rhinecode", "memory", "manager.py")
        self.assertIn("join(timeout=", source)
        self.assertNotIn(".join()", source)

    def test_subagent_thread_was_not_touched(self) -> None:
        """
        ⚠ **反证：子 Agent 线程的 daemon 化刻意保持原样，别顺手一起改。**

        `subagents/runner.py` 那里对 daemon 化有明确的取舍记录，而它写的是
        **临时状态**（跑一半的子任务丢了就丢了）；记忆写的是**持久化数据**。
        取舍不同，而本条修复之前策略抄了同一个——所以这条反证不是多余的，
        它挡的正是「反过来再抄一次」。
        """
        source = self._source("rhinecode", "subagents", "runner.py")
        self.assertIn("daemon=True", source)
        self.assertNotIn("MEMORY_JOIN_TIMEOUT", source)


if __name__ == "__main__":
    unittest.main()
