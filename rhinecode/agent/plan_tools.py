"""
Plan Mode 特殊交互工具（spec F12 / F13）。

这里的两个「工具」与普通工具（读写文件等）不同：它们只用来把「向用户提问 / 提交计划等待审批」
这件事，包装成模型能自然调用的工具协议。模型像调用普通工具一样调用它们，但它们的实际效果
不是读写文件，而是触发一次与用户的交互。

实现策略：
- 仅向模型「暴露 schema」（让模型知道有这两个工具、参数长什么样）。
- 真正的执行由 Agent 循环拦截（按名识别 ask_user / present_plan），路由到对应的回调
  （clarify / approve_plan）去弹面板、等用户选择，再把结果回灌模型。因此它们不进入 ToolRegistry，
  execute 也几乎不会被调用，仅保留兜底实现以防意外路径。

它们标记 read_only=True：本身不修改文件系统，且只在 Plan Mode 规划阶段开放。
"""

from rhinecode.tools.base import Tool, ToolResult

# 特殊工具名称常量。Agent 循环按这两个名字识别并拦截路由，不走 ToolRegistry。
ASK_USER = "ask_user"
PRESENT_PLAN = "present_plan"


class AskUserTool(Tool):
    """
    向用户澄清需求细节的特殊工具（对应 spec F12）。

    模型在规划阶段遇到不清楚的细节时调用它：给出一个问题与若干候选项，
    每个候选项含 summary（概述）与 detail（详细说明）；options 第一个应为最推荐项。
    实际执行由循环拦截，弹出澄清面板让用户选择，所选概述回灌模型。
    """

    name = ASK_USER
    description = (
        "在计划模式下向用户澄清不清楚的需求细节。给出一个问题 question 和若干候选项 options，"
        "每个候选项包含 summary（一句话概述）与 detail（详细说明与取舍）。"
        "请把你最推荐的候选项放在 options 列表的第一个。一次只问一个最关键的问题。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "question": {
                "type": "string",
                "description": "要向用户澄清的问题（一次一个，聚焦最关键的歧义点）",
            },
            "options": {
                "type": "array",
                "description": "候选项列表，第一个为最推荐项",
                "items": {
                    "type": "object",
                    "properties": {
                        "summary": {"type": "string", "description": "选项概述（一行短文本）"},
                        "detail": {"type": "string", "description": "选项详细说明与取舍"},
                    },
                    "required": ["summary"],
                },
            },
        },
        "required": ["question", "options"],
    }
    read_only = True

    def execute(self, args: dict) -> ToolResult:
        """兜底实现：正常路径下本工具由循环拦截，不会走到这里。"""
        return ToolResult(ok=False, output="ask_user 应由 Agent 循环拦截处理，不应直接执行。")


class PresentPlanTool(Tool):
    """
    提交计划等待用户审批的特殊工具（对应 spec F13）。

    模型调研与澄清完成后调用它，给出完整计划 plan；实际执行由循环拦截，
    弹出「是否开始执行」审批，批准后本轮进入执行阶段、放开全部工具。
    """

    name = PRESENT_PLAN
    description = (
        "在计划模式下提交你的完整执行计划，等待用户审批。给出 plan 字段（清晰、分步的计划文本）。"
        "即使需求很明确、无需澄清，也必须先用本工具提交计划并取得批准，才能开始执行。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "plan": {
                "type": "string",
                "description": "完整、分步的执行计划文本，供用户审阅后决定是否开始执行",
            },
        },
        "required": ["plan"],
    }
    read_only = True

    def execute(self, args: dict) -> ToolResult:
        """兜底实现：正常路径下本工具由循环拦截，不会走到这里。"""
        return ToolResult(ok=False, output="present_plan 应由 Agent 循环拦截处理，不应直接执行。")


def plan_schemas() -> list[dict]:
    """
    返回 Plan Mode 规划阶段要额外暴露给模型的特殊工具 schema 列表。

    由 Agent 循环在「Plan Mode 且尚未获批执行」时，附加到只读工具 schema 之后一并发给模型。

    :returns: ask_user、present_plan 两个 function 工具描述
    """
    return [AskUserTool().to_schema(), PresentPlanTool().to_schema()]
