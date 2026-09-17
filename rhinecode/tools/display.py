"""
工具调用的**展示口径**（tui-display 扩展 F11/F12）：内部名 → 展示标签、
以及「括号里放哪个参数的值」。

## 为什么这个模块在 `tools/` 而不是 `tui/`

它有**两个**使用方，分处不同的层：

- `tui/widgets.py` 画主对话的工具行；
- `subagents/runner.py` 给活动区渲染子 Agent 的最近调用（F5）。

而 `subagents` **不能依赖 `tui`**——那不只是分层洁癖，是**真的会炸**：
`tui/widgets.py` 已经 `from rhinecode.subagents.tasks import BRANCH_AGENT_NAME`，
反向再来一条就成环（导入 `subagents.tasks` 会先执行 `subagents/__init__`，
它又去 import runner，runner 再 import 半初始化的 tui.widgets）。
这与 CLAUDE.md 里记的 `agent` ↔ `subagents` 那次是同一个坑。

放在 `tools/` 是安全的：`tools/__init__.py` **保持为空**（既有架构不变量），
因此 `from rhinecode.tools.display import ...` 不会连带拉起任何东西。
`subagents/runner.py` 已经用同样的方式 import `tools.path_guard` 了。

## 本模块产出**纯文本，不做 markup 转义**

转义是渲染方的事，且**只能由渲染方做一次**：

- `tui/widgets.py` 把结果拼进 markup 字符串，必须转义；
- `subagents/runner.py` 把结果存进 `TaskRecord.recent_tools`，那只是内存里的
  一个字符串，转义要等它真的被画出来时（`ActivityView`）才做。

在这里转的话，活动区那条路径会被转**两次**，用户看到字面的 `\\[`。
"""

from __future__ import annotations

from typing import Optional

# ---------------------------------------------------------------------------
# 内部工具名 → 展示标签
# ---------------------------------------------------------------------------
# 取值对齐 **Claude Code 的工具命名**（F11）。用户在两边看到的是同一套词汇，
# 不必在脑子里做一次翻译。`run_command` 由本项目自造的 `Run` 改成 `Bash`
# 也是这个理由。
#
# ⚠ **无对应工具的刻意不登记**，别顺手补上：
# - `mcp_add_server` / `mcp_resolve_server`：Claude Code 那边**根本没有对应物**，
#   硬套一个标签等于凭空造出一条假的对应关系；
# - `ask_user` / `present_plan`：那边叫 `AskUserQuestion` / `ExitPlanMode`。
#   前者只是名字长，后者直译过来是「退出计划模式」，与本项目「提交计划**等待
#   审批**」的语义不符——批准与否还没发生，说「退出」是错的。
# - `run_agent`：走委派特例（标签取角色名，见 `tui/widgets.resolve_call_title`），
#   登记在这里只会变成一个永远用不到的死项。
#
# 判据是一句话：**宁可显示内部名，也不要一个会误导人的假标签。**
# 未登记的工具一律回退到原始名，保证新增工具即便忘了登记也不会显示异常。
TOOL_LABELS = {
    # 文件与检索
    "read_file": "Read",
    "write_file": "Write",
    "edit_file": "Update",
    "glob_files": "Glob",
    "grep_content": "Grep",
    # 命令与网络
    "run_command": "Bash",
    "web_fetch": "WebFetch",
    "web_search": "WebSearch",
    # Skill（c11）
    "load_skill": "Skill",
    # 协作（c15）
    "send_message": "SendMessage",
    "task_create": "TaskCreate",
    "task_list": "TaskList",
    "task_get": "TaskGet",
    "task_update": "TaskUpdate",
}

# 主参数值的展示上限。比 `summarize_args_plain` 的整体上限（60）宽松一档：
# 那边要塞「键=值, 键=值」好几组，这里只有一个值，且这个值正是用户唯一要看的东西。
PRIMARY_ARG_MAX_CHARS = 72

