"""
隔离工作区的生命周期：创建、检查、三层过滤、删除（c14 T8）。

本模块是 `worktree` 包的核心，四个对外函数各自对应 spec 的一组条款：

| 函数 | spec | 一句话 |
| --- | --- | --- |
| `create` | F6–F10 | 建目录、建分支、按清单初始化环境 |
| `inspect` | F16/F20③ | 查这个工作区改了什么 |
| `judge_removal` | F20/F21 | **纯判定**：这个工作区准不准删、删了要不要留分支 |
| `remove` | F20/F22 | **唯一删除入口** |

⚠ **`remove` 是本包唯一的删除入口。** 将来任何第二条删除路径都必须走它，
否则三层过滤形同虚设。CLAUDE.md 里那条「曾因空变量 rmtree 删掉整个仓库」的
教训由 `judge_removal` 的第①层位置校验兜住——它拒绝空路径、拒绝上级引用、
拒绝任何解析后不落在隔离工作区根目录**之内**的路径。
"""

from __future__ import annotations

import os
import shutil
import stat
import threading
from pathlib import Path
from typing import Optional, Sequence

from rhinecode.worktree import gitcmd
from rhinecode.worktree.models import (
    BRANCH_PREFIX,
    MAX_BRANCH_SUFFIX,
    RHINECODE_DIR_NAME,
    WORKTREES_DIR_NAME,
    ChangeStatus,
    GitCommandFailed,
    ProvisionEntry,
    ProvisionResult,
    RemovalVerdict,
    WorktreeError,
    WorktreeHandle,
)
from rhinecode.trace import TraceEventType
from rhinecode.worktree.naming import generate_name, validate_name
from rhinecode.worktree.provision import provision

