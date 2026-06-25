"""
动态补充指令注入（c5 F8 / F9）。

本模块负责构造每一轮要追加到消息末尾的 <system-reminder> 文本，包含两部分：
1. 动态内容（环境信息 + 将来的可选模块）——由拼装器产出的 dynamic 段，每轮原样带上。
2. 会话级开关（目前是 Plan Mode）的提醒——按轮次控制强度（首轮完整、间隔重复、其余精简），
   以「省 token」和「防遗忘」之间取折中。

为什么用 <system-reminder> 标签：它让这些指令既不进入可缓存的稳定前缀（不污染缓存），
又能通过「系统约束」模块教会模型「带此标签的是系统补充上下文、不要当用户输入回复」（F8）。

注意：旧 rhinecode/agent/prompt.py 的 build_plan_prompt() 文本已迁移到这里的
PLAN_FULL_INSTRUCTION 常量，并新增了一句话的精简版 PLAN_BRIEF_INSTRUCTION。
"""

from typing import Optional

# Plan Mode 完整版指令：开关激活的首轮、以及之后每隔 3 轮重发一次，向模型完整交代规划流程。
PLAN_FULL_INSTRUCTION = (
    "你现在处于「计划模式（Plan Mode）」。在用户明确批准之前，你只能调研、不能执行任何"
    "修改类操作（不要写文件、改文件或执行命令），当前也只为你开放了只读工具。\n"
    "\n"
    "请按以下流程工作：\n"
    "1. 先用只读工具（读文件、查找文件、搜索代码等）充分调研，理解现状与需求。\n"
    "2. 如果需求中存在不清楚、有歧义或需要用户拍板的细节，使用 ask_user 工具逐一向用户提问："
    "每次提一个问题并给出若干候选项；每个候选项包含 summary（一句话概述）和 detail（详细说明与取舍）；"
    "把你最推荐的候选项放在 options 列表的第一个。\n"
    "3. 调研与澄清完成后，使用 present_plan 工具提交一份清晰的计划（plan 字段）等待用户审批。\n"
    "4. 只有当用户通过 present_plan 批准后，才会为你开放全部工具，你才能开始执行计划。\n"
    "\n"
    "即使需求看起来已经很明确、无需澄清，也必须先用 present_plan 提交计划并取得批准，再执行。"
)

# Plan Mode 精简版提醒：中间轮次只用一句话「拍肩提醒」，避免每轮重复整段完整指令稀释注意力。
PLAN_BRIEF_INSTRUCTION = (
    "提醒：仍处于计划模式，未获批准前只调研、不修改；完成调研后用 present_plan 提交计划等待审批。"
)


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
