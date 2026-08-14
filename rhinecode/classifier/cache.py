"""
网络判定的会话内缓存（c16 F18/F19）。

## 只有网络类做缓存

对齐 Claude Code 官方口径——他们只对网络主机复用判定：
*the classifier reuses its verdict for a network host and port instead of
re-running on every connection.*

命令类与消息类**每次都重新判定**：

- **命令**：同一条命令在不同上下文下的安全性不同。用户中途说了「先别提交」，
  而 `git commit` 那条已被缓存为放行，边界就失效了——**缓存会让 F8
  （用户声明的边界具约束力）在最需要它的场景下失效**。
- **消息**：判定对象是正文，两条正文不同的消息之间没有任何可复用的东西。

## 两种结论的有效期不同

| 结论 | 有效期 | 为什么 |
| --- | --- | --- |
| 放行 | 到**有新内容进入对话**为止（即下一轮迭代） | 新内容可能改变判断依据（用户新说了一句话、模型刚读了一个可疑的网页） |
| 拒绝 | 到**本回合结束**为止（即本次运行结束） | 让模型在同一回合里换个写法反复试同一个主机这件事不划算 |

对齐官方：*An allow is reused until new content enters the conversation, at which
point that host is checked again. In the interactive CLI, a deny is dropped when
the turn ends.*

本项目里两个概念的对应关系：**一轮迭代** = Agent Loop 的一次循环（每轮都会往
历史里追加 assistant 与 tool 结果，即「有新内容进入对话」）；
**一个回合** = 一条用户消息触发的完整运行。于是「拒绝到回合结束」直接由
本对象的生命周期表达——`VerdictCache` 每次运行建一个，运行结束即销毁。

## ⚠ 缓存键绝不含路径与查询参数

只有主机名 + 端口。含了等于不缓存（每个地址都不同），而且会把地址里可能夹带的
令牌存进内存里的键上——`permission/adapter.py` 的 `to_allow_rule` 踩过同一个坑
（原写法会把 `?token=abc` 写进配置文件），那里的 docstring 记着这件事。
"""

from __future__ import annotations

from rhinecode.classifier.models import SCOPE_URL, ReviewAction, Verdict, VerdictKind


def cache_key(action: ReviewAction) -> str:
    """
    算一次待判动作的缓存键。

    :param action: 待判动作
    :returns: 网络类返回 `"<主机>:<端口>"`；**其余类别一律返回空串**

    调用方约定：**空串表示不缓存**。把「哪些类别参与缓存」收在这一个函数里，
    而不是让调用方各写一次 `if scope == SCOPE_URL`——后者会在将来新增类别时
    产生两处判断，而漏改其中一处不报错、只表现为「某个类别悄悄开始复用判定」。

    副作用：无（纯函数）。
    """
    if action.scope != SCOPE_URL:
        return ""
    host = (action.host or "").strip().lower()
    if not host:
        # 主机名解析不出来时不缓存。退回「每次都判」是偏严方向：
        # 用一个空键去缓存会让所有解析失败的地址共享同一条结论。
        return ""
    return f"{host}:{int(action.port or 0)}"


class VerdictCache:
    """
    一次运行内的网络判定缓存。

    :ivar _allow: 放行结论。`begin_iteration()` 清空
    :ivar _deny: 拒绝结论。**不主动清空**——随本对象生命周期结束而消失，
                 这正好等于「本回合结束即丢弃」

    ## 为什么作用域是「每次运行一个」而不是全局共享

    两条：① 「有新内容进入对话」是**本次运行**的概念，全局共享的话无法定义
    什么时候该失效；② 主对话与子 Agent 并发跑，共享等于让 A 批准过的主机
    对 B 直接生效——那是一次悄悄的能力扩大，而 C13 的承诺是子 Agent 的能力
    只会比主对话小。

    **因此本类不加锁**：一个实例只被一个 `Agent.run` 使用，而 `run` 在单线程内
    跑完。跨线程共享的只有 `ClassifierService`（它自己加锁）。
    """

    def __init__(self) -> None:
        self._allow: dict[str, Verdict] = {}
        self._deny: dict[str, Verdict] = {}

    def get(self, key: str) -> "Verdict | None":
        """
        查缓存。

        :param key: `cache_key` 的返回值；空串一律未命中
        :returns: 命中的结论（`cached=True`），未命中返回 None

        命中时返回的副本把 `cached` 置真、`elapsed_ms` 归零——
        不改的话行为记录里会把一次零成本的命中记成「又花了 800 毫秒」，
        观测设施撒谎且不报错。

        副作用：无。
        """
        if not key:
            return None
        hit = self._allow.get(key) or self._deny.get(key)
        if hit is None:
            return None
        return Verdict(
            kind=hit.kind,
            reason=hit.reason,
            staged=hit.staged,
            cached=True,
            elapsed_ms=0,
        )

    def put(self, key: str, verdict: Verdict) -> None:
        """
        写缓存。

        :param key: `cache_key` 的返回值；空串直接忽略
        :param verdict: 本次判定结果

        ⚠ **只缓存 ALLOW 与 BLOCK，不缓存 FAILED。** 失败是环境问题不是判定
        结论，缓存它会让一次网络抖动在整个回合里持续生效——而那时接口可能
        早就恢复了。

        副作用：修改内部字典。
        """
        if not key:
            return
        if verdict.kind is VerdictKind.ALLOW:
            self._allow[key] = verdict
        elif verdict.kind is VerdictKind.BLOCK:
            self._deny[key] = verdict

    def begin_iteration(self) -> None:
        """
        进入新一轮迭代：清空**放行**缓存。

        这就是「有新内容进入对话就重新判定」的实现——每一轮迭代开始时，
        上一轮的 assistant 正文与工具结果都已经进了历史。

        ⚠ **不清拒绝缓存**：它的有效期是整个回合，见类 docstring。

        副作用：清空 `_allow`。
        """
        self._allow.clear()
