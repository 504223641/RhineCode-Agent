"""
隔离工作区的启动清理（c14 T10，spec F19/F21）。

**只在启动时扫一次，不做后台定时器，也不提供手动命令。** 这是 spec 明确定下的：

- 后台定时器会引入本项目**第一个周期性后台线程**，且清理与运行中的子 Agent
  存在竞态（清理线程判断「这个工作区过期且干净」的那一刻，一个子 Agent 可能
  正要往里写文件）。启动时清理则**竞态从根上不存在**——那一刻不可能有子 Agent
  在跑。
- 收益差距很小：一次会话通常委派几次，攒垃圾的速度不快，下次启动清掉够用。

⚠ **本模块对任何异常 fail-safe。** 清理失败绝不能阻断启动——它是个空间回收的
增强项，而用户是来干活的。每个条目的处理都被单独包住，一个坏条目不影响其余。

**它删得掉东西吗？** 能——但只删两种：① 子 Agent 结束时因崩溃/被杀而没走完
删除流程留下的残骸；② 有提交、无未提交改动的工作区（删目录、**留分支**，
commit 在共享版本库里，零数据损失）。有未提交改动的一律不碰。
"""

from __future__ import annotations

import os
import time
from pathlib import Path

from rhinecode.worktree import gitcmd, lifecycle
from rhinecode.worktree.models import (
    BRANCH_PREFIX,
    ChangeStatus,
    CleanupReport,
    WorktreeHandle,
)

# 计算「目录内文件最近修改时间」时跳过的目录名。
#
# 跳过 `.git` 的理由：隔离工作区里的 `.git` 是个指针**文件**不是目录，
# 本来就不会进 os.walk 的 dirs；但嵌套名字下可能出现别的形态，
# 而且用户手工放进来的目录也会被扫到。统一跳过更稳。
_SKIP_DIRS = {".git", "__pycache__", "node_modules", ".venv", "venv"}


def _latest_mtime(path: Path) -> float:
    """
    取一个目录内**文件**的最近修改时间（spec F19 的过期依据）。

    :param path: 目录
    :returns: Unix 时间戳；目录内没有任何文件时退回目录自身的 mtime

    为什么用「目录内文件的最近修改时间」而不是目录的创建时间：一个持续被用的
    工作区不该被判过期。用创建时间的话，一个连续用了十天的工作区在第八天
    就会被当成垃圾。

    遍历失败（权限、路径过长）时返回当前时间，即**判定为「不过期」**——
    fail-safe 的方向是「不删」。
    """
    latest = 0.0
    try:
        for root, dirs, files in os.walk(path):
            dirs[:] = [d for d in dirs if d not in _SKIP_DIRS]
            for name in files:
                try:
                    mtime = (Path(root) / name).stat().st_mtime
                except OSError:
                    continue
                if mtime > latest:
                    latest = mtime
    except OSError:
        return time.time()

    if latest == 0.0:
        try:
            return path.stat().st_mtime
        except OSError:
            return time.time()
    return latest


def _iter_candidates(root: Path) -> list[Path]:
    """
    列出隔离工作区根目录下的候选条目。

    :returns: 直接子目录列表；根目录不存在时返回空

    ⚠ **只看直接子目录**。嵌套名字（如 `a/b`）会让真正的工作区在第二层，
    此时第一层的 `a` 不是工作区、`judge_removal` 的第②层（归属）会把它挡下，
    结果是它被记进 `kept` 而不是被误删——这是可接受的行为：留一个空壳目录
    比递归下去误删要安全得多。
    """
    try:
        if not root.is_dir():
            return []
        return sorted(p for p in root.iterdir() if p.is_dir())
    except OSError:
        return []


def _guess_branch(main_root: Path, name: str) -> str:
    """
    猜一个残留工作区对应的分支名。

    :returns: 存在的分支名；猜不出时返回空串

    清理面对的是**上次运行留下的**目录，句柄早就没了，分支名只能猜。
    先试 `agent/<名字>`，再试带后缀的形式（创建时冲突改名会产生它们）。

    猜不出来时返回空串，`remove` 会跳过分支删除——后果只是留下一个孤立分支，
    不丢任何数据。这比猜错删掉别人的分支好得多，所以**只接受精确命中**。
    """
    base = f"{BRANCH_PREFIX}{name}"
    try:
        if gitcmd.branch_exists(main_root, base):
            return base
        for suffix in range(2, 10):
            candidate = f"{base}-{suffix}"
            if gitcmd.branch_exists(main_root, candidate):
                return candidate
    except Exception:  # noqa: BLE001 —— 猜不出就算了，绝不因此中断清理
        return ""
    return ""


