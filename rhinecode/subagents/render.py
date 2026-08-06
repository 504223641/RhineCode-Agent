"""
角色清单的注入文本（c13 T19，spec F9）。

**职责**：把已加载的角色渲染成一段进主对话系统提示的文本，让主 Agent
知道「有哪些角色可用、什么时候该委派」。

## ⚠ 成对维护点：表头与委派工具的描述必须同口径

`_INDEX_HEADER` 与 `tools/run_agent.py` 的 `description` 是模型决定
「要不要委派」时读到的**唯一两处文本**。一处写得强、另一处写得弱，
等于白改——模型会按弱的那份行事。
护栏见 `tests/test_subagent_tool.py::SameVoiceTest`。

## 为什么措辞要「有点 pushy」

C11 已经踩过一次：Skill 清单一开始写成公告式（「以下是可用的 Skill」），
实测模型**系统性欠触发**——它把 Skill 当成「另一种可选做法」而不是
「该走的那条路」。改成指令式（命中就加载 / 替代你的默认做法 / 用户不必点名 /
拿不准就加载）之后才正常。

委派面临的是同一个偏差，而且更严重：模型「自己动手」的默认倾向比
「加载一份指令」强得多。因此这里的四条与 C11 逐条对应，且额外强调
**上下文成本**——那是委派唯一真正的收益，不说清楚模型无从权衡。
"""

from __future__ import annotations

from rhinecode.subagents.models import AgentCatalog

_INDEX_HEADER = (
    "以下是可以**委派**给你的子 Agent 角色。每个角色有自己独立的上下文、"
    "受限的工具集和固定的职责。",
    "",
    "**接到任务先扫一遍这份清单。** 如果手上的活属于其中某个角色覆盖的类型，"
    "**用 `run_agent` 委派给它，而不是自己动手做**。",
    "",
    "⚠️ **用户不必明确说「让某个角色去做」。** 判断依据是**任务类型是否匹配**，"
    "不是用户有没有点名。他说「看看这个项目里哪里用到了 X」而清单里有调研类角色，"
    "那就是命中。",
    "",
    "⚠️ **拿不准要不要委派时，倾向委派。** 两边的代价不对称："
    "该委派而没委派 = 二十个文件的内容全部堆进当前对话，"
    "此后每一轮请求都要重发一遍，很快就要压缩历史；"
    "多委派一次 = 多花一次子 Agent 的调用，而它只回流一段结论。",
    "",
    "**尤其是这类任务**：要读很多文件才能回答的调研、要翻遍代码库的定位、"
    "要跑一遍再汇总的检查。它们的中间过程对你毫无价值，只有结论有价值。",
)

# 无角色时返回空串的理由见 `render_agent_index`。
_EMPTY = ""


def render_agent_index(catalog: AgentCatalog, budget: int = 0) -> str:
    """
    渲染角色清单（spec F9）。

    :param catalog: 已扫描的角色目录
    :param budget: 字符预算；`0` 或负数表示不限。超预算时**保名字只砍描述**
    :returns: 一段可直接拼进系统提示动态段的文本；**无角色时返回空串**

    **无角色时返回空串而不是一个空清单**：后者会让模型看到「你有委派能力，
    但一个角色都没有」，进而可能反复尝试 `run_agent` 去试探。空串则等于
    这段提示不存在，模型只会看到工具描述本身。

    **超预算时保名字只砍描述**（照 C11 作者期扩展的结论）：名字是模型
    发起委派的必需品，描述只影响它选得准不准。砍名字等于让那个角色消失，
    砍描述只是让选择变粗。

    副作用：无（纯函数）。
    """
    if not catalog.specs:
        return _EMPTY

    lines = [*_INDEX_HEADER, ""]

    def _entry(name: str, description: str) -> str:
        if description:
            return f"- **{name}** —— {description}"
        return f"- **{name}**"

    full = list(lines)
    for name, spec in catalog.specs.items():
        full.append(_entry(name, spec.description))

    text = "\n".join(full)
    if budget <= 0 or len(text) <= budget:
        return text

    # 降级：只留名字
    degraded = list(lines)
    for name in catalog.specs:
        degraded.append(_entry(name, ""))
    return "\n".join(degraded)


__all__ = ["render_agent_index"]
