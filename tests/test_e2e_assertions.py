"""
断言层测试（P1a T30）。

覆盖：
- **AC36** 十一项断言词汇各自「通过一次 / 失败一次」
- **AC37** 失败诊断含证据序号与邻域整行（且整行确实来自 `render_timeline`）
- **AC38** 复用 `trace.reader` —— 坏行计数与 reader 一致，且源码护栏禁止自行解析记录
- **AC35** 类型 + 作用域组合过滤逐步收窄

用手工造的小记录文件做输入：这样每一条断言的输入都是确定的，
不必先跑起一整个应用。
"""

from __future__ import annotations

import inspect
import json
import re
import tempfile
import unittest
from pathlib import Path

from rhinecode.trace import reader
from tests.e2e import assertions
from tests.e2e.assertions import (
    CheckResult,
    TraceView,
    assert_check,
    check_count,
    check_dynamic_reminder,
    check_history_len,
    check_order,
    check_permission,
    check_scope,
    check_status_bar,
    check_stable_prompt,
    check_tool_outcome,
    check_tools_offered,
    check_ui_contains,
)
from tests.e2e.scripted import ScriptedProvider, done
from rhinecode.provider.base import Message


def make_records() -> list[dict]:
    """一份覆盖各类事件的小记录。seq 连续，作用域含 main 与 isolated:review 两种。"""
    return [
        {"seq": 1, "ts": "2026-07-27T10:00:00.000", "scope": "main", "type": "session_start",
         "config": {"protocol": "deepseek", "model": "deepseek-chat"}, "tool_names": ["read_file"]},
        {"seq": 2, "ts": "2026-07-27T10:00:01.000", "scope": "main", "type": "user_input",
         "kind": "message", "text": "看一下 a.py"},
        {"seq": 3, "ts": "2026-07-27T10:00:01.100", "scope": "main", "type": "api_request",
         "turn": 1, "model": "deepseek-chat", "tool_names": ["read_file"]},
        {"seq": 4, "ts": "2026-07-27T10:00:02.000", "scope": "main", "type": "permission_decision",
         "tool": "read_file", "decision": "allow", "layer": "rule", "reason": "只读工具"},
        {"seq": 5, "ts": "2026-07-27T10:00:02.100", "scope": "main", "type": "tool_execute",
         "tool": "read_file", "outcome": "executed", "ok": True, "duration_ms": 12},
        {"seq": 6, "ts": "2026-07-27T10:00:02.200", "scope": "main", "type": "permission_decision",
         "tool": "run_command", "decision": "deny", "layer": "blacklist", "reason": "危险命令"},
        {"seq": 7, "ts": "2026-07-27T10:00:02.300", "scope": "main", "type": "tool_execute",
         "tool": "run_command", "outcome": "denied_by_permission", "ok": False, "duration_ms": 0},
        {"seq": 8, "ts": "2026-07-27T10:00:03.000", "scope": "main", "type": "ui_message",
         "source": "assistant", "text": "这个文件里定义了三个函数。"},
        {"seq": 9, "ts": "2026-07-27T10:00:03.100", "scope": "main", "type": "status_bar",
         "text": "上下文：19% · 12.3K/64K  Skill:1  \\[DEFAULT]"},
        {"seq": 10, "ts": "2026-07-27T10:00:04.000", "scope": "isolated:review", "type": "api_request",
         "turn": 1, "model": "deepseek-chat", "tool_names": ["read_file"]},
        {"seq": 11, "ts": "2026-07-27T10:00:05.000", "scope": "isolated:review", "type": "tool_execute",
         "tool": "glob_files", "outcome": "out_of_scope", "ok": True, "duration_ms": 0},
        # 被 clip 截断过的字段：断言层必须能从这种结构里取出文本
        {"seq": 12, "ts": "2026-07-27T10:00:06.000", "scope": "main", "type": "ui_message",
         "source": "assistant",
         "text": {"text": "很长的正文开头 中文也要能搜到", "truncated": True, "original_length": 99999}},
    ]


class TraceViewTestBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="rhine_e2e_assert_")
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.trace_path = self.root / "t.jsonl"
        self.write_records(make_records())

    def write_records(self, records, extra_lines=()):
        lines = [json.dumps(r, ensure_ascii=False) for r in records]
        lines.extend(extra_lines)
        self.trace_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    @property
    def view(self) -> TraceView:
        return TraceView.load(self.trace_path)


