"""MemoryManager 编排单测（c9 T11 / AC14–AC18/AC21 相关），用假 provider 断言笔记请求不带工具。"""

import json
import time
import unittest
import tempfile
from pathlib import Path

from rhinecode.provider.base import BaseProvider, Message, StreamChunk
from rhinecode.memory.manager import MemoryManager, NOTE_LOCK_STALE, INDEX_FILENAME
from rhinecode.memory.note_updater import parse_note_response
from rhinecode.memory.notes import INDEX_MAX_LINES
from rhinecode.memory import lockfile


class FakeProvider(BaseProvider):
    """预置响应的假 Provider：记录每次请求的入参，供断言 tools=None 与请求内容。"""

    def __init__(self, responses: list[str]):
        self.responses = list(responses)
        self.calls: list[dict] = []
        self.raise_error = False

    def stream_chat(self, messages, thinking_effort="off", tools=None, system=None):
        self.calls.append(
            {"messages": messages, "tools": tools, "system": system}
        )
        if self.raise_error:
            raise RuntimeError("模拟网络失败")
        text = self.responses.pop(0) if self.responses else "[]"
        yield StreamChunk(type="text", content=text)
        yield StreamChunk(type="done")


def _action_json(scope: str = "project", filename: str = "arch-note.md") -> str:
    return json.dumps([{
        "op": "add", "scope": scope, "filename": filename,
        "name": "arch-note", "summary": "架构决策摘要", "category": "project",
        "body": "决策内容与原因。",
    }], ensure_ascii=False)


class ManagerTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.project = root / "proj"
        self.user_dir = root / "home" / ".rhinecode"
        self.project.mkdir(parents=True)
        self.user_dir.mkdir(parents=True)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _manager(self, provider=None, notes_enabled=True) -> MemoryManager:
        return MemoryManager(
            provider or FakeProvider(["[]"]),
            "test-model",
            self.project,
            self.user_dir,
            notes_enabled=notes_enabled,
        )

    def _wait_notes_done(self, mgr: MemoryManager, timeout: float = 5.0) -> None:
        """等待笔记后台线程结束（in-flight 标志清除）。"""
        deadline = time.time() + timeout
        while mgr._note_inflight.is_set():
            if time.time() > deadline:
                self.fail("笔记线程超时未结束")
            time.sleep(0.01)


class ParseResponseTest(unittest.TestCase):
    """parse_note_response 的宽松解析与防注入（T11 步骤 1）。"""

    def test_valid_array(self) -> None:
        actions = parse_note_response("前置解释\n" + _action_json() + "\n后置")
        self.assertEqual(len(actions), 1)
        self.assertEqual(actions[0].op, "add")
        self.assertEqual(actions[0].note.category, "project")

    def test_bad_json_returns_empty(self) -> None:
        self.assertEqual(parse_note_response("不是 JSON"), [])
        self.assertEqual(parse_note_response("[{未闭合"), [])
        self.assertEqual(parse_note_response(""), [])

    def test_illegal_items_skipped(self) -> None:
        data = json.dumps([
            {"op": "add", "scope": "project", "filename": "../evil.md",  # 路径注入
             "name": "x", "summary": "y", "category": "project"},
            {"op": "add", "scope": "银河系", "filename": "a.md",         # 非法 scope
             "name": "x", "summary": "y", "category": "project"},
            {"op": "add", "scope": "user", "filename": "b.md",
             "name": "x", "summary": "y", "category": "不存在"},         # 非法 category
            {"op": "delete", "scope": "user", "filename": "ok-note.md"},  # 合法 delete
        ])
        actions = parse_note_response(data)
        self.assertEqual(len(actions), 1)
        self.assertEqual(actions[0].op, "delete")


