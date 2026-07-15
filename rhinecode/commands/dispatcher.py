"""
命令分发器（c10 T11–T12）：组合解析器、注册表与控制器，做输入入口的统一分流。

职责（plan 12.4）：
- 普通消息与命令的入口分流（spec F9）：命令绝不作为普通消息进入 Agent；
- 回显恰好一次（plan 8）：无论普通消息、命令、别名还是未知命令，
  聊天区的用户输入回显都由本类统一负责，App / Handler / Manager 不再重复显示；
- 统一未知命令（spec F8）、缺少必需参数（spec F14）与处理异常（spec N5）的本地反馈；
- 不持有 Textual widget——只面向 CommandController 协议。
"""

import logging

from rhinecode.commands.models import (
    CommandController,
    CommandInvocation,
    DispatchKind,
    DispatchResult,
    InputKind,
)
from rhinecode.commands.parser import parse_input
from rhinecode.commands.registry import CommandRegistry

# 调试日志：命令处理异常时记录上下文（用户界面不输出堆栈，plan 8.6）。
_logger = logging.getLogger(__name__)


class CommandDispatcher:
    """
    输入分流器。持有一份 CommandRegistry（与命令面板、输入高亮共用同一实例），
    dispatch 是用户提交输入的唯一入口。
    """

    def __init__(self, registry: CommandRegistry) -> None:
        """:param registry: 已完成内置命令注册的注册表（启动早期构建，plan 3）。"""
        self._registry = registry

    @property
    def registry(self) -> CommandRegistry:
        """暴露注册表引用，供 TUI 组件（命令面板/高亮器）共享同一事实来源。"""
        return self._registry

    def dispatch(self, text: str, controller: CommandController) -> DispatchResult:
        """
        分发一次用户输入（spec F9）。

        执行流程（plan 8）：
        1. 空输入 → EMPTY，不调用任何控制器方法（spec F4）；
        2. 普通消息 → 依次调用一次 show_user_input 与一次 send_user_message；
        3. 斜杠输入 → 注册表按 casefold 解析：
           - 未命中：回显一次原文 + 本地「未知命令 + /help 引导」，绝不发给 Agent（spec F8）；
           - 命中：回显一次原文；requires_argument 且参数为空时显示用法并返回 ERROR
             （spec F14）；否则调用处理函数。
        4. 处理函数异常 → 记录调试上下文、显示简洁本地错误、返回 ERROR；
           不降级为普通消息，也不重复回显（spec N5）。

        :param text: 用户原始输入
        :param controller: 界面控制器（生产为 RhineApp，测试为 Fake）
        :returns: 轻量分发结果（供测试/日志判断，不承载渲染对象）
        """
        parsed = parse_input(text)

        if parsed.kind == InputKind.EMPTY:
            return DispatchResult(kind=DispatchKind.EMPTY)

        if parsed.kind == InputKind.MESSAGE:
            # 普通消息：回显一次原文，然后进入现有对话路径（存档 → Agent → Worker）。
            content = parsed.raw_text.strip()
            controller.show_user_input(content)
            controller.send_user_message(content)
            return DispatchResult(kind=DispatchKind.MESSAGE)

        # 斜杠输入：先回显一次用户实际输入（命中与未知都回显，plan 10.1）。
        typed = parsed.raw_text.strip()
        controller.show_user_input(typed)

        entry = self._registry.resolve_matched(parsed.command_token or "")
        if entry is None:
            message = f"未知命令：{parsed.command_token}。输入 /help 查看可用命令。"
            controller.show_message(message)
            return DispatchResult(kind=DispatchKind.UNKNOWN, message=message)

        spec, matched_name = entry
        invocation = CommandInvocation(
            raw_text=parsed.raw_text,
            typed_name=parsed.command_token or "",
            matched_name=matched_name,
            arguments=parsed.arguments,
            spec=spec,
        )

        # 必需参数校验（spec F14）：缺参时显示自身用法与参数提示，不调用处理函数。
        if spec.requires_argument and not invocation.arguments:
            hint = f"（参数：{spec.argument_hint}）" if spec.argument_hint else ""
            message = f"{spec.name} 需要参数。用法：{spec.usage}{hint}"
            controller.show_message(message)
            return DispatchResult(
                kind=DispatchKind.ERROR, command_name=spec.name, message=message
            )

        try:
            spec.handler(invocation, controller)
        except Exception as exc:  # noqa: BLE001 —— 错误隔离边界（spec N5）
            # 命令失败必须可恢复：显示简洁本地错误（不输出堆栈），
            # 绝不把失败命令降级为普通消息重发给模型。
            _logger.debug("command %s failed", spec.name, exc_info=True)
            message = f"命令 {spec.name} 执行失败：{exc}"
            controller.show_message(message)
            return DispatchResult(
                kind=DispatchKind.ERROR, command_name=spec.name, message=message
            )

        return DispatchResult(kind=DispatchKind.COMMAND, command_name=spec.name)
