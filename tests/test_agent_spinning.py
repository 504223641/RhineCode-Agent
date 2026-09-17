"""
护栏：**模型原地打转时循环要停下来，而正常的排查节奏不许被误伤。**

## 这组用例在防什么

原先 Agent Loop 只有一个写死的 25 轮上限兜底，而那个数字同时扮演两个方向
相反的角色：模型卡住时嫌它太多（用户白等白花钱），任务本身复杂时嫌它太少
（真实 trace 实录：第 25 轮它刚定位到性能瓶颈，正要动手就被掐了）。

拆开之后，「卡住了」由 `agent/spinning.py` 判——纯代码、零成本、每轮都在看。

## 两个方向的判据都要钉，且**误伤那侧更要紧**

漏判的代价是多烧几轮（用户看得见、会来喊停）；**误伤的代价是一次正常的任务
被无理由掐断**，而用户完全看不出原因——他只会觉得这个工具不靠谱。
所以下面「不该触发」的用例比「该触发」的多，且每一条都来自真实样本。
"""

from __future__ import annotations

import threading
import unittest

from rhinecode.agent.events import AgentEventType, StopReason
from rhinecode.agent.loop import Agent
from rhinecode.agent.spinning import (
    REPEAT_LIMIT,
    SpinDetector,
    fingerprint,
    render_spin_message,
)
from rhinecode.permission.engine import PermissionEngine
from rhinecode.permission.models import PermissionMode
from rhinecode.permission.rules import RuleSet
from rhinecode.provider.base import BaseProvider, Message, StreamChunk, ToolCall
from rhinecode.tools.base import Tool, ToolResult
from rhinecode.tools.registry import ToolRegistry


# =========================================================================== #
# 一、纯逻辑：指纹与计数
# =========================================================================== #


class FingerprintTest(unittest.TestCase):
    """指纹要认得出「同一次调用」，也要分得开「不同的调用」。"""

    def test_key_order_does_not_change_the_fingerprint(self) -> None:
        """
        模型两次生成同一个调用时 JSON 键序可能不同，那仍是同一次调用。

        按原始文本比会让一半的重复溜过去——这条钉的就是「必须规范化」。
        """
        a = fingerprint("run_command", {"command": "ls", "timeout": 5}, "out")
        b = fingerprint("run_command", {"timeout": 5, "command": "ls"}, "out")
        self.assertEqual(a, b)

    def test_different_output_is_a_different_fingerprint(self) -> None:
        """
        **最要紧的一条**：结果参与指纹。

        同一条命令第二次跑出不同结果，那是**新信息**不是打转——真实 trace 里
        模型连跑三次性能基准（0.91 → 0.89 → 0.040）正是这个形态。
        """
        a = fingerprint("run_command", {"command": "pytest"}, "1 failed")
        b = fingerprint("run_command", {"command": "pytest"}, "0 failed")
        self.assertNotEqual(a, b)

    def test_different_arguments_is_a_different_fingerprint(self) -> None:
        a = fingerprint("read_file", {"path": "a.py"}, "x")
        b = fingerprint("read_file", {"path": "b.py"}, "x")
        self.assertNotEqual(a, b)

    def test_same_payload_under_different_tools_does_not_collide(self) -> None:
        """
        工具名必须参与指纹，且分隔符不能让它与参数串到一起。

        没有分隔符的话两次不同的调用会拼成同一个字符串，撞了不报错，
        只是凭空多算一次重复——而那是**误伤**那一侧。

        ⚠ 样本刻意用数字：参数走 `json.dumps`，字符串会被加上引号，于是
        `("ab","c")` 与 `("a","bc")` 这种「看起来会撞」的样本其实撞不上，
        拿它当判据**验不到任何东西**（第一版就是这么写的，变异实测当场漏掉）。
        数字没有引号，`("a", 1, "23")` 与 `("a", 12, "3")` 才是真的会撞。
        """
        self.assertNotEqual(fingerprint("a", 1, "23"), fingerprint("a", 12, "3"))
        self.assertNotEqual(fingerprint("a1", 2, "3"), fingerprint("a", 12, "3"))

    def test_unserializable_arguments_do_not_raise(self) -> None:
        """
        ⚠ 参数里有不可序列化的东西时**宁可漏判，也绝不抛异常**。

        本函数跑在循环的收尾处，抛出去会让一次本来正常的运行整个炸掉——
        一个止损机制把被它保护的东西弄挂了，那比不做还糟。
        """
        self.assertIsInstance(fingerprint("t", {"f": object()}, "o"), str)
        self.assertIsInstance(fingerprint("t", None, "o"), str)


