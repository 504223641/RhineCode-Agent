"""
DeepSeek Provider 实现。

DeepSeek 的 API 与 OpenAI 基本兼容，但新版（deepseek-v4-flash / deepseek-v4-pro）
原生支持 Thinking Mode，通过 extra_body 参数传递 {"thinking": {"type": "enabled"}}
来开启，流式响应中会额外返回 delta.reasoning_content 字段（思维链内容）。

本章新增：工具调用（Function Calling）支持。
- stream_chat 接收 tools（OpenAI function 格式），非空时随请求发送以启用工具调用
- 历史中可能包含 assistant(tool_calls) 与 role="tool" 的工具结果消息，需正确转换为 SDK 格式
- 流式响应中工具调用以分片到达（delta.tool_calls，按 index 拼接 id/name/arguments 碎片），
  流结束后解析每个调用的 JSON arguments，产出 type="tool_call" 的 StreamChunk

与 OpenAIProvider 的主要差异：
- thinking_effort != "off" 时通过 extra_body 开启思考模式
- 流式循环中需额外读取 delta.reasoning_content → StreamChunk(type="thinking")
- 实现工具调用（OpenAI 实现本章不支持）

使用方式（config.yaml）：
    protocol: deepseek
    model: deepseek-v4-flash   # 或 deepseek-v4-pro
    base_url: https://api.deepseek.com/v1
    api_key: <your_deepseek_api_key>
"""

import json
from typing import Iterator, Optional
import openai

from rhinecode.config import Config
from rhinecode.provider.base import BaseProvider, Message, StreamChunk, ToolCall


