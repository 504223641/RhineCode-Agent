"""
Skill 工具白名单的两段校验与空集降级（c11 T17/T18）。

**为什么要分两段**（spec F16）：白名单里的工具名分两类，判别式是名字前缀。

| 名字形态 | 何时校验 | 校验不过怎么办 | 理由 |
|---|---|---|---|
| 非 `mcp__` 开头 | **启动时** | **fail-fast 退出** | 内置工具名是固定的，写错就是笔误。等到运行时才发现，用户会以为 Skill 在正常工作，实际白名单少了一项、模型看不到那个工具却不知道为什么 |
| `mcp__` 开头 | **连接完成后** | 剔除该项 + 警告 | MCP Server 可能这次没连上（网络、凭据、进程起不来），这不是 Skill 的错，不该让整个程序起不来 |

**纯函数**：输入是「名字集合」而不是 `ToolRegistry` 对象，本模块不认识注册中心。
这样测试只需要传两个 set，不必构造真实工具。

对应 spec 条款：F16（两段校验）、F17（白名单剔空后降级）。
"""

from dataclasses import dataclass, replace
from pathlib import Path
from typing import Iterable

from rhinecode.skills.models import MCP_PREFIX, SkillSpec


@dataclass(frozen=True)
class FatalToolName:
    """
    一条致命的白名单项——不存在的内置工具名（spec F16 第一段）。

    :param skill_name: 声明它的 Skill 名
    :param path: 该 Skill 的入口文件路径，用户按这个去改
    :param tool_name: 那个不存在的工具名
    """

    skill_name: str
    path: Path
    tool_name: str


def check_builtin_tool_names(
    skills: Iterable[SkillSpec], known: frozenset[str]
) -> list[FatalToolName]:
    """
    第一段校验：找出白名单里不存在的**非 MCP** 工具名（spec F16）。

    :param skills: 待校验的 Skill 列表
    :param known: 已知工具名全集。组成是
                  「工具注册中心当前全部名字」∪「`ask_user`、`present_plan`」——
                  后两个是 Plan Mode 的特殊工具，不在注册中心里但确实可被模型调用，
                  白名单写它们不算笔误。
                  注意 `load_skill` 此刻**已经在注册中心里**（`__main__` 先注册它
                  再调本函数），所以不需要特判。
    :returns: 致命项列表，空列表表示全部通过

    **只检查非 `mcp__` 开头的名字**：MCP 工具的存在与否取决于 Server 这次连没连上，
    在这里判死刑会让「MCP Server 临时挂了」升级成「RhineCode 起不来」。
    它们由 `prune_mcp_tool_names` 在连接完成后处理。

    副作用：无。
    """
    fatals: list[FatalToolName] = []
    for spec in skills:
        if spec.allowed_tools is None:
            continue
        for tool in spec.allowed_tools:
            if tool.startswith(MCP_PREFIX):
                continue
            if tool not in known:
                fatals.append(FatalToolName(spec.name, spec.entry_path, tool))
    return fatals


def collect_exempt_notices(
    skills: Iterable[SkillSpec], exempt: frozenset[str]
) -> list[str]:
    """
    找出「声明了但没有任何效果」的白名单项，产出可删除提示。

    :param skills: 待校验的 Skill 列表
    :param exempt: 豁免收窄的工具名集合（`load_skill` / `ask_user` / `present_plan`）
    :returns: 提示文本列表

    这些工具无论白名单怎么写都可见（spec F8/F15），所以写进白名单既不报错、
    也毫无作用。不提示的话，用户会以为「我把 ask_user 加进白名单了所以它才可见」，
    从而对白名单的作用范围产生错误理解——下次删掉它就会困惑为什么行为没变。

    副作用：无。
    """
    notices: list[str] = []
    for spec in skills:
        if spec.allowed_tools is None:
            continue
        for tool in spec.allowed_tools:
            if tool in exempt:
                notices.append(
                    f"Skill `{spec.name}` 声明的 `{tool}` 不受白名单影响"
                    f"（它始终可见），该声明没有效果，可以删除"
                )
    return notices


