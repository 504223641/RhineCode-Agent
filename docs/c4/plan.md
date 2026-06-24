# RhineCode Agent Loop Plan

## 架构概览

c4 在现有三层（TUI / ConversationManager / Provider）之间，新增一个独立的 **agent 包**，把「自主循环」从 ConversationManager 里抽出来，形成清晰的职责边界：

- **agent 包（新增）** — Agent Loop 的核心。`Agent` 是 ReAct 循环引擎，负责「调模型 → 执行工具 → 回灌 → 再调模型」的多轮推进与停止判断；对外只产出与界面解耦的 `AgentEvent` 事件流（F1/F2/F3）。配套 `StreamCollector`（双路收集，F4）、`plan_tools`（Plan Mode 特殊工具的 schema，F12/F13）、`prompt`（Plan Mode 引导提示，F11）。

- **ConversationManager（改造）** — 仍是 TUI 与下层之间的唯一协调点，但不再亲自跑循环。它负责：维护对话历史、解析斜杠命令（新增 `/plan`）、持有会话级状态（思考模式、Plan Mode 开关、会话级「免确认」标志、取消信号），并在每条普通消息到来时**构造一次 Agent 运行**（把回调与策略以参数/闭包注入 `Agent`），把 `Agent` 产出的事件流交给 TUI。

- **Provider 层（小改）** — `StreamChunk` 增加 `usage` 字段与 `"usage"` 类型；DeepSeek Provider 通过 `stream_options` 取回 token 用量，并把请求里的 `role="system"` 消息透传给 API（用于 Plan Mode 引导提示注入，F7/F11）。

- **TUI 层（改造）** — Worker 改为消费 `AgentEvent`（而非裸 `StreamChunk`）；新增确认三态、澄清面板、计划审批、取消按键、`/plan` 命令与状态栏（Plan Mode + 累计用量）显示。

**事件 vs 回调的分工**：单向「展示用」信息（文本、工具进展、用量、进度、结束）走 `AgentEvent` 事件流，TUI 只读不回写；需要「用户做决定」的同步交互（有副作用工具确认、Plan Mode 需求澄清、计划执行审批）无法用单向事件承载往返，改用**阻塞回调**——循环在 Worker 线程调用回调，回调通过 `call_from_thread` 在主线程弹面板、用 `threading.Event` 阻塞等待用户选择后返回（沿用 c3 `_confirm_tool` 的不死锁模式，N2）。

## 核心数据结构

### AgentEventType（枚举，agent/events.py）
循环对外事件的类型：
- `TEXT` — 正文文本增量
- `THINKING` — 思考内容增量
- `TOOL_START` — 某工具开始执行（载荷 `tool_call`）
- `TOOL_RESULT` — 某工具执行完成（载荷 `tool_call` + `tool_result`）
- `USAGE` — 一轮请求的 token 用量（载荷 `usage`）
- `PROGRESS` — 进入新一轮迭代（载荷 `iteration`）
- `FINISHED` — 循环结束（载荷 `stop_reason`，可选 `message` 补充说明）
- `ERROR` — 发生错误（载荷 `message`）

### StopReason（枚举，agent/events.py）
循环结束原因，与 F2 的停止条件一一对应：
- `COMPLETED` — 模型本轮不再发起工具调用，自然完成
- `MAX_ITERATIONS` — 达到迭代上限（兜底）
- `USER_CANCELLED` — 用户取消
- `UNKNOWN_TOOL` — 连续调用未知工具达阈值
- `STREAM_ERROR` — 底层流出错

### Usage（dataclass，agent/events.py）
- `prompt_tokens: int` / `completion_tokens: int` / `total_tokens: int`

### AgentEvent（dataclass，agent/events.py）
```
type: AgentEventType
text: str = ""                  # TEXT / THINKING 的增量内容
tool_call: Optional[ToolCall] = None    # TOOL_START / TOOL_RESULT
tool_result: Any = None         # TOOL_RESULT（tools.base.ToolResult，标 Any 避免反向依赖）
usage: Optional[Usage] = None   # USAGE
iteration: int = 0              # PROGRESS：当前第几轮
stop_reason: Optional[StopReason] = None   # FINISHED
message: str = ""               # ERROR 描述 / FINISHED 补充说明
```

### ConfirmDecision（枚举，agent/events.py）
确认面板三态返回值（落实 F6）：
- `ALLOW` — 仅放行本次
- `ALLOW_ALWAYS` — 放行并本会话不再询问
- `DENY` — 拒绝执行

