# C8 上下文管理（两层压缩）Tasks

## 文件清单

| 操作 | 文件 | 职责 |
|------|------|------|
| 新建 | `rhinecode/context/__init__.py` | 导出 `ContextManager`、`CompactionNotice` |
| 新建 | `rhinecode/context/models.py` | `CompactionNotice` / `ContextStats` 数据类 |
| 新建 | `rhinecode/context/estimate.py` | 近似 token 估算（锚点 + 增量） |
| 新建 | `rhinecode/context/offload.py` | 第一层：工具结果存盘 + 占位 + 幂等 |
| 新建 | `rhinecode/context/summarize.py` | 第二层纯逻辑：保留边界/转录/Prompt/解析/重构 |
| 新建 | `rhinecode/context/manager.py` | `ContextManager` 编排 + LLM 摘要 + 熔断 |
| 修改 | `rhinecode/config.py` | `Config.context_window` 字段 + 解析 |
| 修改 | `rhinecode/agent/events.py` | 新增 `AgentEventType.NOTICE` |
| 修改 | `rhinecode/agent/loop.py` | `run()` 接入 `before_request` / `record_usage` |
| 修改 | `rhinecode/conversation.py` | 构造 ctx；`/context` `/compact`；`clear` 重置 |
| 修改 | `rhinecode/tui/widgets.py` | `CommandPanel.COMMANDS` 加 `/compact` `/context` |
| 修改 | `rhinecode/tui/app.py` | `_do_stream` 处理 `NOTICE` 事件 |
| 修改 | `config.example.yaml` | `context_window` 注释说明 |
| 新建 | `tests/test_context_estimate.py` | 估算单测 |
| 新建 | `tests/test_context_offload.py` | 存盘/幂等单测 |
| 新建 | `tests/test_context_summarize.py` | 保留边界/解析/重构单测 |
| 新建 | `tests/test_context_manager.py` | 编排/熔断/锚点（假 provider）单测 |

## T1: Config 增加 context_window 字段

**文件：** `rhinecode/config.py`
**依赖：** 无
**步骤：**
1. `Config` dataclass 增加 `context_window: int = 65536`，补字段注释（窗口上限，判断是否逼近溢出的基准）。
2. 仿 `_parse_bool` 加 `_parse_int(value, field_name, default)`：接受 int 或纯数字字符串，非法/`None` 回退默认值（**不**抛异常，fail-safe），负数或 0 也回退默认。
3. `load()` 里 `context_window=_parse_int(data.get("context_window", 65536), "context_window", 65536)`，加入返回的 `Config(...)`。

**验证：** `python -m compileall rhinecode/config.py`；`python -c "import rhinecode.config as c; print(c.load('config.example.yaml').context_window)"` 输出默认值（需 T13 先补示例字段，或临时对不含该字段的配置验证回退到 65536）。

## T2: 新增 NOTICE 事件类型

**文件：** `rhinecode/agent/events.py`
**依赖：** 无
**步骤：**
1. `AgentEventType` 枚举加 `NOTICE = "notice"`，注释：系统级提示（压缩发生等），载荷在 `message`。
2. 在 `AgentEvent` 类 docstring 的字段说明里补一行 `NOTICE：message 为提示文本`。

**验证：** `python -c "from rhinecode.agent.events import AgentEventType; print(AgentEventType.NOTICE)"`。

## T3: context/models.py

**文件：** `rhinecode/context/models.py`
**依赖：** 无
**步骤：**
1. 定义 `@dataclass CompactionNotice`：`kind: str`（"offload"/"summary"/"circuit_break"/"noop"）、`message: str`。
2. 定义 `@dataclass ContextStats`：`estimated_tokens: int`、`window: int`、`headroom: int`、`offloaded_count: int`、`circuit_broken: bool`。
3. 顶部模块 docstring 说明这是纯数据层。

**验证：** `python -c "from rhinecode.context.models import CompactionNotice, ContextStats"`（需 T8 建 `__init__.py`，或本步先建空 `__init__.py`）。

## T4: context/estimate.py

