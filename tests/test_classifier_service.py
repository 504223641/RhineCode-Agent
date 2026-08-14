"""
护栏：分类器门面的两阶段调用与失败收敛（c16 F10–F12、N4）。

## ⚠ 本文件最有分辨力的一条：**断言调用次数，不是断言结果**

「第一阶段判不可疑时第二阶段零次调用」——两个阶段都返回放行的话，
只看结果的用例**分不出**「跑了一次」与「跑了两次」。而那正是两阶段过滤
全部价值所在（成本），也是「命令类不做缓存」这个决定能成立的前提。

同型的还有 `tests/test_classifier_loop.py` 里的「allow 规则短路时零次调用」。
"""

import unittest

from rhinecode.classifier.models import (
    SCOPE_COMMAND,
    ClassifierConfig,
    ReviewAction,
    Transcript,
    VerdictKind,
)
from rhinecode.classifier.service import ClassifierService
from rhinecode.provider.base import StreamChunk

ACTION = ReviewAction(SCOPE_COMMAND, "run_command", "git push origin main", cwd="/p")
TRANSCRIPT = Transcript(user_messages=("这次改动先别提交",), tool_calls=())


class _Recording:
    """
    记录每一次调用的假 Provider。

    :ivar calls: 每次调用的 `(system, tools, thinking_effort)`
    :ivar outs: 依次要产出的文本
    """

    def __init__(self, outs: list[str]) -> None:
        self.outs = list(outs)
        self.calls: list[tuple] = []

    def stream_chat(self, messages, thinking_effort="off", tools=None, system=None):
        self.calls.append((system, tools, thinking_effort))
        text = self.outs.pop(0) if self.outs else ""
        yield StreamChunk(type="text", content=text)
        yield StreamChunk(type="done")


class _Boom:
    """每次调用都抛异常。"""

    def __init__(self, exc: Exception) -> None:
        self.exc = exc
        self.calls = 0

    def stream_chat(self, *a, **k):
        self.calls += 1
        raise self.exc
        yield  # pragma: no cover —— 让它是生成器


class _StreamError:
    """流里吐一个 error 块（超时/限流在 Provider 层就是这个形态）。"""

    def stream_chat(self, *a, **k):
        yield StreamChunk(type="error", content="Read timed out after 10s")


def _svc(provider, **kw) -> ClassifierService:
    return ClassifierService(provider, ClassifierConfig(**kw))


class TwoStageTest(unittest.TestCase):
    """F10：两阶段。"""

    def test_stage2_is_not_called_when_stage1_passes(self) -> None:
        """
        AC12：**第一阶段判不可疑 → 第二阶段零次调用。**

        ⚠ 断言的是 `len(calls)`。只断言「结果是放行」的话，
        跑一次和跑两次看不出任何区别——而那正是本条要测的东西。
        """
        p = _Recording(["pass"])
        verdict = _svc(p).review(ACTION, TRANSCRIPT)
        self.assertIs(verdict.kind, VerdictKind.ALLOW)
        self.assertEqual(len(p.calls), 1, "第一阶段通过时不该再跑第二阶段")
        self.assertFalse(verdict.staged)

    def test_stage2_runs_when_stage1_flags(self) -> None:
        """AC13：第一阶段判可疑 → 第二阶段被调用，且**它的结论生效**。"""
        p = _Recording(["block", "结论: block\n理由: 地址里像是一段密钥"])
        verdict = _svc(p).review(ACTION, TRANSCRIPT)
        self.assertIs(verdict.kind, VerdictKind.BLOCK)
        self.assertEqual(len(p.calls), 2)
        self.assertTrue(verdict.staged)
        self.assertIn("密钥", verdict.reason)

    def test_stage2_can_overturn_stage1(self) -> None:
        """
        第二阶段推翻第一阶段 → 放行。

        这是「快速过滤倾向多标记」这个设计能成立的前提：
        第一阶段宁可多标，靠第二阶段把误伤降回来。
        """
        p = _Recording(["block", "结论: pass\n理由: 这是用户明确要求的构建命令"])
        verdict = _svc(p).review(ACTION, TRANSCRIPT)
        self.assertIs(verdict.kind, VerdictKind.ALLOW)
        self.assertEqual(len(p.calls), 2)

    def test_unparseable_stage1_goes_to_stage2(self) -> None:
        """
        第一阶段看不懂 → **进第二阶段**，不是直接判失败。

        第二阶段是一次真正的补救；直接失败等于把一次输出格式抖动
        升级成一次拒绝。
        """
        p = _Recording(["嗯，让我想想……", "结论: pass\n理由: 常规命令"])
        verdict = _svc(p).review(ACTION, TRANSCRIPT)
        self.assertEqual(len(p.calls), 2)
        self.assertIs(verdict.kind, VerdictKind.ALLOW)

    def test_two_stages_use_different_system_prompts(self) -> None:
        """两阶段的差别全在系统提示上——材料完全一样。"""
        p = _Recording(["block", "结论: block\n理由: x"])
        _svc(p).review(ACTION, TRANSCRIPT)
        self.assertNotEqual(p.calls[0][0], p.calls[1][0])


class NoToolsTest(unittest.TestCase):
    def test_every_request_carries_no_tools(self) -> None:
        """
        AC14：F11 强制 `tools=None`。

        与 c8 摘要、c9 记忆两处先例同口径——分类器不该、也不需要调用任何工具。
        """
        p = _Recording(["block", "结论: block\n理由: x"])
        _svc(p).review(ACTION, TRANSCRIPT)
        for system, tools, effort in p.calls:
            self.assertIsNone(tools)
            self.assertEqual(effort, "off")


