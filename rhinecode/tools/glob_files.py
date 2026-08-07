"""
按模式找文件工具。

给定 glob 模式，返回工作目录下匹配的文件路径列表。属于只读工具
（read_only=True），不需确认且可并发执行。
"""

from pathlib import Path
from typing import Callable

from rhinecode.tools.base import Tool, ToolResult
from rhinecode.tools.path_guard import (
    PathGuardError,
    resolve_in_workspace,
    is_inside,
    require_cwd as _require_cwd,
    worktrees_dir_of,
    validate_glob_pattern,
)

# 返回的最大匹配文件数，避免在大型仓库中产出超长结果撑爆上下文。
MAX_RESULTS = 200
PathFilter = Callable[[str, "Path"], bool]
"""
逐文件权限过滤器（c6）：`(相对路径, 本次调用的工作目录) -> 是否放行`。

⚠ **c14 起第二个参数不可省。** 过滤器要构造一次权限判定，而第②层沙箱按
**调用者的工作目录**算边界——隔离子 Agent 的 grep 必须以它自己的工作区为界。

这个签名变更是**刻意让旧实现当场断掉**的：调用点外面包着
`except Exception: return False`（fail-safe，宁可少返回也不错放），
一个漏改的过滤器会**把所有文件都判成拒绝**，而工具照常返回 ok=True、
只是结果为空——用户看到的是「grep 什么都搜不到」，界面上没有任何异常。
真实踩过：c14 改造期漏了协调层那一处，整套搜索静默失效。
"""


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
    # c14：本工具碰路径/起子进程，必须知道调用者的工作目录。
    workspace_aware = True

    def __init__(self, path_filter: PathFilter | None = None):
        self._path_filter = path_filter

    def set_path_filter(self, path_filter: PathFilter | None) -> None:
        """设置实际返回文件前的权限过滤器；None 表示不过滤。"""
        self._path_filter = path_filter

    def _is_allowed(self, rel_path: str, base) -> bool:
        if self._path_filter is None:
            return True
        try:
            return bool(self._path_filter(rel_path, base))
        except Exception:
            return False

    def execute(self, args: dict, cwd=None) -> ToolResult:
        """
        按 glob 模式匹配文件。

        执行步骤：
        1. 取出 pattern
        2. 在工作目录用 Path.glob 匹配（pattern 含 ** 时自动递归）
        3. 仅保留文件（排除目录），按字典序返回相对路径，超出上限则截断

        :param args: 含 "pattern" 键
        :param cwd: 本次调用的工作目录（c14）。主对话与非隔离子 Agent 是主项目根，
                    隔离子 Agent 是它自己的隔离工作区
        :returns: 命中时 output 为每行一个路径；无匹配时 output 提示空、ok 仍为 True

        副作用：遍历文件系统（无写入）。
        """
        try:
            pattern = args.get("pattern")
            if not pattern:
                return ToolResult(ok=False, output="缺少必填参数 pattern", summary="缺少参数 pattern")

            validate_glob_pattern(pattern)
            base = _require_cwd(cwd)
            worktrees_dir = worktrees_dir_of(base)
            # 仅保留文件、排除目录；转为相对工作目录的路径，便于模型理解与后续操作
            matches = []
            skipped = 0
            for p in sorted(base.glob(pattern)):
                if not p.is_file():
                    continue
                if is_inside(p, worktrees_dir):
                    # c14 F18：隔离工作区里是同一份源码的副本，让它进搜索结果会
                    # 让主 Agent 对每个字符串拿到 N 份重复命中。要看子 Agent 的
                    # 成果走 `git diff <分支>`，那才是交付信息段里给出分支名的用意。
                    continue
                try:
                    resolve_in_workspace(str(p), base)
                except PathGuardError:
                    continue
                rel = str(p.relative_to(base))
                if not self._is_allowed(rel, base):
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