class DetectorCountingTest(unittest.TestCase):
    """计数与阈值。"""

    def test_it_fires_on_the_third_identical_action(self) -> None:
        """阈值是 3（对齐 Codex 的 blocked audit：同一条件连续三轮才算数）。"""
        det = SpinDetector()
        self.assertIsNone(det.record_round([("run_command", {"c": "x"}, "boom")]))
        self.assertIsNone(det.record_round([("run_command", {"c": "x"}, "boom")]))
        hit = det.record_round([("run_command", {"c": "x"}, "boom")])
        self.assertEqual(hit, ("run_command", 3))

    def test_the_limit_is_three(self) -> None:
        """
        ⚠ **护栏自身的护栏**：阈值写死在常量里，改它要在这里改。

        2 太急——「做了、失败了、原样再试一次」是人也会做的事，第二次往往就
        成了；4 以上则要多烧一整轮。改这个数之前先读 `spinning.py` 的说明。
        """
        self.assertEqual(REPEAT_LIMIT, 3)

    def test_repeats_do_not_have_to_be_consecutive(self) -> None:
        """
        ⚠ **不要求「连续」**，这是刻意的。

        真实的死循环常常是 A→B→A→B 的摆动（改一处、跑一次、发现不对、改回去、
        再跑一次），连续口径一次都抓不到。
        """
        det = SpinDetector()
        a = ("edit_file", {"path": "g.py", "to": "True"}, "ok")
        b = ("edit_file", {"path": "g.py", "to": "FREEDOM"}, "ok")
        self.assertIsNone(det.record_round([a]))
        self.assertIsNone(det.record_round([b]))
        self.assertIsNone(det.record_round([a]))
        self.assertIsNone(det.record_round([b]))
        self.assertEqual(det.record_round([a]), ("edit_file", 3))

    def test_a_stuck_call_bundled_with_a_fresh_one_is_still_caught(self) -> None:
        """
        ⚠ **一轮里的多个调用要逐个记，不能把整轮压成一个指纹。**

        模型完全可能每轮都把那个卡住的调用和一个新调用放在一起发——整轮指纹
        于是次次不同，而那个卡住的调用照样在原地。

        ⚠ **卡住的那个刻意排在第二位。** 排第一位时，一个只看
        `calls[0]`（或任何「只记头一个」的写法）的实现**照样全绿**——
        变异实测确认过，第一版就是这么写的、当场漏掉了那次变异。
        """
        det = SpinDetector()
        stuck = ("run_command", {"c": "pytest"}, "boom")
        for i in range(2):
            self.assertIsNone(
                det.record_round([("read_file", {"path": f"{i}.py"}, f"{i}"), stuck])
            )
        hit = det.record_round([("read_file", {"path": "z.py"}, "z"), stuck])
        self.assertEqual(hit, ("run_command", 3))

    def test_the_hit_names_the_tool_that_actually_repeated(self) -> None:
        """
        返回的工具名必须是**真正重复的那个**，不是本轮的头一个。

        搞错了不报错，只是给用户的那句话点了个无辜的工具的名——而那句话的
        全部价值就在于告诉他「卡在哪」。
        """
        det = SpinDetector()
        stuck = ("run_command", {"c": "pytest"}, "boom")
        for i in range(3):
            hit = det.record_round(
                [("read_file", {"path": f"{i}.py"}, f"{i}"), stuck]
            )
        self.assertEqual(hit, ("run_command", 3))


