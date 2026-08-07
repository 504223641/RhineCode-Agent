"""
Trace 纯函数单测（trace T9）：枚举齐备、截断约定、脱敏、循环事件白名单、缺省路径。

本文件只测 `trace/models.py` 的纯函数，不碰文件系统（`default_trace_path`
按设计**不创建目录**，故也无需临时目录）。
"""

import unittest
from pathlib import Path
from types import SimpleNamespace

from rhinecode.trace.models import (
    MAX_FIELD_CHARS,
    REDACTED,
    SCOPE_MAIN,
    TraceEventType,
    agent_event_payload,
    clip,
    default_trace_path,
    isolated_scope,
    redact_config,
)


class EventTypeTest(unittest.TestCase):
    def test_member_count_and_snake_case_values(self) -> None:
        # 显式写死条数是刻意的：新增事件类型时这条会红，提醒去同步
        # `reader.SUMMARIZERS`、CLAUDE.md 与 docs/internals（漏了不报错）。
        # 十五类（c2–c11）+ c12 Hook 两类 + c13 子 Agent 两类。
        members = list(TraceEventType)
        self.assertEqual(len(members), 19)
        for m in members:
            # 取值必须是成员名的小写形式：落盘的 type 字段与阅读器的 --type 参数直接比对
            self.assertEqual(m.value, m.name.lower())
            self.assertRegex(m.value, r"^[a-z_]+$")

    def test_str_enum_compares_with_plain_string(self) -> None:
        # 继承 str 的好处：json.dumps 与 == 都能直接用
        self.assertEqual(TraceEventType.USER_INPUT, "user_input")


class ClipTest(unittest.TestCase):
    def test_within_limit_returns_bare_string(self) -> None:
        # 未截断返回裸串（不是包了一层的对象）——阅读器与测试都依赖这条约定
        self.assertEqual(clip("ab", 5), "ab")
        self.assertEqual(clip("abcde", 5), "abcde")

    def test_over_limit_returns_three_fields(self) -> None:
        out = clip("a" * 10, 5)
        self.assertIsInstance(out, dict)
        self.assertEqual(out["text"], "aaaaa")
        self.assertIs(out["truncated"], True)
        self.assertEqual(out["original_length"], 10)

    def test_default_limit_is_max_field_chars(self) -> None:
        out = clip("x" * (MAX_FIELD_CHARS + 1))
        self.assertEqual(out["original_length"], MAX_FIELD_CHARS + 1)
        self.assertEqual(len(out["text"]), MAX_FIELD_CHARS)

    def test_chinese_truncated_by_character_without_mojibake(self) -> None:
        # 按字符切分，不会把一个中文字切成半个字节序列
        out = clip("中文测试内容", 3)
        self.assertEqual(out["text"], "中文测")
        self.assertEqual(out["original_length"], 6)
        # 能正常编解码，证明没有产生非法序列
        self.assertEqual(out["text"].encode("utf-8").decode("utf-8"), "中文测")

    def test_non_string_is_stringified(self) -> None:
        self.assertEqual(clip({"a": 1}), str({"a": 1}))
        self.assertEqual(clip(123), "123")


class RedactConfigTest(unittest.TestCase):
    def test_api_key_masked_other_fields_kept(self) -> None:
        cfg = SimpleNamespace(
            protocol="deepseek",
            model="deepseek-chat",
            base_url="https://api.example.com",
            api_key="sk-real-secret",
            debug_log=True,
            context_window=65536,
        )
        out = redact_config(cfg)
        self.assertEqual(out["api_key"], REDACTED)
        self.assertNotIn("sk-real-secret", str(out))
        self.assertEqual(out["protocol"], "deepseek")
        self.assertEqual(out["model"], "deepseek-chat")
        self.assertEqual(out["base_url"], "https://api.example.com")
        self.assertEqual(out["context_window"], 65536)

    def test_missing_fields_tolerated(self) -> None:
        # 容忍配置类字段增减：缺的字段记 None 而不是抛 AttributeError
        out = redact_config(SimpleNamespace(protocol="deepseek"))
        self.assertEqual(out["protocol"], "deepseek")
        self.assertIsNone(out["model"])
        self.assertIsNone(out["api_key"])

    def test_empty_api_key_still_masked(self) -> None:
        # 空串也记掩码，避免「没这个键」被误读成「没配 key」
        out = redact_config(SimpleNamespace(api_key=""))
        self.assertEqual(out["api_key"], REDACTED)


