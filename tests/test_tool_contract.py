"""
工具层的两条**结构契约**护栏：`Tool.execute` 的签名 ↔ 标志位一致性（C3），
以及 `tools/__init__.py` 必须保持为空（C4）。

两条都是 `CLAUDE.md` 架构表里标了 ⚠ 的致命不变量，此前**一条测试都没有**。
它们的共同失败形态是本项目反复吃过的那种：**漏改一律不报错**——编译过、
全量测试绿、界面正常，只在某个特定路径真的被走到时才炸，或者干脆永远不炸、
只是某个行为悄悄不生效了。

## 为什么现有护栏盖不住这两条

- `tests/test_loop_cwd_dispatch.py` 的 7 条用**替身工具**验的是「循环的分发
  逻辑对不对」——替身是照着契约写的，所以它证明不了**真实工具**的签名与标志
  是否对得上。分发逻辑正确 + 某个真实工具签名不对 = 那 7 条照样全绿。
- `tests/test_todo_tool.py::PlanStageContractTest` 只盯 `todo_write` 一个工具。
  全仓 20 个 `Tool` 实现里，此前只有它做过 `inspect.signature` 检查。

写法参照 `tests/test_classifier_broad.py::LeafPackageTest` 与
`tests/test_trace_reader.py::LayerNamesConsistencyTest`——都是「用 AST 断言一条
结构约定」，而不是端到端跑一遍。**测试代码不受叶子包 / 空 `__init__` 这类约束**，
它 import 谁都行、解析谁都行。
"""

import ast
import importlib
import inspect
import textwrap
import unittest
from pathlib import Path

import rhinecode
from rhinecode.tools.base import Tool
from rhinecode.tools.registry import ToolRegistry

# 包根目录（`rhinecode/`），两组用例都从这里出发做静态扫描。
_PACKAGE_ROOT = Path(rhinecode.__file__).resolve().parent


def _discover_tool_classes() -> dict[str, type]:
    """
    找出全仓所有**可实例化**的 `Tool` 实现类。

    执行流程：
    1. 用 AST 扫 `rhinecode/**/*.py`，挑出「定义了某个基类名以 `Tool` 结尾的类」
       的模块——这一步纯静态，不导入任何东西。
    2. 只 import 上一步命中的那十几个模块，再用 `issubclass` 做真判定。
    3. 丢掉抽象基类（`inspect.isabstract`），例如 `tools/team_tasks.py` 的
       `_BoardTool`——它不实现 `execute`，也从不被注册。

    :returns: {类的限定名: 类对象}
    :raises ImportError: 某个命中模块导入失败时——那本身就是要暴露的问题

    副作用：会 import 若干产品模块（工具层模块均无导入期副作用）。

    ## 为什么不直接遍历 `ToolRegistry.default()`

    那个注册中心里**只有 7 个工具**（六个核心工具 + `mcp_resolve_server`）。
    另外 13 个由装配层按条件注册：`todo_write` / `ask_user` / `present_plan` /
    `web_fetch` / `web_search` / `run_agent` / `load_skill` / `mcp_add_server` /
    四个 `task_*` / `send_message`。

    ⚠ **而 `plan_safe=True` 的 6 个工具一个都不在 `default()` 里。**
    只遍历 `default()` 的话，本文件想钉的两条契约里有一整条
    （`plan_safe` ↔ `plan_stage`）**永远不会被求值**——测试全绿，但那一半从来
    没验过。这正是「反证测试必须先证明自己不是空跑」那类坑，
    故另有 `ToolDiscoveryTest` 专门证明本函数没在空转。

    ## 为什么用 AST 扫描而不是写死一份模块清单

    写死清单本身就会变成一个新的成对维护点：新增一个工具模块却忘了往清单里补
    一行，本文件就静默地不再覆盖它——又是一次「漏改不报错」。扫描是自动发现的，
    新工具落盘即被纳入。

    `mcp/tool_adapter.py` 的 `MCPTool` **刻意不排除**：远端工具虽是运行期动态
    注册的，但它们全都是这**同一个类**的实例，签名契约因此静态可查；反过来说，
    排除它等于把「唯一一个能凭远端 schema 凭空出现的工具类」放在护栏之外。
    """
    modules: list[str] = []
    for path in sorted(_PACKAGE_ROOT.rglob("*.py")):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (OSError, SyntaxError):  # pragma: no cover —— 正常仓库不该走到
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.ClassDef):
                continue
            # 基类名可能写成 `Tool`、`_BoardTool`，也可能是 `base.Tool` 这种带
            # 前缀的形态，故只看 unparse 之后的文本尾巴。宁可多导入一个模块，
            # 也不要因为写法不同而漏掉一个工具。
            if any(ast.unparse(b).endswith("Tool") for b in node.bases):
                rel = path.relative_to(_PACKAGE_ROOT.parent).with_suffix("")
                modules.append(".".join(rel.parts))
                break

    found: dict[str, type] = {}
    for name in modules:
        module = importlib.import_module(name)
        for obj in vars(module).values():
            if (
                isinstance(obj, type)
                and issubclass(obj, Tool)
                and obj is not Tool
                # 只认「在本模块里定义」的，避免 import 进来的类被重复计数
                and obj.__module__ == name
                and not inspect.isabstract(obj)
            ):
                found[obj.__qualname__] = obj
    return found


