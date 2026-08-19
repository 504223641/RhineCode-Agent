"""
分类器的全部文案（c16 F13/F17/F21/F27）：纯函数，零 IO。

## ⚠ 本文件最要紧的一条：给模型的文案与给用户的理由是两样东西

    分类器拦下一个动作
        ├─ 给**模型** → DENIED_BY_CLASSIFIER（固定文案，不含具体理由）
        └─ 给**用户** → render_denied_notice(...)（完整理由 + 怎么放行）

**为什么模型不能看到具体理由**：那对它而言是一份**绕过指南**。
「原来是因为这个域名不在白名单里」会直接导致它换个域名重试；
「原来是因为你说过别提交」会导致它换成 `git push` 而不是 `git commit`。

对齐 Claude Code：v2.1.208 起大多数会话里给模型的理由是固定文本
`Blocked by classifier`，完整理由只在用户侧可查。

⚠ **「顺手让模型也看到具体理由，反正它更有用」看起来永远像是个改进。**
它确实会让模型下一轮的表现「更聪明」——直到你发现它聪明的方向是绕过。
这就是为什么两者定义在同一个文件里相邻的位置，并互相指认。

## 界面提示走警告级

被拒与熔断都属于「用户没主动做什么、但情况变了」，按 tui-display 的四级分级
落在**警告级**（有「警告：」文字前缀，脱离颜色也认得出）。
不能走提示级——那一档是给「记忆已更新」这类**误读代价为零**的消息用的。
"""

from __future__ import annotations

from typing import Iterable, Sequence

from rhinecode.classifier.models import (
    SCOPE_COMMAND,
    SCOPE_MESSAGE,
    SCOPE_SEARCH,
    SCOPE_URL,
    BreakerReason,
    BreakerState,
    ReviewAction,
    Verdict,
)

# 各类别在文案里的说法。集中一处，免得四个函数各写一遍中文措辞。
_SCOPE_LABELS = {
    SCOPE_COMMAND: "命令",
    SCOPE_URL: "网络访问",
    SCOPE_MESSAGE: "队友消息",
    SCOPE_SEARCH: "网络搜索",
}


# ── 给模型的固定文案（⚠ 不含任何具体理由，见模块 docstring）──────────────
#
# ⚠ **成对维护点**：改这里就要看一眼下面的 `render_denied_notice`。
# 两者是同一次判定的两个出口，一个给模型一个给用户。
DENIED_BY_CLASSIFIER = (
    "[被安全审查拦下] 这个动作在执行前经过了一次独立的安全审查，未获通过，"
    "因此**没有执行**。\n"
    "请不要换一种写法、换一个参数或换一个工具重试同一件事——"
    "那不会通过，只会浪费轮次。\n"
    "请改为向用户说明你想做什么、为什么需要它，由用户决定是否放行。"
)

# 消息类专用（spec F3a）。
#
# ⚠ **必须写明投递没有发生**。C15 里消息是叫醒待命队员的**唯一**手段，
# 所以拦下一条消息可能连带拦掉一次唤醒——发送方若以为「对方已经收到了」，
# 它会坐等一个永远不会来的结果，而界面上看不出任何异常。
MESSAGE_NOT_DELIVERED = (
    "[被安全审查拦下] 这条消息在投递前经过了一次独立的安全审查，未获通过。\n"
    "⚠ **消息没有送达**：对方没有收到任何内容；如果对方处于待命状态，"
    "它**没有被唤醒**，不要按「对方已经在处理了」继续推进。\n"
    "请不要改写措辞重发同一条内容。若这件事确有必要，"
    "请向用户说明你想传达什么、为什么需要，由用户决定。"
)


def render_denied_notice(action: ReviewAction, verdict: Verdict) -> str:
    """
    被拒时给**用户**看的系统行（spec F27）。

    :param action: 被拒的动作
    :param verdict: 判定结果（`reason` 是完整理由）
    :returns: 一段多行文本，交给界面的警告级通道

    ⚠ **成对维护点**：见上方 `DENIED_BY_CLASSIFIER`。这里可以写得很具体，
    那里不行。

    ⚠ **必须给出可操作的下一步。** 只说「被拦下了」的话，用户会以为是 bug
    而不是一次判定——而「怎么让它过去」恰恰是他此刻唯一想知道的事。

    副作用：无（纯函数）。
    """
    label = _SCOPE_LABELS.get(action.scope, "动作")
    subject = _summarize_subject(action)

    lines = [
        f"警告：安全审查拦下了一次{label} —— {subject}",
        f"  理由：{verdict.reason}",
    ]
    if action.scope == SCOPE_MESSAGE:
        # 消息类没有「写一条 allow 规则」这种细粒度手段（协作工具落 other 分支，
        # 只认不带括号的整工具规则），所以提示的两条与另外两类不同。
        lines.append("  这条消息没有送达。若确实需要，可在 permissions.yaml 里写 "
                     "`allow: send_message`，或关掉分类器（classifier.enabled: false）。")
    else:
        lines.append(
            "  若这是你要的操作，可在 permissions.yaml 里为它写一条**具体的** "
            "allow 规则（写窄，别写通配），或关掉分类器（classifier.enabled: false）。"
        )
    return "\n".join(lines)


