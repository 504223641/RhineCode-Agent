"""
护栏：**模型写坏的那段参数必须留在记录里**。

## 这组用例在防什么

模型发起工具调用时，参数是一串 JSON 文本，按流式碎片到达、由 Provider 拼接后
解析。解析失败时 `ToolCall.arguments` 是 `None`，协调层把它转成一条结构化错误
回灌模型——这一步本身没问题，**问题在于那串原文原先就此消失了**：记录里只剩
`"arguments": null`，事后既说不清模型写坏在哪，也分不清是「模型生成了非法
JSON」还是「我们拼碎片时丢了东西」。

真实撞到过一次（2026-09-18 的 trace，一次约 1200 token 的 `edit_file` 参数），
当场无从排查，只能看着它白烧掉一整轮迭代。

这与「工具裁剪了 output 必须同时填 `full_output`」是同一条纪律的两个落点：
**观测设施丢内容就不再是证据。**

## 为什么这组用例必须存在

典型的「漏改不报错」形态：不带原文时功能完全正常——模型收到错误、重试、
往下跑，测试全绿、界面正常。**唯一的区别只在事后有没有东西可查**，
而那件事要等到下一次真的出问题时才发现。

同一次失败会在记录里留下**两条**事件（`api_response` 与 `tool_execute`），
两条各有各的组装代码，所以下面**逐条写护栏**——只钉其中一条时，
把另一条改回原样照样全绿。
"""

import json
import tempfile
import unittest
from pathlib import Path

from rhinecode.agent.loop import _invalid_args_result
from rhinecode.config import Config
from rhinecode.provider.base import StreamChunk, ToolCall
from rhinecode.trace import reader
from rhinecode.trace.models import TraceEventType
from rhinecode.trace.recorder import TraceRecorder
from rhinecode.trace.tracing_provider import TracingProvider
from rhinecode.tools.base import ToolResult


# 刻意造得比任何「顺手加的截断阈值」都长：短样本下一个 4000 字符的封顶
# 也测不出来，而记录层恰恰有过那样的阈值（已删，见 test_trace_models）。
_LONG = "x" * 9000
# 一段**断在半路**的参数，形态照抄那次真实失败：末尾缺右花括号
BROKEN_JSON = '{"path": "a.py", "old_string": "' + _LONG + '", "new_string": "b"'
# 合法 JSON、但不是对象——权限层只认字典，因此同样按解析失败处理
NOT_AN_OBJECT = "[1, 2, 3]"


# --------------------------------------------------------------------------- #
# SDK 流式对象的替身（与 test_tui_tool_pending 同形，刻意各留一份：
# 那边验的是「播报时机」，这边验的是「解析结果」，合一会让两处互相牵制）
# --------------------------------------------------------------------------- #


class _FakeFunction:
    def __init__(self, name: str = "", arguments: str = "") -> None:
        self.name = name
        self.arguments = arguments


class _FakeToolCallDelta:
    def __init__(self, index: int, id: str = "", function=None) -> None:
        self.index = index
        self.id = id
        self.function = function


class _FakeDelta:
    def __init__(self, tool_calls=None, content: str = "") -> None:
        self.tool_calls = tool_calls
        self.content = content
        self.reasoning_content = None


class _FakeChoice:
    def __init__(self, delta) -> None:
        self.delta = delta


class _FakeChunk:
    def __init__(self, delta) -> None:
        self.choices = [_FakeChoice(delta)]
        self.usage = None


def _fragment(index: int, id: str = "", name: str = "", arguments: str = "") -> _FakeChunk:
    """造一个「工具调用碎片」块，形态与 OpenAI 兼容协议的 delta.tool_calls 一致。"""
    return _FakeChunk(
        _FakeDelta(
            tool_calls=[
                _FakeToolCallDelta(
                    index=index, id=id, function=_FakeFunction(name, arguments)
                )
            ]
        )
    )


