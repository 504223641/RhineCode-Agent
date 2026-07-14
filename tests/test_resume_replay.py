"""
/resume 交互化与历史回放测试（c9 增强）。

覆盖两块纯逻辑/协调层行为：
1. build_replay_items —— 「历史消息 → 回放渲染项」的纯函数转换
   （user/assistant/tool 配对、空 assistant 跳过、缺结果兜底、首行截断）；
2. ConversationManager 的 /resume 分发 —— 无参返回 SessionListRequest（弹面板信号）
   或提示字符串（空档/全锁定）；带参事件流成功时含 HISTORY 快照、失败时不含。

TUI 面板（SessionPanel）与真实渲染留 TUI 手测（见 docs/c9/checklist.md 的交互场景）。
"""

import json
import os
import tempfile
import unittest
from pathlib import Path

from rhinecode.config import Config
from rhinecode.conversation import ConversationManager, SessionListRequest
from rhinecode.agent.events import AgentEventType
from rhinecode.provider.base import BaseProvider, Message, StreamChunk, ToolCall
from rhinecode.tools.registry import ToolRegistry
from rhinecode.tui.widgets import build_replay_items, _first_line_truncated
from rhinecode.memory import lockfile


class BuildReplayItemsTest(unittest.TestCase):
    """build_replay_items 纯函数：消息 → 渲染项的转换规则。"""

    def test_user_assistant_tool_pairing(self) -> None:
        """典型历史：user → assistant(纯 tool_calls) → tool 结果 → assistant 正文。"""
        tc = ToolCall(id="call-1", name="read_file", arguments={"path": "a.txt"})
        messages = [
            Message(role="user", content="读一下 a.txt"),
            Message(role="assistant", content="", tool_calls=[tc]),
            Message(role="tool", content="文件内容第一行\n第二行", tool_call_id="call-1"),
            Message(role="assistant", content="内容是……"),
        ]
        items = build_replay_items(messages)
        self.assertEqual(
            [item[0] for item in items], ["user", "tool", "assistant"]
        )  # 空 content 的 assistant 不产出正文项；role="tool" 并入 tool 项
        self.assertEqual(items[0][1], "读一下 a.txt")
        self.assertIs(items[1][1], tc)
        self.assertEqual(items[1][2], "文件内容第一行")  # 结果只取首行
        self.assertEqual(items[2][1], "内容是……")

    def test_assistant_with_text_and_tool_calls(self) -> None:
        """assistant 同时有正文与 tool_calls：先正文项、后工具项（保持阅读顺序）。"""
        tc = ToolCall(id="c1", name="run_command", arguments={"command": "ls"})
        messages = [
            Message(role="assistant", content="我来看看目录", tool_calls=[tc]),
            Message(role="tool", content="a.txt", tool_call_id="c1"),
        ]
        items = build_replay_items(messages)
        self.assertEqual([item[0] for item in items], ["assistant", "tool"])

    def test_missing_tool_result_fallback(self) -> None:
        """载入历史理论上已配对，但缺结果时防御性兜底为占位文本。"""
        tc = ToolCall(id="orphan", name="glob_files", arguments={"pattern": "*"})
        items = build_replay_items([Message(role="assistant", content="", tool_calls=[tc])])
        self.assertEqual(items[0][2], "（无结果）")

    def test_empty_assistant_skipped(self) -> None:
        """content 为空且无 tool_calls 的 assistant 整体跳过（不渲染空 Rhine 行）。"""
        self.assertEqual(build_replay_items([Message(role="assistant", content="")]), [])

    def test_unknown_role_skipped(self) -> None:
        self.assertEqual(build_replay_items([Message(role="system", content="x")]), [])

    def test_first_line_truncated(self) -> None:
        long_line = "字" * 100
        self.assertEqual(_first_line_truncated(long_line), "字" * 80 + "…")
        self.assertEqual(_first_line_truncated("首行\n次行"), "首行")
        self.assertEqual(_first_line_truncated(""), "")


class SilentProvider(BaseProvider):
    """从不被期望调用的假 Provider：/resume 分发路径不应发起任何 LLM 请求。"""

    def __init__(self) -> None:
        self.calls = 0

    def stream_chat(self, messages, thinking_effort="off", tools=None, system=None):
        self.calls += 1
        yield StreamChunk(type="text", content="unexpected")
        yield StreamChunk(type="done")


