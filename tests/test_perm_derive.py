"""
权限引擎派生的单测（c13 T9，覆盖 AC14b / AC15 / AC15b）。

本文件钉住 c13 spec F16/F17 的两条安全承诺：

- **子 Agent 只能收紧不能放宽**——`narrower_mode` 是它的全部实现；
- **派生实例不得污染主引擎**——这是「后台线程静默改掉主对话权限档位」
  这个故障的反证，它在真机上完全看不出来，只能靠测试钉。
"""

import unittest

from rhinecode.permission.engine import PermissionEngine, narrower_mode
from rhinecode.permission.models import PermissionMode, Rule
from rhinecode.permission.rules import RuleSet


def _rule(pattern: str = "echo *", effect: str = "allow") -> Rule:
    """造一条最简单的规则，内容本身不重要——本文件只关心它在哪个列表里。"""
    return Rule(effect=effect, tool="Bash", pattern=pattern, source="session")


class NarrowerModeTest(unittest.TestCase):
    """`narrower_mode` 的六种组合全覆盖（三档两两组合 + 三种同档）。"""

    def test_pairs(self):
        S = PermissionMode.STRICT
        D = PermissionMode.DEFAULT
        P = PermissionMode.PERMISSIVE
        cases = [
            (S, D, S), (D, S, S),
            (S, P, S), (P, S, S),
            (D, P, D), (P, D, D),
            (S, S, S), (D, D, D), (P, P, P),
        ]
        for a, b, expected in cases:
            with self.subTest(a=a, b=b):
                self.assertIs(narrower_mode(a, b), expected)

    def test_covers_every_mode(self):
        """
        遍历枚举全部取值，确保每一档都能参与比较。

        **这条是给将来新增档位的人准备的**：`_MODE_ORDER` 是一张手写的表，
        新增一档而忘了登记，表现是 `KeyError` 而不是「行为不对」——
        有这条用例，忘记登记当场红。
        """
        for mode in PermissionMode:
            with self.subTest(mode=mode):
                self.assertIs(narrower_mode(mode, mode), mode)


class DeriveIsolationTest(unittest.TestCase):
    """派生实例与主引擎之间哪些共享、哪些独立。"""

    def setUp(self):
        self.main = PermissionEngine(
            RuleSet([_rule()]), mode=PermissionMode.PERMISSIVE
        )

    def test_mode_is_independent(self):
        """派生实例的档位改动**不回流**主引擎（AC14b 的核心反证）。"""
        sub = self.main.derive(PermissionMode.STRICT)

        self.assertIs(sub.mode, PermissionMode.STRICT)
        # 主引擎必须原样不动——它是「后台线程静默改主对话权限」这个故障的判据。
        self.assertIs(self.main.mode, PermissionMode.PERMISSIVE)

    def test_turn_rules_not_inherited(self):
        """
        回合级预授权**不被继承**（spec F17 / AC15）。

        场景还原：主对话里一个 Skill 的 `allowed-tools` 授予了若干放行，
        随后模型在同一次执行里发起委派。子 Agent 不该拿到那批授权——
        它绑在 Skill 的那一次触发上，不该跟着委派跑出去。
        """
        self.main.grant_turn_rules([_rule("Bash(rm *)")])
        self.assertEqual(len(self.main.turn_rules), 1)

        sub = self.main.derive(PermissionMode.DEFAULT)
        self.assertEqual(sub.turn_rules, [])

    def test_turn_rules_are_separate_objects(self):
        """往派生实例的 turn_rules 追加，不影响主引擎（反向隔离）。"""
        sub = self.main.derive(PermissionMode.DEFAULT)
        sub.turn_rules.append(_rule())

        self.assertEqual(self.main.turn_rules, [])

    def test_session_rules_are_shared(self):
        """
        会话级规则**共享同一个列表对象**（AC15b）。

        与 turn_rules 刻意相反：用户在确认面板上选「本会话放行」是**明确授予**，
        理应对子 Agent 生效。两条一起写出来是为了防止后来者把其中一条当 bug 修掉。
        """
        sub = self.main.derive(PermissionMode.DEFAULT)
        # 派生之后主对话又授予了一条——子 Agent 必须能看到（同一个对象，不是快照）。
        self.main.session_rules.append(_rule("Bash(ls *)"))

        self.assertIs(sub.session_rules, self.main.session_rules)
        self.assertEqual(len(sub.session_rules), 1)

    def test_rulesets_are_shared(self):
        """文件级与策略级规则集共享对象——配置在本会话内不变，复制没有意义。"""
        sub = self.main.derive(PermissionMode.DEFAULT)

        self.assertIs(sub.file_ruleset, self.main.file_ruleset)
        self.assertIs(sub.policy_ruleset, self.main.policy_ruleset)
        self.assertIs(sub.load_errors, self.main.load_errors)

    def test_derive_does_not_mutate_source(self):
        """`derive` 本身无副作用：调用前后主引擎的四个可变字段完全不变。"""
        before = (
            self.main.mode,
            list(self.main.turn_rules),
            list(self.main.session_rules),
            list(self.main.load_errors),
        )

        self.main.derive(PermissionMode.STRICT)

        self.assertEqual(
            before,
            (
                self.main.mode,
                list(self.main.turn_rules),
                list(self.main.session_rules),
                list(self.main.load_errors),
            ),
        )


if __name__ == "__main__":
    unittest.main()
