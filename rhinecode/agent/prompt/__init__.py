"""
系统提示包（c5）。

把原 rhinecode/agent/prompt.py 升级为包，集中管理「结构化系统提示」相关能力：
- modules     ：PromptModule 与七个固定模块 + 三个可选空槽
- builder     ：SystemPromptBuilder / AssembledPrompt / build_default_prompt（拼装 + 分通道）
- environment ：EnvironmentInfo / collect_environment（环境信息采集）
- reminders   ：<system-reminder> 构造 + Plan Mode 完整/精简文本 + 按轮注入节奏

对外只需从本包导入下列入口，无需关心内部文件划分。
"""

from rhinecode.agent.prompt.environment import EnvironmentInfo, collect_environment
from rhinecode.agent.prompt.modules import PromptModule, fixed_modules, optional_slots
from rhinecode.agent.prompt.builder import (
    AssembledPrompt,
    SystemPromptBuilder,
    build_default_prompt,
)
from rhinecode.agent.prompt.reminders import (
    PLAN_BRIEF_INSTRUCTION,
    PLAN_FULL_INSTRUCTION,
    build_system_reminder,
    plan_toggle_instruction,
)

__all__ = [
    "EnvironmentInfo",
    "collect_environment",
    "PromptModule",
    "fixed_modules",
    "optional_slots",
    "AssembledPrompt",
    "SystemPromptBuilder",
    "build_default_prompt",
    "PLAN_BRIEF_INSTRUCTION",
    "PLAN_FULL_INSTRUCTION",
    "build_system_reminder",
    "plan_toggle_instruction",
]
