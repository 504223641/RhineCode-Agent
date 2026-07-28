"""
Skill 三层目录扫描单测（c11 T11）。

覆盖 spec AC2（单文件型与目录型都被发现）、AC3（三层同名按优先级覆盖、整份替换）、
AC5（坏文件不阻断其余）、AC31（层内同名去重确定性）。

用真实临时目录而非 mock 文件系统：本模块的职责就是「和文件系统打交道」，
mock 掉它等于什么也没测。
"""

import tempfile
import unittest
from pathlib import Path

from rhinecode.skills.discovery import discover
from rhinecode.skills.models import RESOURCE_LIST_MAX, SkillMode, SkillSource


def _write(path: Path, text: str) -> None:
    """写一个文件，自动建父目录。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _skill_text(name: str, description: str = "说明", **extra) -> str:
    """造一份最小可用的 Skill 文本，extra 追加到 frontmatter。"""
    lines = [f"name: {name}", f"description: {description}"]
    lines.extend(f"{k}: {v}" for k, v in extra.items())
    return "---\n" + "\n".join(lines) + "\n---\n" + f"{name} 的 SOP 正文\n"


class DiscoveryTestBase(unittest.TestCase):
    """提供临时三层目录。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.project_root = root / "proj"
        self.user_dir = root / "user"
        self.builtin_dir = root / "builtin"
        # 三层的实际扫描目录
        self.project_skills = self.project_root / ".rhinecode" / "skills"
        self.user_skills = self.user_dir / "skills"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _discover(self):
        return discover(self.project_root, self.user_dir, self.builtin_dir)


class ScanTest(DiscoveryTestBase):
    """单层扫描：两种形态、资源清单、坏样本。"""

    def test_single_file_and_directory_skill(self) -> None:
        """单文件型与目录型都被发现，目录型正确定位 SKILL.md（AC2）。"""
        _write(self.user_skills / "alpha.md", _skill_text("alpha"))
        _write(self.user_skills / "beta" / "SKILL.md", _skill_text("beta"))
        _write(self.user_skills / "beta" / "template.txt", "模板内容")

        catalog = self._discover()
        names = [s.name for s in catalog.skills]
        self.assertEqual(names, ["alpha", "beta"])

        alpha = catalog.skills[0]
        self.assertIsNone(alpha.resource_dir)
        self.assertEqual(alpha.resource_files, ())

        beta = catalog.skills[1]
        self.assertEqual(beta.entry_path.name, "SKILL.md")
        self.assertEqual(beta.resource_dir, self.user_skills / "beta")
        self.assertEqual(beta.resource_files, ("template.txt",))

    def test_name_wins_over_filename(self) -> None:
        """name 与文件名不一致时以 frontmatter 的 name 为准。"""
        _write(self.user_skills / "whatever.md", _skill_text("real-name"))
        catalog = self._discover()
        self.assertEqual([s.name for s in catalog.skills], ["real-name"])

    def test_resource_files_sorted_excluding_entry_and_truncated(self) -> None:
        """资源清单：不含 SKILL.md、按字典序、超上限被截断（F13）。"""
        d = self.user_skills / "pack"
        _write(d / "SKILL.md", _skill_text("pack"))
        for i in range(RESOURCE_LIST_MAX + 10):
            _write(d / f"f{i:03d}.txt", "x")
        _write(d / "nested" / "deep.md", "y")

        catalog = self._discover()
        files = catalog.skills[0].resource_files
        self.assertEqual(len(files), RESOURCE_LIST_MAX)
        self.assertNotIn("SKILL.md", files)
        self.assertEqual(list(files), sorted(files))

    def test_nested_resource_uses_posix_separator(self) -> None:
        """嵌套资源的相对路径统一用 / 分隔（跨平台一致，模型不必处理反斜杠）。"""
        d = self.user_skills / "pack"
        _write(d / "SKILL.md", _skill_text("pack"))
        _write(d / "tpl" / "a.md", "x")
        catalog = self._discover()
        self.assertIn("tpl/a.md", catalog.skills[0].resource_files)

    def test_missing_directory_is_not_an_error(self) -> None:
        """三层目录都不存在 → 空 catalog，不报错。"""
        catalog = self._discover()
        self.assertEqual(catalog.skills, ())
        self.assertEqual(catalog.errors, ())

    def test_non_md_files_silently_skipped(self) -> None:
        """非 .md 文件静默跳过，不刷错误（用户可能放 README 或临时文件）。"""
        _write(self.user_skills / "notes.txt", "随手记")
        _write(self.user_skills / "ok.md", _skill_text("ok"))
        catalog = self._discover()
        self.assertEqual([s.name for s in catalog.skills], ["ok"])
        self.assertEqual(catalog.errors, ())

    def test_four_bad_samples_do_not_block_the_rest(self) -> None:
        """四种坏样本同时存在 → 好的照常可用，errors 含四条各自原因（AC5）。"""
        _write(self.user_skills / "good.md", _skill_text("good"))
        _write(self.user_skills / "bad-yaml.md", "---\nname: [坏\n---\n正文\n")
        _write(self.user_skills / "no-desc.md", "---\nname: x\n---\n正文\n")
        _write(self.user_skills / "bad-name.md", "---\nname: BAD\ndescription: d\n---\n正文\n")
        _write(self.user_skills / "dir-no-entry" / "readme.md", "不是入口")

        catalog = self._discover()
        self.assertEqual([s.name for s in catalog.skills], ["good"])
        self.assertEqual(len(catalog.errors), 4)
        reasons = " | ".join(e.reason for e in catalog.errors)
        self.assertIn("解析失败", reasons)
        self.assertIn("description", reasons)
        self.assertIn("不合法", reasons)
        self.assertIn("SKILL.md", reasons)

    def test_intra_layer_duplicate_resolved_by_sort_order(self) -> None:
        """层内同名：字典序在前的生效，另一条进 errors（AC31/F29）。"""
        _write(self.user_skills / "aaa.md", _skill_text("dup", "先到的"))
        _write(self.user_skills / "zzz.md", _skill_text("dup", "后到的"))

        catalog = self._discover()
        self.assertEqual(len(catalog.skills), 1)
        self.assertEqual(catalog.skills[0].description, "先到的")
        self.assertEqual(len(catalog.errors), 1)
        self.assertIn("与同层", catalog.errors[0].reason)
        self.assertEqual(catalog.errors[0].path.name, "zzz.md")


