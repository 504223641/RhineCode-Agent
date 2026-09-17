"""
读文件工具。

给定文件路径，返回其文本内容。属于只读工具（read_only=True），
执行前不需用户确认，且可与其他只读工具并发执行。
"""

from rhinecode.tools.base import Tool, ToolResult, human_size
from rhinecode.tools.path_guard import (
    PathGuardError,
    is_offload_store_path,
    require_cwd as _require_cwd,
    resolve_readable,
)

MAX_READ_BYTES = 1024 * 1024
MAX_RANGE_LINES = 2000

# 一次读取**给模型**的字符上限。
#
# ## 为什么需要它（2026-09-17 新增）
#
# 在此之前 `read_file` 的输出**实际上没有体量上限**：整文件读只在超过
# `MAX_READ_BYTES`（1 MiB）时才拒绝，于是一个 900 KB 的文件会被整个塞进历史
# （约 30 万 token）；范围读只限行数、不限行长。
#
# 这个洞此前由 c8 第一层存盘兜着——塞进去，下一轮立刻被换成占位。那一层
# 已于 2026-09-17 删除（上游 Claude Code 与 Codex 都没有「事后删历史」这种
# 机制，它们一律在**产出的那一刻**限量），因此闸门必须收到这里来。
#
# ⚠ **第二层 LLM 摘要救不了这种**：它压的是「较早的历史」，而刚读回来的这条
# 是最新的，正好落在「保留近期原文」的保护区里。
#
# 取值比 `run_command` 的 30 000 宽，理由有两条：读文件是核心操作；而且模型
# 有现成的续读手段（`start_line` / `max_lines`），超出部分不是丢了而是分页。
# 形态对齐 Claude Code 的 Read——它同样是给第一页 + 一句「还有多少、怎么接着读」。
READ_OUTPUT_MAX_CHARS = 100_000

# 读到 c8 存盘目录时回灌给模型的说法。
#
# ⚠ **措辞要说清「为什么白读」并给出出路**，不能只写一句「不允许」——
# 只说不允许会让模型去找绕过的办法（同 `todo_write.plan_blocked_hint` 那条教训）。
# 这里的事实是：那份文件是上一条工具结果的**逐字副本**，读它拿不到任何新东西，
# 而它比原文更大、必然再次被存盘，于是形成一个不收敛的循环
# （见 `context/offload.py` 模块 docstring 的实测记录）。
OFFLOAD_STORE_REFUSAL = (
    "这是上下文管理存下的工具结果副本，不是原始文件，读它拿不到任何新内容——"
    "而且它比原结果更大，读回来会立刻被再次存盘，反复下去只会白烧迭代。"
    "请改为重新调用产生那条结果的工具本身，并缩小范围分段取回"
    "（例如给 read_file 传 start_line / max_lines，或给 grep_content 收窄 pattern）。"
)


def _fit_to_budget(rendered: list[str], budget: int) -> tuple[list[str], bool, bool]:
    """
    从头保留尽可能多的完整行，直到用满字符预算。

    :param rendered: 已经带好行号的各行（不含换行符）
    :param budget: 字符预算
    :returns: (保留下来的行, 是否发生了截断, 是否是**在一行内部**切的)

    第三个返回值决定调用方该给哪一句续读提示，**两种情况的出路完全不同**：
    按行切时 `start_line=N+1` 能精确续上；在一行内部切时那个建议是**错的**
    （被切掉的是第 N 行的后半截，而 `start_line=N+1` 会从第 N+1 行开始，
    正好跳过它）。给一条走不通的建议比不给更糟——模型会照做，然后拿着
    一份缺了一块的内容继续推理，而它看不出有什么不对。

    **从头保留而不是头尾各留一半**，这一点与 `run_command._clip` 刻意不同：
    命令输出的信息集中在两端（开头是在做什么、结尾是结果），而文件是顺序读的，
    模型拿到前 N 行之后可以用 `start_line=N+1` 精确地接着读——给它一个中间有
    窟窿的文件反而没法续。

    ⚠ 单行长度超过整份预算时（压缩过的 JS、单行 JSON），保留列表会是空的。
    调用方必须自己处理那种情况，否则模型拿到的是「一行内容都没有」。

    副作用：无（纯函数）。
    """
    kept: list[str] = []
    used = 0
    for line in rendered:
        # +1 是重新 join 时补回的换行符，不计的话拼出来会超预算
        if used + len(line) + 1 > budget:
            if not kept:
                # 第一行就装不下：硬切它并**就地说明**。不说明的话模型会拿着
                # 半行代码当成完整的一行去推理，而它看不出有什么不对。
                return [line[:budget] + "…（本行过长，已截断）"], True, True
            return kept, True, False
        kept.append(line)
        used += len(line) + 1
    return kept, False, False


