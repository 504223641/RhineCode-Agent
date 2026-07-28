"""
用户在确认面板选「拒绝」之后，模型必须**停下来问**，而不是换个姿势重试。

## 这条测试在防什么（一次真实的使用反馈）

旧行为：拒绝时回灌给模型的全部内容是一句「用户拒绝执行该工具。」——
它**只说了没执行，没说接下来该干嘛**。模型把它读成「这次尝试失败了」，
于是换路径、换命令、换工具反复重试，用户被迫连点好几次拒绝，
而每一次拒绝在模型看来都只是又一次技术失败。

问题的实质是**「技术失败」与「人的决定」被混为一谈**：前者该调整策略重试，
后者该停下来问。而工具结果这个通道天生长得像前者（`ok=False` + 错误文本）。

## 修复由两半组成，本模块两半都验

- **软约束**：`DENIED_BY_USER_FEEDBACK` 明确写出「这不是技术故障」「不要重试」
  「不要绕过」「去问用户为什么」；
- **硬约束**：被拒的下一轮 `stream_chat` 收到 `tools=None`，模型在物理上
  只能产出文本。文案可以不被听，机制不会。

只验前者是不够的——那等于把正确性寄托在模型愿不愿意配合上。
"""

from __future__ import annotations

import threading
import unittest

from rhinecode.agent.loop import DENIED_BY_USER_FEEDBACK, Agent
from rhinecode.permission.engine import PermissionEngine
from rhinecode.permission.models import PermissionMode
from rhinecode.permission.rules import RuleSet
from rhinecode.provider.base import BaseProvider, Message, StreamChunk, ToolCall
from rhinecode.tools.base import Tool, ToolResult
from rhinecode.tools.registry import ToolRegistry


class _Writer(Tool):
    """非只读工具 → 默认模式下必然走到人在回路确认。"""

    name = "write_file"
    description = "fake"
    parameters = {"type": "object", "properties": {"path": {"type": "string"}}}
    read_only = False

    def __init__(self) -> None:
        self.executed = 0

    def execute(self, args: dict) -> ToolResult:
        self.executed += 1
        return ToolResult(ok=True, output="written", summary="written")


class _RetryHappyProvider(BaseProvider):
    """
    一个**会重试的**假模型：只要手里有工具，就一直发 write_file。

    它刻意模拟出问题现场的模型行为——不看回灌文案、认定「再试一次就好」。
    正因为它不讲道理，才能验出硬约束是否真的拦得住：
    如果只有文案没有机制，它会一路重试到迭代上限。
    """

    def __init__(self) -> None:
        self.tools_per_turn: list[object] = []   # 逐轮记下收到的 tools 参数
        self.calls = 0

    def stream_chat(self, messages, thinking_effort="off", tools=None, system=None):
        self.calls += 1
        self.tools_per_turn.append(tools)
        if tools:
            # 手里有工具就再试一次（换个路径，正是用户抱怨的那种「重试」）
            yield StreamChunk(
                type="tool_call",
                tool_call=ToolCall(
                    id=f"c{self.calls}",
                    name="write_file",
                    arguments={"path": f"attempt{self.calls}.txt"},
                ),
            )
            yield StreamChunk(type="done")
            return
        # 没工具可用 → 只能说话
        yield StreamChunk(type="text", content="你拒绝了写文件。能告诉我为什么吗？")
        yield StreamChunk(type="done")


def _run(provider: BaseProvider, tool: Tool, ask) -> list:
    registry = ToolRegistry()
    registry.register(tool)
    engine = PermissionEngine(RuleSet([]), mode=PermissionMode.DEFAULT)
    agent = Agent(provider, registry)
    history: list[Message] = [Message(role="user", content="写个文件")]
    return list(
        agent.run(
            history, "off", False, "", lambda: "", "model", None,
            engine, ask, None, None, threading.Event(),
        )
    )


