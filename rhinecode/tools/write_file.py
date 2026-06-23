"""
写文件工具。

给定文件路径与内容，将内容写入文件（覆盖已有或新建）。属于有副作用工具
（read_only=False），执行前需用户确认，且不与其他工具并发执行（串行）。
"""

import os

from rhinecode.tools.base import Tool, ToolResult


class WriteFileTool(Tool):
    """把内容写入指定文件，覆盖已有内容或新建文件。"""

    name = "write_file"
    description = (
        "把给定内容写入指定路径的文件：文件已存在则覆盖，不存在则新建（必要时自动创建父目录）。"
        "用于生成新文件或整体重写文件。仅修改局部内容时应优先用 edit_file。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "要写入的文件路径，相对路径以项目工作目录为基准。",
            },
            "content": {
                "type": "string",
                "description": "要写入文件的完整文本内容。",
            },
        },
        "required": ["path", "content"],
    }
    read_only = False

    def execute(self, args: dict) -> ToolResult:
        """
        写入文件内容。

        执行步骤：
        1. 取出 path 与 content，解析绝对路径
        2. 父目录不存在则递归创建
        3. 以 UTF-8 覆盖写入，返回写入字节数摘要

        :param args: 含 "path" 与 "content" 键
        :returns: 成功时 output 说明写入路径与字节数；异常时 ok=False

        副作用：创建/覆盖文件系统中的文件，可能创建父目录。
        """
        try:
            path = args.get("path")
            content = args.get("content")
            if not path:
                return ToolResult(ok=False, output="缺少必填参数 path")
            if content is None:
                return ToolResult(ok=False, output="缺少必填参数 content")

            abs_path = os.path.abspath(path)

            # 父目录缺失时递归创建，避免因目录不存在导致写入失败
            parent = os.path.dirname(abs_path)
            if parent and not os.path.exists(parent):
                os.makedirs(parent, exist_ok=True)

            with open(abs_path, "w", encoding="utf-8") as f:
                f.write(content)

            byte_len = len(content.encode("utf-8"))
            return ToolResult(ok=True, output=f"已写入 {path}（{byte_len} 字节）")

        except Exception as e:
            return ToolResult(ok=False, output=f"写入文件失败: {e}")
