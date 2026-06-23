"""
对话管理模块。

ConversationManager 是 TUI 层与 Provider 层之间的协调者，职责包括：
- 维护多轮对话的完整消息历史（history）
- 解析并执行斜杠命令（/think、/clear、/exit）
- 管理思考模式的三档强度（off / high / max）
- 在流式回复完成后，将 AI 回复追加到历史记录
- 工具编排（本章新增）：把模型发起的工具调用执行并回灌，自动触发一次最终回答

TUI 层调用 handle_input() 获取结果，结果类型决定 TUI 的后续行为：
- str：斜杠命令的反馈文本，直接作为系统提示显示
- Iterator[StreamChunk]：流式回复生成器，由 TUI 的 Worker 消费并逐块渲染

工具编排的单轮往返（仅 DeepSeek 且注入了 registry 时启用）：
  用户消息 → 第一轮请求(带 tools) → 模型可能发起工具调用
  → 执行工具(只读并发 / 有副作用串行 + 执行前确认) → 结果回灌 history
  → 第二轮请求 → 模型基于结果产出最终文本回答 → 停（不做跨轮循环）
"""

from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Callable, Iterator, Optional

from rhinecode.provider.base import BaseProvider, Message, StreamChunk, ToolCall
from rhinecode.tools.base import Tool, ToolResult
from rhinecode.tools.registry import ToolRegistry

# 执行前确认回调类型：给定工具调用与工具实例，返回 True(允许)/False(拒绝)。
# 由 TUI 层实现（弹出确认框），协调层只调用、不感知 TUI 细节。
ConfirmCallback = Callable[[ToolCall, Tool], bool]


