"""
工具活动归并的**整轮**验收（tui-activity-fold 扩展 T37，端到端场景 1–3）。

与 `test_tui_batch.py` 的区别：那个文件直接调 `HistoryView` 的方法，验的是
**组件行为**；本文件把 `AgentEvent` 序列喂进 `_do_stream`，验的是**接线**
——事件怎么流到组件上、批次在哪一步被封闭、写操作有没有被漏进聚合行。

## 为什么不起 e2e 宿主

`tests/e2e/scripts.py` 里备了两个可供手工驱动的剧本
（`FOLD_MIXED_RUN` / `FOLD_WITH_FAILURE`），手测时直接 `--script` 引用即可。
但自动化用例走 `_do_stream` 这条更轻的路：它跑在同一个进程里、不起子进程，
而要验的东西（事件 → 组件的接线）完全一样。宿主那条路径的价值在于真实
Provider 与真实权限管线，那些在别的文件里已经各有护栏。

## ⚠ 本文件最要紧的一条

`MixedRunTest.test_write_file_is_never_folded_away` —— 一次运行里既有检索
又有写文件时，**写文件那一行在折叠状态下必须始终可见**。这是 spec 那条
安全判据的整轮落点：被折叠的永远只是「读」。
"""

import unittest

from rhinecode.agent.events import AgentEvent, AgentEventType, StopReason
from rhinecode.provider.base import ToolCall
from rhinecode.tools.base import ToolResult
# 颜色断言的归一化助手在组件级那份测试里（两种渲染通路给出的样式串形态不同），
# 两处共用一份，别各写一个。
from tests.test_tui_batch import has_color
from rhinecode.tui.widgets import (
    DETAIL_FOLDED,
    DETAIL_ITEMS,
    HistoryView,
    ToolBatchWidget,
    ToolCallWidget,
)


def call(tid: str, name: str, **arguments) -> ToolCall:
    return ToolCall(id=tid, name=name, arguments=arguments)


def ran(tc: ToolCall, summary: str, ok: bool = True, output: str = "") -> "list[AgentEvent]":
    """一次完整的工具调用：开始 + 结果。"""
    return [
        AgentEvent(type=AgentEventType.TOOL_START, tool_call=tc),
        AgentEvent(
            type=AgentEventType.TOOL_RESULT,
            tool_call=tc,
            tool_result=ToolResult(ok=ok, output=output or summary, summary=summary),
        ),
    ]


def said(text: str) -> AgentEvent:
    return AgentEvent(type=AgentEventType.TEXT, text=text)


DONE = AgentEvent(type=AgentEventType.FINISHED, stop_reason=StopReason.COMPLETED)


class _RunBase(unittest.IsolatedAsyncioTestCase):
    """把一串事件喂进 `_do_stream`，跑完后在**应用仍活着**时执行断言。"""

    async def _drive(self, events: "list[AgentEvent]", after) -> None:
        """
        :param events: 要喂进 `_do_stream` 的事件序列
        :param after: `async (app, view, pilot) -> None`，在 `run_test` 块**内**
            执行的断言

        ⚠ **断言必须在块内做。** `run_test` 一退出应用就关了，此后任何
        `query` 都会抛 `ScreenStackError: No screens on stack`——而那看起来
        像「组件没建出来」，会把人往错误的方向带（实现期真踩过）。
        """
        from tests.test_command_tui import _make_app

        app, _ = _make_app()

        def gen():
            yield from events

        async with app.run_test(size=(120, 40)) as pilot:
            view = app.query_one(HistoryView)
            # 与真实启动同序：两份映射都在事件流开始之前灌好。
            # ⚠ 必须在这里设而不是靠 `on_mount`——测试用的假管理器两份都返回
            # 空字典（模拟「未启用工具能力」那一支）。
            view.set_primary_args(
                {
                    "read_file": "path",
                    "write_file": "path",
                    "glob_files": "pattern",
                    "grep_content": "pattern",
                }
            )
            view.set_fold_groups(
                {
                    "glob_files": ("glob", "查找文件 {n} 次"),
                    "grep_content": ("grep", "搜索内容 {n} 次"),
                    "read_file": ("read", "读取 {n} 个文件"),
                }
            )
            app._start_stream_worker(gen())
            await app.workers.wait_for_complete()
            await pilot.pause()
            await after(app, view, pilot)


