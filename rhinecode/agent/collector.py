"""
双路流式收集器（spec F4）。

「双路」指：每轮模型的流式响应同时走两条路——
1. 实时展示路：把可展示的块（文本、思考、用量）翻译成 AgentEvent 立即返回给循环转发给 TUI，
   保证用户看到逐字输出，不必等整轮结束。
2. 内部累积路：把整轮响应攒成完整形态（完整正文 text + 完整工具调用列表 tool_calls + 用量 usage），
   供循环在本轮结束后判断「模型是否要调工具 / 是否继续下一轮 / 如何回灌历史」。

用法（在 Agent.run 的每一轮内）：
    collector = StreamCollector()
    for chunk in provider.stream_chat(...):
        ev = collector.feed(chunk)
        if ev is not None:
            yield ev               # 实时展示路
    # 本轮结束后读取累积结果（内部累积路）
    text = collector.text
    tool_calls = collector.tool_calls
    usage = collector.usage
"""

from typing import Optional

from rhinecode.provider.base import StreamChunk, ToolCall
from rhinecode.agent.events import AgentEvent, AgentEventType, Usage


class StreamCollector:
    """
    逐块消费 provider 的 StreamChunk，一边产出可展示的 AgentEvent，一边累积完整响应。

    状态（累积路结果）：
    - text：本轮模型正文的完整拼接（不含思考内容）
    - tool_calls：本轮模型发起的全部工具调用（顺序与到达顺序一致）
    - usage：本轮 token 用量（无则为 None）
    """

    def __init__(self) -> None:
        self.text: str = ""
        self.tool_calls: list[ToolCall] = []
        self.usage: Optional[Usage] = None

    def feed(self, chunk: StreamChunk) -> Optional[AgentEvent]:
        """
        喂入一个流式块：更新内部累积，并按需返回一个用于实时展示的 AgentEvent。

        映射规则：
        - "text"：累积进 self.text，返回 AgentEvent(TEXT)（供逐字渲染）
        - "thinking"：不累积进 text（思考与正文分离），返回 AgentEvent(THINKING)
        - "tool_call"：累积进 self.tool_calls，不产生展示事件（完整调用的呈现由 TOOL_START 负责），返回 None
        - "tool_pending"：**不累积**，只返回 AgentEvent(TOOL_PENDING) 让界面立刻显示
          「这一步开始了」。它是纯展示锚点，参数生成期（可能几十秒）唯一的活体信号
        - "usage"：把原生 usage 转成 Usage 存入 self.usage，返回 AgentEvent(USAGE)
        - 其他（done / error）：交由循环处理，返回 None

        :param chunk: provider 产出的单个流式块
        :returns: 需要实时展示时返回对应 AgentEvent，否则 None

        副作用：更新 self.text / self.tool_calls / self.usage。
        """
        if chunk.type == "text":
            self.text += chunk.content
            return AgentEvent(type=AgentEventType.TEXT, text=chunk.content)

        if chunk.type == "thinking":
            return AgentEvent(type=AgentEventType.THINKING, text=chunk.content)

        if chunk.type == "tool_call":
            if chunk.tool_call is not None:
                self.tool_calls.append(chunk.tool_call)
            return None

        if chunk.type == "tool_pending":
            # **绝不累积进 self.tool_calls**：这只是「模型开始吐这个调用」的播报，
            # arguments 恒为 None、参数还在流里。累积进去会让循环把它当成一次真实
            # 调用去执行，同一个调用因此执行两遍（第二遍参数为 None → 结构化错误）。
            if chunk.tool_call is None:
                return None
            return AgentEvent(type=AgentEventType.TOOL_PENDING, tool_call=chunk.tool_call)

        if chunk.type == "usage":
            self.usage = self._to_usage(chunk.usage)
            return AgentEvent(type=AgentEventType.USAGE, usage=self.usage)

        # done / error 等：不在收集器层处理，返回 None 由循环决定后续
        return None

    @staticmethod
    def _to_usage(raw) -> Usage:
        """
        把 provider SDK 的原生 usage 对象（或字典）转成内部 Usage。

        兼容两种形态：SDK 对象（属性访问）与字典（键访问），缺失字段一律按 0 处理，
        保证即便协议字段变动也不崩溃。

        :param raw: SDK usage 对象或字典
        :returns: 内部 Usage
        """
        def pick(name: str) -> int:
            if raw is None:
                return 0
            if isinstance(raw, dict):
                return int(raw.get(name) or 0)
            return int(getattr(raw, name, 0) or 0)

        return Usage(
            prompt_tokens=pick("prompt_tokens"),
            completion_tokens=pick("completion_tokens"),
            total_tokens=pick("total_tokens"),
            # DeepSeek 在 usage 中额外返回缓存命中/未命中字段；其他协议没有这两个键，
            # pick 会按 0 返回，因此无需为 Provider 做特判（c5 F10 的上游采集）。
            prompt_cache_hit_tokens=pick("prompt_cache_hit_tokens"),
            prompt_cache_miss_tokens=pick("prompt_cache_miss_tokens"),
        )
