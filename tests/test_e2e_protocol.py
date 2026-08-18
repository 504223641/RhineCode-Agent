"""
控制通道协议层测试（P1a T4）。

覆盖：编解码往返（含中文与字面 markup 标记）、`ok` / `err` 结构、错误码越界防护、
四种面板的 `choice` 校验、`via` 组合校验、非法 JSON 不被吞。

本文件**不起任何进程、不碰文件系统**——protocol.py 是纯数据模块，
它的测试也应当能在毫秒级跑完。
"""

from __future__ import annotations

import json
import unittest

from tests.e2e import protocol


class EncodeDecodeTest(unittest.TestCase):
    """编解码往返：spec N9 的「显式 UTF-8」在这里体现为「中文原样往返」。"""

    def test_roundtrip_keeps_chinese_and_markup(self):
        # 字面 `[dim]` 是关键用例：面板原文里带 Textual markup 标记，
        # 协议层不得对它做任何转义或渲染（plan §2.1 的 markup 口径）。
        payload = {
            "cmd": "answer",
            "text": "确认执行 read_file 吗？",
            "hint": "[dim]按 Esc 取消[/dim]",
            "nested": {"list": ["中文", "a[b]c"]},
        }
        line = protocol.encode(payload)
        self.assertIsInstance(line, bytes)
        self.assertTrue(line.endswith(b"\n"), "每条消息必须以换行结尾，它是分帧依据")
        self.assertEqual(protocol.decode(line), payload)

    def test_encode_is_utf8_not_ascii_escaped(self):
        # ensure_ascii=False 让中文在线上就是中文（抓包可读）。
        line = protocol.encode({"t": "中文"})
        self.assertIn("中文".encode("utf-8"), line)
        self.assertNotIn(b"\\u", line)

    def test_decode_accepts_line_without_trailing_newline(self):
        # socket 侧按换行切分后可能已经把 `\n` 去掉，两种形态都要能解。
        self.assertEqual(protocol.decode(b'{"a": 1}'), {"a": 1})

    def test_decode_rejects_invalid_json(self):
        # spec N7：非法 JSON 不吞。吞掉只会让问题在更远的地方以别的形态冒出来。
        with self.assertRaises(json.JSONDecodeError):
            protocol.decode(b"{not json}\n")


class ResponseShapeTest(unittest.TestCase):
    def test_ok_shape(self):
        self.assertEqual(protocol.ok({"state": "idle"}), {"ok": True, "data": {"state": "idle"}})
        self.assertEqual(protocol.ok(), {"ok": True, "data": None})

    def test_err_shape(self):
        got = protocol.err("busy", "会话不空闲", {"state": "busy"})
        self.assertEqual(
            got,
            {"ok": False, "error": {"code": "busy", "message": "会话不空闲", "data": {"state": "busy"}}},
        )

    def test_err_rejects_unknown_code(self):
        # 拼错码字 = 客户端分支静默失效，必须在构造响应的那一刻就炸出来。
        with self.assertRaises(ValueError) as ctx:
            protocol.err("bussy", "typo")
        self.assertIn("bussy", str(ctx.exception))
        # 异常消息要把合法取值列出来，让人能直接照着改
        self.assertIn("busy", str(ctx.exception))


class ValidateChoiceTest(unittest.TestCase):
    """四种面板各一条合法、各一条非法。"""

    def test_confirm(self):
        for good in ("once", "session", "permanent", "deny"):
            self.assertIsNone(protocol.validate_choice("confirm", good))
        msg = protocol.validate_choice("confirm", "yes")
        self.assertIsNotNone(msg)
        self.assertIn("confirm", msg)

    def test_approve(self):
        self.assertIsNone(protocol.validate_choice("approve", "yes"))
        self.assertIsNone(protocol.validate_choice("approve", "no"))
        self.assertIsNotNone(protocol.validate_choice("approve", "once"))

    def test_clarify_index(self):
        self.assertIsNone(protocol.validate_choice("clarify", "0"))
        self.assertIsNone(protocol.validate_choice("clarify", "12"))
        # 非数字串不合法（越界与否要等拿到实际选项列表才知道，不在本层判定）
        self.assertIsNotNone(protocol.validate_choice("clarify", "first"))
        self.assertIsNotNone(protocol.validate_choice("clarify", "-1"))

    def test_session_open_values(self):
        self.assertIsNone(protocol.validate_choice("session", "20260727-101112-ab12"))
        self.assertIsNone(protocol.validate_choice("session", "cancel"))
        self.assertIsNone(protocol.validate_choice("session", "2"))
        self.assertIsNotNone(protocol.validate_choice("session", ""))

    def test_unknown_kind_and_non_string(self):
        self.assertIsNotNone(protocol.validate_choice("popup", "once"))
        self.assertIsNotNone(protocol.validate_choice("confirm", 1))
        self.assertIsNotNone(protocol.validate_choice("confirm", None))


class ValidateViaTest(unittest.TestCase):
    def test_channel_ok_for_all_kinds(self):
        for kind in protocol.PANEL_KINDS:
            self.assertIsNone(protocol.validate_via(kind, "channel"))

    def test_keys_ok_for_supported_kinds(self):
        # ⚠ **clarify 在 ask-user 扩展里被放开了。** 原先禁它的理由
        # （「候选项之间夹着 disabled 详情行，按键次数推不稳」）实测不成立：
        # `_answer_by_keys` 算步数用的是 `extract_panel` **过滤掉 disabled 之后**
        # 的可选项序列，详情行本来就不参与计数。
        #
        # 放开它是必须的：多选的核心交互就是按空格勾选，不走按键路径验不到。
        for kind in ("confirm", "approve", "session", "clarify"):
            self.assertIsNone(protocol.validate_via(kind, "keys"))

    def test_keys_rejected_for_clarify_free_text(self):
        """
        唯一仍被拒绝的组合：clarify 的 `other:<文本>` 走 keys。

        自由输入的键盘全链路有更贴近真实的验法（`keys` 选中「其它…」+
        `send` 打字，后者走真人提交入口），让驱动器逐字模拟按键反而
        绕开了要验的那条岔路。
        """
        msg = protocol.validate_via("clarify", "keys", "other:放到 docs 下面")
        self.assertIsNotNone(msg)
        self.assertIn("channel", msg, "错误消息要给出可行的替代做法")
        self.assertIn("send", msg, "错误消息要指出更贴近真人的那条路")

    def test_clarify_choice_forms(self):
        """四种应答形态都要认（ask-user 扩展 F21）。"""
        for choice in ("2", "0,2", "other:自己写的答案", "skip"):
            with self.subTest(choice=choice):
                self.assertIsNone(protocol.validate_choice("clarify", choice))

    def test_clarify_rejects_garbage(self):
        for choice in ("abc", "0,", "other:", "other:   ", "1;2"):
            with self.subTest(choice=choice):
                self.assertIsNotNone(protocol.validate_choice("clarify", choice))

    def test_unknown_via(self):
        self.assertIsNotNone(protocol.validate_via("confirm", "mouse"))


if __name__ == "__main__":
    unittest.main()
