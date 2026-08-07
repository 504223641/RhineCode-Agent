"""
`/agents` 命令的分发测试（c13 T26）。

只验**命令层**：输入怎么切分、调到哪个控制器方法、参数是什么。
报告与取消的实际内容由 `test_subagent_report.py` 与 `test_subagent_service.py` 负责。

`/agents` 与既有的只读命令（`/mcp` / `/hooks`）有一处不同：它带一个 `cancel`
子命令，因此不是纯只读。理由写在 `CommandController.cancel_subagents` 的
docstring 里——一个跑偏的后台子 Agent 若没有取消入口，用户只能退出整个程序。
"""

import unittest

from rhinecode.commands import CommandDispatcher, ReportTarget, build_builtin_registry

from tests.test_command_dispatcher import FakeController


class AgentsCommandTest(unittest.TestCase):
    def setUp(self) -> None:
        self.dispatcher = CommandDispatcher(build_builtin_registry())

    def _dispatch(self, text: str) -> FakeController:
        controller = FakeController()
        self.dispatcher.dispatch(text, controller)
        return controller

    # ---------- 只读形态 ----------

    def test_bare_shows_report(self) -> None:
        c = self._dispatch("/agents")
        self.assertIn(("query_report", ReportTarget.AGENTS), c.calls)
        self.assertIn(("show_message", "report:agents"), c.calls)

    def test_uppercase_resolves(self) -> None:
        """C10 起命令大小写不敏感。"""
        c = self._dispatch("/AGENTS")
        self.assertIn(("query_report", ReportTarget.AGENTS), c.calls)

    def test_bare_does_not_cancel(self) -> None:
        """
        **反证**：无参形态绝不能碰取消。

        `/agents` 是用户最常敲的形态（就是想看一眼），它若顺手取消了任务，
        故障现场会是「我只是看了一下，后台任务就没了」。
        """
        c = self._dispatch("/agents")
        self.assertNotIn("cancel_subagents", c.names())

    # ---------- 取消形态 ----------

    def test_cancel_with_id(self) -> None:
        c = self._dispatch("/agents cancel a3f1c9")
        self.assertIn(("cancel_subagents", "a3f1c9"), c.calls)
        self.assertIn(("show_message", "cancel:a3f1c9"), c.calls)

    def test_cancel_all_maps_to_none(self) -> None:
        """
        `all` 与「不带参数」都归一成 `None`。

        统一成一种表示是刻意的：下游只需处理 `None` 一种「全部」，
        不必同时认识 `"all"` 字面量——两处对「全部」的表示不一致是典型的
        「改一处忘一处」来源。
        """
        for text in ("/agents cancel all", "/agents cancel"):
            with self.subTest(text=text):
                c = self._dispatch(text)
                self.assertIn(("cancel_subagents", None), c.calls)

    def test_cancel_all_is_case_insensitive(self) -> None:
        c = self._dispatch("/agents cancel ALL")
        self.assertIn(("cancel_subagents", None), c.calls)

    def test_cancel_refreshes_status(self) -> None:
        """取消改变了运行中的任务数，状态栏必须跟着刷新。"""
        c = self._dispatch("/agents cancel all")
        self.assertIn(("refresh_status",), c.calls)

    # ---------- 未知子命令 ----------

    def test_unknown_subcommand_shows_usage(self) -> None:
        c = self._dispatch("/agents bogus")
        shown = [x[1] for x in c.calls if x[0] == "show_message"]
        self.assertTrue(shown)
        self.assertIn("未知子命令", shown[0])
        self.assertIn("cancel", shown[0])
        # 既不查报告也不取消
        self.assertNotIn("query_report", c.names())
        self.assertNotIn("cancel_subagents", c.names())

    def test_unknown_subcommand_does_not_reach_ai(self) -> None:
        """C10 的「未知命令不进 AI」对子命令同样成立。"""
        c = self._dispatch("/agents bogus")
        self.assertNotIn("send_user_message", c.names())

    # ---------- 元数据 ----------

    def test_appears_in_help(self) -> None:
        c = self._dispatch("/help")
        shown = [x[1] for x in c.calls if x[0] == "show_message"]
        self.assertIn("/agents", shown[0])

    def test_registered_as_visible_command(self) -> None:
        registry = build_builtin_registry()
        names = [s.name for s in registry.visible_commands()]
        self.assertIn("/agents", names)


if __name__ == "__main__":
    unittest.main()
