"""
护栏：宽泛放行规则的识别（c16 F20/F21）。

## 这一层在防什么

`allow` 规则会**短路分类器**（③在④之前）。所以一条写得宽的放行规则等于对
那一类命令关掉整层。最典型的一条：

    allow:
      - Bash(python *)

看起来很窄——用户只是想让它随便跑 python。而 `python -c "<任意程序>"`
能干任何事，包括把 `config.yaml` 的内容发到外部地址。

## ⚠ 两条钉「刻意」的反证

1. **窄规则必须保留**（`Bash(npm test)` / `Bash(python -m unittest*)`）。
   判据是「首段之后**立刻**是通配」——`python -m unittest*` 的 `-m` 已经把
   要跑的模块锁死了，末尾通配扩展不出别的程序。
2. **`Bash(git *)` 必须不在清单内**。它确实有洞
   （`git -c core.pager='<任意命令>' log`），但清单对齐官方那一份、
   **刻意不自行加料**——一张自己扩充的启发式清单会给人虚假的安全感，
   而它永远补不全。这条已登记在 spec 的「已知边界」里，
   **别当成漏改顺手补上**。
"""

import unittest

from rhinecode.classifier.broad import (
    INTERPRETERS,
    PACKAGE_RUNNERS,
    is_broad_command_allow,
    why_broad,
)


class BroadTest(unittest.TestCase):
    """三类判据（AC28）。"""

    def test_whole_tool_allow_is_broad(self) -> None:
        """① 整工具放行：空模式或只有一个通配。"""
        self.assertTrue(is_broad_command_allow("Bash", ""))
        self.assertTrue(is_broad_command_allow("Bash", "*"))

    def test_interpreter_with_wildcard_is_broad(self) -> None:
        """② 解释器 + 通配。遍历常量表，新增项自动被覆盖。"""
        for name in sorted(INTERPRETERS):
            with self.subTest(interpreter=name):
                self.assertTrue(is_broad_command_allow("Bash", f"{name} *"))

    def test_package_runner_with_wildcard_is_broad(self) -> None:
        """③ 包管理器的 run 类命令。遍历常量表。"""
        for head, second in sorted(PACKAGE_RUNNERS):
            pattern = f"{head} {second} *" if second else f"{head} *"
            with self.subTest(pattern=pattern):
                self.assertTrue(is_broad_command_allow("Bash", pattern))

    def test_absolute_path_does_not_evade(self) -> None:
        """
        写绝对路径同样算宽泛。

        不做这一步的话，一条 `allow: Bash(/usr/bin/python *)` 会绕过整张清单
        ——而那不需要谁蓄意为之，写绝对路径是很自然的习惯。
        """
        for token in ("/usr/bin/python", "C:\\Python311\\python.exe", "./node"):
            with self.subTest(token=token):
                self.assertTrue(is_broad_command_allow("Bash", f"{token} *"))


class NarrowTest(unittest.TestCase):
    """⚠ 反证一：窄规则必须**照常保留**（AC29）。"""

    NARROW = [
        "npm test",
        "python -m unittest",
        "python -m unittest*",
        "python -m unittest discover -s tests",
        "pytest",
        "ls",
        "ls -la",
        "cargo build",
        "make build",
        "node scripts/build.js",
    ]

    def test_narrow_rules_are_kept(self) -> None:
        for pattern in self.NARROW:
            with self.subTest(pattern=pattern):
                self.assertFalse(
                    is_broad_command_allow("Bash", pattern),
                    f"{pattern} 是一条窄规则，丢掉它会让用户白配",
                )

    def test_no_trailing_wildcard_is_never_broad(self) -> None:
        """不以通配结尾 = 一条精确的命令，表达不了任意代码。"""
        self.assertFalse(is_broad_command_allow("Bash", "python -c print(1)"))


class DeliberatelyExcludedTest(unittest.TestCase):
    """
    ⚠ 反证二：**`git *` 刻意不在清单内**。

    这条用例存在的唯一目的是把「刻意」钉住。看到它红了，先去读
    `classifier/broad.py` 顶上那段说明，再决定要不要真的加。
    """

    def test_git_wildcard_is_not_treated_as_broad(self) -> None:
        self.assertFalse(
            is_broad_command_allow("Bash", "git *"),
            "git * 刻意不在宽泛清单内（对齐官方清单，不自行加料）——"
            "见 classifier/broad.py 顶部说明与 spec 已知边界",
        )

    def test_other_common_tools_are_not_treated_as_broad(self) -> None:
        """同理：docker / kubectl / ssh 都不在清单内。"""
        for pattern in ("docker *", "kubectl *", "ssh *", "curl *"):
            with self.subTest(pattern=pattern):
                self.assertFalse(is_broad_command_allow("Bash", pattern))


class NonCommandTest(unittest.TestCase):
    """AC32：只管命令类，别的一律不动。"""

    def test_non_command_rule_names_are_ignored(self) -> None:
        for tool in ("Read", "Write", "Edit", "WebFetch", "send_message", "run_agent"):
            with self.subTest(tool=tool):
                self.assertFalse(is_broad_command_allow(tool, "*"))

    def test_domain_rules_are_never_broad(self) -> None:
        """
        F22：域名规则**不被丢弃**。

        它同时承担「建立白名单」的语义（②′层据此判断白名单是否已建立），
        丢弃会连带改变**另一层**的行为——那不在本章范围内。
        """
        self.assertFalse(is_broad_command_allow("WebFetch", "domain:*"))


class WhyBroadTest(unittest.TestCase):
    """AC31：给用户的原因必须**具体到这一条**。"""

    def test_reason_is_specific_not_generic(self) -> None:
        """
        用户要据此决定「改窄」还是「关掉分类器」，
        而那两个决定需要他知道**这条规则实际上能表达什么**。
        与 `permission/protected.py` 的 `_WHY` 是同一条理由。
        """
        reason = why_broad("Bash", "python *")
        self.assertIn("python", reason)
        self.assertIn("任意代码", reason)

    def test_package_runner_reason_mentions_config_file(self) -> None:
        reason = why_broad("Bash", "npm run *")
        self.assertIn("npm", reason)
        self.assertIn("配置文件", reason)

    def test_narrow_rule_has_no_reason(self) -> None:
        self.assertEqual(why_broad("Bash", "npm test"), "")


class LeafPackageTest(unittest.TestCase):
    """
    结构护栏：本模块**不 import `permission`**。

    入参收成两个字符串正是为此——`import permission.models` 会连带执行
    `permission/__init__.py`，把引擎与 `rhinecode.tools` 一起拉起来，
    分类器包就不再是叶子了（spec N5）。
    """

    def test_no_permission_import(self) -> None:
        """
        ⚠ 只看 **import 语句**，不看整段源码。

        模块 docstring 里会（而且应该）提到 `rhinecode.permission`——那正是
        「为什么不 import 它」的说明。按全文搜索的话这条用例会因为一段
        正确的注释而红，于是下一个人的第一反应是把注释删掉。
        """
        import ast
        import inspect

        from rhinecode.classifier import broad

        tree = ast.parse(inspect.getsource(broad))
        imported: list[str] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported += [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                imported.append(node.module or "")
        for name in imported:
            for banned in ("permission", "agent", "tools", "tui", "conversation"):
                self.assertNotIn(
                    banned, name, f"分类器是叶子包，不该 import {name}"
                )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
