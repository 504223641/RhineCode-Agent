"""
花名册（c15 T9–T11，spec F1/F2/F3/F13/F14/F15，N3）。

## 它是什么

「名字 → 队员」的电话簿，同时是每位队员的**信箱持有者**与
**待命历史的保管处**。本章的中心数据结构，一个会话一份。

名字是本章一切协作的地基：消息按名字投递、任务按名字认领、
`/agents` 按名字展示。而**名字在队员干完之后依然有效**——
再发一条消息就能把它从原来的上下文唤醒继续干（spec F13），
这是「待命历史保管」存在的全部理由。

## ⚠ 重名一律失败，不采用「后来者接管」

Claude Code 的规则是 latest wins（新 agent 占用同名，旧的只能用原始 id 找）。
**本项目刻意不同**（spec F2）：那会让一条发给 `worker-a` 的消息
**静默送到另一个 Agent 手里**，而两边都不报错——与本项目通篇
「不静默降级、失败要看得见」的口径直接冲突，且本项目没有 Claude Code
那套能让用户当场看清「谁是谁」的界面。

## ⚠ 加锁不变量（与 C11/C12/C13 同一条）

**临界区只做纯内存读写**，一切跨线程调度在锁外。本模块里需要小心的是
`threading.Event.set()`——它唤醒等待线程，必须在锁外调用。

本类的三个方法会 `set` 事件（`mark_idle` 的降级、`mark_terminal`、`clear`），
一律写成「**锁内算出要唤醒谁 → 出锁 → 逐个 set**」的三段式。

## ⚠ 被降级 / 被清空的队员必须被唤醒

待命中的队员线程阻塞在自己的 `wake_event` 上。把它降级为 `RETIRED`
或在 `clear()` 里抹掉之后，若不 `set` 那个事件，**那个线程会一直等下去**
（它是 daemon 线程，要到进程退出才结束）。醒来后它调 `wake()` 拿到 `None`，
据此知道自己已经出局、干净地退出。
"""

from __future__ import annotations

import re
import threading
import time
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Optional

from rhinecode.team.models import (
    MAIN_NAME,
    MAX_IDLE_MEMBERS,
    MEMBER_STATE_LABELS,
    MemberEntry,
    MemberState,
)

if TYPE_CHECKING:  # pragma: no cover —— 仅类型检查期
    from rhinecode.provider.base import Message


# 名字里不允许出现的字符：控制字符、空白、引号与尖括号。
#
# 尖括号与引号是**硬要求**而不是洁癖：名字会被嵌进注入消息的标记块属性里
# （`<teammate-message from="...">`），一个引号就能把那个标记块拆坏，
# 让模型读到一段结构错乱的文本。空白折叠是为了让 `worker a` 与 `worker  a`
# 不会变成两个看起来一样的名字。
_NAME_FORBIDDEN = re.compile(r"[\x00-\x1f\x7f<>\"'`\s]+")

# 名字长度上限。够长以容纳有意义的中文名，又不至于把 `/agents` 的表格撑破。
_NAME_MAX_LEN = 40


@dataclass(frozen=True)
class RegisterResult:
    """
    一次注册的结果。

    :param ok: 是否成功
    :param name: 最终生效的名字（自动生成时由本层给出）
    :param reason: 失败原因（可读中文，直接回灌给模型）
    """

    ok: bool
    name: str = ""
    reason: str = ""


