"""
澄清提问的解析与回灌文案（ask-user 扩展）。

## 这个模块是什么

它只做两件事，**都是纯函数、零 IO**：

1. **解析**——把模型给的 `questions` 原始参数变成一串 `ClarifyQuestion`，
   顺带产出「哪些地方不合规、被怎么处理了」的说明（`parse_questions`）；
2. **渲染**——把用户的作答变成一段给模型读的文本，以及「问不了人」
   「用户跳过」「已熔断」几种情形的固定文案。

## 为什么不放在 loop.py 里

两条：`agent/loop.py` 已经近 2000 行；而这里的每个函数都是**纯输入输出**，
放在独立模块里可以脱离整个 Agent 循环、不起线程不发请求地单测
（`tests/test_ask_user_parse.py`）。

## 依赖方向

只依赖 `agent/events.py` 的三个数据类，**不 import 任何 tui / permission /
tools / provider**。保持这一点，本模块就永远可以被任何一层安全地引用。

## 一条贯穿全文的原则：能救就救，救不了就说破

模型给的形状会有各种瑕疵（问题提太多、某题忘了给候选项、字段名写错）。
本模块的口径是**尽量救活，并把「我动了什么手脚」写进回灌文本**，而不是
整次调用打回让它重来（那要多烧一轮）。

⚠ **这与 todo 清单那边「MAX_ITEMS 是拒绝线不是截断线」方向相反，是刻意的。**
判据只有一条：**被截掉的事实，模型下一轮能不能自己发现。**
那边不能——它以为整份清单写进去了，后续判断全建立在一个假的进度上；
这边能——回灌是一份按问题原文逐条列出的清单，少了哪个问题它看得见，
而且下面 `render_answers` 还会在末尾明说。
"""

from typing import Optional

from rhinecode.agent.events import ClarifyOption, ClarifyQuestion, ClarifyReply

# 一次调用最多几个问题（spec F7，对齐 Claude Code 官方的 1–4）
MAX_QUESTIONS = 4
# 每个问题最多几个候选项（spec F8，同上；界面另外无条件追加一项「其它…」）
MAX_OPTIONS = 4
# 徽章最多几个字符（spec F9，对齐官方的 max 12 characters）
MAX_HEADER = 12
# 一次运行里用户跳过几次之后就不再弹面板（spec F17 熔断）
#
# 2 这个数字的依据：连按两次「我不选」已经把「别问我」表达得很清楚了。
# 再往上调会让熔断形同虚设，往下调（1 次）则一次误触就再也问不了。
SKIP_LIMIT = 2


# ---------------------------------------------------------------------------
# 解析
# ---------------------------------------------------------------------------
def _parse_options(raw) -> tuple[tuple[ClarifyOption, ...], str]:
    """
    解析一个问题的候选项列表。

    :param raw: 模型给的原始值，什么都可能是
    :returns: (候选项元组, 瑕疵说明)；说明为空串表示没有瑕疵

    宽松规则：不是数组 → 空；数组里不是对象的、选项名为空的一律跳过；
    超过 `MAX_OPTIONS` 取前几个并给出说明。

    ⚠ **同时认 `label`/`description` 与旧的 `summary`/`detail`。**
    新 schema 只教模型写前一套，但模型可能凭训练先验写出后一套
    （本项目已多次实测到「凭先验硬造调用」的形态）。认两套的成本是两行，
    而认不出的代价是**整题被丢掉、白烧一轮**。

    副作用：无（纯函数）。
    """
    if not isinstance(raw, list):
        return (), ""

    options: list[ClarifyOption] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        # 官方名字优先，旧名字兜底
        label = str(item.get("label") or item.get("summary") or "").strip()
        if not label:
            continue
        description = str(item.get("description") or item.get("detail") or "").strip()
        options.append(ClarifyOption(label=label, description=description))

    note = ""
    if len(options) > MAX_OPTIONS:
        note = (
            f"其中一个问题给了 {len(options)} 个候选项，一个问题最多 {MAX_OPTIONS} 个；"
            f"只呈现了前 {MAX_OPTIONS} 个。"
        )
        options = options[:MAX_OPTIONS]
    return tuple(options), note


