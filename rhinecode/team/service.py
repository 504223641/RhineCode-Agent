"""
子 Agent 协作的服务门面（c15 T17）。

**职责**：把花名册、共享清单、信箱组合起来，对外只暴露一组方法。
协作工具、协调层、TUI、`/tasks` 与 `/agents` 命令**都只跟本类打交道**，
不直接碰底下三个组件。

这与 C13 `SubAgentService`、C11 `SkillManager` 是同一种形态：
包内多个组件各管一件事，包外只看见一个门面。

## 本类额外持有的两件全局状态

1. **自动唤起连锁计数**（spec F20）——「消息 → 自动唤起主对话 → 回消息 →
   对方又发 → 又被唤起」这条链的连续次数。没有上限的话，两个 Agent
   能互相唤醒到天亮、把用户的额度烧光，而界面上看起来只是「一直在动」。
   计数在**用户提交任何一条消息时清零**（用户回来了，链条重新开始算）。
2. **待命降级的待通知队列**——`Roster.mark_idle` 会返回被降级的名字，
   spec N3 要求这件事**看得见**。运行器在后台线程里拿到它，而通知要在
   界面上出现，因此先存这里，由 TUI 的既有轮询取走。

## 线程模型

底下三个组件各自加锁、自己线程安全。本类只额外保护那两件全局状态，
用一把独立的锁，**临界区只做纯内存读写**（加减一个整数、进出一个列表）。
"""

from __future__ import annotations

import threading
from typing import Optional

from rhinecode.team.board import TaskBoard
from rhinecode.team.mailbox import Mailbox, SendResult
from rhinecode.team.models import (
    MAIN_NAME,
    MAX_AUTO_WAKE_CHAIN,
    MAX_IDLE_MEMBERS,
    Envelope,
    MemberEntry,
    MemberState,
)
from rhinecode.team.render import render_board, render_roster
from rhinecode.team.roster import RegisterResult, Roster
from rhinecode.trace import TraceEventType, full_text


