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

它走的是 OpenAI 兼容协议（因此依赖 openai SDK），但在三处之上做了扩展：
- thinking_effort != "off" 时通过 extra_body 开启思考模式
- 流式循环中需额外读取 delta.reasoning_content → StreamChunk(type="thinking")
- 实现工具调用

⚠ 本项目曾另有一个走原生 OpenAI 协议的 `OpenAIProvider`，已于 2026-08-20
删除（它一直停留在纯对话能力）。**但 openai SDK 的依赖不能跟着删**——
本 Provider 用的就是它。

使用方式（config.yaml）：
    protocol: deepseek
    model: deepseek-v4-flash   # 或 deepseek-v4-pro
    base_url: https://api.deepseek.com/v1
    api_key: <your_deepseek_api_key>
"""

import json
import logging
import time
from typing import Iterator, Optional
import openai

from rhinecode.config import Config
from rhinecode.provider import errors
from rhinecode.provider.base import BaseProvider, Message, StreamChunk, ToolCall

# C5：Provider 是「它卡住了」这类报障最常见的落点（等首字节、限流重试、
# 连不上端点），因此它是日志设施的头号关键路径。
# ⚠ **只记形状与结果，绝不记内容**：不记消息正文、不记工具参数、不记 api_key。
# 要看原文请开 --trace（那份产物的敏感度与会话存档同级）。
_logger = logging.getLogger(__name__)


class DeepSeekProvider(BaseProvider):
    """
    DeepSeek Provider，支持普通对话、Thinking Mode 与工具调用。

    直接实现 stream_chat（不复用任何基类的默认实现），以便正确处理 extra_body、
    reasoning_content 字段以及工具调用相关的消息转换与流式解析。
    """

    def __init__(self, config: Config):
        """
        初始化 DeepSeek 客户端。

        使用 openai.OpenAI 客户端并指向 DeepSeek 的 base_url，
        DeepSeek API 与 OpenAI 协议兼容，可直接复用 SDK。

        :param config: 包含 api_key、base_url、model 的配置对象
        """
        # c16：`request_timeout` 非 None 时把超时交给 SDK 客户端。
        #
        # ⚠ **超时必须落在这一层，不能只在调用方计时。** 调用方那种写法在
        # 「第一个数据块永远不到达」时完全无效——它还没开始迭代，计时器根本
        # 没有可中断的对象。本项目踩过同型的坑：
        # `subprocess.run(shell=True, capture_output=True, timeout=T)` 这个组合下
        # `timeout` 是假的，实测 `timeout=1` 在命令 `sleep 8` 时真的等了 8 秒
        # （见 `tools/run_command.py` 与 `tests/test_subprocess_timeout.py`）。
        #
        # ⚠ **不能无条件传。** 在这个 SDK 里显式传 `timeout=None` 的语义是
        # 「不设超时」，与「用 SDK 的默认值」**不是一回事**。因此缺省不传，
        # 使既有行为逐字不变。
        #
        # 当前唯一的传入方是装配层给**分类器专用**的那个 Provider 副本
        # （`bootstrap.py`）。主对话刻意不设超时：它的一次请求可能生成几分钟
        # （模型在吐一份大文件的内容），设超时会把正常工作腰斩。
        client_kwargs = {"api_key": config.api_key, "base_url": config.base_url}
        if getattr(config, "request_timeout", None) is not None:
            client_kwargs["timeout"] = config.request_timeout
        self._client = openai.OpenAI(**client_kwargs)
        self._model = config.model
        # C7：错误分类要用到的三样上下文。都是**已经在 Config 里的值**，
        # 只是此前没人往下传——401 那条文案必须报出实际生效的配置文件路径
        # （`--config` 与用户级两条来源，用户常改错文件），连接类那条必须
        # 报出主机名。
        self._base_url = config.base_url
        self._config_path = config.source_path

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

        # 只记「这次请求长什么样」——条数、开没开思考、带没带工具。
        # 三个数字合起来足以回答「是不是历史太长了」「是不是工具集太大了」，
        # 而它们一个字节的对话内容都不含。
        _logger.info(
            "请求模型 model=%s 消息=%d 条 thinking=%s 工具=%d 个",
            self._model,
            len(sdk_messages),
            thinking_effort,
            len(tools or []),
        )
        started = time.monotonic()

        try:
            stream = self._client.chat.completions.create(**create_kwargs)

            # 工具调用按 index 累积：index → {"id", "name", "arguments"(字符串拼接)}
            # 流式中首片携带 id 与 function.name，后续片仅追加 function.arguments 碎片
            tool_buffers: dict[int, dict] = {}
            # 已经产出过 "tool_pending" 的 index 集合。
            # 每个工具调用只**播报一次**：参数碎片会到达几十上百次，每次都播报会把
            # 事件流淹掉（同 text 增量的量级），而界面只需要一个「这一步开始了」的锚点。
            announced: set[int] = set()

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

                        # 拿到「id + 工具名」的第一时间播报一次 tool_pending，让界面
                        # 立刻出现一行状态。**必须放在累积之后**：名字与 id 可能分散在
                        # 前两个碎片里，放在累积前会拿到空值。
                        #
                        # 两个字段都非空才播报（而不是只看 name）——id 是界面把这行状态
                        # 与后续 tool_start / tool_result 对上的唯一键，缺了它就会多出
                        # 一行永远转圈的孤儿状态。协议上两者本来同片到达；万一没有，
                        # 这里安静地退回旧行为（不播报），不做任何猜测。
                        if idx not in announced and buf["id"] and buf["name"]:
                            announced.add(idx)
                            yield StreamChunk(
                                type="tool_pending",
                                tool_call=ToolCall(
                                    id=buf["id"], name=buf["name"], arguments=None
                                ),
                            )

            # 流结束：把累积的工具调用解析并产出（按 index 顺序保持稳定）
            for idx in sorted(tool_buffers.keys()):
                buf = tool_buffers[idx]
                raw_args = buf["arguments"] or "{}"
                try:
                    parsed_raw = json.loads(raw_args)
                except (json.JSONDecodeError, TypeError):
                    # 模型可能生成非法 JSON：标记 arguments=None，由协调层转结构化错误
                    parsed = None
                else:
                    # Function-calling arguments must be a JSON object. Other JSON values
                    # are treated like a parse failure so permission code never sees them.
                    parsed = parsed_raw if isinstance(parsed_raw, dict) else None
                yield StreamChunk(
                    type="tool_call",
                    tool_call=ToolCall(id=buf["id"], name=buf["name"], arguments=parsed),
                )

            _logger.info("请求完成 耗时=%.1fs", time.monotonic() - started)
            yield StreamChunk(type="done", content="")

        except Exception as e:
            # 这里是**唯一**能看到 SDK 原始异常类型的地方（往上只剩一个字符串），
            # 因此类型名必须记下来——它是「401 还是连不上」这个问题的唯一答案。
            # ⚠ 不记 `str(e)`：服务端的 message 里可能回显掩码后的 key 与请求细节，
            # 格式随服务端变，不适合进一份用户会随手贴出来的日志。
            # C7：按异常类型分派，给出「一句中文说明 + 服务端原文」。
            #
            # ⚠ 此前这里是 `content=str(e)`，于是 401 / 429 / 断网 / 模型名写错
            # 四类在界面上长得**一模一样**（R3 实跑四类确认），而紧接着的
            # 「因流错误已停止」同样不含任何行动信息。分类表在 `errors.py`——
            # 它是 SDK 的知识，不该漏到 agent/ 或 tui/。
            _logger.log(
                errors.log_level_for(e),
                "请求失败 耗时=%.1fs 异常=%s",
                time.monotonic() - started,
                type(e).__name__,
                # 已被 SDK 分好类的九类不带堆栈（那只是噪音）；
                # 未预期的那一类才需要，它是作者唯一能拿到的线索。
                exc_info=not errors.is_sdk_error(e),
            )
            yield StreamChunk(
                type="error",
                content=errors.classify(
                    e,
                    model=self._model,
                    base_url=self._base_url,
                    config_path=self._config_path,
                ),
            )
