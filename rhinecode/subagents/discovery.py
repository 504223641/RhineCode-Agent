"""
角色定义的三层目录扫描（c13 T4，spec F2/F3）。

**职责**：三个目录 → 一份 `AgentCatalog`。扫目录、读文件、调 `parser.parse_agent`、
做层内去重与跨层覆盖。

**只读文件系统，全程 fail-safe**：任何单个文件的问题（坏 YAML、缺 description、
读不出来）都只影响它自己，其余角色照常可用；目录不存在直接当空层，不算错误。
理由与 C11 相同——角色是可选增强，不该因为某人往目录里丢了个记事本文件
就让整个 RhineCode 起不来。

## 与 C11 的一处刻意不同：身份来自 `name` 而非路径

C11 的对齐改造把 Skill 的命令名定为「来自文件系统路径」，理由是「外部 Skill
原样可用」。角色**反过来**：Claude Code 的角色文件把身份放在 `name` 上、
明确允许文件名不匹配（官方文档原话：*The filename doesn't have to match*），
若我们强行改用路径，从官方生态复制来的定义会因为文件名不一致而变成另一个角色——
用户按 `description` 里写的名字去委派会找不到。

代价是**同层内可能重名**（路径天然不会重）。处理见下面的 `_scan_layer`：
保留字典序靠前的那份，另一份记进 `shadowed` 并出一条错误，两份都在 `/agents` 里可见。
不静默择一，因为用户看到的现象会是「我明明写了这个角色却是另一套配置」。
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from rhinecode.subagents.models import (
    ENTRY_SUFFIX,
    AgentCatalog,
    AgentLoadError,
    AgentSource,
    AgentSpec,
    ShadowedAgent,
)
from rhinecode.subagents.parser import AgentParseError, parse_agent


def _scan_layer(
    directory: Optional[Path],
    source: AgentSource,
) -> tuple[list[AgentSpec], list[AgentLoadError], list[ShadowedAgent]]:
    """
    扫描单个层级目录。

    :param directory: 该层目录；`None` 或不存在时当作空层
    :param source: 该层的来源标记，写进产出的 spec 与错误
    :returns: `(specs, errors, shadowed)` 三元组，`specs` 按**扫描顺序**排列

    识别规则：

    - `*.md` 文件 → 一个角色定义；
    - 其它文件（`.txt` / `.yaml` / 无后缀 / 子目录）→ **静默跳过**，不记错误。
      理由与 C11 相同：用户可能在目录里放草稿或编辑器临时文件，
      为这些刷一堆错误只会让 `/agents` 变成噪音。
      （注意 `README.md` 会被当角色解析并因缺 `description` 报错——
      这是**刻意的**：`.md` 就是角色定义的后缀，不为特定文件名开后门，
      否则「哪些 .md 算角色」会变成一张要维护的名单。）

    用 `sorted(iterdir())` 遍历，让扫描顺序（进而是「同层重名谁生效」
    与错误的先后）在任何机器上都确定。

    副作用：读目录与读文件。不写任何东西。
    """
    specs: list[AgentSpec] = []
    errors: list[AgentLoadError] = []
    shadowed: list[ShadowedAgent] = []

    if directory is None:
        return specs, errors, shadowed

    try:
        if not directory.is_dir():
            # 目录不存在是**正常情况**（用户没建过 agents 目录），不是错误。
            return specs, errors, shadowed
        entries = sorted(directory.iterdir())
    except OSError as exc:
        errors.append(
            AgentLoadError(directory, source, f"读取角色目录失败：{exc}")
        )
        return specs, errors, shadowed

    seen: dict[str, AgentSpec] = {}
    for entry in entries:
        if not (entry.is_file() and entry.suffix.lower() == ENTRY_SUFFIX):
            continue

        try:
            text = entry.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            errors.append(AgentLoadError(entry, source, f"读取失败：{exc}"))
            continue

        try:
            spec = parse_agent(text, entry, source, entry.stem)
        except AgentParseError as exc:
            errors.append(AgentLoadError(entry, source, str(exc)))
            continue

        # ── 层内重名：先到先得（字典序靠前），后来者进 shadowed ──
        if spec.name in seen:
            winner = seen[spec.name]
            shadowed.append(
                ShadowedAgent(spec.name, source, entry, source)
            )
            errors.append(
                AgentLoadError(
                    entry,
                    source,
                    f"角色名 {spec.name!r} 与同层的 {winner.path.name} 重复，"
                    f"本文件未生效（同层重名按文件名字典序取靠前者）",
                )
            )
            continue

        seen[spec.name] = spec
        specs.append(spec)

    return specs, errors, shadowed


def discover_agents(
    project_dir: Optional[Path],
    user_dir: Optional[Path],
    builtin_dir: Optional[Path],
) -> AgentCatalog:
    """
    扫描三层目录并按优先级合并（spec F2）。

    :param project_dir: `<项目根>/.rhinecode/agents/`
    :param user_dir: `~/.rhinecode/agents/`
    :param builtin_dir: 随包分发的内置目录，通常是 `builtin_agents_dir()`
    :returns: 合并后的目录；任何一层缺失都不影响其余层

    优先级 **项目 > 用户 > 内置**：按这个顺序扫描，**先到先得**——
    高优先层的角色先进 `specs`，低优先层的同名者被记进 `shadowed`。

    `specs` 的插入顺序即扫描顺序，报告与清单据此展示，保证任何机器上一致。

    副作用：读文件系统。不写任何东西、不抛任何异常。
    """
    layers = (
        (project_dir, AgentSource.PROJECT),
        (user_dir, AgentSource.USER),
        (builtin_dir, AgentSource.BUILTIN),
    )

    specs: dict[str, AgentSpec] = {}
    errors: list[AgentLoadError] = []
    shadowed: list[ShadowedAgent] = []

    for directory, source in layers:
        layer_specs, layer_errors, layer_shadowed = _scan_layer(directory, source)
        errors.extend(layer_errors)
        shadowed.extend(layer_shadowed)
        for spec in layer_specs:
            if spec.name in specs:
                # 被高优先层覆盖。**不记 error**——这是正常的覆盖行为，
                # 不是谁写错了；只进 shadowed，让用户在报告里能查到
                # 「我改的那个文件为什么没生效」。
                shadowed.append(
                    ShadowedAgent(
                        spec.name, source, spec.path, specs[spec.name].source
                    )
                )
                continue
            specs[spec.name] = spec

    return AgentCatalog(
        specs=specs,
        errors=tuple(errors),
        shadowed=tuple(shadowed),
    )


__all__ = ["discover_agents"]
