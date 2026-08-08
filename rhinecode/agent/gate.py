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


class CompositeGate:
    """
    把多个闸门并成一个（c15 T18）。

    C15 让子 Agent 之间能互相发消息，而**消息注入与子 Agent 结论注入
    在循环里的处理逐字相同**——都是「取出来 → 追加进历史 → 交给存档」。
    因此不给循环加第二个调用点，改为在这里组合：

    - **主对话**传 `CompositeGate([SubAgentGate, TeamGate("main")])`；
    - **子 Agent** 传 `TeamGate(<它自己的名字>)`（C13 时它用的是 `NullGate`）。

    于是 `agent/loop.py` 的注入逻辑一行都不用改。

    ## ⚠ 本类必须留在 `agent/` 这一侧

    与协议同一条理由：`agent` 层**不得依赖 `subagents` / `team`**
    （它们都是 `agent.loop` 的使用者），硬去 import 会直接撞循环导入——
    C13 实测过，报错是 `cannot import name 'Agent' from partially
    initialized module`。本类只依赖协议本身，不认识任何具体实现。

    :param gates: 若干个满足 `SubAgentGateProtocol` 的对象。
        `None` 会被过滤掉，因此调用方可以直接传
        `[maybe_subagent_gate, maybe_team_gate]` 而不必先自己筛。
    """

    def __init__(self, gates) -> None:
        self._gates = tuple(g for g in gates if g is not None)

    def take_pending(self) -> "list[Message]":
        """
        按传入顺序依次取，结果拼接。

        顺序即优先级：主对话那边先放子 Agent 结论、再放队友消息。
        两者在同一轮注入时，先看到「我派出去的活回来了」更符合模型的
        思考顺序（它多半正等着那个结果）。
        """
        out: "list[Message]" = []
        for gate in self._gates:
            out.extend(gate.take_pending())
        return out

    def has_awaited(self) -> bool:
        """任一闸门还有要等的东西。"""
        return any(gate.has_awaited() for gate in self._gates)

    def wait_any(self, cancel_event) -> bool:
        """
        等到任一闸门有东西可交付。

        ⚠ **只对 `has_awaited()` 为真的闸门调 `wait_any`。**

        `wait_any` 是**阻塞**的：无差别地逐个调用，第一个会一直等下去，
        排在后面的永远轮不到。先问 `has_awaited` 就把「没什么可等的」
        那些跳过去了——而本章的 `TeamGate.has_awaited` 恒为假，
        因此它根本不会被调到，队员也就不会为「可能有人给我发消息」
        赖着不收工。
        """
        for gate in self._gates:
            if gate.has_awaited() and gate.wait_any(cancel_event):
                return True
        return False

    def describe_awaited(self) -> str:
        """把各闸门的描述拼起来，空的跳过。"""
        parts = [gate.describe_awaited() for gate in self._gates]
        return "、".join(part for part in parts if part)


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


__all__ = ["MAX_WAIT_ROUNDS", "CompositeGate", "NullGate", "SubAgentGateProtocol"]
