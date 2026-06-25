"""
Anthropic Claude Provider 实现。

使用官方 anthropic Python SDK 发起流式对话请求。
相比 OpenAI 协议，有两处关键差异：
1. 支持 Extended Thinking：需在请求参数中注入 thinking 字段，且必须将 temperature 设为 1
2. 事件模型不同：SDK 以事件类型名称区分文本增量与思考增量，需逐类型映射到 StreamChunk

依赖：anthropic>=0.40.0（在 pyproject.toml 中声明）
"""

from typing import Iterator, Optional
import anthropic

from rhinecode.config import Config
from rhinecode.provider.base import BaseProvider, Message, StreamChunk


class AnthropicProvider(BaseProvider):
    """
    Anthropic Claude 的 Provider 实现，支持流式输出和 Extended Thinking。
    """

    def __init__(self, config: Config):
        """
        初始化 Anthropic 客户端。

        :param config: 包含 api_key、base_url、model 的配置对象
                       base_url 可指向官方地址或中转代理
        """
        self._client = anthropic.Anthropic(
            api_key=config.api_key,
            base_url=config.base_url,
        )
        self._model = config.model

    def stream_chat(
        self,
        messages: list[Message],
        thinking_effort: str = "off",
        tools: Optional[list[dict]] = None,
        system: Optional[str] = None,
    ) -> Iterator[StreamChunk]:
        """
        向 Anthropic API 发起流式对话请求，逐块产出 StreamChunk。

        执行步骤：
        1. 将内部 Message 列表转换为 SDK 要求的字典格式
        2. 构造请求参数；若启用 thinking，注入 thinking 字段并强制 temperature=1
           （Anthropic 规定：启用 Extended Thinking 时 temperature 必须为 1）
        3. 通过 SDK 的 stream() 上下文管理器建立 SSE 连接，迭代事件
        4. 将 RawContentBlockDeltaEvent 中的 TextDelta 和 ThinkingDelta 映射为 StreamChunk
        5. 流正常结束后产出 type="done" 的结束块
        6. 任何异常均捕获并以 type="error" 块返回，保证调用方不因网络错误崩溃

        :param messages: 完整对话历史（含本轮用户消息）
        :param thinking_effort: "off" 关闭，"high"/"max" 均映射为开启（Anthropic 无力度区分）
        :param tools: 工具描述列表；本章不为 Anthropic 实现工具调用，传入后直接忽略
                      （保留参数仅为与 BaseProvider 接口一致）
        :param system: 稳定系统提示（可缓存通道）；映射到 Anthropic SDK 的顶层 system 参数。
                       将来可在此把 system 改成带 cache_control 断点的结构化形式以显式缓存（c5 F6）。
        :returns: StreamChunk 迭代器

        副作用：发起 HTTPS 请求，消耗 Anthropic token 配额。
        """
        # Anthropic 的 messages 不接受 role="system"：上层循环为统一注入逻辑，会把动态提醒
        # （<system-reminder>）作为 role="system" 消息追加到 messages 末尾。这里把所有 system
        # 消息从 messages 中剥离，与传入的 system 参数一起合并到顶层 system，剩余消息照常下发。
        system_parts: list[str] = [system] if system else []
        sdk_messages: list[dict] = []
        for m in messages:
            if m.role == "system":
                if m.content:
                    system_parts.append(m.content)
                continue
            sdk_messages.append({"role": m.role, "content": m.content})

        params: dict = {
            "model": self._model,
            "max_tokens": 16000,
            "messages": sdk_messages,
        }
        # 合并后的稳定系统提示 + 动态提醒，作为顶层 system 下发（空则不传）。
        if system_parts:
            params["system"] = "\n\n".join(system_parts)

        # Anthropic 无 high/max 区分，只要不是 "off" 就开启 Extended Thinking
        if thinking_effort != "off":
            # budget_tokens 控制思考过程最多消耗的 token 数量
            # temperature 必须设为 1，这是 Anthropic Extended Thinking 的硬性要求
            params["thinking"] = {"type": "enabled", "budget_tokens": 10000}
            params["temperature"] = 1

        try:
            with self._client.messages.stream(**params) as stream:
                for event in stream:
                    event_type = type(event).__name__

                    # 只处理内容增量事件，其他生命周期事件（start/stop）忽略
                    if event_type == "RawContentBlockDeltaEvent":
                        delta = event.delta
                        delta_type = type(delta).__name__

                        if delta_type == "TextDelta":
                            yield StreamChunk(type="text", content=delta.text)
                        elif delta_type == "ThinkingDelta":
                            # thinking 内容由 Anthropic 在正文前单独产出
                            yield StreamChunk(type="thinking", content=delta.thinking)

                # 流正常结束，通知调用方可以执行收尾操作（如将回复追加到 history）
                yield StreamChunk(type="done", content="")

        except Exception as e:
            # 网络超时、鉴权失败、模型不可用等异常均在此捕获
            # 以 error 块返回而非抛出，确保 TUI 层可以显示错误而不崩溃
            yield StreamChunk(type="error", content=str(e))