**文件：** `rhinecode/context/estimate.py`
**依赖：** T3
**步骤：**
1. 定义常量 `CHARS_PER_TOKEN = 3.0`、`MSG_OVERHEAD_TOKENS = 4`，注释「偏保守、宁可高估早触发」。
2. `estimate_message_tokens(msg: Message) -> int`：`len(content)` 加上 `tool_calls` 里各调用 `name + json 参数` 的字符数，整除 `CHARS_PER_TOKEN` 后加 `MSG_OVERHEAD_TOKENS`。
3. `estimate_tokens(history, anchor_tokens, anchor_len) -> int`：`anchor_tokens is None` → 全量求和；否则 `anchor_tokens + Σ estimate_message_tokens(history[anchor_len:])`。`anchor_len` 越界（历史被截短）时按 `min(anchor_len, len(history))` 兜底。
4. 关键注释：解释锚点为何精确、增量为何足够粗估。

**验证：** `python -m unittest tests.test_context_estimate`（T14 编写）。临时 `python -c` 构造几条 Message 验证 anchor 分支与无 anchor 分支数值合理。

## T5: context/offload.py

**文件：** `rhinecode/context/offload.py`
**依赖：** T3, T4
**步骤：**
1. 常量 `SINGLE_RESULT_TOKENS=4000`、`COMBINED_RESULT_TOKENS=16000`、`PREVIEW_CHARS=500`。
2. `class Offloader`：`__init__(self, store_dir: Path)` 保存目录、初始化 `self._offloaded: set[str] = set()`。
3. `_placeholder(full: str, path: Path) -> str`：构造「`[大型工具结果已存盘 · 原 <human_size>]` + 预览前 `PREVIEW_CHARS` 字 + 文件路径 + 重读提示」。
4. `_offload_one(self, msg) -> bool`：确保 `store_dir` 存在；把 `msg.content` 写入 `store_dir/<tool_call_id>.txt`（无 id 时用递增序号）；成功则 `msg.content = _placeholder(...)`、`self._offloaded.add(key)`、返回 True；`OSError` → 返回 False（保留原文，N2）。
5. `run(self, history) -> list[CompactionNotice]`：
   - 第一趟：遍历 `role=="tool"` 且 key 未在 `_offloaded` 的消息，`estimate_message_tokens > SINGLE_RESULT_TOKENS` 的逐个 `_offload_one`。
   - 第二趟：对仍未存盘的 tool 结果算合计估算，若 `> COMBINED_RESULT_TOKENS`，按估算体积降序依次 `_offload_one` 直到合计达标。
   - 汇总本次存盘条数/体量，返回 0 或 1 条 `CompactionNotice(kind="offload", ...)`（无存盘则返回空列表）。
6. `count` property 返回 `len(self._offloaded)`；`reset()` 清空集合。

**验证：** `python -m unittest tests.test_context_offload`（T14）。断言：超阈值 tool 消息被替换为占位且磁盘有文件；user 消息不变；二次 `run` 不重复处理（幂等）；聚合场景挑大的先存。

## T6: context/summarize.py

**文件：** `rhinecode/context/summarize.py`
**依赖：** T4
**步骤：**
1. 常量 `RETAIN_TOKENS=10000`、`MIN_RETAIN_MESSAGES=5`、`SUMMARY_MARKER="<<<正式摘要>>>"`。
2. `SUMMARY_SYSTEM_PROMPT`：固定五部分结构（任务目标/已完成关键步骤与结论/关键文件与改动/当前状态与待办/重要约束与决策）+ 明确「禁止调用任何工具」+「先写分析草稿，再在 `<<<正式摘要>>>` 之后写正式摘要」。
3. `compute_retain_index(history) -> int`：从尾部累加 `estimate_message_tokens` 直到 `>=RETAIN_TOKENS` 或已数满 `MIN_RETAIN_MESSAGES`（取更靠前者，即保留更多）；再把该 index 回退到「最近的 `role=="user"` 消息下标」；找不到 user（极端）则返回 0。
4. `render_transcript(messages) -> str`：逐条渲染 `【角色】内容`（tool 带 tool_call_id、assistant 带 tool_calls 概要），拼成转录文本。
5. `parse_summary(text) -> Optional[str]`：按 `SUMMARY_MARKER` 分割取最后一段并 strip；无标记则取整段 strip；空串返回 None。
6. `reconstruct(summary_text, retained) -> list[Message]`：返回 `[Message(role="user", content=summary_text), Message(role="assistant", content=<边界提示>), *retained]`；边界提示常量文本「以上是早前对话的结构化摘要。如需具体文件内容或代码细节，我会重新读取相关文件，绝不照摘要臆测代码。」

