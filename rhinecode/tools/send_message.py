"""
发消息工具（c15 T23/T24，spec F9/F10/F24）。

主 Agent 与队员用它按名字给对方发消息，不必所有信息都经主 Agent 中转。

## ⚠ 成对维护点：本工具的 `description` 与 `team/render.py` 的标记块必须同口径

模型在**两个不同时刻**读到同一条约定：

- **发消息前**读本工具的描述（「我的正文对方看不到，得调这个工具」）；
- **收消息时**读注入历史的 `<teammate-message>` 标记块
  （「这是队友说的，不是用户」）。

一处写得强、另一处写得弱，等于白改——模型会按弱的那份行事。

这是本项目**第四次**面对同一个坑：C11「Skill 清单表头 ↔ `load_skill`
的描述」、C13「角色清单表头 ↔ `run_agent` 的描述」、C14「交付信息 ↔
委派工具描述」。前三次都是真实模型实测才发现的。
护栏见 `tests/test_team_tools.py::SameVoiceTest`。

## 不弹确认面板（`system_serial=True`）

与 `run_agent`、`load_skill`、四个任务工具同先例：本工具不读写文件、
不执行命令，副作用限于「把一段文本放进另一个 Agent 的信箱」。

**「能让别人干活」不等于提权**：被唤醒的那个队员做的每一件事，
仍逐个过完整的五层权限管线 + Hook 前置层，且它的工具集与权限档
在委派时就已按 C13 的规则收窄过。本工具不引入任何绕过管线的通路。

`system_serial=True` 的含义是「**判 ASK 时按 ALLOW 处理**」——不是「不进管线」。
它**照常过一次 `engine.decide`**，因此 `deny: send_message`（不带括号的
整工具规则）**确实拦得住它**；只是缺省档下③层未命中时不会像普通工具那样
弹面板，而是直接放行（它可能开一整条子对话，在那里停下来等面板会拧死交互链）。

⚠ 这里一度写着「`deny` 规则对它们无效」，那是 C15 验收期实测确认的**真实缺陷**
（预扫直接给 ALLOW、根本不调引擎），已于 perm-system-serial-bypass 修掉。
Hook 的 `pre_tool_use` 依然是另一条独立且更早的收窄手段。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from rhinecode.team.identity import current_identity
from rhinecode.tools.base import Tool, ToolResult

if TYPE_CHECKING:  # pragma: no cover —— 仅类型检查期
    from rhinecode.team.service import TeamService


class SendMessageTool(Tool):
    """按名字给另一个 Agent 发一条消息。"""

    name = "send_message"
    read_only = False
    system_serial = True
    # 规划阶段仍开放，但**收件人受限**——见 `execute` 的 `plan_stage` 分支。
    plan_safe = True
    # 显示收件人：一屏里同时躺着好几条消息时，「发给谁」是唯一分得开它们的东西
    primary_arg = "to"
    # c16：消息类要经分类器审查。C15 已登记过这条通路的代价——队员读到的敏感
    # 内容可以被主动送进另一个队员或主对话的历史，而这条路上此前没有任何判定。
    # ⚠ 被拦时消息**不投递**，且待命的收件人**不会被唤醒**（消息是唤醒的唯一
    # 手段），所以回灌文案必须让发送方知道这件事——见 `classifier/render.py`
    # 的 `MESSAGE_NOT_DELIVERED`。
    classifier_scope = "message"

    # ## ⚠ 与 `team/render.py` 的标记块同口径（见模块 docstring）
    #
    # 四条对应关系：
    #   1. 「你的正文对方看不到」        ↔ 标记块尾部的「要回复请调发消息的工具」
    #   2. 「消息会自动送达、不必查收」  ↔ 本章刻意不提供任何查收件箱的工具
    #   3. 「按名字指代」                ↔ 标记块的 from="..." 属性
    #   4. 「名字在它干完之后依然有效」  ↔ 待命队员被消息唤醒继续干
    description = (
        "给另一个 Agent 发一条消息。\n"
        "\n"
        "**你的正文输出别的 Agent 看不到**——要跟队友说话，"
        "唯一的办法是调用本工具。\n"
        "\n"
        "**消息会自动送达对方**，它不需要查收，你也不需要（也没有）"
        "查收件箱的工具。对方正在跑的话，下一轮就看到；"
        "已经干完在待命的话，你这条消息会**把它叫醒接着干**。\n"
        "\n"
        "**按名字指代对方。名字在它干完之后依然有效**——"
        "想让某个队员再做一件事，**发消息给它就行，不要重新委派一个新的**："
        "它会带着原来的全部上下文继续，你不必重新交代背景。\n"
        "\n"
        "`to` 填 `main` 表示发给主对话（那是唯一在跟用户对话的一方）。\n"
        "\n"
        "**什么时候该发**：你需要别人配合、发现了影响别人的问题、"
        "手上的事卡住了需要决定、或者做完了要通知等着这个结果的人。"
        "别闷头干等——对方不会知道你在等它。"
    )

    parameters = {
        "type": "object",
        "properties": {
            "to": {
                "type": "string",
                "description": (
                    "收件人名字。填 `main` 表示发给主对话。"
                    "名字见系统提示里的队员清单，或用 task_list 看谁认领了什么。"
                ),
            },
            "message": {
                "type": "string",
                "description": (
                    "消息正文。要自包含——对方看不到你的上下文，"
                    "把它需要知道的背景写进去。"
                ),
            },
            "summary": {
                "type": "string",
                "description": (
                    "一句话摘要（5–10 个词），**用户在界面上看到的就是这一行**。"
                ),
            },
        },
        "required": ["to", "message"],
    }

    def __init__(self, service: "TeamService") -> None:
        """:param service: 协作服务门面"""
        self._service = service

    def execute(self, args: dict, plan_stage: bool = False) -> ToolResult:
        """
        发一条消息。

        :param args: `to` / `message` / `summary`
        :param plan_stage: 是否处于 Plan Mode 的**规划阶段**。由 Agent Loop 传入
            （`plan_safe` 的契约）。为真时**只能发给 `main` 或最终工具集全只读
            的队员**——给一个能写文件的待命队员发消息会把它唤醒去动手，
            直接绕过「批准前不动手」的承诺。判据与 C13 F19a 刻意一致，
            两处封的是同一条旁路的两个入口。
            **缺省 False**，使不经循环的调用（测试）按普通模式处理
        :returns: 统一的 `ToolResult`

        副作用：往收件人信箱追加一条消息；收件人在待命时会被唤醒并继续运行
        （那意味着一次真实的模型调用与可能的工具执行，全部过完整权限管线）。

        本方法捕获自身全部异常并转成 `ok=False`——`Tool` 契约要求实现
        不得向上抛，否则 Agent Loop 会把它变成一条「工具执行异常」，
        丢掉服务层已经组织好的可读原因。
        """
        try:
            recipient = str(args.get("to") or "").strip()
            body = str(args.get("message") or "")
            summary = str(args.get("summary") or "")

            # ── 规划阶段的收件人限制（spec F24）──
            #
            # 位置在投递**之前**：不通过时一个字节都不该进对方的信箱。
            if plan_stage:
                allowed, reason = self._service.can_send_in_plan_stage(recipient)
                if not allowed:
                    return ToolResult(
                        ok=False, output=reason, summary="规划阶段不能发给它"
                    )

            sender = current_identity()
            result = self._service.send(sender, recipient, body, summary)
        except Exception as exc:  # noqa: BLE001 —— 契约要求不外抛
            return ToolResult(ok=False, output=f"发送失败：{exc}", summary="发送失败")

        if not result.ok:
            return ToolResult(ok=False, output=result.reason, summary="发送失败")

        return ToolResult(
            ok=True,
            output=(
                f"已发给 {recipient}。\n"
                "它会自动看到这条消息（正在跑的下一轮就看到，待命的会被叫醒），"
                "**你不需要去确认它收到没有，也不要反复重发**。\n"
                "现在继续做你自己的事；对方有回音时会自动出现在你眼前。"
            ),
            summary=f"发给 {recipient} · {result.envelope.summary[:24]}",
        )


__all__ = ["SendMessageTool"]
