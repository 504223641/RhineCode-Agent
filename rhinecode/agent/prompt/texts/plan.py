"""
Plan Mode 提醒文案（供 reminders.py 引用）。

只存放 prompt 正文常量，不含逻辑。注入节奏（首轮/间隔重发完整版、其余精简版）由
rhinecode/agent/prompt/reminders.py 的 plan_toggle_instruction 决定。

命名说明：reminders.py 对外仍以 PLAN_FULL_INSTRUCTION / PLAN_BRIEF_INSTRUCTION 暴露，
本文件用更短的 PLAN_FULL / PLAN_BRIEF 作为「文案源」，由 reminders 重新绑定到公开名。
"""

# Plan Mode 完整版：开关激活首轮、及之后每隔 3 轮重发，完整交代「先调研/澄清、再 present_plan、获批后才执行」的流程。
PLAN_FULL = (
    "你现在处于「计划模式（Plan Mode）」。用户批准前你只能调研、不能做任何修改"
    "（不写文件、不改文件、不执行命令），当前也只开放了只读工具和计划交互工具。\n"
    "\n"
    "按以下流程工作：\n"
    "1. 先用只读工具（读文件、查找、搜索代码等）充分调研，理解现状、约束、风险和需求边界。\n"
    "2. 若需求有不清楚、有歧义或需要用户拍板的高影响细节，用 ask_user 工具逐一提问：一次一个问题、"
    "给出若干候选项，每个候选项含 summary（一句话概述）与 detail（详细说明与取舍），"
    "并把你最推荐的放在 options 第一个。不要询问可通过调研自行确认的事实。\n"
    "3. 调研与澄清完成后，用 present_plan 工具提交一份决策完整、可执行的计划（plan 字段）等待审批；"
    "计划要说明目标、关键改动、验证方式、风险与默认假设。\n"
    "4. 用户通过 present_plan 批准后才会开放全部工具，你才能开始执行；执行阶段仍需遵守工具确认与安全边界。\n"
    "\n"
    "即使需求看起来已很明确、无需澄清，也必须先用 present_plan 提交计划并获批，再执行。"
)

# Plan Mode 精简版：中间轮次的一句话「拍肩提醒」，避免每轮重复整段完整指令稀释注意力。
PLAN_BRIEF = (
    "提醒：仍处于计划模式，未获批准前只调研、不修改、不执行命令；完成调研后用 present_plan 提交计划等待审批。"
)
