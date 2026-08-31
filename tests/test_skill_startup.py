"""
Skill 启动接线测试（c11 T59）。

覆盖 spec AC16（白名单两段校验的三个分支）、AC25（短命令重名跳过）、
AC38（项目级 Skill 提示的零状态语义）。

参照 c10 `test_command_startup` 的写法：用 patch 替换重量构造，
断言退出码与「哪些资源没被创建」。
"""

import io
import os
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest.mock import MagicMock, patch

import rhinecode.__main__ as entry
import rhinecode.bootstrap as bootstrap
from rhinecode.tools.registry import ToolRegistry


def _named_tool(name: str) -> MagicMock:
    """
    造一个只保证 `.name` 正确的假工具。

    `MagicMock().name` 是 Mock 自身的属性名机制，不能直接赋值，
    必须走 `configure_mock`——直接 `m.name = "x"` 在构造时会被当成 Mock 的名字。
    """
    tool = MagicMock()
    tool.configure_mock(name=name)
    return tool


def _fake_config() -> MagicMock:
    cfg = MagicMock()
    cfg.api_key = "real-key"
    cfg.protocol = "deepseek"
    cfg.model = "test-model"
    # ⚠ **搜索那三项必须显式给值**（web_search 扩展）。
    #
    # `MagicMock` 的任意属性都是**真值**，于是 `cfg.search_enabled` 恒为真、
    # 装配层会去建搜索工具，然后拿一个 Mock 当服务商名去查表——
    # 表现是启动期直接炸掉，而错误信息（`KeyError: <MagicMock ...>`）
    # 跟这几个用例要测的东西毫无关系。
    #
    # 关掉它最省事：这批用例测的是 Skill / 命令的接线，与搜索无关。
    cfg.search_enabled = False
    cfg.search_provider = "brave"
    cfg.search_api_key = ""
    return cfg


