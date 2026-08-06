"""
服务门面的单测（c13 T18，覆盖 AC8c / AC12 / AC17a / AC17b / AC18 / AC20c）。

重点是**失败路径的代价**：F14（空工具集）与 F20（并发上限）的全部价值
在于「不起线程、不发 API」。只断言返回值 `ok=False` 验不出这一点——
一个先起线程再返回失败的实现照样能过。因此本模块用**计数器**做判据。
"""

from __future__ import annotations

import threading
import time
import unittest
from pathlib import Path

from rhinecode.permission.engine import PermissionEngine
from rhinecode.permission.models import PermissionMode
from rhinecode.permission.rules import RuleSet
from rhinecode.provider.base import BaseProvider, StreamChunk
from rhinecode.subagents.models import AgentCatalog, AgentSource, AgentSpec
from rhinecode.subagents.runner import ParentSnapshot, SubAgentRuntime
from rhinecode.subagents.service import SubAgentService
from rhinecode.subagents.tasks import KIND_BRANCH, KIND_ROLE, TaskStatus
from rhinecode.tools.base import Tool, ToolResult
from rhinecode.tools.registry import ToolRegistry


class _Reader(Tool):
    name = "read_file"
    description = "fake"
    parameters = {"type": "object", "properties": {}}
    read_only = True

    def execute(self, args: dict) -> ToolResult:
        return ToolResult(ok=True, output="x", summary="x")


class _CountingProvider(BaseProvider):
    """记下被调了几次——「没发过 API」这条判据靠它。"""

    def __init__(self, delay: float = 0.0, reply: str = "结论") -> None:
        self.calls = 0
        self._delay = delay
        self._reply = reply

    def stream_chat(self, messages, thinking_effort="off", tools=None, system=None):
        self.calls += 1
        if self._delay:
            time.sleep(self._delay)
        yield StreamChunk(type="text", content=self._reply)
        yield StreamChunk(type="done")


def _spec(name="explorer", **kw) -> AgentSpec:
    base = dict(
        name=name,
        description="x",
        body="你是调研员。",
        source=AgentSource.BUILTIN,
        path=Path(f"{name}.md"),
    )
    base.update(kw)
    return AgentSpec(**base)


class ServiceBase(unittest.TestCase):
    def setUp(self) -> None:
        self.registry = ToolRegistry()
        self.registry.register(_Reader())
        self.engine = PermissionEngine(RuleSet([]), mode=PermissionMode.DEFAULT)
        self.provider = _CountingProvider()

    def _service(self, specs=None, provider=None, **kw) -> SubAgentService:
        provider = provider or self.provider
        catalog = AgentCatalog(specs=specs if specs is not None else {"explorer": _spec()})
        runtime = SubAgentRuntime(
            provider_for=lambda name: provider,
            registry=self.registry,
            engine=self.engine,
            main_mode=lambda: self.engine.mode,
            environment_text=lambda: "env",
            default_model="m",
        )
        return SubAgentService(
            catalog,
            runtime,
            tool_names_provider=lambda: self.registry.names(),
            **kw,
        )

    @staticmethod
    def _settle(service, timeout: float = 5.0) -> None:
        """等全部任务进入终态，避免线程泄漏到下一个用例。"""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if all(r.status.is_terminal for r in service.tasks.snapshot()):
                return
            time.sleep(0.01)


class ArgumentValidationTest(ServiceBase):
    def test_invalid_kind(self) -> None:
        outcome = self._service().delegate("bogus", "explorer", "t")
        self.assertFalse(outcome.ok)
        self.assertIn("type", outcome.text)
        self.assertEqual(self.provider.calls, 0)

    def test_blank_task(self) -> None:
        outcome = self._service().delegate(KIND_ROLE, "explorer", "   ")
        self.assertFalse(outcome.ok)
        self.assertIn("task", outcome.text)
        self.assertEqual(self.provider.calls, 0)

    def test_task_text_message_explains_self_containment(self) -> None:
        """
        空任务的提示要说明**为什么**必须自包含。

        只说「task 不能为空」的话，模型下次可能给一句「继续刚才那个」——
        而子 Agent 看不到「刚才」。
        """
        outcome = self._service().delegate(KIND_ROLE, "explorer", "")
        self.assertIn("自包含", outcome.text)