# ---------------------------------------------------------------------------
# 版本库改动的串行闸门（2026-08-30 加入，2026-09-01 扩到回收路径）
# ---------------------------------------------------------------------------
# ⚠ **它原名 `_CREATE_LOCK`**，只圈住创建那半边。改名是因为它现在同时圈住
# 创建与回收——名字还写着 create，会让下一个人以为回收不归它管，
# 而那恰恰就是 2026-08-31 那次 CI 红灯的成因。
#
# **同一个版本库上的「建工作区」与「回收工作区」不能并发跑。**
#
# git 那边的三条事实（v2.50.1 `builtin/worktree.c` 逐字核对 + 本机实测）：
#
# * **回收路径会删掉共享的父目录。** `git worktree remove` 与 `git worktree prune`
#   都走到 `delete_worktrees_dir_if_empty()`，那就是一句 `rmdir(.git/worktrees)`
#   ——只要它空了就删。实测：两条命令都会让 `.git/worktrees` 整个消失。
# * **prune 还会删掉半成品条目。** `.git/worktrees/<名字>/` 只要还没写出
#   `locked` / `gitdir`，就被判为失效条目并递归删除，随后父目录一空又被 rmdir。
#   实测确认。
# * **创建路径没有任何保护。** `add_worktree()` 里 `safe_create_leading_directories`
#   （建出 `.git/worktrees`）与 `mkdir(.git/worktrees/<名字>)` 是**相邻两句**，
#   中间什么都没有；mkdir 拿到 ENOENT 就
#   `die_errno("could not create directory of '%s'")`。
#
# 于是两次 CI 红灯是**同一个成因在两个相邻瞬间**的两种表现：
#
#     2026-08-30 windows/3.12
#       fatal: could not open '.git/worktrees/<名字>/locked' for writing:
#       No such file or directory
#       ——子目录已建、`locked` 还没写出，被并发的 prune 当成半成品删掉了
#
#     2026-08-31 windows/3.11（PR #63）
#       fatal: could not create directory of '.git/worktrees/<名字>':
#       No such file or directory
#       ——父目录刚建好还空着，被并发回收的 rmdir 抢先删掉了
#
# ⚠ **第一次的修法认错了对象，这一点必须写下来，否则下一个人会照着改第三遍。**
# 当时的判断是「`git worktree add` 开工时会做一次隐式 prune，于是两个并发的 add
# 互相拆台」，据此把创建的三步收进锁里。**「add 会隐式 prune」是错的**——
# v2.50.1 的 `add()` 与 `add_worktree()` 里都没有 `prune_worktrees()` 调用。
# 真正会 prune、会 rmdir 的是**回收路径**，而它当时根本不在锁里：
# 两个真正会碰撞的操作**从来没有互斥过**，锁住 add 只是把并发面收窄了一点。
#
# ⚠ **窗口有多窄，决定了它为什么本机复现不出来。** 实测 `add` 里「父目录已建、
# 子目录未建」这个状态只存在 **<10µs**（高频轮询观测器四轮只抓到一次）。
# 用未改动的 git 复现，要靠一个独立进程死循环 rmdir 那个父目录，8 轮才中 1 轮
# （中的那轮报的就是上面第二条，一字不差）。CI 上真正发生的是**受害进程恰好在
# 那两句之间被调度器抢走**——所以它只在机器够慢时张开（红掉的两格分片分别跑了
# 150.9s 与 175.8s，平时约 60s），也所以「本机 8 路 × 6 轮跑不出来」
# **不构成「它不存在」的证据**。
#
# ⚠ 顺带排除掉两个曾被怀疑的方向：不是 Windows 的目录创建瞬时失败（杀毒持句柄
# 给的是 EACCES / 共享冲突，而这里是 ENOENT，且实测「父目录被删」能一字不差地
# 复现出那句报错），也不是残留的 daemon 线程（受害者与凶手都在同一次委派的
# 生命周期内，时序完全对得上）。
#
# 因此闸门圈住**两条路径**：`create` 的第 ④⑤⑥ 步与 `remove` 的全过程。
# `_pick_branch` 那段「先查在不在、再拿来用」是 check-then-act，一并留在里面。
#
# ⚠ **回收路径收进来之后，「这把锁不参与死锁」那段论证必须重新成立，已逐条核过。**
# 它是模块级的，只被两处走到：`subagents/runner.py` 的结算段（子 Agent 工作线程）
# 与 `worktree/cleanup.py` 的 `scan_and_clean`（启动时的主线程，那一刻还没有任何
# 子 Agent 线程，因此连争用都不可能发生）。**两处都不持有任何回调、不碰界面、
# 不做任何跨线程调度**，因此不可能与 Textual 的阻塞式 `call_from_thread` 组成
# 那类确定性死锁；`remove` 也不会反过来调 `create`，这把非重入锁不存在自锁。
# 代价只有排队：创建与回收各自几百毫秒，最坏受 `gitcmd._TIMEOUT`（120 秒）封顶，
# 委派跑起来之后各走各的，一点不受影响。
#
# 护栏：`tests/test_worktree_lifecycle.py::ConcurrentCreateAndRemoveTest`
# ——它把那 <10µs 的窗口用 sleep 摆成最坏情况，把本锁去掉当场红。
_REPO_LOCK = threading.Lock()


def _emit(recorder, event, **fields) -> None:
    """
    产一条行为记录事件（c14 F24）。

    :param recorder: 记录器；`None` 或不可用时**什么都不做**

    ⚠ **任何异常都吞掉。** 观测设施绝不能反过来影响被观测的系统——
    这是 trace 从 P0 起就定死的纪律。本模块尤其要守：它跑在子 Agent 的后台
    线程上，一个埋点异常逃逸出去会让整个任务失败。
    """
    if recorder is None:
        return
    try:
        recorder.emit(event, **fields)
    except Exception:  # noqa: BLE001
        pass


def worktrees_root(main_root: Path) -> Path:
    """
    隔离工作区的根目录：`<主项目根>/.rhinecode/worktrees`（spec F6）。

    :param main_root: 主项目根
    :returns: 绝对路径（**可能不存在**，创建时才建）
    """
    return (Path(main_root) / RHINECODE_DIR_NAME / WORKTREES_DIR_NAME).resolve()


