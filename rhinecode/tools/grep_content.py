"""
搜代码内容工具。

给定正则模式，在工作目录（或指定子路径）下的文本文件中逐行搜索，返回命中的
「文件:行号:行内容」列表。属于只读工具（read_only=True），不需确认且可并发执行。
"""

import os
import re
from pathlib import Path
from typing import Callable

from rhinecode.tools.base import Tool, ToolResult
from rhinecode.tools.path_guard import PathGuardError, resolve_in_workspace, workspace_root

# 返回的最大命中行数，避免超长结果。
MAX_MATCHES = 200
# 跳过的目录名（版本控制、虚拟环境、缓存等），减少噪声与无意义遍历。
SKIP_DIRS = {".git", ".venv", "venv", "__pycache__", "node_modules", ".mypy_cache", ".pytest_cache"}

PathFilter = Callable[[str], bool]


class GrepTool(Tool):
    """在文本文件中按正则搜索内容。"""

    name = "grep_content"
    description = (
        "在项目工作目录（或指定子路径）下的文本文件中按正则表达式逐行搜索，"
        "返回命中的 文件:行号:行内容。用于查找函数定义、变量引用、关键字等。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "pattern": {
                "type": "string",
                "description": "Python 正则表达式。",
            },
            "path": {
                "type": "string",
                "description": "搜索的起始目录或文件，相对项目工作目录（可选，默认当前目录 '.'）。",
            },
        },
        "required": ["pattern"],
    }
    read_only = True

    def __init__(self, path_filter: PathFilter | None = None):
        self._path_filter = path_filter

    def set_path_filter(self, path_filter: PathFilter | None) -> None:
        """设置实际读取文件前的权限过滤器；None 表示不过滤。"""
        self._path_filter = path_filter

    def _is_allowed(self, rel_path: str) -> bool:
        if self._path_filter is None:
            return True
        try:
            return bool(self._path_filter(rel_path))
        except Exception:
            return False

    def execute(self, args: dict) -> ToolResult:
        """
        在文本文件中按正则逐行搜索。

        执行步骤：
        1. 编译 pattern，解析 path（默认 '.'）
        2. 若 path 是文件则只搜该文件，是目录则递归遍历（跳过 SKIP_DIRS）
        3. 逐行匹配，记录 文件:行号:行内容，达到上限即停止
        4. 二进制/不可解码文件静默跳过

        :param args: 含 "pattern"（必填）与 "path"（可选）键
        :returns: 命中时 output 为每行一条结果；无命中时提示空、ok 仍为 True；
                  正则非法时 ok=False

        副作用：遍历并读取文件系统中的文本文件（无写入）。
        """
        try:
            pattern = args.get("pattern")
            if not pattern:
                return ToolResult(ok=False, output="缺少必填参数 pattern", summary="缺少参数 pattern")

            try:
                regex = re.compile(pattern)
            except re.error as e:
                return ToolResult(ok=False, output=f"正则表达式非法: {e}", summary="正则非法")

            target = resolve_in_workspace(args.get("path") or ".")
            if not target.exists():
                return ToolResult(ok=False, output=f"路径不存在: {args.get('path')}", summary="路径不存在")

            base = workspace_root()
            # 统一收集待搜索文件列表：文件直接加入，目录递归收集
            files: list[Path] = []
            if target.is_file():
                files = [target]
            else:
                for root, dirs, filenames in os.walk(target):
                    # 原地修改 dirs：跳过噪声目录，并防止符号链接或异常路径越过工作区。
                    safe_dirs = []
                    for d in dirs:
                        if d in SKIP_DIRS:
                            continue
                        child = Path(root) / d
                        try:
                            resolve_in_workspace(str(child))
                        except PathGuardError:
                            continue
                        safe_dirs.append(d)
                    dirs[:] = safe_dirs
                    for fn in filenames:
                        fp = Path(root) / fn
                        try:
                            files.append(resolve_in_workspace(str(fp)))
                        except PathGuardError:
                            continue

            # 收集 (相对路径, 行号, 行内容) 三元组，保持遍历顺序，便于后续按文件分组
            matches: list[tuple[str, int, str]] = []
            skipped = 0
            for fp in files:
                if len(matches) >= MAX_MATCHES:
                    break
                try:
                    rel = str(fp.relative_to(base))
                except ValueError:
                    rel = str(fp)
                if not self._is_allowed(rel):
                    skipped += 1
                    continue
                try:
                    with open(fp, "r", encoding="utf-8") as f:
                        for lineno, line in enumerate(f, start=1):
                            if regex.search(line):
                                matches.append((rel, lineno, line.rstrip()))
                                if len(matches) >= MAX_MATCHES:
                                    break
                except (UnicodeDecodeError, OSError):
                    # 二进制文件、无权限文件等静默跳过，不影响整体搜索
                    continue

            total = len(matches)
            if total == 0:
                suffix = f"；跳过 {skipped} 个被权限规则拒绝的文件" if skipped else ""
                summary = f"无匹配 · 跳过 {skipped} 个" if skipped else "无匹配"
                return ToolResult(ok=True, output=f"无匹配内容（模式: {pattern}）{suffix}", summary=summary)

            file_count = len({rel for rel, _, _ in matches})
            capped = total >= MAX_MATCHES

            # 按文件分组输出：文件名独占一行，其下命中行以「行号│ 内容」缩进展示
            lines_out: list[str] = [f"{total} 处匹配 · {file_count} 个文件"]
            current = None
            for rel, lineno, line in matches:
                if rel != current:
                    lines_out.append(rel)
                    current = rel
                lines_out.append(f"  {lineno}│ {line}")
            if capped:
                lines_out.append(f"…（已达上限 {MAX_MATCHES} 条，可能还有更多）")
            if skipped:
                lines_out.append(f"…（跳过 {skipped} 个被权限规则拒绝的文件）")
            output = "\n".join(lines_out)

            summary = f"≥{MAX_MATCHES} 处（已截断）" if capped else f"{total} 处匹配 · {file_count} 个文件"
            if skipped:
                summary += f" · 跳过 {skipped} 个"
            return ToolResult(ok=True, output=output, summary=summary)

        except PathGuardError as e:
            return ToolResult(ok=False, output=str(e), summary="路径越界")
        except Exception as e:
            return ToolResult(ok=False, output=f"搜索内容失败: {e}", summary="搜索失败")