# 键值对摘要里，单个值的上限与整体上限。取值沿用改造前 `summarize_args` 的口径，
# 免得兜底分支的形态跟着变。
_KV_VALUE_MAX_CHARS = 30
_KV_TOTAL_MAX_CHARS = 60


def clip_value(value: object, max_chars: int = PRIMARY_ARG_MAX_CHARS) -> str:
    """
    把一个参数值压成单行并按上限截断。

    换行折成空格：工具行只有一行高度，留着换行会把布局撑开。

    :param value: 任意值，非字符串先 `str()`
    :param max_chars: 上限，超出截断并补省略号
    :returns: 单行纯文本

    副作用：无（纯函数）。
    """
    text = str(value).replace("\n", " ").replace("\r", " ").strip()
    if len(text) > max_chars:
        text = text[:max_chars] + "…"
    return text


def summarize_args_plain(
    arguments: "Optional[dict]", max_len: int = _KV_TOTAL_MAX_CHARS
) -> str:
    """
    把工具调用参数压成「键=值, 键=值」单行摘要——**兜底形态**。

    只在工具没有声明 `primary_arg` 时用得上。之所以保留它而不是显示一个空括号：
    `Read()` 会被读成「无参调用」，而真相是「这个工具没登记主参数」。

    :param arguments: 解析后的参数字典；None（解析失败）时返回占位提示
    :param max_len: 整体上限。⚠ **必须可覆盖**——确认面板传 200，那一行是用户
        判断放不放行的依据，按工具行的 60 截会把关键信息切掉
    :returns: 单行纯文本，**未转义**

    副作用：无（纯函数）。
    """
    if arguments is None:
        return "<参数解析失败>"
    if not isinstance(arguments, dict):
        return "<参数格式错误>"
    parts = []
    for key, value in arguments.items():
        parts.append(f"{key}={clip_value(value, _KV_VALUE_MAX_CHARS)}")
    summary = ", ".join(parts)
    if len(summary) > max_len:
        summary = summary[:max_len] + "…"
    return summary


def resolve_call_parts(
    tool_call, primary_args: "Optional[dict]" = None
) -> "tuple[str, str]":
    """
    解析一次工具调用的标题两段：`(标签, 括号内文本)`，**纯文本、未转义**。

    两条分支：
    1. `primary_args` 里登记了该工具、且本次调用真的带了那个键且值非空
       → 括号里放该值；
    2. 否则回退到键值对摘要（见 `summarize_args_plain`）。

    ⚠ **委派特例（F12 第 2 条）不在这里**。`run_agent` 要把标签换成角色名，
    而角色占位名定义在 `subagents.tasks`——本模块 import 它会成环（见模块
    docstring）。那一支放在 `tui/widgets.resolve_call_title` 里，那边本来就
    需要它。子 Agent 侧不受影响：`run_agent` 在 `GLOBAL_DENIED_TOOLS` 里，
    子 Agent 根本调不到它。

    :param tool_call: 有 `name` 与 `arguments` 两个属性的对象
    :param primary_args: `{工具名: 主参数键名}`；None / 空字典时**全部走分支 2**
    :returns: `(标签, 括号内文本)`

    副作用：无（纯函数）。
    """
    name = str(getattr(tool_call, "name", "") or "")
    raw = getattr(tool_call, "arguments", None)
    args = raw if isinstance(raw, dict) else {}
    label = TOOL_LABELS.get(name, name)

    key = (primary_args or {}).get(name)
    if key:
        value = args.get(key)
        if value is not None and str(value).strip():
            return label, clip_value(value)

    return label, summarize_args_plain(raw)


