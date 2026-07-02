# C8 上下文管理（两层压缩）Plan

## 架构概览

新增一个独立的 **Context 层**（`rhinecode/context/`），职责是「在每次 API 请求前，把对话历史压到 token 预算内」。它与 `permission/`、`mcp/` 同为「纯逻辑 + 单点接入」的横切层：核心算法（估算、存盘、摘要保留边界）是可单测的纯函数，编排状态（估算锚点、熔断计数、已存盘集合）收敛在一个 `ContextManager` 对象里。

分层与接入点：

- **Context 层内部**（下层不感知上层）：
  - `estimate.py` — 近似 token 估算（锚点 + 增量），纯函数。
  - `offload.py` — 第一层预防：工具结果存盘 + 占位替换，幂等。
  - `summarize.py` — 第二层兜底的纯逻辑：保留边界计算、历史转录、摘要 Prompt、草稿/正文解析、历史重构。
  - `models.py` — 数据结构（压缩通知、上下文统计）。
  - `manager.py` — `ContextManager` 编排两层、持有会话级状态、封装 LLM 摘要调用与熔断。
- **接入点（低侵入，N1）**：
  - `Agent.run()`（`loop.py`）：每轮请求前调 `ctx.before_request(history)`（自动路径）；每轮拿到 usage 后调 `ctx.record_usage(...)` 更新估算锚点。这是**唯一**嵌入循环的两处调用，主流程不为压缩铺分支。
  - `ConversationManager`：构造并持有 `ContextManager`（仅 DeepSeek 工具模式）；解析 `/context`（只读，返回 `str`）与 `/compact`（重量压缩，返回事件流生成器，走 Worker 线程不卡 UI）；`/clear` 时重置上下文状态。
  - `provider`：`summarize` 复用现有 `stream_chat`，system 传摘要 Prompt、`tools=None`，与普通对话共用一条通道。

数据流一句话：`loop` 每轮把 `history` 交给 `ContextManager` → 先第一层存盘（改写超大工具结果为「预览 + 路径」）→ 估算逼近上限则第二层摘要（LLM 生成结构化摘要，重构 `history`）→ 循环继续用压缩后的 `history` 发请求；请求回来的 `usage.prompt_tokens` 作为下一次估算的精确锚点。

## 核心数据结构

### Config 新增字段（`rhinecode/config.py`）

给 `Config` 增加一个可选字段（与 `debug_log` 同样式，老配置不写也能加载）：

```python
context_window: int = 65536   # 上下文窗口上限（token），判断是否逼近溢出的基准
```

`load()` 里按可选整数解析（`_parse_int(data.get("context_window", 65536), ...)`，非法/缺失回退默认值，不崩溃）。`config.example.yaml` 补一行注释说明。对应 **F1**。

### CompactionNotice / ContextStats（`context/models.py`）

```python
@dataclass
class CompactionNotice:
    kind: str      # "offload" | "summary" | "circuit_break" | "noop"
    message: str   # 面向用户的一行中文反馈（F17）

@dataclass
class ContextStats:
    estimated_tokens: int   # 当前估算 token
    window: int             # 窗口上限
    headroom: int           # 距上限余量 = window - estimated
    offloaded_count: int    # 已存盘的工具结果数
    circuit_broken: bool    # 摘要是否已熔断
```

### 估算接口（`context/estimate.py`）

```python
CHARS_PER_TOKEN: float = 3.0     # 字符→token 近似比（偏保守，宁可高估早触发）
MSG_OVERHEAD_TOKENS: int = 4     # 每条消息的角色/框架固定开销

def estimate_message_tokens(msg: Message) -> int: ...
    # ≈ len(content)//CHARS_PER_TOKEN + MSG_OVERHEAD；含 tool_calls 参数字符

def estimate_tokens(history, anchor_tokens: Optional[int], anchor_len: int) -> int: ...
    # anchor_tokens 为 None → 对全部消息求和（无锚点，首次请求）
    # 否则 → anchor_tokens + Σ estimate_message_tokens(history[anchor_len:])
```

