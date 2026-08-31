"""
启动接线测试（c10 T54）：命令注册表在昂贵资源之前构建、冲突时 fail-fast。

用 unittest.mock.patch 替换 __main__ 里的重量构造（Provider / 工具注册中心 /
MCP / ConversationManager / RhineApp），验证：
- 正常路径：build_builtin_registry 的同一实例注入 RhineApp；
- 冲突路径：以退出码 1 结束、stderr 含冲突标识，且 create_provider、
  ToolRegistry.default、MCPManager、RhineApp 均未被调用（spec F2/N4/C08）。
"""

import io
import unittest
from contextlib import redirect_stderr
from unittest.mock import MagicMock, patch

import rhinecode.__main__ as entry
import rhinecode.bootstrap as bootstrap
from rhinecode.commands import CommandRegistrationError, CommandRegistry


def _fake_skill_manager() -> MagicMock:
    """
    SkillManager 的假替身（c11）。

    必须显式给出返回值：`startup()` 默认返回一个**真值** MagicMock，
    而 `__main__` 把非空返回视为「白名单里有不存在的工具名」并 exit(1)——
    不设的话本文件所有用例都会以退出码 1 结束，且报错完全指不到真正的原因。
    """
    sm = MagicMock()
    sm.startup.return_value = []
    sm.runtime_warnings.return_value = ()
    sm.project_skill_notice.return_value = None
    sm.command_infos.return_value = ()
    return sm


def _fake_config() -> MagicMock:
    """构造能通过占位符校验的假 Config。"""
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


class StartupWiringTests(unittest.TestCase):
    def test_registry_instance_injected_into_app(self) -> None:
        """正常构建：同一命令注册表实例注入 RhineApp（spec F3 单一来源）。"""
        registry = CommandRegistry()
        fake_app = MagicMock()
        # C6：入口在 cleanup 之后会读 `app.return_code` 决定进程退出码。
        # MagicMock 的属性默认是**真值**，不给它一个真实的 0 会让 `main()` 以为
        # 程序崩溃了并 `sys.exit(<MagicMock>)`。真 App 上这个字段是 int。
        # ⚠ 同一处修改在 `tests/test_skill_startup.py` 也要做——两份都调 `entry.main()`。
        fake_app.return_code = 0
        with (
            patch.object(entry, "load", return_value=_fake_config()),
            patch.object(bootstrap, "build_builtin_registry", return_value=registry) as build,
            patch.object(bootstrap, "create_provider", return_value=MagicMock()),
            patch.object(bootstrap.ToolRegistry, "default", return_value=MagicMock()),
            patch.object(bootstrap.mcp_config, "load_all", return_value=({}, [])),
            patch.object(bootstrap, "MCPManager") as mcp_cls,
            patch.object(bootstrap, "MCPAddServerTool", return_value=MagicMock()),
            patch.object(bootstrap, "SkillManager", return_value=_fake_skill_manager()),
            patch.object(bootstrap, "LoadSkillTool", return_value=MagicMock()),
            patch.object(bootstrap, "build_skill_command_specs", return_value=[]),
            patch.object(bootstrap, "ConversationManager", return_value=MagicMock()),
            patch.object(bootstrap, "RhineApp", return_value=fake_app) as app_cls,
            patch("sys.argv", ["rhine", "--config", "fake.yaml"]),
        ):
            entry.main()
        build.assert_called_once()
        # RhineApp 的第三个位置参数就是命令注册表实例（同一对象，非拷贝）
        self.assertIs(app_cls.call_args.args[2], registry)
        fake_app.run.assert_called_once()
        mcp_cls.return_value.close_all.assert_called_once()

    def test_registration_conflict_exits_before_expensive_resources(self) -> None:
        """冲突路径：退出码 1、stderr 含冲突标识，昂贵资源均未创建（C03–C08）。"""
        conflict = CommandRegistrationError(
            "command name collision: /ctx is declared by /context and /other"
        )
        stderr = io.StringIO()
        with (
            patch.object(entry, "load", return_value=_fake_config()),
            patch.object(bootstrap, "build_builtin_registry", side_effect=conflict),
            patch.object(bootstrap, "create_provider") as create_provider,
            patch.object(bootstrap.ToolRegistry, "default") as tool_default,
            patch.object(bootstrap, "MCPManager") as mcp_cls,
            patch.object(bootstrap, "ConversationManager") as manager_cls,
            patch.object(bootstrap, "RhineApp") as app_cls,
            patch("sys.argv", ["rhine", "--config", "fake.yaml"]),
            redirect_stderr(stderr),
        ):
            with self.assertRaises(SystemExit) as ctx:
                entry.main()

        self.assertEqual(ctx.exception.code, 1)
        message = stderr.getvalue()
        self.assertIn("/ctx", message)
        self.assertIn("/context", message)
        self.assertIn("/other", message)
        # 冲突发生在昂贵资源之前：Provider、工具注册中心、MCP、Manager（会话锁）、
        # RhineApp 均未创建（spec N4 / checklist C08）
        create_provider.assert_not_called()
        tool_default.assert_not_called()
        mcp_cls.assert_not_called()
        manager_cls.assert_not_called()
        app_cls.assert_not_called()


if __name__ == "__main__":
    unittest.main()