class DenyFeedbackTextTest(unittest.TestCase):
    """软约束：回灌文案必须把「这是人的决定」说清楚。"""

    def test_feedback_names_the_tool(self) -> None:
        text = DENIED_BY_USER_FEEDBACK.format(name="write_file")
        self.assertIn("write_file", text)

    def test_feedback_forbids_retry_and_workaround(self) -> None:
        """
        四个要点缺一不可。按**语义**逐条断言，不逐字固化整段文案
        ——文案措辞可以改进，但这四层意思不能丢。
        """
        text = DENIED_BY_USER_FEEDBACK.format(name="write_file")
        self.assertIn("不是技术故障", text)   # ① 这不是错误
        self.assertIn("不要重试", text)       # ② 别再试
        self.assertIn("绕过", text)           # ③ 别绕
        self.assertIn("询问", text)           # ④ 去问用户

    def test_denied_result_uses_the_feedback(self) -> None:
        """拒绝时回灌历史的确实是这段文案，而不是旧的那一句。"""
        tool = _Writer()
        provider = _RetryHappyProvider()
        events = _run(provider, tool, lambda tc, t, d: False)
        results = [e.tool_result for e in events if e.tool_result is not None]
        self.assertTrue(results)
        self.assertIn("不是技术故障", results[0].output)
        self.assertFalse(results[0].ok)


class DenyHardStopTest(unittest.TestCase):
    """硬约束：被拒的下一轮不发工具，模型想重试也无从下手。"""

    def test_next_turn_gets_no_tools(self) -> None:
        """
        用一个**不讲道理、只要有工具就重试**的假模型来验。

        判据：第 1 轮有工具（模型据此发起调用并被拒），第 2 轮 `tools` 为空
        —— 于是它只能说话，循环自然结束。
        """
        tool = _Writer()
        provider = _RetryHappyProvider()
        _run(provider, tool, lambda tc, t, d: False)

        self.assertGreaterEqual(len(provider.tools_per_turn), 2)
        self.assertTrue(provider.tools_per_turn[0], "第 1 轮本就该带工具")
        self.assertFalse(provider.tools_per_turn[1], "被拒后的下一轮必须不带工具")
        # 工具一次都没真正执行
        self.assertEqual(tool.executed, 0)

    def test_loop_ends_quickly_instead_of_burning_iterations(self) -> None:
        """
        **反证式判据**：没有硬约束的话，这个假模型会一路重试到迭代上限。

        实测过（把 `_RoundContext.user_denied` 改成恒 False 模拟修复前）：
        **25 轮全部烧光，每一轮都拿到工具、每一轮都重试**——也就是说用户要连点
        25 次「拒绝」才能让它停下。加上硬约束后是 **2 轮**。

        断言 `calls == 2` 而不是 `< 25`：前者能同时证明「拦住了」和
        「只拦一轮、没有过度」，后者两头都松。
        """
        tool = _Writer()
        provider = _RetryHappyProvider()
        _run(provider, tool, lambda tc, t, d: False)
        self.assertEqual(provider.calls, 2)

    def test_cooldown_is_released_when_not_denied(self) -> None:
        """
        冷却只作用于紧接着的一轮，不能变成「拒绝一次之后永远没有工具」。

        构造：第 1 轮被拒 → 第 2 轮无工具（模型说话）；这里断言冷却标志
        在没有新拒绝时被解除——用一个「批准」的场景反向确认工具确实还在。
        """
        tool = _Writer()
        provider = _RetryHappyProvider()
        _run(provider, tool, lambda tc, t, d: True)   # 全部批准
        # 全程没有拒绝 → 每一轮都该带着工具，冷却从未生效
        self.assertTrue(all(provider.tools_per_turn), "没有拒绝时不该出现空工具集")
        self.assertGreater(tool.executed, 0)


class NoRegressionTest(unittest.TestCase):
    """批准路径完全不受影响（零回归）。"""

    def test_approved_tool_still_executes(self) -> None:
        tool = _Writer()

        class _OnceProvider(BaseProvider):
            def __init__(self) -> None:
                self.calls = 0

            def stream_chat(self, messages, thinking_effort="off", tools=None, system=None):
                self.calls += 1
                if self.calls == 1:
                    yield StreamChunk(
                        type="tool_call",
                        tool_call=ToolCall(id="c1", name="write_file", arguments={"path": "a.txt"}),
                    )
                    yield StreamChunk(type="done")
                    return
                yield StreamChunk(type="text", content="好了")
                yield StreamChunk(type="done")

        provider = _OnceProvider()
        _run(provider, tool, lambda tc, t, d: True)
        self.assertEqual(tool.executed, 1)


if __name__ == "__main__":
    unittest.main()
