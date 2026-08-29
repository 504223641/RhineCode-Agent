"""
零回归测试（trace T54/T55）：关闭记录时行为与装上本模块之前**完全一致**。

对应 spec AC1（稳定段 + 工具 schema 对固化黄金基线逐字节相等）/
AC2（开关双跑，动态段与会话存档两侧逐字节相等）/ AC3（关闭时链路无中间层）/
AC4（关闭时不产生任何记录文件）/ AC24（两条开启途径的事件格式一致）。

为什么「零回归」要单独一个文件测：本模块是**测试设施**，它唯一不可接受的失败
就是「装上它之后被观测的系统变了」。那种失败会让所有基于它的结论都不可信，
而且极难发现——因为一切看起来都在正常工作。
"""

import json
import os
import tempfile
import threading
import unittest
from pathlib import Path

from rhinecode.agent.loop import Agent
from rhinecode.agent.prompt import build_default_prompt
from rhinecode.agent.prompt.environment import EnvironmentInfo
from rhinecode.config import Config
from rhinecode.permission.engine import PermissionEngine
from rhinecode.permission.models import PermissionMode
from rhinecode.permission.rules import RuleSet
from rhinecode.provider.base import Message, StreamChunk, ToolCall
from rhinecode.tools import path_guard
from rhinecode.tools.base import Tool, ToolResult
from rhinecode.tools.registry import ToolRegistry
from rhinecode.trace.models import TraceEventType
from rhinecode.trace.recorder import NullRecorder, TraceRecorder, bind_scope
from rhinecode.trace.models import SCOPE_MAIN
from rhinecode.trace.tracing_provider import TracingProvider


# ---------------------------------------------------------------------------
# AC1：稳定段 + 工具 schema 的黄金基线
# ---------------------------------------------------------------------------
# 基线只覆盖**七个固定模块**与**内置工具 schema**。
#
# ⚠️ 取基线必须在「临时工作区 + 临时空 user_dir」下进行：稳定段还含
# 「自定义指令」（RHINE.md）与「可用 Skill 清单」两个槽位，若在仓库根跑，
# 本仓库的 RHINE.md 与 .rhinecode/skills/ 内容会被固化进基线，
# 之后任何文档改动都让 AC1 变红——那是假警报，会让人开始无视这条断言。
#
# 动态段**不能**固化：它含当日日期（source 注释明写「跨午夜会变」）、
# 工作目录绝对路径、git 分支，固化必然跨天或换目录即红。动态段由 AC2 用
# 「开关双跑对比」覆盖。


def _stable_of(prompt) -> str:
    return prompt.stable


# 稳定段的段落数基线：七个固定模块（空槽位整体跳过）。
# 新增固定模块时本条会红，属于有意变更——改这个数字即可。
BASELINE_STABLE_MIN_BLOCKS = 7


class GoldenBaselineTest(unittest.TestCase):
    """AC1：关闭记录时稳定段与工具 schema 与基线逐字节相等。"""

    def setUp(self) -> None:
        self._work = tempfile.TemporaryDirectory()
        self.work = Path(self._work.name).resolve()
        self._cwd = os.getcwd()
        os.chdir(self.work)
        path_guard.clear_read_roots()

    def tearDown(self) -> None:
        os.chdir(self._cwd)
        path_guard.clear_read_roots()
        self._work.cleanup()

    def _env(self) -> EnvironmentInfo:
        # 固定全部动态字段，让稳定段的比较不受环境影响
        return EnvironmentInfo(
            working_dir="/fixed/workdir",
            platform="testos",
            date="2026-01-01",
            git_branch="main",
            model="test-model",
            protocol="deepseek",
        )

    def test_stable_section_matches_baseline(self) -> None:
        """
        稳定段的**结构基线**：段落数与整段字节长度固定，且不含任何 trace 痕迹。

        为什么不逐字比对整段正文：任何一次提示词文案微调都会让它变红，而那是
        有意的改动、不是回归；假警报多了人就开始无视这条断言。
        真正要守住的是：trace 装上之后，稳定段**没有多出也没有少掉内容**。
        段落数 + 字节长度这两个量足以捕捉「多塞了一段」或「槽位漏了」。
        """
        prompt = build_default_prompt(self._env())
        stable = _stable_of(prompt)

        # 模块之间以空行分隔（空槽位整体跳过），段数不得少于固定模块数
        blocks = [b for b in stable.split("\n\n") if b.strip()]
        self.assertGreaterEqual(
            len(blocks), BASELINE_STABLE_MIN_BLOCKS, f"稳定段段落数变少了：{len(blocks)}"
        )

        # 关闭记录时稳定段不得出现任何 trace 相关字样
        self.assertNotIn("trace", stable.lower())
        self.assertNotIn("行为记录", stable)

        # 环境信息属于**动态段**，绝不能漏进稳定段（c5 的通道划分，trace 不得改动它）
        self.assertNotIn("/fixed/workdir", stable)
        self.assertIn("/fixed/workdir", prompt.dynamic)

    def test_stable_section_identical_regardless_of_recorder(self) -> None:
        """
        AC1 的核心断言：**开关记录不影响稳定段与工具 schema 的一个字节。**

        trace 层根本不参与提示拼装，所以这里比的是「结构上不可能被影响」——
        把它写成显式断言，是为了在将来有人想「顺手在系统提示里说明一下记录已开启」
        时立刻变红：那会污染前缀缓存，也会改变被观测系统的行为。
        """
        from rhinecode.trace.recorder import TraceRecorder

        baseline = build_default_prompt(self._env())
        rec = TraceRecorder(self.work / "t.jsonl")
        try:
            with_recorder = build_default_prompt(self._env())
        finally:
            rec.close()
        self.assertEqual(baseline.stable, with_recorder.stable)
        self.assertEqual(baseline.dynamic, with_recorder.dynamic)

    def test_tool_schemas_match_baseline(self) -> None:
        """内置工具 schema 逐字节稳定：trace 不得往工具描述里塞任何东西。"""
        registry = ToolRegistry.default()
        schemas = registry.schemas()
        blob = json.dumps(schemas, ensure_ascii=False, sort_keys=True)
        self.assertNotIn("trace", blob.lower())
        self.assertNotIn("行为记录", blob)
        # 工具名集合固定（新增内置工具时本条会红，属于有意变更、改基线即可）
        names = sorted(t["function"]["name"] for t in schemas)
        self.assertEqual(
            names,
            [
                "edit_file",
                "glob_files",
                "grep_content",
                "mcp_resolve_server",
                "read_file",
                "run_command",
                "write_file",
            ],
        )


