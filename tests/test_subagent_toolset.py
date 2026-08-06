"""
分层工具过滤的单测（c13 T8，覆盖 AC11a / AC12）。

三层顺序（全局禁止 → 角色白名单 → 角色黑名单 → 后台附加）是**安全边界在前、
用户配置在后**。反过来的话，一条 `tools: run_agent` 就能让子 Agent 拿到委派能力、
无限嵌套下去。本模块把这条顺序钉死。
"""

from __future__ import annotations

import unittest
from pathlib import Path

from rhinecode.subagents.models import AgentSource, AgentSpec
from rhinecode.subagents.toolset import (
    GLOBAL_DENIED_TOOLS,
    ToolsetResult,
    resolve_toolset,
)

ALL_TOOLS = (
    "read_file",
    "glob_files",
    "grep_content",
    "write_file",
    "edit_file",
    "run_command",
    "web_fetch",
    "run_agent",
    "load_skill",
)


def _spec(tools=None, disallowed=()) -> AgentSpec:
    return AgentSpec(
        name="demo",
        description="x",
        body="",
        source=AgentSource.USER,
        path=Path("demo.md"),
        tools=tools,
        disallowed_tools=tuple(disallowed),
    )


class GlobalDenyTest(unittest.TestCase):
    """第 1 层：任何子 Agent 都看不到的工具。"""

    def test_denied_tools_absent_when_inheriting(self) -> None:
        result = resolve_toolset(ALL_TOOLS, _spec())
        for name in GLOBAL_DENIED_TOOLS:
            with self.subTest(tool=name):
                self.assertNotIn(name, result.allowed)

    def test_denied_tools_absent_for_branch_delegation(self) -> None:
        """分支式（`spec is None`）同样受第 1 层约束。"""
        result = resolve_toolset(ALL_TOOLS, None)
        for name in GLOBAL_DENIED_TOOLS:
            with self.subTest(tool=name):
                self.assertNotIn(name, result.allowed)

    def test_whitelist_cannot_reintroduce_denied_tool(self) -> None:
        """
        **本模块最重要的一条**：角色白名单**翻不过**全局禁止层。

        这是「安全边界排在用户配置之前」的直接判据。若顺序反了，
        这条会拿到一个含 `run_agent` 的工具集——子 Agent 就能再委派，无限嵌套。
        """
        result = resolve_toolset(ALL_TOOLS, _spec(tools=("run_agent", "read_file")))

        self.assertNotIn("run_agent", result.allowed)
        self.assertIn("read_file", result.allowed)
        # 它会作为「没解析成工具的名字」出现——因为在第 1 层就被摘掉了
        self.assertIn("run_agent", result.unresolved)

    def test_covers_every_denied_tool_by_iteration(self) -> None:
        """
        遍历常量集合而不是写死两个名字。

        新增禁止项时这条自动覆盖到它，不需要有人记得回来改测试。
        """
        result = resolve_toolset(ALL_TOOLS, _spec(tools=tuple(GLOBAL_DENIED_TOOLS)))
        self.assertEqual(result.allowed, frozenset())


