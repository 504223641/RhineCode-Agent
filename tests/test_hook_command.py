"""
`/hooks` 命令与报告的测试（c12 T39，对应 checklist 第九节 / spec AC22）。

三块：
- 命令登记（规范名 / 类型 / 无别名 / 进补全与帮助）
- 报告三段的内容与「无警告时该段整体不出现」
- **含 `[` 的命令串真的走一次 Textual 布局计算**——`jq '.[]'` 这种写法在
  Hook 配置里很常见，而落单的 `[` 会在布局阶段的主线程抛 `MarkupError`，
  没有任何 try/except 兜得住（见 `test_tui_markup_escape.py` 的现场记载）。
"""

from __future__ import annotations

import asyncio
import unittest

from textual.app import App, ComposeResult

from rhinecode.commands import build_builtin_registry
from rhinecode.commands.models import CommandType, ReportTarget
from rhinecode.hooks import HookManager
from rhinecode.hooks.models import CommandAction, HookEventType, HookRule, PromptAction
from rhinecode.tui.widgets import HistoryView


def _rule(name, event=HookEventType.PRE_TOOL_USE, action=None, source="user", index=0):
    return HookRule(
        name, source, index, event, None,
        action or CommandAction("echo hi", 5),
    )


class CommandRegistrationTest(unittest.TestCase):
    """`/hooks` 的登记形态。"""

    def setUp(self):
        self.registry = build_builtin_registry()

    def test_registered_as_local_without_aliases(self):
        matched = self.registry.resolve_matched("/hooks")
        self.assertIsNotNone(matched, "/hooks 必须可解析")
        spec, hit = matched
        self.assertEqual(hit, "/hooks")
        self.assertEqual(spec.name, "/hooks")
        self.assertEqual(spec.aliases, ())
        self.assertEqual(spec.command_type, CommandType.LOCAL)

    def test_visible_in_help_and_completion(self):
        names = [s.name for s in self.registry.visible_commands()]
        self.assertIn("/hooks", names)

    def test_report_target_enum_has_hooks(self):
        """`ReportTarget` 的成对维护点：枚举 + query_report 分支 + 领域方法。"""
        self.assertEqual(ReportTarget.HOOKS.value, "hooks")


class ReportContentTest(unittest.TestCase):
    """报告三段（AC22）。"""

    def test_empty_manager_points_at_config_locations(self):
        text = HookManager([]).report()
        self.assertIn("没有加载任何 Hook 规则", text)
        self.assertIn("用户级", text)
        self.assertIn("项目级", text)

    def test_lists_rules_in_execution_order(self):
        m = HookManager([
            _rule("用户级一", source="user", index=0),
            _rule("用户级二", source="user", index=1),
            _rule("项目级一", source="project", index=0),
        ])
        text = m.report()
        self.assertLess(text.index("用户级一"), text.index("用户级二"))
        self.assertLess(text.index("用户级二"), text.index("项目级一"))
        self.assertIn("共 3 条", text)

    def test_shows_event_condition_action_and_stats(self):
        m = HookManager([_rule("我的规则", action=PromptAction("记得跑测试"))])
        text = m.report()
        self.assertIn("pre_tool_use", text)
        self.assertIn("无条件", text)
        self.assertIn("记得跑测试", text)
        self.assertIn("触发：0 次", text)

    def test_warning_section_omitted_when_empty(self):
        """恒定出现的空段落会让人下意识跳过整个报告。"""
        self.assertNotIn("加载警告", HookManager([_rule("r")]).report())

    def test_warning_section_present_and_counted(self):
        text = HookManager([_rule("r")], warnings=["某条写坏了", "另一条也坏了"]).report()
        self.assertIn("加载警告（2 条", text)
        self.assertIn("某条写坏了", text)
        self.assertIn("另一条也坏了", text)

    def test_command_string_is_never_truncated(self):
        """
        报告里的命令串完整展示。

        `/hooks` 是用户排查「这条规则到底会跑什么」的唯一入口，
        截断了就答不了那个问题。
        """
        long_cmd = "python -m pytest tests/ -k 'not slow' --maxfail=1 --tb=short -q"
        text = HookManager([_rule("跑测试", action=CommandAction(long_cmd, 30))]).report()
        self.assertIn(long_cmd, text)