**为什么这样估**：`anchor_tokens` 是上一次 API 亲口返回的 `prompt_tokens`，对「已发送过的那段历史（含 system/reminder 开销）」是**精确值**；只有锚点之后新追加的少量消息按字符粗估。误差被限制在「增量」上（通常几条消息），所以窗口余量（13K/3K）足以吸收，无需精确 tokenizer（**F2/N3**）。

### 第一层：Offloader（`context/offload.py`）

```python
SINGLE_RESULT_TOKENS: int = 4000     # 单个工具结果超此值即存盘
COMBINED_RESULT_TOKENS: int = 16000  # 工具结果合计超此值触发聚合存盘
PREVIEW_CHARS: int = 500             # 占位里保留的预览字符数

class Offloader:
    def __init__(self, store_dir: Path): ...        # store_dir = <项目根>/.rhinecode/context
    def run(self, history) -> list[CompactionNotice]: ...
    @property
    def count(self) -> int: ...                     # 已存盘数（供 /context）
    def reset(self) -> None: ...                     # /clear 时清空幂等集合
```

- 幂等（**F7**）：内部 `self._offloaded: set[str]`，以 `tool_call_id` 为键；已存盘的消息不再处理。
- `run` 两趟：① 单结果趟——遍历未存盘的 `role="tool"` 消息，`estimate > SINGLE_RESULT_TOKENS` 的逐个存盘；② 聚合趟——若剩余未存盘工具结果合计 `> COMBINED_RESULT_TOKENS`，按体积从大到小依次存盘直到合计达标（**F4/F5**）。
- 只动 `role="tool"` 消息，绝不碰 `role="user"`（**F6**）。
- 存盘 = 把完整 `content` 写入 `store_dir/<tool_call_id>.txt`，把消息 `content` 替换为占位串：`[大型工具结果已存盘 · 原 <size>]\n预览：<前 PREVIEW_CHARS 字>…\n完整内容见文件：<path>\n（需要完整内容请用 read_file 读取该文件）`。写盘 I/O 失败 → 跳过该条、保留原文（**N2**）。

### 第二层：摘要纯逻辑（`context/summarize.py`）

```python
RETAIN_TOKENS: int = 10000          # 尾部保留原文的目标 token
MIN_RETAIN_MESSAGES: int = 5        # 至少保留的尾部消息条数
SUMMARY_MARKER: str = "<<<正式摘要>>>"  # 草稿与正文分隔标记
SUMMARY_SYSTEM_PROMPT: str = "..."  # 固定结构 + 禁用工具 + 先草稿后正文（见下）

def compute_retain_index(history) -> int: ...
    # 从尾部按 token 回数，凑够 max(RETAIN_TOKENS, MIN_RETAIN_MESSAGES 条)；
    # 再把边界「回退到最近的 role==user 消息」，保证：不切碎消息、
    # 保留区以 user 消息开头、assistant(tool_calls) 与其 tool 结果不被拆散。

def render_transcript(messages) -> str: ...
    # 把待摘要消息渲染成纯文本转录（角色前缀 + 内容），作为一条 user 消息发给模型，
    # 规避「裸 tool 消息缺配对」的 API 校验问题。

def parse_summary(text: str) -> Optional[str]: ...
    # 取 SUMMARY_MARKER 之后的正文（丢弃草稿，F11）；无标记则退化为整段文本；空则 None。

def reconstruct(summary_text: str, retained: list[Message]) -> list[Message]: ...
    # 返回 [user(结构化摘要), assistant(边界提示), *retained]
```

**摘要 Prompt 固定结构（F10）**：任务目标 / 已完成的关键步骤与结论 / 涉及的关键文件与改动 / 当前状态与待办 / 重要约束与决策。Prompt 明确：**禁止调用任何工具**；**先写「分析草稿」再写正式摘要**，正式摘要以 `SUMMARY_MARKER` 起头（**F11**）。

