"""
阅读器测试（trace T56）：摘要、过滤、展开、只读、容错、交付形态。

对应 spec AC33（不注册控制台入口）/ AC34（每事件一行）/ AC35（过滤）/
AC36（展开含原长标注）/ AC37（运行前后文件不变）。
"""

import hashlib
import io
import json
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr
from pathlib import Path

from rhinecode.trace import reader
from rhinecode.trace.models import MAX_FIELD_CHARS, TraceEventType, clip


def _line(seq: int, type_: str, scope: str = "main", **payload) -> str:
    rec = {"seq": seq, "ts": f"2026-01-01T00:00:0{seq % 10}.000", "type": type_, "scope": scope}
    rec.update(payload)
    return json.dumps(rec, ensure_ascii=False)


class ReaderTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "t.jsonl"
        self.path.write_text(
            "\n".join(
                [
                    _line(1, "session_start", project_root="/x", tool_names=["read_file"],
                          config={"protocol": "deepseek", "model": "m"}),
                    _line(2, "user_input", text="帮我看看代码", kind="message"),
                    _line(3, "api_request", turn=1, model="m", messages=[{"role": "user"}],
                          tool_names=["read_file"], thinking_effort="off"),
                    _line(4, "api_request", "summary", turn=1, model="m", messages=[],
                          tool_names=[], thinking_effort="off"),
                    _line(5, "tool_execute", tool="read_file", ok=True, outcome="executed",
                          duration_ms=12, is_concurrent=True, summary="读了 30 行",
                          output=clip("超长" * 5000)),
                    _line(6, "brand_new_type", foo=1),
                ]
            )
            + "\n",
            encoding="utf-8",
        )

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def run_reader(self, *args: str) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = reader.main([str(self.path), *args])
        return code, out.getvalue(), err.getvalue()


class TimelineTest(ReaderTestBase):
    def test_one_line_per_event_with_seq_and_type(self) -> None:
        """AC34：摘要模式每事件一行，含序号与类型。"""
        code, out, _ = self.run_reader()
        self.assertEqual(code, 0)
        body = [l for l in out.splitlines() if l.strip() and not l.startswith("共")]
        self.assertEqual(len(body), 6)
        for seq, expected_type in enumerate(
            ["session_start", "user_input", "api_request", "api_request",
             "tool_execute", "brand_new_type"],
            start=1,
        ):
            self.assertIn(str(seq), body[seq - 1])
            self.assertIn(expected_type, body[seq - 1])

    def test_summary_carries_key_info(self) -> None:
        _, out, _ = self.run_reader()
        self.assertIn("deepseek/m", out)          # session_start
        self.assertIn("帮我看看代码", out)          # user_input
        self.assertIn("read_file", out)            # tool_execute
        self.assertIn("executed", out)

    def test_unregistered_type_is_explicitly_marked(self) -> None:
        """
        未登记类型输出显式标记而非空白——它是「新增事件类型忘了登记摘要函数」
        的自检信号（成对维护点）。
        """
        _, out, _ = self.run_reader()
        self.assertIn(reader.UNREGISTERED, out)

    def test_all_fifteen_types_registered(self) -> None:
        """成对维护点的正向守卫：十五类事件在摘要表里一个都不少。"""
        missing = [t.value for t in TraceEventType if t.value not in reader.SUMMARIZERS]
        self.assertEqual(missing, [], f"这些类型缺摘要函数：{missing}")

    def test_malformed_record_does_not_crash_summary(self) -> None:
        """一条畸形记录只影响它自己那行摘要，不让整个阅读器失败。"""
        text = self.path.read_text(encoding="utf-8")
        # api_request 缺 messages 字段
        self.path.write_text(text + _line(7, "api_request") + "\n", encoding="utf-8")
        code, out, _ = self.run_reader()
        self.assertEqual(code, 0)
        self.assertIn("api_request", out)


