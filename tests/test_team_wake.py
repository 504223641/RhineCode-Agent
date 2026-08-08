"""
待命与唤醒续跑的单测（c15 T33，覆盖 AC17–AC20、AC36 的运行面）。

用一个**假 Provider** 驱动真实的 `run_subagent`，因此验的是真实路径：
真的 Agent Loop、真的闸门、真的花名册。

重点四处：
- **有待命队员时主 Agent 能正常收工**（plan 风险 1，本章最容易埋的雷）；
- **唤醒续跑保留历史**（AC17）；
- **轮次预算按次重置、界面轮次累计**（AC20 / F16）；
- **失败与取消不待命**（AC18）。
"""

from __future__ import annotations

import threading
import time
import unittest

from rhinecode.agent.gate import CompositeGate
from rhinecode.permission.engine import PermissionEngine
from rhinecode.permission.models import PermissionMode
from rhinecode.permission.rules import RuleSet
from rhinecode.provider.base import StreamChunk
from rhinecode.subagents.gate import SubAgentGate, render_subagent_message
from rhinecode.subagents.runner import SubAgentRuntime, run_subagent
from rhinecode.subagents.tasks import KIND_ROLE, TaskManager, TaskStatus
from rhinecode.subagents.toolset import ToolsetResult
from rhinecode.team import TeamService
from rhinecode.team.models import MAIN_NAME, MemberState
from rhinecode.tools.registry import ToolRegistry


class _ScriptedProvider:
    """
    按脚本逐轮回话的假 Provider。

    每次 `stream_chat` 消费脚本里的下一条文本并作为完整回复返回
    （不带工具调用 → Agent Loop 判自然完成）。脚本用完之后一直回最后一条。
    """

    def __init__(self, script: list[str]) -> None:
        self.script = list(script)
        self.index = 0
        self.seen_histories: list[list] = []

    def stream_chat(self, messages, thinking_effort, tools=None, system=None):
        # 记下每次拿到的历史，用于验证「唤醒后带着原有上下文」
        self.seen_histories.append(list(messages))
        text = self.script[min(self.index, len(self.script) - 1)]
        self.index += 1
        yield StreamChunk(type="text", content=text)


def _runtime(provider, team, registry=None) -> SubAgentRuntime:
    return SubAgentRuntime(
        provider_for=lambda _model: provider,
        registry=registry or ToolRegistry(),
        engine=PermissionEngine(RuleSet(rules=()), mode=PermissionMode.DEFAULT),
        main_mode=lambda: PermissionMode.DEFAULT,
        environment_text=lambda _cwd: "",
        default_model="fake",
        team=team,
    )


def _start(runtime, team, tasks, name: str):
    """注册一个队员并在后台线程里跑它，返回 (record, thread)。"""
    registration = team.register_member(name, "explorer")
    record = tasks.create(KIND_ROLE, "explorer", "干活")
    record.member_name = registration.name
    thread = threading.Thread(
        target=run_subagent,
        args=(
            runtime, None, "干活", record, tasks,
            ToolsetResult(allowed=frozenset()), frozenset(),
        ),
        daemon=True,
    )
    thread.start()
    return record, thread