class RoleFilterTest(unittest.TestCase):
    """第 2 层：白名单交集 + 黑名单相减。"""

    def test_none_means_inherit_all(self) -> None:
        result = resolve_toolset(ALL_TOOLS, _spec())
        self.assertEqual(result.allowed, frozenset(ALL_TOOLS) - GLOBAL_DENIED_TOOLS)

    def test_whitelist_intersects(self) -> None:
        result = resolve_toolset(ALL_TOOLS, _spec(tools=("read_file", "grep_content")))
        self.assertEqual(result.allowed, frozenset({"read_file", "grep_content"}))

    def test_blacklist_subtracts(self) -> None:
        result = resolve_toolset(ALL_TOOLS, _spec(disallowed=("write_file", "run_command")))
        self.assertNotIn("write_file", result.allowed)
        self.assertNotIn("run_command", result.allowed)
        self.assertIn("read_file", result.allowed)

    def test_blacklist_wins_over_whitelist(self) -> None:
        """同时命中时黑名单赢——它排在白名单之后。"""
        result = resolve_toolset(
            ALL_TOOLS, _spec(tools=("read_file", "write_file"), disallowed=("write_file",))
        )
        self.assertEqual(result.allowed, frozenset({"read_file"}))

    def test_unresolved_names_recorded(self) -> None:
        result = resolve_toolset(ALL_TOOLS, _spec(tools=("read_file", "nope", "alsonope")))
        self.assertEqual(result.allowed, frozenset({"read_file"}))
        self.assertEqual(result.unresolved, ("nope", "alsonope"))

    def test_unresolved_preserves_declaration_order(self) -> None:
        """
        未解析名字按**声明顺序**输出，便于用户对照自己写的那一行。

        （用集合的话顺序随机，用户对着一行 `tools: a, b, c` 看不出是哪个。）
        """
        result = resolve_toolset(ALL_TOOLS, _spec(tools=("zzz", "aaa")))
        self.assertEqual(result.unresolved, ("zzz", "aaa"))


class EmptyToolsetTest(unittest.TestCase):
    """AC12：空工具集要有可读诊断。"""

    def test_all_names_wrong(self) -> None:
        result = resolve_toolset(ALL_TOOLS, _spec(tools=("redFile", "globFiles")))

        self.assertTrue(result.is_empty)
        self.assertEqual(result.unresolved, ("redFile", "globFiles"))
        # 说明里必须出现那些错误名字——否则用户不知道去改哪个
        self.assertIn("redFile", result.reason)
        self.assertIn("globFiles", result.reason)

    def test_declared_empty_whitelist_is_explained(self) -> None:
        """
        `tools: []` 与「不写」的区别必须在说明里讲清楚。

        用户写空列表多半是误以为「留空 = 全部」，说明里点破这一点
        比只报「工具集为空」有用得多。
        """
        result = resolve_toolset(ALL_TOOLS, _spec(tools=()))
        self.assertTrue(result.is_empty)
        self.assertIn("空的 tools 白名单", result.reason)

    def test_blacklist_can_empty_the_set(self) -> None:
        result = resolve_toolset(("read_file",), _spec(disallowed=("read_file",)))
        self.assertTrue(result.is_empty)
        self.assertIn("黑名单", result.reason)

    def test_non_empty_has_no_reason(self) -> None:
        """非空时 `reason` 必须是空串——它是「失败原因」，不是常规说明。"""
        self.assertEqual(resolve_toolset(ALL_TOOLS, _spec()).reason, "")

    def test_empty_input_pool(self) -> None:
        result = resolve_toolset((), _spec())
        self.assertTrue(result.is_empty)
        self.assertIn("0 个", result.reason)


class BackgroundLayerTest(unittest.TestCase):
    """第 3 层：本章为空集，前后台结果必须一致。"""

    def test_background_and_foreground_identical(self) -> None:
        """
        **这条钉住 spec F13 的那个说明块**：本章全程非交互，
        前台与后台面对的约束相同，因此这一层为空集。

        将来若真给它填了内容，这条会红——那时应当先回去确认
        「同一个角色在两种场景下行为不同」这件事是不是可接受的。
        """
        spec = _spec()
        fg = resolve_toolset(ALL_TOOLS, spec, is_background=False)
        bg = resolve_toolset(ALL_TOOLS, spec, is_background=True)
        self.assertEqual(fg.allowed, bg.allowed)


class ResultShapeTest(unittest.TestCase):
    def test_is_empty_property(self) -> None:
        self.assertTrue(ToolsetResult(allowed=frozenset()).is_empty)
        self.assertFalse(ToolsetResult(allowed=frozenset({"a"})).is_empty)


if __name__ == "__main__":
    unittest.main()
