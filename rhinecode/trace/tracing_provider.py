"""
Provider 装饰器：在不改动任何 Provider 实现的前提下，记录每一次模型请求与响应。

**这是 spec N4 允许的唯一分层例外**——trace 包本应是叶子包（只依赖标准库），
但本模块必须继承 `provider.base.BaseProvider`。之所以可以接受，是因为
`provider/base.py` 是一个**零副作用的纯抽象模块**：里面只有一个 ABC 和三个
数据类，不 import 任何业务模块、不做任何 I/O。依赖它不会把 trace 拖进依赖环。

## 什么是装饰器模式（Decorator Pattern）

一句话：**做一个和原对象长得一模一样的壳，把调用转发给里面那个真对象，顺便在
转发前后做点自己的事。** 因为壳和真对象接口相同，所有调用方都察觉不到区别。

这里解决的问题是：我们想记录「每次发给模型的完整请求和收到的完整响应」，
但不想在 `deepseek.py` / `anthropic.py` / `openai.py` 里各加一遍埋点代码
（三处重复、将来新增 Provider 还会漏）。用装饰器，埋点只写一份，
装配层在构造完真 Provider 后套上这个壳即可，`stream_chat` 的签名一个字不改。
"""

from __future__ import annotations

import contextlib
import time
from typing import Any, Iterator, Optional

from rhinecode.provider.base import BaseProvider, Message, StreamChunk
from rhinecode.trace.models import (
    MAX_MESSAGE_ITEMS,
    TraceEventType,
    clip,
)
from rhinecode.trace.recorder import TraceRecorderProtocol


class TracingProvider(BaseProvider):
    """
    包住任意 Provider，为每次 `stream_chat` 产出一对 `api_request` / `api_response` 事件。

    :param inner: 被包装的真实 Provider。**存为公开属性**，供测试断言
                  （AC3 要求「关闭记录时链路上不得有中间层」）与调试时向内看
    :param recorder: 行为记录器
    :param model: 模型名。Provider 接口本身不暴露模型名，而 trace 需要它来
                  区分「主对话用的模型」与「Skill 指定的模型」，故由装配层传入

    ⚠️ **内层的 `stream_chat` 必须是生成器函数。** 下面用 `contextlib.closing`
    要求返回对象有 `close()` 方法，而生成器天生有。现有三个真实 Provider 与
    测试里全部的假 Provider 都是生成器函数，但将来若有人写成
    `return iter([...])`（普通迭代器没有 close），`closing` 会抛 AttributeError。
    """

    def __init__(
        self,
        inner: BaseProvider,
        recorder: TraceRecorderProtocol,
        model: str = "",
    ) -> None:
        self.inner = inner
        self._recorder = recorder
        self._model = model

    def stream_chat(
        self,
        messages: list[Message],
        thinking_effort: str = "off",
        tools: Optional[list[dict]] = None,
        system: Optional[str] = None,
    ) -> Iterator[StreamChunk]:
        """
        转发给内层 Provider，并在前后各记一条事件。

        :returns: 与内层**逐块相同**的 StreamChunk 迭代器（原样转发，不修改任何块）

        副作用：产出两条 trace 事件；实际的网络请求由内层发起。

        执行流程：
        1. 取当前作用域并为它分配一个轮次号（`api_request` 与 `api_response`
           靠 `scope` + `turn` 配对）。
        2. 记 `api_request`——完整的请求内容（消息历史、工具 schema、系统提示）。
        3. 逐块转发内层的输出，**旁路**累积正文/思考/工具调用/用量/错误。
        4. 无论正常结束还是被中途放弃，都在 `finally` 里记 `api_response`。
        """
        scope = self._recorder.current_scope()
        turn = self._recorder.next_turn(scope)
        t0 = time.monotonic()

        # 走 emit_lazy 而不是 emit：这个负载要遍历整份消息历史并逐条 clip，
        # 是典型的「昂贵负载」，关闭记录时不应该白构造一遍（spec N1）。
        self._recorder.emit_lazy(
            TraceEventType.API_REQUEST,
            lambda: {
                "turn": turn,
                "model": self._model,
                "thinking_effort": thinking_effort,
                "system": clip(system) if system else None,
                "tool_names": _tool_names(tools),
                # 完整 schema 也记：AC1 要固化「工具 schema 的黄金基线」，
                # 只记名字的话「某个参数描述被改坏了」这类回归查不出来。
                "tools": tools,
                "messages": _clip_messages(messages),
            },
        )

        # 旁路累积：这些变量只用于最后组装 api_response，不影响转发出去的内容
        text_parts: list[str] = []
        thinking_parts: list[str] = []
        tool_calls: list[dict] = []
        usage: Any = None
        stream_error: Optional[str] = None
        # 流式体验的两个可观测量。它们替代了「每个块记一条 agent_event」那种做法
        # ——那样会产出几百条只含 `text_length: 2` 的噪音记录，而且反而算不出
        # 首字延迟（要自己去减两条记录的时间戳）。这里聚合成两个数字：
        # - first_chunk_ms：从发起请求到**第一个正文/思考块**到达的毫秒数，
        #   即用户「等了多久才看到第一个字」，是流式体验最关键的指标；
        # - chunk 计数：块的密度，配合总耗时可看出是稳定出字还是卡顿后爆发。
        first_chunk_at: Optional[float] = None

        try:
            # 为什么必须显式 contextlib.closing：
            # 调用方（collector / loop）在收到 type="error" 的块时会 `break` 跳出
            # for 循环，生成器**不会被耗尽**。只靠 try/finally，在 CPython 上确实
            # 也能靠引用计数在 for 语句结束时立刻回收并结算生成器——但那是
            # 解释器实现细节（PyPy 等不保证即时回收）。显式 closing 把「何时结算」
            # 变成一条代码事实，并保证 `api_response` 仍然排在调用方随后产出的
            # ERROR / FINISHED 事件**之前**，时间线顺序才符合直觉。
            with contextlib.closing(
                self.inner.stream_chat(
                    messages,
                    thinking_effort=thinking_effort,
                    tools=tools,
                    system=system,
                )
            ) as stream:
                for chunk in stream:
                    if chunk.type == "text":
                        if first_chunk_at is None:
                            first_chunk_at = time.monotonic()
                        text_parts.append(chunk.content)
                    elif chunk.type == "thinking":
                        if first_chunk_at is None:
                            first_chunk_at = time.monotonic()
                        thinking_parts.append(chunk.content)
                    elif chunk.type == "tool_call" and chunk.tool_call is not None:
                        tool_calls.append(
                            {
                                "id": chunk.tool_call.id,
                                "name": chunk.tool_call.name,
                                "arguments": clip(chunk.tool_call.arguments)
                                if chunk.tool_call.arguments is not None
                                else None,
                            }
                        )
                    elif chunk.type == "usage":
                        usage = chunk.usage
                    elif chunk.type == "error":
                        stream_error = chunk.content
                    # 原样转发，一个字节都不改——这是「零侵入」的物理保证
                    yield chunk
        finally:
            self._recorder.emit_lazy(
                TraceEventType.API_RESPONSE,
                lambda: {
                    "turn": turn,
                    "model": self._model,
                    "text": clip("".join(text_parts)),
                    "thinking": clip("".join(thinking_parts)),
                    "tool_calls": tool_calls,
                    "usage": _usage_payload(usage),
                    "duration_ms": int((time.monotonic() - t0) * 1000),
                    # 流式聚合指标（替代逐块的 agent_event，见 tui/app.py 的说明）
                    "first_chunk_ms": (
                        int((first_chunk_at - t0) * 1000)
                        if first_chunk_at is not None
                        else None
                    ),
                    "text_chunks": len(text_parts),
                    "thinking_chunks": len(thinking_parts),
                    "stream_error": stream_error,
                },
            )


