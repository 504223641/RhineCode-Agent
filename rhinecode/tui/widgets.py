"""
TUI 组件模块，定义四个自定义 Textual Widget。

组件职责：
- HistoryView：对话历史展示区，支持流式逐块更新和滚动
- UserMessageWidget：历史区里的用户消息行（整行灰底，与其它消息视觉区分）
- CommandPanel：斜杠命令提示面板，输入 "/" 时弹出，支持键盘上下选择
- InputBar：用户输入框，拦截回车事件并发出自定义消息
- StatusBar：底部状态栏，展示当前 Provider、模型和思考模式状态

渲染策略：
  HistoryView 采用 ScrollableContainer + 动态 Static 组件的方案，
  而非 RichLog。原因是 RichLog.write() 不支持行内追加（无 end="" 参数），
  无法实现流式逐字更新同一行内容；而 Static.update() 可以原地刷新，
  配合 call_from_thread 即可实现从 Worker 线程安全地驱动 UI 更新。
"""

import re
from enum import Enum
from time import monotonic
from typing import Optional, TypeVar

from rich.cells import cell_len
from rich.console import Group as RichGroup
from rich.highlighter import Highlighter
from rich.markdown import Markdown as RichMarkdown
from rich.segment import Segment
from rich.style import Style
from rich.text import Text as RichText
from textual.app import ComposeResult
from textual.binding import Binding
from textual.content import Content, Span
from textual.events import Key
from textual.style import Style as VisualStyle
from textual.widgets import Static, Input, OptionList
from textual.widgets.option_list import Option
from textual.containers import ScrollableContainer, Vertical
from textual.message import Message as TextualMessage

from rhinecode.todo.models import TODO_STATE_LABELS, TodoState

from rhinecode.agent.events import ClarifyOption
from rhinecode.commands.registry import CommandRegistry
from rhinecode.memory.session import SessionInfo
from rhinecode.subagents.tasks import BRANCH_AGENT_NAME, STATUS_LABELS, TaskStatus
from rhinecode.tools.diff import MARK_ADD, MARK_CONTEXT, MARK_GAP, MARK_REMOVE
from rhinecode.tools.display import (
    SEGMENT_SEP,
    TOOL_LABELS,
    clip_value,
    compose_batch_summary,
    resolve_call_parts,
    resolve_full_title,
    running_verb,
    summarize_args_plain,
)


# 「把一段纯文本安全地嵌进 markup 字符串」的转义正则：匹配任意 `[` 及其前导反斜杠。
#
# ⚠️ **不要换回 `rich.markup.escape`**（这是一次真实崩溃的根因，见下）。
# 它的正则是 `(\\*)(\[[a-z#/@][^[]*?])` —— **必须找到闭合的 `]` 才认为这是标签**，
# 于是「括号被截断」的文本会被它整个放过：
#
#     summarize_args 把 `allowed_tools: [read_file, glob_files, ...]` 截成
#     `allowed_tools: [read_file, glo…`  → `]` 没了 → rich 不转义 → `[` 原样留下
#
# 而 **Textual 的 Content markup 比 Rich 严格**：Rich 把落单的 `[` 当普通文本放过，
# Textual 认定它是标签开头、去解析后面的内容当样式值，抛
# `MarkupError: Expected markup value`。更糟的是它抛在 `OptionList.get_content_height`
# 里——也就是**布局阶段的主线程**，不在 `show_for` 的调用栈上，没有任何 try/except
# 兜得住，Textual 直接拆掉整个 app，程序退出。
#
# 触发条件很窄（某个值的前 30 字符里有 `[`、配对的 `]` 在 30 字符之外），
# 所以它表现为「偶尔莫名其妙退出」，极难归因。
#
# 结论：这些位置嵌进去的都是**纯文本**（工具参数、路径、错误消息、决策原因），
# 里面的每个 `[` 都是字面量、没有一个是标签，因此无条件全转义才是正确语义。
_ESCAPE_BRACKET = re.compile(r"(\\*)(\[)")


def escape(text: object) -> str:
    """
    把任意文本转义成「可安全嵌入 markup 字符串」的形式。

    与 `rich.markup.escape` 的差别：**无条件转义每一个 `[`**，不要求它看起来像
    一个完整标签。理由见上方 `_ESCAPE_BRACKET` 的注释——「像标签才转义」遇上
    被截断的括号会漏掉，而漏掉的后果是整个应用崩溃退出。

    行为上它是 `rich.markup.escape` 的**严格超集**：后者会转义的，这里全都会转义。

    :param text: 任意对象，非字符串会先 `str()`
    :returns: 转义后的字符串

    副作用：无（纯函数）。
    """
    markup = str(text)
    # 前导反斜杠要一并加倍，否则 `\[` 这种「本就转义过的输入」会被二次转义成
    # 「字面反斜杠 + 标签」，语义反而错了。这一步与 rich 的做法一致。
    markup = _ESCAPE_BRACKET.sub(lambda m: f"{m.group(1)}{m.group(1)}\\{m.group(2)}", markup)
    # 结尾落单的反斜杠会把后续拼接进来的字符转义掉，补一个使其成为字面反斜杠。
    if markup.endswith("\\") and not markup.endswith("\\\\"):
        markup += "\\"
    return markup


def summarize_args(arguments: "dict | None", max_len: int = 60) -> str:
    """
    把工具调用参数压缩成单行摘要，用于工具行与确认框展示。

    取每个参数 "键=值" 拼接，值过长则截断，整体再做长度上限截断，
    目的是让用户一眼看清工具将操作什么（如 path=...），而非展示完整内容。

    判定本身在 `tools/display.py`（那份是纯文本、无转义，子 Agent 侧也要用它）；
    这里只加一层**转义**。⚠ 转义必须留在这一侧且只做一次：产出会被拼进 markup
    字符串，而参数值里的 `[` 全是字面量。

    :param arguments: 解析后的参数字典；None（解析失败）时返回占位提示
    :param max_len: 摘要最大长度，超出截断
    :returns: 单行参数摘要字符串，**已转义**
    """
    return escape(summarize_args_plain(arguments, max_len))


# 回放时工具结果摘要的最大展示长度（取首行再截断，避免长结果撑爆历史区）
_REPLAY_RESULT_MAX_CHARS = 80


def _first_line_truncated(text: str, max_chars: int = _REPLAY_RESULT_MAX_CHARS) -> str:
    """取文本首行并按长度截断，用于回放场景的工具结果摘要（与运行时摘要口径一致）。"""
    line = (text or "").strip().split("\n", 1)[0]
    if len(line) > max_chars:
        line = line[:max_chars] + "…"
    return line


def build_replay_items(messages) -> "list[tuple]":
    """
    把一段历史消息（provider.base.Message 列表）转换为回放渲染项序列（纯函数，可单测）。

    /resume 载入会话后，TUI 需要把整段历史画回聊天区。本函数负责「消息 → 渲染项」的
    纯逻辑转换，不做任何 UI 操作，由 HistoryView.render_history 消费。

    执行流程：
    1. 第一趟收集 {tool_call_id: 工具结果文本} 映射——载入历史已经过 SessionStore 的
       _drop_unpaired 严格配对清理，但仍用 dict.get 防御性兜底（缺结果时展示占位）；
    2. 第二趟按原始顺序产出渲染项：
       - role="user"      → ("user", 显示文本)：优先取非空 display_content
                            （c10 双内容——/init 等提示词命令回放时只显示原命令），
                            缺失或为空回退 content
       - role="assistant" → content 非空先产出 ("assistant", content)；
                            随后每个 tool_call 产出 ("tool", tool_call, 结果首行截断)。
                            content 为空且无 tool_calls 的消息整体跳过（不渲染空 Rhine 行）
       - role="tool"      → 跳过（结果已并入所属 assistant 的 tool 项）
       - 其它 role        → 防御性跳过

    已知降级：会话存档不含思考（thinking）内容，回放不出现思考块。

    :param messages: 历史消息列表（元素为 provider.base.Message）
    :returns: 渲染项列表，元素为 ("user", str) / ("assistant", str) / ("tool", ToolCall, str)
    """
    # 第一趟：tool_call_id → 结果文本（后到覆盖先到，正常历史中 id 唯一）
    results: dict = {}
    for msg in messages:
        if msg.role == "tool" and msg.tool_call_id:
            results[msg.tool_call_id] = msg.content
    # 第二趟：按序产出渲染项
    items: list[tuple] = []
    for msg in messages:
        if msg.role == "user":
            # 双内容（c10 F26）：display_content 非空时优先展示（提示词命令原文），
            # 旧消息 / 普通消息（None 或空串）回退完整 content。
            display = getattr(msg, "display_content", None)
            items.append(("user", display if display else msg.content))
        elif msg.role == "assistant":
            if msg.content:
                items.append(("assistant", msg.content))
            for tc in msg.tool_calls or []:
                summary = _first_line_truncated(results.get(tc.id, "（无结果）"))
                items.append(("tool", tc, summary))
        # role="tool" 与未知 role：跳过
    return items


def content_from_rich(renderable, width: int) -> Content:
    """
    把任意 **Rich 可渲染对象**转成 Textual 的 `Content`，使其**可被选中、
    可被高亮、可被复制**。

    ## 为什么必须做这一步转换（读 Textual 源码才看得出来的事）

    Textual 有两套渲染对象：自家的 `Content`/`Visual`，与包一层的 `RichVisual`
    （用来兼容 Rich 生态）。**选区功能只对前者成立**，而且是**三处一起失效**：

    1. `RichVisual.render_strips()` 收到 `RenderOptions.selection` 之后
       **原样丢弃**——于是拖选时那块区域**一点高亮都不会画**，用户看到的是
       「这里根本选不中」。
    2. 光标定位靠段落样式里的 `meta["offset"]`，那是 `Content` 渲染时才写进去的。
       Rich 段落没有它，于是 `Screen.get_widget_and_offset_at()` 返回的偏移是
       `None`——**没有偏移就没有「从这个字到那个字」**，该组件只能整块选中。
    3. `Widget.get_selection()` 的默认实现只认 `Text` 与 `Content`，
       别的**一律返回 None**，于是复制时整块内容凭空消失。

    第 3 条可以靠子类补一个 `get_selection` 绕过（本项目上一轮就是这么做的），
    但**前两条绕不过去**：补了也只是「看不见地整块选中」。用户的原话是
    「其他的连选择都不行」——那不是复制的问题，是**屏幕上没有任何反馈**。

    转换之后三条一起解决，且**视觉一模一样**：本函数走的是 Rich 自己的
    `console.render()`，拿到的段落就是原本要画到屏幕上的那些，只是把
    「文本 + 样式」重新装进 `Content` 而已。

    ## 为什么需要 width 参数

    Rich 的排版是**宽度相关**的：Markdown 要按宽度折行、diff 块要按宽度给
    整行补背景。因此转换必须发生在**知道宽度之后**，调用方一律在
    `render()` / `get_content_height()` 里调它（那两处才拿得到实时宽度），
    **不要在构造组件时提前转**——那样终端一 resize 排版就错了。

    :param renderable: 任意 Rich 可渲染对象（`Text` / `Group` / `Markdown` / 自定义）
    :param width: 渲染宽度（单位是终端单元格），必须 > 0
    :returns: 等价的 `Content`；样式逐段保留

    副作用：无（只读地跑一遍 Rich 渲染）。
    """
    from textual.app import active_app

    console = active_app.get().console
    options = console.options.update(width=width, height=None, highlight=False)
    parts: "list[str]" = []
    spans: "list[Span]" = []
    pos = 0
    for seg_text, seg_style, control in console.render(renderable, options):
        # 控制段（光标移动之类）不产生可见字符，带进来只会污染文本
        if control or not seg_text:
            continue
        parts.append(seg_text)
        end = pos + len(seg_text)
        if seg_style is not None:
            spans.append(Span(pos, end, VisualStyle.from_rich_style(seg_style)))
        pos = end
    text = "".join(parts)

    # Rich 习惯在整体渲染的末尾补换行；`Content` 会把它算成真实的一行，
    # 于是每个组件底下都多出一条空行。裁掉尾部换行的同时要把越界的 span 一并夹回去。
    trimmed = text.rstrip("\n")
    if len(trimmed) != len(text):
        limit = len(trimmed)
        spans = [
            Span(span.start, min(span.end, limit), span.style)
            for span in spans
            if span.start < limit
        ]
    return Content(trimmed, spans)


# diff 块配色：删除/新增行用「背景色」高亮整行（不改前景字色，保持默认终端文字色），
# 上下文行、行号、概要统一用灰色前景。
# Rich 的 "on <color>" 表示设置背景色；不写前景即沿用终端默认字色。
_DIFF_REMOVE_BG = "on #5f1f1f"   # 删除行：暗红底
_DIFF_ADD_BG = "on #1f4f1f"      # 新增行：暗绿底
_DIFF_DIM = "#808080"            # 上下文/行号/概要：灰色前景
# 行号列宽度（右对齐），保证不同行的内容左缘对齐。
_DIFF_NUM_WIDTH = 6


def _count_phrase(added: int, removed: int) -> str:
    """
    把增删行数拼成自然语言概要，如 "Added 4 lines, removed 1 line"。

    规则：有增才说 Added、有删才说 removed，单数用 line、复数用 lines；
    句首词首字母大写（只删时即 "Removed 1 line"）；都为 0 时返回 "No changes"。

    :param added: 新增行数
    :param removed: 删除行数
    :returns: 概要短语
    """
    parts = []
    if added:
        parts.append(f"Added {added} line{'s' if added != 1 else ''}")
    if removed:
        parts.append(f"removed {removed} line{'s' if removed != 1 else ''}")
    if not parts:
        return "No changes"
    phrase = ", ".join(parts)
    return phrase[0].upper() + phrase[1:]


class _DiffBlock:
    """
    diff 块的自定义 Rich 渲染对象，供工具行展示。

    布局（缩进体现「隶属于上方状态行」的层级，类似树形分支）：
        ⎿  Added 4 lines, removed 1 line
            279     上下文行（灰）
            282 -   删除行（暗红底，整行高亮）
            283 +   新增行（暗绿底，整行高亮）
            ⋮               （hunk 之间的省略）

    为什么用自定义渲染对象而非现成的 RichText：
      终端里背景色只覆盖字符本身，文本短于行宽时右侧不会着色，背景条会「断在文末」。
      要做到「整行背景都是背景色」，必须把高亮行补足空格到**当前可用宽度**。
      可用宽度只有在真正渲染时才知道（且会随终端 resize 变化），因此实现 __rich_console__，
      从渲染参数 options.max_width 取得实时宽度再补空格——这样高亮条能铺满整行并自适应宽度。

    行号取值：上下文/新增显示新文件行号，删除显示旧文件行号；
    与 DiffView.to_text() 的取值规则一致，保证人看到的与回灌给模型的对得上。
    """

    def __init__(self, view, expanded: bool = False) -> None:
        """
        :param view: tools.diff.DiffView
        :param expanded: 全局展开开关（`Ctrl+O`）。为假时 diff 行数受
                         `DIFF_ROW_LIMIT` 约束（F41）
        """
        self._view = view
        self._expanded = expanded

    def __rich_console__(self, console, options):
        """
        Rich 渲染协议：产出本块的 Segment 序列。

        每行先组装出文本与样式，并标记是否需要「整行背景」。需要背景的行（删除/新增）
        按 options.max_width 补足空格，使背景铺满整行；其余行（概要/上下文/省略）原样输出。
        行间以换行分隔，末行不补换行，避免产生多余空行。
        """
        width = options.max_width
        rows_out = self._rows()
        last = len(rows_out) - 1
        for idx, (text, style, fill) in enumerate(rows_out):
            if fill:
                # 补空格到整行宽度（cell_len 正确计算中文/全角宽度），使背景铺满整行
                pad = max(0, width - cell_len(text))
                yield Segment(text + " " * pad, style)
            else:
                yield Segment(text, style)
            if idx != last:
                yield Segment("\n")

    def plain_text(self) -> str:
        """
        本块的**纯文本**，供选中复制使用。

        ⚠ **与渲染同源**（都走 `_rows`）。各拼一遍的话，复制出来的内容会与
        屏幕上看到的悄悄不一致——而那种不一致极难发现：两边单看都是对的。
        """
        return "\n".join(text for text, _style, _fill in self._rows())

    def _rows(self) -> "list[tuple[str, Style, bool]]":
        """
        组装每行的 `(文本, 样式, 是否整行铺背景)`。**渲染与取纯文本共用这一处。**
        """
        view = self._view
        dim = Style.parse(_DIFF_DIM)
        remove_bg = Style.parse(_DIFF_REMOVE_BG)
        add_bg = Style.parse(_DIFF_ADD_BG)

        rows_out: list[tuple[str, Style, bool]] = []
        # 概要分支行（灰色，无背景）
        rows_out.append((f"{BRANCH_PREFIX}{_count_phrase(view.added, view.removed)}", dim, False))

        # 折叠（tui-display 扩展 F41 / AC31c）：改造前 diff 块**完全没有上限**，
        # 一次大改动会把整块差异铺进历史区，后面的对话全被挤出屏幕。
        # 与结果分支行同一套语义：截断 + 如实写出还剩多少行 + 指出怎么展开。
        rows = list(view.rows)
        hidden = 0
        if not self._expanded and len(rows) > DIFF_ROW_LIMIT:
            hidden = len(rows) - DIFF_ROW_LIMIT
            rows = rows[:DIFF_ROW_LIMIT]

        for row in rows:
            if row.marker == MARK_GAP:
                # hunk 间省略：用居中省略号表示中间有未展示的未改动内容
                rows_out.append(("        ⋮", dim, False))
                continue
            no = row.new_no if row.new_no is not None else row.old_no
            num = f"    {no:>{_DIFF_NUM_WIDTH}} "  # 4 格缩进 + 右对齐行号
            if row.marker == MARK_REMOVE:
                rows_out.append((f"{num}- {row.text}", remove_bg, True))
            elif row.marker == MARK_ADD:
                rows_out.append((f"{num}+ {row.text}", add_bg, True))
            else:  # MARK_CONTEXT：无背景，灰色前景
                rows_out.append((f"{num}  {row.text}", dim, False))
        if hidden:
            rows_out.append((f"{BRANCH_CONT_INDENT}… +{hidden} 行", dim, False))
        # `view.truncated` 是 **diff 生成侧**（tools/diff.py）的截断标记，与本处的
        # 展示折叠是两回事：前者说「这份 diff 本身就没算全」，后者说「算全了但
        # 没画全」。两条都可能出现，故各画各的、不合并——合并会让用户以为
        # 按 Ctrl+O 就能看到那些**根本没被生成出来**的行。
        if view.truncated:
            rows_out.append(("        …（diff 已截断）", dim, False))

        return rows_out


def render_diff_block(view, expanded: bool = False) -> "_DiffBlock":
    """
    构造 diff 块的可渲染对象（见 _DiffBlock）。保留函数形式，调用方无需感知具体类型。

    :param view: tools.diff.DiffView
    :param expanded: 全局展开开关；为假时行数受 `DIFF_ROW_LIMIT` 约束
    :returns: 一个 Rich 可渲染对象，删除/新增行整行背景高亮、自适应宽度
    """
    return _DiffBlock(view, expanded=expanded)


# ---------------------------------------------------------------------------
# 分支符号与次级信息配色：**全界面单一来源**（tui-display 扩展 F14 / AC12）
# ---------------------------------------------------------------------------
# 三处会画「从属于上一行」的次级信息：工具行的结果分支、活动区展开后的子调用行、
# 命令报告里缩进的详情行。它们**必须取自同一处定义**——各写一份的话，改了一处
# 另外两处不会跟着变，而这**不报错**：界面照常渲染，只是三处的灰度慢慢分叉，
# 最后没人说得清哪个才是「对的灰」。
#
# `⎿` 与 `·` `↑` `●` `>` `✻` 一起构成本项目的**符号白名单**（F29）。
# 不在白名单里的符号一律不用，新增要先进 CLAUDE.md 里那张表。
BRANCH_MARK = "⎿"
# 分支行的完整前缀：两格缩进体现层级，符号后两个空格与正文拉开距离。
BRANCH_PREFIX = f"  {BRANCH_MARK}  "
# 分支块**续行**的缩进：与首行正文左缘对齐，让多行结果读起来是一块而不是几条。
# 宽度由 `cell_len` 算而不是写死——`⎿` 是 East Asian Width「模糊」字符，
# 在 CJK 终端里占两格，写死 5 会让续行在中文环境下错位一格。
BRANCH_CONT_INDENT = " " * cell_len(BRANCH_PREFIX)

# 折叠上限（tui-display 扩展 F41 / N5「一切展示都有界」）。
#
# 改造前工具结果**只取首行、截到 80 字符**：一次 `grep` 命中 23 处，用户只看得到
# 第一处，而且**没有任何迹象表明还有别的**——既不知道被省了什么，也没法展开。
# 现在改成保留全文、折叠展示，并在末行如实写出还有多少行。
BRANCH_LINE_LIMIT = 5
# diff 块的行数上限。改造前**完全没有上限**，一次大改动会把整块差异铺进历史区。
# 取 12 而不是 5：diff 的每一行信息量比结果行低（大半是上下文行），
# 5 行往往连一个 hunk 都放不下。
DIFF_ROW_LIMIT = 12
# ⚠ **已停用**（tui-activity-fold 验收期修订）：行内不再写「（Ctrl+O 展开）」。
#
# 它原本挂在每一处被折叠的地方——每个批次聚合行、每个超长结果块、每个 diff 块。
# 一屏上出现四五次同一句话，而它说的是**一个全局快捷键**，重复到第二次就已经
# 没有信息量了；本轮改造的整个目的又恰恰是把重复的过程噪音压下去。
#
# 发现性改由**输入框占位符**承担（那里本来就列着 `/`、Tab、Esc、Ctrl+C，
# 唯独缺 Ctrl+O）——说一次，说在用户找快捷键时会看的地方。
#
# 常量保留是为了让「还剩多少行」那个数字的语义有处可查：**被折叠的部分仍然
# 如实写出行数**，只是不再附带怎么展开。
EXPAND_HINT = ""
# 次级信息的灰色前景。取值沿用改造前 `ToolCallWidget._COLOR_BRANCH` 的 #808080，
# 这样「统一来源」这件事本身不改变任何一处的既有观感。
SECONDARY_COLOR = "#808080"

