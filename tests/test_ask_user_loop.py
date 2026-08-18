"""
澄清提问在 Agent 循环里的行为（ask-user 扩展 F1–F4 / F17，spec AC1–AC5、AC15、AC16）。

被测的是循环侧的四件事：

1. **可见性判据**——`ask_user` 什么时候发给模型（F1），以及它顺带兑现的
   两条不变量（子 Agent / 无人值守轮拿不到，F2/F3）；
2. **「问不了人」的三条文案**（F4）；
3. **`Esc` 的语义按阶段分岔**（F17）——规划阶段停整轮，其余继续；
4. **跳过熔断**（F17）。

界面完全不参与：`clarify` 回调在这里是一个记账用的假函数。
"""

from __future__ import annotations

import threading
import unittest
from typing import Optional

from rhinecode.agent.clarify import SKIP_LIMIT
from rhinecode.agent.events import ClarifyQuestion, ClarifyReply
from rhinecode.agent.loop import Agent, _RoundContext
from rhinecode.permission import PermissionEngine, PermissionMode
from rhinecode.permission.rules import RuleSet
from rhinecode.provider.base import ToolCall
from rhinecode.tools.base import Tool, ToolResult
from rhinecode.tools.registry import ToolRegistry


class _Reader(Tool):
    name = "peek"
    description = "读点东西"
    parameters = {"type": "object", "properties": {}}
    read_only = True

    def execute(self, args: dict) -> ToolResult:
        return ToolResult(ok=True, output="读到了", summary="ok")


class _Clarify:
    """
    记账用的假澄清回调。

    ⚠ **它记的是「被调了几次、每次拿到什么」**，而不只是最后的结果。
    熔断那条判据只有靠调用次数才分得出「熔断了」与「又问了一次而用户又跳过」。
    """

    def __init__(self, replies: Optional[list] = None) -> None:
        # 每次调用按顺序取一个回复；取完之后一律返回 None（=跳过）
        self._replies = list(replies or [])
        self.calls: list[tuple[str, int, int]] = []

    def __call__(
        self, question: ClarifyQuestion, index: int, total: int
    ) -> Optional[ClarifyReply]:
        self.calls.append((question.question, index, total))
        if self._replies:
            return self._replies.pop(0)
        return None


def _ask_call(questions, call_id="1") -> ToolCall:
    return ToolCall(id=call_id, name="ask_user", arguments={"questions": questions})


def _q(text="放哪一层？", labels=("甲", "乙")):
    return {"question": text, "options": [{"label": x} for x in labels]}


def _drive(
    agent: Agent,
    calls: list[ToolCall],
    *,
    clarify=None,
    planning: bool = False,
    interactive: bool = True,
    unattended: bool = False,
    clarify_skips: int = 0,
    ctx: Optional[_RoundContext] = None,
):
    """跑一次 `_execute`，返回 (结果字典, 本轮上下文)。"""
    results: dict[str, ToolResult] = {}
    ctx = ctx or _RoundContext()
    list(
        agent._execute(
            calls,
            results,
            ctx,
            PermissionEngine(RuleSet([]), mode=PermissionMode.PERMISSIVE),
            lambda *a: True,
            clarify,
            None,
            threading.Event(),
            frozenset(),
            planning=planning,
            interactive=interactive,
            unattended=unattended,
            clarify_skips=clarify_skips,
        )
    )
    return results, ctx


def _agent() -> Agent:
    reg = ToolRegistry()
    reg.register(_Reader())
    return Agent(provider=None, registry=reg)


# ---------------------------------------------------------------------------
# F1 / F2 / F3：可见性判据
# ---------------------------------------------------------------------------
class VisibilityTest(unittest.TestCase):
    """
    ⚠ **本组是整个扩展的地基。**

    「能不能问」= 「有没有人可问」，这是**结构**不是约定：子 Agent（F2）与
    无人值守轮（F3）都因为拿不到澄清回调而看不到这个工具，不必另立
    一张「哪些场合禁用」的清单——那种清单要在每个新场合出现时记得去加一行，
    而漏加不报错。
    """

    def setUp(self) -> None:
        self.agent = _agent()

    def _names(self, **kwargs) -> list[str]:
        schemas = self.agent._schema_for(**kwargs) or []
        return [s["function"]["name"] for s in schemas]

    def test_visible_whenever_someone_can_answer(self) -> None:
        """有澄清回调 → 任何阶段都发。这是 F1 的正面。"""
        for plan_mode, execution_phase in ((False, False), (True, False), (True, True)):
            with self.subTest(plan_mode=plan_mode, execution_phase=execution_phase):
                self.assertIn(
                    "ask_user",
                    self._names(
                        plan_mode=plan_mode,
                        execution_phase=execution_phase,
                        can_ask_user=True,
                    ),
                )

    def test_invisible_when_nobody_can_answer(self) -> None:
        """
        没有澄清回调 → 一个阶段都不发。

        **反证**：这条与上面那条构成 F1 的完整定义。少了它，一个「无条件
        发送」的实现也能让上面那条通过。
        """
        for plan_mode, execution_phase in ((False, False), (True, False), (True, True)):
            with self.subTest(plan_mode=plan_mode, execution_phase=execution_phase):
                self.assertNotIn(
                    "ask_user",
                    self._names(
                        plan_mode=plan_mode,
                        execution_phase=execution_phase,
                        can_ask_user=False,
                    ),
                )

    def test_present_plan_stays_planning_only(self) -> None:
        """
        ⚠ **`present_plan` 的判据没有跟着变。**

        两个特殊工具此前同进同出，本次刻意分了家（F1）。这条钉住
        「只放开了 `ask_user` 一个」——把 `ask_schemas()` 写回成
        「返回两个」会当场红。
        """
        self.assertIn(
            "present_plan",
            self._names(plan_mode=True, execution_phase=False, can_ask_user=True),
        )
        for plan_mode, execution_phase in ((False, False), (True, True)):
            with self.subTest(plan_mode=plan_mode, execution_phase=execution_phase):
                self.assertNotIn(
                    "present_plan",
                    self._names(
                        plan_mode=plan_mode,
                        execution_phase=execution_phase,
                        can_ask_user=True,
                    ),
                )

    def test_default_is_invisible(self) -> None:
        """
        缺省不发——**偏严方向**。

        新增调用点忘了传 `can_ask_user` 时，后果是「该问的没问」（看得见、
        用户会来问），而不是「不该弹的弹了」（用户不在场时尤其糟）。
        """
        self.assertNotIn(
            "ask_user", self._names(plan_mode=False, execution_phase=False)
        )