def _wait_until(predicate, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


class IdleTest(unittest.TestCase):
    def setUp(self) -> None:
        self.team = TeamService()
        self.tasks = TaskManager()
        self.provider = _ScriptedProvider(["第一轮结论"])
        self.runtime = _runtime(self.provider, self.team)

    def test_natural_stop_becomes_idle(self) -> None:
        """AC17：自然停止 → 待命，且状态是「叫得醒」的。"""
        record, _thread = _start(self.runtime, self.team, self.tasks, "worker-a")
        self.assertTrue(
            _wait_until(lambda: self.team.member("worker-a").state is MemberState.IDLE)
        )
        self.assertTrue(self.team.member("worker-a").state.is_wakeable)

    def test_conclusion_is_delivered_before_going_idle(self) -> None:
        """
        ⚠ 待命之前必须先把这一轮的结论交付出去。

        不交付的话主 Agent 永远看不到队员干了什么——而它正等着这个结果。
        """
        record, _thread = _start(self.runtime, self.team, self.tasks, "worker-a")
        self.assertTrue(_wait_until(lambda: record.status is TaskStatus.COMPLETED))
        self.assertIn("第一轮结论", record.conclusion)

    def test_main_agent_can_finish_while_a_member_idles(self) -> None:
        """
        ⚠⚠ **本章最容易埋的雷**（plan 风险 1）。

        队员待命时，如果它的**任务记录**还停在「运行中」，主 Agent 的闸门
        会认为「还有委派没回来」而在收工前一直等它——而这个队员正等着
        主 Agent 给它发消息。**双方互等，永远结束不了。**

        修法是让两套状态各管一个维度：任务记录照常置 COMPLETED
        （结论确实产出了），「人还在场」由花名册的 IDLE 表达。

        这条用例请勿删改：它验的是两套状态确实分开了。
        """
        record, _thread = _start(self.runtime, self.team, self.tasks, "worker-a")
        self.assertTrue(
            _wait_until(lambda: self.team.member("worker-a").state is MemberState.IDLE)
        )
        gate = CompositeGate([SubAgentGate(self.tasks, render_subagent_message)])
        self.assertFalse(
            gate.has_awaited(),
            "队员待命时主 Agent 不该还在等它——那会双方互等",
        )

    def test_idle_member_does_not_occupy_concurrency(self) -> None:
        """AC19 / F15：待命的人不占并发名额。"""
        _record, _thread = _start(self.runtime, self.team, self.tasks, "worker-a")
        self.assertTrue(
            _wait_until(lambda: self.team.member("worker-a").state is MemberState.IDLE)
        )
        self.assertEqual(self.tasks.running_count(), 0)


class WakeTest(unittest.TestCase):
    def setUp(self) -> None:
        self.team = TeamService()
        self.tasks = TaskManager()
        self.provider = _ScriptedProvider(["第一轮结论", "第二轮结论"])
        self.runtime = _runtime(self.provider, self.team)

    def test_message_wakes_and_resumes(self) -> None:
        """AC17：发一条消息就能把待命的队员唤醒继续干。"""
        _record, _thread = _start(self.runtime, self.team, self.tasks, "worker-a")
        self.assertTrue(
            _wait_until(lambda: self.team.member("worker-a").state is MemberState.IDLE)
        )
        self.team.send(MAIN_NAME, "worker-a", "再做一件事")
        self.assertTrue(
            _wait_until(lambda: self.provider.index >= 2),
            "队员应当被唤醒并再跑一轮",
        )

    def test_resumed_run_keeps_the_original_history(self) -> None:
        """
        AC17 的实质：**不重新交代背景**就接着干。

        验证方式是看第二轮请求里还带着第一轮的对话——那正是
        「带着原来的全部上下文继续」的物理含义。
        """
        _record, _thread = _start(self.runtime, self.team, self.tasks, "worker-a")
        self.assertTrue(
            _wait_until(lambda: self.team.member("worker-a").state is MemberState.IDLE)
        )
        self.team.send(MAIN_NAME, "worker-a", "再做一件事")
        self.assertTrue(_wait_until(lambda: self.provider.index >= 2))

        second = self.provider.seen_histories[1]
        texts = [str(getattr(m, "content", "")) for m in second]
        self.assertTrue(
            any("干活" in t for t in texts), "第二轮应当还带着最初的任务描述"
        )
        self.assertTrue(
            any("第一轮结论" in t for t in texts), "第二轮应当还带着第一轮的回答"
        )

    def test_the_waking_message_is_injected(self) -> None:
        """唤醒它的那条消息本身要出现在第二轮的历史里（走闸门注入）。"""
        _record, _thread = _start(self.runtime, self.team, self.tasks, "worker-a")
        self.assertTrue(
            _wait_until(lambda: self.team.member("worker-a").state is MemberState.IDLE)
        )
        self.team.send(MAIN_NAME, "worker-a", "去看看登录模块")
        self.assertTrue(_wait_until(lambda: self.provider.index >= 2))
        texts = [str(getattr(m, "content", "")) for m in self.provider.seen_histories[1]]
        self.assertTrue(any("去看看登录模块" in t for t in texts))

    def test_resumed_round_creates_a_new_task_record(self) -> None:
        """
        每轮一条记录 = 每轮一段交付。复用第一条的话，第二轮跑完时它已经是
        终态，`finish` 不会再产生交付，**主 Agent 永远看不到队员被唤醒后
        做了什么**。
        """
        _record, _thread = _start(self.runtime, self.team, self.tasks, "worker-a")
        self.assertTrue(
            _wait_until(lambda: self.team.member("worker-a").state is MemberState.IDLE)
        )
        self.team.send(MAIN_NAME, "worker-a", "再做一件事")
        self.assertTrue(_wait_until(lambda: len(self.tasks.snapshot()) == 2))
        second = [r for r in self.tasks.snapshot() if r.task_id != _record.task_id][0]
        self.assertEqual(second.member_name, "worker-a")
        self.assertFalse(
            second.awaited, "主 Agent 没委派这一轮，不该为它停下来等"
        )

    def test_turn_budget_resets_but_display_accumulates(self) -> None:
        """
        F16 / AC20：每次唤醒重新拿到**完整**的迭代预算，
        而界面上的轮次是**累计**的。

        不重置预算的话，第三次唤醒时预算已用光，队员一句话都说不了就被
        掐断——那等于「叫不醒」。不累计显示的话，一个被唤醒三次的队员
        在 `/agents` 上永远显示「跑了 1 轮」。
        """
        _record, _thread = _start(self.runtime, self.team, self.tasks, "worker-a")
        self.assertTrue(
            _wait_until(lambda: self.team.member("worker-a").state is MemberState.IDLE)
        )
        first_turns = _record.turns
        self.team.send(MAIN_NAME, "worker-a", "再来")
        self.assertTrue(_wait_until(lambda: len(self.tasks.snapshot()) == 2))
        second = [r for r in self.tasks.snapshot() if r.task_id != _record.task_id][0]
        self.assertTrue(_wait_until(lambda: second.status.is_terminal))
        self.assertGreater(second.turns, first_turns, "界面轮次应当累计")


class TerminalTest(unittest.TestCase):
    def test_cancel_during_standby_marks_terminal(self) -> None:
        """待命期间被取消 → 终态，之后叫不醒（AC18）。"""
        team = TeamService()
        tasks = TaskManager()
        provider = _ScriptedProvider(["结论"])
        runtime = _runtime(provider, team)
        record, thread = _start(runtime, team, tasks, "worker-a")
        self.assertTrue(
            _wait_until(lambda: team.member("worker-a").state is MemberState.IDLE)
        )
        record.cancel_event.set()
        self.assertTrue(
            _wait_until(lambda: team.member("worker-a").state is MemberState.CANCELLED)
        )
        result = team.send(MAIN_NAME, "worker-a", "喂")
        self.assertFalse(result.ok)
        self.assertIn("已取消", result.reason)

    def test_clear_releases_a_standby_member(self) -> None:
        """
        ⚠ `/clear` 之后待命线程必须退出，否则它们会挂到进程结束。

        花名册在清空时唤醒它们，它们调 `wake()` 拿到 `None`，据此干净退出。
        """
        team = TeamService()
        tasks = TaskManager()
        provider = _ScriptedProvider(["结论"])
        runtime = _runtime(provider, team)
        _record, thread = _start(runtime, team, tasks, "worker-a")
        self.assertTrue(
            _wait_until(lambda: team.member("worker-a").state is MemberState.IDLE)
        )
        team.clear()
        thread.join(timeout=5.0)
        self.assertFalse(thread.is_alive(), "待命线程应当在会话清空后退出")

    def test_retired_member_thread_exits(self) -> None:
        """N3 降级同理：被降级的队员线程要能退出（AC36 的运行面）。"""
        team = TeamService(max_idle=1)
        tasks = TaskManager()
        provider = _ScriptedProvider(["结论"])
        runtime = _runtime(provider, team)
        _r1, thread1 = _start(runtime, team, tasks, "w0")
        self.assertTrue(
            _wait_until(lambda: team.member("w0").state is MemberState.IDLE)
        )
        _r2, _thread2 = _start(runtime, team, tasks, "w1")
        self.assertTrue(
            _wait_until(lambda: team.member("w0").state is MemberState.RETIRED)
        )
        thread1.join(timeout=5.0)
        self.assertFalse(thread1.is_alive())
        self.assertTrue(team.drain_notices(), "降级必须留下一条给用户看的通知")


class IdentityBindingTest(unittest.TestCase):
    def test_member_acts_under_its_own_name(self) -> None:
        """
        ⚠ 运行器必须在线程开头绑定协作身份。

        漏绑不报错，只会让这个队员以 `main` 的身份发消息与认领任务——
        表现是「worker-a 认领的任务显示成 main 认领的」。
        """
        team = TeamService()
        tasks = TaskManager()
        team.board.create("给它认领的活")

        class _ClaimingProvider(_ScriptedProvider):
            def stream_chat(self, messages, thinking_effort, tools=None, system=None):
                # 在子 Agent 的线程里直接用工具认领，验证身份绑定
                from rhinecode.tools.team_tasks import TaskUpdateTool

                TaskUpdateTool(team).execute({"task_id": "1", "owner": "me"})
                yield from super().stream_chat(messages, thinking_effort, tools, system)

        provider = _ClaimingProvider(["干完了"])
        runtime = _runtime(provider, team)
        _record, _thread = _start(runtime, team, tasks, "worker-a")
        self.assertTrue(
            _wait_until(lambda: team.member("worker-a").state is MemberState.IDLE)
        )
        self.assertEqual(team.board.get("1").owner, "worker-a")


class ZeroRegressionTest(unittest.TestCase):
    def test_without_team_the_member_just_finishes(self) -> None:
        """
        spec N5：不启用协作能力时行为与 C13/C14 逐字一致——
        跑完即结束，没有待命、没有第二条任务记录。
        """
        tasks = TaskManager()
        provider = _ScriptedProvider(["结论"])
        runtime = _runtime(provider, team=None)
        record = tasks.create(KIND_ROLE, "explorer", "干活")
        thread = threading.Thread(
            target=run_subagent,
            args=(
                runtime, None, "干活", record, tasks,
                ToolsetResult(allowed=frozenset()), frozenset(),
            ),
            daemon=True,
        )
        thread.start()
        thread.join(timeout=5.0)
        self.assertFalse(thread.is_alive())
        self.assertIs(record.status, TaskStatus.COMPLETED)
        self.assertEqual(len(tasks.snapshot()), 1)


if __name__ == "__main__":
    unittest.main()
