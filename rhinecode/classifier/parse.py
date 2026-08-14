"""
分类器输出的解析（c16 F10/F12）：纯函数，零 IO。

两个阶段各有一个解析函数，它们的**失败语义刻意不同**，这是本模块最要紧的一点。

## ⚠ 第一阶段解析不出来 ≠ 判定失败

    parse_stage1 返回 None（看不懂）
        → 调用方按「可疑」处理 → **进第二阶段**

    parse_stage2 返回 None（看不懂）
        → 判定失败 → 未熔断时按拒绝处理（F12）

为什么这样分：第二阶段的提示词更明确、允许模型想清楚再说，它是一次**真正的
补救**。让第一阶段的一次输出格式抖动直接升级成一次拒绝，是拿用户的可用性
去换一个本来就有下一道工序的问题。

**这不是放宽。** 放行的路径**只有一条**：`parse_stage1` 明确返回 `False`。
看不懂、超时、抛异常，没有任何一种会走到放行。方向仍然是收紧。

## 为什么解析要宽松

模型不总是严格照格式输出——它可能加句号、加引号、加一句「好的，」，或者
把结论写成中文。解析器每严格一分，落到「看不懂」的概率就高一分，而那条路
通往「多跑一次第二阶段」（第一阶段）或「拒绝一次正常操作」（第二阶段）。
所以这里做归一化 + 关键词匹配，而不是要求逐字相等。
"""

from __future__ import annotations

import re
from typing import Optional

# 第一阶段期望的两个词。写成集合而不是单个字符串，是为了容纳模型的常见变体
# ——`allow` / `no` 在语义上等价于 `pass`，模型偶尔会自己换词。
_PASS_WORDS = frozenset({"pass", "allow", "ok", "no", "safe", "放行", "通过"})
_BLOCK_WORDS = frozenset({"block", "deny", "yes", "unsafe", "拦截", "拦下", "拒绝"})

# 归一化时要剥掉的东西：空白、常见标点、反引号与星号（模型爱用 markdown 强调）。
_STRIP_CHARS = " \t\r\n.。,，:：;；!！?？\"'`*·-—_[]()（）<>《》"

# 第二阶段的结论行。允许「结论」「结果」「verdict」「decision」几种写法，
# 冒号可以是中英文，后面允许有空格与 markdown 强调。
_VERDICT_LINE = re.compile(
    r"(?:结论|结果|判定|verdict|decision)\s*[:：]\s*[*`\s]*([A-Za-z一-鿿]+)",
    re.IGNORECASE,
)

# 第二阶段的理由行。同样允许几种写法。
_REASON_LINE = re.compile(
    r"(?:理由|原因|reason|because)\s*[:：]\s*(.+)",
    re.IGNORECASE | re.DOTALL,
)


def _normalize(text: str) -> str:
    """
    把一段模型输出归一化成便于比较的形态。

    :param text: 原始输出
    :returns: 去空白、去常见标点、转小写后的字符串

    副作用：无（纯函数）。
    """
    return str(text or "").strip().strip(_STRIP_CHARS).strip().lower()


def parse_stage1(text: str) -> Optional[bool]:
    """
    解析第一阶段（快速过滤）的输出。

    :param text: 模型输出全文
    :returns: `True` = 可疑（要进第二阶段）；`False` = 放行；
              `None` = **看不懂**（调用方按可疑处理，见模块 docstring）

    :raises: 不抛任何异常。

    副作用：无（纯函数）。

    ## 匹配策略

    先看整段归一化之后是不是恰好等于某个已知词（模型照格式输出时的情形），
    再退回「首行里出现了哪个词」。**不做全文搜索**——一段解释性文字里同时
    出现 pass 与 block 是很可能的，全文搜索会让结果取决于谁先出现。
    """
    normalized = _normalize(text)
    if not normalized:
        return None

    if normalized in _PASS_WORDS:
        return False
    if normalized in _BLOCK_WORDS:
        return True

    # 退回首行。取首行而不是全文，理由见上方 docstring。
    first_line = _normalize(normalized.splitlines()[0] if normalized.splitlines() else "")
    for word in first_line.replace("/", " ").split():
        token = word.strip(_STRIP_CHARS)
        if token in _PASS_WORDS:
            return False
        if token in _BLOCK_WORDS:
            return True

    return None


def parse_stage2(text: str) -> Optional[tuple[bool, str]]:
    """
    解析第二阶段（带理由的复核）的输出。

    :param text: 模型输出全文
    :returns: `(是否拦截, 理由)`；**看不懂时返回 `None`**（调用方按失败处理）

    :raises: 不抛任何异常。

    副作用：无（纯函数）。

    ## 理由缺失时不算失败

    结论解析出来了、理由没有，返回一句兜底文案而不是 `None`。
    理由是给用户看的补充信息，它缺失的代价是「用户看到的提示不够具体」；
    而判 `None` 的代价是「一次本该被拦下的动作因为格式问题走了失败路径」
    ——虽然失败也是拒绝，但记录里会归错类，排查时会以为是接口出了问题。
    """
    raw = str(text or "")
    if not raw.strip():
        return None

    match = _VERDICT_LINE.search(raw)
    if match:
        verdict_word = _normalize(match.group(1))
    else:
        # 没写「结论:」前缀时退回第一阶段那套判断——模型偶尔会直接甩一个词。
        flag = parse_stage1(raw)
        if flag is None:
            return None
        verdict_word = "block" if flag else "pass"

    if verdict_word in _PASS_WORDS:
        blocked = False
    elif verdict_word in _BLOCK_WORDS:
        blocked = True
    else:
        return None

    reason_match = _REASON_LINE.search(raw)
    if reason_match:
        reason = reason_match.group(1).strip()
    else:
        reason = ""
    if not reason:
        reason = "（分类器未给出具体理由）"

    return blocked, reason