### ClarifyOption（dataclass，agent/events.py）
Plan Mode 澄清面板的单个选项（落实 F12）：
- `summary: str` — 概述（可上下导航选择的一行）
- `detail: str` — 详细描述（说明该选项含义与取舍）

### StreamChunk 扩展（provider/base.py，改）
现有结构新增：
- 新增字段 `usage: Any = None`（承载 provider 解析出的用量，标 Any 以免 provider 反向依赖 agent 包）
- `type` 取值新增 `"usage"`：流末尾产出，携带 `usage`

### Agent 回调签名（agent/loop.py 依赖、由 ConversationManager 注入）
- `confirm: Callable[[ToolCall, Tool], bool]` — 有副作用工具是否执行（**已在闭包内解析过三态与会话级免确认**，循环只看 bool）
- `clarify: Callable[[str, list[ClarifyOption]], Optional[str]]` — 弹澄清面板，返回所选概述；返回 `None` 表示用户取消
- `approve_plan: Callable[[str], bool]` — 弹「是否开始执行」，返回是否批准

## 模块设计

### 模块：agent/events.py（新建）
**职责：** 定义上述全部纯数据类型（事件、枚举、用量、澄清选项、确认决定）。无任何行为逻辑，供各层共享，避免循环依赖。
**对外接口：** 上述数据类型。
**依赖：** 仅 `provider.base.ToolCall`（类型引用）。

### 模块：agent/collector.py（新建）
**职责：** 落实 F4「双路流式收集」。`StreamCollector` 逐块吃下 provider 的 `StreamChunk`，一边把可展示的块翻译成 `AgentEvent` 实时返回给循环转发，一边在内部累积本轮完整响应。
**对外接口：**
- `feed(chunk: StreamChunk) -> Optional[AgentEvent]` — 喂入一块：`text`→累积并返回 `AgentEvent(TEXT)`；`thinking`→返回 `AgentEvent(THINKING)`；`tool_call`→累积到工具调用列表（不返回展示事件）；`usage`→记录用量并返回 `AgentEvent(USAGE)`；`done`/`error`→返回 `None`（由循环处理）。
- 属性 `text: str` — 累积的完整正文
- 属性 `tool_calls: list[ToolCall]` — 累积的完整工具调用列表
- 属性 `usage: Optional[Usage]` — 本轮用量
**依赖：** `provider.base`、`agent.events`。

### 模块：agent/prompt.py（新建）
**职责：** 落实 F11。构造 Plan Mode 的引导 system prompt——要求模型「先用只读工具调研、对不清楚的需求细节用 `ask_user` 逐一澄清、用 `present_plan` 提交计划等待用户审批、获批前不要执行任何修改类操作」。
**对外接口：** `build_plan_prompt() -> str`。
**依赖：** 无（纯文本）。

### 模块：agent/plan_tools.py（新建）
**职责：** 落实 F12/F13 的「特殊交互工具」。定义两个仅用于 Plan Mode 规划阶段、以 Tool schema 暴露给模型、但**不走 registry 执行**（由循环拦截路由到回调）的工具，以及它们的名称常量。
**对外接口：**
- `ASK_USER = "ask_user"` / `PRESENT_PLAN = "present_plan"`（名称常量）
- `AskUserTool`（`Tool` 子类，`read_only=True`）：参数 schema 为 `{question: str, options: [{summary, detail}]}`，描述里说明「options 第一个应为最推荐项」。`execute` 不会被调用（循环拦截），保留兜底。
- `PresentPlanTool`（`Tool` 子类，`read_only=True`）：参数 schema 为 `{plan: str}`。
- `plan_schemas() -> list[dict]` — 返回这两个工具的 schema 列表，供规划阶段附加到工具列表。
**依赖：** `tools.base.Tool`。

### 模块：agent/loop.py（新建，核心）
**职责：** ReAct 循环引擎，落实 F1/F2/F4/F5/F11/F13/F14。
**对外接口：**
- `Agent(provider, registry)` — 构造时持有长期依赖。
- `run(history, thinking_effort, plan_mode, system_prompt, confirm, clarify, approve_plan, cancel_event) -> Iterator[AgentEvent]` — 跑一次完整循环，逐个产出 `AgentEvent`；过程中向 `history`（同一引用）追加 assistant / tool 消息（N5）。