def _continue_hint(next_start: int, total: int) -> str:
    """
    生成「还剩多少、怎么接着读」那一句。

    :param next_start: 续读应当从第几行开始（1 起）
    :param total: 文件总行数
    :returns: 以换行开头的一句提示

    ⚠ **必须把续读参数直接写出来**，不能只说「内容已截断」。只说截断会让模型
    要么当没看见继续推理（拿半个文件当全文），要么去猜一个范围重试——而猜错
    一次就是白烧一轮。这与 `context/offload.py` 那条占位符的教训同源：
    拒绝或截断的文案要说清「怎么办」，不能只说「不行」。

    副作用：无（纯函数）。
    """
    return (
        f"\n…（本次只显示到第 {next_start - 1} 行，共 {total} 行。"
        f"用 start_line={next_start} 继续读后面的部分）"
    )


# 在一行内部切时的提示。**刻意不给 `start_line`**，理由见 `_fit_to_budget`
# 的第三个返回值：那条建议在这种情况下是错的。
LONG_LINE_HINT = (
    "\n…（这一行本身就超过了单次读取的字符上限，后半截未显示。"
    "它多半是压缩过的代码或单行 JSON——`start_line` 帮不上忙，"
    "要看具体某一段请用 `grep_content` 搜关键词定位）"
)


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
            "start_line": {
                "type": "integer",
                "description": "从第几行开始读取（可选，1 起始）。读取大文件时必须指定范围。",
            },
            "max_lines": {
                "type": "integer",
                "description": f"最多读取多少行（可选，最大 {MAX_RANGE_LINES}）。读取大文件时默认 {MAX_RANGE_LINES} 行。",
            },
        },
        "required": ["path"],
    }
    read_only = True
    # c14：本工具碰路径/起子进程，必须知道调用者的工作目录。
    workspace_aware = True
    # 界面上只显示路径（F12）——offset/limit 对「这次在读什么」没有信息量
    primary_arg = "path"

    def execute(self, args: dict, cwd=None) -> ToolResult:
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

            # 只读工具不经过确认，因此必须先把路径钉死在项目工作目录内；
            # c9 起额外放行「只读白名单」目录（当前仅用户级记忆目录），写类工具不受影响。
            root = _require_cwd(cwd)
            abs_path = resolve_readable(path, root)

            # c8 存盘目录：路径合法、文件也确实在，但读它没有意义且会形成死循环。
            # 判定放在存在性检查**之前**是刻意的——存盘文件随时可能已被清理，
            # 放在后面的话模型会先拿到一句「文件不存在」，那句话既没解释原因、
            # 也没给出路，它多半会换一个存盘路径接着试。
            if is_offload_store_path(abs_path, root):
                return ToolResult(
                    ok=False,
                    output=OFFLOAD_STORE_REFUSAL,
                    summary="存盘副本，不可直接读取",
                )

            if not abs_path.exists():
                return ToolResult(ok=False, output=f"文件不存在: {path}", summary="文件不存在")
            if abs_path.is_dir():
                return ToolResult(ok=False, output=f"路径是目录而非文件: {path}", summary="不是文件")

            try:
                size = abs_path.stat().st_size
            except OSError:
                size = 0

            range_requested = "start_line" in args or "max_lines" in args
            start_line = self._parse_positive_int(args.get("start_line", 1), "start_line")
            if "max_lines" in args:
                max_lines = self._parse_positive_int(args.get("max_lines"), "max_lines")
                if max_lines > MAX_RANGE_LINES:
                    return ToolResult(
                        ok=False,
                        output=f"max_lines 不能超过 {MAX_RANGE_LINES}",
                        summary="参数非法",
                    )
            else:
                max_lines = MAX_RANGE_LINES

            if size > MAX_READ_BYTES and not range_requested:
                return ToolResult(
                    ok=False,
                    output=(
                        f"文件过大: {path} · {human_size(size)}。"
                        "请指定 start_line/max_lines 分段读取。"
                    ),
                    summary="文件过大",
                )

            if range_requested:
                return self._read_range(abs_path, path, size, start_line, max_lines)

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
            rendered = [
                f"{i:>{width}}│ {line}" for i, line in enumerate(lines, start=1)
            ]
            full = f"{header}\n" + "\n".join(rendered)

            kept, clipped, line_cut = _fit_to_budget(rendered, READ_OUTPUT_MAX_CHARS)
            if not clipped:
                return ToolResult(
                    ok=True,
                    output=full,
                    summary=f"读取 {total} 行 · {human_size(byte_len)}",
                )

            hint = LONG_LINE_HINT if line_cut else _continue_hint(len(kept) + 1, total)
            output = f"{header}\n" + "\n".join(kept) + hint
            # 完整原文只进行为记录，模型仍然只拿上面那份
            # （见 `ToolResult.full_output`，与 `run_command` 同一条约定）。
            return ToolResult(
                ok=True,
                output=output,
                summary=f"读取 {len(kept)}/{total} 行 · {human_size(byte_len)}",
                full_output=full,
            )

        except UnicodeDecodeError:
            # 二进制文件或非 UTF-8 编码，无法作为文本读取
            return ToolResult(
                ok=False,
                output=f"文件无法以 UTF-8 文本解码（可能是二进制文件）: {args.get('path')}",
                summary="非文本文件",
            )
        except PathGuardError as e:
            return ToolResult(ok=False, output=str(e), summary="路径越界")
        except ValueError as e:
            return ToolResult(ok=False, output=str(e), summary="参数非法")
        except Exception as e:
            # 兜底：权限不足等其他异常统一转结构化错误，绝不向上抛出
            return ToolResult(ok=False, output=f"读取文件失败: {e}", summary="读取失败")

    @staticmethod
    def _parse_positive_int(value, field_name: str) -> int:
        try:
            parsed = int(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{field_name} 必须是正整数") from exc
        if parsed <= 0:
            raise ValueError(f"{field_name} 必须是正整数")
        return parsed

    def _read_range(self, abs_path, display_path: str, byte_len: int, start_line: int, max_lines: int) -> ToolResult:
        selected: list[tuple[int, str]] = []
        truncated = False
        with open(abs_path, "r", encoding="utf-8") as f:
            for lineno, line in enumerate(f, start=1):
                if lineno < start_line:
                    continue
                if len(selected) >= max_lines:
                    truncated = True
                    break
                selected.append((lineno, line.rstrip("\n\r")))

        if not selected:
            header = (
                f"文件: {display_path} · {human_size(byte_len)} · "
                f"从第 {start_line} 行起 0 行（超出文件末尾）"
            )
            return ToolResult(ok=True, output=header, summary="范围读取 0 行")

        last_line = selected[-1][0]
        width = len(str(last_line))
        rendered = [f"{lineno:>{width}}│ {line}" for lineno, line in selected]
        full_header = (
            f"文件: {display_path} · {human_size(byte_len)} · "
            f"第 {start_line}-{last_line} 行"
        )
        full = f"{full_header}\n" + "\n".join(rendered)

        # 两个上限**都**可能收住这次读取：行数（max_lines）与字符预算。
        # 哪个先到就按哪个截，但给模型的续读提示只有一句——它不需要知道
        # 是被哪一条拦下的，它只需要知道从第几行接着读。
        kept, clipped, line_cut = _fit_to_budget(rendered, READ_OUTPUT_MAX_CHARS)
        shown_last = selected[len(kept) - 1][0] if kept else start_line
        header = (
            full_header
            if not clipped
            else (
                f"文件: {display_path} · {human_size(byte_len)} · "
                f"第 {start_line}-{shown_last} 行"
            )
        )
        output = f"{header}\n" + "\n".join(kept)

        if clipped and line_cut:
            output += LONG_LINE_HINT
        elif clipped:
            # 字符预算先到：还剩多少行不知道（没读完整个文件），
            # 所以这一句只说「从哪儿接着读」，不报总行数。
            output += (
                f"\n…（本次输出已达字符上限，只显示到第 {shown_last} 行。"
                f"用 start_line={shown_last + 1} 继续读后面的部分）"
            )
        elif truncated:
            output += (
                f"\n…（已达本次读取上限 {max_lines} 行，只显示到第 {last_line} 行。"
                f"用 start_line={last_line + 1} 继续读后面的部分）"
            )

        return ToolResult(
            ok=True,
            output=output,
            summary=f"范围读取 {len(kept)} 行 · {human_size(byte_len)}",
            # 完整原文只进行为记录（同 run_command 那条约定）；没截断时不另存，
            # 两份一模一样只会让记录文件白白翻倍。
            full_output=full if clipped else None,
        )
