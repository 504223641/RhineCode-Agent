"""
系统提示拼装器（c5 F1 / F4 / F5）。

职责：把若干 PromptModule 按优先级拼装成最终文本，并按「是否可缓存」分成两条通道：
- stable ：所有 cacheable=True 模块拼成的稳定前缀，逐轮逐字节一致，走 stream_chat 的 system 参数，
           依赖 Provider 的前缀缓存命中（DeepSeek 自动前缀缓存）。
- dynamic：所有 cacheable=False 模块拼成的动态内容（环境信息、可选模块），由循环每轮包进
           <system-reminder> 注入到消息末尾，不污染 stable 的缓存。

为什么按通道拆分而不是拼成一整段：稳定与动态混在一起，任何动态字段（如日期）变化都会让整段
缓存失效；拆开后只有动态部分不缓存，稳定前缀始终命中（见 spec F5/N4）。
"""

from dataclasses import dataclass

from rhinecode.agent.prompt.environment import EnvironmentInfo
from rhinecode.agent.prompt.modules import PromptModule, fixed_modules, optional_slots


@dataclass
class AssembledPrompt:
    """
    拼装结果，按通道分离。

    :param stable: 可缓存的稳定系统提示（→ stream_chat 的 system 参数）
    :param dynamic: 动态内容（→ 每轮的 <system-reminder>）
    """

    stable: str
    dynamic: str


class SystemPromptBuilder:
    """
    系统提示拼装器。

    用法：逐个 add(module)，最后 build() 得到 AssembledPrompt。
    模块的添加顺序无关紧要——build() 内部会按 priority 排序，保证拼装顺序稳定可预测。
    """

    # 模块之间的分隔：空行（即两个换行），与 spec「模块之间空行分隔」一致。
    _SEPARATOR = "\n\n"

    def __init__(self) -> None:
        # 收集到的模块列表；build() 时统一排序、过滤、拼接。
        self._modules: list[PromptModule] = []

    def add(self, module: PromptModule) -> "SystemPromptBuilder":
        """
        加入一个模块。

        :param module: 要加入的 PromptModule
        :returns: self，便于链式调用

        副作用：向内部列表追加 module。
        """
        self._modules.append(module)
        return self

    def build(self) -> AssembledPrompt:
        """
        按优先级拼装出 stable 与 dynamic 两段文本。

        执行步骤：
        1. 按 priority 升序稳定排序（priority 相同则保持添加顺序）。
        2. 跳过 content 去除首尾空白后为空的模块（空槽不输出、不产生多余空行，F3）。
        3. cacheable=True 的模块拼进 stable，其余拼进 dynamic；各自用空行分隔。

        :returns: AssembledPrompt(stable, dynamic)

        副作用：无（不修改已加入的模块）。
        """
        ordered = sorted(self._modules, key=lambda m: m.priority)
        stable_parts: list[str] = []
        dynamic_parts: list[str] = []
        for m in ordered:
            if not m.content.strip():
                # 空内容模块（如 c5 的可选空槽）整体跳过，避免拼出多余空行。
                continue
            if m.cacheable:
                stable_parts.append(m.content)
            else:
                dynamic_parts.append(m.content)
        return AssembledPrompt(
            stable=self._SEPARATOR.join(stable_parts),
            dynamic=self._SEPARATOR.join(dynamic_parts),
        )