# 界面主题色。取自 CSS 里 HistoryView / InputBar 的边框与命令面板的分隔线，
# 三处本来就是同一个值，这里给它一个名字供 markup 侧引用。
# ⚠ 改这个值要连同 `app.py` 的 CSS 一起改，否则状态行会与边框脱色。
THEME_COLOR = "#7AEEFF"

# ---------------------------------------------------------------------------
# 详细度档位（tui-activity-fold 扩展 F8/F9/F10）
# ---------------------------------------------------------------------------
# 三档循环取代改造前的布尔开关：
#   0 折叠  批次只显示一行聚合语
#   1 逐条  批次展开为逐次调用；单条仍受结果行数上限约束
#   2 全文  单条显示完整参数与输出原文
#
# ⚠ **用整数而不是两个布尔。** 两个布尔能表达四种状态，其中一种是非法的
# （「不展开批次，却展开单条」）——而非法状态迟早会被某条路径构造出来，
# 到时候表现为「折叠着的批次里露出半截原文」，没有任何报错。
DETAIL_FOLDED = 0
DETAIL_ITEMS = 1
DETAIL_FULL = 2
DETAIL_CYCLE = (DETAIL_FOLDED, DETAIL_ITEMS, DETAIL_FULL)

# 档位的动作措辞。⚠ **不再挂在聚合行末尾**（tui-activity-fold 验收期修订）：
# 那句话说的是一个**全局**快捷键，而一屏上可能有好几个批次、好几个折叠块——
# 同一句话重复四五次，第二次起就没有信息量了，而本轮改造的整个目的恰恰是
# 把重复的过程噪音压下去。
#
# 发现性改由**输入框占位符**承担：说一次，说在用户找快捷键时会看的地方。
#
# 本表保留，供 `/help`、占位符与将来可能的状态提示复用——三档的**名字**
# 仍然需要一处权威定义，而「展开 / 看全文 / 收起」这套**动作**措辞比
# 「档 1 / 档 2 / 档 3」这种状态编号好懂（用户不必先知道自己在第几档）。
NEXT_LEVEL_HINT = {
    DETAIL_FOLDED: "展开",
    DETAIL_ITEMS: "看全文",
    DETAIL_FULL: "收起",
}

# ---------------------------------------------------------------------------
# 回合状态行的旋转标记（tui-activity-fold 扩展 F16/F16a）
# ---------------------------------------------------------------------------
# 四帧循环：空心菱形 → 含心菱形 → 实心菱形 → 含心菱形。
# 由用户指定「菱形向外扩散」并在真实终端逐候选预览后选定。
#
# ⚠ **两条硬约束**：
# 1. **四帧的显示宽度必须两两相等。** 不等的话每换一帧就把整行文字左右推一下，
#    整行看起来在抖，比没有动画更糟。护栏见 `tests/test_tui_status_line.py`。
# 2. 四个字形都已登记进符号白名单（`tests/test_tui_symbols.py`）。
#
# ⚠ **一条已知风险，本轮明确接受**：这组字形属 Unicode「模糊宽度」
# （East Asian Width = Ambiguous）。Rich 的 `cell_len` 按 **1 格**计算，
# 而终端在中日韩环境下**可能画成 2 格**——两者不一致会让该行后续内容错位。
# 用户已在真实终端预览全部候选后选定此方案，故接受；实装后须真机复核。
#
# **若真机复核发现错位，退路是换成全「中性宽度」的字形组**（Rich 与终端
# 两侧都算 1 格、不存在分歧），下面两组均已实测可用：
#     ("⬩", "⟐", "✥", "⟐")   中心小菱形恒在、外框三级外扩
#     ("✢", "✣", "✤", "✥")   同族四变体、笔画风格最统一
# **换字形只需替换这张表，不动任何结构**——记下这条是因为没有它的话，
# 下一个人遇到错位会误以为要重做整个状态行。
# 批次尚未进入执行态时聚合行显示的兜底文案。
# ⚠ **渲染与 `summary_text()` 必须共用它**——各写一份的话，一个刚建好的批次
# 会出现「界面上显示『执行中…』而行为记录里是空串」这种对不上的情况。
DEFAULT_BATCH_VERB = "执行中…"

SPINNER_FRAMES = ("◇", "◈", "◆", "◈")
# 帧间隔（秒）。太快让人眼疲劳，太慢则失去「还活着」的提示作用。
# 刻意是常量而非配置项——本轮不引入新的配置面。
SPINNER_INTERVAL = 0.15

# 系统行分级里两个高档位的**文字**前缀（tui-display 扩展 F19/F21）。
#
# 用文字而不是图形，是为了让它们**脱离颜色也能辨认**：截图、配色异常的终端、
# 端到端驱动抓到的纯文本里，颜色都可能丢失，而这三个字不会。
# 提示级与事件级刻意**没有**前缀——误读那两者的代价为零，见 `append_event`。
# 思考块的标记（F28/F29）。`💭` 换成 `✻`——单色字形，任何终端里都不会被渲染成
# 彩色图形，且这是 Claude Code 的同款记号。它是白名单里唯一的「特例」类符号。
THINKING_MARK = "✻"

WARNING_PREFIX = "警告："
ERROR_PREFIX = "错误："


class ReportLineKind(Enum):
    """
    命令报告里一行的层级（tui-display 扩展 F16）。

    改造前所有报告整段走 `append_system` 的 `[dim]` 通道——段落标题、条目、
    次级信息在视觉上完全等价，一份 `/agents` 报告读起来是一堵均匀的暗色墙。
    本枚举让展示层能按行施加不同的亮度与强调。
    """

    TITLE = "title"      # 首行：加粗 + 强调色，一眼看出「这是一次命令的结果」
    SECTION = "section"  # 段落标题（无缩进的非条目行）：正常亮度 + 加粗
    ITEM = "item"        # 条目行（• / - / · 开头）：正常亮度
    DETAIL = "detail"    # 缩进 ≥4 的次级信息：暗色
    BLANK = "blank"      # 空行


# 条目行的项目符号。`·` 也算，因为部分报告用它做次级列表。
_ITEM_BULLETS = ("•", "-", "·")
# 判定为「次级信息」的最小缩进。取 4 是因为各报告的产出函数一律用
# 「2 空格 = 条目、4 空格 = 该条目的细节」这套缩进（见 subagents/report.py 的
# `_agent_block`：`  • 名字` 配 `    说明：…`）。
_DETAIL_INDENT = 4


def classify_report(text: str) -> "tuple[tuple[ReportLineKind, str], ...]":
    """
    把一段命令报告逐行判定层级。

    ## 为什么只看「行的形状」，不认识任何一个具体报告

    spec F16 要求**各报告的文本产出函数一字不改**——它们是纯函数，且有大量
    逐字断言的护栏钉着（`subagents/report.py` / `team/render.py` /
    `skills/render.py` / `hooks/report.py` …）。为某个报告的具体措辞写判定，
    等于把展示层与那些函数的内容绑死，改一句话就要改这里。

    所以判据只有缩进与首字符。代价是判错时无非是某一行的亮度不对，
    不会出任何功能问题——这是刻意选的、**失败代价极小**的方向。

    判定顺序（**顺序即优先级，不可调换**）：
    1. 第一行且非空 → TITLE；
    2. 空行（或纯空白）→ BLANK；
    3. 去掉前导空格后以 `•` / `-` / `·` 开头 → ITEM
       （**排在缩进判定之前**：一个缩进 6 格的条目仍是条目，不是详情）；
    4. 前导空格 ≥ 4 → DETAIL；
    5. 其余 → SECTION。

    :param text: 报告全文（多行）
    :returns: `((级别, 原始行), …)`，行文本**未转义**——转义由渲染方按 markup
              需要施加，在这里做会让本函数没法被别的渲染方式复用

    副作用：无（纯函数）。
    """
    out: list[tuple[ReportLineKind, str]] = []
    for index, line in enumerate(text.splitlines()):
        stripped = line.strip()
        if index == 0 and stripped:
            out.append((ReportLineKind.TITLE, line))
            continue
        if not stripped:
            out.append((ReportLineKind.BLANK, line))
            continue
        if stripped[0] in _ITEM_BULLETS:
            out.append((ReportLineKind.ITEM, line))
            continue
        if len(line) - len(line.lstrip(" ")) >= _DETAIL_INDENT:
            out.append((ReportLineKind.DETAIL, line))
            continue
        out.append((ReportLineKind.SECTION, line))
    return tuple(out)


# ---------------------------------------------------------------------------
# 面板选项的序号与高亮指示符（tui-display 扩展 F23/F24）
# ---------------------------------------------------------------------------
# 当前高亮项的前缀。与用户消息的前缀**是同一个符号**，这不是冲突而是收敛
# （F29）：两处从不同时出现在同一区域，语义也确实同源——「这是你 / 这是你选的」。
SELECTED_MARK = ">"
# 未选中项的前缀。**必须与 `SELECTED_MARK + " "` 等宽**，否则高亮在选项之间移动
# 时整列文字会左右抖一格。
_UNSELECTED_MARK = " " * (len(SELECTED_MARK) + 1)


def numbered_prompt(index: int, text: str, selected: bool) -> str:
    """
    把一个可选项拼成「指示符 + 序号 + 文本」。

    ## 为什么要序号（F23）

    对齐 Claude Code：带序号的选项可以**直接按数字键选中**，不必先用方向键
    把高亮移过去再回车。四个选项的确认面板因此从「最多按四次方向键 + 回车」
    变成「按一个键」。

    ## 为什么要非颜色的指示符（F24）

    改造前「当前是哪一项」**只靠背景色**表达。配色异常的终端（或截图、
    或端到端驱动设施抓到的纯文本）里那个信息就整个丢了。`>` 让它脱离颜色
    也能读出来。

    :param index: 序号，从 1 开始。**只由调用方对可选项递增**——表头与详情行
        这类 `disabled` 的行不占号，否则按 `2` 会落到一行说明文字上
    :param text: 选项文本，**可以已经是 markup**（三个面板都往里塞
        `[dim]…[/dim]` 的说明）
    :returns: 形如 `"> 1. 本次放行"` / `"  2. 本会话放行"`

    副作用：无（纯函数）。

    ⚠ **本函数不转义**：传进来的文本有的已是 markup，在这里转义会把那些样式
    标签打成字面量。纯文本的转义责任在调用方——三个面板的 `show_*` 里，
    每一处嵌入自由文本（工具名、问题、会话标题）的地方都已各自 `escape` 过。
    """
    mark = f"{SELECTED_MARK} " if selected else _UNSELECTED_MARK
    return f"{mark}{index}. {text}"


# 工具名 → 标题展示标签。**定义在 `tools/display.py`**，这里只是别名。
#
# 单一来源是必须的：`subagents/runner.py` 给活动区渲染子 Agent 的最近调用时
# 用的是同一张表，而它**不能** import 本模块（会撞循环导入，见 display.py 的
# docstring）。两处各写一份的话，同一个工具会在主对话与活动区显示成两个名字。
_TOOL_LABELS = TOOL_LABELS


# 委派工具的特例（spec F12 第 2 条）。这三个字符串必须与 `tools/run_agent.py`
# 的 `parameters` 对得上：`agent` 是角色名、`task` 是任务陈述。
#
# ⚠ **这一支刻意留在本模块、不下沉到 `tools/display.py`**：它要用到
# `BRANCH_AGENT_NAME`，而 display.py import `subagents.tasks` 会成环。
# 子 Agent 侧不受影响——`run_agent` 在 `GLOBAL_DENIED_TOOLS` 里，它调不到。
_DELEGATE_TOOL = "run_agent"
_DELEGATE_LABEL_KEY = "agent"
_DELEGATE_VALUE_KEY = "task"


def resolve_call_title(tool_call, primary_args: "Optional[dict]" = None) -> "tuple[str, str]":
    """
    解析一次工具调用在界面上的标题：`标签(括号内文本)`。

    ## 为什么不再显示「键=值」列表（spec F12）

    改造前是 `Task(name=explorer, task=调研权限层…)`。`name=` `task=` 这些**键名**
    对用户零信息量——它们是给模型看的参数结构；而真正有用的那个值被键名挤占了
    本就不多的横向空间，往往正好在关键处被截断。改成只显示一个主参数的值之后，
    同样的宽度里能看清「在对什么东西做什么」。

    ## 三条分支，顺序固定

    1. **委派特例**（本函数自己处理）：`run_agent` 的标签取**角色名**、括号里放
       **任务描述**，于是委派的工具行与活动区那条终态留痕行**天然同形**——
       用户在两个时刻看到的是同一个东西，不用在脑子里做一次对应（这也是
       Claude Code 的做法：把 agent 类型当标签）。角色名缺席（分支式委派）
       时用占位名。
    2 与 3（已声明主参数 / 回退键值对摘要）由 `tools/display.resolve_call_parts`
    判定——那份是**纯文本**内核，`subagents/runner.py` 给活动区渲染时用的是
    同一份。本函数只在它外面加一层**转义**。

    :param tool_call: `provider.base.ToolCall`，提供 `name` 与 `arguments`
    :param primary_args: `{工具名: 主参数键名}`，由 `app.on_mount` 从工具注册中心
        建一次（工具集启动后不变）。为 None / 空字典时**全部走兜底分支**，
        因此非 DeepSeek Provider（拿不到注册中心）下行为与改造前逐字一致
    :returns: `(标签, 括号内文本)`，**两者都已经过本模块的 `escape`**，可直接拼进
        markup

    副作用：无（纯函数）。

    ⚠ **转义在这里做，不能推给调用方。** F12 让**主参数的原始值**直接进入标题
    （不再被 `键=值` 的格式包裹），而路径、命令、URL、任务描述全是可能含字面
    `[` 的自由文本。漏一次转义就是布局阶段 `MarkupError`、整个应用退出，
    没有任何 try/except 兜得住。
    """
    name = str(getattr(tool_call, "name", "") or "")
    raw = getattr(tool_call, "arguments", None)
    args = raw if isinstance(raw, dict) else {}

    # ── 分支 1：委派特例 ──
    #
    # ⚠ 附加条件 `and args`：参数**还没到**（`tool_pending` 阶段 `arguments` 是
    # None）时不走这一支。否则「角色名缺席 → 用分支占位名」那条规则会把一次
    # 普通的角色委派在参数生成期显示成 `(branch)`——那是**错的信息**，
    # 比显示内部名更糟。等 `tool_start` 带着参数到达时它自然转成角色名。
    if name == _DELEGATE_TOOL and args:
        role = str(args.get(_DELEGATE_LABEL_KEY) or "").strip() or BRANCH_AGENT_NAME
        return escape(role), escape(clip_value(args.get(_DELEGATE_VALUE_KEY) or ""))

    label, inner = resolve_call_parts(tool_call, primary_args)
    return escape(label), escape(inner)


class DetailLevelClicked(TextualMessage):
    """
    某一行被鼠标点开/收起了，携带它切到的**新档位**。

    ## 为什么要往上报（真机反馈）

    `Ctrl+O` 是**全局**档位，由 `RhineApp._detail_level` 保管；鼠标点击是
    **单行**的，只改那一行。两者各记各的，于是出现这个现象——

    > 「当我先点击展开后，得按两下 `Ctrl+O` 才能切换到第 3 档。」

    因为点击把那一行推到了逐条档，而全局档位仍停在折叠：第一下 `Ctrl+O`
    只是把全局从折叠推到逐条（那一行**看不出任何变化**），第二下才到全文。
    用户按下去没反应，会以为是按键丢了。

    修法是让点击**把全局档位也带到同一处**——此后 `Ctrl+O` 接着往下走。
    ⚠ **只同步数字，不重新广播**：广播会把满屏的批次一起摊开，
    而「点一下只开这一个」正是鼠标存在的理由。
    """

    def __init__(self, level: int) -> None:
        super().__init__()
        self.level = level


class SelectableStatic(Static):
    """
    **能被拖选、能被高亮、能被复制**的 `Static`——即便内容来自 Rich 渲染对象。

    ## 它解决的是「屏幕上根本没反应」，不只是「复制不到」

    Textual 把 Rich 可渲染对象包进 `RichVisual`，而选区功能对它**三处一起失效**
    （逐条论证见 `content_from_rich` 的说明）：不画高亮、拿不到字符偏移、
    默认的 `get_selection` 直接返回 None。上一轮只补了第三条，于是出现了用户
    描述的那个状态——「其他的连选择都不行」：内容其实进了选区，但屏幕上
    一点反馈都没有，也没法只选其中一段。

    本类的做法是**把 Rich 对象在渲染那一刻转成 `Content`**（`set_rich`），
    之后一切都走 Textual 的原生通路：高亮它自己会画，偏移它自己会写，
    `get_selection` 用父类的默认实现就够了。视觉与转换前一模一样。

    ## 两种用法

    - `update(markup)`：内容本来就是 markup 字符串，什么都不用做（原生已支持）。
    - `set_rich(renderable)`：内容是 Rich 对象（AI 正文的 Markdown、
      工具行的标题 + 结果块），由本类在 `render()` 里按**实时宽度**转换。

    ⚠ **宽度必须是渲染期的实时值**：Markdown 要按宽度折行、diff 块要按宽度
    补整行背景。提前转好存起来的话，终端一 resize 排版就错了。
    """

    def __init__(self, *args, plain: str = "", **kwargs) -> None:
        super().__init__(*args, **kwargs)
        # 兼容字段：仍有调用方（回放路径、测试）按纯文本读这一行的内容。
        # 走 `set_rich` 时它由转换结果回填，不必调用方自己维护。
        self._plain = plain
        self._rich = None
        # (宽度, Content) 记忆化。流式正文每来一块就重画一次，不缓存的话
        # 每一块都要把整篇 Markdown 重排一遍——宽度没变时直接复用即可。
        self._rich_cache: "Optional[tuple[int, Content]]" = None
        # `get_content_height` 先于 `render` 被调用，且它拿得到确切宽度；
        # 记下来供 `render` 在 `self.size` 尚未定稿时兜底。
        self._last_width = 0

    def set_plain(self, text: str) -> None:
        """更新纯文本缓存。**改了显示内容就要跟着调**，否则复制到的是旧内容。"""
        self._plain = text or ""

    def set_markup(self, markup: str) -> None:
        """
        把本行换回**普通 markup 字符串**内容（并清掉 Rich 内容与记忆化）。

        markup 字符串走 Textual 原生的 `Content` 通路，选区功能本来就成立，
        所以这条路径不需要任何额外处理——但**必须把 `_rich` 清掉**，
        否则 `render()` 会继续画上一次的 Rich 内容（内容不更新，且不报错）。
        """
        self._rich = None
        self._rich_cache = None
        self.update(markup)
        self._plain = RichText.from_markup(markup).plain

    def set_rich(self, renderable, plain: str = "") -> None:
        """
        把本行的内容换成一个 Rich 可渲染对象（在渲染期转成 `Content`）。

        :param renderable: 任意 Rich 可渲染对象
        :param plain: 可选的纯文本；不给则由转换结果回填

        副作用：清掉记忆化并请求重排（内容高度可能变）。
        """
        self._rich = renderable
        self._rich_cache = None
        if plain:
            self._plain = plain
        self.refresh(layout=True)

    def plain_text(self) -> str:
        """
        本行**当前显示内容的纯文本**（唯一权威来源）。

        ⚠ **不要改回直接读 `widget.content`**：走 `set_rich` 的行（终态工具行、
        AI 正文）内容不在那里，读到的是上一次 markup 的残留——测试会拿着
        「执行中…」去断言「失败」，红得莫名其妙。
        """
        if self._rich is not None:
            width = self.size.width or self._last_width or 80
            return self._rich_content(width).plain
        return self._plain

    def _rich_content(self, width: int) -> Content:
        """按给定宽度取（并缓存）转换结果，同时回填纯文本缓存。"""
        if self._rich_cache is not None and self._rich_cache[0] == width:
            return self._rich_cache[1]
        content = content_from_rich(self._rich, width)
        self._rich_cache = (width, content)
        self._plain = content.plain
        return content

    def render(self):
        """
        渲染本行。带 Rich 内容时转成 `Content`，否则沿用父类（markup 字符串）。
        """
        if self._rich is None:
            return super().render()
        width = self.size.width or self._last_width
        if not width:
            # 还没排版过，先给一个不会崩的值；拿到真实宽度后会重画。
            width = 80
        return self._rich_content(width)

    def get_content_height(self, container, viewport, width: int) -> int:
        """
        算内容高度。**这里是全流程中第一个知道确切宽度的地方**，顺手记下来
        供 `render()` 兜底——否则首次渲染可能按 80 列排版、与实际宽度不符。
        """
        if self._rich is not None and width:
            self._last_width = width
            return self._rich_content(width).get_height(self.styles, width)
        return super().get_content_height(container, viewport, width)

    def get_selection(self, selection):
        """
        交出被选中的那段文本。

        ⚠ **不可见时必须返回 None**：折叠起来的行**照样会被全选问到**，
        不挡的话用户拿到的文本里会混进屏幕上根本没有的内容
        （所见非所得，与「看得见却复制不走」是同一类毛病的两面）。

        可见时交给父类：本类的 `render()` 保证产出的是 `Content`，
        父类的默认实现对它是完全正确的（还顺带支持了「只选中一部分」）。
        """
        if not self.display:
            return None
        result = super().get_selection(selection)
        if result is not None:
            return result
        # 父类拿不到（极少数仍用非 Content 渲染对象的路径）时退回纯文本缓存
        if not self._plain:
            return None
        return selection.extract(self._plain), "\n"


