"""
工具活动批次归并的组件级单测（tui-activity-fold 扩展 T16，AC1–AC10）。

一批连续的只读检索调用在历史区收成**一行聚合语**。本文件验的是这件事的
四个方面：**什么时候开一批 / 什么时候封一批 / 哪些调用进得来 / 那一行写什么**。

⚠ 本文件里分量最重的两条：

- `MountingTest` —— 组件挂载时序。批次容器是本轮唯一的新增容器，
  而实现期在这里连踩三个**不报错**的坑（详见各用例的 docstring）。
  它们全都表现为「界面上东西凭空少了」，没有任何异常抛出。
- `NonFoldableTest` —— 写文件 / 执行命令 / 委派 / Skill **必须独立成行**。
  这是 spec 的安全判据在组件层的落点：被折叠的永远只是「读」。
"""

import unittest

from textual.app import App, ComposeResult
from textual.containers import Vertical

from rhinecode.provider.base import ToolCall
from rhinecode.tui.widgets import (
    DETAIL_FOLDED,
    DETAIL_FULL,
    DETAIL_ITEMS,
    HistoryView,
    ToolBatchWidget,
    ToolCallWidget,
)

# 与 `app.on_mount` 从工具注册中心建出来的那两份映射同形。
FOLD = {
    "glob_files": ("glob", "查找文件 {n} 次"),
    "grep_content": ("grep", "搜索内容 {n} 次"),
    "read_file": ("read", "读取 {n} 个文件"),
}
PRIMARY = {
    "read_file": "path",
    "write_file": "path",
    "glob_files": "pattern",
    "grep_content": "pattern",
    "run_command": "command",
}


class _Harness(App):
    """只挂历史区的最小应用，让批次真的经历挂载与渲染。"""

    def compose(self) -> ComposeResult:
        yield HistoryView()


def call(tid: str, name: str, **arguments):
    """造一次工具调用。"""
    return ToolCall(id=tid, name=name, arguments=arguments or {})


def children_of(view: HistoryView):
    """
    历史区的直接子节点类型名列表——批次是否真的还挂在 DOM 上，看这个。

    ⚠ **批次与它统辖的工具行在这里是平级的。** 批次不是容器（它就是那行
    聚合语），工具行照常挂在历史区。因此一次检索的结构是
    `['ToolBatchWidget', 'ToolCallWidget']` 而不是只有前者——
    折叠是靠把工具行 `display = False` 做到的，不是靠嵌套。
    """
    container = view.query_one("#history-messages", Vertical)
    return [type(child).__name__ for child in container.children]


def visible_children_of(view: HistoryView):
    """只看**可见**的子节点——这才是用户在屏幕上真正看到的东西。"""
    container = view.query_one("#history-messages", Vertical)
    return [type(c).__name__ for c in container.children if c.display]


def summary_of(batch: ToolBatchWidget) -> str:
    """取聚合行的纯文本（未转义、不含档位提示）。"""
    return batch.summary_text()


async def prepared(app: App) -> HistoryView:
    """取出历史区并灌好两份映射（与真实启动顺序一致）。"""
    view = app.query_one(HistoryView)
    view.set_primary_args(PRIMARY)
    view.set_fold_groups(FOLD)
    return view


