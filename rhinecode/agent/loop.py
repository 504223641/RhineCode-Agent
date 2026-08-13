"""
Agent Loop（ReAct 自主循环）引擎，c4 的核心（spec F1/F2/F4/F5/F11/F13/F14）。

它把 c3 写死的「单轮往返」升级为真正的循环：
    一轮 = 调模型（带工具）→ 收齐文本与工具调用 → 若有工具则执行并回灌结果 → 进入下一轮
循环持续到模型本轮不再要工具（自然完成），或命中某个停止条件（迭代上限 / 用户取消 /
连续未知工具 / 流出错）。

设计原则：
- 与界面解耦（N4）：循环只产出 AgentEvent 单向事件流；需要用户决定的交互（确认 / 澄清 / 审批）
  通过传入的阻塞回调完成，循环不感知 TUI。
- 不阻塞 UI（N2）：本类预期运行在 TUI 的 Worker 线程；回调内部负责跨线程弹面板与等待。
- 不崩溃（N1）：工具执行异常统一兜底为 ToolResult(ok=False)，流错误转 FINISHED(STREAM_ERROR)。
- 历史一致（N5）：每轮按协议顺序向传入的 history 追加 assistant(含 tool_calls) 与各 tool 结果。

Plan Mode 两段式（F13）在循环内体现为每轮局部状态 execution_phase：
- 规划阶段（plan_mode 且未获批）：只暴露只读工具 + ask_user/present_plan 两个特殊工具。
- 用户通过 present_plan 批准后，execution_phase 置真，本轮后续迭代放开全部工具开始执行；
  但写文件、改文件、运行命令等副作用工具仍逐个确认，除非用户选择本会话免确认。
  Plan Mode 开关本身仍由上层维持（下一条用户消息会重新从规划阶段开始）。
"""

import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Callable, Iterator, Optional

if TYPE_CHECKING:
    # 仅类型检查期导入，运行期用字符串注解——避免与 context 层产生任何潜在导入顺序问题。
    from rhinecode.context import ContextManager

from rhinecode.provider.base import BaseProvider, Message, ToolCall
from rhinecode.tools.base import Tool, ToolResult
from rhinecode.tools.path_guard import main_project_root
from rhinecode.tools.registry import ToolRegistry
from rhinecode.agent.collector import StreamCollector
from rhinecode.agent.events import (
    AgentEvent,
    AgentEventType,
    ClarifyOption,
    StopReason,
)
from rhinecode.agent.plan_tools import ASK_USER, PRESENT_PLAN, plan_schemas
from rhinecode.agent.prompt import build_system_reminder, plan_toggle_instruction
from rhinecode.agent.cache_log import log_cache_usage
from rhinecode.hooks import (
    HookDecision,
    HookEventType,
    HookVerdict,
    NO_VERDICT,
    NullHookManager,
)
from rhinecode.agent.gate import MAX_WAIT_ROUNDS, NullGate
from rhinecode.permission import Decision, DecisionResult, Layer, PermissionEngine, to_request
from rhinecode.trace import (
    SCOPE_MAIN,
    NullRecorder,
    TraceEventType,
    TraceRecorderProtocol,
    full_text,
)

# ── 工具执行的结局取值（trace）──
# 埋 tool_execute 时用它区分「为什么这次调用是这个结果」。读 trace 时只看 ok=False
# 分不出「权限拒了」「用户拒了」「工具本身报错」，而这三者的排查方向完全不同。
OUTCOME_EXECUTED = "executed"                    # 真的跑了（不论 ok 真假）
OUTCOME_OUT_OF_SCOPE = "out_of_scope"            # 工具存在但本轮没发给模型（Skill 白名单）
OUTCOME_UNKNOWN_TOOL = "unknown_tool"            # 注册中心里没有这个工具
OUTCOME_INVALID_ARGUMENTS = "invalid_arguments"  # 模型生成的参数 JSON 非法
OUTCOME_DENIED_BY_PERMISSION = "denied_by_permission"  # 权限管线判 DENY
OUTCOME_DENIED_BY_USER = "denied_by_user"        # 人在回路面板里选了拒绝
OUTCOME_PLAN_BLOCKED = "plan_blocked"            # Plan Mode 规划阶段夹带的副作用工具
OUTCOME_BLOCKED_BY_HOOK = "blocked_by_hook"      # c12：被 Hook 前置层拦下（含 Hook 自身失败的 fail-closed）
OUTCOME_DENIED_NON_INTERACTIVE = "denied_non_interactive"  # c13：非交互执行（子 Agent）里判 ASK，无人可确认

# ── 用户在人在回路面板里选「拒绝」时回灌给模型的文本 ──
#
# ## 为什么这段话要写得这么长
#
# 旧文案只有一句「用户拒绝执行该工具。」——它**只说了没执行，没说接下来该干嘛**。
# 实测后果：模型把它读成「这次尝试失败了」，于是换个路径、换个命令、换个工具
# 反复重试，把一次明确的「不许」当成了「姿势不对，再试试」。用户被迫连点好几次
# 拒绝，而每一次拒绝在模型看来都只是又一次技术失败。
#
# 关键区分是**「技术失败」与「人的决定」**：前者该调整策略重试，后者该停下来问。
# 工具结果这个通道天生长得像前者（`ok=False` + 一段错误文本），所以必须由文案
# 把语义掰回来，明确写出「这不是错误」「不要绕」「去问为什么」。
#
# ## 光有文案不够，所以还有硬约束
#
# 文案是软约束，模型可以不听。因此 `_RoundContext.user_denied` 置位后，
# 主循环的**下一轮不发任何工具**（`tools=None`）——模型在物理上只能产出文本，
# 也就只能向用户说话。两者配合：文案解释「为什么」，机制保证「一定」。
#
# ## 为什么不直接终止循环
#
# 终止的话模型没有机会解释，用户只会看到工具行变红然后什么都没有——
# 比反复重试更糟。让它多跑一轮、但手里没有工具，才既停得住又说得出话。
DENIED_BY_USER_FEEDBACK = (
    "用户拒绝了这次 {name} 调用。\n"
    "\n"
    "**这不是技术故障，是用户的决定。** 不要重试、不要改参数再试一次、"
    "也不要换用其它工具绕过它——那会把一次明确的拒绝当成「姿势不对」，"
    "正是用户不希望发生的事。\n"
    "\n"
    "请立即停止本次任务的推进，向用户说明你原本打算做什么、为什么需要这一步，"
    "并询问他拒绝的原因、以及希望你怎么继续。等他答复后再行动。"
)

# ── 非交互执行下判 ASK 时回灌给模型的文本（c13 spec F15）──
#
# ## 它与 DENIED_BY_USER_FEEDBACK 的语义差别
#
# 上面那条说的是「**人做了决定**」——所以它要求模型停下来、去问用户为什么，
# 并且配套一条硬约束（下一轮不发任何工具），让它物理上只能说话。
#
# 这一条说的是「**没有人能做决定**」。子 Agent 跑在非交互环境里（可能在后台，
# 用户正在跟主对话说话），确认面板压根弹不出来。这不是谁拒绝了它，
# 是这条路在当前环境下走不通。
#
# 因此两处指引恰好相反：那条要求「停止推进」，这条要求「换条路继续」——
# 子 Agent 的价值就在于把活干完并回流一段结论，让它一遇到确认就瘫掉，
# 等于缺省配置下它什么都做不成（spec F15 已明示这个代价：缺省档下只能做只读的事）。
#
# ## 也因此**不置 `user_denied`**
#
# 那个标志会让下一轮 `tools=None`。对子 Agent 用它是错的：它应该带着工具继续，
# 改用只读方式达成目标。护栏见 `tests/test_agent_non_interactive.py`（含反证）。
DENIED_NON_INTERACTIVE_FEEDBACK = (
    "这次 {name} 调用需要人工确认，而当前是**非交互执行环境**（子 Agent），"
    "没有人能应答确认面板，因此它被自动拒绝。\n"
    "\n"
    "**这不是有人拒绝了你，也不是工具坏了**——是这条路在当前环境下走不通。"
    "原样重试同一个调用不会有不同结果。\n"
    "\n"
    "**换一个工具也没用**：写文件、改文件、执行命令这一类操作在这个环境下"
    "会被同样拒绝，不必逐个试过去——那只会白烧轮次。\n"
    "\n"
    "请改用不需要确认的方式推进：优先用只读工具达成目标。"
    "如果这一步确实非做不可，就把它写进你最终的结论里，"
    "说明「这一步需要用户授权，建议的做法是……」，由主对话去和用户确认。"
)

# ── 无人值守轮里判 ASK 时回灌给模型的文本（c15 spec F19）──
#
# ## 三条拒绝文案的语义必须两两不同
#
# | 常量 | 说的是 | 要求模型 |
# | --- | --- | --- |
# | `DENIED_BY_USER_FEEDBACK` | **人做了决定**，否决了这件事 | 停止推进，去问用户为什么 |
# | `DENIED_NON_INTERACTIVE_FEEDBACK` | **子 Agent 环境**里没人能应答 | 换只读方式把活干完 |
# | 本条 | **用户此刻不在场**（这一轮是队友的消息自动唤起的） | 回消息给队友 + 留言给用户 |
#
# 混用的后果都不报错，只是模型走岔路：套用第一条会让主 Agent 以为被否决、
# 直接放弃整件事；套用第二条会让它去「换只读方式达成」，而它此刻真正该做的
# 是**把决定权留给回来的用户**，同时别让等着它的队友干耗着。
#
# ## 与那两条一样**不置 `user_denied`**
#
# 那个标志会让下一轮 `tools=None`。对无人轮是错的：它应当带着工具继续——
# 回消息、读文件、改共享清单都是它现在做得到且该做的事。
DENIED_UNATTENDED_FEEDBACK = (
    "这次 {name} 调用需要人工确认，而**用户当前不在场**"
    "（这一轮是队友发来的消息自动唤起的，不是用户让你开始的），"
    "确认面板弹出来也没人应答，因此它被自动拒绝。\n"
    "\n"
    "**这不是有人拒绝了你，也不是工具坏了。** 原样重试不会有不同结果，"
    "换一个会改动东西的工具也一样。\n"
    "\n"
    "现在你**做得到**的事：\n"
    "- 用只读工具把情况调查清楚；\n"
    "- 给等着这件事的队友回一条消息，告诉它当前进展、让它先做别的，"
    "不要让它干耗着；\n"
    "- 更新共享任务清单，把状态和卡点写下来。\n"
    "\n"
    "**然后把「需要用户批准什么」明确写在你的回答里**——"
    "用户回来第一眼就能看到，一句话就能让你继续。"
)

# 迭代上限：兜底安全网，任何情况下循环都不会超过这么多轮（spec N3）。
MAX_ITERATIONS = 25
# 连续「整轮都是未知工具」达到该次数即停止，避免模型在不存在的工具上空转（spec F2）。
MAX_CONSECUTIVE_UNKNOWN = 3