class LoadAndFilterTest(TraceViewTestBase):
    """AC35：类型 + 作用域组合后条数逐步收窄。"""

    def test_progressive_narrowing(self):
        view = self.view
        self.assertEqual(len(view.records), 12)
        self.assertEqual(len(view.of_type("tool_execute")), 3)
        self.assertEqual(len(view.in_scope("main").records), 10)
        self.assertEqual(len(view.in_scope("main").of_type("tool_execute")), 2)
        self.assertEqual(len(view.in_scope("isolated:review").of_type("tool_execute")), 1)

    def test_of_type_union(self):
        self.assertEqual(len(self.view.of_type("tool_execute", "permission_decision")), 5)

    def test_by_seq_and_nth(self):
        view = self.view
        self.assertEqual(view.by_seq(5)["tool"], "read_file")
        self.assertEqual(view.nth("tool_execute", 1)["tool"], "run_command")
        with self.assertRaises(LookupError):
            view.by_seq(999)
        with self.assertRaises(LookupError) as ctx:
            view.nth("tool_execute", 9)
        self.assertIn("只有 3 条", str(ctx.exception))

    def test_missing_file_raises(self):
        # 记录没产出本身就是重要结论，不能静默返回空视图
        with self.assertRaises(FileNotFoundError):
            TraceView.load(self.root / "nope.jsonl")


class ReuseReaderTest(TraceViewTestBase):
    """AC38：复用 reader，且源码层面禁止自行解析记录。"""

    def test_skipped_matches_reader(self):
        self.write_records(make_records(), extra_lines=["{这不是 JSON", "12345"])
        view = self.view
        _, reader_skipped = reader.load_records(self.trace_path)
        self.assertEqual(view.skipped, reader_skipped)
        self.assertEqual(view.skipped, 2, "一行坏 JSON + 一行非对象")
        self.assertEqual(len(view.records), 12, "坏行不该影响正常记录")

    def test_source_guard_no_manual_json_parsing(self):
        """
        **硬护栏**：`assertions.py` 里除 `check_history_len`（它读的是会话存档）之外
        不得出现 `json.loads`。防止后来者绕过 reader 自己解析记录，
        两边解析口径一漂移，断言层报告的东西就与阅读器对不上了。
        """
        source = inspect.getsource(assertions)
        history_fn_source = inspect.getsource(assertions.check_history_len)
        # 排除两处：① 被豁免的那个函数；② 模块 docstring —— 它里面那句
        # 「绝不自行 json.loads 记录文件」是**解释护栏本身的散文**，
        # 护栏管的是代码不是文字，把说明文字算成违规会让这条护栏无法成立。
        rest = source.replace(history_fn_source, "").replace(assertions.__doc__ or "", "")
        self.assertIn("json.loads", history_fn_source, "被豁免的那个函数确实用了它")
        self.assertNotIn("json.loads", rest, "除 check_history_len 外不得自行解析")

    def test_view_delegates_to_reader(self):
        # 正向证据：load / filter 确实走的是 reader 的那两个函数
        src = inspect.getsource(TraceView)
        self.assertIn("reader.load_records", src)
        self.assertIn("reader.filter_records", src)


