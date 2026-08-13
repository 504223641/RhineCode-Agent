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


class ActivityFixture(unittest.IsolatedAsyncioTestCase):
    """装配一个真实 App + 带闸门的假 Provider，供本文件各组用例共用。"""

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

    @staticmethod
    async def _send(app, pilot, text: str) -> None:
        """
        走**真人提交入口**（聚焦输入框 → 设值 → 按回车）。

        不调任何内部方法：那会绕过命令层与协调层的接线，验到的就不是用户
        实际走的那条路（驱动设施的同一条纪律）。
        """
        bar = app.query_one(InputBar)
        bar.focus()
        bar.value = text
        await pilot.press("enter")


class ActivityWiringTest(ActivityFixture):
    """AC1 / AC2：无委派时不存在活动区；发起一次委派后它出现并带三个数字。"""

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
            await self._send(app, pilot, "找人复核一下")

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
            await self._send(app, pilot, "找人复核一下")

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


class FinishTraceTest(ActivityFixture):
    """AC6 / AC6b / AC7：终态成本、历史留痕**只有一条**、且不依赖模型说话。"""

    async def test_history_gets_exactly_one_line_with_the_cost(self) -> None:
        """
        ⚠ **F6 的历史永久痕与 F7 的完成通知是同一行。**

        写成两行不报错，只是每个子 Agent 在历史区留下重复的两条——
        用户会以为它跑了两次。这条用例是那个「同一行」的唯一判据。
        """
        app = self.result.app
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            await self._send(app, pilot, "找人复核一下")
            await _wait_for(lambda: self.provider.subagent_started.is_set(), pilot, 20.0)
            self.gate.set()

            await _wait_for(
                lambda: "checker(reviewer)" in _history_text(app), pilot, 20.0
            )

            text = _history_text(app)
            self.assertEqual(
                text.count("checker(reviewer)"),
                1,
                f"该任务的完成行只该出现一次，实际：\n{text}",
            )
            self.assertIn("已完成", text)
            self.assertIn("次调用", text)
            self.assertIn("tokens", text)
            self.assertIn("结论将在下一轮对话中自动交给 AI", text)

    async def test_cost_numbers_match_the_activity_row(self) -> None:
        """
        AC6：历史留痕与活动区终态行的成本数字**同源**。

        两边各自从 TaskRecord 上取字段拼一遍的话，一次口径改动只改一处不报错，
        而两个数字都「看起来对」、只是不相等——那种不一致最难解释。
        """
        from rhinecode.subagents.tasks import TaskManager
        from rhinecode.tui.widgets import format_activity_cost

        app = self.result.app
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            await self._send(app, pilot, "找人复核一下")
            await _wait_for(lambda: self.provider.subagent_started.is_set(), pilot, 20.0)
            self.gate.set()
            await _wait_for(
                lambda: "checker(reviewer)" in _history_text(app), pilot, 20.0
            )

            record = self.result.manager.subagent_service.tasks.snapshot()[0]
            expected = format_activity_cost(TaskManager.row_of(record))
            # 耗时那一段可能因为「已结束」而冻结，取前两段（次数 · token）比对
            head = " · ".join(expected.split(" · ")[:2])
            self.assertIn(head, _history_text(app))

    async def test_notice_does_not_depend_on_the_model(self) -> None:
        """
        AC7：完成通知由**界面轮询**产出，不依赖模型说任何话。

        本文件的剧本里子 Agent 只说了「我看完了，没发现问题。」——
        一个数字、一个状态词都没提。
        """
        app = self.result.app
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            await self._send(app, pilot, "找人复核一下")
            await _wait_for(lambda: self.provider.subagent_started.is_set(), pilot, 20.0)
            self.gate.set()
            await _wait_for(lambda: "已完成" in _history_text(app), pilot, 20.0)

            self.assertIn("次调用", _history_text(app))


