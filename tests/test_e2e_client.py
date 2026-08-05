"""
瘦客户端 `send_command` 的失败翻译测试（全阶段复测缺陷 D1）。

## 这个文件在钉什么

`tests/e2e/client.py` 的模块 docstring 把「四种失败必须翻译成人话」写成了设计目标，
其中两种是**「宿主在处理本指令期间没了」的两种 TCP 形态**：

| 宿主怎么没的 | TCP 层 | 客户端读到什么 |
| --- | --- | --- |
| `quit` / 空闲超时 / 正常崩溃 | FIN | `recv` 返回 `b""`（EOF） |
| 强杀（`Stop-Process -Force` / `SIGKILL`） | RST | `recv` **直接抛异常** |

原实现只处理了 EOF 那一路，强杀时抛出裸 `ConnectionResetError` traceback
（`docs/e2e-sweep/testing-p1a-driver.md` 缺陷 D1，稳定复现）。

**为什么值得单独立护栏**：这类缺陷在单测里天然不可见——它只在「宿主被强杀」
这个真人操作下才出现，而正常的自动化流程永远走优雅退出那一路。也就是说，
**不写这条用例，下一次有人重构 `send_command` 时把 `except` 弄丢，
要到再一次全阶段复测才会被发现。**

## 反证在哪

`test_rst_translated_to_readable_error` 断言的是 `RuntimeError`。
`ConnectionResetError` 继承自 `OSError`，**不是** `RuntimeError` 的子类——
所以一旦 `except` 被删掉，这条用例当场红，而不是「照样通过」。
"""

from __future__ import annotations

import unittest
from unittest import mock

from tests.e2e import client, discovery
from tests.e2e.discovery import HostInfo


def make_info(pid: int = 4321, port: int = 6666) -> HostInfo:
    """一份最小可用的名片，字段取值只要能在报错文案里认出来即可。"""
    return HostInfo(
        pid=pid,
        port=port,
        workspace="/tmp/ws",
        user_dir="/tmp/user",
        trace_path="/tmp/ws/.rhinecode/traces/host.jsonl",
        fingerprint="abc123def456",
        mode="scripted",
        started_at=1700000000.0,
    )


class FakeSocket:
    """
    只实现 `send_command` 用到的三个方法的假 socket。

    :param chunks: 每次 `recv` 依次返回的内容；元素若是异常实例则**抛出**它
        （用来模拟 RST——真实 socket 在对端 RST 后就是从 `recv` 抛异常）

    刻意不用 `mock.Mock(side_effect=...)`：那样写出来的用例，读的人得先想清楚
    side_effect 对「异常实例」的特殊语义，而这里恰恰是全文最需要一眼看懂的地方。
    """

    def __init__(self, chunks):
        self._chunks = list(chunks)
        self.sent = b""
        self.closed = False

    def sendall(self, data: bytes) -> None:
        self.sent += data

    def recv(self, _size: int) -> bytes:
        if not self._chunks:
            return b""  # 列表耗尽等同于 EOF，省得每个用例都补一个尾巴
        item = self._chunks.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item

    def close(self) -> None:
        self.closed = True


class SendCommandFailureTranslationTest(unittest.TestCase):
    """`send_command` 的两条「宿主没了」路径都必须给出同一条可读提示。"""

    def _run(self, chunks):
        """把假 socket 塞给 `discovery.connect`，跑一次 `send_command`，返回 (异常上下文, 假 socket)。"""
        sock = FakeSocket(chunks)
        info = make_info()
        with mock.patch.object(discovery, "connect", return_value=sock):
            with self.assertRaises(RuntimeError) as ctx:
                client.send_command(info, {"cmd": "wait", "timeout": 180}, timeout=5.0)
        return ctx, sock

    def test_eof_translated_to_readable_error(self):
        """优雅退出（FIN → 空 bytes）：原有行为，一并钉住防回归。"""
        ctx, sock = self._run([])
        message = str(ctx.exception)
        self.assertIn("宿主在处理本指令期间退出了", message)
        self.assertIn("pid=4321", message)
        self.assertIn("'wait'", message)
        self.assertIn("client hosts", message)  # 必须给出下一步怎么排查
        self.assertTrue(sock.closed, "finally 里的 close 不能漏——否则会攒一堆半开连接")

    def test_rst_translated_to_readable_error(self):
        """
        强杀（RST → recv 抛异常）：**缺陷 D1 的正面判据**。

        断言 `RuntimeError` 本身即反证：`ConnectionResetError` 是 `OSError` 的子类，
        删掉 `except` 后它会原样穿透，这条用例当场红。
        """
        ctx, sock = self._run([ConnectionResetError(10054, "远程主机强迫关闭了一个现有的连接")])
        message = str(ctx.exception)
        # 与 EOF 路径共用同一条主文案——两种形态对使用者是同一件事
        self.assertIn("宿主在处理本指令期间退出了", message)
        self.assertIn("pid=4321", message)
        self.assertIn("'wait'", message)
        # 但要点明形态，否则使用者无从判断「是不是我自己刚才 kill 的」
        self.assertIn("强制关闭", message)
        self.assertIn("ConnectionResetError", message)
        self.assertIn("临时工作区", message)  # 强杀特有的善后提示
        self.assertTrue(sock.closed)

    def test_aborted_also_translated(self):
        """`ConnectionAbortedError` 是同一现象在另一些平台/时序下的形态，不能漏。"""
        ctx, _ = self._run([ConnectionAbortedError(10053, "connection aborted")])
        self.assertIn("宿主在处理本指令期间退出了", str(ctx.exception))
        self.assertIn("ConnectionAbortedError", str(ctx.exception))

    def test_rst_after_partial_data_still_readable(self):
        """
        **收到半截数据之后才 RST**，仍必须是可读提示，不能退化成 JSONDecodeError。

        这条对应实现里那段结构性理由：`while` 的条件是「buffer 里还没有 `\\n`」，
        所以能执行到那次 `recv`，就说明完整的一行响应必然还没到齐——
        此时保留半截 buffer 交给 `decode`，只会把一个明确的「宿主没了」
        换成一个没信息量的解析错误。

        注意与 EOF 路径的**刻意不同**：EOF 那一路收到过数据时是 `break`
        （「能解多少算多少」，是既有设计），RST 这一路则无条件抛。
        """
        ctx, _ = self._run([b'{"ok": true, "sta', ConnectionResetError(10054, "reset")])
        self.assertIn("宿主在处理本指令期间退出了", str(ctx.exception))

    def test_normal_response_unaffected(self):
        """正常路径一个字都不能变：分两块到达的完整响应仍要能正确解出。"""
        sock = FakeSocket([b'{"ok": true, "sta', b'te": "idle"}\n'])
        with mock.patch.object(discovery, "connect", return_value=sock):
            response = client.send_command(make_info(), {"cmd": "status"}, timeout=5.0)
        self.assertEqual(response, {"ok": True, "state": "idle"})
        self.assertTrue(sock.closed)


if __name__ == "__main__":
    unittest.main()
