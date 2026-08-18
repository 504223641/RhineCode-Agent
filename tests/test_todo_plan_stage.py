"""
规划阶段不许列待办（todo-list 扩展，2026-08-18 推翻原 spec F8）。

## 这一条在治什么

用户实测的症状：**任务还没聊定，待办清单就先冒出来了。**

根因是把两个东西混成了一个：

| | 计划（`present_plan`） | 待办（`todo_write`） |
| --- | --- | --- |
| 回答 | 我们**要不要**这么干 | 我**干到哪了** |
| 时机 | 用户点头**之前** | 用户点头**之后** |
| 用户看到它的反应 | 「让我想想」 | 「已经在做了」 |

原设计给 `todo_write` 声明了 `plan_safe = True`，理由是「它零副作用」。
**那半句到今天仍然成立**——改的不是事实判断，是产品判断：
`plan_safe` 承诺的是「不产生副作用」，而它确实没有**外部**副作用；
但它有一个**界面**后果，清单一出现，用户看到的就是「它已经开始动手了」，
而 Plan Mode 的全部承诺就是「批准前不动手」。
零副作用与「此刻该不该出现」是两回事。

## 为什么这需要一个独立的测试文件

`tests/test_plan_stage_guard.py` 钉的是**守卫本身**（用两个假工具），
`tests/test_subagent_plan_stage.py` 钉的是**豁免条件是 `plan_safe`**。
两者都不会因为某个真实工具改了标志而变红——而标志被改回去恰恰是
本次改动最可能的退化方式（`plan_safe = True` 看起来人畜无害，
「它零副作用啊」这个理由永远说得通）。

因此本模块钉的是**这一个具体工具在两个阶段的行为必须相反**。

## ⚠ 「获批后照常可用」那条反证不可省

没有它的话，一个「永远拒绝 todo_write」的实现会让其余每条都通过，
而那是个真实的退化：用户批准了计划、开始动手了，却再也列不出待办
——症状与改之前正好相反，同样难查。
"""

from __future__ import annotations

import threading
import unittest

from rhinecode.agent.loop import Agent, _RoundContext
from rhinecode.permission import PermissionEngine, PermissionMode
from rhinecode.permission.rules import RuleSet
from rhinecode.provider.base import ToolCall
from rhinecode.tools.base import ToolResult
from rhinecode.tools.registry import ToolRegistry
from rhinecode.todo.store import TodoStore
from rhinecode.tools.todo_write import TodoWriteTool


_ROWS = [{"title": "改 a.py", "state": "in_progress"}]


def _drive(*, planning: bool):
    """
    跑一次 `_execute`，模型在这一轮调 `todo_write`。

    :param planning: 是否处于规划阶段（获批后为 False）
    :returns: `(工具结果, 清单条数)`

    ⚠ 权限模式取 **PERMISSIVE** 且 `ask` 恒真——最宽松的组合。
    这样一来「被挡下」只可能来自规划阶段守卫，不会是权限管线顺手拦的，
    否则这几条断言证明不了守卫真的在工作。
    """
    store = TodoStore()
    registry = ToolRegistry()
    registry.register(TodoWriteTool(store))
    agent = Agent(provider=None, registry=registry)

    results: dict[str, ToolResult] = {}
    list(
        agent._execute(
            [ToolCall(id="1", name="todo_write", arguments={"todos": _ROWS})],
            results,
            _RoundContext(),
            PermissionEngine(RuleSet([]), mode=PermissionMode.PERMISSIVE),
            lambda *a: True,
            None,
            None,
            threading.Event(),
            frozenset(),
            planning=planning,
        )
    )
    return results["1"], len(store.snapshot())


