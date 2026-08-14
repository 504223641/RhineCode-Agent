"""
预设层的纯逻辑护栏（auto-plan 扩展 T2）。

这些用例钉住的是**结构性质**而不是某个具体取值：多数用例遍历 `Preset` 枚举，
因此将来新增预设时它们会自动覆盖到，漏改映射表当场红。
"""

import unittest

from rhinecode.permission.models import PermissionMode
from rhinecode.presets import (
    DEFAULT_PRESET,
    MODE_LABELS,
    PRESET_AXES,
    PRESET_CYCLE,
    Preset,
    axes_of,
    next_preset,
    preset_of,
)


class PresetAxesTest(unittest.TestCase):
    """预设与两条轴之间的映射（spec F1/F2/F4）。"""

    def test_every_preset_is_registered_in_both_tables(self):
        """
        每个预设都必须同时登记在 `PRESET_AXES` 与 `PRESET_CYCLE` 里。

        遍历枚举而不是硬编码两个名字：新增预设却漏改任一张表时当场红，
        而漏改的后果（`axes_of` 抛 KeyError / 循环切不过去）在界面上
        表现为「按了没反应」，不看代码查不出来。
        """
        for preset in Preset:
            with self.subTest(preset=preset):
                self.assertIn(preset, PRESET_AXES)
                self.assertIn(preset, PRESET_CYCLE)

    def test_both_presets_pin_the_same_permission_mode(self):
        """
        当前两个预设**共用同一个权限档**（spec F2）。

        ⚠ 这条钉住的是与 todo 原文的刻意分歧：`plan` **不动权限档**。
        它一旦变红，说明有人把 `plan` 做成了「设 STRICT」——那会让
        `present_plan` 获批后的同一回合里，第一次写文件就被④层拒绝
        （获批是回合中途发生的，档位不会跟着变）。

        改这条之前先读 `presets.py` 模块 docstring 的「为什么 plan 不动权限档」。
        """
        modes = {mode for mode, _ in PRESET_AXES.values()}
        self.assertEqual(modes, {PermissionMode.PERMISSIVE})

    def test_planning_flag_distinguishes_the_two_presets(self):
        """两个预设靠规划阶段那条轴区分：auto 关、plan 开。"""
        self.assertEqual(axes_of(Preset.AUTO), (PermissionMode.PERMISSIVE, False))
        self.assertEqual(axes_of(Preset.PLAN), (PermissionMode.PERMISSIVE, True))

    def test_axes_round_trip(self):
        """
        **spec AC6a 的可断言对象**：预设 → 两条轴 → 预设，往返后回到自己。

        这条是「切过去再切回来，两条轴逐字复原」在纯逻辑层的形态。
        """
        for preset in Preset:
            with self.subTest(preset=preset):
                _mode, planning = axes_of(preset)
                self.assertIs(preset_of(planning), preset)


class PresetCycleTest(unittest.TestCase):
    """切换循环（spec F5/F6）。"""

    def test_cycle_is_a_two_state_loop(self):
        """从任一预设连按两次都回到出发点。"""
        for preset in Preset:
            with self.subTest(preset=preset):
                self.assertIs(next_preset(next_preset(preset)), preset)

    def test_cycle_covers_every_preset(self):
        """
        循环必须走遍所有预设，不能有到不了的孤岛。

        遍历枚举求闭包而不是硬编码两步：将来加第三个预设时，
        若只把它接进表里却没接进环，这条会红。
        """
        reached = {Preset.AUTO}
        cursor = Preset.AUTO
        for _ in range(len(Preset)):
            cursor = next_preset(cursor)
            reached.add(cursor)
        self.assertEqual(reached, set(Preset))

    def test_default_preset_is_auto(self):
        """启动缺省是 auto（spec F3）。"""
        self.assertIs(DEFAULT_PRESET, Preset.AUTO)


class ModeLabelTest(unittest.TestCase):
    """权限档的显示名（spec F16）。"""

    def test_permissive_is_displayed_as_auto(self):
        """
        放行档的显示名以 `auto` 打头——与预设同名是刻意的。

        `auto` 预设内部就是这个档位，叫同一个名字用户才不会以为它们是两回事
        （显示成「放行」的话，没人能确认那和 `auto` 是不是同一个东西）。
        """
        self.assertTrue(MODE_LABELS[PermissionMode.PERMISSIVE].startswith(Preset.AUTO.value))

    def test_every_label_carries_its_yaml_value(self):
        """
        每个显示名都要带上**在 YAML 里该写什么**。

        本表唯一的消费者是子 Agent 报告，用途是帮用户对照自己写的角色定义。
        只显示「auto」的话，用户不知道 `permission_mode:` 该填什么——YAML 里
        认的是 `permissive`，压根没有 `auto` 这个取值。这条对三档一视同仁，
        免得将来有人觉得中文名够用了就把括号去掉。
        """
        for mode in PermissionMode:
            with self.subTest(mode=mode):
                self.assertIn(mode.value, MODE_LABELS[mode])

    def test_all_three_modes_keep_a_label(self):
        """
        三档**都要**有显示名（spec F16）。

        `/perm` 删掉之后 STRICT / DEFAULT 用户切不到了，但它们仍可经
        `permissions.yaml` 与角色定义的 `permission_mode` 抵达——内置的
        `explorer` / `planner` 就声明了 `strict`，子 Agent 报告要显示得出来。
        顺手把它们从表里删掉的话，那一列会退化成显示英文枚举值。
        """
        for mode in PermissionMode:
            with self.subTest(mode=mode):
                self.assertIn(mode, MODE_LABELS)
                self.assertTrue(MODE_LABELS[mode])

    def test_labels_are_distinct(self):
        """三个显示名互不相同——重名等于把两个档位显示成同一个东西。"""
        self.assertEqual(len(set(MODE_LABELS.values())), len(PermissionMode))


if __name__ == "__main__":
    unittest.main()
