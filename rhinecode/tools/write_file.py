"""
写文件工具。

给定文件路径与内容，将内容写入文件（覆盖已有或新建）。属于有副作用工具
（read_only=False），执行前需用户确认，且不与其他工具并发执行（串行）。
"""

from rhinecode.tools.base import Tool, ToolResult, human_size
from rhinecode.tools.diff import build_diff
from rhinecode.tools.path_guard import PathGuardError, resolve_in_workspace


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
        2. 写前判断文件是否已存在（用于区分「新建」与「覆盖」），覆盖时读取旧内容以便算 diff
        3. 父目录不存在则递归创建
        4. 以 UTF-8 写入，构造差异，返回含新建/覆盖、行数、字节数的摘要

        :param args: 含 "path" 与 "content" 键
        :returns: 成功时 output 说明写入路径、新建/覆盖、行数与字节数，并附结构化 diff；异常时 ok=False

        副作用：创建/覆盖文件系统中的文件，可能创建父目录。
        """
        try:
            path = args.get("path")
            content = args.get("content")
            if not path:
                return ToolResult(ok=False, output="缺少必填参数 path", summary="缺少参数 path")
            if content is None:
                return ToolResult(ok=False, output="缺少必填参数 content", summary="缺少参数 content")

            abs_path = resolve_in_workspace(path)

            # 写入前判断，区分新建/覆盖（写入后再判断就分不清了）
            existed = abs_path.exists()

            # 覆盖已有文件时先读旧内容，供 diff 对比；新建（或旧内容读不出）时按空文本处理。
            # 二进制/编码异常不应阻断写入，只是退化为「全是新增行」的 diff，故吞掉异常。
            old_content = ""
            if existed and abs_path.is_file():
                try:
                    with open(abs_path, "r", encoding="utf-8") as f:
                        old_content = f.read()
                except (UnicodeDecodeError, OSError):
                    old_content = ""

            # 父目录缺失时递归创建，避免因目录不存在导致写入失败
            abs_path.parent.mkdir(parents=True, exist_ok=True)

            with open(abs_path, "w", encoding="utf-8") as f:
                f.write(content)

            byte_len = len(content.encode("utf-8"))
            # 空内容算 0 行；否则行数 = 换行数 + 1（末尾无换行也算最后一行）
            line_count = 0 if content == "" else content.count("\n") + 1
            action = "覆盖" if existed else "新建"

            # 结构化差异：新建时 old_content 为空 → 全部为新增行；覆盖时展示新旧逐行差异。
            diff_view = build_diff("Write", path, old_content, content)

            return ToolResult(
                ok=True,
                output=f"已写入 {path}（{action}，{line_count} 行，{byte_len} 字节）\n{diff_view.to_text()}",
                summary=f"{action} · {line_count} 行 · {human_size(byte_len)}",
                diff=diff_view,
            )

        except PathGuardError as e:
            return ToolResult(ok=False, output=str(e), summary="路径越界")
        except Exception as e:
            return ToolResult(ok=False, output=f"写入文件失败: {e}", summary="写入失败")
