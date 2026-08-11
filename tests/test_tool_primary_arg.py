"""
`Tool.primary_arg` 的护栏（tui-display 扩展 T16，spec F12 / AC10）。

工具行的标题写成 `标签(主参数的值)`，主参数由工具自己声明。这里钉住两件
**漏了不报错**的事：

1. 声明的键名必须**真的存在于 `parameters` 里**。写错一个字母不会有任何
   异常——界面只是悄悄退回改造前的键值对摘要，而那正是本轮要改掉的形态。
   拼写错误因此完全隐形，只有人眼盯着界面才可能发现。
2. 该显示路径 / 模式 / 命令的工具确实声明了它。新增工具忘了声明是安全的
   （有兜底），但**既有的这几个退回去**就是回归。
"""

from __future__ import annotations

import unittest

from rhinecode.tools.base import Tool
from rhinecode.tools.edit_file import EditFileTool
from rhinecode.tools.glob_files import GlobTool
from rhinecode.tools.grep_content import GrepTool
from rhinecode.tools.read_file import ReadFileTool
from rhinecode.tools.run_command import RunCommandTool
from rhinecode.tools.team_tasks import (
    TaskCreateTool,
    TaskGetTool,
    TaskListTool,
    TaskUpdateTool,
)
from rhinecode.tools.write_file import WriteFileTool


def _classes():
    """
    本轮声明过 `primary_arg` 的工具类。

    ⚠ 刻意**只收那些不需要构造参数**的工具：`web_fetch` / `send_message` /
    `load_skill` 要注入服务对象才建得起来，为了一条属性断言把它们的依赖搬进来
    不划算——它们的键名由 `test_tui_tool_title.py` 的映射表间接覆盖。
    协作任务四件套无参可建，故收进来。
    """
    return [
        ReadFileTool,
        WriteFileTool,
        EditFileTool,
        GlobTool,
        GrepTool,
        RunCommandTool,
        TaskCreateTool,
        TaskListTool,
        TaskGetTool,
        TaskUpdateTool,
    ]


class DeclarationTest(unittest.TestCase):
    def test_default_is_empty(self) -> None:
        """
        基类缺省是空串（= 未声明）。

        这是「新增工具忘了声明也不会显示异常」那条兜底的前提：空串让展示层
        走回键值对摘要，而不是拼出 `Read()` 这种看起来像无参调用的形态。
        """
        self.assertEqual(Tool.primary_arg, "")

    def test_declared_key_exists_in_the_schema(self) -> None:
        """
        **本文件的核心判据**：声明的键必须在 `parameters.properties` 里。

        写错一个字母不报错——界面只是悄悄退回改造前的形态，而那正是本轮要改掉
        的东西。没有这条，一次笔误可以一直活到有人盯着界面看为止。
        """
        for cls in _classes():
            with self.subTest(tool=cls.__name__):
                key = cls.primary_arg
                if not key:
                    continue
                props = (cls.parameters or {}).get("properties") or {}
                self.assertIn(
                    key,
                    props,
                    f"{cls.__name__} 声明的 primary_arg={key!r} 不在参数 schema 里"
                    f"（现有：{sorted(props)}）",
                )

    def test_the_six_core_tools_declare_it(self) -> None:
        """
        回归护栏：这几个是用户看得最多的工具，退回未声明就是回归。

        新增工具忘了声明是安全的（有兜底），但这几个不是新增的。
        """
        expected = {
            ReadFileTool: "path",
            WriteFileTool: "path",
            EditFileTool: "path",
            GlobTool: "pattern",
            GrepTool: "pattern",
            RunCommandTool: "command",
        }
        for cls, key in expected.items():
            with self.subTest(tool=cls.__name__):
                self.assertEqual(cls.primary_arg, key)

    def test_write_shows_the_path_not_the_content(self) -> None:
        """
        **选值的反证**：写文件必须显示路径而不是内容。

        `content` 可能是整份文件——把它放进标题会让那一行变成一堵墙，
        而用户想知道的只是「在写哪个文件」。
        """
        self.assertNotEqual(WriteFileTool.primary_arg, "content")

    def test_grep_shows_the_pattern_not_the_scope(self) -> None:
        """`grep_content` 两个参数都合法，选 pattern 是刻意的：「在找什么」比「在哪找」更能说明这次调用。"""
        self.assertEqual(GrepTool.primary_arg, "pattern")
        self.assertIn("path", (GrepTool.parameters or {}).get("properties", {}))

    def test_task_list_deliberately_has_none(self) -> None:
        """
        `task_list` 刻意不声明——它就是「列一下」，没有哪个参数值得进标题。

        留一条用例是为了让下一个人知道这是**刻意**的，不是漏了。
        """
        self.assertEqual(TaskListTool.primary_arg, "")


if __name__ == "__main__":
    unittest.main()