def _within_worktrees_root(main_root: Path, path: Path) -> bool:
    """
    三层过滤第①层的核心判断：`path` 是否**严格位于**隔离工作区根目录之内。

    :returns: 位于其内且不等于根目录本身时为 True

    ⚠ 这里**刻意不复用 `path_guard.resolve_in_workspace`**：那个函数以主项目根
    为界，而隔离工作区根目录只是主项目根下的一个子目录。用它判定的话，
    `.rhinecode/sessions`（会话存档！）也会被判为「合法的删除目标」。
    边界必须收紧到 `worktrees` 这一层。

    「不等于根目录本身」这一条同样不能省：删掉根目录会一次性清空全部隔离工作区，
    包括正在运行的那些。
    """
    root = worktrees_root(main_root)
    try:
        resolved = Path(path).resolve()
    except OSError:
        return False
    if resolved == root:
        return False
    try:
        resolved.relative_to(root)
        return True
    except ValueError:
        return False


def _force_rmtree(path: Path) -> None:
    """
    删除一个目录树，容忍只读文件。

    副作用：递归删除 `path`。

    ⚠ **为什么不能直接 `shutil.rmtree(path, ignore_errors=True)`**：git 在
    `.git/objects` 下留的是**只读**文件，Windows 上直接删会失败；配上
    `ignore_errors=True` 之后它会「删一半且一个错都不报」，留下一个残骸目录。
    CLAUDE.md 里为这件事专门留过一条纪律（测试侧走 `tests/e2e/sandbox.py` 的
    `force_rmtree`）。

    ⚠ **刻意不 import 测试侧那份同名实现**：`tests/` 不进产品包，产品代码
    依赖它会在真实安装后直接 `ImportError`。两份实现是必要的重复。
    """

    def _on_error(func, target, _exc_info):
        """把只读文件改成可写后重试一次；仍失败则放弃（由调用方判断后果）。"""
        try:
            os.chmod(target, stat.S_IWRITE)
            func(target)
        except OSError:
            pass

    if not path.exists():
        return
    # onexc 是 3.12+ 的名字，onerror 在 3.12 起被弃用但仍可用。
    # 本项目要求 3.11+，故用 onerror 以覆盖 3.11。
    shutil.rmtree(path, onerror=_on_error)


def _is_recoverable(main_root: Path, target: Path) -> bool:
    """
    快速恢复判定（spec F9）：这个已存在的目录是不是本仓库的隔离工作区？

    :returns: 是则 True（调用方据此跳过创建）

    ⚠ **全程不调 git。**

    依据是一条实测结论：隔离工作区里的 `.git` 不是目录，而是一个 51 字节的
    文本文件，内容形如 `gitdir: <主仓库>/.git/worktrees/<名字>`。
    「这个目录是不是本仓库的工作目录」这件事**物理上就写在那个文件里**，
    读它即可确认归属，无需启动 git 子进程。

    这条快路径同时消除了一个失败模式：恢复不会再因为 git 暂时不可用而失败。

    ⚠ **删除路径刻意不复用本函数**（`judge_removal` 第②层用的是
    `gitcmd.list_worktrees`，那是真的调 git）。两处对「归属」的确认强度不同
    是有意的：恢复是读操作，判错的代价是多建一个目录；删除是写操作，
    判错的代价是删掉不该删的东西，必须问 git 要权威答案。
    """
    if not target.is_dir():
        return False

    marker = target / ".git"
    # 工作区的 .git 必须是**文件**。是目录说明那是一个独立仓库（比如用户手工
    # clone 进来的），不是本仓库的工作区，绝不能当成可恢复的对象。
    if not marker.is_file():
        return False

    try:
        content = marker.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return False

    if not content.startswith("gitdir:"):
        return False

    pointer = content[len("gitdir:"):].strip()
    if not pointer:
        return False

    try:
        pointed = Path(pointer).resolve()
        expected_parent = (Path(main_root) / ".git" / "worktrees").resolve()
    except OSError:
        return False

    try:
        pointed.relative_to(expected_parent)
        return True
    except ValueError:
        return False


