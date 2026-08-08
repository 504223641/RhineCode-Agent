"""
协作的端到端集成测试（c15 T47，覆盖 AC43–AC45 与 Hook 集成）。

用**假 Provider 驱动真实路径**：真的 `run_subagent`、真的 Agent Loop、
真的权限管线、真的工具注册中心、真的花名册与清单。假的只有「模型说什么」。

三个场景与 checklist 的端到端一一对应：

- 场景 1（AC43）完整协作闭环：拆任务 → 派两个具名队员 → 各自认领 →
  完成解锁后续 → **队员之间直接发一条消息**（不经主 Agent）；
- 场景 2（AC44）无人值守闭环：队员发消息给 `main` → 主对话被自动唤起；
- 场景 3（AC45）唤醒续跑闭环：待命 → 发消息指派第二件事 → 不重新交代背景。
"""

from __future__ import annotations

import threading
import time
import unittest

from rhinecode.hooks import NO_VERDICT
from rhinecode.permission.engine import PermissionEngine
from rhinecode.permission.models import PermissionMode
from rhinecode.permission.rules import RuleSet
from rhinecode.provider.base import StreamChunk, ToolCall
from rhinecode.subagents.runner import SubAgentRuntime, run_subagent
from rhinecode.subagents.tasks import KIND_ROLE, TaskManager
from rhinecode.subagents.toolset import resolve_toolset
from rhinecode.team import TeamService
from rhinecode.team.models import MAIN_NAME, MemberState, TaskState
from rhinecode.tools.registry import ToolRegistry
from rhinecode.tools.send_message import SendMessageTool
from rhinecode.tools.team_tasks import build_board_tools


class _ScriptedAgent:
    """
    按脚本逐轮行动的假 Provider。

    脚本每项是 `("tool", 工具名, 参数)` 或 `("text", 正文)`。
    脚本走完之后一直回最后一条文本（Agent Loop 据此判自然完成）。
    """

    def __init__(self, script) -> None:
        self.script = list(script)
        self.step = 0
        self.rounds = 0
        self.seen: list[list] = []

    def stream_chat(self, messages, thinking_effort, tools=None, system=None):
        self.rounds += 1
        self.seen.append(list(messages))
        if self.step >= len(self.script):
            yield StreamChunk(type="text", content="做完了")
            return
        kind, *rest = self.script[self.step]
        self.step += 1
        if kind == "tool":
            name, args = rest
            yield StreamChunk(
                type="tool_call",
                tool_call=ToolCall(id=f"c{self.step}", name=name, arguments=args),
            )
        else:
            yield StreamChunk(type="text", content=rest[0])


def _registry(team: TeamService) -> ToolRegistry:
    registry = ToolRegistry()
    for tool in build_board_tools(team):
        registry.register(tool)
    registry.register(SendMessageTool(team))
    return registry


def _runtime(provider, team, registry) -> SubAgentRuntime:
    return SubAgentRuntime(
        provider_for=lambda _m: provider,
        registry=registry,
        engine=PermissionEngine(RuleSet(rules=[]), mode=PermissionMode.DEFAULT),
        main_mode=lambda: PermissionMode.DEFAULT,
        environment_text=lambda _cwd: "",
        default_model="fake",
        team=team,
    )


def _spawn(team, tasks, registry, provider, name: str, task_text: str = "干活"):
    """委派一个具名队员并在后台线程里跑它。"""
    registration = team.register_member(name, "worker")
    record = tasks.create(KIND_ROLE, "worker", task_text)
    record.member_name = registration.name
    toolset = resolve_toolset(registry.names(), None)
    thread = threading.Thread(
        target=run_subagent,
        args=(
            _runtime(provider, team, registry),
            None, task_text, record, tasks, toolset, registry.names(),
        ),
        daemon=True,
    )
    thread.start()
    return record, thread