**验证：** `python -m unittest tests.test_context_summarize`（T14）。断言：保留边界落在 user 且不切碎；`parse_summary` 正确丢草稿；`reconstruct` 结构为 user→assistant→retained。

## T7: context/manager.py

**文件：** `rhinecode/context/manager.py`
**依赖：** T3, T4, T5, T6
**步骤：**
1. `class ContextManager.__init__(self, provider, model, window, store_dir, auto_margin=13000, manual_margin=3000)`：初始化 `_offloader`、`_anchor_tokens=None`、`_anchor_len=0`、`_summary_failures=0`、`_circuit_broken=False`。
2. `_estimate(self, history) -> int`：调 `estimate_tokens(history, self._anchor_tokens, self._anchor_len)`。
3. `before_request(self, history) -> list[CompactionNotice]`：先 `notices = self._offloader.run(history)`；再 `if not self._circuit_broken and self._estimate(history) > self.window - self.auto_margin:` → append `self._do_summary(history)`。返回 notices。
4. `record_usage(self, usage, sent_len)`：`_anchor_tokens = usage.prompt_tokens`、`_anchor_len = sent_len`。
5. `manual_compact(self, history) -> CompactionNotice`：`if self._estimate(history) <= self.window - self.manual_margin:` → 返回 `noop`「上下文尚宽裕（估算 X / 上限 Y），无需压缩」；否则 `return self._do_summary(history)`。
6. `_do_summary(self, history) -> CompactionNotice`：
   - `idx = compute_retain_index(history)`；`idx==0`（无可摘要早段）→ 返回 `noop`。
   - `to_summarize, retained = history[:idx], history[idx:]`。
   - `try:` 调 `provider.stream_chat(messages=[Message("user", render_transcript(to_summarize))], thinking_effort="off", tools=None, system=SUMMARY_SYSTEM_PROMPT)`，累积 `type=="text"` 文本，遇 `type=="error"` 视为失败。
   - `summary = parse_summary(text)`；`None` → 计失败。
   - 成功：`history[:] = reconstruct(summary, retained)`；`_anchor_tokens=None`；`_summary_failures=0`；返回 `CompactionNotice("summary", "已摘要早前 N 条消息，保留近 M 条原文")`。
   - 失败/异常：`_summary_failures += 1`；`>=3` → `_circuit_broken=True` 返回 `circuit_break` 通知；否则返回 `summary` 失败通知（本次未压缩）。
7. `usage_report(self, history) -> str`：组 `ContextStats` → 多行文本（估算 token / 上限 / 余量 / 已存盘数 / 熔断态）。
8. `reset(self)`：锚点/失败/熔断归零，`self._offloader.reset()`。

**验证：** `python -m unittest tests.test_context_manager`（T14）。用假 provider（可控返回摘要文本 / 返回 error）覆盖：正常摘要重构、锚点失效、连续 3 次失败熔断、manual 未达阈值 noop。

## T8: context/__init__.py 导出

**文件：** `rhinecode/context/__init__.py`
**依赖：** T3, T7
**步骤：**
1. `from .manager import ContextManager`、`from .models import CompactionNotice, ContextStats`，定义 `__all__`。

**验证：** `python -c "from rhinecode.context import ContextManager, CompactionNotice"`。

## T9: loop.py 接入压缩

**文件：** `rhinecode/agent/loop.py`
**依赖：** T2, T8
**步骤：**
1. `run()` 签名增加 `context_manager: "Optional[ContextManager]" = None`（放在参数表末尾，避免打乱既有位置参数）；docstring 补该参数说明。
2. 每轮循环体内、`tools = self._schema_for(...)` **之前**：`if context_manager is not None:` 遍历 `context_manager.before_request(history)`，每条 `yield AgentEvent(type=AgentEventType.NOTICE, message=notice.message)`。
3. 在构造 `req_messages` 处记录 `sent_len = len(history)`（**append reminder 前、且用 history 长度而非 req_messages 长度**——锚点覆盖的是纯历史消息数）。
4. 拿到 usage 后（`if debug_log_path and collector.usage` 附近）：`if context_manager is not None and collector.usage is not None: context_manager.record_usage(collector.usage, sent_len)`。
5. 顶部按需 `from rhinecode.context import ContextManager`（或用字符串注解避免导入环，二选一并注释）。

