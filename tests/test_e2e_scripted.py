"""
脚本化假模型测试（P1a T24）。

覆盖：四类数据块的构造、按轮次作答、剧本耗尽的兜底、`RecordedCall` 的两个取值属性
（含「动态提醒不在 system 参数里」这条最容易写错的口径）、并发追加不丢不撕裂。
"""

from __future__ import annotations

import threading
import unittest

from rhinecode.provider.base import Message
from tests.e2e import scripted
from tests.e2e.scripted import (
    FALLBACK_MARKER,
    CountingProviderFactory,
    ScriptedProvider,
    done,
    stream_error,
    text,
    thinking,
    tool,
    usage,
)


class ChunkBuilderTest(unittest.TestCase):
    def test_text_and_thinking(self):
        self.assertEqual(text("你好").type, "text")
        self.assertEqual(text("你好").content, "你好")
        self.assertEqual(thinking("想一想").type, "thinking")

    def test_tool_chunk(self):
        chunk = tool("read_file", {"path": "a.py"})
        self.assertEqual(chunk.type, "tool_call")
        self.assertEqual(chunk.tool_call.name, "read_file")
        self.assertEqual(chunk.tool_call.arguments, {"path": "a.py"})
        self.assertTrue(chunk.tool_call.id, "call_id 不能为空，结果回灌靠它配对")

    def test_tool_call_ids_are_unique_by_default(self):
        # 同一轮发多个工具调用时 id 必须各不相同，否则结果配对会错乱
        ids = {tool("read_file").tool_call.id for _ in range(5)}
        self.assertEqual(len(ids), 5)

    def test_tool_explicit_call_id(self):
        self.assertEqual(tool("x", call_id="fixed").tool_call.id, "fixed")

    def test_stream_error_and_done(self):
        self.assertEqual(stream_error("连接中断").type, "error")
        self.assertEqual(stream_error("连接中断").content, "连接中断")
        self.assertEqual(done().type, "done")

    def test_usage_chunk(self):
        chunk = usage(prompt=100, completion=20)
        self.assertEqual(chunk.type, "usage")
        self.assertEqual(chunk.usage["prompt_tokens"], 100)
        self.assertEqual(chunk.usage["total_tokens"], 120)


class ScriptedProviderTest(unittest.TestCase):
    def test_answers_by_turn(self):
        provider = ScriptedProvider(
            [
                [text("第一轮"), tool("read_file", {"path": "a"}), done()],
                [text("第二轮"), done()],
            ]
        )
        first = list(provider.stream_chat([Message(role="user", content="hi")]))
        second = list(provider.stream_chat([Message(role="user", content="hi")]))

        self.assertEqual([c.type for c in first], ["text", "tool_call", "done"])
        self.assertEqual([c.type for c in second], ["text", "done"])
        self.assertEqual(first[0].content, "第一轮")
        self.assertEqual(second[0].content, "第二轮")

    def test_fallback_when_script_exhausted(self):
        """
        剧本耗尽必须**不抛错、不挂起**：上下文摘要与自动记忆都会额外调模型，
        为它们抛错等于把一次正常的系统行为变成测试失败。
        """
        provider = ScriptedProvider([[text("唯一一轮"), done()]])
        list(provider.stream_chat([]))
        third = list(provider.stream_chat([]))
        fourth = list(provider.stream_chat([]))

        for chunks in (third, fourth):
            self.assertEqual([c.type for c in chunks], ["text", "done"])
            self.assertIn(FALLBACK_MARKER, chunks[0].content)

    def test_custom_fallback(self):
        provider = ScriptedProvider([], fallback=[text("自定义兜底"), done()])
        chunks = list(provider.stream_chat([]))
        self.assertEqual(chunks[0].content, "自定义兜底")

    def test_empty_script_uses_fallback_immediately(self):
        provider = ScriptedProvider()
        self.assertIn(FALLBACK_MARKER, list(provider.stream_chat([]))[0].content)


