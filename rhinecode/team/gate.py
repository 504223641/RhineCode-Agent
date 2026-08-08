"""
队友消息的注入闸门（c15 T19，spec F11）。

## 它做什么

实现 `agent/gate.py` 的 `SubAgentGateProtocol`，让 Agent Loop 在**每轮
迭代开头**把收到的队友消息注入历史。这是 spec F11「推送投递」在正在运行
的收件人身上的落实——收件人不需要主动查收，消息自己出现在它眼前。

复用 C13 的闸门协议而不是新加一个循环调用点，是因为两者的处理逐字相同
（取出来 → 追加进历史 → 交给存档），且 `take_pending` 的位置已经论证过
协议合法性：它在两轮迭代**之间**，而协议只禁止在 `assistant(tool_calls)`
与它对应的 `tool` 结果中间插消息。

## ⚠ `has_awaited` / `wait_any` 恒为假

这是本模块唯一容易改错的地方，也是最要紧的一条。

C13 的闸门用这两个方法表达「模型准备收工了，但它委派出去的活还没回来，
循环停下来等一等」。**队友消息不适用这套语义**：

- 一个队员**永远可能**收到消息（只要它还在花名册上），
  返回 `True` 就等于说「你永远有东西要等」——它再也不会收工，
  会一直挂在 `wait_any` 里直到撞上兜底上限；
- 而「等消息」这件事本来就不该发生在 Agent Loop 里。队员跑完之后进入
  **待命**状态，那时它阻塞在自己的 `wake_event` 上，一条消息就能叫醒它
  （见 `subagents/runner.py` 的待命循环）。这条路径不占任何迭代预算，
  也不需要循环配合。

`tests/test_team_gate.py` 有一条单独的用例钉住这两个恒假，
并在注释里写明原因——防止后来的人看到「一个恒返回 False 的方法」
就顺手「修正」它。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from rhinecode.team.render import render_incoming

if TYPE_CHECKING:  # pragma: no cover —— 仅类型检查期
    from rhinecode.provider.base import Message
    from rhinecode.team.service import TeamService


class TeamGate:
    """
    某一个 Agent 的队友消息闸门。

    :param service: 协作服务门面
    :param name: **本闸门服务于谁**——主对话传 `main`，队员传它自己的名字。

        一个闸门只管一个人的信箱。共用一个闸门实例会让 A 取走 B 的消息，
        因此每次构造都要绑定名字。

    线程模型：本类无可变状态（信箱在花名册里，自己线程安全），
    方法只在它所属的那条 Agent Loop 线程里被调用。
    """

    def __init__(self, service: "TeamService", name: str) -> None:
        self._service = service
        self._name = name

    @property
    def name(self) -> str:
        """本闸门服务于谁（排错与测试用）。"""
        return self._name

    def take_pending(self) -> "list[Message]":
        """
        取走未读队友消息，渲染成要追加进历史的消息（spec F11/F12）。

        :returns: 至多一条消息（多条未读合并成一条）；没有未读时空列表

        **取走即置位**，因此重复调用幂等——同一条消息不会被注入两遍。

        由 Agent Loop 在每轮组装请求**之前**调用。

        副作用：把那些消息标记为已读。
        """
        unread = self._service.take_unread(self._name)
        if not unread:
            return []
        return [render_incoming(unread)]

    def has_awaited(self) -> bool:
        """
        **恒为 `False`**，见模块 docstring。

        返回 `True` 会让这个 Agent 为「可能有人给我发消息」赖着不收工。
        等消息发生在待命状态，不在 Agent Loop 里。
        """
        return False

    def wait_any(self, cancel_event) -> bool:  # noqa: ARG002 —— 协议要求这个参数
        """
        **恒为 `False`**（配套 `has_awaited`，实际不会被调到）。

        `CompositeGate.wait_any` 只对 `has_awaited()` 为真的闸门调用它，
        因此本方法在真实运行里走不到。保留实现是为了满足协议——
        一个 `runtime_checkable` 的 Protocol 会检查方法存在。
        """
        return False

    def describe_awaited(self) -> str:
        """恒为空串：没有在等的东西，就没什么可告诉用户的。"""
        return ""


__all__ = ["TeamGate"]
