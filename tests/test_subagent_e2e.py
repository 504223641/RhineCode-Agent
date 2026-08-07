"""
子 Agent 的端到端验收（c13，覆盖 AC17a / AC19a-c / AC21b / AC23）。

## 与 `test_subagent_integration.py` 的分工

那个文件手工拼装协调层与服务，验的是**模块之间的接线**。
本文件走**真实装配链路 `build_app`**，验的是「装配层有没有把它们接对」——
这是单元测试证明不了的一类问题：每个零件都对，组装顺序错了照样跑不起来。

具体来说，本文件能抓到而单元测试抓不到的：

- `run_agent` 有没有真的注册进工具中心、且在 `session_start` 快照之前；
- 角色清单有没有真的进到发给模型的系统提示里；
- 子 Agent 的 `stable` 是不是角色正文（而不是主对话的八模块）；
- 结论有没有真的出现在**后续几轮**的请求体里。

## 判据取自「发给模型的东西」

断言对象是假 Provider 收到的 `system` 与 `messages` 原文，而不是中间状态。
理由：模型最终看到什么，才是这一章是否成立的唯一判据。
"""

from __future__ import annotations

import json
import tempfile
import time
import unittest
from pathlib import Path

from rhinecode.bootstrap import build_app
from rhinecode.config import Config
from rhinecode.provider.base import BaseProvider, StreamChunk, ToolCall
from rhinecode.trace import create_recorder

_ROLE = """---
name: finder
description: 需要在项目里查找信息时用它。
tools: read_file, glob_files
permission_mode: strict
---
你是查找员。最后一段必须是自包含的结论。
"""

# 子 Agent 的 stable 就是角色正文，用它的开头判断「这一轮是谁发的」。
_ROLE_BODY_HEAD = "你是查找员"


class _ScriptedProvider(BaseProvider):
    """
    主对话第 1 轮委派，之后说话；子 Agent 直接给结论。

    记下每一轮的 `system` / `messages` / `tools`，供断言。
    """

    def __init__(self, background: bool = False, sub_delay: float = 0.0) -> None:
        self.turns = 0
        self.systems: list[str] = []
        self.bodies: list[str] = []
        # 主对话那些轮次的请求体，**与 `bodies` 分开收**。
        #
        # ⚠ `bodies` 由主线程与子 Agent 线程**共同追加**，因此 `bodies[-1]`
        # 不一定是主对话最后那一轮——子 Agent 可能刚好在它之后发了请求。
        # 用 `bodies[-1]` 断言会得到一个**间歇性失败**的测试（实测约 1/8 概率），
        # 而失败信息看起来像产品出了问题（「结论怎么不在请求里」），极易误判。
        self.main_bodies: list[str] = []
        self.tool_names: list[list[str]] = []
        self._background = background
        self._sub_delay = sub_delay

    def stream_chat(self, messages, thinking_effort="off", tools=None, system=None):
        self.turns += 1
        self.systems.append(system or "")
        body = "\n".join(str(getattr(m, "content", "") or "") for m in messages)
        self.bodies.append(body)
        if not (system or "").startswith(_ROLE_BODY_HEAD):
            self.main_bodies.append(body)
        self.tool_names.append(
            sorted(t["function"]["name"] for t in (tools or []))
        )

        if (system or "").startswith(_ROLE_BODY_HEAD):
            if self._sub_delay:
                time.sleep(self._sub_delay)
            yield StreamChunk(type="text", content="结论：在 a.py 与 b.py 各有一处。")
            yield StreamChunk(type="done")
            return

        if self.turns == 1:
            yield StreamChunk(
                type="tool_call",
                tool_call=ToolCall(
                    id="c1",
                    name="run_agent",
                    arguments={
                        "type": "role",
                        "agent": "finder",
                        "task": "找出所有 X",
                        "background": self._background,
                    },
                ),
            )
            yield StreamChunk(type="done")
            return

        yield StreamChunk(type="text", content="收到。")
        yield StreamChunk(type="done")

    # ---- 便捷查询 ----

    def sub_turn_index(self) -> int:
        for i, s in enumerate(self.systems):
            if s.startswith(_ROLE_BODY_HEAD):
                return i
        return -1