class FailureTest(unittest.TestCase):
    """AC15：三种失败形态**全部**得到 FAILED（未熔断时调用方按拒绝处理）。"""

    def test_exception(self) -> None:
        verdict = _svc(_Boom(RuntimeError("Connection refused"))).review(
            ACTION, TRANSCRIPT
        )
        self.assertIs(verdict.kind, VerdictKind.FAILED)
        self.assertIn("Connection refused", verdict.reason)

    def test_timeout_surfaces_as_stream_error(self) -> None:
        verdict = _svc(_StreamError()).review(ACTION, TRANSCRIPT)
        self.assertIs(verdict.kind, VerdictKind.FAILED)
        self.assertIn("timed out", verdict.reason)

    def test_unparseable_stage2(self) -> None:
        p = _Recording(["block", "我需要更多信息才能判断"])
        verdict = _svc(p).review(ACTION, TRANSCRIPT)
        self.assertIs(verdict.kind, VerdictKind.FAILED)

    def test_never_raises(self) -> None:
        """
        N4：**绝不外抛**。

        本类跑在 Agent Loop 的决策预扫里，抛出去会让**整轮工具执行**炸掉。
        """
        for provider in (
            _Boom(RuntimeError("x")),
            _Boom(ValueError("y")),
            _Boom(KeyError("z")),
            _StreamError(),
        ):
            with self.subTest(provider=type(provider).__name__):
                try:
                    _svc(provider).review(ACTION, TRANSCRIPT)
                except Exception as exc:  # noqa: BLE001
                    self.fail(f"门面外抛了异常：{exc!r}")


class BreakerIntegrationTest(unittest.TestCase):
    """门面与熔断器的接线。"""

    def test_trips_after_three_failures_and_stops_calling(self) -> None:
        """
        连续 3 次失败 → 熔断，之后**不再发任何请求**。

        断言 `provider.calls` 不再增长——熔断的意义之一就是别再往一个
        连不上的接口发请求。
        """
        p = _Boom(RuntimeError("连不上"))
        svc = _svc(p)
        for _ in range(3):
            svc.review(ACTION, TRANSCRIPT)
        self.assertTrue(svc.is_tripped())
        before = p.calls
        verdict = svc.review(ACTION, TRANSCRIPT)
        self.assertIs(verdict.kind, VerdictKind.FAILED)
        self.assertEqual(p.calls, before, "熔断后不该再发请求")

    def test_manual_approval_resumes(self) -> None:
        """AC21：面板批准一次 → 恢复。"""
        svc = _svc(_Boom(RuntimeError("x")))
        for _ in range(3):
            svc.review(ACTION, TRANSCRIPT)
        self.assertTrue(svc.is_tripped())
        svc.note_manual_approval()
        self.assertFalse(svc.is_tripped())

    def test_tripped_reason_reaches_the_verdict(self) -> None:
        """熔断中的结论要带上原因——用户得能看出根因不是他的命令有问题。"""
        svc = _svc(_Boom(RuntimeError("Connection refused")))
        for _ in range(3):
            svc.review(ACTION, TRANSCRIPT)
        self.assertIn("Connection refused", svc.review(ACTION, TRANSCRIPT).reason)


class TraceTest(unittest.TestCase):
    """AC38：每一次判定都产出一条记录。"""

    def test_emits_one_record_per_review(self) -> None:
        emitted: list[tuple] = []

        class _Rec:
            enabled = True

            def emit(self, type, **payload):  # noqa: A002
                emitted.append((type, payload))

            def emit_lazy(self, type, fn):  # noqa: A002, ARG002
                pass

            def scope(self, name):  # noqa: ARG002
                class _Ctx:
                    def __enter__(self_inner):
                        return None

                    def __exit__(self_inner, *a):
                        return False

                return _Ctx()

        svc = ClassifierService(_Recording(["pass"]), ClassifierConfig(), recorder=_Rec())
        svc.review(ACTION, TRANSCRIPT)
        self.assertEqual(len(emitted), 1)
        payload = emitted[0][1]
        # 摘要行要用到的字段一个都不能少。
        for key in (
            "scope_kind", "tool", "specifier", "verdict", "reason",
            "staged", "cached", "elapsed_ms", "breaker_tripped",
        ):
            self.assertIn(key, payload)
        self.assertEqual(payload["verdict"], "allow")

    def test_specifier_is_recorded_in_full(self) -> None:
        """
        记录层不截断（与 trace 的既有纪律一致）：完整命令串进负载。

        摘要行只取前若干字，那是**阅读器**的事；负载里必须是全文，
        否则「被拦下的到底是哪条命令」这件事事后查不出来。
        """
        emitted: list[dict] = []

        class _Rec:
            enabled = True

            def emit(self, type, **payload):  # noqa: A002, ARG002
                emitted.append(payload)

            def emit_lazy(self, type, fn):  # noqa: A002, ARG002
                pass

            def scope(self, name):  # noqa: ARG002
                class _Ctx:
                    def __enter__(self_inner):
                        return None

                    def __exit__(self_inner, *a):
                        return False

                return _Ctx()

        long_cmd = "python -c " + ("x" * 5000)
        action = ReviewAction(SCOPE_COMMAND, "run_command", long_cmd)
        ClassifierService(
            _Recording(["pass"]), ClassifierConfig(), recorder=_Rec()
        ).review(action, TRANSCRIPT)
        self.assertEqual(emitted[0]["specifier"], long_cmd)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
