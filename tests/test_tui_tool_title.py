"""
工具行标题解析的纯函数单测（tui-display 扩展 T8/T10/T16，spec F11/F12/AC9/AC10/AC22）。

`resolve_call_title` 决定用户在工具行上看到什么。改造前是
`Task(name=explorer, task=调研权限层…)`——键名对用户零信息量，却挤占了本就
不多的横向空间，真正要看的值反而被截断。改造后每个工具只显示一个主参数的值。

⚠ **本文件最要紧的一条是转义**（AC22）：F12 让主参数的**原始值**直接进入标题，
不再被 `键=值` 的格式包裹。路径、命令、URL、任务描述全是可能含字面 `[` 的自由
文本，而漏一次转义就是布局阶段 `MarkupError` + 整个应用退出，没有任何
try/except 兜得住。下面有专门的用例覆盖「未闭合 `[`」这个真实崩溃形态。
"""

import unittest
from types import SimpleNamespace

from rhinecode.subagents.tasks import BRANCH_AGENT_NAME
from rhinecode.tui.widgets import _TOOL_LABELS, escape, resolve_call_title


def call(name: str, arguments=None):
    """造一个形态等价于 `provider.base.ToolCall` 的最小对象。"""
    return SimpleNamespace(name=name, arguments=arguments)


# 与 `app.on_mount` 从工具注册中心建出来的那份映射同形。
PRIMARY = {
    "read_file": "path",
    "write_file": "path",
    "edit_file": "path",
    "glob_files": "pattern",
    "grep_content": "pattern",
    "run_command": "command",
    "web_fetch": "url",
    "send_message": "to",
    "load_skill": "name",
}


class PrimaryArgBranchTest(unittest.TestCase):
    """分支 2：登记过主参数的工具，括号里只放那个值。"""

    def test_read_shows_only_the_path(self) -> None:
        label, inner = resolve_call_title(
            call("read_file", {"path": "rhinecode/tui/app.py", "offset": 10}), PRIMARY
        )
        self.assertEqual(label, "Read")
        self.assertEqual(inner, "rhinecode/tui/app.py")

    def test_no_key_equals_form_anywhere(self) -> None:
        """AC10a：括号里不得出现 `键=` 形式。"""
        _, inner = resolve_call_title(
            call("run_command", {"command": "python -m unittest", "timeout": 60}), PRIMARY
        )
        self.assertNotIn("=", inner)
        self.assertEqual(inner, "python -m unittest")

    def test_each_registered_tool(self) -> None:
        samples = {
            "write_file": ({"path": "docs/notes.md", "content": "x"}, "Write", "docs/notes.md"),
            "edit_file": ({"path": "a/b.py", "old_string": "x"}, "Update", "a/b.py"),
            "glob_files": ({"pattern": "rhinecode/**/*.py"}, "Glob", "rhinecode/**/*.py"),
            "grep_content": ({"pattern": "Layer", "path": "x"}, "Grep", "Layer"),
            "run_command": ({"command": "git status"}, "Bash", "git status"),
            "web_fetch": ({"url": "https://x.dev/g", "prompt": "y"}, "WebFetch", "https://x.dev/g"),
            "send_message": ({"to": "explorer", "body": "hi"}, "SendMessage", "explorer"),
            "load_skill": ({"name": "commit"}, "Skill", "commit"),
        }
        for tool_name, (args, want_label, want_inner) in samples.items():
            with self.subTest(tool=tool_name):
                label, inner = resolve_call_title(call(tool_name, args), PRIMARY)
                self.assertEqual(label, want_label)
                self.assertEqual(inner, want_inner)

    def test_long_value_is_clipped(self) -> None:
        """值仍然截断（N5「一切展示都有界」）。"""
        _, inner = resolve_call_title(call("read_file", {"path": "a" * 300}), PRIMARY)
        self.assertLess(len(inner), 300)
        self.assertTrue(inner.endswith("…"))

    def test_newlines_collapse_to_spaces(self) -> None:
        """多行命令压成一行——工具行只有一行高度，换行会把布局撑开。"""
        _, inner = resolve_call_title(
            call("run_command", {"command": "line1\nline2"}), PRIMARY
        )
        self.assertNotIn("\n", inner)
        self.assertEqual(inner, "line1 line2")


