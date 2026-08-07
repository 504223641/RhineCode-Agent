"""
内置角色的护栏（c13 T31，覆盖 AC5）。

内置角色是**用户看到的唯一范例**。它自己写得不规范，等于示范了不该学的写法——
所以这里的判据比普通角色严。

**通用判据遍历全部内置角色**（零警告、描述得像样、轮次合法、能真的启动），
每个角色再各有几条专属断言。这样新增一个内置角色时，通用那批自动覆盖到它，
不需要有人记得回来抄一遍。
"""

from __future__ import annotations

import unittest

from rhinecode.permission.models import PermissionMode
from rhinecode.subagents.discovery import discover_agents
from rhinecode.subagents.models import HARD_MAX_TURNS, builtin_agents_dir
from rhinecode.subagents.toolset import GLOBAL_DENIED_TOOLS, resolve_toolset
from rhinecode.tools.registry import ToolRegistry

# ⚠ **硬编码的内置角色清单**：新增内置角色时这条当场红，逼人来登记。
#
# 照 C11 `BuiltinSamplesTest` 的先例。它防的是「悄悄多了一个内置角色，
# 而没人检查过它自己合不合规」——内置角色会出现在每个用户的 /agents 里，
# 加一个的门槛应当高于加一个普通文件。
#
# 三个角色的分工（对标 Claude Code 的 Explore / Plan / general-purpose）：
#   explorer        —— 只读调研：现在是什么样
#   planner         —— 只读方案：接下来该怎么做
#   general-purpose —— 全工具执行：既要查又要动手
EXPECTED_BUILTINS = {"explorer", "planner", "general-purpose"}

# 只读工具集合。声明了白名单的内置角色一律只限这些——
# 缺省配置下（判 ASK 自动拒绝）写类工具本来就用不了，声明了只会让用户以为它能写。
READ_ONLY_TOOLS = {"read_file", "glob_files", "grep_content"}

# 明确**不**声明白名单（继承全部工具）的内置角色。
# 单列出来是为了让「哪个角色能动手」这件事在测试里也一目了然。
FULL_TOOL_BUILTINS = {"general-purpose"}


def _catalog():
    return discover_agents(None, None, builtin_agents_dir())


class BuiltinCatalogTest(unittest.TestCase):
    def setUp(self) -> None:
        self.catalog = _catalog()

    def test_exactly_the_registered_builtins(self) -> None:
        self.assertEqual(set(self.catalog.specs), EXPECTED_BUILTINS)

    def test_no_load_errors(self) -> None:
        """内置目录里不该有任何加载错误——它随程序分发，用户改不了。"""
        self.assertEqual(self.catalog.errors, ())

    def test_no_shadowed(self) -> None:
        self.assertEqual(self.catalog.shadowed, ())


class EveryBuiltinTest(unittest.TestCase):
    """
    对**每个**内置角色都成立的判据。

    写成遍历而不是逐个复制：新增内置角色时它们自动覆盖到，
    不会出现「新加的那个没人验」。
    """

    def setUp(self) -> None:
        self.catalog = _catalog()
        self.tool_names = ToolRegistry.default().names()

    def test_zero_warnings(self) -> None:
        """
        **内置样板自身必须零警告。**

        它是用户看到的唯一范例。自己触发警告（写了不支持的字段、轮次越界）
        等于示范了不该学的写法——用户会照着抄。
        """
        for name, spec in self.catalog.specs.items():
            with self.subTest(agent=name):
                self.assertEqual(spec.warnings, (), f"{name} 不该有警告：{spec.warnings}")

    def test_description_is_substantial_and_trigger_first(self) -> None:
        """
        描述要够长，且**触发词前置**（照 C11 作者期扩展的结论）。

        模型是靠这一句判断「要不要委派给它」的。写成「一个调研角色」这种
        自我介绍式的句子，模型无从判断什么时候该用；先写「什么时候用它」
        才对得上它实际的决策过程。

        判据取**结构**而不是某个关键词：三条描述都写成
        「〈什么时候〉**时用它**」，于是「时用它」出现在前段就等价于
        「触发条件排在自我介绍之前」。查某个具体的词（比如「需要」）
        是更弱的代理——换个同义说法就误报，而结构没变。
        """
        for name, spec in self.catalog.specs.items():
            with self.subTest(agent=name):
                desc = spec.description
                self.assertGreaterEqual(len(desc), 30, f"{name} 的描述太短")
                self.assertLessEqual(len(desc), 400, f"{name} 的描述过长，会挤占清单预算")
                head = desc[:60]
                self.assertIn(
                    "时用它", head,
                    f"{name} 的描述应写成「〈什么时候〉时用它」，触发条件在前。实际开头：{head}",
                )

    def test_max_turns_within_hard_cap(self) -> None:
        for name, spec in self.catalog.specs.items():
            with self.subTest(agent=name):
                self.assertGreater(spec.max_turns, 0)
                self.assertLessEqual(spec.max_turns, HARD_MAX_TURNS)

    def test_body_demands_self_contained_conclusion(self) -> None:
        """
        每个角色的正文都必须交代「只有最后一段会回流」。

        这是子 Agent 最容易犯的错：写一段依赖上文的结论（「如上所述」），
        而主对话根本看不到「上文」。
        """
        for name, spec in self.catalog.specs.items():
            with self.subTest(agent=name):
                self.assertIn("自包含", spec.body, f"{name} 的正文要求结论自包含")

    def test_body_says_the_whole_reply_is_returned(self) -> None:
        """
        **正文必须说「整条回复」而不是「最后一段」。**

        真实模型验收实测：正文原本写的是「最后一段是唯一会被带回主对话的东西」，
        而实现取的是**最后那条 assistant 消息的全文**——两者不符。
        模型照着字面理解，在结论前面写了三段过程叙述（还是英文），全都被带了回去。

        这不是措辞偏好问题，是**文案描述错了实现**。
        """
        for name, spec in self.catalog.specs.items():
            with self.subTest(agent=name):
                self.assertIn("整条", spec.body)
                self.assertNotIn("最后一段是唯一", spec.body)

    def test_can_actually_start(self) -> None:
        """AC5：缺省工具集下每个内置角色的最终工具集都非空。"""
        for name, spec in self.catalog.specs.items():
            with self.subTest(agent=name):
                result = resolve_toolset(self.tool_names, spec)
                self.assertFalse(
                    result.is_empty, f"{name} 无法启动：{result.reason}"
                )
                self.assertEqual(
                    result.unresolved, (), f"{name} 的工具名有拼写错误"
                )

    def test_never_gets_the_globally_denied_tools(self) -> None:
        """防无限嵌套：内置角色一个都拿不到委派工具与 Skill 加载工具。"""
        for name, spec in self.catalog.specs.items():
            with self.subTest(agent=name):
                allowed = resolve_toolset(self.tool_names, spec).allowed
                for denied in GLOBAL_DENIED_TOOLS:
                    self.assertNotIn(denied, allowed)