class _ReportApp(App):
    """把一段报告文本喂进真实的 HistoryView，触发一次完整布局计算。"""

    def __init__(self, text: str) -> None:
        super().__init__()
        self._text = text

    def compose(self) -> ComposeResult:
        yield HistoryView()

    def on_mount(self) -> None:
        self.query_one(HistoryView).append_system(self._text)


class MarkupSafetyTest(unittest.TestCase):
    """
    含 `[` 的报告必须能真的渲染出来（checklist 第九节）。

    ## 为什么要真跑一次布局

    崩溃发生在**渲染期**而不是构造期：落单的 `[` 被 Textual 当作标签开头，
    在 `OptionList.get_content_height` 这类布局阶段的主线程调用里抛
    `MarkupError`，不在业务调用栈上，没有任何 try/except 兜得住，
    Textual 直接拆掉整个 app。

    只断言 `escape()` 的返回值拦不住「新增一处忘了转义」——那正是最可能发生的形态。
    """

    BRACKET_COMMANDS = [
        # jq 取数组元素——Hook 里最常见的写法之一（从 stdin 的 JSON 取字段）
        "jq -r '.[] | .file_path'",
        # sed 字符类
        "sed -i 's/[a-z]*//g' out.txt",
        # 只有开括号、没有闭括号（截断/半截写法）
        "echo [unclosed",
    ]

    def _render(self, text: str) -> None:
        async def run() -> None:
            app = _ReportApp(text)
            async with app.run_test():
                pass

        asyncio.run(run())

    def test_bracket_commands_render_without_markup_error(self):
        for command in self.BRACKET_COMMANDS:
            with self.subTest(command=command):
                report = HookManager([_rule("含括号", action=CommandAction(command, 5))]).report()
                self.assertIn(command, report, "报告里应原样含该命令")
                self._render(report)  # 不抛 MarkupError 即通过

    def test_project_notice_with_brackets_renders(self):
        command = "jq -r '.[] | .tool' >> /tmp/audit.log"
        m = HookManager([_rule("审计", action=CommandAction(command, 5), source="project")])
        notice = m.project_notice()
        self.assertIsNotNone(notice)
        self.assertIn(command, notice)
        self._render(notice)


class ProminenceTest(unittest.TestCase):
    """
    项目级提示必须**醒目**，不能走 dim 通道（人眼评审后补的护栏）。

    ## 这条防的是什么

    `append_system` 把系统消息统一包成 `[dim]◆ …[/dim]`——比正文更暗。
    项目级 Hook 的逐条展示曾经走的就是它，于是本项目里唯一一段
    「这些命令会在你机器上直接执行」的警告，渲染出来比普通提示还不显眼，
    方向正好反了。

    另一半是 Markdown 星号：`**直接执行**` 在 Textual 里**不会变粗**
    （它只认 `[bold]…[/bold]`），只会显示成两个字面星号。
    """

    def _spans(self, markup: str):
        from textual.content import Content

        return [s.style for s in Content.from_markup(markup).spans]

    def test_append_warning_is_not_dim(self):
        from rhinecode.tui.widgets import escape

        styles = self._spans(f"[bold #FFA500]{escape('⚠ 危险')}[/bold #FFA500]")
        self.assertTrue(styles, "应当有样式跨度")
        self.assertNotIn("dim", " ".join(styles), "警告不能比正文更暗")
        self.assertIn("bold", " ".join(styles))

    def test_append_system_is_dim_for_contrast(self):
        """对照组：普通系统消息确实是 dim 的——两者必须不同，否则这条护栏没意义。"""
        from rhinecode.tui.widgets import escape

        styles = self._spans(f"[dim]◆ {escape('普通提示')}[/dim]")
        self.assertIn("dim", " ".join(styles))

    def test_project_notice_has_no_markdown_asterisks(self):
        """上屏文本里不能有 `**`——Textual 不认，只会显示成字面星号。"""
        m = HookManager([_rule("上报", source="project")])
        notice = m.project_notice() or ""
        self.assertNotIn("**", notice)
        self.assertIn("直接执行", notice, "强调没了但话还得在")

    def test_report_has_no_markdown_asterisks(self):
        text = HookManager([_rule("r")], warnings=["某条写坏了"]).report()
        self.assertNotIn("**", text)


if __name__ == "__main__":
    unittest.main()