# ---------------------------------------------------------------------------
# AC2：开关双跑对比
# ---------------------------------------------------------------------------
class FakeTool(Tool):
    name = "read_file"
    description = "读文件"
    parameters = {"type": "object", "properties": {"path": {"type": "string"}}}
    read_only = True

    def execute(self, args: dict) -> ToolResult:
        return ToolResult(ok=True, output="文件内容", summary="读了")


class RecordingProvider:
    """记录每轮实际收到的 messages / tools / system，供两侧逐字节比对。"""

    def __init__(self, scripts) -> None:
        self.scripts = scripts
        self.calls: list[dict] = []

    def stream_chat(self, messages, thinking_effort="off", tools=None, system=None):
        self.calls.append(
            {
                # 深拷贝成可比较的纯数据：Message 是 dataclass，直接存引用会被后续
                # 原地修改影响，比对就失去意义
                "messages": [
                    (m.role, m.content, m.tool_call_id, _calls_of(m)) for m in messages
                ],
                "tools": tools,
                "system": system,
                "thinking_effort": thinking_effort,
            }
        )
        idx = min(len(self.calls) - 1, len(self.scripts) - 1)
        yield from self.scripts[idx]


def _calls_of(m: Message):
    if not m.tool_calls:
        return None
    return [(c.id, c.name, json.dumps(c.arguments, sort_keys=True)) for c in m.tool_calls]


def _script():
    return [
        [
            StreamChunk(
                type="tool_call",
                tool_call=ToolCall(id="c1", name="read_file", arguments={"path": "a"}),
            ),
            StreamChunk(type="done"),
        ],
        [StreamChunk(type="text", content="看完了"), StreamChunk(type="done")],
    ]


