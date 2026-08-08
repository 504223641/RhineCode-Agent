"""
消息路由与投递（c15 T13/T14，spec F9/F10/F11/F14）。

## 它是什么

点对点消息的**策略层**：校验、组织可读的失败原因、以及**在锁外唤醒**收件人。
消息数据本身住在花名册的 `MemberEntry.inbox` 里（`roster.py` 提供了三个
受控原语），本模块不另存一份。

分层的理由：花名册回答「能不能收、收下了、要唤醒谁」，本模块回答
「说什么话给模型听」。把文案放进花名册会让那个纯数据类背上一堆措辞，
而措辞是要反复打磨的东西——每改一句就动一次并发数据结构，不划算。

## 投递是推送式的，没有「查收件箱」这回事

对齐 Claude Code：「Messages from teammates are delivered automatically;
**you don't check an inbox**」。本章因此**不提供任何查收件箱的工具**，
收件人在下面两条路径之一上被动看到消息：

- **正在跑**：它的闸门在下一轮迭代开头 `take_unread` 并注入历史；
- **待命中**：`wake_event` 把它的线程叫醒，它取回历史继续跑。

主对话是第三条：TUI 的定时轮询发现有未读就触发一次「自动轮」（spec F17）。

## ⚠ 三段式：锁内写、出锁、锁外唤醒

```
① 锁内（在 roster.deliver 里）：校验 → 追加消息 → 取出要唤醒的事件
② 出锁
③ 锁外：event.set()
```

`Event.set()` 唤醒等待线程，属**跨线程调度**。落进临界区会与 Textual
阻塞式 `call_from_thread` 组成**确定性死锁，整个 TUI 冻结**，而调用栈上
没有任何线索——C11 `SkillManager`、C12 `HookManager`、C13 `TaskManager`
已经踩过三次，本章是第四次同源风险。
"""

from __future__ import annotations

from dataclasses import dataclass

from rhinecode.team.models import MAIN_NAME, Envelope
from rhinecode.team.roster import Roster


@dataclass(frozen=True)
class SendResult:
    """
    一次发送的结果。

    :param ok: 是否送达
    :param reason: 失败原因（可读中文，**直接回灌给模型**，
        因此必须包含足够它自我纠正的信息）
    :param envelope: 送出的消息；失败时为 `None`
    """

    ok: bool
    reason: str = ""
    envelope: "Envelope | None" = None


class Mailbox:
    """
    消息路由。一个会话一份，由 `TeamService` 持有。

    :param roster: 花名册。信箱数据住在它里面，本类只做路由与文案。

    线程模型：本类**自己不持有任何可变状态**（全部落在花名册里），
    因此不需要自己的锁。加锁由花名册的原语负责，唤醒由本类在锁外做。
    """

    def __init__(self, roster: Roster) -> None:
        self._roster = roster

    def send(
        self, sender: str, recipient: str, body: str, summary: str = ""
    ) -> SendResult:
        """
        发一条消息（spec F9/F10）。

        :param sender: 发件人名字（某个队员，或 `main`）
        :param recipient: 收件人名字
        :param body: 正文
        :param summary: 一句话摘要，界面单行展示用；为空时从正文首行截取
        :returns: `SendResult`

        四种失败，每一种都给出可自我纠正的信息：

        1. 正文为空——发一条空消息只会浪费对方一轮迭代；
        2. 发给自己——通常是模型把「记笔记」误当成「发消息」；
        3. 收件人不存在——**列出当前花名册**，让它用对的名字重试；
        4. 收件人已出局——**说明是失败 / 取消 / 已退休哪一种**，
           并提示改为委派一个新队员。

        副作用：往收件人信箱追加一条消息；**在锁外**唤醒待命中的收件人。
        """
        sender = (sender or "").strip()
        recipient = (recipient or "").strip()
        body = (body or "").strip()

        if not body:
            return SendResult(
                ok=False,
                reason="消息正文不能为空——把你要对方知道的事写进去再发。",
            )

        if not recipient:
            return SendResult(
                ok=False,
                reason=f"没有指定收件人。{self._roster_hint()}",
            )

        if recipient == sender:
            return SendResult(
                ok=False,
                reason=(
                    "不能给自己发消息。要记下待办请用共享任务清单；"
                    "要跟别人说话请填对方的名字。"
                ),
            )

        envelope = Envelope(
            sender=sender or MAIN_NAME,
            recipient=recipient,
            summary=self._normalize_summary(summary, body),
            body=body,
        )

        outcome = self._roster.deliver(recipient, envelope)

        if not outcome.ok:
            if outcome.kind == "terminal":
                return SendResult(
                    ok=False,
                    reason=(
                        f"{recipient!r} 已经不在场了（{outcome.state_label}），"
                        "叫不醒——它的上下文已经释放。\n"
                        "如果这件事还需要人做，委派一个新队员，"
                        "并把必要的背景写进任务描述里。"
                    ),
                )
            return SendResult(
                ok=False,
                reason=f"没有名为 {recipient!r} 的队员。{self._roster_hint()}",
            )

        # ── 出锁之后再唤醒（见模块 docstring 的三段式）──
        if outcome.wake_event is not None:
            outcome.wake_event.set()

        return SendResult(ok=True, envelope=envelope)

    def take_unread(self, name: str) -> tuple[Envelope, ...]:
        """
        取走某人的未读消息（取走即置位，幂等）。

        :param name: 收件人名字
        :returns: 未读消息元组

        副作用：把那些消息标记为已读。
        """
        return self._roster.take_unread(name)

    def has_unread(self, name: str) -> bool:
        """
        某人有没有未读。**只读**，供 TUI 每 0.5 秒轮询用。

        副作用：无。
        """
        return self._roster.has_unread(name)

    # ------------------------------------------------------------------ #
    # 内部
    # ------------------------------------------------------------------ #

    def _roster_hint(self) -> str:
        """
        「当前有哪些人」的提示。

        **必须列出全部名字**：模型据此能自我纠正（用对的名字重试），
        只说「查无此人」的话它只能猜。这与 C13 `_unknown_agent_text`、
        本章 `board._not_found_locked` 是同一条经验。
        """
        names = self._roster.names()
        if not names:
            return "当前花名册是空的。"
        return f"当前可以发给：{'、'.join(names)}。"

    @staticmethod
    def _normalize_summary(summary: str, body: str) -> str:
        """
        整理摘要：去空白、压成单行、截断。

        摘要为空时**从正文首行截取**而不是留空——它是用户在界面上唯一
        看得到的一行，留空会让通知变成「worker-a 发来一条消息」这种
        什么都没说的提示。

        压成单行是硬要求：多行摘要会把界面的单行布局撑破。
        """
        text = (summary or "").strip()
        if not text:
            text = (body or "").strip().splitlines()[0] if body.strip() else ""
        text = " ".join(text.split())
        return text[:80]


__all__ = ["Mailbox", "SendResult"]
