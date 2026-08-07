"""
c14 隔离工作区测试的共用脚手架。

本模块只提供「造一个临时 Git 仓库」和「安全地删掉它」两件事，不含任何断言——
断言留在各测试文件里，这样每条护栏读起来是自解释的。

⚠ **清理一律走 `tests/e2e/sandbox.py` 的 `force_rmtree`。**
直接写 `shutil.rmtree(path, ignore_errors=True)` 撞上 git 留下的**只读**
`.git/objects` 会「删一半且一个错都不报」，留下一个只剩空 `.git` 的残骸。
实测只在全量测试的并发负载下出现，单跑那条用例必成功，追起来极费劲。
"""

from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path

from tests.e2e.sandbox import MARKER, force_rmtree


def git(args: list[str], cwd: Path) -> str:
    """
    在给定目录执行一条 git 命令，返回标准输出。

    :raises AssertionError: 命令失败（测试里失败就该当场炸，不要静默）
    """
    done = subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        shell=False,
    )
    assert done.returncode == 0, f"git {' '.join(args)} 失败: {done.stderr}"
    return done.stdout


def make_repo(prefix: str = "c14-") -> Path:
    """
    造一个带一次初始提交的临时 Git 仓库。

    :returns: 仓库根目录的绝对路径

    为什么必须有初始提交：`git worktree add` 需要一个基点，空仓库里
    `rev-parse HEAD` 会失败。

    为什么要显式配 user.name / user.email：CI 或干净的开发机上可能没有全局配置，
    此时 `git commit` 会失败——而失败信息是英文的 git 报错，
    看起来像本章的 bug 而不是环境问题。

    副作用：在系统临时目录下创建一个目录并跑几条 git 命令。
    调用方负责用 `force_rmtree` 清理。
    """
    root = Path(tempfile.mkdtemp(prefix=prefix)).resolve()

    # ⚠ 标记文件必须落下：`force_rmtree` 的可丢弃校验（判据②）认它。
    # 那道闸门是一次真实事故（空变量 rmtree 删掉整个代码仓库）之后加的，
    # **不要为了让用例跑通去绕过它**——正确做法就是像这里一样把标记补上。
    #
    # 在初始提交**之前**写它，于是它会被一并提交进去：这样隔离工作区里也有
    # 一份，主仓库与工作区的 `git status` 都保持干净，不会干扰变更检测的用例。
    root.joinpath(MARKER).write_text(
        "role=c14-worktree-test\n", encoding="utf-8"
    )

    git(["init", "-b", "main"], root)
    git(["config", "user.name", "c14 test"], root)
    git(["config", "user.email", "c14@example.invalid"], root)
    git(["config", "commit.gpgsign", "false"], root)
    (root / "seed.txt").write_text("seed\n", encoding="utf-8")
    git(["add", "."], root)
    git(["commit", "-m", "init"], root)
    return root


def make_plain_dir(prefix: str = "c14-plain-") -> Path:
    """
    造一个**不是 Git 仓库**的临时目录（已落好可丢弃标记）。

    用于验「在任意目录启动 rhine」这个常见情形：非 git 环境下，
    声明了隔离的角色必须**明确失败**而不是降级（spec F12 / AC16）。

    :returns: 目录的绝对路径。调用方负责用 `cleanup` 清理。
    """
    root = Path(tempfile.mkdtemp(prefix=prefix)).resolve()
    root.joinpath(MARKER).write_text("role=c14-plain\n", encoding="utf-8")
    return root


def commit_all(root: Path, message: str) -> None:
    """在给定工作目录里提交当前全部改动（含未跟踪文件）。"""
    git(["add", "-A"], root)
    git(["commit", "-m", message], root)


def cleanup(root: Path) -> None:
    """删掉一个由 `make_repo` 造出来的仓库（含它下面的全部隔离工作区）。"""
    force_rmtree(root)


__all__ = ["git", "make_repo", "commit_all", "cleanup"]