class E2EBase(unittest.TestCase):
    def _build(self, provider, recorder=None):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        user_dir = root / "user"
        (user_dir / "agents").mkdir(parents=True)
        (user_dir / "agents" / "finder.md").write_text(_ROLE, encoding="utf-8")

        cfg = Config(protocol="deepseek", model="m", api_key="k", base_url="")
        result = build_app(
            cfg,
            user_dir=user_dir,
            provider_factory=lambda c: provider,
            recorder=recorder,
        )
        self.addCleanup(result.cleanup, "normal_exit")
        return result, root

    @staticmethod
    def _settle(manager, timeout: float = 5.0) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            tasks = manager.subagent_service.tasks.snapshot()
            if tasks and all(t.status.is_terminal for t in tasks):
                return
            time.sleep(0.01)


class ForegroundE2ETest(E2EBase):
    """前台委派：装配 → 委派 → 子 Agent 跑完 → 结论作为工具结果回灌。"""

    def setUp(self) -> None:
        self.provider = _ScriptedProvider()
        self.result, _ = self._build(self.provider)
        self.manager = self.result.manager
        list(self.manager.submit_user_message("找一下"))

    def test_run_agent_registered(self) -> None:
        self.assertIn("run_agent", self.result.tool_registry.names())

    def test_agent_index_reaches_the_model(self) -> None:
        """
        角色清单要真的出现在发给模型的**系统提示**里。

        只断言 `_agent_index_text()` 非空是不够的——那证明不了它被拼进了
        `build_default_prompt` 的产物、更证明不了它进了 `system` 参数。
        """
        self.assertIn("finder", self.provider.systems[0])
        self.assertIn("而不是自己动手做", self.provider.systems[0])

    def test_subagent_stable_is_the_role_body_only(self) -> None:
        """AC7a：子 Agent 的 stable 是角色正文，不含主对话的八模块。"""
        idx = self.provider.sub_turn_index()
        self.assertGreaterEqual(idx, 0, "子 Agent 应当真的跑过")
        self.assertEqual(self.provider.systems[idx], "你是查找员。最后一段必须是自包含的结论。")

    def test_subagent_toolset_is_the_whitelist(self) -> None:
        idx = self.provider.sub_turn_index()
        self.assertEqual(self.provider.tool_names[idx], ["glob_files", "read_file"])

    def test_delegation_tool_visible_to_main_not_to_sub(self) -> None:
        idx = self.provider.sub_turn_index()
        self.assertIn("run_agent", self.provider.tool_names[0])
        self.assertNotIn("run_agent", self.provider.tool_names[idx])
        self.assertNotIn("load_skill", self.provider.tool_names[idx])

    def test_conclusion_returned_as_tool_result(self) -> None:
        tasks = self.manager.subagent_service.tasks.snapshot()
        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0].status.value, "completed")
        self.assertIn("a.py", tasks[0].conclusion)
        # 工具结果回灌进了第 2 轮的请求体
        self.assertIn("a.py", self.provider.main_bodies[-1])

    def test_main_engine_mode_unchanged(self) -> None:
        """AC14b 的端到端侧判据：角色声明 strict，主引擎仍是 default。"""
        self.assertEqual(self.manager.permission_engine.mode.value, "default")

    def test_agents_report_renders(self) -> None:
        report = self.manager.agents_report()
        self.assertIn("finder", report)
        self.assertIn("严格", report)
        self.assertIn(
            self.manager.subagent_service.tasks.snapshot()[0].task_id, report
        )