class ToolCallWidget(SelectableStatic):
    """
    单个工具调用的展示行，自管理计时。

    标题统一用展示标签（_TOOL_LABELS，如 Read/Grep/Update）而非内部工具名（snake_case）。

    四种视觉状态：
    - 参数生成中：橘色，"● 标签 参数生成中… Ns"（pending=True 时的初始态，见下）
    - 执行中：橘色，显示 "● 标签(主参数) 执行中… Ns"，N 由主线程定时器每秒刷新
    - 成功：绿色 "● 标签(主参数) 完成 [(Ns)]" + 下方 "⎿ 结果块"
    - 失败：红色 "● 标签(主参数) 失败 [(Ns)]" + 下方 "⎿ 错误块"

    终态的耗时括号**不足一秒时整个不出现**（F13，见 `_elapsed_suffix`）。

    **为什么需要「参数生成中」这一态**：写文件类调用的参数里塞着整份文件内容，
    模型生成这段 JSON 可能要几十秒，而真正的写盘往往只花几毫秒。若只有「执行中」态，
    那一行会在肉眼看不见的时间里闪过（实测 tool_start 到 tool_result 相隔 2 毫秒），
    用户看到的是一个几十秒完全静止的窗口，然后确认面板突然弹出来。

    计时不依赖 Worker 线程：on_mount 中用 set_interval 在主线程每秒触发重绘，
    因此即使 Worker 正阻塞在工具执行/并发等待中，耗时显示仍持续更新（spec F14/N3）。
    """

    # 执行中橘色 / 成功绿色 / 失败红色
    _COLOR_RUNNING = "#FFA500"
    _COLOR_OK = "#5FD75F"
    _COLOR_FAIL = "#FF5F5F"
    # 分支行的灰色**不在这里定义**：它与活动区、命令报告共用模块级的
    # `SECONDARY_COLOR`（F14 / AC12）。此处保留同名别名只是为了不改动既有调用点，
    # 取值必须继续指向那一处，别改回字面量。
    _COLOR_BRANCH = SECONDARY_COLOR

    def __init__(self, tool_call, pending: bool = False, primary_args: "Optional[dict]" = None) -> None:
        """
        :param tool_call: provider.base.ToolCall，提供工具名与参数用于展示
        :param pending: True 表示「模型还在生成调用参数」，此时 tool_call.arguments
                        通常为 None，本行先只显示工具名；等 TOOL_START 到达时由
                        begin_running() 补上参数摘要并转入执行态
        :param primary_args: `{工具名: 主参数键名}`（F12）。由 `HistoryView` 透传，
                             缺省 None → 标题全部走键值对摘要，形态与改造前一致
        """
        # ⚠ 必须给一个**初始内容**（空串）而不是留空。留空时 renderable 是 None，
        # 而本组件进入批次容器之后，挂载时序变了——Textual 可能在 `on_mount`
        # 跑之前先渲染一次，于是在合成器里抛
        # `AttributeError: 'NoneType' has no attribute 'render_strips'`。
        # 那是**布局阶段主线程**的异常，业务调用栈上没有任何线索。
        # 改造前它直接挂在历史区、总是先 on_mount 再渲染，所以一直没暴露。
        super().__init__("", markup=True)
        self._pending = pending
        self._name = tool_call.name
        self._primary_args = primary_args or {}
        self._label, self._args_summary = resolve_call_title(tool_call, self._primary_args)
        self._start = 0.0
        self._timer = None  # set_interval 返回的定时器，finish 时停止
        # ── 终态的完整素材（F41）──
        # 组件**保留全文**，折叠只发生在渲染那一刻。存半截的话展开就没得展了，
        # 而那正是改造前「只取首行截到 80 字符」的问题所在。
        self._finished = False
        self._ok = True
        self._summary = ""
        self._diff = None
        self._final_elapsed = 0
        # 全局详细度档位的本地副本，由 `set_detail_level` 广播进来
        # （见 app.action_toggle_expand）。改造前是个布尔 `_expanded`，
        # 三档循环之后换成整数——理由见模块级 `DETAIL_CYCLE` 的注释。
        self._detail_level = DETAIL_FOLDED
        # ── 归并（tui-activity-fold A 组）──
        # 保留原始调用：**最详细一档要重算标题**（完整参数、不截断），
        # 而 `_args_summary` 是构造时就算好的截断版。
        self._tool_call = tool_call
        # 所属批次（可能为 None——不可归并的工具独立成行）。
        # ⚠ 回调只在主线程内直接发生，不新增任何跨线程通道。
        self._batch: "Optional[ToolBatchWidget]" = None
        # 最详细一档显示的输出原文；空则回退显示 `_summary`
        self._detail = ""
        # 本行当前显示内容的**纯文本**，供选中复制使用（见 `get_selection`）。
        # 每次重绘时同步更新——它必须与屏幕上看到的一致。
        self._plain = ""

    def on_mount(self) -> None:
        """
        挂载后记录起始时刻并立即渲染一次。

        ## 为什么这里**没有**每秒刷新的定时器（tui-activity-fold F19）

        改造前每一行都自持一个 `set_interval(1.0)` 来滚动「执行中… Ns」。
        并发执行五个只读工具时，屏幕上就有五个数字各自在跳——它们表达的是
        同一件事（「还在跑」），却占了五份注意力。

        现在这件事统一由**底部的回合状态行**（`StatusLine`）承担：
        全界面同时至多一个该类定时器，显示的是**本回合总耗时**。

        ⚠ **这个改动依赖状态行先存在**（实现期为此把本任务从 A 组挪到了 C 组
        之后）。tui-display 曾有一条反证护栏专门保护这里的秒数，理由是
        「那是参数生成期界面上唯一的活体信号」——那条需求仍然成立，
        只是承载者从工具行换成了状态行。**在状态行做好之前撤掉它，
        `write_file` 的参数生成期（模型吐整份文件内容，可能几十秒）
        会退回到一个完全静止的窗口**，而写文件不参与归并、折叠救不了它。

        ⚠ **终态的耗时不受影响。** `finish` 时用 `monotonic() - self._start`
        一次性算出即可，那从来不需要定时器——定时器只是为了让**运行中**的
        数字每秒变一下。
        """
        self._start = monotonic()
        self._render_running()

    @property
    def pending(self) -> bool:
        """
        本行是否仍停在「参数生成中」阶段（即从未进入执行）。

        供 TUI 的 Worker 判断该不该先补参数摘要再定色。只读一个 bool、不碰 Textual
        任何 API，因此可以在 Worker 线程里直接读，无需 call_from_thread。
        """
        return self._pending

    def begin_running(self, tool_call) -> None:
        """
        从「参数生成中」转入「执行中」：补上完整参数摘要并**重新起算耗时**。

        由 TUI 的 Worker 在收到 TOOL_START 时通过 call_from_thread 调用（线程安全）。
        对本来就是执行态的行调用它同样安全（幂等地刷新一次参数摘要）。

        **为什么重置 self._start**：pending 阶段的计时覆盖「模型生成参数」，而这一行
        最终定色时显示的 "(Ns)" 语义是**工具执行耗时**——沿用旧起点会把生成时间、
        乃至用户盯着确认面板发呆的时间都算进去，一次 2 毫秒的写盘可能显示成 "(600s)"。
        生成阶段花了多久用户刚才已经在屏幕上看着它涨了，不必再累计一遍。

        :param tool_call: 参数已完整的同一次调用（id 与本行一致）
        """
        self._pending = False
        # 标签也要重算：委派工具在 pending 阶段还不知道派给哪个角色，
        # 参数到齐后标签才从内部名转成角色名（F12 分支 1）。
        self._label, self._args_summary = resolve_call_title(tool_call, self._primary_args)
        # 最详细一档要按**当前**参数重算完整标题，故原始调用也要更新
        self._tool_call = tool_call
        self._start = monotonic()
        # 告诉所属批次「这一类活儿开始了」，让聚合行切到对应的进行时文案（F5）
        if self._batch is not None:
            self._batch.note_running(self._name, self._args_summary)
        self._render_running()

    def _elapsed(self) -> int:
        """返回从当前阶段起算到现在的整数秒数。"""
        return int(monotonic() - self._start)

    def _render_running(self) -> None:
        """
        以橘色渲染进行中状态（按阶段选文案）。

        ⚠ **不显示秒数**（tui-activity-fold F19/AC20）：运行中的时间统一由
        底部的回合状态行显示**一处总耗时**。「这一行还在跑」由橘色 + 文案
        表达，不需要每行各带一个数字来证明。
        """
        if self._pending:
            # 参数还没到，写不出参数摘要，故不带括号——写成 "Write()" 像是无参调用。
            markup = f"[{self._COLOR_RUNNING}]● {self._label} 参数生成中…[/]"
        else:
            markup = (
                f"[{self._COLOR_RUNNING}]● {self._label}({self._args_summary}) 执行中…[/]"
            )
        self.set_markup(markup)

    def finish(self, ok: bool, summary: str, diff=None, detail: str = "") -> None:
        """
        结束计时并切换到成功/失败终态。

        由 TUI 的 Worker 通过 call_from_thread 在主线程调用，线程安全。

        本方法只**记下终态素材**，画由 `_render_finished` 负责——两者分开是为了
        让 `set_detail_level` 能在任何时候重画同一行（展开/收回）。

        ## 为什么结果文本收两份

        `summary` 是工具自报的**规模描述**（「读取 234 行 · 12.3 KB」），
        `detail` 是**输出原文**。两者服务不同档位：折叠与逐条档要的是前者
        （一眼看清做了多大一件事），最详细一档要的是后者（核对具体内容）。

        改造前只收一份且优先取 summary，于是**展开之后看到的仍是那句规模描述**
        ——「展开」等于没展开。这是 tui-activity-fold 自检时发现的缺口。

        :param ok: 工具是否成功（决定绿/红）
        :param summary: 结果的规模描述，**可以是多行**（折叠交给渲染，见
                        `BRANCH_LINE_LIMIT`）
        :param diff: 可选的 tools.diff.DiffView。改文件类工具会带上它，
                     此时在状态行下方追加渲染一个彩色 diff 块；其它工具留空。
        :param detail: 输出原文，仅最详细一档显示；**为空时该档回退显示 summary**

        副作用：停止计时定时器，原地更新本行内容，并回调所属批次重算聚合语。
        """
        if self._timer is not None:
            self._timer.stop()
        self._final_elapsed = self._elapsed()
        self._finished = True
        self._detail = detail or ""
        self._ok = ok
        self._summary = summary or ""
        self._diff = diff
        self._render_finished()
        # 回调所属批次重算聚合语（计数与失败数都可能变）。
        # ⚠ 在 `_render_finished` **之后**：本行先把自己画对，再让批次去读
        # 已经落定的状态——反过来的话批次读到的是上一轮的成败。
        if self._batch is not None:
            self._batch.note_finished(self._name, ok)

    @property
    def batch(self) -> "Optional[ToolBatchWidget]":
        """
        本行所属的批次；**独立成行时为 None**。

        `HistoryView.set_detail_level` 靠它区分「批次内的子行」（档位由批次转发）
        与「独立行」（直接下发），避免重复重绘。
        """
        return self._batch

    @property
    def primary_text(self) -> str:
        """
        本次调用的主参数值（纯文本、未转义），供所属批次做从属行显示。

        与标题括号里的是同一份内容——批次的从属行与展开后的工具行标题
        因此不会出现「同一次调用，两处显示的参数不一样」。
        """
        return self._args_summary

    def set_batch(self, batch: "Optional[ToolBatchWidget]") -> None:
        """
        登记所属批次（tui-activity-fold A 组）。

        登记之后，本行每次进入执行态或定色都会回调批次重算聚合语。

        ⚠ **回调只在主线程内直接发生**——`begin_running` 与 `finish` 本身
        就是 Worker 经 `call_from_thread` 调进主线程的，因此这条链上
        **不新增任何跨线程通道**（本项目已因「加锁临界区内做跨线程调度」死锁四次）。

        :param batch: 所属批次；不可归并的工具独立成行，传 None
        """
        self._batch = batch

    def on_click(self, event) -> None:
        """
        点这一行 → 这**一次调用**在「逐条 ↔ 全文」之间切换。

        与批次那一层配合成两级：点聚合行摊开这一批，点其中某一条看它的
        完整参数与输出原文——不必为了看一次调用的细节把满屏都切到全文档。

        ⚠ 仍处**折叠档**时点单条是够不着的（那时它根本不可见），
        因此这里只在「逐条 ↔ 全文」之间切，不回落到折叠。

        ⚠ `event.stop()` 的理由与批次那边相同：不拦会冒泡到可滚动的历史区。

        ⚠ **拖选也会发 `Click`，必须挡掉。** Textual 判定「是不是一次点击」
        的依据是 **`MouseDown` 与 `MouseUp` 落在同一个组件**——**根本不看鼠标
        有没有移动**（`app.py`：`if mouse_up_widget is mouse_down_widget`）。
        于是在一行之内拖着选文字，抬手时照样发 Click，内容就被展开了。
        用户原话：「我选择以后会自动展开」。

        判据用 `screen.selections`：单纯点击时它是空的，拖选过就非空。
        """
        event.stop()
        if self.screen.selections:
            return
        nxt = DETAIL_ITEMS if self._detail_level == DETAIL_FULL else DETAIL_FULL
        self.set_detail_level(nxt)
        # 把全局档位带到同一处，否则下一次 `Ctrl+O` 会先「补」上这一步、
        # 按下去看不出任何变化（见 `DetailLevelClicked` 的说明）。
        self.post_message(DetailLevelClicked(nxt))

    def set_detail_level(self, level: int) -> None:
        """
        接收全局详细度档位的广播（`Ctrl+O`，见 `app.action_toggle_expand`）
        或单次点击。

        只对**已定色**的行重画；仍在执行中的行没有分支内容可展，记下档位即可，
        等它 `finish` 时自然按新档位渲染。

        :param level: `DETAIL_FOLDED` / `DETAIL_ITEMS` / `DETAIL_FULL` 之一

        副作用：可能原地重绘本行。
        """
        if self._detail_level == level:
            return
        self._detail_level = level
        if self._finished:
            self._render_finished()

    def set_expanded(self, expanded: bool) -> None:
        """
        **薄封装**，保留供回放路径与既有测试使用（它们只认「展开 / 收起」两态）。

        映射：展开 → 最详细一档；收起 → 折叠档。
        新代码请直接用 `set_detail_level`。

        :param expanded: 展开为真、折叠为假
        """
        self.set_detail_level(DETAIL_FULL if expanded else DETAIL_FOLDED)

    def _branch_block(self) -> RichText:
        """
        把结果摘要渲染成分支块：首行带 `⎿`，续行缩进对齐，超限时折叠。

        折叠时**保留前 `BRANCH_LINE_LIMIT` 行**并在末尾追加「… +N 行（Ctrl+O 展开）」。
        为什么写出确切的 N 而不是一个「更多」：改造前只取首行、且**没有任何迹象
        表明还有别的**——用户既不知道被省了什么，也没法展开。数字本身就是那个迹象。

        用 `RichText` 纯文本而不是 markup：结果摘要来自工具输出原文，
        含 `[` 是常态，纯文本渲染天然免转义（与本类既有做法一致）。
        """
        # 档位决定读哪一份素材（tui-activity-fold F12）：
        # 最详细一档读**输出原文**，其余读工具自报的那句规模描述。
        # `_detail` 为空时回退——脚本化 Provider 与回放路径都可能只给 summary。
        source = self._detail if (self._detail_level == DETAIL_FULL and self._detail) else self._summary
        lines = source.split("\n")
        # 去掉尾部空行：命令输出几乎都以换行结尾，留着会白占一行折叠额度
        while lines and not lines[-1].strip():
            lines.pop()
        if not lines:
            lines = [""]

        hidden = 0
        if self._detail_level != DETAIL_FULL and len(lines) > BRANCH_LINE_LIMIT:
            hidden = len(lines) - BRANCH_LINE_LIMIT
            lines = lines[:BRANCH_LINE_LIMIT]

        rendered = [BRANCH_PREFIX + lines[0]]
        rendered.extend(BRANCH_CONT_INDENT + line for line in lines[1:])
        if hidden:
            rendered.append(f"{BRANCH_CONT_INDENT}… +{hidden} 行")
        return RichText("\n".join(rendered), style=self._COLOR_BRANCH)

    def _render_finished(self) -> None:
        """
        画终态（成功/失败）。`finish` 与 `set_expanded` 共用这一处。

        统一为两段式：第一行 `● 标题 完成/失败 [(Ns)]`，其下是 `⎿` 分支块。
        """
        color = self._COLOR_OK if self._ok else self._COLOR_FAIL
        # 成功态**不写「完成」二字**（tui-activity-fold F7）：绿色已经把状态说完了，
        # 文字重复一遍只是多占宽度、多一处视觉停顿。
        #
        # ⚠ **失败态的「失败」必须保留。** 它是这一行脱离颜色之后**唯一**还能
        # 辨认状态的依靠——截图、配色异常的终端、端到端驱动抓到的纯文本里，
        # 颜色全都可能丢失。这个不对称是刻意的：成功是常态（可以安静），
        # 失败要抢注意力（必须写出来）。
        #
        # 前导空格并进本变量而不是留在 f-string 里，否则成功态会拖一个尾部空格。
        result = "" if self._ok else " 失败"
        diff = self._diff
        if diff is not None and diff.rows:
            # 改文件类工具（成功）：标题用 diff 自带的 op/path（比工具名+参数摘要
            # 更贴近改动语义），分支由 render_diff_block 产出。
            header = (
                f"[{color}]● {escape(str(diff.op))}({escape(str(diff.path))})"
                f"{result}{self._elapsed_suffix()}[/]"
            )
            block = render_diff_block(
                diff, expanded=self._detail_level == DETAIL_FULL
            )
            self.set_rich(RichGroup(RichText.from_markup(header), block))
            return
        # 其它工具（或改文件但无差异）：标题用 "标签(参数摘要)"。
        # 仍处 pending 的行（参数没生成完就被取消/拒绝）不写括号——那会显示成
        # "Write() 失败"，像是「调用无参数」而不是「参数没来得及生成」。
        header = f"[{color}]● {self._title_text()}{result}{self._elapsed_suffix()}[/]"
        branch = self._branch_block()
        self.set_rich(RichGroup(RichText.from_markup(header), branch))

    def _title_text(self) -> str:
        """
        按当前档位产出标题（**已转义**，可直接嵌进 markup）。

        ## 两份内容，不是一份的长短版（tui-activity-fold F11）

        - 折叠 / 逐条档：`标签(主参数值)`——挑一个最有辨识度的值给人扫读，
          其余参数不显示，长值截断。
        - 最详细一档：`标签(键: 值, 键: 值…)`——**列全部参数、每个值都不截断**。

        改造前展开态沿用构造时算好的截断标题，于是「展开」了却看不到被截掉的
        部分——那正是本方法要解决的问题。

        ⚠ 仍处 pending 的行不写括号：参数还没生成完，写成 `Write()`
        像是「调用无参数」而不是「参数没来得及生成」。

        ⚠ **转义只做一次。** `self._label` 与 `self._args_summary` 来自
        `resolve_call_title`，那个函数**已经转义过**；而 `resolve_full_title`
        是 `tools/display` 的纯文本内核、**未转义**，必须在这里补上。
        搞反任一边都会出问题：漏转会在布局阶段抛 `MarkupError` 并拆掉整个应用，
        重复转会让用户看到字面的 `\\[`。
        """
        if self._pending:
            return self._label
        if self._detail_level == DETAIL_FULL:
            label, inner = resolve_full_title(self._tool_call)
            # 参数为空时退回只写标签，避免出现一个空括号
            return f"{escape(label)}({escape(inner)})" if inner else escape(label)
        return f"{self._label}({self._args_summary})"

    def _elapsed_suffix(self) -> str:
        """
        终态的耗时后缀，**不足一秒时返回空串**（tui-display 扩展 F13）。

        改造前每一行都挂着 `(0s)`。绝大多数工具调用是毫秒级的
        （实测 `write_file` 从 `tool_start` 到 `tool_result` 只隔 2 毫秒），
        那个恒为零的括号是纯噪音，还会把真正跑了很久的那几行淹掉——
        一屏十个 `(0s)` 里夹着一个 `(43s)`，反而不显眼了。

        ⚠ **只管终态。** 执行中的实时计时（`_render_running`）一字不动：
        那是那段时间里界面上唯一的活体信号，从 0s 开始涨正是它的价值所在。
        """
        if self._final_elapsed < 1:
            return ""
        return f" ({self._final_elapsed}s)"


