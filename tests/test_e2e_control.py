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
import threading
import time
import unittest
from pathlib import Path
from typing import Optional
from unittest import mock

from rhinecode.bootstrap import build_app
from rhinecode.config import Config
from rhinecode.tools import path_guard
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
        装配一次并返回 (app, provider)。自动笔记关掉——它是不确定性来源。

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
        self.result.manager.memory_manager.notes_enabled = False
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
        async with app.run_test(size=(120, 40)) as pilot:
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
        async with app.run_test(size=(120, 40)) as pilot:
            core = self.make_core(app, pilot, asyncio.get_running_loop())

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
            await core.shutdown_on_main("test")

    async def test_send_rejected_when_not_idle(self):
        app, _ = self.assemble(CONFIRM_SCRIPT)
        async with app.run_test(size=(120, 40)) as pilot:
            core = self.make_core(app, pilot, asyncio.get_running_loop())
            await asyncio.to_thread(core.send, "写个文件")
            await asyncio.to_thread(core.wait, 30.0)

            busy = await asyncio.to_thread(core.send, "再来一句")
            self.assertFalse(busy["ok"])
            self.assertEqual(busy["error"]["code"], "busy")

            await asyncio.to_thread(core.answer, "deny")
            await asyncio.to_thread(core.wait, 30.0)
            await core.shutdown_on_main("test")

    async def test_turn_budget_blocks_send(self):
        app, _ = self.assemble([[text("好"), done()]], turn_budget=1)
        async with app.run_test(size=(120, 40)) as pilot:
            core = self.make_core(app, pilot, asyncio.get_running_loop())
            self.assertTrue((await asyncio.to_thread(core.send, "第一句"))["ok"])
            await asyncio.to_thread(core.wait, 30.0)

            blocked = await asyncio.to_thread(core.send, "第二句")
            self.assertFalse(blocked["ok"])
            self.assertEqual(blocked["error"]["code"], "turn_budget")
            self.assertIn("--max-turns", blocked["error"]["message"], "要给出可照做的下一步")
            await core.shutdown_on_main("test")


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
        async with app.run_test(size=(120, 40)) as pilot:
            core = self.make_core(app, pilot, asyncio.get_running_loop())
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
            await core.shutdown_on_main("test")


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
        async with app.run_test(size=(120, 40)) as pilot:
            core = self.make_core(app, pilot, asyncio.get_running_loop())
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
            await core.shutdown_on_main("test")


class ShutdownTest(DriverFixture):
    """
    T44 / AC6 内核级：退出时的强制结算。

    **这是 plan 里唯一被实测证明「写错就永久挂死」的地方**，必须在内核层就地验证，
    不能推到十几步之后的宿主进程测试。
    """

    async def test_forced_settlement_lets_process_exit(self):
        app, _ = self.assemble(CONFIRM_SCRIPT)
        async with app.run_test(size=(120, 40)) as pilot:
            core = self.make_core(app, pilot, asyncio.get_running_loop())
            await asyncio.to_thread(core.send, "写个文件")
            waited = await asyncio.to_thread(core.wait, 30.0)
            self.assertEqual(waited["data"]["terminal"], SessionState.PENDING.value)

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
        async with app.run_test(size=(120, 40)) as pilot:
            core = self.make_core(app, pilot, asyncio.get_running_loop())
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

            # 收尾：正常路径退出，别把挂着的面板留给 tearDown
            await core.shutdown_on_main("quit")


class DeadlockGuardTest(DriverFixture):
    """
    AC22 / N6：跨线程死锁护栏。

    另起一个线程反复 `snapshot()`，主线程同时驱动交互。
    用**完成计数**而不是布尔标志判定——同线程版本在可重入锁下会静默通过，
    **不可简化成单线程写法**。
    """

    async def test_concurrent_snapshot_while_driving(self):
        app, _ = self.assemble(CONFIRM_SCRIPT)
        async with app.run_test(size=(120, 40)) as pilot:
            core = self.make_core(app, pilot, asyncio.get_running_loop())
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
            await core.shutdown_on_main("test")


