"""
`[tool.setuptools.package-data]` 的覆盖护栏（A5 / A6）。

## 这条护栏钉的是什么

`rhinecode/` 下**每一个非 `.py` 文件**都必须被 `pyproject.toml` 的
`[tool.setuptools.package-data]` 里某一条模式覆盖，否则它**不会被打进 wheel
与 sdist**。

## 违反会发生什么

**什么都不会发生——这正是它需要一条护栏的原因。**

真实发生过一次（A5 / R5-1）：package-data 只写了 `"rhinecode.skills"`，
漏了 `"rhinecode.subagents"`，于是 `pip install .`（注意不是 `-e`）出来的
副本里 `rhinecode/subagents/builtin/` 一个文件都没有，`explorer` /
`planner` / `general-purpose` 三个内置角色全丢，C13 的「定义式委派」整个失效。
而全链路上**没有任何一处会报错**：

- 角色的分层扫描对缺失的层是**设计上的静默跳过**（那个容错本是为
  「用户没建 `.rhinecode/agents/`」写的），扫描结果是「0 个角色、**0 个错误**」；
- 无角色时 `render_agent_index` 刻意返回**空串**（给模型看一个
  「你有委派能力但一个角色都没有」的清单会让它反复试探），于是系统提示里
  那一段整个消失，模型看不出少了什么；
- 唯一会说出真相的是模型猜错角色名时的回灌文案——它只在猜错时才出现。

⚠ **这类问题比「版本号写错」难发现一个量级**：后者会当场
`ModuleNotFoundError`，本条什么都不会说，用户只是觉得「这个功能好像没有」。

## 为什么本项目已有的三千多条测试看不见它

**它们全部跑在源码树上，而源码树里那些文件一直都在。** 打包这一步是
源码树与用户拿到的那份副本之间**唯一**的差异来源，没有任何一条跑在源码树上
的测试能覆盖它。这与 C3（`Tool.execute` 签名契约）/ C4（`tools/__init__.py`
必须为空）同型：共同点是**漏改一律不报错**——编译过、测试绿、界面正常，
只是某个行为悄悄不对了。

## 为什么不能简化

1. **不能改成「断言 package-data 里有 `rhinecode.subagents` 这一条」。**
   那只钉住了已经踩过的那一个坑，下一个新增随附资源的包（比如将来
   `rhinecode/hooks/builtin/`）照样静默漏掉。判据必须是**从文件反查模式**，
   即「实际存在的每一个文件都有人管」，而不是「某条模式在不在」。

2. **不能用 `fnmatch(rel_path, pattern)` 直接比。** `fnmatch` 的 `*`
   **跨目录分隔符**，而 setuptools 的 glob **不跨**——用它会让
   `builtin/*.md` 看起来覆盖了 `builtin/skill-creator/SKILL.md`，
   于是一份**真的不进包**的文件在护栏眼里是安全的。护栏比被护的东西更宽松
   等于没有护栏，因此本文件自带一条反证（`PatternMatchingTest`）钉住这个语义。

3. **不能只看「文件在某个 package-data 键的目录下」就算覆盖。** setuptools
   把文件算给**直接包含它的那个包**，而不是任意祖先包。所以本文件先按
   「最近的、带 `__init__.py` 的祖先目录」定位归属包，再用该包的模式去比。
"""

import fnmatch
import pathlib
import tomllib
import unittest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
PACKAGE_ROOT = REPO_ROOT / "rhinecode"

# ---------------------------------------------------------------------------
# 排除清单
# ---------------------------------------------------------------------------
#
# ⚠ **这份清单不是「让测试变绿的地方」。** 往里加一项，等于宣布「这个文件
# 刻意不进分发包」，而那是一个产品决定，必须写清楚理由——A5 的教训正是
# 「一个该进包的文件不在包里，而没有任何人会发现」。
#
# 目前只有构建产物，**一条「刻意不进包的源文件」都没有**：
#
# - `__pycache__/`：CPython 的字节码缓存目录，跑一次测试就会长出来。
#   它不是源码树的一部分，`packages.find` 也不会把它当包（没有 __init__.py）。
# - `*.pyc` / `*.pyo`：同上，字节码本身。单列一条是因为它们也可能落在
#   `__pycache__` 之外（老式布局或手工产物）。
#
# 将来如果真出现一个「刻意不该进包」的非 .py 源文件（例如只在开发期用的
# 大体积夹具），加进来的同时必须在这里写明**它为什么不该进包**，
# 让下一个人看得懂这不是随手加的。
EXCLUDED_DIR_NAMES = frozenset({"__pycache__"})
EXCLUDED_FILE_PATTERNS = ("*.pyc", "*.pyo")