class ToolBatchWidget(SelectableStatic):
    """
    一批**连续的只读检索调用**在历史区的呈现（tui-activity-fold 扩展 A 组）。

    ## 它解决什么

    改造前每次工具调用各占屏幕两行且全部累积：一次「读一下项目」发 20 次
    `Read`/`Glob`/`Grep`，历史区净增 40 行，把真正要读的结论淹掉了。
    本组件把这样一批调用收成**一行聚合语**，默认折叠。

    ## 两种形态，按「封闭与否」切换

    - **未封闭**（还可能有新调用进来）：`● 搜索中…` + 从属行显示当前调用的参数
    - **已封闭**：`● 搜索内容 3 次 · 读取 2 个文件` + 档位提示

    ⚠ **运行期间的形态与最终调用数量无关。** 批次的规模只有封闭时才知道——
    第一次 `Read` 发出去时，无从预知后面还会不会有第二次。因此
    「按数量选形态」这件事在运行中做不到，也不该做。

    ## ⚠ 为什么它**不是容器**

    最自然的写法是让批次做一个 `Vertical`、把工具行 mount 进去。
    实现期试过，**在挂载时序上踩了一连串不报错的坑**：

    - `HistoryView.add_tool_widget` 挂完批次会**紧接着**调 `attach`，
      而此时容器自身的挂载还没落地。往未挂载的容器 mount，实测会让
      **整个批次连同工具行从 DOM 里消失**（挂一条系统行之后
      `#history-messages` 里只剩那条系统行），且不报任何错。
    - 改成「先入队、`on_mount` 时补挂」之后，补挂要多一轮事件循环才落地；
      在那之前若发生一次布局（例如封闭批次触发重绘），批次照样被挤掉。

    现在的做法从**结构上**消掉这一整类问题：**批次只是一个 `Static`
    （就是那行聚合语），工具行仍旧直接挂在历史区、与批次平级。**
    批次只持有它们的引用来控制可见性。DOM 上没有嵌套，也就没有嵌套的时序。

    折叠时把这些工具行 `display = False`，屏幕上就只剩聚合行那一行——
    与「装进容器再隐藏容器」的视觉效果完全一致。

    ## 三个不变量

    1. **一切嵌入的纯文本必须过本模块的 `escape`。** 主参数值来自工具参数，
       含方括号是常态，落单的 `[` 会在布局阶段的主线程抛 `MarkupError`，
       没有任何 try/except 兜得住。
    2. **状态变更方法只在主线程内被调用**（`ToolCallWidget.finish` 本身就是
       经 `call_from_thread` 到主线程的），本类**不新增任何跨线程通道**。
    3. **本类不参与 `tool_widgets` 表的登记与摘除。** Worker 侧仍持有
       `ToolCallWidget` 引用、仍按原口径 `pop`。
    """

    def __init__(self, detail_level: int = DETAIL_FOLDED) -> None:
        """
        :param detail_level: 建立时的详细度档位，由 `HistoryView` 按全局档位传入
                             （新批次必须跟上当前档位，否则展开状态下新产生的
                             批次会是折叠的）
        """
        super().__init__("", markup=True)
        # ⚠ **字段名前缀 `_batch_` 是刻意的，别改回 `_closed`。**
        #
        # Textual 的 `MessagePump`（`Static` 的基类之一）在实例上放了一个
        # `_closed` 属性，用来标记「这个组件的消息泵已关停」。本类原先把
        # 「批次已封闭」也存成 `self._closed`，于是 `close()` 一执行就等于
        # 告诉 Textual「我关停了」——**它随后把整个节点从 DOM 里清理掉**，
        # 界面上批次连同它统辖的工具行一起消失，而且**不报任何错**。
        #
        # 这是本轮撞上的**第二个** Textual 内部名（第一个是 `_render`，
        # 那次的表现是合成器里抛 `'NoneType' has no attribute 'render_strips'`）。
        # 新增字段前先在实例上 `hasattr` 查一遍，比事后二分省得多。
        self._batch_closed = False
        # [(工具名, 是否成功)]，按发生时序；第二项 None 表示尚未产生结果
        self._entries: "list[tuple[str, Optional[bool]]]" = []
        # 本批次统辖的工具行。**只持引用，不做父节点**——它们挂在历史区里，
        # 与本组件平级（见类 docstring）。
        self._widgets: "list[ToolCallWidget]" = []
        self._detail_level = detail_level
        # 未封闭态显示的两段：进行时文案 + 当前调用的主参数值
        self._running_text = ""
        self._running_arg = ""
        # 单次调用时聚合行下方要保留的那条从属行（F4）——多次时为空
        self._single_arg = ""

    def on_mount(self) -> None:
        """挂载后立即画一次，避免出现一瞬间的空行。"""
        self._repaint()

    @property
    def closed(self) -> bool:
        """本批次是否已封闭（封闭后不再接受新的工具行）。"""
        return self._batch_closed

    @property
    def call_count(self) -> int:
        """本批次已纳入的调用次数（供测试与埋点用）。"""
        return len(self._entries)

    def attach(self, widget: "ToolCallWidget", tool_name: str) -> None:
        """
        把一条工具行纳入本批次。

        ⚠ **本方法不挂载 widget**——挂载由 `HistoryView` 负责，工具行与本组件
        在历史区里是**平级**的（理由见类 docstring：嵌套会踩挂载时序的坑）。
        这里只登记引用与计数，并按当前档位设定它的可见性。

        :param widget: 工具行（挂载与否都可以，本方法不关心）
        :param tool_name: 内部工具名（用于聚合计数，非展示标签）

        副作用：登记 entry 与引用、给 widget 装上回指本批次的引用、
        改 widget 的可见性与档位、重绘聚合行。
        """
        self._entries.append((str(tool_name or ""), None))
        self._widgets.append(widget)
        widget.set_batch(self)
        widget.set_detail_level(self._detail_level)
        # 折叠档下工具行整体不可见——折叠的全部意义就在这里
        widget.display = self._detail_level != DETAIL_FOLDED
        # 单次时聚合行下面要显示这一次调用的参数（F4），先记下来
        self._single_arg = widget.primary_text
        self._repaint()

    def note_running(self, tool_name: str, primary_value: str) -> None:
        """
        某次调用进入执行态：更新未封闭态显示的进行时文案与从属行（F5）。

        并发时**后到的覆盖先到的**——从属行的语义是「最近开始的那一个」。

        :param tool_name: 内部工具名
        :param primary_value: 该次调用的主参数值（已是纯文本，未转义）
        """
        self._running_text = running_verb(tool_name)
        self._running_arg = primary_value or ""
        self._single_arg = self._running_arg or self._single_arg
        self._repaint()

    def note_finished(self, tool_name: str, ok: bool) -> None:
        """
        某次调用定色：把对应 entry 的成败落定并重算聚合语。

        按工具名从后往前找**第一个尚未落定**的条目——同一个批次里同名调用
        可能有多次，从后往前配对与「后发起的先完成」这种并发形态更吻合，
        且无论配到哪一个，聚合计数的结果都相同（计数只看总数与失败数）。

        :param tool_name: 内部工具名
        :param ok: 该次调用是否成功
        """
        name = str(tool_name or "")
        for i in range(len(self._entries) - 1, -1, -1):
            entry_name, entry_ok = self._entries[i]
            if entry_name == name and entry_ok is None:
                self._entries[i] = (entry_name, bool(ok))
                break
        self._repaint()

    def close(self) -> None:
        """
        封闭本批次：形态从进行时切到完成时，此后不再接受新行。

        **幂等**——`_mount_widget` 每挂一条非工具行内容就会调它一次，
        而连续几条系统行是常态。

        副作用：重绘聚合行。
        """
        if self._batch_closed:
            return
        self._batch_closed = True
        self._repaint()

    def on_click(self, event) -> None:
        """
        点这一行 → 这**一个**批次在「折叠 ↔ 逐条」之间切换。

        `Ctrl+O` 是全局档位，管所有批次；鼠标是**单个**的——想看某一批具体
        做了什么，不必把满屏的批次一起摊开。

        ⚠ **必须 `event.stop()`**：不拦的话事件继续往上冒泡到历史区，
        而历史区是可滚动容器，Textual 会把它当成一次滚动交互处理
        （表现为「点一下内容跳一段」）。

        ⚠ **拖选也会发 `Click`，必须挡掉。** Textual 判定「是不是一次点击」
        的依据是 **`MouseDown` 与 `MouseUp` 落在同一个组件**——**根本不看鼠标
        有没有移动**（`app.py`：`if mouse_up_widget is mouse_down_widget`）。
        于是在一行之内拖着选文字，抬手时照样发 Click，内容就被展开了。
        用户原话：「我选择以后会自动展开」。

        判据用 `screen.selections`：单纯点击时它是空的，拖选过就非空。
        """
        event.stop()
        if self.screen.selections:
            return
        nxt = DETAIL_FOLDED if self._detail_level != DETAIL_FOLDED else DETAIL_ITEMS
        self.set_detail_level(nxt)
        self.post_message(DetailLevelClicked(nxt))

    def set_detail_level(self, level: int) -> None:
        """
        接收全局档位广播（`Ctrl+O`）或单次点击。

        折叠档 → 本批次统辖的工具行整体隐藏，屏幕上只留聚合行；
        其余档位 → 显示它们并把档位逐个转发。

        :param level: `DETAIL_FOLDED` / `DETAIL_ITEMS` / `DETAIL_FULL` 之一

        副作用：改所辖工具行的可见性、转发档位、重绘聚合行。
        """
        if level == self._detail_level:
            return
        self._detail_level = level
        visible = level != DETAIL_FOLDED
        # 遍历**引用列表**而不是 `self.query(...)`——工具行不是本组件的子节点，
        # 它们与本组件平级地挂在历史区里（见类 docstring）。
        for widget in self._widgets:
            widget.display = visible
            widget.set_detail_level(level)
        self._repaint()

    def summary_text(self) -> str:
        """
        当前聚合语的**纯文本**（未转义、不含档位提示），供埋点与测试使用。

        单独抽出来是为了让 trace 负载与界面显示同源——各拼一遍的话，
        记录里的聚合语与用户看到的会悄悄不一致。

        ⚠ **实现就在 `_compose_head`，两边共用同一处**。这正是「同源」要防的事：
        渲染那边写「已完成的聚合语 · 进行时」、这边只返回进行时的话，
        记录里的聚合语与用户看到的会悄悄不一致——而两边单看都是对的。
        """
        return self._compose_head()

    def _compose_head(self) -> str:
        """
        聚合行首行的**纯文本**（未转义）。渲染与埋点共用这一处。

        两态：
        - **未封闭**：`已落定的聚合语 · 进行时`，例如
          `查找文件 1 次 · 读取 5 个文件 · 读取中…`。
          ⚠ 前半截不可省（真机反馈：「上面一行不会实时更新状态」），
          且**只统计已落定的调用**——正在跑的那一次由进行时表达，
          算进计数会让数字比实际完成量多一个。
        - **已封闭**：完整聚合语。
        """
        if self._batch_closed:
            return compose_batch_summary(self._entries)
        done = [(name, ok) for name, ok in self._entries if ok is not None]
        summary = compose_batch_summary(done)
        head = self._running_text or DEFAULT_BATCH_VERB
        return f"{summary}{SEGMENT_SEP}{head}" if summary else head

    def _has_failure(self) -> bool:
        """本批次里是否有已落定的失败调用（F6）。"""
        return any(ok is False for _name, ok in self._entries)

    def _repaint(self) -> None:
        """
        画聚合行。两态分支见类 docstring。

        ⚠ 所有纯文本都过 `escape`：主参数值与聚合语都可能含字面 `[`。

        本组件**自己就是那行聚合语**（继承 `Static`），因此这里直接 `update`
        自身，不存在「子组件挂没挂上」的问题——那正是不做容器换来的简化。
        """
        color = ToolCallWidget._COLOR_FAIL if self._has_failure() else ToolCallWidget._COLOR_OK

        if not self._batch_closed:
            # 未封闭：**已完成的聚合语 + 进行时**，下面一行是当前这次调用的参数。
            # **不显示耗时**——那由状态行统一承担（F5），两处各显示一份会让用户
            # 去比对两个不相等的数字。
            #
            # ⚠ 首行文案走 `_compose_head`（与埋点共用一处），别在这里另拼一遍。
            lines = [f"[{ToolCallWidget._COLOR_RUNNING}]● {escape(self._compose_head())}[/]"]
            if self._running_arg:
                lines.append(
                    f"[{SECONDARY_COLOR}]{BRANCH_PREFIX}{escape(self._running_arg)}[/]"
                )
            self.set_markup("\n".join(lines))
            return

        # 已封闭：只写聚合语。
        # ⚠ **不再附档位提示**（tui-activity-fold 验收期修订）：那句话说的是一个
        # 全局快捷键，而一屏上可能有好几个批次——重复到第二次就没有信息量了。
        # 发现性改由输入框占位符承担，见 `NEXT_LEVEL_HINT` 的注释。
        lines = [f"[{color}]● {escape(self._compose_head())}[/]"]
        # F4：只有一次调用时，聚合行下方保留一条从属行放主参数值——
        # 单次时那个信息放得下，不给是纯损失；多次时十个文件名塞不进一行。
        if len(self._entries) == 1 and self._single_arg:
            lines.append(
                f"[{SECONDARY_COLOR}]{BRANCH_PREFIX}{escape(self._single_arg)}[/]"
            )
        self.set_markup("\n".join(lines))


# `_mount_widget` 的返回类型占位：挂什么组件就原样返回什么组件（见其 docstring）
_WidgetT = TypeVar("_WidgetT", bound=Static)


class UserMessageWidget(Static):
    """
    用户消息行：**整行铺满灰底**（对齐 Claude Code 的呈现方式）。

    ## 为什么单独开一个类而不是在 markup 里写 `[on grey23]`

    markup 的背景色只覆盖**字符所在的格子**——一句 5 个字的消息就只有 5 格变灰，
    右边一大片仍是底色，看起来像「选中了几个字」而不是「这是一条用户消息」。
    要让灰底铺满整行（包括换行后的每一行），背景必须落在 **widget 的区域**上，
    也就是必须走 CSS。故把它抽成独立组件，用 `DEFAULT_CSS` 把样式与组件放在一起
    （而不是塞进 `app.py` 的全局 CSS——那份是布局，这份是某个组件自己的外观）。

    ## 样式取值的理由

    - `width: 1fr`：占满父容器宽度，短消息也铺满整行。
    - `background: $panel`：主题里比正文底色略亮的一档中性灰，跟随主题走
      （硬编码 `#2A2A2A` 之类在浅色主题下会变成一块黑疤）。
    - `padding: 0 1`：文字与灰块边缘留一格，不然字贴着色块边显得脏。
    - `margin: 1 0`：上下各空一行，把这条消息与前后的 AI 回复/工具行分开——
      灰块紧贴着别的内容时，视觉上会被读成「这一段属于上一条」。

    ⚠️ 与本文件其它组件同理：传进来的文本必须先经本模块的 `escape`，
    绝不能换成 `rich.markup.escape`（理由见文件头那段注释）。
    """

    DEFAULT_CSS = """
    UserMessageWidget {
        width: 1fr;
        background: $panel;
        padding: 0 1;
        margin: 1 0;
    }
    """