class FilterTest(ReaderTestBase):
    def test_type_filter(self) -> None:
        _, out, _ = self.run_reader("--type", "api_request")
        body = [l for l in out.splitlines() if l.strip() and not l.startswith("共")]
        self.assertEqual(len(body), 2)

    def test_scope_filter(self) -> None:
        _, out, _ = self.run_reader("--scope", "summary")
        body = [l for l in out.splitlines() if l.strip() and not l.startswith("共")]
        self.assertEqual(len(body), 1)
        self.assertIn("summary", body[0])

    def test_combined_filters_intersect(self) -> None:
        """AC35：--type 与 --scope 同时给出时取交集。"""
        _, out, _ = self.run_reader("--type", "api_request", "--scope", "main")
        body = [l for l in out.splitlines() if l.strip() and not l.startswith("共")]
        self.assertEqual(len(body), 1)
        self.assertIn("   3 ", body[0] + " ")

    def test_multiple_values_are_comma_separated(self) -> None:
        _, out, _ = self.run_reader("--type", "user_input,tool_execute")
        body = [l for l in out.splitlines() if l.strip() and not l.startswith("共")]
        self.assertEqual(len(body), 2)

    def test_filter_matching_nothing_reports_zero(self) -> None:
        _, out, _ = self.run_reader("--type", "no_such_type")
        self.assertIn("共 0 条事件", out)


class DetailTest(ReaderTestBase):
    def test_seq_expands_full_payload(self) -> None:
        code, out, _ = self.run_reader("--seq", "5")
        self.assertEqual(code, 0)
        self.assertIn("tool_execute", out)
        self.assertIn("read_file", out)
        self.assertIn("duration_ms: 12", out)

    def test_truncated_field_shows_original_length(self) -> None:
        """AC36：被截断的字段必须显式标出原长——否则结论会完全不同。"""
        _, out, _ = self.run_reader("--seq", "5")
        self.assertIn("已截断，原长", out)
        self.assertIn(str(len("超长" * 5000)), out)

    def test_missing_seq_returns_error(self) -> None:
        code, _, err = self.run_reader("--seq", "999")
        self.assertEqual(code, 1)
        self.assertIn("999", err)


class ToleranceTest(ReaderTestBase):
    def test_bad_lines_skipped_and_counted(self) -> None:
        """坏行跳过并计入报告（与会话存档同口径）。"""
        text = self.path.read_text(encoding="utf-8")
        self.path.write_text(
            text + '{"seq": 7, "type": "user_i\n' + "not json at all\n", encoding="utf-8"
        )
        code, out, _ = self.run_reader()
        self.assertEqual(code, 0)
        self.assertIn("跳过 2 个坏行", out)

    def test_missing_file_reports_error(self) -> None:
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = reader.main([str(self.path.parent / "nope.jsonl")])
        self.assertEqual(code, 1)
        self.assertIn("找不到记录文件", err.getvalue())


class ReadOnlyTest(ReaderTestBase):
    def test_file_unchanged_after_all_modes(self) -> None:
        """AC37：运行前后文件哈希不变——记录是证据，读不该改它。"""
        before = hashlib.sha256(self.path.read_bytes()).hexdigest()
        self.run_reader()
        self.run_reader("--type", "api_request")
        self.run_reader("--scope", "main")
        self.run_reader("--seq", "3")
        self.run_reader("--seq", "999")
        after = hashlib.sha256(self.path.read_bytes()).hexdigest()
        self.assertEqual(before, after)


class DeliveryFormTest(unittest.TestCase):
    """AC33：随包分发，但**不注册控制台入口**。"""

    def test_pyproject_scripts_only_has_rhine(self) -> None:
        text = Path("pyproject.toml").read_text(encoding="utf-8")
        section = text.split("[project.scripts]", 1)[1].split("[", 1)[0]
        entries = [
            l.split("=")[0].strip()
            for l in section.splitlines()
            if "=" in l and not l.strip().startswith("#")
        ]
        self.assertEqual(entries, ["rhine"])

    def test_main_help_does_not_mention_reader(self) -> None:
        proc = subprocess.run(
            [sys.executable, "-m", "rhinecode", "--help"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=60,
        )
        self.assertEqual(proc.returncode, 0)
        self.assertNotIn("reader", proc.stdout)
        self.assertNotIn("阅读器", proc.stdout)
        # 但 --trace 必须在（那是本模块的开启入口）
        self.assertIn("--trace", proc.stdout)

    def test_module_is_runnable(self) -> None:
        """`python -m rhinecode.trace.reader` 可直接调用（交付形态的正向验证）。"""
        proc = subprocess.run(
            [sys.executable, "-m", "rhinecode.trace.reader", "--help"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=60,
        )
        self.assertEqual(proc.returncode, 0)
        self.assertIn("--seq", proc.stdout)


if __name__ == "__main__":
    unittest.main()