# 回调类型别名（由 ConversationManager 注入；均预期在 Worker 线程被调用）。
# ask：当决策管线判定为 ASK（交人工确认）时调用，弹 HITL 面板。上层闭包已封装四选项
# （本次/本会话/永久/拒绝）与对应的会话规则登记 / 永久落盘，循环只看返回的 bool（是否执行）。
AskFn = Callable[[ToolCall, Tool, DecisionResult], bool]
# clarify：弹澄清面板，返回用户所选概述；返回 None 表示用户取消
ClarifyFn = Callable[[str, list[ClarifyOption]], Optional[str]]
# approve_plan：弹「是否开始执行」审批，返回是否批准
ApprovePlanFn = Callable[[str], bool]


@dataclass(frozen=True)
class RunOptions:
    """
    `Agent.run` 的可选行为开关（c11 T30）。

    **默认值即 C10 行为——不传这个参数等于零回归**，这是 spec N3 的实现保障：
    既有的全部调用点都不传，行为一字不变。

    存在的理由：`run()` 已经有 14 个参数，为独立模式子对话再加四个开关会失控。
    打包成一个 frozen 数据类，新增开关时既有调用点无需改动。

    :param max_iterations: 本次循环的迭代上限。独立模式子对话有**独立且更小**的
        预算（spec N6）——子任务应当聚焦，不该让一个跑偏的 Skill 把主对话的
        额度也一并耗光。
    :param record_usage: 是否用本次 API 返回的 usage 更新 C8 的估算锚点。
        子对话传 False：它跑的是另一条短历史，用它的 usage 去更新**主历史**的
        锚点会让主历史的估算彻底失准。
    :param allow_summary: 是否允许 C8 的第二层（LLM 摘要）。子对话传 False，
        只跑零成本的第一层存盘（spec F21）。
    :param excluded_tools: 本轮**不提供给模型、且调用了也要拒绝**的工具名。

        C11 曾用一个三元组（allowed / exempt / excluded）表达 Skill 的工具收窄，
        随白名单语义改为预授权而整体移除；**只有排除项还有用户**——
        子对话靠它禁用加载工具来防止「Skill 里再激活 Skill」的无限嵌套。

        既然只剩一种用途，就塌缩成一个集合：静态值即可，不需要像 C11 那样
        每轮回调求值（子对话的排除集在整条子对话里恒定不变）。

        缺省空集 = 不排除任何工具。
    :param interactive: 本次执行能否与人交互。**缺省 True 即既有行为，不传等于零回归**。

        为 False 时（c13 的子 Agent）只改一件事：权限管线判 ASK 的调用
        **不再调 `ask` 回调**，直接按 `OUTCOME_DENIED_NON_INTERACTIVE` 拒绝并回灌
        `DENIED_NON_INTERACTIVE_FEEDBACK`，且**不置 `user_denied`**。

        为什么必须是开关而不是「让 ask 回调返回 False」：那条路会走进
        `DENIED_BY_USER_FEEDBACK`（文案写的是「这是用户的决定，不要绕」）
        并硬性禁掉下一轮的全部工具。对子 Agent 这两条都是错的——
        没有任何用户做过决定，而它应当改用只读方式继续干活。
    """

    max_iterations: int = MAX_ITERATIONS
    cwd: Optional[Path] = None
    record_usage: bool = True
    allow_summary: bool = True
    excluded_tools: frozenset = frozenset()
    interactive: bool = True
    # c15 F18/F19：本次运行是不是「无人值守轮」——由队友的消息自动唤起、
    # 用户没有在场。**只影响判 ASK 时回灌哪条文案**，不改变任何权限判定：
    # `interactive=False` 已经决定了「一律拒绝」，本标志只决定「怎么把这件事
    # 说给模型听」（子 Agent 该换只读方式干完，无人轮该回消息 + 留言给用户）。
    #
    # ⚠ **必须与 `interactive=False` 同时使用**。单独置真没有意义——
    # 面板照常弹，文案分支根本走不到。
    unattended: bool = False
    subagent_gate: object = None


def _invalid_args_result(tc: ToolCall) -> ToolResult:
    return ToolResult(
        ok=False,
        output=f"工具 {tc.name} 的参数必须是 JSON 对象，请检查格式后重试。",
        summary="参数格式错误",
    )


class _RoundContext:
    """
    单轮工具执行的上下文，用于把 _execute 内部发生的「状态变化」回传给主循环。

    （用普通对象而非返回值，是因为 _execute 是生成器，需边 yield 事件边记录状态。）

    :param cancelled: 本轮交互中用户取消（如澄清面板按 Esc）
    :param approved: 本轮 present_plan 获得用户批准（据此切换到执行阶段）
    :param plan_rejected: 本轮 present_plan 被用户拒绝，主循环应立即停止
    :param user_denied: 本轮有工具在人在回路面板里被用户拒绝，**下一轮不发工具**
    :param known_count: 本轮命中的已知工具（含特殊工具）数量
    :param unknown_count: 本轮命中的未知工具数量
    """

    def __init__(self) -> None:
        self.cancelled: bool = False
        self.approved: bool = False
        self.plan_rejected: bool = False
        self.user_denied: bool = False
        self.known_count: int = 0
        self.unknown_count: int = 0


