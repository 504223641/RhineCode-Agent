"""
Skill 三层目录扫描单测（c11 T11）。

覆盖：单文件型与目录型都被发现、**命令名来自文件系统路径**（对齐改造 F2）、
三层同名按优先级覆盖且整份替换、坏文件不阻断其余。

C11 的「层内同名去重」用例整段删除——命令名现在来自路径，同一目录下不可能重名。

用真实临时目录而非 mock 文件系统：本模块的职责就是「和文件系统打交道」，
mock 掉它等于什么也没测。
"""

import tempfile
import unittest
from pathlib import Path

from rhinecode.skills.discovery import discover
from rhinecode.skills.models import RESOURCE_LIST_MAX, SkillSource


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
        names = [s.command_name for s in catalog.skills]
        self.assertEqual(names, ["alpha", "beta"])

        alpha = catalog.skills[0]
        self.assertIsNone(alpha.resource_dir)
        self.assertEqual(alpha.resource_files, ())

        beta = catalog.skills[1]
        self.assertEqual(beta.entry_path.name, "SKILL.md")
        self.assertEqual(beta.resource_dir, self.user_skills / "beta")
        self.assertEqual(beta.resource_files, ("template.txt",))

    def test_path_wins_over_frontmatter_name(self) -> None:
        """
        **命令名来自路径，frontmatter 的 name 只做显示**（对齐改造 F2）。

        这与 C11 恰好相反，是「外部 Skill 原样可用」的地基：从任何来源拉一个
        目录丢进去，命令名就是目录名，不必检查也不必修改 frontmatter。
        """
        _write(self.user_skills / "whatever.md", _skill_text("real-name"))
        catalog = self._discover()
        self.assertEqual([s.command_name for s in catalog.skills], ["whatever"])
        self.assertEqual(catalog.skills[0].display_name, "real-name")

    def test_directory_name_is_the_command_name(self) -> None:
        _write(self.user_skills / "my-pack" / "SKILL.md", _skill_text("别的名字"))
        catalog = self._discover()
        self.assertEqual([s.command_name for s in catalog.skills], ["my-pack"])

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
        self.assertEqual([s.command_name for s in catalog.skills], ["ok"])
        self.assertEqual(catalog.errors, ())

    def test_bad_samples_do_not_block_the_rest(self) -> None:
        """
        坏样本不阻断其余。

        ⚠️ 与 C11 相比**失败面小了很多**：缺 description（改从正文提取）、
        名字含大写（不再校验）都不再是失败。真正还会失败的只剩「YAML 坏了」
        「正文为空」「目录型缺入口」——它们让 Skill 真的没法用。
        """
        _write(self.user_skills / "good.md", _skill_text("good"))
        _write(self.user_skills / "bad-yaml.md", "---\nname: [坏\n---\n正文\n")
        _write(self.user_skills / "empty-body.md", "---\nname: x\n---\n   \n")
        _write(self.user_skills / "no-desc.md", "---\nname: x\n---\n这段会成为说明\n")
        _write(self.user_skills / "UpperName.md", "---\ndescription: d\n---\n正文\n")
        _write(self.user_skills / "dir-no-entry" / "readme.md", "不是入口")

        catalog = self._discover()
        self.assertEqual(
            sorted(s.command_name for s in catalog.skills),
            ["UpperName", "good", "no-desc"],
        )
        self.assertEqual(len(catalog.errors), 3)
        reasons = " | ".join(e.reason for e in catalog.errors)
        self.assertIn("解析失败", reasons)
        self.assertIn("正文为空", reasons)
        self.assertIn("SKILL.md", reasons)

    def test_reserved_subcommand_name_rejected(self) -> None:
        """
        命令名取了 `/skills` 的保留子命令词 → 加载失败。

        这是命令名**仅剩的**校验（对齐改造 F4）：字符集与长度不再校验，
        文件系统已保证名字合法，再叠一层自定义规则只会让本可直接使用的外部
        Skill 目录（含大写、下划线、超长名）被无理由拒绝。
        """
        _write(self.user_skills / "run.md", _skill_text("x"))
        catalog = self._discover()
        self.assertEqual(catalog.skills, ())
        self.assertIn("保留子命令词", catalog.errors[0].reason)

    def test_uppercase_and_long_names_now_accepted(self) -> None:
        """C11 会判这两个名字非法；现在一律接受。"""
        _write(self.user_skills / "MyLongSkillNameThatExceedsThirtyTwoChars.md", _skill_text("x"))
        catalog = self._discover()
        self.assertEqual(len(catalog.skills), 1)


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

    def test_shadowed_records_the_layer_that_lost(self) -> None:
        """
        被覆盖那份要在**丢弃之前**被记下来（作者期扩展 F2a）。

        三层同名 → 项目级生效，用户级与内置各记一条。
        没有这份记录，体检就报不出「你覆盖了一个内置样板」——
        因为成品清单里连它存在过的痕迹都没有。
        """
        _write(self.project_skills / "s.md", _skill_text("s", "项目级"))
        _write(self.user_skills / "s.md", _skill_text("s", "用户级"))
        _write(self.builtin_dir / "s.md", _skill_text("s", "内置"))

        catalog = self._discover()
        self.assertEqual(
            sorted(catalog.shadowed, key=lambda item: item[1].value),
            [("s", SkillSource.BUILTIN), ("s", SkillSource.USER)],
        )

    def test_shadowed_empty_when_no_name_collision(self) -> None:
        """各层名字互不相同 → 没有任何覆盖。"""
        _write(self.project_skills / "a.md", _skill_text("a"))
        _write(self.user_skills / "b.md", _skill_text("b"))
        _write(self.builtin_dir / "c.md", _skill_text("c"))
        self.assertEqual(self._discover().shadowed, ())

    def test_shadowed_distinguishes_which_layer_was_covered(self) -> None:
        """
        只盖了用户级、没盖内置 → 记录里**不能**出现内置层。

        体检对这两种情况的处理不同（只报覆盖内置），所以记录必须分得清是哪一层，
        不能只记一个「这个名字被覆盖过」的布尔。
        """
        _write(self.project_skills / "s.md", _skill_text("s", "项目级"))
        _write(self.user_skills / "s.md", _skill_text("s", "用户级"))

        catalog = self._discover()
        self.assertEqual(catalog.shadowed, (("s", SkillSource.USER),))

    def test_override_is_wholesale_not_field_merge(self) -> None:
        """
        覆盖是整份替换，不做字段合并（AC3）。

        造两层字段互补的样本反证：项目级只写基本字段，用户级额外声明了
        预授权与 fork。若实现做了字段合并，项目级那份就会「继承」到用户级的
        声明——这里断言它没有。
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
        self.assertEqual(won.granted_tools, ())
        self.assertFalse(won.forked)
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

        内置层的 s 不是 fork 却声明了 model（会产出一条提示），
        但它被项目级覆盖了——若仍发出该警告，用户会被指去改一个根本没生效的文件。
        """
        _write(self.project_skills / "s.md", _skill_text("s", "项目级"))
        _write(
            self.builtin_dir / "s.md",
            "---\nname: s\ndescription: 内置\nmode: shared\nmodel: m\n---\n正文\n",
        )
        catalog = self._discover()
        notices = [n for spec in catalog.skills for n in spec.notices]
        self.assertEqual(notices, [], "被覆盖那份的提示不该出现")

    def test_effective_skill_warning_is_kept(self) -> None:
        """反过来，生效那份的警告必须保留（不能一刀切全丢）。"""
        _write(
            self.project_skills / "s.md",
            "---\nname: s\ndescription: 项目级\nmode: shared\nmodel: m\n---\n正文\n",
        )
        catalog = self._discover()
        # 提示挂在 spec 上（`catalog.warnings` 现在只承载运行期警告）。
        # 「被覆盖那份的提示不发出」是**自动成立**的：报告只遍历 catalog.skills，
        # 被覆盖的 spec 压根不在里面。
        notices = [n for spec in catalog.skills for n in spec.notices]
        self.assertEqual(len(notices), 1)
        self.assertIn("model", notices[0])

    def test_skills_sorted_by_name(self) -> None:
        """产出的 skills 按**命令名**排序（清单展示顺序稳定）。"""
        _write(self.user_skills / "zeta.md", _skill_text("z"))
        _write(self.builtin_dir / "alpha.md", _skill_text("a"))
        _write(self.project_skills / "mid.md", _skill_text("m"))
        catalog = self._discover()
        self.assertEqual([s.command_name for s in catalog.skills], ["alpha", "mid", "zeta"])


if __name__ == "__main__":
    unittest.main()