def visible_kinds(view: HistoryView) -> "list[str]":
    """屏幕上**可见**的历史区子节点类型——用户真正看到的东西。"""
    container = view.query_one("#history-messages")
    return [type(c).__name__ for c in container.children if c.display]


def batch_texts(view: HistoryView) -> "list[str]":
    return [b.summary_text() for b in view.query(ToolBatchWidget)]


class MixedRunTest(_RunBase):
    """
    端到端场景 1：三个检索 → 正文 → 写文件。

    三者缺一不可：只有检索验不出「什么时候该断开」，只有检索+正文验不出
    「写操作没有被藏进聚合行」——而后者正是折叠的安全底线。
    """

    def _events(self):
        return [
            said("我先看看项目里有什么。"),
            *ran(call("c1", "glob_files", pattern="src/**/*.py"), "共 2 个文件"),
            *ran(call("c2", "grep_content", pattern="def "), "共 5 处匹配"),
            *ran(call("c3", "read_file", path="src/app.py"), "读取 2 行"),
            # 这段正文是**批次的断开点**
            said("看明白了，src 下有两个模块。我来加一个说明文件。"),
            *ran(call("c4", "write_file", path="NOTES.md"), "新建 · 3 行"),
            said("写好了。"),
            DONE,
        ]

    async def test_three_retrievals_collapse_into_one_batch(self) -> None:
        """AC1：连续三次检索在屏幕上只占**一行**。"""
        async def check(app, view, pilot):
            batches = list(view.query(ToolBatchWidget))
            self.assertEqual(len(batches), 1, "三次检索应当只产生一个批次")
            self.assertEqual(
                batches[0].summary_text(),
                "查找文件 1 次 · 搜索内容 1 次 · 读取 1 个文件",
            )

        await self._drive(self._events(), check)

    async def test_write_file_is_never_folded_away(self) -> None:
        """
        **本文件最要紧的一条**（AC3）：写文件那一行在折叠状态下始终可见。

        被折叠的永远只是「读」——写操作若被静默收进聚合行，用户**看不出来**。
        """
        async def check(app, view, pilot):
            kinds = visible_kinds(view)
            self.assertIn("ToolCallWidget", kinds, "写文件行必须可见")
            # 那个可见的工具行必须真的是写文件，而不是漏网的检索
            visible_tools = [
                c
                for c in view.query_one("#history-messages").children
                if isinstance(c, ToolCallWidget) and c.display
            ]
            self.assertEqual(len(visible_tools), 1)
            self.assertIsNone(visible_tools[0].batch, "写文件不得属于任何批次")

        await self._drive(self._events(), check)

    async def test_text_between_runs_closes_the_batch(self) -> None:
        """AC1：正文封闭批次——聚合语里不该混进正文之后发生的调用。"""
        async def check(app, view, pilot):
            batch = view.query_one(ToolBatchWidget)
            self.assertTrue(batch.closed)
            self.assertNotIn("写", batch.summary_text())

        await self._drive(self._events(), check)

    async def test_screen_is_dramatically_shorter_than_before(self) -> None:
        """
        归并的**收益**本身要有判据，否则「做了但没省几行」也能全绿。

        四次调用改造前 = 8 行（每次标题 + 结果）；现在折叠态屏幕上是
        「聚合行 + 写文件行」共 2 个可见块，外加三段正文。
        """
        async def check(app, view, pilot):
            kinds = visible_kinds(view)
            tool_ish = [k for k in kinds if k in ("ToolBatchWidget", "ToolCallWidget")]
            self.assertEqual(
                len(tool_ish), 2, f"折叠态下与工具有关的可见块应为 2 个，实际 {kinds}"
            )

        await self._drive(self._events(), check)


