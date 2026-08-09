"""
护栏：`system_serial=True` 的工具也要留下一条判定记录。

## 背景（读这组用例之前先看这里）

`agent/loop.py` 的决策预扫里有一条分支：`if tool.system_serial:` **直接造一个
ALLOW 决策塞进串行桶，不调 `engine.decide`**。受影响的有七个工具——
`run_agent`、`load_skill`，以及 C15 的五个协作工具。

这个分支本身是刻意的（它们不读写文件、不执行命令，没有可映射的
Bash/Read/Edit/Write 语义），但它带来一个观测缺口：**记录里没有任何
`permission_decision` 事件**，时间线上表现为「一条 tool_execute 凭空出现，
前面什么判定都没有」。

后果两层：
① 写不了「每次工具执行前都有一条判定」这种通用护栏（对七个工具恒假）；
② 已知项 #18 记的那个 bypass（`deny: send_message` 配了也不生效）
   **无法从 trace 复核**——只能靠「少了一条」反推，而缺失永远是最弱的证据。

现在如实记一条，并带 `bypassed_engine=True` 标记。

## ⚠ 这组用例**不**验「deny 规则应该生效」

那是安全边界变更（已知项 #18），要单独立项评审。这里验的只有一件事：
**绕过这件事在记录上是可见的**。两者别混——把这组用例改成断言 deny 生效，
等于在没有评审的情况下悄悄改了安全语义。
"""

import json
import tempfile
import threading
import unittest
from pathlib import Path

from rhinecode.agent.loop import Agent, RunOptions
from rhinecode.permission.engine import PermissionEngine
from rhinecode.permission.models import PermissionMode
from rhinecode.permission.rules import RuleSet
from rhinecode.provider.base import Message, StreamChunk, ToolCall
from rhinecode.tools.base import Tool, ToolResult
from rhinecode.tools.registry import ToolRegistry
from rhinecode.trace import TraceEventType, TraceRecorder


class _SystemSerialTool(Tool):
    """一个最小的系统级工具，行为对齐 `run_agent` / `send_message`。"""

    name = "fake_send_message"
    description = "测试用系统级工具"
    parameters = {"type": "object", "properties": {}}
    read_only = False
    system_serial = True

    def execute(self, args: dict, **kwargs) -> ToolResult:
        return ToolResult(ok=True, output="已送达", summary="已送达")


class _OrdinaryTool(Tool):
    """对照组：普通工具，必须照常走引擎。"""

    name = "fake_read"
    description = "测试用普通只读工具"
    parameters = {"type": "object", "properties": {}}
    read_only = True

    def execute(self, args: dict, **kwargs) -> ToolResult:
        return ToolResult(ok=True, output="内容", summary="读了")


class _ScriptedProvider:
    """按剧本出块的假模型：先调一次工具，再收工。"""

    def __init__(self, tool_name: str):
        self._tool_name = tool_name
        self._round = 0

    def stream_chat(self, messages, thinking_effort="off", tools=None, system=None):
        self._round += 1
        if self._round == 1:
            yield StreamChunk(
                type="tool_call",
                tool_call=ToolCall(id="c1", name=self._tool_name, arguments={}),
            )
        else:
            yield StreamChunk(type="text", content="做完了")
        yield StreamChunk(type="done")


class SystemSerialDecisionRecordedTest(unittest.TestCase):
    def _run(self, tool: Tool) -> list[dict]:
        """跑一轮循环，返回落盘的全部记录。"""
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "t.jsonl"
            rec = TraceRecorder(path)

            registry = ToolRegistry()
            registry.register(tool)
            engine = PermissionEngine(RuleSet([]), mode=PermissionMode.PERMISSIVE)

            agent = Agent(_ScriptedProvider(tool.name), registry, recorder=rec)
            list(
                agent.run(
                    [Message(role="user", content="去发条消息")],
                    "off",
                    False,
                    "",
                    lambda: "",
                    "model",
                    None,
                    engine,
                    lambda *a: True,
                    None,
                    lambda _p: True,
                    threading.Event(),
                    options=RunOptions(cwd=Path(d)),
                )
            )
            rec.close()
            return [
                json.loads(line)
                for line in path.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]

    def _decisions(self, records: list[dict]) -> list[dict]:
        return [
            r for r in records if r["type"] == TraceEventType.PERMISSION_DECISION.value
        ]

    def test_system_serial_tool_produces_a_decision_record(self) -> None:
        """核心判据：系统级工具不再「无声执行」。"""
        records = self._run(_SystemSerialTool())
        decisions = self._decisions(records)

        self.assertEqual(
            len(decisions),
            1,
            "system_serial 工具也必须留下判定记录，否则 tool_execute 前面是个空洞",
        )
        d = decisions[0]
        self.assertEqual(d["tool"], "fake_send_message")
        self.assertEqual(d["decision"], "allow")
        self.assertIs(d["bypassed_engine"], True)
        # reason 必须说清「没经过引擎」——读记录的人据此才知道
        # ③层 deny 规则对它不生效（已知项 #18）
        self.assertIn("未经权限引擎", d["reason"])

    def test_every_tool_execution_is_preceded_by_a_decision(self) -> None:
        """
        通用不变量：**每一条 `tool_execute` 前面都有一条同 id 的判定**。

        这正是改造前写不出来的那条护栏——七个系统级工具会让它恒假。
        它是本次改动最实际的收益：将来任何新分支若忘了埋判定，这条当场红。
        """
        for tool in (_SystemSerialTool(), _OrdinaryTool()):
            with self.subTest(tool=tool.name):
                records = self._run(tool)
                executed = [
                    r
                    for r in records
                    if r["type"] == TraceEventType.TOOL_EXECUTE.value
                ]
                decided = {
                    r.get("tool_call_id") for r in self._decisions(records)
                }
                self.assertTrue(executed, "剧本应当产出一次工具执行")
                for e in executed:
                    self.assertIn(
                        e["tool_call_id"],
                        decided,
                        f"{e['tool']} 执行了却没有对应的 permission_decision",
                    )

    def test_ordinary_tool_is_not_marked_as_bypassed(self) -> None:
        """
        **反证**：普通工具必须 `bypassed_engine=False`。

        没有这条的话，把标记写成常量 True 也能让上面两条全绿——
        而那等于给每条判定都盖上「绕过引擎」的戳，标记随即失去意义。
        """
        records = self._run(_OrdinaryTool())
        decisions = self._decisions(records)
        self.assertEqual(len(decisions), 1)
        self.assertIs(decisions[0]["bypassed_engine"], False)
        self.assertNotEqual(decisions[0]["kind"], "system_serial")


if __name__ == "__main__":
    unittest.main()
