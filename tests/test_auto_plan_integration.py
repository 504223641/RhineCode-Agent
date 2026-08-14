"""
auto-plan 扩展的跨层护栏（T11）。

这些用例验的是「几层拼起来之后行为对不对」，单看任何一层都验不出来：
启动档由协调层的构造点决定、命令消失由注册表决定、审批归属跨了协调层与
Agent 循环两侧。

⚠ **本文件分辨力最高的是三条反证**（拒绝后留在 plan、审批不碰 engine.mode、
strict 角色仍被收窄）。只验正路的话，一个「审批时把两条轴一起清掉」的实现
也会全绿，而那会静默改掉主对话的权限档。
"""

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from rhinecode.commands import build_builtin_registry
from rhinecode.config import Config
from rhinecode.conversation import ConversationManager
from rhinecode.permission.engine import narrower_mode
from rhinecode.permission.models import PermissionMode
from rhinecode.presets import Preset
from rhinecode.tools.registry import ToolRegistry


def _manager(tmp: Path) -> ConversationManager:
    """
    造一个工具可用（`protocol: deepseek` + 注册中心）的协调层实例。

    Provider 用 `SimpleNamespace` 替身：本文件一条模型请求都不发，
    只看状态与判定。`user_dir` 指向临时目录，免得读到开发机上真实的
    `~/.rhinecode/permissions.yaml`——那会让用例的结果取决于跑它的人。
    """
    config = Config(
        protocol="deepseek",
        model="test-model",
        base_url="http://test",
        api_key="test-key",
        debug_log=False,
    )
    return ConversationManager(
        SimpleNamespace(), config, ToolRegistry(), user_dir=tmp / "userdir"
    )


class StartupPresetTest(unittest.TestCase):
    """启动即 auto（spec F3/AC1）。"""

    def test_startup_preset_is_auto(self):
        """
        不做任何操作时，预设是 auto、底层档位是放行档。

        两个都断言是刻意的：只看 `preset_value` 的话，一个「预设写死返回
        auto、档位其实还是默认档」的实现也会绿——而那种实现下用户会在
        「界面说 auto」的同时被确认面板反复打断。
        """
        with tempfile.TemporaryDirectory() as tmp:
            manager = _manager(Path(tmp))
            self.assertEqual(manager.preset_value, "auto")
            self.assertIs(manager.preset, Preset.AUTO)
            self.assertIs(manager.permission_engine.mode, PermissionMode.PERMISSIVE)
            self.assertFalse(manager.plan_mode)


class PermCommandGoneTest(unittest.TestCase):
    """旧的权限档命令彻底消失（spec F7/AC7）。"""

    def test_perm_command_and_all_aliases_are_gone(self):
        """
        `/perm` 与它的两个别名都解析不到——于是它们走「未知命令」路径、
        **不进 AI**（c10 的既有语义）。

        逐个断言而不是只查 `/perm`：别名是单独登记的，删规范名却漏删别名
        的话，`/permissions` 仍然能执行，而 `/help` 里已经看不到它。
        """
        registry = build_builtin_registry()
        for name in ("/perm", "/permissions", "/allowed-tools"):
            with self.subTest(name=name):
                self.assertIsNone(registry.resolve(name))

    def test_mode_command_and_plan_alias_resolve_to_the_same_spec(self):
        """`/mode` 与别名 `/plan` 必须解析到同一条命令（spec F6/AC6）。"""
        registry = build_builtin_registry()
        self.assertIsNotNone(registry.resolve("/mode"))
        self.assertIs(registry.resolve("/plan"), registry.resolve("/mode"))