class ExpandAcrossRunTest(_RunBase):
    """端到端场景 2：跑完之后逐档巡视。"""

    async def test_items_level_reveals_each_call(self) -> None:
        events = [
            said("查一下。"),
            *ran(call("c1", "grep_content", pattern="def "), "共 5 处匹配"),
            *ran(call("c2", "read_file", path="src/app.py"), "读取 2 行"),
            said("看完了。"),
            DONE,
        ]
        async def check(app, view, pilot):
            def shown() -> int:
                return len([c for c in view.query(ToolCallWidget) if c.display])

            self.assertEqual(shown(), 0, "折叠档下逐条不可见")

            view.set_detail_level(DETAIL_ITEMS)
            await pilot.pause()
            self.assertEqual(shown(), 2, "逐条档下两次调用都要露出来")

            view.set_detail_level(DETAIL_FOLDED)
            await pilot.pause()
            self.assertEqual(shown(), 0, "收回后又该藏起来")

        await self._drive(events, check)


class FailureRunTest(_RunBase):
    """
    端到端场景 3：并行检索里有一个失败，而模型不停下来。

    真正要紧的失败会让模型停下来说明，那种情况批次自然断开、那条调用单独可见；
    这里覆盖的是兜底路径——聚合行必须变色并写出失败个数（AC9）。
    """

    async def test_folded_summary_stays_quiet_about_the_failure(self) -> None:
        """
        **整轮反证（原 AC9 已于 2026-09-17 反转）**：聚合行不写失败个数。

        失败的那次仍计入总数——聚合语说的是「做了几次」不是「成了几次」。
        """
        events = [
            said("我查几个地方。"),
            *ran(call("c1", "grep_content", pattern="def "), "共 5 处匹配"),
            *ran(call("c2", "read_file", path="does/not/exist.py"), "文件不存在", ok=False),
            *ran(call("c3", "read_file", path="src/util.py"), "读取 2 行"),
            said("有一个文件不在，其余看完了。"),
            DONE,
        ]
        async def check(app, view, pilot):
            batch = view.query_one(ToolBatchWidget)
            summary = batch.summary_text()
            self.assertNotIn("失败", summary)
            self.assertIn("读取 2 个文件", summary)

        await self._drive(events, check)

    async def test_failure_is_visible_only_after_expanding(self) -> None:
        """
        **失败换了落点，不是被删了**（原 AC9 反转后这条接替它的位置）。

        折叠态那一行**一个失败字样都没有、颜色是成功色**；按 `Ctrl+O` 展开
        之后，那条工具行本身仍是失败色、仍写着「失败」二字（F7 未改）。

        ⚠ **两半必须在同一条用例里**：只验前半等于只证明「它安静了」，
        而那与「它把失败吃掉了」在断言上无法区分——后半才是「安静是安全的」
        这句话的唯一依据。
        """
        events = [
            said("查一下。"),
            *ran(call("c1", "read_file", path="nope.py"), "文件不存在", ok=False),
            said("没找到。"),
            DONE,
        ]
        async def check(app, view, pilot):
            batch = view.query_one(ToolBatchWidget)
            self.assertNotIn("失败", batch.summary_text())
            styles = [str(s.style) for s in batch.render().spans]
            self.assertTrue(has_color(styles, ToolCallWidget._COLOR_OK), styles)
            self.assertFalse(has_color(styles, ToolCallWidget._COLOR_FAIL), styles)

            # 展开：那一条工具行必须自己把实情说出来
            view.set_detail_level(DETAIL_ITEMS)
            await pilot.pause()
            row = view.query_one(ToolCallWidget)
            self.assertTrue(row.display, "展开档下工具行必须可见")
            self.assertIn("失败", row.plain_text())
            row_styles = [str(s.style) for s in row.render().spans]
            self.assertTrue(has_color(row_styles, ToolCallWidget._COLOR_FAIL), row_styles)

        await self._drive(events, check)


if __name__ == "__main__":
    unittest.main()
