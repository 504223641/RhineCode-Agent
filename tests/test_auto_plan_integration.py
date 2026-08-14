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


class ShiftTabDoesNotEchoTest(unittest.TestCase):
    """
    `Shift+Tab` 切换成功时**不往历史区写东西**（状态栏已经显示了模式）。

    ## 为什么值得一条护栏

    这是「同一件事说两遍」的那种问题——**加回去不会有任何东西报错**，
    只是聊天区多出一行状态信息。而聊天区是对话内容、不是状态显示，
    与 tui-display F31 给 `Ctrl+C` 提示定的口径一致
    （那条同样只活在状态栏左区，刻意不进聊天区）。

    ## ⚠ 但「切不动」那一支必须留着

    非 DeepSeek Provider 上两条轴都无可控对象。此时若也保持安静，
    用户按下去毫无反应，分不清是「没生效」还是「这个键压根没被接住」。
    """

    def test_action_shows_message_only_when_unavailable(self) -> None:
        """
        结构护栏：按键动作里那次 `show_message` 被一个**条件**包着，
        且判据是具名常量而不是字面量。

        字面量比较会在有人改文案时静默失配，表现为「切不动时也不再提示」
        ——而那正是这条分支存在的全部理由。
        """
        import inspect

        from rhinecode.conversation import PRESET_SWITCH_UNAVAILABLE
        from rhinecode.tui.app import RhineApp

        source = inspect.getsource(RhineApp.action_cycle_preset)
        self.assertIn("PRESET_SWITCH_UNAVAILABLE", source)
        # 反证：不得无条件回显。
        self.assertNotIn(
            "self.show_message(self.switch_mode(ModeTarget.PRESET))",
            source,
            "Shift+Tab 切换成功时不该往历史区写东西——状态栏已经显示模式了",
        )
        self.assertTrue(PRESET_SWITCH_UNAVAILABLE)

    def test_slash_mode_still_echoes(self) -> None:
        """
        ⚠ 反证：`/mode` 那条入口**仍然照常回显**。

        这不是两条入口的分叉：用户**敲了一条命令**，一条命令不给任何回应
        看起来就是没执行；而按键有状态栏当回执。两条入口共用的是
        **领域行为**（`cycle_preset` 写哪两条轴），本来就不包括
        「界面上怎么回执」。

        没有这一条的话，「顺手把 /mode 也改安静」会让一条命令看起来坏掉。
        """
        import inspect

        from rhinecode.commands import builtins

        source = inspect.getsource(builtins._handle_mode)
        self.assertIn("show_message", source)


class ApprovePanelTellsTheTruthTest(unittest.IsolatedAsyncioTestCase):
    """
    计划审批面板上那句说明，必须与**获批之后实际会发生什么**一致。

    ## 这条护栏对应的真实缺陷

    面板长期写着「开始执行  写文件/改文件/运行命令仍会逐个确认」——那是
    auto-plan 扩展**之前**的行为。扩展之后计划获批即回到 `auto` 预设（放行档），
    那三类操作一次面板都不弹。用户的 trace 实录：获批后一次 `write_file`、
    两次 `run_command` 全部 `allow（④模式）`，零确认。

    ## 为什么它比「一句文案写错了」严重

    用户是**据此**点下「开始执行」的：他以为后面还有一道人工闸门，
    于是对计划本身的审视就松一档。实际上这就是最后一道。
    项目里已经写下的同类判断是「错误的安全承诺比没有承诺更危险」
    （见 `CLAUDE.md` 已知项 18 对 `deny` 规则失效那次的定性）。

    ## 判据形态

    正面断言「直接执行 / 不再逐个确认」的意思在，**反面断言那句旧承诺不在**
    ——只留正面的话，两句话同时出现（改文案时旧的那半没删干净）也会通过。
    """

    async def test_yes_label_does_not_promise_per_call_confirmation(self) -> None:
        from tests.test_command_tui import _make_app
        from rhinecode.tui.widgets import ConfirmPanel

        app, _ = _make_app()
        async with app.run_test() as pilot:
            app._show_approve_panel("## 目标\n随便一个计划")
            await pilot.pause()

            panel = app.query_one(ConfirmPanel)
            # 按 `id` 取而不是按文字找：表头「计划已就绪，是否开始执行？」里
            # 同样有「开始执行」四个字，按文字找会取到表头，判据就落空了。
            yes = next(
                str(option.prompt) for option in panel._options if option.id == "yes"
            )

        self.assertIn("不再逐个确认", yes)
        # ⚠ 反证：旧文案里那句承诺不得残留。缺省档是放行档，它是假的。
        self.assertNotIn("仍会逐个确认", yes)

    def test_the_claim_matches_the_auto_preset_axis(self) -> None:
        """
        文案的依据是 `auto` 预设的**档位**，这条把两者钉在一起。

        缺省档若改回 `default`（灰色地带交人工确认），面板就该改回「仍会逐个
        确认」——那时本条会红，提醒改文案。没有它的话，两处会各自漂移，
        而漂移的结果是面板对着一个已经变了的世界继续说旧话。
        """
        from rhinecode.presets import PRESET_AXES

        mode, plan_stage = PRESET_AXES[Preset.AUTO]
        self.assertIs(mode, PermissionMode.PERMISSIVE)
        self.assertFalse(plan_stage)


if __name__ == "__main__":
    unittest.main()