class RecordVocabularyTest(TraceViewTestBase):
    """AC36：取自记录的七项词汇，各一次通过、各一次失败。"""

    def test_tool_outcome(self):
        view = self.view
        self.assertTrue(check_tool_outcome(view, "read_file", "executed").ok)
        self.assertTrue(check_tool_outcome(view, "run_command", "denied_by_permission").ok)
        self.assertTrue(check_tool_outcome(view, "glob_files", "out_of_scope").ok)

        bad = check_tool_outcome(view, "read_file", "denied_by_user")
        self.assertFalse(bad.ok)
        self.assertIn("executed", bad.message)
        self.assertTrue(bad.evidence_seqs)

        missing = check_tool_outcome(view, "write_file", "executed")
        self.assertFalse(missing.ok)
        self.assertIn("read_file", missing.message, "要列出实际出现过的工具，省一轮排查")

    def test_ui_contains(self):
        view = self.view
        self.assertTrue(check_ui_contains(view, "三个函数").ok)
        self.assertTrue(check_ui_contains(view, "三个函数", source="assistant").ok)
        # 被 clip 截断过的字段也必须能搜到
        self.assertTrue(check_ui_contains(view, "中文也要能搜到").ok)

        bad = check_ui_contains(view, "根本没说过的话")
        self.assertFalse(bad.ok)
        self.assertTrue(bad.evidence_seqs)
        self.assertFalse(check_ui_contains(view, "三个函数", source="user_echo").ok)

    def test_status_bar_both_directions(self):
        view = self.view
        self.assertTrue(check_status_bar(view, "Skill:1").ok)
        self.assertTrue(check_status_bar(view, "Skill:9", present=False).ok)

        self.assertFalse(check_status_bar(view, "Skill:9").ok)
        bad = check_status_bar(view, "Skill:1", present=False)
        self.assertFalse(bad.ok)
        self.assertIn("不应出现", bad.message)

    def test_permission(self):
        view = self.view
        self.assertTrue(check_permission(view, "read_file", layer="rule", decision="allow").ok)
        # 「不扩大权限面」那组护栏读的就是这个：黑名单层必须仍然拦得住
        self.assertTrue(check_permission(view, "run_command", layer="blacklist", decision="deny").ok)
        self.assertTrue(check_permission(view, "run_command").ok, "只给工具名也应成立")

        bad = check_permission(view, "run_command", layer="mode")
        self.assertFalse(bad.ok)
        self.assertIn("blacklist", bad.message)
        self.assertFalse(check_permission(view, "write_file").ok)

    def test_scope(self):
        view = self.view
        self.assertTrue(check_scope(view, 10, "isolated:review").ok)
        bad = check_scope(view, 10, "main")
        self.assertFalse(bad.ok)
        self.assertIn("isolated:review", bad.message)
        self.assertFalse(check_scope(view, 999, "main").ok)

    def test_order(self):
        view = self.view
        self.assertTrue(check_order(view, 4, 5).ok)
        bad = check_order(view, 5, 4)
        self.assertFalse(bad.ok)
        self.assertEqual(bad.evidence_seqs, (5, 4))
        self.assertFalse(check_order(view, 5, 5).ok, "相等不算先后确定")

    def test_count(self):
        view = self.view
        self.assertTrue(check_count(view, "tool_execute", 3).ok)
        self.assertTrue(check_count(view, "history_restored", 0).ok)
        bad = check_count(view, "tool_execute", 2)
        self.assertFalse(bad.ok)
        self.assertIn("实际 3 条", bad.message)


class HistoryLenTest(TraceViewTestBase):
    """词汇⑨：取自会话存档而不是记录。"""

    def _write_session(self, rows, name="20260727-100000-ab12.jsonl"):
        sessions = self.root / "sessions"
        sessions.mkdir(exist_ok=True)
        path = sessions / name
        path.write_text(
            "\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n", encoding="utf-8"
        )
        return sessions

    def test_counts_all_and_by_role(self):
        sessions = self._write_session([
            {"role": "user", "content": "第一个请求"},
            {"role": "assistant", "content": "好的"},
            {"role": "user", "content": "第二个请求"},
            {"role": "assistant", "content": "完成"},
        ])
        self.assertTrue(check_history_len(sessions, 4).ok)
        self.assertTrue(check_history_len(sessions, 2, role="user").ok)
        bad = check_history_len(sessions, 3, role="user")
        self.assertFalse(bad.ok)
        self.assertIn("实际 2 条", bad.message)

    def test_skips_bad_lines(self):
        sessions = self._write_session([{"role": "user", "content": "x"}])
        latest = next(iter(sessions.glob("*.jsonl")))
        with latest.open("a", encoding="utf-8") as fh:
            fh.write("{半截\n")
            fh.write('{"no_role": 1}\n')
            fh.write("[1,2,3]\n")
        self.assertTrue(check_history_len(sessions, 1).ok)

    def test_missing_dir(self):
        bad = check_history_len(self.root / "no_sessions", 1)
        self.assertFalse(bad.ok)
        self.assertIn("没有任何会话存档", bad.message)