def _execute_params(tool_cls: type) -> "dict[str, inspect.Parameter]":
    """取 `tool_cls.execute` 的形参表（含 `self`）。"""
    return dict(inspect.signature(tool_cls.execute).parameters)


def _accepts_keyword(tool_cls: type, keyword: str) -> bool:
    """
    `tool_cls.execute` 能不能以**关键字**形式收下 `keyword`。

    :param tool_cls: 待检查的工具类（用类而非实例，避免构造各工具的依赖）
    :param keyword: `"cwd"` 或 `"plan_stage"`
    :returns: 具名形参存在且可按关键字传入时为 True

    ⚠ **`**kwargs` 刻意不算数。** 调用方 `agent/loop.py` 传的是
    `tool.execute(args, cwd=cwd)`，一个 `**kwargs` 确实能让它不抛 `TypeError`
    ——但参数会被静默吞掉，工具照样在错误的工作目录里干活。那正是 c14 最忌讳的
    形态：**看起来接住了，实际上没生效**。所以这里要求一个**具名**形参。
    """
    param = _execute_params(tool_cls).get(keyword)
    return param is not None and param.kind in (
        inspect.Parameter.POSITIONAL_OR_KEYWORD,
        inspect.Parameter.KEYWORD_ONLY,
    )


def _body_references(tool_cls: type, name: str) -> bool:
    """
    `tool_cls.execute` 的函数体里有没有**真的读取**过 `name` 这个形参。

    :returns: AST 里出现过同名 `Name` 节点时为 True

    ⚠ **只看 AST 里的 `Name` 节点，不做全文搜索**——理由与
    `tests/test_classifier_broad.py::LeafPackageTest` 那条完全相同：docstring
    里会（而且应该）大段解释「这个参数为什么留着不用」，按字符串搜的话，一段
    **正确的注释**会让用例变红，于是下一个人的第一反应是把注释删掉。

    形参声明本身在 AST 里是 `ast.arg` 而不是 `ast.Name`，因此不会误命中。
    """
    tree = ast.parse(textwrap.dedent(inspect.getsource(tool_cls.execute)))
    return any(
        isinstance(node, ast.Name) and node.id == name for node in ast.walk(tree)
    )


