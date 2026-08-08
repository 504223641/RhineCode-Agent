"""
改文件工具。

在文件中以「原文匹配替换」的方式做精确编辑。支持两种用法，二选一：

1. 单处替换：传顶层 old_string / new_string（可选 replace_all），改一处文本。
2. 批量替换：传 edits 数组，一次调用按顺序应用多处替换（每项 {old_string, new_string, replace_all?}）。

无论哪种用法，都属于有副作用工具（read_only=False），执行前需用户确认，且串行执行。

为什么要求匹配次数受控（唯一匹配 / 显式 replace_all）：
- 匹配 0 次说明模型提供的原文有误，直接替换会无声失败；
- 匹配多次而未声明 replace_all 时，定位不唯一，盲目全替换可能改错位置。
两种情况都返回带「第几处编辑 + 出现次数」的清晰错误，让模型补充上下文或显式 replace_all 后重试。

原子性（all-or-nothing）：
- 先把整个文件读入内存，按顺序对「内存副本」逐项应用替换；
- 任何一处编辑失败（找不到 / 多处但未声明 replace_all）立即返回错误，且绝不写盘，文件保持原样；
- 全部编辑成功后，才把最终内容一次性写回磁盘。
这样多处编辑不会出现「改了一半」的中间状态。

顺序语义：edits 按数组顺序依次作用于「上一处编辑后的内容」，因此后一处的 old_string
可以命中前一处刚替换进去的文本（与常见编辑器的多重替换一致）。
"""

from rhinecode.tools.base import Tool, ToolResult
from rhinecode.tools.diff import build_diff
from rhinecode.tools.path_guard import PathGuardError, require_cwd as _require_cwd, resolve_in_workspace


