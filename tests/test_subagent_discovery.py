"""
角色三层扫描的单测（c13 T5，覆盖 AC2 / AC3a / AC3b）。

三件事：**优先级覆盖**、**单文件失败不阻断**、**未生效的定义要看得见**。

最后一件最容易被忽略却最要紧：被覆盖或重名而未生效的定义若静默丢弃，
用户看到的现象是「我明明改了这个文件，配置却是另一套」，而文件就在那里、
没有任何报错。
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from rhinecode.subagents.discovery import discover_agents
from rhinecode.subagents.models import AgentSource


def _write(directory: Path, filename: str, front: str, body: str = "正文") -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / filename
    path.write_text(f"---\n{front}\n---\n{body}", encoding="utf-8")
    return path


class DiscoveryBase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.project = root / "project"
        self.user = root / "user"
        self.builtin = root / "builtin"
        self.addCleanup(self._tmp.cleanup)

    def discover(self):
        return discover_agents(self.project, self.user, self.builtin)


class LayerPrecedenceTest(DiscoveryBase):
    """AC2：项目 > 用户 > 内置。"""

    def test_project_beats_user(self) -> None:
        _write(self.project, "rev.md", "description: 项目版\ntools: read_file")
        _write(self.user, "rev.md", "description: 用户版\ntools: run_command")

        catalog = self.discover()
        spec = catalog.specs["rev"]

        self.assertIs(spec.source, AgentSource.PROJECT)
        # 生效的配置也必须是项目级那份——只比对 source 验不出「读了这份的配置」
        self.assertEqual(spec.description, "项目版")
        self.assertEqual(spec.tools, ("read_file",))

    def test_user_beats_builtin(self) -> None:
        _write(self.user, "explorer.md", "description: 我的版本")
        _write(self.builtin, "explorer.md", "description: 内置版本")

        self.assertIs(self.discover().specs["explorer"].source, AgentSource.USER)

    def test_shadowed_definition_is_visible(self) -> None:
        """
        被覆盖的那份必须能在报告里查到——含它在哪一层、以及谁赢了。
        """
        _write(self.project, "rev.md", "description: 项目版")
        user_path = _write(self.user, "rev.md", "description: 用户版")

        shadowed = self.discover().shadowed
        self.assertEqual(len(shadowed), 1)
        self.assertEqual(shadowed[0].name, "rev")
        self.assertIs(shadowed[0].source, AgentSource.USER)
        self.assertEqual(shadowed[0].path, user_path)
        self.assertIs(shadowed[0].winner_source, AgentSource.PROJECT)

    def test_override_is_not_an_error(self) -> None:
        """跨层覆盖是正常行为，不该刷错误——那会让 /agents 一片红。"""
        _write(self.project, "rev.md", "description: a")
        _write(self.user, "rev.md", "description: b")
        self.assertEqual(self.discover().errors, ())

    def test_name_field_wins_over_filename(self) -> None:
        """
        身份来自 `name` 而非文件名（与 C11 刻意相反，见模块 docstring）。

        这条保证从官方生态复制来的定义（文件名与 name 不一致）原样可用。
        """
        _write(self.user, "任意文件名.md", "description: x\nname: reviewer")
        catalog = self.discover()
        self.assertIn("reviewer", catalog.specs)
        self.assertNotIn("任意文件名", catalog.specs)


class FailSafeTest(DiscoveryBase):
    """AC3a：单文件失败不阻断。"""

    def test_broken_file_does_not_affect_others(self) -> None:
        _write(self.user, "good.md", "description: 好的")
        (self.user / "bad.md").write_text("---\ndescription: [坏\n---\nx", encoding="utf-8")

        catalog = self.discover()

        self.assertIn("good", catalog.specs)
        self.assertEqual(len(catalog.errors), 1)
        self.assertEqual(catalog.errors[0].path.name, "bad.md")

    def test_missing_description_is_an_error_not_a_crash(self) -> None:
        _write(self.user, "nodesc.md", "name: x")
        catalog = self.discover()
        self.assertEqual(catalog.specs, {})
        self.assertIn("description", catalog.errors[0].message)

    def test_missing_directories_are_empty_layers(self) -> None:
        """三个目录一个都不存在时，返回空目录且**零错误**。"""
        catalog = self.discover()
        self.assertEqual(catalog.specs, {})
        self.assertEqual(catalog.errors, ())
        self.assertEqual(catalog.shadowed, ())

    def test_none_directory_is_an_empty_layer(self) -> None:
        _write(self.user, "a.md", "description: x")
        catalog = discover_agents(None, self.user, None)
        self.assertEqual(list(catalog.specs), ["a"])

    def test_non_md_files_silently_skipped(self) -> None:
        """`.txt` / 无后缀 / 子目录都静默跳过，不刷错误。"""
        self.user.mkdir(parents=True, exist_ok=True)
        (self.user / "note.txt").write_text("随手记", encoding="utf-8")
        (self.user / "nosuffix").write_text("随手记", encoding="utf-8")
        (self.user / "sub").mkdir()
        _write(self.user, "a.md", "description: x")

        catalog = self.discover()
        self.assertEqual(list(catalog.specs), ["a"])
        self.assertEqual(catalog.errors, ())

    def test_readme_md_is_parsed_and_reported(self) -> None:
        """
        `README.md` **会**被当角色解析并因缺 description 报错——这是刻意的。

        `.md` 就是角色定义的后缀，不为特定文件名开后门；否则「哪些 .md 算角色」
        会变成一张要维护的名单。报出来也比静默跳过好：用户至少知道
        「这个目录里的 .md 都会被当角色读」。
        """
        (self.user).mkdir(parents=True, exist_ok=True)
        (self.user / "README.md").write_text("# 说明\n这个目录放角色定义", encoding="utf-8")

        catalog = self.discover()
        self.assertEqual(catalog.specs, {})
        self.assertEqual(len(catalog.errors), 1)
        self.assertEqual(catalog.errors[0].path.name, "README.md")


class SameLayerDuplicateTest(DiscoveryBase):
    """AC3b：同层重名——字典序靠前者生效，两份都可见。"""

    def setUp(self) -> None:
        super().setUp()
        self.a = _write(self.user, "aaa.md", "description: 第一份\nname: dup")
        self.z = _write(self.user, "zzz.md", "description: 第二份\nname: dup")
        self.catalog = self.discover()

    def test_alphabetically_first_wins(self) -> None:
        self.assertEqual(self.catalog.specs["dup"].description, "第一份")

    def test_loser_is_shadowed(self) -> None:
        self.assertEqual(len(self.catalog.shadowed), 1)
        self.assertEqual(self.catalog.shadowed[0].path, self.z)

    def test_duplicate_produces_a_readable_error(self) -> None:
        """
        同层重名**要报错**（与跨层覆盖不同）。

        跨层覆盖是设计好的行为，同层重名多半是用户自己没意识到——
        两个文件里写了同一个 `name`，而文件名不同所以他看不出来。
        错误消息里要同时出现两个文件名，他才知道去改哪一个。
        """
        self.assertEqual(len(self.catalog.errors), 1)
        message = self.catalog.errors[0].message
        self.assertIn("dup", message)
        self.assertIn("aaa.md", message)

    def test_scan_order_is_deterministic(self) -> None:
        """重扫一次结果完全一致——依赖 sorted(iterdir())。"""
        again = self.discover()
        self.assertEqual(
            again.specs["dup"].description, self.catalog.specs["dup"].description
        )


class OrderingTest(DiscoveryBase):
    """`specs` 的插入顺序即扫描顺序，报告据此展示。"""

    def test_insertion_order_follows_layers(self) -> None:
        _write(self.builtin, "c.md", "description: x")
        _write(self.user, "b.md", "description: x")
        _write(self.project, "a.md", "description: x")

        self.assertEqual(list(self.discover().specs), ["a", "b", "c"])


if __name__ == "__main__":
    unittest.main()