def _relative_name(root: Path, path: Path) -> str:
    """把候选目录转成相对隔离工作区根的名字，用于展示。"""
    try:
        return str(path.relative_to(root)).replace("\\", "/")
    except ValueError:
        return path.name


def scan_and_clean(main_root: Path, max_age_days: int) -> CleanupReport:
    """
    扫描隔离工作区目录一次，清理过期条目（spec F19/F21）。

    :param main_root: 主项目根
    :param max_age_days: 过期阈值（天）。小于等于 0 时**不清理任何东西**
                         （视为用户关闭了本功能）
    :returns: `CleanupReport`

    执行流程：

    1. 根目录不存在 → 返回空报告
    2. 逐个直接子目录：算「目录内文件最近修改时间」，未过期则跳过（不计入报告）
    3. 过期项：`lifecycle.inspect` 拿变更状态 → `lifecycle.remove` 走三层过滤
    4. 按结论填 `removed`（含保留的分支名）或 `kept`（含中文原因）

    ⚠ **整个函数不抛异常。** 每个条目单独 try 住，最外层再兜一次。
    清理失败只会让某个条目进 `kept`，绝不阻断启动。

    副作用：可能删除磁盘目录、修改版本库登记、删除分支。
    """
    root = lifecycle.worktrees_root(Path(main_root))
    removed: list[tuple[str, str]] = []
    kept: list[tuple[str, str]] = []
    scanned = 0

    if max_age_days is None or max_age_days <= 0:
        return CleanupReport()

    try:
        candidates = _iter_candidates(root)
    except Exception:  # noqa: BLE001
        return CleanupReport()

    cutoff = time.time() - max_age_days * 86400

    for candidate in candidates:
        name = _relative_name(root, candidate)
        try:
            if _latest_mtime(candidate) >= cutoff:
                # 未过期：连报告都不进，避免每次启动刷一堆「这个还新鲜」。
                continue

            scanned += 1
            branch = _guess_branch(Path(main_root), name)
            handle = WorktreeHandle(
                name=name,
                path=candidate,
                branch=branch,
                # 基点未知（上次运行的句柄早没了）。`inspect` 在基点为空时
                # 按「无新增提交」处理，于是有提交的工作区会被判成
                # keep_branch=False……这是不可接受的，所以下面单独兜。
                base_commit="",
            )
            status = lifecycle.inspect(handle)

            # ⚠ **基点必须在这里补算出来。**
            #
            # `handle.base_commit` 是空的（上次运行的句柄早没了），于是
            # `inspect` 里的 `commits_since` 恒返回 0。若就这么用，
            # 「有提交的工作区」会被判成 `keep_branch=False`，**连分支一起删掉**
            # ——那正是数据丢失。
            #
            # `merge-base` 能把分叉点算回来（见 `gitcmd.commits_on_branch`）。
            # 算不出来时它返回 -1，这里按「有提交」处理：宁可留一个多余的分支，
            # 不可丢成果（spec N5）。
            if branch:
                counted = gitcmd.commits_on_branch(Path(main_root), branch)
                effective = 1 if counted < 0 else counted
                status = ChangeStatus(
                    dirty=status.dirty,
                    commits=effective,
                    files=status.files,
                )

            verdict = lifecycle.remove(Path(main_root), candidate, branch, status)
            if verdict.allowed:
                removed.append((name, branch if verdict.keep_branch else ""))
            else:
                kept.append((name, verdict.reason))
        except Exception as exc:  # noqa: BLE001 —— 一个坏条目不影响其余
            kept.append((name, f"处理时出错，已跳过（{exc}）"))

    return CleanupReport(
        removed=tuple(removed), kept=tuple(kept), scanned=scanned
    )


__all__ = ["scan_and_clean"]