class EditFileTool(Tool):
    """以原文匹配的方式替换文件中的一段或多段文本（支持一次调用批量替换）。"""

    name = "edit_file"
    description = (
        "对文件做精确局部修改。编辑前必须先用 read_file 读取该文件：未读取就直接编辑，"
        "极易因不了解原文导致 old_string 匹配失败或改错位置。"
        "两种用法二选一："
        "①单处：传 old_string 与 new_string；"
        "②批量：传 edits 数组，一次调用完成多处替换（推荐用于需要改动多个位置的场景，避免反复调用）。"
        "默认要求每个 old_string 在当前内容中唯一出现（包含足够上下文以保证唯一）；"
        "若该片段会重复出现且都要替换，把对应的 replace_all 设为 true。"
        "匹配 0 次、或出现多次却未设 replace_all，会返回错误且不写入文件，请据提示修正后重试。"
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
                "description": "【单处用法】要被替换的原文片段，须与文件内容逐字符一致。提供 edits 时本字段忽略。",
            },
            "new_string": {
                "type": "string",
                "description": "【单处用法】替换后的新文片段。提供 edits 时本字段忽略。",
            },
            "replace_all": {
                "type": "boolean",
                "description": "【单处用法】为 true 时替换 old_string 的所有出现；默认 false 要求唯一匹配。",
            },
            "edits": {
                "type": "array",
                "description": (
                    "【批量用法】多处替换列表，按顺序依次应用，全部成功才写入文件。"
                    "提供本字段（非空）时优先于顶层 old_string/new_string。"
                ),
                "items": {
                    "type": "object",
                    "properties": {
                        "old_string": {"type": "string", "description": "要被替换的原文片段。"},
                        "new_string": {"type": "string", "description": "替换后的新文片段。"},
                        "replace_all": {
                            "type": "boolean",
                            "description": "为 true 时替换该片段的所有出现；默认 false 要求唯一匹配。",
                        },
                    },
                    "required": ["old_string", "new_string"],
                },
            },
        },
        "required": ["path"],
    }
    read_only = False
    # c14：本工具碰路径/起子进程，必须知道调用者的工作目录。
    workspace_aware = True

    def execute(self, args: dict, cwd=None) -> ToolResult:
        """
        执行单处或批量替换（原子写入）。

        执行步骤：
        1. 校验 path；把参数归一化成「编辑项列表」（单处用法转成单元素列表）。
        2. 解析绝对路径并校验文件存在、非目录。
        3. 读取文件到内存，按顺序对内存副本逐项替换：
           - 每项统计 old_string 在「当前内存内容」中的出现次数；
           - 0 次 → 失败返回（不写盘）；
           - 多次且未 replace_all → 失败返回（不写盘）；
           - 唯一匹配或 replace_all → 应用替换，累计替换计数。
        4. 全部成功后一次性写回，并基于「原始内容 vs 最终内容」构造结构化 diff。

        :param args: 含 "path"；以及 "edits" 数组，或顶层 "old_string"/"new_string"(/"replace_all")
        :returns: 成功时 output 含替换处数与 diff、附结构化 diff；任一校验不过或异常时 ok=False（不写盘）

        副作用：仅在所有编辑项都成功时，覆盖写入一次目标文件。
        """
        try:
            path = args.get("path")
            if not path:
                return ToolResult(ok=False, output="缺少必填参数 path", summary="缺少参数 path")

            # 归一化参数：单处用法与批量用法统一成「编辑项列表」；参数非法时抛 ValueError
            try:
                edits = self._normalize_edits(args)
            except ValueError as ve:
                return ToolResult(ok=False, output=str(ve), summary="参数非法")

            abs_path = resolve_in_workspace(path, _require_cwd(cwd))
            if not abs_path.exists():
                return ToolResult(ok=False, output=f"文件不存在: {path}", summary="文件不存在")
            if abs_path.is_dir():
                return ToolResult(ok=False, output=f"路径是目录而非文件: {path}", summary="不是文件")

            with open(abs_path, "r", encoding="utf-8") as f:
                original = f.read()

            # 在内存副本上按顺序应用所有编辑，全部通过才写盘（保证原子性）
            working = original
            total_replacements = 0
            for i, edit in enumerate(edits, start=1):
                old_s = edit["old_string"]
                new_s = edit["new_string"]
                replace_all = edit["replace_all"]

                count = working.count(old_s)
                if count == 0:
                    return ToolResult(
                        ok=False,
                        output=(
                            f"第 {i} 处编辑：在 {path} 中未找到 old_string（匹配 0 次）。"
                            f"请确认原文是否准确（注意它需匹配前面编辑应用后的内容）。文件未改动。"
                        ),
                        summary=f"第 {i} 处未找到原文",
                    )
                if count > 1 and not replace_all:
                    return ToolResult(
                        ok=False,
                        output=(
                            f"第 {i} 处编辑：old_string 在 {path} 中出现 {count} 次，定位不唯一。"
                            f"请加入更多上下文以唯一定位，或将该处 replace_all 设为 true 以全部替换。文件未改动。"
                        ),
                        summary=f"第 {i} 处出现 {count} 次，需更精确",
                    )

                if replace_all:
                    working = working.replace(old_s, new_s)
                    total_replacements += count
                else:
                    # 仅替换第一处（此时已确认恰好 1 处）
                    working = working.replace(old_s, new_s, 1)
                    total_replacements += 1

            # 全部编辑成功，一次性写回
            with open(abs_path, "w", encoding="utf-8") as f:
                f.write(working)

            # 净行数变化 = 最终内容换行数 - 原始内容换行数
            delta = working.count("\n") - original.count("\n")
            # 基于整文件原始/最终内容构造结构化差异：difflib 会自动只保留变更附近的 hunk。
            # diff_view 同时服务两端——to_text() 回灌给模型，结构化数据交 TUI 渲染彩色 diff 块。
            diff_view = build_diff("Update", path, original, working)

            output = f"已编辑 {path}：{total_replacements} 处替换（净 {delta:+d} 行）\n{diff_view.to_text()}"
            return ToolResult(
                ok=True,
                output=output,
                summary=f"{total_replacements} 处替换 · 净 {delta:+d} 行",
                diff=diff_view,
            )

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
    def _normalize_edits(args: dict) -> list[dict]:
        """
        把两种参数用法统一成「编辑项列表」，每项形如
        {"old_string": str, "new_string": str, "replace_all": bool}。

        归一化规则：
        - 提供了非空 edits 数组：逐项校验并采用，忽略顶层 old_string/new_string；
        - 否则回退到单处用法：用顶层 old_string/new_string(/replace_all) 组成单元素列表。

        :param args: execute 收到的原始参数字典
        :returns: 至少含一项的编辑项列表
        :raises ValueError: 参数缺失或结构非法时抛出（消息面向模型，提示如何修正）
        """
        raw = args.get("edits")
        if raw is not None:
            if not isinstance(raw, list) or not raw:
                raise ValueError("edits 必须是非空数组；或改用单处用法提供 old_string/new_string。")
            normalized: list[dict] = []
            for i, item in enumerate(raw, start=1):
                if not isinstance(item, dict):
                    raise ValueError(f"edits 第 {i} 项必须是对象（含 old_string/new_string）。")
                old_s = item.get("old_string")
                new_s = item.get("new_string")
                if old_s is None or new_s is None:
                    raise ValueError(f"edits 第 {i} 项缺少 old_string 或 new_string。")
                normalized.append({
                    "old_string": old_s,
                    "new_string": new_s,
                    "replace_all": bool(item.get("replace_all", False)),
                })
            return normalized

        # 单处用法
        old_s = args.get("old_string")
        new_s = args.get("new_string")
        if old_s is None or new_s is None:
            raise ValueError("缺少 old_string/new_string；或改用 edits 数组进行批量替换。")
        return [{
            "old_string": old_s,
            "new_string": new_s,
            "replace_all": bool(args.get("replace_all", False)),
        }]