**验证：** `python -m compileall rhinecode/agent/loop.py`；`python -m unittest discover -s tests`（既有 loop 测试仍绿，证明默认 `None` 不改变现有行为）。

## T10: conversation.py 接线

**文件：** `rhinecode/conversation.py`
**依赖：** T1, T8, T9
**步骤：**
1. `__init__` 中在 `_tools_enabled` 为真时构造 `self._context_manager = ContextManager(provider, config.model, config.context_window, workspace_root()/".rhinecode"/"context")`；否则 `None`。
2. `_run()` 调 `self._agent.run(..., self._cancel_event, self._context_manager)`（追加末位实参）。
3. `handle_input` 加分支（均需 `_tools_enabled` 守卫，仿 `/perm`）：
   - `/context` → 返回 `self._context_manager.usage_report(self.history)`（str）。
   - `/compact` → 返回一个生成器方法 `self._manual_compact()`：`yield AgentEvent(NOTICE, message=ctx.manual_compact(history).message)`，再 `yield AgentEvent(FINISHED, stop_reason=COMPLETED)`。**不**向 history 追加 user 消息。
4. `clear()` 末尾：`if self._context_manager is not None: self._context_manager.reset()`。

**验证：** `python -m compileall rhinecode/conversation.py`；启动前先跑 `python -m unittest discover -s tests`。手动路径留待 checklist 端到端。

## T11: widgets.py 补全列表

**文件：** `rhinecode/tui/widgets.py`
**依赖：** 无（但语义依赖 T10）
**步骤：**
1. `CommandPanel.COMMANDS` 追加 `("/compact", "压缩上下文（LLM 摘要早前对话）")` 与 `("/context", "查看当前上下文用量")`。

**验证：** `python -c "from rhinecode.tui.widgets import CommandPanel; print([c for c in CommandPanel.COMMANDS if c[0] in ('/compact','/context')])"`。

## T12: app.py 处理 NOTICE

**文件：** `rhinecode/tui/app.py`
**依赖：** T2
**步骤：**
1. `_do_stream` 的事件分支里加 `elif etype == AgentEventType.NOTICE:` → `self.call_from_thread(history_view.append_system, event.message)`。

**验证：** `python -m compileall rhinecode/tui/app.py`。

## T13: config.example.yaml 补注释

**文件：** `config.example.yaml`
**依赖：** 无
**步骤：**
1. 加一行带注释的可选字段示例：`# context_window: 65536   # 上下文窗口上限(token)，逼近时自动压缩；缺省 65536`。

**验证：** `python -c "import rhinecode.config as c; print(c.load('config.example.yaml').context_window)"` 输出预期值。

## T14: 单元测试

**文件：** `tests/test_context_estimate.py`、`tests/test_context_offload.py`、`tests/test_context_summarize.py`、`tests/test_context_manager.py`
**依赖：** T4, T5, T6, T7
**步骤：**
1. `estimate`：无锚点全量求和；有锚点 = 锚点 + 增量；`anchor_len` 越界兜底。
2. `offload`：单结果超阈值被占位 + 磁盘落文件（用 tmp 目录）；user 消息不动；二次 run 幂等；聚合挑大先存。
3. `summarize`：`compute_retain_index` 回退到 user 且 ≥5 条 / 10K；`parse_summary` 丢草稿；`reconstruct` 结构正确。
4. `manager`：假 provider 返回可控文本 → 正常摘要重构 + 锚点失效；假 provider 连返 error → 3 次熔断；manual 未达阈值 → noop；`before_request` 先 offload 后 summary 的顺序（offload 降低估算）。

**验证：** `python -m compileall rhinecode tests` + `python -m unittest discover -s tests` 全绿。

## 执行顺序

```
T1  ─┐
T2  ─┤（独立基础，可并行）
T3 → T4 → T5 ─┐
         └ T6 ┤→ T7 → T8
T8 → T9 → T10
T11 ─┐
T12 ─┤（接线，依赖前置）
T13 ─┘
T4..T7 → T14（测试随对应模块完成即可写）
```
建议线性推进：T1 → T2 → T3 → T4 → T5 → T6 → T7 → T8 → T9 → T10 → T11 → T12 → T13 → T14。