def build_default_prompt(
    env: EnvironmentInfo,
    custom_instructions: str = "",
    memory_index: str = "",
    skill_index: str = "",
    active_skills: str = "",
    untrusted_enabled: bool = False,
) -> AssembledPrompt:
    """
    构造 RhineCode 默认系统提示：7 固定模块 + 环境信息 + 可选槽位（c5 预留、c9 填充）。

    这是上层（ConversationManager）每次运行调用的便捷入口。环境信息被包装成一个
    priority=100、cacheable=False 的模块，因此它排在 7 个固定模块之后、走动态通道（F2/F5）。

    c9 起两个预留槽位有了真实内容：
    - custom_instructions（「自定义指令」，priority 110）：三层 RHINE.md 拼接结果；
    - memory_index（「长期记忆」，priority 130）：两级记忆索引。
    两者都以 **cacheable=True** 进稳定通道，覆盖 c5 空槽的 False 预设——动态通道的
    内容每轮作为不缓存的尾巴重发，RHINE.md 可达数百行、索引最大 25KB，每轮重发太贵；
    而这两块在会话内基本稳定（RHINE.md 启动加载后不变、索引仅笔记更新后变化），
    进 stable 尾部可被 DeepSeek 前缀缓存命中，索引变化也只失效它自己那段尾部缓存
    （c9 plan 技术决策）。传空串时槽位照旧整体跳过，输出与 c8 完全一致。

    c11 起再填两个 Skill 槽位，二者**分属不同通道**，这是两阶段加载在提示层的体现：
    - skill_index（「可用 Skill 清单」，priority 140，**cacheable=True → 稳定通道**）：
      只有名字与一句话说明，会话内基本不变（仅 `/skills reload` 后变化），
      进稳定通道可被前缀缓存命中。排在最末使热更新只失效它自己那段（见 optional_slots）。
    - active_skills（「已激活 Skill」，priority 120，**cacheable=False → 动态通道**）：
      已激活 Skill 的完整 SOP 正文。它必须每轮重发——模型可能在循环中途才激活
      某个 Skill，而稳定通道的内容在一次 `run()` 里是固定的，装不下这种变化。

    两者传空串时槽位照旧整体跳过，输出与 c10 逐字节一致（spec N3 零回归）。

    :param env: 已采集的环境信息，其 render() 作为环境模块的内容
    :param custom_instructions: 「自定义指令」槽位内容（c9：RHINE.md 拼接结果），空串跳过
    :param memory_index: 「长期记忆」槽位内容（c9：记忆索引），空串跳过
    :param skill_index: 「可用 Skill 清单」槽位内容（c11：第一阶段清单），空串跳过
    :param active_skills: 「已激活 Skill」槽位内容（c11：已激活 SOP 正文），空串跳过
    :returns: AssembledPrompt(stable=固定模块+记忆槽位+Skill 清单,
              dynamic=环境信息+已激活 Skill)

    副作用：无。
    """
    builder = SystemPromptBuilder()
    # untrusted_enabled 缺省 False：既有调用点不改也能跑，且输出逐字等于
    # web_fetch 扩展之前（spec F4）。
    for m in fixed_modules(untrusted_enabled):
        builder.add(m)
    builder.add(
        PromptModule(name="环境信息", priority=100, cacheable=False, content=env.render())
    )
    # c9 实际填充的两个槽位（优先级沿用 c5 预留值：110 自定义指令 / 130 长期记忆）。
    builder.add(
        PromptModule(name="自定义指令", priority=110, cacheable=True, content=custom_instructions)
    )
    builder.add(
        PromptModule(name="长期记忆", priority=130, cacheable=True, content=memory_index)
    )
    # c11 填充的两个 Skill 槽位。注意通道不同：清单进 stable（可缓存），
    # 已激活正文进 dynamic（每轮重发，因为循环中途可能新增）。
    builder.add(
        PromptModule(name="可用 Skill 清单", priority=140, cacheable=True, content=skill_index)
    )
    builder.add(
        PromptModule(name="已激活 Skill", priority=120, cacheable=False, content=active_skills)
    )
    # 其余仍为空的预留槽：跳过上面已实际填充的四个，避免重复添加空槽。
    _FILLED = ("自定义指令", "长期记忆", "可用 Skill 清单", "已激活 Skill")
    for slot in optional_slots():
        if slot.name in _FILLED:
            continue
        builder.add(slot)
    return builder.build()