class NotesFlowTest(ManagerTestBase):
    """自然停止 → 笔记落盘 → 索引重建 → 通知（T11 步骤 2–6）。"""

    def test_natural_stop_writes_note_and_index(self) -> None:
        provider = FakeProvider([_action_json("project", "arch-note.md")])
        mgr = self._manager(provider)
        notices: list[str] = []
        mgr.notify = notices.append

        history = [Message(role="user", content="记住这个架构决策"),
                   Message(role="assistant", content="好的")]
        mgr.on_natural_stop(history)
        self._wait_notes_done(mgr)

        # 笔记请求不带工具（F15/N6④）
        self.assertEqual(len(provider.calls), 1)
        self.assertIsNone(provider.calls[0]["tools"])
        # 项目级目录出现笔记文件与索引
        note_path = self.project / ".rhinecode" / "memory" / "arch-note.md"
        self.assertTrue(note_path.exists())
        index = (self.project / ".rhinecode" / "memory" / INDEX_FILENAME).read_text(encoding="utf-8")
        self.assertIn("arch-note", index)
        # 通知与结果记录
        self.assertEqual(len(notices), 1)
        self.assertIn("已更新", mgr._last_note_result)
        # 正常写入完成后锁被释放（AC21）
        self.assertFalse((self.project / ".rhinecode" / "memory" / ".lock").exists())

    def test_locked_dir_skipped_without_wait(self) -> None:
        provider = FakeProvider([_action_json("project")])
        mgr = self._manager(provider)
        target = self.project / ".rhinecode" / "memory"
        target.mkdir(parents=True)
        lockfile.try_acquire(target / ".lock", NOTE_LOCK_STALE)  # 模拟另一实例持锁

        mgr._update_notes([Message(role="user", content="x")])  # 同步调用便于断言
        self.assertFalse((target / "arch-note.md").exists())
        self.assertIn("跳过", mgr._last_note_result)

    def test_provider_error_silently_recorded(self) -> None:
        provider = FakeProvider([])
        provider.raise_error = True
        mgr = self._manager(provider)
        mgr.on_natural_stop([Message(role="user", content="x")])
        self._wait_notes_done(mgr)
        self.assertIn("失败", mgr._last_note_result)
        self.assertFalse(mgr._note_inflight.is_set())  # 标志已清，不影响后续轮次

    def test_inflight_skips_round(self) -> None:
        mgr = self._manager()
        mgr._note_inflight.set()  # 模拟上一轮仍在跑
        mgr.on_natural_stop([Message(role="user", content="x")])
        self.assertEqual(mgr._note_watermark, 0)  # 高水位未推进（本轮未消费）
        self.assertIn("跳过", mgr._last_note_result)

    def test_watermark_only_new_messages_sent(self) -> None:
        provider = FakeProvider(["[]", "[]"])
        mgr = self._manager(provider)
        history = [Message(role="user", content="第一轮问题"),
                   Message(role="assistant", content="第一轮回答")]
        mgr.on_natural_stop(history)
        self._wait_notes_done(mgr)
        history += [Message(role="user", content="第二轮问题"),
                    Message(role="assistant", content="第二轮回答")]
        mgr.on_natural_stop(history)
        self._wait_notes_done(mgr)

        second_req = provider.calls[1]["messages"][0].content
        self.assertIn("第二轮问题", second_req)
        self.assertNotIn("第一轮问题", second_req)  # 高水位之前的不重复审视

    def test_notes_disabled_noop(self) -> None:
        provider = FakeProvider([_action_json()])
        mgr = self._manager(provider, notes_enabled=False)
        mgr.on_natural_stop([Message(role="user", content="x")])
        time.sleep(0.05)
        self.assertEqual(provider.calls, [])  # 完全不调 LLM（F21）