def _pick_branch(main_root: Path, name: str) -> str:
    """
    为一个隔离工作区挑一个不冲突的分支名（spec F8）。

    :returns: 形如 `agent/<name>`；已存在时依次试 `agent/<name>-2`、`-3`……
    :raises WorktreeError: 连试 MAX_BRANCH_SUFFIX 次仍冲突

    为什么要自动改名而不是直接失败：分支名冲突是很正常的（同一个角色对同一个
    模块委派两次），失败会让用户莫名其妙。但**实际用的名字必须回报给调用方**，
    否则主 Agent 会拿着期望的名字去合并一个不存在的分支——所以 `create` 把它
    放进 `WorktreeHandle.branch`，交付信息段取的也是那个值（spec F17）。
    """
    base = f"{BRANCH_PREFIX}{name}"
    if not gitcmd.branch_exists(main_root, base):
        return base
    for suffix in range(2, MAX_BRANCH_SUFFIX + 1):
        candidate = f"{base}-{suffix}"
        if not gitcmd.branch_exists(main_root, candidate):
            return candidate
    raise WorktreeError(
        f"分支名 {base} 连续 {MAX_BRANCH_SUFFIX} 次冲突，放弃创建隔离工作区"
    )


def create(
    main_root: Path,
    name: Optional[str] = None,
    agent_name: str = "agent",
    task_id: str = "",
    entries: Sequence[ProvisionEntry] = (),
    recorder=None,
) -> tuple[WorktreeHandle, ProvisionResult]:
    """
    创建（或快速恢复）一个隔离工作区（spec F6–F10）。

    :param main_root: 主项目根
    :param name: 委派方给的名字；`None` 时由系统生成
    :param agent_name: 角色名，仅用于生成名字时让目录可读
    :param task_id: 任务标识，仅用于生成名字时区分并发
    :param entries: 环境初始化清单
    :param recorder: 行为记录器（c14 F24）。`None` 时不埋点，**不传等于零成本**
    :returns: `(WorktreeHandle, ProvisionResult)`
    :raises WorktreeNameError: 名字未通过安全校验
    :raises NotARepository: 当前目录不是 Git 仓库
    :raises GitUnavailable: 找不到 git
    :raises WorktreeError: 其它创建失败

    **本函数的任何异常都应当被调用方转成一次明确的委派失败**（spec F12），
    绝不降级为「在主项目根里跑」——那会让用户以为隔离了而实际没有。

    执行流程：

    1. **名字校验或生成**。⚠ 校验在**任何路径拼接之前**——先拼后验的话，
       拼接那一步就已经产生了越界路径。
    2. **位置校验**：目标必须严格落在 `<主项目根>/.rhinecode/worktrees/` 之内。
    3. **快速恢复判定**（不调 git，见 `_is_recoverable`）。命中即返回。
    4. `ensure_repository` / `head_commit`。
    5. 挑一个不冲突的分支名。
    6. `git worktree add`。
    7. 按清单初始化环境（失败只记警告，不影响创建成败）。

    ⚠ **第 4~6 步在一把进程内的锁里串行执行**（`_REPO_LOCK`）：它们动的是
    同一个版本库，而**回收路径（`remove`）会 rmdir 掉两者共用的**
    `.git/worktrees` **父目录**，正建到一半的 `add` 会当场拿到
    `No such file or directory`。理由与实测见那个常量上方的说明。
    第 7 步不在锁内（它只碰新工作区自己的目录）。

    副作用：在磁盘上产生一整份源码 checkout，在版本库中登记工作目录并创建分支；
    按清单复制或软链文件。
    """
    main_root = Path(main_root).resolve()

    # ① 名字先行校验（spec F7）。这一步必须在任何 Path 拼接之前。
    final_name = validate_name(name) if name else generate_name(agent_name, task_id)

    # ② 位置校验。
    root = worktrees_root(main_root)
    target = (root / final_name).resolve()
    if not _within_worktrees_root(main_root, target):
        # 正常情况下 validate_name 已经挡住了一切能跳出去的写法，
        # 走到这里说明校验与拼接之间出现了错位——宁可失败也不落盘。
        raise WorktreeError(f"隔离工作区落点越界，拒绝创建：{final_name}")

    # ③ 快速恢复（spec F9）：目录已在且确属本仓库的工作区 → 不调任何 git。
    if _is_recoverable(main_root, target):
        _emit(
            recorder,
            TraceEventType.WORKTREE_CREATE,
            name=final_name,
            # ⚠ 路径必须记：它是「隔离到底有没有真的发生」唯一的锚点。
            # 没有它的话，`permission_decision.cwd` 记下来的那个目录
            # 与「本次委派用的工作区」对不上号——读记录的人只能靠名字猜。
            path=str(target),
            branch="",
            base_commit="",
            recovered=True,
        )
        return (
            WorktreeHandle(
                name=final_name,
                path=target,
                # 恢复路径拿不到当初的分支与基点（那要调 git 才知道），
                # 留空由调用方按需补。实际使用中恢复只发生在同一次会话内的
                # 重复委派，调用方手里就有原句柄。
                branch="",
                base_commit="",
                recovered=True,
            ),
            ProvisionResult(),
        )

    # ④⑤⑥ 一并收进串行闸门——它们全都在动**同一个版本库**，并发下会互相拆台。
    # 理由见 `_REPO_LOCK` 上方那段。⚠ 锁的范围到 `add_worktree` 为止：
    # 第 ⑦ 步的环境初始化只碰新工作区自己的目录，不必排队。
    with _REPO_LOCK:
        # ④ 环境确认。
        gitcmd.ensure_repository(main_root)
        base = gitcmd.head_commit(main_root)

        # ⑤ 分支。⚠ 「查它在不在 → 拿来用」是 check-then-act，
        # 必须与下面的 add 在同一个临界区里，否则两个线程会挑中同一个名字。
        branch = _pick_branch(main_root, final_name)

        # ⑥ 建目录。父目录要先建出来（嵌套名字如 a/b 需要）。
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            gitcmd.add_worktree(main_root, target, branch, base)
        except GitCommandFailed as exc:
            raise WorktreeError(f"创建隔离工作区失败：{exc}") from exc

    handle = WorktreeHandle(
        name=final_name,
        path=target,
        branch=branch,
        base_commit=base,
        recovered=False,
    )

    # ⑦ 环境初始化。它从不失败（内部把每条的异常转成警告）。
    result = provision(main_root, target, entries)
    _emit(
        recorder,
        TraceEventType.WORKTREE_CREATE,
        name=final_name,
        path=str(target),
        branch=branch,
        base_commit=base,
        recovered=False,
    )
    _emit(
        recorder,
        TraceEventType.WORKTREE_PROVISION,
        name=final_name,
        applied=len(result.applied),
        warnings=len(result.warnings),
        details=list(result.warnings),
    )
    return handle, result