class MountingTest(unittest.IsolatedAsyncioTestCase):
    """
    挂载时序。**实现期在这里踩了三个坑，全都不报错。**

    共同点：异常（如果有）抛在布局阶段的主线程或干脆没有异常，
    业务调用栈上没有任何线索，界面上只表现为「东西凭空少了」。
    """

    async def test_batch_survives_a_following_system_line(self) -> None:
        """
        **回归护栏 ①**：挂一条系统行之后，批次必须还在 DOM 上。

        实现期真实故障：`attach` 在容器自身 `compose` 之前被调用，于是对一个
        尚未挂载的聚合行 `Static` 调了 `update()`——**整个批次容器在下一次
        布局时从 DOM 里掉了出去**，`#history-messages` 里只剩那条系统行，
        批次连同它的工具行一起凭空消失，**不报任何错**。
        """
        app = _Harness()
        async with app.run_test(size=(120, 40)) as pilot:
            view = await prepared(app)
            view.add_tool_widget(call("c1", "read_file", path="a.py"))
            await pilot.pause()
            self.assertEqual(children_of(view), ["ToolBatchWidget", "ToolCallWidget"])

            view.append_system("记忆已更新")
            await pilot.pause()
            self.assertEqual(
                children_of(view),
                ["ToolBatchWidget", "ToolCallWidget", "Static"],
                "批次不得因为后面挂了一条系统行而从 DOM 掉出去",
            )
            # 折叠档下用户实际看到的只有聚合行与那条系统行
            self.assertEqual(visible_children_of(view), ["ToolBatchWidget", "Static"])

    async def test_widget_renders_before_on_mount(self) -> None:
        """
        **回归护栏 ②**：批次能被渲染，不抛 `render_strips` 那个异常。

        实现期真实故障有两个来源，都表现为合成器里抛
        `AttributeError: 'NoneType' object has no attribute 'render_strips'`：
        一是把「画聚合行」的方法命名为 `_render`，**覆盖了 Textual 用来产出
        Visual 的内部方法**；二是工具行的 renderable 初始为 None。
        两者都抛在**布局阶段的主线程**，没有任何 try/except 兜得住。
        """
        app = _Harness()
        async with app.run_test(size=(120, 40)) as pilot:
            view = await prepared(app)
            view.add_tool_widget(call("c1", "grep_content", pattern="x"))
            await pilot.pause()
            # 能走到这里就说明渲染没炸；再确认内容确实画出来了
            batch = view.query_one(ToolBatchWidget)
            self.assertTrue(batch.is_mounted)
            self.assertIn("中…", summary_of(batch))

    async def test_summary_line_stays_on_top(self) -> None:
        """
        聚合行必须排在它统辖的工具行**之前**——它是这一批的标题。

        实现期真实故障：工具行曾排到聚合行前面去（`attach` 在容器 `compose`
        之前就 mount 了子节点），读起来就成了「先干活、后报标题」。
        """
        app = _Harness()
        async with app.run_test(size=(120, 40)) as pilot:
            view = await prepared(app)
            view.add_tool_widget(call("c1", "read_file", path="a.py"))
            view.add_tool_widget(call("c2", "read_file", path="b.py"))
            await pilot.pause()

            kinds = children_of(view)
            self.assertEqual(kinds[0], "ToolBatchWidget", "聚合行必须排在最前")
            self.assertEqual(kinds.count("ToolCallWidget"), 2)


class BatchFormationTest(unittest.IsolatedAsyncioTestCase):
    """AC1：什么时候开一批、什么时候封一批。"""

    async def test_consecutive_retrievals_share_one_batch(self) -> None:
        """AC1：连续三次检索只产生**一个**批次块。"""
        app = _Harness()
        async with app.run_test(size=(120, 40)) as pilot:
            view = await prepared(app)
            view.add_tool_widget(call("c1", "grep_content", pattern="a"))
            view.add_tool_widget(call("c2", "grep_content", pattern="b"))
            view.add_tool_widget(call("c3", "read_file", path="c.py"))
            await pilot.pause()

            batches = list(view.query(ToolBatchWidget))
            self.assertEqual(len(batches), 1)
            self.assertEqual(batches[0].call_count, 3)

    async def test_assistant_text_closes_the_batch(self) -> None:
        """AC1：模型输出正文即封闭当前批次，其后的检索开新的一批。"""
        app = _Harness()
        async with app.run_test(size=(120, 40)) as pilot:
            view = await prepared(app)
            view.add_tool_widget(call("c1", "read_file", path="a.py"))
            await pilot.pause()
            first = view.query_one(ToolBatchWidget)

            view.begin_assistant_turn()
            await pilot.pause()
            self.assertTrue(first.closed, "正文一出现，当前批次就该封闭")

            view.add_tool_widget(call("c2", "read_file", path="b.py"))
            await pilot.pause()
            self.assertEqual(len(list(view.query(ToolBatchWidget))), 2)

    async def test_thinking_block_also_closes(self) -> None:
        """
        思考块同样封闭批次。

        它是一段独立呈现的内容，让它插进批次中间会让时序错乱——
        聚合行说的事情，一部分发生在那段思考之前、一部分之后。
        """
        app = _Harness()
        async with app.run_test(size=(120, 40)) as pilot:
            view = await prepared(app)
            view.add_tool_widget(call("c1", "read_file", path="a.py"))
            await pilot.pause()
            batch = view.query_one(ToolBatchWidget)

            view.begin_thinking_turn()
            await pilot.pause()
            self.assertTrue(batch.closed)

    async def test_close_is_idempotent(self) -> None:
        """连续挂几条系统行是常态，重复封闭不得出错也不得改变内容。"""
        app = _Harness()
        async with app.run_test(size=(120, 40)) as pilot:
            view = await prepared(app)
            view.add_tool_widget(call("c1", "read_file", path="a.py"))
            await pilot.pause()
            batch = view.query_one(ToolBatchWidget)

            view.append_system("一")
            view.append_system("二")
            view.append_system("三")
            await pilot.pause()
            self.assertTrue(batch.closed)
            self.assertEqual(summary_of(batch), "读取 1 个文件")

    async def test_clear_leaves_no_ghost_batch(self) -> None:
        """
        AC21 的组件侧：清空之后再来调用，必须开一个**新**批次。

        漏掉「清空时把当前批次置空」的话，这里会往一个已被 `remove_children`
        删掉的「幽灵批次」里挂工具行——界面上什么都不出现，且不报错。
        """
        app = _Harness()
        async with app.run_test(size=(120, 40)) as pilot:
            view = await prepared(app)
            view.add_tool_widget(call("c1", "read_file", path="a.py"))
            await pilot.pause()

            view.clear_all()
            await pilot.pause()
            self.assertEqual(len(list(view.query(ToolBatchWidget))), 0)

            view.add_tool_widget(call("c2", "read_file", path="b.py"))
            await pilot.pause()
            self.assertEqual(len(list(view.query(ToolBatchWidget))), 1)