class TeamService:
    """
    协作能力的唯一入口。

    :param max_idle: 待命队员上限（spec N3），测试可覆盖
    :param max_auto_wake_chain: 自动唤起连锁上限（spec F20），测试可覆盖
    """

    def __init__(
        self,
        max_idle: int = MAX_IDLE_MEMBERS,
        max_auto_wake_chain: int = MAX_AUTO_WAKE_CHAIN,
        recorder=None,
    ) -> None:
        self.roster = Roster(max_idle=max_idle)
        self.board = TaskBoard()
        self.mailbox = Mailbox(self.roster)

        # 行为记录器（c15 T42/T43）。`None` 时全部埋点退化成零成本空调用。
        # ⚠ 埋点一律走 `_emit`，它吞掉一切异常——观测设施绝不能反过来
        # 阻断被观测的系统（spec N7）。
        self._recorder = recorder

        self._lock = threading.Lock()
        self._auto_wake_chain = 0
        self._max_auto_wake_chain = max(1, int(max_auto_wake_chain))
        # 待命降级的通知，等 TUI 轮询取走（spec N3 要求降级看得见）
        self._pending_notices: list[str] = []

    def _emit(self, event_type, **fields) -> None:
        """
        受保护的埋点漏斗（spec N7）。

        **吞掉一切异常**：记录失败最多是时间线上少一行，而异常逃逸会让一次
        正常的协作动作变成失败。与 `agent/loop.py` 的 `_safe_emit`、
        C14 各处埋点同一条口径。
        """
        if self._recorder is None:
            return
        try:
            self._recorder.emit(event_type, **fields)
        except Exception:  # noqa: BLE001 —— 观测设施绝不能阻断被观测的系统
            pass

    # ------------------------------------------------------------------ #
    # 花名册
    # ------------------------------------------------------------------ #

    def register_member(
        self,
        name: Optional[str],
        role: str,
        task_id: str = "",
        read_only: bool = False,
    ) -> RegisterResult:
        """
        占一个队员名字（spec F1/F2）。转发给花名册，失败原因原样带回。

        副作用：改花名册。
        """
        result = self.roster.register(name, role, task_id=task_id, read_only=read_only)
        self._emit(
            TraceEventType.TEAM_MEMBER,
            name=result.name or (name or ""),
            event="register" if result.ok else "register_failed",
            detail=role if result.ok else result.reason,
        )
        return result

    def mark_idle(self, name: str, history) -> tuple[str, ...]:
        """
        队员转待命并保管历史（spec F13）。

        :returns: 因此被降级的名字（spec N3）

        ⚠ **返回值同时被记进待通知队列**：调用方（运行器）在后台线程里，
        没法直接写界面。不记的话降级就成了静默降级，而 spec N3 明令禁止。

        副作用：改花名册；可能唤醒被降级者的线程（在花名册的锁外）；
        可能往待通知队列追加。
        """
        retired = self.roster.mark_idle(name, history)
        self._emit(TraceEventType.TEAM_MEMBER, name=name, event="idle")
        for victim in retired:
            self._emit(
                TraceEventType.TEAM_MEMBER,
                name=victim,
                event="retired",
                detail=f"待命超过上限 {self.roster.max_idle}",
            )
        if retired:
            with self._lock:
                for victim in retired:
                    self._pending_notices.append(
                        f"队员 {victim} 因待命人数超过上限（{self.roster.max_idle}）"
                        f"已被回收，它的上下文已释放、不能再唤醒了。"
                        f"这件事还需要人做的话，重新委派一个。"
                    )
        return retired

    def mark_terminal(self, name: str, state: MemberState) -> None:
        """队员进入终态并释放历史（spec F14）。副作用：改花名册。"""
        self.roster.mark_terminal(name, state)
        self._emit(TraceEventType.TEAM_MEMBER, name=name, event=state.value)

    def wake(self, name: str):
        """唤回一个待命队员并取出它的历史；不可唤醒时返回 `None`。"""
        history = self.roster.wake(name)
        self._emit(
            TraceEventType.TEAM_MEMBER,
            name=name,
            event="woken" if history is not None else "wake_refused",
        )
        return history

    def members(self) -> tuple[MemberEntry, ...]:
        """全部队员的只读快照（含 `main`，它排在最前）。"""
        return self.roster.snapshot()

    def member(self, name: str) -> Optional[MemberEntry]:
        """取一位队员的副本。"""
        return self.roster.get(name)

    def is_read_only(self, name: str) -> bool:
        """
        某个名字是不是「只读」——`main` 之外的判据是委派时算好的工具集。

        `main` 返回 `False`：它当然能写。这个方法只服务于 F24 的判定，
        而那里 `main` 走单独的放行分支。
        """
        entry = self.roster.get(name)
        return bool(entry is not None and entry.read_only)

    # ------------------------------------------------------------------ #
    # 消息
    # ------------------------------------------------------------------ #

    def send(
        self, sender: str, recipient: str, body: str, summary: str = ""
    ) -> SendResult:
        """
        发一条消息（spec F9/F10）。

        副作用：往收件人信箱追加；可能唤醒待命中的收件人（在锁外）。
        """
        result = self.mailbox.send(sender, recipient, body, summary)
        self._emit(
            TraceEventType.TEAM_MESSAGE,
            sender=sender,
            recipient=recipient,
            ok=result.ok,
            summary=(result.envelope.summary if result.envelope else result.reason),
            body=full_text(body),
        )
        return result

    def take_unread(self, name: str) -> tuple[Envelope, ...]:
        """取走某人的未读（取走即置位，幂等）。"""
        return self.mailbox.take_unread(name)

    def has_unread(self, name: str) -> bool:
        """某人有没有未读。**只读**，供 TUI 每 0.5 秒轮询用。"""
        return self.mailbox.has_unread(name)

    def has_unread_for_main(self) -> bool:
        """主对话有没有未读——自动唤起的触发条件之一（spec F17）。"""
        return self.mailbox.has_unread(MAIN_NAME)

    # ------------------------------------------------------------------ #
    # Plan Mode（spec F24）
    # ------------------------------------------------------------------ #

    def can_send_in_plan_stage(self, recipient: str) -> tuple[bool, str]:
        """
        规划阶段能不能给这个人发消息（spec F24）。

        :param recipient: 收件人名字
        :returns: `(能不能发, 不能发时的可读原因)`

        判据：`main`，或**最终工具集全只读**的队员，才能发。

        理由：给一个能写文件的待命队员发消息会把它**唤醒去动手**，
        而 Plan Mode 的承诺是「批准前不动手」。这条旁路不封的话，
        规划阶段就能通过「让别人去做」绕过整个 Plan Mode。

        判据与 C13 F19a（规划阶段只能委派全只读角色）**刻意一致**——
        两处封的是同一条旁路的两个入口。

        副作用：无（只读花名册）。
        """
        recipient = (recipient or "").strip()
        if recipient == MAIN_NAME:
            return True, ""

        entry = self.roster.get(recipient)
        if entry is None:
            # 收件人不存在的失败留给投递层去说（那里有完整的花名册提示），
            # 这里放行，避免同一个错误被两处各说一遍、措辞还不一样。
            return True, ""

        if entry.read_only:
            return True, ""

        allowed = [MAIN_NAME] + [
            m.name for m in self.roster.snapshot() if m.read_only and not m.is_main
        ]
        return False, (
            "当前处于 Plan Mode 的**规划阶段**——批准计划之前不动手，"
            f"因此不能给 {recipient!r} 发消息：它的工具集里有能改动东西的工具，"
            "而一条消息会把它唤醒去干活，那就绕过了「批准前不动手」的承诺。\n"
            f"现在可以发给：{'、'.join(allowed)}。\n"
            "如果这件事确实需要动手，请先用 present_plan 把计划提交给用户，"
            "获批之后再发。"
        )

    # ------------------------------------------------------------------ #
    # 自动唤起（spec F17/F20）
    # ------------------------------------------------------------------ #

    def can_auto_wake(self) -> bool:
        """
        连锁计数还没到上限吗（spec F20）。**只读**，供 TUI 轮询判断。
        """
        with self._lock:
            return self._auto_wake_chain < self._max_auto_wake_chain

    def bump_auto_wake(self) -> int:
        """
        记一次自动唤起。

        :returns: 记完之后的次数（界面显示「第 N/M 次」用）

        副作用：改内部计数。
        """
        with self._lock:
            self._auto_wake_chain += 1
            count = self._auto_wake_chain
        # ⚠ 埋点在**锁外**：它会写盘，而临界区只做纯内存读写（spec N2）。
        self._emit(
            TraceEventType.AUTO_WAKE,
            count=count,
            limit=self._max_auto_wake_chain,
            trigger=MAIN_NAME,
        )
        return count

    def reset_auto_wake(self) -> None:
        """
        连锁计数清零——**用户提交任何一条消息时调**（spec F20）。

        用户回来了，「两个 Agent 互相唤醒」这条链就该重新开始算。
        漏调的后果是自动唤起在用户回来后仍处于停用状态，
        而界面上看不出原因。

        副作用：改内部计数。
        """
        with self._lock:
            self._auto_wake_chain = 0

    @property
    def auto_wake_chain(self) -> int:
        """当前连锁次数（展示与测试用）。"""
        with self._lock:
            return self._auto_wake_chain

    @property
    def max_auto_wake_chain(self) -> int:
        """连锁上限（展示用）。"""
        return self._max_auto_wake_chain

    # ------------------------------------------------------------------ #
    # 通知
    # ------------------------------------------------------------------ #

    def drain_notices(self) -> tuple[str, ...]:
        """
        取走待展示的通知（目前只有 N3 降级）。

        :returns: 通知文本；没有时空元组

        **取走即清空**（幂等）：与 C13 `drain_notifications` 同一条契约，
        由 TUI 的既有轮询消费。

        副作用：清空内部队列。
        """
        with self._lock:
            if not self._pending_notices:
                return ()
            out = tuple(self._pending_notices)
            self._pending_notices.clear()
            return out

    # ------------------------------------------------------------------ #
    # 展示
    # ------------------------------------------------------------------ #

    def board_text(self) -> str:
        """共享清单的展示文本（`/tasks` 与列清单工具共用）。"""
        return render_board(self.board.snapshot(), self.board.is_blocked)

    def roster_text(self) -> str:
        """
        花名册的展示文本（`/agents` 用）。

        主对话不作为「队员」列出（列出来只会让用户困惑「我什么时候招了个叫
        main 的人」），**但它的未读要说一句**：自动唤起被连锁上限挡住时，
        消息会一直堆在那儿，而用户在界面上没有任何别的地方看得到这件事。
        """
        text = render_roster(self.roster.snapshot())
        main = self.roster.get(MAIN_NAME)
        if main is not None and main.unread_count:
            note = f"主对话有 {main.unread_count} 条未读队友消息"
            if not self.can_auto_wake():
                note += "（自动唤起已达上限，说句话即可继续处理）"
            text = (text + "\n" if text and "没有队员" not in text else "") + note
        return text

    # ------------------------------------------------------------------ #
    # 会话边界
    # ------------------------------------------------------------------ #

    def clear(self) -> None:
        """
        清空全部协作状态（spec F25：`/clear` / `/resume` / 退出）。

        花名册、清单、未读消息、待通知、连锁计数**一并复位**。

        ⚠ 漏掉任何一项的后果都不报错：漏花名册会让上一轮的队员消息
        出现在新对话里；漏连锁计数会让自动唤起在新会话里仍处于停用状态。

        副作用：改全部内部状态；唤醒全部待命队员（让它们的线程退出）。
        """
        self.roster.clear()
        self.board.clear()
        with self._lock:
            self._auto_wake_chain = 0
            self._pending_notices.clear()


__all__ = ["TeamService"]
