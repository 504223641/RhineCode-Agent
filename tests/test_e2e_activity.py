"""
子 Agent 活动区的端到端验证（tui-display 扩展 A 组，AC1/AC2/AC5/AC6/AC7/AC8）。

## 为什么这组要端到端跑，而不是只有组件单测

`ActivityView` 自己的行为已由 `test_tui_activity.py` 钉住。这里验的是**接线**：
`compose` 里的位置、CSS 的缺省隐藏、`_poll_subagents` 每 0.5 秒真的会去刷它、
以及「委派 → 任务表 → 领域快照 → 组件」这条链是通的。这四件事**任何一件漏了，
组件单测照样全绿**——而用户看到的就是「派出去之后界面上还是什么都没有」，
也就是本轮要解决的那个原始症状。

## 怎么让「运行中」这一刻停得住

脚本化 Provider 是瞬时返回的，子 Agent 会在一个 `pilot.pause()` 之内跑完，
「运行中的活动行」根本捕捉不到。所以这里给子 Agent 的作用域挂一个**闸门**：
它的第一次 `stream_chat` 会阻塞在 `threading.Event` 上，直到测试放行。
那段时间里主线程照常跑轮询，活动区就是活的。

⚠ 这是**产品之外**的阻塞，不动任何产品代码——闸门在假 Provider 里。
"""

from __future__ import annotations

import asyncio
import os
import threading
import unittest
from pathlib import Path

from rhinecode.bootstrap import build_app
from rhinecode.config import Config
from rhinecode.provider.base import Message, StreamChunk
from rhinecode.trace import TraceRecorder
from rhinecode.trace.recorder import current_scope
from rhinecode.tui.widgets import ActivityView, HistoryView, InputBar
from rhinecode.tools import path_guard
from tests.e2e import c15_scenarios, sandbox


class _GatedProvider:
    """
    按 trace 作用域分派的假 Provider，且**子 Agent 那一路带闸门**。

    与 `ScopedScriptedProvider` 的分派语义相同（按 `current_scope()` 取剧本），
    但刻意不复用它：那个类没有、也不该有阻塞能力——阻塞是本文件为了
    「让运行中那一刻停得住」而造的测试装置，不是设施的通用需求。
    """

    def __init__(self, gate: threading.Event) -> None:
        self._gate = gate
        self.main_turn = 0
        # 子 Agent 真的被起起来了没有：判据之一，防止「活动区没出现」被误判成
        # 「活动区坏了」，实际是委派压根没发生。
        self.subagent_started = threading.Event()

    def stream_chat(self, messages, thinking_effort="off", tools=None, system=None):
        scope = current_scope()
        if scope.startswith("subagent:"):
            self.subagent_started.set()
            # 卡住，直到测试放行。活动区就在这段时间里被反复刷新。
            self._gate.wait(timeout=30.0)
            yield StreamChunk(type="text", content="我看完了，没发现问题。")
            yield StreamChunk(type="done")
            return

        self.main_turn += 1
        if self.main_turn == 1:
            yield StreamChunk(type="text", content="我派个人去看。")
            yield StreamChunk(
                type="tool_call",
                tool_call=_tool_call(
                    "t-run", "run_agent",
                    {
                        "type": "role",
                        "agent": "reviewer",
                        "name": "checker",
                        "task": "复核一下 src/app.py",
                        "background": True,
                    },
                ),
            )
        else:
            yield StreamChunk(type="text", content="好了。")
        yield StreamChunk(type="done")


def _tool_call(call_id: str, name: str, args: dict):
    from rhinecode.provider.base import ToolCall

    return ToolCall(id=call_id, name=name, arguments=args)


