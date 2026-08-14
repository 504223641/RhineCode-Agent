"""
宿主进程测试（P1a T57–T66、T68–T69）。

**这一层起真实子进程**：`python -m tests.e2e.host`，经控制通道驱动，
从外部验证「跨进程的交互闭环」确实成立。

覆盖 AC1、AC4–AC12、AC16–AC17、AC19、AC23–AC28、AC30、AC34、AC42、AC43。

## 每个用例都必须兜底清理

`addCleanup` 注册「强杀宿主 + 删名片 + chdir 回去 + rmtree」，且该清理对
「宿主已自行退出」**幂等**。缺了它，任何一条用例断言失败都会留下进程、
临时目录与名片文件，AC42 直接红——而那时你要排查的是「上一次是哪条用例挂的」，
而不是本来的问题。
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import time
import unittest
from pathlib import Path
from typing import Optional

from tests.e2e import discovery, protocol, sandbox
from tests.e2e.assertions import (
    TraceView,
    assert_check,
    check_history_len,
    check_permission,
    check_tool_outcome,
    check_ui_contains,
)
from tests.e2e.discovery import HostInfo, StaleHostError


REPO_ROOT = Path(__file__).resolve().parent.parent

# 宿主起来需要多久（装配 + Textual 挂载）。本机实测约 8–12 秒，给到 90 秒余量。
HOST_READY_TIMEOUT = 90.0

# 「慢速专项」开关：连续起停宿主那条用例每次要跑一分钟以上，不进全量。
SLOW_TESTS = os.environ.get("RHINE_E2E_SLOW") == "1"


class HostFixture(unittest.TestCase):
    """起一个宿主子进程，并保证无论如何都能收干净。"""

    def setUp(self) -> None:
        self._cwd = Path.cwd()
        self.proc: Optional[subprocess.Popen] = None
        self.info: Optional[HostInfo] = None
        self._stderr_handle = None
        self.stderr_path = Path(
            os.environ.get("TEMP", "/tmp")
        ) / f"rhine_e2e_host_{os.getpid()}_{int(time.time() * 1000)}.log"

    def tearDown(self) -> None:
        os.chdir(self._cwd)
        self.kill_host()
        # ⚠️ **必须先关句柄再删文件**：Windows 上删不掉正被打开的文件。
        # 早先把 close 挂在 `addCleanup` 上是错的——unittest 的 cleanup 在 tearDown
        # **之后**才跑，于是这里每跑一条用例就在系统临时目录里留一个日志文件
        # （实测一轮 25 条用例攒了 44 个，把 AC42 的「无残留」直接顶红）。
        if self._stderr_handle is not None:
            try:
                self._stderr_handle.close()
            except OSError:
                pass
            self._stderr_handle = None
        try:
            self.stderr_path.unlink()
        except OSError:
            pass

    # ------------------------------------------------------------------ #
    def start_host(self, *extra: str, wait_ready: bool = True) -> HostInfo:
        """
        起一个宿主并等它把名片写出来。

        :param wait_ready: True 时额外等到 `status` 不再回 `starting`
            （装配失败的用例要传 False——它永远等不到 idle）

        ## 为什么缺省补一个 `--permission-mode default`（auto-plan 扩展）

        驱动设施的用例大量把「确认面板」当**夹具**使——它们验的是控制通道
        （面板就绪的原子性、应答的两条路径、结算与退出），面板本身只是个
        能稳定造出 `pending` 态的东西。

        auto-plan 扩展之后，工作区内的普通写入在缺省档（放行）下**不再弹面板**，
        于是那些用例会等到一个永远不来的 `pending`。而它们**不能**改用保护路径
        那种面板：那个只有三个选项，`permanent` 那一支覆盖不到。

        故在这里统一补一档。**只在调用方没自己指定时才补**——需要验证产品
        缺省行为的用例（如 `test_e2e_protected` 那组）显式传自己的档位，
        补的这一档不会盖掉它。
        """
        if not any(a == "--permission-mode" for a in extra):
            extra = (*extra, "--permission-mode", "default")
        before = {h.pid for h in discovery.list_hosts()}
        # 同一条用例可能起两次宿主（如「改代码 → 重启 → 比指纹」那条），
        # 开新句柄前先把上一个关掉——否则它既泄漏文件描述符，
        # 也会让 tearDown 只删得掉最后那一个日志文件。
        if self._stderr_handle is not None:
            try:
                self._stderr_handle.close()
            except OSError:
                pass
        # 句柄存到实例上，由 tearDown 负责关闭（见那里的注释：不能挂 addCleanup）
        stderr = self.stderr_path.open("w", encoding="utf-8")
        self._stderr_handle = stderr
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "tests.e2e.host", *extra],
            cwd=str(REPO_ROOT),
            stdout=subprocess.DEVNULL,
            stderr=stderr,
        )
        self.addCleanup(self.kill_host)

        deadline = time.monotonic() + HOST_READY_TIMEOUT
        while time.monotonic() < deadline:
            for host in discovery.list_hosts():
                # ⚠️ **只按「名片是新出现的」判定，绝不拿 `self.proc.pid` 去比对。**
                #
                # 实测事实：`sys.executable` 在本机是 venv 的 shim
                # （`venv\Scripts\python.exe`），它会把真正的解释器**再 exec 成一个
                # 子进程**，于是 `Popen.pid`（外层 shim）与宿主自己 `os.getpid()`
                # 写进名片的那个 pid **根本不是同一个数**
                # （实测 popen=6704 / 名片=28356）。
                #
                # 拿 pid 比对的后果有两层，第二层更糟：
                # ① 永远匹配不上，每条用例都以「宿主未就绪」超时；
                # ② `proc.kill()` 杀的是 shim 而不是宿主，**宿主全部泄漏**——
                #    几十个常驻进程堆在机器上，后续用例因资源不足而级联失败，
                #    而失败信息全都指向「启动慢」这个假象。
                if host.pid not in before:
                    self.info = host
                    if not wait_ready:
                        return host
                    if self._wait_until_not_starting(deadline):
                        return host
                    self.fail(f"宿主起来了但一直是 starting：\n{self.host_stderr()}")
            if self.proc.poll() is not None:
                self.fail(f"宿主进程提前退出（码 {self.proc.returncode}）：\n{self.host_stderr()}")
            time.sleep(0.2)
        self.fail(f"宿主未在 {HOST_READY_TIMEOUT} 秒内就绪：\n{self.host_stderr()}")

    def _wait_until_not_starting(self, deadline: float) -> bool:
        while time.monotonic() < deadline:
            try:
                res = self.cmd({"cmd": "status"})
            except (StaleHostError, OSError):
                time.sleep(0.2)
                continue
            if res.get("ok") and res["data"]["state"] != "starting":
                return True
            if not res.get("ok") and res["error"]["code"] == "fatal":
                return True
            time.sleep(0.2)
        return False

    def kill_host(self) -> None:
        """
        强杀 + 删名片 + 删临时目录。**对「宿主已自行退出」幂等。**

        ⚠️ **必须杀整棵进程树，不能只 `proc.kill()`**：`sys.executable` 是 venv 的
        shim，它把真正的解释器 exec 成了子进程（见 `start_host` 的注释）。
        只杀 shim 的话宿主会活下来变成常驻孤儿——实测攒过二十几个，
        直接把机器拖到后续用例全部超时。
        """
        proc, info = self.proc, self.info

        # ① **先给它一次优雅退出的机会**。强杀的宿主来不及跑自己的清理，
        #    留下的工作区里常有仍被占用的文件（`.git` 尤其明显），
        #    随后的暴力 rmtree 只能删掉一半——实测就留下过一个只剩 `.git` 的空壳。
        if proc is not None and proc.poll() is None and info is not None:
            try:
                self.cmd({"cmd": "quit"}, timeout=10.0)
                proc.wait(timeout=25)
            except Exception:  # noqa: BLE001 —— 优雅退出失败很正常，下面还有强杀兜底
                pass

        # ② 还活着就杀整棵树（名片上的 pid 才是宿主自己，见 start_host 的注释）
        if info is not None:
            self._kill_pid(info.pid)
        if proc is not None and proc.poll() is None:
            self._kill_pid(proc.pid, tree=True)
            try:
                proc.wait(timeout=20)
            except subprocess.TimeoutExpired:
                pass
        self.proc = None

        # ③ 补清理（宿主自己退出时已经清过，这里对已删的路径天然幂等）
        if info is not None:
            discovery.unpublish(info.pid)
            for path in (info.workspace, info.user_dir):
                self._rmtree_with_retry(Path(path))
            self.info = None

    @staticmethod
    def _rmtree_with_retry(path: Path, attempts: int = 8) -> None:
        """
        删目录，失败就短暂等一下再试。

        Windows 下刚退出的进程可能还没释放文件句柄，第一次 `rmtree` 会撞
        `PermissionError`。**不用 `ignore_errors=True`**：那会「删一半」并留下
        一个残缺目录，看起来像是清理成功了其实没有。
        """
        for i in range(attempts):
            if not path.exists():
                return
            try:
                # 复用 sandbox 的只读位处理（git 的 pack 文件在 Windows 上是只读的）
                shutil.rmtree(path, onerror=sandbox._clear_readonly)
                return
            except OSError:
                time.sleep(0.3 * (i + 1))
        # 最后一次尽力而为；仍然删不掉就**说出来**（spec N7：不静默）。
        # 不抛异常是因为这是 tearDown 路径——在这里抛会把用例本身的失败原因盖掉。
        shutil.rmtree(path, ignore_errors=True)
        if path.exists():
            print(
                f"[e2e] 清理残留：{path} 删不干净（剩 "
                f"{[p.name for p in path.rglob('*')][:5]}），请手工删除",
                file=sys.stderr,
            )

    @staticmethod
    def _kill_pid(pid: int, *, tree: bool = False) -> None:
        """杀掉一个进程（`tree=True` 时连同它的子进程）。已退出的进程静默略过。"""
        if sys.platform == "win32":
            args = ["taskkill", "/F"] + (["/T"] if tree else []) + ["/PID", str(pid)]
            subprocess.run(args, capture_output=True)
        else:
            try:
                os.kill(pid, 9)
            except (ProcessLookupError, PermissionError):
                pass

    def host_stderr(self) -> str:
        """
        读回宿主的 stderr。

        ⚠️ **按 GBK 解码**：宿主的 stderr 是 Python 在 Windows 上按控制台代码页
        （本机是 GBK）写出去的，按 UTF-8 读会得到满屏乱码——而这段文字正是
        用例失败时唯一的线索，读不出来等于没有。
        """
        for encoding in ("gbk", "utf-8"):
            try:
                return self.stderr_path.read_text(encoding=encoding, errors="replace")[-3000:]
            except (OSError, LookupError):
                continue
        return "（读不到宿主 stderr）"

    # ------------------------------------------------------------------ #
    def cmd(self, request: dict, *, timeout: float = 300.0) -> dict:
        """
        直发一条指令并收响应（**不经 client 子进程**：快，且能拿到结构化数据）。
        """
        assert self.info is not None, "还没起宿主"
        sock = discovery.connect(self.info, timeout=timeout)
        try:
            sock.sendall(protocol.encode(request))
            buffer = b""
            while b"\n" not in buffer:
                chunk = sock.recv(65536)
                if not chunk:
                    break
                buffer += chunk
            return protocol.decode(buffer.partition(b"\n")[0])
        finally:
            sock.close()

    def send(self, text: str) -> dict:
        return self.cmd({"cmd": "send", "text": text})

    def wait(self, timeout: float = 120.0) -> dict:
        return self.cmd({"cmd": "wait", "timeout": timeout})

    def answer(self, choice: str, via: str = "channel") -> dict:
        return self.cmd({"cmd": "answer", "choice": choice, "via": via})

    def status(self) -> dict:
        res = self.cmd({"cmd": "status"})
        self.assertTrue(res["ok"], res)
        return res["data"]

    def observe(self, since: int = 0, types=None) -> dict:
        res = self.cmd({"cmd": "observe", "since": since, "types": types})
        self.assertTrue(res["ok"], res)
        return res["data"]

    def quit_host(self, *, expect_exit: int = 0, timeout: float = 60.0) -> None:
        """
        优雅退出并**等宿主真正跑完自己的清理**。

        ⚠️ **只等 `proc.wait()` 是不够的**：`self.proc` 是 venv shim，它退出并不
        代表真正的宿主进程也退出了（两者 pid 不同，见 `start_host` 的注释）。
        实测后果——`quit_host` 返回后 `tearDown` 立刻强杀，把正在删工作区的宿主
        拦腰打断，留下一个只剩 `.git/objects` 的残骸。
        所以这里还要等宿主把自己的名片撤掉（那是它清理流程的**最后一步**）。
        """
        self.cmd({"cmd": "quit"})
        assert self.proc is not None
        self.proc.wait(timeout=timeout)
        self.assertEqual(self.proc.returncode, expect_exit, self.host_stderr())

        if self.info is not None:
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                if not discovery.host_file(self.info.pid).exists():
                    return  # 名片没了 = 宿主的清理已经走完
                time.sleep(0.1)

    def view(self) -> TraceView:
        assert self.info is not None
        return TraceView.load(Path(self.info.trace_path))

    def workspace(self) -> Path:
        assert self.info is not None
        return Path(self.info.workspace)


class ClosedLoopTest(HostFixture):
    """AC1：完整闭环序列，全程不重启宿主。"""

    def test_closed_loop(self):
        self.start_host(
            "--mode", "scripted",
            "--script", "tests.e2e.scripts:CONFIRM_THEN_DONE",
            "--idle-timeout", "300",
        )
        self.assertEqual(self.status()["state"], "idle")

        # send → wait（pending）
        self.assertTrue(self.send("写个文件")["ok"])
        pending = self.wait()
        self.assertTrue(pending["ok"], pending)
        self.assertEqual(pending["data"]["terminal"], "pending")

        # status 读到面板展示原文与可选项列表
        panel = self.status()["panel"]
        self.assertEqual(panel["kind"], "confirm")
        # 面板表头走 B 组的主参数口径（tui-display 扩展 F12）：显示
        # `Write(x.txt)` 而不是内部名 + 键值对。判据跟着改成**展示标签**——
        # 它才是用户实际看到的字，而 option 的 id 才是契约（下面那条断言）。
        self.assertIn("Write(x.txt)", panel["display"])
        self.assertEqual(
            [o["id"] for o in panel["options"]],
            ["yes", "yes_session", "yes_permanent", "no"],
        )

        # answer → wait（idle）
        self.assertTrue(self.answer("once")["ok"])
        self.assertEqual(self.wait()["data"]["terminal"], "idle")
        self.assertTrue((self.workspace() / "x.txt").is_file(), "工具真的执行了")

        # **再 send 一次**（全程不重启，这正是「常驻」的价值所在）
        self.assertTrue(self.send("再说一句")["ok"])
        self.assertEqual(self.wait()["data"]["terminal"], "idle")

        # 会话存档里有两轮用户输入。
        # ⚠️ 权威来源是**存档**而不是记录（记录的消息列表受双重截断）。
        sessions = self.workspace() / ".rhinecode" / "sessions"
        assert_check(check_history_len(sessions, 2, role="user"))

        self.quit_host()


class ObserveSurfaceTest(HostFixture):
    """AC12 / AC34 / AC4：输入入口、观察面与响应时延。"""

    def test_input_entry_and_observation(self):
        self.start_host(
            "--mode", "scripted",
            "--script", "tests.e2e.scripts:THINK_AND_READ",
            "--seed", "tests.e2e.scripts:seed_basic",
            "--idle-timeout", "300",
        )
        self.send("读一下 seed.txt")
        self.wait()

        view = self.view()
        # **AC12**：走的是真人提交入口，所以产出了 user_input 事件
        # （直接调内部方法是不会有这条的）
        self.assertTrue(view.of_type("user_input"), "必须走真人提交入口")

        # **AC34**：observe 的 events 里含遍历界面控件取不到的完整 payload
        data = self.observe()
        types = {e["type"] for e in data["events"]}
        self.assertIn("ui_message", types, "富文本 AI 正文")
        self.assertIn("tool_execute", types, "工具行的完整 payload")
        tool_events = [e for e in data["events"] if e["type"] == "tool_execute"]
        self.assertIn("output", tool_events[0], "payload 要含工具输出原文")

        # 命令也走同一个入口：命令分发事件
        self.send("/skills")
        self.wait()
        self.assertTrue(self.view().of_type("command_dispatch"), "命令必须产出分发事件")

    def test_queries_return_fast_while_busy(self):
        """AC4：忙碌期间的查询必须一秒内返回（spec N4）。"""
        self.start_host(
            "--mode", "scripted",
            "--script", "tests.e2e.scripts:CONFIRM_THEN_DONE",
            "--idle-timeout", "300",
        )
        self.send("写个文件")
        self.wait()  # 停在 pending，此时忙碌态仍为真

        for _ in range(5):
            started = time.monotonic()
            data = self.status()
            elapsed = time.monotonic() - started
            self.assertLess(elapsed, 1.0, f"status 耗时 {elapsed:.3f}s，超过一秒预算")
            self.assertEqual(data["state"], "pending")

            started = time.monotonic()
            self.observe()
            elapsed = time.monotonic() - started
            self.assertLess(elapsed, 1.0, f"observe 耗时 {elapsed:.3f}s，超过一秒预算")

        self.answer("deny")
        self.wait()


class QuitWithPendingTest(HostFixture):
    """
    AC6 进程级：挂着面板时直接 quit 也必须干净退出。

    T44 已在内核级验过一次，这条是**进程级**——两条都要。
    内核级证明「强制结算这段逻辑对」，进程级证明「真进程确实退得出去」，
    而实测挂死正是发生在 `asyncio.run()` 的收尾阶段，那一段只有真进程才走得到。
    """

    def test_quit_with_pending_panel(self):
        # `--keep-workspace`：宿主正常退出时会把工作区整个删掉，
        # 而本条要在**退出之后**读那份记录，不保留就只能读到 FileNotFoundError。
        self.start_host(
            "--mode", "scripted",
            "--script", "tests.e2e.scripts:CONFIRM_THEN_DONE",
            "--idle-timeout", "300",
            "--keep-workspace",
        )
        self.addCleanup(sandbox.force_rmtree, self.info.workspace)
        self.addCleanup(sandbox.force_rmtree, self.info.user_dir)
        self.send("写个文件")
        self.assertEqual(self.wait()["data"]["terminal"], "pending")

        trace_path = Path(self.info.trace_path)
        started = time.monotonic()
        self.quit_host(timeout=60.0)
        elapsed = time.monotonic() - started
        self.assertLess(elapsed, 60.0, f"退出耗时 {elapsed:.1f}s")

        # 记录里必须留下强制结算的痕迹
        view = TraceView.load(trace_path)
        forced = [e for e in view.of_type("interaction") if e.get("source") == "driver_forced"]
        self.assertTrue(forced, "必须有一条 source=driver_forced 的交互事件")


class IdleTimeoutTest(HostFixture):
    """AC7：空闲超时自行退出，且产物完整可读。"""

    def test_idle_timeout_exits_cleanly(self):
        info = self.start_host(
            "--mode", "scripted",
            "--script", "tests.e2e.scripts:SAY_HELLO",
            "--idle-timeout", "3",
            "--keep-workspace",  # 保留产物才能在进程退出后检查它
        )
        assert self.proc is not None
        self.proc.wait(timeout=90)
        self.assertEqual(self.proc.returncode, 0, self.host_stderr())

        # 记录文件可完整解析，且**末条是 session_end**
        view = TraceView.load(Path(info.trace_path))
        self.assertEqual(view.skipped, 0, "记录必须可完整解析")
        self.assertEqual(view.records[-1]["type"], "session_end")
        self.assertEqual(view.records[-1]["reason"], "idle_timeout")

        # 会话锁已释放
        locks = list((Path(info.workspace) / ".rhinecode" / "sessions").glob("*.lock"))
        self.assertEqual(locks, [], "会话锁必须已释放")

        # 临时工作区可被删除（--keep-workspace 时由本用例负责清理）
        sandbox.force_rmtree(info.workspace)
        sandbox.force_rmtree(info.user_dir)
        self.assertFalse(Path(info.workspace).exists())


class FatalAndDiscoveryTest(HostFixture):
    """AC5 / AC9 / AC10 / AC11：致命错误经通道回报、发现机制、陈旧判定、指纹。"""

    def test_bootstrap_fatal_reported_over_channel(self):
        """
        AC5：装配期致命错误必须**经通道回报**而不是超时。

        用一份**非法 protocol** 的配置触发 `create_provider` 报错——这正是
        socket 必须先于装配的理由：先装配再监听的话，这里只会收到「连接被拒」。

        ⚠️ C11 时这里用的是「白名单笔误的 Skill」。对齐改造把白名单 fail-fast
        整个删除后，那个触发源不再致命，只好换一个仍然致命的。

        ⚠️ **必须用 `--mode live`**：scripted 模式下宿主会把 `provider_factory`
        换成假模型，`create_provider` 压根不会被调用，非法 protocol 也就不会报错
        （第一次改这条时就撞上了，宿主一切正常地起来了）。
        本条用的假 key 只是为了过宿主自己那道「凭据不能是占位符」的检查，
        它永远不会被用来发请求——装配在此之前就失败了。
        """
        bad_config = self.stderr_path.parent / "bad-protocol.yaml"
        bad_config.write_text(
            "protocol: no-such-protocol\nmodel: m\n"
            "base_url: https://x\napi_key: k\n",
            encoding="utf-8",
        )
        self.start_host(
            "--mode", "live",
            "--config", str(bad_config),
            "--idle-timeout", "120",
            wait_ready=False,
        )
        deadline = time.monotonic() + 90
        res = None
        while time.monotonic() < deadline:
            res = self.cmd({"cmd": "status"})
            if not res.get("ok") and res["error"]["code"] == "fatal":
                break
            time.sleep(0.3)
        self.assertIsNotNone(res)
        self.assertFalse(res.get("ok"), f"应当是 fatal 而不是成功：{res}")
        self.assertEqual(res["error"]["code"], "fatal")
        # 文案与既有启动测试逐字一致（P0 已把它做成成文的完整 stderr 文案）
        message = res["error"]["message"]
        self.assertIn("Provider 初始化错误", message)
        self.assertIn("no-such-protocol", message)

        self.quit_host(expect_exit=1)

    def test_publish_file_and_one_step_connect(self):
        """AC9：名片含足够信息，客户端据此**一步连上**（不需要先知道工作区）。"""
        info = self.start_host(
            "--mode", "scripted", "--script", "tests.e2e.scripts:SAY_HELLO",
            "--idle-timeout", "120",
        )
        self.assertGreater(info.port, 0)
        self.assertTrue(Path(info.workspace).is_dir())
        self.assertEqual(info.mode, "scripted")

        # 监听地址是回环
        with socket.socket() as probe:
            probe.settimeout(5)
            probe.connect(("127.0.0.1", info.port))

        # 客户端进程只知道「有个宿主」就能连上并拿到 pid
        out = subprocess.run(
            [sys.executable, "-m", "tests.e2e.client", "status", "--json"],
            cwd=str(REPO_ROOT), capture_output=True, encoding="utf-8", errors="replace",
        )
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(json.loads(out.stdout)["data"]["pid"], info.pid)

    def test_stale_card_fails_fast(self):
        """AC10：指向未监听端口的名片，客户端立即报陈旧（不等满超时）。"""
        probe = socket.socket()
        probe.bind(("127.0.0.1", 0))
        dead_port = probe.getsockname()[1]
        probe.close()

        fake = HostInfo(
            pid=999999, port=dead_port, workspace="/tmp/x", user_dir="/tmp/y",
            trace_path="/tmp/z", fingerprint="dead", mode="scripted", started_at=0.0,
        )
        discovery.publish(fake)
        self.addCleanup(discovery.unpublish, fake.pid)

        started = time.monotonic()
        out = subprocess.run(
            [sys.executable, "-m", "tests.e2e.client", "status", "--pid", "999999"],
            cwd=str(REPO_ROOT), capture_output=True, encoding="utf-8", errors="replace",
        )
        elapsed = time.monotonic() - started
        self.assertEqual(out.returncode, 1)
        self.assertIn("宿主已不在", out.stderr)
        # 阈值同 test_e2e_discovery：Windows 上 OS 自己要 ~2 秒才报连接被拒
        self.assertLess(elapsed, 20.0, f"必须立即失败，实测 {elapsed:.1f}s")

    def test_fingerprint_changes_when_code_changes(self):
        """AC11：改了产品代码 → 重启宿主 → 指纹不同。"""
        first = self.start_host(
            "--mode", "scripted", "--script", "tests.e2e.scripts:SAY_HELLO",
            "--idle-timeout", "120",
        ).fingerprint
        self.quit_host()
        self.kill_host()

        target = REPO_ROOT / "rhinecode" / "bootstrap.py"
        original = target.stat()
        os.utime(target, (original.st_atime, original.st_mtime + 10))
        self.addCleanup(os.utime, target, (original.st_atime, original.st_mtime))

        second = self.start_host(
            "--mode", "scripted", "--script", "tests.e2e.scripts:SAY_HELLO",
            "--idle-timeout", "120",
        ).fingerprint
        self.assertNotEqual(first, second, "代码变了指纹必须跟着变（漏报是本机制存在的理由）")


class PermissionSurfaceTest(HostFixture):
    """
    AC16 / AC17：权限面护栏。

    这两条的分量：spec 目标「不扩大权限面」全靠它俩——驱动器替人应答等于换了
    第⑤层的执行者，第①层黑名单必须证明仍然拦得住。
    """

    def test_four_confirm_choices(self):
        """AC16：四档确认各走一次；`permanent` 额外验证本地配置真被写入。"""
        self.start_host(
            "--mode", "scripted",
            "--script", "tests.e2e.scripts:FOUR_WRITES",
            "--idle-timeout", "300",
        )
        self.send("写四个文件")

        for choice, filename, should_exist in (
            ("once", "a.txt", True),
            ("session", "b.txt", True),
            ("permanent", "c.txt", True),
            ("deny", "d.txt", False),
        ):
            terminal = self.wait()["data"]["terminal"]
            if terminal == "idle":
                self.fail(f"轮到 {choice} 时会话已空闲，剧本或权限规则与预期不符")
            res = self.answer(choice)
            self.assertTrue(res["ok"], f"{choice}: {res}")
            self.wait()
            self.assertEqual(
                (self.workspace() / filename).is_file(),
                should_exist,
                f"{choice} 之后 {filename} 的存在性不符预期",
            )

        # permanent 必须落进本地权限配置
        local = self.workspace() / ".rhinecode" / "permissions.local.yaml"
        self.assertTrue(local.is_file(), "永久放行必须写入 permissions.local.yaml")
        self.assertIn("Write", local.read_text(encoding="utf-8"))

    def test_blacklist_still_blocks_even_if_driver_allows(self):
        """
        AC17：危险命令即使驱动者「放行」也必须被第①层黑名单拦住。

        黑名单在管线最前面，压根走不到人在回路那一层——所以这条用例里
        **不会有面板可答**，判据是「命中层是 blacklist」且「没有执行成功事件」。
        """
        self.start_host(
            "--mode", "scripted",
            "--script", "tests.e2e.scripts:DANGEROUS_COMMAND",
            "--idle-timeout", "300",
        )
        self.send("清理一下")
        for _ in range(6):
            res = self.wait(timeout=60)
            if res["ok"] and res["data"]["terminal"] == "pending":
                # 万一真弹了面板，驱动者选「放行」——黑名单仍必须拦住
                self.answer("once")
                continue
            break

        view = self.view()
        assert_check(check_permission(view, "run_command", layer="blacklist", decision="deny"), view)
        executed = [
            e for e in view.of_type("tool_execute")
            if e.get("tool") == "run_command" and e.get("outcome") == "executed"
        ]
        self.assertEqual(executed, [], "危险命令绝不能有执行成功事件")


class InjectionAndIsolationTest(HostFixture):
    """AC19 / AC25 / AC27 / AC28 / AC30：注入面与隔离。"""

    def test_real_components_with_fake_model(self):
        """AC28：只有模型是假的，其余组件全是真的。"""
        self.start_host(
            "--mode", "scripted", "--script", "tests.e2e.scripts:THINK_AND_READ",
            "--seed", "tests.e2e.scripts:seed_basic", "--idle-timeout", "300",
        )
        self.send("读一下 seed.txt")
        self.wait()

        view = self.view()
        start = view.of_type("session_start")[0]
        # 真实工具注册中心
        self.assertIn("read_file", start["tool_names"])
        self.assertIn("write_file", start["tool_names"])
        # 真实权限引擎与真实 Skill 管理器都产出了事件
        self.assertTrue(view.of_type("permission_decision"), "权限引擎是真的")
        self.assertTrue(view.of_type("skill_state"), "Skill 管理器是真的")
        self.assertTrue(view.of_type("command_dispatch") or view.of_type("user_input"))
        # 真实记录器（这份文件本身就是证据）
        self.assertGreater(len(view.records), 5)

    def test_excluded_tools_absent_everywhere(self):
        """AC19：两个被摘掉的工具在 status 清单与 session_start 快照里都不见，且两者一致。"""
        self.start_host(
            "--mode", "scripted", "--script", "tests.e2e.scripts:SAY_HELLO",
            "--idle-timeout", "120",
        )
        status_tools = set(self.status()["tool_names"])
        snapshot_tools = set(self.view().of_type("session_start")[0]["tool_names"])

        for name in ("mcp_add_server", "mcp_resolve_server"):
            self.assertNotIn(name, status_tools, f"{name} 应已被摘除")
            self.assertNotIn(name, snapshot_tools, f"{name} 不该出现在快照里")
        self.assertEqual(status_tools, snapshot_tools, "两处清单必须一致")
        # 其余工具还在（不是把整个注册中心清空了）
        self.assertIn("read_file", status_tools)

    def test_user_dir_isolated_from_real_home(self):
        """AC25：用户级目录位于临时目录之下，不是真实主目录。"""
        data = self.status() if self.info else None
        info = self.start_host(
            "--mode", "scripted", "--script", "tests.e2e.scripts:SAY_HELLO",
            "--idle-timeout", "120",
        )
        user_dir = Path(info.user_dir).resolve()
        temp_root = Path(os.environ.get("TEMP", "/tmp")).resolve()
        self.assertTrue(
            str(user_dir).startswith(str(temp_root)),
            f"用户目录必须在临时目录下，实际 {user_dir}",
        )
        real_home = (Path.home() / ".rhinecode").resolve()
        self.assertNotEqual(user_dir, real_home)

        snapshot = self.view().of_type("session_start")[0]
        self.assertEqual(Path(snapshot["user_dir"]).resolve(), user_dir)

    def test_no_mcp_connections_in_scripted_mode(self):
        """AC27：确定性形态下没有任何 MCP 连接。"""
        self.start_host(
            "--mode", "scripted", "--script", "tests.e2e.scripts:SAY_HELLO",
            "--idle-timeout", "120",
        )
        snapshot = self.view().of_type("session_start")[0]
        self.assertEqual(snapshot["mcp_status"], [], "临时用户目录里没有 MCP 声明，不该有连接")

    def test_isolated_skill_with_custom_model_stays_fake(self):
        """
        AC30：声明了自定义 `model:` 的独立模式 Skill 走的仍是假模型。

        ⚠️ 这是 spec F21 自己标注「**最容易漏的一条**」——不透传 `provider_factory`
        时该旁路会静默连真实网络，而现象是「测试很慢且偶尔失败」，极难定位。
        判据：子对话的模型请求出现在记录里，且**作用域为 `isolated:<skill名>`**。
        """
        self.start_host(
            "--mode", "scripted",
            "--script", "tests.e2e.scripts:ISOLATED_SKILL",
            "--seed", "tests.e2e.scripts:seed_isolated_skill",
            "--idle-timeout", "300",
        )
        self.send("/skills run solo 看看情况")
        self.wait(timeout=120)

        view = self.view()
        isolated = [
            r for r in view.records
            if str(r.get("scope", "")).startswith("isolated:")
        ]
        self.assertTrue(isolated, f"必须有 isolated 作用域的事件，实际作用域："
                                  f"{sorted({str(r.get('scope')) for r in view.records})}")
        self.assertTrue(
            any(r["type"] == "api_request" for r in isolated),
            "子对话必须发出过模型请求",
        )
        # 全程没有真实网络：假模型是唯一被调用的实现（真实模型在这个 api_key 下会报错）
        errors = [
            r for r in view.of_type("api_response") if r.get("stream_error")
        ]
        self.assertEqual(errors, [], f"不该有任何流错误（那意味着连了真实网络）：{errors}")


class SeedingTest(HostFixture):
    """AC23：预置的项目内容对产品真实可见。"""

    def test_seeded_skill_and_git_history(self):
        self.start_host(
            "--mode", "scripted",
            "--script", "tests.e2e.scripts:GIT_LOG",
            "--seed", "tests.e2e.scripts:seed_with_git",
            "--idle-timeout", "300",
        )
        # ① 预置的项目级 Skill 出现在 /skills 输出里
        self.send("/skills")
        self.wait()
        view = self.view()
        assert_check(check_ui_contains(view, "review"), view)

        # ② 只读版本控制命令能读到提交历史
        self.send("看看提交历史")
        for _ in range(6):
            res = self.wait(timeout=60)
            if res["ok"] and res["data"]["terminal"] == "pending":
                self.answer("once")
                continue
            break

        view = self.view()
        git_events = [
            e for e in view.of_type("tool_execute")
            if e.get("tool") == "run_command" and e.get("outcome") == "executed"
        ]
        self.assertTrue(git_events, "git 命令必须真的跑了")
        output = json.dumps(git_events[-1].get("output"), ensure_ascii=False)
        self.assertIn("初始提交", output, "必须能读到预置的提交历史")

        # 显式优雅退出，让宿主自己清理工作区。
        # ⚠️ 本条是唯一会在工作区里建 git 仓库的用例，而 `.git` 下有大量**只读**
        # 对象文件、且 git 子进程的句柄释放有延迟——靠 tearDown 的强杀 + 重试删，
        # 在全量并发负载下会偶尔删不干净（实测残留过一个只剩 `.git` 的空壳）。
        # 走正常退出路径最稳，也顺带验了这条路径本身。
        self.quit_host(timeout=90)


class SemanticsTest(HostFixture):
    """AC31 / AC32 / AC33 的端到端一半。"""

    def test_skill_activation_takes_effect_next_turn(self):
        """
        AC31：第 N 轮调 `load_skill` 激活，其 SOP 从第 N+1 轮起才出现在动态提醒里。

        ⚠️ 这条只能在**宿主进程内部**验——断言词汇 ⑪ 取自假模型留存的原始参数，
        而假模型在宿主进程里。所以这里改为读记录中的 api_request 事件间接判定：
        激活事件的 seq 必须早于「提到该 Skill 的那一轮请求」。
        """
        self.start_host(
            "--mode", "scripted",
            "--script", "tests.e2e.scripts:ACTIVATE_SKILL",
            "--seed", "tests.e2e.scripts:seed_basic",
            "--idle-timeout", "300",
        )
        self.send("审阅一下")
        self.wait(timeout=120)

        view = self.view()
        activations = [
            e for e in view.of_type("skill_state") if e.get("action") == "activate"
        ]
        self.assertTrue(activations, "load_skill 必须真的激活了一个 Skill")
        requests = view.of_type("api_request")
        self.assertGreaterEqual(len(requests), 2, "至少要有两轮请求才能验「下一轮生效」")
        # 激活发生在第一轮之后、第二轮之前
        self.assertGreater(activations[0]["seq"], requests[0]["seq"])
        self.assertLess(activations[0]["seq"], requests[1]["seq"])

    def test_stream_error_stops_the_loop(self):
        """AC32 消费侧：流错误块让循环以「流错误」停止。"""
        self.start_host(
            "--mode", "scripted",
            "--script", "tests.e2e.scripts:STREAM_ERROR",
            "--idle-timeout", "120",
        )
        self.send("说句话")
        self.wait()

        view = self.view()
        finished = [
            e for e in view.of_type("agent_event") if e.get("event_type") == "finished"
        ]
        self.assertTrue(finished)
        self.assertEqual(finished[-1]["stop_reason"], "stream_error")

    def test_thinking_and_text_are_rendered(self):
        """AC32 消费侧：正文与思考块各自渲染到界面。"""
        self.start_host(
            "--mode", "scripted",
            "--script", "tests.e2e.scripts:THINK_AND_READ",
            "--seed", "tests.e2e.scripts:seed_basic",
            "--idle-timeout", "300",
        )
        self.send("读一下")
        self.wait()
        view = self.view()
        assert_check(check_ui_contains(view, "seed.txt"), view)
        assert_check(check_tool_outcome(view, "read_file", "executed"), view)

    def test_fallback_is_identifiable(self):
        """
        AC33：剧本耗尽后模型仍被调用（比如上下文摘要），
        兜底响应可识别，且会话正常回到空闲。
        """
        self.start_host(
            "--mode", "scripted",
            "--script", "tests.e2e.scripts:SAY_HELLO",  # 只有一轮
            "--idle-timeout", "300",
        )
        self.send("第一句")
        self.wait()
        self.send("第二句")  # 剧本已耗尽，走兜底
        self.assertEqual(self.wait()["data"]["terminal"], "idle", "兜底不得让会话卡住")

        # 判据取 `api_response`（模型实际回了什么）而不是 `ui_message`（界面上出现了什么）。
        # ⚠️ 两者在这里**不等价**：`ui_message` 的 AI 正文由 `reset_text_widgets()`
        # 在「下一轮开始 / 工具开始 / 历史回放」三个时机收尾产出，所以**一轮运行里
        # 最后一段正文不会产生 ui_message 事件**（实测：SAY_HELLO 跑完两轮，
        # ui_message 里只有两条 user_echo，两段 AI 正文一条都没有）。
        # 这是 P0 埋点的既有缺口，已单独记录；本条要验的「兜底可识别」
        # 权威来源本就是响应事件。
        view = self.view()
        texts = json.dumps(
            [e.get("text") for e in view.of_type("api_response")], ensure_ascii=False
        )
        self.assertIn("e2e-fallback", texts, "兜底响应必须一眼可识别")


class EncodingTest(HostFixture):
    """AC43：中文与字面 `[` 全链路不乱码。"""

    def test_utf8_and_markup_roundtrip(self):
        self.start_host(
            "--mode", "scripted",
            "--script", "tests.e2e.scripts:CHINESE_MARKUP",
            "--seed", "tests.e2e.scripts:seed_chinese",
            "--idle-timeout", "300",
        )
        self.send("读一下带方括号的文件")
        pending = self.wait()
        self.assertEqual(pending["data"]["terminal"], "pending")

        # status 的面板 display 是**含 markup 标记的原始字符串**，中文原样可读
        panel = self.status()["panel"]
        # ⚠ 判据从 `[dim]` 换成 `[#FFA500]`（tui-activity-fold 验收期）：
        # 表头原本在工具名后面拼一段 `[dim]· 判定原因[/dim]`，那是当时唯一的
        # `[dim]`。真机反馈把**第④层那句恒定的兜底话**（「默认模式：无规则命中」）
        # 撤了，于是这里没有 `[dim]` 了。
        # **这条判据要的是「原文保留 markup 标记、不做渲染」**，换一个仍然存在
        # 的标记即可——别顺手删掉它。
        # （同一处判据在 `test_e2e_control.py` 里还有一份，两处一起改的。）
        self.assertIn("[#FFA500]", panel["display"], "原文保留 markup 标记")
        self.assertIn("确认执行", panel["display"], "中文原样可读")

        self.answer("once")
        self.wait()

        # 记录以 UTF-8 完整解析，中文与字面方括号都原样保留
        view = self.view()
        self.assertEqual(view.skipped, 0)
        blob = json.dumps(view.records, ensure_ascii=False)
        self.assertIn("中文标题", blob)
        self.assertIn("[方括号]", blob)


class ConfigPassthroughTest(HostFixture):
    """
    `--config` 在 **scripted 模式**下也必须生效。

    ⚠️ **这条护栏是补出来的，起因是一次真实的静默失效**：`make_config` 早先在
    scripted 分支里完全无视 `--config`，直接返回写死的 `context_window=65536`。
    用 P1a 验 P0 的「压缩动作可解释」场景时传了 `context_window: 8192`，
    参数被悄悄丢掉、仍按 64K 判定，于是第二层摘要永远不触发——
    **现象出在 C8 那边**（看起来像上下文管理坏了），排查绕了一大圈才发现是这里吞了参数。

    **静默丢弃参数是最坏的一类缺陷**：调用方以为生效了，症状却出在别处。
    """

    def test_scripted_mode_honours_config(self):
        import tempfile

        import yaml

        cfg = Path(tempfile.gettempdir()) / f"e2e_cfgtest_{os.getpid()}.yaml"
        cfg.write_text(
            yaml.safe_dump({
                "protocol": "deepseek", "model": "deepseek-chat",
                "base_url": "https://api.deepseek.com", "api_key": "placeholder-should-be-replaced",
                "debug_log": False, "context_window": 8192,
            }, allow_unicode=True),
            encoding="utf-8",
        )
        self.addCleanup(cfg.unlink, True)

        self.start_host(
            "--mode", "scripted", "--script", "tests.e2e.scripts:SAY_HELLO",
            "--config", str(cfg), "--idle-timeout", "120",
        )
        snapshot = self.view().of_type("session_start")[0]
        self.assertEqual(
            snapshot["config"]["context_window"], 8192,
            "scripted 模式必须认 --config 里的 context_window，"
            "写死 65536 会让任何靠调窗口构造的场景静默失效",
        )
        # api_key 被强制换成假值：scripted 下真 key 本就用不上，没必要让它进内存与快照
        self.assertNotIn("placeholder-should-be-replaced", str(snapshot["config"]))

    def test_scripted_mode_without_config_uses_default(self):
        """不传 --config 时仍是原来的默认值（缺省行为不变）。"""
        self.start_host(
            "--mode", "scripted", "--script", "tests.e2e.scripts:SAY_HELLO",
            "--idle-timeout", "120",
        )
        snapshot = self.view().of_type("session_start")[0]
        self.assertEqual(snapshot["config"]["context_window"], 65536)


class CleanupTest(HostFixture):
    """AC42 / AC26：不留残留、连跑不污染。"""

    def test_no_leftovers_after_clean_exit(self):
        info = self.start_host(
            "--mode", "scripted", "--script", "tests.e2e.scripts:SAY_HELLO",
            "--idle-timeout", "120",
        )
        workspace, user_dir, pid = info.workspace, info.user_dir, info.pid
        self.quit_host()
        self.info = None  # 已自行清理，别让 tearDown 再删一次

        self.assertFalse(Path(workspace).exists(), "工作区应已被删除")
        self.assertFalse(Path(user_dir).exists(), "用户目录应已被删除")
        self.assertFalse(discovery.host_file(pid).exists(), "名片应已被删除")
        self.assertNotIn(pid, [h.pid for h in discovery.list_hosts()])

    def test_zzz_no_global_leftovers(self):
        """
        AC42 的**全局**一半：跑完全部宿主用例之后，系统临时目录里不该有本设施的残留。

        用例名以 `zzz` 开头是为了让 unittest 的字典序把它排在本类最后
        （**注意它只保证本类内的次序**，不保证跨类；真正的兜底仍然是每条用例
        自己的 `addCleanup`）。

        发现残留说明某条用例的清理漏了——**修那条用例，而不是放宽这条断言**（spec N7）。
        """
        temp_root = Path(os.environ.get("TEMP", "/tmp"))
        leftovers = [
            p.name
            for p in temp_root.glob("rhine_e2e_*")
            # 排除本用例自己那份仍在使用的 stderr 日志
            if p.name != self.stderr_path.name
        ]
        self.assertEqual(
            leftovers, [], f"系统临时目录里有 {len(leftovers)} 个残留：{leftovers[:10]}"
        )
        cards = list(discovery.publish_dir().glob("host-*.json")) if discovery.publish_dir().is_dir() else []
        self.assertEqual(cards, [], f"发布目录里有残留名片：{[c.name for c in cards]}")

    @unittest.skipUnless(SLOW_TESTS, "慢速专项：设 RHINE_E2E_SLOW=1 才跑")
    def test_repeated_start_stop(self):
        """AC24 专项：连续起停若干次，无一次因目录占用失败。"""
        for i in range(3):
            info = self.start_host(
                "--mode", "scripted", "--script", "tests.e2e.scripts:SAY_HELLO",
                "--idle-timeout", "60",
            )
            self.send("你好")
            self.wait()
            self.quit_host()
            self.info = None
            self.assertFalse(Path(info.workspace).exists(), f"第 {i + 1} 次未清理干净")


class InProcessCleanupTest(unittest.TestCase):
    """
    AC8 / AC26 的**进程内**一半。

    为什么要拆成两条：只读路径白名单与 MCP 连接都是**进程内内存状态**，
    宿主进程都退出了，外部无从检查——只能在测试进程内直接装配再验。
    """

    def test_cleanup_resets_process_globals(self):
        from rhinecode.bootstrap import build_app
        from rhinecode.config import Config
        from rhinecode.tools import path_guard

        cwd = Path.cwd()
        ws = sandbox.create_workspace()
        user = sandbox.create_user_dir()
        os.chdir(ws)
        path_guard.clear_read_roots()
        try:
            cfg = Config(
                protocol="deepseek", model="deepseek-chat",
                base_url="https://api.deepseek.com", api_key="fake",
                debug_log=False, context_window=65536,
            )
            result = build_app(cfg, user_dir=user)
            # `_EXTRA_READ_ROOTS` 没有公开访问器，直接读模块级列表——
            # 它正是本条要检查的那份**进程级全局状态**。
            self.assertTrue(path_guard._EXTRA_READ_ROOTS, "装配时应注册过只读根")
            result.cleanup("test")
            self.assertEqual(path_guard._EXTRA_READ_ROOTS, [], "cleanup 必须复位只读白名单")
            self.assertEqual(result.mcp_manager.states, [], "MCP 连接应已回收")

            # 连跑第二次：不得继承第一次注册的路径
            user2 = sandbox.create_user_dir()
            try:
                second = build_app(cfg, user_dir=user2)
                roots = {str(p) for p in path_guard._EXTRA_READ_ROOTS}
                self.assertFalse(
                    any(str(user) in r for r in roots),
                    f"第二次装配不得继承第一次的用户目录，实际：{roots}",
                )
                second.cleanup("test")
            finally:
                sandbox.force_rmtree(user2)
        finally:
            os.chdir(cwd)
            path_guard.clear_read_roots()
            sandbox.force_rmtree(ws)
            sandbox.force_rmtree(user)


if __name__ == "__main__":
    unittest.main()
