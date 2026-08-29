"""
子 Agent 闸门：Agent Loop 与子 Agent 之间的窄接口（c13 修订）。

## 它解决什么

初版把「主 Agent 需要这个结果」实现成**在工具调用里同步等**，造成两个缺陷
（真实使用中暴露）：

1. **结论要等到下一条用户消息才到手**——交付点挂在 `_run()` 上，而那是
   「每条用户消息一次」。主 Agent 在同一次运行里跑了六轮，结论一轮都没进去。
   这等于把一个本该连贯的任务硬生生截断，用户必须插一句话才能让它继续。
2. **多个委派串行**——委派工具是 `system_serial`，前台路径又在串行桶里阻塞
   等待，于是三个各花 0.6 秒的子 Agent 要跑 1.86 秒。

两者是同一个设计错误的两面：**发起与等待被捆在了一起**。

## 修法：发起与等待分离

- **委派永远立即返回**（于是天然并行）；
- **交付点下移到迭代级**：Agent Loop 每轮组装请求前取一次结论；
- **等待交给循环**：模型不再调工具、准备自然结束时，若还有「需等待」的子 Agent，
  循环就阻塞等它们、把结论注入后**再跑一轮**。

## ⚠ 等待不是卡顿，是进度

这一条决定了本模块**没有**超时旋钮。

跑一个子 Agent 就是在执行任务，与主 Agent 自己调 `run_command` 跑一遍测试
套件性质相同——没人会为「跑测试太久」设计一个「别等了」的开关。
给等待设一个体验意义上的上限（初版设过 180 秒）是错的：一个 `max_turns: 20`
的子 Agent 真跑起来可能就要五分钟，那个数字会把正常工作腰斩。

**唯一的逃生口是 `Esc`**（取消整轮），因此 `wait_any` 必须能被取消信号
**立刻**打断。`WAIT_HARD_LIMIT` 只是防「线程挂死」的兜底，不是给用户调的。

「我不需要等这个结果」这件事由**模型**在委派时用 `background=true` 表达，
不需要用户中途干预——那个语义空间已经被 `background` 参数、`Esc`、
`/agents cancel <标识>` 三者占满了。

## 协议定义在哪

**在 `rhinecode/agent/gate.py`**，不在这里。`agent` 层不得依赖 `subagents` 包
（`subagents.runner` 正是 `agent.loop` 的使用者，反向 import 会直接撞循环导入
——我第一版就是这么写的，报错是 `cannot import name 'Agent' from partially
initialized module`）。

所以协议与空对象归消费方，本模块只提供**实现**。
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover —— 仅类型检查期
    from rhinecode.provider.base import Message

from rhinecode.subagents.tasks import STATUS_LABELS, TaskManager

# 防「线程挂死」的兜底上限（秒）。**不是给用户调的体验旋钮**，见模块 docstring。
#
# 正常情况下永远等不到它：子 Agent 有自己的 `max_turns`，运行器的 `finally`
# 保证任务一定走到终态。它防的是「某个网络调用没有超时、线程永远卡住」
# 这类真正的挂死——那时循环至少还能自己走出来，而不是让用户干瞪眼。
WAIT_HARD_LIMIT = 1800.0

# 轮询间隔。等的是以「秒」计的模型调用，0.05 秒完全够用。
_POLL_INTERVAL = 0.05


class SubAgentGate:
    """
    真实闸门，由协调层构造并透传给 `Agent.run`。

    :param tasks: 任务表（唯一数据来源）
    :param render: 把一条已完成的任务渲染成要追加进历史的消息。
        由协调层提供——**本类不认识 `Message` 的构造细节**，
        那属于协调层的「怎么跟模型说话」的知识。

    线程模型：本类的方法只在 Agent Loop 所在线程调用。内部无可变状态
    （任务表自己是线程安全的），因此不需要加锁。
    """

    def __init__(self, tasks: TaskManager, render) -> None:
        self._tasks = tasks
        self._render = render

    # ------------------------------------------------------------------ #
    # 交付（每轮迭代开头调）
    # ------------------------------------------------------------------ #

    def take_pending(self) -> "list[Message]":
        """
        取走已完成、尚未交付的结论，转成要追加进历史的消息。

        :returns: 消息列表；没有待交付时为空列表

        **取走即置位**，因此重复调用幂等——同一段结论不会在历史里出现两遍。

        由 Agent Loop 在**每轮组装请求之前**调用。这个位置是合法的：
        协议只禁止在 `assistant(tool_calls)` 与它对应的 `tool` 结果**之间**
        插消息，而两轮迭代之间上一轮的 tool 结果早已写完。

        副作用：改任务的 `delivered` 标志。
        """
        return [self._render(record) for record in self._tasks.take_deliverables()]

    # ------------------------------------------------------------------ #
    # 等待（循环准备自然结束时调）
    # ------------------------------------------------------------------ #

    def has_awaited(self) -> bool:
        """
        还有没有「需要等」的子 Agent 在跑。

        `background=true` 发起的任务 `awaited` 为假——那是模型明确说过
        「这次我不需要它的结果」的，循环不该为它停留。
        """
        return any(
            r.awaited and not r.status.is_terminal for r in self._tasks.snapshot()
        )

    def describe_awaited(self) -> str:
        """
        等待中的任务简述，用于告诉用户「循环停在这儿是在等什么」。

        这条提示不可省：没有它，界面上表现为「AI 不说话了」，
        用户分不清是在等还是卡死了。
        """
        names = [
            r.label
            for r in self._tasks.snapshot()
            if r.awaited and not r.status.is_terminal
        ]
        return "、".join(names)

    def wait_any(self, cancel_event, timeout: float = WAIT_HARD_LIMIT) -> bool:
        """
        阻塞直到「有结论可交付」或「没有需要等的任务了」。

        :param cancel_event: 取消信号。**必须传**——`Esc` 是等待期间唯一的
            逃生口，不检查它就等于按了没用
        :param timeout: 兜底上限，见 `WAIT_HARD_LIMIT`
        :returns: True = 有东西可交付 / 已无可等；False = 被取消或撞上兜底上限

        实现用轮询而不是等一个聚合 Event：任务是动态增减的，聚合 Event 要在
        任务表里挂回调，而 `TaskManager` **刻意不持有任何回调**
        （那是防死锁的结构性约定，见 `tasks.py`）。

        **一有结论落地就返回**，不等全部跑完：先把它交给模型，让模型自己决定
        还要不要继续等剩下的——这比替它做决定好，也让并行的多个子 Agent
        能陆续回流而不是齐头等到最后一个。

        副作用：阻塞调用线程。
        """
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if cancel_event is not None and cancel_event.is_set():
                return False
            snapshot = self._tasks.snapshot()
            # 已经有结论落地 → 立刻返回，让模型先看到它
            if any(r.status.is_terminal and not r.delivered for r in snapshot):
                return True
            # 没有需要等的了（都跑完且都交付过了）→ 该结束循环了
            if not any(r.awaited and not r.status.is_terminal for r in snapshot):
                return False
            time.sleep(_POLL_INTERVAL)
        return False


def render_subagent_message(record) -> "Message":
    """
    把一条已完成的任务渲染成要追加进主历史的消息。

    :param record: 任务记录
    :returns: `role="user"` 的消息，正文包在 `<subagent-result>` 标记块里

    用 `user` 而不是 `tool`：这条消息与任何 `tool_call_id` 都不配对
    （委派工具早在发起时就返回过结果了），当成工具结果回灌会破坏协议。
    包一层标记块是为了让模型知道**这不是人在说话**。

    `display_content` 置空串，使界面与 `/resume` 回放**不**把它显示成一条
    用户输入——完成通知已经由 TUI 的轮询单独出过一行了。
    """
    from rhinecode.provider.base import Message

    status = STATUS_LABELS.get(record.status, record.status.value)
    content = (
        f'<subagent-result agent="{record.agent_name}" '
        f'task_id="{record.task_id}" status="{record.status.value}">\n'
        f"（{status} · {record.turns} 轮 · {record.duration_seconds:.1f}s）\n"
        f"{record.conclusion}\n"
        f"</subagent-result>"
    )
    return Message(role="user", content=content, display_content="")


__all__ = ["SubAgentGate", "render_subagent_message", "WAIT_HARD_LIMIT"]
