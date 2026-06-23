"""
Provider 工厂模块。

根据配置文件中的 protocol 字段，返回对应的 Provider 实例。
新增 Provider 时只需在此处添加一个 elif 分支，无需改动其他模块。

当前支持的 protocol：
- anthropic：Anthropic Claude，支持 Extended Thinking
- openai：OpenAI，流式对话，thinking 参数无效
- deepseek：DeepSeek，OpenAI 兼容协议，thinking 参数无效
"""

from rhinecode.config import Config
from rhinecode.provider.base import BaseProvider
from rhinecode.provider.anthropic import AnthropicProvider
from rhinecode.provider.openai import OpenAIProvider
from rhinecode.provider.deepseek import DeepSeekProvider


def create_provider(config: Config) -> BaseProvider:
    """
    根据 config.protocol 创建并返回对应的 Provider 实例。

    :param config: 包含 protocol、model、base_url、api_key 的配置对象
    :returns: 实现了 BaseProvider 接口的 Provider 实例
    :raises ValueError: protocol 不在支持列表中时抛出，错误信息包含实际值
    """
    if config.protocol == "anthropic":
        return AnthropicProvider(config)
    elif config.protocol == "openai":
        return OpenAIProvider(config)
    elif config.protocol == "deepseek":
        # DeepSeek 与 OpenAI API 完全兼容，通过 base_url 区分请求目标
        return DeepSeekProvider(config)
    else:
        raise ValueError(f"无效供应商 {config.protocol}，目前仅支持 anthropic / openai / deepseek")
