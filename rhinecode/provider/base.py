"""
Provider 抽象层，定义所有 LLM 后端必须遵守的统一接口。

设计原则：
- ConversationManager 和 TUI 层只依赖此模块，不感知具体 Provider 实现
- 新增 Provider 时只需继承 BaseProvider 并实现 stream_chat，无需改动上层代码
- StreamChunk 采用 type 字段区分内容类型，便于 TUI 层做差异化渲染
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Iterator


@dataclass
class Message:
    """
    单条对话消息。

    :param role: 发送方角色，固定取值为 "user"（用户）或 "assistant"（AI）
    :param content: 消息文本内容
    """
    role: str
    content: str


@dataclass
class StreamChunk:
    """
    流式响应中的单个数据块。

    TUI 层根据 type 字段决定如何渲染：
    - "text"：AI 正文输出，追加到当前回复区域
    - "thinking"：Claude Extended Thinking 的思考过程，以灰色斜体显示
    - "done"：流正常结束信号，content 为空字符串
    - "error"：发生异常，content 为可读错误描述，以红色显示

    :param type: 块类型，取值见上方说明
    :param content: 块内容，"done" 时为空字符串
    """
    type: str
    content: str


class BaseProvider(ABC):
    """
    LLM Provider 抽象基类。

    所有具体 Provider（Anthropic、OpenAI、DeepSeek 等）均须继承此类
    并实现 stream_chat 方法。上层代码通过此接口与 Provider 交互，
    实现对具体实现的解耦。
    """

    @abstractmethod
    def stream_chat(
        self,
        messages: list[Message],
        thinking: bool = False,
    ) -> Iterator[StreamChunk]:
        """
        以流式方式发起对话请求，逐块产出响应内容。

        :param messages: 完整对话历史，含本轮用户消息，按时间顺序排列
        :param thinking: 是否启用 Extended Thinking（仅 Anthropic 生效，其他 Provider 忽略）
        :returns: StreamChunk 迭代器，调用方逐块消费，最后一块 type 为 "done" 或 "error"

        副作用：向远端 API 发起网络请求，消耗 token 配额。
        """
        ...