# ---------------------------------------------------------------------------
# F4：三条「问不了人」的文案
# ---------------------------------------------------------------------------
class UnavailableTest(unittest.TestCase):
    """
    三种情形下模型该做的事不同，所以文案必须分开。

    共同点是**三条都不终止循环、都不置「用户拒绝」**：没有任何用户做过决定。
    """

    def setUp(self) -> None:
        self.agent = _agent()

    def _run(self, **kwargs) -> tuple[ToolResult, _RoundContext]:
        results, ctx = _drive(
            self.agent, [_ask_call([_q()])], clarify=None, **kwargs
        )
        return results["1"], ctx

    def test_three_distinct_texts(self) -> None:
        sub, _ = self._run(interactive=False)
        unattended, _ = self._run(unattended=True)
        generic, _ = self._run()
        texts = {sub.output, unattended.output, generic.output}
        self.assertEqual(len(texts), 3, "三种情形的回灌文案必须各不相同")

    def test_subagent_text_tells_it_to_write_into_the_conclusion(self) -> None:
        """
        子 Agent 那条要指向**结论**——那是它唯一能把不确定性传出去的通道。
        """
        res, _ = self._run(interactive=False)
        self.assertIn("子 Agent", res.output)
        self.assertIn("结论", res.output)

    def test_unattended_text_says_the_user_is_away(self) -> None:
        res, _ = self._run(unattended=True)
        self.assertIn("不在场", res.output)

    def test_none_of_them_stops_the_loop(self) -> None:
        """
        ⚠ **三条都不许置 `cancelled` 或 `user_denied`。**

        `cancelled` 会让主循环立刻以「用户取消」收尾；`user_denied` 会让
        **下一轮一件工具都不发**。这四种情形里用户要么不在场、要么明说了
        「你自己定」，两个标志都用错了地方。
        """
        for kwargs in ({"interactive": False}, {"unattended": True}, {}):
            with self.subTest(kwargs=kwargs):
                _res, ctx = self._run(**kwargs)
                self.assertFalse(ctx.cancelled)
                self.assertFalse(ctx.user_denied)


# ---------------------------------------------------------------------------
# F7 / F14：多问题串行
# ---------------------------------------------------------------------------
class SerialQuestionsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.agent = _agent()

    def test_each_question_gets_one_call_with_progress(self) -> None:
        """三个问题 → 回调被调三次，每次带正确的 (第几题, 共几题)。"""
        clarify = _Clarify([ClarifyReply("option", ("甲",))] * 3)
        _drive(
            self.agent,
            [_ask_call([_q("一"), _q("二"), _q("三")])],
            clarify=clarify,
        )
        self.assertEqual(
            clarify.calls, [("一", 0, 3), ("二", 1, 3), ("三", 2, 3)]
        )

    def test_all_answers_come_back_in_one_result(self) -> None:
        clarify = _Clarify([
            ClarifyReply("option", ("甲",)),
            ClarifyReply("multi", ("甲", "乙")),
        ])
        results, _ = _drive(
            self.agent, [_ask_call([_q("一"), _q("二")])], clarify=clarify
        )
        output = results["1"].output
        self.assertIn("一", output)
        self.assertIn("二", output)
        self.assertIn("甲, 乙", output)

    def test_skipping_stops_the_remaining_questions(self) -> None:
        """
        中途跳过 → 剩下的**不再呈现**。

        他已经表态不想挑了，再弹两次只会更烦。
        """
        clarify = _Clarify([ClarifyReply("option", ("甲",)), None])
        _drive(
            self.agent,
            [_ask_call([_q("一"), _q("二"), _q("三")])],
            clarify=clarify,
        )
        self.assertEqual(len(clarify.calls), 2, "第三题不该再问")