def _matches_setuptools_glob(rel_path: str, pattern: str) -> bool:
    """
    按 **setuptools 的 glob 语义**判断相对路径是否命中模式。

    与 `fnmatch.fnmatch(rel_path, pattern)` 的关键差别：setuptools 的 `*`
    **不跨目录分隔符**。因此本函数把两边都按 `/` 拆成段，要求**段数相同**
    且**逐段** `fnmatch`——`builtin/*.md` 于是只覆盖 `builtin/commit.md`，
    覆盖不到 `builtin/skill-creator/SKILL.md`（后者要靠第二条
    `builtin/*/*.md`）。

    :param rel_path: 文件相对**归属包目录**的路径，一律用 `/` 分隔。
    :param pattern: package-data 里的一条模式，同样用 `/` 分隔。
    :returns: 命中为 True。

    副作用：无（纯函数）。

    ⚠ 这里刻意**不支持 `**` 递归通配**：pyproject 的注释里写明了不用它
    （对 setuptools 版本有要求，且内置样板不会嵌套超过两层）。护栏支持一个
    配置里禁止使用的语法，只会让某天有人写下 `**` 时护栏先点头。
    """
    path_parts = rel_path.split("/")
    pattern_parts = pattern.split("/")
    if len(path_parts) != len(pattern_parts):
        return False
    return all(
        fnmatch.fnmatch(seg, pat) for seg, pat in zip(path_parts, pattern_parts)
    )


def _owning_package(file_path: pathlib.Path) -> str | None:
    """
    找出一个文件的**归属包**（setuptools 把 package-data 算给它）。

    做法：从文件所在目录逐级往上走，第一个含 `__init__.py` 的目录就是归属包
    ——这与 `packages.find` 的发现规则一致（它按 `__init__.py` 认包）。
    走到 `rhinecode/` 之上仍没找到则返回 None（不该发生，说明有文件落在了
    包体系之外）。

    :param file_path: `rhinecode/` 下某个文件的绝对路径。
    :returns: 形如 `"rhinecode.skills"` 的包名，找不到时 None。

    副作用：只读文件系统。
    """
    current = file_path.parent
    while True:
        if (current / "__init__.py").exists():
            rel = current.relative_to(REPO_ROOT)
            return ".".join(rel.parts)
        if current == PACKAGE_ROOT or current == REPO_ROOT:
            return None
        current = current.parent


def _iter_shipped_non_py_files() -> list[pathlib.Path]:
    """
    列出 `rhinecode/` 下全部**应当随包分发**的非 `.py` 文件（已剔除构建产物）。

    :returns: 绝对路径列表，按路径排序（保证失败信息稳定、可 diff）。

    副作用：只读文件系统。
    """
    found: list[pathlib.Path] = []
    for path in PACKAGE_ROOT.rglob("*"):
        if not path.is_file() or path.suffix == ".py":
            continue
        if EXCLUDED_DIR_NAMES & set(path.relative_to(PACKAGE_ROOT).parts[:-1]):
            continue
        if any(fnmatch.fnmatch(path.name, p) for p in EXCLUDED_FILE_PATTERNS):
            continue
        found.append(path)
    return sorted(found)


def _package_data() -> dict[str, list[str]]:
    """读 `pyproject.toml` 里的 `[tool.setuptools.package-data]`。"""
    data = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    return data["tool"]["setuptools"]["package-data"]


