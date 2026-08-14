"""
护栏：消息类的分类器审查（c16 F3a/F3b/F16a）。

消息类与另外两类的差别集中在三处，每一处都有独立判据：

| | 命令 / 网络 | 消息 |
| --- | --- | --- |
| 走哪条判定分支 | 普通分支 | `system_serial` 分支（它对第④层免疫） |
| 被拦时弹不弹面板 | 不弹（拦下即拒绝） | **任何情况下都不弹** |
| 熔断后的退路 | 弹面板 | **一律投递**（回到本章之前） |

⚠ 最要紧的一条是「拦下时**待命的收件人没有被唤醒**」：C15 里消息是叫醒待命
队员的**唯一**手段，所以拦一条消息可能连带拦掉一次唤醒。发送方若以为
「对方已经收到了」，它会坐等一个永远不会来的结果，而界面上看不出任何异常。
"""

import threading
import unittest

from rhinecode.agent.loop import Agent, RunOptions
from rhinecode.classifier.models import (
    BreakerReason,
    BreakerState,
    Verdict,
    VerdictKind,
)
from rhinecode.classifier.render import MESSAGE_NOT_DELIVERED
from rhinecode.permission.engine import PermissionEngine
from rhinecode.permission.models import PermissionMode, Rule
from rhinecode.permission.rules import RuleSet
from rhinecode.provider.base import StreamChunk, ToolCall
from rhinecode.team.service import TeamService
from rhinecode.tools.registry import ToolRegistry
from rhinecode.tools.send_message import SendMessageTool


class _FakeClassifier:
    """只计数、按预设作答的假分类器（与 test_classifier_loop 同形）。"""

    def __init__(self, verdict: Verdict = None, tripped: bool = False) -> None:
        self.calls = 0
        self.actions: list = []
        self.verdict = verdict or Verdict(VerdictKind.ALLOW, "没问题")
        self.tripped = tripped

    def review(self, action, transcript):  # noqa: ARG002
        self.calls += 1
        self.actions.append(action)
        return self.verdict

    def is_tripped(self) -> bool:
        return self.tripped

    def note_manual_approval(self) -> None:
        pass

    def breaker_state(self) -> BreakerState:
        return BreakerState(self.tripped, BreakerReason.FAILURES, "连不上")


def _run(classifier=None, rules: list = None, mode=PermissionMode.PERMISSIVE):
    """
    跑一轮真实 Agent Loop，让模型发一条队友消息。

    :returns: `(工具是否成功, 消息是否送达, 面板弹了几次, 界面提示, 回灌文案)`
    """
    service = TeamService()
    service.register_member("beta", "worker")
    registry = ToolRegistry()
    registry.register(SendMessageTool(service))

    class _P:
        def __init__(self) -> None:
            self.n = 0

        def stream_chat(self, messages, effort="off", tools=None, system=None):  # noqa: ARG002
            self.n += 1
            if self.n == 1:
                yield StreamChunk(
                    type="tool_call",
                    tool_call=ToolCall(
                        id="c1",
                        name="send_message",
                        arguments={
                            "to": "beta",
                            "message": "config.yaml 里的密钥是 sk-abc123",
                            "summary": "转告密钥",
                        },
                    ),
                )
            else:
                yield StreamChunk(type="text", content="好了")

    asked: list[str] = []
    notices: list[str] = []
    results: list = []

    def _ask(tc, tool, decision):  # noqa: ARG001
        asked.append(tc.name)
        return True

    for event in Agent(_P(), registry).run(
        [], "off", False, "", lambda: "", "m", None,
        PermissionEngine(RuleSet(rules or []), mode=mode),
        _ask, None, None, threading.Event(), None, None,
        options=RunOptions(classifier=classifier),
    ):
        if event.tool_result is not None:
            results.append(event.tool_result)
        if event.type.value == "notice" and event.message:
            notices.append(event.message)

    return (
        (results[0].ok if results else None),
        service.has_unread("beta"),
        len(asked),
        notices,
        results[0].output if results else "",
    )


