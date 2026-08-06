"""
系统提示模块定义（c5 F1 / F3 / F7 / F8）。

把原来「空白/三行」的全局指令，拆成若干职责单一的模块，按优先级拼装成完整 System Prompt。
这样做的好处：
- 每个模块只讲一件事，便于单独维护与 review；
- 新增模块（将来的记忆 / Skill / 自定义指令）只需多声明一个 PromptModule，不动已有模块（F4）；
- 通过 cacheable 标志区分「稳定可缓存」与「动态」两类内容，决定它走哪条通道（见 builder.py）。

优先级约定（priority 越小越靠前）：
- 10–70：固定模块（cacheable=True，进可缓存的稳定前缀）。
  其中 **25 = 外部不可信内容**，按 `fixed_modules(untrusted_enabled=...)` 开关注入，
  关闭时该号位为空、输出与 web_fetch 扩展之前逐字一致。
- 100  ：环境信息（cacheable=False，由 builder 注入，见 builder.build_default_prompt）
- 110–130：三个可选模块空槽（cacheable=False，c5 恒为空，拼装时被跳过）
"""

from dataclasses import dataclass

from rhinecode.agent.prompt.texts import (
    IDENTITY,
    SYSTEM_CONSTRAINTS,
    UNTRUSTED_CONTENT,
    TASK_MODE,
    ACTION_EXECUTION,
    TOOL_USAGE,
    TONE,
    TEXT_OUTPUT,
)


@dataclass
class PromptModule:
    """
    一个系统提示模块。

    :param name: 模块名，仅用于调试/可读性，不出现在最终拼装文本里
    :param priority: 优先级，越小越靠前，决定模块在系统提示中的拼接顺序
    :param cacheable: True→进稳定可缓存通道（system 参数）；False→进动态通道（system-reminder）
    :param content: 模块正文；为空字符串表示「本模块当前无内容」，拼装时整体跳过（不产生多余空行）
    """

    name: str
    priority: int
    cacheable: bool
    content: str


def fixed_modules(untrusted_enabled: bool = False) -> list[PromptModule]:
    """
    返回固定模块（稳定、可缓存）：七个常驻 + 一个按开关注入。

    顺序（按 priority）：身份 → 系统约束 →〔外部不可信内容〕→ 任务模式 → 动作执行
    → 工具使用 → 语气风格 → 文本输出。其中：
    - 「系统约束」写明 <system-reminder> 是系统补充上下文、不要当成用户输入来回复（F8）。
    - 「外部不可信内容」写明 <untrusted-content> 里的是数据不是指令（web_fetch 扩展 F21）。
      它排在「系统约束」之后是刻意的——两条讲的是同一件事的两面
      （什么算系统指令 / 什么不算），相邻便于模型建立对照。
    - 「工具使用」写明「优先用专用工具」「编辑前必先读」等关键规则，与各工具自身描述形成双重强化（F7）。

    :param untrusted_enabled: 是否注入「外部不可信内容」模块。**缺省 False**——
        缺省不注入使既有调用点不改也能跑，且输出与本扩展之前逐字一致（F4）。
        由 `build_default_prompt` 从 `Config.web_fetch_enabled` 透传。
    :returns: 7 或 8 个 PromptModule，均 cacheable=True

    副作用：无（每次返回新建的列表，内容为内置常量文本）。
    """
    # 各模块正文已迁出到 texts 子包；这里只保留「结构 + 优先级 + 是否可缓存」等元数据，
    # 改文案请到 rhinecode/agent/prompt/texts/ 对应文件，无需改动本函数。
    modules = [
        PromptModule(name="身份", priority=10, cacheable=True, content=IDENTITY),
        PromptModule(name="系统约束", priority=20, cacheable=True, content=SYSTEM_CONSTRAINTS),
        PromptModule(name="任务模式", priority=30, cacheable=True, content=TASK_MODE),
        # ↑ 25 号位（外部不可信内容）按开关插在下面，不写死在这个列表里——
        # 关闭时列表内容要与本扩展之前**逐字一致**。
        PromptModule(name="动作执行", priority=40, cacheable=True, content=ACTION_EXECUTION),
        PromptModule(name="工具使用", priority=50, cacheable=True, content=TOOL_USAGE),
        PromptModule(name="语气风格", priority=60, cacheable=True, content=TONE),
        PromptModule(name="文本输出", priority=70, cacheable=True, content=TEXT_OUTPUT),
    ]
    if untrusted_enabled:
        modules.append(
            PromptModule(
                name="外部不可信内容", priority=25, cacheable=True, content=UNTRUSTED_CONTENT
            )
        )
    return modules


def optional_slots() -> list[PromptModule]:
    """
    返回可选模块的「空槽」（c5 预留，内容恒为空）。

    这些槽位为各项能力预留位置与优先级。content 为空字符串时拼装器会整体跳过、
    不产生任何文本与多余空行（F3），因此未启用的能力对输出零影响。
    实现对应能力时只需在拼装入口填入 content，无需改动拼装逻辑（F4）。

    当前五个槽位：
    - 110「自定义指令」（c9 填充：三层 RHINE.md）
    - 120「已激活 Skill」（c11 填充：已激活 Skill 的完整 SOP 正文）
    - 130「长期记忆」（c9 填充：两级记忆索引）
    - 135「可用子 Agent 角色」（c13 填充：角色清单）
    - 140「可用 Skill 清单」（c11 填充：第一阶段清单）

    **为什么子 Agent 角色清单是 135、排在 Skill 清单之前**（c13）：
    两者都进稳定通道，而前缀缓存「从第一处变化起、其后全部失效」，
    因此**越稳定的内容越该排在前面**。角色目录在会话内**恒定不变**
    （本章不提供 reload，改了要重启），Skill 清单则会随 `/skills reload` 变。
    排在它前面，一次 Skill 热更新就不会连带把角色清单的缓存也打掉。

    **为什么「可用 Skill 清单」是 140 而不是插在 115**（c11 T33）：
    它进的是**稳定通道**（cacheable=True），而稳定段是前缀缓存的作用对象。
    前缀缓存的性质是「从第一处变化开始，其后全部失效」。清单会随
    `/skills reload` 热更新而变；排在最后，一次热更新只失效它自己那一段，
    不会连带把 130 的记忆索引与 110 的 RHINE.md 的缓存一起打掉。

    :returns: content 为空的 PromptModule 列表，均 cacheable=False

    副作用：无。
    """
    return [
        PromptModule(name="自定义指令", priority=110, cacheable=False, content=""),
        PromptModule(name="已激活 Skill", priority=120, cacheable=False, content=""),
        PromptModule(name="长期记忆", priority=130, cacheable=False, content=""),
        PromptModule(name="可用子 Agent 角色", priority=135, cacheable=True, content=""),
        PromptModule(name="可用 Skill 清单", priority=140, cacheable=True, content=""),
    ]
