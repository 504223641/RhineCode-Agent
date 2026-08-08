"""
闸门的单测（c15 T20，覆盖 AC15、AC16 的注入面）。

重点两处：**`TeamGate.has_awaited` 恒为假**（改成真会让队员再也停不下来），
以及 **`CompositeGate` 只对「真的有东西要等」的闸门调阻塞的 `wait_any`**。
"""

from __future__ import annotations

import threading
import unittest

from rhinecode.agent.gate import CompositeGate, NullGate, SubAgentGateProtocol
from rhinecode.provider.base import Message
from rhinecode.team import TeamService
from rhinecode.team.gate import TeamGate
from rhinecode.team.models import MAIN_NAME
from rhinecode.team.render import TAG


class _FakeGate:
    """一个可编程的假闸门，用来验证组合行为。"""

    def __init__(self, pending=(), awaited=False, wait_result=False) -> None:
        self.pending = list(pending)
        self.awaited = awaited
        self.wait_result = wait_result
        self.wait_calls = 0

    def take_pending(self):
        out, self.pending = self.pending, []
        return out

    def has_awaited(self) -> bool:
        return self.awaited

    def wait_any(self, cancel_event) -> bool:  # noqa: ARG002
        self.wait_calls += 1
        return self.wait_result

    def describe_awaited(self) -> str:
        return "假任务" if self.awaited else ""


class TeamGateTest(unittest.TestCase):
    def setUp(self) -> None:
        self.service = TeamService()
        self.service.register_member("worker-a", "explorer")
        self.service.register_member("worker-b", "explorer")
        self.gate = TeamGate(self.service, "worker-b")

    def test_satisfies_the_protocol(self) -> None:
        self.assertIsInstance(self.gate, SubAgentGateProtocol)

    def test_no_unread_yields_nothing(self) -> None:
        self.assertEqual(self.gate.take_pending(), [])

    def test_unread_is_rendered_into_one_message(self) -> None:
        self.service.send("worker-a", "worker-b", "接口要改成异步的")
        messages = self.gate.take_pending()
        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0].role, "user")
        self.assertIn("接口要改成异步的", messages[0].content)
        self.assertIn(f"<{TAG} ", messages[0].content)

    def test_take_pending_is_idempotent(self) -> None:
        """
        ⚠ 没有这条幂等性，同一条消息会在收件人的**每一轮迭代**里被注入一遍。
        """
        self.service.send("worker-a", "worker-b", "只说一次")
        self.assertEqual(len(self.gate.take_pending()), 1)
        self.assertEqual(self.gate.take_pending(), [])

    def test_multiple_unread_merge_into_one_message(self) -> None:
        for index in range(3):
            self.service.send("worker-a", "worker-b", f"第 {index} 条")
        messages = self.gate.take_pending()
        self.assertEqual(len(messages), 1)
        for index in range(3):
            self.assertIn(f"第 {index} 条", messages[0].content)

    def test_gate_only_sees_its_own_mailbox(self) -> None:
        """一个闸门只管一个人的信箱——共用实例会让 A 取走 B 的消息。"""
        self.service.send("worker-b", "worker-a", "给 A 的")
        self.assertEqual(self.gate.take_pending(), [])
        self.assertEqual(len(TeamGate(self.service, "worker-a").take_pending()), 1)

    def test_main_gate_receives_messages_to_main(self) -> None:
        self.service.send("worker-a", MAIN_NAME, "给主对话的")
        self.assertEqual(len(TeamGate(self.service, MAIN_NAME).take_pending()), 1)

    def test_has_awaited_is_always_false(self) -> None:
        """
        ⚠ 这条**不是多余的**，请勿「顺手修正」成 `True`。

        一个队员只要还在花名册上就永远可能收到消息。返回 `True` 等于说
        「你永远有东西要等」——它再也不会收工，会一直挂在 `wait_any` 里
        直到撞上兜底上限。

        等消息发生在**待命状态**（阻塞在 `wake_event` 上），不在 Agent Loop 里。
        """
        self.assertFalse(self.gate.has_awaited())
        self.service.send("worker-a", "worker-b", "有一条未读了")
        self.assertFalse(
            self.gate.has_awaited(), "即使有未读，也不该让循环停下来等"
        )

    def test_wait_any_is_always_false(self) -> None:
        self.assertFalse(self.gate.wait_any(threading.Event()))

    def test_describe_awaited_is_empty(self) -> None:
        self.assertEqual(self.gate.describe_awaited(), "")


