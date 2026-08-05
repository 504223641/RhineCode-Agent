"""
报告渲染（spec F9.1 / F11）：两个纯函数，把规则与统计渲染成给人看的文本。

- `render_report`         —— `/hooks` 的完整报告（三段）
- `render_project_notice` —— 项目级规则的启动提示

本模块**零 I/O、零依赖**（只依赖同包的 `models`）。

## ⚠ 转义是调用方的事

两个函数的产物都会进 Textual 的 markup 通道，而命令串里出现 `[` 是常事
（`jq '.[]'`、`sed 's/[a-z]//'`）。调用方**必须**用 `tui/widgets.py` 的 `escape`，
**绝不能**用 `rich.markup.escape`——后者只转义「看起来像完整标签」的 `[...]`，
落单的 `[` 会被整个放过，随后在**布局阶段的主线程**抛 `MarkupError`，
没有任何 try/except 兜得住，Textual 直接拆掉整个 app。

本模块不自己转义，是因为它同时服务终端与测试断言，在这里转会让断言变得难写；
且「谁渲染谁转义」在本项目里已是既有约定。
"""

from typing import Any, Optional

from rhinecode.hooks.models import (
    COMBINE_ALL,
    MATCHER_EXACT,
    MATCHER_GLOB,
    MATCHER_REGEX,
    AgentAction,
    CommandAction,
    Condition,
    HookAction,
    HookRule,
    HttpAction,
    PromptAction,
)

# 提示里展示 `prompt` 正文的最大长度。命令与 URL **不截断**（见 render_project_notice）。
_PROMPT_PREVIEW = 60


def describe_action(action: HookAction) -> str:
    """
    把一个动作渲染成一行说明。

    :param action: 四种动作之一
    :returns: 中文一行描述；**命令串与 URL 一律完整展示，不截断**

    ⚠ **成对维护点**：新增动作类型要在这里加一支。**漏改不报错**，只是
    `/hooks` 与项目级启动提示里那条动作显示成「未知动作」——而项目级提示
    正是 spec F9.1 的全部安全价值所在，显示不出内容等于那道防线没了。

    副作用：无。
    """
    if isinstance(action, CommandAction):
        return f"执行命令（超时 {action.timeout}s）：{action.command}"
    if isinstance(action, HttpAction):
        extra = ""
        if action.headers:
            extra = f"，自定义头 {len(action.headers)} 个"
        return f"HTTP {action.method} {action.url}（超时 {action.timeout}s）{extra}"
    if isinstance(action, PromptAction):
        text = action.text.replace("\n", " ")
        if len(text) > _PROMPT_PREVIEW:
            text = text[:_PROMPT_PREVIEW] + "…"
        return f"注入提示词：{text}"
    if isinstance(action, AgentAction):
        return "启动子 Agent（本版本仅占位，不会真的运行）"
    return f"未知动作：{type(action).__name__}"


def _describe_matcher(matcher) -> str:
    """把一个匹配项渲染成紧凑形式，如 `tool=run_command`、`!command~git *`。"""
    bang = "!" if matcher.negated else ""
    if matcher.kind == MATCHER_EXACT:
        op = "="
    elif matcher.kind == MATCHER_GLOB:
        op = "~"
    elif matcher.kind == MATCHER_REGEX:
        op = "=~"
    else:  # 兜底，理论上不可达
        op = "?"
    return f"{bang}{matcher.field}{op}{matcher.pattern}"


def describe_condition(condition: Optional[Condition]) -> str:
    """
    把条件渲染成一行说明。

    :param condition: 条件；None 表示无条件
    :returns: 中文一行描述

    副作用：无。
    """
    if condition is None:
        return "无条件（每次都触发）"
    joiner = "，且 " if condition.combine == COMBINE_ALL else "，或 "
    head = "全部满足：" if condition.combine == COMBINE_ALL else "任一满足："
    return head + joiner.join(_describe_matcher(m) for m in condition.matchers)


def _describe_stat(stat: Optional[dict[str, Any]]) -> str:
    """
    把一条规则的执行统计渲染成一行。

    :param stat: 该规则的统计字典；None 或空表示本次运行内从未触发

    「从未触发」这句话是排查的主力信息——`pre_tool_use` 等三个工具级事件的
    字段集是开放的，写错字段名在加载期发现不了，唯一的表现就是这里恒为 0。
    """
    if not stat or not stat.get("triggered"):
        return "触发：0 次（本次运行内从未触发——若与预期不符，先检查条件里的字段名）"
    parts = [f"触发：{stat['triggered']} 次"]
    if stat.get("last_verdict"):
        parts.append(f"最近结论：{stat['last_verdict']}")
    if stat.get("last_ms") is not None:
        parts.append(f"最近耗时：{stat['last_ms']}ms")
    if stat.get("failures"):
        parts.append(f"失败：{stat['failures']} 次")
    if stat.get("once_consumed"):
        parts.append("once 已消耗")
    return "，".join(parts)


