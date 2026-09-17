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

from rhinecode.provider.base import BaseProvider, Message, StreamChunk, ToolCall
from rhinecode.trace.models import (
    TraceEventType,
    full_text,
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

        # 走 emit_lazy 而不是 emit：这个负载要遍历整份消息历史并逐条渲染，
        # 是典型的「昂贵负载」，关闭记录时不应该白构造一遍（spec N1）。
        # 去掉条数上限之后它更贵了，emit_lazy 也因此更重要——不开 `--trace` 时
        # 这个 lambda 一次都不会被调用，产品运行的开销仍然是零。
        self._recorder.emit_lazy(
            TraceEventType.API_REQUEST,
            lambda: {
                "turn": turn,
                "model": self._model,
                "thinking_effort": thinking_effort,
                "system": full_text(system) if system else None,
                "tool_names": _tool_names(tools),
                # 完整 schema 也记：AC1 要固化「工具 schema 的黄金基线」，
                # 只记名字的话「某个参数描述被改坏了」这类回归查不出来。
                "tools": tools,
                "messages": _render_messages(messages),
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
                        tool_calls.append(_tool_call_payload(chunk.tool_call))
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
                    "text": full_text("".join(text_parts)),
                    "thinking": full_text("".join(thinking_parts)),
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


def _tool_call_payload(tc: ToolCall) -> dict:
    """
    把一次工具调用转成可落盘的形态。

    :param tc: Provider 拼接完成的工具调用
    :returns: 至少含 id / name / arguments 三个键的字典

    ## 解析失败时多写两个键，成功时一个都不多写

    `arguments` 解析成功时它就是内容的无损形态，不必再存一份原文。
    解析**失败**时 `arguments` 是 None，这时候若不把 `raw_arguments`
    一并写下来，那段内容在记录里就彻底消失了——事后只看得到
    `"arguments": null`，说不清模型写坏在哪。`arguments_error` 记的是失败原因
    （JSON 语法错误的位置，或「解析出来不是对象」），它是排查时的第一条线索。

    ⚠ 这两个键与 `tool_execute` 那边的同名键**必须保持同一口径**（都只在失败时
    出现、都不截断）——一次失败的调用会在记录里留下两条事件（本函数产出的
    `api_response` 与循环产出的 `tool_execute`），两边说法不一致会让读的人
    以为是两回事。

    副作用：无（纯函数）。
    """
    payload: dict = {
        "id": tc.id,
        "name": tc.name,
        "arguments": full_text(tc.arguments) if tc.arguments is not None else None,
    }
    raw = getattr(tc, "raw_arguments", None)
    if raw is not None:
        # 刻意走 full_text：它是「这里是一段可能很长、且绝不截断的正文」的语义标记
        payload["raw_arguments"] = full_text(raw)
    err = getattr(tc, "arguments_error", None)
    if err:
        payload["arguments_error"] = err
    return payload


def _render_messages(messages: list[Message]) -> list[dict]:
    """
    把消息历史转成可落盘的形态：逐条取角色、完整内容与工具调用。

    :param messages: 本次请求实际发给模型的全部消息
    :returns: 与入参**等长**的字典列表

    ## 为什么这里不再有条数上限

    旧实现在超过 400 条时只保留头部并记一个 `original_length`。它与 trace 的
    立项目的直接冲突：读记录的人问的是「模型这一轮到底看到了什么」，
    而一个被砍掉尾部的历史**恰好丢掉了离当前最近、最可能解释当前行为的那一段**。
    （旧注释给的理由是「头部是 c8 压缩后的摘要，最该看」——那只在排查压缩本身时成立，
    排查其它任何问题时都反了。）

    现在**逐条全记、内容不截断**。代价是长会话下单行 JSON 很大——`api_request`
    每轮都携带完整历史，一次几百轮的会话可以产出数十 MB 的记录文件。
    这是刻意接受的：JSONL 是逐行独立的，阅读器按行处理不必整份读进内存，
    而「证据不完整」的代价没有上限。

    副作用：无（纯函数）。
    """
    rendered = []
    for m in messages:
        entry: dict = {
            "role": getattr(m, "role", None),
            "content": full_text(getattr(m, "content", "") or ""),
        }
        calls = getattr(m, "tool_calls", None)
        if calls:
            entry["tool_calls"] = [
                {"id": c.id, "name": c.name, "arguments": full_text(c.arguments)}
                for c in calls
            ]
        tcid = getattr(m, "tool_call_id", None)
        if tcid:
            entry["tool_call_id"] = tcid
        rendered.append(entry)
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
