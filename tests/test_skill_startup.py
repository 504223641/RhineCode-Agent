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
from rhinecode.commands import build_builtin_registry
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
        self.captured: dict = {}

    def tearDown(self) -> None:
        os.chdir(self._old_cwd)
        self._ws.cleanup()
        self._home.cleanup()

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

        def fake_app(manager, config, command_registry):
            self.captured["command_registry"] = command_registry
            return self.app

        stderr = io.StringIO()
        code = None
        with (
            patch.object(entry, "load", return_value=_fake_config()),
            patch.object(entry, "create_provider", return_value=MagicMock()),
            patch.object(entry.mcp_config, "load_all", return_value=({}, [])),
            patch.object(entry, "MCPManager") as mcp_cls,
            # 假的 MCPAddServerTool 必须有真实的 name：注册中心用 tool.name
            # 作键，MagicMock 的默认 name 是个 Mock 对象，会让
            # `mcp_add_server` 压根不在 known 里、被当成笔误。
            patch.object(
                entry,
                "MCPAddServerTool",
                return_value=_named_tool("mcp_add_server"),
            ),
            patch.object(entry, "builtin_skills_dir", return_value=empty_builtin),
            patch.object(Path, "home", return_value=self.home),
            patch.object(entry, "ConversationManager", side_effect=fake_conversation),
            patch.object(entry, "RhineApp", side_effect=fake_app),
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

    def test_typo_in_builtin_tool_name_exits_before_mcp_connect(self) -> None:
        """
        白名单含不存在的内置工具名 → 退出码 1，且 **connect_all 未被调用**。

        「connect_all 未被调用」是「无子进程残留」的可测形式：stdio 子进程
        只可能由 connect_all → _connect_one → StdioTransport.start() 创建，
        比查进程表稳定得多。
        """
        self._write_skill("bad", allowed_tools="[read_fil]")
        code, err = self._run_main()

        self.assertEqual(code, 1)
        self.assertIn("read_fil", err)
        self.assertIn("bad.md", err)
        self.assertIn("版本", err)
        self.captured["mcp_cls"].return_value.connect_all.assert_not_called()
        self.app.run.assert_not_called()
        self.assertNotIn("conversation_kwargs", self.captured)

    def test_exempt_tool_names_start_normally_with_notice(self) -> None:
        """白名单含 ask_user / load_skill → 正常启动，只给「声明无效果」提示。"""
        self._write_skill("ex", allowed_tools="[ask_user, load_skill, read_file]")
        code, err = self._run_main()

        self.assertIsNone(code)
        self.assertIn("没有效果", err)
        self.app.run.assert_called_once()

    def test_whitelisting_load_skill_does_not_kill_startup(self) -> None:
        """
        白名单里写 `load_skill` → 正常启动。

        **顺序护栏**：`LoadSkillTool` 必须在算 `known_tools` 之前注册。
        若在 `startup()` 之后才注册（一个很自然的写法，因为它逻辑上属于
        「Skill 系统的一部分」），`load_skill` 就不在 known 里，
        这条完全合法的声明会被判成笔误，启动直接挂掉。
        """
        self._write_skill("ls", allowed_tools="[load_skill]")
        code, err = self._run_main()
        self.assertIsNone(code, f"不该退出，stderr={err}")
        self.assertIn("没有效果", err)

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

    def test_unconnected_mcp_tool_is_pruned_with_warning(self) -> None:
        """白名单含未连接的 mcp__ 工具 → 正常启动、有警告、该项被剔除。"""
        self._write_skill("m", allowed_tools="[read_file, mcp__nope__t]")
        code, err = self._run_main()

        self.assertIsNone(code)
        self.assertIn("未连接", err)
        sm = self.captured["conversation_kwargs"]["skill_manager"]
        self.assertEqual(sm.get("m").allowed_tools, ("read_file",))

    # ---- AC25：短命令重名跳过 ----

    def test_skill_colliding_with_builtin_command_is_skipped(self) -> None:
        """
        名字与内置命令重名 → 短命令未注册、有提示、内置命令行为不变，
        但该 Skill **仍在** command_infos 里（它本身完全可用，走 /skills run）。
        """
        self._write_skill("clear")
        self._write_skill("mine")
        code, err = self._run_main()

        self.assertIsNone(code)
        self.assertIn("/clear", err)
        self.assertIn("/skills run clear", err)

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

    # ---- AC38：项目级提示的零状态语义 ----

    def test_project_skill_notice_printed_every_startup(self) -> None:
        """
        含项目级 Skill → 每次启动都提示，**不做「只提示一次」的持久化**。

        项目级 Skill 来自代码仓库，git pull 后可能凭空多出几个；
        有状态的话新增时状态不失效，新来的就被静默吞掉了。
        """
        self._write_skill("proj")
        _, err1 = self._run_main()
        self.assertIn("项目级 Skill", err1)
        self.assertIn("proj", err1)

        self.app = MagicMock()
        _, err2 = self._run_main()
        self.assertIn("项目级 Skill", err2)

    def test_no_project_skill_no_notice(self) -> None:
        _, err = self._run_main()
        self.assertNotIn("项目级 Skill", err)

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
