"""
护栏：`system_serial=True` 的工具也要留下一条判定记录，且 ASK 被降级这件事可见。

## 背景（读这组用例之前先看这里）

`agent/loop.py` 的决策预扫里有一条分支：`if tool.system_serial:`。
受影响的有七个工具——`run_agent`、`load_skill`，以及 C15 的五个协作工具。

这条分支**曾经直接造一个 ALLOW 决策塞进串行桶、不调 `engine.decide`**，
于是 `permissions.yaml` 里的 deny 规则一条都不生效，而且记录里连一个
`permission_decision` 事件都没有——时间线上表现为「一条 tool_execute
凭空出现，前面什么判定都没有」。

那个绕过已于 perm-system-serial-bypass 修掉：它们现在照常过引擎，
只是**对第④层（权限档兜底）整层免疫**——④判 ASK 或 DENY 都按 ALLOW 处理
（保住「不弹确认面板」这条既有性质，那是 `system_serial` 存在的理由之一；
而 DENY 也免疫是 `perm-system-serial-mode-immune` 一轮加的，理由见
`tests/test_team_tools.py::SystemSerialPermissionTest.test_strict_mode_does_not_disable_it`）。

## 这组用例现在验什么

① **每一条 `tool_execute` 前面都有一条同 id 的判定**——通用不变量，
   改造前对七个工具恒假、整条写不出来；
② **④层结论被降级这件事在记录上可见**（`mode_downgraded=True`）。不记的话
   时间线上只剩 `allow（④模式）`，读的人会以为用户切到了放行档——
   观测设施撒谎且不报错，而缺省档下这才是那七个工具的常态。

「deny 规则确实生效」在 `tests/test_team_tools.py::SystemSerialPermissionTest`
里验（那里跑得到工具是否真的执行、面板弹没弹）。
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
    def _run(self, tool: Tool, rules=None) -> list[dict]:
        """
        跑一轮循环，返回落盘的全部记录。

        ⚠ 用**缺省档**（不是放行档）。放行档下④层直接判 ALLOW，
        `mode_downgraded` 恒为假——那样就验不到本组要验的东西了，
        而缺省档才是那七个工具的实际常态。
        """
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "t.jsonl"
            rec = TraceRecorder(path)

            registry = ToolRegistry()
            registry.register(tool)
            engine = PermissionEngine(
                RuleSet(rules or []), mode=PermissionMode.DEFAULT
            )

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
        """核心判据：系统级工具不再「无声执行」，且 ASK 降级可见。"""
        records = self._run(_SystemSerialTool())
        decisions = self._decisions(records)

        self.assertEqual(
            len(decisions),
            1,
            "system_serial 工具也必须留下判定记录，否则 tool_execute 前面是个空洞",
        )
        d = decisions[0]
        self.assertEqual(d["tool"], "fake_send_message")
        # 缺省档下④模式层判 ASK，被降级为放行执行。
        self.assertEqual(d["decision"], "allow")
        self.assertIs(d["mode_downgraded"], True)
        # reason 要说清「按放行处理、不弹面板」——只写 `allow（④模式）` 的话，
        # 读记录的人会以为用户切到了放行档。
        self.assertIn("按放行处理", d["reason"])

    def test_deny_rule_is_recorded_and_stops_execution(self) -> None:
        """
        `deny` 规则现在对系统级工具生效，且记录里看得见。

        这条同时是「绕过已被修掉」的物证：改造前 `engine.decide` 压根没被调用，
        这里必然是 allow + 一次 tool_execute。
        """
        from rhinecode.permission.models import Rule

        records = self._run(
            _SystemSerialTool(),
            rules=[Rule("deny", "fake_send_message", "", "test")],
        )
        decisions = self._decisions(records)
        self.assertEqual(len(decisions), 1)
        self.assertEqual(decisions[0]["decision"], "deny")
        self.assertEqual(decisions[0]["layer"], "rule")
        # ③层的 DENY 不降级——免疫只覆盖④层。
        self.assertIs(decisions[0]["mode_downgraded"], False)
        executed = [
            r for r in records if r["type"] == TraceEventType.TOOL_EXECUTE.value
        ]
        self.assertEqual(
            [r["outcome"] for r in executed],
            ["denied_by_permission"],
            "被 deny 拦下的调用不该真的执行",
        )

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

    def test_ordinary_tool_is_not_marked_as_downgraded(self) -> None:
        """
        **反证**：普通工具必须 `mode_downgraded=False`。

        没有这条的话，把标记写成常量 True 也能让上面几条全绿——
        而那等于给每条判定都盖上「④层已降级」的戳，标记随即失去意义。
        """
        records = self._run(_OrdinaryTool())
        decisions = self._decisions(records)
        self.assertEqual(len(decisions), 1)
        self.assertIs(decisions[0]["mode_downgraded"], False)


if __name__ == "__main__":
    unittest.main()
