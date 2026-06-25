"""
动态补充指令注入（c5 F8 / F9）。

本模块负责构造每一轮要追加到消息末尾的 <system-reminder> 文本，包含两部分：
1. 动态内容（环境信息 + 将来的可选模块）——由拼装器产出的 dynamic 段，每轮原样带上。
2. 会话级开关（目前是 Plan Mode）的提醒——按轮次控制强度（首轮完整、间隔重复、其余精简），
   以「省 token」和「防遗忘」之间取折中。

为什么用 <system-reminder> 标签：它让这些指令既不进入可缓存的稳定前缀（不污染缓存），
又能通过「系统约束」模块教会模型「带此标签的是系统补充上下文、不要当用户输入回复」（F8）。

注意：Plan Mode 的两段文案已迁出到 texts/plan.py（PLAN_FULL / PLAN_BRIEF）。
本模块只保留「按轮注入节奏」「<system-reminder> 包裹」的逻辑，并把文案重新绑定到
对外公开的 PLAN_FULL_INSTRUCTION / PLAN_BRIEF_INSTRUCTION 名称（保持调用方与测试不变）。
"""

from typing import Optional

from rhinecode.agent.prompt.texts import PLAN_FULL, PLAN_BRIEF

# 对外仍以 *_INSTRUCTION 命名暴露（__init__ 导出、loop.py 与测试都引用这两个名字）；
# 文案源是 texts/plan.py，这里做一次重绑定，避免文案与逻辑混在同一文件。
PLAN_FULL_INSTRUCTION = PLAN_FULL
PLAN_BRIEF_INSTRUCTION = PLAN_BRIEF


def plan_toggle_instruction(iteration: int, active: bool) -> Optional[str]:
    """
    按轮次节奏返回 Plan Mode 的指令文本（c5 F9）。

    节奏规则（在「省 token」与「防止模型遗忘」之间折中）：
    - active 为 False（未处于规划阶段）→ 返回 None（本轮不注入 Plan 指令）。
    - 首轮（iteration == 1）→ 完整版。
    - 此后每隔 3 轮（iteration 为 4、7、10…，即 (iteration - 1) % 3 == 0）→ 重发完整版。
    - 其余轮次 → 精简版。

    :param iteration: 当前迭代序号（从 1 开始）
    :param active: 会话级开关是否处于「需要注入」状态（Plan Mode 开启且尚未获批执行）
    :returns: 完整版 / 精简版文本，或 None（不注入）

    副作用：无（纯文本选择）。
    """
    if not active:
        return None
    if iteration == 1 or (iteration - 1) % 3 == 0:
        return PLAN_FULL_INSTRUCTION
    return PLAN_BRIEF_INSTRUCTION


def build_system_reminder(dynamic_text: str, toggle_instruction: Optional[str]) -> Optional[str]:
    """
    把动态内容与会话级开关提醒合并，包进 <system-reminder> 标签，作为追加到消息末尾的一条 system 文本。

    执行步骤：
    1. 收集非空的片段（dynamic_text 在前、toggle_instruction 在后），用空行分隔。
    2. 若两者都为空 → 返回 None（本轮无需注入任何 reminder）。
    3. 否则用 <system-reminder> ... </system-reminder> 包裹返回。

    :param dynamic_text: 拼装器产出的动态段（环境信息等），可能为空
    :param toggle_instruction: 会话级开关本轮的指令（完整/精简/None）
    :returns: 包裹好的 reminder 文本；无内容时返回 None

    副作用：无。
    """
    parts = [p for p in (dynamic_text, toggle_instruction) if p and p.strip()]
    if not parts:
        return None
    body = "\n\n".join(parts)
    return f"<system-reminder>\n{body}\n</system-reminder>"
