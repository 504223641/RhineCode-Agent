"""
RHINE.md 的坏编码护栏（审查报告 B6 / R4-1）。

## 这条护栏钉的是什么

`UnicodeDecodeError` 是 `ValueError` 的子类、**不是 `OSError`**。修复之前
`memory/instructions.py` 读 RHINE.md 时只 `except OSError`，于是一个存成 GBK 的
RHINE.md（中文 Windows 上记事本、旧编辑器、`cmd` 的 `>` 重定向默认都写 GBK/ANSI，
**用户不需要做错任何事**）会让异常**穿过**那层逐层容错，被
`MemoryManager.startup` 的 catch-all 接住，`self._instructions` 被整个换成
`LoadedInstructions(text="", layers=[])`。

两条后果，第二条才是要害：

1. `text=""` → 系统提示里的「自定义指令」槽位整个消失（`prompt/builder.py` 跳过
   空模块）——**连同另外两层已经读好的内容一起没了**；
2. `layers=[]` → `/memory` 报告里那个负责说真话的循环一次都不执行，
   「RHINE.md 项目指令：」后面**一片空白**：既不说加载成功，也不说加载失败。

## 为什么必须是「三组对照」而不是单点断言

R4-1 的实跑结论是：三组输入只差一个文件的编码，「三层全缺」列出三行「未找到」、
「`@include` 越界」列出三行并带一条「警告」，**只有「坏编码」这一组什么都不显示**
——而它恰恰是唯一「用户的指令确实存在、却没生效」的那一组。

因此本模块的核心判据（`ThreeGroupsTest`）不是「坏编码那组说了话」，而是
**「坏编码那组不再是三组里唯一沉默的那一个」**——它把 A/C 两组一起量进来做基准。
⚠ **这条不能简化成「断言报告里有『读取失败』四个字」**：那种写法在报告整体退化
（比如将来有人给 `memory_report` 加个「出错就整段省略」的分支）时照样能通过，
只要那四个字还在别处出现；而三组对照量的是**相对表现**，任何一组退回沉默都会红。

⚠ **两侧的修复防的是不同的失败形态，所以两组用例缺一不可**（这也是本轮新登记的
成对维护点）：`ThreeGroupsTest` 钉 `instructions.py` 那侧（已知失败落回
`layer.errors`、另外两层照常生效），`FallbackKeepsLayersTest` 钉 `manager.py` 那侧
（没想到的失败仍带着三层结构冒出来）。撤掉任一处修复只会红掉其中一组——
变异实测两次分别确认过。
"""

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from rhinecode.memory.instructions import (
    fallback_instructions,
    load_instructions,
)
from rhinecode.memory.manager import MemoryManager
from rhinecode.provider.base import BaseProvider


class _SilentProvider(BaseProvider):
    """本模块只验启动期的加载与报告，一次模型调用都不会发生。"""

    def stream_chat(self, messages, thinking_effort="off", tools=None, system=None):
        raise AssertionError("本模块不应触发任何模型调用")
        yield  # pragma: no cover - 让它仍是生成器函数


# GBK 编码的项目根 RHINE.md 内容：中文 Windows 上最常见的形态。
_GBK_BYTES = "# 项目指令\n禁止推送到 main。\n".encode("gbk")
# 用户级那层永远是干净的 UTF-8——它的存活与否就是「连坐清空」的判据。
_USER_TEXT = "# 用户级指令\n请始终用中文回答。\n"