def inspect(handle: WorktreeHandle) -> ChangeStatus:
    """
    查询一个隔离工作区改了什么（spec F16/F20③）。

    :param handle: 工作区句柄
    :returns: `ChangeStatus`

    **结束决策与三层过滤第③层共用本函数**，避免两处口径漂移。

    目录已不存在（被外部删掉）时返回「无变更」——这是安全的方向：
    调用方据此会去调 `remove`，而 `remove` 的第①②层会把它拦下或让 git 的
    prune 把残留登记清掉，不会误删任何东西。

    副作用：启动 git 子进程（两条只读命令）。
    """
    path = Path(handle.path)
    if not path.is_dir():
        return ChangeStatus(dirty=False, commits=0, files=())

    try:
        dirty, dirty_files = gitcmd.status(path)
    except WorktreeError:
        # 查不出来时按「有变更」处理——这是 fail-safe 的方向：
        # 判错的后果是留下一个本可删除的目录（浪费空间），
        # 而反过来会删掉一个可能有未提交改动的工作区（丢数据）。
        return ChangeStatus(dirty=True, commits=0, files=())

    commits, commit_files = (0, ())
    if handle.base_commit:
        try:
            commits, commit_files = gitcmd.commits_since(path, handle.base_commit)
        except WorktreeError:
            commits, commit_files = 0, ()

    merged = sorted(set(dirty_files) | set(commit_files))
    return ChangeStatus(dirty=dirty, commits=commits, files=tuple(merged))


