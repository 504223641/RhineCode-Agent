"""
git 子进程封装（c14 T4）。

⚠ **本模块是全项目唯一执行 git 的地方。** 包外任何位置都不应出现 git 子进程
调用——收拢在这一处，「本章到底会执行哪些 git 操作」才能被穷举审计。

三条安全约定（spec 的安全边界一节据此论证）：

1. **`shell=False`，参数以列表传递。** 绝不拼接命令行字符串。因此即使名字里
   混进了 `;` 或 `&&`，它也只是一个普通的参数值，不可能被解释成第二条命令。
   （名字其实已经过 `naming.validate_name` 挡掉了这些字符，这里是纵深防御。）
2. **参数全部由代码构造。** 模型能影响的只有「名字」这一个值，而它已过校验。
3. **这些调用不经权限管线。** 与 c8 的工具结果存盘、c9 的记忆落盘同一先例：
   它们不是模型发起的工具调用，而是系统为兑现用户配置执行的内部操作。
   删除类操作另有 `lifecycle.judge_removal` 的三层过滤把关。

为什么用子进程而不是某个 Git 绑定库：本项目依赖清单里没有 Git 库，为本章引入
一个是不成比例的；而 `--porcelain` 系列输出**正是为机器解析设计**的、跨 git
版本稳定，这恰恰是我们需要的。
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from rhinecode.worktree.models import (
    GitCommandFailed,
    GitUnavailable,
    NotARepository,
)

# 单条 git 命令的超时（秒）。
#
# 取值理由：本模块跑的都是本地版本库操作（add / list / status / rev-list），
# 不涉及网络，正常在百毫秒量级。给 120 秒是为了容忍超大仓库上的
# `worktree add`（它要 checkout 一整份源码）与机械硬盘。
#
# ⚠ 超时**必须有**：没有它的话，一个卡住的 git 进程会让子 Agent 的后台线程
# 永久挂起，而界面上只显示「任务运行中」——用户按 Esc 也救不回来。
_TIMEOUT = 120


def _run(args: list[str], cwd: Path, *, timeout: int = _TIMEOUT) -> str:
    """
    执行一条 git 命令并返回其标准输出。

    :param args: `git` **之后**的参数列表（不含 `git` 本身）
    :param cwd: 命令的工作目录
    :param timeout: 超时秒数
    :returns: 标准输出原文（未去除首尾空白，由调用方按需处理）
    :raises GitUnavailable: 机器上找不到 git 可执行文件
    :raises GitCommandFailed: 返回码非零，或超时

    副作用：启动一个子进程。部分命令（`worktree add` / `branch -D` 等）
    会修改文件系统与版本库。
    """
    try:
        completed = subprocess.run(
            ["git", *args],
            cwd=str(cwd),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            # ⚠ 绝不改成 True：见模块 docstring 的安全约定第 1 条。
            shell=False,
            timeout=timeout,
        )
    except FileNotFoundError as exc:
        raise GitUnavailable("找不到可执行的 git，无法使用工作区隔离") from exc
    except subprocess.TimeoutExpired as exc:
        raise GitCommandFailed(
            f"git {' '.join(args)} 执行超时（{timeout} 秒）"
        ) from exc

    if completed.returncode != 0:
        raise GitCommandFailed(
            f"git {' '.join(args)} 失败", completed.stderr or ""
        )
    return completed.stdout


def ensure_repository(root: Path) -> None:
    """
    确认给定目录位于一个 Git 工作树内。

    :param root: 待确认的目录
    :raises NotARepository: 不是 Git 仓库
    :raises GitUnavailable: 找不到 git

    存在的理由：`rhine` 可以在**任意目录**启动，而很多目录不是 Git 仓库。
    先确认再动手，才能给出「当前目录不是 Git 仓库」这种用户看得懂的失败原因
    （spec F12/AC16），而不是抛一段 git 的英文 stderr。

    副作用：启动一个 git 子进程。
    """
    try:
        out = _run(["rev-parse", "--is-inside-work-tree"], root)
    except GitCommandFailed as exc:
        raise NotARepository(f"当前目录不是 Git 仓库：{root}") from exc
    if out.strip() != "true":
        raise NotARepository(f"当前目录不是 Git 工作树：{root}")


def head_commit(root: Path) -> str:
    """
    取当前 HEAD 的短哈希。

    :returns: 形如 `b225368` 的短哈希
    :raises GitCommandFailed: 仓库还没有任何提交时 git 会失败

    它是隔离工作区的**基点**（spec F8）：子 Agent 从这里出发，
    `commits_since` 也以它为界算「这次改了什么」。
    """
    return _run(["rev-parse", "--short", "HEAD"], root).strip()


def add_worktree(root: Path, path: Path, branch: str, base: str) -> None:
    """
    创建一个隔离工作区并同时建立新分支（spec F8）。

    :param root: 主项目根（命令在这里执行）
    :param path: 目标目录，必须尚不存在
    :param branch: 要创建的新分支名
    :param base: 基点（提交哈希）

    副作用：在磁盘上产生一整份源码 checkout，并在版本库中登记该工作目录、
    创建一个新分支。

    ⚠ 调用方必须**先**校验 `path` 的位置（`lifecycle` 的第②步），
    本函数不做位置判断——它只是个忠实的 git 封装。
    """
    _run(["worktree", "add", "-b", branch, str(path), base], root)


def list_worktrees(root: Path) -> tuple[Path, ...]:
    """
    列出本仓库已登记的全部工作目录（含主工作目录本身）。

    :returns: 解析后的绝对路径元组

    解析 `git worktree list --porcelain` 的输出。该格式每段以 `worktree <路径>`
    行开头，段与段之间空行分隔——这是 git 为机器解析提供的稳定契约，
    比解析人类可读的 `git worktree list` 可靠得多（后者的路径列会按内容对齐，
    路径含空格时无法切分）。

    本函数是三层过滤第②层「归属」的依据（spec F20②）：只有出现在这个列表里的
    目录才被认为是本仓库的工作目录，手工放进去的普通目录不会被误删。
    """
    out = _run(["worktree", "list", "--porcelain"], root)
    paths: list[Path] = []
    for line in out.splitlines():
        if line.startswith("worktree "):
            raw = line[len("worktree "):].strip()
            if raw:
                paths.append(Path(raw).resolve())
    return tuple(paths)


def branch_exists(root: Path, branch: str) -> bool:
    """
    判断一个分支是否已存在（spec F8 的冲突改名依据）。

    :returns: 存在返回 True

    用 `rev-parse --verify` 而不是解析 `git branch` 的输出：前者的退出码就是
    答案，不需要做任何字符串匹配，也不会被分支名里的特殊字符干扰。
    """
    try:
        _run(["rev-parse", "--verify", "--quiet", f"refs/heads/{branch}"], root)
        return True
    except GitCommandFailed:
        return False


def remove_worktree(root: Path, path: Path) -> None:
    """
    注销并删除一个工作目录。

    :raises GitCommandFailed: git 拒绝或删除失败

    ⚠ **实测的失败形态**（spec F22 的依据）：当有进程的当前工作目录位于该工作区
    内时（Windows 上尤其常见），git 会**先注销登记、再在删除磁盘目录时失败**，
    留下一个「版本库不认、磁盘上还在」的残留，而且此后再调本函数会报
    「不是一个工作目录」。因此调用方（`lifecycle.remove`）必须在本函数之后
    再跑一次 `prune_worktrees` 并对残留目录做独立的删除兜底。

    这里用 `--force`：调用方已经通过三层过滤确认过「没有会丢失的数据」
    （无未提交改动），此时 git 自己那道「工作区不干净就拒绝」的保护是多余的，
    反而会让「只剩未跟踪的构建产物」这种情形永远删不掉。
    """
    _run(["worktree", "remove", "--force", str(path)], root)


def prune_worktrees(root: Path) -> None:
    """
    修剪版本库中已失效的工作目录登记（spec F22）。

    对应 `git worktree prune`。它只清理「登记还在、目录已经没了」的条目，
    不碰任何磁盘上真实存在的工作区，因此是安全的幂等操作。
    """
    _run(["worktree", "prune"], root)


def delete_branch(root: Path, branch: str) -> None:
    """
    删除一个分支（spec F21：仅当该工作区**没有任何新增提交**时才调用）。

    :raises GitCommandFailed: 分支不存在或删除失败

    用 `-D` 而不是 `-d`：调用方已确认 `commits == 0`，此时分支与基点同点，
    `-d` 的「未合并就拒绝」检查不会触发；但在 rebase 之类的边角情形下 `-d`
    可能误判，而我们这里的前提是「删掉不丢任何东西」，用 `-D` 语义更准确。
    """
    _run(["branch", "-D", branch], root)


def status(path: Path) -> tuple[bool, tuple[str, ...]]:
    """
    查询一个工作目录的未提交改动（spec F16/F20③）。

    :param path: 工作目录
    :returns: `(dirty, 变更文件路径元组)`

    用 `git status --porcelain`，它**包含未跟踪文件**（`??` 前缀）——这一点很关键：
    子 Agent 新建的文件默认是未跟踪的，如果只看「已跟踪文件的修改」，
    一个刚写完新文件还没 add 的工作区会被判成「干净」而被自动删掉，
    成果直接消失。

    porcelain 每行的格式是 `XY <路径>`，前两列是状态码。含空格或特殊字符的
    路径会被 git 用引号包起来，这里保持原样返回——它只用于展示，不用于再次
    访问文件系统。
    """
    out = _run(["status", "--porcelain"], path)
    files: list[str] = []
    for line in out.splitlines():
        if not line.strip():
            continue
        # 前两列是状态码，第三列起是路径（中间恰好一个空格）。
        entry = line[3:].strip() if len(line) > 3 else line.strip()
        if entry:
            files.append(entry)
    return bool(files), tuple(files)


def commits_since(path: Path, base: str) -> tuple[int, tuple[str, ...]]:
    """
    查询一个工作目录相对基点的新增提交（spec F16/F21）。

    :param path: 工作目录
    :param base: 基点提交哈希
    :returns: `(提交数, 这些提交涉及的文件路径元组)`

    提交数决定两件事：是否保留这个工作区（F16），以及删除时是否保留分支
    （F21——有提交就保留，因为 commit 在共享版本库里，删目录不丢数据）。

    基点已不存在（例如分支被强制改写）时按「无新增提交」处理并返回空——
    这是 fail-safe 的方向吗？**不是**，它偏向「判定为无变更 → 可能被删除」。
    因此调用方必须记住：`dirty` 那一路才是删除的无条件否决，本函数的结论
    只影响「删了之后要不要留分支」。真出现基点丢失时，最坏结果是留下一个
    多余的分支，不会丢数据。
    """
    try:
        count_out = _run(["rev-list", "--count", f"{base}..HEAD"], path)
        count = int(count_out.strip() or "0")
    except (GitCommandFailed, ValueError):
        return 0, ()

    if count == 0:
        return 0, ()

    try:
        names_out = _run(["diff", "--name-only", f"{base}..HEAD"], path)
    except GitCommandFailed:
        return count, ()

    files = tuple(x.strip() for x in names_out.splitlines() if x.strip())
    return count, files


def merge_base(root: Path, a: str, b: str) -> str:
    """
    求两个引用的最近共同祖先（分叉点）。

    :param root: 任一属于本仓库的工作目录
    :param a: 引用一（通常是隔离工作区的分支）
    :param b: 引用二（通常是主项目根的 HEAD）
    :returns: 分叉点的提交哈希；求不出时返回空串

    **它是启动清理算「这个分支上有几个提交」的唯一办法。**
    清理面对的是上次运行留下的目录，`WorktreeHandle` 早就没了，
    创建时那个基点无从得知。`merge-base` 能把它算回来：

    - 子 Agent 提交了 2 次 → 分叉点是当初的基点，`base..branch` 数出 2
    - 一次都没提交 → 分叉点就是分支自身，数出 0

    主分支后来被 rebase / squash 也不会出错到危险的方向：最坏是**多数**几个，
    于是判定为「有提交 → 保留分支」——宁可留一个多余的分支，不可丢成果
    （spec N5）。

    求不出时返回空串，调用方据此退回「无法判定」的保守分支。
    """
    try:
        return _run(["merge-base", a, b], root).strip()
    except (GitCommandFailed, GitUnavailable):
        return ""


def commits_on_branch(root: Path, branch: str, against: str = "HEAD") -> int:
    """
    数一个分支相对它与 `against` 的分叉点有多少个提交。

    :returns: 提交数；无法判定时返回 -1

    ⚠ **返回 -1 而不是 0**：0 的语义是「确定没有提交，可以连分支一起删」，
    而「算不出来」绝不能被当成「确定没有」——那会删掉可能装着成果的分支。
    调用方必须把 -1 当作「保留分支」处理。
    """
    base = merge_base(root, branch, against)
    if not base:
        return -1
    try:
        out = _run(["rev-list", "--count", f"{base}..{branch}"], root)
        return int(out.strip() or "0")
    except (GitCommandFailed, GitUnavailable, ValueError):
        return -1


def hooks_path(root: Path) -> Path:
    """
    返回给定工作目录实际使用的 git 钩子目录。

    :returns: 绝对路径

    **只用于测试护栏**（spec F11/AC15）：实测主项目根与隔离工作区在这里返回
    **同一个路径**（`<主仓库>/.git/hooks`）——git 钩子属于版本库的共享部分，
    隔离工作区原生继承，本章因此**什么都不做**。

    留这个函数是为了让「什么都不做」这个决定有一条可运行的证据钉着，
    而不是只写在文档里。
    """
    out = _run(["rev-parse", "--git-path", "hooks"], root).strip()
    candidate = Path(out)
    if not candidate.is_absolute():
        candidate = root / candidate
    return candidate.resolve()


__all__ = [
    "ensure_repository",
    "head_commit",
    "add_worktree",
    "list_worktrees",
    "branch_exists",
    "remove_worktree",
    "prune_worktrees",
    "delete_branch",
    "status",
    "commits_since",
    "merge_base",
    "commits_on_branch",
    "hooks_path",
]
