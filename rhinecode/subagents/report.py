"""
`/agents` 报告（c13 T20，spec F24）。

**职责**：把角色目录与任务表渲染成一段给用户看的文本。纯函数，只读。

## ⚠ 转义纪律：本模块**不转义**，由 TUI 调用方转义

与 `hooks/report.py` 同口径。产出是**纯文本**，`HistoryView.append_system`
会对整段走一次 `tui/widgets.py` 的 `escape`。这里再转一次就是双重转义，
用户会看到字面的 `\[`。

两条连带纪律：

1. **本模块绝不 import `rhinecode.tui`**。`subagents` 不依赖 `tui` / `commands` /
   `conversation`，这是包的架构不变量（见 `__init__.py`）。
2. 调用方**必须**用 `tui/widgets.py` 的 `escape`，**绝不能**用
   `rich.markup.escape`——后者只转义「看起来像完整标签」的 `[...]`，
   被截断的括号会被它整个放过，而 Textual 会把落单的 `[` 当标签开头、
   在**布局阶段的主线程**抛 `MarkupError`，没有任何 try/except 兜得住。

本模块的高危字段是**结论文本**：它来自模型输出，里面出现 `[` 是家常便饭。
"""

from __future__ import annotations

from typing import Callable, Optional

from rhinecode.permission.models import PermissionMode
from rhinecode.subagents.models import (
    SOURCE_LABELS,
    UNSUPPORTED_FIELDS,
    AgentCatalog,
    AgentSpec,
)
from rhinecode.subagents.tasks import STATUS_LABELS, TaskRecord

# 档位显示名（auto-plan 扩展起收在 `presets.MODE_LABELS`，本文件不再自留一份）。
#
# 合一的理由：`permissive` 现在对用户显示成 `auto`（它就是 `auto` 预设内部的档位），
# 各留一份的话同一个档位在两处叫两个名字，而用户没法确认那是不是同一个东西。
#
# ⚠ 这与 `CLAUDE.md` 里「`Layer` 的三份标签表刻意不合一」不是同一回事：
# 那三份不合一是**架构约束**（合并会让只依赖标准库的 `trace` 叶子包反向依赖
# `permission`）。这里没有那个约束——`subagents` 本来就依赖 `permission`，
# 而 `presets` 是比两者都低的叶子模块。
from rhinecode.presets import MODE_LABELS as _MODE_LABELS

# 结论在列表里只显示首行的前若干字符，全文靠模型那边的交付。
_CONCLUSION_PREVIEW = 60


def _mode_text(spec: AgentSpec, effective: PermissionMode) -> str:
    """
    渲染「声明值 → 实际生效值」（spec F16 要求两者都可见）。

    只显示生效值是不够的：用户写了 `permission_mode: permissive` 却看到「默认」，
    会以为配置没读到。两个都摆出来，他才知道是被主对话档位夹住了。
    """
    declared = (
        _MODE_LABELS.get(spec.permission_mode, spec.permission_mode.value)
        if spec.permission_mode is not None
        else "继承"
    )
    actual = _MODE_LABELS.get(effective, effective.value)
    if spec.permission_mode is not None and spec.permission_mode is not effective:
        return f"{declared} → 实际 {actual}（受主对话档位限制）"
    return f"{declared} → 实际 {actual}"


def _agent_block(
    spec: AgentSpec,
    tools_text: str,
    effective: PermissionMode,
) -> list[str]:
    """渲染单个角色的展示块。"""
    lines = [
        f"  • {spec.name}"
        f"（{SOURCE_LABELS.get(spec.source, spec.source.value)}）",
        f"    说明：{spec.description}",
        f"    工具：{tools_text}",
        f"    模型：{spec.model or '继承主对话'}"
        f" · 轮次上限：{spec.max_turns}"
        f" · 权限：{_mode_text(spec, effective)}",
    ]
    # c14 F23：只在声明了隔离时显示这一行。
    # 未声明的角色不显示「隔离：无」——绝大多数角色都不隔离，
    # 给每个都加一行只会把真正重要的信息（说明与工具集）挤下去。
    if spec.isolation:
        lines.append(
            f"    隔离：{spec.isolation}"
            "（每次委派在独立的 Git 工作目录中运行，成果经分支交付）"
        )
    for warning in spec.warnings:
        lines.append(f"    警告：{warning}")
    return lines