class ConversationManager:
    """
    多轮对话管理器。

    持有对话历史、思考模式状态、工具注册中心与确认回调，
    是 TUI 与 Provider 之间的唯一协调点。TUI 层不直接调用 Provider，
    所有请求均通过此类中转。
    """

    # /think 命令的三态循环顺序：关闭 → 高效 → 最强 → 关闭
    _EFFORT_CYCLE = {"off": "high", "high": "max", "max": "off"}
    # 各档位对应的中文显示名称
    _EFFORT_LABEL = {"off": "关闭", "high": "高效（high）", "max": "最强（max）"}

    def __init__(
        self,
        provider: BaseProvider,
        provider_protocol: str,
        registry: Optional[ToolRegistry] = None,
    ):
        """
        初始化对话管理器。

        :param provider: 已实例化的 Provider，负责实际的 API 调用
        :param provider_protocol: Provider 的协议名（如 "anthropic"），
                                  用于判断是否支持思考模式与工具调用
        :param registry: 工具注册中心；为 None 时不启用工具能力
        """
        self._provider = provider
        self._protocol = provider_protocol
        self._registry = registry
        self.history: list[Message] = []
        # 思考模式强度：off（关闭）/ high（高效）/ max（最强）
        self.thinking_effort: str = "off"
        # 执行前确认回调，由 TUI 层在挂载后注入；为 None 时有副作用工具默认放行
        self.confirm_callback: Optional[ConfirmCallback] = None
        # 工具能力仅在 DeepSeek 协议且提供了注册中心时启用（本章范围）
        self._tools_enabled = (provider_protocol == "deepseek" and registry is not None)

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
        - 其他     → 追加用户消息到 history，调用工具编排流程，返回生成器

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

        # 普通消息：先追加到历史，再发起工具编排流程
        msg = Message(role="user", content=text)
        self.history.append(msg)
        return self._stream(list(self.history))

    def _stream(self, messages: list[Message]) -> Iterator[StreamChunk]:
        """
        工具编排主流程：第一轮请求 →（如有工具调用）执行并回灌 → 第二轮最终回答。

        本方法是生成器：把面向 TUI 的 StreamChunk 逐个 yield 出去，同时在内部维护
        对话历史。未启用工具或模型未发起工具调用时，行为与纯对话一致。

        :param messages: 本轮请求携带的完整历史快照（含刚追加的用户消息）
        :returns: 透传/产出供 TUI 渲染的 StreamChunk

        副作用：向 self.history 追加 assistant 文本、assistant(tool_calls)、tool 结果消息。
        """
        tools = self._registry.schemas() if self._tools_enabled else None

        # ---------- 第一轮：带 tools 请求，收集文本与工具调用 ----------
        text_buf: list[str] = []
        tool_calls: list[ToolCall] = []

        for chunk in self._provider.stream_chat(messages, self.thinking_effort, tools=tools):
            if chunk.type == "text":
                text_buf.append(chunk.content)
                yield chunk
            elif chunk.type == "thinking":
                yield chunk
            elif chunk.type == "tool_call":
                # 工具调用由协调层内部收集，不直接渲染（执行时再以 tool_start 呈现）
                if chunk.tool_call is not None:
                    tool_calls.append(chunk.tool_call)
            elif chunk.type == "error":
                # API/网络错误：透传给 TUI 显示，终止本轮（不追加历史）
                yield chunk
                return
            elif chunk.type == "done":
                # 第一轮的 done 不直接透传，待后续决定是否进入第二轮
                pass

        # 模型未发起工具调用：与纯对话行为一致
        if not tool_calls:
            if text_buf:
                self.history.append(Message(role="assistant", content="".join(text_buf)))
            yield StreamChunk(type="done", content="")
            return

        # ---------- 有工具调用：追加 assistant(tool_calls) 到历史 ----------
        self.history.append(
            Message(role="assistant", content="".join(text_buf), tool_calls=tool_calls)
        )

        # ---------- 执行工具：只读并发 / 有副作用串行，结果写入 results ----------
        results: dict[str, ToolResult] = {}
        yield from self._execute(tool_calls, results)

        # 按原始顺序把每个工具结果作为 role="tool" 消息回灌历史
        for tc in tool_calls:
            res = results.get(tc.id)
            output = res.output if res is not None else "工具未产生结果"
            self.history.append(Message(role="tool", tool_call_id=tc.id, content=output))

        # ---------- 第二轮：自动再请求，产出最终文本回答 ----------
        # 仍携带 tools，但忽略模型此轮再次发起的 tool_call（不做跨轮循环）
        text2: list[str] = []
        for chunk in self._provider.stream_chat(list(self.history), self.thinking_effort, tools=tools):
            if chunk.type == "text":
                text2.append(chunk.content)
                yield chunk
            elif chunk.type == "thinking":
                yield chunk
            elif chunk.type == "tool_call":
                # 单轮边界：第二轮的工具调用不执行，直接忽略
                pass
            elif chunk.type == "error":
                yield chunk
                return
            elif chunk.type == "done":
                pass

        if text2:
            self.history.append(Message(role="assistant", content="".join(text2)))
        yield StreamChunk(type="done", content="")

    def _execute(
        self,
        tool_calls: list[ToolCall],
        results: dict[str, ToolResult],
    ) -> Iterator[StreamChunk]:
        """
        执行一批工具调用，按只读/有副作用分组：只读并发、有副作用串行。

        为每个工具在开始执行时产出 tool_start、完成时产出 tool_result（携带结果），
        供 TUI 起橘色计时行并最终转绿/红。所有结果按 tool_call.id 写入 results 供回灌。

        分组与执行规则：
        - 只读工具（read_only=True）：并发执行（ThreadPoolExecutor），互不冲突
        - 有副作用工具（read_only=False）/ 未知工具：串行执行，执行前调确认回调
        - 参数解析失败（arguments is None）：不执行，直接结构化错误
        - 未知工具名：不执行，结构化错误

        :param tool_calls: 第一轮解析出的工具调用列表
        :param results: 输出参数，函数把 id → ToolResult 写入其中
        :returns: 产出 tool_start / tool_result 类型的 StreamChunk

        副作用：实际执行工具（可能写文件、跑命令）；通过确认回调与用户交互。
        """
        readonly: list[tuple[ToolCall, Tool]] = []
        side_effect: list[tuple[ToolCall, Optional[Tool]]] = []

        # 按 read_only 分组；未知工具(tool=None)归入串行组以走统一的错误处理
        for tc in tool_calls:
            tool = self._registry.get(tc.name) if self._registry else None
            if tool is not None and tool.read_only:
                readonly.append((tc, tool))
            else:
                side_effect.append((tc, tool))

        # 只读组并发执行
        if readonly:
            yield from self._run_readonly_concurrent(readonly, results)

        # 有副作用组（含未知工具）串行执行
        for tc, tool in side_effect:
            yield from self._run_one_serial(tc, tool, results)

    def _run_readonly_concurrent(
        self,
        items: list[tuple[ToolCall, Tool]],
        results: dict[str, ToolResult],
    ) -> Iterator[StreamChunk]:
        """
        并发执行一组只读工具。

        先为每个调用产出 tool_start（TUI 起橘色计时行），再用线程池并发执行，
        每个完成即产出对应 tool_result。参数解析失败的调用不进线程池，直接报错。

        :param items: (ToolCall, Tool) 列表，均为只读工具
        :param results: 输出参数，写入 id → ToolResult
        :returns: tool_start / tool_result 流

        副作用：并发读取文件系统等（只读，无写入）。
        """
        for tc, _tool in items:
            yield StreamChunk(type="tool_start", tool_call=tc)

        # 参数解析失败的调用：不执行，直接结构化错误
        for tc, _tool in items:
            if tc.arguments is None:
                res = ToolResult(ok=False, output=f"工具 {tc.name} 的参数 JSON 解析失败，请检查参数格式后重试。")
                results[tc.id] = res
                yield StreamChunk(type="tool_result", tool_call=tc, tool_result=res)

        valid = [(tc, tool) for tc, tool in items if tc.arguments is not None]
        if not valid:
            return

        # 线程池并发执行：as_completed 谁先完成先产出结果
        with ThreadPoolExecutor(max_workers=len(valid)) as executor:
            future_to_tc = {
                executor.submit(tool.execute, tc.arguments): tc
                for tc, tool in valid
            }
            for future in as_completed(future_to_tc):
                tc = future_to_tc[future]
                try:
                    res = future.result()
                except Exception as e:
                    # execute 内部应已兜底；此处再兜一层防止线程异常逃逸
                    res = ToolResult(ok=False, output=f"工具执行异常: {e}")
                results[tc.id] = res
                yield StreamChunk(type="tool_result", tool_call=tc, tool_result=res)

    def _run_one_serial(
        self,
        tc: ToolCall,
        tool: Optional[Tool],
        results: dict[str, ToolResult],
    ) -> Iterator[StreamChunk]:
        """
        串行执行单个有副作用工具（或处理未知工具/参数错误）。

        处理顺序：
        1. 未知工具 → 结构化错误
        2. 参数解析失败 → 结构化错误
        3. 否则调确认回调：拒绝 → "用户拒绝执行" 结果；允许 → 执行

        :param tc: 工具调用
        :param tool: 对应工具实例；未知工具时为 None
        :param results: 输出参数，写入 id → ToolResult
        :returns: tool_start / tool_result 流

        副作用：可能写文件、执行命令；通过 confirm_callback 与用户交互。
        """
        # 未知工具：展示一行并返回结构化错误
        if tool is None:
            yield StreamChunk(type="tool_start", tool_call=tc)
            res = ToolResult(ok=False, output=f"未知工具: {tc.name}")
            results[tc.id] = res
            yield StreamChunk(type="tool_result", tool_call=tc, tool_result=res)
            return

        # 参数解析失败
        if tc.arguments is None:
            yield StreamChunk(type="tool_start", tool_call=tc)
            res = ToolResult(ok=False, output=f"工具 {tc.name} 的参数 JSON 解析失败，请检查参数格式后重试。")
            results[tc.id] = res
            yield StreamChunk(type="tool_result", tool_call=tc, tool_result=res)
            return

        # 执行前确认采用 fail-closed：没有确认回调时绝不执行有副作用工具。
        if self.confirm_callback is None:
            res = ToolResult(ok=False, output="缺少执行前确认回调，已拒绝执行该工具。", summary="未确认，已拒绝")
            results[tc.id] = res
            yield StreamChunk(type="tool_result", tool_call=tc, tool_result=res)
            return

        approved = self.confirm_callback(tc, tool)

        if not approved:
            # 用户拒绝：不执行，回灌结构化结果让模型据此回答；只展示结果行（无执行中）
            res = ToolResult(ok=False, output="用户拒绝执行该工具。")
            results[tc.id] = res
            yield StreamChunk(type="tool_result", tool_call=tc, tool_result=res)
            return

        # 确认通过后才展示执行中行并实际执行
        yield StreamChunk(type="tool_start", tool_call=tc)
        try:
            res = tool.execute(tc.arguments)
        except Exception as e:
            res = ToolResult(ok=False, output=f"工具执行异常: {e}")
        results[tc.id] = res
        yield StreamChunk(type="tool_result", tool_call=tc, tool_result=res)
