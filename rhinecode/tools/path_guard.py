"""
工具路径安全边界。

所有本地工具都以进程启动时的当前工作目录作为项目根。文件路径在使用前必须解析到
该根目录之内；glob 模式也不得使用绝对路径或 `..` 跳出项目根。

c9 补充「额外只读根目录」白名单：用户级记忆目录（~/.rhinecode/memory/）位于项目
工作目录之外，但模型需要按记忆索引**只读**其中的笔记全文（spec F18）。白名单是
精确到目录的例外——只对「读」类判定生效（resolve_readable / is_readable_path），
写类判定仍走原有的 resolve_in_workspace / is_within_workspace，边界完全不动（N6③）。
"""

from pathlib import Path

# 额外只读根目录白名单：仅 read 类判定查询。启动时由协调层注册
# （当前只注册用户级 memory 目录），运行期不再变动。
_EXTRA_READ_ROOTS: list[Path] = []


class PathGuardError(ValueError):
    """路径或 glob 模式越过项目工作目录时抛出的结构化错误。"""


def workspace_root() -> Path:
    """返回当前项目工作目录的真实路径。"""
    return Path.cwd().resolve()


def _ensure_no_parent_ref(path: Path, raw: str) -> None:
    """拒绝显式 `..`，避免先跳出再解析回来的路径绕过审计。"""
    if ".." in path.parts:
        raise PathGuardError(f"路径不能包含 '..': {raw}")


def resolve_in_workspace(path: str) -> Path:
    """
    把用户提供的路径解析为工作区内的真实路径。

    相对路径以 `workspace_root()` 为基准；绝对路径只有在真实位置仍位于工作区内时才允许。
    `resolve(strict=False)` 会解析已存在父目录中的符号链接，因此指向工作区外的链接会被拒绝。
    """
    raw = str(path)
    candidate = Path(raw)
    _ensure_no_parent_ref(candidate, raw)

    root = workspace_root()
    if not candidate.is_absolute():
        candidate = root / candidate

    resolved = candidate.resolve(strict=False)
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise PathGuardError(f"路径超出项目工作目录: {raw}") from exc
    return resolved


def register_read_root(path: Path) -> None:
    """
    注册一个额外只读根目录（c9）。

    幂等：重复注册同一目录忽略。解析失败（如目录所在盘符异常）静默跳过——
    白名单是增强项，注册失败最坏是「模型读不了用户级笔记全文」，不该阻断启动。

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
    """清空只读白名单（仅测试用，避免用例间互相污染）。"""
    _EXTRA_READ_ROOTS.clear()


def resolve_readable(path: str) -> Path:
    """
    「读」类路径解析：工作区内 **或** 只读白名单内均放行（c9 F18）。

    执行流程：
    1. 先按原有规则尝试 resolve_in_workspace（覆盖绝大多数正常读取）；
    2. 越界时走白名单分支：仍拒绝显式 `..`（防「先跳出再解析回来」绕过审计）、
       仅接受绝对路径（相对路径的基准永远是工作区，落到白名单只能靠绝对路径——
       记忆索引里给模型的正是绝对目录）；resolve 后位于任一白名单根内才放行；
    3. 两条路都不通 → 重抛工作区越界错误（对模型的报错口径与原来一致）。

    :param path: 待解析的文件路径
    :returns: 解析后的绝对路径
    :raises PathGuardError: 既不在工作区内、也不在任何只读白名单根内
    """
    raw = str(path)
    try:
        return resolve_in_workspace(raw)
    except PathGuardError as workspace_error:
        candidate = Path(raw)
        _ensure_no_parent_ref(candidate, raw)
        if candidate.is_absolute():
            resolved = candidate.resolve(strict=False)
            for root in _EXTRA_READ_ROOTS:
                try:
                    resolved.relative_to(root)
                    return resolved
                except ValueError:
                    continue
        raise workspace_error


def is_readable_path(path: str) -> bool:
    """
    resolve_readable 的布尔版（供权限引擎②沙箱层的 read 类判定使用，c9）。

    与 is_within_workspace 同样的 fail-safe 语义：任何异常都按越界处理（False）。
    """
    try:
        resolve_readable(path)
        return True
    except PathGuardError:
        return False
    except Exception:
        return False


def is_within_workspace(path: str) -> bool:
    """
    判断一个路径是否安全地落在项目工作目录内（布尔版的 resolve_in_workspace）。

    供权限系统的②沙箱层调用：引擎需要的是「是否越界」的布尔结论，而不是抛异常或拿到
    解析后的路径，因此这里复用 resolve_in_workspace 的边界逻辑，把它抛出的 PathGuardError
    捕获并转成 False，绝不向上抛异常（保证 engine.decide 是纯判定、不会因坏输入崩溃）。

    与 resolve_in_workspace 同样的边界：含 `..`、解析后越界的绝对路径、指向工作区外的
    符号链接都判为越界（返回 False）。其它无法解析的异常同样按越界处理（fail-safe，spec N1）。

    :param path: 待校验的文件路径（相对或绝对）
    :returns: 位于工作区内返回 True；越界或无法安全解析返回 False
    """
    try:
        resolve_in_workspace(path)
        return True
    except PathGuardError:
        return False
    except Exception:
        # 任何意料外的解析异常都按「不安全」处理，宁可错拒不可错放（fail-safe）。
        return False


def validate_glob_pattern(pattern: str) -> None:
    """校验 glob 模式只能在工作区内部展开。"""
    raw = str(pattern)
    path = Path(raw)
    if path.is_absolute() or path.drive:
        raise PathGuardError(f"glob 模式不能是绝对路径: {raw}")
    _ensure_no_parent_ref(path, raw)
