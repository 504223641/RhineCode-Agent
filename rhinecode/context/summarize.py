"""
第二层·重量兜底的纯逻辑（c8 F9~F13）。

当整体历史逼近窗口上限时，用 LLM 把「较早的消息」压成结构化摘要，「近期的消息」保留原文。
本模块只放**纯逻辑与常量**（保留边界计算、转录、Prompt、草稿/正文解析、历史重构），
真正的 provider 调用与失败/熔断处理留在 manager.py——这样纯逻辑可脱离网络单测。

关键设计：
- 保留边界回退到最近的 role="user"：保证重构后的历史对 API 合法（不切碎消息、不拆散
  assistant(tool_calls) 与其 tool 结果），且保留区以 user 开头。
- 摘要请求把待摘要段渲染成「一条 user 转录文本」发出，规避裸 tool 消息缺配对的 API 校验。
- 边界消息做成一条合成 assistant 轮：既是 F12 的「重读文件」提示，又恢复 user→assistant→user
  角色交替，让重构后的历史合法。
"""

from typing import Optional

from rhinecode.provider.base import Message
from rhinecode.context.estimate import estimate_message_tokens

# 从尾部保留原文的目标 token 量的**上限**。
#
# 它曾经是个固定常量，直接当目标值用——那是一个已实测确认的缺陷（已知项 #8）：
# 配了小窗口（如 8192）的模型上，保留区目标比整个窗口还大，早段恒为空，
# **第二层摘要永远不会真正压缩**，而触发判据 `window - auto_margin` 已经是负数，
# 于是每一次请求都尝试压缩、每一次都以「无可摘要的早段」告终，历史一路涨到溢出。
#
# 现在改成「按窗口比例算，再夹在上限内」，见 `retain_budget`。
RETAIN_TOKENS_CAP: int = 10000

# 保留区占窗口的比例。取 0.16 使**默认窗口 65536 及以上逐字维持原行为**：
# 65536 × 0.16 = 10485 > 10000，被上限夹回 10000，与改造前完全一致。
# 小窗口才会真正生效：8192 × 0.16 ≈ 1310。
RETAIN_RATIO: float = 0.16


def retain_budget(window: int) -> int:
    """
    按窗口算出「尾部保留原文」的 token 预算。

    :param window: 上下文窗口上限（token），来自 `config.context_window`
    :returns: 保留区目标 token 量，恒 >= 1（避免非正窗口把预算算成 0）

    副作用：无（纯函数）。

    **为什么是比例而不是绝对值**：保留多少「近期原文」本质上是个相对量——
    在 64K 窗口里留 10K 原文是合理的（15%），在 8K 窗口里留 10K 就是荒谬的
    （比窗口还大）。绝对下限由 `MIN_RETAIN_MESSAGES` 兜底：无论 token 预算多小，
    至少留 5 条，近期上下文不会被压没。
    """
    return max(1, min(int(window * RETAIN_RATIO), RETAIN_TOKENS_CAP))
# 无论 token 多少，至少保留的尾部消息条数（保证近期上下文不被压没）。
MIN_RETAIN_MESSAGES: int = 5
# 草稿与正式摘要的分隔标记：模型先自由写分析草稿，再在此标记后写正式摘要。
SUMMARY_MARKER: str = "<<<正式摘要>>>"

