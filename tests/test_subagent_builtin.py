"""
内置角色的护栏（c13 T31，覆盖 AC5）。

内置角色是**用户看到的唯一范例**。它自己写得不规范，等于示范了不该学的写法——
所以这里的判据比普通角色严：零警告、只读、描述得像样。
"""

from __future__ import annotations

import unittest

from rhinecode.permission.models import PermissionMode
from rhinecode.subagents.discovery import discover_agents
from rhinecode.subagents.models import HARD_MAX_TURNS, builtin_agents_dir
from rhinecode.subagents.toolset import resolve_toolset

# ⚠ **硬编码的内置角色清单**：新增内置角色时这条当场红，逼人来登记。
#
# 照 C11 `BuiltinSamplesTest` 的先例。它防的是「悄悄多了一个内置角色，
# 而没人检查过它自己合不合规」——内置角色会出现在每个用户的 /agents 里，
# 加一个的门槛应当高于加一个普通文件。
EXPECTED_BUILTINS = {"explorer"}

# 只读工具集合。内置角色一律只读——缺省配置下（ASK 自动拒绝）写类工具
# 本来就用不了，声明了只会让用户以为它能写。
READ_ONLY_TOOLS = {"read_file", "glob_files", "grep_content"}


class BuiltinCatalogTest(unittest.TestCase):
    def setUp(self) -> None:
        self.catalog = discover_agents(None, None, builtin_agents_dir())

    def test_exactly_the_registered_builtins(self) -> None:
        self.assertEqual(set(self.catalog.specs), EXPECTED_BUILTINS)

    def test_no_load_errors(self) -> None:
        """内置目录里不该有任何加载错误——它随程序分发，用户改不了。"""
        self.assertEqual(self.catalog.errors, ())

    def test_no_shadowed(self) -> None:
        self.assertEqual(self.catalog.shadowed, ())


class ExplorerTest(unittest.TestCase):
    def setUp(self) -> None:
        catalog = discover_agents(None, None, builtin_agents_dir())
        self.spec = catalog.specs["explorer"]

    def test_zero_warnings(self) -> None:
        """
        **内置样板自身必须零警告。**

        它是用户看到的唯一范例。自己触发警告（写了不支持的字段、轮次越界）
        等于示范了不该学的写法——用户会照着抄。
        """
        self.assertEqual(self.spec.warnings, (), f"内置角色不该有警告：{self.spec.warnings}")

    def test_only_read_only_tools(self) -> None:
        self.assertIsNotNone(self.spec.tools, "内置角色必须显式声明工具白名单")
        self.assertTrue(
            set(self.spec.tools) <= READ_ONLY_TOOLS,
            f"explorer 只能用只读工具，实际：{self.spec.tools}",
        )

    def test_strict_permission_mode(self) -> None:
        """
        声明严格档。它只读，本来也不需要更宽的档位；
        显式声明能让 `/agents` 里一眼看出「这个角色动不了任何东西」。
        """
        self.assertIs(self.spec.permission_mode, PermissionMode.STRICT)

    def test_max_turns_within_hard_cap(self) -> None:
        self.assertGreater(self.spec.max_turns, 0)
        self.assertLessEqual(self.spec.max_turns, HARD_MAX_TURNS)

    def test_description_is_substantial_and_trigger_first(self) -> None:
        """
        描述要够长，且**触发词前置**（照 C11 作者期扩展的结论）。

        模型是靠这一句判断「要不要委派给它」的。写成「一个调研角色」这种
        自我介绍式的句子，模型无从判断什么时候该用；先写「什么时候用它」
        才对得上它实际的决策过程。
        """
        desc = self.spec.description
        self.assertGreaterEqual(len(desc), 30, "描述太短，模型判断不了何时该用")
        self.assertLessEqual(len(desc), 400, "描述过长会挤占清单预算")
        # 触发场景应当出现在前半段
        self.assertIn("需要", desc[:40])

    def test_body_tells_it_to_be_self_contained(self) -> None:
        """
        正文必须交代「只有最后一段会回流」——这是子 Agent 最容易犯的错：
        写一段依赖上文的结论（「如上所述」），而主对话根本看不到「上文」。
        """
        self.assertIn("自包含", self.spec.body)

    def test_body_tells_it_not_to_attempt_writes(self) -> None:
        """
        正文要明说「不要尝试修改」。

        不说的话它会去试，然后在权限层被自动拒绝——白烧轮次，
        而且拒绝理由对它是噪音。
        """
        self.assertIn("不要尝试修改", self.spec.body)


class UsableOutOfTheBoxTest(unittest.TestCase):
    """AC5：缺省配置下 explorer 能真的跑起来（工具集非空）。"""

    def test_toolset_non_empty_with_default_registry(self) -> None:
        from rhinecode.tools.registry import ToolRegistry

        catalog = discover_agents(None, None, builtin_agents_dir())
        spec = catalog.specs["explorer"]
        result = resolve_toolset(ToolRegistry.default().names(), spec)

        self.assertFalse(
            result.is_empty,
            f"explorer 在缺省工具集下必须能启动，原因：{result.reason}",
        )
        self.assertEqual(result.unresolved, (), "内置角色的工具名不该有拼写错误")
        self.assertEqual(set(result.allowed), set(spec.tools))


if __name__ == "__main__":
    unittest.main()
