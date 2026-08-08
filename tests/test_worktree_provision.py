"""
c14 T7：隔离工作区的环境初始化（spec F10 / AC13 / AC14）。

本模块的核心承诺是**「从不导致创建失败」**：单条条目出任何问题都只记警告。
因此这里的用例除了验「做对了什么」，同样重要的是验「做错时也没炸」。
"""

import os
import unittest

from rhinecode.worktree.models import ProvisionEntry
from rhinecode.worktree.provision import provision
from tests.worktree_support import cleanup, make_plain_dir


class ProvisionTestBase(unittest.TestCase):
    """造一对「主项目根 / 隔离工作区」目录，不需要真的 git。"""

    def setUp(self):
        self.main = make_plain_dir(prefix="c14-main-")
        self.addCleanup(cleanup, self.main)
        self.target = make_plain_dir(prefix="c14-wt-")
        self.addCleanup(cleanup, self.target)


class CopyModeTest(ProvisionTestBase):
    """copy 的语义是「各自独立一份」——配置文件要的就是这个。"""

    def test_copies_file(self):
        (self.main / "local.yaml").write_text("a: 1\n", encoding="utf-8")
        result = provision(
            self.main, self.target, [ProvisionEntry("local.yaml", "copy")]
        )
        self.assertEqual(result.applied, ("local.yaml",))
        self.assertEqual(result.warnings, ())
        self.assertEqual(
            (self.target / "local.yaml").read_text(encoding="utf-8"), "a: 1\n"
        )

    def test_copy_is_independent(self):
        """
        AC13 的核心：改副本**不能**影响主项目根里的源文件。

        这是 copy 与 link 唯一实质的差别，也是「配置文件用 copy」的全部理由——
        子 Agent 把配置改坏了不该牵连主对话。
        """
        (self.main / "local.yaml").write_text("a: 1\n", encoding="utf-8")
        provision(self.main, self.target, [ProvisionEntry("local.yaml", "copy")])

        (self.target / "local.yaml").write_text("a: 999\n", encoding="utf-8")

        self.assertEqual(
            (self.main / "local.yaml").read_text(encoding="utf-8"), "a: 1\n"
        )

    def test_copies_directory(self):
        src = self.main / "conf"
        src.mkdir()
        (src / "x.txt").write_text("x\n", encoding="utf-8")
        result = provision(self.main, self.target, [ProvisionEntry("conf", "copy")])
        self.assertEqual(result.applied, ("conf",))
        self.assertTrue((self.target / "conf" / "x.txt").is_file())

    def test_copies_nested_path(self):
        nested = self.main / ".rhinecode"
        nested.mkdir()
        (nested / "permissions.local.yaml").write_text("x\n", encoding="utf-8")
        result = provision(
            self.main,
            self.target,
            [ProvisionEntry(".rhinecode/permissions.local.yaml", "copy")],
        )
        self.assertEqual(result.warnings, ())
        self.assertTrue(
            (self.target / ".rhinecode" / "permissions.local.yaml").is_file()
        )


class LinkModeTest(ProvisionTestBase):
    """link 的语义是「共享同一份」——大目录要的就是这个。"""

    def test_links_directory_or_degrades_with_warning(self):
        """
        Windows 上建符号链接常需额外权限。两种结局都合法，但**降级必须留痕**：
        静默降级会让「我配了 link 结果它复制了 500MB」这件事无处可查。
        """
        src = self.main / "deps"
        src.mkdir()
        (src / "big.bin").write_text("data\n", encoding="utf-8")

        result = provision(self.main, self.target, [ProvisionEntry("deps", "link")])

        self.assertEqual(result.applied, ("deps",))
        target = self.target / "deps"
        self.assertTrue((target / "big.bin").is_file(), "内容必须可达")

        if not os.path.islink(target):
            self.assertTrue(
                any("降级为复制" in w for w in result.warnings),
                f"降级了却没有警告：{result.warnings}",
            )

    def test_link_pointing_outside_the_worktree_degrades_to_copy(self):
        """
        指向工作区之外的 link **必须**降级为复制，且留下具体的警告。

        ## 这条是真实模型实测补的，不是设计推演

        `link` 的源在主项目根、落点在隔离工作区，所以它建出来的软链**天然指向
        工作区之外**——而权限管线第②层明令拒绝这类符号链接。实测现场
        （`worktree.link: ["vendor"]`）：隔离子 Agent 连着试
        `read_file('vendor/bigdep/VERSION')`、`glob_files('vendor/**')`、
        `glob_files('vendor/bigdep/*')`，全部拿到「[权限拒绝·sandbox] 路径越界」，
        最后耗尽 12 轮预算失败收场。

        **而系统当时认为它成功了**：`worktree_provision` 记的是 `applied=2`、
        `warnings=1`（那一条警告说的是另一个越界条目）。配了却没生效、
        且界面上完全看不出来——本项目最忌讳的形态。

        断言刻意包含「不是软链」这一条：只断言有警告的话，将来若有人改成
        「建软链 + 记警告」，测试照样绿，而子 Agent 依旧一个字节读不到。
        """
        src = self.main / "vendor"
        src.mkdir()
        (src / "dep.txt").write_text("v1\n", encoding="utf-8")

        result = provision(self.main, self.target, [ProvisionEntry("vendor", "link")])

        target = self.target / "vendor"
        self.assertEqual(result.applied, ("vendor",))
        self.assertFalse(
            os.path.islink(target), "指向工作区外的软链会被第②层一律拒绝，不能留着"
        )
        self.assertEqual((target / "dep.txt").read_text(encoding="utf-8"), "v1\n")
        self.assertTrue(
            any("已降级为复制" in w and "沙箱" in w for w in result.warnings),
            f"降级必须留痕、且说清是为什么：{result.warnings}",
        )


