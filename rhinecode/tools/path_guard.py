"""
工具路径安全边界。

所有本地工具都以进程启动时的当前工作目录作为项目根。文件路径在使用前必须解析到
该根目录之内；glob 模式也不得使用绝对路径或 `..` 跳出项目根。
"""

from pathlib import Path


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


def validate_glob_pattern(pattern: str) -> None:
    """校验 glob 模式只能在工作区内部展开。"""
    raw = str(pattern)
    path = Path(raw)
    if path.is_absolute() or path.drive:
        raise PathGuardError(f"glob 模式不能是绝对路径: {raw}")
    _ensure_no_parent_ref(path, raw)