**历史重构（F12）**：`reconstruct` 产出 `[user(摘要), assistant(边界提示), *retained]`。边界提示做两件事——① 内容为「以上是早前对话摘要，如需文件细节请重新读取，不要照摘要脑补代码」（F12 的边界消息）；② 作为合成的 assistant 轮，恢复 `user→assistant→user...` 交替，保证重构后的历史对 API 合法（因 `retained` 以 user 开头）。

### 编排：ContextManager（`context/manager.py`）

```python
class ContextManager:
    def __init__(self, provider, model, window, store_dir,
                 auto_margin=13000): ...
    # 会话级状态：
    #   _anchor_tokens: Optional[int]；_anchor_len: int（锚点覆盖的历史条数）
    #   _offloader: Offloader
    #   _summary_failures: int；_circuit_broken: bool

    def before_request(self, history) -> list[CompactionNotice]: ...
        # 自动路径：① offloader.run(history)；② est = estimate(...)，
        # 若未熔断且 est > window - auto_margin → _do_summary(history)。返回所有通知。

    def record_usage(self, usage, sent_len: int) -> None: ...
        # _anchor_tokens = usage.prompt_tokens；_anchor_len = sent_len。

    def manual_compact(self, history) -> CompactionNotice: ...
        # /compact：无余量阈值，直接 _do_summary(history)；无够旧早段时由 _do_summary
        #   返回 noop「无可摘要的早段」。只做第二层摘要，不做第一层 offload。

    def usage_report(self, history) -> str: ...   # /context 的只读文本

    def reset(self) -> None: ...                   # /clear：清锚点/熔断/存盘集合

    def _do_summary(self, history) -> CompactionNotice: ...
        # 调 provider.stream_chat（system=摘要 Prompt, tools=None）收集文本 →
        # parse_summary → reconstruct → history[:] = 新列表（原地替换保持引用）→
        # 锚点失效（_anchor_tokens=None）。成功清零失败计数；异常/空则 _summary_failures += 1，
        # 达 3 次置 _circuit_broken（F15）。
```

- 估算锚点在摘要重构后失效（历史索引已变），置 `None` 让下次请求先全字符估算，直到新 usage 回来重新锚定。
- `_do_summary` 里若 `compute_retain_index` 得到「没有可摘要的早段」（全在保留区）→ 返回 `noop`，不算失败。
- 熔断在成功摘要或 `reset()`（/clear）后归零。

### 新增事件类型（`agent/events.py`）

```python
class AgentEventType(str, Enum):
    ...
    NOTICE = "notice"   # 系统级提示（压缩发生等），message 为文本
```

`loop` 与 `/compact` 生成器用它把压缩反馈送到 TUI（**F17**）。

## 模块设计

### `context/estimate.py`
**职责**：近似 token 估算（锚点 + 增量）。
**对外接口**：`estimate_message_tokens`、`estimate_tokens`、常量 `CHARS_PER_TOKEN`。
**依赖**：`provider.base.Message`。纯函数，无 I/O。

### `context/offload.py`
**职责**：第一层预防——超阈值工具结果存盘 + 占位替换 + 幂等。
**对外接口**：`Offloader.run/count/reset`，常量阈值。
**依赖**：`estimate`、`provider.base.Message`、`pathlib`、`tools.base.human_size`（体量格式化）。

### `context/summarize.py`
**职责**：第二层兜底的纯逻辑——保留边界、转录、Prompt、解析、重构。
**对外接口**：`compute_retain_index`、`render_transcript`、`parse_summary`、`reconstruct`、`SUMMARY_SYSTEM_PROMPT`、常量。
**依赖**：`estimate`、`provider.base.Message`。纯逻辑，不含 provider 调用（调用留给 manager，便于单测）。

