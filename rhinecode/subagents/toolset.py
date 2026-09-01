"""
子 Agent 最终工具集的分层计算（c13 T7，spec F13/F14）。

**纯函数，零 IO**：输入是「主对话可见的工具名集合 + 角色定义 + 是否后台」，
输出是「这个子 Agent 能看到哪些工具」。不 import 任何工具实现，也不碰注册中心。

## 三层顺序（固定，不可调换）

1. **全局禁止层**：任何子 Agent 都看不到，且调用了也要拒绝；
2. **角色限制层**：`tools` 白名单取交集，再减 `disallowed_tools` 黑名单；
3. **后台附加层**：本章为空集，理由见 `BACKGROUND_DENIED_TOOLS`。

顺序固定是因为语义不同：第 1 层是**安全边界**（防无限嵌套），
第 2 层是**用户配置**。把用户配置排在安全边界之前，一条
`tools: run_agent` 就能让子 Agent 拿到委派能力。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Optional

from rhinecode.subagents.models import AgentSpec

# ── 第 1 层：全局禁止 ──
#
# 用**字面量**而不是 `from rhinecode.tools.run_agent import RunAgentTool`：
# 本模块若依赖 `tools` 包，`tools ↔ subagents` 的包级互依就会从「只有一个
# 入口文件参与」变成「解析层也参与」，而本模块被 `parser` / `discovery` 之外
# 的纯逻辑测试直接使用，不该背上那份依赖。
#
# ⚠ **成对维护点（其一）**：新增「任何子 Agent 都不该看到」的工具 → 加进这里。
# **漏改不报错**，只是子 Agent 多出一个能力，而配置和界面上都看不出异常。
# 护栏见 `tests/test_subagent_toolset.py`（遍历本集合逐个断言）。
#
# ⚠ **成对维护点（其二）**：新增一个「会再开一层子对话」的工具 →
# 这里**与** `skills/manager.py` 的 `fork_excluded_tools()` **两处齐改**。
# 两张表是**同一条不变量的两个落点**——「已经是一层子对话的东西，不许再往下
# 开一层」；子 Agent 是一层，`context: fork` 的 Skill 也是一层（下面
# `load_skill` 那条的理由逐字就是这句）。**只改一处不报错**：另一条路上那个
# 工具照常可见、照常能调，而两条路的行为从此不一样，界面上完全看不出来。
# 真踩过：`run_agent` 从 C13 起就漏在 fork 那一侧，2026-09-01 才补齐。
# 护栏见 `tests/test_skill_isolated.py::ForkDelegationTest`。
GLOBAL_DENIED_TOOLS = frozenset(
    {
        # 委派工具自身：防无限嵌套（spec「不做的事」明确列出，子 Agent 之间不互相委派）。
        "run_agent",
        # Skill 加载工具：防**间接**嵌套——一个 `context: fork` 的 Skill 会再开
        # 一层子对话。只挡 run_agent 是不够的，那条路径绕开它。
        "load_skill",
        # 待办清单（todo-list 扩展 spec F9）：**主对话的私有进度笔记**，
        # 子 Agent 不该有。
        #
        # 理由不是「危险」——它不读写文件、不执行命令，副作用限于改本进程内存。
        # 理由是**屏幕上会出现两处对不上的进度数字**：子 Agent 的进度已由
        # 活动区（`ActivityView`）呈现，再给它一份待办，用户看到一块写着
        # 「待办 (2/4)」、一块写着「worker-a 运行中 · 7 次调用」，
        # 而这两个数字既不同源也不同义，没有任何线索说明该信哪个。
        #
        # ⚠ 本层排在角色白名单**之前**，因此一条 `tools: [todo_write]` 的
        # 角色定义**也拿不到它**——这正是这一层存在的意义。
        "todo_write",
    }
)

# c15：协作工具（spec F22 要求它们对**全部**子 Agent 可见）。
#
# ⚠ **它们豁免角色的 `tools` 白名单**——白名单取交集之后再加回来。
#
# 这是真实模型验收补的：`explorer` / `planner` 都声明了只读白名单
# （read_file / glob_files / grep_content），于是交集之后**一个协作工具都不剩**。
# 后果是协作里最自然的分工——「一个只读调研员 + 一个执行者」——根本跑不通：
# 调研员连「我查完了」都说不出口，也没法在共享清单上标完成。
# 实测撞到：主 Agent 把 impl-worker 派成 explorer，它跑完才发现没有
# `send_message`，主 Agent 只好又补派一个人重做。
#
# 单元测试没抓到，是因为那些用例用的都是 `tools=None`（继承全部）的角色，
# 而**内置三个角色里有两个声明了白名单**。`CollaborationToolsTest` 现已补上
# 声明白名单的对照组。
#
# `disallowed_tools` 仍然压得过它——用户显式禁掉的照样禁掉。
# 这不与 C13「子 Agent 的能力只会比主对话小」冲突：协作工具不读写文件、
# 不执行命令，副作用限于改本进程内存；而队员通过它让**别人**干的每一件事，
# 仍逐个过完整的五层权限管线 + Hook 前置层。
#
# ⚠ **`run_agent` 与 `load_skill` 必须继续留在 `GLOBAL_DENIED_TOOLS` 里**：
# C15 只让队员「能说话」，没有让它们「能招人」。少了那两条，一条
# `tools: [send_message, run_agent]` 的角色定义就能让子 Agent 拿到委派能力、
# 无限嵌套下去。
ALWAYS_GRANTED_TOOLS = frozenset(
    {
        "task_create",
        "task_list",
        "task_get",
        "task_update",
        "send_message",
    }
)

# ── 第 3 层：后台附加禁止 ──
#
# **本章为空集**，这是刻意的，不是没写完。
#
# Claude Code 里这一层的存在理由是「后台 agent 无法交互，因此摘掉需要交互的工具」。
# 而本章已定为**全程非交互**（spec F15：不论前台后台，判 ASK 一律自动拒绝），
# 前台子 Agent 与后台子 Agent 面对的约束完全相同，这一层因此没有独立内容。
#
# 保留结构位置而不是删掉，是为了让「将来真需要区分前后台」时有个明确的落点；
# 但**不为它造一批人为差异**——那会让同一个角色在两种场景下行为不同，
# 而用户从配置上完全看不出来。
BACKGROUND_DENIED_TOOLS: frozenset = frozenset()


@dataclass(frozen=True)
class ToolsetResult:
    """
    工具集计算的结果。

    :param allowed: 该子 Agent 能看到、也只能调用这些工具
    :param unresolved: 角色 `tools` 里**没有对应工具**的名字。
        它不影响 `allowed` 的正确性，但在 `allowed` 为空时是最有价值的诊断信息——
        用户十有八九是把工具名写错了。
    :param reason: `allowed` 为空时的可读说明；非空时为空串。
        直接作为委派失败的回灌文本，让模型知道是配置问题而不是它调错了。
    """

    allowed: frozenset
    unresolved: tuple[str, ...] = ()
    reason: str = ""

    @property
    def is_empty(self) -> bool:
        return not self.allowed


def resolve_toolset(
    all_tools: Iterable[str],
    spec: Optional[AgentSpec],
    is_background: bool = False,
) -> ToolsetResult:
    """
    算出一个子 Agent 的最终工具集（spec F13）。

    :param all_tools: 主对话当前可见的全部工具名
    :param spec: 角色定义；**`None` 表示分支式委派**（继承父工具集，不做角色限制）
    :param is_background: 是否后台运行，决定要不要应用第 3 层
    :returns: `ToolsetResult`

    执行步骤：

    1. 从 `all_tools` 减去 `GLOBAL_DENIED_TOOLS`；
    2. `spec.tools` 非 `None` 时取交集，并记下没匹配上的名字；
    3. **把 `ALWAYS_GRANTED_TOOLS` 加回来**（c15：协作工具豁免白名单，spec F22）；
    4. 减去 `spec.disallowed_tools`；
    5. 后台时再减 `BACKGROUND_DENIED_TOOLS`。

    ⚠ 第 3 步排在**白名单之后、黑名单之前**，位置是刻意的：
    它要能穿过白名单（否则只读角色一个协作工具都拿不到），
    但**不能穿过黑名单**（用户显式 `disallowed_tools` 禁掉的照样禁掉）。

    **`spec.tools is None` 与 `spec.tools == ()` 语义不同**：前者是「未声明，
    继承全部」，后者是「声明了但一个都不要」。后者会走到空集分支——
    那是用户写错了、该被告知的情形。

    副作用：无（纯函数）。
    """
    base = frozenset(all_tools)
    after_global = base - GLOBAL_DENIED_TOOLS

    unresolved: tuple[str, ...] = ()
    current = after_global

    if spec is not None and spec.tools is not None:
        wanted = frozenset(spec.tools)
        # 保持声明顺序输出未解析名字，便于用户对照自己写的那一行
        unresolved = tuple(name for name in spec.tools if name not in after_global)
        current = current & wanted

    # c15 F22：协作工具豁免角色白名单——见 `ALWAYS_GRANTED_TOOLS` 的说明。
    # 只加**注册中心里真的有**的那些（`after_global` 已经过第 1 层过滤），
    # 因此未启用协作时这一步是零影响。
    current = current | (after_global & ALWAYS_GRANTED_TOOLS)

    if spec is not None and spec.disallowed_tools:
        current = current - frozenset(spec.disallowed_tools)

    if is_background:
        current = current - BACKGROUND_DENIED_TOOLS

    if current:
        return ToolsetResult(allowed=current, unresolved=unresolved)

    return ToolsetResult(
        allowed=current,
        unresolved=unresolved,
        reason=_explain_empty(spec, base, after_global, unresolved),
    )


def _explain_empty(
    spec: Optional[AgentSpec],
    base: frozenset,
    after_global: frozenset,
    unresolved: tuple[str, ...],
) -> str:
    """
    组装「最终工具集为空」的可读说明（spec F14）。

    :returns: 一段中文，会原样回灌给模型

    为什么要说这么细：一个零工具的子 Agent 启动了也只会白烧一次 API 调用
    然后说「我做不了」。把「哪一层去掉了什么」讲清楚，模型才可能自我纠正
    （比如改用另一个角色），用户读到这段也知道去改哪一行。

    副作用：无。
    """
    parts = [f"该子 Agent 的最终工具集为空，无法启动。可见工具原有 {len(base)} 个。"]

    if not after_global:
        parts.append(
            "全部工具都在全局禁止名单里"
            f"（{'、'.join(sorted(GLOBAL_DENIED_TOOLS))}）。"
        )
    if spec is None:
        return "".join(parts)

    if spec.tools is not None:
        if not spec.tools:
            parts.append("角色声明了空的 tools 白名单（写成 `tools: []` 等于一个都不给）。")
        else:
            parts.append(
                f"角色的 tools 白名单声明了 {len(spec.tools)} 项："
                f"{'、'.join(spec.tools)}。"
            )
    if unresolved:
        parts.append(
            f"其中这些名字没有对应的工具（可能拼写有误）：{'、'.join(unresolved)}。"
        )
    if spec.disallowed_tools:
        parts.append(f"另有黑名单排除：{'、'.join(spec.disallowed_tools)}。")

    return "".join(parts)


__all__ = [
    "ALWAYS_GRANTED_TOOLS",
    "GLOBAL_DENIED_TOOLS",
    "BACKGROUND_DENIED_TOOLS",
    "ToolsetResult",
    "resolve_toolset",
]
