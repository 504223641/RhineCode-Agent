"""
②″保护路径的**接线**护栏（protected-paths 扩展）。

判定逻辑本身在 `test_perm_protected.py`；本文件只管「判定出来之后，那个结论有没有
被正确地送到该去的四个地方」：确认面板、协调层的放行结算、行为记录埋点、阅读器摘要。

## 为什么接线要单独一组

本扩展的判定层再对，只要有一处接线漏了，用户看到的就是另一回事：

| 漏在哪 | 用户看到什么 |
| --- | --- |
| 面板仍给「永久放行」 | 点了它，下次还是弹——**一个明确的用户决定看起来失效了** |
| 协调层仍写③层规则 | 同上（那条规则永远不会被求值），而且**悄悄改了配置文件** |
| 埋点漏字段 | 时间线上只剩 `allow（④模式）`，排查的人以为用户切到了放行档 |
| 阅读器漏摘要 | 字段记了等于白记——只有 `--seq` 展开才发现「原来早就记了」 |

四处**没有一处会报错**。
"""

from __future__ import annotations

import json
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace

from textual.app import App, ComposeResult

from rhinecode.agent.events import ConfirmDecision
from rhinecode.agent.loop import Agent, RunOptions
from rhinecode.config import Config
from rhinecode.conversation import ConversationManager
from rhinecode.permission.engine import PermissionEngine
from rhinecode.permission.models import (
    Decision,
    DecisionResult,
    Layer,
    PermissionMode,
)
from rhinecode.permission.rules import RuleSet
from rhinecode.provider.base import Message, StreamChunk, ToolCall
from rhinecode.tools.base import Tool, ToolResult
from rhinecode.tools.registry import ToolRegistry
from rhinecode.trace import TraceEventType, TraceRecorder
from rhinecode.trace.reader import _LAYER_NAMES, _s_permission_decision
from rhinecode.tui.widgets import ConfirmPanel


class _Harness(App):
    def compose(self) -> ComposeResult:
        yield ConfirmPanel()


def _decision(layer: str):
    """造一个决策结果（只带面板会读的那几个字段）。"""
    return SimpleNamespace(
        reason="保护路径：写入 .rhinecode/hooks.yaml 会改变 RhineCode 以后的行为",
        kind="write_path",
        host="",
        layer=SimpleNamespace(value=layer),
    )


def _ids(panel: ConfirmPanel) -> list[str]:
    return [o.id for o in panel._options if o.id is not None]


class PanelTest(unittest.IsolatedAsyncioTestCase):
    """AC11：保护路径的面板少一项，别的场景一项不少。"""

    async def _show(self, layer: str) -> ConfirmPanel:
        app = _Harness()
        async with app.run_test():
            panel = app.query_one(ConfirmPanel)
            panel.show_for(
                ToolCall(
                    id="c1",
                    name="write_file",
                    arguments={"path": ".rhinecode/hooks.yaml", "content": "x"},
                ),
                None,
                _decision(layer),
            )
            return panel

    async def test_protected_panel_has_no_permanent_option(self) -> None:
        """
        「永久放行」写的是一条③层 allow 规则，而本层的升级效力不被③层消解
        ——那条规则永远不会被求值。留着它就是一个点了没用的按钮。
        """
        panel = await self._show("protected")
        self.assertEqual(_ids(panel), ["yes", "yes_session", "no"])

    async def test_ordinary_panel_still_has_four_options(self) -> None:
        """
        **反证**：别的场景一项都不能少。

        没有这条的话，把三选项分支写成无条件生效（比如判据写反）会全绿通过，
        而用户从此再也点不到「永久放行」——那是个每天都在用的选项。
        """
        panel = await self._show("mode")
        self.assertEqual(_ids(panel), ["yes", "yes_session", "yes_permanent", "no"])

    async def test_session_option_says_it_does_not_write_config(self) -> None:
        """
        用户对「本会话放行」的既有心智是「登记一条会话规则」，这里换了机制
        （走引擎里②″层自己的内存豁免集合），说明文字必须说破。
        """
        panel = await self._show("protected")
        texts = [str(o.prompt) for o in panel._options]
        session_line = next(t for t in texts if "本会话放行" in t)
        self.assertIn("不写入配置", session_line)

    async def test_protected_reason_is_shown_in_the_header(self) -> None:
        """
        表头只在「原因有分辨力」时显示它（`layer != "mode"`）。保护路径必须在内——
        那句「为什么这个文件特殊」正是用户决定放不放行的唯一依据。
        """
        panel = await self._show("protected")
        header = str(panel._options[0].prompt)
        self.assertIn("保护路径", header)