class ToolProfileTest(unittest.TestCase):
    """哪个角色只读、哪个能动手——这件事必须是显式登记的，不能靠读文件才知道。"""

    def setUp(self) -> None:
        self.catalog = _catalog()

    def test_read_only_builtins_declare_only_read_only_tools(self) -> None:
        for name, spec in self.catalog.specs.items():
            if name in FULL_TOOL_BUILTINS:
                continue
            with self.subTest(agent=name):
                self.assertIsNotNone(spec.tools, f"{name} 必须显式声明工具白名单")
                self.assertTrue(
                    set(spec.tools) <= READ_ONLY_TOOLS,
                    f"{name} 只能用只读工具，实际：{spec.tools}",
                )
                self.assertIs(
                    spec.permission_mode, PermissionMode.STRICT,
                    f"{name} 应声明严格档，让 /agents 一眼看出它动不了任何东西",
                )

    def test_full_tool_builtins_inherit(self) -> None:
        """
        `general-purpose` 必须**不声明** `tools`（继承全部），也**不声明**严格档。

        声明严格档会让它的写入在灰色地带一律被拒，那就退化成又一个只读角色，
        与它存在的理由直接矛盾。
        """
        for name in FULL_TOOL_BUILTINS:
            with self.subTest(agent=name):
                spec = self.catalog.specs[name]
                self.assertIsNone(spec.tools, f"{name} 不该声明工具白名单")
                self.assertIsNone(
                    spec.permission_mode,
                    f"{name} 不该声明权限档位——声明严格档会让它退化成只读角色",
                )


class ExplorerTest(unittest.TestCase):
    """explorer 专属：只读调研。"""

    def setUp(self) -> None:
        self.spec = _catalog().specs["explorer"]

    def test_body_tells_it_not_to_attempt_writes(self) -> None:
        """
        正文要明说「不要尝试修改」。

        不说的话它会去试，然后在权限层被自动拒绝——白烧轮次，
        而且拒绝理由对它是噪音。
        """
        self.assertIn("不要尝试修改", self.spec.body)


class PlannerTest(unittest.TestCase):
    """planner 专属：只读方案。"""

    def setUp(self) -> None:
        self.spec = _catalog().specs["planner"]

    def test_body_distinguishes_itself_from_explorer(self) -> None:
        """
        正文必须点破它与调研员的区别。

        两者工具集完全相同（都是那三个只读工具），**唯一的差别就在正文与描述**。
        不写清楚的话，它会退化成第二个 explorer，产出一份事实清单而不是方案。
        """
        self.assertIn("接下来该怎么做", self.spec.body)

    def test_description_is_about_how_to_change(self) -> None:
        self.assertIn("怎么改", self.spec.description)

    def test_body_asks_for_ordering_and_risk(self) -> None:
        """方案的价值在于顺序与风险，只列改动清单不算方案。"""
        self.assertIn("顺序", self.spec.body)
        self.assertIn("风险", self.spec.body)


class GeneralPurposeTest(unittest.TestCase):
    """general-purpose 专属：全工具执行。"""

    def setUp(self) -> None:
        self.spec = _catalog().specs["general-purpose"]

    def test_description_warns_about_default_permission(self) -> None:
        """
        **描述里必须写明缺省档下写不了**。

        这条不是啰嗦：`description` 是主 Agent 选角色的唯一依据。
        不写的话，它会把「修复这个 bug」直接委派过去，然后收回一句
        「我需要授权」——白跑一趟，而用户看到的是一次莫名其妙的往返。
        """
        desc = self.spec.description
        self.assertIn("缺省权限档", desc)
        self.assertIn("自动拒绝", desc)

    def test_body_tells_it_how_to_react_to_denial(self) -> None:
        """
        正文要教它被拒之后怎么办：不重试、改只读方式、写进结论。

        这与 Agent Loop 回灌的那段文案是**两处独立的引导**——
        文案是被拒之后才看到的，正文是它开工前就读到的。
        """
        body = self.spec.body
        self.assertIn("不要原样重试", body)
        self.assertIn("只读", body)

    def test_has_the_largest_turn_budget(self) -> None:
        """
        它做的是多步的活，轮次预算应当是三个里最大的。
        """
        catalog = _catalog()
        others = [
            s.max_turns for n, s in catalog.specs.items() if n != "general-purpose"
        ]
        self.assertGreater(self.spec.max_turns, max(others))


if __name__ == "__main__":
    unittest.main()