def _tool_names(tools: Optional[list[dict]]) -> list[str]:
    """
    从 OpenAI function 格式的 schema 列表里抽出工具名。

    schema 形如 `{"type": "function", "function": {"name": ..., ...}}`，
    因此名字在两层里。用 `.get` 逐层取值以容忍格式异常（埋点绝不能因为
    一个畸形 schema 就抛异常，那会连带把整次模型请求搞挂）。
    """
    if not tools:
        return []
    names = []
    for item in tools:
        if isinstance(item, dict):
            fn = item.get("function")
            if isinstance(fn, dict) and fn.get("name"):
                names.append(fn["name"])
    return names


def _clip_messages(messages: list[Message]) -> Any:
    """
    把消息历史转成可落盘的形态：逐条取角色与内容、内容经 `clip`、工具调用只留摘要。

    超过 `MAX_MESSAGE_ITEMS` 条时只保留**头部**并额外返回原条数。为什么保留头部
    而不是尾部：越靠前的消息越可能是被 c8 压缩改写过的摘要与边界提示，
    而「压缩到底把什么留下了」正是排查上下文问题时最要看的部分。
    """
    total = len(messages)
    items = messages[:MAX_MESSAGE_ITEMS]
    rendered = []
    for m in items:
        entry: dict = {
            "role": getattr(m, "role", None),
            "content": clip(getattr(m, "content", "") or ""),
        }
        calls = getattr(m, "tool_calls", None)
        if calls:
            entry["tool_calls"] = [
                {"id": c.id, "name": c.name, "arguments": clip(c.arguments)}
                for c in calls
            ]
        tcid = getattr(m, "tool_call_id", None)
        if tcid:
            entry["tool_call_id"] = tcid
        rendered.append(entry)

    if total > MAX_MESSAGE_ITEMS:
        return {
            "items": rendered,
            "truncated": True,
            "original_length": total,
        }
    return rendered


def _usage_payload(usage: Any) -> Any:
    """
    把 Provider 原生的 usage 对象转成可 JSON 化的字典。

    usage 的具体类型取决于 SDK（DeepSeek/OpenAI 返回自己的 pydantic 对象），
    故用 `getattr` 逐字段取；取不到任何字段时退回 `str()` 兜底，
    保证这个字段永远不会让 `json.dumps` 失败。
    """
    if usage is None:
        return None
    fields = (
        "prompt_tokens",
        "completion_tokens",
        "total_tokens",
        "prompt_cache_hit_tokens",
        "prompt_cache_miss_tokens",
    )
    snapshot = {f: getattr(usage, f, None) for f in fields}
    if all(v is None for v in snapshot.values()):
        return str(usage)
    return {k: v for k, v in snapshot.items() if v is not None}


__all__ = ["TracingProvider"]
