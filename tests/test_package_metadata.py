"""
包元数据与 `__version__` 的护栏（C9）。

## 背景

`rhinecode/__init__.py` 此前是 **0 字节**。空 `__init__.py` 打包上合法，但它让
「这个包是什么、什么版本」在代码里没有任何答案——于是版本号只好在别处再抄
一份，`mcp/client.py` 的 `_CLIENT_VERSION = "0.1.0"` 就是那个副本。它自报给
外部 MCP Server，而**它与 `pyproject.toml` 之间没有任何东西把两者关联起来**：
发版时改了一处忘了另一处不会报错，只是远端日志里记着一个早就不存在的版本号。

本文件钉的就是「那条链没断」。
"""

import ast
import pathlib
import unittest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]


class VersionChainTest(unittest.TestCase):
    """`pyproject.toml` → `rhinecode.__version__` → `mcp/client._CLIENT_VERSION`。"""

    def test_package_exposes_a_version(self) -> None:
        import rhinecode

        self.assertTrue(hasattr(rhinecode, "__version__"))
        self.assertIsInstance(rhinecode.__version__, str)
        self.assertTrue(rhinecode.__version__)

    def test_mcp_client_version_is_not_a_hardcoded_copy(self) -> None:
        """
        ⚠ **本条是这份文件的正主。**

        判据钉的是「`_CLIENT_VERSION` 与 `rhinecode.__version__` 是**同一个值**」，
        而不是「它等于某个字面量」——后者写下的那一刻就又是一份副本了，
        且发版时它会跟着一起过期，于是护栏和被护的东西一起错，谁也发现不了。
        """
        import rhinecode
        from rhinecode.mcp import client

        self.assertEqual(client._CLIENT_VERSION, rhinecode.__version__)

    def test_no_version_literal_remains_in_the_source(self) -> None:
        """
        反证：`mcp/client.py` 的源码里不许再出现版本字面量。

        上一条比的是**运行期的值**，一个写着 `_CLIENT_VERSION = "0.1.0"` 的实现
        在版本恰好还是 0.1.0 时照样能通过它。这条从 AST 上确认那个赋值的右边
        **不是常量**——两条合起来才挡得住「顺手改回字面量」。
        """
        source = (REPO_ROOT / "rhinecode" / "mcp" / "client.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Assign)
                and any(
                    isinstance(t, ast.Name) and t.id == "_CLIENT_VERSION"
                    for t in node.targets
                )
            ):
                self.assertNotIsInstance(
                    node.value,
                    ast.Constant,
                    "_CLIENT_VERSION 又变回硬编码字面量了——它必须引用 "
                    "rhinecode.__version__，唯一事实源是 pyproject.toml",
                )
                break
        else:
            self.fail("没找到 _CLIENT_VERSION 的赋值")


class InitStaysLeafTest(unittest.TestCase):
    """
    ⚠ `rhinecode/__init__.py` **必须保持零第三方依赖、零兄弟包 import**。

    `import rhinecode` 是所有其它 import 的必经之路，在这里拉起任何东西都会
    变成全项目的启动成本，且极易造出包级循环——`tools/__init__.py` 必须保持
    为空正是同一条理由的极端形态（那处的成对维护点写着「`tools ↔ skills` 与
    `tools ↔ mcp` 都是包级互相依赖，不成环唯一依靠这个文件是空的」）。

    而本文件现在有一个真实的下游：`mcp/client.py` 里那句
    `from rhinecode import __version__`。它一旦开始 import 兄弟包，
    那句话就会把半个项目拉起来。
    """

    def test_only_stdlib_imports(self) -> None:
        source = (REPO_ROOT / "rhinecode" / "__init__.py").read_text(encoding="utf-8")
        tree = ast.parse(source)

        offenders = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                offenders += [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                offenders.append(node.module or "")

        # 白名单：只有 importlib.metadata 一项，版本号就是从它读的。
        allowed = {"importlib.metadata"}
        bad = sorted(set(offenders) - allowed)
        self.assertEqual(
            bad,
            [],
            f"rhinecode/__init__.py 多了 import：{bad}——它是所有 import 的必经之路，"
            f"在这里拉起兄弟包会变成全项目的启动成本，且极易造出包级循环",
        )


class ProjectMetadataTest(unittest.TestCase):
    """
    `pyproject.toml` 里那几项面向读者的元数据还在。

    ⚠ 判据刻意**只查字段在不在**、不查内容——内容是产品决定，会随项目演进改；
    而「有人重构 pyproject 时把整段删掉」才是这条要挡的形态。
    """

    def test_reader_facing_fields_present(self) -> None:
        import tomllib

        data = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        project = data["project"]

        for field in ("description", "readme", "authors", "keywords", "classifiers"):
            with self.subTest(field=field):
                self.assertIn(field, project)
                self.assertTrue(project[field], f"{field} 是空的")

        # 装了包的人要能找回项目主页去看文档、提 issue
        self.assertIn("Homepage", data["project"]["urls"])

    def test_classifiers_match_requires_python(self) -> None:
        """
        分类器里写的 Python 版本必须与 `requires-python` 一致。

        多写一个没测过的版本 = 一句没有依据的兼容性承诺。依据是 CI 的六格矩阵
        （windows/ubuntu × 3.11/3.12/3.13），所以这条同时把 pyproject 与 CI
        绑在一起：CI 加一格却忘了这里，或反过来，都会红。
        """
        import re
        import tomllib

        data = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        declared = {
            c.rsplit(":", 1)[1].strip()
            for c in data["project"]["classifiers"]
            if c.startswith("Programming Language :: Python :: 3.")
        }

        ci = (REPO_ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
        in_ci = set(re.findall(r'"(3\.\d+)"', ci))

        self.assertEqual(
            declared,
            in_ci,
            f"pyproject 的分类器写着 {sorted(declared)}，而 CI 跑的是 {sorted(in_ci)}"
            f"——分类器是一句兼容性承诺，它的依据只能是真跑过的那几格",
        )


if __name__ == "__main__":
    unittest.main()
