"""
commands 层的 Skill 接入单测（c11 T42）。

覆盖 spec AC24（命令层）、AC25（短命令注册与重名跳过）、AC26（`/skills` 五形态）、
AC31（注册层的确定性）。
"""

import unittest

from rhinecode.commands.builtins import build_builtin_registry
from rhinecode.commands.dispatcher import CommandDispatcher
from rhinecode.commands.models import (
    CommandSpec,
    CommandType,
    ReportTarget,
)
from rhinecode.commands.registry import CommandRegistry
from rhinecode.commands.skill_commands import build_skill_command_specs
from rhinecode.skills.models import SkillCommandInfo
from tests.test_command_dispatcher import FakeController


def _infos(*names) -> list[SkillCommandInfo]:
    return [
        SkillCommandInfo(name=n, description=f"{n} 的说明", forked=False)
        for n in names
    ]


class SkillCommandFactoryTest(unittest.TestCase):
    """SkillCommandInfo → CommandSpec 的转换。"""

    def test_basic_fields(self) -> None:
        specs = build_skill_command_specs(_infos("commit"))
        self.assertEqual(len(specs), 1)
        spec = specs[0]
        self.assertEqual(spec.name, "/commit")
        self.assertEqual(spec.aliases, ())
        self.assertIn("commit", spec.description)
        self.assertIs(spec.command_type, CommandType.PROMPT)
        self.assertEqual(spec.argument_hint, "[参数]")

    def test_closures_bind_to_their_own_info(self) -> None:
        """
        **闭包绑定回归**：三条命令各自调对自己的 Skill。

        循环里直接 def handler 会让所有闭包捕获同一个 cell、全部指向最后一条。
        失败形态极其恶心：菜单显示 /commit、执行起来跑 review，不崩不报错。
        这里逐条执行并断言收到的 name，第 1 条尤其重要（错误实现下它会调成第 3 条）。
        """
        specs = build_skill_command_specs(_infos("aaa", "bbb", "ccc"))
        registry = CommandRegistry()
        registry.replace_skill_commands(specs)
        dispatcher = CommandDispatcher(registry)

        for expected in ("aaa", "bbb", "ccc"):
            controller = FakeController()
            dispatcher.dispatch(f"/{expected} 参数内容", controller)
            calls = [c for c in controller.calls if c[0] == "run_skill"]
            self.assertEqual(len(calls), 1, expected)
            self.assertEqual(calls[0][1], expected)

    def test_display_is_the_raw_user_input(self) -> None:
        """
        display 必须是用户敲的原始输入（AC24）。

        没有它，`/resume` 回放时用户看到的会是机器生成的自包含文本，
        而不是自己当初敲的那条命令。
        """
        registry = CommandRegistry()
        registry.replace_skill_commands(build_skill_command_specs(_infos("commit")))
        controller = FakeController()
        CommandDispatcher(registry).dispatch("/commit 修复登录超时", controller)
        call = [c for c in controller.calls if c[0] == "run_skill"][0]
        self.assertEqual(call[1], "commit")
        self.assertEqual(call[2], "修复登录超时")
        self.assertEqual(call[3], "/commit 修复登录超时")

    def test_arguments_preserved_verbatim(self) -> None:
        """参数原样保留，不做 shell 分词。"""
        registry = CommandRegistry()
        registry.replace_skill_commands(build_skill_command_specs(_infos("s")))
        controller = FakeController()
        CommandDispatcher(registry).dispatch('/s  "a b" | c  ', controller)
        call = [c for c in controller.calls if c[0] == "run_skill"][0]
        self.assertEqual(call[2], '"a b" | c')


