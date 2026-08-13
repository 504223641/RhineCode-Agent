"""
工具活动归并的纯函数单测（tui-activity-fold 扩展 T1–T4，spec F2/F3/F5/F11）。

这一层全是零 IO 的纯函数，是整个归并特性的地基：**哪些工具参与折叠**、
**折叠后那一行写什么**、**展开到最详细一档时标题写什么**。

⚠ 本文件里分量最重的是 `FoldWhitelistTest`。它遍历一张「明确不该被折叠」
的清单逐个断言——那条清单不是随手写的，它对应 spec 的安全判据：
**能折叠的，要么无副作用，要么已被用户过目。**
将来有人往 `FOLD_GROUPS` 里顺手加一个「结果类」工具时，这里当场红。

（该判据在验收期改过一次：最初是更严的「被折叠的永远只是『读』」，
后来 `run_command` 按用户要求加入归并——理由见 `FOLD_GROUPS` 的注释与
`test_result_changing_tools_are_never_foldable` 的 docstring。）
"""

import unittest
from types import SimpleNamespace

from rhinecode.tools.display import (
    FOLD_GROUPS,
    RUNNING_VERBS,
    compose_batch_summary,
    fold_group_map,
    is_foldable,
    resolve_full_title,
    running_verb,
)


def call(name: str, arguments=None):
    """造一个形态等价于 `provider.base.ToolCall` 的最小对象。"""
    return SimpleNamespace(name=name, arguments=arguments)


class FoldWhitelistTest(unittest.TestCase):
    """F2：只有「过程类」工具参与归并，「结果类」一律独立成行。"""

    def test_process_tools_are_foldable(self) -> None:
        for name in ("read_file", "glob_files", "grep_content", "web_fetch", "run_command"):
            with self.subTest(tool=name):
                self.assertTrue(is_foldable(name))

    def test_result_changing_tools_are_never_foldable(self) -> None:
        """
        **本文件最要紧的一条。** 这张清单对应 spec 的安全判据。

        ## 判据在验收期改过一次，现在是这条

        **能折叠的，要么无副作用，要么已被用户过目。**

        最初更严：「被折叠的永远只是『读』」。后来按用户要求让 `run_command`
        参与归并（对齐 Claude Code 的 `Ran N shell commands`）——命令虽有副作用，
        但每一条执行前都过权限管线，用户要么当场在面板上放行、要么事先写了
        allow 规则，折叠的是「已经过目的过程」。

        ⚠ **写文件与编辑文件是这条判据的边界**：它们改的是工作区内容、且带
        diff 块，那是用户要盯着看的**结果**而非过程。若将来某一项在这里红了，
        先想清楚它到底是「过程」还是「结果」，而不是顺手把断言删掉——
        写文件被静默折进聚合行，用户是**看不出来**的。
        """
        forbidden = (
            "write_file",      # 改工作区内容，且带 diff：是结果不是过程
            "edit_file",       # 同上
            "run_agent",       # 委派：已有活动区与历史区留痕，归并会与那套冲突
            "load_skill",      # 改变整个会话可用的能力集合
            "mcp_add_server",  # 写配置并启动外部进程
            "ask_user",
            "present_plan",
            "send_message",
            "task_create",
        )
        for name in forbidden:
            with self.subTest(tool=name):
                self.assertFalse(is_foldable(name))

    def test_run_command_is_foldable_but_write_is_not(self) -> None:
        """
        **这条是上面那条判据的分辨力所在。**

        只断言「写文件不折叠」的话，把整张表清空也能通过；只断言「命令折叠」
        的话，把写文件一起加进去也能通过。两条摆在一起，才钉得住那条边界
        **恰好画在「过程 vs 结果」这里**。
        """
        self.assertTrue(is_foldable("run_command"), "命令已被用户过目，可折叠")
        self.assertFalse(is_foldable("write_file"), "写入是结果，必须独立成行")

    def test_unknown_tool_is_not_foldable(self) -> None:
        """未登记一律不参与——偏严方向，MCP 远端工具都落这一支。"""
        self.assertFalse(is_foldable("mcp__whatever__do_something"))
        self.assertFalse(is_foldable(""))
        self.assertFalse(is_foldable(None))

    def test_every_group_has_a_running_verb(self) -> None:
        """
        分组表与进行时文案表必须对得上。漏一项不报错，只是那一类工具
        运行时显示成通用的「执行中…」，而用户看不出少了什么。
        """
        for name, (group, _template) in FOLD_GROUPS.items():
            with self.subTest(tool=name):
                self.assertIn(group, RUNNING_VERBS)

    def test_every_template_has_the_count_placeholder(self) -> None:
        """量词模板必须能填入数量，否则聚合语会丢掉计数。"""
        for name, (_group, template) in FOLD_GROUPS.items():
            with self.subTest(tool=name):
                self.assertIn("{n}", template)

    def test_quantifiers_carry_their_object(self) -> None:
        """
        F3：量词必须带宾语。「查找」与「搜索」在中文里近乎同义，
        并排出现时读的人分不出差别——而聚合语的全部价值就是一眼看懂。

        这条钉住的是**措辞**而不是实现，因为它正是本轮改过一次的地方
        （初版写成「查找 N 次 · 搜索 N 次」，用户第一反应就是问两者的区别）。
        """
        self.assertIn("文件", FOLD_GROUPS["glob_files"][1])
        self.assertIn("内容", FOLD_GROUPS["grep_content"][1])


