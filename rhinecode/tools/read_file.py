"""
读文件工具。

给定文件路径，返回其文本内容。属于只读工具（read_only=True），
执行前不需用户确认，且可与其他只读工具并发执行。
"""

from rhinecode.tools.base import Tool, ToolResult, human_size
from rhinecode.tools.path_guard import PathGuardError, resolve_in_workspace


class ReadFileTool(Tool):
    """读取指定文件的文本内容。"""

    name = "read_file"
    description = (
        "读取指定路径文件的文本内容，用于查看源码、配置、文档等文本文件。"
        "返回内容首行是文件元信息，正文每行带形如 ' 12│ ' 的行号前缀，便于定位行。"
        "注意：行号前缀仅用于显示与定位，不属于文件真实内容；若随后用 edit_file 编辑，"
        "old_string 必须是去掉行号前缀后的原始文本。"
    )
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
        3. 以 UTF-8 读取文本，构造「头部元信息 + 带行号正文」的 output
        4. 同时给出量级 summary 供 TUI 单行展示

        :param args: 含 "path" 键
        :returns: 成功时 output 为「文件: path · N 行 · 体量」头部加每行带行号的正文；
                  路径不存在、是目录、解码失败时 ok=False

        副作用：读取文件系统（无写入）。
        """
        try:
            path = args.get("path")
            if not path:
                return ToolResult(ok=False, output="缺少必填参数 path", summary="缺少参数 path")

            # 只读工具不经过确认，因此必须先把路径钉死在项目工作目录内。
            abs_path = resolve_in_workspace(path)

            if not abs_path.exists():
                return ToolResult(ok=False, output=f"文件不存在: {path}", summary="文件不存在")
            if abs_path.is_dir():
                return ToolResult(ok=False, output=f"路径是目录而非文件: {path}", summary="不是文件")

            with open(abs_path, "r", encoding="utf-8") as f:
                content = f.read()

            byte_len = len(content.encode("utf-8"))

            # 空文件：给出明确提示，避免返回空白让模型困惑
            if content == "":
                header = f"文件: {path} · 0 行 · 0 B（空文件）"
                return ToolResult(ok=True, output=header, summary="空文件")

            # splitlines() 不含换行符；行号宽度按总行数对齐，保证 │ 竖线对齐
            lines = content.splitlines()
            total = len(lines)
            width = len(str(total))
            header = f"文件: {path} · {total} 行 · {human_size(byte_len)}"
            numbered = "\n".join(f"{i:>{width}}│ {line}" for i, line in enumerate(lines, start=1))
            output = f"{header}\n{numbered}"

            return ToolResult(ok=True, output=output, summary=f"读取 {total} 行 · {human_size(byte_len)}")

        except UnicodeDecodeError:
            # 二进制文件或非 UTF-8 编码，无法作为文本读取
            return ToolResult(
                ok=False,
                output=f"文件无法以 UTF-8 文本解码（可能是二进制文件）: {args.get('path')}",
                summary="非文本文件",
            )
        except PathGuardError as e:
            return ToolResult(ok=False, output=str(e), summary="路径越界")
        except Exception as e:
            # 兜底：权限不足等其他异常统一转结构化错误，绝不向上抛出
            return ToolResult(ok=False, output=f"读取文件失败: {e}", summary="读取失败")