class LayerPriorityTest(DiscoveryTestBase):
    """跨层覆盖：优先级、整份替换、不产生 error。"""

    def test_project_beats_user_beats_builtin(self) -> None:
        """三层同名 → 项目级生效（AC3）。"""
        _write(self.project_skills / "s.md", _skill_text("s", "项目级"))
        _write(self.user_skills / "s.md", _skill_text("s", "用户级"))
        _write(self.builtin_dir / "s.md", _skill_text("s", "内置"))

        catalog = self._discover()
        self.assertEqual(len(catalog.skills), 1)
        self.assertIs(catalog.skills[0].source, SkillSource.PROJECT)
        self.assertEqual(catalog.skills[0].description, "项目级")

    def test_user_beats_builtin_when_no_project(self) -> None:
        _write(self.user_skills / "s.md", _skill_text("s", "用户级"))
        _write(self.builtin_dir / "s.md", _skill_text("s", "内置"))
        catalog = self._discover()
        self.assertIs(catalog.skills[0].source, SkillSource.USER)

    def test_override_is_wholesale_not_field_merge(self) -> None:
        """
        覆盖是整份替换，不做字段合并（AC3）。

        造两层字段互补的样本反证：项目级只写必填字段，用户级额外声明了
        allowed_tools 与 isolated 模式。若实现做了字段合并，项目级那份就会
        「继承」到用户级的白名单与模式——这里断言它没有。
        """
        _write(self.project_skills / "s.md", _skill_text("s", "项目级"))
        _write(
            self.user_skills / "s.md",
            "---\nname: s\ndescription: 用户级\n"
            "allowed_tools: [read_file]\nmode: isolated\nhistory_messages: 9\n"
            "---\n用户级正文\n",
        )

        catalog = self._discover()
        won = catalog.skills[0]
        self.assertIs(won.source, SkillSource.PROJECT)
        self.assertIsNone(won.allowed_tools)
        self.assertIs(won.mode, SkillMode.SHARED)
        self.assertEqual(won.history_messages, 0)
        self.assertIn("项目级", won.description)
        self.assertNotIn("用户级正文", won.body)

    def test_override_produces_no_error(self) -> None:
        """低优先层被覆盖是正常行为，不记 error（否则每个定制过样板的项目都会刷告警）。"""
        _write(self.project_skills / "s.md", _skill_text("s"))
        _write(self.builtin_dir / "s.md", _skill_text("s"))
        catalog = self._discover()
        self.assertEqual(catalog.errors, ())

    def test_overridden_skill_warning_is_suppressed(self) -> None:
        """
        被覆盖那份的警告不该发出去。

        内置层的 s 是共享模式却声明了 model（会产出 F22 警告），
        但它被项目级覆盖了——若仍发出该警告，用户会被指去改一个根本没生效的文件。
        """
        _write(self.project_skills / "s.md", _skill_text("s", "项目级"))
        _write(
            self.builtin_dir / "s.md",
            "---\nname: s\ndescription: 内置\nmode: shared\nmodel: m\n---\n正文\n",
        )
        catalog = self._discover()
        self.assertEqual(catalog.warnings, ())

    def test_effective_skill_warning_is_kept(self) -> None:
        """反过来，生效那份的警告必须保留（不能一刀切全丢）。"""
        _write(
            self.project_skills / "s.md",
            "---\nname: s\ndescription: 项目级\nmode: shared\nmodel: m\n---\n正文\n",
        )
        catalog = self._discover()
        self.assertEqual(len(catalog.warnings), 1)
        self.assertIn("共享模式", catalog.warnings[0])

    def test_skills_sorted_by_name(self) -> None:
        """产出的 skills 按名字排序（清单展示顺序稳定）。"""
        _write(self.user_skills / "z.md", _skill_text("zeta"))
        _write(self.builtin_dir / "a.md", _skill_text("alpha"))
        _write(self.project_skills / "m.md", _skill_text("mid"))
        catalog = self._discover()
        self.assertEqual([s.name for s in catalog.skills], ["alpha", "mid", "zeta"])


if __name__ == "__main__":
    unittest.main()