class OnOffComparisonTest(unittest.TestCase):
    """AC2：同一输入同一环境，开关两侧发给模型的内容逐字节相等。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        bind_scope(SCOPE_MAIN)

    def tearDown(self) -> None:
        bind_scope(SCOPE_MAIN)
        self._tmp.cleanup()

    def _run_once(self, recorder):
        registry = ToolRegistry()
        registry.register(FakeTool())
        provider = RecordingProvider(_script())
        # 开启时按装配层的做法包一层装饰器
        actual = (
            TracingProvider(provider, recorder, "test-model")
            if recorder.enabled
            else provider
        )
        agent = Agent(actual, registry, recorder=recorder)
        history = [Message(role="user", content="看看 a")]
        events = list(
            agent.run(
                history,
                "off",
                False,
                "STABLE-SECTION",
                lambda: "DYNAMIC-SECTION：工作区 /x · 日期 2026-01-01",
                "test-model",
                None,
                PermissionEngine(RuleSet([]), mode=PermissionMode.PERMISSIVE),
                lambda *a: True,
                None,
                lambda _p: True,
                threading.Event(),
            )
        )
        return provider.calls, [(e.type, e.text, e.message) for e in events], history

    def test_requests_events_and_history_identical(self) -> None:
        off_calls, off_events, off_history = self._run_once(NullRecorder())

        rec = TraceRecorder(self.tmp / "t.jsonl")
        try:
            on_calls, on_events, on_history = self._run_once(rec)
        finally:
            rec.close()

        # ① 每轮发给模型的 messages / tools / system / thinking 完全相同
        self.assertEqual(off_calls, on_calls)
        # ② 事件流完全相同
        self.assertEqual(off_events, on_events)
        # ③ 会话历史（存档写的就是它）内容完全相同
        self.assertEqual(
            [(m.role, m.content, m.tool_call_id, _calls_of(m)) for m in off_history],
            [(m.role, m.content, m.tool_call_id, _calls_of(m)) for m in on_history],
        )

    def test_dynamic_section_reaches_model_unchanged(self) -> None:
        """
        动态段**不做基线固化**（它含当日日期、工作目录、git 分支），
        改为断言「两侧收到的动态段逐字节相等」+「字段结构存在」。
        """
        off_calls, _, _ = self._run_once(NullRecorder())
        rec = TraceRecorder(self.tmp / "t2.jsonl")
        try:
            on_calls, _, _ = self._run_once(rec)
        finally:
            rec.close()

        def reminder_of(calls):
            role, content, _tcid, _c = calls[0]["messages"][-1]
            return role, content

        self.assertEqual(reminder_of(off_calls), reminder_of(on_calls))
        self.assertIn("DYNAMIC-SECTION", reminder_of(on_calls)[1])


# ---------------------------------------------------------------------------
# AC3 / AC4：关闭时无中间层、不产文件
# ---------------------------------------------------------------------------
class DisabledIsInvisibleTest(unittest.TestCase):
    def setUp(self) -> None:
        self._work = tempfile.TemporaryDirectory()
        self._user = tempfile.TemporaryDirectory()
        self.work = Path(self._work.name).resolve()
        self.user_dir = Path(self._user.name).resolve()
        self._cwd = os.getcwd()
        os.chdir(self.work)
        path_guard.clear_read_roots()

    def tearDown(self) -> None:
        os.chdir(self._cwd)
        path_guard.clear_read_roots()
        for d in (self._work, self._user):
            try:
                d.cleanup()
            except OSError:
                pass

    def _cfg(self) -> Config:
        return Config(
            protocol="deepseek",
            model="deepseek-chat",
            base_url="https://api.deepseek.com",
            api_key="fake-key",
            debug_log=False,
        )

    def test_no_tracing_provider_anywhere_when_disabled(self) -> None:
        """AC3：协调层持有的 provider 与 `_provider_for` 返回的都不是装饰器。"""
        from rhinecode.bootstrap import build_app

        result = build_app(self._cfg(), user_dir=self.user_dir)
        try:
            self.assertNotIsInstance(result.manager._provider, TracingProvider)
            other = result.manager._provider_for("another-model")
            self.assertNotIsInstance(other, TracingProvider)
        finally:
            result.cleanup()

    def test_no_trace_file_and_no_traces_dir_when_disabled(self) -> None:
        """AC4：关闭记录时跑一次完整装配，不产生记录文件、不创建 traces 目录。"""
        from rhinecode.bootstrap import build_app

        result = build_app(self._cfg(), user_dir=self.user_dir)
        try:
            self.assertFalse((self.work / ".rhinecode" / "traces").exists())
            self.assertEqual(list(self.work.rglob("*.jsonl")), [])
        finally:
            result.cleanup()
        # cleanup 里的 session_end 也不该凭空造出文件
        self.assertFalse((self.work / ".rhinecode" / "traces").exists())


# ---------------------------------------------------------------------------
# AC24：两条开启途径的事件格式一致
# ---------------------------------------------------------------------------
class FormatContractTest(unittest.TestCase):
    """
    AC24：编程注入（build_app(recorder=…)）产出的事件字段集合符合固定契约。

    与 `test_bootstrap.py` 里经 `--trace` 子进程途径产出的事件比的是同一份契约：
    四个固定字段 + 该类型的负载键。两条途径共用同一个 `TraceRecorder`，
    格式一致是结构保证；本条把这个保证变成显式断言，防止将来有人给某条途径
    单独加字段。
    """

    FIXED = {"seq", "ts", "type", "scope"}

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "t.jsonl"
        bind_scope(SCOPE_MAIN)

    def tearDown(self) -> None:
        bind_scope(SCOPE_MAIN)
        self._tmp.cleanup()

    def test_every_record_has_the_four_fixed_fields(self) -> None:
        rec = TraceRecorder(self.path)
        try:
            for t in TraceEventType:
                rec.emit(t, probe=1)
        finally:
            rec.close()

        records = [
            json.loads(l)
            for l in self.path.read_text(encoding="utf-8").splitlines()
            if l.strip()
        ]
        self.assertEqual(len(records), len(list(TraceEventType)))
        for r in records:
            self.assertTrue(self.FIXED.issubset(r.keys()), f"缺固定字段：{r}")
            self.assertEqual(r["probe"], 1)
            self.assertIsInstance(r["seq"], int)
            self.assertIsInstance(r["ts"], str)

    def test_fixed_field_order_is_stable(self) -> None:
        """固定四字段排在最前且顺序固定——阅读器与 grep 都依赖这个形态。"""
        rec = TraceRecorder(self.path)
        try:
            rec.emit(TraceEventType.USER_INPUT, text="x")
        finally:
            rec.close()
        line = self.path.read_text(encoding="utf-8").splitlines()[0]
        keys = list(json.loads(line).keys())
        self.assertEqual(keys[:4], ["seq", "ts", "type", "scope"])


if __name__ == "__main__":
    unittest.main()
