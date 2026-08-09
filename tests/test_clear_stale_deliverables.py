"""
`/clear` 之后，上一段对话的子 Agent 结论**不得**流进新对话（真实模型验收发现）。

## 现场

真实模型验收（2026-08-10）里，`/clear` 之后紧接着提一个新问题，模型的回答是：

    「explorer 已经回来了，auditor 还在跑字典档、优先级低……」

它坚称上一段对话的子 Agent 还在跑，接着给自己发消息（失败）、查了一份空的
共享清单，然后就此收工——**一次委派都没发起**。

trace 上的物证是一个对不上的数字：`/clear` 之后的第一轮请求是**「消息 3 条」**，
而清空之后本该只有 1 条（用户那句）。多出来的两条，正是上一段对话遗留的
`<subagent-result>` 块。

## 机制（三处各自都对，合起来漏了）

1. `TaskManager.cancel_all()` 只对 **RUNNING** 的任务置取消信号——
   已经终态的它碰都不碰（这是对的，终态任务没什么可取消的）；
2. `take_deliverables()` 的判据是 `is_terminal and not delivered`，
   **不区分这条任务属于哪一段对话**；
3. `clear()` 清空了 `history`，却把任务记录原样留在 `TaskManager` 里。

于是 `/clear` 之后第一次 `_run()` 调 `_deliver_subagent_results()`，
就把上一段对话的结论追加进了本该空白的历史。

`conversation.clear()` 里那段注释声称防的正是这件事（「它们跑完之后会把结论
交付进 history，而那时用户已经清空了对话，凭空多出一段来路不明的内容」），
但它只防住了「**还在跑的**」，没防住「**已经跑完、还没交付的**」。

⚠ C15 的待命队员让这条路径**更容易**被走到，而不是更难：`_clear_team()` 会
唤醒待命队员让它们的线程退出，那恰好把它们从「待命」变成「终态且未交付」。

## 为什么不能只在 clear 那一刻把它们标成已交付

取消是**异步**的：`cancel_all()` 只置信号，被取消的子 Agent 可能在 `clear()`
返回之后才真正走到终态。那时它仍然是「终态且未交付」，照样会漏进新对话。
判据必须与**时机无关**——所以用会话代（epoch）而不是「清空时标记一遍」。
本文件最后一条用例专门钉住这个时序。
"""

from __future__ import annotations

import unittest

from rhinecode.subagents.tasks import KIND_ROLE, TaskManager, TaskStatus


class StaleDeliverableTest(unittest.TestCase):
    """任务表层：跨会话的结论不得再被取走。"""

    def setUp(self) -> None:
        self.tasks = TaskManager()

    def _finished(self, name: str = "explorer") -> None:
        """造一条「已完成、尚未交付」的任务。"""
        record = self.tasks.create(KIND_ROLE, name, "调研 X")
        self.tasks.finish(record.task_id, TaskStatus.COMPLETED, conclusion="结论 X")
        return record

    def test_conclusion_is_deliverable_before_clear(self) -> None:
        """前提：不清空的话它当然该被交付——否则下面那条证明不了什么。"""
        self._finished()
        self.assertEqual(len(self.tasks.take_deliverables()), 1)

    def test_conclusion_does_not_survive_a_session_switch(self) -> None:
        """**本文件的主判据。** 会话切换之后，上一段的结论再也取不走。"""
        self._finished()
        self.tasks.begin_session()

        self.assertEqual(
            self.tasks.take_deliverables(),
            (),
            "上一段对话的结论不得流进新对话",
        )

    def test_new_tasks_after_the_switch_still_deliver(self) -> None:
        """**反证。** 别把交付整个关掉了——新会话里新建的任务照常交付。"""
        self._finished("old")
        self.tasks.begin_session()
        self._finished("new")

        taken = self.tasks.take_deliverables()
        self.assertEqual([r.agent_name for r in taken], ["new"])

    def test_a_task_that_finishes_after_the_switch_still_cannot_leak(self) -> None:
        """
        **时序护栏：取消是异步的。**

        `cancel_all()` 只置信号，被取消的子 Agent 完全可能在 `clear()` 返回
        **之后**才真正走到终态。那一刻它是「终态且未交付」，若判据依赖
        「清空时标记一遍」就会漏——所以判据必须与时机无关。

        少了这条用例，一个「在 clear 里把当前全部任务标成 delivered」的实现
        会让上面两条全绿，而真实世界里最常见的那条竞态原样存在。
        """
        record = self.tasks.create(KIND_ROLE, "slowpoke", "一个跑得慢的活")
        self.tasks.begin_session()  # 用户在它还没跑完时清空了对话
        # ——切换之后它才走到终态——
        self.tasks.finish(record.task_id, TaskStatus.COMPLETED, conclusion="迟到的结论")

        self.assertEqual(
            self.tasks.take_deliverables(),
            (),
            "切换之后才跑完的旧任务，同样不得流进新对话",
        )