def _wait(predicate, timeout: float = 8.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


class FullCollaborationTest(unittest.TestCase):
    """场景 1（AC43）：完整协作闭环。"""

    def test_two_members_self_schedule_through_the_board(self) -> None:
        team = TeamService()
        tasks = TaskManager()
        registry = _registry(team)

        # 主 Agent 拆出三条带依赖的任务
        team.board.create("设计数据表", "建表")
        team.board.create("写后端接口", "依赖表结构")
        team.board.create("写测试", "覆盖新接口")
        team.board.add_dependency("2", "1")
        team.board.add_dependency("3", "2")

        # 队员甲：认领 1 → 完成 1 → **直接告诉乙**（不经主 Agent）→ 收工
        alpha = _ScriptedAgent([
            ("tool", "task_update", {"task_id": "1", "owner": "me"}),
            ("tool", "task_update", {"task_id": "1", "status": "completed"}),
            ("tool", "send_message", {
                "to": "beta",
                "message": "表结构定好了，字段清单在 schema.sql",
                "summary": "表结构已完成",
            }),
            ("text", "1 号任务完成"),
        ])
        _record_a, thread_a = _spawn(team, tasks, registry, alpha, "alpha")
        self.assertTrue(_wait(lambda: team.member("alpha").state is MemberState.IDLE))

        # 甲跑完时乙还不存在，那条消息应当明确失败（收件人不存在）
        # ——这正是「先派人再让它们互相说话」的正确顺序。
        self.assertIs(team.board.get("1").state, TaskState.COMPLETED)
        self.assertEqual(team.board.get("1").owner, "")

        # 队员乙：看清单 → 认领 2（现在解锁了）→ 完成 → 收工
        beta = _ScriptedAgent([
            ("tool", "task_list", {}),
            ("tool", "task_update", {"task_id": "2", "owner": "me"}),
            ("tool", "task_update", {"task_id": "2", "status": "completed"}),
            ("text", "2 号任务完成"),
        ])
        _record_b, _thread_b = _spawn(team, tasks, registry, beta, "beta")
        self.assertTrue(_wait(lambda: team.member("beta").state is MemberState.IDLE))

        # 清单终态与实际进度一致
        self.assertIs(team.board.get("2").state, TaskState.COMPLETED)
        self.assertEqual(team.board.is_blocked("3"), (), "3 号应当已解锁")

    def test_member_to_member_message_bypasses_main(self) -> None:
        """
        ⚠ 本章的核心：队员之间**直接**说话，不经主 Agent 中转。

        验证方式是看消息真的落进了对方的信箱，而 `main` 的信箱是空的。
        """
        team = TeamService()
        tasks = TaskManager()
        registry = _registry(team)
        team.register_member("beta", "worker")

        alpha = _ScriptedAgent([
            ("tool", "send_message", {
                "to": "beta",
                "message": "接口要改成异步的",
                "summary": "接口改异步",
            }),
            ("text", "已通知 beta"),
        ])
        _record, _thread = _spawn(team, tasks, registry, alpha, "alpha")
        self.assertTrue(_wait(lambda: team.member("alpha").state is MemberState.IDLE))

        received = team.take_unread("beta")
        self.assertEqual(len(received), 1)
        self.assertEqual(received[0].sender, "alpha")
        self.assertIn("异步", received[0].body)
        self.assertFalse(team.has_unread_for_main(), "这条消息不该经过主对话")

    def test_blocked_task_cannot_be_claimed_end_to_end(self) -> None:
        """AC8 的端到端形态：被挡住的任务在真实工具路径上也认领不了。"""
        team = TeamService()
        tasks = TaskManager()
        registry = _registry(team)
        team.board.create("前置", "")
        team.board.create("后续", "")
        team.board.add_dependency("2", "1")

        agent = _ScriptedAgent([
            ("tool", "task_update", {"task_id": "2", "owner": "me"}),
            ("text", "认领失败了"),
        ])
        _record, _thread = _spawn(team, tasks, registry, agent, "alpha")
        self.assertTrue(_wait(lambda: team.member("alpha").state is MemberState.IDLE))
        self.assertEqual(team.board.get("2").owner, "", "被挡住的任务不该被认领")


class UnattendedLoopTest(unittest.TestCase):
    """场景 2（AC44）：队员发消息给 main，主对话侧能观察到未读。"""

    def test_member_message_reaches_main_and_arms_auto_wake(self) -> None:
        team = TeamService()
        tasks = TaskManager()
        registry = _registry(team)

        agent = _ScriptedAgent([
            ("tool", "send_message", {
                "to": MAIN_NAME,
                "message": "接口要改成异步的，会影响 3 个调用方，要一并改吗？",
                "summary": "需要你决定改动范围",
            }),
            ("text", "已请示主对话"),
        ])
        _record, _thread = _spawn(team, tasks, registry, agent, "alpha")
        self.assertTrue(_wait(lambda: team.has_unread_for_main()))
        self.assertTrue(
            team.can_auto_wake(), "有未读且未达上限 → 自动唤起的条件成立"
        )


class WakeAndContinueTest(unittest.TestCase):
    """场景 3（AC45）：唤醒续跑，不重新交代背景。"""

    def test_second_assignment_keeps_the_first_context(self) -> None:
        team = TeamService()
        tasks = TaskManager()
        registry = _registry(team)
        team.board.create("第一件事", "")
        team.board.create("第二件事", "")

        agent = _ScriptedAgent([
            ("tool", "task_update", {"task_id": "1", "owner": "me"}),
            ("tool", "task_update", {"task_id": "1", "status": "completed"}),
            ("text", "第一件事做完了"),
            # ↓ 被唤醒之后
            ("tool", "task_update", {"task_id": "2", "owner": "me"}),
            ("text", "第二件事也做完了"),
        ])
        _record, _thread = _spawn(team, tasks, registry, agent, "alpha", "做第一件事")
        self.assertTrue(_wait(lambda: team.member("alpha").state is MemberState.IDLE))

        team.send(MAIN_NAME, "alpha", "接着做 2 号任务")
        self.assertTrue(_wait(lambda: team.board.get("2").owner == "alpha"))

        # 唤醒后那一轮的请求里，仍带着第一件事的上下文
        last = agent.seen[-1]
        texts = [str(getattr(m, "content", "")) for m in last]
        self.assertTrue(
            any("做第一件事" in t for t in texts), "第二次指派不该丢掉最初的任务描述"
        )
        self.assertTrue(
            any("第一件事做完了" in t for t in texts), "也不该丢掉它自己的回答"
        )
        self.assertTrue(
            any("接着做 2 号任务" in t for t in texts), "唤醒它的那条消息要被注入"
        )

    def test_no_respawn_happened(self) -> None:
        """
        续跑不该产生一个**新队员**——花名册上始终是同一个名字。

        这条直接验证「名字在它干完之后依然有效」那句描述兑现了。
        """
        team = TeamService()
        tasks = TaskManager()
        registry = _registry(team)
        agent = _ScriptedAgent([("text", "第一轮"), ("text", "第二轮")])
        _record, _thread = _spawn(team, tasks, registry, agent, "alpha")
        self.assertTrue(_wait(lambda: team.member("alpha").state is MemberState.IDLE))
        team.send(MAIN_NAME, "alpha", "再来")
        self.assertTrue(_wait(lambda: agent.rounds >= 2))
        members = [m.name for m in team.members() if not m.is_main]
        self.assertEqual(members, ["alpha"], "不该多出一个新队员")


class HookIntegrationTest(unittest.TestCase):
    """
    ⚠ Hook 对协作工具**全量生效**（沿用 C13 的同名护栏）。

    不生效的话，主 Agent 只要把「跑 git push」改成「发消息让队员去做」
    就能绕过用户写的拦截规则。
    """

    def test_pre_tool_use_sees_collaboration_tools(self) -> None:
        team = TeamService()
        tasks = TaskManager()
        registry = _registry(team)
        seen: list[str] = []

        class _Verdict:
            """
            `dispatch` 的返回值形状：循环取 `.verdict`，再取它的 `.decision`。

            ⚠ 用真实的 `NO_VERDICT`（不表态）而不是 `None`：循环拿到 `None`
            之后仍会访问 `.decision`，那会抛 AttributeError 并被兜底 `try`
            转成一次「工具执行异常」——测试看到的是子 Agent 莫名其妙失败，
            而根因在替身上。
            """

            verdict = NO_VERDICT

        class _Hooks:
            """
            最小可用的 Hook 编排者替身。

            ⚠ **必须实现 `has_listeners`**：循环先问它再构造负载
            （spec N7 的硬要求——一次 `write_file` 的负载就是整份文件内容）。
            少了它，循环的兜底 `try` 会把 AttributeError 吞掉，
            于是**一条事件都不分发**而没有任何报错——C14 踩过这个形态。
            """

            def has_listeners(self, event) -> bool:  # noqa: ARG002
                return True

            def dispatch(self, event, payload_factory, cwd=None):  # noqa: ARG002
                payload = payload_factory()
                name = payload.get("tool")
                if name:
                    seen.append(str(name))
                return _Verdict()

            def consume_injections(self):
                return []

        import dataclasses

        runtime = dataclasses.replace(
            _runtime(
                _ScriptedAgent([
                    ("tool", "task_create", {"subject": "x", "description": "y"}),
                    ("text", "建好了"),
                ]),
                team,
                registry,
            ),
            hooks=_Hooks(),
        )
        registration = team.register_member("alpha", "worker")
        record = tasks.create(KIND_ROLE, "worker", "干活")
        record.member_name = registration.name
        thread = threading.Thread(
            target=run_subagent,
            args=(
                runtime, None, "干活", record, tasks,
                resolve_toolset(registry.names(), None), registry.names(),
            ),
            daemon=True,
        )
        thread.start()
        self.assertTrue(_wait(lambda: team.member("alpha").state is MemberState.IDLE))
        self.assertIn(
            "task_create", seen, "协作工具必须照常经过 Hook 的工具级事件"
        )


if __name__ == "__main__":
    unittest.main()
