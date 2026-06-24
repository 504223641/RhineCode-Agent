"""
统一 diff 的数据结构与构造逻辑。

为什么单独成一个模块：
- 「算 diff」是纯逻辑（输入旧文本/新文本，输出结构化的差异行），与具体工具无关，
  edit_file / write_file 都会用到，抽出来便于复用与单元测试。
- 产出的 DiffView 是「数据」而非「展示」：它只描述「哪些行被删/被增/是上下文、各自行号是多少」，
  至于怎么上色、怎么画分支符号，交给 TUI 层（rhinecode/tui/widgets.py）决定。
  这样工具层不感知 Textual，TUI 层也不感知 difflib，两边通过 DiffView 解耦。

实现基于标准库 difflib.unified_diff：它已经处理好「按变更聚合 hunk + 每个 hunk 上下保留
context 行 + 跳过中间大段未改动」的逻辑，我们只需解析它的输出，补上「每行对应的行号」即可。
"""

import re
from dataclasses import dataclass, field
from difflib import unified_diff
from typing import Optional

# diff 块最多渲染多少行，超过则截断并标记，避免一次大改动把整个历史区刷爆。
DIFF_MAX_ROWS = 80
# 每个变更块上下各保留的未改动上下文行数（传给 unified_diff 的 n 参数）。
DIFF_CONTEXT = 3

# 解析 unified_diff 的 hunk 头：形如 "@@ -12,7 +12,6 @@"，
# 其中 -12 是旧文件该 hunk 起始行（1-based），+12 是新文件起始行。逗号后的长度可省略（单行时）。
_HUNK_RE = re.compile(r"@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@")

# 行类型标记：上下文（未改动）、删除、新增、hunk 间的省略分隔。
MARK_CONTEXT = " "
MARK_REMOVE = "-"
MARK_ADD = "+"
MARK_GAP = "@"


@dataclass
class DiffRow:
    """
    diff 中的一行。

    :param marker: 行类型——MARK_CONTEXT/MARK_REMOVE/MARK_ADD/MARK_GAP 之一
    :param old_no: 该行在「旧文件」中的行号（1-based）；新增行为 None
    :param new_no: 该行在「新文件」中的行号（1-based）；删除行为 None
    :param text: 该行文本内容（不含行尾换行）；MARK_GAP 行的 text 无意义
    """
    marker: str
    old_no: Optional[int]
    new_no: Optional[int]
    text: str


@dataclass
class DiffView:
    """
    一次文件改动的结构化差异，供 TUI 渲染与模型回灌共用。

    :param op: 操作动词，用于展示标题（"Update" 表示局部编辑，"Write" 表示整体写入）
    :param path: 被改动的文件路径（原样展示给用户）
    :param rows: 差异行列表（已按上下文裁剪、按出现顺序排列）
    :param added: 新增行数
    :param removed: 删除行数
    :param truncated: 是否因超过 DIFF_MAX_ROWS 而被截断
    """
    op: str
    path: str
    rows: list = field(default_factory=list)
    added: int = 0
    removed: int = 0
    truncated: bool = False

    def to_text(self) -> str:
        """
        渲染成纯文本（无颜色），用于回灌给模型的 ToolResult.output。

        模型看到带行号与 +/- 标记的差异，能直观确认这次改动的效果，
        而无需再次 read_file。与 TUI 的彩色渲染共用同一份 DiffView 数据。

        :returns: 多行字符串，首行为 "Op(path)  +A -R" 概要，其后为带行号的差异行
        """
        head = f"{self.op}({self.path})  +{self.added} -{self.removed}"
        lines = [head]
        for r in self.rows:
            if r.marker == MARK_GAP:
                lines.append("        …")
                continue
            # 上下文/新增显示新文件行号，删除显示旧文件行号
            no = r.new_no if r.new_no is not None else r.old_no
            lines.append(f"{no:>6} {r.marker} {r.text}")
        if self.truncated:
            lines.append("        …（diff 已截断）")
        return "\n".join(lines)


def build_diff(
    op: str,
    path: str,
    old_text: str,
    new_text: str,
    context: int = DIFF_CONTEXT,
    max_rows: int = DIFF_MAX_ROWS,
) -> DiffView:
    """
    比较旧/新文本，构造结构化差异 DiffView。

    执行流程：
    1. 按行切分旧/新文本（不保留行尾换行，便于逐行比较与展示）。
    2. 调用 difflib.unified_diff 得到标准的 unified diff 文本行（含 @@ hunk 头、
       带 ' '/'-'/'+' 前缀的正文行），n=context 控制每个变更上下保留的上下文行数。
    3. 解析每个 hunk 头取得旧/新起始行号，随后逐行推进行号并归类为
       上下文 / 删除 / 新增，累加 added/removed 计数。
    4. 多个 hunk 之间插入一行 MARK_GAP（省略号），表示中间有未展示的未改动内容。
    5. 总行数超过 max_rows 时截断并置 truncated。

    :param op: 操作动词（"Update"/"Write"），写入 DiffView.op 供标题展示
    :param path: 文件路径，原样写入 DiffView.path
    :param old_text: 改动前的完整文本（新建文件时传 ""）
    :param new_text: 改动后的完整文本
    :param context: 每个变更上下保留的上下文行数
    :param max_rows: 差异行渲染上限，超出截断
    :returns: 填充好 rows/added/removed/truncated 的 DiffView
    """
    old_lines = old_text.splitlines()
    new_lines = new_text.splitlines()

    view = DiffView(op=op, path=path)
    # 当前正在推进的旧/新文件行号（由 hunk 头初始化，逐行 +1）
    old_no = 0
    new_no = 0
    first_hunk = True

    for line in unified_diff(old_lines, new_lines, n=context, lineterm=""):
        # 跳过 "--- " / "+++ " 文件头（我们用 DiffView.path 自带路径，不展示这两行）
        if line.startswith("--- ") or line.startswith("+++ "):
            continue

        if line.startswith("@@"):
            m = _HUNK_RE.match(line)
            if m:
                old_no = int(m.group(1))
                new_no = int(m.group(2))
            # 非首个 hunk：插入省略分隔，提示中间有被跳过的未改动行
            if not first_hunk:
                view.rows.append(DiffRow(MARK_GAP, None, None, ""))
            first_hunk = False
            continue

        # 正文行：首字符为前缀标记，其余为内容
        marker = line[0] if line else MARK_CONTEXT
        text = line[1:]
        if marker == MARK_REMOVE:
            view.rows.append(DiffRow(MARK_REMOVE, old_no, None, text))
            view.removed += 1
            old_no += 1
        elif marker == MARK_ADD:
            view.rows.append(DiffRow(MARK_ADD, None, new_no, text))
            view.added += 1
            new_no += 1
        else:  # 上下文行：旧/新行号同时推进
            view.rows.append(DiffRow(MARK_CONTEXT, old_no, new_no, text))
            old_no += 1
            new_no += 1

    if len(view.rows) > max_rows:
        view.rows = view.rows[:max_rows]
        view.truncated = True

    return view