class NotSpinningTest(unittest.TestCase):
    """
    **不该触发的那一侧。** 每条都取自真实 trace（20260918-002611 第二轮）。

    误伤的代价比漏判大得多：漏判只是多烧几轮，用户看得见、会来喊停；
    误伤是一次正常的任务被无理由掐断，而用户完全看不出原因。
    """

    def test_measure_fix_remeasure_is_not_spinning(self) -> None:
        """
        真实样本：连跑三次性能基准，参数各不相同、结果从 0.91 到 0.89 到 0.040。

        那是标准的「量 → 改 → 复测 → 换个角度再量」，是排查该有的样子。
        """
        det = SpinDetector()
        rounds = [
            ("run_command", {"c": "bench --full"}, "elapsed 0.91 sims 24"),
            ("run_command", {"c": "bench"}, "elapsed 0.89 sims 23"),
            ("run_command", {"c": "bench --micro"}, "one simulate 0.040s"),
        ]
        for action in rounds:
            self.assertIsNone(det.record_round([action]))

    def test_the_same_command_with_a_changed_result_is_not_spinning(self) -> None:
        """
        改了代码再跑同一条测试命令——参数一模一样，但结果变了。

        这是所有「改完验证」的通用形态，误伤它等于让工具没法做任何调试。
        """
        det = SpinDetector()
        cmd = {"c": "python -m pytest"}
        self.assertIsNone(det.record_round([("run_command", cmd, "3 failed")]))
        self.assertIsNone(det.record_round([("run_command", cmd, "1 failed")]))
        self.assertIsNone(det.record_round([("run_command", cmd, "0 failed")]))

    def test_paging_through_one_file_is_not_spinning(self) -> None:
        """分页读同一个文件：工具与文件都一样，起始行不同、内容也不同。"""
        det = SpinDetector()
        for start in (1, 130, 369):
            self.assertIsNone(
                det.record_round(
                    [("read_file", {"path": "m.py", "start_line": start}, f"...{start}")]
                )
            )

    def test_two_repeats_are_not_enough(self) -> None:
        """
        ⚠ **反证：第二次不许触发。**

        「做了、失败了、原样再试一次」是人也会做的事，而且第二次往往就成了。
        把阈值降到 2 会让这条红——那正是它存在的理由。
        """
        det = SpinDetector()
        action = ("run_command", {"c": "x"}, "boom")
        self.assertIsNone(det.record_round([action]))
        self.assertIsNone(det.record_round([action]))


class MessageTest(unittest.TestCase):
    """给用户看的那句话。"""

    def test_it_names_what_repeated_and_how_many_times(self) -> None:
        """
        ⚠ 只说「已停止」是不够的（同已知项 #21 那条「拒绝的文案要说清为什么」）。

        用户接下来要判断的是「它是真卡了，还是我该换个说法再来一次」，
        而那只有知道是**哪个调用**在重复才判断得了。
        """
        text = render_spin_message("run_command", 3)
        self.assertIn("run_command", text)
        self.assertIn("3", text)

    def test_it_tells_the_user_what_to_do_next(self) -> None:
        """还要给出路，否则用户只知道失败了、不知道怎么办。"""
        text = render_spin_message("read_file", 3)
        self.assertIn("换个说法", text)


# =========================================================================== #
# 二、接线：循环真的会停，且停在正确的原因上
# =========================================================================== #


class _Echo(Tool):
    """一个结果恒定的只读工具——喂给它同样的参数就拿到同样的输出。"""

    name = "read_file"
    description = "fake"
    parameters = {"type": "object", "properties": {"path": {"type": "string"}}}
    read_only = True

    def __init__(self) -> None:
        self.executed = 0

    def execute(self, args: dict) -> ToolResult:
        self.executed += 1
        return ToolResult(ok=True, output=f"内容 {args.get('path')}", summary="读了")


class _StuckProvider(BaseProvider):
    """一个卡住的假模型：永远发同一个调用，参数一字不改。"""

    def __init__(self) -> None:
        self.calls = 0

    def stream_chat(self, messages, thinking_effort="off", tools=None, system=None):
        self.calls += 1
        yield StreamChunk(
            type="tool_call",
            tool_call=ToolCall(id=f"c{self.calls}", name="read_file",
                               arguments={"path": "a.py"}),
        )
        yield StreamChunk(type="done")


class _MovingProvider(BaseProvider):
    """一个在正常干活的假模型：每轮读不同的文件，第五轮收工。"""

    def __init__(self) -> None:
        self.calls = 0

    def stream_chat(self, messages, thinking_effort="off", tools=None, system=None):
        self.calls += 1
        if self.calls > 5:
            yield StreamChunk(type="text", content="做完了")
            yield StreamChunk(type="done")
            return
        yield StreamChunk(
            type="tool_call",
            tool_call=ToolCall(id=f"c{self.calls}", name="read_file",
                               arguments={"path": f"f{self.calls}.py"}),
        )
        yield StreamChunk(type="done")


def _run(provider: BaseProvider) -> list:
    """把一次运行跑完，返回全部事件。放行档 → 不弹任何面板。"""
    registry = ToolRegistry()
    registry.register(_Echo())
    engine = PermissionEngine(RuleSet([]), mode=PermissionMode.PERMISSIVE)
    agent = Agent(provider, registry)
    history: list[Message] = [Message(role="user", content="看下这个项目")]
    return list(
        agent.run(
            history, "off", False, "", lambda: "", "model", None,
            engine, lambda *a, **k: True, None, None, threading.Event(),
        )
    )