class _MemoryFixture(unittest.TestCase):
    """三层目录 + 一个已 startup 的 MemoryManager 的公共夹具。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.project = root / "proj"
        self.user_dir = root / "home" / ".rhinecode"
        self.project.mkdir(parents=True)
        self.user_dir.mkdir(parents=True)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _manager(self) -> MemoryManager:
        """构造并 startup 一个 manager；返回值里带上 startup 的提示文本。"""
        mgr = MemoryManager(
            _SilentProvider(),
            "test-model",
            self.project,
            self.user_dir,
            memories_enabled=False,
        )
        self.notice = mgr.startup(resume_latest=False, history=[])
        return mgr

    def _write_user_layer(self) -> None:
        (self.user_dir / "RHINE.md").write_text(_USER_TEXT, encoding="utf-8")

    def _report_section(self, mgr: MemoryManager) -> "list[str]":
        """截出 /memory 报告里「RHINE.md 项目指令：」那一段的所有行。"""
        lines = mgr.memory_report().splitlines()
        start = lines.index("RHINE.md 项目指令：")
        out: list[str] = []
        for line in lines[start + 1 :]:
            if line and not line.startswith(" "):
                break  # 遇到下一段的标题（顶格行）即停
            if line.strip():
                out.append(line)
        return out


class ThreeGroupsTest(_MemoryFixture):
    """
    B6 的核心反证：三组对照，坏编码那组**不再是唯一沉默的那一个**。

    三组的输入只差一个文件（内容/编码/存在与否），因此报告的差异只能来自被测逻辑。
    """

    def _group_a_all_missing(self) -> "list[str]":
        """A 组：真的一个 RHINE.md 都没有。修复前后都会如实列出三行「未找到」。"""
        return self._report_section(self._manager())

    def _group_b_bad_encoding(self) -> "list[str]":
        """B 组：用户级正常 UTF-8 + 项目根 GBK。修复前这一组整段空白。"""
        self._write_user_layer()
        (self.project / "RHINE.md").write_bytes(_GBK_BYTES)
        return self._report_section(self._manager())

    def _group_c_include_out_of_bounds(self) -> "list[str]":
        """C 组：`@include` 越界——`load_instructions` 自己本来就兜得住的那类。"""
        self._write_user_layer()
        (self.project / "RHINE.md").write_text(
            "# 项目指令\n@../outside.md\n", encoding="utf-8"
        )
        return self._report_section(self._manager())

    def test_bad_encoding_is_not_the_only_silent_group(self) -> None:
        """
        ⚠ **本模块最关键的一条。** 量的是三组的**相对表现**：A 与 C 各说了多少行，
        B 就不能显著更少。修复前 B 是 0 行而 A/C 各 3 行以上。

        用「行数」而不是「有没有某个关键词」，是为了让报告整体退化时也红——
        关键词断言在「报告结构垮了但那几个字还在别处」时会静默通过。
        """
        rows_a = self._group_a_all_missing()
        self.tearDown()
        self.setUp()
        rows_b = self._group_b_bad_encoding()
        self.tearDown()
        self.setUp()
        rows_c = self._group_c_include_out_of_bounds()

        self.assertEqual(len(rows_a), 3, f"A 组该列出三层：{rows_a}")
        # B 组至少要有三层 + 一条警告；修复前它是 0 行。
        self.assertGreaterEqual(
            len(rows_b),
            len(rows_a),
            "坏编码那组说的话比「三层全缺」还少——B6 又回来了：\n"
            f"A（全缺）={rows_a}\nB（坏编码）={rows_b}",
        )
        self.assertGreaterEqual(
            len(rows_b),
            len(rows_c),
            "坏编码那组说的话比「@include 越界」还少——它本该是更严重的那一类：\n"
            f"C（越界）={rows_c}\nB（坏编码）={rows_b}",
        )

    def test_bad_encoding_report_names_three_layers_and_warns(self) -> None:
        """
        R4-1 直接点名的判据：喂一个 GBK 的 RHINE.md，`/memory` 里仍然出现三行层信息
        且至少一行带「警告」。这条在修复前会红（整段空白）。
        """
        rows = self._group_b_bad_encoding()
        for label in ("用户级", "项目级 .rhinecode", "项目根"):
            self.assertTrue(
                any(f"[{label}]" in r for r in rows),
                f"报告里少了「{label}」这一层：{rows}",
            )
        self.assertTrue(
            any("警告：" in r for r in rows), f"报告里一条警告都没有：{rows}"
        )

    def test_other_layers_survive_a_bad_encoding_layer(self) -> None:
        """
        「不被连坐清空」：坏编码只该干掉它自己那一层，另外两层照常进系统提示。

        ⚠ 这条与上面两条防的是同一处修复的**两个不同后果**（`layers=[]` 与
        `text=""`），不能合并——只看报告的话，一个「报告修好了但 text 仍然是空」
        的实现会全绿，而用户写的指令照样不生效。
        """
        self._write_user_layer()
        (self.project / "RHINE.md").write_bytes(_GBK_BYTES)
        mgr = self._manager()
        self.assertIn(
            "请始终用中文回答", mgr.custom_instructions(),
            "用户级那层被坏编码的项目根层连坐清空了",
        )

    def test_startup_notice_mentions_the_unreadable_file(self) -> None:
        """
        用户**不必敲 `/memory` 就能知道出事了**：启动提示里点名那个文件。

        少了这条，修复只做到「查得出来」而没做到「说得出口」——而一个不知道
        出了事的人根本不会去敲 `/memory`。
        """
        self._write_user_layer()
        (self.project / "RHINE.md").write_bytes(_GBK_BYTES)
        self._manager()
        self.assertIsNotNone(self.notice, "坏编码时 startup() 什么都没说")
        self.assertIn(str(self.project / "RHINE.md"), self.notice)

    def test_clean_startup_stays_silent(self) -> None:
        """
        反向反证：一切正常时启动提示必须仍是 None。

        少了它，一个「无条件返回一句提示」的实现会让上面那条全绿，
        而每次启动都多一段谁也不看的噪声——那正是「提示多到没人看」的开端。
        """
        self._write_user_layer()
        (self.project / "RHINE.md").write_text("# 项目指令\n", encoding="utf-8")
        self._manager()
        self.assertIsNone(self.notice, f"一切正常却冒出提示：{self.notice!r}")


class LayerReadFailureTest(_MemoryFixture):
    """`instructions.py` 那侧的直接判据（不经 manager，避免被兜底掩盖）。"""

    def test_decode_error_lands_in_layer_errors(self) -> None:
        """
        坏编码必须**落回它本来就该落的地方**——该层的 `errors`，而不是抛出去。

        直接调 `load_instructions` 是刻意的：经过 manager 的话，即使这一处修复
        被撤掉，`fallback_instructions` 也会让报告看起来还有三层，
        这条就再也发现不了「异常其实穿透了」。
        """
        self._write_user_layer()
        (self.project / "RHINE.md").write_bytes(_GBK_BYTES)
        result = load_instructions(self.user_dir, self.project)  # 不得抛异常
        self.assertEqual([lay.loaded for lay in result.layers], [True, False, False])
        self.assertTrue(
            any("读取失败" in e for e in result.layers[2].errors),
            f"坏编码没有进 layer.errors：{result.layers[2].errors}",
        )
        self.assertIn("请始终用中文回答", result.text)

    def test_included_file_with_bad_encoding_does_not_escape(self) -> None:
        """
        同一个洞的第二个入口：宿主 RHINE.md 是干净的 UTF-8，**被 @include 的子文件**
        是 GBK。修复前它同样会一路抛到 catch-all，而排查时几乎不可能想到根因在
        被引用的那个文件上。
        """
        (self.project / "sub.md").write_bytes(_GBK_BYTES)
        (self.project / "RHINE.md").write_text(
            "# 项目指令\n@sub.md\n", encoding="utf-8"
        )
        result = load_instructions(self.user_dir, self.project)  # 不得抛异常
        self.assertTrue(result.layers[2].loaded, "宿主层本身该照常加载")
        self.assertTrue(
            any("@include 读取失败" in e for e in result.layers[2].errors),
            f"子文件的坏编码没有进 errors：{result.layers[2].errors}",
        )

    def test_malformed_path_does_not_escape_safe_resolve(self) -> None:
        """
        第三个入口：`Path.resolve()` 在 Windows 上对畸形路径抛的是 `ValueError`
        （典型是内嵌 NUL），而 `_safe_resolve` 的契约是「绝不抛」。

        用内嵌 NUL 的 `@` 引用构造——它在各平台上的具体异常类型不完全一致，
        所以判据只写「`load_instructions` 不抛异常」这一条，不断言错误文案。
        """
        (self.project / "RHINE.md").write_text(
            "# 项目指令\n@a\x00b.md\n", encoding="utf-8"
        )
        result = load_instructions(self.user_dir, self.project)  # 不得抛异常
        self.assertTrue(result.layers[2].loaded)


class FallbackKeepsLayersTest(_MemoryFixture):
    """
    `manager.py` 那侧的判据：**没想到的**异常也要带着三层结构冒出来。

    这一组与 `ThreeGroupsTest` 防的是不同的失败形态（见模块 docstring 与
    `paired-maintenance` 里那条成对维护点），撤掉 `instructions.py` 那侧的修复
    不会让这一组变红，反之亦然——两侧各自的变异实测都确认过。
    """

    def test_unexpected_error_still_reports_three_layers(self) -> None:
        """
        ⚠ 判据是 **`layers` 非空**，不是 `text` 非空。`layers=[]` 才是让
        「RHINE.md 项目指令：」后面一片空白的那一个。
        """
        self._write_user_layer()
        with mock.patch(
            "rhinecode.memory.manager.load_instructions",
            side_effect=RuntimeError("谁也没想到的错误"),
        ):
            mgr = self._manager()
        rows = self._report_section(mgr)
        self.assertEqual(len(rows), 4, f"三层 + 一条警告，实际：{rows}")
        for label in ("用户级", "项目级 .rhinecode", "项目根"):
            self.assertTrue(any(f"[{label}]" in r for r in rows), rows)
        self.assertTrue(any("谁也没想到的错误" in r for r in rows), rows)

    def test_unexpected_error_reaches_the_startup_notice(self) -> None:
        """整体失败必须走启动提示这条既有通道，而不是只躺在 /memory 里等人来问。"""
        with mock.patch(
            "rhinecode.memory.manager.load_instructions",
            side_effect=RuntimeError("谁也没想到的错误"),
        ):
            self._manager()
        self.assertIsNotNone(self.notice)
        self.assertIn("谁也没想到的错误", self.notice)

    def test_fallback_layers_match_the_real_layer_specs(self) -> None:
        """
        兜底对象的三层必须与正常加载**逐字同名同路径**——否则 `/memory` 在正常与
        兜底两条路径下会显示成两副样子，用户无从分辨自己看到的是哪一种。

        这也是把层次定义抽成 `_layer_specs()` 由两条路径共用的原因：
        照抄一份的话，将来新增一层时漏改兜底那份**不报错**。
        """
        real = load_instructions(self.user_dir, self.project)
        fake = fallback_instructions(self.user_dir, self.project, "原因")
        self.assertEqual(
            [(lay.label, lay.path) for lay in real.layers],
            [(lay.label, lay.path) for lay in fake.layers],
        )
        self.assertTrue(all(not lay.loaded for lay in fake.layers))
        self.assertEqual(fake.text, "")


class ReportDistinguishesMissingFromUnreadableTest(_MemoryFixture):
    """
    `/memory` 的层状态是**三态**：已加载 / 读取失败 / 未找到。

    ⚠ 别顺手合回两态。`loaded=False` 现在有两种成因，而它们对用户的含义完全相反：
    「未找到」是「你没写这一层」（正常），「读取失败」是「你写了、但它没生效」
    （要动手修）。把一个明明存在的文件报成「未找到」会把人引去建一个已经存在的
    文件，然后发现建不了——比不说更糟。
    """

    def test_unreadable_layer_is_not_reported_as_missing(self) -> None:
        self._write_user_layer()
        (self.project / "RHINE.md").write_bytes(_GBK_BYTES)
        rows = self._report_section(self._manager())
        root_row = next(r for r in rows if "[项目根]" in r)
        self.assertIn("读取失败", root_row)
        self.assertNotIn("未找到", root_row)

    def test_truly_missing_layer_still_says_missing(self) -> None:
        """反证：真的不存在时仍须是「未找到」，别把三态改成「一律读取失败」。"""
        self._write_user_layer()
        rows = self._report_section(self._manager())
        root_row = next(r for r in rows if "[项目根]" in r)
        self.assertIn("未找到", root_row)


if __name__ == "__main__":
    unittest.main()
