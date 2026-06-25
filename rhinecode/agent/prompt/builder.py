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


def build_default_prompt(env: EnvironmentInfo) -> AssembledPrompt:
    """
    构造 RhineCode 默认系统提示：7 固定模块 + 环境信息 + 3 可选空槽。

    这是上层（ConversationManager）每次运行调用的便捷入口。环境信息被包装成一个
    priority=100、cacheable=False 的模块，因此它排在 7 个固定模块之后、走动态通道（F2/F5）。

    :param env: 已采集的环境信息，其 render() 作为环境模块的内容
    :returns: AssembledPrompt(stable=7 固定模块, dynamic=环境信息)；c5 中可选空槽无内容被跳过

    副作用：无。
    """
    builder = SystemPromptBuilder()
    for m in fixed_modules():
        builder.add(m)
    builder.add(
        PromptModule(name="环境信息", priority=100, cacheable=False, content=env.render())
    )
    for slot in optional_slots():
        builder.add(slot)
    return builder.build()
