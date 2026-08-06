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

    def test_all_types_registered(self) -> None:
        """
        成对维护点的正向守卫：**全部**事件类型在摘要表里一个都不少。

        （原名写死了「十五类」，c12 加两类、c13 再加两类之后名字就过期了。
        判据本身一直是遍历枚举，与数量无关，故只改名不改逻辑。）
        """
        missing = [t.value for t in TraceEventType if t.value not in reader.SUMMARIZERS]
        self.assertEqual(missing, [], f"这些类型缺摘要函数：{missing}")

    def test_subagent_summaries(self) -> None:
        """
        c13 两类事件的摘要要点：起始摘出角色与任务，结束摘出轮次与用量。

        断言的是**关键字段出现在摘要里**而不是逐字固化整行——措辞可以调，
        但「读一眼就知道是谁、跑了多少轮」这件事不能丢。

        顺带钉住摘要必须是**单行**：`_text_of` 把换行渲染成 `⏎`，
        这是全项目统一口径。真换行会把时间线的「一事件一行」结构冲散。
        """
        start = reader.summarize(
            {
                "type": "subagent_start",
                "kind": "role",
                "agent": "explorer",
                "task_id": "a3f1c9",
                "tool_count": 3,
                "task": "找出所有实现了 Tool 抽象的文件\n第二行",
            }
        )
        self.assertIn("explorer", start)
        self.assertIn("a3f1c9", start)
        self.assertIn("找出所有实现了", start)
        self.assertNotIn("\n", start)

        end = reader.summarize(
            {
                "type": "subagent_end",
                "task_id": "a3f1c9",
                "status": "completed",
                "turns": 4,
                "usage_tokens": 1234,
                "stop_reason": "completed",
            }
        )
        self.assertIn("a3f1c9", end)
        self.assertIn("4 轮", end)
        self.assertIn("1234", end)

    def test_malformed_record_does_not_crash_summary(self) -> None:
        """一条畸形记录只影响它自己那行摘要，不让整个阅读器失败。"""
        text = self.path.read_text(encoding="utf-8")
        # api_request 缺 messages 字段
        self.path.write_text(text + _line(7, "api_request") + "\n", encoding="utf-8")
        code, out, _ = self.run_reader()
        self.assertEqual(code, 0)
        self.assertIn("api_request", out)


class ChineseEncodingTest(ReaderTestBase):
    """
    N8：中文不乱码，**从落盘到阅读器输出全程**。

    本模块的立项理由之一就是一个中文编码 bug——落盘或读取环节把编码搞坏，
    会直接毁掉这类问题的证据，而且失败形态是「看起来在正常工作」。
    """

    def test_written_and_read_back_intact(self) -> None:
        text = "第一行中文\n第二行：符号 ①②③ 与 emoji 🐛"
        self.path.write_text(
            _line(1, "tool_execute", tool="read_file", ok=True, outcome="executed",
                  summary="读了中文文件", output=text) + "\n",
            encoding="utf-8",
        )
        # 严格 UTF-8 解码，编码坏了这里就抛
        raw = self.path.read_bytes().decode("utf-8")
        self.assertNotIn(chr(92) + "u", raw, "中文被转义成 \\uXXXX，人眼无法直接阅读")
        self.assertIn("第一行中文", raw)

        _, out, _ = self.run_reader()
        self.assertIn("读了中文文件", out)

    def test_detail_mode_prints_chinese_intact(self) -> None:
        _, out, _ = self.run_reader("--seq", "2")
        self.assertIn("帮我看看代码", out)


