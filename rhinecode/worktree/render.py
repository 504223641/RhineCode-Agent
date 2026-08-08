"""
交付信息段的文本渲染（c14 T10，spec F17）。

**为什么这段信息由系统给、不靠模型自述。**

C13 的结论回流机制是「取最后一条 assistant 消息的全文」。真实模型实测过一个
现象：告诉它「只有最后一段会被带回去」，它就会在结论前面写一堆过程叙述。
同一类可靠性问题在本章换了个形式——分支名如果靠模型自己写，它可能：

- 写成任务名而不是分支名
- 漏写
- 写成 `agent/xxx`，而实际分支是 `agent/xxx-2`（创建时撞名，系统改的）

而主 Agent 拿着这个名字去 `git merge`，错了会报「分支不存在」，
排查时谁也想不到根因在这。

代价近乎为零：运行器**本来就要**跑一遍 git 来决定「保留还是删除」
（spec F16），那份信息现成的，顺手渲染出来不多花一次 git 调用。

⚠ **本模块只产出这一段文本，不负责拼接。** 拼接在 `subagents/gate.py` 的
`render_subagent_message` 里——CLAUDE.md 有一条成对维护点要求「子 Agent 结论的
渲染只有一份」（闸门的迭代级交付与协调层的兜底交付共用它），本模块不能绕过它
自己往历史里塞东西。
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from rhinecode.worktree.models import ChangeStatus, CleanupReport, WorktreeHandle

# 变更文件清单最多列几项。超出部分折叠成「等 N 个文件」。
#
# 取 10 的理由：这段信息会**整段进入主对话的上下文**并占用它的预算
# （与 C13 对结论长度的约束同一考量）。主 Agent 需要的是「大致改了哪一块」，
# 真要看全部就去 `git diff`——交付信息段里已经给了分支名。
_MAX_FILES = 10


def _display_path(main_root: Optional[Path], path: Path) -> str:
    """
    把工作区路径转成便于阅读的形式。

    :returns: 能算出相对主项目根的路径时用相对形式，否则用绝对路径

    用相对形式是因为绝对路径又长又含用户名，塞进模型上下文纯属浪费；
    而相对形式（`.rhinecode/worktrees/fix-toolset`）本身就说明了它是什么。
    """
    if main_root is None:
        return str(path)
    try:
        return str(Path(path).resolve().relative_to(Path(main_root).resolve())).replace(
            "\\", "/"
        )
    except (ValueError, OSError):
        return str(path)


def render_delivery(
    handle: WorktreeHandle,
    status: ChangeStatus,
    main_root: Optional[Path] = None,
    removed: bool = False,
) -> str:
    """
    渲染一个隔离子 Agent 的交付信息段（spec F17）。

    :param handle: 工作区句柄。`branch` / `base_commit` 取的是**实际值**
    :param status: 变更状态，来自 `lifecycle.inspect`
    :param main_root: 主项目根，用于把路径显示成相对形式
    :param removed: 该工作区是否已被自动删除（无变更时会删，spec F16）
    :returns: 一段可直接追加到结论末尾的文本

    副作用：无（纯函数）。

    两种形态：

    - **已删除**（子 Agent 什么都没改）：只说明一句，不给分支名与路径——
      它们已经不存在了，给出来只会让主 Agent 去 merge 一个已删的分支。
    - **已保留**：给出分支名、基点、路径、提交数、改动文件、是否有未提交改动。
      主 Agent 后续的 `git merge` / `git diff` 全靠这几行。
    """
    if removed:
        return (
            "── 隔离工作区 ─────────────────────\n"
            "该子 Agent 未产生任何文件变更，其隔离工作区与分支已自动清理。"
        )

    lines = ["── 隔离工作区 ─────────────────────"]

    if handle.branch:
        base = f"（基于 {handle.base_commit}）" if handle.base_commit else ""
        lines.append(f"分支：{handle.branch}{base}")
    lines.append(f"路径：{_display_path(main_root, Path(handle.path))}")
    lines.append(f"提交：{status.commits} 个")

    if status.files:
        shown = list(status.files[:_MAX_FILES])
        rest = len(status.files) - len(shown)
        text = "、".join(shown)
        if rest > 0:
            text += f" 等 {len(status.files)} 个文件"
        lines.append(f"改动：{text}")
    else:
        lines.append("改动：无")

    lines.append(
        "未提交改动：有（这些改动只存在于该目录中，不会随分支合并带走）"
        if status.dirty
        else "未提交改动：无"
    )

    # ## ⚠ 「改了但一个提交都没有」要说重话（真实模型实测补的）
    #
    # 实测两次撞到同一条路：主 Agent 在任务描述里写下「不要执行 git commit」
    # （它是从用户那句「我这边的改动先不提交」推断出来的，很自然），子 Agent
    # 照办，于是成果全部搁浅在那个目录里、分支上 0 个提交。
    #
    # 而原先这一段只是**陈述事实**（「未提交改动：有」），主 Agent 的反应是
    # 把它当成正常结局报给用户「已完成，未提交，符合要求」——用户以为拿到了
    # 成果，实际什么都没交回来。第二次它自己钻进工作区目录逐个读 diff、
    # 再在主目录重打一遍：**隔离本该省下的上下文全吃回来了**，
    # 一次委派的价值当场归零。
    #
    # 所以这里必须由**系统**给出结论与下一步，而不是指望模型自己看出问题——
    # 与 F17「交付信息由系统追加而非模型自述」是同一条理由。
    if status.dirty and status.commits == 0:
        lines.append("")
        lines.append(
            "⚠ **这次委派的成果没有交回来**：分支上一个提交都没有，"
            "所有改动只留在上面那个目录里，`git merge` 拿不到任何东西。"
        )
        lines.append(
            "**不要自己进那个目录去逐个读文件、复制内容**——那会把隔离本该"
            "省下的上下文全部吃回来，这次委派就白做了。"
        )
        lines.append(
            "正确做法：重新委派一次，并在任务描述里明确要求它"
            "`git add` + `git commit`。**委派隔离任务时不要写「不要提交」**"
            "——隔离工作区里的提交不会进入主对话的工作现场，"
            "它是成果唯一的交付通道。"
        )

    return "\n".join(lines)


def render_cleanup_notice(report: CleanupReport) -> str:
    """
    渲染启动清理的结果，供首屏提示（spec F21）。

    :param report: 清理结果
    :returns: 多行文本；无内容时返回空串

    ⚠ **被删除条目的分支名必须列出来。**

    用户看到「目录没了」时，唯一能让他不慌的信息就是「成果还在 xxx 分支上」。
    不列的话，一个删掉了三个工作区的启动会让人以为丢了三份工作——
    而实际上一个提交都没少（删的只是工作目录，commit 在共享版本库里）。

    未删除的条目也要列**并说明原因**：它们占着磁盘却没被回收，用户有权知道
    为什么，否则下次还会问同样的问题。
    """
    if report.is_empty:
        return ""

    lines = ["隔离工作区清理："]
    for name, branch in report.removed:
        if branch:
            lines.append(f"- 已回收 {name}（成果保留在分支 {branch}，可 git checkout 取回）")
        else:
            lines.append(f"- 已回收 {name}（无任何变更，目录与分支一并删除）")
    for name, reason in report.kept:
        lines.append(f"- 保留 {name}：{reason}")
    return "\n".join(lines)


__all__ = ["render_delivery", "render_cleanup_notice"]
