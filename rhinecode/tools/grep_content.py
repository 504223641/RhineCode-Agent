"""
搜代码内容工具。

给定正则模式，在工作目录（或指定子路径）下的文本文件中逐行搜索，返回命中的
「文件:行号:行内容」列表。属于只读工具（read_only=True），不需确认且可并发执行。
"""

import os
import re
from pathlib import Path

from rhinecode.tools.base import Tool, ToolResult

# 返回的最大命中行数，避免超长结果。
MAX_MATCHES = 200
# 跳过的目录名（版本控制、虚拟环境、缓存等），减少噪声与无意义遍历。
SKIP_DIRS = {".git", ".venv", "venv", "__pycache__", "node_modules", ".mypy_cache", ".pytest_cache"}


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
                return ToolResult(ok=False, output="缺少必填参数 pattern")

            try:
                regex = re.compile(pattern)
            except re.error as e:
                return ToolResult(ok=False, output=f"正则表达式非法: {e}")

            target = Path(os.path.abspath(args.get("path") or "."))
            if not target.exists():
                return ToolResult(ok=False, output=f"路径不存在: {args.get('path')}")

            base = Path.cwd()
            # 统一收集待搜索文件列表：文件直接加入，目录递归收集
            files: list[Path] = []
            if target.is_file():
                files = [target]
            else:
                for root, dirs, filenames in os.walk(target):
                    # 原地修改 dirs 以阻止 os.walk 进入被跳过的目录
                    dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
                    for fn in filenames:
                        files.append(Path(root) / fn)

            matches: list[str] = []
            for fp in files:
                if len(matches) >= MAX_MATCHES:
                    break
                try:
                    with open(fp, "r", encoding="utf-8") as f:
                        for lineno, line in enumerate(f, start=1):
                            if regex.search(line):
                                try:
                                    rel = fp.relative_to(base)
                                except ValueError:
                                    rel = fp
                                matches.append(f"{rel}:{lineno}:{line.rstrip()}")
                                if len(matches) >= MAX_MATCHES:
                                    break
                except (UnicodeDecodeError, OSError):
                    # 二进制文件、无权限文件等静默跳过，不影响整体搜索
                    continue

            if not matches:
                return ToolResult(ok=True, output=f"无匹配内容（模式: {pattern}）")

            output = "\n".join(matches)
            if len(matches) >= MAX_MATCHES:
                output += f"\n…（已达上限 {MAX_MATCHES} 条，可能还有更多）"
            return ToolResult(ok=True, output=output)

        except Exception as e:
            return ToolResult(ok=False, output=f"搜索内容失败: {e}")