class PlanningStageTest(unittest.TestCase):
    """规划阶段：挡下，且清单一个字节都不变。"""

    def test_it_is_not_plan_safe(self) -> None:
        """
        ⚠ **最容易被改回去的一处。**

        `plan_safe = True` 看起来人畜无害，而「它零副作用啊」这个理由
        永远说得通——所以这条断言配着 `todo_write.py` 里那段说明一起看。
        """
        self.assertFalse(TodoWriteTool.plan_safe)

    def test_it_is_blocked_during_planning(self) -> None:
        """
        断言**清单没变**，而不只是「返回了失败」。

        真正的危害是它真的写进去了——只看返回值的话，一个
        「写完再报错」的实现照样通过（形态照抄 `test_plan_stage_guard.py`）。
        """
        result, count = _drive(planning=True)
        self.assertFalse(result.ok)
        self.assertEqual(count, 0, "规划阶段绝不能真的写进清单")

    def test_it_is_absent_from_the_planning_schemas(self) -> None:
        """
        规划阶段的工具清单里压根没有它——守卫是第二道，这是第一道。

        ⚠ 两道都要钉：只钉守卫的话，一个「schema 里还发给模型、
        全靠守卫兜」的实现也会绿，而那会让模型每轮都白试一次。
        """
        registry = ToolRegistry()
        registry.register(TodoWriteTool(TodoStore()))
        planning = [s["function"]["name"] for s in registry.planning_schemas()]
        every = [s["function"]["name"] for s in registry.schemas()]
        self.assertNotIn("todo_write", planning)
        self.assertIn("todo_write", every, "非规划阶段必须照常发给模型")


class ExecutionStageTest(unittest.TestCase):
    """获批之后：照常可用。"""

    def test_it_works_once_the_plan_is_approved(self) -> None:
        """
        ⚠ **反证，不可省。**

        没有它的话，一个「永远拒绝 todo_write」的实现会让其余每条都通过，
        而那是个真实的退化：用户批准了计划、开始动手了，却再也列不出待办
        ——症状与改之前正好相反，同样难查。
        """
        result, count = _drive(planning=False)
        self.assertTrue(result.ok, "获批后必须能用")
        self.assertEqual(count, 1)


class BlockedFeedbackTest(unittest.TestCase):
    """
    被挡下时回给模型的话。

    ## ⚠ 通用文案对这个工具是**错的**

    守卫的通用文案说「这个工具会产生副作用」——而 `todo_write` 没有。
    **一句不准确的拒绝理由会把模型推去找绕过的办法**（「那我换个没副作用
    的说法」），这与 `out_of_scope` 那边「只说不允许会让它换个工具名再试」
    是同一条教训。

    手法对齐 Codex：它的 `update_plan` 在 Plan 模式下回的是
    「update_plan is a TODO/checklist tool and is not allowed in Plan mode」
    ——那句话在**教模型区分两个概念**，不只是宣布一条禁令。
    """

    def setUp(self) -> None:
        result, _ = _drive(planning=True)
        self.output = result.output

    def test_it_explains_what_the_tool_is_for(self) -> None:
        """必须点破「待办是执行期的东西」这个概念差异。"""
        self.assertIn("执行阶段", self.output)
        self.assertIn("干到哪了", self.output)

    def test_it_points_back_to_present_plan(self) -> None:
        """要指回正路，并说明获批之后就能用——否则模型会以为这工具坏了。"""
        self.assertIn("present_plan", self.output)
        self.assertIn("批准", self.output)

    def test_it_does_not_claim_a_side_effect(self) -> None:
        """
        ⚠ **反证：不许说它有副作用。**

        这是本类存在的全部理由。没有这一条的话，把 `plan_blocked_hint`
        删掉之后其余断言里只有「present_plan」还能过，而模型收到的会是
        一句关于副作用的假话——而假话不会让任何测试变红。
        """
        self.assertNotIn("会产生副作用", self.output)

    def test_the_hint_is_the_tool_s_own_words(self) -> None:
        """
        自述来自工具自己声明的 `plan_blocked_hint`，不是循环里硬编码的。

        硬编码会让 `agent/loop.py` 认识具体工具的名字——那是本项目
        反复避免的形态（同 `system_serial` / `classifier_scope` 的理由）。
        """
        self.assertIn(TodoWriteTool.plan_blocked_hint, self.output)


if __name__ == "__main__":
    unittest.main()