**主循环逻辑（run）：**
1. 初始化每轮局部状态：`execution_phase = False`（Plan Mode 是否已获批进入执行阶段）、`consecutive_unknown = 0`。
2. `for iteration in 1..MAX_ITERATIONS`：
   - 安全点检查 `cancel_event`：已置位 → 产出 `FINISHED(USER_CANCELLED)` 并返回。
   - 产出 `PROGRESS(iteration)`。
   - 计算本轮工具集 `tools = _schema_for(plan_mode, execution_phase)`：
     - Plan Mode 且未获批执行 → 只读工具 schema + `plan_schemas()`（ask_user / present_plan）
     - 其余（普通模式，或 Plan Mode 已获批执行阶段）→ 全部工具 schema
   - 组装请求消息：`([Message(role="system", content=system_prompt)] if system_prompt else []) + history`。
   - 双路收集：`collector = StreamCollector()`；`for chunk in provider.stream_chat(req, thinking_effort, tools)`：`ev = collector.feed(chunk)`，非空则 `yield ev`；遇 `chunk.type=="error"` 记录并中断。
   - 若流出错 → 产出 `ERROR` + `FINISHED(STREAM_ERROR)` 返回（N1）。
   - 取 `text, tool_calls = collector.text, collector.tool_calls`。
   - **无工具调用** → 有文本则 `history.append(assistant(text))`，产出 `FINISHED(COMPLETED)` 返回（F2 自然完成 / F14 纯对话）。
   - **有工具调用** → `history.append(assistant(text, tool_calls))`；调用 `_execute(...)` 执行并回灌结果；按原始顺序把每个结果作为 `role="tool"` 追加 history（N5）。
   - 根据 `_execute` 回写的轮次上下文更新：`consecutive_unknown`（本轮含未知工具则累加、含已知工具则清零）、`execution_phase`（present_plan 获批则置真）。
   - `consecutive_unknown >= MAX_CONSECUTIVE_UNKNOWN` → `FINISHED(UNKNOWN_TOOL)` 返回。
   - 安全点再查 `cancel_event`。
3. 循环自然走完上限 → `FINISHED(MAX_ITERATIONS)`（F2 兜底 / N3）。

**工具执行 `_execute(tool_calls, history-append via results, ctx, confirm, clarify, approve_plan, execution_phase, cancel_event) -> Iterator[AgentEvent]`：**
- 先把 `tool_calls` 分流：
  - **特殊工具**（名为 `ask_user` / `present_plan`）→ 串行、走交互回调，不进 registry。
    - `ask_user`：解析 options → 调 `clarify(question, options)`；返回所选概述作为结果文本；返回 `None` → 置 `ctx.cancelled`（循环据此结束为 USER_CANCELLED）。
    - `present_plan`：调 `approve_plan(plan)`；批准 → `ctx.approved = True`，结果文本「用户已批准，开始执行」；否则结果文本「用户暂未批准，请据反馈调整计划」。
  - **普通只读工具** → 并发执行（沿用 c3 `ThreadPoolExecutor` 逻辑，F5）。
  - **普通有副作用工具 / 未知工具** → 串行执行（沿用 c3）。