class NonFoldableTest(unittest.IsolatedAsyncioTestCase):
    """
    AC3/AC4：**被折叠的永远只是「读」。**

    这是 spec 那条安全判据在组件层的落点。写文件与执行命令若被静默折进
    聚合行，用户是**看不出来**的。
    """

    async def test_write_file_stands_alone(self) -> None:
        app = _Harness()
        async with app.run_test(size=(120, 40)) as pilot:
            view = await prepared(app)
            widget = view.add_tool_widget(call("c1", "write_file", path="x.txt"))
            await pilot.pause()

            self.assertIsNone(widget.batch, "写文件不得进批次")
            self.assertEqual(children_of(view), ["ToolCallWidget"])
            # 且它在折叠档下**始终可见**——这正是它不参与归并的意义
            self.assertEqual(visible_children_of(view), ["ToolCallWidget"])

    async def test_run_command_stands_alone(self) -> None:
        app = _Harness()
        async with app.run_test(size=(120, 40)) as pilot:
            view = await prepared(app)
            widget = view.add_tool_widget(call("c1", "run_command", command="ls"))
            await pilot.pause()
            self.assertIsNone(widget.batch)

    async def test_unregistered_tool_stands_alone(self) -> None:
        """未登记的（含 MCP 远端工具）一律独立成行——偏严方向。"""
        app = _Harness()
        async with app.run_test(size=(120, 40)) as pilot:
            view = await prepared(app)
            widget = view.add_tool_widget(call("c1", "mcp__x__do", arg="1"))
            await pilot.pause()
            self.assertIsNone(widget.batch)

    async def test_non_foldable_splits_the_batch(self) -> None:
        """
        AC3：检索 → 写文件 → 检索，产生「批次 / 独立行 / 新批次」三个挂载物。

        写文件那一行在折叠状态下**始终可见**，这正是它不参与归并的意义。
        """
        app = _Harness()
        async with app.run_test(size=(120, 40)) as pilot:
            view = await prepared(app)
            view.add_tool_widget(call("c1", "read_file", path="a.py"))
            await pilot.pause()
            view.add_tool_widget(call("c2", "write_file", path="x.txt"))
            await pilot.pause()
            view.add_tool_widget(call("c3", "read_file", path="b.py"))
            await pilot.pause()

            self.assertEqual(
                children_of(view),
                [
                    "ToolBatchWidget", "ToolCallWidget",   # 第一批：聚合行 + 它的检索
                    "ToolCallWidget",                       # 写文件：独立成行
                    "ToolBatchWidget", "ToolCallWidget",   # 第二批
                ],
            )
            # 折叠档下屏幕上是「聚合行 / 写文件行 / 聚合行」三行——
            # **写文件那一行没有被藏起来**，这是本条最要紧的判据
            self.assertEqual(
                visible_children_of(view),
                ["ToolBatchWidget", "ToolCallWidget", "ToolBatchWidget"],
            )


