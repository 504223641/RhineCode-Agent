"""
DeepSeek Provider 实现。

DeepSeek 的 API 与 OpenAI 基本兼容，但新版（deepseek-v4-flash / deepseek-v4-pro）
原生支持 Thinking Mode，通过 extra_body 参数传递 {"thinking": {"type": "enabled"}}
来开启，流式响应中会额外返回 delta.reasoning_content 字段（思维链内容）。

与 OpenAIProvider 的主要差异：
- thinking_effort != "off" 时通过 extra_body 开启思考模式
- 流式循环中需额外读取 delta.reasoning_content → StreamChunk(type="thinking")
- 不支持 temperature、top_p 等采样参数（思考模式下）

使用方式（config.yaml）：
    protocol: deepseek
    model: deepseek-v4-flash   # 或 deepseek-v4-pro
    base_url: https://api.deepseek.com/v1
    api_key: <your_deepseek_api_key>
"""

from typing import Iterator
import openai

from rhinecode.config import Config
from rhinecode.provider.base import BaseProvider, Message, StreamChunk


class DeepSeekProvider(BaseProvider):
    """
    DeepSeek Provider，支持普通对话和 Thinking Mode。

    不再继承 OpenAIProvider，而是独立实现 stream_chat，
    以便在 thinking_effort != "off" 时正确处理 extra_body 和 reasoning_content 字段。
    """

    def __init__(self, config: Config):
        """
        初始化 DeepSeek 客户端。

        使用 openai.OpenAI 客户端并指向 DeepSeek 的 base_url，
        DeepSeek API 与 OpenAI 协议兼容，可直接复用 SDK。

        :param config: 包含 api_key、base_url、model 的配置对象
        """
        self._client = openai.OpenAI(
            api_key=config.api_key,
            base_url=config.base_url,
        )
        self._model = config.model

    def stream_chat(
        self,
        messages: list[Message],
        thinking_effort: str = "off",
    ) -> Iterator[StreamChunk]:
        """
        向 DeepSeek API 发起流式对话请求，支持 Thinking Mode。

        执行步骤：
        1. 将内部 Message 列表转换为 SDK 字典格式
        2. thinking_effort != "off" 时通过 extra_body 开启思考模式，并将 effort 直接透传
        3. 流式迭代每个 chunk：
           - delta.reasoning_content 非空 → StreamChunk(type="thinking")
           - delta.content 非空           → StreamChunk(type="text")
        4. 流结束后产出 type="done"
        5. 任何异常均捕获并以 type="error" 返回

        :param messages: 完整对话历史（含本轮用户消息）
        :param thinking_effort: "off" 关闭，"high" 高效，"max" 最强，直接映射到 reasoning_effort
        :returns: StreamChunk 迭代器

        副作用：发起 HTTPS 请求，消耗 DeepSeek token 配额。
        """
        sdk_messages = [{"role": m.role, "content": m.content} for m in messages]

        # DeepSeek 新版模型默认开启思考，必须显式传 "disabled" 才能关闭
        # 不传 extra_body 或传 None 等效于使用模型默认值（思考开启）
        if thinking_effort == "off":
            extra_body = {"thinking": {"type": "disabled"}}
        else:
            extra_body = {"thinking": {"type": "enabled"}, "reasoning_effort": thinking_effort}

        try:
            stream = self._client.chat.completions.create(
                model=self._model,
                messages=sdk_messages,
                stream=True,
                extra_body=extra_body,
            )

            for chunk in stream:
                delta = chunk.choices[0].delta if chunk.choices else None
                if not delta:
                    continue

                # 思维链内容（Thinking Mode 专属字段）
                reasoning = getattr(delta, "reasoning_content", None)
                if reasoning:
                    yield StreamChunk(type="thinking", content=reasoning)

                # 正文回复内容
                if delta.content:
                    yield StreamChunk(type="text", content=delta.content)

            yield StreamChunk(type="done", content="")

        except Exception as e:
            yield StreamChunk(type="error", content=str(e))
