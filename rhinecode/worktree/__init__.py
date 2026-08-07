"""
隔离工作区（Git worktree）子系统（c14）。

**一句话**：给子 Agent 开一个独立的 Git 工作目录，让它和主 Agent 同时改文件而
不互相覆盖。

本包是**叶子包**：只依赖标准库与 `tools.path_guard`（复用路径安全校验），
不依赖 `subagents` / `agent` / `conversation` / `tui` / `permission`。
反过来 `tools` 侧也不 import 本包——工具拿到的是一个 `Path` 参数而不是对象，
因此不构成包级互依，`tools/__init__.py` 保持为空的既有不变量不受影响。

模块分工：

| 模块 | 职责 |
| --- | --- |
| `models` | 数据结构、异常、常量。零 IO |
| `naming` | 名字安全校验与生成。**纯函数零 IO** |
| `gitcmd` | **全项目唯一执行 git 子进程的地方** |
| `provision` | 环境初始化（复制 / 软链） |
| `lifecycle` | 创建（含快速恢复）/ 检查 / 三层过滤 / 删除 |
| `cleanup` | 启动时扫描清理一次 |
| `render` | 交付信息段的文本渲染 |

⚠ **`lifecycle.remove` 是本包唯一的删除入口。** 将来任何第二条删除路径都必须
走它，否则 spec F20 的三层过滤形同虚设。CLAUDE.md 里那条「曾因空变量 rmtree
删掉整个仓库」的教训由 `judge_removal` 的第①层位置校验兜住。

设计文档见 `docs/c14/`。
"""

from rhinecode.worktree.cleanup import scan_and_clean
from rhinecode.worktree.lifecycle import create, inspect, judge_removal, remove
from rhinecode.worktree.models import (
    BRANCH_PREFIX,
    DEFAULT_CLEANUP_DAYS,
    MAX_NAME_LENGTH,
    WORKTREES_DIR_NAME,
    ChangeStatus,
    CleanupReport,
    GitCommandFailed,
    GitUnavailable,
    NotARepository,
    ProvisionEntry,
    ProvisionResult,
    RemovalVerdict,
    WorktreeError,
    WorktreeHandle,
    WorktreeNameError,
)
from rhinecode.worktree.naming import generate_name, validate_name
from rhinecode.worktree.render import render_delivery

__all__ = [
    # 生命周期
    "create",
    "inspect",
    "judge_removal",
    "remove",
    "scan_and_clean",
    # 渲染
    "render_delivery",
    # 名字
    "validate_name",
    "generate_name",
    # 数据结构
    "WorktreeHandle",
    "ChangeStatus",
    "ProvisionEntry",
    "ProvisionResult",
    "RemovalVerdict",
    "CleanupReport",
    # 异常
    "WorktreeError",
    "WorktreeNameError",
    "GitUnavailable",
    "NotARepository",
    "GitCommandFailed",
    # 常量
    "WORKTREES_DIR_NAME",
    "BRANCH_PREFIX",
    "MAX_NAME_LENGTH",
    "DEFAULT_CLEANUP_DAYS",
]