class FallbackBranchTest(unittest.TestCase):
    """分支 3：兜底。这是「新增工具忘了声明也不会显示异常」的那道保险。"""

    def test_undeclared_tool_falls_back_to_key_value_summary(self) -> None:
        """AC10b：没登记主参数 → 回退键值对摘要，而不是显示成空括号。"""
        label, inner = resolve_call_title(call("mcp_add_server", {"name": "x", "cmd": "y"}), PRIMARY)
        self.assertEqual(label, "mcp_add_server")
        self.assertIn("name=x", inner)

    def test_declared_but_argument_missing(self) -> None:
        """登记了主参数、本次调用却没带那个键 → 同样走兜底，不显示空括号。"""
        _, inner = resolve_call_title(call("read_file", {"offset": 3}), PRIMARY)
        self.assertIn("offset=3", inner)

    def test_declared_but_value_is_blank(self) -> None:
        """值是空串也当作「没有」——`Read()` 看起来像无参调用，是异常形态。"""
        _, inner = resolve_call_title(call("read_file", {"path": "   "}), PRIMARY)
        self.assertNotEqual(inner.strip(), "")

    def test_empty_mapping_reproduces_pre_change_behaviour(self) -> None:
        """
        **零回归判据**（N3）：拿不到工具注册中心时（非 DeepSeek Provider）
        传空映射，全部走兜底，形态与改造前逐字一致。
        """
        label, inner = resolve_call_title(call("read_file", {"path": "a.py"}), {})
        self.assertEqual(label, "Read")
        self.assertEqual(inner, "path=a.py")

    def test_none_mapping(self) -> None:
        label, inner = resolve_call_title(call("read_file", {"path": "a.py"}), None)
        self.assertEqual(inner, "path=a.py")

    def test_arguments_none(self) -> None:
        """参数解析失败时不抛异常（这条路径跑在主线程布局前）。"""
        _, inner = resolve_call_title(call("read_file", None), PRIMARY)
        self.assertIn("参数解析失败", inner)

    def test_arguments_not_a_dict(self) -> None:
        _, inner = resolve_call_title(call("read_file", "不是字典"), PRIMARY)
        self.assertIn("参数格式错误", inner)


class DelegateBranchTest(unittest.TestCase):
    """分支 1：委派特例（AC10c）。"""

    def test_role_name_becomes_the_label(self) -> None:
        label, inner = resolve_call_title(
            call("run_agent", {"type": "role", "agent": "explorer", "task": "调研权限层的判定顺序"}),
            PRIMARY,
        )
        self.assertEqual(label, "explorer")
        self.assertEqual(inner, "调研权限层的判定顺序")

    def test_never_shows_the_internal_tool_name(self) -> None:
        label, _ = resolve_call_title(
            call("run_agent", {"type": "role", "agent": "planner", "task": "给方案"}), PRIMARY
        )
        self.assertNotEqual(label, "run_agent")

    def test_branch_delegation_uses_the_placeholder(self) -> None:
        """分支式委派没有角色名，用既有的分支占位名，不留空标签。"""
        label, _ = resolve_call_title(
            call("run_agent", {"type": "branch", "task": "接着看"}), PRIMARY
        )
        self.assertEqual(label, escape(BRANCH_AGENT_NAME))

    def test_delegate_wins_over_primary_mapping(self) -> None:
        """
        **顺序反证**：委派分支必须排在主参数分支之前。

        `run_agent` 的参数里恰好有个 `name`（队员名）。若有人把它登记进
        `primary_args`，而委派分支排在后面，标签就会退回 `run_agent`——
        AC10c 当场失效，而界面上只是「少了个角色名」，很容易被当成小事。
        """
        label, inner = resolve_call_title(
            call("run_agent", {"agent": "explorer", "task": "干活", "name": "worker1"}),
            {**PRIMARY, "run_agent": "name"},
        )
        self.assertEqual(label, "explorer")
        self.assertEqual(inner, "干活")


