"""
Context 层的纯数据结构（c8）。

本模块只定义 dataclass，不含任何行为逻辑，作用是让 Context 层内部各模块
（estimate / summarize / manager）与上层（loop / conversation / TUI）
通过轻量数据类型交换信息，彼此不产生行为耦合。

两个类型：
- CompactionNotice：一次压缩动作的结果通知，最终被 loop 转成 AgentEvent(NOTICE) 展示给用户。
- ContextStats：当前上下文用量快照，供 /context 命令渲染只读报告。
"""

from dataclasses import dataclass


@dataclass
class CompactionNotice:
    """
    一次压缩动作（摘要 / 熔断 / 空操作）的结果通知。

    ContextManager 的各方法（before_request / manual_compact）产出本类型，
    上层据 message 给用户一行可读反馈（c8 F17）。kind 便于上层/测试区分动作类别。

    :param kind: 动作类别——
                 "summary"（LLM 摘要成功或失败）/
                 "circuit_break"（摘要连续失败触发熔断）/ "noop"（本次无需压缩）

                 ⚠ 2026-09-17 之前还有一个 `"offload"`（第一层存盘），
                 随那一层整个删除，**别再加回来**（理由见 `manager.py` 模块
                 docstring 里那段）。
    :param message: 面向用户的一行中文反馈文本
    """

    kind: str
    message: str


@dataclass
class ContextStats:
    """
    当前上下文用量快照，供 `/context` 命令展示（c8 F16）。

    :param estimated_tokens: 当前对话历史的近似 token 估算值
    :param window: 上下文窗口上限（token），来自配置
    :param headroom: 距窗口上限的余量 = window - estimated_tokens（可能为负，表示已超估）
    :param circuit_broken: LLM 摘要是否已因连续失败而熔断
    """

    estimated_tokens: int
    window: int
    headroom: int
    circuit_broken: bool