class ToolDiscoveryTest(unittest.TestCase):
    """
    先证明**下面那些断言不是空跑的**。

    结构护栏最典型的失效方式不是断错，是**一条都没断到**：扫描逻辑坏掉、返回
    空集合，于是 `for` 循环一次都不进，测试绿得很漂亮。
    这个教训记在「安全反证的取样陷阱」里——0 条判定看起来像通过，其实什么都没验。
    """

    def setUp(self) -> None:
        self.classes = _discover_tool_classes()

    def test_covers_every_tool_in_the_default_registry(self) -> None:
        """默认注册中心里的每一个工具，其实现类都必须被扫描发现。"""
        registry = ToolRegistry.default()
        for tool_name in sorted(registry.names()):
            tool = registry.get(tool_name)
            self.assertIsNotNone(tool)
            self.assertIn(
                type(tool).__qualname__,
                self.classes,
                f"{tool_name} 的实现类没被扫到——发现逻辑出问题了",
            )

    def test_both_flags_have_live_subjects(self) -> None:
        """
        两个标志各自至少有一个真实工具在用。

        少了任何一边，对应那条契约用例就退化成空循环——而空循环是绿的。
        """
        self.assertTrue(
            [c for c in self.classes.values() if c.workspace_aware],
            "一个 workspace_aware 工具都没扫到，cwd 契约用例是空跑",
        )
        self.assertTrue(
            [c for c in self.classes.values() if c.plan_safe],
            "一个 plan_safe 工具都没扫到，plan_stage 契约用例是空跑",
        )

    def test_discovery_floor(self) -> None:
        """
        下限只防「发现逻辑整体失灵」，**不是**工具数量的登记表。

        写这一版时是 20 个。新增或删除工具都不必来改这个数字，它掉到 15 以下
        才说明扫描本身坏了。刻意不写成等值断言——那会让每加一个工具就多一处
        「漏改就红」的机械维护点，而这一条的职责只是「别在空转」。
        """
        self.assertGreaterEqual(len(self.classes), 15, sorted(self.classes))