class DeepSeekProvider(BaseProvider):
    """
    DeepSeek Provider，支持普通对话、Thinking Mode 与工具调用。

    独立实现 stream_chat（不继承 OpenAIProvider），以便正确处理 extra_body、
    reasoning_content 字段以及工具调用相关的消息转换与流式解析。
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

    def _to_sdk_messages(self, messages: list[Message]) -> list[dict]:
        """
        把内部 Message 列表转换为 DeepSeek/OpenAI SDK 接受的字典格式。

        处理三类消息：
        - role="tool"：工具执行结果 → {"role":"tool","tool_call_id":..., "content":...}
        - role="assistant" 且带 tool_calls：模型发起的工具调用 →
          {"role":"assistant","content":..., "tool_calls":[{id,type,function:{name,arguments}}]}
          其中 arguments 必须是 JSON 字符串（与 API 要求一致），故对 dict 做 json.dumps
        - 其余（user / 普通 assistant）：{"role":..., "content":...}

        :param messages: 内部对话历史
        :returns: SDK 可直接发送的消息字典列表

        副作用：无（纯转换）。
        """
        sdk_messages: list[dict] = []
        for m in messages:
            if m.role == "tool":
                # 工具结果消息，必须带上对应的 tool_call_id 以与调用配对
                sdk_messages.append({
                    "role": "tool",
                    "tool_call_id": m.tool_call_id,
                    "content": m.content,
                })
            elif m.role == "assistant" and m.tool_calls:
                # 模型发起工具调用的 assistant 消息；arguments 序列化回 JSON 字符串
                sdk_messages.append({
                    "role": "assistant",
                    "content": m.content or "",
                    "tool_calls": [
                        {
                            "id": tc.id,
                            "type": "function",
                            "function": {
                                "name": tc.name,
                                "arguments": json.dumps(tc.arguments or {}, ensure_ascii=False),
                            },
                        }
                        for tc in m.tool_calls
                    ],
                })
            else:
                sdk_messages.append({"role": m.role, "content": m.content})
        return sdk_messages

    def stream_chat(
        self,
        messages: list[Message],
        thinking_effort: str = "off",
        tools: Optional[list[dict]] = None,
        system: Optional[str] = None,
    ) -> Iterator[StreamChunk]:
        """
        向 DeepSeek API 发起流式对话请求，支持 Thinking Mode 与工具调用。

        执行步骤：
        1. 将内部 Message 列表转换为 SDK 字典格式（含工具消息）
        2. thinking_effort != "off" 时通过 extra_body 开启思考模式，并将 effort 透传
        3. tools 非空时随请求发送，启用工具调用
        4. 流式迭代每个 chunk：
           - delta.reasoning_content 非空 → StreamChunk(type="thinking")
           - delta.content 非空           → StreamChunk(type="text")
           - delta.tool_calls 非空         → 按 index 累积 id/name/arguments 碎片
        5. 流结束后：对每个累积的工具调用解析 JSON arguments（失败→None），
           产出 StreamChunk(type="tool_call")；再产出 type="done"
        6. 任何异常均捕获并以 type="error" 返回

        :param messages: 完整对话历史（含本轮用户消息、可能的工具消息）
        :param thinking_effort: "off" 关闭，"high"/"max" 直接映射到 reasoning_effort
        :param tools: 工具描述列表（OpenAI function 格式），非空时启用工具调用
        :param system: 稳定系统提示（可缓存通道）；非空时作为 SDK 消息序列的首条 system 消息。
                       因其逐轮逐字节一致，会落在请求前缀，命中 DeepSeek 的自动前缀缓存（c5 F5）。
        :returns: StreamChunk 迭代器

        副作用：发起 HTTPS 请求，消耗 DeepSeek token 配额。
        """
        sdk_messages = self._to_sdk_messages(messages)

        # 稳定系统提示置于消息序列最前：DeepSeek 按请求前缀自动缓存，前缀不变即命中缓存，
        # 省去重复计费与计算。动态内容（环境信息/提醒）由上层以 <system-reminder> 放在历史末尾，
        # 不在这条 system 之内，因此不会破坏该前缀的稳定性。
        if system:
            sdk_messages = [{"role": "system", "content": system}] + sdk_messages

        # DeepSeek 新版模型默认开启思考，必须显式传 "disabled" 才能关闭
        if thinking_effort == "off":
            extra_body = {"thinking": {"type": "disabled"}}
        else:
            extra_body = {"thinking": {"type": "enabled"}, "reasoning_effort": thinking_effort}

        # 仅在有工具时附加 tools 参数，保持纯对话请求不变。
        # stream_options.include_usage：OpenAI 兼容协议下开启后，流式响应会在最后额外
        # 多发一块「usage 块」（该块 choices 为空、仅含 usage 统计），用于汇报 token 用量。
        create_kwargs: dict = {
            "model": self._model,
            "messages": sdk_messages,
            "stream": True,
            "extra_body": extra_body,
            "stream_options": {"include_usage": True},
        }
        if tools:
            create_kwargs["tools"] = tools

        try:
            stream = self._client.chat.completions.create(**create_kwargs)

            # 工具调用按 index 累积：index → {"id", "name", "arguments"(字符串拼接)}
            # 流式中首片携带 id 与 function.name，后续片仅追加 function.arguments 碎片
            tool_buffers: dict[int, dict] = {}

            for chunk in stream:
                # usage 块通常在流末尾、choices 为空，必须在「无 delta 就 continue」之前处理，
                # 否则会被下面的 continue 吞掉。每块都尝试读取，最后一块才会真正带 usage。
                usage = getattr(chunk, "usage", None)
                if usage:
                    yield StreamChunk(type="usage", usage=usage)

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

                # 工具调用分片：累积到 tool_buffers，待流结束后统一解析
                delta_tool_calls = getattr(delta, "tool_calls", None)
                if delta_tool_calls:
                    for tc in delta_tool_calls:
                        idx = tc.index
                        buf = tool_buffers.setdefault(idx, {"id": "", "name": "", "arguments": ""})
                        if tc.id:
                            buf["id"] = tc.id
                        if tc.function:
                            if tc.function.name:
                                buf["name"] = tc.function.name
                            if tc.function.arguments:
                                # arguments 以字符串碎片到达，直接拼接，最后再 json 解析
                                buf["arguments"] += tc.function.arguments

            # 流结束：把累积的工具调用解析并产出（按 index 顺序保持稳定）
            for idx in sorted(tool_buffers.keys()):
                buf = tool_buffers[idx]
                raw_args = buf["arguments"] or "{}"
                try:
                    parsed = json.loads(raw_args)
                except (json.JSONDecodeError, TypeError):
                    # 模型可能生成非法 JSON：标记 arguments=None，由协调层转结构化错误
                    parsed = None
                yield StreamChunk(
                    type="tool_call",
                    tool_call=ToolCall(id=buf["id"], name=buf["name"], arguments=parsed),
                )

            yield StreamChunk(type="done", content="")

        except Exception as e:
            yield StreamChunk(type="error", content=str(e))