class RunningVerbTest(unittest.TestCase):
    """F5：运行期间只表达「在做哪一类事」。"""

    def test_each_group_reads_as_present_continuous(self) -> None:
        self.assertEqual(running_verb("grep_content"), "搜索中…")
        self.assertEqual(running_verb("read_file"), "读取中…")
        self.assertEqual(running_verb("glob_files"), "查找中…")

    def test_unknown_tool_falls_back(self) -> None:
        self.assertEqual(running_verb("write_file"), "执行中…")
        self.assertEqual(running_verb(None), "执行中…")


class BatchSummaryTest(unittest.TestCase):
    """F3/F6：聚合语的分组、顺序与失败段。"""

    def test_counts_by_group(self) -> None:
        text = compose_batch_summary(
            [("grep_content", True), ("grep_content", True), ("read_file", True)]
        )
        self.assertEqual(text, "搜索内容 2 次 · 读取 1 个文件")

    def test_group_order_follows_first_occurrence(self) -> None:
        """
        顺序取**首次出现的时序**，不是字母序、也不是表里的定义序——
        聚合语要读起来与实际发生顺序一致。
        """
        text = compose_batch_summary([("read_file", True), ("grep_content", True)])
        self.assertTrue(text.startswith("读取"))
        # 反向的输入必须给出反向的顺序，否则说明它其实按某个固定序排的
        other = compose_batch_summary([("grep_content", True), ("read_file", True)])
        self.assertTrue(other.startswith("搜索"))

    def test_single_call_still_uses_aggregate_wording(self) -> None:
        """
        F4：单次调用**仍套聚合语**（对齐 Claude Code），不退回原始形态。
        信息不靠这一行找补——从属行会显示主参数值，那是组件层的事。
        """
        self.assertEqual(compose_batch_summary([("grep_content", True)]), "搜索内容 1 次")

    def test_failures_appended_at_the_end(self) -> None:
        text = compose_batch_summary(
            [("grep_content", True), ("grep_content", False), ("read_file", True)]
        )
        self.assertTrue(text.endswith("1 个失败"))
        # 失败的那次仍计入本组总数——聚合语说的是「做了几次」，不是「成了几次」
        self.assertIn("搜索内容 2 次", text)

    def test_no_failure_segment_when_all_succeed(self) -> None:
        text = compose_batch_summary([("read_file", True), ("read_file", True)])
        self.assertNotIn("失败", text)

    def test_pending_entries_count_but_do_not_fail(self) -> None:
        """
        尚未产生结果的调用（第二项为 None）计入总数但不算失败——
        写成 `not ok` 的话，运行中的每一次调用都会被显示成失败。
        """
        text = compose_batch_summary([("read_file", None), ("read_file", True)])
        self.assertEqual(text, "读取 2 个文件")

    def test_unregistered_tool_is_skipped(self) -> None:
        """防御性：不可归并的工具本不该进批次，真进来了也不该毁掉整句。"""
        text = compose_batch_summary([("write_file", True), ("read_file", True)])
        self.assertEqual(text, "读取 1 个文件")

    def test_empty_input(self) -> None:
        self.assertEqual(compose_batch_summary([]), "")


class FullTitleTest(unittest.TestCase):
    """F11：最详细一档的标题——列全部参数且不截断。"""

    LONG = "rhinecode/tui/widgets.py"

    def test_lists_every_argument(self) -> None:
        _label, inner = resolve_full_title(
            call("grep_content", {"pattern": "def compose_status_text", "path": self.LONG})
        )
        self.assertIn("pattern:", inner)
        self.assertIn("path:", inner)

    def test_never_truncates(self) -> None:
        """
        改造前展开态沿用折叠态算好的截断标题，于是「展开」了却看不到被截掉的
        部分。这条钉住那个缺陷不会回来。
        """
        very_long = "x" * 400
        _label, inner = resolve_full_title(call("read_file", {"path": very_long}))
        self.assertIn(very_long, inner)
        self.assertNotIn("…", inner)

    def test_uses_display_label_not_internal_name(self) -> None:
        label, _inner = resolve_full_title(call("run_command", {"command": "ls"}))
        self.assertEqual(label, "Bash")

    def test_newlines_folded_to_spaces(self) -> None:
        """标题只有一行高度，留着换行会把布局撑开。"""
        _label, inner = resolve_full_title(call("write_file", {"content": "a\nb\r\nc"}))
        self.assertNotIn("\n", inner)
        self.assertNotIn("\r", inner)

    def test_empty_arguments(self) -> None:
        self.assertEqual(resolve_full_title(call("read_file", None))[1], "")
        self.assertEqual(resolve_full_title(call("read_file", {}))[1], "")


class FoldGroupMapTest(unittest.TestCase):
    """只导出**当前真正注册了**的工具，与 `primary_arg_map` 同构。"""

    class _Registry:
        def __init__(self, names):
            self._names = list(names)

        def names(self):
            return list(self._names)

    def test_intersects_with_registered_tools(self) -> None:
        mapping = fold_group_map(self._Registry(["read_file", "write_file", "run_command"]))
        # run_command 也在表内（验收期改的，见 FOLD_GROUPS 的注释）；write_file 不在
        self.assertEqual(set(mapping), {"read_file", "run_command"})

    def test_excluded_tool_disappears(self) -> None:
        """被 `exclude_tools` 摘掉的工具不出现在映射里，界面侧因此不必再判断。"""
        mapping = fold_group_map(self._Registry(["glob_files"]))
        self.assertNotIn("web_fetch", mapping)

    def test_tolerates_missing_registry(self) -> None:
        self.assertEqual(fold_group_map(None), {})
        self.assertEqual(fold_group_map(object()), {})


if __name__ == "__main__":
    unittest.main()