class ExecuteSignatureContractTest(unittest.TestCase):
    """
    ⚠ **`Tool.execute` 的签名不是「基类里那一个」，而是三种。**

    基类 `tools/base.py` 声明的是 `execute(self, args: dict)`，既没有 `cwd`
    也没有 `plan_stage`。20 个实现分裂成三种签名，靠 `workspace_aware` /
    `plan_safe` 两个**类属性**告诉调用方该传什么——`agent/loop.py` 的串行路径
    与并发路径都按这两个布尔分支（两个标志**各自独立判断**，一个工具完全可能
    两个都声明）。

    契约是双向的：

    | 声明 | 必须 | 违反后果 |
    | --- | --- | --- |
    | `workspace_aware = True` | `execute` 接受 `cwd` | 运行期 `TypeError` |
    | `plan_safe = True` | `execute` 接受 `plan_stage` | 运行期 `TypeError` |
    | 形参存在但没声明标志 | 该形参**不被使用** | 静默失效（见下） |

    ## 为什么必须钉住它

    新工具声明了 `workspace_aware = True` 却忘了给 `execute` 加 `cwd=None`：
    编译过、全量测试绿，**只在该工具真被调用时**抛 `TypeError`。而两条执行路径
    的表现还不一样——串行路径有 `except Exception` 兜成一句「工具执行异常」回灌
    给模型（用户看到的是模型在莫名其妙地重试、改参数、换工具），并发路径刻意
    不加 try/except（异常经 `future.result()` 才被兜住）。两种表现都离真正的
    原因很远，而根因只是签名少了一个形参。

    ## 反向为什么写成「形参不被使用」而不是「形参不许存在」

    循环**只在标志为真时**才传那个参数。所以一个有 `plan_stage` 形参、却忘了写
    `plan_safe = True` 的工具，参数恒为缺省值——它写在 `if plan_stage: ...` 里
    的那段自我约束**一次都不会执行**，而且不报任何错。这才是真正要拦的形态，
    也正是本项目定义的那种「漏改一律不报错」。

    「形参不许存在」这个更严的写法则会**误伤一处刻意为之**：`todo_write` 的
    `plan_safe` 在 2026-08-18 由 True 改成 False，但签名里的 `plan_stage` 被
    **刻意保留**（理由写在该文件 `execute` 的 docstring 里）——留着零成本，
    删掉的话将来若把标志改回 True，循环传进来就直接 `TypeError`，而改标志的人
    不会想到还要改签名。把它当成漏改「修」掉，等于亲手埋下那个雷。
    另见 `tests/test_todo_tool.py::PlanStageContractTest`，那边钉的是同一件事
    的行为侧（`plan_stage` 传与不传行为相同）。
    """

    def setUp(self) -> None:
        self.classes = _discover_tool_classes()

    def test_workspace_aware_tools_accept_cwd(self) -> None:
        for name, cls in sorted(self.classes.items()):
            if not cls.workspace_aware:
                continue
            with self.subTest(tool=name):
                self.assertTrue(
                    _accepts_keyword(cls, "cwd"),
                    f"{name} 声明了 workspace_aware，但 execute 收不下 cwd "
                    f"关键字参数——它一被调用就会抛 TypeError（c14 的 cwd 分发契约）",
                )

    def test_plan_safe_tools_accept_plan_stage(self) -> None:
        for name, cls in sorted(self.classes.items()):
            if not cls.plan_safe:
                continue
            with self.subTest(tool=name):
                self.assertTrue(
                    _accepts_keyword(cls, "plan_stage"),
                    f"{name} 声明了 plan_safe，但 execute 收不下 plan_stage "
                    f"关键字参数——它在 Plan Mode 规划阶段一被调用就会抛 TypeError",
                )

    def test_undeclared_cwd_param_is_never_read(self) -> None:
        """没声明 `workspace_aware` 却收 `cwd` 的工具，不许真去读它。"""
        for name, cls in sorted(self.classes.items()):
            if cls.workspace_aware or "cwd" not in _execute_params(cls):
                continue
            with self.subTest(tool=name):
                self.assertFalse(
                    _body_references(cls, "cwd"),
                    f"{name} 用到了 cwd，却没声明 workspace_aware——循环不会传它，"
                    f"那段逻辑恒等于「cwd 为 None」，表现是工具悄悄落到进程当前"
                    f"目录而不是调用者的工作目录，隔离子 Agent 会读写错文件",
                )

    def test_undeclared_plan_stage_param_is_never_read(self) -> None:
        """没声明 `plan_safe` 却收 `plan_stage` 的工具，不许真去读它。"""
        for name, cls in sorted(self.classes.items()):
            if cls.plan_safe or "plan_stage" not in _execute_params(cls):
                continue
            with self.subTest(tool=name):
                self.assertFalse(
                    _body_references(cls, "plan_stage"),
                    f"{name} 用到了 plan_stage，却没声明 plan_safe——循环不会传它，"
                    f"那段自我约束一次都不会执行，且不报任何错",
                )

    def test_optional_params_have_defaults(self) -> None:
        """
        `cwd` / `plan_stage` 必须带缺省值。

        调用方按两个标志分四支，其中「都为假」那支调的是 `tool.execute(args)`
        ——形参没缺省值的话那一支直接 `TypeError`。更常见的是单元测试与工具的
        直接调用大多只传 `args`，缺省值缺失会让它们成片变红，而报错信息指向
        调用处、不指向签名。
        """
        for name, cls in sorted(self.classes.items()):
            params = _execute_params(cls)
            for keyword in ("cwd", "plan_stage"):
                if keyword not in params:
                    continue
                with self.subTest(tool=name, keyword=keyword):
                    self.assertIsNot(
                        params[keyword].default,
                        inspect.Parameter.empty,
                        f"{name}.execute 的 {keyword} 缺省值缺失",
                    )


