"""
工具路径安全边界。

**c14 起，路径边界判定不再依赖进程的当前工作目录。**

本模块过去把「项目根」写死成 `Path.cwd()`，因此全进程只有一个边界。c14 引入
隔离工作区（子 Agent 在自己的 Git 工作目录里干活）之后，边界必须**按调用者**
决定：主对话以主项目根为界，隔离子 Agent 以它自己的工作区为界。

于是本模块的函数分成语义截然不同的两组，**命名上刻意区分开**：

| 函数 | 语义 |
| --- | --- |
| `main_project_root()` | 进程启动时的当前工作目录。只用于定位**与调用者无关**的位置：配置文件、会话存档、上下文存盘目录等 |
| `resolve_in_workspace(path, root)` 等四个 | **路径边界判定**。`root` 由调用方显式给出 |

⚠ **四个判定函数的 `root` 参数刻意没有默认值。**

给它一个默认值（比如缺省取 `main_project_root()`）会让「忘记传的调用点」
静默按主项目根判定——而那正是 spec N2 要禁止的形态：**一次隔离故障会静默
变成一次越权**，隔离子 Agent 的读写落回主项目根，而界面上完全看不出来。
无默认值让任何遗漏在开发期就变成 `TypeError`。

c9 补充的「额外只读根目录」白名单（用户级记忆目录、Skill 目录）**与 root 正交**：
它的语义是「不论在哪个工作目录下都可读」，因此仍是模块级全局，只对「读」类
判定生效（`resolve_readable` / `is_readable_path`），写类判定完全不受影响。
"""

from pathlib import Path
from typing import Union

# 额外只读根目录白名单：仅 read 类判定查询。启动时由协调层注册
# （当前注册用户级 memory 目录与 Skill 目录），运行期不再变动。
#
# ⚠ 它与调用方传入的 `root` **正交**：白名单里的目录在任何 root 下都可读，
# 因为它们表达的是「这些位置本来就允许模型只读访问」，与调用者站在哪个
# 工作目录里无关。
_EXTRA_READ_ROOTS: list[Path] = []


class PathGuardError(ValueError):
    """路径或 glob 模式越过给定工作目录时抛出的结构化错误。"""


def main_project_root() -> Path:
    """
    返回**主项目根**——进程启动时的当前工作目录。

    :returns: 绝对路径

    ⚠ **只用于定位与调用者无关的位置**：`permissions.yaml` / `hooks.yaml` /
    `mcp.yaml` 的项目级路径、`.rhinecode/` 下的存档与存盘目录、trace 默认输出路径、
    环境信息里展示的项目路径。

    **绝不用于路径边界判定**——那要用下面四个函数并显式传 root。c14 之前
    这两件事共用一个 `workspace_root()`，混在一起看不出区别；重命名就是为了
    强制把它们分开（改造时逐个复核了 15 个既有调用点，其中 12 处要主项目根、
    3 处要调用者的工作目录）。
    """
    return Path.cwd().resolve()


def require_cwd(cwd: Union[str, Path, None]) -> Path:
    """
    取出一次工具调用的工作目录，缺失时**抛错而不是回退**（c14，spec N2）。

    :param cwd: 循环传给工具的工作目录
    :returns: 解析后的绝对路径
    :raises PathGuardError: 未提供或无法解析

    ⚠ **为什么不缺省成主项目根。**

    回退看起来更「健壮」，实际会造成一种静默的串写：权限引擎按隔离工作区
    批准了相对路径 `a.py`（它判定的是 `<工作区>/a.py`），而工具因为没拿到 cwd
    退回主项目根，真正写的是 `<主项目根>/a.py`——**批准的和写的不是同一个文件**，
    主项目的内容被悄悄改掉，两边都不报错。

    抛出的 `PathGuardError` 会被各工具既有的 try/except 兜成
    `ToolResult(ok=False)`，因此不会让循环崩溃，只会明确失败。

    声明了 `Tool.workspace_aware` 的工具都应当用它取 cwd。
    """
    return _normalize_root(cwd)


# 隔离工作区所在目录，相对任一工作目录根（c14 F18）。
#
# ⚠ **这两个字面量与 `worktree/models.py` 的常量刻意重复，不要合并。**
# 合并要让 `tools` 反向 import `worktree`，而 `worktree/provision.py` 又 import
# 本模块——虽然 `tools/__init__.py` 是空的、当前不会真的成环，但那道保护是
# 「碰巧成立」而不是「结构成立」，不值得为两个字符串去赌。
# 与 CLAUDE.md 里 `Layer` 名字表「三份刻意不合一」同一条理由。
_WORKTREES_RELATIVE = (".rhinecode", "worktrees")