class PackageDataCoverageTest(unittest.TestCase):
    """⚠ 本类是这份文件的正主：每个随附资源都得有人管。"""

    def test_every_non_py_file_is_covered(self) -> None:
        """
        `rhinecode/` 下每一个非 `.py` 文件都被某条 package-data 模式覆盖。

        失败信息刻意列出**具体是哪些文件**以及**当前有哪些模式**——这条红了
        的时候，人多半刚刚往包里放了一个新的随附资源，他需要的是
        「去 pyproject 加哪条模式」，而不是「有东西没覆盖」。
        """
        patterns = _package_data()
        files = _iter_shipped_non_py_files()

        # 先确认取样非空：若哪天 rglob 的写法被改坏、一个文件都没扫到，
        # 下面的断言会「零条全过」——那是本项目登记过的取样陷阱形态
        # （0 条判定看起来像通过，其实什么都没验到）。
        self.assertTrue(
            files,
            "一个非 .py 文件都没扫到——rhinecode/ 下至少有内置 Skill 与内置角色的 "
            ".md，这说明取样逻辑坏了，而不是覆盖率完美",
        )

        uncovered: list[str] = []
        for path in files:
            package = _owning_package(path)
            rel_to_repo = path.relative_to(REPO_ROOT).as_posix()
            if package is None:
                uncovered.append(f"{rel_to_repo}（找不到归属包）")
                continue
            package_dir = REPO_ROOT / pathlib.Path(*package.split("."))
            rel = path.relative_to(package_dir).as_posix()
            if not any(
                _matches_setuptools_glob(rel, pat)
                for pat in patterns.get(package, [])
            ):
                uncovered.append(f"{rel_to_repo}（归属包 {package}，相对路径 {rel}）")

        self.assertEqual(
            uncovered,
            [],
            "下列文件不会被打进 wheel / sdist——它们在源码树上一直都在，"
            "所以其余测试全部照常通过，用户拿到的副本里却没有它们：\n  "
            + "\n  ".join(uncovered)
            + "\n当前的 package-data 模式："
            + repr(patterns)
            + "\n修法：去 pyproject.toml 的 [tool.setuptools.package-data] "
            "给对应的包补一条模式（注意 * 不跨目录分隔符，目录型资源要单独一条）",
        )

    def test_every_declared_package_exists(self) -> None:
        """
        反方向：package-data 里写的每个包名都得是真实存在的包。

        写错包名（比如 `"rhinecode.subagent"` 少个 s）**不会报错**，
        setuptools 只是安静地不匹配任何文件——症状与压根没写这一条完全相同。
        """
        for package in _package_data():
            with self.subTest(package=package):
                package_dir = REPO_ROOT / pathlib.Path(*package.split("."))
                self.assertTrue(
                    (package_dir / "__init__.py").exists(),
                    f"package-data 里写着 {package}，但 {package_dir} 不是一个包"
                    f"——写错包名不报错，只是安静地不匹配任何文件",
                )


class PatternMatchingTest(unittest.TestCase):
    """
    反证：`_matches_setuptools_glob` 的语义不能退化成 `fnmatch`。

    ⚠ **这一组是上面那条护栏的护栏。** 如果匹配函数的 `*` 跨了目录分隔符，
    上面那条测试会对一份**真的不进包**的文件点头——护栏比被护的东西更宽松
    就等于没有护栏。真实教训见 pyproject 里那段注释：`builtin/*.md` 覆盖不到
    `builtin/skill-creator/SKILL.md`，所以那里才要写第二条 `builtin/*/*.md`。
    """

    def test_star_does_not_cross_directory_separator(self) -> None:
        self.assertTrue(_matches_setuptools_glob("builtin/commit.md", "builtin/*.md"))
        self.assertFalse(
            _matches_setuptools_glob(
                "builtin/skill-creator/SKILL.md", "builtin/*.md"
            ),
            "`*` 跨了目录分隔符——那是 fnmatch 的语义，不是 setuptools 的",
        )
        self.assertTrue(
            _matches_setuptools_glob(
                "builtin/skill-creator/SKILL.md", "builtin/*/*.md"
            )
        )

    def test_segment_count_must_match(self) -> None:
        """段数不同一律不命中：模式比路径浅或深都不算覆盖。"""
        self.assertFalse(_matches_setuptools_glob("commit.md", "builtin/*.md"))
        self.assertFalse(
            _matches_setuptools_glob("builtin/commit.md", "builtin/*/*.md")
        )


if __name__ == "__main__":
    unittest.main()