class PresetCycleIntegrationTest(unittest.TestCase):
    """切换在真实协调层上的效果（spec F1/F5/AC6a）。"""

    def test_cycle_switches_the_planning_axis(self):
        with tempfile.TemporaryDirectory() as tmp:
            manager = _manager(Path(tmp))
            manager.cycle_preset()
            self.assertIs(manager.preset, Preset.PLAN)
            self.assertTrue(manager.plan_mode)

    def test_cycle_round_trip_restores_both_axes(self):
        """
        **spec AC6a**：auto → plan → auto 走一个来回，两条轴逐字复原。

        这条钉住「预设层没有偷偷记住第三份状态」。切两次之后若档位或
        规划阶段有一个没回到出发值，说明有人在预设层里存了状态。
        """
        with tempfile.TemporaryDirectory() as tmp:
            manager = _manager(Path(tmp))
            before = (manager.permission_engine.mode, manager.plan_mode)
            manager.cycle_preset()
            manager.cycle_preset()
            after = (manager.permission_engine.mode, manager.plan_mode)
            self.assertEqual(before, after)
            self.assertIs(manager.preset, Preset.AUTO)


class PlanApprovalTest(unittest.TestCase):
    """审批后的归属（spec F12/F13/AC8/AC9）。"""

    def test_approved_plan_exits_plan_mode(self):
        """
        **F12**：获批 → 预设当场变回 auto，且下一条消息也在 auto 下跑。

        「下一条消息」这半边由 `plan_mode` 为假直接保证——Agent 循环每次运行
        都从这个字段取规划阶段的入参。
        """
        with tempfile.TemporaryDirectory() as tmp:
            manager = _manager(Path(tmp))
            manager.cycle_preset()
            self.assertTrue(manager.plan_mode)

            manager.approve_plan_callback = lambda plan: True
            approved = manager._approve_plan_then_exit("做这些事")

            self.assertTrue(approved)
            self.assertFalse(manager.plan_mode)
            self.assertIs(manager.preset, Preset.AUTO)

    def test_rejected_plan_stays_in_plan(self):
        """
        **F13（反证）**：被拒 → 仍在 plan。

        拒绝意味着方案不对、还得接着规划。自动退出会让下一轮重想时失去保护
        ——模型可以直接动手了，而用户刚刚明确表示过不认可这个方案。
        """
        with tempfile.TemporaryDirectory() as tmp:
            manager = _manager(Path(tmp))
            manager.cycle_preset()

            manager.approve_plan_callback = lambda plan: False
            approved = manager._approve_plan_then_exit("做这些事")

            self.assertFalse(approved)
            self.assertTrue(manager.plan_mode, "被拒不得退出 plan")
            self.assertIs(manager.preset, Preset.PLAN)

    def test_missing_callback_is_fail_closed(self):
        """没有注入审批回调时按「未批准」处理，且不改预设。"""
        with tempfile.TemporaryDirectory() as tmp:
            manager = _manager(Path(tmp))
            manager.cycle_preset()
            self.assertFalse(manager._approve_plan_then_exit("x"))
            self.assertTrue(manager.plan_mode)

    def test_approval_does_not_touch_engine_mode(self):
        """
        **反证，本文件分辨力最高的一条**：审批不得改动共享引擎的档位。

        两个预设的档位本来就相同，所以「顺手在审批回调里 set_mode 一下」
        看起来无害、测试也照样绿——但那正是 spec 分歧一整段论证要排除的形态：

        - 它跑在**工作线程**上，而主线程正在读同一个字段渲染状态栏；
        - 子 Agent 的档位是委派那一刻按 `min(主对话档, 角色声明档)` 派生的，
          回合中途翻转会让前后两次委派的**同名角色拿到不同档位**，
          而配置和界面上都看不出来。

        ⚠ **单靠本条的行为断言是不够的，必须配隔壁那条结构断言。**
        两个预设的档位本来就相同，所以最可能出现的错法——在审批路径里补一句
        `set_mode(PERMISSIVE)`——写下去之后本条**照样绿**。行为断言只挡得住
        「改成了别的档位」这种更明显的错法。
        """
        with tempfile.TemporaryDirectory() as tmp:
            manager = _manager(Path(tmp))
            manager.cycle_preset()
            before = manager.permission_engine.mode

            manager.approve_plan_callback = lambda plan: True
            manager._approve_plan_then_exit("做这些事")
            self.assertIs(manager.permission_engine.mode, before)

            # 被拒那条路径同样不得碰它
            manager.cycle_preset()
            manager.approve_plan_callback = lambda plan: False
            manager._approve_plan_then_exit("做这些事")
            self.assertIs(manager.permission_engine.mode, before)

    def test_approval_path_does_not_mention_set_mode(self):
        """
        **结构反证**：审批包装的实现里不得出现 `set_mode`。

        这条补的是上一条测不到的那一半。写成结构断言而不是行为断言，
        是因为「顺手补一句 `set_mode(PERMISSIVE)`」在行为上**看不出区别**
        （两个预设档位相同），只有从源码上才判得出来。

        同类先例：`test_env_filter.py::SingleChokepointTest`——同样是「行为上
        分辨不出、但结构必须成立」的不变量。
        """
        import inspect

        src = inspect.getsource(ConversationManager._approve_plan_then_exit)
        self.assertNotIn("set_mode", src)
        # 反向自检：这个方法确实是改预设的那一个（否则上面那条断言毫无意义，
        # 因为任何一个不相干的方法里都不会出现 set_mode）
        self.assertIn("plan_mode", src)

    def test_notify_is_only_fired_on_approval(self):
        """
        界面刷新通知只在**真的改了预设**时发出。

        被拒时也发的话，状态栏会平白刷新一次；虽然看不出问题，但那意味着
        「通知」与「变化」脱钩了，将来靠它判断状态的地方会被误导。
        """
        calls = []
        with tempfile.TemporaryDirectory() as tmp:
            manager = _manager(Path(tmp))
            manager.notify_preset_change = lambda: calls.append(1)

            manager.cycle_preset()
            manager.approve_plan_callback = lambda plan: False
            manager._approve_plan_then_exit("x")
            self.assertEqual(calls, [], "被拒不得触发刷新")

            manager.approve_plan_callback = lambda plan: True
            manager._approve_plan_then_exit("x")
            self.assertEqual(len(calls), 1)


