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
    "primary_arg_map",
]
