"""
Plan Mode 规划阶段的工具阶段强校验（已知项 #2，本轮修复）。

## 这组护栏钉的是一个真实观测样本

C11 场景 10 验收时实测撞到：规划阶段那一轮的 `tool_names` 里**没有**
`run_command`（`_schema_for` 用 `readonly_schemas()` 滤掉了它），模型仍凭训练
先验把它调了出来。而当时 `_execute` 里唯一的守卫 `_visible` 只查 **Skill 白名单**、
不查规划阶段的只读过滤，于是 `outcome=executed`——**规划阶段真的执行了副作用命令**。

那次夹带的恰好是 `git diff`（无害），但同一条路径上完全可能是写命令。

## 为什么不能靠五层权限管线兜底

管线确实一层没少，会弹确认面板。但那是「最后一道」而不是「本该有的一道」：
Plan Mode 对用户的承诺是**「批准前不动手」**，退化成「批准前每次都问你要不要动手」
是两回事——尤其在放行权限模式下，管线根本不会问。

## 与 `out_of_scope` 的关系

**两处过滤职责不同，故走两条独立通道**：
- `out_of_scope`：工具本轮没发给模型（子对话防嵌套），指引是「换个可见工具」；
- `plan_blocked`：工具发了也不行，因为现在是规划阶段，指引是「先提交计划」。

合并成一条会让回灌文案没法同时说对两件事。
"""

from __future__ import annotations

import threading
import unittest

from rhinecode.agent.events import AgentEventType
from rhinecode.agent.loop import (
    Agent,
    OUTCOME_PLAN_BLOCKED,
    _RoundContext,
)
from rhinecode.permission import PermissionEngine, PermissionMode
from rhinecode.permission.rules import RuleSet
from rhinecode.provider.base import ToolCall
from rhinecode.tools.base import Tool, ToolResult
from rhinecode.tools.registry import ToolRegistry


class _Reader(Tool):
    """只读工具——规划阶段应当放行。"""

    name = "peek"
    description = "读点东西"
    parameters = {"type": "object", "properties": {}}
    read_only = True

    def execute(self, args: dict) -> ToolResult:
        return ToolResult(ok=True, output="读到了", summary="ok")


class _Writer(Tool):
    """有副作用的工具——规划阶段应当被挡下。"""

    name = "mutate"
    description = "改点东西"
    parameters = {"type": "object", "properties": {}}
    read_only = False

    def __init__(self) -> None:
        self.ran = 0

    def execute(self, args: dict) -> ToolResult:
        self.ran += 1
        return ToolResult(ok=True, output="改完了", summary="ok")


def _drive(agent: Agent, calls: list[ToolCall], *, planning: bool):
    """跑一次 `_execute`，返回 (结果字典, 事件列表)。"""
    results: dict[str, ToolResult] = {}
    events = list(
        agent._execute(
            calls,
            results,
            _RoundContext(),
            PermissionEngine(RuleSet([]), mode=PermissionMode.PERMISSIVE),
            lambda *a: True,          # ask：一律放行，好让「没被拦」能暴露出来
            None,
            None,
            threading.Event(),
            frozenset(),
            planning=planning,
        )
    )
    return results, events


