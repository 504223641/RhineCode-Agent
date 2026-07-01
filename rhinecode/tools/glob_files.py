"""
按模式找文件工具。

给定 glob 模式，返回工作目录下匹配的文件路径列表。属于只读工具
（read_only=True），不需确认且可并发执行。
"""

from typing import Callable

from rhinecode.tools.base import Tool, ToolResult
from rhinecode.tools.path_guard import (
    PathGuardError,
    resolve_in_workspace,
    validate_glob_pattern,
    workspace_root,
)

# 返回的最大匹配文件数，避免在大型仓库中产出超长结果撑爆上下文。
MAX_RESULTS = 200
PathFilter = Callable[[str], bool]


class GlobTool(Tool):
    """按 glob 模式查找文件路径。"""

    name = "glob_files"
    description = (
        "按 glob 模式在项目工作目录下查找文件，返回匹配的相对路径列表。"
        "支持 ** 递归，例如 '**/*.py' 匹配所有 Python 文件、'src/*.md' 匹配 src 下的 Markdown。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "pattern": {
                "type": "string",
                "description": "glob 模式，相对项目工作目录，例如 '**/*.py'。",
            },
        },
        "required": ["pattern"],
    }
    read_only = True

    def __init__(self, path_filter: PathFilter | None = None):
        self._path_filter = path_filter

    def set_path_filter(self, path_filter: PathFilter | None) -> None:
        """设置实际返回文件前的权限过滤器；None 表示不过滤。"""
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
        按 glob 模式匹配文件。

        执行步骤：
        1. 取出 pattern
        2. 在工作目录用 Path.glob 匹配（pattern 含 ** 时自动递归）
        3. 仅保留文件（排除目录），按字典序返回相对路径，超出上限则截断

        :param args: 含 "pattern" 键
        :returns: 命中时 output 为每行一个路径；无匹配时 output 提示空、ok 仍为 True

        副作用：遍历文件系统（无写入）。
        """
        try:
            pattern = args.get("pattern")
            if not pattern:
                return ToolResult(ok=False, output="缺少必填参数 pattern", summary="缺少参数 pattern")

            validate_glob_pattern(pattern)
            base = workspace_root()
            # 仅保留文件、排除目录；转为相对工作目录的路径，便于模型理解与后续操作
            matches = []
            skipped = 0
            for p in sorted(base.glob(pattern)):
                if not p.is_file():
                    continue
                try:
                    resolve_in_workspace(str(p))
                except PathGuardError:
                    continue
                rel = str(p.relative_to(base))
                if not self._is_allowed(rel):
                    skipped += 1
                    continue
                matches.append(rel)

            total = len(matches)
            if total == 0:
                suffix = f"；跳过 {skipped} 个被权限规则拒绝的文件" if skipped else ""
                summary = f"无匹配 · 跳过 {skipped} 个" if skipped else "无匹配"
                return ToolResult(ok=True, output=f"无匹配文件（模式: {pattern}）{suffix}", summary=summary)

            shown = matches[:MAX_RESULTS]
            # 顶部计数头 + 路径列表
            output = f"找到 {total} 个文件：\n" + "\n".join(shown)
            if total > MAX_RESULTS:
                output += f"\n…（共 {total} 个，仅显示前 {MAX_RESULTS} 个）"
                summary = f"找到 {total} 个（显示前 {MAX_RESULTS}）"
            else:
                summary = f"找到 {total} 个文件"
            if skipped:
                output += f"\n…（跳过 {skipped} 个被权限规则拒绝的文件）"
                summary += f" · 跳过 {skipped} 个"
            return ToolResult(ok=True, output=output, summary=summary)

        except PathGuardError as e:
            return ToolResult(ok=False, output=str(e), summary="路径越界")
        except Exception as e:
            return ToolResult(ok=False, output=f"查找文件失败: {e}", summary="查找失败")