class RoleModeStillNarrowsTest(unittest.TestCase):
    """既有配置继续生效（spec F9/F10/F11/N4/AC10）。"""

    def test_strict_role_still_narrows_under_auto(self):
        """
        auto 主对话下，声明 `permission_mode: strict` 的角色生效档位仍是 strict。

        `/perm` 删掉之后 strict 用户切不到了，但它**没有从系统里消失**——
        内置的 `explorer` / `planner` 就声明了它。这条钉住「删的是切换入口，
        不是档位本身」（spec F10 的 `dontAsk` 手法）。
        """
        with tempfile.TemporaryDirectory() as tmp:
            manager = _manager(Path(tmp))
            main_mode = manager.permission_engine.mode
            self.assertIs(main_mode, PermissionMode.PERMISSIVE)

            for declared, expected in (
                (PermissionMode.STRICT, PermissionMode.STRICT),
                (PermissionMode.DEFAULT, PermissionMode.DEFAULT),
                # 声明放行档不产生提权效果——它与主对话档相同，取严即它自己
                (PermissionMode.PERMISSIVE, PermissionMode.PERMISSIVE),
            ):
                with self.subTest(declared=declared):
                    self.assertIs(narrower_mode(main_mode, declared), expected)

    def test_all_three_modes_survive_in_the_enum(self):
        """
        三个档位仍在枚举里（spec F9）。

        删掉任何一个都会让既有的 `permissions.yaml` 与角色定义解析失败——
        而那是 spec N4「不破坏既有配置」的底线。
        """
        self.assertEqual(
            {m.value for m in PermissionMode}, {"strict", "default", "permissive"}
        )


if __name__ == "__main__":
    unittest.main()