class RecordedCallTest(unittest.TestCase):
    def test_records_all_parameters(self):
        provider = ScriptedProvider([[done()]])
        schemas = [{"type": "function", "function": {"name": "read_file", "parameters": {}}}]
        msgs = [Message(role="user", content="读一下")]
        list(provider.stream_chat(msgs, thinking_effort="high", tools=schemas, system="稳定提示"))

        calls = provider.calls
        self.assertEqual(len(calls), 1)
        call = calls[0]
        self.assertEqual(call.index, 0)
        self.assertEqual(call.system, "稳定提示")
        self.assertEqual(call.thinking_effort, "high")
        self.assertIs(call.messages, msgs, "消息列表原样引用，不截断不拷贝")

    def test_tool_names(self):
        provider = ScriptedProvider([[done()], [done()]])
        schemas = [
            {"type": "function", "function": {"name": "read_file"}},
            {"type": "function", "function": {"name": "glob_files"}},
        ]
        list(provider.stream_chat([], tools=schemas))
        list(provider.stream_chat([], tools=None))

        self.assertEqual(provider.calls[0].tool_names, {"read_file", "glob_files"})
        # tools=None 是「本轮禁用工具」（摘要/记忆的调用就是这样），空集而不是报错
        self.assertEqual(provider.calls[1].tool_names, set())

    def test_dynamic_reminder_reads_last_system_message(self):
        """
        ⚠️ 动态提醒（含已激活 Skill 的 SOP 正文）在**消息列表末条 system 消息**里，
        不在 `system` 参数里。这条口径写错会得到永远失败的断言。
        """
        provider = ScriptedProvider([[done()], [done()]])
        with_reminder = [
            Message(role="user", content="干活"),
            Message(role="system", content="<system-reminder>已激活 Skill：review</system-reminder>"),
        ]
        without_reminder = [Message(role="user", content="干活")]

        list(provider.stream_chat(with_reminder, system="稳定段"))
        list(provider.stream_chat(without_reminder, system="稳定段"))

        self.assertIn("review", provider.calls[0].dynamic_reminder)
        # 稳定段与动态段是两条通道，不能混
        self.assertNotIn("review", provider.calls[0].system or "")
        self.assertEqual(provider.calls[1].dynamic_reminder, "", "末条不是 system 时返回空串")

    def test_dynamic_reminder_on_empty_history(self):
        provider = ScriptedProvider([[done()]])
        list(provider.stream_chat([]))
        self.assertEqual(provider.calls[0].dynamic_reminder, "")

    def test_calls_returns_a_copy(self):
        provider = ScriptedProvider([[done()]])
        list(provider.stream_chat([]))
        snapshot = provider.calls
        snapshot.clear()
        self.assertEqual(len(provider.calls), 1, "外部误改快照不得污染真实记录")


class ConcurrencyTest(unittest.TestCase):
    def test_concurrent_calls_are_all_recorded(self):
        """
        Agent 的并发只读工具桶会让多个线程同时走到模型调用路径附近。
        断言 `len(calls)` **精确等于**总次数——不丢失、不撕裂。
        """
        threads_n, per_thread = 8, 40
        provider = ScriptedProvider()

        def worker():
            for _ in range(per_thread):
                list(provider.stream_chat([Message(role="user", content="x")]))

        threads = [threading.Thread(target=worker) for _ in range(threads_n)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)
            self.assertFalse(t.is_alive(), "工作线程未在上限内结束")

        calls = provider.calls
        self.assertEqual(len(calls), threads_n * per_thread)
        # index 必须是 0..N-1 的完整集合（无重号、无跳号）
        self.assertEqual({c.index for c in calls}, set(range(threads_n * per_thread)))


class CountingFactoryTest(unittest.TestCase):
    def test_counts_and_routes_by_model(self):
        main = ScriptedProvider()
        other = ScriptedProvider()
        factory = CountingProviderFactory(main, per_model={"other-model": other})

        class Cfg:
            model = "deepseek-chat"

        class Cfg2:
            model = "other-model"

        self.assertIs(factory(Cfg()), main)
        self.assertIs(factory(Cfg2()), other)
        self.assertEqual(factory.created, ["deepseek-chat", "other-model"])


if __name__ == "__main__":
    unittest.main()
