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


# ---------------------------------------------------------------------------
# ask_user 的工具描述（ask-user 扩展 F19 / F20）
# ---------------------------------------------------------------------------
# ⚠⚠ **下面那八个示例是本工具唯一有效的触发手段，别当装饰删掉省 token。**
#
# 依据是 todo-list 扩展 2026-08-18 的真机复测（证据链在
# `docs/extensions/todo-list/acceptance-live.md`）：静态规则那条路**走到头了**
# ——能力描述 → 带可匹配条件的指令 → 每轮 `<system-reminder>`，三个杠杆逐个
# 加满，两个模型在一个明确五步的任务上仍是**各 0 次**调用；而补上 Claude Code
# 那种带 reasoning 的正反示例之后，`deepseek-v4-flash` 从 0 次变成 4/4 通过。
#
# 机制上的解释：**规则要模型做抽象判断，示例把它降级成模式匹配。**
#
# 三条不可动的结构：
# ① **正反各四个**。只给正例是单向推力，那正是已知项 #17 翻车的形态
#    （C13 委派口径为治欠触发写下四条推力，结果用户实测「一个非常简单的
#    任务都要让子 Agent 去做」）。
# ② **负例里必须留一个贴着下限的临界情形**——下面那条「pytest 还是 unittest」
#    形式上完全符合「有几种都说得通的做法」，只是答案一两次检索就能查到。
#    「用户刚说过的」那类模型本来就不会误判，全放那种等于白占篇幅。
# ③ **下限写成可数的**（「一两次」而不是「能自行确认的」）。已知项 #17 留下的
#    线索：模型对有具体可匹配项的指令遵循得好，对抽象判断系统性偷懒。
#
# ⚠ 本文本与 `prompt/texts/task_mode.py`、`prompt/texts/plan.py` **三处同口径**
# （成对维护点，护栏在 `tests/test_ask_user_trigger.py`）。
_ASK_USER_DESCRIPTION = """向用户提一到四个选择题，让他点选而不是手打一大段回答。你会拿到他选的答案，然后接着干活。

只在你**卡住**的时候用它：这个决定本来就该用户拍板，而你从他说的话里、从代码里、从常规默认做法里三处都定不下来，且选错了要返工。

**下面这四种一律不要用它**，自己拿主意然后在回答里说一句就行：
- 一两次只读检索（读文件、搜代码）就能查清的**事实**——去查，别问。
- 有约定俗成的**默认做法**、或跟着周围代码走就行的（命名、格式、目录结构）。
- 用户在这轮对话里**已经说过**的——翻回去看。
- 与当前任务无关的顺手事项——先记下来，别问。

用法：每个问题给 2 到 4 个都说得通的候选项，每项一句「选了会怎样」；最推荐的排第一，并在它的 label 末尾写「（推荐）」。用户看不到的第五项「其它…」由界面自动补上，不用你给。选项之间不互斥、可以多选时把 multiSelect 设为真。

<例子>
用户：给下载接口加个缓存。
判断：**该问**。进程内存与 Redis 是两条路，后续代码结构完全不同，选错要重写；而他偏好哪种，代码里读不出来。
</例子>

<例子>
用户：把这几个写死的值挪到配置文件里。
判断：**该问**。放项目级（跟着仓库走、团队共用）还是用户级（只对他生效、不进版本库），关系到别人会不会受影响——这是他要拍板的，不是我能替他定的。
</例子>

<例子>
用户：做个导出功能。
判断：**该问**，而且是多选。CSV / JSON / Excel 做哪几个是范围问题，做多了浪费、做少了返工，而需求里没写。
</例子>

<例子>
我在改 A 处时发现 B 处有同样的问题，用户只提了 A。
判断：**该问**。只改一半可能留下前后不一致，但擅自扩大范围又是明令禁止的——两种都说得通，得他定。
</例子>

<例子>
我需要知道这个项目用的是什么测试框架。
判断：**不要问**。这是事实不是取舍，看一眼 pyproject.toml 或 tests 目录就有答案。
</例子>

<例子>
我要新写一个测试，用 pytest 还是 unittest？两个都是合理的选择。
判断：**不要问**。它听起来像个取舍题，但**一两次**检索就能定——项目里已经有测试了，照它们的写法来。形式上像选择题、实际有唯一正确答案的，一律自己去查。
</例子>

<例子>
这个新函数的参数名用 camelCase 还是 snake_case？
判断：**不要问**。纯风格问题，有**默认做法**：跟着同一个文件里周围的代码走。
</例子>

<例子>
用户开头说「测试先别动」。我改完发现有个测试会挂。
判断：**不要问**。他**已经说过**了。按他说的不动，在回答里告诉他哪个测试会挂、为什么。
</例子>"""


