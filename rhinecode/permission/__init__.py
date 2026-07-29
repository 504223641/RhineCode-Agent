"""
权限系统包（c6 五层防御）。

对外导出引擎、模式、决策结果等公共符号，使上层（agent.loop / conversation）只需
`from rhinecode.permission import PermissionEngine, PermissionMode` 即可接入，无需感知
内部分层（blacklist/matching/rules/config/adapter）。
"""

from rhinecode.permission.models import (
    Decision,
    DecisionResult,
    Layer,
    PermissionMode,
    PermissionRequest,
    Rule,
)
from rhinecode.permission.engine import PermissionEngine
from rhinecode.permission.adapter import to_allow_rule, to_request

__all__ = [
    "Decision",
    "DecisionResult",
    "Layer",
    "PermissionMode",
    "PermissionRequest",
    "Rule",
    "PermissionEngine",
    "to_allow_rule",
    "to_request",
]