def _parse_via_provider(raw: str, *, pieces: int = 3) -> ToolCall:
    """
    让真正的 Provider 把 `raw` 按碎片拼起来再解析，返回它产出的 ToolCall。

    :param raw: 模型「写出来」的参数原文
    :param pieces: 切成几片送达——**刻意大于 1**，因为真实场景下参数总是分片
                   到达的，一次性给全会让「拼接」这一步根本没被执行到
    :returns: Provider 产出的最终 ToolCall（断言恰好只有一个）

    副作用：无（Provider 的 SDK 客户端被替身顶掉，不发任何网络请求）。
    """
    from rhinecode.provider.deepseek import DeepSeekProvider

    provider = DeepSeekProvider(
        Config(
            protocol="deepseek",
            model="test-model",
            base_url="http://test",
            api_key="test-key",
            debug_log=False,
        )
    )

    size = max(1, len(raw) // pieces + 1)
    chunks = [_fragment(0, id="call_1", name="edit_file", arguments=raw[:size])]
    for start in range(size, len(raw), size):
        chunks.append(_fragment(0, arguments=raw[start : start + size]))

    class FakeCompletions:
        def create(self, **kwargs):
            return iter(chunks)

    class FakeChat:
        completions = FakeCompletions()

    class FakeClient:
        chat = FakeChat()

    provider._client = FakeClient()

    calls = [
        c.tool_call
        for c in provider.stream_chat([], tools=None, system=None)
        if c.type == "tool_call" and c.tool_call is not None
    ]
    assert len(calls) == 1, f"期望恰好一个工具调用，实际 {len(calls)} 个"
    return calls[0]


def _agent_with_recorder(recorder: TraceRecorder):
    """
    造一个只够用来发埋点的 Agent 壳子。

    `_trace_tool` 只读 `self._recorder`，而完整构造一个 Agent 要拉起 Provider、
    工具注册中心与权限引擎——那些与本组判据无关，反而会把失败引到别处。
    """
    from rhinecode.agent.loop import Agent

    agent = Agent.__new__(Agent)
    agent._recorder = recorder
    return agent


def _read_events(path: Path, event_type: str) -> list[dict]:
    """
    读出记录文件里某一类事件。

    ⚠ 句柄必须显式关掉（`with`）：Windows 下一个还开着的读句柄会让
    `TemporaryDirectory` 的清理失败，表现成与判据毫无关系的偶发红。
    """
    with open(path, "rb") as fh:
        records = [json.loads(line) for line in fh]
    return [r for r in records if r["type"] == event_type]


class ProviderKeepsTheRawTextTest(unittest.TestCase):
    """Provider 侧：解析失败时原文必须留下，解析成功时刻意不留。"""

    def test_broken_json_keeps_every_character(self) -> None:
        """
        **本文件最核心的一条**：原文必须一个字符都不差。

        判据刻意是「逐字相等」而不是「长度大于 0」或「包含某个片段」——
        一个只留前 200 字符的实现在后两种判据下照样全绿，而那正是要防的形态。
        """
        tc = _parse_via_provider(BROKEN_JSON)
        self.assertIsNone(tc.arguments, "非法 JSON 必须解析成 None")
        self.assertEqual(tc.raw_arguments, BROKEN_JSON, "原文必须一字不差地留下")

    def test_broken_json_says_why_it_failed(self) -> None:
        """失败原因要能指出位置——那是模型改对它的唯一线索。"""
        tc = _parse_via_provider(BROKEN_JSON)
        self.assertTrue(tc.arguments_error, "必须记下失败原因")
        # JSON 解析器给的位置信息（column / char）是这条线索的全部价值所在
        self.assertRegex(tc.arguments_error, r"(column|char)\s*\d+")

    def test_valid_json_does_not_store_a_second_copy(self) -> None:
        """
        解析成功时 `raw_arguments` 必须为空。

        **反证性质**：`arguments` 已经无损承载了同样的内容，再存一份会让每条
        `api_response` 里的写文件正文凭空翻倍——而写文件的参数动辄几十 KB，
        长会话下这是实打实的磁盘与内存代价。「反正都记上」看起来永远像是
        更安全的选择，这条用例就是拦它的。
        """
        tc = _parse_via_provider('{"path": "a.py", "content": "' + _LONG + '"}')
        self.assertIsInstance(tc.arguments, dict)
        self.assertIsNone(tc.raw_arguments, "解析成功时不该再存一份原文")
        self.assertIsNone(tc.arguments_error)

    def test_valid_json_that_is_not_an_object_also_keeps_the_raw_text(self) -> None:
        """
        第二种失败形态：JSON 合法但不是对象。

        它走的是**另一条分支**（`json.loads` 成功、类型检查失败），只钉住语法
        错误那条时这一条会静默退化成「原文丢失」。
        """
        tc = _parse_via_provider(NOT_AN_OBJECT)
        self.assertIsNone(tc.arguments)
        self.assertEqual(tc.raw_arguments, NOT_AN_OBJECT)
        self.assertIn("对象", tc.arguments_error or "")


class ApiResponseEventTest(unittest.TestCase):
    """第一条落点：`api_response` 事件里的 tool_calls。"""

    def _record(self, tc: ToolCall) -> dict:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / "t.jsonl"
        recorder = TraceRecorder(path)

        class Inner:
            def stream_chat(self, messages, thinking_effort=None, tools=None, system=None):
                yield StreamChunk(type="tool_call", tool_call=tc)
                yield StreamChunk(type="done", content="")

        for _ in TracingProvider(Inner(), recorder, model="fake").stream_chat(
            [], tools=None, system=None
        ):
            pass
        recorder.close()

        responses = _read_events(path, TraceEventType.API_RESPONSE.value)
        self.assertEqual(len(responses), 1)
        return responses[0]

    def test_raw_text_reaches_the_trace_file_unchanged(self) -> None:
        """从 Provider 到磁盘的整条链路上一个字符都不能少。"""
        call = self._record(_parse_via_provider(BROKEN_JSON))["tool_calls"][0]
        self.assertIsNone(call["arguments"])
        self.assertEqual(call["raw_arguments"], BROKEN_JSON)
        self.assertTrue(call["arguments_error"])

    def test_successful_call_has_no_extra_keys(self) -> None:
        """解析成功时那两个键一个都不该出现。"""
        call = self._record(_parse_via_provider('{"path": "a.py"}'))["tool_calls"][0]
        self.assertIsNotNone(call["arguments"])
        self.assertNotIn("raw_arguments", call)
        self.assertNotIn("arguments_error", call)


class ToolExecuteEventTest(unittest.TestCase):
    """
    第二条落点：`tool_execute` 事件。

    ⚠ 与上一个类**必须分开写**：两条事件各有各的组装代码，只钉一条时把另一条
    改回原样照样全绿（同 `read_file` 两条读取路径那次变异实测的教训）。
    """

    def _emit(self, tc: ToolCall, res: ToolResult, outcome: str) -> dict:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / "t.jsonl"
        recorder = TraceRecorder(path)
        _agent_with_recorder(recorder)._trace_tool(
            tc, res, outcome, is_concurrent=False, cwd=None
        )
        recorder.close()

        events = _read_events(path, TraceEventType.TOOL_EXECUTE.value)
        self.assertEqual(len(events), 1)
        return events[0]

    def test_raw_text_is_recorded_here_too(self) -> None:
        tc = _parse_via_provider(BROKEN_JSON)
        event = self._emit(tc, _invalid_args_result(tc), "invalid_arguments")
        self.assertIsNone(event["arguments"])
        self.assertEqual(event["raw_arguments"], BROKEN_JSON)
        self.assertTrue(event["arguments_error"])

    def test_successful_call_has_no_extra_keys(self) -> None:
        tc = _parse_via_provider('{"path": "a.py"}')
        event = self._emit(
            tc, ToolResult(ok=True, output="done", summary="ok"), "executed"
        )
        self.assertNotIn("raw_arguments", event)
        self.assertNotIn("arguments_error", event)


class ReaderShowsItTest(unittest.TestCase):
    """
    阅读器的摘要行要一眼看得见。

    记了却不显示等于白记（同「trace 埋点新增字段 → 同步摘要函数」那条成对
    维护点）：这一轮模型**是**发了工具调用的，而「工具调用 1 个」这句话
    分辨不出它压根没能执行。
    """

    def _summarize(self, calls: list[dict]) -> str:
        return reader.SUMMARIZERS[TraceEventType.API_RESPONSE.value](
            {"turn": 3, "duration_ms": 10, "tool_calls": calls, "text": ""}
        )

    def test_timeline_flags_the_failure(self) -> None:
        line = self._summarize(
            [
                {
                    "id": "1",
                    "name": "edit_file",
                    "arguments": None,
                    "raw_arguments": BROKEN_JSON,
                    "arguments_error": "JSON 解析失败：Expecting ',' delimiter",
                }
            ]
        )
        self.assertIn("参数解析失败", line)
        self.assertIn("Expecting", line, "原因要带上，不能只说『失败了』")

    def test_a_normal_response_stays_clean(self) -> None:
        """反证：正常响应的摘要行不能凭空多出这段。"""
        line = self._summarize([{"id": "1", "name": "edit_file", "arguments": "{}"}])
        self.assertNotIn("参数解析失败", line)

    def test_the_raw_text_itself_stays_out_of_the_summary_line(self) -> None:
        """摘要行是一行，原文在负载里（`--seq` 展开可见），别把 9000 字符贴上去。"""
        line = self._summarize(
            [
                {
                    "id": "1",
                    "name": "edit_file",
                    "arguments": None,
                    "raw_arguments": BROKEN_JSON,
                    "arguments_error": "JSON 解析失败",
                }
            ]
        )
        self.assertNotIn(_LONG, line)


class ModelFacingMessageTest(unittest.TestCase):
    """
    回灌给模型的那句话要说清错在哪。

    只说「格式不对、重试」的话，模型只能凭猜重写一遍同样长的参数——真实 trace
    里一次 1200 token 的 `edit_file` 参数就这么白烧了一整轮。这与已知项 #21
    的教训同源：**拒绝的文案要说清为什么，否则模型会去找绕过的办法。**
    """

    def test_reason_is_handed_back_to_the_model(self) -> None:
        res = _invalid_args_result(_parse_via_provider(BROKEN_JSON))
        self.assertFalse(res.ok)
        self.assertIn("edit_file", res.output)
        self.assertRegex(res.output, r"(column|char)\s*\d+")

    def test_the_raw_text_is_not_echoed_back(self) -> None:
        """
        **反证**：原文不能回灌给模型。

        那是它自己刚写的，再贴一遍只会白占一遍上下文——而这次的原文有 9000
        字符。原文留给 trace 给人看，两个去向刻意不同。
        """
        res = _invalid_args_result(_parse_via_provider(BROKEN_JSON))
        self.assertNotIn(_LONG, res.output)
        self.assertLess(len(res.output), 300)

    def test_it_still_works_when_there_is_no_reason(self) -> None:
        """没有原因时（比如别的 Provider 没填）文案仍要成立，不能拼出半句话。"""
        res = _invalid_args_result(ToolCall(id="1", name="write_file", arguments=None))
        self.assertIn("write_file", res.output)
        self.assertNotIn("（）", res.output)


if __name__ == "__main__":
    unittest.main()