# 摘要系统提示（F10 固定结构 + F11 禁工具 + 先草稿后正文）。
SUMMARY_SYSTEM_PROMPT: str = (
    "你是一个对话历史压缩器。你的唯一任务是把下面提供的一段较早的对话历史，"
    "压缩成一份忠实、结构化的摘要，供后续对话继续使用。\n\n"
    "严格约束：\n"
    "1. 禁止调用任何工具，也不要请求调用工具——你只能输出纯文本。\n"
    "2. 先写一段【分析草稿】：梳理这段历史里发生了什么、哪些信息必须保留。"
    "草稿只用于帮你理清思路，不会被保存。\n"
    f"3. 然后输出一行分隔标记 {SUMMARY_MARKER}，其后写【正式摘要】。\n"
    "4. 正式摘要必须忠实保留用户的原始诉求与关键决策，不得改写或臆造。\n"
    "5. 涉及具体文件内容或代码细节时，只概述「读过/改过哪个文件、结论是什么」，"
    "不要大段复制原文——后续需要细节会重新读取文件。\n\n"
    "正式摘要按以下固定五部分组织（用小标题分隔）：\n"
    "① 任务目标：用户想要达成什么。\n"
    "② 已完成的关键步骤与结论：做了哪些操作、得到哪些结果。\n"
    "③ 涉及的关键文件与改动：动过或读过的文件路径及其要点。\n"
    "④ 当前状态与待办：进行到哪一步、下一步要做什么。\n"
    "⑤ 重要约束与决策：必须遵守的限制、已敲定的技术选择。"
)

# 边界消息文本（F12）：合成的 assistant 轮，提示模型重读文件、勿照摘要脑补。
BOUNDARY_MESSAGE: str = (
    "以上是早前对话的结构化摘要。如需具体文件内容或代码细节，"
    "我会重新读取相关文件，绝不照摘要臆测代码。"
)


def compute_retain_index(history: list[Message], window: int) -> int:
    """
    计算「保留区」的起始下标：history[idx:] 保留原文，history[:idx] 交给摘要。

    步骤：
    1. 从尾部往前累加 estimate_message_tokens，直到累计 >= retain_budget(window)
       或已数满 MIN_RETAIN_MESSAGES 条——两条件谁先「让保留区更靠前（保留更多）」就用谁，
       即取更小的 idx。
    2. 把该 idx 回退到「<= idx 的最近一个 role='user' 消息下标」，保证保留区以 user 开头，
       从而不切碎消息、不拆散 assistant(tool_calls)↔tool 配对，重构后历史对 API 合法。
    3. 找不到任何 user（极端情况）→ 返回 0（即全部保留、无可摘要段，由上层按 noop 处理）。

    :param history: 当前对话历史
    :param window: 上下文窗口上限（token）。保留区预算由它按比例算出——
                   **不可省**：固定预算在小窗口下会让早段恒为空、第二层永不压缩
                   （已知项 #8 的实测缺陷）。
    :returns: 保留区起始下标 idx（0 表示没有可摘要的早段）

    副作用：无。
    """
    n = len(history)
    if n == 0:
        return 0

    budget = retain_budget(window)

    # —— 步骤 1：先按 token 从尾部回数，得到「按 token」的边界 idx_tok ——
    acc = 0
    idx_tok = n  # 若循环没提前 break，说明全部累加仍不足 budget，边界落在 0
    for i in range(n - 1, -1, -1):
        acc += estimate_message_tokens(history[i])
        if acc >= budget:
            idx_tok = i
            break
    else:
        idx_tok = 0

    # 按条数的边界：至少保留 MIN_RETAIN_MESSAGES 条。
    idx_cnt = max(0, n - MIN_RETAIN_MESSAGES)

    # 取更靠前者（保留更多原文）。
    idx = min(idx_tok, idx_cnt)

    # —— 步骤 2：回退到最近的 user 边界 ——
    # 找不到边界时本函数返回 0 = 「全部保留、无可摘要段」，语义与抽出该辅助函数前一致。
    boundary = snap_back_to_user(history, idx)
    return 0 if boundary is None else boundary


