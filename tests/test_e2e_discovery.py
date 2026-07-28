"""
发布文件与两级陈旧判定测试（P1a T8）。

覆盖：写入/扫描/解析往返、`unpublish` 幂等、坏文件跳过、`resolve_host` 在
0/1/2 个宿主时的三种行为、**AC10 的单元级一半**（连不上立即失败而不是等超时）、
`verify_pid` 的端口复用识别。

全程用 `mock.patch` 把发布目录指到测试自己的临时目录，**不污染真实的系统临时目录**。
"""

from __future__ import annotations

import json
import socket
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from tests.e2e import discovery
from tests.e2e.discovery import HostInfo, StaleHostError


def make_info(pid: int = 1234, port: int = 5555) -> HostInfo:
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


class DiscoveryTestBase(unittest.TestCase):
    """把发布目录重定向到用例私有的临时目录。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="rhine_e2e_pub_test_")
        self.addCleanup(self._tmp.cleanup)
        self.pub = Path(self._tmp.name) / "rhinecode-e2e"
        patcher = mock.patch.object(discovery, "publish_dir", return_value=self.pub)
        patcher.start()
        self.addCleanup(patcher.stop)


class PublishTest(DiscoveryTestBase):
    def test_publish_creates_dir_and_roundtrips(self):
        info = make_info()
        path = discovery.publish(info)
        self.assertTrue(path.exists())
        self.assertEqual(path.name, "host-1234.json")
        # 文件内容必须是可读 UTF-8 JSON（排障时人要直接 cat 它）
        raw = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(raw["pid"], 1234)
        self.assertEqual(raw["port"], 5555)

        hosts = discovery.list_hosts()
        self.assertEqual(len(hosts), 1)
        self.assertEqual(hosts[0], info)

    def test_unpublish_is_idempotent(self):
        info = make_info()
        discovery.publish(info)
        self.assertTrue(discovery.unpublish(info.pid))
        # 第二次删：文件已不在，返回 False 而不是抛错。
        # 正常退出路径上宿主与测试的 addCleanup 都会调它，必然被调两次。
        self.assertFalse(discovery.unpublish(info.pid))

    def test_list_hosts_on_missing_dir(self):
        # 发布目录还没被任何宿主创建过时，列表为空而不是抛错
        self.assertEqual(discovery.list_hosts(), [])

    def test_list_hosts_skips_broken_files(self):
        discovery.publish(make_info(pid=1))
        discovery.publish(make_info(pid=2))
        # 半截 JSON（宿主写到一半被杀）
        (self.pub / "host-999.json").write_text("{not json", encoding="utf-8")
        # 字段缺失
        (self.pub / "host-998.json").write_text('{"pid": 998}', encoding="utf-8")

        hosts = discovery.list_hosts()
        self.assertEqual([h.pid for h in hosts], [1, 2], "坏文件必须被跳过且不影响其余")


class ResolveHostTest(DiscoveryTestBase):
    def test_resolve_none_running(self):
        with self.assertRaises(StaleHostError) as ctx:
            discovery.resolve_host()
        msg = str(ctx.exception)
        self.assertIn("没有正在运行的宿主", msg)
        # 异常消息即用户可见文案：要给出下一步能照做的命令
        self.assertIn("tests.e2e.host", msg)

    def test_resolve_exactly_one(self):
        info = make_info(pid=42)
        discovery.publish(info)
        self.assertEqual(discovery.resolve_host(), info)

    def test_resolve_multiple_requires_pid(self):
        discovery.publish(make_info(pid=11, port=1111))
        discovery.publish(make_info(pid=22, port=2222))
        with self.assertRaises(StaleHostError) as ctx:
            discovery.resolve_host()
        msg = str(ctx.exception)
        self.assertIn("2 个宿主", msg)
        self.assertIn("--pid", msg)
        # 列表里要能看到各自的 pid，用户才知道 --pid 填什么
        self.assertIn("pid=11", msg)
        self.assertIn("pid=22", msg)

        # 指定 pid 后能精确取到
        self.assertEqual(discovery.resolve_host(pid=22).port, 2222)

    def test_resolve_unknown_pid(self):
        discovery.publish(make_info(pid=11))
        with self.assertRaises(StaleHostError) as ctx:
            discovery.resolve_host(pid=77)
        self.assertIn("77", str(ctx.exception))


class StaleDetectionTest(DiscoveryTestBase):
    def test_connect_to_dead_host_fails_immediately(self):
        """
        **AC10 的单元级一半**：名片指向一个没人监听的端口时，
        `connect` 必须**因连接被拒而失败**，而不是等满调用方给的超时。

        做法：先 bind 一个端口拿到号码，然后立刻关掉——
        这样这个端口号在事实上无人监听，且几乎不可能被别人瞬间抢走。

        ⚠️ **阈值的平台事实（实测，不是拍脑袋定的）**：
        task.md 原写「断言耗时 < 1 秒」，但在 Windows 上这个判据按字面执行做不到——
        **操作系统自己**就要约 2.03 秒才把 `ConnectionRefusedError` 返回给用户态
        （WinSock 对回环连接仍走 SYN 重试；实测裸 `socket.connect` 连测三次
        分别是 2.032 / 2.031 / 2.016 秒，与本设施的代码完全无关）。
        AC10 真正要保证的是「**不等满调用方给的超时**」——这里给 30 秒超时、
        断言远早于它返回，正是这个语义；同时断言异常类型是「连接被拒」而不是超时，
        排除掉「实现其实是在等超时」的可能。阈值取 5 秒是 2.03 秒实测值的安全余量。
        """
        probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        probe.bind(("127.0.0.1", 0))
        dead_port = probe.getsockname()[1]
        probe.close()

        info = make_info(pid=31337, port=dead_port)
        discovery.publish(info)

        timeout = 30.0
        started = time.monotonic()
        with self.assertRaises(StaleHostError) as ctx:
            # 故意给一个很大的 timeout：如果实现是「等超时」，这条用例会跑 30 秒
            discovery.connect(info, timeout=timeout)
        elapsed = time.monotonic() - started

        self.assertLess(
            elapsed,
            5.0,
            f"必须在 OS 报连接被拒时立即失败，不得等满 {timeout} 秒超时；实测耗时 {elapsed:.3f}s",
        )
        self.assertLess(elapsed, timeout / 2, "耗时必须与调用方给的超时无关")
        msg = str(ctx.exception)
        self.assertIn("宿主已不在", msg)
        self.assertIn(str(dead_port), msg)
        self.assertIn("31337", msg, "消息里要有 pid，人才知道该删哪张名片")
        self.assertIn("host-31337.json", msg, "消息里要有名片路径，能直接照着删")

    def test_verify_pid_detects_port_reuse(self):
        info = make_info(pid=100)
        # 对面答话的是另一个 pid —— 端口被别的进程复用了
        with self.assertRaises(StaleHostError) as ctx:
            discovery.verify_pid(info, {"pid": 200, "state": "idle"})
        msg = str(ctx.exception)
        self.assertIn("复用", msg)
        self.assertIn("100", msg)
        self.assertIn("200", msg)

    def test_verify_pid_passes_on_match(self):
        info = make_info(pid=100)
        self.assertIsNone(discovery.verify_pid(info, {"pid": 100}))


if __name__ == "__main__":
    unittest.main()
