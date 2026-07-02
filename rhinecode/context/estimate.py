"""
近似 token 估算（c8 F2/N3）。

为什么不用精确 tokenizer：精确分词要引入模型相关依赖、且较慢。本项目的巧办法是
「锚点 + 增量」——

- 锚点：上一次 API 请求返回的 usage.prompt_tokens 是 API 亲口给出的**精确值**，
  它覆盖「上次发送过的那段历史（含 system 提示与 <system-reminder> 开销）」。
- 增量：只有锚点之后新追加的少量消息（通常几条 assistant/tool）需要估算，
  按字符数粗略换算即可。

因此估算误差被限制在「增量」这一小段上，配合窗口安全余量（13K/3K）足以吸收，
无需精确 tokenizer。CHARS_PER_TOKEN 取偏小值以「宁可高估、早触发压缩」，高估只会
让压缩提前发生（安全），低估才可能导致真正溢出（危险）。

本模块为纯函数，无任何 I/O 与状态。
"""

import json
from typing import Optional

from rhinecode.provider.base import Message

# 字符→token 近似比：取偏保守的小值。中英文/代码混排下真实比值差异大
# （英文约 4、代码 3~4、中文约 1~2），取 3.0 使整体略偏高估，避免低估导致溢出。
CHARS_PER_TOKEN: float = 3.0
# 每条消息除正文外的固定框架开销（角色标记、分隔符等）的近似 token 数。
MSG_OVERHEAD_TOKENS: int = 4


def estimate_message_tokens(msg: Message) -> int:
    """
    估算单条消息的近似 token 数。

    计入三部分：正文 content 字符、tool_calls 里每次调用的函数名与参数 JSON 字符、
    以及每条消息的固定框架开销 MSG_OVERHEAD_TOKENS。

    :param msg: 待估算的消息
    :returns: 近似 token 数（>=MSG_OVERHEAD_TOKENS）

    副作用：无。
    """
    chars = len(msg.content or "")
    # assistant 发起工具调用时，tool_calls 的函数名与参数也会占 token，需一并计入。
    if msg.tool_calls:
        for tc in msg.tool_calls:
            chars += len(tc.name or "")
            if tc.arguments is not None:
                # 用紧凑 JSON 近似参数序列化后的体量（真实请求也会把参数序列化发送）。
                chars += len(json.dumps(tc.arguments, ensure_ascii=False))
    return int(chars / CHARS_PER_TOKEN) + MSG_OVERHEAD_TOKENS


def estimate_tokens(
    history: list[Message],
    anchor_tokens: Optional[int],
    anchor_len: int,
) -> int:
    """
    估算整段历史的近似 token 数（锚点 + 增量）。

    - 无锚点（anchor_tokens 为 None，会话首个请求或摘要重构后）：对全部消息逐条估算求和。
    - 有锚点：以 anchor_tokens 为精确起点，只对 history[anchor_len:]（锚点后新增消息）
      逐条估算并累加。anchor_len 若因历史被截短而越界，按 min 兜底，避免负向切片错位。

    :param history: 当前完整对话历史
    :param anchor_tokens: 上次 API 返回的精确 prompt_tokens；None 表示无锚点
    :param anchor_len: 锚点覆盖的历史消息条数（record_usage 时记录的 sent_len）
    :returns: 近似 token 估算值

    副作用：无。
    """
    if anchor_tokens is None:
        return sum(estimate_message_tokens(m) for m in history)
    start = min(anchor_len, len(history))
    delta = sum(estimate_message_tokens(m) for m in history[start:])
    return anchor_tokens + delta