class HistoryView(ScrollableContainer):
    """
    对话历史展示区。

    内部维护一个 Vertical 容器（id="history-messages"），
    每条消息（用户、AI、系统提示、错误）均以独立的 Static 组件挂载其中。

    流式渲染时，begin_assistant_turn() / begin_thinking_turn() 返回新建的
    Static 组件引用，TUI 层的 Worker 在收到每个 StreamChunk 后调用
    update_widget() 原地更新该组件内容，实现逐字显示效果。
    """

    # `{工具名: 主参数键名}`（F12）。由 `app.on_mount` 从工具注册中心建一次并调
    # `set_primary_args` 灌进来——工具集在启动之后不再变化，故只建一份。
    #
    # 缺省空字典是**零回归的关键**：拿不到注册中心时（非 DeepSeek Provider）
    # 它一直是空的，`resolve_call_title` 全部走键值对摘要兜底，工具行的形态
    # 与改造前逐字一致。
    _primary_args: dict = {}

    # 全局详细度档位的本地副本（`Ctrl+O`，三档循环）。
    #
    # ⚠ **必须记在这里，而不是只广播给「当前挂着的行」**：切档之后新产生的
    # 每一行都要按当前档位画。只广播不记的话，用户按下 Ctrl+O 之后接着跑的工具
    # 又是折叠的——现象是「这个开关时灵时不灵」，而那比没有开关更让人困惑。
    _detail_level: int = DETAIL_FOLDED

    # 归并分组表（tui-activity-fold F2），由 `set_fold_groups` 灌进来。
    # 缺省空字典同样是**零回归的关键**：拿不到它时一个工具都不可归并，
    # 每次调用照旧独立成行，形态与改造前逐字一致。
    _fold_groups: dict = {}

    # 当前尚未封闭的批次。None 表示「此刻没有正在收集的批次」。
    # ⚠ 类属性写 None（不可变）是安全的：赋值 `self._current_batch = x` 会创建
    # 实例属性，不会串到别的实例上。写成可变对象才会有那个坑。
    _current_batch: "Optional[ToolBatchWidget]" = None

    def set_detail_level(self, level: int) -> None:
        """
        接收全局详细度档位，**记下来并广播给已挂载的批次与独立工具行**。

        :param level: `DETAIL_FOLDED` / `DETAIL_ITEMS` / `DETAIL_FULL` 之一

        副作用：改自身状态；重绘全部批次与已定色的工具行。
        """
        self._detail_level = level
        # 先发给批次——它会把档位转发给自己的子行，并按档位控制它们的可见性。
        for batch in self.query(ToolBatchWidget):
            batch.set_detail_level(level)
        # 再发给**不在任何批次里**的独立工具行（写文件 / 执行命令 / 委派 / Skill）。
        # 批次内的子行刚才已由批次转发过，这里跳过，免得重复重绘。
        for widget in self.query(ToolCallWidget):
            if widget.batch is None:
                widget.set_detail_level(level)

    def set_expanded(self, expanded: bool) -> None:
        """**薄封装**，保留供既有调用方使用（它们只认「展开 / 收起」两态）。"""
        self.set_detail_level(DETAIL_FULL if expanded else DETAIL_FOLDED)

    def set_fold_groups(self, mapping: dict) -> None:
        """
        接收「哪些工具参与批次归并」的映射（tui-activity-fold F2）。

        :param mapping: 由工具注册中心导出的一次性快照
                        （见 `tools.display.fold_group_map`）

        副作用：只影响**此后**新建的工具行。
        ⚠ 与 `set_primary_args` 同理，**必须在历史回放之前**调用——`--continue`
        恢复出来的历史里有工具行，晚一步的话首屏那批会用空表画成独立行，
        与其后新产生的形态不一致（界面上表现为「上下两截风格不同」）。
        """
        self._fold_groups = dict(mapping or {})

    # 批次封闭时的回调（`(聚合语, 调用数) -> None`），由 `app.on_mount` 注入。
    # 缺省是个空实现——历史区不认识行为记录器，装配不到时它照常工作。
    _on_batch_closed = None

    def set_batch_closed_hook(self, callback) -> None:
        """
        登记「批次封闭」的回调，供上层产出行为记录（tui-activity-fold N7）。

        ⚠ 走回调而不是让本组件直接持有记录器：历史区是纯展示层，
        认识 `trace` 会让依赖方向倒过来。
        """
        self._on_batch_closed = callback

    def _close_batch(self) -> None:
        """
        封闭当前批次（若有）。**幂等**——连续挂几条系统行是常态。

        调用点只有一个：`_mount_widget` 挂载非工具行内容时。
        """
        batch = self._current_batch
        if batch is None:
            return
        batch.close()
        self._current_batch = None
        if self._on_batch_closed is not None:
            # 埋点整段兜异常：观测设施绝不能反过来打断被观测的界面
            try:
                self._on_batch_closed(batch.summary_text(), batch.call_count)
            except Exception:  # noqa: BLE001
                pass

    def set_primary_args(self, mapping: dict) -> None:
        """
        接收「工具名 → 主参数键名」映射（F12）。

        :param mapping: 由工具注册中心导出的一次性快照

        副作用：只影响**此后**新建的工具行；已经画好的行不重绘
        （启动时机决定了它总是在第一条消息之前被调用，实际不会出现半新半旧）。
        """
        self._primary_args = dict(mapping or {})

    def compose(self) -> ComposeResult:
        # 内层 Vertical 作为消息列表容器，便于统一清空（remove_children）
        yield Vertical(id="history-messages")
        # 待办清单块（todo-list 扩展 F10）。它是本容器的**第二个子节点**且
        # `dock: bottom`，因此固定在历史区底部、不随消息内容滚动，
        # 同时占掉可滚动区的相应高度（实测数据见该扩展 plan 的「T1 实测结论」）。
        #
        # ⚠ 放在 `HistoryView` **之内**是刻意的：历史区自带的那圈框线正好把它
        # 一起围住，框线天生是连续的一整圈。放到外面就要自己重画左右与下边框
        # （`#panel-dock` 就是那么做的），多一处得同步的东西。
        yield TodoPane()

    def on_mount(self) -> None:
        """
        打开 Textual 的**滚动锚点**（anchor），让历史区默认「粘」在底部。

        ## 为什么必须用它，而不是自己调 scroll_end

        `mount()` 只是把组件放进 DOM，**它的高度要等下一次布局才算得出来**。
        而 `scroll_end()` 取的是**当前**的 `max_scroll_y`——也就是加新组件之前的值，
        于是每次都停在「差最后一条消息」的位置，消息占几行就差几行。

        实测（80×24 终端、历史已铺满时）：

            append_user 之前   scroll_y=38  max=38
            append_user 之后   scroll_y=38  max=41   ← 新消息整条在视口外

        用户看到的症状正是「发完消息还得自己拨滚轮才看得到最新内容」。
        试过「多等一帧再滚」，但那是在赌布局刚好在第几帧落定，不可靠。

        anchor 是 Textual 为这件事内置的机制：被 anchor 的可滚动组件，
        **由合成器（compositor）在每次排布时把它按到底部**——与新组件的高度
        在同一次布局里算出，因此不存在「用了过期的 max_scroll_y」这回事。

        用户手动往上翻时 Textual 会自动松开锚点（不会把正在看历史的人硬拽回底部），
        松开后由 `_scroll_to_latest()` 在有新消息时重新按住。
        """
        self.anchor()

    def _scroll_to_latest(self) -> None:
        """
        有新内容时把视口带回底部。

        与 `on_mount` 里的 anchor 是**一对**：anchor 负责「粘住」，这里负责
        「用户翻上去之后，新消息把他带回来」——Textual 在用户手动滚动时会把锚点
        标记为已松开（`_anchor_released`），而 `scroll_end` 会重新按住它。

        `immediate=True` 只是省掉 Textual 内部那次 `call_after_refresh`：
        这一下滚到的位置可能仍是旧的 `max_scroll_y`（原因见 `on_mount`），
        真正滚到底由锚点在紧接着的那次布局里完成。
        """
        self.scroll_end(animate=False, immediate=True)

    def _mount_widget(self, widget: _WidgetT) -> _WidgetT:
        """
        把一个已构造好的组件挂到历史区末尾并滚到底。

        用 TypeVar 而不是写死 `-> Static`：`add_tool_widget` 对外承诺返回
        `ToolCallWidget`（调用方要拿它调 `begin_running` / `finish`），
        写死父类会让那个承诺在类型上退化成「某个 Static」。

        ## ⚠ 本方法同时是**批次封闭的唯一判定点**（tui-activity-fold F1）

        挂载**任何非工具行内容**（AI 正文、思考块、系统行、通知、用户消息）
        之前先封闭当前批次。规则一句话：
        **「历史区里出现了别的东西」就是「这批工具调用结束了」。**

        为什么判定放在这里而不是去监听 `TEXT` 事件：这里是历史区一切内容的
        **必经之路**，规则因此简单到不可能漏。监听事件的写法要在 app 层枚举
        所有断开时机（正文 / 思考 / 各类系统行 / 通知 / 恢复回放…），
        漏一处就会出现「一个批次跨越了中间那段正文」——而那在界面上表现为
        时序错乱：聚合行说的事情，一部分发生在它上面那段话之前，一部分之后。

        ⚠ **确认面板不在此列**，因此不会断开批次（AC2）：四个交互面板都是
        `compose` 里的独立组件，弹出时不往历史区挂任何东西。这不是特意写的
        判断，是既有布局结构的自然结果。

        :param widget: 任意 Static 子类实例（普通消息行 / 用户消息行 / 工具行 / 批次）
        :returns: 原样返回该组件（流式场景下供后续 update_widget 使用）
        """
        # 工具行与批次容器本身不封闭批次——前者要进批次，后者就是批次。
        if not isinstance(widget, (ToolCallWidget, ToolBatchWidget)):
            self._close_batch()
        container = self.query_one("#history-messages", Vertical)
        container.mount(widget)
        # 每次新增消息后自动滚动到底部，保持用户视角始终看到最新内容
        self._scroll_to_latest()
        return widget

    def _add_widget(self, markup: str) -> Static:
        """
        在历史区末尾添加一个新的 Static 消息组件并自动滚动到底部。

        :param markup: Rich markup 格式的显示内容
        :returns: 新建的组件引用（流式场景下供后续 update_widget 使用）

        ⚠ 用 `SelectableStatic` 而不是裸 `Static`：这些行**后续可能被换成
        Rich 渲染对象**（AI 正文就是），而 Textual 对那一类**不画选区高亮、
        不给字符偏移、也复制不走**。`SelectableStatic.set_rich` 会在渲染那一刻
        把它转成 `Content`，三条一起解决——见该类与 `content_from_rich` 的说明。
        """
        return self._mount_widget(
            SelectableStatic(markup, markup=True, plain=RichText.from_markup(markup).plain)
        )

    @staticmethod
    def _build_user_widget(text: str) -> "UserMessageWidget":
        """
        构造一条用户消息行（灰底整行 + 青色粗体 "◈" 前缀）。

        ⚠️ **成对维护点**：实时回显（`append_user`）与会话回放（`render_history`）
        必须共用这一处构造。各拼一次的话，`/resume` 回放出来的用户消息会与刚发的那条
        长得不一样（改了样式只改一处不报错，只是历史区里两种样式混着出现）。
        """
        return UserMessageWidget(
            f"[bold #99FFFF]◈[/bold #99FFFF] {escape(text)}", markup=True
        )

    def append_user(self, text: str) -> None:
        """追加一条用户消息：整行灰底，以青色粗体 "◈" 为前缀。"""
        self._mount_widget(self._build_user_widget(text))

    def begin_assistant_turn(self) -> Static:
        """
        在历史区新增一个空的 AI 回复占位组件，返回其引用。

        调用方（Worker）后续通过 update_widget() 将流式文本不断写入此组件，
        实现逐字出现的视觉效果。

        :returns: 新建的 Static 组件，内容初始为带前缀的空字符串
        """
        return self._add_widget("[bold #CCFF99]Rhine[/bold #CCFF99] ")

    def begin_thinking_turn(self) -> Static:
        """
        在历史区新增一个 Extended Thinking 占位组件，返回其引用。

        思考内容以灰色斜体显示，与正文回复视觉区分。
        流式思考块到来时，Worker 通过 update_widget() 逐步追加内容。

        :returns: 新建的 Static 组件，内容初始为带思考图标的空字符串
        """
        return self._add_widget(f"[dim italic]{THINKING_MARK} [/dim italic]")

    def update_widget(self, widget: Static, markup: str) -> None:
        """
        原地更新指定 Static 组件的显示内容并滚动到底部。

        此方法在 Worker 线程中通过 call_from_thread 调用，必须是线程安全的。
        Textual 的 call_from_thread 保证此方法在主线程的事件循环中执行。

        :param widget: 要更新的 Static 组件（begin_assistant_turn 等方法的返回值）
        :param markup: 新的 Rich markup 内容（完整替换，非追加）

        ⚠ **必须走 `set_markup`**，它会把内容与纯文本缓存一起换掉。
        改造前这里只调 `update()`、缓存不动，于是这一行**复制到的是建行时
        那一瞬的内容**。思考块正是这条路径：建行时只有一个 `✻ ` 前缀，
        内容全靠这里流式灌进去——不同步的话用户拖选整段思考，
        复制出来只有那个孤零零的前缀。实测扫出来的就是 `'✻ '`。
        """
        if hasattr(widget, "set_markup"):
            # `set_markup` 顺带清掉 Rich 内容与纯文本缓存——不清的话，
            # 一行从 Rich 内容切回 markup 时会**继续画上一次的内容**（不报错）。
            widget.set_markup(markup)
        else:  # 兼容仍传裸 Static 的调用方
            widget.update(markup)
        self._scroll_to_latest()

    def update_ai_widget(self, widget: Static, content: str) -> None:
        """
        原地更新 AI 回复组件，将 content 作为 Markdown 渲染并滚动到底部。

        与 update_widget() 不同，此方法使用 Rich Markdown 渲染器（经
        `set_rich` 在渲染期转成 `Content`，保住选区功能），
        支持代码块语法高亮、标题、粗体、斜体、列表、表格等 Markdown 格式。
        "Rhine" 前缀以青绿色粗体单独渲染，正文内容整体作为 Markdown 文档渲染，
        两者通过 RichGroup 纵向组合后传给 Static.update()。

        此方法在 Worker 线程中通过 call_from_thread 调用，是线程安全的。

        :param widget: begin_assistant_turn() 返回的占位 Static 组件
        :param content: AI 回复的完整累积文本（原始 Markdown 格式，非 markup）
        """
        label = RichText("Rhine ", style="bold #CCFF99")
        body = RichMarkdown(content)
        # RichGroup 将前缀标签和 Markdown 正文纵向组合为单个 renderable。
        # ⚠ 走 `set_rich` 而不是 `update`：后者会让这段正文变成 `RichVisual`，
        # 而**选区功能对它三处一起失效**——不画高亮、没有字符偏移、
        # 复制拿不到内容（详见 `content_from_rich`）。`set_rich` 会在渲染那一刻
        # 按实时宽度把它转成 `Content`，视觉一模一样，选区则全部可用。
        if hasattr(widget, "set_rich"):
            widget.set_rich(RichGroup(label, body))
        else:  # 兼容极少数仍传裸 Static 的调用方（回放路径的老测试）
            widget.update(RichGroup(label, body))
        self._scroll_to_latest()

    def add_tool_widget(self, tool_call, pending: bool = False) -> "ToolCallWidget":
        """
        在历史区末尾挂载一个工具调用展示行（ToolCallWidget），返回其引用。

        Worker 线程在收到 tool_pending 时先以 pending=True 建行（此时只有工具名），
        收到 tool_start 时对返回的引用调用 begin_running() 补参数并转执行态，
        收到 tool_result 时再调用 finish() 定色。两阶段共用同一行，不新建第二行。

        ## 两条分流（tui-activity-fold F2）

        - **可归并**（只读检索类）：进当前批次；没有正在收集的批次就先新建一个。
        - **不可归并**（写文件 / 执行命令 / 委派 / 加载 Skill / 未登记的一切）：
          先封闭当前批次，再按改造前的方式独立挂进历史区。

        ⚠ **返回值口径一字不变**：无论走哪条分流，返回的都是 `ToolCallWidget`，
        Worker 侧仍持它调 `begin_running` / `finish`、仍按原口径从 `tool_widgets`
        表里 `pop`。批次**只改变这个组件挂在哪**，不参与那张表的登记与摘除
        ——「建行/定色必须成对」那条既有不变量因此原样成立。

        :param tool_call: provider.base.ToolCall，用于初始化展示内容
        :param pending: True 表示模型仍在生成该调用的参数（见 ToolCallWidget）
        :returns: 新建的 ToolCallWidget，供后续 begin_running() / finish() 更新
        """
        widget = ToolCallWidget(
            tool_call, pending=pending, primary_args=self._primary_args
        )
        name = str(getattr(tool_call, "name", "") or "")
        if name in self._fold_groups:
            batch = self._current_batch
            if batch is None or batch.closed:
                batch = ToolBatchWidget(self._detail_level)
                self._current_batch = batch
                self._mount_widget(batch)
            # ⚠ 工具行**照常挂在历史区**，与批次平级——批次不是容器，
            # 它只持引用来控制可见性（理由见 `ToolBatchWidget` 的 docstring：
            # 嵌套挂载在时序上踩过一连串不报错的坑）。
            self._mount_widget(widget)
            # 档位与可见性由 `attach` 一并设定，故这里不单独调 set_detail_level。
            batch.attach(widget, name)
            return widget

        # 不可归并：独立成行，形态与改造前逐字一致。
        # ⚠ 显式封闭——`_mount_widget` 里那条判断放行工具行（可归并的要进批次），
        # 所以走到这里必须自己把批次收掉。
        self._close_batch()
        # 新行也要跟上当前的档位（见 `set_detail_level` 里那条注释）。
        widget.set_detail_level(self._detail_level)
        return self._mount_widget(widget)

    def append_system(self, text: str) -> None:
        """
        追加一条**提示级**系统行：暗色、**无前缀**（tui-display 扩展 F19）。

        四级里最低的一档，用于「记忆已更新」「上下文已压缩」「Skill 已激活」
        这类**误读代价为零**的消息。

        ⚠ 改造前它带一个 `◆` 前缀，本轮去掉，两个理由：
        ① `◆` 不在 F29 收敛后的符号白名单里；
        ② F21 明确要求**提示级与事件级之间只差亮度**——留着前缀的话两者会
        差两样东西（亮度 + 有没有符号），而那个符号本身不表达任何用户能用上的
        信息（每条系统行都有它，等于没有）。
        """
        self._add_widget(f"[dim]{escape(text)}[/dim]")

    def append_event(self, text: str) -> None:
        """
        追加一条**事件级**系统行：正常亮度、无前缀（tui-display 扩展 F19）。

        用于「子 Agent 完成」「自动唤起」「会话已恢复」这类**真的发生了一件事**
        的消息。改造前它们与「记忆已更新」走同一条 `[dim]` 通道，于是一屏里
        最要紧的那条和最可忽略的那条长得一模一样。

        与提示级只差亮度是**刻意的**（F21）：误读这两者的代价为零——把一条
        「记忆已更新」当成事件，不会导致任何错误决策。真正会让人做错决定的是
        漏看警告与错误，而那两级由**文字前缀**承担，脱离颜色也认得出。
        """
        self._add_widget(escape(text))

    def append_report(self, text: str) -> None:
        """
        追加一段**分级渲染**的命令报告（tui-display 扩展 F15/F16/F17）。

        改造前 `/agents` `/skills` 这类多行报告整段走 `append_system` 的 `[dim]`
        通道——段落标题、条目、次级信息在视觉上完全等价，读起来是一堵均匀的
        暗色墙。这里按 `classify_report` 判出的层级分别施加亮度与强调。

        四级的样式取值理由：

        - **首行**：加粗 + 强调色。它承担「这是一次命令的结果，不是 AI 说的话」
          这个判断（F17）。⚠ **刻意不发前缀符号**——按 F29 收敛后的词汇表，
          `●` 专属于工具行与活动行，为报告再造一个图形会让符号表重新变杂；
        - **段落标题**：正常亮度 + 加粗；
        - **条目**：正常亮度；
        - **次级信息**：暗色（与工具行的分支、活动区的子行同一个
          `SECONDARY_COLOR`，三处单一来源）。

        :param text: 报告全文（多行，纯文本）

        ⚠ 报告里嵌着路径、错误消息、任务标题与模型产出的结论——全是可能含
        字面 `[` 的自由文本，必须逐行经本模块的 `escape`。

        副作用：往历史区挂一个组件并滚到底。
        """
        rendered: list[str] = []
        for kind, line in classify_report(text):
            safe = escape(line)
            if kind is ReportLineKind.TITLE:
                rendered.append(f"[bold #7AEEFF]{safe}[/bold #7AEEFF]")
            elif kind is ReportLineKind.SECTION:
                rendered.append(f"[bold]{safe}[/bold]")
            elif kind is ReportLineKind.DETAIL:
                rendered.append(f"[{SECONDARY_COLOR}]{safe}[/{SECONDARY_COLOR}]")
            else:
                # ITEM 与 BLANK 都用正常亮度原样输出——条目行本身就是主干内容，
                # 加任何强调都会与段落标题打架。
                rendered.append(safe)
        self._add_widget("\n".join(rendered))

    def append_error(self, text: str) -> None:
        """
        追加一条**错误级**系统行：红色粗体 + 文字前缀「错误：」（F19/F21）。

        ⚠ 改造前它带一个 `●`，本轮去掉——按 F29 收敛后的词汇表，
        `●` 专属于工具行与活动行，让它同时表示「一条错误」会稀释掉那个语义。
        """
        self._add_widget(f"[bold red]{ERROR_PREFIX}{escape(text)}[/bold red]")

    def append_warning(self, text: str) -> None:
        """
        追加一条**警告级**系统行：橙色粗体 + 文字前缀「警告：」（F19/F21）。

        ## 为什么需要它

        最早的用户是**项目级 Hook 的启动提示**——那是本项目里唯一一段
        「可能来自别人的仓库、且会被直接执行」的内容，它的可读性就是那道防线的强度。
        用 `append_system` 渲染的话，这条警告会比普通提示**更不显眼**（dim），
        方向正好反了（人眼评审时发现）。

        ## 为什么前缀是**文字**而不是一个图形（F21）

        这一条曾设计成「每级各发一个前缀符号（`·` / `◆` / `▲` / `×`）」，
        **已推翻**。Claude Code 不给严重级别发图形，它靠颜色 + 文字本身
        （`Error:`）；自创四个图形是「符号越加越杂」的来源，而符号一多，
        每个的语义就都记不住了。

        文字前缀还兑现了一件图形做不到的事：**脱离颜色也能辨认**。
        截图、配色异常的终端、端到端驱动抓到的纯文本里，颜色都可能丢失，
        而「警告：」三个字不会。

        ⚠ 与本类其它方法同理，文本必须经 `escape` —— 那是 `tui/widgets.py` 自己的
        版本，绝不能换成 `rich.markup.escape`（落单的 `[` 会被它放过并在布局阶段崩）。
        """
        self._add_widget(f"[bold #FFA500]{WARNING_PREFIX}{escape(text)}[/bold #FFA500]")

    def clear_all(self) -> None:
        """
        清空所有历史消息组件（对应 /clear 命令的 UI 侧操作）。

        ⚠ 必须一并把 `_current_batch` 置空：`remove_children` 已经把批次容器
        从 DOM 里删掉了，但这里还攥着一个指向已删除组件的引用——下一次
        可归并的调用会往那个「幽灵批次」里 `mount`，界面上什么都不出现。

        ⚠ **它只清 `#history-messages` 的子节点，天生碰不到待办块**
        （待办块是本容器的另一个子节点）。**别改成 `self.remove_children()`**
        ——那会把待办块从 DOM 里一起删掉，而它是 `compose` 产出的、
        不会被重建，于是 `/clear` 之后待办功能**永久失效**且没有任何报错。
        待办的清空走协调层（`_clear_todo`）与 `_reset_display_state`，
        见 todo-list 扩展 F17。
        """
        self.query_one("#history-messages", Vertical).remove_children()
        self._current_batch = None

    def todo_pane(self) -> "TodoPane":
        """
        取待办块（todo-list 扩展）。

        :returns: 本历史区内的 `TodoPane`

        收敛成一个方法而不是让 `app.py` 到处 `query_one`——待办块的位置
        是本类的内部结构，将来若挪位置（比如 plan 里登记的方案 B），
        只有这一处要改。
        """
        return self.query_one(TodoPane)

    # ------------------------------------------------------------------ #
    # 会话历史回放（c9 /resume 交互化）
    # ------------------------------------------------------------------ #

    @staticmethod
    def _build_assistant_widget(content: str) -> Static:
        """
        构造一条「完整 AI 回复」的静态组件（回放场景，非流式）。

        渲染形态与流式的 update_ai_widget 一致：青绿色 "Rhine" 前缀 + Markdown 正文，
        区别只是内容一次到位、无需占位-更新两步。
        """
        label = RichText("Rhine ", style="bold #CCFF99")
        widget = SelectableStatic("")
        # ⚠ 走 `set_rich` 而不是把 RichGroup 直接塞进构造函数：后者会让这一行
        # 变成 `RichVisual`，回放出来的历史**选不中也复制不走**（详见
        # `content_from_rich`）。与实时路径 `update_ai_widget` 同一个理由。
        widget.set_rich(RichGroup(label, RichMarkdown(content)))
        return widget

    @staticmethod
    def _build_tool_record_widget(
        tool_call, result_summary: str, primary_args: "Optional[dict]" = None
    ) -> Static:
        """
        构造一条「历史工具调用记录」的简化静态行（回放场景）。

        两行式：绿色 "● 标签(主参数)" + 灰色 "⎿ 结果首行摘要"。
        刻意**不复用 ToolCallWidget**：它的 on_mount 会启动每秒计时器并重绘「执行中」
        状态——回放时 finish() 与挂载的时序无保证，终态会被 on_mount 覆盖且定时器
        永不停止（泄漏）。历史记录也没有耗时数据，简化行语义更贴切。

        ⚠ **标题必须与实时工具行同口径**（都走 `resolve_call_title`）。
        各拼一次的话，`/resume` 回放出来的工具行会与刚跑过的那条长得不一样——
        与 `_build_user_widget` 那个成对维护点是同一类问题。

        :param tool_call: provider.base.ToolCall（提供工具名与参数）
        :param result_summary: 已截断的结果摘要（build_replay_items 产出）
        :param primary_args: `{工具名: 主参数键名}`（F12）
        """
        label, inner = resolve_call_title(tool_call, primary_args)
        # 标题行走 markup（resolve_call_title 的两个产出都已 escape，安全）；
        # 分支行用 RichText 纯文本拼接——结果摘要来自工具输出原文，可能含 "["，
        # 纯文本渲染天然免转义（与 ToolCallWidget.finish 的 branch 同一做法）。
        header = f"[{ToolCallWidget._COLOR_OK}]● {label}({inner})[/]"
        branch = RichText(f"{BRANCH_PREFIX}{result_summary}", style=ToolCallWidget._COLOR_BRANCH)
        widget = SelectableStatic("")
        widget.set_rich(RichGroup(RichText.from_markup(header), branch))
        return widget

    def render_history(self, messages) -> None:
        """
        清空聊天区并整体回放一段历史消息（/resume 载入、--continue 启动恢复）。

        必须在主线程调用（Worker 侧经 call_from_thread 转入），清空与重画在同一次
        调用内完成，对用户呈现为原子切换。性能考虑：先把全部消息构造成组件列表，
        一次 mount(*widgets) 批量挂载、末尾只滚动一次——逐条挂载+滚动会触发 N 次布局，
        长会话下明显卡顿。

        :param messages: 恢复出来的历史消息列表（provider.base.Message）

        副作用：移除聊天区现有全部组件并挂载回放组件。
        """
        container = self.query_one("#history-messages", Vertical)
        container.remove_children()
        widgets: list[Static] = []
        for item in build_replay_items(messages):
            kind = item[0]
            if kind == "user":
                # 与实时回显共用同一处构造，样式不会分叉（见 _build_user_widget）
                widgets.append(self._build_user_widget(item[1]))
            elif kind == "assistant":
                widgets.append(self._build_assistant_widget(item[1]))
            elif kind == "tool":
                widgets.append(
                    self._build_tool_record_widget(item[1], item[2], self._primary_args)
                )
        if widgets:
            container.mount(*widgets)
        self._scroll_to_latest()


def format_duration(seconds: float) -> str:
    """
    把秒数渲染成人读的时长：`43s` / `1m 12s`。

    不足一分钟只写秒——`0m 43s` 里那个零毫无信息量。

    副作用：无（纯函数）。
    """
    total = max(0, int(seconds))
    if total < 60:
        return f"{total}s"
    return f"{total // 60}m {total % 60}s"


def format_tokens(tokens: int) -> str:
    """
    把 token 数渲染成人读的量级：`820 tokens` / `28.5k tokens`。

    上千就换成 `k`：活动行的横向空间很紧，而这个数字要的是**量级**
    （「烧得快不快」），不是精确值。

    副作用：无（纯函数）。
    """
    value = max(0, int(tokens))
    if value < 1000:
        return f"{value} tokens"
    return f"{value / 1000:.1f}k tokens"


