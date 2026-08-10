"""
角色清单的注入文本（c13 T19，spec F9）。

**职责**：把已加载的角色渲染成一段进主对话系统提示的文本，让主 Agent
知道「有哪些角色可用、什么时候该委派」。

## ⚠ 成对维护点：表头与委派工具的描述必须同口径

`_INDEX_HEADER` 与 `tools/run_agent.py` 的 `description` 是模型决定
「要不要委派」时读到的**唯一两处文本**。一处写得强、另一处写得弱，
等于白改——模型会按弱的那份行事。
护栏见 `tests/test_subagent_tool.py::SameVoiceTest`。

## ⚠ 措辞方向已于 2026-08-10 整个反转，别照着旧注释推理

**此前这里写的是「措辞要有点 pushy」**，理由是 C13/C15 真实模型验收观测到
**系统性欠触发**（0 次委派、0 条共享任务）。据此写下了四条推力：
命中就委派 / 用户不必点名 / 不要先看一眼再决定 / 拿不准就委派。

**那四条把模型推到了另一个极端。** 用户实测反馈：一个非常简单的任务
（一次 grep 就能答的问题）也被委派出去。根因是那四条**单向**：
只写了「该委派的理由」，一句「什么时候不该委派」都没有，而且
**明确拆掉了模型自己会用的两个刹车**——「先看一眼再决定」与
「按规模判断」都被点名禁止了。于是任何一个字面属于「调研类」的活，
不论多小都会被派出去。

⚠ **关键事实：欠触发与过触发是同一个模型（`deepseek-v4-flash`）。**
所以这不是模型强弱问题，而是提示词把默认值定在了错误的一侧。
`docs/todo/3-team-adoption.md` 原本假设「换强模型跑对照」能定性，
那个假设已被这次观测推翻。

**现在的口径对齐 Claude Code 的 Agent 工具**，四层意思逐条相反：

| | 旧（推力） | 新（对齐 Claude Code） |
| --- | --- | --- |
| 默认值 | 拿不准就委派 | **默认不委派**，除非有明确理由 |
| 用户要不要点名 | 不必点名 | **点名是主路径** |
| 委派贵不贵 | 「多一次调用」 | **冷启动、要重新推导你已有的上下文** |
| 「活有好几部分」算信号吗 | 算（任务之间相不相干） | **不算**，自己内联做完 |

Claude Code 原文（`Agent` 工具描述第二段）：
「Do not spawn agents unless the user asks. Each spawn starts cold and
re-derives context you already have — it's the expensive path on this plan.
A task with "multiple angles," "thorough," or several parts is not a request
to spawn; handle it inline with your own tools.」

## ⚠ 这一反转会让一个曾被当成缺陷的行为复现，那是预期的

旧护栏 `test_both_forbid_looking_first` 记录过一次真实观测：模型判断
「适合委派给 explorer」，接着说「不过在此之前，我先快速看一下项目结构」，
读完 7 个文件后反过来用「项目不大」证明自己不该委派。

**在新口径下那不再是缺陷，恰恰是期望行为**——先自己看、发现活不大就自己
做完，正是 `handle it inline with your own tools`。不写清楚的话，
下一个人看到那条旧护栏消失会以为是漏改。

## ⚠ Skill 那一侧（`skills/render.py`）**刻意保持 pushy**，别顺手统一

两处措辞几乎一样、文件就在隔壁，极容易被「统一口径」。但它们的成本结构
**相反**：加载 Skill 只是往上下文里加一段文本（便宜、可逆），
委派要起一整条子对话并冷启动（贵）。Claude Code 自己也是这么分的——
它的 Skill 工具写「call this tool first」，Agent 工具写「do not spawn
unless the user asks」。反证护栏见 `test_subagent_tool.py::SameVoiceTest`。
"""

from __future__ import annotations

from rhinecode.subagents.models import AgentCatalog

_INDEX_HEADER = (
    "以下是可以**委派**给你的子 Agent 角色。每个角色有自己独立的上下文、"
    "受限的工具集和固定的职责。",
    "",
    "⚠️ **默认不要委派——先考虑自己动手做。** 委派不是免费的：子 Agent 从"
    "**空白上下文冷启动**，你已经知道的背景它要重新推导一遍，为此要多花"
    "若干轮请求。对大多数任务来说，**这比你自己做完更贵**。",
    "",
    "**只在这三种情况下委派**：",
    "",
    "1. **用户开口要求**——他说「用子 Agent」「让 explorer 去查」，"
    "或者直接点了某个角色的名字。这是最主要的一条。",
    "2. **广度调研**：要回答它得扫过**很多**文件、目录或命名习惯（不是三五个），"
    "而你只要那个结论、不需要文件内容本身。",
    "3. **确实独立的长活**：几件互不依赖、各自都要好几轮才能做完的事，"
    "并行做能省下可观的时间。",
    "",
    "⚠️ **这些都不构成委派信号**：任务分成了好几个部分、用户说了「彻底」"
    "「全面」「仔细」、你觉得活有点多。**用你自己手上的工具内联做完。**",
    "",
    "⚠️ **一两次工具调用就能做完的活，自己做。** 一次 `grep_content` 能定位的"
    "问题、读一个文件就能回答的问题，委派的开销远大于收益——"
    "起子对话、冷启动、等它跑完、再把结论读回来，比你直接搜一次贵得多。",
    "",
    "**允许先看一眼再决定。** 拿不准规模时，先用一两次搜索摸清楚，"
    "然后多半会发现自己做完就行了。",
    "",
    "**一旦决定要委派**，任务描述必须**自包含**——子 Agent 看不到你和用户的对话，"
    "背景、目标、期望的产出都要写进去。",
    "",
    "⚠️ **要改文件、而你手上也有未提交的改动时，委派时设 `isolation: true`。** "
    "它会在一个**独立的 Git 工作目录**里跑，你俩同时改文件也不会互相覆盖；"
    "成果通过一个新分支交回来，结论末尾会给出分支名，你据它 `git merge` 即可。"
    "不涉及写文件的调研类任务不需要它——建工作区要 checkout 一整份源码。"
    "注意角色自己声明了隔离时，你传 `false` 不生效（隔离只能加不能减）。",
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