class ConversationResumeTest(unittest.TestCase):
    """ConversationManager 的 /resume 分发与事件流（临时工作区，仿 test_review_fixes 手法）。"""

    def setUp(self) -> None:
        self._old_cwd = os.getcwd()
        self._workspace = tempfile.TemporaryDirectory()
        os.chdir(self._workspace.name)

    def tearDown(self) -> None:
        os.chdir(self._old_cwd)
        self._workspace.cleanup()

    def _write_archive(self, sid: str, contents: list[str], ts_prefix: str) -> None:
        """直接写一份合法 JSONL 存档，模拟历史会话。"""
        d = Path(".rhinecode") / "sessions"
        d.mkdir(parents=True, exist_ok=True)
        lines = [
            json.dumps(
                {"ts": f"{ts_prefix}T10:{i:02d}:00", "role": "user", "content": c},
                ensure_ascii=False,
            )
            for i, c in enumerate(contents)
        ]
        (d / f"{sid}.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")

    def _manager(self) -> ConversationManager:
        config = Config(
            protocol="deepseek",
            model="test-model",
            base_url="http://test",
            api_key="test-key",
            debug_log=False,
        )
        return ConversationManager(SilentProvider(), config, ToolRegistry())

    def test_resume_no_archives_returns_str(self) -> None:
        manager = self._manager()
        result = manager.handle_input("/resume")
        self.assertIsInstance(result, str)
        self.assertIn("没有可恢复的会话存档", result)

    def test_resume_returns_session_list_request(self) -> None:
        self._write_archive("20260101-000000-aaaa", ["旧对话"], "2026-01-01")
        self._write_archive("20260102-000000-bbbb", ["新对话"], "2026-01-02")
        manager = self._manager()
        result = manager.handle_input("/resume")
        self.assertIsInstance(result, SessionListRequest)
        self.assertEqual(
            [i.session_id for i in result.sessions],
            ["20260102-000000-bbbb", "20260101-000000-aaaa"],
        )
        # 当前会话惰性建档（尚无文件），不出现在列表里，但 current_id 必须给到面板
        self.assertEqual(result.current_id, manager.memory_manager.session_id)

    def test_resume_all_locked_returns_str(self) -> None:
        """唯一的存档被另一实例新鲜锁占用 → 不弹面板、如实反馈。"""
        self._write_archive("20260101-000000-aaaa", ["x"], "2026-01-01")
        lockfile.try_acquire(
            Path(".rhinecode") / "sessions" / "20260101-000000-aaaa.lock", 600
        )
        manager = self._manager()
        result = manager.handle_input("/resume")
        self.assertIsInstance(result, str)
        self.assertIn("没有可恢复的其它会话", result)

    def test_resume_stream_success_yields_history_snapshot(self) -> None:
        """带参载入成功：事件流首个为 HISTORY 且快照与存档一致，随后 NOTICE + FINISHED。"""
        self._write_archive("20260101-000000-aaaa", ["第一句", "第二句"], "2026-01-01")
        manager = self._manager()
        events = list(manager.handle_input("/resume 20260101-000000-aaaa"))

        history_events = [e for e in events if e.type == AgentEventType.HISTORY]
        self.assertEqual(len(history_events), 1)
        self.assertEqual(events[0].type, AgentEventType.HISTORY)  # 回放先于 NOTICE
        self.assertEqual(
            [m.content for m in history_events[0].messages], ["第一句", "第二句"]
        )
        self.assertEqual(events[-1].type, AgentEventType.FINISHED)
        self.assertTrue(any(e.type == AgentEventType.NOTICE for e in events))

    def test_resume_stream_failure_no_history_event(self) -> None:
        """带参载入失败（找不到会话）：不产 HISTORY（TUI 不清屏），仅 NOTICE 反馈。"""
        manager = self._manager()
        events = list(manager.handle_input("/resume 不存在的ID"))
        self.assertFalse(any(e.type == AgentEventType.HISTORY for e in events))
        notices = [e for e in events if e.type == AgentEventType.NOTICE]
        self.assertTrue(any("找不到会话" in e.message for e in notices))


if __name__ == "__main__":
    unittest.main()