def worktrees_dir_of(root: Union[str, Path]) -> Path:
    """
    给定工作目录下的隔离工作区目录（c14 F18）。

    :param root: 工作目录根
    :returns: `<root>/.rhinecode/worktrees` 的绝对路径（**可能不存在**）

    搜索类工具用它把隔离工作区排除在结果之外。
    """
    return Path(root).joinpath(*_WORKTREES_RELATIVE)


def is_inside(path: Union[str, Path], container: Union[str, Path]) -> bool:
    """
    判断 `path` 是否位于 `container` 之内（含相等）。

    :returns: 位于其内返回 True；任何解析失败返回 False

    ⚠ 用**路径相等**判断，而不是「目录名叫 worktrees」——后者会误伤用户自己
    叫这个名字的业务目录（比如一个真的在做 worktree 管理的项目）。
    """
    try:
        target = Path(path).resolve()
        base = Path(container).resolve()
    except OSError:
        return False
    if target == base:
        return True
    try:
        target.relative_to(base)
        return True
    except ValueError:
        return False


def _ensure_no_parent_ref(path: Path, raw: str) -> None:
    """拒绝显式 `..`，避免先跳出再解析回来的路径绕过审计。"""
    if ".." in path.parts:
        raise PathGuardError(f"路径不能包含 '..': {raw}")


def _normalize_root(root: Union[str, Path, None]) -> Path:
    """
    把调用方给的 root 归一为绝对路径。

    :param root: 工作目录
    :returns: 解析后的绝对路径
    :raises PathGuardError: root 缺失、为空、或无法解析

    ⚠ **root 无效时抛错而不是回退到主项目根**（spec N2）。回退看起来「更健壮」，
    实际是把一次隔离故障静默变成一次越权：隔离子 Agent 的路径突然按主项目根
    判定，它就能读写整个项目了，而调用栈上没有任何线索。
    """
    if root is None:
        raise PathGuardError("未提供工作目录，无法进行路径边界判定")
    raw = str(root).strip()
    if not raw:
        raise PathGuardError("工作目录为空，无法进行路径边界判定")
    try:
        return Path(raw).resolve()
    except OSError as exc:
        raise PathGuardError(f"工作目录无法解析: {root}") from exc


def resolve_in_workspace(path: str, root: Union[str, Path, None]) -> Path:
    """
    把用户提供的路径解析为**给定工作目录内**的真实路径。

    :param path: 待解析的路径（相对或绝对）
    :param root: 本次判定的工作目录。**必填**，理由见模块 docstring
    :returns: 解析后的绝对路径
    :raises PathGuardError: 含 `..`、解析后越界、或 root 无效

    相对路径以 `root` 为基准；绝对路径只有在真实位置仍位于 `root` 内时才允许。
    `resolve(strict=False)` 会解析已存在父目录中的符号链接，因此指向 `root`
    之外的链接会被拒绝。
    """
    resolved_root = _normalize_root(root)

    raw = str(path)
    candidate = Path(raw)
    _ensure_no_parent_ref(candidate, raw)

    if not candidate.is_absolute():
        candidate = resolved_root / candidate

    resolved = candidate.resolve(strict=False)
    try:
        resolved.relative_to(resolved_root)
    except ValueError as exc:
        raise PathGuardError(f"路径超出工作目录: {raw}") from exc
    return resolved


def register_read_root(path: Path) -> None:
    """
    注册一个额外只读根目录（c9）。

    幂等：重复注册同一目录忽略。解析失败静默跳过——白名单是增强项，
    注册失败最坏是「模型读不了用户级笔记全文」，不该阻断启动。

    :param path: 要放行只读访问的目录（如 ~/.rhinecode/memory）

    副作用：向模块级白名单追加一项。
    """
    try:
        resolved = Path(path).resolve()
    except OSError:
        return
    if resolved not in _EXTRA_READ_ROOTS:
        _EXTRA_READ_ROOTS.append(resolved)


def clear_read_roots() -> None:
    """
    清空只读白名单。

    这是一条**正式的清理原语**（不再只服务测试）。除测试用来避免用例间互相污染外，
    装配层的清理动作也会调用它：白名单是**进程级全局状态**，一次装配会往里注册
    用户级 memory / skills 与内置 skills 三个目录。同一个进程内连续装配两次时
    （测试常见），若不清理，第二次会继承第一次注册的路径，于是「传了临时 user_dir
    却仍能读到上一次那个目录」——沙箱边界被悄悄放宽（trace spec F24）。

    副作用：清空模块级 `_EXTRA_READ_ROOTS`，影响后续所有读类路径判定。
    """
    _EXTRA_READ_ROOTS.clear()


