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
from rhinecode.tools.path_guard import (
    PathGuardError,
    is_inside,
    require_cwd as _require_cwd,
    resolve_in_workspace,
    runtime_artifact_dirs_of,
    worktrees_dir_of,
)

# 返回的最大命中行数，避免超长结果。
MAX_MATCHES = 200
# 单条命中行保留的最大字符数，超出部分截断并就地标注。
#
# ## 为什么需要它（2026-09-17）
#
# `MAX_MATCHES` 限的是**条数**，而单行长度不限。搜一个压缩过的 JS 或单行 JSON，
# 200 条命中每条几万字符，一次搜索就能产出上百万字符。
#
# 这在 c8 第一层存盘还在的时候由那一层兜着（塞进去、下一轮换成占位）。第一层
# 已整层删除——上游两家都是在工具**产出的那一刻**限量、进了历史就不再动——
# 所以每个可能产出大结果的工具都要自己把住这一关。`read_file` 与 `run_command`
# 各有自己的字符预算，这里是同一件事在搜索工具上的落点。
#
# 取 500：一行源码通常在 120 字符以内，500 足够把命中行连同上下文看清楚；
# 200 条 × 500 字符 ≈ 100 000 字符，与 `read_file` 的预算同量级。
#
# ⚠ **截断要就地标注**，不能默默切掉——模型看不出这行还有后半截，会拿着半行
# 代码去推理（同 `read_file` 那条超长单行的教训）。真要看全文请它去读那个文件。
MAX_LINE_CHARS = 500
# 跳过的目录名（版本控制、虚拟环境、缓存等），减少噪声与无意义遍历。
SKIP_DIRS = {".git", ".venv", "venv", "__pycache__", "node_modules", ".mypy_cache", ".pytest_cache"}

def _clip_line(line: str) -> str:
    """
    把单条命中行截到 `MAX_LINE_CHARS`，超出时就地标注。

    :param line: 原始命中行
    :returns: 不超过上限的行（必要时带「…（本行过长…）」标注）

    副作用：无（纯函数）。
    """
    if len(line) <= MAX_LINE_CHARS:
        return line
    return line[:MAX_LINE_CHARS] + f"…（本行过长，已截断，原长 {len(line)} 字）"


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
    # c14：本工具碰路径/起子进程，必须知道调用者的工作目录。
    workspace_aware = True
    # 显示正则本身而不是搜索范围——「在找什么」比「在哪找」更能说明这次调用
    primary_arg = "pattern"

    def __init__(self, path_filter: PathFilter | None = None):
        self._path_filter = path_filter

    def set_path_filter(self, path_filter: PathFilter | None) -> None:
        """设置实际读取文件前的权限过滤器；None 表示不过滤。"""
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

            base = _require_cwd(cwd)
            worktrees_dir = worktrees_dir_of(base)
            artifact_dirs = runtime_artifact_dirs_of(base)
            target = resolve_in_workspace(args.get("path") or ".", base)
            if not target.exists():
                return ToolResult(ok=False, output=f"路径不存在: {args.get('path')}", summary="路径不存在")

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
                        # c14 F18：隔离工作区是同一份源码的副本，进搜索结果会让
                        # 主 Agent 对每个字符串拿到 N 份重复命中。要看子 Agent
                        # 的成果走 `git diff <分支>`。
                        #
                        # ⚠ 这条**不能并进上面的 SKIP_DIRS**：那张表按**目录名**
                        # 匹配，而这里必须按**路径相等**判断，否则会误伤用户自己
                        # 叫 worktrees 的业务目录。两者语义不同，刻意分开。
                        if is_inside(child, worktrees_dir):
                            continue
                        # 运行期产物（会话存档 / 上下文存盘 / 行为记录）同样跳过。
                        # 它们逐字复刻了对话与工具输出，进搜索结果会把真正的源码
                        # 命中淹掉，还会形成「搜索 → 存盘 → 搜到存盘」的自放大。
                        # 详见 `runtime_artifact_dirs_of` 的 docstring。
                        if any(is_inside(child, d) for d in artifact_dirs):
                            continue
                        try:
                            resolve_in_workspace(str(child), base)
                        except PathGuardError:
                            continue
                        safe_dirs.append(d)
                    dirs[:] = safe_dirs
                    for fn in filenames:
                        fp = Path(root) / fn
                        try:
                            files.append(resolve_in_workspace(str(fp), base))
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
                if not self._is_allowed(rel, base):
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
                lines_out.append(f"  {lineno}│ {_clip_line(line)}")
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
