"""
每次 `Agent.run` 一个的判定会话（c16 F9/F18）。

## 它把三样东西绑在一起

1. **转录的两个来源**——用户消息取自哪段历史、工具调用取自哪段历史。
   这两者对主对话是同一段，对子 Agent **不是**（见 `own_history` 的说明）。
2. **网络判定缓存**——它的有效期是「本次运行」，因此生命周期与本对象一致。
3. **共享的门面**——真正去问模型的那一个，跨线程共享。

## ⚠ 本类不加锁，这是有条件的

一个 `ReviewSession` 只被一个 `Agent.run` 使用，而 `run` 在单线程内跑完
（子 Agent 各跑各的线程，但各自有各自的 run 与 session）。
跨线程共享的只有 `ClassifierService`，它自己加锁。

**新增字段前先确认这个条件还成立**：哪天有人让两个线程共用一个 session，
缓存字典的并发读写不会报错，只会偶尔丢一条缓存或读到半截——那种问题查起来
极费劲，而现在这行注释是唯一的提醒。
"""

from __future__ import annotations

from typing import Optional

from rhinecode.classifier.cache import VerdictCache, cache_key
from rhinecode.classifier.models import (
    ClassifierProtocol,
    ReviewAction,
    Transcript,
    Verdict,
)
from rhinecode.provider.base import Message


class ReviewSession:
    """
    一次运行内的判定会话。

    :ivar _service: 共享的分类器门面
    :ivar _own: 本次运行自己的历史（取工具调用）
    :ivar _principal: 取用户消息的历史；None 表示与 `_own` 相同
    :ivar _cache: 网络判定缓存
    """

    def __init__(
        self,
        service: ClassifierProtocol,
        own_history: list[Message],
        principal_history: "Optional[list[Message]]" = None,
    ) -> None:
        """
        :param service: 共享的分类器门面
        :param own_history: **本次运行自己的历史**。工具调用从这里取——
            分类器要看的是「这个 Agent 此前做过什么」，那是它自己的行为记录
        :param principal_history: **取用户消息的历史**。缺省 None = 与
            `own_history` 相同（主对话的情形）。

            ⚠ **子 Agent 必须传主对话的历史**（spec F9）：它自己的历史里没有
            真人发言，它收到的「任务描述」是主模型写的。把那段文字当成用户的话，
            等于让模型给自己签授权书——用户说「别提交」，模型在委派时写一句
            「请提交代码」，边界就没了。

            这条也是「只喂用户消息」这个口径能在多 Agent 场景下继续成立的
            全部依据。
        """
        self._service = service
        self._own = own_history
        self._principal = principal_history
        self._cache = VerdictCache()

    def begin_iteration(self) -> None:
        """
        进入新一轮迭代。

        转发给缓存清掉放行结论——「有新内容进入对话就重新判定」（F18）。
        调用点在 `Agent.run` 的每轮循环开头。

        副作用：清空放行缓存。
        """
        self._cache.begin_iteration()

    def is_tripped(self) -> bool:
        """分类器当前是否处于熔断。调用方据此决定 FAILED 走哪条退路（F16a）。"""
        return self._service.is_tripped()

    def breaker_state(self):
        """
        取熔断状态快照，供界面渲染熔断提示（F17）。

        直接转发给门面：熔断状态是**会话共享**的（跨主对话与全部子 Agent），
        不属于本对象。转发而不是让调用方自己去拿门面，是为了让 `_execute`
        只认识 `ReviewSession` 一个类型。
        """
        return self._service.breaker_state()

    def review(self, action: ReviewAction) -> Verdict:
        """
        对一次待判动作给出结论。

        :param action: 待判动作
        :returns: 判定结果；**绝不抛异常**（门面已收敛，这里不再包一层）

        副作用：可能发起模型请求；可能写缓存。

        ## 顺序：先查缓存，再问模型

        缓存只对网络类非空（`cache_key` 对其余类别返回空串），
        因此命令类与消息类恒不命中、恒去问模型——「哪些类别参与缓存」
        这件事只在 `cache_key` 一处判断，见那里的说明。
        """
        key = cache_key(action)
        hit = self._cache.get(key)
        if hit is not None:
            return hit

        transcript = self._build_transcript()
        verdict = self._service.review(action, transcript)
        self._cache.put(key, verdict)
        return verdict

    def _build_transcript(self) -> Transcript:
        """
        取当前两段历史构造转录。

        :returns: 转录

        **每次判定都重新构造**，不缓存：历史随着每一轮迭代增长，缓存转录
        会让分类器看到过期的上下文——而「用户刚说了一句新的边界」恰恰是
        最需要它看见的那种变化。

        副作用：无（只读历史）。
        """
        # 延迟 import：`prompt` 模块本身零依赖，但放在这里能让
        # 「本文件只是接线」这件事在文件头的 import 列表上一眼可见。
        from rhinecode.classifier import prompt

        principal = self._principal if self._principal is not None else self._own
        return prompt.build_transcript(principal, self._own)