def _parse_header(raw) -> str:
    """
    解析徽章文本：去空白、超长截断。

    :returns: 归一后的徽章；空串表示不显示徽章（**不是显示一对空括号**）

    副作用：无（纯函数）。
    """
    header = str(raw or "").strip()
    if len(header) > MAX_HEADER:
        # 截断而不是丢弃：一个被截短的标签仍然有定位作用，丢掉就什么都没有了。
        # 不记进瑕疵说明——它在界面上看得出来，不值得占模型的 token。
        header = header[: MAX_HEADER - 1] + "…"
    return header


def parse_questions(raw) -> tuple[list[ClarifyQuestion], list[str]]:
    """
    把模型给的 `questions` 原始参数解析成问题列表。

    :param raw: 模型给的原始值，什么都可能是（None / 字符串 / 嵌套错层的数组…）
    :returns: (可用问题列表, 瑕疵说明列表)。问题列表为空表示这次提问没法进行，
              此时说明列表里必有至少一条解释原因

    **处理顺序本身是需求**（spec F10），逐条：

    1. `raw` 不是数组 → 直接返回空 + 一条说明；
    2. 逐项解析，下列三种一律**跳过该题**（不毁掉整次提问）：
       不是对象 / 问题原文为空 / 一个可用候选项都没有；
    3. 某题候选项超上限 → 取前几个，记说明；
    4. 徽章超长 → 截断（不记说明）；
    5. `multiSelect` 不是布尔 → 当作单选（不记说明）；
    6. 可用问题超上限 → 取前几个，记说明。

    ⚠ **本函数对任何输入都不抛异常**（spec N1）。它跑在 Agent 循环里，
    抛出去会把「模型参数写歪了」变成「整轮工具执行炸掉」。

    副作用：无（纯函数）。
    """
    if not isinstance(raw, list):
        return [], [
            "questions 必须是一个数组，每一项是一个问题对象"
            "（含 question、options，可选 header 与 multiSelect）。"
        ]

    parsed: list[ClarifyQuestion] = []
    notes: list[str] = []
    skipped = 0

    for item in raw:
        if not isinstance(item, dict):
            skipped += 1
            continue
        question = str(item.get("question") or "").strip()
        if not question:
            skipped += 1
            continue
        options, option_note = _parse_options(item.get("options"))
        if not options:
            # 一个可用候选项都没有的问题没法呈现——面板上只会剩一个「其它…」，
            # 那等于让用户手打，而本工具存在的全部理由就是免去手打。
            skipped += 1
            continue
        if option_note:
            notes.append(option_note)
        # 连字符与下划线两种写法都认（与 C13 角色定义解析同口径）
        multi = item.get("multiSelect")
        if multi is None:
            multi = item.get("multi_select")
        parsed.append(
            ClarifyQuestion(
                question=question,
                options=options,
                header=_parse_header(item.get("header")),
                multi_select=multi is True,
            )
        )

    if skipped:
        notes.append(
            f"有 {skipped} 个问题因为缺少问题原文或候选项被跳过，没有向用户呈现。"
        )
    if len(parsed) > MAX_QUESTIONS:
        notes.append(
            f"你一次提了 {len(parsed)} 个问题，一次最多 {MAX_QUESTIONS} 个；"
            f"只向用户呈现了前 {MAX_QUESTIONS} 个，其余的没有提问——"
            f"如果仍然需要，请在下一轮单独再问。"
        )
        parsed = parsed[:MAX_QUESTIONS]

    return parsed, notes


# ---------------------------------------------------------------------------
# 回灌文案
# ---------------------------------------------------------------------------
# 一个可用问题都没有时回灌的（spec F10 第一行）。
NO_QUESTIONS_FEEDBACK = (
    "这次提问没有成立：questions 里没有一个可用的问题。"
    "每个问题需要有 question（问题原文）与 options（2 到 4 个候选项，"
    "每项含 label 与 description）。请补齐后再问，或者按你的最佳判断直接继续。"
)

# 用户按 Esc 跳过时回灌的（spec F17）。
#
# ⚠ **两层意思缺一不可**：
# ① 按最佳判断继续，并说明你采用了哪种做法；
# ② 不要为同一件事再问一次。
#
# 少了第 ② 层，模型下一轮会原样再问一遍——这与「用户在确认面板里选拒绝」
# 那条软约束是同一个形态，那次实测过一个不听劝的模型把 25 轮迭代
# 全烧在重试上（见 CLAUDE.md 安全边界里 DENIED_BY_USER_FEEDBACK 那段）。
SKIP_FEEDBACK = (
    "用户没有选择——他的意思是这件事交给你定。"
    "请按你的最佳判断继续推进，并在最终回答里说明你采用了哪种做法、"
    "基于什么假设。**不要为同一件事再问一次**，也不要停下来等他。"
)