### `context/manager.py`
**职责**：编排两层、持会话状态、封装 LLM 摘要调用与熔断。
**对外接口**：`ContextManager` 的 `before_request/record_usage/manual_compact/usage_report/reset`。
**依赖**：`estimate`、`offload`、`summarize`、`models`、`provider.base`。是 Context 层唯一持有 provider 引用、唯一有副作用编排的模块。

### `context/models.py`
**职责**：数据结构。**依赖**：无（纯 dataclass）。

### 改动：`agent/loop.py`
**职责变化**：`run()` 增参 `context_manager: Optional[ContextManager]`；每轮请求前调 `before_request` 并 `yield NOTICE`，记录 `sent_len`，usage 到手后调 `record_usage`。仅此两处，主流程不变。

### 改动：`conversation.py`
**职责变化**：构造/持有 `ContextManager`（仅 `_tools_enabled`）；`_run` 把它传入 `agent.run`；`handle_input` 加 `/context`（返回 `str`）与 `/compact`（返回事件流生成器）分支；`clear()` 调 `ctx.reset()`。

### 改动：`agent/events.py`、`tui/app.py`、`tui/widgets.py`、`config.py`
- `events.py`：加 `AgentEventType.NOTICE`。
- `app.py`：`_do_stream` 处理 `NOTICE` → `history_view.append_system(message)`。
- `widgets.py`：`CommandPanel.COMMANDS` 加 `/compact`、`/context`。
- `config.py`：`Config` 加 `context_window` 字段 + `load` 解析 + 模板/示例注释。

## 模块交互

**自动压缩（每轮请求前）**：
```
Agent.run 循环第 k 轮
  └─ ctx.before_request(history)
       ├─ Offloader.run(history)                # 第一层：改写超大 tool 结果为占位
       │    └─ 写 <root>/.rhinecode/context/<id>.txt
       └─ estimate_tokens(...) > window-13K ?    # 第二层判断
            └─ 是 → _do_summary(history)
                     ├─ compute_retain_index / render_transcript
                     ├─ provider.stream_chat(system=摘要Prompt, tools=None)  # LLM 摘要
                     ├─ parse_summary（丢草稿）
                     └─ history[:] = reconstruct(...)   # 原地替换，锚点失效
  └─ 每个 CompactionNotice → yield AgentEvent(NOTICE)
  └─ sent_len = len(history)；构造 req_messages；stream_chat 主对话
  └─ 拿到 collector.usage → ctx.record_usage(usage, sent_len)   # 更新精确锚点
```

**手动 `/compact`（Worker 线程）**：
```
用户输入 /compact
  └─ handle_input 返回生成器（不追加 user 消息）
       └─ TUI 走 Worker：ctx.manual_compact(history)（余量 3K）
            └─ 返回 CompactionNotice → yield NOTICE → yield FINISHED(COMPLETED)
```

**只读 `/context`（UI 线程）**：
```
用户输入 /context
  └─ handle_input 返回 str = ctx.usage_report(history)
       └─ TUI append_system 展示（估算 token / 余量 / 已存盘数 / 熔断态）
```

**`/clear`**：`ConversationManager.clear()` 清空 history 后调 `ctx.reset()`（锚点、熔断、存盘集合归零）。

## 文件组织

```
rhinecode/
├── context/                      # 新增：上下文管理层
│   ├── __init__.py               # 导出 ContextManager、CompactionNotice
│   ├── models.py                 # CompactionNotice / ContextStats
│   ├── estimate.py               # 近似 token 估算（锚点+增量）
│   ├── offload.py                # 第一层：工具结果存盘 + 占位 + 幂等
│   ├── summarize.py              # 第二层纯逻辑：保留边界/转录/Prompt/解析/重构
│   └── manager.py                # ContextManager 编排 + LLM 摘要 + 熔断
├── agent/
│   ├── loop.py                   # 改：run() 接入 before_request / record_usage
│   └── events.py                 # 改：新增 AgentEventType.NOTICE
├── conversation.py               # 改：构造 ctx；/context /compact；clear 重置
├── config.py                     # 改：Config.context_window + 解析
└── tui/
    ├── app.py                    # 改：_do_stream 处理 NOTICE
    └── widgets.py                # 改：CommandPanel.COMMANDS 加 /compact /context
tests/
├── test_context_estimate.py      # 新增
├── test_context_offload.py       # 新增
├── test_context_summarize.py     # 新增
└── test_context_manager.py       # 新增（含熔断、锚点失效、假 provider）
docs/c8/                          # 本章文档
```