# ---------------------------------------------------------------------------
# F17：Esc 的语义按阶段分岔
# ---------------------------------------------------------------------------
class SkipSemanticsTest(unittest.TestCase):
    """
    ⚠ **两条是两个方向的断言，缺一条就分不出「改对了」与「两边都改成了同一种」。**
    """

    def setUp(self) -> None:
        self.agent = _agent()

    def test_planning_stage_still_stops_the_whole_round(self) -> None:
        """
        规划阶段维持 C4 以来的语义：Esc = 不想规划了，整轮结束。

        这是 spec N3「Plan Mode 零回归」的一部分。
        """
        _results, ctx = _drive(
            self.agent, [_ask_call([_q()])], clarify=_Clarify(), planning=True
        )
        self.assertTrue(ctx.cancelled, "规划阶段按 Esc 必须停掉整轮")

    def test_outside_planning_the_loop_continues(self) -> None:
        results, ctx = _drive(
            self.agent, [_ask_call([_q()])], clarify=_Clarify(), planning=False
        )
        self.assertFalse(ctx.cancelled, "非规划阶段按 Esc 不该停掉整轮")
        self.assertTrue(results["1"].ok)

    def test_the_feedback_carries_both_meanings(self) -> None:
        """
        回灌必须同时含两层意思（F17）：

        ① 按你的最佳判断继续；② **不要为同一件事再问一次**。

        少了第 ② 层，模型下一轮会原样再问一遍——与「用户在确认面板里选拒绝」
        那条软约束同型，那次实测过一个不听劝的模型把 25 轮迭代全烧在重试上。
        """
        results, _ = _drive(self.agent, [_ask_call([_q()])], clarify=_Clarify())
        output = results["1"].output
        self.assertIn("最佳判断", output)
        self.assertIn("不要为同一件事再问一次", output)

    def test_skip_is_counted(self) -> None:
        _results, ctx = _drive(self.agent, [_ask_call([_q()])], clarify=_Clarify())
        self.assertEqual(ctx.clarify_skipped, 1)


# ---------------------------------------------------------------------------
# F17：跳过熔断
# ---------------------------------------------------------------------------
class SkipCircuitTest(unittest.TestCase):
    """
    ⚠ **本组的判据一律是「回调被调了几次」，不是「结果是什么」。**

    只断言「结果是跳过」的话，「熔断了、一次面板都没弹」与「又弹了一次面板、
    用户又跳过了」**看起来一模一样**——而那正是这个功能的全部内容。
    """

    def setUp(self) -> None:
        self.agent = _agent()

    def test_below_the_limit_still_asks(self) -> None:
        clarify = _Clarify()
        _drive(
            self.agent,
            [_ask_call([_q()])],
            clarify=clarify,
            clarify_skips=SKIP_LIMIT - 1,
        )
        self.assertEqual(len(clarify.calls), 1, "还没到上限，该问就得问")

    def test_at_the_limit_no_panel_at_all(self) -> None:
        clarify = _Clarify()
        results, _ = _drive(
            self.agent,
            [_ask_call([_q()])],
            clarify=clarify,
            clarify_skips=SKIP_LIMIT,
        )
        self.assertEqual(len(clarify.calls), 0, "已熔断，一次面板都不该弹")
        self.assertTrue(results["1"].ok)
        self.assertIn("跳过", results["1"].output)

    def test_two_calls_in_the_same_round_also_trip_it(self) -> None:
        """
        ⚠ **同一轮里的两次调用也要计入。**

        判据写成「只看本轮之前的累计」的话，模型在同一轮里连发两次
        `ask_user`，第二次仍然会弹——用户刚说完两次「别问我」，
        马上又被问第三次。而这个 bug 只在「模型一轮发多次」时出现，
        单次调用的手测完全看不到。
        """
        clarify = _Clarify()
        ctx = _RoundContext()
        _drive(
            self.agent,
            [_ask_call([_q()], "1"), _ask_call([_q()], "2"), _ask_call([_q()], "3")],
            clarify=clarify,
            clarify_skips=0,
            ctx=ctx,
        )
        # 第 1、2 次各弹一次并被跳过；第 3 次已达上限，不再弹。
        self.assertEqual(len(clarify.calls), SKIP_LIMIT)
        self.assertEqual(ctx.clarify_skipped, SKIP_LIMIT)


# ---------------------------------------------------------------------------
# F10：没有可用问题
# ---------------------------------------------------------------------------
class NoQuestionsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.agent = _agent()

    def test_bad_payload_does_not_pop_a_panel(self) -> None:
        clarify = _Clarify()
        results, ctx = _drive(
            self.agent,
            [ToolCall(id="1", name="ask_user", arguments={"questions": "放哪一层？"})],
            clarify=clarify,
        )
        self.assertEqual(len(clarify.calls), 0)
        self.assertFalse(results["1"].ok)
        self.assertFalse(ctx.cancelled, "参数写歪了不是「用户取消」")


if __name__ == "__main__":
    unittest.main()