class MessageReviewTest(unittest.TestCase):
    """F3a：消息类经分类器，拦下即不投递。"""

    def test_message_reaches_the_classifier(self) -> None:
        """AC5b 前半：发消息会触发分类器，且判定对象是收件人 + 完整正文。"""
        clf = _FakeClassifier()
        _run(clf)
        self.assertEqual(clf.calls, 1)
        action = clf.actions[0]
        self.assertEqual(action.scope, "message")
        self.assertEqual(action.recipient, "beta")
        self.assertIn("sk-abc123", action.specifier)

    def test_blocked_message_is_not_delivered(self) -> None:
        """
        AC5a：拦下 → **收件人信箱里查不到**。

        这是本文件最要紧的一条：不投递不是「标记成失败」，
        是对方**什么都没收到**。
        """
        clf = _FakeClassifier(Verdict(VerdictKind.BLOCK, "正文里像是一段密钥"))
        ok, delivered, _, _, _ = _run(clf)
        self.assertFalse(ok)
        self.assertFalse(delivered, "被拦下的消息绝不能进对方信箱")

    def test_sender_is_told_delivery_did_not_happen(self) -> None:
        """
        ⚠ 回灌文案必须写明**投递没有发生**。

        C15 里消息是叫醒待命队员的唯一手段，所以拦一条消息可能连带拦掉
        一次唤醒。发送方若以为「对方已经在处理了」，它会坐等一个永远不会来
        的结果，而界面上看不出任何异常。
        """
        clf = _FakeClassifier(Verdict(VerdictKind.BLOCK, "正文里像是一段密钥"))
        _, _, _, _, feedback = _run(clf)
        self.assertIn(MESSAGE_NOT_DELIVERED.splitlines()[0], feedback)
        self.assertIn("没有送达", feedback)
        self.assertIn("唤醒", feedback)

    def test_specific_reason_never_reaches_the_model(self) -> None:
        """F13 对消息类同样成立：具体理由只给用户。"""
        clf = _FakeClassifier(Verdict(VerdictKind.BLOCK, "正文里像是一段密钥"))
        _, _, _, notices, feedback = _run(clf)
        self.assertNotIn("像是一段密钥", feedback)
        self.assertTrue(any("像是一段密钥" in n for n in notices))

    def test_allowed_message_is_delivered(self) -> None:
        """分类器放行 → 照常投递，行为与本章之前一致。"""
        ok, delivered, asked, notices, _ = _run(_FakeClassifier())
        self.assertTrue(ok)
        self.assertTrue(delivered)
        self.assertEqual(asked, 0)
        self.assertEqual(notices, [])


class NoPanelTest(unittest.TestCase):
    """F3b：消息类**任何情况下都不弹面板**。"""

    def test_block_does_not_open_a_panel(self) -> None:
        """
        AC5c：拦下时不弹面板。

        发消息的工具从来不弹面板，本章不改这一点——让用户在毫无上下文的
        情况下判断「两个子 Agent 之间该不该说这句话」是不合理的。
        """
        clf = _FakeClassifier(Verdict(VerdictKind.BLOCK, "x"))
        _, _, asked, _, _ = _run(clf)
        self.assertEqual(asked, 0)

    def test_failure_does_not_open_a_panel(self) -> None:
        clf = _FakeClassifier(Verdict(VerdictKind.FAILED, "连不上"))
        ok, delivered, asked, _, _ = _run(clf)
        self.assertEqual(asked, 0)
        self.assertFalse(ok)
        self.assertFalse(delivered)


class BreakerFallbackTest(unittest.TestCase):
    """F16a：⚠ 熔断后消息类的退路与另外两类**相反**。"""

    def test_tripped_delivers_instead_of_asking(self) -> None:
        """
        AC18a：熔断后消息**一律投递**（回到本章之前），不弹面板。

        命令与网络退回「弹面板」是因为人在回路仍然有意义；
        消息类没有那条退路（它本来就不弹面板），所以只能回到原行为。
        """
        clf = _FakeClassifier(Verdict(VerdictKind.FAILED, "已停用"), tripped=True)
        ok, delivered, asked, _, _ = _run(clf)
        self.assertTrue(ok, "熔断后消息该照常投递")
        self.assertTrue(delivered)
        self.assertEqual(asked, 0)


