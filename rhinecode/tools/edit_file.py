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

from rhinecode.tools.base import Tool, ToolResult
from rhinecode.tools.path_guard import PathGuardError, resolve_in_workspace

# 成功后回显「变更附近」片段的最大行数，超过则截断，避免大段替换刷屏。
EDIT_CONTEXT_MAX = 14
# 变更区域上下各保留的上下文行数。
EDIT_CONTEXT_PAD = 2


class EditFileTool(Tool):
    """以原文唯一匹配的方式替换文件中的一段文本。"""

    name = "edit_file"
    description = (
        "对文件做精确局部修改：把 old_string 替换为 new_string。"
        "要求 old_string 在文件中唯一出现；为保证唯一性，old_string 应包含足够的上下文。"
        "若匹配 0 次或多次会返回错误，请补充上下文后重试。"
        "重要：old_string 必须是文件的真实内容，不要包含 read_file 显示用的 ' N│ ' 行号前缀。"
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
                return ToolResult(ok=False, output="缺少必填参数 path", summary="缺少参数 path")
            if old_string is None:
                return ToolResult(ok=False, output="缺少必填参数 old_string", summary="缺少参数 old_string")
            if new_string is None:
                return ToolResult(ok=False, output="缺少必填参数 new_string", summary="缺少参数 new_string")

            abs_path = resolve_in_workspace(path)
            if not abs_path.exists():
                return ToolResult(ok=False, output=f"文件不存在: {path}", summary="文件不存在")
            if abs_path.is_dir():
                return ToolResult(ok=False, output=f"路径是目录而非文件: {path}", summary="不是文件")

            with open(abs_path, "r", encoding="utf-8") as f:
                content = f.read()

            # 统计出现次数，唯一匹配才执行替换
            count = content.count(old_string)
            if count == 0:
                return ToolResult(
                    ok=False,
                    output=f"在 {path} 中未找到 old_string（匹配 0 次），请确认原文是否准确。",
                    summary="未找到原文",
                )
            if count > 1:
                return ToolResult(
                    ok=False,
                    output=f"old_string 在 {path} 中出现 {count} 次，定位不唯一；请在 old_string 中加入更多上下文以唯一定位。",
                    summary=f"原文出现 {count} 次，需更精确",
                )

            # 记录替换位置（用于回显变更附近片段）后再替换写回
            idx = content.index(old_string)
            new_content = content.replace(old_string, new_string)
            with open(abs_path, "w", encoding="utf-8") as f:
                f.write(new_content)

            # 净行数变化 = 新内容换行数 - 原内容换行数
            delta = new_content.count("\n") - content.count("\n")
            snippet = self._context_snippet(content, new_content, idx, new_string)

            output = f"已替换 {path} 1 处（净 {delta:+d} 行）\n变更附近：\n{snippet}"
            return ToolResult(ok=True, output=output, summary=f"替换 1 处 · 净 {delta:+d} 行")

        except UnicodeDecodeError:
            return ToolResult(
                ok=False,
                output=f"文件无法以 UTF-8 文本解码（可能是二进制文件）: {args.get('path')}",
                summary="非文本文件",
            )
        except PathGuardError as e:
            return ToolResult(ok=False, output=str(e), summary="路径越界")
        except Exception as e:
            return ToolResult(ok=False, output=f"编辑文件失败: {e}", summary="编辑失败")

    @staticmethod
    def _context_snippet(old_content: str, new_content: str, idx: int, new_string: str) -> str:
        """
        构造替换后「变更附近」的带行号片段，供模型确认改动结果而无需重新读文件。

        :param old_content: 替换前的完整内容（用 idx 定位改动起始行）
        :param new_content: 替换后的完整内容（片段行号取自此）
        :param idx: old_string 在 old_content 中的起始字符下标
        :param new_string: 替换进去的新文本（用于估算改动区域行数）
        :returns: 形如 ' 10│ ...' 的多行片段；过长则截断并以 '…' 结尾

        说明：以改动起始行为中心，上下各取 EDIT_CONTEXT_PAD 行；
        若窗口超过 EDIT_CONTEXT_MAX 行则截断。
        """
        new_lines = new_content.splitlines()
        total = len(new_lines)
        # 改动起始行（1-based）= 改动前 idx 之前的换行数 + 1
        start_line = old_content[:idx].count("\n") + 1
        # 新区域行数 = 新文本换行数 + 1
        region = new_string.count("\n") + 1
        end_line = start_line + region - 1

        s = max(1, start_line - EDIT_CONTEXT_PAD)
        e = min(total, end_line + EDIT_CONTEXT_PAD)
        truncated = False
        if e - s + 1 > EDIT_CONTEXT_MAX:
            e = s + EDIT_CONTEXT_MAX - 1
            truncated = True

        width = len(str(e))
        rendered = "\n".join(f"{i:>{width}}│ {new_lines[i - 1]}" for i in range(s, e + 1))
        if truncated:
            rendered += "\n…"
        return rendered