def render_failed_notice(action: ReviewAction, verdict: Verdict) -> str:
    """
    分类器**自身失败**导致拒绝时给用户看的系统行。

    :param action: 被拒的动作
    :param verdict: 判定结果（`reason` 是错误描述）
    :returns: 一段多行文本

    与 `render_denied_notice` **刻意分开**：两者的根因完全不同，而用户要做的
    事也不同。把「分类器认为这条命令危险」与「分类器连不上」显示成同一句话，
    会让一次接口故障看起来像一次安全判定——用户会去改自己的命令，而问题在别处。

    副作用：无（纯函数）。
    """
    label = _SCOPE_LABELS.get(action.scope, "动作")
    subject = _summarize_subject(action)
    return (
        f"警告：安全审查没能给出结论，已按拒绝处理 —— {label}：{subject}\n"
        f"  原因：{verdict.reason}\n"
        "  这通常是分类器的接口不通或超时，不是你的操作有问题。"
        "连续 3 次之后会自动停用分类器并改为逐次确认。"
    )


def render_breaker_notice(state: BreakerState) -> str:
    """
    熔断时给用户看的系统行（spec F17）。

    :param state: 熔断状态快照
    :returns: 一段多行文本

    ⚠ **静默熔断等于静默关掉一层安全机制**，比不做还糟：用户以为分类器还在，
    实际已经不在了。所以这条提示必须说清三件事——发生了什么、之后的行为是什么、
    怎么恢复。

    两种成因的说法不同（`BreakerReason`），因为用户要做的事不同：拦得太多是
    「它不了解你的环境」，连不上是「去看接口配置」。

    副作用：无（纯函数）。
    """
    if not state.tripped:
        return ""

    if state.reason is BreakerReason.FAILURES:
        head = f"警告：安全审查已停用 —— {state.detail}"
        cause = "  多半是分类器的接口地址、凭据或网络有问题，先去看配置里的 classifier 段。"
    else:
        head = f"警告：安全审查已停用 —— {state.detail}"
        cause = "  连续拦下这么多次，通常说明它不了解你的环境、在反复误伤。"

    return "\n".join([
        head,
        cause,
        "  之后：跑命令与访问网络会**逐次弹确认面板**交给你决定；队友消息恢复为直接投递。",
        "  恢复：在确认面板上批准一次即可重新启用。",
    ])


def render_dropped_rules(dropped: Sequence[tuple[str, str, str]]) -> str:
    """
    启动时告知被丢弃的宽泛放行规则（spec F21）。

    :param dropped: 三元组序列 `(规则原文, 来源层, 为什么宽泛)`
    :returns: 一段多行文本；`dropped` 为空时返回空串

    ⚠ **静默丢弃会让「我明明配了为什么还弹」无从查起。** 用户写下那条规则是
    一次明确的决定，我们推翻了它，就必须当面说清楚是哪一条、为什么、
    以及他有哪两个选择。

    副作用：无（纯函数）。
    """
    if not dropped:
        return ""

    # ⚠ **措辞刻意不限定成「命令」**（web_search 扩展 T20）。
    #
    # 本函数原本写的是「过宽的**命令**放行规则」与「让分类器完全看不到对应的
    # **命令**」——那在只有命令类会被丢弃时是准确的。web_search 加进来之后，
    # 一条 `WebSearch` 被丢弃时那两句话会变成「过宽的命令放行规则：WebSearch」，
    # 自相矛盾。
    #
    # 别把它改回去那个「更精确的说法」：这里是**一个面向多类别的通用出口**，
    # 具体是哪一类由每条自己的 `why`（`broad.why_broad`）说清楚。
    lines = [
        f"安全审查已启用，因此暂时不使用下面 {len(dropped)} 条过宽的放行规则："
    ]
    for rule_text, source, why in dropped:
        lines.append(f"  · {rule_text}（来源：{source}）")
        lines.append(f"    {why}")
    lines.append(
        "  这类规则会让分类器完全看不到对应的动作。"
        "想保留请把它改窄（例如写成一条具体的命令），"
        "或关掉分类器（classifier.enabled: false）。"
    )
    return "\n".join(lines)


def render_broad_domain_warning(rules: Iterable[str]) -> str:
    """
    全域名放行规则的启动提醒（spec F22）。

    :param rules: 命中「全域名放行」的规则原文
    :returns: 一段文本；无命中时返回空串

    ⚠ **只提醒、不丢弃**，与命令类规则的处理刻意不同：域名规则同时承担
    「建立白名单」的语义（`permission/network.py` 的②′层据此判断白名单是否
    已建立），丢弃它会连带改变**另一层**的行为，那不在本章范围内。

    副作用：无（纯函数）。
    """
    items = [r for r in rules if r]
    if not items:
        return ""
    return (
        "提示：下面的域名放行规则会让分类器对网络访问完全不生效："
        + "、".join(items)
        + "。（它未被丢弃，因为它同时用于建立域名白名单。）"
    )


def _summarize_subject(action: ReviewAction) -> str:
    """
    把待判动作压成一行可读的说法，供各条提示复用。

    :param action: 待判动作
    :returns: 一行文本

    命令与地址原样给出（用户要据此判断），消息只给收件人与正文首段——
    ⚠ 队友消息的**完整正文不上主界面**（spec 已知边界 8）：把两个子 Agent
    之间的对话原文往主界面上贴，会把用户的注意力从他自己的任务上扯走，
    而 C13 的设计前提正是「只回流结论」。完整正文在行为记录里。

    副作用：无（纯函数）。
    """
    if action.scope == SCOPE_MESSAGE:
        head = (action.specifier or "").strip().splitlines()
        first = head[0] if head else ""
        if len(first) > 40:
            first = first[:40] + "…"
        return f"发给 {action.recipient or '?'}：{first}"
    return (action.specifier or "").strip()