def format_fatal_message(fatals: Iterable[FatalToolName]) -> str:
    """
    把致命项渲染成启动失败时打给 stderr 的完整消息。

    :param fatals: 致命项列表
    :returns: 多行消息文本

    必须给出三样东西，缺一样用户就得自己猜：
    **哪个文件**（路径，直接能打开）、**哪个名字写错了**、**可能的原因**。

    最后那句「版本不匹配」不是套话：用户从别人那里拷来的 Skill 可能是为更新版本的
    RhineCode 写的，白名单里那个工具在当前版本还不存在。这跟自己打错字是完全不同的
    两种情况，处理方式也不同（前者要升级或删掉该项，后者改拼写），提示里说清楚
    能省掉一轮困惑。

    副作用：无。
    """
    items = list(fatals)
    lines = [
        "启动失败：Skill 的 allowed_tools 白名单中存在不存在的工具名。",
        "",
    ]
    for f in items:
        lines.append(f"  Skill `{f.skill_name}`（{f.path}）")
        lines.append(f"    未知工具名：{f.tool_name}")
    lines.extend(
        [
            "",
            "请检查上述工具名是否拼写正确。",
            "若确认该工具名无误，可能是 RhineCode 版本与该 Skill 不匹配"
            "（该 Skill 是为其它版本编写的），请升级 RhineCode 或删除该白名单项。",
        ]
    )
    return "\n".join(lines)


def prune_mcp_tool_names(
    skills: Iterable[SkillSpec], registered: frozenset[str]
) -> tuple[list[SkillSpec], list[str]]:
    """
    第二段校验：剔除指向未连接 MCP Server 的白名单项（spec F16/F17）。

    :param skills: 待处理的 Skill 列表
    :param registered: 连接完成后注册中心的全部工具名
    :returns: `(新的 Skill 列表, 警告列表)`。新列表与输入等长、顺序一致，
              只有 `allowed_tools` 可能被改写

    两种改写：
    1. **部分剔除**——白名单里有 `mcp__` 项没连上，剔掉它，其余保留，产出一条警告。
    2. **全部剔空后降级**（F17）——若剔完变成空 tuple，把 `allowed_tools`
       **置回 None**（= 不收窄）而不是留一个空白名单。
       理由：空白名单意味着「模型一个工具都看不见」，这个 Skill 直接变成废物，
       而且失败形态很隐蔽（模型说「我没有工具可用」，用户完全不知道是 MCP 没连上）。
       降级成「不收窄」至少 Skill 还能跑，只是少了工具集收窄这个精度优化——
       白名单本来就是**提升模型选对工具的准确率**的手段，不是安全边界（N10），
       降级不会造成任何安全问题。

    用 `dataclasses.replace` 产出新对象，保持 `SkillSpec` 的 frozen 语义。

    副作用：无。
    """
    out: list[SkillSpec] = []
    warnings: list[str] = []

    for spec in skills:
        if spec.allowed_tools is None:
            out.append(spec)
            continue

        kept: list[str] = []
        for tool in spec.allowed_tools:
            if tool.startswith(MCP_PREFIX) and tool not in registered:
                warnings.append(
                    f"Skill `{spec.name}` 的白名单项 `{tool}` 对应的 MCP Server "
                    f"未连接，已从白名单剔除"
                )
                continue
            kept.append(tool)

        if not kept:
            warnings.append(
                f"Skill `{spec.name}` 的白名单已全部失效，本次运行不收窄工具集"
                f"（全部工具对它可见）"
            )
            out.append(replace(spec, allowed_tools=None))
        elif len(kept) != len(spec.allowed_tools):
            out.append(replace(spec, allowed_tools=tuple(kept)))
        else:
            # 一项都没剔，原样返回同一个对象，避免无意义的重建。
            out.append(spec)

    return out, warnings