class Roster:
    """
    花名册。一个会话一份，由 `TeamService` 持有。

    构造时自动放入 `main` 条目——**把主对话也当成一名队员**，
    而不是在投递路径上特判它。理由：`send_message` 的路由因此只有
    一条代码路径，`to: "main"` 与 `to: "worker-a"` 走同样的校验与投递，
    少一处「特例分支忘了同步」的机会。
    """

    def __init__(self, max_idle: int = MAX_IDLE_MEMBERS) -> None:
        """
        :param max_idle: 同时保留完整历史的待命队员上限，测试可覆盖
        """
        self._lock = threading.Lock()
        self._members: dict[str, MemberEntry] = {}
        self._max_idle = max(1, int(max_idle))
        self._install_main_locked()

    # ------------------------------------------------------------------ #
    # 注册与命名
    # ------------------------------------------------------------------ #

    def register(
        self,
        name: Optional[str],
        role: str,
        task_id: str = "",
        read_only: bool = False,
    ) -> RegisterResult:
        """
        占一个名字（spec F1/F2）。

        :param name: 想要的名字；`None` 或空串表示**由系统按 `role` 自动生成**
        :param role: 角色名，自动生成时作为前缀
        :param task_id: 关联的 C13 任务标识，`/agents` 对齐展示用
        :param read_only: 该队员的最终工具集是否全只读（spec F24 判定用）
        :returns: `RegisterResult`

        ## 为什么自动命名也走这一个方法

        生成候选名与占用它**必须在同一个临界区内**完成。分两步做
        （先 `suggest_name` 再 `register`）会留下竞态：两个并发的委派
        可能拿到同一个候选名，然后一个成功一个失败——而失败的那个
        本来只是想要「一个不冲突的名字」，它并不在乎叫什么。

        副作用：改内部状态。
        """
        role = (role or "member").strip() or "member"

        with self._lock:
            if name is None or not str(name).strip():
                final = self._suggest_locked(role)
            else:
                final = self._sanitize(name)
                if not final:
                    return RegisterResult(
                        ok=False,
                        reason=(
                            f"名字 {name!r} 不合法（去掉空白、引号与尖括号之后什么都不剩）。"
                            "请换一个由字母、数字、汉字、连字符组成的名字。"
                        ),
                    )
                if final == MAIN_NAME:
                    return RegisterResult(
                        ok=False,
                        reason=(
                            f"{MAIN_NAME!r} 是主对话的保留名字，队员不能叫它。"
                            "换一个名字即可。"
                        ),
                    )
                existing = self._members.get(final)
                if existing is not None:
                    label = MEMBER_STATE_LABELS.get(
                        existing.state, existing.state.value
                    )
                    return RegisterResult(
                        ok=False,
                        reason=(
                            f"名字 {final!r} 已经被占用了（当前状态：{label}）。"
                            "换一个名字重新委派；"
                            "如果你本来就想让**那一位**接着干，"
                            "别再委派一个新的——直接给它发消息即可，"
                            "它会带着原来的上下文继续。"
                        ),
                    )

            self._members[final] = MemberEntry(
                name=final,
                task_id=task_id,
                state=MemberState.RUNNING,
                read_only=read_only,
            )
            return RegisterResult(ok=True, name=final)

    def suggest_name(self, role: str) -> str:
        """
        看一眼「如果现在按这个角色自动命名会叫什么」（只读，不占用）。

        :param role: 角色名
        :returns: 当前不冲突的候选名

        ⚠ **它不保证之后 `register` 一定能拿到这个名字**——两次调用之间
        可能有别的线程占走。要真正拿到名字请直接调 `register(None, role)`，
        那条路径在同一个临界区内生成并占用。本方法只用于展示与测试。
        """
        with self._lock:
            return self._suggest_locked((role or "member").strip() or "member")

    # ------------------------------------------------------------------ #
    # 状态流转
    # ------------------------------------------------------------------ #

    def mark_idle(self, name: str, history: "list[Message]") -> tuple[str, ...]:
        """
        队员自然停止 → 转**待命**，保管它的完整历史（spec F13）。

        :param name: 队员名字
        :param history: 它这次跑完之后的完整对话历史
        :returns: **因本次转待命而被降级的队员名字**（spec N3）

        ## ⚠ 返回值必须被消费

        待命队员的历史不会自动释放——那正是「叫醒就能接着干」的物理前提。
        因此有上限，超限时最久未活动的那个被降级为 `RETIRED`、历史释放。

        spec N3 明令**降级必须看得见**：调用方要把这些名字告诉用户，
        否则就成了它禁止的静默降级——用户会遇到「同一个名字昨天叫得醒、
        今天叫不醒」而毫无线索。

        副作用：改内部状态；**在锁外**唤醒被降级者的线程（让它们退出）。
        """
        retired: list[str] = []
        wake_events: list[threading.Event] = []

        with self._lock:
            entry = self._members.get(name)
            if entry is None or entry.is_main:
                return ()
            entry.state = MemberState.IDLE
            entry.history = list(history)
            entry.last_active = time.time()

            # 超限降级：挑 last_active 最小的（最久没干活的）。
            # 刻意不挑「历史最长的」——那会惩罚干得最多的队员。
            idles = [
                e
                for e in self._members.values()
                if e.state is MemberState.IDLE and not e.is_main
            ]
            if len(idles) > self._max_idle:
                idles.sort(key=lambda e: e.last_active)
                for victim in idles[: len(idles) - self._max_idle]:
                    victim.state = MemberState.RETIRED
                    victim.history = []
                    retired.append(victim.name)
                    wake_events.append(victim.wake_event)

        # ── 锁外 ──
        # 被降级的队员正阻塞在自己的 wake_event 上。不唤醒它，那个线程
        # 会一直等下去（daemon，要到进程退出才结束）。醒来后它调 wake()
        # 拿到 None，据此知道自己出局、干净退出。
        for event in wake_events:
            event.set()
        return tuple(retired)

    def mark_terminal(self, name: str, state: MemberState) -> None:
        """
        队员进入终态（失败 / 被取消 / 被降级），**释放历史**（spec F14）。

        :param name: 队员名字
        :param state: 终态之一；传非终态会被当成编程错误而断言失败

        副作用：改内部状态；**在锁外**唤醒它的线程（若它正待命）。
        """
        assert state.is_terminal, f"mark_terminal 只接受终态，收到 {state}"
        event: Optional[threading.Event] = None

        with self._lock:
            entry = self._members.get(name)
            if entry is None or entry.is_main:
                return
            was_idle = entry.state is MemberState.IDLE
            entry.state = state
            entry.history = []
            entry.last_active = time.time()
            if was_idle:
                event = entry.wake_event

        if event is not None:
            event.set()

    def wake(self, name: str) -> "Optional[list[Message]]":
        """
        把一个待命队员唤回运行中，取回它保管的历史（spec F13）。

        :param name: 队员名字
        :returns: 保管的历史；**不可唤醒时 `None`**

        `None` 的两种成因：队员不存在，或它已进入终态
        （含被 N3 降级为 `RETIRED`）。运行器据此退出等待循环。

        ⚠ 醒来之后**清掉 `wake_event`**：不清的话下一次待命会立刻返回，
        队员进入空转。

        副作用：改内部状态。
        """
        with self._lock:
            entry = self._members.get(name)
            if entry is None or not entry.state.is_wakeable:
                return None
            entry.state = MemberState.RUNNING
            entry.wake_event.clear()
            history = entry.history
            entry.history = []
            return history

    # ------------------------------------------------------------------ #
    # 读
    # ------------------------------------------------------------------ #

    def get(self, name: str) -> Optional[MemberEntry]:
        """
        取一位队员的**副本**。

        ⚠ 副本里的 `inbox` / `history` 是**浅拷贝的新列表**：调用方增删
        它不会影响册内，但列表里的 `Envelope` 本身是 frozen 的，共享无害。

        副作用：无。
        """
        with self._lock:
            entry = self._members.get(name)
            return self._copy(entry) if entry is not None else None

    def snapshot(self) -> tuple[MemberEntry, ...]:
        """
        全部队员的只读快照。`main` **排在最前**，其余按注册顺序。

        副作用：无。
        """
        with self._lock:
            main = self._members.get(MAIN_NAME)
            others = [e for e in self._members.values() if not e.is_main]
            entries = ([main] if main is not None else []) + others
            return tuple(self._copy(e) for e in entries)

    def names(self) -> tuple[str, ...]:
        """当前全部名字（失败文案里「列出花名册」用）。"""
        with self._lock:
            return tuple(self._members.keys())

    def idle_count(self) -> int:
        """当前待命人数（不含 `main`）。"""
        with self._lock:
            return sum(
                1
                for e in self._members.values()
                if e.state is MemberState.IDLE and not e.is_main
            )

    # ------------------------------------------------------------------ #
    # 清空
    # ------------------------------------------------------------------ #

    def clear(self) -> None:
        """
        清空花名册并重建 `main`（spec F25：`/clear` / `/resume` / 退出）。

        副作用：改内部状态；**在锁外**唤醒全部待命队员，让它们的线程退出。

        ⚠ 唤醒这一步不可省：待命线程阻塞在自己的事件上，抹掉册子并不会
        让它们醒来。醒来后它们调 `wake()` 拿到 `None`（名字已不存在），
        据此干净退出。
        """
        with self._lock:
            events = [
                e.wake_event
                for e in self._members.values()
                if e.state is MemberState.IDLE and not e.is_main
            ]
            self._members.clear()
            self._install_main_locked()

        for event in events:
            event.set()

    # ------------------------------------------------------------------ #
    # 内部
    # ------------------------------------------------------------------ #

    def _install_main_locked(self) -> None:
        """放入 `main` 条目。**调用方必须已持锁**（构造期除外）。"""
        self._members[MAIN_NAME] = MemberEntry(
            name=MAIN_NAME, state=MemberState.RUNNING, read_only=False
        )

    def _suggest_locked(self, role: str) -> str:
        """
        生成一个当前不冲突的名字。**调用方必须已持锁。**

        形态是 `<角色>-1`、`<角色>-2`…… 从 1 开始找第一个空位
        （而不是「已有数量 + 1」）：中间那个被回收之后，
        新队员应该补上那个空位，而不是让编号无限增长。
        """
        base = self._sanitize(role) or "member"
        index = 1
        while f"{base}-{index}" in self._members:
            index += 1
        return f"{base}-{index}"

    @staticmethod
    def _sanitize(name: str) -> str:
        """
        清洗名字：去掉控制字符、空白、引号与尖括号，截断到上限。

        :returns: 清洗后的名字；全部被清掉时返回空串（调用方据此判失败）

        尖括号与引号必须去掉：名字会被嵌进注入消息的标记块属性里，
        一个引号就能把标记块拆坏，让模型读到结构错乱的文本。
        """
        cleaned = _NAME_FORBIDDEN.sub("-", str(name)).strip("-")
        return cleaned[:_NAME_MAX_LEN]

    @staticmethod
    def _copy(entry: MemberEntry) -> MemberEntry:
        """
        产出一个对外安全的副本。

        `inbox` / `history` 换成新列表，使调用方的增删不影响册内；
        `wake_event` **原样共享**（它是同步原语，复制它毫无意义
        且会让「唤醒副本」这种错误变得可能）。
        """
        return replace(entry, inbox=list(entry.inbox), history=list(entry.history))


__all__ = ["RegisterResult", "Roster"]