class PlanStageGuardTest(unittest.TestCase):
    def setUp(self) -> None:
        self.writer = _Writer()
        reg = ToolRegistry()
        reg.register(_Reader())
        reg.register(self.writer)
        self.agent = Agent(provider=None, registry=reg)

    def test_side_effect_tool_is_blocked_during_planning(self) -> None:
        """
        规划阶段夹带的副作用工具**不执行**。

        断言 `ran == 0` 而不只是断言 `ok is False`：真正的危害是它跑了，
        只看返回值的话，一个「跑完再报错」的实现也能通过。
        """
        results, _ = _drive(
            self.agent, [ToolCall(id="1", name="mutate", arguments={})], planning=True
        )
        self.assertEqual(self.writer.ran, 0, "规划阶段绝不能真的执行副作用工具")
        self.assertFalse(results["1"].ok)

    def test_permissive_mode_does_not_let_it_through(self) -> None:
        """
        **放行权限模式下也挡得住**。

        这条是本模块的要害：权限管线在 PERMISSIVE 下根本不会问用户，
        若把 Plan Mode 的保证寄托在管线上，这个组合就直接失守。
        上面的 `_drive` 用的正是 PERMISSIVE + 恒真的 ask。
        """
        _drive(self.agent, [ToolCall(id="1", name="mutate", arguments={})], planning=True)
        self.assertEqual(self.writer.ran, 0)

    def test_readonly_tool_still_runs_during_planning(self) -> None:
        """只读调研照常放行——规划阶段本来就是用来调研的。"""
        results, _ = _drive(
            self.agent, [ToolCall(id="1", name="peek", arguments={})], planning=True
        )
        self.assertTrue(results["1"].ok)
        self.assertEqual(results["1"].output, "读到了")

    def test_execution_phase_lets_it_through(self) -> None:
        """
        **反证**：计划获批后（planning=False）同一个调用必须能执行。

        没有这条，一个「永远拒绝 mutate」的错误实现也会让上面三条全绿。
        """
        results, _ = _drive(
            self.agent, [ToolCall(id="1", name="mutate", arguments={})], planning=False
        )
        self.assertEqual(self.writer.ran, 1)
        self.assertTrue(results["1"].ok)

    def test_feedback_tells_the_model_what_to_do_next(self) -> None:
        """
        回灌文案要说清**现在是什么阶段**与**下一步该做什么**。

        只说「不允许」会让模型换个工具名再试一次——这是 `out_of_scope` 那边
        踩过的教训（实测有模型把 25 轮迭代全烧在重试上）。
        """
        results, _ = _drive(
            self.agent, [ToolCall(id="1", name="mutate", arguments={})], planning=True
        )
        out = results["1"].output
        self.assertIn("规划阶段", out)
        self.assertIn("present_plan", out, "必须指回提交计划这条正路")
        self.assertIn("mutate", out, "要点名是哪个工具被挡了")

    def test_loop_continues_after_block(self) -> None:
        """被挡不终止循环——同一轮里的其它调用照常处理。"""
        results, events = _drive(
            self.agent,
            [
                ToolCall(id="1", name="mutate", arguments={}),
                ToolCall(id="2", name="peek", arguments={}),
            ],
            planning=True,
        )
        self.assertFalse(results["1"].ok)
        self.assertTrue(results["2"].ok)
        kinds = [e.type for e in events]
        self.assertIn(AgentEventType.TOOL_RESULT, kinds)


class PlanStageTraceTest(unittest.TestCase):
    """被挡的调用要有自己的 trace outcome，不能混进 `out_of_scope`。"""

    def test_outcome_constant_is_distinct(self) -> None:
        from rhinecode.agent.loop import OUTCOME_OUT_OF_SCOPE

        self.assertNotEqual(OUTCOME_PLAN_BLOCKED, OUTCOME_OUT_OF_SCOPE)
        self.assertEqual(OUTCOME_PLAN_BLOCKED, "plan_blocked")

    def test_trace_records_plan_blocked(self) -> None:
        """读 trace 时必须能把「规划阶段挡下」与其它拒绝区分开。"""
        recorded: list[str] = []

        writer = _Writer()
        reg = ToolRegistry()
        reg.register(writer)
        agent = Agent(provider=None, registry=reg)
        # `**kw` 而不是逐个列出关键字参数：本用例只关心 `outcome`，
        # 而 `_trace_tool` 的可选参数会随章节增加（c14 加了 `cwd`）。
        # 写死签名的话，每加一个字段这条就以一个和判据毫不相干的
        # TypeError 挂掉——实测撞过一次。
        agent._trace_tool = lambda tc, res, outcome, **kw: recorded.append(outcome)

        _drive(agent, [ToolCall(id="1", name="mutate", arguments={})], planning=True)
        self.assertEqual(recorded, [OUTCOME_PLAN_BLOCKED])


if __name__ == "__main__":
    unittest.main()
