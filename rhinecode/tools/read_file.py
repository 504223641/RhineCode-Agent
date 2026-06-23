"""
读文件工具。

给定文件路径，返回其文本内容。属于只读工具（read_only=True），
执行前不需用户确认，且可与其他只读工具并发执行。
"""

import os

from rhinecode.tools.base import Tool, ToolResult


class ReadFileTool(Tool):
    """读取指定文件的文本内容。"""

    name = "read_file"
    description = "读取指定路径文件的文本内容。用于查看源码、配置、文档等文本文件。"
    parameters = {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "要读取的文件路径，相对路径以项目工作目录为基准。",
            },
        },
        "required": ["path"],
    }
    read_only = True

    def execute(self, args: dict) -> ToolResult:
        """
        读取文件内容。

        执行步骤：
        1. 从 args 取出 path，以工作目录为基准解析为绝对路径
        2. 校验路径存在且不是目录
        3. 以 UTF-8 读取文本并返回

        :param args: 含 "path" 键
        :returns: 成功时 output 为文件内容；路径不存在、是目录、解码失败时 ok=False

        副作用：读取文件系统（无写入）。
        """
        try:
            path = args.get("path")
            if not path:
                return ToolResult(ok=False, output="缺少必填参数 path")

            # 相对路径以当前工作目录为基准，保证与命令执行、glob/grep 一致
            abs_path = os.path.abspath(path)

            if not os.path.exists(abs_path):
                return ToolResult(ok=False, output=f"文件不存在: {path}")
            if os.path.isdir(abs_path):
                return ToolResult(ok=False, output=f"路径是目录而非文件: {path}")

            with open(abs_path, "r", encoding="utf-8") as f:
                content = f.read()
            return ToolResult(ok=True, output=content)

        except UnicodeDecodeError:
            # 二进制文件或非 UTF-8 编码，无法作为文本读取
            return ToolResult(ok=False, output=f"文件无法以 UTF-8 文本解码（可能是二进制文件）: {args.get('path')}")
        except Exception as e:
            # 兜底：权限不足等其他异常统一转结构化错误，绝不向上抛出
            return ToolResult(ok=False, output=f"读取文件失败: {e}")