def judge_removal(
    main_root: Path, path: Path, status: ChangeStatus
) -> RemovalVerdict:
    """
    三层过滤（spec F20/F21）。**纯判定、零副作用**，可被单测穷举。

    :param main_root: 主项目根
    :param path: 待删除的路径
    :param status: 该工作区的变更状态
    :returns: `RemovalVerdict`。`allowed=False` 时 `reason` 必填

    三层依次：

    ① **位置**——`path` 非空、可解析、严格落在 `<主项目根>/.rhinecode/worktrees/`
       之内且不等于该根目录本身。这一层挡住的是「空变量 rmtree」「`..` 跳出去」
       「误删会话存档目录」这类事故。

    ② **归属**——`path` 出现在 `git worktree list` 中，即版本库认它是本仓库的
       工作目录。手工放进那个目录里的普通目录不会被碰（spec AC27）。

    ③ **变更**——`status.dirty` 为真则**无条件拒绝**。未提交的改动只存在于
       那个目录里，删掉就永久丢失（spec N5）。这一层不看过期时长、不看任何
       其它条件。

    三层都通过时，`keep_branch = status.commits > 0`：含新增提交的只删目录、
    **保留分支**——commit 在共享版本库里完好无损，`git checkout <branch>`
    随时能取回（spec F21）。这正是清理既能真正回收空间、又不丢数据的原因。

    副作用：无（第②层会调一次只读的 git 命令；查询失败按「不通过」处理）。
    """
    # ① 位置
    #
    # ⚠ **空路径必须单独判，而且要认出 `"."`。**
    # `Path("")` 在 pathlib 里等于 `Path(".")`，也就是**当前工作目录**——
    # CLAUDE.md 记的那次真实事故正是这个形态：调用方的变量成了空串，
    # `rmtree` 把整个代码仓库删了个精光。
    #
    # 下面的位置校验其实也能把它拦下（`.` 解析出来不在 worktrees 根内），
    # 但**不能靠那个兜底**：位置校验的拒绝理由会写成「不在隔离工作区目录内」，
    # 而真正的问题是「调用方传了个空值」。理由说错会让排查走上另一条路。
    if path is None:
        return RemovalVerdict(False, reason="待删路径为空")
    raw = str(path).strip()
    if raw in ("", "."):
        return RemovalVerdict(False, reason="待删路径为空")
    if not _within_worktrees_root(main_root, Path(path)):
        return RemovalVerdict(
            False, reason=f"待删路径不在隔离工作区目录内：{path}"
        )

    # ② 归属
    try:
        registered = gitcmd.list_worktrees(main_root)
    except WorktreeError as exc:
        return RemovalVerdict(False, reason=f"无法确认工作区归属，拒绝删除（{exc}）")
    try:
        resolved = Path(path).resolve()
    except OSError:
        return RemovalVerdict(False, reason=f"待删路径无法解析：{path}")
    if resolved not in registered:
        return RemovalVerdict(
            False, reason="该目录未被版本库登记为本仓库的工作区，不做处理"
        )

    # ③ 变更
    if status.dirty:
        return RemovalVerdict(False, reason="存在未提交的改动，保留以免丢失")

    return RemovalVerdict(
        True,
        keep_branch=status.commits > 0,
        reason=(
            f"无未提交改动，含 {status.commits} 个提交，删除目录并保留分支"
            if status.commits > 0
            else "无任何变更，删除目录与分支"
        ),
    )