class NonUtf8ConsoleTest(ReaderTestBase):
    """
    真实现场重演：Windows 控制台（代码页 GBK）读一份含 emoji 的记录。

    记录里的 `ui_message` 正文天然带 emoji（🔄「第 N 轮」、📦「已存盘」），
    而整条时间线是**一次性** print 出去的——GBK 编不了其中任何一个字符，
    就会让**整份记录一行都读不出来**，而不是少显示一个字符。

    现场报错（`G:\\Rhine-test\\web-tool-test` 的记录，2026-07-29）：
        UnicodeEncodeError: 'gbk' codec can't encode character '\\U0001f504'
    """

    def _gbk_console(self) -> tuple[io.TextIOWrapper, io.BytesIO]:
        """造一个行为等价于 GBK 控制台的输出流（errors 严格，编不了就抛）。"""
        buf = io.BytesIO()
        return io.TextIOWrapper(buf, encoding="gbk", errors="strict", newline=""), buf

    def _write_emoji_record(self) -> None:
        self.path.write_text(
            _line(1, "ui_message", source="system", text="🔄 第 2 轮") + "\n"
            + _line(2, "ui_message", source="system", text="📦 已把 1 个大型工具结果存盘") + "\n",
            encoding="utf-8",
        )

    def test_emoji_readable_on_gbk_console(self) -> None:
        """修复后的正向断言：GBK 控制台下能读完，非 emoji 部分逐字保留。"""
        self._write_emoji_record()
        stream, buf = self._gbk_console()
        with redirect_stdout(stream), redirect_stderr(io.StringIO()):
            code = reader.main([str(self.path)])
            stream.flush()

        self.assertEqual(code, 0)
        text = buf.getvalue().decode("gbk")
        # emoji 退化成占位符是可以接受的；「读不出来」才是缺陷。
        self.assertIn("第 2 轮", text)
        self.assertIn("已把 1 个大型工具结果存盘", text)
        self.assertIn("共 2 条事件", text)

    def test_strict_gbk_would_have_crashed(self) -> None:
        """
        反证：不做 `errors="replace"` 放宽时，同样的内容确实会把 print 打死。

        没有这条，上面那条正向断言无法区分「修复生效」与「本来就不会崩」。
        """
        stream, _ = self._gbk_console()
        with self.assertRaises(UnicodeEncodeError):
            stream.write("🔄 第 2 轮")
            stream.flush()

    def test_detail_mode_also_relaxed(self) -> None:
        """--seq 展开走的是另一条 print 路径，同样必须挺过 GBK 控制台。"""
        self._write_emoji_record()
        stream, buf = self._gbk_console()
        with redirect_stdout(stream), redirect_stderr(io.StringIO()):
            code = reader.main([str(self.path), "--seq", "2"])
            stream.flush()

        self.assertEqual(code, 0)
        self.assertIn("已把 1 个大型工具结果存盘", buf.getvalue().decode("gbk"))

    def test_relax_tolerates_stream_without_reconfigure(self) -> None:
        """
        观测设施不得因自我保护动作失败而阻断读取：
        流是 StringIO（无 `reconfigure`）时静默跳过，不抛。
        """
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            reader._relax_stdio_encoding()  # 不抛即通过


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


class LayerNamesConsistencyTest(unittest.TestCase):
    """
    `_LAYER_NAMES` ↔ `permission.models.Layer` 的一致性护栏（web_fetch 扩展 T10/T25）。

    ## 为什么是「两份表 + 一条测试」而不是「合并成一份」

    直觉上该让 `reader.py` 直接 import `Layer`、把两处合一，少一个成对维护点。
    实测的后果是——`permission/__init__.py` re-export 了 `PermissionEngine`，
    所以**导入任何一个子模块都会先执行包 `__init__`**，连带拉起整个 `permission`
    包 + `rhinecode.tools` + **`yaml`**。而 `trace/__init__.py` 与 `CLAUDE.md`
    都写着「trace 是只依赖标准库的叶子包」。

    拿一条硬架构不变量去换一个「漏改只显示英文原名」的软维护点，是净亏。
    所以两份表各留在自己的包里，靠这条测试钉住不漂移——**测试代码不受叶子包约束**，
    它 import 谁都行。

    效果上比合并还好一点：合并只能保证「取值一致」，这条还能保证「不漏行」。
    """

    def test_every_layer_has_a_chinese_name(self) -> None:
        from rhinecode.permission.models import Layer
        from rhinecode.trace.reader import _LAYER_NAMES

        for member in Layer:
            self.assertIn(
                member.value,
                _LAYER_NAMES,
                f"Layer.{member.name} 没有对应的中文名——"
                f"新增枚举值时要在 reader.py 的 _LAYER_NAMES 补一行",
            )

    def test_no_stale_entries(self) -> None:
        from rhinecode.permission.models import Layer
        from rhinecode.trace.reader import _LAYER_NAMES

        known = {m.value for m in Layer}
        for key in _LAYER_NAMES:
            self.assertIn(key, known, f"_LAYER_NAMES 里的 {key!r} 已不是合法的 Layer 取值")

    def test_network_layer_renders_in_chinese(self) -> None:
        """新增的②′层在阅读器里显示中文名而非英文原名。"""
        from rhinecode.trace.reader import summarize

        line = summarize(
            {
                "type": "permission_decision",
                "tool": "web_fetch",
                "decision": "deny",
                "layer": "network",
                "reason": "网络边界拒绝：不允许访问非公网地址",
            }
        )
        self.assertIn("②′网络边界", line)
        self.assertNotIn("（network）", line)


class WebExtractScopeTest(unittest.TestCase):
    """抽取作用域常量已登记（web_fetch 扩展 T10，spec F23）。"""

    def test_exported_from_package(self) -> None:
        from rhinecode.trace import SCOPE_WEB_EXTRACT

        self.assertEqual(SCOPE_WEB_EXTRACT, "web_extract")

    def test_distinct_from_other_scopes(self) -> None:
        from rhinecode.trace import SCOPE_MAIN, SCOPE_NOTES, SCOPE_SUMMARY, SCOPE_WEB_EXTRACT

        scopes = {SCOPE_MAIN, SCOPE_SUMMARY, SCOPE_NOTES, SCOPE_WEB_EXTRACT}
        self.assertEqual(len(scopes), 4, "作用域取值必须互不相同，否则 --scope 过滤会串")


if __name__ == "__main__":
    unittest.main()