def format_activity_cost(row) -> str:
    """
    渲染一条活动行的三个数字（tui-display 扩展 F2/F3/F6）。

    :param row: `subagents.tasks.ActivityRow`
    :returns: 形如 `23s · ↑3.1k tokens · 8 次调用`（运行中）
              或 `14 次调用 · 28.5k tokens · 1m 12s`（终态）

    ## 为什么两种顺序不同

    运行中把**耗时排在最前**——它是唯一每秒都在跳的数字，用户扫一眼就是想确认
    「它还活着」。终态把**调用次数排在最前**——那时耗时已经不重要了，用户要的
    是「这次委派花了多少」。

    ## 为什么这是一个共用的纯函数（F6）

    终态的这串数字要在**两个地方**出现：活动区那条即将淡出的终态行，
    与历史区那条永久留痕。spec 明确要求两处**同源同口径**——各拼一次的话，
    一次改动只改一处不报错，用户会看到同一个任务的成本在两个地方对不上，
    而那种不一致最难解释。

    副作用：无（纯函数）。
    """
    duration = format_duration(row.seconds)
    tokens = format_tokens(row.tokens)
    calls = f"{row.tool_calls} 次调用"
    if row.status is TaskStatus.RUNNING:
        return f"{duration} · ↑{tokens} · {calls}"
    return f"{calls} · {tokens} · {duration}"


class ActivityView(Vertical):
    """
    子 Agent 活动区（tui-display 扩展 A 组，F1–F6）。

    位于历史区**下方**、各交互面板与输入框**上方**的一块独立区域。
    「有正在运行的任务」或「有尚未淡出的终态行」时出现，两者都没有时
    **整块隐藏且不占布局空间**。

    ## 它解决的问题

    改造前，子 Agent 派出去之后是几分钟的静默——唯一的活体信号是状态栏角落
    那个 `子Agent:2` 计数。用户无从判断它是在干活还是卡住了。

    ## 为什么数据靠轮询而不是推送（F4 / N1）

    本项目**已经因为「在加锁临界区里做跨线程调度」死锁过四次**。子 Agent 跑在
    独立线程上，任何「跑完一步就通知界面」的设计都要跨线程，而跨线程调度一旦
    与持锁相遇就是确定性死锁（Textual 的 `call_from_thread` 是阻塞式的）。
    轮询从结构上消掉这一整类问题：**主线程**每 0.5 秒去任务表读一份不可变快照，
    没有任何一条边是从子 Agent 线程指向界面的。

    复用既有的子 Agent 轮询节拍、**不新增定时器**，因此空闲会话的开销与改造前
    完全一致。

    ## 它只观测，不操作（F10）

    区内没有取消或任何改变任务状态的入口。取消仍走 `/agents cancel`，
    全量信息仍看 `/agents`——**活动区给概览，`/agents` 给全量**。

    ## 展开（F5）

    默认折叠，每个任务一行。`Ctrl+O` 切换**整个区域**的展开态，展开时每条任务
    下方列出它内部最近若干次工具调用。⚠ 快捷键**不引入焦点切换**——活动区
    任何时候都不抢焦点，输入框与四个面板的键位体系一字不动。
    """

    # 缺省隐藏，避免依赖外部 App CSS 才能初始隐藏（与四个面板同一做法）。
    DEFAULT_CSS = "ActivityView { display: none; }"

    # 状态 → 颜色。运行中橘色（与工具行的「执行中」同色系，语义都是「还在跑」），
    # 完成绿、失败与取消红。
    _STATUS_COLORS = {
        TaskStatus.RUNNING: "#FFA500",
        TaskStatus.COMPLETED: "#5FD75F",
        TaskStatus.FAILED: "#FF5F5F",
        TaskStatus.CANCELLED: "#FF5F5F",
    }

    def update_rows(self, rows, expanded: bool = False) -> None:
        """
        整块重绘活动区。

        :param rows: `ActivityRow` 元组（`conversation.subagent_activity()` 的产出）
        :param expanded: 展开态则在每行下方列出最近的工具调用

        **整块重绘而不是增量 diff**：行数以并发上限（5）为界，重绘一次比算差异
        便宜，而且不会错——增量更新要维护「哪一行对应哪个任务」的映射，
        那是一类典型的、出错后表现为「数字串行」的 bug。

        无行时 `display = False`，Textual 会连带收回它占的布局空间（F1）。

        ⚠ **一切文本必须经本模块的 `escape`**：队员名来自模型给的参数、
        角色名来自用户写的角色定义文件、工具文本里含路径与命令。落单的 `[`
        会在布局阶段抛 `MarkupError`，没有任何 try/except 兜得住，整个应用退出。

        副作用：移除并重建全部子组件；改自身可见性。
        """
        self.remove_children()
        if not rows:
            self.display = False
            return

        lines: list[str] = []
        for row in rows:
            color = self._STATUS_COLORS.get(row.status, SECONDARY_COLOR)
            lines.append(
                f"[{color}]● {escape(row.display_name)} "
                f"{STATUS_LABELS.get(row.status, row.status.value)} "
                f"({format_activity_cost(row)})[/]"
            )
            if expanded:
                for brief in row.recent_tools:
                    lines.append(
                        f"[{SECONDARY_COLOR}]{BRANCH_PREFIX}{escape(brief)}[/]"
                    )
        # 末行给出快捷键提示——不写的话没人知道还能展开。
        hint = "Ctrl+O 收回" if expanded else "Ctrl+O 展开"
        lines.append(f"[{SECONDARY_COLOR}]  {hint}[/]")

        self.mount(Static("\n".join(lines), markup=True))
        self.display = True


class TodoPane(SelectableStatic):
    """
    主对话的待办清单块（todo-list 扩展 F10–F14）。

    挂在 `HistoryView` **内部**、`dock: bottom`——历史内容在它上面正常滚动，
    它**不跟着滚**。形如：

        ● 待办 (2/4)
          ● 读现有实现        已完成
          ● 改 login 接口     进行中
          ● 改三处调用方      待办
          ……还有 6 条，已完成 3 条

    ## 它是观测区，不是交互区

    区内没有任何操作入口。待办的增删改**只有一条写路径**（模型调 `todo_write`）
    ——用户改不了。理由与 C15 共享清单同一条：两条并行的写路径里，
    命令层那条绕开了「谁改的」这个记录；而在整表覆写语义下它还多一层麻烦，
    模型下一次覆写会把用户的改动整个抹掉。

    ## 三条实现约束

    1. **本类不做任何显示判断。** 「该不该显示」「留哪 5 条」「省略行怎么写」
       全在 `todo.render.build_view` 里算好了，这里只负责上色与转义。
       两处各判一次必然分叉。
    2. **一切文本经本模块的 `escape`**，绝不用 rich 那版——待办标题来自模型
       给的参数，落单的 `[` 会在布局阶段抛 `MarkupError`，
       **没有任何 try/except 兜得住，整个应用退出**。
    3. **继承 `SelectableStatic` 而不是 `Static`**：内容要能被拖选、能被复制。
       走 `set_markup` 的 Content 通路时选区功能原生成立，
       但 `plain_text()` 这个唯一权威取文本入口来自它。

    ## ⚠ 字段名预检过

    `_render` / `_closed` / `_running` 是 Textual `MessagePump` 的实例字段，
    撞上**一律不报错**、只表现为「界面上东西凭空少了」（本项目已撞三次）。
    本类只新增 `_view`，已在 `Static("x")` 实例上 `hasattr` 验过为干净。
    """

    # 缺省隐藏（与四个面板、活动区同一做法）：不依赖外部 App CSS 也能初始隐藏，
    # 于是不使用这个能力的用户界面表现与改造前逐字一致（spec N5）。
    DEFAULT_CSS = "TodoPane { display: none; }"

    # 状态 → 颜色。**取值与子 Agent 活动区同源**：进行中橘（与工具行「执行中」
    # 同色系，语义都是「还在跑」）、已完成绿、待办用次级灰。
    #
    # ⚠ 颜色只是**第二重**区分。第一重是中文文字标签——单色终端、截图、
    # 以及色觉障碍用户那里颜色全都会丢失，而文字不会（spec F14）。
    _STATE_COLORS = {
        TodoState.IN_PROGRESS: "#FFA500",
        TodoState.COMPLETED: "#5FD75F",
        TodoState.PENDING: SECONDARY_COLOR,
    }

    def __init__(self) -> None:
        super().__init__("", markup=True)
        # 当前画着的视图；`None` 表示这块没在显示。
        self._view = None

    def update_view(self, view) -> None:
        """
        整块重绘待办块。

        :param view: `todo.render.TodoView`；**`None` 表示整块不该显示**

        `None` 时 `display = False`，Textual 会连带收回它占的布局空间——
        于是历史区的可用高度回到没有待办时的样子（spec F11/N5）。

        **整块重绘而不是增量 diff**：行数以 5 为界，重绘一次比算差异便宜，
        而且不会错——增量更新要维护「哪一行对应哪一条」的映射，
        那是一类典型的、出错后表现为「状态串行」的 bug（与活动区同一判断）。

        副作用：改自身内容与可见性。
        """
        self._view = view
        if view is None:
            self.display = False
            self.set_markup("")
            return

        # 标题列按 `cell_len` 补齐，使右侧的状态标签对齐成一列。
        # 用 `cell_len` 而不是 `len`：中文占两格，按字符数补齐会参差不齐
        # （与确认面板四个选项的对齐同一做法）。
        width = max((cell_len(row.title) for row in view.rows), default=0)

        lines = [f"[{SECONDARY_COLOR}]●[/] {escape(view.header)}"]
        for row in view.rows:
            color = self._STATE_COLORS.get(row.state, SECONDARY_COLOR)
            padding = " " * max(0, width - cell_len(row.title) + 2)
            lines.append(
                f"  [{color}]●[/] {escape(row.title)}{padding}"
                f"[{color}]{escape(TODO_STATE_LABELS[row.state])}[/]"
            )
        if view.overflow:
            lines.append(f"  [{SECONDARY_COLOR}]{escape(view.overflow)}[/]")

        self.set_markup("\n".join(lines))
        self.display = True

    @property
    def view(self):
        """当前画着的视图（`None` = 未显示）。供测试与界面判断用。"""
        return self._view


class OverlayPanel:
    """
    浮层面板的共用行为：**改变可见性时告诉外面一声**。

    ## 为什么需要它

    四个交互面板改成浮层之后（`layer: panels` + `dock: bottom`），它们不再
    挤压历史区——这解决了抖动，但带来一个新问题：**它们会盖住历史区最后几行**，
    而那几行往往正是用户要看的（比如「我在批准哪一次写入」的那条工具行）。

    补偿办法是给历史区加一个等于面板高度的**底部内边距**，让内容上移。
    但那要求「面板一显示/隐藏就有人来同步」，而 Textual 的 `Show` / `Hide`
    事件**在 app 层收不到**（实测：`on_show` / `on_hide` 一次都不触发）。

    因此改由面板自己在改可见性时 `post_message`。

    ⚠ **四个面板必须都走 `set_visible`，不能再直接写 `self.display = ...`**。
    漏一处不报错，只是那个面板弹出时把历史区末尾几行盖住了——而用户看到的是
    「内容莫名其妙少了几行」，不会想到是面板压上去了。
    """

    class VisibilityChanged(TextualMessage):
        """面板显示或隐藏了。app 据此重算历史区要让出多少底部空间。"""

    def set_visible(self, visible: bool) -> None:
        """
        切换可见性并广播一次变化。

        :param visible: 是否显示

        副作用：改 `display`；向上发 `VisibilityChanged` 消息。
        """
        self.display = visible
        self.post_message(self.VisibilityChanged())


class CommandHighlighter(Highlighter):
    """
    输入框命令字段高亮器（c10 T36，spec F24）。

    通过 Textual `Input` 的公开 `highlighter` 扩展点安装（不覆写私有渲染实现）。
    规则：
    - 只检查输入开头到第一个空白之前的字段；
    - 仅当完整字段能被注册表解析（规范名或别名、大小写不敏感）时，为该字段
      施加命令强调样式；参数与后续空白保持普通样式；
    - 未完整命中的前缀（如 "/res"）不高亮，避免让用户误以为命令有效；
    - 正文内的斜杠（不在输入开头）不高亮；
    - 不改变用户输入的显示文本（大小写形式可命中，显示仍保留原文）。
    """

    # 命令字段的强调样式：与主题青色一致的加粗，与普通输入文字明显区分。
    COMMAND_STYLE = "bold #7AEEFF"

    def __init__(self, registry: CommandRegistry) -> None:
        self._registry = registry

    def highlight(self, text: RichText) -> None:
        """
        rich Highlighter 协议：原地为 text 施加样式（不改变字符内容）。

        :param text: 输入框当前内容的 rich Text 对象
        """
        plain = text.plain
        if not plain.startswith("/"):
            return
        # 命令字段 = 开头到第一个空白之前；无空白时整段都是命令字段
        end = next((i for i, ch in enumerate(plain) if ch.isspace()), len(plain))
        token = plain[:end]
        # 只有完整命中注册表（规范名或别名）才着色（spec F24）
        if self._registry.resolve(token) is not None:
            text.stylize(self.COMMAND_STYLE, 0, end)


class CommandPanel(OverlayPanel, OptionList):
    """
    斜杠命令提示面板（c10 起从注册表动态取候选，不再维护静态 COMMANDS 列表）。

    继承自 Textual OptionList，内置 Up/Down 键盘导航和 Enter 选中能力。
    默认 display:none 不占布局空间；当用户输入以 "/" 开头且光标位于命令字段时
    由 App 层调用 show_for() 使其出现，候选来自 CommandRegistry.complete()：
    - 只匹配规范名（大小写不敏感），别名不参与候选——别名仍可直接输入执行、
      /help 仍列出（2026-07 变更，见 docs/c10/checklist.md C36）；
    - 隐藏命令不出现（spec F20）；
    - 候选顺序稳定：按命令注册顺序排列。

    选中某条候选后，App 层监听 OptionList.OptionSelected 事件，
    将候选文本填入 InputBar 并自动提交（或经提交入口直接分发）。
    """

    # 默认隐藏自身，避免依赖外部 App CSS 才能初始隐藏
    DEFAULT_CSS = "CommandPanel { display: none; }"

    def __init__(self, registry: CommandRegistry, **kwargs) -> None:
        """
        :param registry: 命令注册表（与分发器、输入高亮器共享同一实例，spec F3）
        """
        super().__init__(**kwargs)
        self._registry = registry

    def show_for(self, prefix: str) -> None:
        """
        用注册表候选刷新 OptionList：有匹配则显示面板，零候选则隐藏。

        每次调用先清空现有选项再重建，避免残留上次的过滤结果；
        重建后把首个候选设为高亮，供「菜单可见时按 Enter 执行当前高亮项」
        （spec F23）。Option 的 id 即候选文本（规范名或别名）。

        :param prefix: 用户当前输入的命令字段前缀（如 "/"、"/co"、"/ctx"）
        """
        items = self._registry.complete(prefix)
        self.clear_options()
        if not items:
            self.set_visible(False)
            return
        for item in items:
            self.add_option(
                Option(f"{item.value}  [dim]{escape(item.description)}[/dim]", id=item.value)
            )
        self.set_visible(True)
        # 首个候选默认高亮：Enter 即执行（与确认面板的顺手体验一致）
        self.highlighted = 0

    def hide(self) -> None:
        """隐藏面板并收回布局空间。"""
        self.set_visible(False)


class InputBar(Input):
    """
    用户输入框。

    继承自 Textual Input，拦截内置的 Submitted 事件，在内容非空时
    发出自定义的 InputSubmitted 消息，并自动清空输入框，为下次输入做准备。

    c10 增强：
    - 构造时接收 CommandRegistry，经 Textual `Input` 公开的 `highlighter`
      参数安装 CommandHighlighter——完整命中的命令字段着色，参数保持普通样式；
    - 值以 "/" 开头且光标仍处于命令字段时拦截 Tab，post CommandCompletionRequested
      交给 App 做单候选补全/多候选菜单；已进入参数区或普通文本时不拦截，
      保留 Textual 的正常 Tab 行为（焦点切换）。

    使用自定义消息而非直接调用方法，是为了保持 Widget 间的松耦合：
    InputBar 不需要持有 App 或其他组件的引用。
    """

    class InputSubmitted(TextualMessage):
        """
        用户提交输入时发出的自定义消息。

        :param text: 经过 strip 处理的用户输入文本（保证非空）
        """
        def __init__(self, text: str) -> None:
            super().__init__()
            self.text = text

    class CommandCompletionRequested(TextualMessage):
        """
        用户在命令字段按 Tab 请求补全时发出（c10 T39）。

        :param prefix: 当前输入的命令字段前缀（含开头 "/"，如 "/co"）
        """
        def __init__(self, prefix: str) -> None:
            super().__init__()
            self.prefix = prefix

    def __init__(self, registry: "Optional[CommandRegistry]" = None, **kwargs) -> None:
        """
        :param registry: 命令注册表；非 None 时安装命令字段高亮器（公开扩展点，
                         不覆写 Textual 私有渲染方法）
        :param kwargs: 透传给 Textual Input（placeholder 等）
        """
        if registry is not None:
            kwargs.setdefault("highlighter", CommandHighlighter(registry))
        super().__init__(**kwargs)

    def _command_field_end(self) -> int:
        """返回命令字段的结束位置（开头到第一个空白之前；无空白即整段长度）。"""
        return next((i for i, ch in enumerate(self.value) if ch.isspace()), len(self.value))

    def on_key(self, event: Key) -> None:
        """
        拦截命令字段内的 Tab：post 补全请求并阻止默认焦点切换（c10 T39）。

        拦截条件：值以 "/" 开头，且光标位置不超过命令字段末尾（仍在命令字段内）。
        参数区（第一个空白之后）或普通文本的 Tab 不拦截，Textual 默认行为保留。
        """
        if event.key != "tab":
            return
        if not self.value.startswith("/"):
            return
        end = self._command_field_end()
        if self.cursor_position > end:
            return  # 已进入参数区：不做命令补全
        event.stop()
        event.prevent_default()
        self.post_message(self.CommandCompletionRequested(self.value[:end]))

    def apply_completion(self, value: str, trailing_space: bool = False) -> None:
        """
        用候选文本替换命令字段并把光标移到命令字段末尾（供 App 单候选补全调用）。

        只替换第一个空白之前的命令字段，参数区内容原样保留（spec：/resume 12
        按 Tab 不改动 12）。

        :param value: 替换后的完整命令（如 "/compact"）
        :param trailing_space: True 且原输入无参数时在末尾补一个空格
                               （候选命令有参数提示时，方便用户继续输入参数）
        """
        end = self._command_field_end()
        rest = self.value[end:]
        if rest:
            self.value = value + rest
            self.cursor_position = len(value)
        else:
            suffix = " " if trailing_space else ""
            self.value = value + suffix
            self.cursor_position = len(self.value)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        """
        拦截 Textual 内置的回车提交事件。

        过滤空输入（纯空格），防止触发无意义的 API 请求。
        提交后立即清空输入框，恢复待输入状态。

        副作用：向消息总线 post InputSubmitted 消息；清空 self.value。
        """
        text = self.value.strip()
        if text:
            self.post_message(self.InputSubmitted(text))
            self.value = ""


# 模式标记样式（c10 F29/F30；auto-plan 扩展把 [DEFAULT] 改成 [AUTO]）：
# - [AUTO] 用 dim（中性、低强调），深浅色主题下都可读；
# - [PLAN] 用加粗的醒目青色（与主题青一致但更饱和），两种主题下均与 AUTO 明显区分。
# N9 可访问性：模式不只靠颜色表达——文字本身就是 [AUTO]/[PLAN]，忽略样式也能区分。
#
# ⚠ **[AUTO] 刻意保持 dim，不用橘色。** 它是缺省状态，而橘色在本项目里专指
# 「需要用户留意」（上下文逼近上限、确认面板、原先的放行档）。把一个常年为真的
# 状态标成橘色，会稀释掉橘色在别处的分量——改造前那段常驻橘色的「权限模式：放行」
# 正是因为这个原因随本扩展一并删除。
_MODE_AUTO_MARKUP = "[dim]\\[AUTO][/dim]"
_MODE_PLAN_MARKUP = "[bold #00D7D7]\\[PLAN][/bold #00D7D7]"

# 「再按一次 Ctrl+C 退出」的提示文本（tui-display 扩展 F31）。
#
# 它**不进聊天区**：那是对话内容的地方，而这条提示是一个**只活两秒的瞬时状态**，
# 留在历史里等于给每一次误按都攒下一条永久噪音。状态栏才是「当前是什么状态」
# 该待的地方——窗口一过它自己消失，什么痕迹都不留。
#
# ⚠ 它是**独立组件**（`StatusHint`），不是状态栏文本的一段。
#
# 原因是 `StatusBar` 整块 `text-align: right`：把提示拼进那串文本，它只会落在
# **右对齐块的最左边**——也就是随其余各段的总长度在屏幕中间某处浮动，
# 而不是贴着状态栏的左边缘。要真的贴左，这一行必须拆成左右两个区
# （`StatusHint` 靠左 + `StatusBar` 靠右），与 Claude Code 底部那一行同构。
#
# 用**灰色**（`dim`）而不是橘色，同样对齐 Claude Code 的同款提示。
#
# ⚠ 这条与状态栏其余高亮段的取舍**方向相反**，别顺手统一：橘色在本项目里有确定
# 语义——「需要用户留意的状态」（放行档、上下文预警、确认面板），那些是**用户没
# 主动做什么、但情况变了**，所以要抢注意力。而这条提示是用户**刚刚按下一个键**的
# 直接回应，他的视线本来就在等反馈，不需要抢；用橘色反而会让真正该抢注意力的
# 那三处贬值——一个界面上醒目的东西越多，醒目就越不值钱。
#
# 刻意不加任何图形符号——F29 的符号白名单里没有为提示级新造的记号，
# 脱离颜色也能辨认的唯一依靠就是文字本身（灰色下这点尤其要紧）。
QUIT_HINT_TEXT = "再按一次 Ctrl+C 退出"
_QUIT_HINT_MARKUP = f"[dim]{QUIT_HINT_TEXT}[/dim]"