def resolve_full_title(tool_call) -> "tuple[str, str]":
    """
    解析一次工具调用的**完整**标题两段：`(标签, 括号内文本)`，纯文本、未转义。

    与 `resolve_call_parts` 的差别只有两点，但都是刻意的：

    1. **列出全部参数**，不只是主参数——展开到最详细一档时，用户要判断的是
       「这次调用到底做了什么」，只给一个主参数答不了（比如一次搜索限定了
       `path`，而 `path` 不是主参数，折叠态根本看不见）；
    2. **每个值都不截断**，也不做整体长度上限。

    折叠态与本函数的关系不是「同一份内容截长短」，而是**两份为不同用途
    准备的内容**：折叠态挑一个最有辨识度的值给人扫读，展开态给全量供人核对。
    改造前展开态沿用折叠态算好的截断标题，于是「展开」了却看不到被截掉的部分，
    这正是本函数要解决的问题（tui-activity-fold F11）。

    :param tool_call: 有 `name` 与 `arguments` 两个属性的对象
    :returns: `(标签, 括号内文本)`；参数为空或非字典时括号内为空串

    副作用：无（纯函数）。
    """
    name = str(getattr(tool_call, "name", "") or "")
    label = TOOL_LABELS.get(name, name)
    raw = getattr(tool_call, "arguments", None)
    if not isinstance(raw, dict) or not raw:
        return label, ""
    # 换行折成空格与 `clip_value` 同理（标题只有一行高度），但**不截断长度**。
    parts = []
    for key, value in raw.items():
        text = str(value).replace("\n", " ").replace("\r", " ").strip()
        parts.append(f"{key}: {text}")
    return label, ", ".join(parts)


# ---------------------------------------------------------------------------
# 工具活动归并（tui-activity-fold 扩展 F2/F3/F5）
# ---------------------------------------------------------------------------
# 一批连续的**只读检索**调用在历史区归并成一行，这张表同时回答两个问题：
# **哪些工具参与归并**（在表里的才参与）、**归到哪一组、用什么量词**。
#
# ⚠ **白名单与分组表刻意合一。** 拆成两张的话，将来新增一个检索工具时
# 极易只改其中一张——只加分组会让它永远独立成行，只加白名单会让它进了批次
# 却没有量词。合成一张则「登记了就两件事都成立」。
#
# ⚠ **刻意不按 `Tool.read_only` 派生。** 那个标志的语义是「无副作用、可并发」，
# 与「这次调用该不该折叠」并不等价：
# - `load_skill` 不写任何文件，却会改变整个会话可用的能力集合（不折叠）；
# - `run_command` 有副作用，但每一条都已被用户过目（折叠，见表内注释）；
# - MCP 工具的实际语义完全未知（远端 Server 想干什么都行，不折叠）。
# 显式表是**偏严**方向——**未登记的一律独立成行**。遗漏的代价只是少折叠一行
# （看得见、有人会来问），反过来则会让一个不该藏的工具被静默藏进聚合行。
#
# ## 折叠的安全判据（验收期改过一次，这是现在的版本）
#
# **能折叠的，要么无副作用，要么已被用户过目。**
#
# 最初的判据是更严的「被折叠的永远只是『读』」。验收时按用户要求让
# `run_command` 也参与归并（对齐 Claude Code 的 `Ran N shell commands`），
# 那条就不成立了，于是改成现在这条——它同样能撑住「折叠不藏重要信息」：
# 命令在执行前必过权限管线，用户要么当场在面板上放行、要么事先写了 allow 规则。
#
# ⚠ **写文件与编辑文件仍在表外**，那是这条判据的边界：它们改的是工作区内容、
# 且带 diff 块，那正是用户要盯着看的**结果**，不是过程。
# ---------------------------------------------------------------------------
# 历史区静默工具（todo-list 扩展，真机反馈后加）
# ---------------------------------------------------------------------------
# **这些工具的调用不在历史区产生工具行。**
#
# ⚠ 与 `FOLD_GROUPS`（折叠成一行）是**两回事**：折叠是「压缩成一行还看得见」，
# 静默是「一行都不出」。判据也不同——
#
#   折叠的判据是「无副作用，或已被用户过目」；
#   **静默的判据是「这次调用的结果已经由界面上另一块常驻区域完整呈现」。**
#
# 目前唯一的成员是 `todo_write`：它每调一次就在历史区留下一行
# `● todo_write(todos=[{'title': ..., 'state': ...}])`，而**同一份内容**
# 此刻正完整地画在底部的待办块里。一次十几步的任务会因此多出十几行
# 参数被截断的噪音，把真正的对话内容挤下去——这正是 tui-activity-fold
# 花一整轮解决的那个问题（「20 次调用 = 净增 40 行且全部累积」）。
# 对齐 Claude Code：它的 `TodoWrite` 同样不显示成工具调用。
#
# ⚠ **加进这张表之前先问一句：它的结果在界面上还有别的地方看得到吗？**
# 看不到就不能静默——那等于让一次真实发生的动作在界面上**完全没有痕迹**，
# 比噪音危险得多。行为记录（trace）不受影响，静默的只是界面。
SILENT_TOOLS: frozenset = frozenset({"todo_write"})