def resolve_readable(path: str, root: Union[str, Path, None]) -> Path:
    """
    「读」类路径解析：给定工作区内 **或** 只读白名单内均放行（c9 F18）。

    :param path: 待解析的文件路径
    :param root: 本次判定的工作目录。**必填**
    :returns: 解析后的绝对路径
    :raises PathGuardError: 既不在工作区内、也不在任何只读白名单根内

    执行流程：
    1. 先按原有规则尝试 `resolve_in_workspace`（覆盖绝大多数正常读取）；
    2. 越界时走白名单分支：仍拒绝显式 `..`（防「先跳出再解析回来」绕过审计）、
       仅接受绝对路径（相对路径的基准永远是工作区，落到白名单只能靠绝对路径——
       记忆索引里给模型的正是绝对目录）；resolve 后位于任一白名单根内才放行；
    3. 两条路都不通 → 重抛工作区越界错误（对模型的报错口径与原来一致）。

    ⚠ 白名单分支**不看 root 的内容**：它的语义是「这些位置在任何工作目录下都可读」。
    因此隔离子 Agent 同样能读用户级记忆目录，与主对话一致（spec F5/AC5）。

    ⚠ **但 root 本身仍须有效**——所以下面第一句就先归一化它、让无效 root 当场抛出。
    不这么做的话会留下一条 N2 的旁路：`resolve_in_workspace` 因 root 无效而抛错，
    异常被 except 接住，白名单分支不看 root 于是照常放行——**漏传 cwd 时白名单
    读取仍然成功**。它不构成提权（白名单本来在任何 root 下都可读），但会造成
    「同一次漏传，工作区读取失败、白名单读取成功」这种一半一半的现象，
    排查的人会以为是白名单配错了。
    """
    _normalize_root(root)

    raw = str(path)
    try:
        return resolve_in_workspace(raw, root)
    except PathGuardError as workspace_error:
        candidate = Path(raw)
        _ensure_no_parent_ref(candidate, raw)
        if candidate.is_absolute():
            resolved = candidate.resolve(strict=False)
            for extra in _EXTRA_READ_ROOTS:
                try:
                    resolved.relative_to(extra)
                    return resolved
                except ValueError:
                    continue
        raise workspace_error


def is_readable_path(path: str, root: Union[str, Path, None]) -> bool:
    """
    `resolve_readable` 的布尔版（供权限引擎②沙箱层的 read 类判定使用，c9）。

    :param path: 待校验的文件路径
    :param root: 本次判定的工作目录。**必填**
    :returns: 可读返回 True

    与 `is_within_workspace` 同样的 fail-safe 语义：任何异常都按越界处理（False）。
    **root 无效同样返回 False**——它会走进 `_normalize_root` 抛出的 PathGuardError，
    被这里捕获成 False，而不是悄悄按主项目根放行（spec N2/AC3）。
    """
    try:
        resolve_readable(path, root)
        return True
    except PathGuardError:
        return False
    except Exception:
        return False


def is_within_workspace(path: str, root: Union[str, Path, None]) -> bool:
    """
    判断一个路径是否安全地落在给定工作目录内（布尔版的 `resolve_in_workspace`）。

    :param path: 待校验的文件路径（相对或绝对）
    :param root: 本次判定的工作目录。**必填**
    :returns: 位于工作区内返回 True；越界、root 无效或无法安全解析返回 False

    供权限系统的②沙箱层调用：引擎需要的是「是否越界」的布尔结论，而不是抛异常或
    拿到解析后的路径，因此这里复用 `resolve_in_workspace` 的边界逻辑，把它抛出的
    PathGuardError 捕获并转成 False，绝不向上抛异常（保证 `engine.decide` 是纯判定、
    不会因坏输入崩溃）。

    与 `resolve_in_workspace` 同样的边界：含 `..`、解析后越界的绝对路径、指向工作区
    外的符号链接都判为越界（返回 False）。其它无法解析的异常同样按越界处理
    （fail-safe，spec N1）。
    """
    try:
        resolve_in_workspace(path, root)
        return True
    except PathGuardError:
        return False
    except Exception:
        # 任何意料外的解析异常都按「不安全」处理，宁可错拒不可错放（fail-safe）。
        return False


def validate_glob_pattern(pattern: str) -> None:
    """
    校验 glob 模式只能在工作区内部展开。

    :param pattern: glob 模式
    :raises PathGuardError: 模式是绝对路径或含 `..`

    **本函数不需要 root**：它只检查模式**自身的形状**（不是绝对路径、不含上级引用），
    与具体在哪个工作目录下展开无关。展开后每个结果路径的越界判定仍由调用方
    用 `resolve_in_workspace(..., root)` 逐个完成。
    """
    raw = str(pattern)
    path = Path(raw)
    if path.is_absolute() or path.drive:
        raise PathGuardError(f"glob 模式不能是绝对路径: {raw}")
    _ensure_no_parent_ref(path, raw)


__all__ = [
    "PathGuardError",
    "main_project_root",
    "require_cwd",
    "worktrees_dir_of",
    "is_inside",
    "resolve_in_workspace",
    "resolve_readable",
    "is_readable_path",
    "is_within_workspace",
    "validate_glob_pattern",
    "register_read_root",
    "clear_read_roots",
]
