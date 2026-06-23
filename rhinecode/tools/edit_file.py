"""
改文件工具。

在文件中以「原文唯一匹配替换」的方式做精确编辑：把 old_string 替换为
new_string，但要求 old_string 在文件中恰好出现一次。属于有副作用工具
（read_only=False），执行前需用户确认，且串行执行。

为什么要求唯一匹配：
- 匹配 0 次说明模型提供的原文有误，直接替换会无声失败
- 匹配多次说明定位不唯一，盲目全替换可能改错位置
两种情况都返回带出现次数的清晰错误，让模型补充更多上下文后重试（spec F4）。
"""

import os

from rhinecode.tools.base import Tool, ToolResult


class EditFileTool(Tool):
    """以原文唯一匹配的方式替换文件中的一段文本。"""

    name = "edit_file"
    description = (
        "对文件做精确局部修改：把 old_string 替换为 new_string。"
        "要求 old_string 在文件中唯一出现；为保证唯一性，old_string 应包含足够的上下文。"
        "若匹配 0 次或多次会返回错误，请补充上下文后重试。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "要修改的文件路径，相对路径以项目工作目录为基准。",
            },
            "old_string": {
                "type": "string",
                "description": "要被替换的原文片段，必须与文件中的内容逐字符一致，且在文件中唯一出现。",
            },
            "new_string": {
                "type": "string",
                "description": "替换后的新文片段。",
            },
        },
        "required": ["path", "old_string", "new_string"],
    }
    read_only = False

    def execute(self, args: dict) -> ToolResult:
        """
        执行唯一匹配替换。

        执行步骤：
        1. 取出 path / old_string / new_string，解析绝对路径并校验文件存在
        2. 读取文件，统计 old_string 出现次数
        3. 次数为 0 或 ≥2：返回带次数的错误，且不修改文件
        4. 次数为 1：替换并写回

        :param args: 含 "path" / "old_string" / "new_string" 键
        :returns: 成功时 output 说明已替换；匹配次数不为 1 或异常时 ok=False

        副作用：仅在唯一匹配成功时覆盖写入目标文件。
        """
        try:
            path = args.get("path")
            old_string = args.get("old_string")
            new_string = args.get("new_string")
            if not path:
                return ToolResult(ok=False, output="缺少必填参数 path")
            if old_string is None:
                return ToolResult(ok=False, output="缺少必填参数 old_string")
            if new_string is None:
                return ToolResult(ok=False, output="缺少必填参数 new_string")

            abs_path = os.path.abspath(path)
            if not os.path.exists(abs_path):
                return ToolResult(ok=False, output=f"文件不存在: {path}")
            if os.path.isdir(abs_path):
                return ToolResult(ok=False, output=f"路径是目录而非文件: {path}")

            with open(abs_path, "r", encoding="utf-8") as f:
                content = f.read()

            # 统计出现次数，唯一匹配才执行替换
            count = content.count(old_string)
            if count == 0:
                return ToolResult(
                    ok=False,
                    output=f"在 {path} 中未找到 old_string（匹配 0 次），请确认原文是否准确。",
                )
            if count > 1:
                return ToolResult(
                    ok=False,
                    output=f"old_string 在 {path} 中出现 {count} 次，定位不唯一；请在 old_string 中加入更多上下文以唯一定位。",
                )

            new_content = content.replace(old_string, new_string)
            with open(abs_path, "w", encoding="utf-8") as f:
                f.write(new_content)

            return ToolResult(ok=True, output=f"已在 {path} 完成 1 处替换。")

        except UnicodeDecodeError:
            return ToolResult(ok=False, output=f"文件无法以 UTF-8 文本解码（可能是二进制文件）: {args.get('path')}")
        except Exception as e:
            return ToolResult(ok=False, output=f"编辑文件失败: {e}")