def silent_tool_names(registry) -> frozenset:
    """
    当前真正注册了的静默工具名。

    :param registry: 工具注册中心；`None` 时返回空集合
    :returns: `SILENT_TOOLS` 与已注册工具名的交集

    与直接用 `SILENT_TOOLS` 的差别：**只保留真的注册了的**。
    非 DeepSeek 工具模式下注册中心为空，返回空集合，历史区行为逐字不变。

    副作用：无。
    """
    if registry is None:
        return frozenset()
    try:
        names = set(registry.names())
    except Exception:  # noqa: BLE001 —— 显示层的辅助，取不到就退回「都不静默」
        return frozenset()
    return frozenset(SILENT_TOOLS & names)


FOLD_GROUPS: "dict[str, tuple[str, str]]" = {
    "glob_files": ("glob", "查找文件 {n} 次"),
    "grep_content": ("grep", "搜索内容 {n} 次"),
    "read_file": ("read", "读取 {n} 个文件"),
    "web_fetch": ("fetch", "抓取 {n} 个网页"),
    # ⚠ **执行命令参与归并，是验收期改的，与上面四个的理由不同。**
    #
    # 上面四个是「无副作用的检索」，折叠它们不藏任何东西。命令**有副作用**，
    # 按最初那条判据（「被折叠的永远只是读」）本该排除。改口径的依据有两条：
    #
    # 1. **它已经被人看过一遍了。** 命令在执行前必过权限管线，默认档下每一条
    #    都弹确认面板、用户亲手放行；配了 allow 规则的则是用户**事先**写下的
    #    授权。折叠的是「已经过目的过程」，不是「悄悄发生的事」。
    # 2. **它折叠后仍然说得出实情。** 聚合语写「执行 3 条命令」，失败会让整行
    #    变红并写出个数——「跑了什么」按一次 `Ctrl+O` 就能逐条看到。
    #
    # ⚠ **写文件与编辑文件仍然不归并**，那条边界没动：它们改的是工作区内容，
    # 且带 diff 块——那正是用户要盯着看的东西，不是过程。
    "run_command": ("bash", "执行 {n} 条命令"),
}

# 运行期间的进行时文案（F5）。批次在封闭之前不知道自己最终有几次调用，
# 因此这一档只表达「在做哪一类事」。
RUNNING_VERBS: "dict[str, str]" = {
    "glob": "查找中…",
    "grep": "搜索中…",
    "read": "读取中…",
    "fetch": "抓取中…",
    "bash": "执行中…",
}

# 未登记工具的兜底进行时文案。正常路径下用不到（不可归并的工具压根不进批次），
# 留着是为了让 `running_verb` 对任意输入都有定义。
DEFAULT_RUNNING_VERB = "执行中…"

# 行内分隔符。与界面其它位置同源（符号白名单内的六个之一），
# 别在这里另写一个字面量。
SEGMENT_SEP = " · "


