"""
`/agents` 报告的单测（c13 T21，覆盖 AC14c / AC16 / AC3b）。

两处要点：

- **权限档位要同时显示声明值与生效值**（spec F16）。只显示生效值的话，
  用户写了 `permission_mode: permissive` 却看到「默认」，会以为配置没读到。
- **空段不出现**。无错误时冒出一个空的「加载错误」标题，会让用户以为出了什么事。
"""

from __future__ import annotations

import unittest
from pathlib import Path

from rhinecode.permission.models import PermissionMode
from rhinecode.subagents.models import (
    AgentCatalog,
    AgentLoadError,
    AgentSource,
    AgentSpec,
    ShadowedAgent,
)
from rhinecode.subagents.report import render_report
from rhinecode.subagents.tasks import KIND_ROLE, TaskManager, TaskStatus


def _spec(name="explorer", **kw) -> AgentSpec:
    base = dict(
        name=name,
        description="只读调研",
        body="",
        source=AgentSource.BUILTIN,
        path=Path(f"{name}.md"),
    )
    base.update(kw)
    return AgentSpec(**base)


def _render(catalog, tasks=(), effective=PermissionMode.DEFAULT, **kw) -> str:
    return render_report(
        catalog,
        tasks,
        tools_for=lambda spec: "read_file、glob_files",
        effective_mode_for=lambda spec: effective,
        **kw,
    )


class AgentSectionTest(unittest.TestCase):
    def test_lists_name_source_description_tools(self) -> None:
        text = _render(AgentCatalog(specs={"explorer": _spec()}))

        self.assertIn("explorer", text)
        self.assertIn("内置", text)
        self.assertIn("只读调研", text)
        self.assertIn("read_file", text)

    def test_empty_catalog_tells_user_where_to_put_files(self) -> None:
        """
        一个角色都没有时，报告要说清**去哪儿放文件**。

        只说「尚未加载任何角色」的话，用户下一步无从下手。
        """
        text = _render(AgentCatalog())
        self.assertIn(".rhinecode/agents", text)

    def test_warnings_shown_per_agent(self) -> None:
        spec = _spec(warnings=("本项目不支持 memory 字段，已忽略——xxx",))
        text = _render(AgentCatalog(specs={"explorer": spec}))
        self.assertIn("memory", text)


class PermissionModeDisplayTest(unittest.TestCase):
    """AC14c：声明值与生效值都要可见。"""

    def test_shows_both_when_clamped(self) -> None:
        spec = _spec(permission_mode=PermissionMode.PERMISSIVE)
        text = _render(
            AgentCatalog(specs={"explorer": spec}), effective=PermissionMode.DEFAULT
        )

        # auto-plan 扩展：放行档对用户显示成 `auto（permissive）`——它就是 auto
        # 预设内部的档位。括号里那个 YAML 值不可省：这一列的用途正是帮用户对照
        # 自己写的角色定义，只显示 auto 的话他不知道 `permission_mode:` 该填什么。
        self.assertIn("auto", text, "声明值必须可见")
        self.assertIn("permissive", text, "还要看得出 YAML 里该写什么")
        self.assertIn("默认", text, "生效值必须可见")
        self.assertIn("受主对话档位限制", text, "还要说明为什么不一样")

    def test_no_clamp_note_when_identical(self) -> None:
        """未被夹住时不该出现那句解释——它会让用户以为出了什么事。"""
        spec = _spec(permission_mode=PermissionMode.STRICT)
        text = _render(
            AgentCatalog(specs={"explorer": spec}), effective=PermissionMode.STRICT
        )
        self.assertNotIn("受主对话档位限制", text)

    def test_inherit_is_labelled(self) -> None:
        text = _render(
            AgentCatalog(specs={"explorer": _spec()}), effective=PermissionMode.DEFAULT
        )
        self.assertIn("继承", text)


class ErrorAndShadowSectionTest(unittest.TestCase):
    """AC3a / AC3b。"""

    def test_error_section_appears_with_path_and_reason(self) -> None:
        catalog = AgentCatalog(
            errors=(AgentLoadError(Path("bad.md"), AgentSource.PROJECT, "YAML 坏了"),)
        )
        text = _render(catalog)

        self.assertIn("加载错误", text)
        self.assertIn("bad.md", text)
        self.assertIn("YAML 坏了", text)

    def test_error_section_absent_when_clean(self) -> None:
        self.assertNotIn("加载错误", _render(AgentCatalog(specs={"a": _spec("a")})))

    def test_shadowed_section_names_the_winner(self) -> None:
        """
        未生效的定义要说明**生效的是哪一层**——用户才知道去哪儿找那份。
        """
        catalog = AgentCatalog(
            specs={"rev": _spec("rev", source=AgentSource.PROJECT)},
            shadowed=(
                ShadowedAgent("rev", AgentSource.USER, Path("u/rev.md"), AgentSource.PROJECT),
            ),
        )
        text = _render(catalog)

        self.assertIn("未生效的定义", text)
        self.assertIn("u/rev.md", text.replace("\\", "/"))
        self.assertIn("生效的是项目级那份", text)

    def test_shadowed_section_absent_when_none(self) -> None:
        self.assertNotIn("未生效的定义", _render(AgentCatalog(specs={"a": _spec("a")})))