class ClearDoesNotLeakIntoNextRequestTest(unittest.TestCase):
    """
    协调层：`/clear` 之后发出的第一次请求里，不得含上一段对话的结论。

    这一层才是缺陷真正显形的地方——任务表那几条证明「取不走了」，
    这条证明「**模型确实看不到了**」。判据取自假 Provider 收到的 `messages`，
    与 `test_subagent_e2e.py` 同一口径：模型最终看到什么，才是唯一判据。
    """

    def _build(self):
        import tempfile
        from pathlib import Path

        from rhinecode.bootstrap import build_app
        from rhinecode.config import Config
        from rhinecode.provider.base import BaseProvider, StreamChunk

        class _Recorder(BaseProvider):
            def __init__(self) -> None:
                self.bodies: list[str] = []

            def stream_chat(self, messages, thinking_effort="off", tools=None, system=None):
                self.bodies.append(
                    "\n".join(str(getattr(m, "content", "") or "") for m in messages)
                )
                yield StreamChunk(type="text", content="好的。")
                yield StreamChunk(type="done")

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        user_dir = Path(tmp.name) / "user"
        (user_dir / "agents").mkdir(parents=True)

        provider = _Recorder()
        cfg = Config(protocol="deepseek", model="m", api_key="k", base_url="")
        result = build_app(
            cfg, user_dir=user_dir, provider_factory=lambda c: provider
        )
        self.addCleanup(result.cleanup, "normal_exit")
        return result.manager, provider

    def test_stale_conclusion_never_reaches_the_model_after_clear(self) -> None:
        manager, provider = self._build()
        tasks = manager.subagent_service.tasks

        # 造一条「上一段对话里跑完、还没交付」的任务
        record = tasks.create(KIND_ROLE, "explorer", "调研 X")
        tasks.finish(
            record.task_id,
            TaskStatus.COMPLETED,
            conclusion="上一段对话的结论：RETRY_LIMIT 在三个文件里",
        )

        manager.clear()
        list(manager.submit_user_message("这是一个全新的问题"))

        body = provider.bodies[-1]
        self.assertNotIn(
            "<subagent-result",
            body,
            "清空之后的第一次请求不得含上一段对话的结论块",
        )
        self.assertNotIn("上一段对话的结论", body)
        self.assertIn("这是一个全新的问题", body, "用户这句本身当然要在")

    def test_a_fresh_conclusion_after_clear_still_reaches_the_model(self) -> None:
        """
        **反证。** 别把交付整个关掉了——`/clear` **之后**产生的结论照常送达。

        少了这条，一个「clear 之后永不交付」的实现也能让上一条通过，
        而那会让 `/clear` 静默废掉子 Agent 的整条回流链路。
        """
        manager, provider = self._build()
        manager.clear()

        tasks = manager.subagent_service.tasks
        record = tasks.create(KIND_ROLE, "explorer", "新的调研")
        tasks.finish(
            record.task_id, TaskStatus.COMPLETED, conclusion="新会话里的结论"
        )

        list(manager.submit_user_message("汇报一下"))

        body = provider.bodies[-1]
        self.assertIn("<subagent-result", body)
        self.assertIn("新会话里的结论", body)


if __name__ == "__main__":
    unittest.main()