def is_foldable(tool_name: str) -> bool:
    """
    该工具是否参与历史区的批次归并（F2）。

    :param tool_name: 内部工具名（非展示标签）
    :returns: 在 `FOLD_GROUPS` 内为真

    副作用：无（纯函数）。
    """
    return str(tool_name or "") in FOLD_GROUPS


def running_verb(tool_name: str) -> str:
    """
    该工具运行期间的进行时文案（F5），如「搜索中…」。

    :param tool_name: 内部工具名
    :returns: 进行时文案；未登记的工具回退到通用文案

    副作用：无（纯函数）。
    """
    entry = FOLD_GROUPS.get(str(tool_name or ""))
    if entry is None:
        return DEFAULT_RUNNING_VERB
    return RUNNING_VERBS.get(entry[0], DEFAULT_RUNNING_VERB)


def compose_batch_summary(entries: "list[tuple[str, Optional[bool]]]") -> str:
    """
    把一个批次里的若干次调用压成一句聚合语（F3）。

    产出形如：`查找文件 1 次 · 搜索内容 3 次 · 读取 8 个文件`

    ## 两条口径

    1. **分组顺序取该组第一次出现的时序**，不是字母序也不是表里的定义序——
       聚合语要读起来与实际发生顺序一致（先搜后读时，搜索段就该在前）。
    2. **聚合语说的是「做了几次」，不是「成了几次」**：失败的那次照常计入
       本组总数，且**聚合语里不出现任何失败字样**（见下方那段勘误）。

    ## ⚠ 勘误：失败段已移除（原 F6，2026-09-17 反转）

    原实现在末尾追加 `· N 个失败` 并把整行染成失败色，依据是 spec F6
    「折叠不藏失败」。**现在不这么做了**，理由是那条规则把「这一轮干成了没有」
    与「过程里有没有某一次调用返回了错」混成了同一个信号：

    - 检索类调用的失败**绝大多数是正常探路**（`read_file` 撞上一个不存在的
      路径、`grep_content` 一处没匹配上），模型看一眼就换个地方继续，
      整轮任务照样完成。把整行染红等于反复报一个不需要用户处理的警报，
      而警报报多了就等于没有警报。
    - **真正要紧的失败会让模型停下来说明**，那会封闭批次、让那条调用单独可见
       ——那条路径一个字都没动（F1 的正文断开边界）。

    失败**并没有被藏起来**，只是换了落点：那一条工具行本身仍是失败色、
    仍写着「失败」二字（F7 未改），按 `Ctrl+O` 展开就在那里；
    行为记录里另有 `failures` 字段直接给出个数（`count_batch_failures`）。

    ⚠ **别顺手把失败段加回来**：它字面上永远像句好话（「折叠不该藏信息」），
    而这次反转恰恰是用户实测提出来的——把整行的颜色从「这一轮成了没有」
    降格成「过程里有没有一次报错」，是**拿一个高频无用的红色换掉一个有用的绿色**。

    ## 为什么量词都带宾语

    「查找」与「搜索」在中文里近乎同义，并排出现时读的人分不出差别，
    而聚合语的全部价值就是「一眼看懂这一轮干了什么」。带上宾语
    （文件名 / 内容）之后区分度才立得住。

    :param entries: 按**发生时序**排列的 `(工具名, 是否成功)`；
        第二项为 None 表示该次调用尚未产生结果
    :returns: 单行聚合语；`entries` 为空时返回空串

    副作用：无（纯函数）。
    """
    order: "list[str]" = []          # 组标识，按首次出现排
    counts: "dict[str, int]" = {}
    for name, ok in entries or []:
        entry = FOLD_GROUPS.get(str(name or ""))
        if entry is None:
            # 防御性跳过：不可归并的工具本不该进批次，真进来了也不该让它
            # 把整句聚合语搞成半截。
            continue
        group = entry[0]
        if group not in counts:
            order.append(group)
            counts[group] = 0
        counts[group] += 1

    segments = []
    for group in order:
        # 取该组的量词模板。模板与组标识同在 FOLD_GROUPS 里，故必然取得到。
        template = next(t for g, t in FOLD_GROUPS.values() if g == group)
        segments.append(template.format(n=counts[group]))
    return SEGMENT_SEP.join(segments)