class ProviderVocabularyTest(unittest.TestCase):
    """AC36 的另一半：取自假模型的三项词汇。"""

    def setUp(self):
        self.provider = ScriptedProvider([[done()], [done()]])
        schemas = [
            {"type": "function", "function": {"name": "read_file"}},
            {"type": "function", "function": {"name": "glob_files"}},
        ]
        # 第 0 轮：无动态提醒
        list(self.provider.stream_chat(
            [Message(role="user", content="干活")], tools=schemas, system="你是 RhineCode。"
        ))
        # 第 1 轮：末条是 system，SOP 正文在这里
        list(self.provider.stream_chat(
            [
                Message(role="user", content="干活"),
                Message(role="system", content="<system-reminder>SOP：先跑测试</system-reminder>"),
            ],
            tools=[schemas[0]],
            system="你是 RhineCode。",
        ))

    def test_tools_offered(self):
        self.assertTrue(check_tools_offered(self.provider, 0, {"read_file", "glob_files"}).ok)
        self.assertTrue(check_tools_offered(self.provider, 1, {"read_file"}).ok)

        bad = check_tools_offered(self.provider, 1, {"read_file", "glob_files"})
        self.assertFalse(bad.ok)
        self.assertIn("缺少", bad.message)
        out_of_range = check_tools_offered(self.provider, 9, set())
        self.assertFalse(out_of_range.ok)
        self.assertIn("只被调用了 2 次", out_of_range.message)

    def test_stable_prompt(self):
        self.assertTrue(check_stable_prompt(self.provider, 0, "你是 RhineCode").ok)
        self.assertTrue(check_stable_prompt(self.provider, 0, "先跑测试", present=False).ok,
                        "SOP 在动态段，稳定段里不该有")
        bad = check_stable_prompt(self.provider, 0, "不存在的片段")
        self.assertFalse(bad.ok)
        self.assertIn("第 0 轮", bad.message)

    def test_dynamic_reminder(self):
        """「第 N 轮激活、第 N+1 轮生效」这条判据的两次调用。"""
        self.assertTrue(check_dynamic_reminder(self.provider, 0, "先跑测试", present=False).ok)
        self.assertTrue(check_dynamic_reminder(self.provider, 1, "先跑测试").ok)

        bad = check_dynamic_reminder(self.provider, 0, "先跑测试")
        self.assertFalse(bad.ok)
        self.assertIn("动态提醒", bad.message)


class AssertCheckTest(TraceViewTestBase):
    """AC37：失败诊断含证据序号与邻域整行。"""

    def test_passing_check_returns_silently(self):
        self.assertIsNone(assert_check(CheckResult(True), self.view))

    def test_failure_message_contains_seq_and_timeline(self):
        view = self.view
        result = check_tool_outcome(view, "read_file", "denied_by_user")
        with self.assertRaises(AssertionError) as ctx:
            assert_check(result, view)
        msg = str(ctx.exception)

        self.assertIn("证据序号", msg)
        self.assertRegex(msg, r"证据序号：\[\d+", "必须能看到具体是哪一号")
        # 邻域整行必须来自 render_timeline —— 它带 scope 与 type 字段，
        # 而 summarize 只给一句话摘要（那两个恰恰是诊断时最先要看的）
        self.assertIn("main", msg, "整行里要有 scope")
        self.assertIn("tool_execute", msg, "整行里要有 type")
        self.assertIn("permission_decision", msg, "邻域要带上前后几条，而不只是命中那条")

    def test_neighborhood_is_deduped_and_sorted(self):
        view = self.view
        # 两个挨得很近的证据，邻域必然重叠；去重后不该出现重复行
        result = CheckResult(False, "造一个失败", (5, 6))
        with self.assertRaises(AssertionError) as ctx:
            assert_check(result, view)
        lines = [l for l in str(ctx.exception).splitlines() if re.match(r"^\s*\d+\s", l)]
        seqs = [int(l.split()[0]) for l in lines]
        self.assertEqual(seqs, sorted(seqs), "邻域按 seq 排序")
        self.assertEqual(len(seqs), len(set(seqs)), "重叠邻域必须去重")

    def test_failure_without_view_still_raises(self):
        # 词汇 ①⑩⑪ 没有证据序号，也没有 view 可传，但仍要能抛出可读的断言失败
        with self.assertRaises(AssertionError) as ctx:
            assert_check(CheckResult(False, "第 3 轮发出的工具集不对"))
        self.assertIn("第 3 轮", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