def compose_status_text(
    provider: str,
    model: str,
    thinking_effort: str,
    preset: "str | None" = None,
    mcp_status: "str | None" = None,
    context_status: "str | None" = None,
    context_warn: bool = False,
    skill_status: "str | None" = None,
    subagent_status: "str | None" = None,
) -> str:
    """
    组装状态栏的 Content markup 文本（纯函数，c10 抽出便于单测）。

    显示格式：\\[protocol] model | 思考模式：X | [AUTO]/[PLAN] | MCP：… | 上下文：…
    c10 起旧的「计划模式：开/关」文字替换为醒目的模式标记（spec F29–F30）。

    auto-plan 扩展的两处变更：

    - `plan_mode`（布尔）与 `permission_mode`（三档字符串）**两个参数合并为
      `preset` 一个**。用户界面上只剩两个模式，两个参数拼一个标记等于把
      预设层的推导又在渲染层做了一遍（spec N5 明令只许有一处推导点）。
    - **独立的「权限模式：X」整段删除。** `/perm` 删掉之后主对话的档位恒为放行档，
      也没有任何配置项能改启动档——一个恒定不变的段是纯噪音，且与 [AUTO] 重复。
      连带去掉它的橘色高亮（理由见 `_MODE_AUTO_MARKUP` 上方的注释）。

    :param preset: 预设取值（"auto" / "plan"）；为 None（工具不可用的 Provider）
                   时**整段不展示**——那些 Provider 既无受控工具也无 Plan Mode，
                   显示一个模式标记只会误导。
    :returns: 可交给 Static(markup=True) 渲染的 markup 字符串
    """
    _LABEL = {"off": "关闭", "high": "高效", "max": "最强"}
    state = _LABEL.get(thinking_effort, thinking_effort)
    # Static(markup=True) 走 Textual 的 Content markup：`[xxx]` 会被当成样式标签解析。
    # 这里 `[provider]`、`[AUTO]`、`[PLAN]` 的方括号是想当「字面量」显示的，必须转义
    # 开口的 `[`（写成 `\[`），否则会被解析成无效样式标签而整段消失（项目已知坑）。
    text = (
        f" \\[{escape(str(provider))}] {escape(str(model))} | "
        f"思考模式：{escape(str(state))}"
    )
    if preset is not None:
        text += f" | {_MODE_PLAN_MARKUP if preset == 'plan' else _MODE_AUTO_MARKUP}"
    # MCP 段（c7）：仅在启用且有 Server 时展示；文本可能含字面 `[`，统一 escape 兜底。
    if mcp_status is not None:
        text += f" | {escape(str(mcp_status))}"
    # 上下文段（c8）：仅在有 ContextManager 时展示。接近上限/熔断时橘色高亮——
    # 与 permissive、确认面板同色，语义都是「需要用户留意」。注意先 escape 文本再包裹
    # 颜色标签（颜色标签本身不能被转义，否则会被当字面量显示）。
    if context_status is not None:
        seg = escape(str(context_status))
        if context_warn:
            seg = f"[#FFA500]{seg}[/#FFA500]"
        text += f" | {seg}"
    # Skill 段（c11）：仅在有已激活 Skill 时展示（形如 `Skill:2`），
    # 与 MCP 段「None 即隐藏」同构——没用 Skill 的用户状态栏与 c10 完全一致。
    # 注意该文本刻意不含方括号（见 SkillManager.status_segment 的说明），
    # 这里仍走 escape 兜底，与其它段口径一致。
    if skill_status is not None:
        text += f" | {escape(str(skill_status))}"
    # 子 Agent 段（c13）：仅在有运行中的任务时展示（形如 `子Agent:2`），
    # 与 MCP / Skill 两段「None 即隐藏」同构——没用委派的用户状态栏与 c12 一致。
    if subagent_status is not None:
        text += f" | {escape(str(subagent_status))}"
    return text + " "


class StatusLine(Static):
    """
    本回合的**活体状态行**（tui-activity-fold 扩展 C 组，F14–F19）。

    位于各交互面板**下方**、输入框**上方**，形如：

        ◈ 处理中… (12s · ↑ 2.1k · esc 中断)

    ## 它与底部另外两个区的分工

    | 区 | 装什么 | 生命周期 |
    | --- | --- | --- |
    | `#status-row` 右区（`StatusBar`） | **配置态**：provider / 模型 / 权限档 | 常驻 |
    | `#status-row` 左区（`StatusHint`） | 瞬时提示（「再按一次 Ctrl+C 退出」） | 两秒 |
    | **本组件** | **本回合活体态**：还在跑、跑了多久、烧了多少 | 一次运行 |

    ⚠ **它不进历史区。** 「跑了 12 秒」这条信息几秒后就过期，
    写进历史等于往对话里灌过期数据。运行一结束就整个隐藏、不占布局。

    ## 为什么它是全界面唯一的动画定时器

    改造前每个工具行各自持一个每秒刷新的定时器，并发执行五个只读工具时
    屏幕上就有五个数字各自在跳——它们表达的是同一件事（「还在跑」），
    却占了五份注意力。现在统一由本组件承担。
    """

    # ⚠ 字段名避开了 Textual 内部名。本轮已经撞过两次（`_render` 与 `_closed`），
    # 两次都**不报错**、只是界面上东西凭空少了。`_running` 同样是
    # `MessagePump` 的内部字段，**不要拿它存「是否在运行」**。
    def __init__(self) -> None:
        super().__init__("", markup=True)
        self._start_time = 0.0
        # 输入侧取**最近一轮**的值、输出侧**跨轮累加**，两者口径不同且都是刻意的
        # ——理由见 `set_usage`。
        self._input_tokens = 0
        self._output_tokens = 0
        self._frame_index = 0
        self._phase = "处理中…"
        self._interruptible = True
        self._spin_timer = None
        # 「这一轮在跑吗」。⚠ **不能叫 `_running`**——那是 Textual `MessagePump`
        # 的内部字段（本轮预检时抓到，见 `ToolBatchWidget` 里那段关于撞名的注释）。
        self._active = False

    def start(self) -> None:
        """
        开始一次运行：清零计数、启动帧定时器。

        **幂等**——重复调用只是重新起算（`_set_streaming(True)` 在异常路径上
        可能被调两次）。

        ⚠ **不改 `display`**：本组件固定占一行、永不隐藏。空闲时画空串。
        理由见 `app.py` 里 `StatusLine` 那段 CSS 的注释——按需出现会让
        `HistoryView` 每轮重排两次，用户看到的是历史区在抖。

        副作用：起一个主线程定时器、重绘自身。
        """
        self._start_time = monotonic()
        self._input_tokens = 0
        self._output_tokens = 0
        self._frame_index = 0
        self._phase = "处理中…"
        self._interruptible = True
        self._active = True
        if self._spin_timer is None:
            self._spin_timer = self.set_interval(SPINNER_INTERVAL, self._tick)
        self._repaint()

    def stop(self) -> None:
        """
        运行结束：停定时器、把这一行**画空**。**幂等**。

        ⚠ 同样不改 `display`（见 `start`）。空闲时留下的是一行空白，
        而不是一行消失——后者才是抖动的来源。
        """
        if self._spin_timer is not None:
            self._spin_timer.stop()
            self._spin_timer = None
        self._active = False
        self.update("")

    def set_usage(self, prompt_tokens: int, completion_tokens: int) -> None:
        """
        更新本回合的 token 用量，**输入与输出分开**（`↑` / `↓`）。

        :param prompt_tokens: 本轮的输入 token（`usage.prompt_tokens`）
        :param completion_tokens: 本轮的输出 token（`usage.completion_tokens`）

        ## ⚠ 两侧的口径**故意不同**，这是本方法最容易被「顺手统一」掉的地方

        - **输入取最近一轮的值，不累加。** 每一轮请求都要把**整段历史重发一遍**，
          累加等于把同一段历史重复计入很多次，得到的数字既不是花费也不是上下文。
          取最近一轮，它的含义就变得很干净：**当前这段对话有多大**。
        - **输出跨轮累加。** 每一轮的输出都是**新产出**的内容，不存在重复计入，
          累加起来正好回答「模型这一回合总共生成了多少」。

        换句话说，不对称不是疏忽：**输入重发所以只能看当下，输出新增所以可以累加**。

        ## 与底部状态栏的关系

        `↑` 与状态栏那段「上下文：N% · X/Y」现在是**同一个量**（前者是 API 亲口
        给出的精确值，后者是 c8 的估算），因此两者应当**接近**——实测 `16.0k`
        对 `15.9K`。这正是改口径的目的：此前 `↑` 是「每轮 total 累加」，
        同一次 9 轮的运行显示 `105.7K`，与状态栏差 6.6 倍，用户看到两个量级
        差这么多的 token 数字同屏，第一反应是「有一个算错了」（真实反馈）。

        ## 这个口径对齐 Claude Code，而且它自己也反转过一次

        官方状态行文档里 `context_window.total_input_tokens` /
        `total_output_tokens` 的说明写着「**当前在上下文窗口中的**令牌计数，
        来自最近的 API 响应」，并明确记着「**在 v2.1.132 之前，这些是累积的
        会话总计**」——也就是说 Anthropic 自己把「累加」改成了「取最近一次」。
        另：它的上下文百分比**只由输入侧算**（`input + cache_creation +
        cache_read`，不含 output），与本项目状态栏的口径一致。

        ⚠ **它仍是跳变式更新，不是持续滚动**（F17）：Provider 协议只在每轮流
        末尾产出一次用量。这与耗时那一段的节奏不同，是**已知且如实记录**的行为。
        """
        if prompt_tokens and prompt_tokens > 0:
            # 覆写而非累加——见上面「输入取最近一轮」那条。
            self._input_tokens = int(prompt_tokens)
        if completion_tokens and completion_tokens > 0:
            self._output_tokens += int(completion_tokens)
        if (prompt_tokens and prompt_tokens > 0) or (
            completion_tokens and completion_tokens > 0
        ):
            self._repaint()

    def set_phase(self, phase: str, interruptible: bool = True) -> None:
        """
        切换阶段词与中断提示的可见性。

        :param phase: 阶段文案（如「处理中…」「等待确认」）
        :param interruptible: 假 → **不显示中断提示**（F18）。确认面板弹出期间
            用它：面板有自己的取消方式，两套提示同屏会误导

        ⚠ **不重置 `_start_time`。** F18 要求面板等待期间耗时继续累计——
        那段时间确实在这次回合内，用户等了多久就是等了多久。
        """
        self._phase = phase or "处理中…"
        self._interruptible = interruptible
        self._repaint()

    def _tick(self) -> None:
        """定时器回调：推进一帧并重绘（主线程内，不涉及任何跨线程调度）。"""
        self._frame_index = (self._frame_index + 1) % len(SPINNER_FRAMES)
        self._repaint()

    def _cost_segments(self) -> "list[str]":
        """
        括号里那几段：耗时 / token / 中断提示。**渲染与纯文本产出共用这一处**
        ——各拼一遍的话，记录里的状态行与用户看到的会悄悄不一致。

        无数据的段**整段隐藏**（与状态栏各段的既有做法一致）：
        没消耗 token 时不写 `↑ 0 tokens`，不可中断时不写 `esc 中断`。

        ⚠ 输入与输出**合成一段**（`↑16.0k ↓1.2k`）而不是两段，是因为段间的
        ` · ` 分隔符表达的是「彼此无关的几件事」，而这两个数字是同一件事的两半。
        两侧**各自隐藏**：只有输入没有输出时（第一轮还没答完）不写 `↓0`。
        """
        segments = [f"{int(monotonic() - self._start_time)}s"]
        usage = []
        if self._input_tokens:
            usage.append(f"↑{format_tokens(self._input_tokens)}")
        if self._output_tokens:
            usage.append(f"↓{format_tokens(self._output_tokens)}")
        if usage:
            segments.append(" ".join(usage))
        if self._interruptible:
            segments.append("esc 中断")
        return segments

    def _current_frame(self) -> str:
        """当前这一帧的旋转标记。"""
        return SPINNER_FRAMES[self._frame_index % len(SPINNER_FRAMES)]

    def compose_text(self) -> str:
        """产出状态行的**纯文本**（不含颜色标记），供测试与埋点使用。"""
        return f"{self._current_frame()} {self._phase} ({SEGMENT_SEP.join(self._cost_segments())})"

    def _repaint(self) -> None:
        """
        重绘。旋转标记取主题青（与历史区/输入框边框同色）——
        同色是刻意的：它表达「状态行属于界面框架，不属于对话内容」。

        ⚠ 方法名不叫 `_render`：那是 Textual 用来产出 Visual 的内部方法，
        覆盖它会让合成器抛 `'NoneType' has no attribute 'render_strips'`
        （本轮真实踩过，见 `ToolBatchWidget` 的注释）。
        """
        if not self._active:
            return
        body = SEGMENT_SEP.join(self._cost_segments())
        self.update(
            f"[{THEME_COLOR}]{self._current_frame()}[/] {escape(self._phase)}"
            f"[{SECONDARY_COLOR}] ({escape(body)})[/]"
        )


class StatusHint(Static):
    """
    状态栏那一行的**左区**：贴着左边缘的瞬时提示位（tui-display 扩展 F31）。

    目前只有一种内容——「再按一次 Ctrl+C 退出」。做成一个组件而不是
    `compose_status_text` 里的一段，是因为 `StatusBar` 整块右对齐，
    拼进去的东西只能贴在右对齐块的左边、随其余各段长度浮动（详见
    `QUIT_HINT_TEXT` 上方的说明）。

    与右区的分工：**左区是「刚发生了什么」，右区是「现在是什么状态」**。
    将来若还有同类瞬时提示，落点在这里而不是往右区那串里塞。
    """

    def set_quit_hint(self, active: bool) -> None:
        """
        挂出或撤下退出提示。

        :param active: 是否处在「按了一次 Ctrl+C」的有效期内

        幂等：重复传同一个值只是重画同样的内容，无副作用。
        """
        self.update(_QUIT_HINT_MARKUP if active else "")


class StatusBar(Static):
    """
    底部状态栏的**右区**，实时展示当前会话的关键状态信息。

    显示格式：[protocol] model | 思考模式：X | [DEFAULT]/[PLAN] | 权限模式：X | MCP：… | 上下文：…
    模式命令执行后（以及每轮流式结束时），App 层会调用 update_status() 刷新显示；
    c10 起刷新由命令处理函数经控制器显式触发，不再依赖命令字符串白名单。
    """

    def update_status(
        self,
        provider: str,
        model: str,
        thinking_effort: str,
        preset: "str | None" = None,
        mcp_status: "str | None" = None,
        context_status: "str | None" = None,
        context_warn: bool = False,
        skill_status: "str | None" = None,
        subagent_status: "str | None" = None,
    ) -> None:
        """
        刷新状态栏显示内容（文本组装见 compose_status_text 纯函数）。

        ⚠ **成对维护点**：本方法的签名必须与 `compose_status_text` 同步——
        它只是转发，多一个少一个参数都不报错，只表现为状态栏少一段内容。

        :param provider: Provider 协议名（anthropic / openai / deepseek）
        :param model: 当前使用的模型名称
        :param thinking_effort: 思考模式强度（off / high / max）
        :param preset: 运行预设取值（"auto" / "plan"），显示为 [AUTO] / [PLAN] 标记。
                       为 None（工具不可用的 Provider）时不展示该段，避免误导。
                       auto-plan 扩展起取代原来的 plan_mode + permission_mode 两个参数。
        :param mcp_status: MCP 连接状态摘要；为 None（未启用 MCP）时不展示该段。
        :param context_status: 上下文用量摘要；为 None（无 ContextManager）时不展示该段。
        :param context_warn: 上下文是否接近上限或已熔断；为真时该段橘色高亮预警。
        :param skill_status: 已激活 Skill 摘要（如 "Skill:2"）；为 None 时不展示该段（c11）。
        :param subagent_status: 运行中的子 Agent 摘要（如 "子Agent:2"）；
                                为 None（无任务在跑）时不展示该段（c13）。
        """
        self.update(
            compose_status_text(
                provider,
                model,
                thinking_effort,
                preset,
                mcp_status,
                context_status,
                context_warn,
                skill_status,
                subagent_status,
            )
        )


class NumberedPanel(OverlayPanel, OptionList):
    """
    三个可选面板（确认 / 澄清 / 会话）的共用底座（tui-display 扩展 E 组）。

    它只做两件事，**不碰任何交互契约**（F27/N8）：

    1. **给可选项编号**（F23）——序号只分配给真正可选的行；表头与详情行这类
       `disabled` 的**不占号**，否则按 `2` 会落到一行说明文字上；
    2. **让当前高亮项带一个非颜色的指示符**（F24）——改造前「当前是哪一项」
       只靠背景色表达，而颜色在截图、配色异常的终端、端到端驱动抓到的纯文本里
       都可能丢失。

    ## 为什么抽成基类而不是在三个面板里各写一遍

    序号与指示符必须**三处一致**，否则用户在确认面板学会的「按 2」到了会话面板
    就不灵。而且 `watch_highlighted` 这类 Textual 钩子写错一次就会自激
    （见下），三份实现意味着三次犯错的机会。

    ## `watch_highlighted` 为什么不会自激

    它用 `replace_option_prompt_at_index` 只换那一行的**提示文本**，
    **不动 `highlighted` 本身**，因此不会触发第二次 watch。
    换成「清空重建选项」的写法就会自激（重建会重置高亮 → 再次触发）。
    """

    def _reset_choices(self) -> None:
        """清空选项与编号表。每次 `show_*` 的第一件事。"""
        self.clear_options()
        # `[(选项下标, 未加前缀的展示文本)]`，按可选顺序。序号即它在本表里的位置 + 1。
        self._choices: "list[tuple[int, str]]" = []

    def _add_static(self, markup: str) -> None:
        """加一行**不可选**的行（表头、详情、URL 补充行）——不占序号。"""
        self.add_option(Option(markup, disabled=True))

    def _add_choice(self, option_id: "Optional[str]", markup: str) -> int:
        """
        加一个可选项，自动带上序号与高亮指示符。

        :param option_id: `OptionList.OptionSelected` 里回传的标识
        :param markup: 该项的展示内容（**可以已经是 markup**）
        :returns: 该项在 `OptionList` 里的下标

        副作用：往选项列表追加一项，并登记进编号表。
        """
        index = self.option_count
        number = len(self._choices) + 1
        # 建的时候一律按「未选中」画；真正的高亮由 `watch_highlighted` 铺上去。
        self.add_option(Option(numbered_prompt(number, markup, False), id=option_id))
        self._choices.append((index, markup))
        return index

    def choice_index(self, number: int) -> "Optional[int]":
        """
        序号（从 1 起）→ 该项在 `OptionList` 里的下标；越界返回 None。

        数字键选中走这条（F23）。
        """
        if 1 <= number <= len(self._choices):
            return self._choices[number - 1][0]
        return None

    def watch_highlighted(self, highlighted: "Optional[int]") -> None:
        """
        高亮变化时，把指示符从旧行挪到新行（F24）。

        ⚠ 用 `replace_option_prompt_at_index` 而不是重建选项：那个方法
        **不改 `highlighted`**，因此不会触发第二次 watch。重建会重置高亮，
        进而再次触发本方法——一个安静的无限循环。

        整段包 try/except：这是 Textual 的 watch 钩子，跑在**主线程的消息泵**上，
        异常逃逸会打断整个界面；而它的职责只是「换一个前缀」，
        失败的最坏后果是指示符没跟上，不值得为它拆掉应用。
        """
        try:
            for number, (index, markup) in enumerate(self._choices, start=1):
                self.replace_option_prompt_at_index(
                    index, numbered_prompt(number, markup, index == highlighted)
                )
        except Exception:  # noqa: BLE001 —— 见上：装饰性更新绝不打断界面
            pass


