"""
Skill 系统包（c11）。

把可复用的 AI 操作封装成带元信息的独立 Markdown 文件，支持两阶段加载
（启动只注入名字与一句话说明，用时再由 `load_skill` 工具加载完整 SOP）
与两种执行模式（共享当前对话 / 开独立子对话跑完回流结论）。

设计范式与 `permission/` `context/` `memory/` `commands/` 一致：
**纯逻辑 + 单点接入**——不导入 Textual 与 Provider SDK，可在无终端无网络的
测试进程中独立验证（spec N1）。

包内七模块严格单向依赖：

    models → parser → discovery → ┬→ render ─────┬→ audit → manager
                                  └→ validation ─┘

⚠️ `render` 与 `validation` 是**同层并列**，彼此不 import（前者只依赖 `models`，
后者只依赖 `permission` + `models`）。这一点是「`audit` 同时依赖两者
**不会成环**」的判断依据，不要把它们画成串联。
"""

from rhinecode.skills.models import (
    ActivationStatus,
    SkillCommandInfo,
    SkillSource,
    SkillSpec,
    builtin_skills_dir,
)
from rhinecode.skills.manager import SkillManager

# ⚠️ 这里的每一项都必须真能从本模块取到。曾经有一项 `SkillMode` 是
# 对齐改造的残留——那个枚举随 `mode: shared/isolated` 一起删除了
# （执行模式改由 `context: fork` 表达），但 `__all__` 忘了跟着改，
# 于是 `from rhinecode.skills import *` 会当场 AttributeError。
#
# 它一直没被发现，是因为**项目内没有任何地方用星号导入**——
# 这个列表实际上只在「有人第一次尝试星号导入」时才被求值。
__all__ = [
    "SkillManager",
    "SkillSource",
    "SkillSpec",
    "SkillCommandInfo",
    "ActivationStatus",
    "builtin_skills_dir",
]
