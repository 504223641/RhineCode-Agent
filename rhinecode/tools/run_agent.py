"""
委派工具（c13 T23，spec F6）。

主 Agent 通过它把子任务交给独立上下文的子 Agent。**只暴露一个工具**，
用 `type` 参数分流定义式与分支式两条路径——因此不论加载了多少角色、
有没有后台任务在跑，模型看到的工具数量始终不变。

## 依赖说明

本模块 `import rhinecode.subagents`，而 `subagents` 又依赖 `rhinecode.tools`
（注册中心、Tool 抽象）。这是本项目的**第四组包级互相依赖**
（另三组：`tools ↔ skills`、`tools ↔ mcp`、`tools ↔ web`）。
**不成环的唯一依靠是 `rhinecode/tools/__init__.py` 保持为空。**
"""

from __future__ import annotations

from typing import Optional

from rhinecode.subagents.service import SubAgentService
from rhinecode.subagents.tasks import KIND_BRANCH, KIND_ROLE
from rhinecode.tools.base import Tool, ToolResult


class RunAgentTool(Tool):
    """
    把一个子任务委派给独立上下文的子 Agent。

    ## `system_serial = True` 的两条理由

    与 `LoadSkillTool` 同先例：

    1. **它会开一整条子对话**。放进只读并发桶意味着子 Agent 的执行会从
       线程池的工作线程里发生，而本工具的前台路径要阻塞等待——
       占着并发桶的槽位干等，会把同轮其它只读工具一起堵住。
    2. 该标志同时意味着**这次工具调用本身不进权限管线**。

    ## 不进权限管线的安全论证

    委派这个动作**本身不产生任何副作用**：它只是起一条子对话。
    副作用全部来自子 Agent 调用的工具，而那些调用**逐个**过完整的五层
    权限管线 + Hook 前置层（spec F25），一道都不少。

    更进一步，子 Agent 的能力**只会比主对话小**：
    - 工具集经三层过滤（`subagents/toolset.py`），委派工具与 Skill 加载工具
      永远不在其中；
    - 权限档位取 `min(主对话档, 角色声明档)`，声明放行档不产生提权效果；
    - 判 ASK 一律自动拒绝，且不继承回合级预授权。

    因此「模型能不能委派」不需要单独设一道闸——它委派出去也做不了
    自己直接做不了的事。

    另：本工具**有意不在 `permission/adapter.py` 的 `_TOOL_MAP` 中登记**，
    理由与 `load_skill` 相同——它既不读文件也不执行命令，没有可映射的
    Bash/Read/Edit/Write 语义。未登记的工具落进 `other` 分支，
    仍可用 `deny: run_agent` 整个禁掉。
    """

    name = "run_agent"
    system_serial = True
    read_only = False
    # Plan Mode 的规划阶段仍然开放——那恰恰是最需要把调研赶出主上下文的场景
    # （规划要读很多东西，而那些内容要一路背到执行阶段）。
    # ⚠ 声明它等于承诺「规划阶段不产生副作用」，兑现方式见 `execute` 的
    # `plan_stage` 分支：那时**只允许委派给最终工具集全只读的角色**。
    plan_safe = True

    # ## ⚠ 成对维护点：本描述与 `subagents/render.py` 的 `_INDEX_HEADER` 必须同口径
    #
    # 它们是模型决定「要不要委派」时读到的**唯一两处文本**。一处写得强、
    # 另一处写得弱，等于白改——模型会按弱的那份行事。
    # 护栏见 `tests/test_subagent_tool.py::SameVoiceTest`。
    #
    # ## 为什么措辞要「有点 pushy」
    #
    # C11 已经踩过一次：Skill 清单一开始写成公告式，实测模型**系统性欠触发**。
    # 委派面临同一个偏差且更严重——「自己动手」的默认倾向比「加载一份指令」强得多。
    # 四句与清单表头逐条对应：命中就委派 / 替代自己动手 / 用户不必点名 / 拿不准就委派。
    description = (
        "把一个子任务委派给独立上下文的子 Agent，只拿回它的结论。"
        "**手上的活属于某个角色覆盖的类型时，先调本工具委派出去，而不是自己动手做。**\n"
        "\n"
        "**尤其适合这类任务**：要读很多文件才能回答的调研、要翻遍代码库的定位、"
        "要跑一遍再汇总的检查。它们的中间过程对你毫无价值、却会占满你的上下文，"
        "而子 Agent 只回流一段结论。\n"
        "\n"
        "**用户不必明确说「让某个角色去做」**：判断依据是任务类型是否匹配，"
        "不是他有没有点名。\n"
        "\n"
        "**不要「先看一眼再决定」**——先 glob 一下、读两个文件「确认项目有多大」"
        "再判断要不要委派，是最容易犯的错：你一读，上下文就已经被占掉了，"
        "委派的价值当场归零。判断依据是**任务类型，不是项目大小**。\n"
        "\n"
        "拿不准要不要委派时**倾向委派**——该委派而没委派会让那些文件内容常驻你的"
        "上下文、之后每一轮都要重发；多委派一次只是多一次调用。\n"
        "\n"
        "**要改文件、而你手上也有未提交的改动时，设 `isolation: true`**——"
        "它会在一个独立的 Git 工作目录里跑，你俩同时改文件也不会互相覆盖；"
        "成果通过一个分支交回来，结论末尾会给出分支名。"
        "不涉及写文件的调研类任务不需要它（建工作区要 checkout 一整份源码）。\n"
        "\n"
        "⚠ **隔离的子 Agent 看到的是当前已提交的状态，你手上未提交的改动它看不到。**"
        "所以派出去的活要和你手上正在改的东西**落在不同文件、不同函数上**；"
        "碰同一处的话，要么先把你手上那份提交掉再派，要么就别派、自己接着改"
        "——否则两边各改各的，合并时必然冲突。\n"
        "\n"
        "⚠ **隔离委派的任务描述里绝不要写「不要提交」。** 隔离工作区是另一个"
        "工作现场，它的提交**不会**进入你这边的工作现场——`git commit` 是成果"
        "唯一的交付通道，禁止提交等于让它白干一场。拿到结论后照交付信息段里的"
        "分支名 `git merge`，**不要自己钻进那个目录去逐个读文件再重打一遍**，"
        "那会把隔离本该省下的上下文全部吃回来。\n"
        "\n"
        "两种类型：`role` 从空白对话起步、加载一个预定义角色（可用角色见系统提示里的清单）；"
        "`branch` 继承当前对话的历史与工具集、不需要角色，适合「接着刚才的分析继续挖」"
        "这类需要上下文的活，它**总是**在后台运行。\n"
        "\n"
        "任务描述必须**自包含**——子 Agent 看不到你和用户的对话，"
        "背景、目标、期望的产出都要写进去。\n"
        "\n"
        "转入后台时结论会在完成后**自动送达**，你不需要去取、也不要反复询问进度。"
    )

    parameters = {
        "type": "object",
        "properties": {
            "type": {
                "type": "string",
                "enum": [KIND_ROLE, KIND_BRANCH],
                "description": (
                    f"{KIND_ROLE}：委派给一个预定义角色，从空白对话起步；"
                    f"{KIND_BRANCH}：继承当前对话历史开一条分支，强制后台运行。"
                ),
            },
            "agent": {
                "type": "string",
                "description": f"角色名（`type={KIND_ROLE}` 时必填，取自系统提示里的角色清单）。",
            },
            "task": {
                "type": "string",
                "description": (
                    "交给子 Agent 的任务陈述。必须自包含：写清背景、要做什么、"
                    "期望产出什么形式的结论。"
                ),
            },
            "name": {
                "type": "string",
                "description": (
                    "给这个队员起的名字（可选，不给则自动生成）。"
                    "起了名字之后，你和其它队员就能用 send_message 按名字"
                    "跟它说话——**包括它干完之后**：再发一条消息就能把它"
                    "从原来的上下文唤醒继续干。"
                    "名字必须在本次会话内唯一，重名会直接失败。"
                ),
            },
            "isolation": {
                "type": "boolean",
                "description": (
                    "是否在独立的 Git 工作目录中运行（缺省 false）。"
                    "涉及写文件、而主对话手上可能有未提交改动时设为 true——"
                    "它的改动不会与你互相覆盖，成果经一个新分支交回。"
                    "它从**当前已提交的状态**起步，**看不到你未提交的改动**，"
                    "所以派给它的活要和你手上正在改的东西落在不同文件上。"
                    "注意：角色自己声明了隔离时，这里传 false **不生效**"
                    "（隔离只能加不能减）。"
                ),
            },
            "background": {
                "type": "boolean",
                "description": (
                    "是否直接转入后台（缺省 false，即先前台等一会儿）。"
                    "预计要跑很久、而你还有别的事可做时设为 true。"
                    f"`type={KIND_BRANCH}` 时本参数被忽略，总是后台。"
                ),
            },
        },
        "required": ["type", "task"],
    }

    def __init__(self, service: SubAgentService, parent_snapshot=None) -> None:
        """
        :param service: 子 Agent 服务门面
        :param parent_snapshot: 取父对话快照的回调（`type=branch` 用）。
            为 `None` 时分支式委派会失败并提示改用 `type=role`——
            这让本工具在没有协调层的环境（测试）里也能构造。
        """
        self._service = service
        self._parent_snapshot = parent_snapshot

    def execute(self, args: dict, plan_stage: bool = False) -> ToolResult:
        """
        发起一次委派。

        :param args: 模型给的参数
        :param plan_stage: 是否处于 Plan Mode 的**规划阶段**。由 Agent Loop 传入
            （`plan_safe` 工具的契约，见 `Tool.plan_safe`）。为真时只允许委派给
            最终工具集**全只读**的角色——Plan Mode 的承诺是「批准前不动手」，
            而一个能写文件的子 Agent 会直接绕过它。
            **缺省 False**，使不经循环的调用（测试、将来的其它调用方）
            按普通模式处理
        :returns: 统一的 `ToolResult`；`ok` 表示**是否成功发起**
            （转后台也算成功——子 Agent 自己失败与否由后续送达的结论说明）

        副作用：可能起一个后台线程跑一整条子对话（读写文件、执行命令，
        全部过完整权限管线与 Hook）；前台路径会阻塞至多 60 秒。

        本方法捕获自身全部异常并转成 `ok=False`——`Tool` 契约要求实现
        不得向上抛，否则 Agent Loop 会把它变成一条「工具执行异常」，
        丢掉服务层已经组织好的可读原因。
        """
        try:
            kind = str(args.get("type") or "").strip()
            agent_name = str(args.get("agent") or "").strip()
            task_text = str(args.get("task") or "")
            background = bool(args.get("background") or False)

            parent = None
            if kind.lower() == KIND_BRANCH and self._parent_snapshot is not None:
                parent = self._parent_snapshot()

            # c14 F14：`isolation` 取原值（可能是 None）而不是 bool(...)——
            # 「未表态」与「显式 false」在单向加严里都不能撤销角色的声明，
            # 但区分它们能让将来加「显式 false 时给一句提示」之类的行为有落点。
            outcome = self._service.delegate(
                kind, agent_name, task_text, background, parent,
                plan_stage=plan_stage,
                isolation=args.get("isolation"),
                # c15：队员名字。取原值（可能是 None）——`None` 表示
                # 「由系统起一个」，空串表示模型显式给了个空名字（那是错的，
                # 由花名册去报）。两者在 `register` 里的分支不同。
                name=args.get("name"),
            )
        except Exception as exc:  # noqa: BLE001
            return ToolResult(ok=False, output=f"委派失败：{exc}", summary="委派失败")

        return ToolResult(
            ok=outcome.ok,
            output=outcome.text,
            summary=_summary(outcome, kind, agent_name),
        )


def _summary(outcome, kind: str, agent_name: str) -> str:
    """
    工具行上显示的一句话。

    带上任务标识，让用户在界面上就能把这一行与后来的完成通知对上；
    没有标识的话，同时跑两个子 Agent 时那两条通知无从区分。
    """
    who = agent_name if kind == KIND_ROLE and agent_name else "分支"
    if not outcome.ok:
        return f"委派 {who} 失败"
    if outcome.backgrounded:
        return f"委派 {who} · 后台 [{outcome.task_id}]"
    return f"委派 {who} · 完成 [{outcome.task_id}]"


__all__ = ["RunAgentTool"]
