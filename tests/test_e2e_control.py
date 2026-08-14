"""
驱动内核测试（P1a T44–T46）。

覆盖：
- **T44 / AC6 内核级**：`shutdown_on_main` 的强制结算（**含反证**：不做强制结算
  就退不出去。这是 plan 里唯一被实测证明「写错就永久挂死」的地方，必须在此就地验，
  不能推到十几步之后的宿主测试）
- **AC13** 三态判据（重点：面板挂着时忙碌态仍为真，但判定必须是 PENDING）
- **AC3** `wait` 的两个终态与超时诊断
- **AC20** 取消后会话回到空闲且仍可再 `send`
- **AC22 / N6** 跨线程死锁护栏
- **AC14** 四类面板各应答一次
- **AC15** 同一次运行内 `via=channel` 与 `via=keys` 产出两种不同来源

本文件**不起宿主进程**：真实 `RhineApp` + `run_test` + 假模型，全部在测试进程内。
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
import threading
import time
import traceback
import unittest
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional
from unittest import mock

from rhinecode.bootstrap import build_app
from rhinecode.config import Config
from rhinecode.permission.models import PermissionMode
from rhinecode.tools import path_guard
from rhinecode.tui.widgets import InputBar
from rhinecode.trace.recorder import TraceRecorder
from tests.e2e import sandbox
from tests.e2e.assertions import TraceView
from tests.e2e.control import (
    DriverCore,
    ExternalResponder,
    SessionState,
    run_on_main,
)
from tests.e2e.scripted import ScriptedProvider, done, text, tool


def _history_text(app) -> str:
    """
    把聊天区全部 Static 行的可见文本拼接，供内容断言。

    与 `tests/test_command_tui.py` 的同名辅助同口径——控制通道读不到聊天区正文
    （它只暴露面板与 trace），所以「界面上真的出现了」这类断言必须直接查组件树。
    """
    from textual.widgets import Static as _Static

    parts = []
    for widget in app.query("#history-messages > *"):
        if isinstance(widget, _Static):
            try:
                parts.append(widget.render().plain)
            except Exception:
                parts.append(str(widget.render()))
    return "\n".join(parts)


# 宿主装配时一并摘掉的两个工具：前者写真实用户主目录且不吃 user_dir，
# 后者是 read_only 却要访问外部包索引（只读且被放行的工具根本不弹面板，拦不住）。
EXCLUDED = frozenset({"mcp_add_server", "mcp_resolve_server"})


def make_config(**over) -> Config:
    """一份不联网的假配置（create_provider 只构造客户端对象，假 key 够用）。"""
    base = dict(
        protocol="deepseek",
        model="deepseek-chat",
        base_url="https://api.deepseek.com",
        api_key="fake-key-for-test",
        debug_log=False,
        context_window=65536,
    )
    base.update(over)
    return Config(**base)


class DriverFixture(unittest.IsolatedAsyncioTestCase):
    """
    在临时工作区里装配一个**真实**应用，并给出 `DriverCore`。

    与 `test_bootstrap.py` 的 fixture 同口径（chdir 进临时目录 + 临时 user_dir +
    清理进程级只读白名单），额外接上假模型与真实记录器。
    """

    def setUp(self) -> None:
        self._cwd = Path.cwd()
        self.ws = sandbox.create_workspace()
        self.user_dir = sandbox.create_user_dir()
        os.chdir(self.ws)
        path_guard.clear_read_roots()
        self.trace_path = self.ws / ".rhinecode" / "traces" / "host.jsonl"
        self.trace_path.parent.mkdir(parents=True, exist_ok=True)
        self.result = None

    def tearDown(self) -> None:
        if self.result is not None:
            self.result.cleanup("test_teardown")
        os.chdir(self._cwd)
        path_guard.clear_read_roots()
        for path in (self.ws, self.user_dir):
            sandbox.force_rmtree(path)

    def assemble(
        self,
        turns=None,
        *,
        provider: Optional[ScriptedProvider] = None,
        plan_mode: bool = False,
        turn_budget: int = 40,
    ):
        """
        装配一次并返回 (app, provider)。自动记忆关掉——它是不确定性来源。

        :param provider: 直接给一个自定义假 Provider（如「故意很慢」的那种）。

        ⚠️ **假模型必须在装配时经 `provider_factory` 注入**，装配之后再改
        `manager._provider` 是**无效**的——`ConversationManager.__init__` 里
        `self._agent = Agent(provider, ...)`，Agent 在构造时就把 provider 捕获走了。
        （实测踩过：改了 `manager._provider` 之后循环照旧用原来那个，
        表现为「我明明换了慢模型，请求却 0ms 就回来了」。）
        """
        self.provider = provider if provider is not None else ScriptedProvider(turns)
        self.recorder = TraceRecorder(self.trace_path)
        self.result = build_app(
            make_config(),
            user_dir=self.user_dir,
            recorder=self.recorder,
            provider_factory=lambda cfg: self.provider,
            exclude_tools=EXCLUDED,
        )
        self.result.manager.memory_manager.memories_enabled = False
        # auto-plan 扩展：启动缺省档已是放行档（auto 预设），工作区内的普通写入
        # 不再弹面板。本文件大量用例把**确认面板当夹具**——它们验的是控制通道
        # （面板就绪的原子性、两条应答路径、结算与退出），面板只是个能稳定造出
        # `pending` 态的东西。不设回默认档的话，它们会等一个永远不来的 pending。
        #
        # ⚠ 它们**不能**改用保护路径那种面板：那个只有三个选项，
        # `permanent` 那一支覆盖不到。
        #
        # 产品缺省行为的验收在 `test_e2e_protected.py` 与 checklist 的 C 类场景，
        # 不在本文件——本文件验的是驱动设施本身。
        self.result.manager.permission_engine.set_mode(PermissionMode.DEFAULT)
        if plan_mode:
            self.result.manager.plan_mode = True
        self.turn_budget = turn_budget
        return self.result.app, self.provider

    def make_core(self, app, pilot, loop) -> DriverCore:
        self.responder = ExternalResponder()
        return DriverCore(
            app,
            pilot,
            loop,
            self.result,
            self.responder,
            turn_budget=self.turn_budget,
            trace_path=self.trace_path,
        )

    @asynccontextmanager
    async def driving(self, app, size=(120, 40)):
        """
        起应用 → 建 `DriverCore` → 用完**一定**收尾，收尾在 `finally` 里。

        用法：

        ```python
        app, provider = self.assemble(SOME_SCRIPT)
        async with self.driving(app) as (pilot, core):
            ...断言...
        ```

        ## 为什么收尾必须在 `finally` 里（这是本包装存在的唯一理由）

        原先每条用例把 `await core.shutdown_on_main("test")` 写在函数最后一行。
        **任何一条断言失败，那一行就不会执行**，后果不是「这条红了」而是
        **整个测试套件永久挂住**：

        ```
        断言抛出
          → 强制结算没做，被阻塞在确认盒 `box["event"].wait()` 上的工作线程
            永远醒不过来
          → IsolatedAsyncioTestCase 收尾调 loop.shutdown_default_executor()
          → Python 3.11 的该方法**没有超时参数、永不返回**（3.12 才加了 5 分钟默认值）
          → 卡死
        ```

        标准 runner 把 traceback 攒到最后统一打印，而这里根本走不到「最后」——
        tui-activity-fold 验收期因此连续几次「跑十几分钟不出结果、只看到一串点
        和一个 F」，**不知道哪条红、更不知道为什么红**，掩盖了两个真实缺陷。
        挂起的进程还不退出，几个并存时互相争 CPU，把别的时序敏感用例也压翻
        ——「偶发 flaky」里有一部分是这么来的。

        ## 收尾自己抛异常时，绝不替换原始错误

        这就是下面分成 `except` / `else` 两支而不是写一个 `finally` 的原因：

        - **用例已经失败**：收尾只做尽力而为。它自己抛出的异常打到 stderr 作为
          **附加**信息，然后 `raise` 把原始断言错误原样放走。
          用收尾的异常盖掉原始错误比挂起好一点，但仍然把「为什么红」丢了。
        - **用例通过**：收尾若失败，那本身就是个真问题（关停语义变了），照常抛出。

        :param app: `assemble()` 返回的应用
        :param size: 终端尺寸，与既有用例一致的 (120, 40)
        :yields: `(pilot, core)`
        """
        async with app.run_test(size=size) as pilot:
            core = self.make_core(app, pilot, asyncio.get_running_loop())
            try:
                yield pilot, core
            except BaseException:
                try:
                    await core.shutdown_on_main("test")
                except BaseException:  # noqa: BLE001
                    print(
                        "⚠ driving() 收尾失败（原始错误见下方，此处仅为附加信息）：",
                        file=sys.stderr,
                    )
                    traceback.print_exc(file=sys.stderr)
                raise
            else:
                await core.shutdown_on_main("test")

    def view(self) -> TraceView:
        """读一份当前的记录视图（先 flush 由记录器在每次 emit 时保证）。"""
        return TraceView.load(self.trace_path)

    def interactions(self) -> list[dict]:
        return self.view().of_type("interaction")


# 一个会触发确认面板的两轮剧本：write_file 非只读，默认模式下无规则命中 → 问用户
CONFIRM_SCRIPT = [
    [text("我来写个文件。"), tool("write_file", {"path": "x.txt", "content": "hi"}), done()],
    [text("写完了。"), done()],
]


class RunOnMainTest(DriverFixture):
    async def test_timeout_is_real(self):
        """
        `run_on_main` 的超时确实生效——这是它相对 `call_from_thread` 存在的全部理由
        （后者没有超时参数，会一直阻塞到工作跑完）。
        """
        app, _ = self.assemble([[text("hi"), done()]])
        # 这条用例不驱动会话（没有面板、没有被阻塞的工作线程），本可以直接用
        # `app.run_test`。仍然走 `driving()` 是为了**不留下第二种写法**——
        # 留一条例外，下一个人照着它写新用例时就把收尾又漏在最后一行了。
        async with self.driving(app) as (pilot, core):
            loop = asyncio.get_running_loop()

            async def _slow() -> None:
                await asyncio.sleep(5.0)

            def _call():
                started = time.monotonic()
                try:
                    run_on_main(loop, _slow(), timeout=0.5)
                    return None, time.monotonic() - started
                except TimeoutError:
                    return "timeout", time.monotonic() - started

            kind, elapsed = await asyncio.to_thread(_call)
            self.assertEqual(kind, "timeout")
            self.assertLess(elapsed, 2.0, f"必须在给定超时附近返回，实测 {elapsed:.2f}s")


class StateJudgementTest(DriverFixture):
    """AC13：三态判据。"""

    async def test_idle_then_pending_then_idle(self):
        app, provider = self.assemble(CONFIRM_SCRIPT)
        async with self.driving(app) as (pilot, core):

            snap = await asyncio.to_thread(core.snapshot)
            self.assertEqual(snap["state"], SessionState.IDLE.value)
            self.assertIsNone(snap["panel"])

            await asyncio.to_thread(core.send, "写个文件")
            waited = await asyncio.to_thread(core.wait, 30.0)
            self.assertTrue(waited["ok"])
            self.assertEqual(waited["data"]["terminal"], SessionState.PENDING.value)

            snap = await asyncio.to_thread(core.snapshot)
            self.assertEqual(snap["state"], SessionState.PENDING.value)
            # ⚠️ 关键断言：面板挂着的时候**忙碌态仍然为真**（Worker 正阻塞等结算），
            # 但判定必须是 PENDING。三态推导的顺序颠倒过来，
            # 所有 PENDING 都会被误报成 BUSY，wait 就永远等不到「需要你应答」。
            self.assertTrue(snap["stream_active"], "面板挂着时忙碌态本就为真（实测）")

            await asyncio.to_thread(core.answer, "once")
            done_wait = await asyncio.to_thread(core.wait, 30.0)
            self.assertEqual(done_wait["data"]["terminal"], SessionState.IDLE.value)
            self.assertFalse((await asyncio.to_thread(core.snapshot))["stream_active"])

    async def test_send_rejected_when_not_idle(self):
        app, _ = self.assemble(CONFIRM_SCRIPT)
        async with self.driving(app) as (pilot, core):
            await asyncio.to_thread(core.send, "写个文件")
            await asyncio.to_thread(core.wait, 30.0)

            busy = await asyncio.to_thread(core.send, "再来一句")
            self.assertFalse(busy["ok"])
            self.assertEqual(busy["error"]["code"], "busy")

            await asyncio.to_thread(core.answer, "deny")
            await asyncio.to_thread(core.wait, 30.0)

    async def test_turn_budget_blocks_send(self):
        app, _ = self.assemble([[text("好"), done()]], turn_budget=1)
        async with self.driving(app) as (pilot, core):
            self.assertTrue((await asyncio.to_thread(core.send, "第一句"))["ok"])
            await asyncio.to_thread(core.wait, 30.0)

            blocked = await asyncio.to_thread(core.send, "第二句")
            self.assertFalse(blocked["ok"])
            self.assertEqual(blocked["error"]["code"], "turn_budget")
            self.assertIn("--max-turns", blocked["error"]["message"], "要给出可照做的下一步")


class WaitTest(DriverFixture):
    """AC3 / AC21：等待两终态与超时诊断。"""

    async def test_timeout_diagnostics(self):
        """
        ⚠️ 超时用例的假模型必须是「**耗时超过等待上限**」而不是「永不结束」：
        非守护线程会在解释器退出时被汇合，真「永不结束」会让**测试进程整个挂死**。
        """
        slow = threading.Event()

        class SlowProvider(ScriptedProvider):
            def stream_chat(self, *a, **kw):
                # 至多 3 秒，远超本用例 0.6 秒的等待上限，但一定会结束
                slow.wait(timeout=3.0)
                yield text("终于说完了")
                yield done()

        app, _ = self.assemble(provider=SlowProvider())
        async with self.driving(app) as (pilot, core):
            await asyncio.to_thread(core.send, "慢一点")
            timed_out = await asyncio.to_thread(core.wait, 0.6)

            self.assertFalse(timed_out["ok"])
            self.assertEqual(timed_out["error"]["code"], "timeout")
            data = timed_out["error"]["data"]
            for key in ("stream_active", "pending", "session_panel", "last_action", "waited", "state"):
                self.assertIn(key, data, f"超时诊断缺字段 {key}")
            self.assertIn("慢一点", data["last_action"], "诊断要能看出卡在哪个动作上")
            self.assertEqual(data["state"], SessionState.BUSY.value)

            slow.set()  # 放行，让它自然结束
            await asyncio.to_thread(core.wait, 30.0)


class QuiescentWaitTest(DriverFixture):
    """
    `wait --until quiescent`：**「idle」不等于「系统静止」**。

    ## 这组用例在防什么

    三态只描述界面。C13 起，界面空闲时进程里仍可能有活：后台委派在跑，
    或者队友消息躺在信箱里等主对话**自动唤起**（`_maybe_auto_wake` 每 0.5 秒
    在空闲时检查一次，符合条件就自己起一条流）。

    于是 `wait` 返回 `idle` 之后会话随时可能又忙起来，**基于它的断言是竞态的**
    ——而竞态判据比没有判据更坏：它偶尔通过，于是没人相信失败是真的。
    """

    async def test_terminal_mode_is_unchanged_by_default(self):
        """
        **零回归**：缺省 `until="terminal"` 时行为与改造前逐字相同。

        这条排在最前是刻意的——既有的 C2–C11 场景全都不传 `until`，
        它们的行为一个字都不该变。
        """
        app, _ = self.assemble([[text("好的"), done()]])
        async with self.driving(app) as (pilot, core):
            await asyncio.to_thread(core.send, "你好")
            res = await asyncio.to_thread(core.wait, 30.0)
            self.assertTrue(res["ok"])
            self.assertEqual(res["data"]["terminal"], SessionState.IDLE.value)

    async def test_status_reports_background_and_quiescent(self):
        """
        没有任何后台活动时：`background` 三个键都在，`quiescent` 为真。

        字段必须**存在**而不只是「值对”——它们是场景的读取契约，
        少一个键会让断言以 KeyError 的形式失败在一个和判据无关的地方。
        """
        app, _ = self.assemble([[text("好"), done()]])
        async with self.driving(app) as (pilot, core):
            await asyncio.to_thread(core.send, "你好")
            await asyncio.to_thread(core.wait, 30.0)

            snap = await asyncio.to_thread(core.snapshot)
            for key in ("subagents", "idle_members", "unread_for_main"):
                self.assertIn(key, snap["background"], f"background 缺字段 {key}")
            self.assertTrue(snap["quiescent"], "没有后台活动时应当判为静止")

    async def test_quiescent_waits_for_background_subagents(self):
        """
        后台还有子 Agent 在跑时，`quiescent` 模式**不得**提前返回。

        用打桩的方式伪造「还有 1 个在跑」——起一个真子 Agent 会把这条用例
        变成一个依赖模型剧本与线程时序的集成测试，而这里要验的只是
        **判据本身**：`subagents > 0` 时不算静止。
        """
        app, _ = self.assemble([[text("好"), done()]])
        async with self.driving(app) as (pilot, core):
            await asyncio.to_thread(core.send, "你好")
            await asyncio.to_thread(core.wait, 30.0)

            manager = app._manager
            manager.running_subagent_count = lambda: 1
            try:
                res = await asyncio.to_thread(core.wait, 0.4, "quiescent")
                self.assertFalse(res["ok"], "还有子 Agent 在跑就不该判为静止")
                self.assertEqual(res["error"]["code"], "timeout")
                data = res["error"]["data"]
                # 超时诊断必须能区分「子 Agent 还在跑」与「有消息没人处理」
                self.assertEqual(data["until"], "quiescent")
                self.assertEqual(data["background"]["subagents"], 1)
                self.assertEqual(data["state"], SessionState.IDLE.value)
            finally:
                del manager.running_subagent_count

            # 恢复之后立刻能等到
            res = await asyncio.to_thread(core.wait, 5.0, "quiescent")
            self.assertTrue(res["ok"])

    async def test_quiescent_waits_for_pending_auto_wake(self):
        """
        信箱里有给 `main` 的未读消息时不算静止——它会自己起一条流。

        这是 C15 特有的形态，也是最容易骗过 `terminal` 模式的那个：
        界面此刻确实空闲，一秒后却开始跑一整轮。
        """
        app, _ = self.assemble([[text("好"), done()]])
        async with self.driving(app) as (pilot, core):
            await asyncio.to_thread(core.send, "你好")
            await asyncio.to_thread(core.wait, 30.0)

            manager = app._manager
            manager.team_has_unread_for_main = lambda: True
            try:
                res = await asyncio.to_thread(core.wait, 0.4, "quiescent")
                self.assertFalse(res["ok"])
                self.assertIs(res["error"]["data"]["background"]["unread_for_main"], True)
            finally:
                del manager.team_has_unread_for_main

    async def test_idle_members_alone_do_not_block_quiescence(self):
        """
        ⚠ **反证：待命队员不算「还在动」。**

        C15 有一条明写的不变量——主对话可以在队员待命时正常收工
        （`TeamGate.has_awaited` 恒为假）。把待命人数算进静止判据的话，
        `wait --until quiescent` 会**永远等不到**，然后超时，
        然后下一个人把这条判据整条删掉。

        这与 CLAUDE.md 那条「绝不要给 TaskStatus 加 is_terminal 为假的 IDLE」
        是同一个坑：待命是稳定状态，不是未完成的工作。
        """
        app, _ = self.assemble([[text("好"), done()]])
        async with self.driving(app) as (pilot, core):
            await asyncio.to_thread(core.send, "你好")
            await asyncio.to_thread(core.wait, 30.0)

            manager = app._manager
            manager.team_idle_member_count = lambda: 3
            try:
                res = await asyncio.to_thread(core.wait, 5.0, "quiescent")
                self.assertTrue(res["ok"], "有队员待命仍应判为静止")
                snap = await asyncio.to_thread(core.snapshot)
                self.assertEqual(snap["background"]["idle_members"], 3)
                self.assertTrue(snap["quiescent"])
            finally:
                del manager.team_idle_member_count


class CancelTest(DriverFixture):
    """AC20：取消后循环以「用户取消」结束，会话回到空闲且仍可再 send。"""

    async def test_cancel_then_still_usable(self):
        gate = threading.Event()

        class GatedProvider(ScriptedProvider):
            """
            第一轮卡住等放行、并**发起一个只读工具调用**；之后每轮立即作答。

            为什么第一轮必须带工具调用：取消信号是在**每轮循环开头**轮询的
            （`loop.py` 的 `if cancel_event.is_set()`）。若第一轮就自然结束，
            循环压根走不到第二轮，停止原因会是 `completed` 而不是 `cancelled`
            ——那验的就不是取消了。带上工具调用，循环才会进入第二轮并读到取消信号。
            """

            def __init__(self):
                super().__init__()
                self._first = True

            def stream_chat(self, *a, **kw):
                if self._first:
                    self._first = False
                    gate.wait(timeout=5.0)
                    yield text("我先看一眼文件。")
                    yield tool("read_file", {"path": "seed.txt"})
                    yield done()
                    return
                yield text("这一轮说完了")
                yield done()

        app, _ = self.assemble(provider=GatedProvider())
        (self.ws / "seed.txt").write_text("内容\n", encoding="utf-8")
        async with self.driving(app) as (pilot, core):
            await asyncio.to_thread(core.send, "干个长活")
            # 等它真的忙起来，否则取消会打在一个还没开始的循环上
            deadline = time.monotonic() + 5.0
            while time.monotonic() < deadline and not app._stream_active:
                await asyncio.sleep(0.02)
            self.assertTrue(app._stream_active, "循环应当已经跑起来了")

            cancelled = await asyncio.to_thread(core.cancel)
            self.assertTrue(cancelled["ok"])

            gate.set()
            back = await asyncio.to_thread(core.wait, 30.0)
            self.assertTrue(back["ok"])
            self.assertEqual(back["data"]["terminal"], SessionState.IDLE.value)

            # 循环以「用户取消」结束
            finished = [
                e
                for e in self.view().of_type("agent_event")
                if e.get("event_type") == "finished"
            ]
            self.assertTrue(finished, "必须有一条结束事件")
            self.assertEqual(finished[-1].get("stop_reason"), "user_cancelled")

            # 取消之后仍然可以再提交（取消的是这一轮，不是整个会话）
            again = await asyncio.to_thread(core.send, "再来")
            self.assertTrue(again["ok"], "取消后必须仍可再 send")
            after = await asyncio.to_thread(core.wait, 30.0)
            self.assertEqual(after["data"]["terminal"], SessionState.IDLE.value)


class ShutdownTest(DriverFixture):
    """
    T44 / AC6 内核级：退出时的强制结算。

    **这是 plan 里唯一被实测证明「写错就永久挂死」的地方**，必须在内核层就地验证，
    不能推到十几步之后的宿主进程测试。
    """

    async def test_forced_settlement_lets_process_exit(self):
        app, _ = self.assemble(CONFIRM_SCRIPT)
        async with self.driving(app) as (pilot, core):
            await asyncio.to_thread(core.send, "写个文件")
            waited = await asyncio.to_thread(core.wait, 30.0)
            self.assertEqual(waited["data"]["terminal"], SessionState.PENDING.value)

            # ⚠ 这一条是**故意在中途**调关停的——被测的就是它本身，后面还要
            # 接着断言它的效果。`driving()` 退出时会再调一次，靠的是
            # `shutdown_on_main` 的显式幂等标志（见其 docstring）。
            started = time.monotonic()
            await core.shutdown_on_main("quit")
            elapsed = time.monotonic() - started
            self.assertLess(elapsed, 20.0, f"必须在上限内返回，实测 {elapsed:.2f}s")

            # 强制结算必须在记录里留下痕迹（来源单列以便审计）
            forced = [e for e in self.interactions() if e.get("source") == "driver_forced"]
            self.assertTrue(forced, "记录里必须有一条 source=driver_forced 的交互事件")
            self.assertEqual(forced[0]["result"], "deny", "安全默认值是最保守的那一档")

            # 被阻塞的工作线程必须已经醒来退出
            deadline = time.monotonic() + 5.0
            while time.monotonic() < deadline:
                if not app._stream_active:
                    break
                await asyncio.sleep(0.05)
            self.assertFalse(app._stream_active, "工作线程仍被阻塞，进程将无法退出")

    async def test_counter_proof_without_forced_settlement_it_hangs(self):
        """
        **反证**：跳过强制结算、只等忙碌态转假，在短上限内**等不到**。

        这条用例证明强制结算是**必需**的，不是保险性质的多余代码——
        少了它，`asyncio.run()` 收尾会去 join 那个阻塞在 `box["event"].wait()` 的
        工作线程，而 Python 3.11 的 `shutdown_default_executor` 没有超时参数、永不返回。
        """
        app, _ = self.assemble(CONFIRM_SCRIPT)
        async with self.driving(app) as (pilot, core):
            await asyncio.to_thread(core.send, "写个文件")
            await asyncio.to_thread(core.wait, 30.0)

            deadline = time.monotonic() + 1.5
            while time.monotonic() < deadline:
                if not app._stream_active:
                    break
                await asyncio.sleep(0.05)
            self.assertTrue(
                app._stream_active,
                "不做强制结算就应当一直卡住——若这条断言失败，说明产品侧的阻塞语义变了，"
                "shutdown_on_main 的交织循环需要重新评估",
            )
            # 收尾（正常路径退出、别把挂着的面板留给 tearDown）由 `driving()` 做。
            # ⚠ 这条用例**必须**依赖那个 finally：上面那句断言正是「面板还挂着、
            # 工作线程还堵着」的时刻，它一旦红了而收尾没跑，就是本 todo 描述的
            # 那次永久挂起——挂在验证「不挂起」的用例上。


class DeadlockGuardTest(DriverFixture):
    """
    AC22 / N6：跨线程死锁护栏。

    另起一个线程反复 `snapshot()`，主线程同时驱动交互。
    用**完成计数**而不是布尔标志判定——同线程版本在可重入锁下会静默通过，
    **不可简化成单线程写法**。
    """

    async def test_concurrent_snapshot_while_driving(self):
        app, _ = self.assemble(CONFIRM_SCRIPT)
        async with self.driving(app) as (pilot, core):
            completed = [0]
            stop = threading.Event()
            errors: list[Exception] = []

            def poller():
                while not stop.is_set():
                    try:
                        core.snapshot()
                        completed[0] += 1
                    except Exception as e:  # noqa: BLE001
                        errors.append(e)
                        return
                    time.sleep(0.01)

            thread = threading.Thread(target=poller, name="e2e-poller", daemon=True)
            thread.start()
            try:
                await asyncio.to_thread(core.send, "写个文件")
                await asyncio.to_thread(core.wait, 30.0)
                await asyncio.to_thread(core.answer, "once")
                await asyncio.to_thread(core.wait, 30.0)
            finally:
                stop.set()
                # ⚠️ **join 必须放到工作线程上做，不能在这里直接 join**：
                # 本协程跑在 Textual 的事件循环线程上，直接 `thread.join()` 会把
                # 事件循环整个堵住；而轮询线程此刻可能正卡在 `run_on_main` 上等
                # 那个循环执行它的协程——两边互等，**测试自己就死锁了**
                # （实测：join(10) 超时、轮询线程仍活着，看起来像被测代码死锁，
                #  其实是测试写法的问题）。
                await asyncio.to_thread(thread.join, 10.0)

            self.assertFalse(thread.is_alive(), "轮询线程未在上限内退出——疑似死锁")
            self.assertEqual(errors, [])
            self.assertGreater(completed[0], 5, "轮询线程必须持续跑完多次快照，而不是卡住")


class PanelAnswerTest(DriverFixture):
    """AC14 / AC15：四类面板的应答，以及两种来源。"""

    async def test_confirm_panel_full_cycle(self):
        app, provider = self.assemble(CONFIRM_SCRIPT)
        async with self.driving(app) as (pilot, core):
            await asyncio.to_thread(core.send, "写个文件")
            await asyncio.to_thread(core.wait, 30.0)

            snap = await asyncio.to_thread(core.snapshot)
            panel = snap["panel"]
            # ① 应答前两个前置态都成立
            self.assertTrue(snap["panel_visible"])
            self.assertEqual(snap["focused"], "ConfirmPanel")
            # 面板原文取自 0 号 disabled 表头，且是**含 markup 标记的原始字符串**。
            #
            # ⚠ 表头现在走 B 组的主参数口径（tui-display 扩展 F12）：显示
            # `Write(x.txt)` 而不是内部名 + 键值对。判据跟着改成**展示标签**
            # ——它才是用户实际看到的字；option 的 id 那条断言（下面）
            # 才是契约，那个一字未动。
            self.assertIn("Write(x.txt)", panel["display"])
            # ⚠ 判据从 `[dim]` 换成 `[#FFA500]`（tui-activity-fold 验收期）：
            # 表头原本在工具名后面拼一段 `[dim]· 判定原因[/dim]`，那是当时唯一的
            # `[dim]`。真机反馈把那段删了（它恒为「默认模式：无规则命中」，
            # 每次都一样、还把要读的 `工具名(参数)` 挤到一边）。
            # **这条判据要的是「原文保留 markup 标记、不做渲染」**，换一个仍然
            # 存在的标记即可——别顺手删掉它。
            self.assertIn("[#FFA500]", panel["display"], "原文保留 markup 标记，不做渲染")
            # 可选项四个，且不含任何 disabled 项（表头已被跳过）
            self.assertEqual(
                [o["id"] for o in panel["options"]],
                ["yes", "yes_session", "yes_permanent", "no"],
            )

            await asyncio.to_thread(core.answer, "once")
            await asyncio.to_thread(core.wait, 30.0)

            # ② 恰好一条交互事件
            events = [e for e in self.interactions() if e["kind"] == "confirm"]
            self.assertEqual(len(events), 1)
            self.assertEqual(events[0]["source"], "driver")
            self.assertEqual(events[0]["result"], "allow")
            # ③ 被阻塞的循环得以继续：第二轮模型请求发生了，文件也真写了
            self.assertEqual(len(provider.calls), 2, "应答后循环必须继续跑下一轮")
            self.assertTrue((self.ws / "x.txt").is_file())

    async def test_answer_deny_and_via_keys_sources(self):
        """
        **AC15**：同一次运行内 `via=channel` 与 `via=keys` 各应答一次，
        两条交互事件的来源分别是 `driver` 与 `human`。
        """
        script = [
            [text("先写一个"), tool("write_file", {"path": "a.txt", "content": "1"}), done()],
            [text("再写一个"), tool("write_file", {"path": "b.txt", "content": "2"}), done()],
            [text("都写完了"), done()],
        ]
        app, provider = self.assemble(script)
        async with self.driving(app) as (pilot, core):
            await asyncio.to_thread(core.send, "写两个文件")
            await asyncio.to_thread(core.wait, 30.0)
            first = await asyncio.to_thread(core.answer, "once", "channel")
            self.assertEqual(first["data"]["source"], "driver")

            await asyncio.to_thread(core.wait, 30.0)
            second = await asyncio.to_thread(core.answer, "deny", "keys")
            self.assertTrue(second["ok"], second)
            self.assertEqual(second["data"]["source"], "human")
            await asyncio.to_thread(core.wait, 30.0)

            events = [e for e in self.interactions() if e["kind"] == "confirm"]
            self.assertEqual(len(events), 2)
            self.assertEqual([e["source"] for e in events], ["driver", "human"])
            self.assertEqual([e["result"] for e in events], ["allow", "deny"])
            # via=keys 走的是面板自身的按键路径，结果同样生效
            self.assertTrue((self.ws / "a.txt").is_file())
            self.assertFalse((self.ws / "b.txt").exists(), "第二次选了拒绝，文件不该存在")

    async def test_answer_when_no_panel(self):
        app, _ = self.assemble([[text("好"), done()]])
        async with self.driving(app) as (pilot, core):
            res = await asyncio.to_thread(core.answer, "once")
            self.assertFalse(res["ok"])
            self.assertEqual(res["error"]["code"], "not_pending")

    async def test_bad_choice_is_rejected_before_touching_product(self):
        app, _ = self.assemble(CONFIRM_SCRIPT)
        async with self.driving(app) as (pilot, core):
            await asyncio.to_thread(core.send, "写个文件")
            await asyncio.to_thread(core.wait, 30.0)

            bad = await asyncio.to_thread(core.answer, "yes")  # confirm 不认识 yes
            self.assertFalse(bad["ok"])
            self.assertEqual(bad["error"]["code"], "bad_request")
            # 面板仍然挂着，非法应答不该有任何副作用
            self.assertEqual(
                (await asyncio.to_thread(core.snapshot))["state"], SessionState.PENDING.value
            )

            await asyncio.to_thread(core.answer, "deny")
            await asyncio.to_thread(core.wait, 30.0)


class PlanPanelTest(DriverFixture):
    """AC14 的另外两类：计划审批（approve）与需求澄清（clarify）。"""

    async def test_approve_panel(self):
        script = [
            [
                text("我先给个计划。"),
                tool("present_plan", {"plan": "第一步：读代码\n第二步：改代码"}),
                done(),
            ],
            [text("按计划执行完毕。"), done()],
        ]
        app, provider = self.assemble(script, plan_mode=True)
        async with self.driving(app) as (pilot, core):
            await asyncio.to_thread(core.send, "做个计划")
            waited = await asyncio.to_thread(core.wait, 30.0)
            self.assertEqual(waited["data"]["terminal"], SessionState.PENDING.value)

            snap = await asyncio.to_thread(core.snapshot)
            self.assertEqual(snap["panel"]["kind"], "approve")
            self.assertEqual(snap["focused"], "ConfirmPanel", "approve 复用 ConfirmPanel 控件")
            self.assertEqual([o["id"] for o in snap["panel"]["options"]], ["yes", "no"])

            await asyncio.to_thread(core.answer, "yes")
            await asyncio.to_thread(core.wait, 30.0)

            events = [e for e in self.interactions() if e["kind"] == "approve"]
            self.assertEqual(len(events), 1)
            self.assertEqual(events[0]["source"], "driver")
            self.assertGreaterEqual(len(provider.calls), 2, "批准后循环必须继续")

    async def test_clarify_panel(self):
        script = [
            [
                text("有几个方向不确定。"),
                tool(
                    "ask_user",
                    {
                        "question": "先改哪一块？",
                        "options": [
                            {"summary": "先改登录", "detail": "认证模块超时"},
                            {"summary": "先改上传", "detail": "大文件失败"},
                        ],
                    },
                ),
                done(),
            ],
            [text("好，按你说的来。"), done()],
        ]
        app, provider = self.assemble(script, plan_mode=True)
        async with self.driving(app) as (pilot, core):
            await asyncio.to_thread(core.send, "问问我")
            waited = await asyncio.to_thread(core.wait, 30.0)
            self.assertEqual(waited["data"]["terminal"], SessionState.PENDING.value)

            snap = await asyncio.to_thread(core.snapshot)
            self.assertEqual(snap["panel"]["kind"], "clarify")
            # ⚠️ ClarifyPanel 在候选项之间夹着 disabled 详情行，
            # options 必须只剩两个真候选（详情行被跳过）
            self.assertEqual([o["id"] for o in snap["panel"]["options"]], ["0", "1"])

            # clarify + keys 在协议层被拒（详情行使按键次数推不稳）
            rejected = await asyncio.to_thread(core.answer, "1", "keys")
            self.assertFalse(rejected["ok"])
            self.assertEqual(rejected["error"]["code"], "bad_request")

            await asyncio.to_thread(core.answer, "1")
            await asyncio.to_thread(core.wait, 30.0)

            events = [e for e in self.interactions() if e["kind"] == "clarify"]
            self.assertEqual(len(events), 1)
            self.assertEqual(events[0]["source"], "driver")
            self.assertEqual(events[0]["result"], "先改上传", "结算值取自 App 的澄清选项列表")
            self.assertGreaterEqual(len(provider.calls), 2, "澄清后循环必须继续")


class SessionPanelTest(DriverFixture):
    """AC14 的第四类：会话选择面板。它不隶属任何循环，判据是「历史被载入」。"""

    def _seed_other_session(self) -> str:
        """
        在工作区里预置一份**别的**会话存档，让 `/resume` 有东西可列。

        存档格式与 c9 的 `SessionStore` 一致：一行一条消息的 JSONL。
        """
        sessions = self.ws / ".rhinecode" / "sessions"
        sessions.mkdir(parents=True, exist_ok=True)
        session_id = "20260101-120000-aaaa"
        rows = [
            {"role": "user", "content": "上一场会话的问题", "ts": "2026-01-01T12:00:00"},
            {"role": "assistant", "content": "上一场会话的回答", "ts": "2026-01-01T12:00:05"},
        ]
        (sessions / f"{session_id}.jsonl").write_text(
            "\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n", encoding="utf-8"
        )
        return session_id

    async def test_session_panel_select_loads_history(self):
        session_id = self._seed_other_session()
        app, _ = self.assemble([[text("好"), done()]])
        async with self.driving(app) as (pilot, core):
            await asyncio.to_thread(core.send, "/resume")

            waited = await asyncio.to_thread(core.wait, 30.0)
            self.assertTrue(waited["ok"], waited)
            snap = await asyncio.to_thread(core.snapshot)
            self.assertEqual(snap["state"], SessionState.PENDING.value)
            self.assertEqual(snap["panel"]["kind"], "session")
            self.assertEqual(snap["focused"], "SessionPanel")
            ids = [o["id"] for o in snap["panel"]["options"]]
            self.assertIn(session_id, ids, f"预置的会话应当可选，实际候选：{ids}")

            await asyncio.to_thread(core.answer, session_id)
            await asyncio.to_thread(core.wait, 30.0)

            events = [e for e in self.interactions() if e["kind"] == "session"]
            self.assertEqual(len(events), 1, "一次会话选择恰好一条交互事件")
            self.assertEqual(events[0]["source"], "driver")
            self.assertEqual(events[0]["result"], "selected")
            # ④ 会话面板不隶属任何循环，判据是**历史被载入**
            deadline = time.monotonic() + 10.0
            while time.monotonic() < deadline:
                if any("上一场会话" in (m.content or "") for m in self.result.manager.history):
                    break
                await asyncio.sleep(0.05)
            self.assertTrue(
                any("上一场会话" in (m.content or "") for m in self.result.manager.history),
                "选中会话后主历史必须换成那一场的内容",
            )

    async def test_session_panel_cancel(self):
        self._seed_other_session()
        app, _ = self.assemble([[text("好"), done()]])
        async with self.driving(app) as (pilot, core):
            await asyncio.to_thread(core.send, "/resume")
            await asyncio.to_thread(core.wait, 30.0)

            await asyncio.to_thread(core.answer, "cancel")
            back = await asyncio.to_thread(core.wait, 30.0)
            self.assertEqual(back["data"]["terminal"], SessionState.IDLE.value)

            events = [e for e in self.interactions() if e["kind"] == "session"]
            self.assertEqual(len(events), 1)
            self.assertEqual(events[0]["result"], "cancelled")


class PanelArmingIsAtomicTest(DriverFixture):
    """
    **「正在等你应答」与「面板已经画出来」必须同时成立**（全量测试实测抓到）。

    ## 这是一个真实的产品竞态，不是测试写法问题

    `_interact` 原先在**工作线程**上先写 `self._pending_interaction = box`，
    再 `call_from_thread(show_fn)` 弹面板。中间那一小段时间里，程序对外
    宣称「pending」，而屏幕上什么都没有。

    tui-activity-fold 把窗口拉宽了——F18 要在弹面板前先把状态行切到
    「等待确认」，那是**第二次**跨线程往返。于是全量测试开始偶发红：
    `wait` 返回 pending 之后立刻取快照，`panel_visible` 是 `False`、
    `focused` 还停在 `InputBar`。**单跑必过、全量偶发**——竞态的典型形态。

    修法是把「登记 + 改状态行 + 弹面板」合成主线程上的**一次**调用，
    外部因此不可能观察到中间态。

    ⚠ 判据必须**连着取好几次快照**：竞态窗口只有几毫秒，取一次很容易
    恰好落在窗口外面，那样这条护栏就退化成了一句安慰。

    ⚠ **这条护栏是概率性的，如实记在这里**：把 `_interact` 改回旧写法之后
    连跑六次，红了**一次**。也就是说它抓得住这个缺陷，但不保证每次都抓住
    ——全量测试里之所以频繁翻车，是因为并发负载把那个窗口撑大了。
    别因为「单跑绿了」就认为竞态不存在。

    ## ⚠ 焦点**不属于**这条原子性判据，它结构上就晚一拍

    本用例原先在「pending 出现的那一瞬间」同时断言 `panel_visible` 与
    `focused == "ConfirmPanel"`。**后者是错的判据**，2026-08-15 收尾改造后
    全量测试当场红出来（此前它一失败就是一次永久挂起，所以从没人见过它）：

    - `panel_visible` 读的是控件的 `display`，`show_fn()` 在 `_arm` 里**同步**设好
      ——它确实被那一次主线程调用覆盖，是原子的；
    - `focused` 读的是 `app.focused`，而 Textual 8.2.7 的 `Widget.focus()` 是
      `self.app.call_later(set_focus, self)`——**排进主循环的下一个回调**，
      不在 `_arm` 那一次调用里。产品这边无从「合成一次」，除非改 Textual。

    佐证：驱动器自己的 `answer()` 就是**轮询等**两者一起就绪的
    （`control.py` 的「应答前复核面板就绪」不变量），它从不假设焦点立刻到位。
    只有这条用例在第一瞬间就断言它。

    所以现在分成两段判：**原子性只判 `panel_visible`**（那才是 `_arm` 承诺的
    东西，也是回归真正会破坏的那一条），焦点单独判、允许晚一拍但**必须最终到达**
    ——去掉它会让「焦点永远不落到面板上」这种真回归无人看守。
    """

    async def test_pending_never_precedes_the_panel(self):
        app, _ = self.assemble(CONFIRM_SCRIPT)
        async with self.driving(app) as (pilot, core):
            await asyncio.to_thread(core.send, "写个文件")

            # 一路盯着，直到出现 pending：**它出现的那一刻面板就必须已经在了**。
            #
            # ⚠ 这里原先自己包了一层 try/finally 去应答面板——理由是「断言失败时
            # 若不收尾，被阻塞在确认盒上的工作线程永远醒不过来，收尾时的
            # `shutdown_default_executor()`（Python 3.11 无超时）会永久挂住」。
            # 那个理由现在由 `driving()` 统一兜住了（它在 `finally` 里做强制结算，
            # 挂着的面板会被按最保守的一档结算掉），所以这层去掉。
            #
            # **去掉它本身也是修正**：那个 `finally` 里的 `answer` / `wait` 一旦
            # 自己抛异常，就会把原始断言错误盖掉——正是这个 todo 要避免的第二种
            # 丢信息方式。
            bad = None
            seen_pending = False
            for _ in range(600):
                snap = await asyncio.to_thread(core.snapshot)
                if snap["state"] == SessionState.PENDING.value:
                    seen_pending = True
                    if not snap["panel_visible"]:
                        bad = f"宣称 pending 但面板不可见：{snap}"
                    break
                await asyncio.sleep(0.005)

            self.assertTrue(seen_pending, "剧本必须真的弹出过确认面板")
            self.assertIsNone(bad, bad)

            # 焦点单独判：允许晚一拍（Textual 的 `focus()` 走 `call_later`），
            # 但**必须最终到达**——不判它的话，「焦点永远不落到面板上」这种
            # 真回归就没人看守了，而那会让用户的按键全部打进输入框。
            focused = None
            deadline = time.monotonic() + 5.0
            while time.monotonic() < deadline:
                focused = (await asyncio.to_thread(core.snapshot))["focused"]
                if focused == "ConfirmPanel":
                    break
                await asyncio.sleep(0.01)
            self.assertEqual(focused, "ConfirmPanel", "焦点最终必须落到确认面板上")


class FinalTextRecordedTest(DriverFixture):
    """
    **回归护栏**：一轮运行里**最后**一段 AI 正文必须产出 `ui_message` 事件。

    背景（由 P1a 端到端驱动实测发现的 P0 埋点缺口）：
    `_do_stream` 的 `reset_text_widgets()` 原本只在「下一轮开始 / 工具开始 /
    历史回放」三个时机被调用，也就是**总靠下一个动作给上一段正文收尾**。
    于是最后一段正文永远等不到那个动作，一条 `ui_message` 都不产出——
    而那恰恰是用户看到的结论。修法是在 `_do_stream` 的 `finally` 里补一次调用。

    这个缺口**在界面上完全看不出来**（界面显示得好好的，只是没被记下来），
    所以必须由自动化钉死。

    ⚠️ 本护栏放在这里而不是 `test_trace_hooks.py`，是因为它验的是 `_do_stream`
    这条 TUI 层路径——那个文件直接驱动 Agent 循环，起不到真实 App。
    """

    async def test_last_assistant_text_produces_ui_message(self):
        app, _ = self.assemble([[text("这是最后一句结论。"), done()]])
        async with self.driving(app) as (pilot, core):
            await asyncio.to_thread(core.send, "说句话")
            await asyncio.to_thread(core.wait, 30.0)

            messages = self.view().of_type("ui_message")
            assistant = [m for m in messages if m.get("source") == "assistant"]
            self.assertTrue(
                assistant,
                "最后一段 AI 正文必须产出 assistant 的 ui_message；"
                f"实际只有：{[(m.get('source'), m.get('text')) for m in messages]}",
            )
            joined = json.dumps([m.get("text") for m in assistant], ensure_ascii=False)
            self.assertIn("这是最后一句结论", joined)

    async def test_no_duplicate_ui_message_when_tool_runs(self):
        """
        补完之后**不得重复**：工具执行时 `reset_text_widgets` 已经给前一段收过尾，
        `finally` 那次只该处理它之后新累积的那段。
        """
        app, _ = self.assemble(CONFIRM_SCRIPT)
        async with self.driving(app) as (pilot, core):
            await asyncio.to_thread(core.send, "写个文件")
            await asyncio.to_thread(core.wait, 30.0)
            await asyncio.to_thread(core.answer, "once")
            await asyncio.to_thread(core.wait, 30.0)

            texts = [
                m.get("text")
                for m in self.view().of_type("ui_message")
                if m.get("source") == "assistant"
            ]
            # 剧本两轮各一段正文，恰好两条，且互不重复
            self.assertEqual(len(texts), 2, f"应恰好两条，实际 {texts}")
            self.assertEqual(len(set(map(str, texts))), 2, f"两条不得重复：{texts}")


class MemoryNotifyRecordedTest(DriverFixture):
    """
    **回归护栏**：记忆更新的低打扰通知必须产出 `ui_message` 事件
    （全阶段复测观察 O5，`docs/e2e-sweep/c9.md` 发现一）。

    背景与 `FinalTextRecordedTest` 是同一类问题：`_notify_memory` 原先直接调
    `append_system`、绕过了 `_trace_ui_message`，于是 c9 的 AC19
    「记忆变更时界面出现低打扰提示」在任何基于 trace 的验收里都是**盲区**——
    「显示了没记」与「压根没显示」在记录上完全无法区分，判据判不了。

    ⚠️ 必须从**别的线程**调用：`_notify_memory` 真实运行在记忆 daemon 线程上，
    它内部靠 `call_from_thread` 把渲染调度回主线程。在主线程里直接调，
    Textual 会拒绝（那正是这个方法存在的理由），验的也就不是真实路径了。
    """

    async def test_memory_notice_produces_ui_message(self):
        app, _ = self.assemble([[text("好的。"), done()]])
        async with self.driving(app) as (pilot, core):
            await asyncio.to_thread(app._notify_memory, "🧠 已更新记忆（1 条记忆）")

            systems = [
                m.get("text")
                for m in self.view().of_type("ui_message")
                if m.get("source") == "system"
            ]
            self.assertTrue(
                any("已更新记忆" in str(t) for t in systems),
                "记忆通知必须产出 source=system 的 ui_message；"
                f"实际只有：{systems}",
            )

    async def test_notice_actually_rendered(self):
        """记录里有的那一行，界面上也必须真的有——正向确认两者配套。"""
        app, _ = self.assemble([[text("好的。"), done()]])
        async with self.driving(app) as (pilot, core):
            await asyncio.to_thread(app._notify_memory, "🧠 已更新记忆（1 条记忆）")
            await pilot.pause()

            self.assertIn(
                "已更新记忆",
                _history_text(app),
                "通知必须真的出现在历史区里",
            )

    async def test_no_phantom_record_when_render_fails(self):
        """
        **顺序护栏**：渲染失败时**不得**留下记录。

        这条才是真正钉住「埋点排在 `call_from_thread` 之后」的用例。上面两条
        都只覆盖正常路径——把埋点挪到渲染之前，它们照样全绿。

        构造的是真实的退出竞态：应用正在关闭时 `call_from_thread` 会抛异常，
        而 `_notify_memory` 的 `except Exception: pass` 会把它吞掉。若埋点排在
        前面，这里就会留下一条**界面上从未出现过的** `ui_message`——观测设施
        撒谎，而且因为异常被吞了，连个错都不报。
        """
        app, _ = self.assemble([[text("好的。"), done()]])
        async with self.driving(app) as (pilot, core):

            def boom(*_args, **_kwargs):
                raise RuntimeError("模拟应用正在退出")

            with mock.patch.object(app, "call_from_thread", side_effect=boom):
                # 不应抛出——异常必须被 _notify_memory 自己吞掉
                await asyncio.to_thread(app._notify_memory, "🧠 已更新记忆（1 条记忆）")

            phantom = [
                m.get("text")
                for m in self.view().of_type("ui_message")
                if "已更新记忆" in str(m.get("text"))
            ]
            self.assertEqual(
                phantom,
                [],
                "渲染失败时不得留下记录，否则 trace 里会出现界面上从未有过的行；"
                f"实际记到了：{phantom}",
            )


class ObserveTest(DriverFixture):
    async def test_observe_returns_full_payload_and_cursor(self):
        app, _ = self.assemble([[text("你好，我读一下。"), done()]])
        async with self.driving(app) as (pilot, core):
            await asyncio.to_thread(core.send, "说句话")
            await asyncio.to_thread(core.wait, 30.0)

            first = await asyncio.to_thread(core.observe, 0, None)
            self.assertGreater(first["next_since"], 0)
            self.assertTrue(first["events"])
            self.assertTrue(first["timeline"])
            self.assertEqual(first["skipped"], 0)
            # events 给的是**完整记录**而不只是摘要行
            self.assertIn("type", first["events"][0])

            # 增量：从上次游标之后再读，不该重复给出旧事件
            second = await asyncio.to_thread(core.observe, first["next_since"], None)
            self.assertEqual(second["events"], [])

            typed = await asyncio.to_thread(core.observe, 0, ["ui_message"])
            self.assertTrue(typed["events"])
            self.assertTrue(all(e["type"] == "ui_message" for e in typed["events"]))


if __name__ == "__main__":
    unittest.main()


class KeysAndScreenTest(DriverFixture):
    """
    P1b 的两个设施缺口：**任意按键投递**与**可见文本导出**。

    `docs/e2e-sweep/summary.md` 把 6 条「未覆盖」归到这两个缺口上：
    C2 AC9 的 Ctrl+Q / Ctrl+C、C4 场景 11 的澄清面板键盘导航、
    C10 E03 的补全与高亮、C10 E05 的配色可辨性。补上之后它们都能自动判定。
    """

    async def test_keys_reaches_the_input_bar(self):
        """
        最基本的一条：投递的按键真的到了界面上。

        用输入框而不是某个绑定动作来验，是因为输入框的 `value` 是**可读的状态**
        ——「按键有没有生效」有一个确定的观测点。验绑定动作（比如 Ctrl+Q 退出）
        的话，成功的表现是应用没了，反而不好从同一个进程里断言。
        """
        app, _ = self.assemble([[text("好"), done()]])
        async with self.driving(app) as (pilot, core):

            # ⚠ 这里**直接调**而不是走 `run_on_main`：测试体本身就跑在事件循环
            # 线程上，`run_on_main` 会把协程投给同一个循环再同步等结果——
            # 自己等自己，确定性死锁（实测：15 秒后以 TimeoutError 收场，
            # 而报错完全不指向「你在主线程上调了它」）。
            # `run_on_main` 是给**别的线程**用的，见它的 docstring。
            app.query_one(InputBar).focus()
            await pilot.pause()

            res = await asyncio.to_thread(core.keys, ["a", "b", "c"])
            self.assertTrue(res["ok"], res)
            self.assertEqual(res["data"]["pressed"], ["a", "b", "c"])

            await pilot.pause()
            self.assertEqual(app.query_one(InputBar).value, "abc")

    async def test_keys_rejects_bad_input(self):
        """
        非法输入走 `bad_request`，不抛。

        ⚠ 上限那条不是防滥用（驱动者是自己人），是防手滑：一个写错的循环
        把上万个按键投进去，Textual 会在主线程逐个处理，界面卡死几分钟
        而调用方只看到一次超时——排查方向会被完全带偏。
        """
        app, _ = self.assemble([[text("好"), done()]])
        async with self.driving(app) as (pilot, core):
            for bad in ([], "ctrl+q", [""], [1, 2], ["a"] * 500):
                res = await asyncio.to_thread(core.keys, bad)
                self.assertFalse(res["ok"], f"{bad!r} 应当被拒")
                self.assertEqual(res["error"]["code"], "bad_request")

    async def test_screen_exports_visible_history_text(self):
        """
        导出的文本里能找到界面上真的出现过的那句话。

        ⚠ 这与 `ui_message` 事件**不等价**：事件只能证明产品**打算**显示它，
        证明不了它真的渲染进了组件树。两者会分叉——markup 异常会让渲染失败
        而事件照常落盘，那正是 CLAUDE.md 里 `MarkupError` 那条坑的形态。
        """
        app, _ = self.assemble([[text("界面上要出现的这句话"), done()]])
        async with self.driving(app) as (pilot, core):
            await asyncio.to_thread(core.send, "说句话")
            await asyncio.to_thread(core.wait, 30.0)
            await pilot.pause()

            res = await asyncio.to_thread(core.screen, "")
            self.assertTrue(res["ok"], res)
            self.assertIn("界面上要出现的这句话", res["data"]["text"])
            self.assertTrue(res["data"]["widgets"], "widgets 不该为空")

    async def test_screen_selector_narrows_and_reports_bad_selector(self):
        """选择器能收窄范围；语法错误走 `bad_request` 而不是抛。"""
        app, _ = self.assemble([[text("正文在这里"), done()]])
        async with self.driving(app) as (pilot, core):
            await asyncio.to_thread(core.send, "说句话")
            await asyncio.to_thread(core.wait, 30.0)
            await pilot.pause()

            narrowed = await asyncio.to_thread(core.screen, "#history-messages > *")
            self.assertTrue(narrowed["ok"], narrowed)
            self.assertIn("正文在这里", narrowed["data"]["text"])
            # 收窄之后不该再包含状态栏那些内容
            whole = await asyncio.to_thread(core.screen, "")
            self.assertGreater(
                len(whole["data"]["widgets"]),
                len(narrowed["data"]["widgets"]),
                "整屏导出应当比收窄后的多",
            )

            bad = await asyncio.to_thread(core.screen, "#!!!not a selector")
            self.assertFalse(bad["ok"])
            self.assertEqual(bad["error"]["code"], "bad_request")

    async def test_screen_exposes_markup_for_style_assertions(self):
        """
        样式判据靠 **markup 原文**，不靠渲染后的 ANSI。

        导出 ANSI 既难读又依赖终端能力；而 markup 原文就是产品自己写下的
        那份意图（比如状态栏里的 `[dim]…[/dim]`），断言它才稳。
        """
        app, _ = self.assemble([[text("好"), done()]])
        async with self.driving(app) as (pilot, core):
            await pilot.pause()
            res = await asyncio.to_thread(core.screen, "")
            self.assertTrue(res["ok"], res)
            markups = [w["markup"] for w in res["data"]["widgets"] if w["markup"]]
            self.assertTrue(
                any("[" in m for m in markups),
                "至少应当有一个控件带 markup 原文（状态栏就有 [dim]）",
            )