class ConfirmPanel(NumberedPanel):
    """
    工具执行前的内联确认面板（取代旧的模态弹窗 ConfirmScreen）。

    交互体验与斜杠命令面板 CommandPanel 一致：出现在输入框上方，用方向键在
    「执行 / 取消」间选择，回车确认，Esc 取消，不遮挡历史区。

    用于决策管线判定为 ASK（交人工确认）的工具调用：顶部以橘色表头展示工具名、关键参数
    摘要与「为何需要确认」的原因（仅展示、不可选），下方四个可选项对应四态放行（c6 F6）。

    结果如何回传：面板本身不持有协调层状态。用户选择某项时由 OptionList 原生发出
    OptionList.OptionSelected（App 据 option.id 解析为 ConfirmDecision）；按 Esc 时发出本类的
    Cancelled 消息（App 视为拒绝）。App 再唤醒被阻塞的 Worker 线程（见 RhineApp._confirm_tool）。

    四个可选项的 option.id 约定（c6）：
    - "yes"           → 仅放行本次（ConfirmDecision.ALLOW）
    - "yes_session"   → 本会话放行（ConfirmDecision.ALLOW_SESSION，登记会话级 allow 规则）
    - "yes_permanent" → 永久放行（ConfirmDecision.ALLOW_PERMANENT，写入本地级配置）
    - "no"            → 拒绝（ConfirmDecision.DENY）

    ⚠ **②″保护路径（`layer == "protected"`）是唯一的例外：只给三项，
    没有 "yes_permanent"**（protected-paths 扩展 F8）。那个选项写的是一条③层
    allow 规则，而保护路径层的升级效力不被③层消解——写下的规则永远不会被求值，
    用户会看到「点了永久放行，下次还是弹」。**那比不做还糟**，它让一个明确的
    用户决定看起来失效了。同一场景下的「本会话放行」也换了机制（走引擎里
    ②″层自己的内存豁免集合，不落盘）。详见 `show_for` 里的说明。

    设计取舍：确认期间 App 会把焦点临时移到本面板，从而直接复用 OptionList 原生的
    上/下/回车 选择能力（输入框为空时回车不会触发自定义提交消息，移焦到面板最稳健）。
    """

    # 默认隐藏自身，避免依赖外部 App CSS 才能初始隐藏
    DEFAULT_CSS = "ConfirmPanel { display: none; }"

    # 表头在 OptionList 中的索引（表头 disabled 不可选）
    _HEADER_INDEX = 0

    # 工具行标题用的主参数映射（F12），由 `app.on_mount` 灌进来。
    # 缺省空字典 → 退回键值对摘要，与改造前逐字一致。
    _primary_args: dict = {}

    # 全局展开开关的本地副本（`Ctrl+O`，F5/F41）。
    #
    # ⚠ **必须记在这里，而不是只广播给「当前挂着的行」**：展开之后新产生的
    # 每一行都要按展开态画。只广播不记的话，用户按下 Ctrl+O 之后接着跑的工具
    # 又是折叠的——现象是「这个开关时灵时不灵」，而那比没有开关更让人困惑。
    _expanded: bool = False

    def set_expanded(self, expanded: bool) -> None:
        """
        接收全局展开开关，**记下来并广播给已挂载的工具行**（F5/F41）。

        :param expanded: 展开为真、折叠为假

        副作用：改自身状态；重绘全部已定色的工具行。
        """
        self._expanded = expanded
        for widget in self.query(ToolCallWidget):
            widget.set_expanded(expanded)

    def set_primary_args(self, mapping: dict) -> None:
        """接收「工具名 → 主参数键名」映射（F12），与 `HistoryView` 同一份。"""
        self._primary_args = dict(mapping or {})

    def _add_choices(self, items) -> None:
        """
        批量加可选项：`[(id, 主文本, 说明)]`。

        说明用暗色跟在主文本后面——它是次级信息，与主文本同亮度会让每一行
        都在争注意力，而用户真正要读的只有那几个动词。

        ⚠ **主文本按显示宽度补齐，让说明列对齐**（真机反馈）。
        四个选项的主文本宽度不一（「拒绝」4 格、「本会话放行」10 格），
        直接拼两个空格会让说明参差不齐，一眼扫过去像四段互不相干的话。

        补齐必须用 `cell_len` 而不是 `len`：中文一个字占**两格**，
        按字符数补出来的「对齐」在屏幕上照样是歪的。
        """
        first = None
        width = max((cell_len(label) for _id, label, _d in items), default=0)
        for option_id, label, detail in items:
            if detail:
                pad = " " * (width - cell_len(label) + 2)
                markup = f"{label}{pad}[dim]{detail}[/dim]"
            else:
                markup = label
            index = self._add_choice(option_id, markup)
            if first is None:
                first = index
        # 默认高亮第一个可选项。**用实际下标而不是写死的 1**：URL 类请求会在
        # 表头后面插几行补充说明（web_fetch 扩展 F9），写死会落到一行 disabled
        # 的说明文字上。
        self._first_choice = first

    class Cancelled(TextualMessage):
        """用户按 Esc 取消确认时发出，由 App 视为拒绝执行。"""
        pass

    BINDINGS = [
        # Esc 取消：发出 Cancelled 消息交给 App 处理（等价于选择「取消」）
        Binding("escape", "cancel", "取消", show=False),
    ]

    # 判定层 → 面板上显示的中文名。与 trace/reader.py 的 _LAYER_NAMES 是同一批取值，
    # 但**两处刻意不合一**：让 trace（只依赖标准库的叶子包）反向依赖 permission
    # 会破坏它的架构不变量。一致性由 tests 里遍历 Layer 的断言钉住。
    _LAYER_LABELS = {
        "hook": "⓪Hook 规则",
        "blacklist": "①危险命令黑名单",
        "sandbox": "②路径沙箱",
        "protected": "②″保护路径",
        "network": "②′网络边界",
        "rule": "③可配置规则",
        "mode": "④权限模式",
    }

    def _url_detail_lines(self, tool_call, decision) -> list[str]:
        """
        为 URL 类请求生成补充展示行：完整地址、主机名、命中层。

        :param tool_call: 本次工具调用（从中取未经截断的原始地址）
        :param decision: DecisionResult（提供 host 与 layer）
        :returns: 已转义、可直接进 markup 的行文本列表

        ⚠ **完整 URL 进 markup 前一律用本模块的 `escape`，绝不要
        `from rich.markup import escape`。** URL 天然含 `[`
        （IPv6 字面量 `http://[::1]/`、含 `[` 的查询串），而 rich 那版只转义
        「看起来像完整标签」的 `[...]`，落单的 `[` 会被整个放过，
        然后在 Textual 的布局阶段抛 MarkupError——那是没有任何 try/except
        兜得住、会直接拆掉整个 app 的那一类。
        """
        args = tool_call.arguments if isinstance(tool_call.arguments, dict) else {}
        raw_url = str(args.get("url") or "")
        lines: list[str] = []
        if raw_url:
            # **不截断**：这一行的全部价值就在于让用户看到完整地址。
            lines.append(f"   [dim]完整地址：[/dim]{escape(raw_url)}")
        host = getattr(decision, "host", "") or ""
        if host:
            lines.append(f"   [dim]主机名：[/dim]{escape(host)}")
        layer = getattr(decision, "layer", None)
        layer_value = getattr(layer, "value", layer)
        if layer_value:
            label = self._LAYER_LABELS.get(str(layer_value), str(layer_value))
            lines.append(f"   [dim]判定来自：[/dim]{escape(label)}")
        return lines

    def show_for(self, tool_call, tool, decision=None) -> None:
        """
        为一次工具调用填充并显示确认面板（c6 四态放行）。

        每次调用先清空旧选项再重建，避免残留上一次确认的内容。
        表头（disabled）展示工具名、参数摘要与「为何需要确认」的原因供用户判断；
        其后是四个可选项（本次/本会话/永久/拒绝），默认高亮「本次放行」（_YES_INDEX）。

        :param tool_call: provider.base.ToolCall，提供工具名与参数
        :param tool: tools.base.Tool（暂用于潜在扩展，如展示描述）
        :param decision: 决策管线给出的 DecisionResult；其 reason 展示在表头说明缘由。
                         为 None 时仅展示工具信息（向后兼容）。

        副作用：修改 OptionList 选项并使面板可见。
        """
        # 工具名与参数走 B 组的主参数口径（F12）：确认面板上显示
        # `Write(docs/notes.md)` 而不是 `write_file(path=docs/notes.md, con…)`。
        # 用户是靠这一行决定放不放行的，键名在这里同样只占地方。
        label, inner = resolve_call_title(tool_call, self._primary_args)
        if not self._primary_args or not inner:
            # 没有主参数映射（非 DeepSeek Provider）时退回宽松的键值对摘要。
            # ⚠ 这里的上限是 **200 而不是工具行的 60**：面板这一行是人在回路的
            # 判断依据，按工具行的宽度截会把关键信息切掉（web_fetch 扩展 F9
            # 记过这个坑：地址被截断意味着攻击者只要把恶意部分放在第 31 个字符
            # 之后，这一层就形同虚设）。
            inner = summarize_args(tool_call.arguments, max_len=200)
        # 判定原因：**只在它有分辨力的时候才显示**（真机反馈后收窄，不是一刀砍掉）。
        #
        # 原本无条件拼在表头后面。问题是绝大多数确认走的是**第④层兜底**，
        # 那句话恒为「默认模式：无规则命中」——每次都一样、对判断放不放行
        # 没有任何帮助，纯粹占掉表头宽度，把真正要读的 `工具名(参数)` 挤到一边。
        #
        # ⚠ **但不能因此整段删掉。** 别的层给出的原因是**这一次特有**的，
        # 而且往往是用户唯一能看到它的地方：
        #   - `hook` → 「Hook 规则「x」（来源：y）要求这次调用由你确认。<自定义原因>」
        #   - `rule` / `sandbox` / `network` → 具体命中了哪条、越了哪个界
        # 一刀砍掉的后果实测过：`test_e2e_hooks` 场景 2 当场红——**用户再也
        # 看不出这次面板是哪条 Hook 规则要求弹的**。
        #
        # 因此判据是「原因来自哪一层」，不是「有没有原因」。
        layer = getattr(decision, "layer", None) if decision is not None else None
        layer_value = str(getattr(layer, "value", layer) or "")
        reason = (
            f"  [dim]· {escape(decision.reason)}[/dim]"
            if decision is not None and decision.reason and layer_value != "mode"
            else ""
        )
        self._reset_choices()
        # 橘色表头：醒目提示这是有副作用的操作；disabled 使其不可被选中/跳过导航。
        # `⚠` 去掉（F28）——「确认执行」四个字 + 橘色分隔线已经说清了它的性质。
        self._add_static(f"[#FFA500]确认执行  {label}({inner})[/#FFA500]{reason}")
        # URL 类专用补充行（web_fetch 扩展 F9）。
        #
        # **为什么需要它**：上面那行走 summarize_args，它把每个参数值截到 30 字符，
        # 一条 `https://docs.example.com/reference/v2?token=abc` 只会显示成
        # `url=https://docs.example.com/re…`。而用户正是靠面板上那个地址来决定
        # 放不放行的——地址被截断意味着攻击者只要把恶意部分放在第 31 个字符之后，
        # 人在回路这层就形同虚设。
        #
        # 判据用 `decision is not None and ...`：decision 是可选参数，
        # 既有代码一律先判非空再取属性。这里是**主线程布局路径**，
        # AttributeError 属于「没有任何 try/except 兜得住」的那一类。
        if decision is not None and getattr(decision, "kind", "") == "url":
            for line in self._url_detail_lines(tool_call, decision):
                self._add_static(line)
        # 四个可选项带序号（F23），用户可以直接按数字键选中。
        # 改造前这里是四个彩色 emoji（`✅ 🟢 💾 ❌`）——四种颜色反而盖过了
        # 「哪个是当前选中」这个唯一重要的信息。语义现在由序号 + 文字承担。
        #
        # ⚠ **②″保护路径场景下少一项：不提供「永久放行」**（protected-paths 扩展 F8）。
        #
        # 那个选项写的是一条③层 allow 规则，而本层的升级效力**不被③层消解**
        # ——换句话说，用户点了「永久放行」之后写下的那条规则**永远不会被求值**，
        # 下次改同一个文件还是弹。**这比不做还糟**：它让一个明确的用户决定
        # 看起来失效了，而界面上没有任何线索说明为什么。
        #
        # 于是这里给出的「本会话放行」也换了机制——它不登记③层规则，
        # 而是登记进权限引擎里②″层自己的内存豁免集合（见
        # `PermissionEngine.grant_protected_exemption`）。
        # 说明文字里那句「不写入配置」不可省：用户对这个选项的既有心智
        # 就是「登记一条会话规则」，机制换了必须说破。
        #
        # ⚠ **成对维护点**：这里少给一个选项，`conversation.py` 的 `_build_ask`
        # 就必须同步把保护路径的「本会话放行」改走豁免登记。只改一处都不报错——
        # 只改这里：面板不给「永久放行」了，但「本会话放行」仍写③层规则，
        #           用户点了之后下次还弹；
        # 只改那里：面板仍显示一个点了没用的「永久放行」。
        protected = layer_value == "protected"
        self._add_choices(
            [
                ("yes", "本次放行", "仅执行本次"),
                (
                    "yes_session",
                    "本会话放行",
                    "本会话内对该文件不再询问，不写入配置"
                    if protected
                    else "本会话内相同调用不再询问",
                ),
                *(
                    []
                    if protected
                    else [("yes_permanent", "永久放行", "写入本地配置，重启仍生效")]
                ),
                # ⚠ `Esc` 后面**不再手工塞空格**：说明列已由 `_add_choices` 按
                # 显示宽度对齐，手工空格只会把这一行又推歪。
                ("no", "拒绝", "让模型据此调整（Esc）"),
            ]
        )
        self.set_visible(True)
        # 默认高亮「本次放行」，回车即执行（与 / 命令面板一致的顺手体验）
        self.highlighted = self._first_choice

    def show_prompt(self, title: str, yes_label: str, no_label: str) -> None:
        """
        以通用「是/否」提示复用本面板（c4 用于 Plan Mode 的「是否开始执行」审批）。

        与 show_for 不同：不展示工具名/参数，而是展示一段自定义标题，下面给出两个选项
        （id 固定为 "yes"/"no"，无第三项）。App 据 option.id=="yes" 判定是否批准。

        :param title: 表头提示文本（单行，过长请由调用方先截断）
        :param yes_label: 「是」选项的展示文本
        :param no_label: 「否」选项的展示文本

        副作用：修改 OptionList 选项并使面板可见。
        """
        self._reset_choices()
        self._add_static(f"[#FFA500]{escape(title)}[/#FFA500]")
        self._add_choices([("yes", yes_label, ""), ("no", no_label, "Esc")])
        self.set_visible(True)
        self.highlighted = self._first_choice

    def hide(self) -> None:
        """隐藏面板并收回布局空间。"""
        self.set_visible(False)

    def action_cancel(self) -> None:
        """Esc 绑定：发出 Cancelled 消息，由 App 解释为拒绝执行。"""
        self.post_message(self.Cancelled())


class ClarifyPanel(NumberedPanel):
    """
    Plan Mode 需求澄清面板（spec F12）。

    模型在规划阶段通过 ask_user 工具发起提问时，App 用本面板把问题与候选项呈现给用户：
    出现在输入框上方，方向键上下选择，回车确认，Esc 取消，不遮挡历史区（与确认面板同款交互）。

    每个候选项展示「概述 + 详细描述」，但只有概述可被选中：
    - 实现方式：每个候选项渲染为「可选的概述行」+ 紧随其后的「disabled 详情行」。
      OptionList 的上下导航会自动跳过 disabled 项，从而做到「导航只在概述之间移动」，
      同时详情仍然可见，帮助用户判断（对应需求：上下移动只在概述间移动、每个选择下有详细描述）。
    - 最推荐的候选项排在第一位（由模型保证），概述文本自身已含推荐信息，不再额外加标记。

    结果如何回传：用户选中某概述行时由 OptionList 原生发出 OptionList.OptionSelected
    （option.id 为该候选项在 options 中的下标字符串，App 据此取回所选概述）；按 Esc 发出
    本类的 Cancelled 消息（App 视为用户取消澄清）。App 再唤醒被阻塞的 Worker（见 RhineApp._clarify）。
    """

    # 默认隐藏自身，避免依赖外部 App CSS 才能初始隐藏
    DEFAULT_CSS = "ClarifyPanel { display: none; }"

    class Cancelled(TextualMessage):
        """用户按 Esc 取消澄清时发出，由 App 视为用户取消。"""
        pass

    BINDINGS = [
        # Esc 取消：发出 Cancelled 消息交给 App 处理
        Binding("escape", "cancel", "取消", show=False),
    ]

    def show_for(self, question: str, options: list[ClarifyOption]) -> None:
        """
        为一次澄清提问填充并显示面板。

        先清空旧选项再重建，避免残留上一次提问内容。结构为：
        - 一个 disabled 表头（展示问题 question，不可选）
        - 对每个候选项：一个可选概述行（id=下标字符串）+ 一个 disabled 详情行（若有 detail）

        :param question: 模型要澄清的问题
        :param options: 候选项列表；第一个为最推荐项（概述文本自身已含推荐信息）

        副作用：修改 OptionList 选项并使面板可见。
        """
        self._reset_choices()
        # 青色表头：展示问题本身；disabled 使其不可被选中、导航跳过。
        # `❓` 去掉（F28）——问句本身加上青色分隔线已经说清它是个提问。
        self._add_static(f"[#7AEEFF]{escape(question)}[/#7AEEFF]")

        first_selectable: "int | None" = None
        for idx, opt in enumerate(options):
            # 概述行：可选、带序号，id 为该候选项下标（字符串）。
            # 注：推荐顺序由模型保证（第一位即最推荐），概述文本本身已带推荐信息，
            #     故不再额外加「推荐」前缀，避免重复提示。
            option_index = self._add_choice(str(idx), escape(opt.summary))
            if first_selectable is None:
                first_selectable = option_index
            # 详情行：disabled，仅展示，导航会跳过，**不占序号**（F23）——
            # 占了的话按 `2` 会落到一行说明文字上。
            # 缩进与上方概述行的正文左缘对齐（序号前缀占四格）。
            if opt.detail:
                self._add_static(f"[dim]     {escape(opt.detail)}[/dim]")

        self._add_static("[dim]                                              Esc 取消[/dim]")
        self.set_visible(True)
        # 默认高亮第一个可选概述行
        if first_selectable is not None:
            self.highlighted = first_selectable

    def hide(self) -> None:
        """隐藏面板并收回布局空间。"""
        self.set_visible(False)

    def action_cancel(self) -> None:
        """Esc 绑定：发出 Cancelled 消息，由 App 解释为用户取消澄清。"""
        self.post_message(self.Cancelled())


class SessionPanel(NumberedPanel):
    """
    /resume 的交互式会话选择面板（c9 交互化）。

    用户输入 /resume（无参）后，App 用本面板列出全部历史会话：出现在输入框上方，
    方向键上下选择、回车载入、Esc 退出，不遮挡历史区（与确认/澄清面板同款交互）。
    展示期间 App 会把焦点移到本面板，直接复用 OptionList 原生的上/下/回车导航。

    条目呈现规则：
    - 每个会话一行紧凑显示：编号、锁标记、会话 ID、（当前）标记、时间、消息数、标题；
      单行可让全部条目一屏尽收（两行式在会话多时必然滚动、扫读效率减半）。
    - 被其它实例锁定的会话（🔒）与当前会话（（当前））设为 disabled——OptionList
      导航自动跳过，从源头避免「选中后才报错」的挫败感。注意这只是 UX 优化：
      列表展示与实际载入之间存在锁竞态窗口，真正的锁检查仍在 resume_into → attach，
      竞态失败会以 NOTICE 反馈且不清屏。

    结果如何回传：用户选中某会话时由 OptionList 原生发出 OptionList.OptionSelected，
    option.id 即完整 session_id（App 据此直接以 "/resume <session_id>" 走带参载入路径，
    绕过编号解析）；按 Esc 发出本类的 Cancelled 消息（App 关闭面板、界面原样保留）。
    """

    # 默认隐藏自身，避免依赖外部 App CSS 才能初始隐藏
    DEFAULT_CSS = "SessionPanel { display: none; }"

    class Cancelled(TextualMessage):
        """用户按 Esc 退出会话选择时发出，由 App 关闭面板（不做任何载入）。"""
        pass

    BINDINGS = [
        # Esc 退出：发出 Cancelled 消息交给 App 处理（界面原样保留）
        Binding("escape", "cancel", "取消", show=False),
    ]

    def show_for(self, infos: list[SessionInfo], current_id: str) -> None:
        """
        填充会话列表并显示面板。

        先清空旧选项再重建，避免残留上次列表。结构为：
        - 一个 disabled 青色表头（操作提示，不可选）
        - 每个会话一行：可恢复项 id=session_id 可选；锁定项/当前会话 disabled

        :param infos: 全部会话信息（MemoryManager.list_resume_sessions 产出，已按时间倒序）
        :param current_id: 当前会话 ID（用于标注「（当前）」并禁用）

        副作用：修改 OptionList 选项并使面板可见、重置高亮到第一个可选项。
        """
        self._reset_choices()
        # `📂` 与 `↑↓` 都去掉（F28/F29）：前者是装饰，后者在中文界面里写字更清楚。
        self._add_static(
            "[#7AEEFF]选择要恢复的会话        上下键选择，回车载入，Esc 取消[/#7AEEFF]"
        )
        first_selectable: "int | None" = None
        for info in infos:
            when = info.last_time.strftime("%Y-%m-%d %H:%M") if info.last_time else "未知时间"
            is_current = info.session_id == current_id
            locked = info.locked and not is_current
            # ⚠ session_id / title 都可能含 "["（title 来自用户消息原文），必须
            # escape，否则被 Textual markup 当标签吞掉（项目已知坑）。
            #
            # `🔒`→`[锁定]`、`（当前）`→`[当前]`（F28/F30）：语义由**文字**承担，
            # 不靠一个图形。这两条本来就不可选，用户需要知道的是「为什么点不了」。
            mark = "[锁定] " if locked else ("[当前] " if is_current else "")
            line = (
                f"{escape(mark)}{escape(info.session_id)} · {when} · "
                f"{info.message_count} 条 · [dim]{escape(info.title)}[/dim]"
            )
            if locked or is_current:
                # 不可选行**不占序号**（F23）——占了的话序号会跳号，
                # 而用户按下的那个数字对应的是另一条。
                self._add_static(f"[dim]   {line}[/dim]")
                continue
            option_index = self._add_choice(info.session_id, line)
            if first_selectable is None:
                first_selectable = option_index
        self.set_visible(True)
        # 默认高亮第一个可选会话（最近的可恢复会话，回车即载入）
        if first_selectable is not None:
            self.highlighted = first_selectable

    def hide(self) -> None:
        """隐藏面板并收回布局空间。"""
        self.set_visible(False)

    def action_cancel(self) -> None:
        """Esc 绑定：发出 Cancelled 消息，由 App 关闭面板。"""
        self.post_message(self.Cancelled())