- 有副作用工具执行前是否确认：**`plan_mode 执行阶段（execution_phase=True）跳过确认**（计划审批即放行，F13）；否则调 `confirm(tool_call, tool)`（已封装三态+会话免确认，F6）。
- 未知工具：结构化错误结果，并在 `ctx` 记一次未知（用于连续未知统计）。
- 每个工具开始/结束分别产出 `TOOL_START` / `TOOL_RESULT`；所有结果写入 `results` 供主循环回灌。
- 全程异常兜底为 `ToolResult(ok=False)`（N1）。

**模块常量：** `MAX_ITERATIONS = 25`、`MAX_CONSECUTIVE_UNKNOWN = 3`。
**依赖：** `provider.base`、`tools.*`、`agent.events`、`agent.collector`、`agent.plan_tools`。

### 模块：conversation.py（改造）
**职责：** 协调层。移除 c3 写死的 `_stream` 单轮往返，改为委托 `Agent`。
**改动点：**
- `__init__`：构造 `self._agent = Agent(provider, registry)`；新增状态 `self.plan_mode = False`、`self._always_allow = False`；回调字段 `confirm_callback`（改为返回 `ConfirmDecision`）、`clarify_callback`、`approve_plan_callback`；取消信号 `self._cancel_event`（每次运行重建）。
- `handle_input`：新增 `/plan` 分支——仅工具可用（DeepSeek）时切换 `plan_mode` 并返回状态文本，否则返回「当前 Provider 不支持」；普通消息追加 history 后调用 `_run`。
- `_run(...) -> Iterator[AgentEvent]`：重建 `self._cancel_event`；计算 `system_prompt = build_plan_prompt() if self.plan_mode else None`；构造 `confirm` 闭包（内部读/写 `self._always_allow`、调用 TUI 三态 `confirm_callback`，返回 bool）；调用 `self._agent.run(self.history, self.thinking_effort, self.plan_mode, system_prompt, confirm, self.clarify_callback, self.approve_plan_callback, self._cancel_event)` 并返回其事件流。
- `request_cancel()`：置位 `self._cancel_event`，供 TUI 取消时调用（F9）。
**依赖：** `agent.loop`、`agent.events`、`agent.prompt`。

### 模块：provider/base.py（改造）
- `StreamChunk` 增加 `usage: Any = None` 字段，`type` 文档补充 `"usage"`。

### 模块：provider/deepseek.py（改造）
- `stream_chat` 在 `create_kwargs` 加 `stream_options={"include_usage": True}`，使最后一块携带 `usage`。
- 流式循环中：`if getattr(chunk, "usage", None): yield StreamChunk(type="usage", usage=chunk.usage)`（在 done 之前）。
- `_to_sdk_messages` 已能透传 `role="system"`（走通用 else 分支），无需改动；确认其正确即可（用于 Plan Mode 引导提示）。

### 模块：tui/widgets.py（改造）
- `StatusBar.update_status(...)`：新增 `plan_mode: bool` 与可选 `usage` 参数，显示 `… | 思考：X | 计划模式：开/关 | Tokens: N`。
- `CommandPanel.COMMANDS`：新增 `("/plan", "切换计划模式：先规划并澄清需求，审批后再执行（DeepSeek）")`。
- `ConfirmPanel.show_for`：选项由两项扩为三项——「执行」「执行且本会话不再询问」「取消」，`option.id` 分别为 `yes` / `yes_always` / `no`（F6）。
- 新增 `ClarifyPanel(OptionList)`：`show_for(question, options)` 把每个选项渲染为「可选概述行 + 紧随其后的禁用详情行」，导航因 OptionList 跳过 disabled 项而只落在概述上；首个选项概述带「⭐ 推荐」标记（F12）。选中产出 `OptionSelected`（id=该选项索引），Esc 产出 `Cancelled`。

### 模块：tui/app.py（改造）
**职责：** 消费 `AgentEvent`，实现三类交互回调与取消、`/plan` 与状态栏。
**改动点：**
- `on_mount`：注入 `manager.confirm_callback = self._confirm_tool`（返回 `ConfirmDecision`）、`manager.clarify_callback = self._clarify`、`manager.approve_plan_callback = self._approve_plan`。
- `_do_stream`：改为按 `AgentEvent.type` 分发——`TEXT/THINKING` 同 c3 渲染；`TOOL_START/TOOL_RESULT` 同 c3 工具行；`USAGE` 累加并刷新状态栏；`PROGRESS` 刷新状态栏/瞬时行显示「第 N 轮」；`FINISHED` 按 `stop_reason` 追加系统行（完成 / 已达上限 / 已取消 / 连续未知工具停止）；`ERROR` 红色行。
- `_confirm_tool`：复用 c3 阻塞模式，但读取三选项 → 返回 `ConfirmDecision`。
- `_clarify(question, options)` / `_approve_plan(plan)`：同款阻塞模式，分别弹 `ClarifyPanel` / 复用 Yes-No 面板，返回所选概述 / bool；统一用 `self._pending_interaction` + `threading.Event` 管理（N2 不死锁）。
- 取消：`on_key` 中当 `_stream_active` 且按下 `escape` → 调 `self._manager.request_cancel()`（F9）；输入框禁用期间该键仍可达 App 层。
- `/plan`：`on_input_bar_input_submitted` 对 `/plan` 走 str 反馈分支并 `_refresh_status()` 同步状态栏（F10）。
- `_refresh_status`：传入 `manager.plan_mode` 与累计用量。

### 模块：__main__.py
- 基本不变（`Agent` 由 ConversationManager 内部构造）；如需可补注释。

## 模块交互

主调用链（一条普通用户消息）：
```
InputBar 提交
  └─ RhineApp.on_input_bar_input_submitted
       └─ ConversationManager.handle_input(text)
            ├─ "/plan" → 切换 plan_mode，返回 str（App 刷新状态栏）
            └─ 普通消息 → 追加 history → ConversationManager._run()
                 └─ Agent.run(history, …, confirm/clarify/approve, cancel_event)
                      → 返回 Iterator[AgentEvent]
  └─ RhineApp.run_worker(_do_stream(events))   # 后台线程消费