class ReplaceSkillCommandsTest(unittest.TestCase):
    """注册表的运行时替换（AC25/AC31，改造点 2）。"""

    def test_conflicting_spec_is_skipped_others_registered(self) -> None:
        """
        与内置命令重名的那条被跳过，其余照常注册（F25）。

        不能整批失败——一个 Skill 与内置命令重名，不该连累其它 Skill 都没短命令。
        """
        registry = build_builtin_registry()
        specs = build_skill_command_specs(_infos("clear", "mine"))
        skipped = registry.replace_skill_commands(specs)

        self.assertEqual([s.name for s in skipped], ["/clear"])
        self.assertTrue(registry.has_skill_command("mine"))
        self.assertFalse(registry.has_skill_command("clear"))

    def test_builtin_behaviour_unchanged_after_conflict(self) -> None:
        """重名不影响内置命令：/clear 仍解析到内置那条。"""
        registry = build_builtin_registry()
        builtin_clear = registry.resolve("/clear")
        registry.replace_skill_commands(build_skill_command_specs(_infos("clear")))
        self.assertIs(registry.resolve("/clear"), builtin_clear)

    def test_alias_conflict_also_skipped(self) -> None:
        """与内置命令**别名**重名的同样被跳过（/reset 是 /clear 的别名）。"""
        registry = build_builtin_registry()
        skipped = registry.replace_skill_commands(
            build_skill_command_specs(_infos("reset"))
        )
        self.assertEqual(len(skipped), 1)
        self.assertFalse(registry.has_skill_command("reset"))

    def test_second_call_fully_replaces_previous_set(self) -> None:
        """
        二次调用完全替换旧集合——热更新删掉的 Skill 其短命令必须消失。

        （注意别用 `new` 之类的名字做样本：`/new` 是内置 `/clear` 的别名，
        会走「重名跳过」分支，测的就不是替换语义了。）
        """
        registry = build_builtin_registry()
        registry.replace_skill_commands(build_skill_command_specs(_infos("older")))
        self.assertTrue(registry.has_skill_command("older"))

        registry.replace_skill_commands(build_skill_command_specs(_infos("newer")))
        self.assertFalse(registry.has_skill_command("older"))
        self.assertIsNone(registry.resolve("/older"))
        self.assertTrue(registry.has_skill_command("newer"))

    def test_empty_list_removes_all_skill_commands(self) -> None:
        registry = build_builtin_registry()
        registry.replace_skill_commands(build_skill_command_specs(_infos("a", "b")))
        registry.replace_skill_commands([])
        self.assertIsNone(registry.resolve("/a"))
        # 内置命令一个不少（auto-plan 扩展起十五条：C10 十二条 + /skills + /hooks
        # + /agents + /tasks，减去 /plan 与 /perm 合并成的 /mode 那一条）。
        self.assertIsNotNone(registry.resolve("/clear"))
        self.assertEqual(len(registry.visible_commands()), 15)

    def test_index_has_no_ghost_entry_for_skipped_spec(self) -> None:
        """
        被跳过的 spec 不得在索引里留下幽灵项。

        `_stage` 是边遍历边写入、遇冲突才抛、不回滚的；若不用独立的 probe 字典，
        一个多标识 spec 的前几个标识会留在索引里，指向一个不在 _skill_specs
        中的 spec，`resolve` 会解析出一条实际不存在的命令。
        """
        registry = build_builtin_registry()
        # 造一条带别名的 spec：别名与内置 /ctx 冲突，规范名 /brand 不冲突。
        # 正确实现会整条跳过；错误实现会把 /brand 留在索引里。
        bad = CommandSpec(
            name="/brand",
            aliases=("/ctx",),
            description="d",
            usage="u",
            command_type=CommandType.PROMPT,
            handler=lambda i, c: None,
        )
        skipped = registry.replace_skill_commands([bad])
        self.assertEqual(len(skipped), 1)
        self.assertIsNone(registry.resolve("/brand"))
        # /ctx 仍是内置 /context 的别名。
        self.assertEqual(registry.resolve("/ctx").name, "/context")

    def test_registry_unchanged_when_all_conflict(self) -> None:
        """全部冲突时注册表保持可用状态（原子性）。"""
        registry = build_builtin_registry()
        before = len(registry.visible_commands())
        registry.replace_skill_commands(build_skill_command_specs(_infos("clear", "exit")))
        self.assertEqual(len(registry.visible_commands()), before)

    def test_skill_commands_appear_in_completion(self) -> None:
        """Tab 补全候选里出现 Skill 短命令（AC24）。"""
        registry = build_builtin_registry()
        registry.replace_skill_commands(build_skill_command_specs(_infos("commit")))
        values = [c.value for c in registry.complete("/com")]
        self.assertIn("/commit", values)
        # 内置的 /compact 也在，说明没被挤掉。
        self.assertIn("/compact", values)

    def test_builtin_commands_come_first_in_help(self) -> None:
        """拼接顺序：内置在前、Skill 在后（顺序稳定）。"""
        registry = build_builtin_registry()
        registry.replace_skill_commands(build_skill_command_specs(_infos("zzz")))
        names = [s.name for s in registry.visible_commands()]
        self.assertEqual(names[-1], "/zzz")
        self.assertEqual(names[0], "/help")