def snap_back_to_user(history: list[Message], idx: int) -> Optional[int]:
    """
    从 idx 向前回退到最近一个 `role == "user"` 的下标（c11 T35）。

    :param history: 消息列表
    :param idx: 起始下标
    :returns: 找到的 user 下标；**找不到、空序列、idx 越界一律返回 None**

    用途：任何「从某处切一刀」的场景都必须切在 user 边界上，否则会拆散
    `assistant(tool_calls)` 与其配对的 `tool` 消息，切出来的片段发给 API 会被拒。

    **为什么返回 `Optional[int]` 而不是用 0 当哨兵**——这是本函数存在的全部理由：
    两个调用方对「找不到边界」的正确反应**恰好相反**。

    - `compute_retain_index`（C8 摘要）：找不到边界 → 返回 0 = **全部保留**，
      这是安全的（什么都不摘要）。
    - `_take_tail`（C11 独立模式取尾部历史）：找不到边界 → 应当**不带入任何历史**，
      返回空列表。若沿用「返回 0」，下标 0 意味着 `history[0:]` = **整个主历史**——
      用户写 `history_messages: 3`，实际却把几百条消息全灌进子对话，
      既违背独立模式的目的，又因子对话关闭了第二层摘要而当场撑爆窗口。

    返回 None 强制每个调用方自己声明「找不到时该怎么办」，把这个反向 bug
    变成一个必须显式处理的分支。

    副作用：无。
    """
    if not history or idx < 0 or idx >= len(history):
        return None
    while idx > 0 and history[idx].role != "user":
        idx -= 1
    if history[idx].role != "user":
        return None
    return idx


def render_transcript(messages: list[Message]) -> str:
    """
    把待摘要消息渲染成一段纯文本转录，作为「一条 user 消息」发给摘要模型。

    渲染成转录而非直接转发原始消息，是为了规避 API 对 tool 消息「必须紧跟对应 tool_calls」
    的配对校验，同时让格式完全可控。不同角色加中文前缀；assistant 的工具调用与 tool 结果
    也如实转出（附 tool_call_id），供模型理解发生过哪些工具动作。

    :param messages: 待摘要的历史消息
    :returns: 转录文本
    """
    lines: list[str] = []
    for m in messages:
        if m.role == "user":
            lines.append(f"【用户】{m.content}")
        elif m.role == "assistant":
            text = m.content or ""
            if m.tool_calls:
                calls = ", ".join(f"{tc.name}({tc.id})" for tc in m.tool_calls)
                text = (text + f"\n（发起工具调用：{calls}）").strip()
            lines.append(f"【助手】{text}")
        elif m.role == "tool":
            lines.append(f"【工具结果·{m.tool_call_id}】{m.content}")
        else:  # system 等其它角色，原样带上角色名
            lines.append(f"【{m.role}】{m.content}")
    return "\n\n".join(lines)


def parse_summary(text: str) -> Optional[str]:
    """
    从摘要模型的完整输出里提取正式摘要，丢弃草稿（F11）。

    以 SUMMARY_MARKER 分割：取标记之后的最后一段并 strip；无标记则退化为整段 strip
    （宽松兜底：即便模型没按格式给标记，也不至于丢失摘要）。结果为空返回 None（视为失败）。

    :param text: 摘要模型输出的完整文本
    :returns: 正式摘要文本；无有效内容返回 None

    副作用：无。
    """
    if text is None:
        return None
    if SUMMARY_MARKER in text:
        # 取最后一次标记之后的内容，避免草稿里恰好也提到标记时误取。
        tail = text.rsplit(SUMMARY_MARKER, 1)[1].strip()
    else:
        tail = text.strip()
    return tail or None


def reconstruct(summary_text: str, retained: list[Message]) -> list[Message]:
    """
    用摘要 + 边界消息 + 保留区原文，重构出新的历史列表（F12）。

    结构固定为：
        [ user(结构化摘要), assistant(边界提示), *retained ]
    - 摘要作为一条 user 消息置顶，保证历史以 user 开头。
    - 边界提示作为合成 assistant 轮：兼作「重读文件」提示，并恢复 user→assistant→user
      角色交替（retained 以 user 开头），使重构后的历史对 API 合法。

    :param summary_text: 正式摘要正文
    :param retained: 保留区原文消息（应以 user 消息开头）
    :returns: 重构后的新历史列表

    副作用：无（返回新列表，由调用方决定如何原地替换）。
    """
    return [
        Message(role="user", content=f"[早前对话的结构化摘要]\n{summary_text}"),
        Message(role="assistant", content=BOUNDARY_MESSAGE),
        *retained,
    ]