def remove(
    main_root: Path,
    path: Path,
    branch: str,
    status: ChangeStatus,
) -> RemovalVerdict:
    """
    删除一个隔离工作区（spec F20/F21/F22）。**本包唯一的删除入口。**

    :param main_root: 主项目根
    :param path: 待删除的工作区目录
    :param branch: 该工作区的分支名（为空时跳过分支删除）
    :param status: 该工作区的变更状态
    :returns: `judge_removal` 的结论。未获许可时**一步都没往下走**

    执行流程（获得许可之后）：

    1. `git worktree remove --force`
    2. `git worktree prune` —— 处理「已注销但目录还在」的残留（spec F22）
    3. 目录若仍存在 → `_force_rmtree` 兜底
    4. `keep_branch` 为假时删除分支

    第 2、3 步不是多余的。**实测形态**：当有进程的当前工作目录位于该工作区内时
    （Windows 上尤其常见），git 会先注销登记、再在删除磁盘目录时失败，
    留下一个「版本库不认、磁盘上还在」的残骸，而且此后 `git worktree remove`
    会报「不是一个工作目录」——只有独立的目录删除兜底能收拾它。

    ⚠ **获得许可之后的全过程在 `_REPO_LOCK` 里串行执行**（2026-09-01 加入）。
    第 1、2 步动的是**与创建路径同一个版本库**：`git worktree remove` 与
    `git worktree prune` 都会 rmdir 掉两者共用的 `.git/worktrees` 父目录，
    而一次正在进行的 `git worktree add` 会当场拿到
    `could not create directory of '.git/worktrees/<名字>': No such file or directory`。
    这就是 2026-08-31 那次 CI 红灯。完整论证（含「为什么这把锁不参与死锁」
    在把本函数收进来之后仍然成立）见 `_REPO_LOCK` 上方那段。

    ⚠ **`judge_removal` 也在锁内**：它的第②层要问「版本库认不认这个目录」，
    而并发的创建正在改那份登记——判定与据此执行的删除必须看到同一个版本库状态。

    副作用：删除磁盘目录、修改版本库登记、可能删除一个分支。
    """
    # ⚠ 判定与删除**必须在同一个临界区里**，理由见 docstring 与 `_REPO_LOCK`
    # 上方那段：并发的创建正在改同一份版本库登记，而第②层归属判定读的就是它。
    with _REPO_LOCK:
        verdict = judge_removal(main_root, path, status)
        if not verdict.allowed:
            # ⚠ 一步都不往下走。这是三层过滤唯一的意义所在。
            return verdict

        target = Path(path)

        # 1. 让 git 自己来（它会同时清掉登记）。
        try:
            gitcmd.remove_worktree(main_root, target)
        except WorktreeError:
            # 失败是预期内的一种情形（见 docstring 的实测形态），继续走兜底。
            pass

        # 2. 修剪失效登记。
        try:
            gitcmd.prune_worktrees(main_root)
        except WorktreeError:
            pass

        # 3. 目录仍在 → 独立兜底。
        if target.exists():
            _force_rmtree(target)

        # 4. 分支。
        if branch and not verdict.keep_branch:
            try:
                gitcmd.delete_branch(main_root, branch)
            except WorktreeError:
                # 分支删不掉不影响「目录已回收」这个主要目的，不向上抛。
                pass

        return verdict


__all__ = [
    "worktrees_root",
    "create",
    "inspect",
    "judge_removal",
    "remove",
]
