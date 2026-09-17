"""
原地打转检测：**纯逻辑、零成本、确定性**，只依赖标准库。

## 这个模块在回答什么问题

Agent Loop 原先靠一个写死的 25 轮上限兜底。那个数字同时扮演两个角色，
而它们要的方向正好相反：

- 「模型卡住了、在重复同一件事」—— 这时 25 轮**太多**，用户白等白花钱；
- 「任务本身就复杂，要读要写要调」—— 这时 25 轮**太少**，活干到一半被腰斩
  （真实 trace 实录：第 25 轮它刚定位到性能瓶颈，正要动手就被掐了）。

一个数字答不了两个问题，所以把它们拆开：**「卡住了」由本模块判**（便宜、
确定、随时都在看），**「要不要接着跑」另有一条路**（见 `agent/loop.py` 里
续跑判定那一段）。

## 判据：同一个「动作指纹」重复出现，且结果一模一样

指纹 = `(工具名, 规范化后的参数)`。加上**那次调用的结果**之后构成一次
「动作快照」；同一个快照在一次运行里出现 `REPEAT_LIMIT` 次即判定打转。

三个设计取舍，每一条都有真实样本支撑：

① **不要求「连续」。** 真实的死循环常常是 A→B→A→B 的摆动（改一处、跑一次、
   发现不对、改回去、再跑），连续口径一次都抓不到。

② **必须连结果一起比**，只比参数会误伤。真实 trace 里模型连着跑了三次性能
   基准（量 → 改代码 → 复测 → 换个角度再量），那是**正常的排查节奏**：
   三次的参数各不相同，结果也从 0.91 变成 0.89 又变成 0.040。
   反过来，真正的打转形态是「同样的命令、同样的报错，再来一遍」。

③ **参数要规范化再比。** 模型两次生成的 JSON 键序可能不同（`{"a":1,"b":2}`
   与 `{"b":2,"a":1}`），那是同一次调用；按原始字符串比会漏掉一半。

## ⚠ 它判的是「重复」，不是「无进展」

一个每轮都在做新鲜事、但整体毫无意义的模型，本模块**抓不到**——那需要语义
判断，不是代码干得了的活。那一半交给续跑判定（要花一次模型调用），本模块
只负责**免费就能抓到的那部分**。两者是叠加关系，不是替代。

## ⚠ 结果参与指纹，所以不能拿它当安全机制

模型可以（无意地）让每次调用略有不同从而绕开本检测。这不是漏洞：本模块的
职责是止损，不是防御。真正的兜底是续跑判定与它上面那道「问用户」。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Optional

# 同一个动作快照出现这么多次即判定打转。
#
# ⚠ 取 3 不是随手定的，对齐 Codex 的 blocked audit（原文：只有当**同一个
# 阻塞条件连续三轮重复**时才允许报 blocked）。2 太急——「做了、失败了、
# 原样再试一次」是人也会做的事，第二次往往就成了；4 以上则要多烧一整轮。
REPEAT_LIMIT = 3


def fingerprint(tool_name: str, arguments: Any, output: str) -> str:
    """
    把一次工具调用压成一个可比较的字符串。

    :param tool_name: 工具名
    :param arguments: 模型给的参数（通常是 dict；解析失败时是 None）
    :param output: 那次调用回灌给模型的结果全文
    :returns: 指纹字符串

    ## 为什么参数要 `sort_keys` 序列化

    模型两次生成同一个调用时键序可能不同，那仍然是同一次调用。按原始文本比
    会让一半的重复溜过去。序列化失败（参数里有不可序列化的东西）时退回
    `repr`——**宁可偶尔漏判，也不能在这里抛异常**：本函数跑在循环的收尾处，
    抛出去会让一次本来正常的运行整个炸掉。

    副作用：无（纯函数）。
    """
    try:
        args = json.dumps(arguments, sort_keys=True, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        args = repr(arguments)
    return f"{tool_name}\x00{args}\x00{output}"


@dataclass
class SpinDetector:
    """
    一次运行里的动作计数。**不是线程安全的**——每次 `run` 各持一个。

    :ivar counts: 指纹 → 出现次数
    :ivar limit: 判定阈值，缺省 `REPEAT_LIMIT`

    ⚠ **作用域必须是「一次运行」**，不是整个会话。跨会话累计会让用户第二次
    问同一件事时凭空少了几次额度，而那两次之间他可能已经改了代码——
    「同样的命令给出同样的结果」在那时是**新信息**，不是打转。
    """

    counts: dict[str, int] = field(default_factory=dict)
    limit: int = REPEAT_LIMIT

    def record(self, tool_name: str, arguments: Any, output: str) -> int:
        """
        记一次调用，返回这个动作累计出现了几次。

        :returns: 含本次在内的出现次数

        副作用：更新 `counts`。
        """
        key = fingerprint(tool_name, arguments, output)
        count = self.counts.get(key, 0) + 1
        self.counts[key] = count
        return count

    def record_round(self, calls: list[tuple[str, Any, str]]) -> Optional[tuple[str, int]]:
        """
        记下一整轮的全部调用，返回第一个达到阈值的动作。

        :param calls: `[(工具名, 参数, 结果全文), ...]`，本轮实际执行过的调用
        :returns: 达到阈值时返回 `(工具名, 次数)`；否则 `None`

        ⚠ **一轮里的多个调用要逐个记**，不能把整轮压成一个指纹：模型完全可能
        每轮都把那个卡住的调用和一个新调用放在一起发，整轮指纹于是次次不同，
        而那个卡住的调用照样在原地。

        副作用：更新 `counts`。
        """
        hit: Optional[tuple[str, int]] = None
        for tool_name, arguments, output in calls:
            count = self.record(tool_name, arguments, output)
            if count >= self.limit and hit is None:
                hit = (tool_name, count)
        return hit


def render_spin_message(tool_name: str, count: int) -> str:
    """
    判定打转时给用户看的那句话。

    :param tool_name: 卡住的那个工具
    :param count: 它重复了几次
    :returns: 一句中文说明

    ⚠ **要说清「重复的是什么」与「我为什么停」**，别只说「已停止」。
    用户接下来要做的判断是「它是真卡了，还是我该换个说法再来一次」，
    而那只有知道是哪个调用在重复才判断得了（同已知项 #21 那条
    「拒绝的文案要说清为什么」）。

    副作用：无（纯函数）。
    """
    return (
        f"检测到原地打转：`{tool_name}` 带着同样的参数、拿到同样的结果，"
        f"已经重复了 {count} 次，循环自动停止。\n"
        f"它多半是卡在某个判断上了——换个说法、或者直接告诉它下一步该怎么做，"
        f"通常就能继续。"
    )


__all__ = [
    "REPEAT_LIMIT",
    "SpinDetector",
    "fingerprint",
    "render_spin_message",
]