class ActivityWiringTest(unittest.IsolatedAsyncioTestCase):
    """AC1：无委派时不存在活动区；发起一次委派后它出现。"""

    def setUp(self) -> None:
        # ⚠ 沙箱目录必须走 `tests/e2e/sandbox.py` 建与删：它在目录里放一个
        # 标记文件，`force_rmtree` 认那个标记才肯动手。这道闸门是「空变量
        # rmtree 删掉整个仓库」那次事故之后加的，不许绕开。
        self._cwd = Path.cwd()
        self.ws = sandbox.create_workspace()
        self.user_dir = sandbox.create_user_dir()
        os.chdir(self.ws)
        path_guard.clear_read_roots()
        self.addCleanup(path_guard.clear_read_roots)
        self.addCleanup(sandbox.force_rmtree, self.user_dir)
        self.addCleanup(sandbox.force_rmtree, self.ws)
        self.addCleanup(os.chdir, self._cwd)
        c15_scenarios.seed_team(self.ws, self.user_dir)

        # ⚠ **必须传一个真的 `TraceRecorder`。**
        # 假 Provider 靠 `current_scope()` 分辨「这一轮是主对话还是子 Agent」，
        # 而作用域是运行器调 `recorder.bind_scope(...)` 绑上去的——
        # `NullRecorder` 的那个方法是空实现，于是子 Agent 那一路的作用域
        # 恒为 `main`，闸门永远拦不住它。
        # 症状极具迷惑性：委派**成功了**、任务也跑完了，只是快到活动区来不及
        # 显示，看起来像「活动区坏了」。
        trace_path = self.ws / ".rhinecode" / "traces" / "activity.jsonl"
        trace_path.parent.mkdir(parents=True, exist_ok=True)

        self.gate = threading.Event()
        self.addCleanup(self.gate.set)
        self.provider = _GatedProvider(self.gate)
        self.result = build_app(
            Config(
                protocol="deepseek",
                model="deepseek-chat",
                base_url="https://api.deepseek.com",
                api_key="fake-key-for-test",
                debug_log=False,
                context_window=65536,
            ),
            user_dir=self.user_dir,
            recorder=TraceRecorder(trace_path),
            provider_factory=lambda cfg: self.provider,
            exclude_tools=frozenset({"mcp_add_server", "mcp_resolve_server"}),
        )
        # 自动记忆是不确定性来源（它另起一条对话、另调一次模型），关掉
        self.result.manager.memory_manager.memories_enabled = False
        # ⚠ **回收要排在 `force_rmtree` 之前**（`addCleanup` 是后进先出，
        # 所以这一句要写在注册删除之后）。记录器的文件句柄不关掉，
        # Windows 下删目录会一路重试到超时——一条用例因此白等十几秒。
        self.addCleanup(self.result.cleanup, "test_teardown")

    async def test_hidden_before_any_delegation(self) -> None:
        """
        AC1a / F9：从未委派过时活动区**不存在于视觉上**，也不占布局空间。

        这是零回归的落点——不使用子 Agent 的用户永远停在这一步。
        """
        async with self.result.app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            view = self.result.app.query_one(ActivityView)
            self.assertFalse(view.display)
            self.assertEqual(view.region.height, 0)

    async def test_appears_while_a_subagent_runs(self) -> None:
        """AC1b / AC2：委派之后活动区出现，且带着名字与三个数字。"""
        app = self.result.app
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            bar = app.query_one(InputBar)
            bar.focus()
            bar.value = "找人复核一下"
            await pilot.press("enter")

            # 等子 Agent 真的起来（否则「活动区没出现」会被误判成组件坏了）
            await _wait_for(lambda: self.provider.subagent_started.is_set(), pilot, 20.0)
            # 等主线程那趟 0.5 秒轮询把活动区刷出来
            view = app.query_one(ActivityView)
            await _wait_for(lambda: view.display, pilot, 10.0)

            text = _activity_text(view)
            self.assertIn("checker", text, "名字口径应与 /agents 一致（队员名在前）")
            self.assertIn("reviewer", text)
            self.assertIn("运行中", text)
            self.assertIn("次调用", text)
            self.assertIn("tokens", text)

            self.gate.set()

    async def test_numbers_come_from_the_task_table_not_the_model(self) -> None:
        """
        AC7 的前半：活动区那些数字由**界面轮询**从任务表读出来，
        不依赖模型说任何话。

        本文件的剧本里模型一个数字都没提过——它只说了「我派个人去看」。
        """
        app = self.result.app
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            bar = app.query_one(InputBar)
            bar.focus()
            bar.value = "找人复核一下"
            await pilot.press("enter")

            await _wait_for(lambda: self.provider.subagent_started.is_set(), pilot, 20.0)
            view = app.query_one(ActivityView)
            await _wait_for(lambda: view.display, pilot, 10.0)

            # 直接往任务表里记两笔，再等一次轮询——数字必须跟着变
            tasks = self.result.manager.subagent_service.tasks
            record = tasks.snapshot()[0]
            tasks.bump(record.task_id, turns=4, tokens=4321)
            tasks.note_tool(record.task_id, "Read(src/app.py)")
            await _wait_for(lambda: "4.3k tokens" in _activity_text(view), pilot, 10.0)

            text = _activity_text(view)
            self.assertIn("1 次调用", text)

            self.gate.set()


def _activity_text(view: ActivityView) -> str:
    children = list(view.children)
    if not children:
        return ""
    content = children[0].content
    return content if isinstance(content, str) else str(content)


async def _wait_for(predicate, pilot, timeout: float) -> None:
    """
    反复 `pause` 直到条件成立。

    ⚠ 必须 `pause` 而不是干等：活动区的刷新发生在**主线程的定时器**里，
    不让事件循环转起来它永远不会被调到。
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        await pilot.pause()
        if predicate():
            return
        await asyncio.sleep(0.05)
    raise AssertionError(f"等待 {timeout} 秒条件仍未成立")


if __name__ == "__main__":
    unittest.main()
