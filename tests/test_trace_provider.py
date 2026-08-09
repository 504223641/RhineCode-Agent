"""
TracingProvider 单测（trace T11）：请求响应成对、break 后仍结算、轮次分作用域计数、零改写。

对应 spec AC10（请求内容完整）/ AC3（`inner` 可见）/ 零侵入（转发的块逐个相等）。
"""

import json
import tempfile
import unittest
from pathlib import Path

from rhinecode.provider.base import BaseProvider, Message, StreamChunk, ToolCall
from rhinecode.trace.models import SCOPE_MAIN, SCOPE_SUMMARY
from rhinecode.trace.recorder import TraceRecorder, bind_scope
from rhinecode.trace.tracing_provider import TracingProvider


class FakeProvider(BaseProvider):
    """按脚本产出固定块序列的假 Provider，并记录收到的参数供断言。"""

    def __init__(self, chunks: list[StreamChunk]) -> None:
        self.chunks = chunks
        self.calls: list[dict] = []

    def stream_chat(self, messages, thinking_effort="off", tools=None, system=None):
        self.calls.append(
            {
                "messages": messages,
                "thinking_effort": thinking_effort,
                "tools": tools,
                "system": system,
            }
        )
        for c in self.chunks:
            yield c


def _schema(name: str) -> dict:
    return {"type": "function", "function": {"name": name, "parameters": {}}}


class TracingProviderTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "trace.jsonl"
        self.rec = TraceRecorder(self.path)
        bind_scope(SCOPE_MAIN)

    def tearDown(self) -> None:
        self.rec.close()
        bind_scope(SCOPE_MAIN)
        self._tmp.cleanup()

    def records(self) -> list[dict]:
        text = self.path.read_text(encoding="utf-8")
        return [json.loads(l) for l in text.splitlines() if l.strip()]

    def test_normal_stream_produces_paired_events(self) -> None:
        inner = FakeProvider(
            [
                StreamChunk(type="thinking", content="想一下"),
                StreamChunk(type="text", content="你好"),
                StreamChunk(type="text", content="世界"),
                StreamChunk(type="done"),
            ]
        )
        p = TracingProvider(inner, self.rec, model="deepseek-chat")
        list(p.stream_chat([Message(role="user", content="hi")]))

        recs = self.records()
        self.assertEqual([r["type"] for r in recs], ["api_request", "api_response"])
        req, resp = recs
        self.assertEqual(req["turn"], 1)
        self.assertEqual(resp["turn"], 1)
        self.assertEqual(resp["text"], "你好世界")
        self.assertEqual(resp["thinking"], "想一下")
        self.assertIsNone(resp["stream_error"])
        self.assertGreaterEqual(resp["duration_ms"], 0)
        self.assertEqual(req["model"], "deepseek-chat")

    def test_break_on_error_still_settles_response_first(self) -> None:
        """
        消费方在 error 块处 break 时，生成器不会被耗尽——显式 closing 保证
        api_response 仍然产出，且 seq 小于调用方随后产出的任何事件。
        """
        inner = FakeProvider(
            [
                StreamChunk(type="text", content="部分"),
                StreamChunk(type="error", content="网络中断"),
                StreamChunk(type="done"),
            ]
        )
        p = TracingProvider(inner, self.rec, model="m")

        seen = []
        for chunk in p.stream_chat([Message(role="user", content="hi")]):
            seen.append(chunk)
            if chunk.type == "error":
                break

        recs = self.records()
        self.assertEqual([r["type"] for r in recs], ["api_request", "api_response"])
        self.assertLess(recs[0]["seq"], recs[1]["seq"])
        self.assertEqual(recs[1]["stream_error"], "网络中断")
        # done 块没有被消费（证明确实 break 了），但响应事件照样结算
        self.assertEqual([c.type for c in seen], ["text", "error"])

    def test_tool_names_match_passed_schema(self) -> None:
        tools = [_schema("read_file"), _schema("grep_content")]
        inner = FakeProvider([StreamChunk(type="done")])
        p = TracingProvider(inner, self.rec, model="m")
        list(p.stream_chat([Message(role="user", content="x")], tools=tools))

        req = self.records()[0]
        self.assertEqual(req["tool_names"], ["read_file", "grep_content"])
        # 完整 schema 也在（AC1 的黄金基线需要它）
        self.assertEqual(req["tools"], tools)

    def test_malformed_schema_does_not_break_request(self) -> None:
        # 埋点绝不能因为一个畸形 schema 就把整次模型请求搞挂
        inner = FakeProvider([StreamChunk(type="done")])
        p = TracingProvider(inner, self.rec, model="m")
        list(p.stream_chat([Message(role="user", content="x")], tools=["not a dict", {}]))
        self.assertEqual(self.records()[0]["tool_names"], [])

    def test_stream_aggregates_replace_per_chunk_events(self) -> None:
        """
        流式聚合指标：块数与首字延迟。

        它们替代了「每个流式块记一条 agent_event」那种做法——手测实测那样会让
        93% 的记录变成只含 `text_length: 2` 的噪音，而且反而算不出首字延迟
        （得自己去减两条记录的时间戳）。
        """
        inner = FakeProvider(
            [
                StreamChunk(type="thinking", content="想"),
                StreamChunk(type="text", content="你"),
                StreamChunk(type="text", content="好"),
                StreamChunk(type="text", content="呀"),
                StreamChunk(type="done"),
            ]
        )
        p = TracingProvider(inner, self.rec, model="m")
        list(p.stream_chat([Message(role="user", content="hi")]))

        resp = self.records()[1]
        self.assertEqual(resp["text_chunks"], 3)
        self.assertEqual(resp["thinking_chunks"], 1)
        self.assertIsNotNone(resp["first_chunk_ms"])
        self.assertGreaterEqual(resp["first_chunk_ms"], 0)
        self.assertLessEqual(resp["first_chunk_ms"], resp["duration_ms"])

    def test_no_chunks_leaves_first_chunk_none(self) -> None:
        """一个字都没出（比如直接报错）时首字延迟为 None，而不是伪造一个 0。"""
        inner = FakeProvider([StreamChunk(type="error", content="炸了")])
        p = TracingProvider(inner, self.rec, model="m")
        list(p.stream_chat([Message(role="user", content="hi")]))
        resp = self.records()[1]
        self.assertIsNone(resp["first_chunk_ms"])
        self.assertEqual(resp["text_chunks"], 0)

    def test_tool_calls_recorded_in_response(self) -> None:
        inner = FakeProvider(
            [
                StreamChunk(
                    type="tool_call",
                    tool_call=ToolCall(id="c1", name="read_file", arguments={"path": "a"}),
                ),
                StreamChunk(type="done"),
            ]
        )
        p = TracingProvider(inner, self.rec, model="m")
        list(p.stream_chat([Message(role="user", content="x")]))
        resp = self.records()[1]
        self.assertEqual(resp["tool_calls"][0]["name"], "read_file")
        self.assertEqual(resp["tool_calls"][0]["id"], "c1")

    def test_turn_counts_per_scope_independently(self) -> None:
        inner = FakeProvider([StreamChunk(type="done")])
        p = TracingProvider(inner, self.rec, model="m")

        list(p.stream_chat([Message(role="user", content="1")]))
        list(p.stream_chat([Message(role="user", content="2")]))
        with self.rec.scope(SCOPE_SUMMARY):
            list(p.stream_chat([Message(role="user", content="s")]))

        reqs = [r for r in self.records() if r["type"] == "api_request"]
        self.assertEqual(
            [(r["scope"], r["turn"]) for r in reqs],
            [(SCOPE_MAIN, 1), (SCOPE_MAIN, 2), (SCOPE_SUMMARY, 1)],
        )

    def test_inner_exposed_and_chunks_forwarded_unchanged(self) -> None:
        chunks = [
            StreamChunk(type="text", content="a"),
            StreamChunk(type="usage", usage=None),
            StreamChunk(type="done"),
        ]
        inner = FakeProvider(chunks)
        p = TracingProvider(inner, self.rec, model="m")

        self.assertIs(p.inner, inner)
        out = list(p.stream_chat([Message(role="user", content="x")]))
        # 逐个「是同一个对象」——不是拷贝、不是改写后的新块
        self.assertEqual(len(out), len(chunks))
        for got, expected in zip(out, chunks):
            self.assertIs(got, expected)

    def test_parameters_forwarded_verbatim(self) -> None:
        inner = FakeProvider([StreamChunk(type="done")])
        p = TracingProvider(inner, self.rec, model="m")
        msgs = [Message(role="user", content="x")]
        tools = [_schema("read_file")]
        list(p.stream_chat(msgs, thinking_effort="high", tools=tools, system="SYS"))

        call = inner.calls[0]
        self.assertIs(call["messages"], msgs)
        self.assertEqual(call["thinking_effort"], "high")
        self.assertIs(call["tools"], tools)
        self.assertEqual(call["system"], "SYS")

    def test_messages_recorded_in_full(self) -> None:
        """
        消息历史**全量**落盘：既不裁内容，也不裁条数。

        这条替代了原来的 `test_messages_clipped_and_counted`——它当时逐条断言
        「超过 4000 字符包成截断对象」「超过 400 条只留头部」，两条现在都已作废。

        用的长度刻意远超旧阈值（10 倍字符 / 4 倍条数），这样万一有人把阈值
        「调大一点」而不是删掉，这条依然红。
        """
        long_msg = Message(role="user", content="x" * 40_000)
        inner = FakeProvider([StreamChunk(type="done")])
        p = TracingProvider(inner, self.rec, model="m")
        list(p.stream_chat([long_msg]))

        content = self.records()[0]["messages"][0]["content"]
        # 裸字符串、原样等长——不是那个 {"text":…, "truncated":…} 对象
        self.assertIsInstance(content, str)
        self.assertEqual(len(content), 40_000)

        # 条数同样不设上限：1600 条全都在，且顺序与内容都对得上
        many = [Message(role="user", content=str(i)) for i in range(1600)]
        list(p.stream_chat(many))
        msgs_field = self.records()[2]["messages"]
        self.assertIsInstance(msgs_field, list)
        self.assertEqual(len(msgs_field), 1600)
        # **尾部必须在**：旧实现保留头部丢尾部，而离当前最近的那几条
        # 恰恰是最可能解释当前行为的（这正是删掉上限的直接理由）
        self.assertEqual(msgs_field[-1]["content"], "1599")

    def test_system_prompt_recorded_in_full(self) -> None:
        """
        系统提示不截断——**这是删掉阈值的头号动因**。

        实测：结构化系统提示在最小配置下已有 3886 字符，贴着旧的 4000 线；
        而稳定通道按「越稳定越靠前」排序，尾部依次是 134 组队协作 /
        135 角色清单 / 140 Skill 清单。旧阈值下，任何一份真实的 RHINE.md
        一进来，被切掉的**正好是 C15 / C13 / C11 那三段清单**——
        也就是「模型到底看没看到这个能力」唯一的证据。

        用一段带尾部哨兵的长提示钉住：哨兵在 4000 之后，能读到它
        就说明尾部没被切。
        """
        sentinel = "【尾部哨兵·组队协作清单】"
        system = "S" * 9000 + sentinel
        inner = FakeProvider([StreamChunk(type="done")])
        p = TracingProvider(inner, self.rec, model="m")
        list(p.stream_chat([Message(role="user", content="hi")], system=system))

        recorded = self.records()[0]["system"]
        self.assertIsInstance(recorded, str)
        self.assertTrue(
            recorded.endswith(sentinel),
            "系统提示的尾部被切掉了——三个清单槽位恰好排在那里",
        )
        self.assertEqual(len(recorded), len(system))


if __name__ == "__main__":
    unittest.main()
