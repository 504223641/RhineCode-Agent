"""
`/agents` 任务段的有界性与会话代号过滤（C10-b）的护栏。

## 这条缺口的真实形态与原报告不同

原报告写「任务表无界增长 → 长会话内存单调增长」。R3 实测量了增速：

    空表     64 字节 ·  1 条 11,253 · 10 条 39,624 · 50 条 161,376 · 200 条 624,018
    /clear（begin_session）之后：条数=200  占用=624,018 字节
    全文件里的删除操作：一处都没有

**约 3 KB / 次委派。作为内存问题它可以忽略**——一次一千次委派的超长会话也只有
3 MB。真实后果在别的两处，本文件钉的是第一处：

- **`/agents` 的输出无界，且含已清空会话的任务。** 一次跑过 60 次委派的会话会
  刷出 60 行，大半来自 `/clear` 之前——而用户敲 `/agents` 想知道的是
  「**现在**有谁在跑」。
- 对照 `recent_tools` 是有界的（`[-ACTIVITY_RECENT_LIMIT:]`）：
  **「一切展示都有界」这条只落到了列表层，没落到字典层。**

## ⚠ 本轮只治展示，不治存储

删记录有三条约束（运行中的一条都不能删、终态但未交付的不能删、删了会让完成
通知失去成本数字），而它换来的只有 3 MB——不值得为它引入一个新的成对维护点。
`StorageUntouchedTest` 那组钉住这个「刻意」。
"""

import unittest

from rhinecode.subagents.report import TASK_LIST_LIMIT, _task_section
from rhinecode.subagents.tasks import TaskRecord, TaskStatus


def _record(task_id: str, *, epoch: int, terminal: bool) -> TaskRecord:
    return TaskRecord(
        task_id=task_id,
        kind="role",
        agent_name="explorer",
        task_text=f"任务 {task_id}",
        status=TaskStatus.COMPLETED if terminal else TaskStatus.RUNNING,
        epoch=epoch,
    )


def _render(records, epoch) -> str:
    return "\n".join(_task_section(tuple(records), epoch))


class EpochFilterTest(unittest.TestCase):
    def test_only_current_epoch_is_listed(self) -> None:
        """
        `/clear` 之前的任务不再逐条出现在列表里。

        `begin_session` 只把代号 +1、一条记录都不删，所以不过滤的话用户清空之后
        敲 `/agents` 看到的仍是上一段对话的任务。
        """
        records = [
            _record("old-1", epoch=0, terminal=True),
            _record("old-2", epoch=0, terminal=True),
            _record("new-1", epoch=1, terminal=False),
        ]
        text = _render(records, 1)

        self.assertIn("new-1", text)
        self.assertNotIn("old-1", text)
        self.assertNotIn("old-2", text)

    def test_earlier_tasks_get_a_one_line_summary(self) -> None:
        """
        ⚠ **旧任务不逐条列，但也不能完全不提。**

        完全不提会让「我明明派过活」显得可疑——用户记得自己委派过，
        而列表里一条都没有。一行汇总同时满足「此刻要的是现在」与「别装作没发生」。
        """
        records = [
            _record("old-1", epoch=0, terminal=True),
            _record("old-2", epoch=0, terminal=True),
            _record("new-1", epoch=1, terminal=False),
        ]
        text = _render(records, 1)
        self.assertIn("另有 2 条属于此前的会话", text)

    def test_no_epoch_means_old_behaviour(self) -> None:
        """
        反证：调用方不给代号时**逐字退回旧行为**（列全部、不筛不截）。

        这一支不是摆设——既有的直接调用（测试、将来别的入口）不该被迫改签名，
        而一个「不给代号就什么都不显示」的实现会让它们静默变空。
        """
        records = [
            _record("old-1", epoch=0, terminal=True),
            _record("new-1", epoch=1, terminal=False),
        ]
        text = _render(records, None)
        self.assertIn("old-1", text)
        self.assertIn("new-1", text)
        self.assertNotIn("此前的会话", text)

    def test_empty_current_epoch_says_so(self) -> None:
        """清空之后还没派过活：那句话要说「本次对话」，不是「本次运行」。"""
        records = [_record("old-1", epoch=0, terminal=True)]
        text = _render(records, 1)
        self.assertIn("本次对话尚未发起过委派", text)
        self.assertIn("另有 1 条", text)