class HasSkillCommandTest(unittest.TestCase):
    """has_skill_command 只查 Skill 短命令，不看内置（F7 入口提示的正确性）。"""

    def test_true_for_registered_skill_command(self) -> None:
        registry = build_builtin_registry()
        registry.replace_skill_commands(build_skill_command_specs(_infos("mine")))
        self.assertTrue(registry.has_skill_command("mine"))

    def test_false_when_name_collides_with_builtin(self) -> None:
        """
        **关键分支**：名字与内置命令重名、短命令未注册 → 必须返回 False，
        即使 `resolve("/context")` 能命中内置命令。

        若返回 True，给用户的入口提示会变成「请执行 /context」，而那条命令
        跑的是上下文用量报告。指向一个存在但错误的命令，比指向不存在的更糟。
        """
        registry = build_builtin_registry()
        registry.replace_skill_commands(build_skill_command_specs(_infos("context")))
        self.assertIsNotNone(registry.resolve("/context"))
        self.assertFalse(registry.has_skill_command("context"))

    def test_false_for_unknown_name(self) -> None:
        self.assertFalse(build_builtin_registry().has_skill_command("nope"))


class SkillsCommandTest(unittest.TestCase):
    """/skills 五种形态的分派（AC26）。"""

    def setUp(self) -> None:
        self.registry = build_builtin_registry()
        self.dispatcher = CommandDispatcher(self.registry)

    def _run(self, text: str) -> FakeController:
        controller = FakeController()
        self.dispatcher.dispatch(text, controller)
        return controller

    def test_bare_shows_report(self) -> None:
        c = self._run("/skills")
        self.assertIn(("query_report", ReportTarget.SKILLS), c.calls)

    def test_prompt_shows_injection_report(self) -> None:
        c = self._run("/skills prompt")
        self.assertIn(("query_report", ReportTarget.SKILLS_PROMPT), c.calls)

    def test_reload_calls_and_refreshes_status(self) -> None:
        c = self._run("/skills reload")
        self.assertIn("reload_skills", c.names())
        # 热更新可能自动卸载消失的 Skill，激活数变了，状态栏必须刷新。
        self.assertIn("refresh_status", c.names())

    def test_off_with_name(self) -> None:
        c = self._run("/skills off commit")
        self.assertIn(("deactivate_skill", "commit"), c.calls)
        self.assertIn("refresh_status", c.names())

    def test_off_without_name_means_all(self) -> None:
        c = self._run("/skills off")
        self.assertIn(("deactivate_skill", None), c.calls)

    def test_run_with_arguments(self) -> None:
        c = self._run("/skills run review 只看 auth 模块")
        call = [x for x in c.calls if x[0] == "run_skill"][0]
        self.assertEqual(call[1], "review")
        self.assertEqual(call[2], "只看 auth 模块")

    def test_run_preserves_inner_whitespace(self) -> None:
        """参数原样保留，不做 shell 分词。"""
        c = self._run("/skills run x a  b")
        call = [x for x in c.calls if x[0] == "run_skill"][0]
        self.assertEqual(call[2], "a  b")

    def test_run_without_name_shows_usage(self) -> None:
        c = self._run("/skills run")
        self.assertNotIn("run_skill", c.names())
        msg = [x for x in c.calls if x[0] == "show_message"][0][1]
        self.assertIn("用法", msg)

    def test_unknown_subcommand_shows_hint_and_usage(self) -> None:
        c = self._run("/skills frobnicate")
        msg = [x for x in c.calls if x[0] == "show_message"][0][1]
        self.assertIn("未知子命令", msg)
        self.assertIn("用法", msg)

    def test_subcommand_is_case_insensitive(self) -> None:
        c = self._run("/skills RELOAD")
        self.assertIn("reload_skills", c.names())

    def test_command_itself_is_case_insensitive(self) -> None:
        c = self._run("/SKILLS")
        self.assertIn(("query_report", ReportTarget.SKILLS), c.calls)


if __name__ == "__main__":
    unittest.main()