class CompositeGateTest(unittest.TestCase):
    def test_satisfies_the_protocol(self) -> None:
        self.assertIsInstance(CompositeGate([NullGate()]), SubAgentGateProtocol)

    def test_pending_is_concatenated_in_order(self) -> None:
        first = _FakeGate(pending=[Message(role="user", content="子 Agent 结论")])
        second = _FakeGate(pending=[Message(role="user", content="队友消息")])
        messages = CompositeGate([first, second]).take_pending()
        self.assertEqual(
            [m.content for m in messages], ["子 Agent 结论", "队友消息"]
        )

    def test_none_entries_are_dropped(self) -> None:
        """调用方可以直接传 `[maybe_a, maybe_b]` 而不必先自己筛。"""
        gate = CompositeGate([None, _FakeGate(pending=[Message(role="user", content="x")])])
        self.assertEqual(len(gate.take_pending()), 1)

    def test_empty_composite_is_harmless(self) -> None:
        gate = CompositeGate([])
        self.assertEqual(gate.take_pending(), [])
        self.assertFalse(gate.has_awaited())
        self.assertFalse(gate.wait_any(threading.Event()))
        self.assertEqual(gate.describe_awaited(), "")

    def test_has_awaited_is_any(self) -> None:
        self.assertFalse(CompositeGate([_FakeGate(), _FakeGate()]).has_awaited())
        self.assertTrue(CompositeGate([_FakeGate(), _FakeGate(awaited=True)]).has_awaited())

    def test_wait_any_skips_gates_with_nothing_awaited(self) -> None:
        """
        ⚠ `wait_any` 是**阻塞**的。无差别地逐个调用，第一个会一直等下去、
        后面的永远轮不到。先问 `has_awaited` 才能跳过「没什么可等的」那些。
        """
        idle = _FakeGate(awaited=False, wait_result=True)
        busy = _FakeGate(awaited=True, wait_result=True)
        self.assertTrue(CompositeGate([idle, busy]).wait_any(threading.Event()))
        self.assertEqual(idle.wait_calls, 0, "没东西要等的闸门不该被调到")
        self.assertEqual(busy.wait_calls, 1)

    def test_wait_any_short_circuits_on_first_success(self) -> None:
        first = _FakeGate(awaited=True, wait_result=True)
        second = _FakeGate(awaited=True, wait_result=True)
        self.assertTrue(CompositeGate([first, second]).wait_any(threading.Event()))
        self.assertEqual(second.wait_calls, 0, "第一个成功后不该继续等")

    def test_describe_joins_non_empty_parts(self) -> None:
        gate = CompositeGate([_FakeGate(awaited=True), _FakeGate(awaited=False)])
        self.assertEqual(gate.describe_awaited(), "假任务")

    def test_team_gate_never_blocks_a_composite(self) -> None:
        """
        组合起来之后的整体行为：只有子 Agent 那一侧能让循环停下来等，
        队友消息那一侧永远不会。
        """
        service = TeamService()
        service.register_member("worker-a", "explorer")
        service.send("worker-a", MAIN_NAME, "有未读")
        composite = CompositeGate([_FakeGate(), TeamGate(service, MAIN_NAME)])
        self.assertFalse(composite.has_awaited())
        self.assertEqual(len(composite.take_pending()), 1, "但消息照样注入")


if __name__ == "__main__":
    unittest.main()