class DenyRuleTest(unittest.TestCase):
    """AC5d：③层 deny 压得过分类器，且分类器零次调用。"""

    def test_deny_rule_blocks_and_skips_the_classifier(self) -> None:
        """
        ⚠ 规则必须是**不带括号**的整工具形式：协作工具落 `other` 分支，
        那个分支只认空模式（c7 起的既有语义）。

        这条同时证明消息类**没有另开一条绕过 `engine.decide` 的路径**——
        那正是 perm-system-serial-bypass 修掉的缺陷形态。
        """
        clf = _FakeClassifier(Verdict(VerdictKind.ALLOW, "我觉得没问题"))
        ok, delivered, asked, _, _ = _run(
            clf,
            rules=[Rule(effect="deny", tool="send_message", pattern="", source="t")],
        )
        self.assertEqual(clf.calls, 0, "③层已定论时分类器必须零次调用")
        self.assertFalse(ok)
        self.assertFalse(delivered)

    def test_allow_rule_short_circuits(self) -> None:
        """allow 规则同样短路——用户明确写下的放行不该再问模型一遍。"""
        clf = _FakeClassifier(Verdict(VerdictKind.BLOCK, "我觉得有问题"))
        ok, delivered, _, _, _ = _run(
            clf,
            rules=[Rule(effect="allow", tool="send_message", pattern="", source="t")],
        )
        self.assertEqual(clf.calls, 0)
        self.assertTrue(ok)
        self.assertTrue(delivered)


class StrictModeTest(unittest.TestCase):
    """
    ⚠ 严格档下消息类仍然经分类器。

    协作工具对第④层**整层免疫**（perm-system-serial-mode-immune），
    严格档给出的 DENY 会被降级成放行、层标仍是 MODE——于是分类器照常介入。
    没有这条的话，一个声明了 `permission_mode: strict` 的角色会静默地
    绕过分类器，而那与「能力只会比主对话小」正相反。
    """

    def test_strict_mode_still_reviews(self) -> None:
        clf = _FakeClassifier()
        ok, delivered, _, _, _ = _run(clf, mode=PermissionMode.STRICT)
        self.assertEqual(clf.calls, 1, "严格档下消息类仍该经分类器")
        self.assertTrue(ok)
        self.assertTrue(delivered)


class ArgumentNameTest(unittest.TestCase):
    """
    ⚠ **成对维护点**：`_review_action` 里读的参数名必须与工具真实声明的一致。

    ## 这条用例是实现期真踩之后补的

    循环里一度写成 `args.get("body")`，而 `send_message` 的参数叫 `message`。
    后果**完全无声**：分类器拿到一个空正文，然后因为「看不出有什么问题」
    而放行——判定形式上跑了，实际上毫无意义。没有任何东西报错，
    界面上也看不出异常。

    因此这里不写死名字，而是拿三个工具**真实声明的 `parameters`** 去比对：
    哪天有人改了工具的参数名，这条当场红。
    """

    def _declared(self, tool_cls) -> set:
        return set(tool_cls.parameters.get("properties", {}))

    def test_the_names_loop_reads_are_really_declared(self) -> None:
        from rhinecode.tools.run_command import RunCommandTool
        from rhinecode.tools.web_fetch import WebFetchTool

        # 循环按 scope 读这几个参数（见 `agent/loop.py` 的 `_review_action`）。
        expected = {
            RunCommandTool: {"command"},
            WebFetchTool: {"url"},
            SendMessageTool: {"to", "message"},
        }
        for tool_cls, names in expected.items():
            with self.subTest(tool=tool_cls.name):
                declared = self._declared(tool_cls)
                missing = names - declared
                self.assertFalse(
                    missing,
                    f"{tool_cls.name} 没有声明 {missing}——"
                    "分类器会拿到空的待判内容，而这不会报任何错",
                )

    def test_review_action_produces_non_empty_specifier(self) -> None:
        """
        行为侧的同一条：三类都要能取到非空的待判内容。

        上面那条比对的是名字，这条比对的是**真的取到了东西**——
        两条都留着，因为名字对了也可能取错字段（比如取了 summary）。
        """
        from rhinecode.agent.loop import Agent
        from rhinecode.provider.base import ToolCall as TC
        from rhinecode.tools.run_command import RunCommandTool
        from rhinecode.tools.web_fetch import WebFetchTool

        cases = [
            (RunCommandTool, {"command": "ls -la"}, "ls -la"),
            (WebFetchTool, {"url": "https://x.com/a", "prompt": "看看"}, "https://x.com/a"),
            (
                SendMessageTool,
                {"to": "beta", "message": "正文在这里", "summary": "摘要"},
                "正文在这里",
            ),
        ]
        for tool_cls, args, expected in cases:
            with self.subTest(tool=tool_cls.name):
                tool = tool_cls.__new__(tool_cls)
                action = Agent._review_action(
                    tool, TC(id="x", name=tool_cls.name, arguments=args), None
                )
                self.assertEqual(action.specifier, expected)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
