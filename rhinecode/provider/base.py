"""
Provider 抽象层，定义所有 LLM 后端必须遵守的统一接口。

设计原则：
- ConversationManager 和 TUI 层只依赖此模块，不感知具体 Provider 实现
- 新增 Provider 时只需继承 BaseProvider 并实现 stream_chat，无需改动上层代码
- StreamChunk 采用 type 字段区分内容类型，便于 TUI 层做差异化渲染
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, Iterator, Optional


@dataclass
class ToolCall:
    """
    一次完整的工具调用（流式碎片拼接完成后的最终形态）。

    DeepSeek/OpenAI 协议中，工具调用在流式响应里以分片到达：首片携带 id 与
    函数名，后续片增量追加 JSON 参数字符串。Provider 负责把这些碎片拼接、
    解析为本结构，再交给协调层执行。

    :param id: API 返回的调用唯一标识，结果回灌时需原样带回（role=tool 的 tool_call_id）
    :param name: 被调用的工具名（对应 Tool.name）
    :param arguments: 解析后的参数字典；若模型生成的 JSON 非法导致解析失败，则为 None，
                      由协调层转成结构化错误回灌模型，而非崩溃
    :param raw_arguments: **只在 `arguments` 为 None 时才有值**——模型生成的那串原始
                      文本，一个字符都没改。解析成功时刻意留空：`arguments` 已经
                      无损地承载了同样的内容，再存一份会让每条记录里的写文件正文
                      凭空翻倍。
    :param arguments_error: 同样只在解析失败时有值，说明是怎么失败的（JSON 语法错
                      误的位置，或「解析出来不是一个对象」）。

    ## 为什么失败时必须把原文留下来

    解析失败时只记一个 `arguments: None`，等于**把唯一的证据扔了**：
    事后谁也说不清模型到底写坏在哪、是它生成了非法 JSON 还是我们这边拼接碎片
    时丢了东西。真实 trace 里出现过一次（2026-09-18，一次约 1200 token 的
    `edit_file` 参数），当场无从排查。这与「工具裁剪了 output 必须同时填
    `full_output`」是同一条纪律的两个落点：**观测设施丢内容就不再是证据。**
    """
    id: str
    name: str
    arguments: Optional[dict]
    raw_arguments: Optional[str] = None
    arguments_error: Optional[str] = None


@dataclass
class Message:
    """
    单条对话消息，支持普通文本消息与工具调用相关消息。

    三种典型形态：
    - 普通消息：role="user"/"assistant"，仅含 content
    - 模型发起工具调用：role="assistant"，content 为可选文本，tool_calls 为发起的调用列表
    - 工具执行结果：role="tool"，content 为结果文本，tool_call_id 指向对应的调用

    :param role: 发送方角色，取值 "user"（用户）/ "assistant"（AI）/ "tool"（工具结果）
    :param content: 消息文本内容；assistant 发起工具调用时可为空字符串。
                    模型实际接收的完整内容（语义历史），上下文估算/摘要/Provider 均只读它
    :param tool_calls: 仅 assistant 发起工具调用时存在，记录本轮发起的所有 ToolCall
    :param tool_call_id: 仅 role="tool" 时存在，标明该结果对应哪一次工具调用
    :param display_content: 用户界面与历史回放优先显示的原始输入（c10 双内容模型，
                            spec F25–F27）。仅提示词型命令需要设置（如 /init 时
                            content 为展开后的完整提示词、display_content 为 "/init"）；
                            普通消息为 None。各 Provider 序列化时显式挑选模型字段，
                            本字段不会发送给模型 API
    """
    role: str
    content: str = ""
    tool_calls: Optional[list[ToolCall]] = None
    tool_call_id: Optional[str] = None
    display_content: Optional[str] = None


@dataclass
class StreamChunk:
    """
    流式响应中的单个数据块。

    TUI 层根据 type 字段决定如何渲染：
    - "text"：AI 正文输出，追加到当前回复区域
    - "thinking"：Claude Extended Thinking 的思考过程，以灰色斜体显示
    - "done"：流正常结束信号，content 为空字符串
    - "error"：发生异常，content 为可读错误描述，以红色显示
    - "tool_call"：Provider 解析出的一次完整工具调用（第一轮流结束后产出），
                   由协调层内部收集，不直接渲染；载荷在 tool_call 字段
    - "tool_pending"：Provider 在流**进行中**发现「模型开始吐一个工具调用」时产出一次，
                      此时只知道 id 与 name，`tool_call.arguments` 恒为 None。
                      纯展示用途（让界面立刻显示「这一步开始了」），**不参与任何判定**，
                      协调层也不把它累积进 tool_calls——完整调用仍由后续的 "tool_call" 承载。
                      为什么需要它：写文件类调用的参数里塞着整份文件内容，这段 JSON
                      可能生成几十秒，期间既无正文增量也未进入执行，界面会完全静止
    - "tool_start"：协调层在某工具开始执行时产出，TUI 据此新建橘色工具行并启动计时器；
                    载荷在 tool_call 字段
    - "tool_result"：协调层在某工具执行完成时产出，TUI 据此把工具行转绿/红并展示摘要；
                     载荷在 tool_call（哪个调用）与 tool_result（执行结果）字段
    - "usage"：本轮请求的 token 用量（c4 新增）。OpenAI 兼容协议在开启 include_usage 后，
               流末尾会额外返回一块携带 usage 统计；Provider 据此产出本类型，载荷在 usage 字段

    :param type: 块类型，取值见上方说明
    :param content: 文本类块的内容；非文本类块为空字符串
    :param tool_call: tool_call / tool_start / tool_result 类型携带的工具调用信息
    :param tool_result: tool_result 类型携带的执行结果（类型为 tools.base.ToolResult，
                        此处标注为 Any 以避免 provider 包反向依赖 tools 包形成循环导入）
    :param usage: usage 类型携带的 token 用量（provider SDK 原生 usage 对象或字典，
                  标注为 Any 以免 provider 反向依赖 agent 包；由上层 collector 转成 Usage）
    """
    type: str
    content: str = ""
    tool_call: Optional[ToolCall] = None
    tool_result: Any = None
    usage: Any = None


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
        thinking_effort: str = "off",
        tools: Optional[list[dict]] = None,
        system: Optional[str] = None,
    ) -> Iterator[StreamChunk]:
        """
        以流式方式发起对话请求，逐块产出响应内容。

        :param messages: 完整对话历史，含本轮用户消息，按时间顺序排列；可能包含
                         assistant(tool_calls) 与 role="tool" 的工具结果消息
        :param thinking_effort: 思考模式强度，取值 "off"（关闭）/ "high"（高效）/ "max"（最强）
                                Anthropic 中 high/max 均映射为开启，DeepSeek 直接传给 reasoning_effort，
                                OpenAI 协议忽略此参数
        :param tools: 工具描述列表（OpenAI function 格式）；非空时随请求发送以启用工具调用。
                      仅 DeepSeek 实现真正使用，OpenAI/Anthropic 实现忽略此参数（本章不支持工具）
        :param system: 稳定系统提示，作为「可缓存通道」的抽象入口（c5）。内容逐轮逐字节一致，
                       各 Provider 自行决定如何利用缓存：DeepSeek 把它作为消息序列首条 system 消息、
                       依赖自动前缀缓存；Anthropic 映射到顶层 system 参数（将来可加 cache_control 断点）。
                       与 messages 中带 <system-reminder> 标签的「动态」system 消息分属两条通道。
        :returns: StreamChunk 迭代器，调用方逐块消费，最后一块 type 为 "done" 或 "error"；
                  若模型发起工具调用，会在 done 之前产出若干 type="tool_call" 的块

        副作用：向远端 API 发起网络请求，消耗 token 配额。
        """
        ...
