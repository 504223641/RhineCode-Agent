"""
底部状态栏的**上下文用量在回合进行中就跟着涨**（不是只在收工时刷一次）。

## 这条护栏对应的真实问题

用户在 Plan Mode 下反馈「上下文用量一直没变化，直到确认计划切回 auto 才更新」。
查 trace（`G:\\Rhine-test` 那份）的结论是：

- 进入规划 `01:48:21` 状态栏 `0/976.6K`；
- 中间 5 轮请求、4 次澄清面板、历时 92 秒——**一条 `status_bar` 事件都没有**；
- `01:50:08` 计划获批那一刻才跳到 `8.7K`。

而那次跳变**不是**「切回 auto 顺带更新了用量」：审批回调会调
`_notify_preset_change()` 去刷模式标记，用量只是搭了那趟车。换句话说
**用量本身从来没有自己的刷新点**，`_do_stream` 的 `finally` 是唯一一处。

auto 模式下同样冻结，只是一个回合通常几十秒就结束、不容易被察觉；
Plan Mode 把「一次运行」拉长到几分钟，问题才显形。

## 判据为什么取「两个观察点之间的增量」

不能只断言「刷过至少一次」——开流前后本来就有别的刷新路径（模式切换、
Skill 激活、子 Agent 计数轮询）。要证明的是**回合中途、随迭代推进**在刷，
所以由生成器自己在两次 PROGRESS 之间记下「到此为止刷了几次」，
断言后一个观察点严格大于前一个。

把 `_do_stream` 里 PROGRESS 分支那句 `call_from_thread(self._refresh_status)`
删掉，本条当场红（两个观察点相等）——变异实测确认过。
"""

from __future__ import annotations

import unittest

from rhinecode.agent.events import AgentEvent, AgentEventType, StopReason


class MidRunRefreshTest(unittest.IsolatedAsyncioTestCase):
    """回合进行中，每一轮迭代都要把状态栏重算一次。"""

    async def test_status_bar_refreshes_between_iterations(self) -> None:
        from tests.test_command_tui import _make_app

        app, manager = _make_app()

        # 每次 `_refresh_status()` 都会无条件调一次它取用量（返回 None 也照调），
        # 因此它的调用次数就是状态栏的刷新次数。
        refreshes: list[int] = []
        manager.context_status_line = lambda: refreshes.append(1)

        # 观察点：生成器在**自己这个 Worker 线程**里推进，而 `call_from_thread`
        # 是阻塞调用——所以 yield 返回时，该轮触发的刷新一定已经完成。
        seen: list[int] = []

        def gen():
            yield AgentEvent(type=AgentEventType.PROGRESS)
            seen.append(len(refreshes))
            yield AgentEvent(type=AgentEventType.PROGRESS)
            seen.append(len(refreshes))
            yield AgentEvent(type=AgentEventType.FINISHED, stop_reason=StopReason.COMPLETED)

        async with app.run_test() as pilot:
            app._start_stream_worker(gen())
            await app.workers.wait_for_complete()
            await pilot.pause()

        self.assertEqual(len(seen), 2, "两个观察点都要被走到")
        self.assertGreaterEqual(seen[0], 1, "第一轮迭代就该刷一次")
        self.assertGreater(
            seen[1],
            seen[0],
            "两轮迭代之间必须又刷了一次——否则用量在整个回合里是冻结的，"
            "Plan Mode 那种长回合会一直显示进入规划那一刻的旧值",
        )


if __name__ == "__main__":
    unittest.main()