def count_batch_failures(entries: "list[tuple[str, Optional[bool]]]") -> int:
    """
    数一个批次里**已落定的失败**次数。

    聚合语不再显示失败个数（见 `compose_batch_summary` 的勘误段），但这个数字
    仍要有人算——两个用处：界面侧判断要不要做别的处理，以及**行为记录**里
    `ui_tool_batch.failures` 那一格。

    ⚠ **它与聚合语刻意放在同一个模块里并互相指认**：「不显示」是一个显示决定，
    「不记录」会是一次观测能力的损失，两件事完全不同。折叠态不再说出失败之后，
    记录就是回答「那一轮到底有没有报错」的唯一去处——这一条不能跟着一起省掉。

    :param entries: 与 `compose_batch_summary` 同一份 `(工具名, 是否成功)` 列表
    :returns: `ok is False` 的条目数；尚未产生结果（None）的不算失败

    副作用：无（纯函数）。
    """
    return sum(1 for _name, ok in entries or [] if ok is False)


def fold_group_map(registry) -> dict:
    """
    从工具注册中心导出「工具名 → (组标识, 量词模板)」映射，与
    `primary_arg_map` **同构**（同样的入参、同样的容错、同样只建一份）。

    与直接使用 `FOLD_GROUPS` 的差别：**只保留当前真正注册了的工具**。
    这样一个被 `exclude_tools` 摘掉的工具不会出现在映射里，
    界面侧不必再判断「这个名字对应的工具还在不在」。

    :param registry: `ToolRegistry`（或任何有 `names()` 的对象）；None 时返回空字典
    :returns: `FOLD_GROUPS` 与已注册工具名的交集

    副作用：无（只读快照）。
    """
    if registry is None:
        return {}
    names = getattr(registry, "names", None)
    if names is None:
        return {}
    return {name: FOLD_GROUPS[name] for name in names() if name in FOLD_GROUPS}


def primary_arg_map(registry) -> dict:
    """
    从工具注册中心导出「工具名 → 主参数键名」映射。

    :param registry: `ToolRegistry`（或任何有 `names()` / `get()` 的对象）；
        None 时返回空字典
    :returns: 只收**声明过** `primary_arg` 的工具

    两个调用方各建一份：协调层给主对话的工具行用（`conversation.primary_arg_map`），
    运行器给子 Agent 的活动区用。**各自建一次即可**——工具集在启动装配完成
    之后不再变化（MCP 运行期重载只增删远端工具，而远端工具一律没有
    `primary_arg`，映射里本来就没有它们）。

    副作用：无（只读快照）。
    """
    if registry is None:
        return {}
    names = getattr(registry, "names", None)
    get = getattr(registry, "get", None)
    if names is None or get is None:
        return {}
    mapping: dict = {}
    for name in names():
        tool = get(name)
        key = getattr(tool, "primary_arg", "") if tool is not None else ""
        if key:
            mapping[name] = key
    return mapping


__all__ = [
    "TOOL_LABELS",
    "PRIMARY_ARG_MAX_CHARS",
    "clip_value",
    "summarize_args_plain",
    "resolve_call_parts",
    "resolve_full_title",
    "primary_arg_map",
    # 工具活动归并（tui-activity-fold）
    "FOLD_GROUPS",
    "RUNNING_VERBS",
    "SEGMENT_SEP",
    "is_foldable",
    "running_verb",
    "compose_batch_summary",
    "count_batch_failures",
    "fold_group_map",
]
