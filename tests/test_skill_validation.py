"""
Skill 白名单两段校验单测（c11 T19）。

覆盖 spec AC16 的纯逻辑部分（三个分支：内置名笔误致命 / mcp__ 名不致命 /
单下划线 mcp_ 内置工具正常通过）与 AC17（剔空后降级为不收窄）。
"""

import unittest
from pathlib import Path

from rhinecode.skills.models import SkillMode, SkillSource, SkillSpec
from rhinecode.skills.validation import (
    check_builtin_tool_names,
    collect_exempt_notices,
    format_fatal_message,
    prune_mcp_tool_names,
)

# 模拟一个真实的已知工具集：7 个内置工具 + Plan Mode 的两个特殊工具 + load_skill。
KNOWN = frozenset(
    {
        "read_file",
        "write_file",
        "edit_file",
        "run_command",
        "glob_files",
        "grep_content",
        "mcp_resolve_server",
        "mcp_add_server",
        "load_skill",
        "ask_user",
        "present_plan",
    }
)
EXEMPT = frozenset({"load_skill", "ask_user", "present_plan"})


def _spec(name: str, tools):
    return SkillSpec(
        name=name,
        description="说明",
        body="正文",
        mode=SkillMode.SHARED,
        allowed_tools=tools,
        history_messages=0,
        model=None,
        source=SkillSource.USER,
        entry_path=Path("/skills") / f"{name}.md",
        resource_dir=None,
        resource_files=(),
    )


class BuiltinNameCheckTest(unittest.TestCase):
    """第一段：启动时的严格校验（AC16）。"""

    def test_typo_is_fatal_with_full_locating_info(self) -> None:
        """不存在的内置工具名 → 致命项含 Skill 名、路径、工具名。"""
        fatals = check_builtin_tool_names([_spec("a", ("read_fil",))], KNOWN)
        self.assertEqual(len(fatals), 1)
        self.assertEqual(fatals[0].skill_name, "a")
        self.assertEqual(fatals[0].tool_name, "read_fil")
        self.assertEqual(fatals[0].path.name, "a.md")

    def test_mcp_double_underscore_never_fatal(self) -> None:
        """
        `mcp__` 开头的不存在名字**不**致命。

        MCP Server 这次没连上不是 Skill 的错，不该让整个程序起不来。
        """
        fatals = check_builtin_tool_names([_spec("a", ("mcp__srv__tool",))], KNOWN)
        self.assertEqual(fatals, [])

    def test_single_underscore_mcp_builtin_passes(self) -> None:
        """
        `mcp_add_server` 是**单**下划线的内置工具，不匹配 mcp__ 判别式。

        它走严格校验分支，且确实在 known 中，所以既不致命也不豁免（AC16 第三分支）。
        这条同时是启动接线那个窄时间窗的护栏——mcp_add_server 比其它内置工具晚注册。
        """
        fatals = check_builtin_tool_names([_spec("a", ("mcp_add_server",))], KNOWN)
        self.assertEqual(fatals, [])
        self.assertEqual(collect_exempt_notices([_spec("a", ("mcp_add_server",))], EXEMPT), [])

    def test_exempt_tools_are_not_fatal(self) -> None:
        """load_skill / ask_user / present_plan 在 known 中，不致命。"""
        spec = _spec("a", ("load_skill", "ask_user", "present_plan"))
        self.assertEqual(check_builtin_tool_names([spec], KNOWN), [])

    def test_none_whitelist_skipped(self) -> None:
        """未声明白名单的 Skill 直接跳过。"""
        self.assertEqual(check_builtin_tool_names([_spec("a", None)], KNOWN), [])

    def test_multiple_fatals_all_collected(self) -> None:
        """多个笔误全部收集，让用户一次改完而不是改一个重启一次。"""
        fatals = check_builtin_tool_names(
            [_spec("a", ("x1", "read_file")), _spec("b", ("x2",))], KNOWN
        )
        self.assertEqual({f.tool_name for f in fatals}, {"x1", "x2"})