class EscapeTest(unittest.TestCase):
    """
    AC22：每一处嵌入自由文本的地方都必须经**本项目的** `escape`。

    ⚠ 判据一律用「转义后的形态」而不是「不抛异常」——`resolve_call_title` 本身
    不渲染，漏转义在这里不会报错，要到布局阶段才崩，而那时已经没救了。
    """

    def test_unclosed_bracket_in_path(self) -> None:
        """真实崩溃形态：路径里一个落单的 `[`。"""
        _, inner = resolve_call_title(call("read_file", {"path": "src/[wip/a.py"}), PRIMARY)
        self.assertIn("\\[wip", inner)
        # 摘掉全部已转义的 `\[` 之后不得再剩下任何裸 `[`
        self.assertNotIn("[", inner.replace("\\[", ""))

    def test_unclosed_bracket_in_task_text(self) -> None:
        """委派的任务描述来自模型输出，`[` 是家常便饭。"""
        _, inner = resolve_call_title(
            call("run_agent", {"agent": "explorer", "task": "看看 [未闭合"}), PRIMARY
        )
        self.assertIn("\\[未闭合", inner)

    def test_unclosed_bracket_in_role_name(self) -> None:
        """角色名来自用户写的角色定义文件，同样不可信。"""
        label, _ = resolve_call_title(
            call("run_agent", {"agent": "we[ird", "task": "x"}), PRIMARY
        )
        self.assertIn("\\[", label)

    def test_unregistered_tool_name_is_escaped(self) -> None:
        """MCP 工具名由远端 Server 决定，也可能含 `[`。"""
        label, _ = resolve_call_title(call("mcp__srv__we[ird", {}), PRIMARY)
        self.assertIn("\\[", label)

    def test_truncation_cannot_split_a_bracket_pair(self) -> None:
        """
        **这是那次真实崩溃的确切形态**：先截断、后转义时，`[a, b]` 被从中间切开，
        `]` 丢了而 `[` 留下。本函数必须先截断再无条件转义每一个 `[`，
        使「被截断的括号」也照样转义。
        """
        long_pattern = "[" + "x" * 200 + "]"
        _, inner = resolve_call_title(call("grep_content", {"pattern": long_pattern}), PRIMARY)
        self.assertNotIn("]", inner)          # 确认确实被截断了，用例才有效
        self.assertNotIn("[", inner.replace("\\[", ""))  # 留下的 `[` 必须全部转义


class ToolLabelTest(unittest.TestCase):
    """T10/AC9：展示标签对齐 Claude Code 的工具命名。"""

    def test_claude_code_vocabulary(self) -> None:
        expected = {
            "read_file": "Read",
            "write_file": "Write",
            "edit_file": "Update",
            "glob_files": "Glob",
            "grep_content": "Grep",
            "run_command": "Bash",
            "web_fetch": "WebFetch",
            "send_message": "SendMessage",
            "load_skill": "Skill",
            "task_create": "TaskCreate",
            "task_list": "TaskList",
            "task_get": "TaskGet",
            "task_update": "TaskUpdate",
        }
        for internal, label in expected.items():
            with self.subTest(tool=internal):
                self.assertEqual(_TOOL_LABELS.get(internal), label)

    def test_tools_without_a_claude_code_counterpart_keep_their_own_name(self) -> None:
        """
        **刻意不登记**（AC9 后半）：硬套会造出假的对应关系。

        `mcp_*` 在 Claude Code 那边根本没有对应物；`ask_user` / `present_plan`
        那边叫 `AskUserQuestion` / `ExitPlanMode`，后者直译与本项目「提交计划」
        的语义不符。宁可显示内部名，也不要一个会误导人的假标签。
        """
        for internal in ("mcp_add_server", "mcp_resolve_server", "ask_user", "present_plan"):
            with self.subTest(tool=internal):
                self.assertNotIn(internal, _TOOL_LABELS)
                label, _ = resolve_call_title(call(internal, {"x": "y"}), PRIMARY)
                self.assertEqual(label, internal)

    def test_run_agent_is_not_in_the_label_table(self) -> None:
        """委派走特例分支，登记标签只会变成一个永远用不到的死项。"""
        self.assertNotIn("run_agent", _TOOL_LABELS)


if __name__ == "__main__":
    unittest.main()