# 跳过次数达到上限、本次直接不弹面板时回灌的（spec F17 熔断）。
SKIP_CIRCUIT_FEEDBACK = (
    f"这次没有向用户提问：他在本次任务里已经连着跳过了 {SKIP_LIMIT} 次提问，"
    "说明他希望你自己拿主意。后续也不会再弹出提问面板。"
    "请按你的最佳判断继续推进，并在最终回答里说明你的选择与假设。"
)


def render_unavailable(*, interactive: bool, unattended: bool) -> str:
    """
    「现在问不了人」时回灌给模型的文案（spec F4）。

    :param interactive: 本次运行能否与人交互（子 Agent 为假）
    :param unattended: 本次运行是不是无人值守轮（队友消息自动唤起的那种）
    :returns: 一段说明「为什么问不了」以及「那你现在该做什么」的文本

    ⚠ **三条文案刻意各不相同**，不是同一件事的三种说法：三种情形下模型
    该做的事不一样。一句通用的「当前不支持向用户澄清」会让子 Agent 以为
    是一次故障而反复重试，也会让无人轮的模型不知道「用户只是不在，
    不是不想回答」。

    副作用：无（纯函数）。
    """
    if not interactive:
        return (
            "这次没有提问：你是一个子 Agent，没有人能回答你——委派你的主 Agent "
            "拿到的只有你最后的结论。请按你的最佳判断继续，"
            "并把你拿不准的地方与所做的假设**写进结论里**，交给主 Agent 去判断。"
        )
    if unattended:
        return (
            "这次没有提问：这一轮是由队友的消息自动唤起的，用户不在场，"
            "面板弹出来也没人看。请按你的最佳判断继续，"
            "并在回答里说明你的选择与假设，等用户回来他自然会看到。"
        )
    return (
        "这次没有提问：当前拿不到与用户交互的通道。"
        "请按你的最佳判断继续，并在回答里说明你的选择与假设。"
    )


def _render_reply(reply: Optional[ClarifyReply]) -> str:
    """
    把一条作答渲染成回灌清单里「→」右边那一截。

    :param reply: 用户的作答；`None` 表示跳过
    :returns: 一行短文本

    四种答案类型的措辞**刻意各不相同**（spec F11/F12）——尤其
    「多选一项都没勾」与「跳过」必须能分辨：前者是「都不要」，
    后者是「随你」，那是两个相反的指令。

    副作用：无（纯函数）。
    """
    if reply is None:
        return "（用户没有选择，请按你的最佳判断继续）"
    if reply.kind == "free_text":
        return f"{reply.text}（用户自己输入的）"
    if not reply.labels:
        # 只可能出现在多选：他看过全部选项，一个都不要。
        return "（用户一项都没有选——这些候选项他都不要）"
    return ", ".join(reply.labels)


def render_answers(
    pairs: list[tuple[ClarifyQuestion, Optional[ClarifyReply]]],
    notes: Optional[list[str]] = None,
) -> str:
    """
    把整次提问的作答渲染成回灌文本（spec F12）。

    :param pairs: [(问题, 作答或 None)]，按提问顺序。**只包含真的问过的那些**
        ——用户中途跳过之后剩下的问题压根没呈现，不该出现在这份清单里
    :param notes: 解析阶段的瑕疵说明，附在末尾
    :returns: 形如

        用户的回答：
        - 报告用什么格式？ → 摘要
        - 要包含哪几节？ → 概述, 结论建议
        - 配置文件放哪一层？ → 放到 docs 下面（用户自己输入的）
        - 用什么命名风格？ → （用户没有选择，请按你的最佳判断继续）

    ⚠ **刻意是一份人读的清单而不是 JSON**（spec 明确不做结构化回传）：
    省 token，而且 JSON 容易被模型误当成「答案要照这个格式回给用户」。

    副作用：无（纯函数）。
    """
    lines = ["用户的回答："]
    lines.extend(f"- {q.question} → {_render_reply(r)}" for q, r in pairs)

    # 有人跳过了 → 把「接下来该怎么办」说清楚，否则模型会原地再问一次。
    if any(r is None for _q, r in pairs):
        lines.append("")
        lines.append(SKIP_FEEDBACK)

    if notes:
        lines.append("")
        lines.extend(f"注意：{n}" for n in notes)
    return "\n".join(lines)
