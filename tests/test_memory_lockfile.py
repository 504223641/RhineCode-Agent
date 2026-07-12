"""锁原语单测（c9 T2 / AC21/AC23 相关）：原子互斥、释放、过期自愈、fail-safe。"""

import os
import time
import unittest
import tempfile
from pathlib import Path

from rhinecode.memory.lockfile import try_acquire, release, touch, is_fresh

# 测试用过期阈值（秒）：远大于测试耗时，保证「新鲜」判定稳定。
STALE = 600.0


def _age_lock(path: Path, seconds: float) -> None:
    """把锁文件的 mtime 改到 seconds 秒之前，模拟残留旧锁。"""
    old = time.time() - seconds
    os.utime(str(path), times=(old, old))


class LockfileTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.lock = Path(self._tmp.name) / "test.lock"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_acquire_then_second_fails(self) -> None:
        """首次获取成功；未释放、未过期时第二次获取必须失败（互斥）。"""
        self.assertTrue(try_acquire(self.lock, STALE))
        self.assertTrue(self.lock.exists())
        self.assertFalse(try_acquire(self.lock, STALE))

    def test_release_allows_reacquire(self) -> None:
        """释放后可再次获取；重复释放（文件已不在）静默不抛。"""
        self.assertTrue(try_acquire(self.lock, STALE))
        release(self.lock)
        self.assertFalse(self.lock.exists())
        release(self.lock)  # 二次释放不抛异常
        self.assertTrue(try_acquire(self.lock, STALE))

    def test_stale_lock_taken_over(self) -> None:
        """过期残留锁可被接管：旧锁被清除、新锁建立（AC23 自愈）。"""
        self.assertTrue(try_acquire(self.lock, STALE))
        _age_lock(self.lock, STALE + 10)
        # 另一个「实例」再获取：残留判定 → 清除 → 重试成功
        self.assertTrue(try_acquire(self.lock, STALE))
        # 接管后的锁是新的（mtime 新鲜）
        self.assertTrue(is_fresh(self.lock, STALE))

    def test_is_fresh(self) -> None:
        """is_fresh：新锁 True、过期锁 False、不存在 False。"""
        self.assertFalse(is_fresh(self.lock, STALE))  # 不存在
        self.assertTrue(try_acquire(self.lock, STALE))
        self.assertTrue(is_fresh(self.lock, STALE))
        _age_lock(self.lock, STALE + 10)
        self.assertFalse(is_fresh(self.lock, STALE))

    def test_touch_keeps_lock_fresh(self) -> None:
        """touch 刷新 mtime：过期的锁被心跳救回新鲜（会话锁保鲜机制）。"""
        self.assertTrue(try_acquire(self.lock, STALE))
        _age_lock(self.lock, STALE + 10)
        self.assertFalse(is_fresh(self.lock, STALE))
        touch(self.lock)
        self.assertTrue(is_fresh(self.lock, STALE))

    def test_unwritable_parent_returns_false(self) -> None:
        """锁路径的父目录不存在（不可创建）：返回 False 而不是抛异常（fail-safe）。"""
        bad = Path(self._tmp.name) / "no_such_dir" / "x.lock"
        self.assertFalse(try_acquire(bad, STALE))
        # touch / release 对不存在的路径同样静默
        touch(bad)
        release(bad)

    def test_lock_content_has_pid(self) -> None:
        """锁文件内容含 PID 与时间戳（仅供人工排查，不参与判定）。"""
        self.assertTrue(try_acquire(self.lock, STALE))
        text = self.lock.read_text(encoding="utf-8")
        self.assertIn(f"pid={os.getpid()}", text)
        self.assertIn("time=", text)


if __name__ == "__main__":
    unittest.main()
