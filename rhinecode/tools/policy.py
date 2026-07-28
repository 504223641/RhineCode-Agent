"""
每轮工具集的收窄策略（c11 T1）。

本模块只定义一个不可变数据类 `ToolPolicy`，描述「这一轮模型能看见哪些工具」。

**为什么放在 tools 包而不是 agent 或 skills**：
`ToolPolicy` 由 `skills/manager.py` **生产**（根据已激活 Skill 的白名单算出来）、
由 `agent/loop.py` **消费**（用它过滤 `registry.schemas()`）。放进任一方都会制造
反向依赖（agent → skills 或 skills → agent），因此下沉到两者的共同下层 `tools`：
`agent/loop.py` 本来就导入 `tools.base` 与 `tools.registry`，而 `skills` 依赖
`tools.policy` 是新增的单向边。

本模块**零依赖**（只用标准库），这是打破 `agent ↔ skills` 潜在环的关键——
它不导入 `tools` 包内的任何其它模块，也不导入 `skills`。

依赖方向：`tools/policy.py` ← `agent/loop.py`、`skills/models.py`。
"""

from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class ToolPolicy:
    """
    一轮对话中工具集的收窄策略。

    Agent 循环在**每一轮**请求前重新求值本策略（因为模型可能在第 N 轮激活 Skill，
    第 N+1 轮的工具集就该随之收窄），再按三个字段过滤要发给模型的工具描述列表。

    过滤优先级（见 `agent/loop.py` 的 `_visible`）：
    `excluded` > `exempt` > `allowed`——先无条件剔除，再无条件保留，最后才看白名单。

    :param allowed: 白名单。**None 表示不收窄**（全部已注册工具可见）；
                    非 None 时只保留集合内的工具。对应 spec F14——多个 Skill
                    同时激活时取各自白名单的**并集**，再与注册中心当前工具名取交集
                    （运行期自愈：MCP 热重载后消失的工具自动不出现在可见集里）。
    :param exempt: 豁免收窄、无论白名单如何都可见的工具名。对应 spec F8/F15——
                   主对话里恒含 `load_skill`（它是系统级第二阶段加载入口，
                   若被白名单挡住，模型就再也无法加载其它 Skill）。
    :param excluded: 无条件移除的工具名，优先级最高。对应 spec F23——
                     独立模式子对话里放 `load_skill`，禁止子对话再嵌套激活 Skill。
    """

    allowed: Optional[frozenset[str]]
    exempt: frozenset[str]
    excluded: frozenset[str]
