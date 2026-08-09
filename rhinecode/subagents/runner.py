"""
子 Agent 运行器（c13 T15，spec F7/F8/F10/F11/F12/F15/F16/F17/F26）。

**职责**：在**独立线程**里跑完一个子 Agent，把结果写进 `TaskRecord`。

**不做的事**：不决定要不要跑（那是 `service` 的事）、不与 TUI 通信
（子 Agent 的过程不渲染，spec F23）、不碰主对话的任何状态。

## 为什么运行环境是一个显式数据类

`SubAgentRuntime` 把「跑一个子 Agent 需要的全部外部依赖」打包成参数注入。
换成让本模块 `import rhinecode.conversation` 会破坏架构不变量
（上层可依赖下层，反之不可）——`subagents` 会变成协调层的下游又是上游。

## 三处容易写错的地方（都有测试钉着）

1. **权限必须派生**（`engine.derive`），绝不能改主引擎的 `mode`——
   后台线程静默改掉主对话的权限档位，界面上完全看不出来。
2. **`hooks` 必须传进 `Agent` 构造**，否则用户的 `pre_tool_use` 拦截规则
   对子 Agent 静默失效，而主 Agent 可以靠委派绕过它。
3. **绝不调 `hooks.consume_injections()`**——那是个会被取走的队列，
   后台子 Agent 消费它会让主对话的注入型 Hook 凭空消失。
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Callable, Optional

from rhinecode.agent.events import AgentEventType, StopReason
from rhinecode.agent.loop import Agent, RunOptions
from rhinecode.permission.engine import PermissionEngine, narrower_mode
from rhinecode.permission.models import PermissionMode
from rhinecode.provider.base import BaseProvider, Message
from rhinecode.subagents.models import AgentSpec
from rhinecode.subagents.tasks import (
    BRANCH_AGENT_NAME,
    KIND_BRANCH,
    KIND_ROLE,
    TaskManager,
    TaskRecord,
    TaskStatus,
)
from rhinecode.subagents.toolset import ToolsetResult
from rhinecode.team.gate import TeamGate
from rhinecode.team.identity import bind_identity
from rhinecode.team.models import MemberState
from rhinecode.tools.path_guard import main_project_root
from rhinecode.trace import NullRecorder, TraceEventType, full_text, subagent_scope
from rhinecode.worktree import (
    WorktreeHandle,
    inspect as worktree_inspect,
    remove as worktree_remove,
    render_delivery,
)

# 非正常结束时回流给主对话的说明文本。
#
# **一律替换掉子历史里最后那段正文**（spec F11）：计划被拒或被取消时，
# 最后一条 assistant 可能带着非空的前言（「我打算这样做：……」），
# 只按「找最后一条非空 assistant」会把那段前言当成结论回流，
# 主 Agent 会以为任务完成了。C11 的 `_ISOLATED_FAILURE_TEXT` 是同一个坑。
_FAILURE_TEXT = {
    StopReason.MAX_ITERATIONS: (
        "子 Agent 用完了自己的轮次预算（{turns} 轮）仍未得出结论。"
        "任务可能过大，建议拆小之后再委派，或换一个轮次上限更高的角色。"
    ),
    StopReason.USER_CANCELLED: "子 Agent 被取消，未产出结论。",
    StopReason.UNKNOWN_TOOL: (
        "子 Agent 反复调用不存在的工具而停止。"
        "多半是角色的 tools 白名单与它被要求做的事对不上。"
    ),
    StopReason.STREAM_ERROR: (
        "子 Agent 的模型请求出错，未产出结论。"
        "若任务很大，可能是它自己的上下文超了窗口——建议拆小后重试。"
    ),
    StopReason.PLAN_REJECTED: "子 Agent 的计划被拒绝，未产出结论。",
}

_UNEXPECTED_TEXT = "子 Agent 运行时出错，未产出结论：{error}"

# c15：待命时检查取消信号的间隔（秒）。
#
# 队员要同时响应「收到消息」与「被取消」两件事，而标准库没有「等多个 Event
# 中任意一个」的原语。用短超时轮询而不是起一条监视线程——后者是把一个简单
# 问题复杂化，还多一条要管生命周期的线程。
#
# 这**不是** spec N6 禁止的那种轮询：它发生在一条已经空闲的线程上，
# 0.2 秒醒一次的 CPU 占用可以忽略。N6 禁的是「没有队员时也持续烧 CPU」。
_WAKE_POLL_INTERVAL = 0.2

# 被唤醒之后那一轮的任务描述。
#
# 刻意**不**去复述唤醒它的那条消息：消息由闸门在第一轮迭代注入，模型看得到
# 全文；在这里再抄一遍只会让 `/agents` 的任务行变得又长又重复。
_WOKEN_TASK_TEXT = "（被消息唤醒，带着原有上下文继续）"

# c15：任务终态 → 队员终态。
#
# 两套状态回答不同的问题（「结论产出了没有」vs「人还在不在场」），
# 但队员**退场**时两者必须对得上，否则 `/agents` 会同时显示
# 「任务失败」与「队员已完成」。
#
# 缺省 `DONE`（自然干完但不保留上下文）——只有隔离委派会走到那里，
# 它与待命互斥，理由见 `run_subagent` 里 `can_idle` 的注释。
_MEMBER_STATE_FOR_TASK = {
    TaskStatus.COMPLETED: MemberState.DONE,
    TaskStatus.FAILED: MemberState.FAILED,
    TaskStatus.CANCELLED: MemberState.CANCELLED,
}

# 所有子 Agent 都要遵守的两条约定，注入每个子 Agent 的 <system-reminder>。
#
# ## 为什么放在这里，而不是写进每个角色的正文
#
# 它们是**产品级的事实**，与角色是谁无关；写进正文的话，用户自己写的角色
# 一个都盖不到（内置那三个写得再全也没用）。放在运行器里，任何角色——包括
# 从别处复制来的——都自动带上。
#
# ## 第一条：语言
#
# 子 Agent 的系统提示**只有角色正文**（spec F7 照 Claude Code 口径），
# 因此 `RHINE.md` 里的「用中文回答」这类项目约定**到不了它**。
# 真实模型验收实测到：一个中文项目里的 planner 开口就是
# "I now have complete knowledge of the codebase."
#
# 写「用与任务描述相同的语言」而不是写死「用中文」：任务描述由主 Agent 生成，
# 而主 Agent 拿得到 RHINE.md、也在用用户的语言说话，于是这条对任何语言都成立。
#
# ## 第二条：长度
#
# 结论会**整段进入主对话的上下文**。委派本来就是为了省上下文，回流一份万字
# 长文会把收益吃掉一大半——实测过一次 9776 字符的方案。
# 角色正文只说了「最后一段是唯一会被带回去的」（强调自包含），
# 没说它**有代价**，模型于是没有精简的动机。
SUBAGENT_CONVENTIONS = (
    "<subagent-conventions>\n"
    "两条对你同样有效的约定：\n"
    "1. **用与任务描述相同的语言作答**（任务描述是中文就用中文）。\n"
    "2. 你的最终结论会**整段进入主对话的上下文**并占用它的预算，因此要精炼："
    "把结论压到必要的长度，**不要贴改动前后的完整代码对照**，"
    "用「文件:行 → 一句话」代替；过程叙述与自我陈述"
    "（「我现在已经完全了解了代码库」这类）一律不要写进结论。\n"
    "</subagent-conventions>"
)


@dataclass(frozen=True)
class ParentSnapshot:
    """
    分支式委派用的父对话快照（spec F8）。

    **快照而非引用**：子 Agent 跑起来之后主对话还会继续追加消息，
    共享同一个列表会让子 Agent 的历史在它跑到一半时被改动——
    那既破坏消息协议顺序，也让「它到底看到了什么」无法复现。

    :param history: 父历史的**副本**
    :param stable: 父对话的稳定系统提示
    :param tool_names: 父对话当前可见的工具名
    """

    history: tuple[Message, ...]
    stable: str
    tool_names: tuple[str, ...]


@dataclass(frozen=True)
class SubAgentRuntime:
    """
    跑一个子 Agent 所需的全部外部依赖，由协调层构造后注入。

    :param provider_for: 模型名 → Provider。传 `None` 取主对话的默认 Provider。
        复用协调层已有的缓存，避免每个子 Agent 都新建一个客户端。
    :param registry: 工具注册中心（共享）
    :param engine: **主**权限引擎。运行器据它 `derive` 出子引擎，绝不修改它。
    :param main_mode: 取主对话当前权限档位的回调。**必须是回调而不是取值**——
        用户可能在子 Agent 排队期间按 `/perm` 切档，取值型会用上一个陈旧的档位。
    :param hooks: Hook 编排者（共享）
    :param recorder: 行为记录器（共享；写入端已加锁，作用域是 threading.local）
    :param new_context_manager: **每次调用返回一个新的** ContextManager。
        它持有 `_anchor_tokens` / `_circuit_broken` 等可变状态且无锁，
        并发共享会互相污染估算锚点。新建实例很轻，只共享存盘目录。
        为 `None` 时子 Agent 不跑任何上下文压缩（测试与非工具模式）。
    :param environment_text: 取环境信息段的回调，**入参是本次子 Agent 的工作目录**
        （c14 修正）。**刻意不给默认值**——与 `PermissionRequest.cwd` 同一条理由：
        给了默认值等于「忘记传的地方静默按主项目根算」，而隔离子 Agent 拿到
        主项目根的环境信息，会与 `<isolated-workspace>` 段自相矛盾。
        实测后果：模型把工作区路径当成相对主项目根的路径去拼
        （`.rhinecode/worktrees/<名字>/calc/x.py`），连读几次都落空，白烧轮次。
    :param untrusted_section: 「外部不可信内容」段原文。**只在子 Agent 的
        最终工具集含网络访问工具时才注入**（spec F7 的例外）。
    :param default_model: 主对话模型名，角色未指定 `model` 时用它
    :param thinking_effort: 思考强度，继承主对话
    :param network_tool_names: 哪些工具名算「能联网」。用集合而不是硬编码
        `"web_fetch"`，是为了将来加 web_search 时只改这一处。
    :param team: 协作服务门面（c15）。`None` 表示协作能力未启用——
        那时子 Agent 跑完即结束，没有队友消息注入、没有待命与唤醒，
        行为与 C13/C14 **逐字一致**（spec N5 零回归的落点）。
    """

    provider_for: Callable[[Optional[str]], BaseProvider]
    registry: object
    engine: PermissionEngine
    main_mode: Callable[[], PermissionMode]
    environment_text: Callable[[str], str]
    default_model: str = ""
    hooks: object = None
    recorder: object = None
    new_context_manager: Optional[Callable[[], object]] = None
    untrusted_section: str = ""
    thinking_effort: str = "off"
    team: object = None
    network_tool_names: frozenset = field(
        default_factory=lambda: frozenset({"web_fetch"})
    )


def _deny_ask(tool_call, tool, decision) -> bool:
    """
    子 Agent 的人工确认回调：恒拒绝。

    它**实际上不会被调用**——`RunOptions.interactive=False` 让 Agent Loop 在
    判 ASK 时直接走非交互拒绝分支，压根不碰这个回调。留着是纵深防御：
    万一将来有人改动那条分支，这里也不会把面板弹到某个后台线程上去。
    """
    return False


def _resolve_mode(
    runtime: SubAgentRuntime, spec: Optional[AgentSpec]
) -> PermissionMode:
    """
    算出子 Agent 的实际生效权限档位（spec F16）。

    :returns: `min(主对话档位, 角色声明档位)`；角色未声明时直接取主对话档位

    这是「只能收紧不能放宽」的落点：角色写 `permission_mode: permissive`
    在默认档的主对话下仍然只拿到默认档。
    """
    main = runtime.main_mode()
    if spec is None or spec.permission_mode is None:
        return main
    return narrower_mode(main, spec.permission_mode)


def _isolation_notice(handle: "WorktreeHandle") -> str:
    """
    告诉子 Agent 它在一个隔离工作区里（spec F15）。

    ⚠ **这段与 `SUBAGENT_CONVENTIONS` 并列追加，不写进角色正文。**
    与那条既有的成对维护点同一条理由：写进正文的话，用户自己写的角色一个都
    盖不到——而隔离是**运行环境**的事实，与角色是谁无关。

    内容上刻意只讲三件事：在哪、在哪个分支、成果怎么交。**不讲「你出不去」**
    ——出不出得去是权限管线第②层的事，不靠模型自觉；写进提示反而像在暗示
    「这里有条边界可以试探」。
    """
    base = f"（基于 {handle.base_commit}）" if handle.base_commit else ""
    return (
        "<isolated-workspace>\n"
        "你运行在一个**独立的 Git 工作目录**中，与主对话及其它子 Agent 完全隔离。\n"
        f"- 工作目录：{handle.path}\n"
        f"- 所在分支：{handle.branch}{base}\n\n"
        "你的一切文件读写与命令执行都发生在这个目录里，"
        "不会影响主对话正在编辑的文件。\n"
        "**改完之后请把工作提交到当前分支**（`git add` + `git commit`）——"
        "成果是通过这个分支交回去的；没有提交的改动只留在目录里，"
        "不会随分支被合并走。\n"
        "</isolated-workspace>"
    )


def _team_notice(member_name: str, team, can_idle: bool) -> str:
    """
    告诉队员它在团队里的身份与协作方式（c15 F1/F9/F13）。

    :param member_name: 它自己的名字
    :param team: 协作服务门面
    :param can_idle: 它干完之后会不会留在场上待命
    :returns: 一段要并进 dynamic 的说明

    ⚠ **与 `SUBAGENT_CONVENTIONS`、`_isolation_notice` 并列追加，
    不写进角色正文。** 与那两条同一条理由：写进正文的话，用户自己写的角色
    一个都盖不到——而「你叫什么、队友有谁」是**运行环境**的事实，
    与角色是谁无关。

    ⚠ **必须每轮重算**（放在 `dynamic` 里而不是 `stable` 里）：
    队友名单会变——新队员随时可能被派进来，旧的可能已经退休。
    放进可缓存的 `stable` 会让一个队员按一份陈旧的名单去发消息，
    收到的全是「查无此人」。

    ## ⚠ 「等回复时该收工待命」是真实模型验收补上的

    实测撞到：`impl-worker` 需要先从 `spec-writer` 拿到一份规范才能开工，
    它发完请求之后**反复调 `task_list` 当轮询**在等——空转六次，
    烧掉 106K token、几乎耗尽 15 轮预算（对照组 `spec-writer` 只用了 7 轮 43K）。
    如果对方再慢一点，它会**耗尽轮次而失败**。

    根因是本段原先只说了「做完手上的事就收尾」，**没说「等别人回话时也该收尾」**。
    而收尾恰恰是本章设计好的等待方式：待命零成本，对方回话时自动唤醒、
    上下文一个字不丢。模型不知道这条，就只能用它熟悉的方式——轮询。

    副作用：无（只读花名册）。
    """
    peers = [
        m.name
        for m in team.members()
        if m.name != member_name and not m.state.is_terminal
    ]
    peer_line = (
        f"- 现在在场的还有：{'、'.join(peers)}\n" if peers else "- 目前只有你和主对话\n"
    )
    # ⚠ 第二段是**真实模型验收补的**，见函数 docstring 的「等回复空转」那一条。
    idle_line = (
        "**你自然结束之后不会消失**，而是留在场上待命——"
        "别人再发一条消息就能把你叫醒、带着现在的全部上下文接着干。"
        "所以做完手上的事就正常收尾，不必为了「保持在线」硬撑着多跑几轮。\n"
        "\n"
        "⚠️ **在等别人回话才能往下做时，也请直接收尾。** 你会进入待命，"
        "对方回话的那一刻你就被叫醒、带着现在的全部上下文接着干，"
        "**什么都不会丢**。\n"
        "**不要靠反复查清单、反复看文件来「等」**——消息不是查出来的，"
        "它会自己出现在你眼前。空转只会烧光你的轮次预算，"
        "等对方真回话时你已经没有轮次可用了。\n"
        if can_idle
        else ""
    )
    return (
        "<team>\n"
        f"你在这个团队里的名字是 **{member_name}**。\n"
        f"{peer_line}"
        "- 主对话叫 `main`，它是唯一在跟用户对话的一方\n"
        "\n"
        "**你的正文输出别的 Agent 看不到。** 要跟谁说话就调 send_message，"
        "按名字发。消息会自动送到对方眼前，对方不需要查收，你也不需要——"
        "别人发给你的消息会自己出现在你这里。\n"
        "\n"
        "**任务清单是大家共用的一份。** 认领之前先看它现在有没有被别的任务挡着；"
        "做完一条就立刻标成完成——别人正等着它解锁自己的活。"
        "没真做完就别标完成（测试还红着、只写了一半、卡住了），"
        "那种情况应该保持进行中，并把实情发消息告诉相关的人。\n"
        f"{idle_line}"
        "</team>"
    )


def _build_prompts(
    runtime: SubAgentRuntime,
    spec: Optional[AgentSpec],
    task_text: str,
    toolset: ToolsetResult,
    parent: Optional[ParentSnapshot],
    handle: "Optional[WorktreeHandle]" = None,
    member_name: str = "",
    can_idle: bool = False,
) -> tuple[str, Callable[[], str], list[Message]]:
    """
    组装子 Agent 的系统提示与初始历史（spec F7/F8）。

    :returns: `(stable, dynamic 回调, 初始历史)`

    **定义式**：`stable` = 角色正文，历史只有一条任务描述。
    照 Claude Code 的口径——子 Agent 只拿自己的系统提示 + 基本环境信息，
    **不拿主对话的八模块结构化提示**。

    **一处例外**：最终工具集含网络访问工具时追加「外部不可信内容」段。
    缺了它，子 Agent 能抓到投毒页面却没有那道约束——这正是 web_fetch 扩展
    里踩过的坑（两个注入点漏一个就静默失效，界面上完全看不出来）。

    **分支式**：`stable` 与历史都取父快照，任务描述作为最后一条 user 消息追加。

    ⚠ `dynamic` 里**绝不调 `hooks.consume_injections()`**：那是个会被**取走**的
    队列，后台子 Agent 去消费它，会让一条挂在 `turn_start` 上的注入型 Hook
    从主对话里凭空消失——不报错、不留痕，只是用户的 Hook 偶尔不生效。

    副作用：无。
    """
    needs_untrusted = bool(
        runtime.untrusted_section and (toolset.allowed & runtime.network_tool_names)
    )

    # c14 F15：隔离说明。一次算好而不是每轮重算——句柄是不可变的。
    isolation_notice = _isolation_notice(handle) if handle is not None else ""

    # c14 修正：环境信息段必须报**本次子 Agent 的工作目录**，不是主项目根。
    # 这是 CLAUDE.md 那条「cwd 的分发点」在提示词侧漏掉的一处：
    # 权限管线、工具执行、Hook 子进程都已按工作区判定，唯独告诉模型的那句话没跟上，
    # 于是同一份系统提示里出现两个互相矛盾的「工作目录」。
    # 一次算好：句柄不可变，主项目根在一次运行内也不变。
    agent_cwd = str(handle.path) if handle is not None else str(main_project_root())

    def dynamic() -> str:
        parts = [runtime.environment_text(agent_cwd), SUBAGENT_CONVENTIONS]
        if isolation_notice:
            parts.append(isolation_notice)
        # c15：团队说明**每轮重算**——队友名单会变（新人随时被派进来、
        # 旧人可能已退休）。放进 `stable` 会让它按一份陈旧的名单发消息。
        if member_name and runtime.team is not None:
            parts.append(_team_notice(member_name, runtime.team, can_idle))
        if needs_untrusted:
            parts.append(runtime.untrusted_section)
        return "\n\n".join(p for p in parts if p)

    if parent is not None:
        history = list(parent.history)
        history.append(Message(role="user", content=task_text))
        return parent.stable, dynamic, history

    stable = spec.body if spec is not None else ""
    return stable, dynamic, [Message(role="user", content=task_text)]


def _extract_conclusion(
    history: list[Message], stop_reason: StopReason, turns: int
) -> tuple[str, bool]:
    """
    从子历史里取出要回流的结论（spec F11）。

    :returns: `(结论文本, 是否成功)`

    正常完成时取**最后一条非空 assistant 正文**。

    `stop_reason != COMPLETED` 时**一律**替换为说明文本——这个条件不可省：
    被取消或计划被拒时，最后一条 assistant 可能带着非空的前言
    （「我打算这样做：……」），只按「找最后一条非空 assistant」会把那段前言
    当成结论回流，主 Agent 会以为任务完成了。

    副作用：无。
    """
    if stop_reason is not StopReason.COMPLETED:
        template = _FAILURE_TEXT.get(stop_reason, "子 Agent 未产出结论。")
        return template.format(turns=turns), False

    for msg in reversed(history):
        if msg.role == "assistant" and (msg.content or "").strip():
            return msg.content.strip(), True

    # 自然完成却一个字都没说——理论上不该发生，但兜住它比让主 Agent
    # 拿到一段空文本好：空结论在界面上表现为「任务完成了，但什么都没有」。
    return "子 Agent 自然结束，但没有产出任何正文。", False


def _emit_end(
    recorder, record, agent_label: str, status, turns: int, stop_reason, conclusion: str
) -> None:
    """
    埋一条「子 Agent 结束」事件。**吞掉一切异常。**

    抽成函数是因为 c15 之后它有**两个调用点**：待命循环里每一轮结束时一次、
    最终收尾时一次。两处漏一处的表现是「有些轮次在 trace 里没有结束事件」，
    而时间线看起来只是少了一行、不报错。

    副作用：写 trace（失败静默——观测设施绝不能反过来影响被观测的系统）。
    """
    try:
        recorder.emit(
            TraceEventType.SUBAGENT_END,
            task_id=record.task_id,
            agent=agent_label,
            status=status.value,
            turns=turns,
            usage_tokens=record.usage_tokens,
            stop_reason=stop_reason.value,
            conclusion=full_text(conclusion),
        )
    except Exception:  # noqa: BLE001 —— 观测设施绝不能反过来影响被观测的系统
        pass


def _await_wake(team, member_name: str, record) -> "Optional[list[Message]]":
    """
    待命：阻塞等一条消息，被唤醒后取回保管的历史（c15 F13）。

    :param team: 协作服务门面
    :param member_name: 队员名字
    :param record: 当前任务记录（用它的取消信号）
    :returns: 保管的历史；**不可唤醒时 `None`**（被取消 / 被降级 / 会话清空）

    ## 为什么是「带超时的轮询」而不是纯 `Event.wait()`

    队员要同时响应两件事：**收到消息**（`wake_event`）与**被取消**
    （`record.cancel_event`，来自 `Esc` / `/agents cancel` / 会话切换）。
    标准库没有「等多个 Event 中的任意一个」的原语，而为此起一条监视线程
    是把一个简单问题复杂化。

    因此用短超时轮询：每 0.2 秒醒一次看看取消信号。这**不是** spec N6
    禁止的那种轮询——它发生在一个已经空闲的线程上，CPU 占用可以忽略，
    而且待命队员本来就在等一件不知道何时发生的事。真正被 N6 禁掉的是
    「没有队员时也持续烧 CPU」，那种情况这里根本不会进来。

    副作用：阻塞调用线程；成功唤醒时改花名册状态。
    """
    entry = team.member(member_name)
    if entry is None:
        return None
    event = entry.wake_event

    while True:
        if record.cancel_event.is_set():
            team.mark_terminal(member_name, MemberState.CANCELLED)
            return None
        if event.wait(_WAKE_POLL_INTERVAL):
            # 醒来了。可能是收到消息，也可能是被降级 / 会话清空叫醒来退出的
            # ——`wake()` 返回 None 就是后者（花名册已经不认它了）。
            return team.wake(member_name)


def _next_round_record(tasks, kind: str, agent_label: str, member_name: str):
    """
    为「被唤醒后的这一轮」新建一条任务记录（c15 F13）。

    :returns: 新的 `TaskRecord`

    ## 为什么每轮一条记录，而不是复用第一条

    一条任务记录 = 一段交付出去的结论。复用的话，第二轮跑完时那条记录
    已经是终态了，`tasks.finish` 不会再产生交付，**主 Agent 永远看不到
    队员被唤醒之后做了什么**。

    新记录一律 `awaited=False`：主 Agent 没有「委派」这一轮，
    不该在收工前为它停下来等。结论仍会在下一轮迭代注入它的历史。

    副作用：往任务表加一条记录。
    """
    record = tasks.create(kind, agent_label, _WOKEN_TASK_TEXT)
    record.member_name = member_name
    record.awaited = False
    return record


def run_subagent(
    runtime: SubAgentRuntime,
    spec: Optional[AgentSpec],
    task_text: str,
    record: TaskRecord,
    tasks: TaskManager,
    toolset: ToolsetResult,
    all_tool_names: frozenset,
    parent: Optional[ParentSnapshot] = None,
    handle: "Optional[WorktreeHandle]" = None,
) -> None:
    """
    跑完一个子 Agent（本函数就是后台线程的 target）。

    :param runtime: 外部依赖
    :param spec: 角色定义；`None` 表示分支式
    :param task_text: 任务描述
    :param record: 任务记录，结果全部写进它
    :param tasks: 任务表，用于 `bump` / `finish`
    :param toolset: 已算好的最终工具集
    :param all_tool_names: 注册中心里的全部工具名（算 `excluded_tools` 用）
    :param parent: 分支式的父快照
    :param handle: 隔离工作区句柄（c14）。非 `None` 时本子 Agent 的一切文件读写
        与命令执行都发生在 `handle.path` 内，结束时按变更情况保留或删除
    :returns: 无。结果看 `record`

    执行步骤：

    1. 绑定 trace 作用域（`threading.local`，并发子 Agent 天然互不干扰）；
    2. 埋 `subagent_start`；
    3. 组装系统提示与初始历史；
    4. 派生权限引擎（`min(主档, 角色档)`，`turn_rules` 为空）；
    5. 构造 `Agent`（**必须传 `hooks`**）并跑 `Agent.run`；
    6. 消费事件流，只累计轮次与用量、捕获 `stop_reason`，**不转发给任何人**；
    7. 取结论；
    8. 埋 `subagent_end`，`tasks.finish`。

    副作用：发起多次 API 请求；执行工具（可能写文件、跑命令，全部过完整权限
    管线与 Hook）；写 trace；改 `record`。

    **不抛任何异常**：后台线程的异常无人接管，逃逸出去会让任务永远停在
    「运行中」、前台等待方永远等不到 `done_event`。全部转成失败状态。
    """
    recorder = runtime.recorder or NullRecorder()
    agent_label = spec.name if spec is not None else BRANCH_AGENT_NAME
    kind = KIND_ROLE if spec is not None else KIND_BRANCH
    turns = 0
    stop_reason = StopReason.COMPLETED
    status = TaskStatus.COMPLETED

    # ── c15：协作身份与待命资格 ──
    member_name = record.member_name
    team = runtime.team if member_name else None

    # ⚠ **隔离的子 Agent 不进入待命**，这是实现期定下的一条边界。
    #
    # 理由是「工作区什么时候结算」没有第二个说得通的答案：
    # - 跑完就结算：无改动的工作区会被回收（c14 F16），而它一旦被叫醒
    #   就没有目录可写了——权限管线第②层会把它的每一次写入都拒掉；
    # - 推迟到最后再结算：第一轮的结论里就没有分支名，而主 Agent 正是靠
    #   那段交付信息去 `git merge` 的（c14 踩过「成果搁浅」那次事故）。
    #
    # 因此隔离与待命互斥：隔离委派保持 C14 的语义（跑完 → 交付分支 → 结束）。
    # 需要同一个人接着干下一件事时，重新委派一次即可——它的成果在分支上，
    # 不会丢。
    can_idle = team is not None and handle is None

    if member_name:
        # ⚠ 线程本地身份：协作工具全进程共享一份实例，发件人与认领人
        # 只能从这里取。漏绑不报错，只会让这个队员以 `main` 的身份
        # 发消息和认领任务——表现是「worker-a 认领的任务显示成 main 认领的」。
        bind_identity(member_name)

    try:
        # ① 本线程从现在起属于该作用域。用 bind_scope 而非 with scope(...)：
        #    整个线程的生命周期就是这一次运行，不需要「出来自动恢复」。
        # c15 修正：**有队员名字时用名字，没有才退回角色名**。
        #
        # 真实模型验收撞到过：同一个角色派出两个队员（spec-writer 与
        # impl-worker 都是 general-purpose），两者的 trace 事件全落在
        # `subagent:general-purpose` 这一个作用域里**混成一片**，
        # 排查「这一步是谁做的」只能靠时间戳和内容猜。
        recorder.bind_scope(subagent_scope(member_name or agent_label))

        recorder.emit(
            TraceEventType.SUBAGENT_START,
            kind=kind,
            agent=agent_label,
            task_id=record.task_id,
            task=full_text(task_text),
            tool_count=len(toolset.allowed),
            tools=sorted(toolset.allowed),
            model=(spec.model if spec is not None else None) or runtime.default_model,
            max_turns=spec.max_turns if spec is not None else None,
            # ⚠ 以下四项都是「这次委派实际在什么条件下跑」，缺了就没法从记录复现。
            #
            # `member_name` —— 作用域已经用它了，但作用域是个字符串前缀，
            #   要把「队员」与「角色」对上还得再猜一次（同一个角色可以派出多个队员）。
            # `cwd` / `isolated` —— C14 的隔离是**物理的**（第②层按这个目录量边界）。
            #   不记的话，「隔离到底生效没有」在记录上完全看不出来，
            #   而 c14 成对维护点里最容易漏的恰恰是「读落到主项目根、写却是对的」。
            # `permission_mode` —— C13 的核心承诺是「取 min(主对话档, 角色声明档)，
            #   声明放行档不产生提权」。**实际生效的那个档位此前一处都没记**，
            #   只能去 `/agents` 报告里看当下状态，事后无从复核。
            member=member_name or None,
            cwd=str(handle.path) if handle is not None else str(main_project_root()),
            isolated=handle is not None,
            permission_mode=_resolve_mode(runtime, spec).value,
        )

        stable, dynamic, history = _build_prompts(
            runtime, spec, task_text, toolset, parent, handle,
            member_name=member_name, can_idle=can_idle,
        )

        # ④ 权限派生。**绝不改 runtime.engine.mode**——那是主对话的档位。
        sub_engine = runtime.engine.derive(_resolve_mode(runtime, spec))

        provider = runtime.provider_for(spec.model if spec is not None else None)

        # ⑤ hooks 必须传：工具级三事件由 Agent 内部分发，传了就自动对子 Agent 生效。
        #    漏传不报错，只是用户的 pre_tool_use 拦截规则静默失效，
        #    而主 Agent 可以靠「委派出去」绕过它。
        agent = Agent(
            provider,
            runtime.registry,
            recorder=recorder,
            hooks=runtime.hooks,
        )

        context_manager = (
            runtime.new_context_manager() if runtime.new_context_manager else None
        )
        max_turns = spec.max_turns if spec is not None else None

        # ── c15：待命循环 ──
        #
        # C13/C14 时这里是一次性的：跑一遍 `agent.run` 就结束。现在改成外层
        # 循环——队员**自然停止**后不销毁，而是留在场上待命，一条消息就能
        # 把它从原来的上下文唤醒继续干（spec F13）。
        #
        # `can_idle` 为假时循环只走一圈，行为与 C13/C14 **逐字一致**。
        #
        # `turns_base` 让 `/agents` 上的轮次是**累计**的，而模型每次被唤醒
        # 拿到的是**完整的**迭代预算（spec F16）：后者由 `agent.run` 自己
        # 从 1 开始计数天然成立，前者靠这个偏移量补上。
        # 不加偏移的话，一个被唤醒三次的队员在界面上永远显示「跑了 2 轮」。
        turns_base = 0
        while True:
            events = agent.run(
                history,
                runtime.thinking_effort,
                False,                  # plan_mode：子 Agent 非交互，没人能审批计划
                stable,
                dynamic,
                (spec.model if spec is not None else None) or runtime.default_model,
                None,                   # 不写缓存调试日志
                sub_engine,
                _deny_ask,
                None,                   # clarify：问不了人
                None,                   # approve_plan：没人审批
                record.cancel_event,
                context_manager,
                None,                   # recorder=None：子 Agent 的消息不进会话存档
                options=RunOptions(
                    max_iterations=max_turns or RunOptions().max_iterations,
                    record_usage=False, # 别拿子 Agent 的 usage 污染主历史锚点
                    allow_summary=False,# 只跑 C8 第一层（工具结果存盘）
                    # 注册中心里除最终工具集之外的一律排除：既不发给模型，
                    # 调用了也拒绝（spec F13 的第二半）。
                    excluded_tools=frozenset(all_tool_names) - toolset.allowed,
                    interactive=False,  # 判 ASK 直接拒，见 spec F15
                    # c14 F1/F2：隔离工作区就是本次运行的工作目录。
                    # 它同时决定三件事：工具的路径解析基准、权限管线第②层的
                    # 沙箱边界、以及工具级 Hook 命令的执行目录。
                    # `None` 时循环取主项目根，行为与 c14 之前逐字一致。
                    cwd=handle.path if handle is not None else None,
                    # c15：队友消息的注入口。`None` 时循环用 NullGate 兜底，
                    # 与 C13/C14 逐字一致。
                    subagent_gate=(
                        TeamGate(team, member_name) if team is not None else None
                    ),
                ),
            )

            # ⑥ 消费事件流但**不转发**（spec F23：子 Agent 的过程不渲染）。
            for event in events:
                if event.type == AgentEventType.PROGRESS:
                    turns = turns_base + (event.iteration or 0)
                    tasks.bump(record.task_id, turns=turns)
                elif event.type == AgentEventType.USAGE and event.usage is not None:
                    tasks.bump(
                        record.task_id, tokens=getattr(event.usage, "total_tokens", 0)
                    )
                elif event.type == AgentEventType.FINISHED:
                    stop_reason = event.stop_reason or StopReason.COMPLETED

            conclusion, ok = _extract_conclusion(history, stop_reason, turns)
            if not ok:
                status = (
                    TaskStatus.CANCELLED
                    if stop_reason is StopReason.USER_CANCELLED
                    else TaskStatus.FAILED
                )
                # 失败 / 被取消 → 终态，不待命（spec F14）
                if team is not None:
                    team.mark_terminal(
                        member_name,
                        MemberState.CANCELLED
                        if stop_reason is StopReason.USER_CANCELLED
                        else MemberState.FAILED,
                    )
                break

            if not can_idle:
                break

            # ── 自然停止且可待命：交付这一轮的结论，然后等消息 ──
            #
            # ⚠ **必须现在就 `finish`。** 任务记录停在「运行中」的话，
            # 主 Agent 的闸门会认为「还有委派没回来」而在收工前一直等它，
            # 而这个队员正等着主 Agent 发消息——**双方互等，永远结束不了**。
            # 这就是 plan 决策 2 说的那个雷的另一半：状态维度必须分开，
            # 「结论产出了」（TaskStatus）与「人还在场」（MemberState）
            # 各走各的。
            _emit_end(recorder, record, agent_label, status, turns, stop_reason, conclusion)
            tasks.finish(record.task_id, status, conclusion, stop_reason.value)

            for notice in team.mark_idle(member_name, history):
                # N3 降级必须看得见（通知由 TUI 的既有轮询取走）
                _ = notice

            woken = _await_wake(team, member_name, record)
            if woken is None:
                # 被取消 / 被降级 / 会话清空 —— 已经 finish 过了，直接退出
                return

            history = woken
            turns_base = turns
            record = _next_round_record(tasks, kind, agent_label, member_name)
            stop_reason = StopReason.COMPLETED
            status = TaskStatus.COMPLETED

    except BaseException as exc:  # noqa: BLE001
        # 后台线程的异常无人接管。逃逸出去 = 任务永远「运行中」+ 等待方永远阻塞。
        status = TaskStatus.FAILED
        stop_reason = StopReason.STREAM_ERROR
        conclusion = _UNEXPECTED_TEXT.format(error=exc)

    # ⑧ 隔离工作区结算（c14 F16/F17）。
    #
    # ⚠ **必须排在 `tasks.finish` 之前**：`finish` 会置 `done_event`，
    # 而那是「这条真的结束了」的信号——测试与闸门都以它为同步点。
    # 放在它之后的话，交付信息会在结论已经交出去之后才拼好，主 Agent 拿到的
    # 是一段没有分支名的结论。
    #
    # ⚠ **整段用 try 兜住，绝不向上抛**。它跑在后台线程上，异常逃逸的后果是
    # 任务永远停在「运行中」、等待方永远阻塞。git 命令失败、目录被外部删掉、
    # 磁盘满——任何一种都不该让一次已经跑完的委派变成永久挂起。
    if handle is not None:
        try:
            status_info = worktree_inspect(handle)
            removed = False
            if status_info.untouched:
                # 什么都没改 → 目录与分支一并回收（spec F16）。
                verdict = worktree_remove(
                    main_project_root(), handle.path, handle.branch, status_info
                )
                removed = verdict.allowed
            # 让 `/agents` 知道这个工作区已经没了（c14 修正）。**纯内存赋值**，
            # 与 service 里设 path/branch 同形态，不进 TaskManager 的临界区。
            record.worktree_removed = removed
            conclusion = (
                conclusion
                + "\n\n"
                + render_delivery(
                    handle, status_info, main_project_root(), removed=removed
                )
            )
            try:
                recorder.emit(
                    TraceEventType.WORKTREE_SETTLE,
                    name=handle.name,
                    removed=removed,
                    dirty=status_info.dirty,
                    commits=status_info.commits,
                    # 保留了目录时分支必然还在；删了目录则看是否留分支。
                    keep_branch=(not removed) or status_info.commits > 0,
                )
            except Exception:  # noqa: BLE001 —— 观测设施绝不能反过来影响被观测的系统
                pass
        except Exception as exc:  # noqa: BLE001
            # 结算失败不影响结论本身——但要如实说一句，否则用户会看到一个
            # 隔离任务却完全没有工作区信息，以为是隔离没生效。
            conclusion = (
                conclusion
                + "\n\n── 隔离工作区 ─────────────────────\n"
                + f"结算时出错，工作区已保留在 {handle.path}"
                + (f"（分支 {handle.branch}）" if handle.branch else "")
                + f"：{exc}"
            )

    # ⑨ 收尾。埋点与 finish 都在 try 之外，保证任何路径都会执行到。
    _emit_end(recorder, record, agent_label, status, turns, stop_reason, conclusion)

    # c15：走到这里意味着这个队员**不再待命**（失败 / 被取消 / 不具备待命
    # 资格 / 出了未预期的异常）。把它从花名册上转成终态，否则别人还会看到
    # 一个「运行中」的名字、给它发消息，而没有任何线程会来处理。
    #
    # ⚠ 用 `getattr` 取门面而不是直接用上面的 `team` 局部变量：本段在
    # `except BaseException` **之外**，而那个分支可能在 `team` 赋值之前
    # 就被触发（比如 `_build_prompts` 抛异常）。
    if member_name and getattr(runtime, "team", None) is not None:
        runtime.team.mark_terminal(
            member_name,
            _MEMBER_STATE_FOR_TASK.get(status, MemberState.DONE),
        )

    tasks.finish(record.task_id, status, conclusion, stop_reason.value)


def start_subagent_thread(
    runtime: SubAgentRuntime,
    spec: Optional[AgentSpec],
    task_text: str,
    record: TaskRecord,
    tasks: TaskManager,
    toolset: ToolsetResult,
    all_tool_names: frozenset,
    parent: Optional[ParentSnapshot] = None,
    handle: "Optional[WorktreeHandle]" = None,
) -> threading.Thread:  # noqa: D401 —— 见下方 docstring
    """
    起一个 daemon 线程跑 `run_subagent`。

    :returns: 已 start 的线程对象（调用方通常不需要它，返回是为了测试可 join）

    daemon=True：进程退出时不等它们。spec 明确不做跨会话持久化，
    退出即丢弃；非 daemon 会让 `/exit` 卡住等一个可能要跑几十秒的子 Agent。

    副作用：起线程。
    """
    thread = threading.Thread(
        target=run_subagent,
        args=(
            runtime, spec, task_text, record, tasks, toolset,
            all_tool_names, parent, handle,
        ),
        name=f"subagent-{record.task_id}",
        daemon=True,
    )
    thread.start()
    return thread


__all__ = [
    "ParentSnapshot",
    "SubAgentRuntime",
    "run_subagent",
    "start_subagent_thread",
]
