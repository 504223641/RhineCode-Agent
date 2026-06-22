"""
DeepSeek Provider 实现。

DeepSeek 的 API 与 OpenAI 完全兼容（相同的 endpoint 格式、相同的 chat completions 接口），
因此直接继承 OpenAIProvider，复用其流式处理逻辑，无需重复实现。

主要差异：
- base_url 默认指向 https://api.deepseek.com/v1
- 常用模型：deepseek-chat（通用对话）、deepseek-reasoner（推理增强）
- thinking 参数对 DeepSeek 无效，与 OpenAI 一致会被忽略

使用方式（config.yaml）：
    protocol: deepseek
    model: deepseek-chat
    base_url: https://api.deepseek.com/v1
    api_key: <your_deepseek_api_key>
"""

from rhinecode.config import Config
from rhinecode.provider.openai import OpenAIProvider


class DeepSeekProvider(OpenAIProvider):
    """
    DeepSeek Provider。

    DeepSeek 采用与 OpenAI 相同的 API 协议，因此继承 OpenAIProvider
    即可获得完整的流式对话能力，无需额外实现。

    后续若 DeepSeek 推出专有功能（如原生 reasoning 流式 token），
    可在此类中重写 stream_chat 方法来处理差异。
    """

    def __init__(self, config: Config):
        """
        初始化 DeepSeek Provider。

        直接调用父类 OpenAIProvider 的构造函数，传入配置中的
        api_key 和 base_url，由父类初始化 openai.OpenAI 客户端。

        :param config: 包含 api_key、base_url、model 的配置对象
        """
        super().__init__(config)
