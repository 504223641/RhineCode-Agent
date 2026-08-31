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


# `/agents` 任务段最多列几行（C10-b）。
#
# 与 `tasks.ACTIVITY_RECENT_LIMIT` **刻意分开**：那个管的是活动区展开时每个队员
# 列几次工具调用（一个很窄的视图，5 条正好），这里管的是「本段对话发起过哪些
# 委派」——一次十几步的任务派出七八个队员是正常的，卡到 5 会把用户正在找的那条
# 挡掉。两个数字合一之后，调其中一个必然把另一个调坏。
TASK_LIST_LIMIT = 15


def _task_section(
    tasks: tuple[TaskRecord, ...], current_epoch: Optional[int]
) -> list[str]:
    """
    渲染 `/agents` 的任务段（C10-b）。

    :param tasks: 全部任务记录（含 `/clear` 之前的）
    :param current_epoch: 当前会话代号；None 表示调用方没提供，退回旧行为（列全部）
    :returns: 该段的行列表（末尾带一个空行）

    ## 这一段治的是什么

    R3 实测：`begin_session`（`/clear` 走它）只做 `self._epoch += 1`，让旧记录
    **不再被交付**，但记录本身一条不少、全文件里一处删除都没有。于是：

    - **`/agents` 的输出无界，且含已清空会话的任务。** 一次跑过 60 次委派的会话
      会刷出 60 行，其中大半来自 `/clear` 之前——而用户敲 `/agents` 想知道的是
      「**现在**有谁在跑」。
    - 对照同文件的 `recent_tools` 是有界的（`[-ACTIVITY_RECENT_LIMIT:]`）：
      **「一切展示都有界」这条只落到了列表层，没落到字典层。**

    ## ⚠ 为什么只治展示、不治存储

    R3 的建议是「先只治展示」，理由是删记录有三条约束（运行中的一条都不能删、
    终态但未交付的不能删、删了会让完成通知失去成本数字），而它换来的只有约
    3 KB/次委派的内存——**不值得为它引入一个新的成对维护点**。本函数一条记录
    都不删。

    ## ⚠ 运行中的任务永不被裁掉

    截断只作用在**终态**任务上。用户敲 `/agents` 的第一诉求就是「还有谁在跑」，
    把正在跑的那条截掉等于把这个命令最有用的部分砍了——而且**不报错**。

    副作用：无（纯函数）。
    """
    if not tasks:
        return ["本次运行尚未发起过委派。", ""]

    if current_epoch is None:
        # 调用方没给代号：退回旧行为，一条不筛。保留这一支是为了让
        # 既有的直接调用（测试、将来别的入口）不必被迫改签名。
        current, earlier = list(tasks), []
    else:
        current = [t for t in tasks if t.epoch == current_epoch]
        earlier = [t for t in tasks if t.epoch != current_epoch]

    lines: list[str] = []
    if current:
        # 终态的才参与截断；运行中的一条不少地全列出来。
        done = [t for t in current if t.status.is_terminal]
        hidden = max(0, len(done) - TASK_LIST_LIMIT)
        if hidden:
            # 只在真的超了才重排：留最近 TASK_LIST_LIMIT 条终态的，
            # 加上全部运行中的。⚠ 用集合成员判断而不是「running + done[-N:]」，
            # 为的是**保持创建顺序**——后者会把所有运行中的提到最前面，于是同一份
            # 列表在超限前后顺序不一样，用户会以为任务被重排了。
            keep = {id(t) for t in done[-TASK_LIST_LIMIT:]}
            shown = [t for t in current if not t.status.is_terminal or id(t) in keep]
        else:
            shown = current

        lines.append(f"本次对话的任务（{len(current)} 个）：")
        lines.extend(_task_line(record) for record in shown)
        if hidden:
            lines.append(f"  （另有 {hidden} 条已结束的任务未列出）")
    else:
        lines.append("本次对话尚未发起过委派。")

    if earlier:
        # ⚠ 一行汇总，不逐条列。它们已经不会再交付了（代号对不上），
        # 用户此刻要的是「现在」，但完全不提又会让「我明明派过活」显得可疑。
        lines.append(f"另有 {len(earlier)} 条属于此前的会话，已不再交付。")

    lines.append("")
    return lines


def render_report(
    catalog: AgentCatalog,
    tasks: tuple[TaskRecord, ...],
    tools_for: Callable[[AgentSpec], str],
    effective_mode_for: Callable[[AgentSpec], PermissionMode],
    project_dir: Optional[str] = None,
    user_dir: Optional[str] = None,
    current_epoch: Optional[int] = None,
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
    :param current_epoch: 当前会话代号（C10-b）。给了就只列本段对话的任务、
        对更早的给一行汇总；不给则退回旧行为（列全部），见 `_task_section`
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
    lines.extend(_task_section(tasks, current_epoch))

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