## 技术决策

| 决策点 | 选择 | 理由 |
|--------|------|------|
| 压缩接入点 | `Agent.run()` 每轮请求前单点调用 `before_request` | 请求在循环内逐轮发生，必须逐轮压；单点接入满足 N1 低侵入 |
| 估算方式 | 上次 `usage.prompt_tokens` 为锚点 + 增量按字符估 | 精确值锚住大头，误差限于少量新增消息，无需 tokenizer 依赖（F2/N3） |
| 锚点在摘要后处理 | 置 `None` 失效，下次全字符估至新 usage | 重构后历史索引全变，旧锚点不再对应；失效后偏高估、安全 |
| offload 只作用 tool 结果 | 跳过 `role="user"` | 用户原文是任务锚点，不可有损压缩（F6/N4） |
| offload 幂等键 | `tool_call_id` 集合 | tool 消息天然带唯一 id，比「内容打标记」健壮（F7） |
| 存盘位置 | `<项目根>/.rhinecode/context/` | 与现有 `.rhinecode/` 一致、可 gitignore、按路径可重读 |
| 存盘不走权限管线 | 内部可信写盘，路径锁定 `.rhinecode/context/` | 避免每次存盘弹确认；非模型可调用工具（N6） |
| 摘要请求形态 | 待摘要段渲染成一条 user 转录文本，`tools=None` | 规避裸 tool 消息缺配对的 API 校验；完全掌控格式 |
| 保留边界回退到 user | `compute_retain_index` snap 到最近 user | 保证重构历史 API 合法、不拆散 tool_calls↔tool |
| 边界消息形态 | 合成一条 assistant 轮 | 兼作 F12 边界提示 + 恢复角色交替，重构后历史合法 |
| 草稿/正文分离 | `SUMMARY_MARKER` 标记，取标记后正文 | 简单稳健；缺标记退化为整段，不因解析失败丢摘要 |
| `/compact` 返回类型 | 事件流生成器（走 Worker） | 摘要是阻塞 LLM 调用，放 UI 线程会卡死；`/context` 只读则返回 str |
| 手动触发阈值 | 余量收窄到 3K 的同一套阈值判断 | 忠实用户「余量 3K」表述；未达阈值报 noop（如需强制压缩可后续调整） |
| 熔断 | 连续 3 次摘要失败置熔断，成功/clear 归零 | 防摘要持续失败时死循环（F15/N2） |
| 阈值常量 vs 配置 | 仅 `context_window` 入配置，其余为模块常量 | 窗口随模型/账号变动最需可调；其余保持简单（YAGNI） |

## spec 覆盖对照

F1→Config.context_window；F2→estimate；F3→before_request 两步顺序；F4/F5→Offloader.run；F6→只动 tool、幂等键；F7→`_offloaded` 集合；F8→before_request/manual 阈值；F9→compute_retain_index；F10→SUMMARY_SYSTEM_PROMPT 固定结构；F11→Prompt 禁工具+草稿标记+parse_summary；F12→reconstruct 边界 assistant；F13→render/reconstruct 保留区原文；F14→/compact 生成器；F15→熔断计数；F16→/context+usage_report；F17→NOTICE 事件。N1→单点接入；N2→各处 try 兜底；N3→估算无依赖；N4→跳过 user；N5→注释规范；N6→内部写盘不走权限。
