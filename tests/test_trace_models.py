"""
Trace 纯函数单测（trace T9）：枚举齐备、截断约定、脱敏、循环事件白名单、缺省路径。

本文件只测 `trace/models.py` 的纯函数，不碰文件系统（`default_trace_path`
按设计**不创建目录**，故也无需临时目录）。
"""

import unittest
from pathlib import Path
from types import SimpleNamespace

from rhinecode.trace.models import (
    REDACTED,
    SCOPE_MAIN,
    TraceEventType,
    agent_event_payload,
    default_trace_path,
    full_text,
    isolated_scope,
    redact_config,
)


class EventTypeTest(unittest.TestCase):
    def test_member_count_and_snake_case_values(self) -> None:
        # 显式写死条数是刻意的：新增事件类型时这条会红，提醒去同步
        # `reader.SUMMARIZERS`、CLAUDE.md 与 docs/internals（漏了不报错）。
        # 十五类（c2–c11）+ c12 Hook 两类 + c13 子 Agent 两类 + c14 隔离工作区四类
        # + c15 协作四类 + tui-activity-fold 界面两类（批次归并 / 档位切换）
        # + c16 分类器一类 + todo-list 待办一类 + 续跑判定一类。
        members = list(TraceEventType)
        self.assertEqual(len(members), 32)
        for m in members:
            # 取值必须是成员名的小写形式：落盘的 type 字段与阅读器的 --type 参数直接比对
            self.assertEqual(m.value, m.name.lower())
            self.assertRegex(m.value, r"^[a-z_]+$")

    def test_str_enum_compares_with_plain_string(self) -> None:
        # 继承 str 的好处：json.dumps 与 == 都能直接用
        self.assertEqual(TraceEventType.USER_INPUT, "user_input")

    def test_docstring_count_matches_actual_members(self) -> None:
        """
        枚举的 docstring 里那个中文数字必须与真实成员数一致。

        **为什么值得为一句注释写测试**：这个数字漂移过两次——docstring 停在
        「十九类」、CLAUDE.md 停在「二十三类」，而实际已经是 27。
        它是纯文字、漏改不报错，于是每一章都往下带一次错。
        上面那条 `assertEqual(len(members), 27)` 只钉住数量，钉不住**说法**。

        读到这条用例失败时：改 `TraceEventType` 的 docstring，
        **并且**同步 `CLAUDE.md` 里「二十三类结构化事件」那句
        （那一处没有护栏，只能靠这里提醒）。
        """
        digits = "零一二三四五六七八九"
        n = len(list(TraceEventType))
        if n < 10:
            chinese = digits[n]
        elif n < 20:
            chinese = "十" + (digits[n % 10] if n % 10 else "")
        else:
            chinese = digits[n // 10] + "十" + (digits[n % 10] if n % 10 else "")

        doc = TraceEventType.__doc__ or ""
        self.assertIn(
            chinese,
            doc,
            f"枚举现有 {n} 个成员（中文写作「{chinese}」），"
            f"但 docstring 里没有这个数字——请同步 docstring 与 CLAUDE.md",
        )


class FullTextTest(unittest.TestCase):
    """
    `full_text` 的全部契约：**不截断**。

    这组用例是「trace 不做任何取舍」这条产品承诺在代码里的落点。
    它替代了原来的 `ClipTest`——那组用例逐条断言了截断行为
    （超限返回三字段对象、按字符切分不产生乱码……），现在**方向完全相反**。
    """

    def test_short_string_returned_as_is(self) -> None:
        self.assertEqual(full_text("ab"), "ab")

    def test_long_string_is_not_truncated_at_any_length(self) -> None:
        # 旧阈值是 4000。取一个远超它的长度，断言**一个字符都没少**、
        # 且返回的仍是裸字符串而不是「截断对象」。
        text = "a" * 100_000
        out = full_text(text)
        self.assertIsInstance(out, str)
        self.assertEqual(len(out), 100_000)
        self.assertEqual(out, text)

    def test_chinese_is_intact(self) -> None:
        # 中文一个字符占三字节，是最容易被字节级截断切坏的形态。
        text = "中文测试内容" * 5000
        out = full_text(text)
        self.assertEqual(out, text)
        # 能原样编解码，证明没有产生非法序列
        self.assertEqual(out.encode("utf-8").decode("utf-8"), text)

    def test_non_string_is_stringified(self) -> None:
        self.assertEqual(full_text({"a": 1}), str({"a": 1}))
        self.assertEqual(full_text(123), "123")

    def test_module_exposes_no_threshold_constants(self) -> None:
        """
        **反证**：旧的两个阈值常量必须真的不存在。

        只把 `full_text` 改成不截断、却把 `MAX_FIELD_CHARS` 留在原地，
        下一个人很容易「顺手」再用起来——而那是静默回归：
        记录看起来正常，只是又开始丢内容了。
        """
        import rhinecode.trace.models as m
        import rhinecode.trace as pkg

        for name in ("MAX_FIELD_CHARS", "MAX_MESSAGE_ITEMS", "clip"):
            self.assertFalse(
                hasattr(m, name), f"models 不应再有 {name}——trace 不做任何截断"
            )
            self.assertFalse(hasattr(pkg, name), f"trace 包不应再导出 {name}")


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