class ExemptNoticeTest(unittest.TestCase):
    def test_notice_for_exempt_declaration(self) -> None:
        """声明豁免工具 → 提示「没有效果，可以删除」。"""
        notices = collect_exempt_notices([_spec("a", ("ask_user", "read_file"))], EXEMPT)
        self.assertEqual(len(notices), 1)
        self.assertIn("ask_user", notices[0])
        self.assertIn("没有效果", notices[0])


class FatalMessageTest(unittest.TestCase):
    def test_message_has_path_tool_and_version_hint(self) -> None:
        """
        启动失败消息必须给出三样东西：哪个文件、哪个名字、可能的原因。

        版本错配那句不是套话——拷来的 Skill 可能是为更新版本写的，
        这与自己打错字是完全不同的两种情况，处理方式也不同。
        """
        fatals = check_builtin_tool_names([_spec("a", ("read_fil",))], KNOWN)
        msg = format_fatal_message(fatals)
        self.assertIn("a.md", msg)
        self.assertIn("read_fil", msg)
        self.assertIn("版本", msg)


class PruneTest(unittest.TestCase):
    """第二段：连接完成后的 MCP 剪枝与降级（AC17）。"""

    REGISTERED = frozenset({"read_file", "run_command", "mcp__ok__tool"})

    def test_partial_prune_keeps_the_rest(self) -> None:
        specs, warnings = prune_mcp_tool_names(
            [_spec("a", ("read_file", "mcp__gone__t"))], self.REGISTERED
        )
        self.assertEqual(specs[0].allowed_tools, ("read_file",))
        self.assertEqual(len(warnings), 1)
        self.assertIn("未连接", warnings[0])

    def test_connected_mcp_tool_kept(self) -> None:
        specs, warnings = prune_mcp_tool_names(
            [_spec("a", ("mcp__ok__tool",))], self.REGISTERED
        )
        self.assertEqual(specs[0].allowed_tools, ("mcp__ok__tool",))
        self.assertEqual(warnings, [])

    def test_pruned_to_empty_degrades_to_none(self) -> None:
        """
        全部剔空 → allowed_tools 置回 None（不收窄），而不是留一个空白名单。

        空白名单意味着模型一个工具都看不见，Skill 直接变废物，
        且失败形态隐蔽（模型只说「我没有工具可用」）。降级不影响安全——
        白名单是提升选对工具准确率的手段，不是安全边界（N10）。
        """
        specs, warnings = prune_mcp_tool_names(
            [_spec("a", ("mcp__gone__t", "mcp__also_gone__t2"))], self.REGISTERED
        )
        self.assertIsNone(specs[0].allowed_tools)
        # 两条剔除警告 + 一条降级警告
        self.assertEqual(len(warnings), 3)
        self.assertIn("全部失效", warnings[-1])
        self.assertIn("不收窄", warnings[-1])

    def test_none_whitelist_untouched(self) -> None:
        """未声明白名单的 Skill 经剪枝后仍为 None，不产生警告。"""
        specs, warnings = prune_mcp_tool_names([_spec("a", None)], self.REGISTERED)
        self.assertIsNone(specs[0].allowed_tools)
        self.assertEqual(warnings, [])

    def test_unchanged_spec_is_same_object(self) -> None:
        """一项都没剔时原样返回同一个对象，避免无意义重建。"""
        spec = _spec("a", ("read_file",))
        specs, _ = prune_mcp_tool_names([spec], self.REGISTERED)
        self.assertIs(specs[0], spec)

    def test_order_and_length_preserved(self) -> None:
        """输出列表与输入等长同序，调用方可以直接整体替换 catalog。"""
        given = [_spec("a", ("read_file",)), _spec("b", None), _spec("c", ("mcp__gone__t",))]
        specs, _ = prune_mcp_tool_names(given, self.REGISTERED)
        self.assertEqual([s.name for s in specs], ["a", "b", "c"])


if __name__ == "__main__":
    unittest.main()