class BoundedTest(unittest.TestCase):
    def test_finished_tasks_are_capped(self) -> None:
        """终态任务超过上限时只列最近的那些，并给一行「另有 N 条未列出」。"""
        records = [
            _record(f"t-{i}", epoch=1, terminal=True)
            for i in range(TASK_LIST_LIMIT + 7)
        ]
        text = _render(records, 1)

        self.assertIn("（另有 7 条已结束的任务未列出）", text)
        # 最早的被裁掉、最近的还在
        self.assertNotIn("t-0 ", text)
        self.assertIn(f"t-{TASK_LIST_LIMIT + 6}", text)
        # 总数仍然如实报出——列表被截断了，但「一共几条」不能跟着缩水
        self.assertIn(f"本次对话的任务（{TASK_LIST_LIMIT + 7} 个）：", text)

    def test_running_tasks_are_never_hidden(self) -> None:
        """
        ⚠ **本条是这份文件最要紧的一条：运行中的任务永不被裁掉。**

        用户敲 `/agents` 的第一诉求就是「还有谁在跑」。把正在跑的那条截掉等于把
        这个命令最有用的部分砍了——**而且不报错**，用户只会以为委派没发出去。

        构造成「一堆终态的把上限占满 + 几条运行中的」，一个朴素的
        `records[-N:]` 实现会当场红。
        """
        records = [
            _record(f"done-{i}", epoch=1, terminal=True)
            for i in range(TASK_LIST_LIMIT + 10)
        ]
        records += [_record(f"live-{i}", epoch=1, terminal=False) for i in range(3)]

        text = _render(records, 1)
        for i in range(3):
            with self.subTest(running=i):
                self.assertIn(f"live-{i}", text)

    def test_creation_order_is_preserved_across_the_cap(self) -> None:
        """
        ⚠ 超限前后**顺序不能变**。

        一个朴素的「运行中的 + 最近 N 条终态的」拼接实现会把所有运行中的提到
        最前面，于是同一份列表在跨过上限的那一刻突然重排——用户会以为任务的
        先后关系变了。判据比较的是两条记录在文本里的先后。
        """
        records = [_record("live-early", epoch=1, terminal=False)]
        records += [
            _record(f"done-{i}", epoch=1, terminal=True)
            for i in range(TASK_LIST_LIMIT + 3)
        ]
        text = _render(records, 1)

        self.assertLess(
            text.index("live-early"),
            text.index(f"done-{TASK_LIST_LIMIT + 2}"),
            "截断之后顺序被打乱了：先创建的任务跑到后创建的后面去了",
        )

    def test_limit_is_not_shared_with_activity_limit(self) -> None:
        """
        ⚠ 反证：`TASK_LIST_LIMIT` 与 `ACTIVITY_RECENT_LIMIT` **刻意是两个数**。

        后者管的是活动区展开时每个队员列几次工具调用（很窄的视图，5 条正好），
        前者管的是「本段对话发起过哪些委派」——一次十几步的任务派出七八个队员
        是正常的，卡到 5 会把用户正在找的那条挡掉。合一之后，调其中一个必然把
        另一个调坏，而两边都不报错。
        """
        from rhinecode.subagents.tasks import ACTIVITY_RECENT_LIMIT

        self.assertNotEqual(TASK_LIST_LIMIT, ACTIVITY_RECENT_LIMIT)
        self.assertGreater(TASK_LIST_LIMIT, ACTIVITY_RECENT_LIMIT)


class StorageUntouchedTest(unittest.TestCase):
    """
    ⚠ **本轮只治展示，一条记录都不删——这是刻意的，别当成没做完顺手补上。**

    删记录有三条约束，每条都对应一处会静默出错的地方：
    ① 运行中的记录一条都不能删（`gate.py` 在读它判断「还要不要等」）；
    ② 终态但未交付的不能删（`take_deliverables` 靠它），而成对维护点
       「新增会话切换入口」明确警告过：**别改成「切换时把当前任务标成已交付」**
       ——取消是异步的，被取消的子 Agent 可能在切换返回之后才走到终态；
    ③ 删掉记录会让完成通知失去成本数字（成对维护点「活动区终态行与历史区完成
       通知的成本数字必须同源 → `TaskManager.row_of`」）。

    换来的只有约 3 MB（一千次委派的超长会话）。**不值得为它引入一个新的成对
    维护点。**
    """

    def test_render_does_not_mutate_the_records(self) -> None:
        records = [
            _record("old-1", epoch=0, terminal=True),
            _record("new-1", epoch=1, terminal=False),
        ]
        _render(records, 1)
        self.assertEqual(len(records), 2)
        self.assertEqual(records[0].task_id, "old-1")

    def test_task_manager_still_has_no_delete(self) -> None:
        """
        `tasks.py` 里仍然一处删除都没有。

        判据钉的是「本轮没有偷偷加淘汰逻辑」。将来真要治存储时这条会红，
        那时请连同上面 docstring 里的三条约束一起处理，**并补一条反证**：
        断言一条「终态但未交付」的旧记录**不会**被淘汰。
        """
        import pathlib

        source = (
            pathlib.Path(__file__).resolve().parents[1]
            / "rhinecode"
            / "subagents"
            / "tasks.py"
        ).read_text(encoding="utf-8")

        for pattern in ("del self._tasks", ".pop(", "self._tasks.clear()"):
            with self.subTest(pattern=pattern):
                self.assertNotIn(pattern, source)


if __name__ == "__main__":
    unittest.main()