def _task_line(record: TaskRecord) -> str:
    """渲染单条任务。"""
    # c15：有队员名字时显示 `名字(角色)`，没有则退回只显示角色。
    #
    # 名字排在**前面**：用户要拿它去理解「谁在干什么」，而同一个角色可能
    # 派出了三个队员——只显示角色的话那三行看起来一模一样。
    who = (
        f"{record.member_name}({record.agent_name})"
        if record.member_name
        else record.agent_name
    )
    head = (
        f"  • {record.task_id} · {who}"
        f" · {STATUS_LABELS.get(record.status, record.status.value)}"
        f" · {record.turns} 轮 · {record.usage_tokens} token"
        f" · {record.duration_seconds:.1f}s"
    )
    # c14 F23：隔离任务多一行工作区信息。
    #
    # 非隔离任务这两个字段是空串，整行不出现——`/agents` 里绝大多数任务都不隔离，
    # 无条件加一行「隔离工作区：无」纯属噪音。
    #
    # 分支名排在路径**前面**：用户要拿它做 `git merge`，路径只是排查时才看。
    if record.worktree_path or record.worktree_branch:
        if record.worktree_removed:
            # ⚠ 已回收时**不能再报分支名与路径**（c14 修正，真实模型实测撞到）。
            #
            # 两个只读任务跑完即回收（无变更 → 目录与分支一并删，F16），
            # 而 `/agents` 仍原样展示「分支 agent/surveyor-xxx · 路径 …」。
            # 用户照着它去 `git checkout` 会拿到「分支不存在」，去看路径会发现
            # 目录没了——而任务行明明白白写着它们在。这与交付信息段刻意
            # 「不给已删分支的名字」是同一条理由，那边做对了、这边漏了。
            head += "\n    隔离工作区：已回收（无变更，目录与分支均已删除）"
        else:
            parts = []
            if record.worktree_branch:
                parts.append(f"分支 {record.worktree_branch}")
            if record.worktree_path:
                parts.append(f"路径 {record.worktree_path}")
            head += "\n    隔离工作区：" + " · ".join(parts)

    body = record.conclusion or record.task_text
    if body:
        preview = body.strip().splitlines()[0][:_CONCLUSION_PREVIEW]
        head += f"\n    {preview}"
    return head


def render_report(
    catalog: AgentCatalog,
    tasks: tuple[TaskRecord, ...],
    tools_for: Callable[[AgentSpec], str],
    effective_mode_for: Callable[[AgentSpec], PermissionMode],
    project_dir: Optional[str] = None,
    user_dir: Optional[str] = None,
) -> str:
    """
    渲染 `/agents` 的完整报告（spec F24）。

    :param catalog: 角色目录
    :param tasks: 本次运行的全部任务
    :param tools_for: 角色 → 最终工具集的可读描述（由协调层提供，它才知道
        当前注册了哪些工具）
    :param effective_mode_for: 角色 → 实际生效的权限档位
    :param project_dir: 项目级角色目录路径，展示在末尾
    :param user_dir: 用户级角色目录路径
    :returns: 多行文本

    分五段，**空段不出现**（无错误时不该有一个空的「加载错误」标题，
    那会让用户以为出了什么事）。

    副作用：无（纯函数）。
    """
    lines: list[str] = ["子 Agent 角色与任务", ""]

    # ── 角色段 ──
    if catalog.specs:
        lines.append(f"已加载角色（{len(catalog.specs)} 个）：")
        for spec in catalog.specs.values():
            lines.extend(
                _agent_block(spec, tools_for(spec), effective_mode_for(spec))
            )
    else:
        lines.append("尚未加载任何角色。")
        lines.append(
            "  在项目的 .rhinecode/agents/ 或用户级 ~/.rhinecode/agents/ 下"
            "放一个 .md 文件即可（frontmatter 只有 description 是必填的）。"
        )
    lines.append("")

    # ── 加载错误段 ──
    if catalog.errors:
        lines.append(f"加载错误（{len(catalog.errors)} 条）：")
        for err in catalog.errors:
            lines.append(
                f"  • {err.path}"
                f"（{SOURCE_LABELS.get(err.source, err.source.value)}）"
                f"：{err.message}"
            )
        lines.append("")

    # ── 被覆盖段 ──
    if catalog.shadowed:
        lines.append(f"未生效的定义（{len(catalog.shadowed)} 条）：")
        for item in catalog.shadowed:
            winner = SOURCE_LABELS.get(item.winner_source, item.winner_source.value)
            lines.append(
                f"  • {item.name} @ {item.path}"
                f"（{SOURCE_LABELS.get(item.source, item.source.value)}）"
                f"—— 生效的是{winner}那份"
            )
        lines.append("")

    # ── 任务段 ──
    if tasks:
        lines.append(f"本次运行的任务（{len(tasks)} 个）：")
        for record in tasks:
            lines.append(_task_line(record))
    else:
        lines.append("本次运行尚未发起过委派。")
    lines.append("")

    # ── 位置与提示 ──
    if project_dir:
        lines.append(f"项目级目录：{project_dir}")
    if user_dir:
        lines.append(f"用户级目录：{user_dir}")
    lines.append("改动角色定义后需重启生效（本章不提供 reload）。")

    if any(spec.warnings for spec in catalog.specs.values()):
        lines.append(
            f"未支持的 frontmatter 字段共 {len(UNSUPPORTED_FIELDS)} 个，"
            "它们只被忽略、不影响角色可用性。"
        )

    return "\n".join(lines)


__all__ = ["render_report"]