class Agent:
    """
    ReAct 循环引擎。构造时持有长期依赖（provider、工具注册中心），每条用户消息调用一次 run()。

    run() 不持有跨消息状态：迭代计数、执行阶段、连续未知计数等都是 run() 内的局部变量；
    会话级状态（如「免确认」）由上层封进 confirm 闭包，Plan Mode 开关由上层按消息传入。
    """

    def __init__(
        self,
        provider: BaseProvider,
        registry: Optional[ToolRegistry],
        recorder: "Optional[TraceRecorderProtocol]" = None,
        hooks=None,
    ):
        """
        :param provider: 已实例化的 Provider，负责实际 API 调用
        :param registry: 工具注册中心；为 None 时不向模型暴露任何工具（退化为纯对话循环）
        :param recorder: 行为记录器（trace 设施）。缺省用 `NullRecorder()`，
                         **不传等于零回归**——本类的全部埋点都变成空调用
        :param hooks: Hook 编排者（c12）。缺省用 `NullHookManager()`，
                      **不传同样等于零回归**——三个工具级事件的分发全部变成空调用，
                      且负载构造器一次都不会被执行
        """
        self._provider = provider
        self._registry = registry
        self._recorder: TraceRecorderProtocol = recorder or NullRecorder()
        self._hooks = hooks if hooks is not None else NullHookManager()

    # ------------------------------------------------------------------ #
    # 行为记录埋点（trace）
    # ------------------------------------------------------------------ #
    def _trace_tool(
        self,
        tc: ToolCall,
        res: ToolResult,
        outcome: str,
        duration_ms: float = 0,
        is_concurrent: bool = False,
        cwd: Optional[Path] = None,
    ) -> None:
        """
        记一条 `tool_execute` 事件。八处 `results[tc.id]` 写入点共用本方法。

        :param tc: 工具调用
        :param res: 执行结果（未执行的分支也有结构化结果）
        :param outcome: 六个 OUTCOME_* 之一，说明「为什么是这个结果」
        :param duration_ms: 实际执行耗时（毫秒，**保留三位小数**）；
            `outcome != executed` 时按约定记 0。
            为什么不取整：读小文件常在 1 毫秒内完成，取整后一律显示 `0ms`，
            与「计时压根没生效」无法区分（手测场景 1 里五条 read_file 全是 0ms，
            当时确实要多看一眼才能确认不是 bug）。保留小数则显示 `0.412ms`，
            一眼就知道是真的快
        :param is_concurrent: 是否走了只读并发桶

        :param cwd: 本次调用的工作目录（c14）。**隔离子 Agent 与主对话在这里分道**，
            不记的话「读落到主项目根、写却是对的」这类隔离故障在记录上完全看不出来
            ——那恰恰是 c14 成对维护点里最容易漏的一条（并发只读桶漏传 `cwd`）。
            `None` 表示调用点没有工作目录概念

        ## 两个 output 字段的分工

        - `output`：**完整原文**。trace 的职责是完整，主字段就该是完整的那一份。
        - `model_output`：模型**实际收到**的那一份，**只在与完整原文不同时才出现**。
          目前唯一会不同的是 `run_command`（它裁掉中间行以省 token）。
          两者都记是因为它们回答的是两个不同的问题：「命令到底输出了什么」
          与「模型是据什么做的下一步判断」——排查时经常需要同时知道。

        走 `emit_lazy` 是因为负载里的 `output` 可能很大（一次 `read_file` 就是整份文件），
        关闭记录时不该白构造一遍（spec N1）。

        副作用：产出一条 trace 事件；**任何异常都被吞掉**，不影响循环。
        """
        self._safe_emit_lazy(
            TraceEventType.TOOL_EXECUTE,
            lambda: {
                "tool": tc.name,
                "tool_call_id": tc.id,
                "arguments": full_text(tc.arguments) if tc.arguments is not None else None,
                "ok": res.ok,
                "summary": res.summary,
                "output": full_text(
                    res.full_output if res.full_output is not None else res.output
                ),
                # 只在真的不同时才写，避免每条事件都存两份一模一样的正文
                **(
                    {"model_output": full_text(res.output)}
                    if res.full_output is not None
                    else {}
                ),
                "duration_ms": duration_ms if outcome == OUTCOME_EXECUTED else 0,
                "is_concurrent": is_concurrent,
                "outcome": outcome,
                "cwd": str(cwd) if cwd is not None else None,
            },
        )

    # ------------------------------------------------------------------ #
    # Hook 前置层（c12）
    # ------------------------------------------------------------------ #
    def _tool_fields(self, tc: ToolCall, tool: Tool, **extra) -> dict:
        """
        构造工具级事件的负载字段（spec F2 工具级三事件）。

        :param tc: 工具调用
        :param tool: 对应工具实例
        :param extra: 事件专有的补充字段（如 `tool_output` / `duration_ms` / `error`）
        :returns: 字段字典

        ## `tool_input` 逐字展开

        模型生成的工具参数**每个键都升成顶层字段名**（spec F3.2），因此用户可以直接写
        `command:` / `file_path:` 而不必写 `tool_input.command`。

        ## ⚠ 事件字段覆盖同名的参数

        `tool` / `tool_call_id` / `is_read_only` / `scope` 在参数之后写入，因此**覆盖**
        同名的工具参数。理由与公共字段相同：一个恰好叫 `scope` 的工具参数若能盖掉
        分发作用域，一条「只在主对话生效」的规则会在某些工具上突然错位。
        代价是这四个名字无法用于匹配工具参数——已知且可接受。

        副作用：无。
        """
        fields: dict = {}
        if isinstance(tc.arguments, dict):
            fields.update(tc.arguments)
        fields.update(
            {
                "tool": tc.name,
                "tool_call_id": tc.id,
                "is_read_only": tool.read_only,
                "scope": self._safe_scope(),
            }
        )
        fields.update(extra)
        return fields

    def _dispatch_pre_tool(
        self, tc: ToolCall, tool: Tool, cwd: Optional[Path] = None
    ) -> "HookVerdict":
        """
        分发 `pre_tool_use` 并取回结论（spec F6）。

        :returns: Hook 的结论；无人监听或分发出错时为 `NO_VERDICT`（不表态）

        先问 `has_listeners` 是 spec N7 的硬要求：为假时**不构造负载**——
        一次 `write_file` 的 `tool_input` 就是整份文件内容，白构造一遍不可接受。

        兜底 `try` 是纵深防御：`dispatch` 自己已保证不抛，这里再兜一层，
        因为本方法所在的位置是「异常会变成工具结果回灌模型」的高危区
        （与 `_safe_emit` 系列同一理由）。

        副作用：可能起子进程 / 发 HTTP 请求（由命中的规则决定）。
        """
        if not self._hooks.has_listeners(HookEventType.PRE_TOOL_USE):
            return NO_VERDICT
        try:
            # c14 F25：命令跑在触发它的那个 Agent 的工作目录里。
            return self._hooks.dispatch(
                HookEventType.PRE_TOOL_USE,
                lambda: self._tool_fields(tc, tool),
                cwd,
            ).verdict
        except Exception:
            return NO_VERDICT

    def _dispatch_post_tool(
        self,
        tc: ToolCall,
        tool: Tool,
        res: ToolResult,
        duration_ms: float,
        cwd: Optional[Path] = None,
    ) -> None:
        """
        分发 `post_tool_use` 或 `post_tool_use_failure`（spec F2）。

        :param res: 执行结果，按 `res.ok` 决定分发哪个事件

        ## ⚠ 只在「真的执行了」之后调用

        本方法的两个调用点都紧挨着 `outcome == OUTCOME_EXECUTED` 的 `_trace_tool`。
        六种「压根没执行」的分支（未知工具 / 参数错误 / out_of_scope / plan_blocked /
        权限 DENY / 用户拒绝 / Hook 拦截）**一个都不能挂**——`post_tool_use_failure`
        的语义是「跑了但失败了」，把「没跑」混进来会让「统计工具失败率」
        这类用途直接失真，而且不报错（spec AC3）。

        副作用：可能起子进程 / 发 HTTP 请求。异常一律吞掉。
        """
        event = (
            HookEventType.POST_TOOL_USE if res.ok else HookEventType.POST_TOOL_USE_FAILURE
        )
        if not self._hooks.has_listeners(event):
            return
        extra: dict = {"tool_output": res.output, "duration_ms": duration_ms}
        if not res.ok:
            extra["error"] = res.summary or "工具执行失败"
        try:
            self._hooks.dispatch(
                event, lambda: self._tool_fields(tc, tool, **extra), cwd
            )
        except Exception:
            pass

    @staticmethod
    def _apply_hook_ask(
        decision: DecisionResult, verdict: "HookVerdict"
    ) -> DecisionResult:
        """
        Hook 判 ASK 时，把权限管线的 ALLOW **升级**为 ASK（spec F6.1）。

        :param decision: 权限管线的结论
        :param verdict: Hook 的结论
        :returns: 生效后的结论

        ## ⚠ 只升级 ALLOW，绝不降级 DENY

        写成「命中 ASK 就置为 ASK」会让一条 Hook 把①黑名单的 DENY 变成一次
        **可以点「同意」的确认面板**——Hook 于是获得了放宽权限的能力，
        而「Hook 只能收紧」正是本章全部安全论证的依据（spec AC15）。

        副作用：无。
        """
        if verdict.decision != HookDecision.ASK:
            return decision
        if decision.decision != Decision.ALLOW:
            # DENY 原样保留；已经是 ASK 的也不必重复包装。
            return decision
        return DecisionResult(
            Decision.ASK,
            Layer.HOOK,
            verdict.reason,
            kind=decision.kind,
            host=decision.host,
        )

    # 本层的四个「受保护漏斗」。
    #
    # 记录器**自己**已经保证 emit 不抛（见 recorder.py 的 try/except），所以这一层
    # 看起来像是多余的保险。它存在的理由是本层的特殊性：Agent Loop 是**唯一会把
    # 异常变成「工具结果」回灌给模型**的地方——只读并发桶里的异常会被
    # `future.result()` 抓住并包成「工具执行异常: …」交给模型。于是「日志写不进去」
    # 会伪装成「你的工具坏了」，模型开始绕路重试，一个纯观测设施变成了行为污染源
    # （trace spec AC6 就是为这条风险设的护栏）。
    #
    # 把防护收在四个漏斗里（而不是在二十来个埋点处各写一次 try）：本文件的全部
    # 埋点调用都只经这几个方法，一处加固即全层生效。
    def _safe_emit(self, type, **payload) -> None:
        """记一条事件，吞掉任何异常（含记录器实现本身抛错的情形）。"""
        try:
            self._recorder.emit(type, **payload)
        except Exception:
            pass

    def _safe_emit_lazy(self, type, factory) -> None:
        """记一条昂贵负载事件，吞掉任何异常。"""
        try:
            self._recorder.emit_lazy(type, factory)
        except Exception:
            pass

    def _safe_scope(self) -> str:
        """读当前作用域，失败时退回主作用域（不让观测把主流程带下水）。"""
        try:
            return self._recorder.current_scope()
        except Exception:
            return SCOPE_MAIN

    def _safe_bind(self, name: str) -> None:
        """绑定作用域，吞掉任何异常。**池线程入口必须用它。**"""
        try:
            self._recorder.bind_scope(name)
        except Exception:
            pass

    # ------------------------------------------------------------------ #
    # 工具集策略
    # ------------------------------------------------------------------ #
    @staticmethod
    def _visible(name: str, excluded: frozenset) -> bool:
        """
        判断某个工具在本轮是否对模型可见。

        :param name: 工具名
        :param excluded: 本轮被排除的工具名集合
        :returns: 可见为 True

        **排除既作用于「发不发 schema」也作用于「调了认不认」**，两处缺一不可：
        只做前者的话，模型仍可能凭训练先验硬造出一次调用，而那次调用会照常执行——
        子对话的防嵌套防线就此失效。

        副作用：无。
        """
        return name not in excluded

    def _visible_names(self, excluded: frozenset) -> str:
        """
        列出本轮实际可见的工具名，用于「工具不可用」的回灌文案。

        :param excluded: 本轮被排除的工具名集合
        :returns: 顿号分隔的工具名；无注册中心时给一句兜底说明

        只摊开注册中心里的名字，不含 `ask_user` / `present_plan`——那两个不在
        注册中心、只在 Plan Mode 规划阶段才拼接，列给模型只会误导它去调一个
        当前不存在的东西。

        副作用：无。
        """
        if self._registry is None:
            return "（当前没有可用工具）"
        names = set(self._registry.names()) - set(excluded)
        return "、".join(sorted(names)) if names else "（当前没有可用工具）"

    def _schema_for(
        self,
        plan_mode: bool,
        execution_phase: bool,
        excluded: frozenset = frozenset(),
    ) -> Optional[list[dict]]:
        """
        计算本轮要发给模型的工具 schema 列表。

        - 无注册中心 → None（纯对话，不带工具）
        - Plan Mode 且未获批执行（规划阶段）→ 只读工具 + ask_user/present_plan 特殊工具
        - 其余（普通模式，或 Plan Mode 已获批的执行阶段）→ 全部工具
        - 无论哪种，都再排除 `excluded` 里的工具（防子对话嵌套）

        :param plan_mode: 是否处于 Plan Mode
        :param execution_phase: Plan Mode 下是否已获批进入执行阶段
        :param excluded: 本轮要排除的工具名集合；空集表示不排除
        :returns: 工具 schema 列表，或 None

        **`plan_schemas()` 在过滤之后才拼接**，因此 `ask_user` / `present_plan`
        天然不受白名单影响（spec F15）——它们是流程控制工具，与 Skill 声明的
        业务能力无关。
        """
        if self._registry is None:
            return None

        planning = plan_mode and not execution_phase
        base = (
            # c13：规划阶段 = 只读工具 + 声明了 `plan_safe` 的工具。
            # 后者目前只有委派工具——Plan Mode 恰恰最需要把调研赶出主上下文
            # （规划要读很多东西，而那些内容要一路背到执行阶段）。
            # 它在那一阶段的自我约束（只许委派全只读角色）由它自己执行，
            # 循环只负责把阶段告诉它，见下面 `_run_one_serial` 的 `plan_stage`。
            self._registry.planning_schemas() if planning else self._registry.schemas()
        )

        if excluded:
            base = [
                s for s in base if self._visible(s["function"]["name"], excluded)
            ]

        if planning:
            base = base + plan_schemas()
        return base

    # ------------------------------------------------------------------ #
    # 主循环
    # ------------------------------------------------------------------ #
    def run(
        self,
        history: list[Message],
        thinking_effort: str,
        plan_mode: bool,
        stable: str,
        dynamic: Callable[[], str],
        model: str,
        debug_log_path: Optional[str],
        engine: PermissionEngine,
        ask: AskFn,
        clarify: Optional[ClarifyFn],
        approve_plan: Optional[ApprovePlanFn],
        cancel_event: threading.Event,
        context_manager: "Optional[ContextManager]" = None,
        recorder: Optional[Callable[[Message], None]] = None,
        options: "RunOptions" = RunOptions(),
    ) -> Iterator[AgentEvent]:
        """
        跑一次完整的 ReAct 循环，逐个产出 AgentEvent。

        :param history: 对话历史（同一引用）；循环会向其追加 assistant 与 tool 结果消息（N5）
        :param thinking_effort: 思考模式强度（透传给 provider）
        :param plan_mode: 是否处于 Plan Mode
        :param stable: 稳定系统提示（可缓存通道）；逐轮以 system 参数传给 provider，内容不变以命中缓存
        :param dynamic: 取动态内容（环境信息、已激活 Skill 正文等）的**回调，每轮求值一次**；
                        结果与会话级开关提醒合并进 <system-reminder> 注入。

                        **为什么是回调而不是字符串**（c11 改造点 1）：
                        C10 之前这里是个字符串，由协调层在收到用户消息时算一次，
                        整个循环里 `loop` 只是每轮把同一个不可变字符串重新包一层。
                        Skill 引入后这不成立了——模型可能在第 N 轮调 `load_skill`
                        激活一个 Skill，它的 SOP 正文必须从第 N+1 轮起就出现在
                        提醒里。若还是取值型，模型激活了 Skill 却在本次循环剩余的
                        全部轮次里完全看不到指令，两阶段加载直接失效。
        :param model: 当前模型名，仅用于缓存调试日志记录
        :param debug_log_path: 缓存调试日志文件路径；为 None 表示关闭日志（c5 F10）
        :param engine: 权限决策引擎（c6）；每个工具执行前调 engine.decide 算放行/拒绝/问
        :param ask: 人工确认回调；仅当决策为 ASK 时调用，返回是否执行（已封装四选项与规则登记）
        :param clarify: 需求澄清回调（ask_user 用），可为 None
        :param approve_plan: 计划审批回调（present_plan 用），可为 None
        :param cancel_event: 取消信号；循环在安全点轮询，置位即尽快停止
        :param context_manager: 上下文压缩器（c8）；为 None（非 DeepSeek 工具模式）时不压缩，
                                行为与 c8 之前完全一致。非 None 时每轮请求前跑两层压缩、
                                每轮拿到 usage 后更新估算锚点。
        :param recorder: 消息记录回调（c9 会话存档）：循环每向 history 追加一条消息
                         （assistant / tool 结果）就同步调用一次，用于 JSONL 追加写。
                         为 None 时不记录，行为与 c9 之前完全一致。回调内部 fail-safe，
                         这里再包一层 try 保证记录失败绝不影响循环（N2）。
        :param options: 可选行为开关（c11）。**默认值即 C10 行为，不传等于零回归**。
        :returns: AgentEvent 迭代器；末尾必为一个 FINISHED 事件

        副作用：向 history 追加消息；通过 provider 发起多次网络请求；通过回调与用户交互；
                若 debug_log_path 非空，每轮把缓存用量追加写入该文件。
        """
        execution_phase = False       # Plan Mode 下是否已获批执行
        consecutive_unknown = 0       # 连续「整轮仅未知工具」的次数
        # c13：子 Agent 闸门。缺省 NullGate → 两处调用退化为零成本空操作，
        # **不传等于零回归**。
        gate = options.subagent_gate if options.subagent_gate is not None else NullGate()
        # c14 F1/F4：本次运行的工作目录。
        #
        # 主对话与非隔离子 Agent 不传 → 取主项目根，行为与 c14 之前**逐字一致**
        # （spec N3）；隔离子 Agent 由 `subagents/runner.py` 传它的隔离工作区。
        #
        # 在这里一次算好而不是每处现取：`main_project_root()` 读的是进程当前工作
        # 目录，而本项目全程不 chdir，一次运行内它是常量——每次现取只会让
        # 「它到底会不会变」这个问题反复出现在读代码的人脑子里。
        run_cwd = options.cwd if options.cwd is not None else main_project_root()
        wait_rounds = 0               # 已为子 Agent 停留过几次（防无限接力）
        deny_cooldown = False         # 上一轮有工具被用户拒绝 → 本轮不发工具（见 DENIED_BY_USER_FEEDBACK）

        def _record(msg: Message) -> None:
            """把新追加进 history 的消息交给会话存档回调（c9）；失败静默不影响循环。"""
            if recorder is None:
                return
            try:
                recorder(msg)
            except Exception:
                pass

        for iteration in range(1, options.max_iterations + 1):
            # 安全点 1：进入新一轮前检查取消
            if cancel_event.is_set():
                yield AgentEvent(type=AgentEventType.FINISHED, stop_reason=StopReason.USER_CANCELLED)
                return

            yield AgentEvent(type=AgentEventType.PROGRESS, iteration=iteration)

            # 子 Agent 结论的交付点（c13 修订）。**必须在本轮组装请求之前**。
            #
            # 位置合法性：协议只禁止在 `assistant(tool_calls)` 与它对应的
            # `tool` 结果**之间**插消息，而此刻上一轮的 tool 结果早已写完。
            # 初版把这件事挂在协调层的 `_run()` 上（每条用户消息才一次），
            # 后果是主 Agent 在同一次运行里跑了六轮、结论一轮都没进去——
            # 用户必须插一句话才能让任务继续，等于把连贯的任务硬生生截断。
            #
            # 放在压缩**之前**：新追加的消息也要参与 token 估算。
            for extra in gate.take_pending():
                history.append(extra)
                _record(extra)

            # 上下文压缩（c8 F3）：每次 API 请求前先跑两层压缩（先第一层存盘、再按需第二层摘要），
            # 把可能过长的历史压回 token 预算内。压缩可能原地修改 history（改写工具结果 / 重构列表）。
            # 每条压缩动作都以 NOTICE 事件反馈给 TUI（F17）。context_manager 为 None 时整段跳过，
            # 保持 c8 之前的行为不变（N1 低侵入）。
            if context_manager is not None:
                for notice in context_manager.before_request(
                    history, allow_summary=options.allow_summary
                ):
                    # ⚠ **第一层存盘不再通知界面**（tui-activity-fold 验收期修订）。
                    #
                    # 「已把 N 个大型工具结果存盘」是纯内部机制：它不改变对话内容、
                    # 不需要用户做任何事，而它出现的时机恰好是历史最长、屏幕最挤的
                    # 时候——正是本轮改造要腾出的那块地方。留着它等于一边归并工具行
                    # 一边往历史里塞新的过程噪音。
                    #
                    # **证据不丢**：`context/manager.py` 已在存盘处埋了
                    # `context_compaction` 事件，排查时用 `--trace` 照样查得到，
                    # 那才是这条信息该待的地方。
                    #
                    # ⚠ 第二层 LLM 摘要**仍然通知**：它会真的改写历史（早段被压成
                    # 一条摘要），用户此后再往上翻会发现内容变了——那不是内部机制，
                    # 是对话本身发生了变化，必须告知。
                    if notice.kind == "offload":
                        continue
                    # 提示级：上下文压缩是后台常规动作（tui-display 扩展 F19）
                    yield AgentEvent(type=AgentEventType.NOTICE, message=notice.message)

            # 工具集收窄策略**每轮现取**（c11 F14）：模型可能上一轮才激活 Skill，
            # 这一轮的工具集就该随之收窄；注册中心也可能因 MCP 重载增删了工具。
            excluded = options.excluded_tools
            tools = self._schema_for(plan_mode, execution_phase, excluded)

            # 上一轮有工具被用户拒绝 → **本轮一件工具都不发**（硬约束）。
            # 理由见 DENIED_BY_USER_FEEDBACK 的注释：回灌文案是软约束、模型可以不听，
            # 只有真的不给工具，才能保证它停下来跟用户说话而不是换个姿势再试。
            # 只作用于紧接着的这一轮：用户答复后（新的一次 run）工具自然恢复。
            if deny_cooldown:
                tools = None

            # 组装请求消息（c5 分通道）：
            # - 稳定系统提示走 stream_chat 的 system 参数（可缓存前缀），不进 messages；
            # - 动态内容（dynamic 环境信息）+ 会话级开关（Plan Mode）按轮节奏的提醒，
            #   合并进一条 <system-reminder> 系统消息，追加到历史「末尾」。
            #   放末尾而非中间，是为了不破坏历史本身的前缀缓存——变化的提醒只成不缓存的尾巴。
            # Plan Mode 提醒仅在「规划阶段」（plan_mode 且尚未获批执行）注入。
            toggle = plan_toggle_instruction(
                iteration, active=plan_mode and not execution_phase
            )
            # dynamic 每轮求值一次（c11 改造点 1）：模型上一轮激活的 Skill，
            # 其 SOP 正文要从这一轮起出现在提醒里。
            reminder = build_system_reminder(dynamic(), toggle)
            # 记录本轮「纯历史消息条数」作为估算锚点长度（c8）：必须在 append reminder 之前、
            # 用 history 的长度而非 req_messages——reminder 是每轮临时拼的尾巴，不属于持久历史，
            # 而估算锚点覆盖的正是 usage 对应的这段纯历史。
            sent_len = len(history)
            req_messages: list[Message] = list(history)
            if reminder:
                req_messages.append(Message(role="system", content=reminder))

            # ---------- 双路收集本轮流式响应 ----------
            collector = StreamCollector()
            stream_error: Optional[str] = None
            for chunk in self._provider.stream_chat(
                req_messages, thinking_effort, tools=tools, system=stable
            ):
                if chunk.type == "error":
                    stream_error = chunk.content
                    break
                ev = collector.feed(chunk)
                if ev is not None:
                    yield ev

            # 缓存命中调试日志（c5 F10）：本轮拿到用量就记一行，用于验证稳定前缀是否命中缓存。
            # 即使随后判定流出错也照常记录——usage 可能已先于错误到达，记录它有助于排查。
            if debug_log_path and collector.usage is not None:
                log_cache_usage(collector.usage, model, debug_log_path)

            # 更新估算锚点（c8）：本轮 usage.prompt_tokens 是 API 亲口给出的精确输入 token 数，
            # 覆盖 sent_len 条纯历史消息；后续请求只需对锚点之后的新增消息做字符估算。
            # options.record_usage=False 时跳过（c11）：独立模式子对话跑的是另一条
            # 短历史，用它的 usage 去更新**主历史**的锚点会让主历史估算彻底失准。
            if (
                options.record_usage
                and context_manager is not None
                and collector.usage is not None
            ):
                context_manager.record_usage(collector.usage, sent_len)

            # 停止条件：流出错
            if stream_error is not None:
                yield AgentEvent(type=AgentEventType.ERROR, message=stream_error)
                yield AgentEvent(type=AgentEventType.FINISHED, stop_reason=StopReason.STREAM_ERROR)
                return

            text = collector.text
            tool_calls = collector.tool_calls

            # 停止条件：模型本轮不要工具 → 自然完成（也是纯对话/无工具的正常路径，F14）
            if not tool_calls:
                if text:
                    final_msg = Message(role="assistant", content=text)
                    history.append(final_msg)
                    _record(final_msg)

                # ── 子 Agent 等待闸门（c13 修订）──
                #
                # 模型准备收工了，但它自己委派出去的、**声明过要等结果**的子 Agent
                # 还在跑。这时候结束循环等于让它拿着不完整的信息回答，而用户
                # 必须再插一句话才能让任务继续——那正是初版被诟病的「变相中断」。
                #
                # 所以：停下来等，把结论注入后**再跑一轮**，让模型自己决定
                # 是继续干还是收工。
                #
                # **等待不是卡顿，是进度**：跑子 Agent 就是在执行任务，与主 Agent
                # 自己跑一遍测试套件性质相同。因此这里**不设体验意义上的超时**，
                # 唯一的逃生口是 `Esc`（`cancel_event` 会让 `wait_any` 立刻返回）。
                # 「我不需要等这个结果」由模型在委派时用 `background=true` 表达。
                #
                # `wait_rounds` 是兜底：防「等到一个 → 模型又委派一个 → 再等」
                # 无限接力。撞上它就正常收工，未完成的结论仍会在下一条用户消息时送达。
                if wait_rounds < MAX_WAIT_ROUNDS and gate.has_awaited():
                    wait_rounds += 1
                    waiting = gate.describe_awaited()
                    # 这条提示不可省：没有它，界面上就是「AI 突然不说话了」，
                    # 用户分不清是在等还是卡死了。
                    yield AgentEvent(
                        type=AgentEventType.NOTICE,
                        message=f"等待子 Agent 完成：{waiting}（按 Esc 可取消）",
                        # 事件级：这条正在解释「AI 为什么突然不说话了」，
                        # 用 dim 渲染的话它本身也不显眼，等于没解释
                        level="event",
                    )
                    if gate.wait_any(cancel_event):
                        # 等到了结论 → 下一轮迭代开头的 take_pending 会把它注入
                        continue
                    if cancel_event.is_set():
                        yield AgentEvent(
                            type=AgentEventType.FINISHED,
                            stop_reason=StopReason.USER_CANCELLED,
                        )
                        return
                    # 没等到（撞上兜底上限，或那些任务已经全部交付过了）→ 照常收工

                yield AgentEvent(type=AgentEventType.FINISHED, stop_reason=StopReason.COMPLETED)
                return

            # 有工具调用：先把 assistant(含 tool_calls) 追加历史（N5）
            assistant_msg = Message(role="assistant", content=text, tool_calls=tool_calls)
            history.append(assistant_msg)
            _record(assistant_msg)

            # ---------- 执行工具并回灌 ----------
            results: dict[str, ToolResult] = {}
            ctx = _RoundContext()
            yield from self._execute(
                tool_calls, results, ctx,
                engine, ask, clarify, approve_plan,
                cancel_event, excluded,
                # 与 `_schema_for` 的 `planning` **同口径**：两者必须一致，
                # 否则会出现「发了工具却拒绝执行」或「没发工具也照常执行」。
                planning=plan_mode and not execution_phase,
                interactive=options.interactive,
                # c15 F19：只决定判 ASK 时回灌哪条文案，不改变任何权限判定。
                unattended=options.unattended,
                # c14 F1：本次运行的工作目录。`None` 由各工具的 `require_cwd`
                # 兜成明确失败，**不会**静默回退到主项目根（spec N2）。
                cwd=run_cwd,
            )

            # 按原始顺序把每个工具结果作为 role="tool" 消息回灌历史
            for tc in tool_calls:
                res = results.get(tc.id)
                output = res.output if res is not None else "工具未产生结果"
                tool_msg = Message(role="tool", tool_call_id=tc.id, content=output)
                history.append(tool_msg)
                _record(tool_msg)

            # 计划获批 → 本轮后续迭代进入执行阶段
            if ctx.approved:
                execution_phase = True

            # 计划被拒绝 → 终止当前 Agent 回合，不再把结果交回模型继续下一轮
            if ctx.plan_rejected:
                yield AgentEvent(type=AgentEventType.FINISHED, stop_reason=StopReason.PLAN_REJECTED)
                return

            # 停止条件：澄清面板被用户取消
            if ctx.cancelled:
                yield AgentEvent(type=AgentEventType.FINISHED, stop_reason=StopReason.USER_CANCELLED)
                return

            # 用户拒绝冷却：本轮被拒 → 下一轮不发工具；本轮没被拒 → 解除。
            # 放在这里（而不是消费处）是为了让「置位」与「解除」都只有一处，
            # 避免出现「拒绝一次之后永远不给工具」这种更糟的形态。
            deny_cooldown = ctx.user_denied

            # 连续未知工具统计：本轮「有未知且无已知」算一次连续，否则清零
            if ctx.unknown_count > 0 and ctx.known_count == 0:
                consecutive_unknown += 1
            else:
                consecutive_unknown = 0
            if consecutive_unknown >= MAX_CONSECUTIVE_UNKNOWN:
                yield AgentEvent(
                    type=AgentEventType.FINISHED,
                    stop_reason=StopReason.UNKNOWN_TOOL,
                    message=f"连续 {consecutive_unknown} 轮调用未知工具，已停止。",
                )
                return

            # 安全点 2：本轮结束后检查取消
            if cancel_event.is_set():
                yield AgentEvent(type=AgentEventType.FINISHED, stop_reason=StopReason.USER_CANCELLED)
                return

        # 走完上限仍未结束 → 兜底停止（N3）
        yield AgentEvent(
            type=AgentEventType.FINISHED,
            stop_reason=StopReason.MAX_ITERATIONS,
            message=f"已达到迭代上限（{options.max_iterations} 轮），自动停止。",
        )

    # ------------------------------------------------------------------ #
    # 工具执行：特殊工具路由 + 只读并发 + 副作用串行
    # ------------------------------------------------------------------ #
    def _execute(
        self,
        tool_calls: list[ToolCall],
        results: dict[str, ToolResult],
        ctx: _RoundContext,
        engine: PermissionEngine,
        ask: AskFn,
        clarify: Optional[ClarifyFn],
        approve_plan: Optional[ApprovePlanFn],
        cancel_event: threading.Event,
        excluded: frozenset = frozenset(),
        planning: bool = False,
        interactive: bool = True,
        cwd: Optional[Path] = None,
        unattended: bool = False,
    ) -> Iterator[AgentEvent]:
        """
        执行本轮所有工具调用：先做权限「决策预扫」，再按类别分流执行（c6）。

        决策预扫（每个已知工具调用执行前由代码算放行/拒绝/问，spec F1）：
        - 用 adapter.to_request 把调用规范化，调 engine.decide 得到 DecisionResult。
        - ALLOW + 只读 → 进并发桶（无需确认）。
        - ALLOW + 副作用 → 进串行桶，直接执行（命中 allow 规则即免确认，spec AC4）。
        - DENY → 进串行桶，产出结构化拒绝结果、不执行（不终止循环，spec F8）。
        - ASK → 进串行桶，执行前调 ask 回调弹 HITL 面板；
          **`interactive` 为假时（c13 子 Agent）直接判拒，`ask` 不会被调用**。

        分流后：
        - 特殊工具（ask_user / present_plan）：串行，路由到 clarify / approve_plan。
        - 只读且 ALLOW：并发执行（互不冲突，提升效率）。
        - 其余（副作用 / 被拒 / 待确认 / 未知 / 参数错误）：串行执行。

        所有结果写入 results（键为 tool_call.id）；状态变化记录到 ctx 供主循环判断。

        :param tool_calls: 本轮工具调用
        :param results: 输出参数，写入 id → ToolResult
        :param ctx: 本轮上下文，记录 cancelled/approved/known/unknown
        :param engine: 权限决策引擎（决策预扫用）
        :param ask: 人工确认回调（仅 ASK 时调用）
        :param clarify/approve_plan: 特殊工具的交互回调
        :param cancel_event: 取消信号，串行执行前检查
        :param interactive: 能否与人交互（c13）。缺省 True 即既有行为；为假时
            ASK 走非交互拒绝分支，见 `DENIED_NON_INTERACTIVE_FEEDBACK`

        副作用：实际执行工具（可能读写文件、跑命令）；通过回调与用户交互。
        """
        special: list[ToolCall] = []
        readonly: list[tuple[ToolCall, Tool]] = []
        # 被本轮工具收窄挡下的调用（工具真实存在，只是没发给模型）。
        out_of_scope: list[ToolCall] = []
        # Plan Mode 规划阶段夹带的副作用工具（已知项 #2）。
        # **与 out_of_scope 分开是刻意的**：两处过滤职责不同，回灌给模型的
        # 下一步指引也完全不同——那边是「换个工具」，这边是「先提交计划」。
        plan_blocked: list[ToolCall] = []
        # 被 Hook 前置层拦下的调用（c12）。与上面两个分开，同样是因为回灌指引不同——
        # 那两处是「换个工具」「先提交计划」，这里是「这是用户预先写下的规则，别绕」。
        hook_blocked: list[tuple[ToolCall, "HookVerdict"]] = []
        # 串行桶元素：(调用, 工具或None, 决策或None)。
        # tool=None → 未知工具；decision=None → 参数解析失败（两者都不进引擎）。
        serial: list[tuple[ToolCall, Optional[Tool], Optional[DecisionResult]]] = []

        # 分流 + 决策预扫
        for tc in tool_calls:
            if tc.name in (ASK_USER, PRESENT_PLAN):
                special.append(tc)
                ctx.known_count += 1
                continue
            tool = self._registry.get(tc.name) if self._registry else None
            if tool is None:
                serial.append((tc, None, None))
                ctx.unknown_count += 1
                continue
            # 工具存在，但**本轮没发给模型**（被 Skill 白名单收窄掉了）。
            # 模型仍可能凭训练先验硬造出这样一次调用——实测 DeepSeek 就在
            # 只发了 4 个工具的情况下调出了 `edit_file`，参数名还全对，于是
            # 一个「只读审阅」的窄白名单 Skill 动手改了代码。照常执行等于
            # 让 allowed_tools「提升选对工具准确率」的作用彻底失效，
            # 故这里拒绝并回灌结构化原因，让模型改用可见工具（不终止循环）。
            if not self._visible(tc.name, excluded):
                out_of_scope.append(tc)
                ctx.unknown_count += 1
                continue
            # Plan Mode 规划阶段：只允许只读调研（已知项 #2）。
            #
            # `_schema_for` 在规划阶段用 `readonly_schemas()` 已经不发副作用工具，
            # 但**模型仍会凭训练先验硬造出调用**——C11 场景 10 验收时实测撞到：
            # 那一轮 `tool_names` 里没有 `run_command`，模型照样调了出来，
            # 而当时唯一的守卫 `_visible` 只查 Skill 白名单、不查规划阶段的只读过滤，
            # 于是 `outcome=executed`：**规划阶段真的执行了副作用命令**。
            # 那次夹带的恰好是 `git diff`，但同一路径上完全可能是写命令。
            #
            # 五层权限管线仍会照常拦截（会弹确认面板），但那是「最后一道」而非
            # 「本该有的一道」——Plan Mode 的承诺是「批准前不动手」，
            # 不该退化成「批准前每次都问你要不要动手」。
            #
            # ⚠ **豁免条件是 `plan_safe`，不是 `system_serial`**（c13 修订）。
            #
            # 这里原本写的是 `and not tool.system_serial`——那是为 `load_skill`
            # 写的，而 `load_skill` 是 `read_only=True`，本来就被前一个条件挡在外面，
            # 于是那条豁免长期是**空转**的。
            #
            # C13 的委派工具 `run_agent` 恰好把它激活了：`system_serial=True`
            # **且** `read_only=False`。后果实测过——规划阶段模型凭训练先验硬造出
            # 一个 `run_agent` 调用，它**既没被这里挡下、又因为当时 system_serial
            # 压根不进权限引擎**，直接执行了；而它委派出去的子 Agent 可以写文件。
            # Plan Mode「批准前不动手」的承诺就此被绕过。
            #
            # （那个「不进引擎」的缺陷已于 perm-system-serial-bypass 单独修掉，
            # 但本守卫**仍然不可省**：引擎对 `run_agent` 只会判 ASK，而
            # 系统级工具的 ASK 按 ALLOW 处理——挡住规划阶段委派的只有这一道。）
            #
            # 改成 `plan_safe` 之后：`load_skill` 仍靠 `read_only=True` 通过
            # （行为一字不变），而任何**没有明确声明过自己规划期安全**的副作用工具
            # 一律被挡——豁免从「凡是系统级工具」收窄成「明确承诺过的工具」。
            if planning and not tool.read_only and not tool.plan_safe:
                plan_blocked.append(tc)
                ctx.unknown_count += 1
                continue

            ctx.known_count += 1
            if not isinstance(tc.arguments, dict):
                # 参数解析失败或非对象 JSON：不进引擎，也**不触发 Hook**——
                # 这次调用在任何判定之前就已经废了。留待串行路径产出结构化错误。
                #
                # 这一步从「系统级工具分流之后」提到了「之前」，因此系统级工具的
                # 非法参数现在也走结构化错误，而不是带着一个字符串进 `tool.execute`
                # 去撞 AttributeError。两者都是 ok=False 且都不执行，新形态的
                # 报错更可读。
                serial.append((tc, tool, None))
                continue

            # ── Hook 前置层（c12）：**唯一分发点** ──
            #
            # 位置卡在这里的理由，两头都不能挪：
            # - 往前挪会让四个「压根没执行」的分支（未知工具 / out_of_scope /
            #   plan_blocked / 参数非法）也触发 `pre_tool_use`。
            # - 往后挪就跨过了系统级工具的分流，那条路径将拿不到 Hook 结论。
            #
            # 它排在**五层权限管线之前**：Hook 说拦就直接拦，连 `engine.decide`
            # 都不调（这一点有专门的反证测试钉着——防「先跑引擎再看 hook」
            # 这种顺序写反但结果碰巧正确的实现）。
            #
            # ⚠ **「权限 DENY」与「用户在面板拒绝」两种情形下 `pre_tool_use`
            # 照常触发**，这与 spec F2 边界第 2 条的字面表述不同。那条边界写的是
            # 「一个都不触发任何工具级事件」，但它给出的理由只讲 post 事件的语义
            # （「跑了但失败了」）；而 `pre_tool_use` 按决策 1A 排在五层**之前**，
            # 在跑权限判定之前根本无从知道它会不会 DENY——结构上做不到。
            # 因此那条边界按「管后置事件」理解，前置事件对每一次进入判定的调用都触发。
            hook_verdict = self._dispatch_pre_tool(tc, tool, cwd)
            if hook_verdict.decision == HookDecision.DENY:
                hook_blocked.append((tc, hook_verdict))
                continue

            # 系统级串行工具**强制走串行、不进只读并发桶**（对齐改造 F8）。
            #
            # 理由是它可能开一整条子对话（`context: fork` 的 Skill 由模型自行发起、
            # 或 `run_agent` 委派）。在只读并发桶里跑子对话意味着：子对话自己的
            # 确认面板会从线程池的工作线程里弹出来，而那正是 C11 加锁不变量那一课的
            # 同型场景，只是后果更重——那次是状态栏刷新，这次是整条交互链。
            #
            # ── 它**照常过一次 `engine.decide`**（perm-system-serial-bypass）──
            #
            # 这里原本是「直接造一个 ALLOW 塞进串行桶、根本不调引擎」。后果是
            # `permissions.yaml` 里的 `deny: run_agent` / `deny: send_message`
            # **一条都不生效**，影响七个工具（`run_agent` / `load_skill` +
            # C15 的五个协作工具）。危害不在「这些工具很危险」——它们不读写文件、
            # 不执行命令，副作用限于起一条子对话或改进程内存；危害在于**文档从 C13
            # 起一直承诺「仍可被 deny 规则整个禁掉」，而那是错的**。用户照着写一条
            # 规则会以为自己关掉了委派能力，实际没有，且界面上完全看不出来。
            # **错误的安全承诺比没有承诺更危险。**
            #
            # ⚠ **它们对第④层（权限档兜底）免疫，这一条不可省。**
            #
            # 判据是**层**，不是决策取值：只要结论来自 `Layer.MODE`，无论 ASK
            # 还是 DENY 一律按 ALLOW 处理。①黑名单、②沙箱、③规则、Hook 一字不动。
            #
            # 为什么是「整层免疫」而不是原先的「只降级 ASK」——两条理由：
            #
            # **一、④层对这七个工具而言不是「灰色地带更谨慎」，是「功能整个关掉」。**
            # 它们全都不在 `_TOOL_MAP` 里、走 `other` 分支，只认不带括号的整工具
            # 规则，因此③层绝大多数情况下压根不表态——④是**唯一会说话的那一层**。
            # 缺省档下它给 ASK（不降级就等于给七个工具全加上人在回路，交互链会被
            # 一条可能开子对话的工具拧死）；**严格档下它给 DENY**，那就是把委派、
            # Skill 加载、以及全部协作能力一次性关掉。
            #
            # **二、只降级 ASK 会造成一处实测到的、没人打算要的后果。**
            # 内置的 `explorer` / `planner` 声明 `permission_mode: strict`，
            # 而 `engine` 有一条只读短路（`is_read_only` 在③未命中时直接放行、
            # **不进④层**）。于是那两个角色手上唯一会走到④层的东西，恰恰就是
            # C15 F22 特意豁免给它们的协作工具——`strict` 对它们做的**唯一**一件事
            # 就是关掉协作，别的什么都没管。真实模型实测：`explorer` 调
            # `send_message` 拿到 `deny（④模式）严格模式：无规则放行，默认拒绝`，
            # 白烧一轮，还要在结论里向用户解释一遍。那与 C15 已经修过一次的
            # 「只读角色一个协作工具都拿不到」是同一个用户可见症状，只是卡在另一层。
            #
            # 于是这个分支相对「绕过引擎」那一版的净效果仍然**只有一条**：
            # **DENY 现在拦得住了**——但那个 DENY 必须来自用户写下的规则（③）
            # 或安全底线（①②），不能来自权限档兜底（④）。
            # 想整个关掉它们，写 `deny: run_agent` / `deny: send_message`
            # （⚠ 不带括号的整工具形式）；`/perm 严格` 不是、也从来不是这个用途。
            if tool.system_serial:
                request = to_request(tool, tc.arguments, engine.mode, cwd)
                raw = engine.decide(request)
                # ④层的结论一律降级为放行；其余层的 DENY 原样保留
                # （含①黑名单、②沙箱——虽然 `other` 类请求走不到那两层，
                #  这里不写特例是为了「引擎说拒就是拒」这条不留缺口）。
                mode_downgraded = (
                    raw.decision != Decision.ALLOW and raw.layer is Layer.MODE
                )
                if raw.decision != Decision.ALLOW and not mode_downgraded:
                    system_decision = raw
                else:
                    system_decision = DecisionResult(
                        Decision.ALLOW,
                        raw.layer,
                        (
                            f"系统级工具（system_serial）：{raw.reason}；"
                            "该结论来自权限档兜底，对这类工具不生效，按放行处理"
                        ) if mode_downgraded else raw.reason,
                        kind=raw.kind,
                        host=raw.host,
                    )
                # Hook 的 ASK 仍然照常升级为「问用户」——**刻意与④模式层的 ASK
                # 区别对待**：④是灰色地带的兜底，而 Hook 的 ASK 是用户针对这件事
                # 写下的一条规则，那是明确的意愿表达，不是兜底。
                # 这一支的行为与改造前逐字一致。
                system_decision = self._apply_hook_ask(system_decision, hook_verdict)
                # ⚠ **这条埋点不可省。**
                #
                # 它原本是为了让「绕过引擎」这件事在记录上可见（此前这七个工具
                # 一条判定记录都没有，时间线上表现为「一条 tool_execute 凭空出现」）。
                # 绕过已经修掉，但埋点要留下，理由变成两条：
                # ① 通用不变量「每一次 tool_execute 前面都有一条同 id 的判定」
                #    靠它成立（护栏见 `tests/test_trace_system_serial.py`）；
                # ② **④层结论被降级这件事必须可见**。不记的话时间线上只会看到
                #    `allow（④模式）`，读的人会以为用户切到了放行档——
                #    观测设施撒谎且不报错。严格档下降级的是 **DENY**，
                #    那更需要看得见：读的人得能分清「引擎放行了」与
                #    「引擎拒了但这类工具对该层免疫」。
                self._safe_emit(
                    TraceEventType.PERMISSION_DECISION,
                    tool=tc.name,
                    tool_call_id=tc.id,
                    kind=request.kind,
                    specifier=request.specifier,
                    host=request.host,
                    is_read_only=request.is_read_only,
                    decision=system_decision.decision.value,
                    layer=system_decision.layer.value,
                    reason=system_decision.reason,
                    # 「引擎在④模式层判了 ASK 或 DENY，但因为是系统级工具、
                    #  对该层免疫而按 ALLOW 执行了」。阅读器据此在时间线上标记，
                    #  见 `trace/reader.py`。⚠ 字段名从 `ask_downgraded` 改成
                    #  `mode_downgraded` 是因为被降级的**不再只有 ASK**——
                    #  留着旧名字会让「严格档下 DENY 被降级」这件事记不出来。
                    mode_downgraded=mode_downgraded,
                    cwd=str(request.cwd) if request.cwd is not None else None,
                )
                serial.append((
                    tc,
                    tool,
                    system_decision,
                ))
                continue

            # 权限决策：规范化 → engine.decide。
            # c14 F2：把**本次运行的工作目录**一并交给权限判定。
            # 第②层路径沙箱据它算边界——隔离子 Agent 传的是它自己的隔离工作区，
            # 于是同一个写请求在主对话里放行、在隔离子 Agent 里越界即拒。
            request = to_request(tool, tc.arguments, engine.mode, cwd)
            decision = engine.decide(request)
            # 权限决策埋点（trace F14）。
            #
            # **为什么只埋这一处**：`engine.decide` 在生产代码里还有另一个调用点——
            # 协调层注入给 `glob_files` / `grep_content` 的逐文件过滤器。一次 grep
            # 会触发几百次判定，埋进去会把整条时间线淹掉；而且它判的是「这个文件
            # 要不要出现在结果里」，不是「这次工具调用放不放行」，语义也不同。
            #
            # **为什么埋在调用点而不是引擎内部**：保持 `permission/` 包「纯判定、
            # 无副作用」的既有性质（`decide` 的 docstring 明写「副作用：无」）。
            # 把埋点塞进引擎会让那句话变成假话，而权限层是安全边界，它的可预测性
            # 比少写一行埋点重要。
            #
            # ⚠️ **埋点必须排在下面的 Hook 升级之后**，记的是**生效的**那个结论。
            #
            # 埋在升级之前的话，一次「权限判 ALLOW、Hook 把它升级为 ASK」的调用
            # 会在记录里留下 `decision=allow`，而用户实际看到的是一个确认面板——
            # 观测设施撒谎且不报错，排查的人会据此断定「Hook 没生效」。
            # 端到端场景 2 就是靠这条判定层为 `hook` 的记录来验升级的（实测踩过）。
            decision = self._apply_hook_ask(decision, hook_verdict)
            self._safe_emit(
                TraceEventType.PERMISSION_DECISION,
                tool=tc.name,
                tool_call_id=tc.id,
                kind=request.kind,
                specifier=request.specifier,
                # 主机名单独记一份（web_fetch 扩展 F23/AC31）。
                #
                # 只靠 reason 文案不够：②′层自己给出的拒绝会在文案里带上主机名，
                # 但走③层 deny 规则命中时，reason 是
                # 「命中 deny 规则 WebFetch(domain:*.example.com)（来源：user）」
                # ——里面只有**规则的模式**，没有本次请求的主机名。
                # 非 url 类为空串。
                host=request.host,
                is_read_only=request.is_read_only,
                decision=decision.decision.value,
                layer=decision.layer.value,
                reason=decision.reason,
                # ⚠ **c14：判定用的那个边界必须记下来。**
                #
                # `specifier` 只是模型给的那串路径（常常是相对路径），它单看无法
                # 回答「这次读写落在哪儿」。而 c14 成对维护点里最容易漏的一条
                # 恰恰是「并发只读桶漏传 cwd」——漏了之后隔离子 Agent 的**读**
                # 落到主项目根、**写**却是对的，界面上完全看不出来。
                #
                # 不记这个字段的话，那类隔离故障在记录上也看不出来：
                # 主对话与隔离子 Agent 的 `permission_decision` 长得一模一样。
                # 记了之后，一条 `scope=subagent:worker` 却 `cwd=<主项目根>`
                # 的记录本身就是结论。
                cwd=str(request.cwd) if request.cwd is not None else None,
                # 普通工具永远不降级：④层判 ASK 就是弹面板、判 DENY 就是拒绝。
                # 这个常量 False 是有意义的对照——没有它，把标记写成常量 True
                # 也能让系统级工具那条护栏通过，标记随即失去意义。
                mode_downgraded=False,
            )
            # 升级已在埋点之前完成（见上方说明）。这里只做分桶：
            # 升级后的调用必须走串行桶弹面板，不能留在只读并发桶里。
            if decision.decision == Decision.ALLOW and tool.read_only:
                readonly.append((tc, tool))
            else:
                serial.append((tc, tool, decision))

        # 被排除挡下的：不执行，回灌结构化原因并告知可用工具。
        #
        # **这条分支在本轮改造后只剩一个用户：子对话的防嵌套。** Skill 的工具收窄
        # 已随白名单语义变更而移除，普通对话里 `excluded` 恒为空、这里恒不触发。
        # 但它不能删——排除只是不发 schema，模型仍可能凭训练先验硬造出调用，
        # 没有这道判定的话那次调用会照常执行，嵌套防线就此失效。
        for tc in out_of_scope:
            yield AgentEvent(type=AgentEventType.TOOL_START, tool_call=tc)
            res = ToolResult(
                ok=False,
                output=(
                    f"[工具不可用] {tc.name} 本轮未提供给你，因此没有执行。\n"
                    f"当前可用工具：{self._visible_names(excluded)}。\n"
                    f"请改用其中之一；若确实必须用 {tc.name}，请说明理由让用户决定。"
                ),
                summary="不在当前工具集内",
            )
            results[tc.id] = res
            # ⚠️ **这条埋点的分量**：上面那段 `[工具不可用] … 当前可用工具：…` 原文，
            # 是「模型调用了本轮没发给它的工具」这件事的**唯一物证**——
            # 它既不在 `permission_decision` 里（压根没进引擎），
            # 叠加 F17 的字段白名单后也不在 `agent_event` 里（那里只留工具名与 ok）。
            # 漏埋这一条，本模块就查不出当初立项要查的那个问题。
            self._trace_tool(tc, res, OUTCOME_OUT_OF_SCOPE, cwd=cwd)
            yield AgentEvent(type=AgentEventType.TOOL_RESULT, tool_call=tc, tool_result=res)

        # Plan Mode 规划阶段夹带的副作用工具：拒绝并指回「先提交计划」。
        #
        # 文案要点：说清**现在是什么阶段**、**为什么被拒**、**下一步该做什么**。
        # 只说「不允许」会让模型换个工具名再试一次（同 out_of_scope 的教训）。
        for tc in plan_blocked:
            yield AgentEvent(type=AgentEventType.TOOL_START, tool_call=tc)
            res = ToolResult(
                ok=False,
                output=(
                    f"[计划模式] 现在处于**规划阶段**，{tc.name} 会产生副作用，因此没有执行。\n"
                    f"规划阶段只允许只读调研（读文件、搜索、查看结构）与向用户提问。\n"
                    f"若这一步是方案的一部分，请把它写进计划、用 present_plan 提交给用户审批；"
                    f"获批后你才可以执行它。不要改用别的工具绕过这一限制。"
                ),
                summary="规划阶段不执行副作用工具",
            )
            results[tc.id] = res
            self._trace_tool(tc, res, OUTCOME_PLAN_BLOCKED, cwd=cwd)
            yield AgentEvent(type=AgentEventType.TOOL_RESULT, tool_call=tc, tool_result=res)

        # 被 Hook 前置层拦下的：不执行，回灌规则给出的原因（c12 spec F6.3）。
        #
        # 文案整体由 `HookManager` 产出（`BLOCKED_FEEDBACK`），这里只负责搬运——
        # 拦截原因是「用户预先声明的规则」这个语义要靠文案掰正，把它散到两个模块
        # 各写一半，改一处就会漂移。
        #
        # **刻意不置 `ctx.user_denied`**：那个标志的含义是「人刚刚在面板上说了不」，
        # 会让下一轮硬性不发任何工具。Hook 拦截是一条预设规则命中，不是人的即时决定，
        # 借用它会让模型在一条无关的规则触发后突然失去全部工具。
        for tc, verdict in hook_blocked:
            yield AgentEvent(type=AgentEventType.TOOL_START, tool_call=tc)
            res = ToolResult(ok=False, output=verdict.reason, summary="Hook 拦截")
            results[tc.id] = res
            self._trace_tool(tc, res, OUTCOME_BLOCKED_BY_HOOK, cwd=cwd)
            yield AgentEvent(type=AgentEventType.TOOL_RESULT, tool_call=tc, tool_result=res)

        # 只读且放行：并发
        if readonly:
            yield from self._run_readonly_concurrent(readonly, results, cwd)

        # 特殊工具：串行（需用户交互）
        # 裁决（trace T31）：`_run_special`（ask_user / present_plan）内的两处
        # `results[tc.id]` 写入点**刻意不产 `tool_execute`**。理由有二：
        # ① 负载完全重叠——它们的输入输出由界面层的 `interaction` 事件承载，
        #    重复携带违反 F17；② 它们语义上是「与用户交互」而不是「执行工具」，
        #    混进 tool_execute 会让「工具跑了几次、多慢」这类统计失真。
        for tc in special:
            if cancel_event.is_set():
                ctx.cancelled = True
                break
            yield from self._run_special(tc, results, ctx, clarify, approve_plan)
            if ctx.cancelled or ctx.plan_rejected:
                break

        # 其余：串行
        for tc, tool, decision in serial:
            if ctx.cancelled or ctx.plan_rejected:
                break
            if cancel_event.is_set():
                ctx.cancelled = True
                break
            yield from self._run_one_serial(
                tc, tool, decision, results, ctx, ask, interactive, planning, cwd, unattended
            )

    def _run_special(
        self,
        tc: ToolCall,
        results: dict[str, ToolResult],
        ctx: _RoundContext,
        clarify: Optional[ClarifyFn],
        approve_plan: Optional[ApprovePlanFn],
    ) -> Iterator[AgentEvent]:
        """
        执行一个特殊交互工具（ask_user / present_plan），路由到对应回调。

        ask_user：解析 question/options → 调 clarify 弹澄清面板 → 所选概述作为结果；
                  用户取消（返回 None）→ 置 ctx.cancelled，结果记为「用户取消」。
        present_plan：解析 plan → 调 approve_plan 弹审批 → 批准则置 ctx.approved。

        回调缺失或参数非法时返回结构化错误，保证不崩溃（N1）。
        """
        yield AgentEvent(type=AgentEventType.TOOL_START, tool_call=tc)

        if not isinstance(tc.arguments, dict):
            res = _invalid_args_result(tc)
            results[tc.id] = res
            yield AgentEvent(type=AgentEventType.TOOL_RESULT, tool_call=tc, tool_result=res)
            return

        if tc.name == ASK_USER:
            if clarify is None:
                res = ToolResult(ok=False, output="当前不支持向用户澄清（缺少澄清回调）。")
            else:
                question = str(tc.arguments.get("question", "")).strip()
                options = self._parse_options(tc.arguments.get("options"))
                if not options:
                    res = ToolResult(ok=False, output="ask_user 需要至少一个候选项 options。")
                else:
                    chosen = clarify(question, options)
                    if chosen is None:
                        ctx.cancelled = True
                        res = ToolResult(ok=False, output="用户取消了澄清。", summary="用户取消")
                    else:
                        res = ToolResult(ok=True, output=f"用户选择：{chosen}", summary=f"用户选择：{chosen}")
        else:  # PRESENT_PLAN
            if approve_plan is None:
                res = ToolResult(ok=False, output="当前不支持计划审批（缺少审批回调）。")
            else:
                plan = str(tc.arguments.get("plan", "")).strip()
                if plan:
                    yield AgentEvent(type=AgentEventType.TEXT, text=plan)
                approved = approve_plan(plan)
                if approved:
                    ctx.approved = True
                    res = ToolResult(ok=True, output="用户已批准计划，开始执行。", summary="已批准，开始执行")
                else:
                    ctx.plan_rejected = True
                    res = ToolResult(
                        ok=True,
                        output="用户暂未批准计划，已停止本次执行。",
                        summary="未批准",
                    )

        results[tc.id] = res
        yield AgentEvent(type=AgentEventType.TOOL_RESULT, tool_call=tc, tool_result=res)

    @staticmethod
    def _parse_options(raw) -> list[ClarifyOption]:
        """
        把模型给出的 options 原始数据（list[dict]）解析为 list[ClarifyOption]。

        宽松解析：跳过非字典项、缺 summary 的项；detail 缺省为空串。保证不因模型输出瑕疵崩溃。
        """
        if not isinstance(raw, list):
            return []
        options: list[ClarifyOption] = []
        for item in raw:
            if not isinstance(item, dict):
                continue
            summary = str(item.get("summary", "")).strip()
            if not summary:
                continue
            detail = str(item.get("detail", "")).strip()
            options.append(ClarifyOption(summary=summary, detail=detail))
        return options

    def _run_readonly_concurrent(
        self,
        items: list[tuple[ToolCall, Tool]],
        results: dict[str, ToolResult],
        cwd: Optional[Path] = None,
    ) -> Iterator[AgentEvent]:
        """
        并发执行一组只读工具（迁移自 c3，改产出 AgentEvent）。

        先为每个调用产出 TOOL_START，再用线程池并发执行，谁先完成先产出 TOOL_RESULT。
        参数解析失败的调用不进线程池，直接结构化错误。
        """
        for tc, _tool in items:
            yield AgentEvent(type=AgentEventType.TOOL_START, tool_call=tc)

        for tc, _tool in items:
            if not isinstance(tc.arguments, dict):
                res = _invalid_args_result(tc)
                results[tc.id] = res
                self._trace_tool(tc, res, OUTCOME_INVALID_ARGUMENTS, is_concurrent=True, cwd=cwd)
                yield AgentEvent(type=AgentEventType.TOOL_RESULT, tool_call=tc, tool_result=res)

        valid = [(tc, tool) for tc, tool in items if isinstance(tc.arguments, dict)]
        if not valid:
            return

        # 结果循环里只拿得到 tc，而 Hook 的后置事件负载要用 tool.read_only，
        # 因此先建一张 id → 工具 的表。
        tools_by_id = {tc.id: tool for tc, tool in valid}

        # 提交任务**之前**捕获父作用域（trace T32）。
        #
        # 准确的理由（别写成「否则 tool_execute 会被记成 main」——那是错的）：
        # 下面 `as_completed` 循环里的 `_trace_tool` 跑在**生成器所在的 Worker 线程**上，
        # 作用域天然正确。真正跑在池线程里的埋点是 **`tool.execute` 内部产生的事件**
        # ——今天唯一的实例是 `load_skill` → `SkillManager.activate` → `skill_state`。
        # 本包装是纵深防御：兜住工具内部的埋点，并为将来在工具内部埋点留出正确语义。
        # `threading.local()` 不跨线程继承，不显式传就没有别的办法。
        parent_scope = self._safe_scope()
        # 各调用的实际执行耗时（毫秒），由包装函数在池线程里填、主线程读。
        # dict 的单键赋值在 CPython 下是原子的，且每个 tc.id 只被一个线程写一次。
        durations: dict[str, float] = {}

        def _run(tool: Tool, tc: ToolCall):
            """池线程里的任务入口：绑定父作用域、计时，然后执行工具。"""
            # 用 _safe_bind 而不是直接 bind_scope：这里是**唯一**「异常会被
            # future.result() 变成工具结果回灌模型」的位置，必须绝对安全。
            self._safe_bind(parent_scope)
            t0 = time.monotonic()
            try:
                # ⚠ **c14：并发路径同样要传 cwd，这一条极易漏。**
                #
                # 既有的 `plan_stage` 只在**串行**路径传递（它只对非只读工具有
                # 意义），照抄那个写法就会漏掉这里——而 `read_file` /
                # `glob_files` / `grep_content` 全是只读工具、全走**这条**路。
                #
                # 漏掉的后果：隔离子 Agent 的**读**落到主项目根、**写**却是对的。
                # 它既不报错也不越权，只是读到了另一份文件——界面上完全看不出来。
                # 护栏见 `tests/test_loop_cwd_dispatch.py`。
                if tool.workspace_aware:
                    return tool.execute(tc.arguments, cwd=cwd)
                return tool.execute(tc.arguments)
            finally:
                # **不加 try/except 吞异常**：既有的 `future.result()` 兜底逻辑
                # 不能变。埋点自身的异常由 recorder 内部兜住。
                durations[tc.id] = round((time.monotonic() - t0) * 1000, 3)

        with ThreadPoolExecutor(max_workers=len(valid)) as executor:
            future_to_tc = {
                executor.submit(_run, tool, tc): tc
                for tc, tool in valid
            }
            for future in as_completed(future_to_tc):
                tc = future_to_tc[future]
                try:
                    res = future.result()
                except Exception as e:
                    res = ToolResult(ok=False, output=f"工具执行异常: {e}")
                results[tc.id] = res
                duration = durations.get(tc.id, 0)
                self._trace_tool(
                    tc,
                    res,
                    OUTCOME_EXECUTED,
                    duration_ms=duration,
                    is_concurrent=True,
                    cwd=cwd,
                )
                # 工具级后置事件（c12）：只挂在「真的执行了」之后（spec AC3）。
                # 这里跑在生成器所在的 Worker 线程上，不在池线程里。
                self._dispatch_post_tool(tc, tools_by_id[tc.id], res, duration, cwd)
                yield AgentEvent(type=AgentEventType.TOOL_RESULT, tool_call=tc, tool_result=res)

    def _run_one_serial(
        self,
        tc: ToolCall,
        tool: Optional[Tool],
        decision: Optional[DecisionResult],
        results: dict[str, ToolResult],
        ctx: _RoundContext,
        ask: AskFn,
        interactive: bool = True,
        planning: bool = False,
        cwd: Optional[Path] = None,
        unattended: bool = False,
    ) -> Iterator[AgentEvent]:
        """
        串行处理单个工具调用：未知 / 参数错误 / 权限拒绝 / 待确认 / 放行（c6）。

        各分支的结果都写入 results 并产出事件；任何「不执行」的分支（未知、参数错误、
        权限拒绝、用户拒绝）都返回 ok=False 的结构化结果回灌模型，但**不终止循环**
        （spec F8）——与未知工具/参数错误的既有处理一致。

        :param tc: 工具调用
        :param tool: 对应工具实例；None 表示未知工具
        :param decision: 权限决策结果；None 表示参数解析失败（未进引擎）
        :param results: 输出参数，写入 id → ToolResult
        :param ask: 人工确认回调（仅 decision 为 ASK **且 interactive 为真**时调用）
        :param interactive: 能否与人交互（c13）。为假时 ASK 直接判拒并回灌
            `DENIED_NON_INTERACTIVE_FEEDBACK`，`ask` 一次都不会被调用。
            **缺省 True 即既有行为。**
        :param planning: 是否处于 Plan Mode 的规划阶段（c13）。只用于把阶段
            告知 `plan_safe` 工具——它们据此自我约束（见 `Tool.plan_safe`）。
            **缺省 False 即既有行为。**
        """
        # 未知工具：结构化错误（沿用既有行为，含 TOOL_START）
        if tool is None:
            yield AgentEvent(type=AgentEventType.TOOL_START, tool_call=tc)
            res = ToolResult(ok=False, output=f"未知工具: {tc.name}")
            results[tc.id] = res
            self._trace_tool(tc, res, OUTCOME_UNKNOWN_TOOL, cwd=cwd)
            yield AgentEvent(type=AgentEventType.TOOL_RESULT, tool_call=tc, tool_result=res)
            return

        # 参数解析失败（decision 为 None）
        if decision is None:
            yield AgentEvent(type=AgentEventType.TOOL_START, tool_call=tc)
            res = _invalid_args_result(tc)
            results[tc.id] = res
            self._trace_tool(tc, res, OUTCOME_INVALID_ARGUMENTS, cwd=cwd)
            yield AgentEvent(type=AgentEventType.TOOL_RESULT, tool_call=tc, tool_result=res)
            return

        # 权限拒绝：产出结构化拒绝结果，不执行（与「用户拒绝」一致，仅产出 TOOL_RESULT）
        if decision.decision == Decision.DENY:
            res = ToolResult(
                ok=False,
                output=f"[权限拒绝·{decision.layer.value}] {decision.reason}",
                summary="权限拒绝",
            )
            results[tc.id] = res
            self._trace_tool(tc, res, OUTCOME_DENIED_BY_PERMISSION, cwd=cwd)
            yield AgentEvent(type=AgentEventType.TOOL_RESULT, tool_call=tc, tool_result=res)
            return

        # 待确认（非交互执行）：没有人能应答面板，直接拒绝（c13 spec F15）。
        #
        # **刻意排在调 `ask` 之前**：`ask` 回调在子 Agent 场景下会跨线程弹面板，
        # 一旦真的走进去就是「后台线程往主界面弹了一个没人预期的面板」。
        # 这里拦住，那条路径压根不会被触达。
        if decision.decision == Decision.ASK and not interactive:
            # c15：同样是「没人能应答」，但**成因不同、下一步也不同**。
            # 子 Agent 该换只读方式把活干完；无人值守的主对话该回消息给队友、
            # 并把「需要用户批准什么」留给回来的用户。见两个常量的对照表。
            template = (
                DENIED_UNATTENDED_FEEDBACK if unattended
                else DENIED_NON_INTERACTIVE_FEEDBACK
            )
            res = ToolResult(
                ok=False,
                output=template.format(name=tc.name),
                summary="无人值守自动拒绝" if unattended else "非交互环境自动拒绝",
            )
            results[tc.id] = res
            # **不置 `ctx.user_denied`**：那个标志会让下一轮 `tools=None`。
            # 子 Agent 应当带着工具继续、改用只读方式达成，见常量注释。
            self._trace_tool(tc, res, OUTCOME_DENIED_NON_INTERACTIVE, cwd=cwd)
            yield AgentEvent(type=AgentEventType.TOOL_RESULT, tool_call=tc, tool_result=res)
            return

        # 待确认：弹 HITL 面板，用户拒绝则不执行
        if decision.decision == Decision.ASK:
            approved = ask(tc, tool, decision)
            if not approved:
                res = ToolResult(ok=False, output=DENIED_BY_USER_FEEDBACK.format(name=tc.name))
                results[tc.id] = res
                # 置位后主循环下一轮**不发工具**（硬约束，见 DENIED_BY_USER_FEEDBACK 注释）
                ctx.user_denied = True
                self._trace_tool(tc, res, OUTCOME_DENIED_BY_USER, cwd=cwd)
                yield AgentEvent(type=AgentEventType.TOOL_RESULT, tool_call=tc, tool_result=res)
                return

        # 放行（ALLOW 或确认通过）：执行工具
        yield AgentEvent(type=AgentEventType.TOOL_START, tool_call=tc)
        t0 = time.monotonic()
        try:
            if tool.plan_safe:
                # 声明了 `plan_safe` 的工具**必须**接受这个关键字参数
                # （契约写在 `Tool.plan_safe` 的说明里）。多传它是为了让工具
                # 能在规划阶段自我约束——循环不该也不能替它做那个判断，
                # 那需要认识具体工具的语义。
                # c14：两个标志**各自独立判断**，不要写成 if/elif——
                # 一个工具完全可能既 plan_safe 又 workspace_aware。
                if tool.workspace_aware:
                    res = tool.execute(tc.arguments, plan_stage=planning, cwd=cwd)
                else:
                    res = tool.execute(tc.arguments, plan_stage=planning)
            elif tool.workspace_aware:
                res = tool.execute(tc.arguments, cwd=cwd)
            else:
                res = tool.execute(tc.arguments)
        except Exception as e:
            res = ToolResult(ok=False, output=f"工具执行异常: {e}")
        results[tc.id] = res
        duration = round((time.monotonic() - t0) * 1000, 3)
        self._trace_tool(tc, res, OUTCOME_EXECUTED, duration_ms=duration, cwd=cwd)
        # 工具级后置事件（c12）：只挂在「真的执行了」之后。上面每一条提前 return
        # 的分支（未知工具 / 参数错误 / 权限拒绝 / 用户拒绝）都不挂——它们压根没跑。
        self._dispatch_post_tool(tc, tool, res, duration, cwd)
        yield AgentEvent(type=AgentEventType.TOOL_RESULT, tool_call=tc, tool_result=res)
