"""
OpenAI Provider 实现。

使用官方 openai Python SDK 发起流式对话请求。
通过 base_url 支持自定义接入点，兼容任何 OpenAI 兼容协议的第三方服务。

依赖：openai>=1.50.0（在 pyproject.toml 中声明）
"""

from typing import Iterator
import openai

from rhinecode.config import Config
from rhinecode.provider.base import BaseProvider, Message, StreamChunk


class OpenAIProvider(BaseProvider):
    """
    OpenAI 的 Provider 实现，支持流式输出。

    DeepSeekProvider 继承自此类，因为 DeepSeek API 与 OpenAI 协议完全兼容。
    若需要为某个兼容服务添加差异化处理，可继承此类并重写 stream_chat。
    """

    def __init__(self, config: Config):
        """
        初始化 OpenAI 客户端。

        :param config: 包含 api_key、base_url、model 的配置对象
                       base_url 可指向官方地址、Azure、或任何 OpenAI 兼容代理
        """
        self._client = openai.OpenAI(
            api_key=config.api_key,
            base_url=config.base_url,
        )
        self._model = config.model

    def stream_chat(
        self,
        messages: list[Message],
        thinking: bool = False,
    ) -> Iterator[StreamChunk]:
        """
        向 OpenAI API 发起流式对话请求，逐块产出 StreamChunk。

        执行步骤：
        1. 将内部 Message 列表转换为 OpenAI SDK 要求的字典格式
        2. 调用 chat.completions.create（stream=True）建立 SSE 连接
        3. 迭代每个 chunk，提取 delta.content 并产出 type="text" 的块
        4. 流结束后产出 type="done" 的结束块
        5. 任何异常均捕获并以 type="error" 块返回

        :param messages: 完整对话历史（含本轮用户消息）
        :param thinking: OpenAI 协议不支持此参数，传入后直接忽略
        :returns: StreamChunk 迭代器

        副作用：发起 HTTPS 请求，消耗 OpenAI token 配额。
        """
        # 将内部 Message 转换为 OpenAI SDK 接受的字典格式
        sdk_messages = [{"role": m.role, "content": m.content} for m in messages]

        try:
            stream = self._client.chat.completions.create(
                model=self._model,
                messages=sdk_messages,
                stream=True,
            )

            for chunk in stream:
                # choices 可能为空（心跳包），delta.content 可能为 None（流结束前的最后一帧）
                delta = chunk.choices[0].delta if chunk.choices else None
                if delta and delta.content:
                    yield StreamChunk(type="text", content=delta.content)

            yield StreamChunk(type="done", content="")

        except Exception as e:
            yield StreamChunk(type="error", content=str(e))