class AskUserTool(Tool):
    """
    向用户提选择题的特殊工具（c4 spec F12 / ask-user 扩展）。

    模型卡在「有好几种都说得通的做法」上时调用它：一次给 1–4 个问题，
    每题 2–4 个候选项，每项含 label（选项名）与 description（选了会怎样）。
    实际执行由 Agent 循环拦截，逐题弹出澄清面板让用户选择，答案汇总回灌模型。

    ⚠ **可见性判据是「有没有人可问」，不是「在哪个阶段」**（ask-user 扩展 F1）。
    循环拿到了澄清回调就把本工具的 schema 发给模型，没拿到就不发——
    子 Agent（F2）与无人值守轮（F3）由此**结构性地**看不到它，
    不靠另立一张禁用清单。

    ⚠ 参数名对齐 Claude Code 的 `AskUserQuestion`（`questions` / `header` /
    `label` / `description` / `multiSelect`）：模型对那套名字有很强的先验，
    用它见过的名字能降低「参数名写错 → 解析不出 → 白问一轮」的概率。
    """

    name = ASK_USER
    description = _ASK_USER_DESCRIPTION
    parameters = {
        "type": "object",
        "properties": {
            "questions": {
                "type": "array",
                "description": "要问的问题，1 到 4 个。问题之间应当互相独立。",
                "items": {
                    "type": "object",
                    "properties": {
                        "question": {
                            "type": "string",
                            "description": "问题原文，一句完整的话",
                        },
                        "header": {
                            "type": "string",
                            "description": "这个问题问的是哪方面，最多 12 个字，"
                                           "会显示成问题前面的一枚小标签（如「配置位置」「导出格式」）",
                        },
                        "options": {
                            "type": "array",
                            "description": "候选项，2 到 4 个。最推荐的排第一，"
                                           "并在它的 label 末尾写「（推荐）」。"
                                           "不要自己加「其它」——界面会自动补一项让用户手打。",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "label": {
                                        "type": "string",
                                        "description": "选项名，一行短文本",
                                    },
                                    "description": {
                                        "type": "string",
                                        "description": "选了这项会怎样（一句话说清取舍）",
                                    },
                                },
                                "required": ["label"],
                            },
                        },
                        "multiSelect": {
                            "type": "boolean",
                            "description": "候选项之间不互斥、用户可以同时要好几个时设为 true；"
                                           "缺省为 false（只能选一个）",
                        },
                    },
                    "required": ["question", "options"],
                },
            },
        },
        "required": ["questions"],
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


def ask_schemas() -> list[dict]:
    """
    「有人可问」时要额外暴露给模型的特殊工具 schema（ask-user 扩展 F1）。

    由 Agent 循环在**拿到了澄清回调**时附加到工具 schema 之后一并发给模型，
    **与处于哪个阶段无关**。

    :returns: 只含 ask_user 一个 function 工具描述

    副作用：无（纯函数）。
    """
    return [AskUserTool().to_schema()]


def plan_schemas() -> list[dict]:
    """
    Plan Mode **规划阶段**要额外暴露给模型的特殊工具 schema。

    由 Agent 循环在「Plan Mode 且尚未获批执行」时附加。

    :returns: 只含 present_plan 一个 function 工具描述

    ⚠ **本函数与 `ask_schemas()` 的判据从此不同，这是刻意拆开的**
    （ask-user 扩展 F1）。原先它一次返回两个工具、共用「规划阶段」这一个判据；
    现在：

    - `present_plan` —— 只在**规划阶段**有意义（没有计划要审批的时候，
      提交计划这个动作不成立）；
    - `ask_user` —— 只要**有人可问**就有意义，任何阶段都一样。

    合成一个函数加布尔开关也能实现，但那样下一个人读到调用点时
    **看不出判据已经分了家**——他会以为两个工具仍然同进同出。

    副作用：无（纯函数）。
    """
    return [PresentPlanTool().to_schema()]