class AskClosureTest(unittest.TestCase):
    """AC12：协调层的「本会话放行」在保护路径下改走豁免，**不落盘、不产生③层规则**。"""

    def _manager(self) -> ConversationManager:
        config = Config(
            protocol="deepseek",
            model="test-model",
            base_url="http://test",
            api_key="test-key",
            debug_log=False,
        )
        return ConversationManager(SimpleNamespace(), config, ToolRegistry())

    def _ask(self, manager: ConversationManager, choice: ConfirmDecision, layer: Layer):
        """直接取 `_build_ask` 的闭包来调，绕开整条 Agent Loop。"""
        manager.confirm_callback = lambda _tc, _tool, _dec: choice
        tool = SimpleNamespace(name="write_file", read_only=False)
        call = ToolCall(
            id="c1",
            name="write_file",
            arguments={"path": ".rhinecode/hooks.yaml", "content": "x"},
        )
        decision = DecisionResult(Decision.ASK, layer, "reason", kind="write_path")
        return manager._build_ask()(call, tool, decision)

    def test_session_allow_registers_an_exemption_and_writes_nothing(self) -> None:
        manager = self._manager()
        writes = {"n": 0}
        manager._engine.persist_local_rule = lambda _s: writes.__setitem__(
            "n", writes["n"] + 1
        ) or True

        self.assertTrue(self._ask(manager, ConfirmDecision.ALLOW_SESSION, Layer.PROTECTED))

        self.assertEqual(len(manager._engine.protected_exemptions), 1)
        # ⚠ **计数，不是看返回值。** 「悄悄写了盘但照常放行」与「没写盘」
        # 在返回值上完全一样——这一条就是为了区分那两种情况。
        self.assertEqual(writes["n"], 0, "保护路径的本会话放行绝不该写本地配置")
        self.assertEqual(
            manager._engine.session_rules, [], "也不该产生③层规则（那条规则永远不会被求值）"
        )

    def test_permanent_allow_also_falls_back_to_the_exemption(self) -> None:
        """
        **防御性**：面板在这个场景下压根不提供「永久放行」，但若将来出现第二条
        结算路径把它送进来，也必须落到豁免而不是落盘——落盘会写出一条永远不被
        求值的规则，正是本扩展要消灭的那个骗人的按钮。
        """
        manager = self._manager()
        writes = {"n": 0}
        manager._engine.persist_local_rule = lambda _s: writes.__setitem__(
            "n", writes["n"] + 1
        ) or True

        self.assertTrue(
            self._ask(manager, ConfirmDecision.ALLOW_PERMANENT, Layer.PROTECTED)
        )
        self.assertEqual(len(manager._engine.protected_exemptions), 1)
        self.assertEqual(writes["n"], 0)

    def test_non_protected_session_allow_is_unchanged(self) -> None:
        """
        **反证**：非保护路径的「本会话放行」逐字维持既有行为——
        登记一条③层会话规则、不碰豁免集合。

        没有这条的话，把判据写成无条件走豁免会全绿通过，
        而所有普通文件的「本会话放行」都会静默失效。
        """
        manager = self._manager()
        self.assertTrue(self._ask(manager, ConfirmDecision.ALLOW_SESSION, Layer.MODE))
        self.assertEqual(len(manager._engine.session_rules), 1)
        self.assertEqual(manager._engine.session_rules[0].effect, "allow")
        self.assertEqual(manager._engine.protected_exemptions, set())

    def test_deny_still_denies(self) -> None:
        manager = self._manager()
        self.assertFalse(self._ask(manager, ConfirmDecision.DENY, Layer.PROTECTED))
        self.assertEqual(manager._engine.protected_exemptions, set())


class _WriteTool(Tool):
    """最小写入工具：只为让 `to_request` 走到 `write_path` 分支。"""

    name = "write_file"
    description = "测试用写入工具"
    parameters = {"type": "object", "properties": {}}
    read_only = False

    def execute(self, args: dict, **kwargs) -> ToolResult:
        return ToolResult(ok=True, output="已写入", summary="已写入")


class _ScriptedProvider:
    """按剧本出块的假模型：先调一次写入，再收工。"""

    def __init__(self, path: str):
        self._path = path
        self._round = 0

    def stream_chat(self, messages, thinking_effort="off", tools=None, system=None):
        self._round += 1
        if self._round == 1:
            yield StreamChunk(
                type="tool_call",
                tool_call=ToolCall(
                    id="c1",
                    name="write_file",
                    arguments={"path": self._path, "content": "x"},
                ),
            )
        else:
            yield StreamChunk(type="text", content="做完了")
        yield StreamChunk(type="done")