class SummaryTextTest(unittest.IsolatedAsyncioTestCase):
    """AC5–AC10：那一行到底写什么。"""

    async def test_running_shows_present_continuous(self) -> None:
        """AC7：运行期间是进行时文案，且**不含任何耗时数字**（AC8）。"""
        app = _Harness()
        async with app.run_test(size=(120, 40)) as pilot:
            view = await prepared(app)
            widget = view.add_tool_widget(call("c1", "grep_content", pattern="x"))
            widget.begin_running(call("c1", "grep_content", pattern="x"))
            await pilot.pause()

            batch = view.query_one(ToolBatchWidget)
            self.assertEqual(summary_of(batch), "搜索中…")
            # AC8：批次块内不出现耗时——那由底部的状态行统一承担
            self.assertNotIn("s", summary_of(batch).replace("搜索中…", ""))

    async def test_closed_shows_counts_in_occurrence_order(self) -> None:
        """AC5：分组计数，且顺序与实际发生时序一致。"""
        app = _Harness()
        async with app.run_test(size=(120, 40)) as pilot:
            view = await prepared(app)
            for i, (name, kwargs) in enumerate(
                [
                    ("grep_content", {"pattern": "a"}),
                    ("grep_content", {"pattern": "b"}),
                    ("read_file", {"path": "c.py"}),
                ]
            ):
                view.add_tool_widget(call(f"c{i}", name, **kwargs))
            await pilot.pause()
            batch = view.query_one(ToolBatchWidget)
            view.append_system("x")
            await pilot.pause()

            self.assertEqual(summary_of(batch), "搜索内容 2 次 · 读取 1 个文件")

    async def test_single_call_still_uses_aggregate_wording(self) -> None:
        """
        AC6：单次调用**仍套聚合语**（对齐 Claude Code），不退回原始形态。
        """
        app = _Harness()
        async with app.run_test(size=(120, 40)) as pilot:
            view = await prepared(app)
            view.add_tool_widget(call("c1", "grep_content", pattern="def foo"))
            await pilot.pause()
            batch = view.query_one(ToolBatchWidget)
            view.append_system("x")
            await pilot.pause()

            self.assertEqual(summary_of(batch), "搜索内容 1 次")

    async def test_failure_count_appended(self) -> None:
        """AC9：并行检索里有一个失败而模型继续跑时，聚合语末尾写出失败个数。"""
        app = _Harness()
        async with app.run_test(size=(120, 40)) as pilot:
            view = await prepared(app)
            w1 = view.add_tool_widget(call("c1", "grep_content", pattern="a"))
            w2 = view.add_tool_widget(call("c2", "grep_content", pattern="b"))
            await pilot.pause()

            w1.finish(True, "共 3 处匹配")
            w2.finish(False, "正则非法")
            batch = view.query_one(ToolBatchWidget)
            view.append_system("x")
            await pilot.pause()

            text = summary_of(batch)
            self.assertTrue(text.endswith("1 个失败"), text)
            # 失败的那次仍计入本组总数——聚合语说的是「做了几次」不是「成了几次」
            self.assertIn("搜索内容 2 次", text)

    async def test_no_failure_segment_when_all_succeed(self) -> None:
        app = _Harness()
        async with app.run_test(size=(120, 40)) as pilot:
            view = await prepared(app)
            w = view.add_tool_widget(call("c1", "read_file", path="a.py"))
            await pilot.pause()
            w.finish(True, "读取 12 行")
            batch = view.query_one(ToolBatchWidget)
            view.append_system("x")
            await pilot.pause()
            self.assertNotIn("失败", summary_of(batch))


class DetailLevelBroadcastTest(unittest.IsolatedAsyncioTestCase):
    """档位广播在批次上的落点（AC11 的组件侧）。"""

    async def test_folded_hides_children(self) -> None:
        app = _Harness()
        async with app.run_test(size=(120, 40)) as pilot:
            view = await prepared(app)
            view.add_tool_widget(call("c1", "read_file", path="a.py"))
            view.add_tool_widget(call("c2", "read_file", path="b.py"))
            await pilot.pause()
            batch = view.query_one(ToolBatchWidget)

            view.set_detail_level(DETAIL_FOLDED)
            await pilot.pause()
            self.assertTrue(
                all(not c.display for c in batch.query(ToolCallWidget)),
                "折叠档下子行必须整体不可见——折叠的全部意义就在这里",
            )

            view.set_detail_level(DETAIL_ITEMS)
            await pilot.pause()
            self.assertTrue(all(c.display for c in batch.query(ToolCallWidget)))

    async def test_new_batch_follows_current_level(self) -> None:
        """
        切档之后**新产生**的批次也要跟上当前档位。

        只广播不记的话，用户按下展开键之后接着跑的工具又是折叠的——
        现象是「这个开关时灵时不灵」，比没有开关更让人困惑。
        """
        app = _Harness()
        async with app.run_test(size=(120, 40)) as pilot:
            view = await prepared(app)
            view.set_detail_level(DETAIL_ITEMS)
            view.add_tool_widget(call("c1", "read_file", path="a.py"))
            await pilot.pause()

            batch = view.query_one(ToolBatchWidget)
            self.assertTrue(all(c.display for c in batch.query(ToolCallWidget)))

    async def test_full_level_reaches_children(self) -> None:
        """最详细一档要真的传到子行上（它们据此换成完整参数与原文）。"""
        app = _Harness()
        async with app.run_test(size=(120, 40)) as pilot:
            view = await prepared(app)
            widget = view.add_tool_widget(call("c1", "read_file", path="a.py"))
            await pilot.pause()

            view.set_detail_level(DETAIL_FULL)
            await pilot.pause()
            self.assertEqual(widget._detail_level, DETAIL_FULL)


if __name__ == "__main__":
    unittest.main()
