"""
Agent Loop 的事件层与共享数据类型。

本模块只定义「纯数据」（枚举与 dataclass），不含任何行为逻辑，目的：
- 让 Agent 循环与 TUI 彻底解耦——循环只产出 AgentEvent，TUI 只消费 AgentEvent
- 作为各层共享的轻量类型层，避免出现「TUI 依赖 loop、loop 依赖 TUI」的循环依赖

设计要点：
- AgentEvent 用单一 dataclass + type 字段区分种类（与 provider.StreamChunk 同思路），
  不同 type 下只有部分字段有意义（见各字段注释），未用字段保持默认值。
- 「展示用」信息（文本/工具进展/用量/进度/结束）走 AgentEvent 单向事件流；
  「需要用户决定」的交互（确认/澄清/审批）不在这里，而是通过阻塞回调完成
  （ConfirmDecision / ClarifyOption 是那些回调的入参/返回值类型）。
"""

from dataclasses import dataclass
from enum import Enum
from typing import Any, Optional

from rhinecode.provider.base import ToolCall


class AgentEventType(str, Enum):
    """
    Agent 循环对外产出的事件种类。

    继承 str 便于直接和字符串比较、打印调试。各类型对应的有效载荷见 AgentEvent 字段注释。
    """

    TEXT = "text"            # 模型正文文本增量
    THINKING = "thinking"    # 模型思考内容增量（Thinking Mode）
    TOOL_START = "tool_start"    # 某工具开始执行
    TOOL_RESULT = "tool_result"  # 某工具执行完成
    USAGE = "usage"          # 一轮请求的 token 用量
    PROGRESS = "progress"    # 进入新一轮迭代
    FINISHED = "finished"    # 循环结束（携带结束原因）
    ERROR = "error"          # 发生错误（携带可读描述）


class StopReason(str, Enum):
    """
    循环结束原因，与 spec F2 的几种停止条件一一对应。

    每次循环结束都会产出一个 FINISHED 事件并带上其中之一，供 TUI 给用户清晰反馈。
    """

    COMPLETED = "completed"            # 模型本轮不再发起工具调用，自然完成
    MAX_ITERATIONS = "max_iterations"  # 达到迭代上限（兜底安全网）
    USER_CANCELLED = "user_cancelled"  # 用户主动取消
    PLAN_REJECTED = "plan_rejected"    # 用户拒绝执行计划
    UNKNOWN_TOOL = "unknown_tool"      # 连续调用未知工具达到阈值
    STREAM_ERROR = "stream_error"      # 底层流出错


class ConfirmDecision(str, Enum):
    """
    人在回路（HITL）确认面板的四态返回值（c6 spec F6）。

    当决策管线对某工具调用判定为 ASK 时弹出面板，用户从四个选项里择一：

    - ALLOW：仅放行本次执行
    - ALLOW_SESSION：本会话放行——为「该工具 + 本次目标」登记一条会话级 allow 规则，
      本会话内后续相同调用直接放行（关程序即失效）。注意这是「按规则」的放行，
      不再是 c5 那种「一刀切免确认」
    - ALLOW_PERMANENT：永久放行——把同样的 allow 规则写入本地级配置文件，
      重启后仍生效（spec F6）
    - DENY：拒绝执行（把「用户拒绝执行」作为结构化结果回灌模型，不终止循环）
    """

    ALLOW = "allow"
    ALLOW_SESSION = "allow_session"
    ALLOW_PERMANENT = "allow_permanent"
    DENY = "deny"


@dataclass
class Usage:
    """
    一轮模型请求的 token 用量。

    由 Provider 从流末尾的 usage 字段解析，循环累计后供 TUI 在状态栏展示。

    :param prompt_tokens: 输入（提示）消耗的 token 数
    :param completion_tokens: 输出（补全）消耗的 token 数
    :param total_tokens: 合计 token 数
    :param prompt_cache_hit_tokens: 输入中命中前缀缓存的 token 数（DeepSeek 的
                                    prompt_cache_hit_tokens 字段）；命中越多说明稳定系统
                                    提示等前缀复用得越好，请求越省钱省时间（c5 F10）
    :param prompt_cache_miss_tokens: 输入中未命中缓存、按全价计费的 token 数
                                     （DeepSeek 的 prompt_cache_miss_tokens 字段）
    """

    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    # 缓存命中/未命中字段：非 DeepSeek 协议通常不返回这两个字段，此时由 collector 填 0。
    prompt_cache_hit_tokens: int = 0
    prompt_cache_miss_tokens: int = 0


@dataclass
class ClarifyOption:
    """
    Plan Mode 需求澄清面板中的单个选项（spec F12）。

    模型通过 ask_user 工具给出若干选项，每个选项含「概述」与「详细描述」：
    用户在面板里上下导航只在概述之间移动，详情用于帮助判断。

    :param summary: 概述——一行短文本，作为可被上下导航选中的条目
    :param detail: 详细描述——说明该选项的含义与取舍，仅展示、不可单独选中
    """

    summary: str
    detail: str = ""


@dataclass
class AgentEvent:
    """
    Agent 循环对外产出的单个事件。

    用 type 区分种类，不同 type 下只有部分字段有意义：
    - TEXT / THINKING：text 为增量内容
    - TOOL_START：tool_call 为开始执行的调用
    - TOOL_RESULT：tool_call 为对应调用，tool_result 为执行结果（tools.base.ToolResult）
    - USAGE：usage 为本轮用量
    - PROGRESS：iteration 为当前迭代序号（从 1 开始）
    - FINISHED：stop_reason 为结束原因，message 为可选补充说明
    - ERROR：message 为可读错误描述

    :param type: 事件类型
    :param text: TEXT/THINKING 的增量文本
    :param tool_call: TOOL_START/TOOL_RESULT 关联的工具调用
    :param tool_result: TOOL_RESULT 携带的执行结果（标 Any 以免 agent 反向依赖 tools 形成环）
    :param usage: USAGE 携带的用量
    :param iteration: PROGRESS 携带的当前迭代序号
    :param stop_reason: FINISHED 携带的结束原因
    :param message: ERROR 的错误描述 / FINISHED 的补充说明
    """

    type: AgentEventType
    text: str = ""
    tool_call: Optional[ToolCall] = None
    tool_result: Any = None
    usage: Optional[Usage] = None
    iteration: int = 0
    stop_reason: Optional[StopReason] = None
    message: str = ""