class TraceFieldTest(unittest.TestCase):
    """AC16：判定与豁免在行为记录里看得见。"""

    def _run(self, path: str, *, exempt: bool) -> list[dict]:
        with tempfile.TemporaryDirectory() as d:
            root = Path(d).resolve()
            trace = root / "t.jsonl"
            rec = TraceRecorder(trace)

            registry = ToolRegistry()
            registry.register(_WriteTool())
            # 放行档：④层本会给 ALLOW，于是「有没有被本层升级」这件事才有分辨力。
            engine = PermissionEngine(RuleSet([]), mode=PermissionMode.PERMISSIVE)
            if exempt:
                engine.protected_exemptions.add((root / path).resolve())

            agent = Agent(_ScriptedProvider(path), registry, recorder=rec)
            list(
                agent.run(
                    [Message(role="user", content="写个文件")],
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
                    options=RunOptions(cwd=root),
                )
            )
            rec.close()
            return [
                json.loads(line)
                for line in trace.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]

    def _decision(self, records: list[dict]) -> dict:
        hits = [
            r for r in records if r["type"] == TraceEventType.PERMISSION_DECISION.value
        ]
        self.assertEqual(len(hits), 1)
        return hits[0]

    def test_upgrade_is_visible_as_the_protected_layer(self) -> None:
        record = self._decision(self._run(".rhinecode/hooks.yaml", exempt=False))
        self.assertEqual(record["decision"], "ask")
        self.assertEqual(record["layer"], "protected")
        self.assertIs(record["protected_exempt"], False)

    def test_exemption_is_visible_as_a_field(self) -> None:
        """
        ⚠ 这条记录的 `decision` 是 `allow`、`layer` 是 `mode`——与「用户切到了
        放行档」**长得一模一样**。`protected_exempt` 是唯一能把两者分开的东西。
        """
        record = self._decision(self._run(".rhinecode/hooks.yaml", exempt=True))
        self.assertEqual(record["decision"], "allow")
        self.assertEqual(record["layer"], "mode")
        self.assertIs(record["protected_exempt"], True)

    def test_ordinary_write_is_untouched(self) -> None:
        record = self._decision(self._run("src/main.py", exempt=False))
        self.assertEqual(record["decision"], "allow")
        self.assertEqual(record["layer"], "mode")
        self.assertIs(record["protected_exempt"], False)


class ReaderSummaryTest(unittest.TestCase):
    """AC16 的另一半：字段进了负载，还得进**摘要行**——不然等于白记。"""

    def test_protected_layer_has_a_name(self) -> None:
        line = _s_permission_decision(
            {
                "tool": "write_file",
                "decision": "ask",
                "layer": "protected",
                "reason": "保护路径：写入 .rhinecode/hooks.yaml…",
            }
        )
        self.assertIn("②″保护路径", line)

    def test_exemption_is_marked(self) -> None:
        line = _s_permission_decision(
            {
                "tool": "write_file",
                "decision": "allow",
                "layer": "mode",
                "reason": "放行模式：无规则命中，默认允许",
                "protected_exempt": True,
            }
        )
        self.assertIn("保护路径已豁免", line)

    def test_no_mark_without_the_flag(self) -> None:
        """反证：标记不是无条件加的。"""
        line = _s_permission_decision(
            {
                "tool": "write_file",
                "decision": "allow",
                "layer": "mode",
                "reason": "放行模式：无规则命中，默认允许",
            }
        )
        self.assertNotIn("保护路径已豁免", line)


class LayerTableTest(unittest.TestCase):
    """
    AC10：`Layer` 的每个取值在**三份展示表**里都要有名字。

    三份表刻意不合一（合一要让只依赖标准库的 trace 叶子包反向依赖 permission，
    也要让它反向依赖整个 TUI 层）。既有的两条遍历断言分别钉住阅读器与另一处，
    这里补上确认面板那份的正向断言。
    """

    def test_every_layer_has_a_panel_label(self) -> None:
        for layer in Layer:
            with self.subTest(layer=layer.value):
                self.assertIn(layer.value, ConfirmPanel._LAYER_LABELS)

    def test_every_layer_has_a_reader_name(self) -> None:
        for layer in Layer:
            with self.subTest(layer=layer.value):
                self.assertIn(layer.value, _LAYER_NAMES)


if __name__ == "__main__":
    unittest.main()