class UnknownAgentTest(ServiceBase):
    def test_lists_available_agents(self) -> None:
        service = self._service(specs={"explorer": _spec(), "reviewer": _spec("reviewer")})
        outcome = service.delegate(KIND_ROLE, "nope", "t")

        self.assertFalse(outcome.ok)
        self.assertIn("explorer", outcome.text)
        self.assertIn("reviewer", outcome.text)
        self.assertEqual(self.provider.calls, 0)

    def test_empty_catalog_says_so(self) -> None:
        outcome = self._service(specs={}).delegate(KIND_ROLE, "x", "t")
        self.assertIn("一个角色都没有加载", outcome.text)


class EmptyToolsetTest(ServiceBase):
    """AC12：空工具集失败，且**不起线程不发 API**。"""

    def test_all_tool_names_wrong(self) -> None:
        service = self._service(specs={"broken": _spec("broken", tools=("redFile",))})
        outcome = service.delegate(KIND_ROLE, "broken", "t")

        self.assertFalse(outcome.ok)
        self.assertIn("redFile", outcome.text)

    def test_no_thread_and_no_api_call(self) -> None:
        """
        **本类的核心判据**：没建任务、没发请求。

        只验返回值的话，一个「先起线程再返回失败」的实现照样能过，
        而 F14 的全部价值就是「不白烧一次 API 调用」。
        """
        before = threading.active_count()
        service = self._service(specs={"broken": _spec("broken", tools=("nope",))})
        service.delegate(KIND_ROLE, "broken", "t")

        self.assertEqual(self.provider.calls, 0)
        self.assertEqual(service.tasks.snapshot(), ())
        self.assertLessEqual(threading.active_count(), before)


class ConcurrencyLimitTest(ServiceBase):
    """AC18：并发上限。"""

    def test_fourth_delegation_fails_and_names_the_running_ones(self) -> None:
        service = self._service(max_concurrent=3)
        # 塞三个「在跑」的任务（不起线程，直接造记录）
        running = [service.tasks.create(KIND_ROLE, f"a{i}", "t") for i in range(3)]

        outcome = service.delegate(KIND_ROLE, "explorer", "t")

        self.assertFalse(outcome.ok)
        for record in running:
            self.assertIn(record.task_id, outcome.text)
        self.assertEqual(self.provider.calls, 0)

    def test_finished_tasks_do_not_count(self) -> None:
        service = self._service(max_concurrent=1)
        done = service.tasks.create(KIND_ROLE, "a", "t")
        service.tasks.finish(done.task_id, TaskStatus.COMPLETED, "x")

        outcome = service.delegate(KIND_ROLE, "explorer", "t")
        self._settle(service)
        self.assertTrue(outcome.ok)


class ForegroundTest(ServiceBase):
    """AC17b：前台等待与超时转后台。"""

    def test_fast_task_returns_conclusion(self) -> None:
        service = self._service()
        outcome = service.delegate(KIND_ROLE, "explorer", "t")

        self.assertTrue(outcome.ok)
        self.assertFalse(outcome.backgrounded)
        self.assertEqual(outcome.text, "结论")

    def test_timeout_hands_off_to_background(self) -> None:
        """超时后工具立即返回，而任务**仍在跑**（不是被取消）。"""
        slow = _CountingProvider(delay=0.5)
        service = self._service(provider=slow, foreground_timeout=0.05)

        outcome = service.delegate(KIND_ROLE, "explorer", "t")

        self.assertTrue(outcome.ok, "转后台算成功发起")
        self.assertTrue(outcome.backgrounded)
        self.assertIn(outcome.task_id, outcome.text)
        record = service.tasks.get(outcome.task_id)
        self.assertIs(record.status, TaskStatus.RUNNING)
        self.assertFalse(record.cancel_event.is_set(), "转后台不是取消")

        self._settle(service)
        self.assertIs(record.status, TaskStatus.COMPLETED)

    def test_backgrounded_text_forbids_polling(self) -> None:
        """
        转后台的文案必须明确「结论会自动送达、别反复问进度」。

        没有这句的话，模型拿到一个任务标识，很自然的下一步就是去查它——
        而本章刻意不提供查询工具（结论是推过去的，spec F21）。
        """
        service = self._service(foreground_timeout=0.01)
        outcome = service.delegate(KIND_ROLE, "explorer", "t", background=True)

        self.assertIn("自动送达", outcome.text)
        self.assertIn("不要反复询问进度", outcome.text)
        self._settle(service)

    def test_explicit_background_does_not_block(self) -> None:
        """AC17a：显式后台时立即返回，不等那 0.3 秒。"""
        slow = _CountingProvider(delay=0.3)
        service = self._service(provider=slow, foreground_timeout=30.0)

        started = time.monotonic()
        outcome = service.delegate(KIND_ROLE, "explorer", "t", background=True)
        elapsed = time.monotonic() - started

        self.assertTrue(outcome.backgrounded)
        self.assertLess(elapsed, 0.2, "显式后台不该阻塞")
        self._settle(service)


