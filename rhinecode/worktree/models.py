"""
隔离工作区（Git worktree）的数据结构、异常类型与常量表（c14 T1）。

本模块是 `worktree` 包的最底层：只定义「一个隔离工作区长什么样」「一次操作的结果
长什么样」，**不做任何 IO、不依赖包内其它模块**。git 交互在 `gitcmd.py`，
生命周期在 `lifecycle.py`。

术语（第一次接触本章时先看这里）：

- **主项目根**：进程启动时的当前工作目录，也是主 Agent 干活的地方。
- **隔离工作区**：`<主项目根>/.rhinecode/worktrees/<名字>` 下的一个 Git 工作目录。
  它由 `git worktree add` 建立，与主项目根**共享同一个版本库**，但各有独立的
  HEAD、分支、暂存区与工作区文件。

  为什么共享版本库不占空间：实测隔离工作区里的 `.git` 不是目录，而是一个
  **51 字节的文本文件**，内容形如 `gitdir: <主仓库>/.git/worktrees/<名字>`。
  所有 commit 对象仍然只有主仓库那一份，开销全在源码 checkout 上。
  这条实测结论同时是 `lifecycle` 里「快速恢复不调 git」的物理依据——
  「这个目录是不是本仓库的工作目录」就写在那个文件里，读它即可确认。

对应 spec 条款见 `docs/c14/spec.md`：F6（位置）、F7（名字）、F8（分支与基点）、
F10（环境初始化）、F16（结束决策）、F20/F21（三层过滤与分支保留）、F19（清理）。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

# 隔离工作区所在目录名，完整位置是 `<主项目根>/.rhinecode/<WORKTREES_DIR_NAME>/`。
# 放在 `.rhinecode/` 下与 c8 的 context、c9 的 sessions/memory、trace 的 traces
# 同一层级，一并被 `.gitignore` 排除（spec N8）。
WORKTREES_DIR_NAME = "worktrees"

# `.rhinecode` 目录名。这里刻意重复一份字面量而不是从别处 import：
# 本包是叶子包，为一个字符串去依赖上层模块不划算，而这个名字属于产品级约定、
# 不会变。
RHINECODE_DIR_NAME = ".rhinecode"

# 隔离工作区分支名的前缀。加前缀是为了让 `git branch` 一眼能看出哪些分支
# 是子 Agent 产出的，也避免与人类手写的分支名撞车。
BRANCH_PREFIX = "agent/"

# 名字总长上限（spec F7）。与 Claude Code 的 worktree 名字上限一致——
# 从那个生态复制过来的名字应当在这里同样合法。
MAX_NAME_LENGTH = 64

# 分支名冲突时的改名尝试上限。超过就放弃并报错，而不是无限试下去：
# 真撞上 50 个同名分支说明环境有问题，静默试到第 500 次只会让人更晚发现。
MAX_BRANCH_SUFFIX = 50

# 启动清理的过期阈值缺省值（天，spec F19）。可由 config.yaml 覆盖。
DEFAULT_CLEANUP_DAYS = 7


# ---------------------------------------------------------------------------
# 异常
# ---------------------------------------------------------------------------


class WorktreeError(Exception):
    """
    本包全部失败的基类。

    调用方（`subagents/service.py`）只需捕获这一个类型，就能把任何创建失败
    转成一次**明确的委派失败**（spec F12：不降级为无隔离运行）。
    子类型的存在是为了让错误消息能说清「具体是哪一种失败」——用户看到
    「当前目录不是 Git 仓库」才知道该怎么办，看到「创建失败」则不知道。
    """


class WorktreeNameError(WorktreeError):
    """名字未通过安全校验（spec F7）。消息里必须写明具体违反了哪一条。"""


class GitUnavailable(WorktreeError):
    """机器上找不到可执行的 git。"""


class NotARepository(WorktreeError):
    """当前目录不是一个 Git 仓库（在任意目录启动 rhine 时的常见情形）。"""


class GitCommandFailed(WorktreeError):
    """
    一条 git 命令返回了非零退出码。

    :param message: 可读描述（中文，含出错的操作是什么）
    :param stderr: git 自己的错误输出原文，排错时需要它
    """

    def __init__(self, message: str, stderr: str = "") -> None:
        super().__init__(message if not stderr else f"{message}：{stderr.strip()}")
        self.stderr = stderr


# ---------------------------------------------------------------------------
# 数据结构
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class WorktreeHandle:
    """
    一个**已就绪**的隔离工作区（spec F6/F8/F9）。

    创建成功与快速恢复都产出它，调用方不需要区分两者——`recovered` 只用于观测。

    frozen=True：句柄一旦产出就不该被改写。它会被交给在**独立线程**里跑的子
    Agent，不可变让这次跨线程传递不需要任何同步。

    :param name: 归一化后的名字，可含 `/`（嵌套）。已过 `naming.validate_name`。
    :param path: 隔离工作区的绝对路径。
    :param branch: **实际**使用的分支名。与「期望的分支名」可能不同——
        撞上已存在的分支时会自动追加 `-2`、`-3`……
        ⚠ 交付信息里必须报告这个值而不是期望值（spec F17），否则主 Agent
        会拿着一个不存在的分支名去合并。
    :param base_commit: 创建时的基点提交短哈希，即当时主项目根的 HEAD。
        它是「子 Agent 改了什么」的可指认起点，也是 `commits_since` 的参数。
    :param recovered: True 表示走了快速恢复（目录本来就在），**未调用任何 git 命令**。
    """

    name: str
    path: Path
    branch: str
    base_commit: str
    recovered: bool = False


@dataclass(frozen=True)
class ChangeStatus:
    """
    一个隔离工作区的变更状态（spec F16/F20③）。

    **结束决策与三层过滤第③层共用同一份实现**，避免两处口径漂移——
    「结束时判定为无变更所以删掉」和「清理时判定为有变更所以不删」如果用两套
    逻辑，迟早会出现互相矛盾的结论。

    :param dirty: 有未提交的工作区改动（**含未跟踪文件**）。

        ⚠ 这一项是删除的**无条件否决**：未提交的改动只存在于那个目录里，
        删掉就永久丢失，而已提交的内容删掉目录也还在版本库中（spec N5）。
    :param commits: 相对 `base_commit` 的新增提交数。
    :param files: 变更文件清单（未提交 + 已提交，合并去重后按字典序）。
    """

    dirty: bool
    commits: int
    files: tuple[str, ...] = ()

    @property
    def untouched(self) -> bool:
        """
        子 Agent 什么都没改（spec F16 的自动删除条件）。

        :returns: 既无未提交改动、也无新增提交时为 True
        """
        return not self.dirty and self.commits == 0


@dataclass(frozen=True)
class ProvisionEntry:
    """
    一条环境初始化条目（spec F10）。

    存在的理由：隔离工作区是 `git checkout` 出来的，**凡被忽略规则排除的文件
    一概没有**（实测本仓库的隔离工作区里缺 `.rhinecode/` 与 `config.yaml`）。
    有些这类文件是运行必需的，要显式补进去。

    :param source: 相对**主项目根**的来源路径。绝对路径与越界路径会在
        `provision` 里被跳过并记警告——清单来自配置文件，写错了不该让创建失败。
    :param mode: `"copy"` 或 `"link"`。

        两者的区别对用户是有意义的选择：**配置文件要 copy**（子 Agent 改坏了
        不牵连主目录），**大型依赖目录要 link**（500MB 复制三份是灾难）。
    """

    source: str
    mode: str


@dataclass(frozen=True)
class ProvisionResult:
    """
    一次环境初始化的结果（spec F10）。

    ⚠ **本结构没有「失败」态**：单条条目失败只记警告、不阻断创建，
    整体初始化也从不导致创建失败。理由是清单来自用户配置，一条写错了
    不该让整个委派挂掉——但必须让用户看得见，所以警告要具体到条目。

    :param applied: 实际生效的条目来源路径
    :param warnings: 跳过或降级的说明（中文，直接展示）
    """

    applied: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True)
class RemovalVerdict:
    """
    三层过滤的结论（spec F20/F21）。**唯一的删除许可来源。**

    `lifecycle.judge_removal` 是纯判定、零副作用，因此这个结论可以被单测穷举；
    `lifecycle.remove` 拿不到 `allowed=True` 就一步都不往下走。

    :param allowed: 是否准许删除
    :param keep_branch: 准许删除时，是否**保留分支**。

        含新增提交时为 True：删的是工作目录，commit 在共享版本库里完好无损，
        `git checkout <branch>` 随时能取回（spec F21）。这正是清理能真正
        回收空间、又不丢数据的原因。
    :param reason: 中文原因。**拒绝时必填**——启动报告要逐条告诉用户
        「这个为什么没删」，否则用户只会看到目录还在而不知道为什么。
    """

    allowed: bool
    keep_branch: bool = False
    reason: str = ""


@dataclass(frozen=True)
class CleanupReport:
    """
    一次启动清理的产物（spec F19/F21）。

    :param removed: 被删除的条目，每项是 `(名字, 保留的分支名)`。
        分支名为空串表示分支也一并删了（该工作区没有任何提交）。
        **非空时必须展示给用户**——他的成果还在那个分支上，不说他会以为丢了。
    :param kept: 未被删除的条目，每项是 `(名字, 中文原因)`。
    :param scanned: 本次扫描考察过的条目数，用于「扫了但一个都没动」时的说明。
    """

    removed: tuple[tuple[str, str], ...] = ()
    kept: tuple[tuple[str, str], ...] = ()
    scanned: int = 0

    @property
    def is_empty(self) -> bool:
        """没有任何值得向用户汇报的内容时为 True（不弹启动提示）。"""
        return not self.removed and not self.kept


__all__ = [
    "WORKTREES_DIR_NAME",
    "RHINECODE_DIR_NAME",
    "BRANCH_PREFIX",
    "MAX_NAME_LENGTH",
    "MAX_BRANCH_SUFFIX",
    "DEFAULT_CLEANUP_DAYS",
    "WorktreeError",
    "WorktreeNameError",
    "GitUnavailable",
    "NotARepository",
    "GitCommandFailed",
    "WorktreeHandle",
    "ChangeStatus",
    "ProvisionEntry",
    "ProvisionResult",
    "RemovalVerdict",
    "CleanupReport",
]
