"""
命令层公共接口（c10 T23）。

只导出稳定公共 API；不在导入时创建全局注册表或执行任何注册副作用——
注册表由启动入口显式调用 build_builtin_registry() 构建（plan 12.6）。
"""

from rhinecode.commands.models import (
    CommandController,
    CommandHandler,
    CommandInvocation,
    CommandSpec,
    CommandType,
    CompletionItem,
    DispatchKind,
    DispatchResult,
    InputKind,
    ModeTarget,
    ParsedInput,
    ReportTarget,
)
from rhinecode.commands.parser import parse_input
from rhinecode.commands.registry import CommandRegistrationError, CommandRegistry
from rhinecode.commands.dispatcher import CommandDispatcher
from rhinecode.commands.builtins import build_builtin_registry, INIT_PROMPT

__all__ = [
    "CommandController",
    "CommandHandler",
    "CommandInvocation",
    "CommandSpec",
    "CommandType",
    "CompletionItem",
    "DispatchKind",
    "DispatchResult",
    "InputKind",
    "ModeTarget",
    "ParsedInput",
    "ReportTarget",
    "parse_input",
    "CommandRegistrationError",
    "CommandRegistry",
    "CommandDispatcher",
    "build_builtin_registry",
    "INIT_PROMPT",
]