class AgentEventPayloadTest(unittest.TestCase):
    def test_text_only_length_no_body(self) -> None:
        ev = SimpleNamespace(type="text", text="一段很长的正文" * 10)
        out = agent_event_payload(ev)
        self.assertEqual(out["event_type"], "text")
        self.assertEqual(out["text_length"], len(ev.text))
        self.assertNotIn("text", out)

    def test_tool_name_without_arguments(self) -> None:
        ev = SimpleNamespace(
            type="tool_start",
            tool_call=SimpleNamespace(id="c1", name="read_file", arguments={"path": "x"}),
        )
        out = agent_event_payload(ev)
        self.assertEqual(out["tool_name"], "read_file")
        self.assertEqual(out["tool_call_id"], "c1")
        self.assertNotIn("arguments", out)
        self.assertNotIn("output", str(out))

    def test_tool_result_only_ok_flag(self) -> None:
        ev = SimpleNamespace(
            type="tool_result",
            tool_call=SimpleNamespace(id="c1", name="read_file", arguments=None),
            tool_result=SimpleNamespace(ok=True, output="一大坨输出" * 100),
        )
        out = agent_event_payload(ev)
        self.assertIs(out["result_ok"], True)
        self.assertNotIn("一大坨输出", str(out))

    def test_none_keys_omitted(self) -> None:
        ev = SimpleNamespace(type="progress", iteration=3)
        out = agent_event_payload(ev)
        self.assertEqual(out, {"event_type": "progress", "iteration": 3})

    def test_enum_like_values_unwrapped(self) -> None:
        ev = SimpleNamespace(
            type=SimpleNamespace(value="finished"),
            stop_reason=SimpleNamespace(value="completed"),
        )
        out = agent_event_payload(ev)
        self.assertEqual(out["event_type"], "finished")
        self.assertEqual(out["stop_reason"], "completed")

    def test_real_agent_event_accepted(self) -> None:
        # 拿真实事件类型跑一遍，确认 getattr 白名单与实际字段名对得上
        from rhinecode.agent.events import AgentEvent, AgentEventType, StopReason

        out = agent_event_payload(
            AgentEvent(type=AgentEventType.FINISHED, stop_reason=StopReason.COMPLETED)
        )
        self.assertEqual(out["event_type"], "finished")
        self.assertEqual(out["stop_reason"], "completed")


class ScopeAndPathTest(unittest.TestCase):
    def test_isolated_scope_format(self) -> None:
        self.assertEqual(isolated_scope("review"), "isolated:review")
        self.assertNotEqual(isolated_scope("review"), SCOPE_MAIN)

    def test_default_trace_path_shape_and_no_side_effect(self) -> None:
        root = Path("Z:/nonexistent-root")
        p = default_trace_path(root)
        self.assertEqual(p.parent, root / ".rhinecode" / "traces")
        self.assertEqual(p.suffix, ".jsonl")
        # 纯计算，不创建目录
        self.assertFalse(p.parent.exists())

    def test_stamp_carries_milliseconds(self) -> None:
        # AC25 的前置条件：同一秒内的两次运行必须算出不同文件名，否则追加模式
        # 会把两次运行写进同一个文件。做法是时间戳带毫秒段——这里断言形态。
        name = default_trace_path(Path("Z:/x")).name
        self.assertRegex(name, r"^\d{8}-\d{6}-\d{3}\.jsonl$")

    def test_calls_in_different_milliseconds_differ(self) -> None:
        # 真实场景是两次**进程启动**（间隔至少上百毫秒），这里只需证明
        # 毫秒段确实参与区分：隔几毫秒再取一次即不同名。
        import time

        root = Path("Z:/nonexistent-root")
        first = default_trace_path(root).name
        time.sleep(0.01)
        self.assertNotEqual(first, default_trace_path(root).name)


if __name__ == "__main__":
    unittest.main()