class SkillStartupTest(unittest.TestCase):
    """
    在临时工作区里跑真实的 `main()` 启动前半段。

    只 patch 掉 Provider / MCP 连接 / ConversationManager / RhineApp——
    `SkillManager`、`ToolRegistry`、`CommandRegistry` 都用**真的**，
    否则测的就不是接线本身了。
    """

    def setUp(self) -> None:
        self._old_cwd = os.getcwd()
        self._ws = tempfile.TemporaryDirectory()
        os.chdir(self._ws.name)
        self.project_skills = Path(self._ws.name) / ".rhinecode" / "skills"
        self.project_skills.mkdir(parents=True)

        self._home = tempfile.TemporaryDirectory()
        self.home = Path(self._home.name)

        self.app = MagicMock()
        # C6：入口在 cleanup 之后会读 `app.return_code` 决定进程退出码。
        # MagicMock 的属性默认是个**真值**，不给它一个真实的 0 会让每条用例
        # 都以为程序崩溃了（`sys.exit(<MagicMock>)`）。真 App 上它是 int。
        self.app.return_code = 0
        self.captured: dict = {}

    def tearDown(self) -> None:
        os.chdir(self._old_cwd)
        self._ws.cleanup()
        self._home.cleanup()

    def _skill_report(self) -> str:
        """
        取本次启动注入 ConversationManager 的那个 SkillManager 的 `/skills` 报告。

        启动阶段的状态信息（短命令冲突 / 白名单警告 / 项目级 Skill 告知）
        统一由这份报告承载，不再打印到 stderr，故断言也从 stderr 移到这里。
        """
        return self.captured["conversation_kwargs"]["skill_manager"].report()

    def _write_skill(self, name: str, **extra) -> None:
        lines = [f"name: {name}", "description: 说明"]
        lines.extend(f"{k}: {v}" for k, v in extra.items())
        (self.project_skills / f"{name}.md").write_text(
            "---\n" + "\n".join(lines) + "\n---\n正文\n", encoding="utf-8"
        )

    def _run_main(self):
        """
        跑一次 main()，返回 (退出码或 None, stderr 文本)。

        `builtin_skills_dir` 指向一个空目录：本文件测的是接线，
        不该受三个内置样板的干扰（它们由 test_skill_manager 覆盖）。
        """
        empty_builtin = Path(self._home.name) / "no-builtin"

        def fake_conversation(*args, **kwargs):
            self.captured["conversation_kwargs"] = kwargs
            m = MagicMock()
            m.memory_manager = MagicMock()
            return m

        def fake_app(manager, config, command_registry, **kwargs):
            # **kwargs 是必须的：build_app 现在会额外传 recorder=（trace 设施）。
            # 这不是弱化断言——该替身只做捕获，不校验参数。
            self.captured["command_registry"] = command_registry
            return self.app

        stderr = io.StringIO()
        code = None
        with (
            # 装配用到的符号已随 build_app 迁到 bootstrap，故 patch 目标随之改到
            # bootstrap；仍打在 entry 上的只有 load（配置加载没迁走）。
            patch.object(entry, "load", return_value=_fake_config()),
            patch.object(bootstrap, "create_provider", return_value=MagicMock()),
            patch.object(bootstrap.mcp_config, "load_all", return_value=({}, [])),
            patch.object(bootstrap, "MCPManager") as mcp_cls,
            # 假的 MCPAddServerTool 必须有真实的 name：注册中心用 tool.name
            # 作键，MagicMock 的默认 name 是个 Mock 对象，会让
            # `mcp_add_server` 压根不在 known 里、被当成笔误。
            patch.object(
                bootstrap,
                "MCPAddServerTool",
                return_value=_named_tool("mcp_add_server"),
            ),
            patch.object(bootstrap, "builtin_skills_dir", return_value=empty_builtin),
            patch.object(Path, "home", return_value=self.home),
            patch.object(bootstrap, "ConversationManager", side_effect=fake_conversation),
            patch.object(bootstrap, "RhineApp", side_effect=fake_app),
            patch("sys.argv", ["rhine", "--config", "fake.yaml"]),
            redirect_stderr(stderr),
        ):
            self.captured["mcp_cls"] = mcp_cls
            try:
                entry.main()
            except SystemExit as e:
                code = e.code
        return code, stderr.getvalue()

    # ---- AC16：第一段严格校验 ----

    def test_single_underscore_mcp_builtin_is_not_a_typo(self) -> None:
        """
        白名单含 `mcp_add_server`（**单**下划线内置工具）→ 正常启动。

        这是 AC16 的第三分支，也是「校验位置卡在窄窗口」那条决策的回归护栏：
        它比其它内置工具晚注册，校验若放在「核心工具注册完成」的位置就会
        把这个合法条目误判成笔误并硬终止启动。
        """
        self._write_skill("m", allowed_tools="[mcp_add_server]")
        code, err = self._run_main()
        self.assertIsNone(code, f"不该退出，stderr={err}")
        self.app.run.assert_called_once()

    # ---- AC16/AC17：第二段 MCP 剪枝 ----

    def test_skill_colliding_with_builtin_command_is_skipped(self) -> None:
        """
        名字与内置命令重名 → 短命令未注册、有提示、内置命令行为不变，
        但该 Skill **仍在** command_infos 里（它本身完全可用，走 /skills run）。
        """
        self._write_skill("clear")
        self._write_skill("mine")
        code, err = self._run_main()

        self.assertIsNone(code)
        self.assertEqual(err, "")
        # 「短命令被占用、改走 /skills run」的替代入口提示由 /skills 报告承载。
        self.assertIn("/skills run clear", self._skill_report())

        registry = self.captured["command_registry"]
        self.assertFalse(registry.has_skill_command("clear"))
        self.assertTrue(registry.has_skill_command("mine"))
        # 内置 /clear 仍是内置那条（UI 类型，而 Skill 短命令是 PROMPT）。
        from rhinecode.commands.models import CommandType

        self.assertIs(registry.resolve("/clear").command_type, CommandType.UI)

        sm = self.captured["conversation_kwargs"]["skill_manager"]
        self.assertIn("clear", [i.name for i in sm.command_infos()])

    def test_skill_short_commands_registered(self) -> None:
        self._write_skill("deploy")
        self._run_main()
        registry = self.captured["command_registry"]
        self.assertTrue(registry.has_skill_command("deploy"))
        self.assertIsNotNone(registry.resolve("/deploy"))

    # ---- 对齐改造：预授权的无法识别项不再致命 ----

    def test_unknown_granted_tool_no_longer_kills_startup(self) -> None:
        """
        `allowed-tools` 里写了本系统没有的工具名 → **正常启动**，只给一条警告。

        这与 C11 的取舍**正好相反**（那时是 fail-fast 退出），理由是来源变了：
        白名单曾是自家格式、写错就是笔误；现在这份声明可能来自 Claude Code 或
        Codex，里面出现 `Task` / `TodoWrite` 是**正常现象**，不该让程序起不来。

        这条是本次改造在启动路径上最重要的行为变化，必须有护栏钉住。

        ⚠ **样例工具名换过一次**：本用例原先用的是 `WebFetch`，
        而 web_fetch 扩展让它变成了**真工具**（`skills/validation.py` 的
        `_TOOL_ALIASES` 里有它），于是不再产生「没有对应的工具类别」警告。
        现改用 `TodoWrite`——挑样例时请确认它确实不在那张别名表里。
        """
        self._write_skill("ext", **{"allowed-tools": "[TodoWrite, Bash]"})
        code, err = self._run_main()

        self.assertIsNone(code, "不该退出")
        self.assertEqual(err, "")
        report = self._skill_report()
        self.assertIn("TodoWrite", report)
        self.assertIn("没有对应的工具类别", report)
        # 认识的那条仍照常生效
        sm = self.captured["conversation_kwargs"]["skill_manager"]
        rules, _ = sm.grants_for_spec(sm.get("ext"))
        self.assertIn("Bash", [r.tool for r in rules])

    # ---- AC38：项目级提示的零状态语义 ----

    def test_project_skill_notice_printed_every_startup(self) -> None:
        """
        含项目级 Skill → **每次启动的 `/skills` 报告里都有**信任提示，
        不做「只提示一次」的持久化。

        项目级 Skill 来自代码仓库，git pull 后可能凭空多出几个；
        有状态的话新增时状态不失效，新来的就被静默吞掉了。
        零状态语义与提示渠道无关——换到 `/skills` 之后这条依然必须成立。
        """
        self._write_skill("proj")
        self._run_main()
        report1 = self._skill_report()
        self.assertIn("项目级 Skill", report1)
        self.assertIn("proj", report1)

        self.app = MagicMock()
        # C6：入口在 cleanup 之后会读 `app.return_code` 决定进程退出码。
        # MagicMock 的属性默认是个**真值**，不给它一个真实的 0 会让每条用例
        # 都以为程序崩溃了（`sys.exit(<MagicMock>)`）。真 App 上它是 int。
        self.app.return_code = 0
        self._run_main()
        self.assertIn("项目级 Skill", self._skill_report())

    def test_no_project_skill_no_notice(self) -> None:
        _, err = self._run_main()
        self.assertNotIn("项目级 Skill", err)
        self.assertNotIn("项目级 Skill", self._skill_report())

    # ---- 接线本身 ----

    def test_skill_manager_injected_into_conversation(self) -> None:
        """同一个 SkillManager 实例注入 ConversationManager（单一来源）。"""
        self._write_skill("s")
        self._run_main()
        sm = self.captured["conversation_kwargs"].get("skill_manager")
        self.assertIsNotNone(sm)
        self.assertIsNotNone(sm.get("s"))

    def test_load_skill_tool_registered(self) -> None:
        """load_skill 工具被注册进注册中心（它不在 ToolRegistry.default() 里）。"""
        captured_registry = {}
        real_default = ToolRegistry.default

        def spy_default():
            r = real_default()
            captured_registry["r"] = r
            return r

        with patch.object(ToolRegistry, "default", side_effect=spy_default):
            self._run_main()
        self.assertIn("load_skill", captured_registry["r"].names())


if __name__ == "__main__":
    unittest.main()