```

循环内部（Agent.run 每一轮）：
```
PROGRESS ─▶ provider.stream_chat(req, tools) ─▶ StreamCollector.feed ─▶ yield TEXT/THINKING/USAGE
  └─ 收齐 text + tool_calls
       ├─ 无 tool_calls → FINISHED(COMPLETED)
       └─ 有 tool_calls → history.append(assistant) → _execute
             ├─ ask_user      → clarify() 回调（阻塞）→ TOOL_RESULT
             ├─ present_plan   → approve_plan() 回调（阻塞）→ 置 execution_phase → TOOL_RESULT
             ├─ 只读工具(并发) → TOOL_START/RESULT
             └─ 副作用工具(串行) → [非执行阶段] confirm() 回调（阻塞）→ TOOL_START/RESULT
        → history.append(tool 结果) → 更新 consecutive_unknown / execution_phase
        → 检查停止条件 → 下一轮 或 FINISHED(reason)
```

回调跨线程（N2）：`Agent.run` 在 Worker 线程执行；`confirm/clarify/approve` 内部 `call_from_thread` 在主线程弹面板，并以 `threading.Event` 阻塞 Worker 直到用户在主线程选择后唤醒返回，主线程事件循环不被阻塞。

## 文件组织

```
rhinecode/
├── agent/                  # 新增包：Agent Loop 与事件层
│   ├── __init__.py
│   ├── events.py           # AgentEvent / AgentEventType / StopReason / Usage / ClarifyOption / ConfirmDecision
│   ├── collector.py        # StreamCollector（双路收集，F4）
│   ├── prompt.py           # build_plan_prompt（Plan Mode 引导提示，F11）
│   ├── plan_tools.py       # AskUserTool / PresentPlanTool / 名称常量 / plan_schemas（F12/F13）
│   └── loop.py             # Agent（ReAct 循环引擎，核心，F1/F2/F5/F13/F14）
├── conversation.py         # 改：委托 Agent；plan_mode / always_allow / cancel 状态；/plan；回调封装
├── provider/
│   ├── base.py             # 改：StreamChunk 增 usage 字段 + "usage" 类型
│   └── deepseek.py         # 改：stream_options 取 usage、产出 usage chunk
├── tui/
│   ├── app.py              # 改：消费 AgentEvent；确认三态/澄清/审批回调；取消键；/plan；状态栏
│   └── widgets.py          # 改：StatusBar(plan+usage)；ConfirmPanel 三项；新增 ClarifyPanel
└── __main__.py             # 基本不变
```

## 技术决策

| 决策点 | 选择 | 理由 |
|--------|------|------|
| 循环归属 | 新建 `agent` 包的 `Agent` 类，ConversationManager 委托 | N4 解耦、职责单一；ConversationManager 回归「协调 + 状态」本职 |
| 展示 vs 交互 | 单向展示走 `AgentEvent` 事件流；需用户决定的走阻塞回调 | 事件流无法承载同步往返；交互天然需要等待返回值 |
| 特殊工具 ask_user/present_plan | 以 Tool schema 暴露给模型，但由循环拦截路由到回调，不走 registry | 复用工具协议让模型自然「调用」，交互编排留在循环，registry 保持纯净 |
| 会话级「免确认」状态 | 存 ConversationManager（`_always_allow`），用闭包封进 `confirm` 传给循环 | 需跨多轮对话持久（spec 选定「整个会话」）；循环保持无会话状态 |
| 系统提示注入 | 每请求前置 `role="system"` 消息，不入持久 history，仅 Plan Mode 注入 | 改动最小，DeepSeek 透传 system；不污染历史 |
| 取消机制 | `threading.Event`，循环在安全点轮询，TUI 按 Esc 触发 | 与现有 Worker 线程模型一致，不强杀线程，保证历史一致性（N5/N6） |
| token 用量获取 | `stream_options={"include_usage": True}` | OpenAI 兼容协议的标准做法，最后一块带 usage |
| 双路收集 | `StreamCollector.feed` 实时返回展示事件 + 内部累积完整响应 | 直接落实 F4，循环只管转发与判断 |
| 澄清面板导航 | 概述为可选项、详情为紧随的 disabled 项 | OptionList 跳过 disabled，使导航只落概述、详情仍可见（F12） |
| 迭代上限 / 未知阈值 | 模块常量（25 / 3） | spec 明确不做 YAML 配置 |
| Plan 执行阶段确认 | 执行阶段（已审批）跳过逐工具确认 | 计划审批即整体放行（F13），避免重复打断 |