class LoopStopsOnSpinTest(unittest.TestCase):
    """循环接线。"""

    def test_a_stuck_model_stops_early_with_the_spinning_reason(self) -> None:
        """
        **本文件最核心的一条**：停的原因必须是「打转」而不是「迭代上限」。

        两者都会让循环结束，所以只断言「停了」是验不到东西的——旧代码在第 25
        轮也会停。判据必须是 `stop_reason` 本身，而且要顺带钉住**它停得早**。
        """
        provider = _StuckProvider()
        events = _run(provider)
        finished = [e for e in events if e.type is AgentEventType.FINISHED]
        self.assertEqual(len(finished), 1)
        self.assertIs(finished[0].stop_reason, StopReason.SPINNING)
        self.assertLessEqual(provider.calls, REPEAT_LIMIT + 1,
                             "应该在第三次重复就停，不该跑到迭代上限")

    def test_the_message_reaches_the_user(self) -> None:
        """停止事件要带上那句说明，否则用户只看到「停了」。"""
        finished = [e for e in _run(_StuckProvider())
                    if e.type is AgentEventType.FINISHED][0]
        self.assertIn("read_file", finished.message or "")
        self.assertIn("原地打转", finished.message or "")

    def test_a_working_model_is_not_interrupted(self) -> None:
        """
        ⚠ **反证：正常干活的模型不许被打断。**

        一个「每轮都判打转」的实现会让上面那条照样全绿，而它把整个产品废了。
        """
        provider = _MovingProvider()
        finished = [e for e in _run(provider)
                    if e.type is AgentEventType.FINISHED][0]
        self.assertIs(finished.stop_reason, StopReason.COMPLETED)
        self.assertEqual(provider.calls, 6)

    def test_the_detector_is_per_run(self) -> None:
        """
        ⚠ 计数的作用域是**一次运行**，不是整个会话。

        跨会话累计会让用户第二次问同一件事时凭空少了几次额度，而那两次之间
        他可能已经改了代码——「同样的命令给出同样的结果」在那时是新信息。
        """
        agent_events_1 = _run(_StuckProvider())
        agent_events_2 = _run(_StuckProvider())
        for events in (agent_events_1, agent_events_2):
            calls = [e for e in events if e.type is AgentEventType.TOOL_RESULT]
            self.assertEqual(len(calls), REPEAT_LIMIT,
                             "每次运行都该有完整的三次额度")


class StopReasonIsWiredEverywhereTest(unittest.TestCase):
    """
    ⚠ 新增 `StopReason` 要同步四处，漏改**不报错**。

    三张文案表各自缺一项时，用户看到的分别是：界面上一片空白 / Skill 回流说
    「未产出结果」却不说为什么 / 子 Agent 的失败说明退回泛泛的兜底。
    """

    def test_the_tui_has_a_line_for_it(self) -> None:
        from rhinecode.tui.app import RhineApp

        _level, text = RhineApp._finish_line(StopReason.SPINNING, "")
        self.assertTrue(text, "界面缺了这一条 → 用户什么都看不到")

    def test_the_skill_table_has_it(self) -> None:
        from rhinecode.conversation import _ISOLATED_FAILURE_TEXT

        self.assertIn(StopReason.SPINNING, _ISOLATED_FAILURE_TEXT)

    def test_the_subagent_table_has_it(self) -> None:
        from rhinecode.subagents.runner import _FAILURE_TEXT

        self.assertIn(StopReason.SPINNING, _FAILURE_TEXT)

    def test_every_stop_reason_is_covered_by_all_three(self) -> None:
        """
        ⚠ **护栏自身的护栏**：遍历枚举，下一个新增的原因自动被覆盖。

        只钉住 `SPINNING` 的话，这条成对维护点保护的只是这一次；
        遍历之后它保护的是这一类。
        """
        from rhinecode.conversation import _ISOLATED_FAILURE_TEXT
        from rhinecode.subagents.runner import _FAILURE_TEXT
        from rhinecode.tui.app import RhineApp

        # COMPLETED 刻意不进任何一张表：它是「成功」，不需要失败文案，
        # 界面上也刻意不出声（不打扰用户）。
        for reason in StopReason:
            if reason is StopReason.COMPLETED:
                continue
            with self.subTest(reason=reason):
                self.assertIn(reason, _ISOLATED_FAILURE_TEXT)
                self.assertIn(reason, _FAILURE_TEXT)
                _level, text = RhineApp._finish_line(reason, "")
                self.assertTrue(text)


if __name__ == "__main__":
    unittest.main()