class SkipAndWarnTest(ProvisionTestBase):
    """三类跳过。每一类都必须：跳过、记警告、不影响其余条目。"""

    def test_source_outside_project_is_skipped(self):
        result = provision(
            self.main, self.target, [ProvisionEntry("../outside.txt", "copy")]
        )
        self.assertEqual(result.applied, ())
        self.assertEqual(len(result.warnings), 1)
        self.assertIn("超出项目目录", result.warnings[0])

    def test_absolute_source_outside_project_is_skipped(self):
        outside = make_plain_dir(prefix="c14-outside-")
        self.addCleanup(cleanup, outside)
        secret = outside / "id_rsa"
        secret.write_text("KEY\n", encoding="utf-8")

        result = provision(
            self.main, self.target, [ProvisionEntry(str(secret), "copy")]
        )
        self.assertEqual(result.applied, ())
        self.assertEqual(len(result.warnings), 1)
        self.assertFalse((self.target / "id_rsa").exists())

    def test_dot_entry_does_not_wipe_the_worktree(self):
        """
        ⚠ **验收期审计出来的漏洞的反证。**

        `"."` 是一条完全合法的路径写法：越界检查照样通过，而它解析出来
        就是根目录自己。若不单独挡掉，`_copy_one` 会先 `rmtree(工作区根)`
        把刚建好的工作区整个删掉，再把**整个主项目**（含其它隔离工作区）
        递归复制进去。

        它不是攻击构造——用户想「把项目里的东西都带过去」时很自然就会这么写。
        """
        (self.main / "keep.txt").write_text("main\n", encoding="utf-8")
        (self.target / "existing.txt").write_text("worktree\n", encoding="utf-8")

        result = provision(self.main, self.target, [ProvisionEntry(".", "copy")])

        self.assertEqual(result.applied, ())
        self.assertEqual(len(result.warnings), 1)
        self.assertTrue(
            (self.target / "existing.txt").is_file(), "工作区不能被清空"
        )
        self.assertFalse((self.target / "keep.txt").exists(), "不该把主项目拷进来")

    def test_dot_variants_are_rejected(self):
        """`./`、`.\\`、以及绝对形式的项目根都是同一件事。"""
        for raw in (".", "./", ".\\"):
            with self.subTest(raw=raw):
                result = provision(
                    self.main, self.target, [ProvisionEntry(raw, "copy")]
                )
                self.assertEqual(result.applied, (), f"{raw!r} 不该生效")

    def test_absolute_project_root_is_rejected(self):
        result = provision(
            self.main, self.target, [ProvisionEntry(str(self.main), "link")]
        )
        self.assertEqual(result.applied, ())
        self.assertEqual(len(result.warnings), 1)

    def test_missing_source_is_skipped(self):
        result = provision(
            self.main, self.target, [ProvisionEntry("nope.yaml", "copy")]
        )
        self.assertEqual(result.applied, ())
        self.assertIn("不存在", result.warnings[0])

    def test_unknown_mode_is_skipped(self):
        (self.main / "a.txt").write_text("a\n", encoding="utf-8")
        result = provision(self.main, self.target, [ProvisionEntry("a.txt", "move")])
        self.assertEqual(result.applied, ())
        self.assertIn("无法识别", result.warnings[0])

    def test_empty_source_is_skipped(self):
        result = provision(self.main, self.target, [ProvisionEntry("  ", "copy")])
        self.assertEqual(result.applied, ())
        self.assertEqual(len(result.warnings), 1)

    def test_bad_entry_does_not_block_good_one(self):
        """
        AC14：一条坏条目不影响其余，且创建本身仍成功。

        这条是本模块「从不导致创建失败」承诺的具体形态。
        """
        (self.main / "good.yaml").write_text("ok\n", encoding="utf-8")
        result = provision(
            self.main,
            self.target,
            [
                ProvisionEntry("../evil", "copy"),
                ProvisionEntry("good.yaml", "copy"),
                ProvisionEntry("missing", "link"),
            ],
        )
        self.assertEqual(result.applied, ("good.yaml",))
        self.assertEqual(len(result.warnings), 2)
        self.assertTrue((self.target / "good.yaml").is_file())


class EmptyManifestTest(ProvisionTestBase):
    """缺省是空清单——什么都不做，一个额外文件都不产生。"""

    def test_empty(self):
        before = sorted(p.name for p in self.target.iterdir())
        result = provision(self.main, self.target, [])
        after = sorted(p.name for p in self.target.iterdir())
        self.assertEqual(result.applied, ())
        self.assertEqual(result.warnings, ())
        self.assertEqual(before, after)


if __name__ == "__main__":
    unittest.main()
