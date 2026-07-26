"""
Skill 系统包（c11）。

把可复用的 AI 操作封装成带元信息的独立 Markdown 文件，支持两阶段加载
（启动只注入名字与一句话说明，用时再由 `load_skill` 工具加载完整 SOP）
与两种执行模式（共享当前对话 / 开独立子对话跑完回流结论）。

设计范式与 `permission/` `context/` `memory/` `commands/` 一致：
**纯逻辑 + 单点接入**——不导入 Textual 与 Provider SDK，可在无终端无网络的
测试进程中独立验证（spec N1）。

包内六模块严格单向依赖：
models → parser → discovery → render → validation → manager。
"""

from rhinecode.skills.models import (
    ActivationStatus,
    SkillCommandInfo,
    SkillMode,
    SkillSource,
    SkillSpec,
    builtin_skills_dir,
)
from rhinecode.skills.manager import SkillManager

__all__ = [
    "SkillManager",
    "SkillMode",
    "SkillSource",
    "SkillSpec",
    "SkillCommandInfo",
    "ActivationStatus",
    "builtin_skills_dir",
]