class BackgroundE2ETest(E2EBase):
    """AC17a / AC19：后台委派 → 通知 → 交付 → 后续几轮仍能引用。"""

    def setUp(self) -> None:
        self.provider = _ScriptedProvider(background=True, sub_delay=0.15)
        self.result, _ = self._build(self.provider)
        self.manager = self.result.manager

    def test_background_does_not_block_main(self) -> None:
        """
        `background=true` 时主对话不等那 0.15 秒，且**循环也不为它停留**。

        c13 修订注记：这条原本断言回灌文本含「后台」。新语义下委派**永远**
        立即返回（所以「转入后台」这个说法本身没了），`background` 表达的
        是「这次我不要这个结果」——文案随之改成「本轮不会为它停留」。
        """
        started = time.monotonic()
        list(self.manager.submit_user_message("找一下"))
        elapsed = time.monotonic() - started

        self.assertLess(elapsed, 0.12, "委派不该阻塞主对话")
        self.assertIn("不会为它停留", self.provider.main_bodies[-1])
        self._settle(self.manager)

    def test_two_consumption_lines_are_independent(self) -> None:
        list(self.manager.submit_user_message("找一下"))
        self._settle(self.manager)

        self.assertEqual(len(self.manager.drain_subagent_notifications()), 1)
        self.assertEqual(self.manager.drain_subagent_notifications(), ())
        # 通知取走了，交付线照样能拿到
        list(self.manager.submit_user_message("继续"))
        self.assertIn("在 a.py 与 b.py", self.provider.main_bodies[-1])

    def test_conclusion_survives_to_the_round_after_next(self) -> None:
        """
        **AC19c——本章最容易做错的一条。**

        只验「下一轮能引用」是不够的：用一次性的系统提醒实现也能过那一条，
        但第三轮就会失败。这条断言是「结论必须进历史」这个决策的唯一有效判据。
        """
        list(self.manager.submit_user_message("找一下"))
        self._settle(self.manager)

        list(self.manager.submit_user_message("继续"))
        self.assertIn("在 a.py 与 b.py", self.provider.main_bodies[-1], "第二轮应含结论")

        list(self.manager.submit_user_message("再继续"))
        self.assertIn("在 a.py 与 b.py", self.provider.main_bodies[-1], "第三轮仍应含结论")

    def test_conclusion_delivered_exactly_once(self) -> None:
        list(self.manager.submit_user_message("找一下"))
        self._settle(self.manager)
        list(self.manager.submit_user_message("继续"))
        list(self.manager.submit_user_message("再继续"))

        self.assertEqual(
            self.provider.main_bodies[-1].count("<subagent-result"),
            1,
            "结论只该被交付一次，重复会让同一段内容在历史里出现多遍",
        )


class TraceE2ETest(E2EBase):
    """AC23：从 trace 能完整复现一次委派，且与主对话分作用域。"""

    def setUp(self) -> None:
        self.provider = _ScriptedProvider()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.trace_path = Path(tmp.name) / "t.jsonl"
        recorder = create_recorder(self.trace_path)
        self.result, _ = self._build(self.provider, recorder=recorder)
        list(self.result.manager.submit_user_message("找一下"))
        self.result.cleanup("normal_exit")
        self.records = [
            json.loads(line)
            for line in self.trace_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]

    def test_lifecycle_events_present(self) -> None:
        types = {r.get("type") for r in self.records}
        self.assertIn("subagent_start", types)
        self.assertIn("subagent_end", types)

    def test_subagent_has_its_own_scope(self) -> None:
        scopes = {r.get("scope") for r in self.records}
        self.assertTrue(
            any(s and s.startswith("subagent:") for s in scopes),
            f"应有 subagent: 作用域，实际：{scopes}",
        )

    def test_subagent_requests_not_counted_as_main(self) -> None:
        """
        AC21b / AC23：子 Agent 的模型请求**不算进主对话**。

        混在一起的话，读 trace 的人会看到「用户只说了一句话，却发了三轮请求」
        的假象，无从判断哪一轮是谁发的。
        """
        sub = [
            r for r in self.records
            if r.get("type") == "api_request"
            and str(r.get("scope", "")).startswith("subagent:")
        ]
        main = [
            r for r in self.records
            if r.get("type") == "api_request" and r.get("scope") == "main"
        ]
        self.assertGreaterEqual(len(sub), 1)
        self.assertGreaterEqual(len(main), 1)

    def test_start_event_carries_enough_to_reproduce(self) -> None:
        start = next(r for r in self.records if r.get("type") == "subagent_start")
        for key in ("kind", "agent", "task_id", "task", "tool_count"):
            with self.subTest(field=key):
                self.assertIn(key, start)

    def test_end_event_carries_outcome(self) -> None:
        end = next(r for r in self.records if r.get("type") == "subagent_end")
        self.assertEqual(end.get("status"), "completed")
        for key in ("task_id", "turns", "stop_reason"):
            with self.subTest(field=key):
                self.assertIn(key, end)


if __name__ == "__main__":
    unittest.main()