class TaskSectionTest(unittest.TestCase):
    """AC16。"""

    def setUp(self) -> None:
        self.tm = TaskManager()

    def test_shows_status_turns_and_usage(self) -> None:
        record = self.tm.create(KIND_ROLE, "explorer", "去查点东西")
        self.tm.bump(record.task_id, turns=4, tokens=1234)
        self.tm.finish(record.task_id, TaskStatus.COMPLETED, "找到了三处。")

        text = _render(AgentCatalog(), self.tm.snapshot())

        self.assertIn(record.task_id, text)
        self.assertIn("已完成", text)
        self.assertIn("4 轮", text)
        self.assertIn("1234", text)
        self.assertIn("找到了三处。", text)

    def test_running_task_shows_task_text(self) -> None:
        """还没有结论时显示任务描述——总比一片空白强。"""
        self.tm.create(KIND_ROLE, "explorer", "去查点东西")
        text = _render(AgentCatalog(), self.tm.snapshot())
        self.assertIn("运行中", text)
        self.assertIn("去查点东西", text)

    def test_conclusion_preview_is_single_line(self) -> None:
        """
        列表里只显示结论首行。多行结论会把任务段撑开、盖掉其余任务。
        """
        record = self.tm.create(KIND_ROLE, "explorer", "t")
        self.tm.finish(record.task_id, TaskStatus.COMPLETED, "第一行\n第二行\n第三行")

        text = _render(AgentCatalog(), self.tm.snapshot())
        self.assertIn("第一行", text)
        self.assertNotIn("第二行", text)

    def test_no_tasks_message(self) -> None:
        self.assertIn("尚未发起过委派", _render(AgentCatalog()))


class IsolationRowTest(unittest.TestCase):
    """
    c14 F23：隔离任务多一行工作区信息——**但工作区被回收后必须改口**。

    这组用例来自真实模型实测：两个只读任务跑完即回收（无变更 → 目录与分支
    一并删，F16），而 `/agents` 仍原样展示「分支 agent/surveyor-xxx · 路径 …」。
    用户照着它 `git checkout` 会拿到「分支不存在」。交付信息段那边刻意
    「不给已删分支的名字」，是同一条理由——那边做对了、这边漏了。
    """

    def setUp(self) -> None:
        self.tm = TaskManager()

    def _record(self, *, removed: bool):
        record = self.tm.create(KIND_ROLE, "worker", "去干活")
        record.worktree_path = "/proj/.rhinecode/worktrees/worker-a1b2"
        record.worktree_branch = "agent/worker-a1b2"
        record.worktree_removed = removed
        self.tm.finish(record.task_id, TaskStatus.COMPLETED, "干完了。")
        return record

    def test_kept_worktree_shows_branch_and_path(self) -> None:
        self._record(removed=False)
        text = _render(AgentCatalog(), self.tm.snapshot())
        self.assertIn("agent/worker-a1b2", text)
        self.assertIn("worktrees/worker-a1b2", text)

    def test_removed_worktree_never_names_the_dead_branch(self) -> None:
        """
        断言的是**不出现分支名**，而不是「出现了某句话」——措辞会改，
        「别把一个已删的分支名递给用户」这件事不会改。
        """
        self._record(removed=True)
        text = _render(AgentCatalog(), self.tm.snapshot())
        self.assertNotIn("agent/worker-a1b2", text)
        self.assertNotIn("worktrees/worker-a1b2", text)
        self.assertIn("已回收", text)

    def test_non_isolated_task_has_no_row_at_all(self) -> None:
        """反证：不隔离的任务不该冒出这一行（绝大多数任务都不隔离，加了全是噪音）。"""
        record = self.tm.create(KIND_ROLE, "explorer", "去查")
        self.tm.finish(record.task_id, TaskStatus.COMPLETED, "查完了。")
        self.assertNotIn("隔离工作区", _render(AgentCatalog(), self.tm.snapshot()))


class MarkupSafetyTest(unittest.TestCase):
    """
    转义纪律的护栏：本模块**不转义**，产出纯文本。

    与 `hooks/report.py` 同口径——`HistoryView.append_system` 会对整段走一次
    `escape`。这里再转一次就是双重转义，用户会看到字面的 `\\[`。
    """

    def test_report_does_not_pre_escape(self) -> None:
        spec = _spec("weird[name")
        text = _render(AgentCatalog(specs={"weird[name": spec}))

        self.assertIn("weird[name", text)
        self.assertNotIn("weird\\[name", text, "不得在本层预先转义")

    def test_module_does_not_import_tui(self) -> None:
        """
        架构护栏：`subagents` 不依赖 `tui`。

        引入那个 import 会让本包从「纯逻辑 + 运行时」变成依赖界面层，
        破坏「上层可依赖下层，反之不可」。
        """
        source = Path("rhinecode/subagents/report.py").read_text(encoding="utf-8")
        self.assertNotIn("from rhinecode.tui", source)


class LocationSectionTest(unittest.TestCase):
    def test_directories_and_reload_hint(self) -> None:
        text = _render(
            AgentCatalog(),
            project_dir="/proj/.rhinecode/agents",
            user_dir="/home/u/.rhinecode/agents",
        )
        self.assertIn("/proj/.rhinecode/agents", text)
        self.assertIn("/home/u/.rhinecode/agents", text)
        self.assertIn("需重启生效", text)


if __name__ == "__main__":
    unittest.main()
