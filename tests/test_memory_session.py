"""会话存档单测（c9 T8 / AC6/AC7/AC11/AC12/AC13/AC22 相关）。"""

import json
import os
import re
import time
import unittest
import tempfile
from pathlib import Path

from rhinecode.provider.base import Message, ToolCall
from rhinecode.memory.session import SessionStore, SESSION_LOCK_STALE
from rhinecode.memory import lockfile


def _age(path: Path, seconds: float) -> None:
    """把文件 mtime 改到 seconds 秒前。"""
    old = time.time() - seconds
    os.utime(str(path), times=(old, old))


class SessionStoreTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name) / "sessions"
        self.store = SessionStore(self.dir)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    # ------------------------------------------------------------------ #
    # 建档与追加（AC6）
    # ------------------------------------------------------------------ #
    def test_id_format_and_lazy_creation(self) -> None:
        sid = self.store.start_new()
        self.assertRegex(sid, r"^\d{8}-\d{6}-[a-z0-9]{4}$")
        # 惰性：start_new 后目录不存在/为空
        self.assertFalse(self.dir.exists())
        self.store.append(Message(role="user", content="hi"))
        self.assertTrue((self.dir / f"{sid}.jsonl").exists())
        self.assertTrue((self.dir / f"{sid}.lock").exists())

    def test_append_lines_and_roundtrip(self) -> None:
        sid = self.store.start_new()
        msgs = [
            Message(role="user", content="问题"),
            Message(
                role="assistant",
                content="",
                tool_calls=[ToolCall(id="c1", name="read_file", arguments={"path": "a.py"})],
            ),
            Message(role="tool", content="文件内容", tool_call_id="c1"),
            Message(role="assistant", content="答案"),
        ]
        for m in msgs:
            self.store.append(m)
        # 每行可独立解析且含 ts
        lines = (self.dir / f"{sid}.jsonl").read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(lines), 4)
        for line in lines:
            data = json.loads(line)
            self.assertIn("ts", data)
        # load 往返：字段一致（含 tool_calls）
        result = self.store.load(sid)
        self.assertEqual(result.skipped_lines, 0)
        self.assertEqual(len(result.messages), 4)
        self.assertEqual(result.messages[1].tool_calls[0].name, "read_file")
        self.assertEqual(result.messages[1].tool_calls[0].arguments, {"path": "a.py"})
        self.assertEqual(result.messages[2].tool_call_id, "c1")
        self.assertIsNotNone(result.last_time)

    def test_append_without_session_noop(self) -> None:
        """未 start_new 时 append 静默无操作（防御）。"""
        self.store.append(Message(role="user", content="x"))
        self.assertFalse(self.dir.exists())

    # ------------------------------------------------------------------ #
    # 容错载入（AC11）
    # ------------------------------------------------------------------ #
    def _write_archive(self, sid: str, lines: list[str]) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        (self.dir / f"{sid}.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")

    def test_bad_line_skipped(self) -> None:
        self._write_archive("20260101-000000-aaaa", [
            json.dumps({"ts": "2026-01-01T00:00:00", "role": "user", "content": "one"}),
            "{ 这不是合法 JSON",
            json.dumps({"role": "assistant", "content": "two"}),  # 缺 ts 也容忍
        ])
        result = self.store.load("20260101-000000-aaaa")
        self.assertEqual(result.skipped_lines, 1)
        self.assertEqual([m.content for m in result.messages], ["one", "two"])

    def test_unknown_fields_ignored(self) -> None:
        self._write_archive("20260101-000000-bbbb", [
            json.dumps({"role": "user", "content": "x", "future_field": {"a": 1}}),
        ])
        result = self.store.load("20260101-000000-bbbb")
        self.assertEqual(len(result.messages), 1)

    # ------------------------------------------------------------------ #
    # 双内容 display_content（c10 T27 / F26–F27）
    # ------------------------------------------------------------------ #
    def test_display_content_roundtrip(self) -> None:
        """带 display_content 的消息追加/载入往返：JSONL 同时保存两种内容。"""
        sid = self.store.start_new()
        self.store.append(
            Message(role="user", content="展开后的完整初始化提示词……", display_content="/init")
        )
        line = (self.dir / f"{sid}.jsonl").read_text(encoding="utf-8").splitlines()[0]
        data = json.loads(line)
        self.assertEqual(data["content"], "展开后的完整初始化提示词……")
        self.assertEqual(data["display_content"], "/init")
        result = self.store.load(sid)
        self.assertEqual(result.messages[0].content, "展开后的完整初始化提示词……")
        self.assertEqual(result.messages[0].display_content, "/init")

    def test_plain_message_omits_display_field(self) -> None:
        """普通消息（display_content=None）不写该字段，行格式与旧版一致。"""
        sid = self.store.start_new()
        self.store.append(Message(role="user", content="普通消息"))
        data = json.loads((self.dir / f"{sid}.jsonl").read_text(encoding="utf-8"))
        self.assertNotIn("display_content", data)

    def test_legacy_archive_without_display_field(self) -> None:
        """旧 JSONL 无 display_content 字段仍可载入，回退 None（C49）。"""
        self._write_archive("20260101-000000-lgcy", [
            json.dumps({"role": "user", "content": "旧格式消息"}),
        ])
        result = self.store.load("20260101-000000-lgcy")
        self.assertIsNone(result.messages[0].display_content)
        self.assertEqual(result.messages[0].content, "旧格式消息")

    def test_invalid_display_field_falls_back_to_none(self) -> None:
        """非法类型（数字/对象/null）的 display_content 统一回退 None。"""
        self._write_archive("20260101-000000-badd", [
            json.dumps({"role": "user", "content": "a", "display_content": 123}),
            json.dumps({"role": "user", "content": "b", "display_content": None}),
            json.dumps({"role": "user", "content": "c", "display_content": {"x": 1}}),
        ])
        result = self.store.load("20260101-000000-badd")
        self.assertEqual(len(result.messages), 3)
        for msg in result.messages:
            self.assertIsNone(msg.display_content)

    def test_title_prefers_display_content(self) -> None:
        """会话标题优先显示原命令；普通会话仍用 content（C50）。"""
        self._write_archive("20260101-000000-init", [
            json.dumps({
                "ts": "2026-01-01T10:00:00",
                "role": "user",
                "content": "很长很长的展开提示词" * 10,
                "display_content": "/init",
            }),
        ])
        self._write_archive("20260102-000000-norm", [
            json.dumps({"ts": "2026-01-02T10:00:00", "role": "user", "content": "普通标题"}),
        ])
        infos = {i.session_id: i for i in self.store.list_sessions()}
        self.assertEqual(infos["20260101-000000-init"].title, "/init")
        self.assertEqual(infos["20260102-000000-norm"].title, "普通标题")

    def test_unpaired_tail_group_dropped(self) -> None:
        """结尾「有 tool_calls 无结果」：该组丢弃、之前的消息保留。"""
        self._write_archive("20260101-000000-cccc", [
            json.dumps({"role": "user", "content": "q"}),
            json.dumps({"role": "assistant", "content": "a1"}),
            json.dumps({"role": "assistant", "content": "", "tool_calls": [
                {"id": "c1", "name": "run_command", "arguments": {"command": "ls"}}]}),
            # 崩溃：c1 的 tool 结果没写进来
        ])
        result = self.store.load("20260101-000000-cccc")
        self.assertEqual([m.content for m in result.messages], ["q", "a1"])
        self.assertEqual(result.dropped_unpaired, 1)

    def test_unpaired_middle_group_dropped_rest_kept(self) -> None:
        """中间断口：丢组不截断，断口之后的消息保留（F12）。"""
        self._write_archive("20260101-000000-dddd", [
            json.dumps({"role": "user", "content": "q"}),
            json.dumps({"role": "assistant", "content": "", "tool_calls": [
                {"id": "c1", "name": "read_file", "arguments": {}},
                {"id": "c2", "name": "read_file", "arguments": {}}]}),
            json.dumps({"role": "tool", "tool_call_id": "c1", "content": "r1"}),
            # c2 的结果缺失 → 整组（assistant + c1 的 tool 行）都该丢
            json.dumps({"role": "user", "content": "next"}),
            json.dumps({"role": "assistant", "content": "fine"}),
        ])
        result = self.store.load("20260101-000000-dddd")
        self.assertEqual([m.content for m in result.messages], ["q", "next", "fine"])
        self.assertEqual(result.dropped_unpaired, 2)  # assistant + 孤儿 tool 行

    def test_orphan_tool_dropped(self) -> None:
        """孤儿 tool 行（无归属 assistant）被丢弃。"""
        self._write_archive("20260101-000000-eeee", [
            json.dumps({"role": "tool", "tool_call_id": "ghost", "content": "r"}),
            json.dumps({"role": "user", "content": "q"}),
        ])
        result = self.store.load("20260101-000000-eeee")
        self.assertEqual([m.content for m in result.messages], ["q"])
        self.assertEqual(result.dropped_unpaired, 1)

    # ------------------------------------------------------------------ #
    # 扫描列表（AC7 相关）
    # ------------------------------------------------------------------ #
    def test_list_sessions(self) -> None:
        long_title = "第一个会话的" + "很长" * 20 + "标题"  # 超过 30 字，必然触发截断
        self._write_archive("20260101-000000-aaaa", [
            json.dumps({"ts": "2026-01-01T10:00:00", "role": "user", "content": long_title}),
        ])
        self._write_archive("20260102-000000-bbbb", [
            json.dumps({"ts": "2026-01-02T10:00:00", "role": "user", "content": "第二个"}),
            json.dumps({"ts": "2026-01-02T10:01:00", "role": "assistant", "content": "答"}),
        ])
        infos = self.store.list_sessions()
        self.assertEqual(len(infos), 2)
        # 按最后时间倒序：第二个会话在前
        self.assertEqual(infos[0].session_id, "20260102-000000-bbbb")
        self.assertEqual(infos[0].message_count, 2)
        self.assertEqual(infos[1].title, long_title[:30])
        self.assertFalse(infos[0].locked)

    def test_list_marks_locked(self) -> None:
        self._write_archive("20260101-000000-aaaa", [json.dumps({"role": "user", "content": "x"})])
        lockfile.try_acquire(self.dir / "20260101-000000-aaaa.lock", SESSION_LOCK_STALE)
        infos = self.store.list_sessions()
        self.assertTrue(infos[0].locked)

    # ------------------------------------------------------------------ #
    # 接管（AC22 相关）
    # ------------------------------------------------------------------ #
    def test_attach_fresh_lock_refused(self) -> None:
        self._write_archive("20260101-000000-aaaa", [json.dumps({"role": "user", "content": "x"})])
        lockfile.try_acquire(self.dir / "20260101-000000-aaaa.lock", SESSION_LOCK_STALE)
        self.assertFalse(self.store.attach("20260101-000000-aaaa"))

    def test_attach_stale_lock_taken_over_and_append_continues(self) -> None:
        sid = "20260101-000000-aaaa"
        self._write_archive(sid, [json.dumps({"role": "user", "content": "old"})])
        lock = self.dir / f"{sid}.lock"
        lockfile.try_acquire(lock, SESSION_LOCK_STALE)
        _age(lock, SESSION_LOCK_STALE + 10)
        self.assertTrue(self.store.attach(sid))
        # 接管后 append 追加进同一文件（F12）
        self.store.append(Message(role="user", content="new"))
        result = self.store.load(sid)
        self.assertEqual([m.content for m in result.messages], ["old", "new"])

    def test_attach_missing_archive_fails(self) -> None:
        self.assertFalse(self.store.attach("20990101-000000-zzzz"))

    # ------------------------------------------------------------------ #
    # 过期清理（AC13）
    # ------------------------------------------------------------------ #
    def test_cleanup_expired(self) -> None:
        old_id, new_id, locked_id = "20250101-000000-aaaa", "20260701-000000-bbbb", "20250101-000000-cccc"
        for sid in (old_id, new_id, locked_id):
            self._write_archive(sid, [json.dumps({"role": "user", "content": "x"})])
        _age(self.dir / f"{old_id}.jsonl", 31 * 86400)
        _age(self.dir / f"{locked_id}.jsonl", 31 * 86400)
        # locked_id 被新鲜锁保护
        lockfile.try_acquire(self.dir / f"{locked_id}.lock", SESSION_LOCK_STALE)
        # 孤儿锁：有锁没档
        lockfile.try_acquire(self.dir / "20240101-000000-dddd.lock", SESSION_LOCK_STALE)

        removed = self.store.cleanup_expired()
        self.assertEqual(removed, 1)
        self.assertFalse((self.dir / f"{old_id}.jsonl").exists())     # 过期删除
        self.assertTrue((self.dir / f"{new_id}.jsonl").exists())      # 30 天内保留
        self.assertTrue((self.dir / f"{locked_id}.jsonl").exists())   # 新鲜锁保护
        self.assertFalse((self.dir / "20240101-000000-dddd.lock").exists())  # 孤儿锁清理

    # ------------------------------------------------------------------ #
    # /clear 开新档（AC8 的存储侧行为）
    # ------------------------------------------------------------------ #
    def test_start_new_switches_archive(self) -> None:
        sid1 = self.store.start_new()
        self.store.append(Message(role="user", content="one"))
        sid2 = self.store.start_new()
        self.store.append(Message(role="user", content="two"))
        self.assertNotEqual(sid1, sid2)
        self.assertEqual([m.content for m in self.store.load(sid1).messages], ["one"])
        self.assertEqual([m.content for m in self.store.load(sid2).messages], ["two"])
        # 旧会话的锁已随 start_new 释放
        self.assertFalse((self.dir / f"{sid1}.lock").exists())


if __name__ == "__main__":
    unittest.main()