class ToolsPackageInitIsEmptyTest(unittest.TestCase):
    """
    ⚠ 结构护栏：`rhinecode/tools/__init__.py` **不许 import 任何东西**。

    ## 这条约定在挡什么

    `tools` 与几个包之间是**货真价实的互依**（该文件的 docstring 逐条列了）：

        tools.base       ← mcp.tool_adapter      （mcp 依赖 tools）
        tools.mcp_config → mcp.auto_config       （tools 依赖 mcp）
        tools.registry   ← subagents.runner      （subagents 依赖 tools）
        tools.run_agent  → subagents.service     （tools 依赖 subagents）

    这两组互依**之所以不成环，唯一的依靠就是本文件是空的**。Python 导入
    `rhinecode.tools.mcp_config` 时会先执行 `tools/__init__.py`；它是空的，
    `tools` 包立刻初始化完成，后续加载子模块时不会回头去碰 `mcp`。

    图方便写一行 `from rhinecode.tools.registry import ToolRegistry`，后果是：
    `mcp.tool_adapter` 导入 `tools.base` → 触发本文件 → 它去导入
    `tools.registry`（或任何最终牵扯到 `tools.mcp_config` 的模块）→ 后者又要
    导入 `mcp.auto_config` → 而 `mcp` 此刻正处在半初始化状态 →
    `ImportError: cannot import name ...`，**报错位置离真正的原因很远**。

    ## 为什么这条不能简化成「跑一遍全量测试就知道了」

    成环与否取决于**谁先被导入**。同一份代码，从 `rhinecode.tools.base` 进去
    不炸、从 `rhinecode.mcp.tool_adapter` 进去就炸；而测试的导入顺序、产品装配
    的导入顺序、`python -c` 手测的导入顺序三者都不同。「本机跑绿了」证明不了
    这条不变量成立，只证明这一次的顺序恰好没踩上。所以要在**结构上**断言，
    而不是靠现象——这与本项目其它几条「靠一条约定支撑的架构不变量」同一手法。

    同类约束在别的包上都有护栏——`permission/__init__.py` 的在
    `tests/test_classifier_broad.py::LeafPackageTest`、`trace/__init__.py` 的在
    `tests/test_trace_reader.py::LayerNamesConsistencyTest`，唯独 `tools` 此前
    一条都没有。
    """

    def _init_source(self) -> str:
        return (_PACKAGE_ROOT / "tools" / "__init__.py").read_text(encoding="utf-8")

    def test_init_has_no_import_statements(self) -> None:
        """
        ⚠ **只断言 AST 里没有 `Import` / `ImportFrom` 节点，不做全文搜索。**

        那 2799 字节的 docstring 里写满了 `from rhinecode.tools.registry import
        ToolRegistry` 这样的**反面示例**——它们正是「为什么不能这么写」的说明。
        按字符串搜的话这条用例会因为一段正确的注释而红，于是下一个人的第一
        反应是把注释删掉，而那段注释是本约定唯一的现场说明。同样的取舍见
        `test_classifier_broad.py` 的 `test_no_permission_import`。
        """
        offenders = [
            ast.unparse(node)
            for node in ast.walk(ast.parse(self._init_source()))
            if isinstance(node, (ast.Import, ast.ImportFrom))
        ]
        self.assertEqual(
            offenders,
            [],
            "rhinecode/tools/__init__.py 必须保持为空（只留 docstring）——"
            "它是 tools ↔ mcp、tools ↔ subagents 两组互依不成环的唯一依靠，"
            "详见该文件自身的 docstring",
        )

    def test_module_body_is_docstring_only(self) -> None:
        """
        再收一格：整个模块体**只能有一条 docstring 表达式**。

        光挡 import 挡不住 `ToolRegistry = _lazy()` 这类写法——任何一句在包
        `__init__` 里求值的语句都可能间接触发子模块加载（函数调用、装饰器、
        赋值右侧的表达式都算）。这条把范围从「不许 import」收成「不许有可执行
        语句」，覆盖住那些绕开 import 语句、却同样会把 `tools` 包的初始化拖长
        的写法。
        """
        body = ast.parse(self._init_source()).body
        self.assertEqual(
            len(body), 1, "模块体除 docstring 外还有别的语句——它必须保持为空"
        )
        self.assertIsInstance(body[0], ast.Expr)
        self.assertIsInstance(body[0].value, ast.Constant)
        self.assertIsInstance(body[0].value.value, str)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