class ExpandToggleTest(ActivityFixture):
    """AC5：`Ctrl+O` 在折叠与展开之间切换，且**不动焦点**。"""

    async def test_ctrl_o_reveals_recent_calls_and_folds_back(self) -> None:
        app = self.result.app
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            await self._send(app, pilot, "找人复核一下")
            await _wait_for(lambda: self.provider.subagent_started.is_set(), pilot, 20.0)
            view = app.query_one(ActivityView)
            await _wait_for(lambda: view.display, pilot, 10.0)

            # 直接往任务表里塞一次调用记录：本用例验的是**展开开关**，
            # 让子 Agent 真的去调工具会把判据搅进权限与工具执行里。
            tasks = self.result.manager.subagent_service.tasks
            tasks.note_tool(tasks.snapshot()[0].task_id, "Read(src/app.py)")
            await _wait_for(lambda: "Ctrl+O 展开" in _activity_text(view), pilot, 10.0)
            self.assertNotIn("Read(src/app.py)", _activity_text(view))

            # 第一下 → 逐条档：活动区展开
            await pilot.press("ctrl+o")
            await _wait_for(lambda: "Read(src/app.py)" in _activity_text(view), pilot, 10.0)
            self.assertIn("Ctrl+O 收回", _activity_text(view))

            # 第二下 → 全文档。**活动区必须保持不变**（tui-activity-fold F13/AC15）：
            # 它只有折叠 / 展开两态，「逐条」与「全文」对它表现一致——
            # 它展开后列的是最近的工具调用，那些本来就没有「更详细」的第二层可展。
            # ⚠ 这一条是 AC15 的护栏：给活动区造出第三态的话，这里当场红。
            before = _activity_text(view)
            await pilot.press("ctrl+o")
            await pilot.pause()
            self.assertEqual(
                _activity_text(view), before, "活动区在逐条档与全文档下必须一致"
            )

            # 第三下 → 回到折叠，循环闭合
            await pilot.press("ctrl+o")
            await _wait_for(lambda: "Ctrl+O 展开" in _activity_text(view), pilot, 10.0)
            self.assertNotIn("Read(src/app.py)", _activity_text(view))

            self.gate.set()

    async def test_focus_stays_in_the_input(self) -> None:
        """
        AC5b：切换展开**不引入焦点切换**。

        活动区任何时候都不抢焦点——抢了的话用户按 `Ctrl+O` 看一眼之后
        就打不了字了，而这在只看「子行出没出现」的用例里完全测不出来。
        """
        from rhinecode.tui.widgets import InputBar

        app = self.result.app
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            bar = app.query_one(InputBar)
            bar.focus()
            await pilot.pause()

            await pilot.press("ctrl+o")
            await pilot.pause()

            self.assertIs(app.focused, bar, "焦点必须仍在输入框")
            # 再打一个字，确认输入框真的还能用（焦点对了但被禁用也算坏）
            await pilot.press("a")
            await pilot.pause()
            self.assertEqual(bar.value, "a")


class SessionSwitchTest(ActivityFixture):
    """AC8：`/clear` 之后活动区为空。"""

    async def test_clear_empties_the_activity_area(self) -> None:
        """
        清空之后活动区必须立刻空掉，**不能等下一轮轮询**。

        领域侧确实也会把它们滤掉（`/clear` 取消在跑的子 Agent，它们随即转终态、
        再过几秒淡出），但那中间有半秒到几秒的窗口——用户会在一个刚清空的
        界面上看到上一段对话的残影。
        """
        app = self.result.app
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            await self._send(app, pilot, "找人复核一下")
            await _wait_for(lambda: self.provider.subagent_started.is_set(), pilot, 20.0)
            view = app.query_one(ActivityView)
            await _wait_for(lambda: view.display, pilot, 10.0)

            self.gate.set()
            await self._send(app, pilot, "/clear")
            await pilot.pause()

            self.assertFalse(view.display, "清空之后活动区必须立刻隐藏")
            self.assertEqual(_activity_text(view), "")


def _history_text(app) -> str:
    """把历史区所有组件的文本拍平成一段，用于「出现过 / 出现几次」这类判据。"""
    view = app.query_one(HistoryView)
    parts = []
    for child in view.query_one("#history-messages").children:
        # ⚠ 走 `plain_text()` 而不是读 `content`：终态行的内容是 Rich 渲染对象，
        # 要在渲染期转成 `Content` 才成立（见 `content_from_rich`），
        # `content` 里留的是上一次 markup 的残留。
        if hasattr(child, "plain_text"):
            parts.append(child.plain_text())
            continue
        content = getattr(child, "content", "")
        parts.append(content if isinstance(content, str) else str(content))
    return "\n".join(parts)


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
