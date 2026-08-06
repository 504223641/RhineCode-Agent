"""
Agent Loop 的「子 Agent 闸门」协议（c13 修订）。

## 为什么协议定义在这一层

循环需要在两个时机跟子 Agent 系统打交道：

1. **每轮组装请求前**——把已完成的子 Agent 结论注入历史；
2. **模型准备自然结束时**——若还有「声明过要等结果」的子 Agent 在跑，就等它。

但 `agent` 层**不得依赖 `subagents` 包**（依赖方向：上层可依赖下层，反之不可；
而 `subagents.runner` 正是 `agent.loop` 的使用者）。硬去 import 会直接撞上
循环导入——实测过，报错是 `cannot import name 'Agent' from partially
initialized module`。

所以：**协议与空对象定义在消费方（这里），实现留在提供方**
（`rhinecode/subagents/gate.py`）。`subagents` 依赖 `agent` 是允许的方向。

## 为什么不设等待超时

跑一个子 Agent **就是在执行任务**，与主 Agent 自己调 `run_command` 跑一遍
测试套件性质相同——没人会为「跑测试太久」设计一个「别等了」的开关。
给等待设一个体验意义上的上限会把正常工作腰斩（一个 `max_turns: 20` 的
子 Agent 真跑起来可能要五分钟）。

**唯一的逃生口是 `Esc`**，因此 `wait_any` 必须接 `cancel_event` 并能被它
立刻打断。「我不需要等这个结果」由**模型**在委派时用 `background=true` 表达。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:  # pragma: no cover —— 仅类型检查期
    import threading

    from rhinecode.provider.base import Message


# 一次运行里最多为子 Agent 停留多少次。
#
# 兜底安全网，防「等到一个 → 模型又委派一个 → 再等」无限接力。撞上它就正常
# 收工，未交付的结论仍会在下一条用户消息时送达（协调层还有一道交付点）。
MAX_WAIT_ROUNDS = 20


@runtime_checkable
class SubAgentGateProtocol(Protocol):
    """循环对子 Agent 系统的全部要求，只有四个方法。"""

    def take_pending(self) -> "list[Message]":
        """取走已完成、尚未交付的结论，转成要追加进历史的消息。幂等。"""
        ...

    def has_awaited(self) -> bool:
        """还有没有「声明过要等结果」的子 Agent 在跑。"""
        ...

    def wait_any(self, cancel_event: "threading.Event") -> bool:
        """阻塞直到有结论可交付或已无可等；被 `cancel_event` 立刻打断。"""
        ...

    def describe_awaited(self) -> str:
        """等待中的任务简述，用于告诉用户循环停在这儿是在等什么。"""
        ...


class NullGate:
    """
    不启用子 Agent 时的空对象。

    每个方法都返回「什么都没有」的安全值，于是循环里那两处判断退化成
    零成本空调用——**不传 `subagent_gate` 等于零回归**。
    """

    def take_pending(self) -> "list[Message]":
        return []

    def has_awaited(self) -> bool:
        return False

    def wait_any(self, cancel_event) -> bool:  # pragma: no cover —— 不会被调到
        return False

    def describe_awaited(self) -> str:  # pragma: no cover
        return ""


__all__ = ["MAX_WAIT_ROUNDS", "NullGate", "SubAgentGateProtocol"]