class PanelAnswerTest(DriverFixture):
    """AC14 / AC15：四类面板的应答，以及两种来源。"""

    async def test_confirm_panel_full_cycle(self):
        app, provider = self.assemble(CONFIRM_SCRIPT)
        async with app.run_test(size=(120, 40)) as pilot:
            core = self.make_core(app, pilot, asyncio.get_running_loop())
            await asyncio.to_thread(core.send, "写个文件")
            await asyncio.to_thread(core.wait, 30.0)

            snap = await asyncio.to_thread(core.snapshot)
            panel = snap["panel"]
            # ① 应答前两个前置态都成立
            self.assertTrue(snap["panel_visible"])
            self.assertEqual(snap["focused"], "ConfirmPanel")
            # 面板原文取自 0 号 disabled 表头，且是**含 markup 标记的原始字符串**
            self.assertIn("write_file", panel["display"])
            self.assertIn("[dim]", panel["display"], "原文保留 markup 标记，不做渲染")
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
            await core.shutdown_on_main("test")

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
        async with app.run_test(size=(120, 40)) as pilot:
            core = self.make_core(app, pilot, asyncio.get_running_loop())
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
            await core.shutdown_on_main("test")

    async def test_answer_when_no_panel(self):
        app, _ = self.assemble([[text("好"), done()]])
        async with app.run_test(size=(120, 40)) as pilot:
            core = self.make_core(app, pilot, asyncio.get_running_loop())
            res = await asyncio.to_thread(core.answer, "once")
            self.assertFalse(res["ok"])
            self.assertEqual(res["error"]["code"], "not_pending")
            await core.shutdown_on_main("test")

    async def test_bad_choice_is_rejected_before_touching_product(self):
        app, _ = self.assemble(CONFIRM_SCRIPT)
        async with app.run_test(size=(120, 40)) as pilot:
            core = self.make_core(app, pilot, asyncio.get_running_loop())
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
            await core.shutdown_on_main("test")


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
        async with app.run_test(size=(120, 40)) as pilot:
            core = self.make_core(app, pilot, asyncio.get_running_loop())
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
            await core.shutdown_on_main("test")

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
        async with app.run_test(size=(120, 40)) as pilot:
            core = self.make_core(app, pilot, asyncio.get_running_loop())
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
            await core.shutdown_on_main("test")


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
        async with app.run_test(size=(120, 40)) as pilot:
            core = self.make_core(app, pilot, asyncio.get_running_loop())
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
            await core.shutdown_on_main("test")

    async def test_session_panel_cancel(self):
        self._seed_other_session()
        app, _ = self.assemble([[text("好"), done()]])
        async with app.run_test(size=(120, 40)) as pilot:
            core = self.make_core(app, pilot, asyncio.get_running_loop())
            await asyncio.to_thread(core.send, "/resume")
            await asyncio.to_thread(core.wait, 30.0)

            await asyncio.to_thread(core.answer, "cancel")
            back = await asyncio.to_thread(core.wait, 30.0)
            self.assertEqual(back["data"]["terminal"], SessionState.IDLE.value)

            events = [e for e in self.interactions() if e["kind"] == "session"]
            self.assertEqual(len(events), 1)
            self.assertEqual(events[0]["result"], "cancelled")
            await core.shutdown_on_main("test")


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
        async with app.run_test(size=(120, 40)) as pilot:
            core = self.make_core(app, pilot, asyncio.get_running_loop())
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
            await core.shutdown_on_main("test")

    async def test_no_duplicate_ui_message_when_tool_runs(self):
        """
        补完之后**不得重复**：工具执行时 `reset_text_widgets` 已经给前一段收过尾，
        `finally` 那次只该处理它之后新累积的那段。
        """
        app, _ = self.assemble(CONFIRM_SCRIPT)
        async with app.run_test(size=(120, 40)) as pilot:
            core = self.make_core(app, pilot, asyncio.get_running_loop())
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
            await core.shutdown_on_main("test")


class MemoryNotifyRecordedTest(DriverFixture):
    """
    **回归护栏**：笔记更新的低打扰通知必须产出 `ui_message` 事件
    （全阶段复测观察 O5，`docs/e2e-sweep/c9.md` 发现一）。

    背景与 `FinalTextRecordedTest` 是同一类问题：`_notify_memory` 原先直接调
    `append_system`、绕过了 `_trace_ui_message`，于是 c9 的 AC19
    「笔记变更时界面出现低打扰提示」在任何基于 trace 的验收里都是**盲区**——
    「显示了没记」与「压根没显示」在记录上完全无法区分，判据判不了。

    ⚠️ 必须从**别的线程**调用：`_notify_memory` 真实运行在笔记 daemon 线程上，
    它内部靠 `call_from_thread` 把渲染调度回主线程。在主线程里直接调，
    Textual 会拒绝（那正是这个方法存在的理由），验的也就不是真实路径了。
    """

    async def test_memory_notice_produces_ui_message(self):
        app, _ = self.assemble([[text("好的。"), done()]])
        async with app.run_test(size=(120, 40)) as pilot:
            core = self.make_core(app, pilot, asyncio.get_running_loop())
            await asyncio.to_thread(app._notify_memory, "🧠 已更新记忆（1 条笔记）")

            systems = [
                m.get("text")
                for m in self.view().of_type("ui_message")
                if m.get("source") == "system"
            ]
            self.assertTrue(
                any("已更新记忆" in str(t) for t in systems),
                "笔记通知必须产出 source=system 的 ui_message；"
                f"实际只有：{systems}",
            )
            await core.shutdown_on_main("test")

    async def test_notice_actually_rendered(self):
        """记录里有的那一行，界面上也必须真的有——正向确认两者配套。"""
        app, _ = self.assemble([[text("好的。"), done()]])
        async with app.run_test(size=(120, 40)) as pilot:
            core = self.make_core(app, pilot, asyncio.get_running_loop())
            await asyncio.to_thread(app._notify_memory, "🧠 已更新记忆（1 条笔记）")
            await pilot.pause()

            self.assertIn(
                "已更新记忆",
                _history_text(app),
                "通知必须真的出现在历史区里",
            )
            await core.shutdown_on_main("test")

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
        async with app.run_test(size=(120, 40)) as pilot:
            core = self.make_core(app, pilot, asyncio.get_running_loop())

            def boom(*_args, **_kwargs):
                raise RuntimeError("模拟应用正在退出")

            with mock.patch.object(app, "call_from_thread", side_effect=boom):
                # 不应抛出——异常必须被 _notify_memory 自己吞掉
                await asyncio.to_thread(app._notify_memory, "🧠 已更新记忆（1 条笔记）")

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
            await core.shutdown_on_main("test")


class ObserveTest(DriverFixture):
    async def test_observe_returns_full_payload_and_cursor(self):
        app, _ = self.assemble([[text("你好，我读一下。"), done()]])
        async with app.run_test(size=(120, 40)) as pilot:
            core = self.make_core(app, pilot, asyncio.get_running_loop())
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
            await core.shutdown_on_main("test")


if __name__ == "__main__":
    unittest.main()