class StartupResumeTest(ManagerTestBase):
    """startup / --continue / resume_into（T11 步骤 7）。"""

    def _write_archive(self, sid: str, contents: list[str], ts_prefix: str) -> None:
        d = self.project / ".rhinecode" / "sessions"
        d.mkdir(parents=True, exist_ok=True)
        lines = [
            json.dumps({"ts": f"{ts_prefix}T10:{i:02d}:00", "role": "user", "content": c})
            for i, c in enumerate(contents)
        ]
        (d / f"{sid}.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")

    def test_continue_skips_locked_session(self) -> None:
        self._write_archive("20260101-000000-aaaa", ["旧的"], "2026-01-01")
        self._write_archive("20260102-000000-bbbb", ["新的"], "2026-01-02")
        # 最近的 bbbb 被另一实例锁住 → 顺延到 aaaa
        d = self.project / ".rhinecode" / "sessions"
        lockfile.try_acquire(d / "20260102-000000-bbbb.lock", 600)

        mgr = self._manager()
        history: list[Message] = []
        notice = mgr.startup(resume_latest=True, history=history)
        self.assertIn("20260101-000000-aaaa", notice)
        self.assertEqual(history[0].content, "旧的")

    def test_continue_no_sessions_starts_new(self) -> None:
        mgr = self._manager()
        history: list[Message] = []
        notice = mgr.startup(resume_latest=True, history=history)
        self.assertIn("已开始新会话", notice)
        self.assertEqual(history, [])

    def test_resume_into_by_number_and_time_gap(self) -> None:
        self._write_archive("20260101-000000-aaaa", ["很久以前的对话"], "2026-01-01")
        mgr = self._manager()
        mgr.startup(resume_latest=False, history=[])
        mgr.resume_list()  # 建立编号缓存
        history: list[Message] = [Message(role="user", content="当前的")]
        ok, msg = mgr.resume_into("1", history)
        self.assertTrue(ok, msg)
        self.assertEqual(history[0].content, "很久以前的对话")
        # 2026-01-01 距今远超 24h → 登记时间跨度提醒，且取走即清（F11④）
        notice = mgr.consume_pending_notice()
        self.assertIn("距上次对话", notice)
        self.assertEqual(mgr.consume_pending_notice(), "")

    def test_resume_into_locked_refused(self) -> None:
        self._write_archive("20260101-000000-aaaa", ["x"], "2026-01-01")
        d = self.project / ".rhinecode" / "sessions"
        lockfile.try_acquire(d / "20260101-000000-aaaa.lock", 600)
        mgr = self._manager()
        mgr.startup(resume_latest=False, history=[])
        ok, msg = mgr.resume_into("20260101-000000-aaaa", [])
        self.assertFalse(ok)
        self.assertIn("另一个 RhineCode 实例", msg)


class ObservabilityTest(ManagerTestBase):
    """memory_report / memory_index / custom_instructions（T11 步骤 8–9）。"""

    def test_memory_report_fields(self) -> None:
        (self.project / "RHINE.md").write_text("规则", encoding="utf-8")
        mgr = self._manager()
        mgr.startup(resume_latest=False, history=[])
        report = mgr.memory_report()
        self.assertIn("RHINE.md 项目指令", report)
        self.assertIn("项目根", report)
        self.assertIn("自动笔记", report)
        self.assertIn("当前会话", report)
        self.assertIn("写锁", report)
        self.assertIn("最近一次自动更新", report)

    def test_custom_instructions_loaded(self) -> None:
        (self.project / "RHINE.md").write_text("PROJECT-RULE", encoding="utf-8")
        mgr = self._manager()
        mgr.startup(resume_latest=False, history=[])
        self.assertIn("PROJECT-RULE", mgr.custom_instructions())

    def test_memory_index_truncated(self) -> None:
        target = self.project / ".rhinecode" / "memory"
        target.mkdir(parents=True)
        big = "\n".join(f"- note-{i}（n{i}.md）[项目知识] — 摘要" for i in range(300))
        (target / INDEX_FILENAME).write_text(big, encoding="utf-8")
        mgr = self._manager()
        injected = mgr.memory_index()
        # 项目级那一段被截到 ≤200 行（外加头部说明行）
        self.assertIn("note-0", injected)
        self.assertNotIn("note-250", injected)
        self.assertLessEqual(len(injected.splitlines()), INDEX_MAX_LINES + 10)

    def test_memory_index_empty_when_no_files(self) -> None:
        mgr = self._manager()
        self.assertEqual(mgr.memory_index(), "")


if __name__ == "__main__":
    unittest.main()