def _render_rule(rule: HookRule, stat: Optional[dict[str, Any]], ordinal: int) -> list[str]:
    """把一条规则渲染成四行（标题 + 事件 + 条件 + 动作 + 统计）。"""
    async_mark = "，后台异步" if rule.run_async else ""
    once_mark = "，只跑一次" if rule.once else ""
    return [
        f"{ordinal}. [{rule.source}] {rule.name}{once_mark}{async_mark}",
        f"     事件：{rule.event.value}",
        f"     条件：{describe_condition(rule.condition)}",
        f"     动作：{describe_action(rule.action)}",
        f"     {_describe_stat(stat)}",
    ]


def render_report(
    rules: list[HookRule],
    warnings: list[str],
    stats: dict[str, dict[str, Any]],
    *,
    user_path: str = "~/.rhinecode/hooks.yaml",
    project_path: str = "<项目根>/.rhinecode/hooks.yaml",
) -> str:
    """
    渲染 `/hooks` 的完整报告（spec F11）。

    :param rules: 已加载的全部规则（顺序即执行顺序）
    :param warnings: 加载期警告
    :param stats: 规则名 → 统计字典
    :param user_path: 用户级配置路径（展示用）
    :param project_path: 项目级配置路径（展示用）
    :returns: 多行文本

    三段：已加载规则 / 加载警告 / 配置位置。**无警告时该段整体不出现**——
    一个恒定出现的空段落会让人下意识跳过整个报告。

    统计并入每条规则的最后一行，而不是单列一段：排查「这条 hook 跑没跑」时，
    人要看的是「这条规则 + 它的触发情况」，分成两段就得来回对照名字。

    副作用：无。
    """
    lines: list[str] = []

    if not rules:
        lines.append("当前没有加载任何 Hook 规则。")
        lines.append("")
        lines.append("在下面任一位置写 hooks.yaml 即可启用：")
        lines.append(f"  用户级：{user_path}")
        lines.append(f"  项目级：{project_path}")
    else:
        lines.append(f"已加载 Hook 规则（共 {len(rules)} 条，按执行顺序）")
        lines.append("")
        for ordinal, rule in enumerate(rules, 1):
            lines.extend(_render_rule(rule, stats.get(rule.name), ordinal))
            lines.append("")

    if warnings:
        lines.append(f"加载警告（{len(warnings)} 条，对应规则已被丢弃或降级）")
        lines.append("")
        for warning in warnings:
            lines.append(f"  · {warning}")
        lines.append("")

    return "\n".join(lines).rstrip()


def render_project_notice(project_rules: list[HookRule]) -> Optional[str]:
    """
    渲染项目级规则的启动提示（spec F9.1）。

    :param project_rules: 来源层为 project 的规则
    :returns: 提示文本；无项目级规则时返回 None

    ## ⚠ 这是本章的核心安全措施，三条约束不可放宽

    1. **逐条列出**，不折叠、不省略、不只给计数。
    2. **命令串与 URL 完整展示**，不截断——被截断的命令看不出它到底干什么，
       而「看得出它干什么」正是这条提示存在的全部理由。
    3. 调用方**每次启动都要展示**，不做「只提示一次」的持久化。有状态的话，
       新增的规则会在状态未失效时被静默吞掉（与项目级 Skill 同一课）。

    与项目级 Skill 的提示相比强一档，是因为风险性质不同：Skill 正文只是
    「发给模型的文本」，它指挥的每个工具调用照样过五层权限管线；
    Hook 的动作**直接执行**，不经模型、不经人在回路确认。

    副作用：无。
    """
    if not project_rules:
        return None

    lines = [
        f"⚠ 发现 {len(project_rules)} 条项目级 Hook 规则"
        f"（来自 .rhinecode/hooks.yaml，随仓库分发）。",
        "它们会在对应时刻**直接执行**，不经模型、也不经确认面板。请当作代码来评审：",
        "",
    ]
    for ordinal, rule in enumerate(project_rules, 1):
        lines.append(f"  {ordinal}. {rule.event.value} → {describe_action(rule.action)}")
        if rule.condition is not None:
            lines.append(f"     条件：{describe_condition(rule.condition)}")
    lines.append("")
    lines.append("用 /hooks 查看完整规则与触发情况。")
    return "\n".join(lines)


__all__ = [
    "describe_action",
    "describe_condition",
    "render_report",
    "render_project_notice",
]
