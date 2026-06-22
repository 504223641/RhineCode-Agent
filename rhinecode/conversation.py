"""
对话管理模块。

ConversationManager 是 TUI 层与 Provider 层之间的协调者，职责包括：
- 维护多轮对话的完整消息历史（history）
- 解析并执行斜杠命令（/think、/clear、/exit）
- 管理思考模式的三档强度（off / high / max）
- 在流式回复完成后，将 AI 回复追加到历史记录

TUI 层调用 handle_input() 获取结果，结果类型决定 TUI 的后续行为：
- str：斜杠命令的反馈文本，直接作为系统提示显示
- Iterator[StreamChunk]：流式回复生成器，由 TUI 的 Worker 消费并逐块渲染
"""

from typing import Iterator

from rhinecode.provider.base import BaseProvider, Message, StreamChunk


class ConversationManager:
    """
    多轮对话管理器。

    持有对话历史和思考模式状态，是 TUI 与 Provider 之间的唯一协调点。
    TUI 层不直接调用 Provider，所有请求均通过此类中转。
    """

    # /think 命令的三态循环顺序：关闭 → 高效 → 最强 → 关闭
    _EFFORT_CYCLE = {"off": "high", "high": "max", "max": "off"}
    # 各档位对应的中文显示名称
    _EFFORT_LABEL = {"off": "关闭", "high": "高效（high）", "max": "最强（max）"}

    def __init__(self, provider: BaseProvider, provider_protocol: str):
        """
        初始化对话管理器。

        :param provider: 已实例化的 Provider，负责实际的 API 调用
        :param provider_protocol: Provider 的协议名（如 "anthropic"），
                                  用于判断是否支持思考模式
        """
        self._provider = provider
        self._protocol = provider_protocol
        self.history: list[Message] = []
        # 思考模式强度：off（关闭）/ high（高效）/ max（最强）
        self.thinking_effort: str = "off"

    def clear(self) -> None:
        """
        清空对话历史。

        副作用：self.history 被重置为空列表，下次请求将不携带任何上下文。
        """
        self.history = []

    def handle_input(self, text: str) -> "str | Iterator[StreamChunk]":
        """
        处理用户输入，根据内容类型分发到不同处理路径。

        处理规则：
        - "/exit"  → 抛出 SystemExit，由 TUI 层捕获后调用 app.exit()
        - "/clear" → 清空 history，返回确认文本
        - "/think" → 三态循环切换 thinking_effort（off→high→max→off）；
                     OpenAI 原生协议不支持，返回提示文本
        - 其他     → 追加用户消息到 history，调用 Provider 流式接口，返回生成器

        :param text: 用户原始输入（含前后空白）
        :returns: str（斜杠命令反馈）或 Iterator[StreamChunk]（流式回复）
        :raises SystemExit: 用户输入 "/exit" 时抛出

        副作用：
        - "/clear" 会清空 self.history
        - "/think" 会修改 self.thinking_effort
        - 普通消息会向 self.history 追加用户消息（AI 回复在流结束后由 _stream 追加）
        """
        text = text.strip()

        if text == "/exit":
            raise SystemExit

        if text == "/clear":
            self.clear()
            return "对话历史已清空"

        if text == "/think":
            # Anthropic 和 DeepSeek 均支持思考模式，OpenAI 原生协议不支持
            if self._protocol not in ("anthropic", "deepseek"):
                return "当前 Provider 不支持思考模式"
            # 循环切换到下一档位
            self.thinking_effort = self._EFFORT_CYCLE[self.thinking_effort]
            label = self._EFFORT_LABEL[self.thinking_effort]
            return f"思考模式：{label}"

        # 普通消息：先追加到历史，再发起流式请求
        # 注意：此时传给 Provider 的是追加用户消息后的完整历史快照
        msg = Message(role="user", content=text)
        self.history.append(msg)
        return self._stream(list(self.history))

    def _stream(self, messages: list[Message]) -> Iterator[StreamChunk]:
        """
        调用 Provider 发起流式请求，同时收集完整回复以追加到历史。

        此方法是一个生成器函数：它在将每个 chunk yield 给 TUI 层的同时，
        在内部积累 type="text" 的内容，待流结束后将完整回复作为
        assistant Message 追加到 self.history，保证下一轮对话携带正确的上下文。

        :param messages: 本轮请求携带的完整历史快照（包含刚追加的用户消息）
        :returns: 透传 Provider 产出的每个 StreamChunk

        副作用：流结束后向 self.history 追加 assistant 消息。
        """
        full_response: list[str] = []

        for chunk in self._provider.stream_chat(messages, self.thinking_effort):
            if chunk.type == "text":
                # 积累文本块，用于流结束后构造完整的 assistant 消息
                full_response.append(chunk.content)
            yield chunk

        # 仅在有实际文本内容时才追加（避免纯 error 场景产生空的 assistant 消息）
        if full_response:
            self.history.append(Message(role="assistant", content="".join(full_response)))
