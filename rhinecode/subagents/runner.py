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
from rhinecode.tools.path_guard import main_project_root
from rhinecode.trace import NullRecorder, TraceEventType, clip, subagent_scope
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


def _build_prompts(
    runtime: SubAgentRuntime,
    spec: Optional[AgentSpec],
    task_text: str,
    toolset: ToolsetResult,
    parent: Optional[ParentSnapshot],
    handle: "Optional[WorktreeHandle]" = None,
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

    try:
        # ① 本线程从现在起属于该作用域。用 bind_scope 而非 with scope(...)：
        #    整个线程的生命周期就是这一次运行，不需要「出来自动恢复」。
        recorder.bind_scope(subagent_scope(agent_label))

        recorder.emit(
            TraceEventType.SUBAGENT_START,
            kind=kind,
            agent=agent_label,
            task_id=record.task_id,
            task=clip(task_text),
            tool_count=len(toolset.allowed),
            tools=sorted(toolset.allowed),
            model=(spec.model if spec is not None else None) or runtime.default_model,
            max_turns=spec.max_turns if spec is not None else None,
        )

        stable, dynamic, history = _build_prompts(
            runtime, spec, task_text, toolset, parent, handle
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

        events = agent.run(
            history,
            runtime.thinking_effort,
            False,                      # plan_mode：子 Agent 非交互，没人能审批计划
            stable,
            dynamic,
            (spec.model if spec is not None else None) or runtime.default_model,
            None,                       # 不写缓存调试日志
            sub_engine,
            _deny_ask,
            None,                       # clarify：问不了人
            None,                       # approve_plan：没人审批
            record.cancel_event,
            context_manager,
            None,                       # recorder=None：子 Agent 的消息不进会话存档
            options=RunOptions(
                max_iterations=max_turns or RunOptions().max_iterations,
                record_usage=False,     # 别拿子 Agent 的 usage 污染主历史锚点
                allow_summary=False,    # 只跑 C8 第一层（工具结果存盘）
                # 注册中心里除最终工具集之外的一律排除：既不发给模型，
                # 调用了也拒绝（spec F13 的第二半）。
                excluded_tools=frozenset(all_tool_names) - toolset.allowed,
                interactive=False,      # 判 ASK 直接拒，见 spec F15
                # c14 F1/F2：隔离工作区就是本次运行的工作目录。
                # 它同时决定三件事：工具的路径解析基准、权限管线第②层的
                # 沙箱边界、以及工具级 Hook 命令的执行目录。
                # `None` 时循环取主项目根，行为与 c14 之前逐字一致。
                cwd=handle.path if handle is not None else None,
            ),
        )

        # ⑥ 消费事件流但**不转发**（spec F23：子 Agent 的过程不渲染）。
        for event in events:
            if event.type == AgentEventType.PROGRESS:
                turns = event.iteration or turns
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
    try:
        recorder.emit(
            TraceEventType.SUBAGENT_END,
            task_id=record.task_id,
            agent=agent_label,
            status=status.value,
            turns=turns,
            usage_tokens=record.usage_tokens,
            stop_reason=stop_reason.value,
            conclusion=clip(conclusion),
        )
    except Exception:  # noqa: BLE001 —— 观测设施绝不能反过来影响被观测的系统
        pass

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
) -> threading.Thread:
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