class ManualBackgroundTest(ServiceBase):
    """AC17c 的可自动化部分：`request_background` 让等待方提前返回。"""

    def test_request_background_releases_the_waiter(self) -> None:
        slow = _CountingProvider(delay=1.0)
        service = self._service(provider=slow, foreground_timeout=30.0)
        result: list = []

        def worker() -> None:
            result.append(service.delegate(KIND_ROLE, "explorer", "t"))

        thread = threading.Thread(target=worker)
        thread.start()

        # 等它进入前台等待
        deadline = time.monotonic() + 3
        while service.foreground_task_id() is None and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertIsNotNone(service.foreground_task_id(), "应当已进入前台等待")

        switched = service.request_background()
        thread.join(timeout=5)

        self.assertIsNotNone(switched)
        self.assertTrue(result and result[0].backgrounded)
        self._settle(service, timeout=5)

    def test_request_background_without_foreground_task(self) -> None:
        self.assertIsNone(self._service().request_background())

    def test_foreground_id_cleared_after_return(self) -> None:
        service = self._service()
        service.delegate(KIND_ROLE, "explorer", "t")
        self.assertIsNone(service.foreground_task_id())


class BranchDelegationTest(ServiceBase):
    """AC8c：分支式强制后台。"""

    def test_branch_forces_background_even_when_false(self) -> None:
        service = self._service()
        parent = ParentSnapshot(history=(), stable="s", tool_names=("read_file",))

        outcome = service.delegate(KIND_BRANCH, "", "t", background=False, parent=parent)

        self.assertTrue(outcome.ok)
        self.assertTrue(outcome.backgrounded, "分支式必须走后台，即使传了 background=False")
        self._settle(service)

    def test_branch_without_parent_fails_clearly(self) -> None:
        outcome = self._service().delegate(KIND_BRANCH, "", "t")
        self.assertFalse(outcome.ok)
        self.assertIn("type=role", outcome.text)

    def test_branch_ignores_agent_name(self) -> None:
        service = self._service()
        parent = ParentSnapshot(history=(), stable="s", tool_names=())
        outcome = service.delegate(KIND_BRANCH, "不存在的角色", "t", parent=parent)
        self.assertTrue(outcome.ok)
        self._settle(service)


class CancelTest(ServiceBase):
    """AC20c。"""

    def setUp(self) -> None:
        super().setUp()
        self.service = self._service()

    def test_cancel_one(self) -> None:
        record = self.service.tasks.create(KIND_ROLE, "a", "t")
        text = self.service.cancel(record.task_id)
        self.assertIn(record.task_id, text)
        self.assertTrue(record.cancel_event.is_set())

    def test_cancel_all(self) -> None:
        for i in range(2):
            self.service.tasks.create(KIND_ROLE, f"a{i}", "t")
        self.assertIn("2", self.service.cancel(None))

    def test_cancel_all_when_idle(self) -> None:
        self.assertIn("没有正在运行", self.service.cancel(None))

    def test_cancel_unknown_id(self) -> None:
        self.assertIn("没有标识为", self.service.cancel("deadbe"))

    def test_cancel_finished_task_says_so(self) -> None:
        record = self.service.tasks.create(KIND_ROLE, "a", "t")
        self.service.tasks.finish(record.task_id, TaskStatus.COMPLETED, "x")
        self.assertIn("已经结束", self.service.cancel(record.task_id))

    def test_session_switch_returns_count(self) -> None:
        for i in range(3):
            self.service.tasks.create(KIND_ROLE, f"a{i}", "t")
        self.assertEqual(self.service.cancel_all_for_session_switch(), 3)


if __name__ == "__main__":
    unittest.main()
