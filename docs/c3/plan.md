# RhineCode 工具系统 Plan

## 架构概览

在现有三层之上**新增工具层**，并扩展协调层与 Provider 层：

```
┌──────────────────────────────────────────────┐
│ TUI 层 (tui/)                                  │  +工具调用展示(橘/绿/红)、实时计时、Yes/No 确认弹窗
├──────────────────────────────────────────────┤
│ 协调层 (conversation.py)                        │  +工具编排：两轮往返、并发/串行执行、确认回调、结果回灌
├──────────────────────────────────────────────┤
│ Provider 层 (provider/)                         │  +DeepSeek tool_calls 流式解析与回灌；接口加 tools 参数
├──────────────────────────────────────────────┤
│ 工具层 (tools/)  ★新增                          │  Tool 抽象 + 6 个工具 + 注册中心
└──────────────────────────────────────────────┘
```

工具层完全独立、不依赖上层；协调层持有注册中心与确认回调；只有 DeepSeek Provider 真正使用 `tools`，其余 Provider 接口兼容但忽略。

## 核心数据结构

### ToolResult（`tools/base.py`）
```python
@dataclass
class ToolResult:
    ok: bool          # True 成功 / False 失败（决定 TUI 绿/红）
    output: str       # 回灌给模型的文本：成功内容或可读错误描述
```

### Tool（`tools/base.py`）
```python
class Tool(ABC):
    name: str                  # 工具名（API 函数名）
    description: str           # 面向模型的用途描述
    parameters: dict           # 参数 JSON Schema（type=object）
    read_only: bool = True     # True 只读(免确认/可并发) / False 有副作用(需确认/串行)
    @abstractmethod
    def execute(self, args: dict) -> ToolResult: ...
    def to_schema(self) -> dict   # → {"type":"function","function":{name,description,parameters}}
```

### ToolCall（`provider/base.py`）
```python
@dataclass
class ToolCall:               # 一次完整的工具调用（流式拼接后）
    id: str                   # DeepSeek 返回的调用 id（回灌时用）
    name: str                 # 工具名
    arguments: dict | None    # 解析后的参数；JSON 解析失败为 None
```

### Message（`provider/base.py`，扩展）
```python
@dataclass
class Message:                # 扩展：支持工具消息
    role: str                                   # user | assistant | tool
    content: str = ""
    tool_calls: list[ToolCall] | None = None    # assistant 发起的调用
    tool_call_id: str | None = None             # role=tool 时对应的调用 id
```

### StreamChunk（`provider/base.py`，扩展）
```python
@dataclass
class StreamChunk:            # 扩展：新增三种 type 与两个载荷字段
    type: str   # text|thinking|done|error|tool_call|tool_start|tool_result
    content: str = ""
    tool_call: ToolCall | None = None       # tool_call/tool_start/tool_result 携带
    tool_result: ToolResult | None = None   # tool_result 携带
```

`StreamChunk.type` 新增三种语义：
- `tool_call`：Provider 第一轮流结束后，每个解析完成的工具调用产出一个（协调层内部消费收集，不直接渲染）
- `tool_start`：协调层在某工具**开始执行**时产出 → TUI 起橘色行 + 计时器
- `tool_result`：协调层在某工具**执行完成**时产出 → TUI 转绿（成功）/红（失败）+ 结果摘要

### ToolRegistry（`tools/registry.py`）
```python
class ToolRegistry:
    def register(self, tool: Tool) -> None
    def get(self, name: str) -> Tool | None
    def schemas(self) -> list[dict]            # 全部工具的 API 描述列表
    @classmethod
    def default(cls) -> "ToolRegistry"         # 注册 6 个核心工具
```

### 确认回调类型
`ConfirmCallback = Callable[[ToolCall, Tool], bool]`（协调层调用、TUI 实现，返回 Yes=True/No=False）。

## 模块设计

### 工具层（`tools/`，6 个工具）

每个工具继承 `Tool`，声明 `name/description/parameters/read_only`，实现 `execute(args)->ToolResult`。路径一律以**项目工作目录**（`os.getcwd()`）为基准解析相对路径。

| 工具文件 | name | read_only | 参数 | execute 行为 |
|---|---|---|---|---|
| `read_file.py` | `read_file` | True | `path` | 读取文本返回内容；不存在/是目录/解码失败 → `ToolResult(ok=False, ...)` |
| `write_file.py` | `write_file` | **False** | `path`, `content` | 父目录不存在则 `makedirs`；写入（覆盖/新建）；返回写入字节数摘要 |
| `edit_file.py` | `edit_file` | **False** | `path`, `old_string`, `new_string` | 读文件→统计 `old_string` 出现次数：0 次/≥2 次 → 失败并说明次数；恰好 1 次→替换写回 |
| `run_command.py` | `run_command` | **False** | `command`（可选 `timeout`） | `subprocess.run(shell=True, cwd=工作目录, timeout=默认30s, capture_output)`；返回 stdout/stderr/returncode 拼接；超时→结构化超时错误 |
| `glob_files.py` | `glob_files` | True | `pattern` | `pathlib.Path().glob/rglob` 匹配；返回路径列表（无匹配=空列表，仍 `ok=True`） |
| `grep_content.py` | `grep_content` | True | `pattern`（可选 `path`，默认 `.`） | 遍历目标下文本文件、按正则逐行匹配；返回 `文件:行号:行内容` 列表（结果上限截断，无匹配=空，`ok=True`） |

**统一约定**：`execute` 内部捕获所有异常 → 转 `ToolResult(ok=False, output="错误描述")`，绝不抛出（满足 N2/F13）。命令超时常量与 grep 结果上限定义为模块级常量。

### 注册中心（`tools/registry.py`）

- 内部 `dict[name -> Tool]`；`register` 重名覆盖并可校验。
- `schemas()` 遍历调用各工具 `to_schema()`，产出 DeepSeek/OpenAI `tools` 列表。
- `default()` 实例化并注册上述 6 个工具，供 `__main__` 构建。

### Provider 层

**`provider/base.py`**：`stream_chat` 签名增加 `tools: list[dict] | None = None`（默认 None，向后兼容）。

**`provider/deepseek.py`（核心改造）**：
- **请求构造**：`_to_sdk_messages(messages)` 处理三类 Message →
  - `user`：`{"role":"user","content":...}`
  - `assistant` 带 `tool_calls`：`{"role":"assistant","content":content or "", "tool_calls":[{"id","type":"function","function":{"name","arguments": json.dumps(args)}}]}`（arguments 回灌时序列化回 **JSON 字符串**）
  - `tool`：`{"role":"tool","tool_call_id":..., "content":...}`
  - `tools` 非空时传入 `tools=tools`（思考模式 extra_body 逻辑保留）。
- **流式解析**：迭代 chunk 时除 `reasoning_content`/`content` 外，读取 `delta.tool_calls`，按 `tc.index` 累积：首片含 `id` 与 `function.name`，后续片追加 `function.arguments` 字符串碎片。
- **流结束**：对每个累积的调用 `json.loads(arguments)`（失败→`arguments=None`），产出 `StreamChunk(type="tool_call", tool_call=ToolCall(id,name,args))`；最后产出 `done`。

**`provider/openai.py`、`provider/anthropic.py`**：仅在 `stream_chat` 增加并忽略 `tools` 参数（保持接口一致；本章不实现工具）。

### 协调层（`conversation.py`，工具编排核心）

`ConversationManager` 新增：`registry: ToolRegistry | None`、`confirm_callback: ConfirmCallback | None`、`_tools_enabled = (protocol == "deepseek" and registry is not None)`。

`_stream(messages)` 改为两轮往返编排：

```
# 第一轮：带 tools 请求
tools = registry.schemas() if _tools_enabled else None
text_buf, tool_calls = [], []
for chunk in provider.stream_chat(messages, effort, tools=tools):
    text  → 累积 text_buf；yield
    thinking → yield
    tool_call → tool_calls.append(chunk.tool_call)   # 内部收集，不 yield
    error → yield；return
    done  → break

if not tool_calls:               # 纯对话，保持原行为
    if text_buf: history.append(assistant text); yield done; return

# 有工具调用：先把 assistant(含 tool_calls) 追加历史
history.append(Message(role="assistant", content="".join(text_buf), tool_calls=tool_calls))

# 执行（见下）：yield tool_start/tool_result；收集 id→ToolResult
results = {}
yield from self._execute(tool_calls, results)

# 按原顺序把 tool 结果回灌历史
for tc in tool_calls:
    history.append(Message(role="tool", tool_call_id=tc.id, content=results[tc.id].output))

# 第二轮：自动再请求，产出最终文本（仍传 tools 但忽略其再次发起的 tool_call，不再循环）
text2 = []
for chunk in provider.stream_chat(list(history), effort, tools=tools):
    text → 累积 text2；yield
    thinking → yield
    tool_call → 忽略（不执行，满足"不做跨轮循环"）
    error → yield；break
    done → break
if text2: history.append(assistant text2)
yield done
```

`_execute(tool_calls, results)`（生成器，yield UI chunk、把结果写入 `results` 字典）：
- 按 `tool.read_only` 把调用分成**只读组**与**副作用组**。
- **只读组**：对每个先 `yield tool_start`；用 `ThreadPoolExecutor` 并发执行；用 `as_completed` 收集，每个完成 `yield tool_result` 并写入 `results`。
- **副作用组（串行）**：逐个——
  1. `arguments is None` → 直接构造"参数 JSON 解析失败"的 `ToolResult`（不执行）；
  2. 否则调 `confirm_callback(tc, tool)`：返回 False → `ToolResult(ok=False, output="用户拒绝执行该工具")`；返回 True → `yield tool_start` 后执行得到 `ToolResult`；
  3. `yield tool_result` 并写入 `results`。
- 工具名在注册中心查不到 → `ToolResult(ok=False, output="未知工具: name")`。

> 计时由 TUI 的主线程定时器驱动（见下文），协调层只负责在执行前后发 `tool_start`/`tool_result` 两个边界信号，不参与计时。

## 模块交互

**整体调用链（含工具往返）：**
```
用户输入 → RhineApp.on_input_bar_input_submitted
  → manager.handle_input() → _stream() 生成器
  → run_worker(thread=True) 在 Worker 线程消费 → _do_stream()
        ├ text/thinking → 现有渲染
        ├ tool_start    → call_from_thread 新建工具行(橘色)+启动主线程计时器
        ├ tool_result   → call_from_thread 停计时器, 转绿/红 + 结果摘要
        ├ error/done    → 现有处理
  （确认）_execute 内部 → manager.confirm_callback(tc, tool)
        → RhineApp._confirm_tool（Worker 线程）
        → call_from_thread(push_screen_wait, ConfirmScreen) 阻塞等待用户 → 返回 bool
```

**确认交互（满足 N7 不死锁）**：`_execute` 在 Worker 线程调用 `confirm_callback`；TUI 的 `_confirm_tool` 用 `self.call_from_thread(self.push_screen_wait, ConfirmScreen(tc, tool))` 把模态推到主线程并阻塞 Worker 直到用户选择，返回 True/False。主线程事件循环照常运行，不死锁。

**实时计时（满足 F14/N3）**：计时不依赖 Worker。`tool_start` 时新建 `ToolCallWidget` 并在其 `on_mount` 用 `set_interval(1, self._tick)` 启动**主线程**秒级定时器，每秒刷新"执行中… Ns"（橘色）；Worker 此刻可能阻塞在工具执行/并发等待中，互不影响。`tool_result` 时调 `widget.finish(ok, summary)` 停止定时器并定色（成功绿 `●`/失败红 `●`），附最终耗时与结果摘要。工具行用 `tool_call.id` 关联，支持并发时多行各自计时。

**TUI 组件新增（`tui/widgets.py`）**：
- `ToolCallWidget(Static)`：自管理计时器的工具行；方法 `finish(ok, summary)`；橘色执行中 / 绿成功 / 红失败三态。
- `HistoryView.add_tool_widget(tool_call) -> ToolCallWidget`：挂载工具行并返回引用。
- `ConfirmScreen(ModalScreen[bool])`：展示工具名+关键参数与 Yes/No；`y`/`Enter`→`dismiss(True)`，`n`/`Esc`→`dismiss(False)`。

**装配（`__main__.py`）**：构建 `ToolRegistry.default()` 传入 `ConversationManager(provider, protocol, registry)`；`RhineApp.on_mount` 把 `manager.confirm_callback = self._confirm_tool`。

## 文件组织
```
rhinecode/
├── __main__.py            — 修改：构建 registry 注入 manager
├── conversation.py        — 修改：两轮往返编排、_execute 并发/串行、确认回调
├── tools/                 — ★新增
│   ├── __init__.py
│   ├── base.py            — Tool、ToolResult
│   ├── registry.py        — ToolRegistry（含 default()）
│   ├── read_file.py       — ReadFileTool
│   ├── write_file.py      — WriteFileTool
│   ├── edit_file.py       — EditFileTool
│   ├── run_command.py     — RunCommandTool（超时常量）
│   ├── glob_files.py      — GlobTool
│   └── grep_content.py    — GrepTool（结果上限常量）
├── provider/
│   ├── base.py            — 修改：ToolCall、Message/StreamChunk 扩展、stream_chat 加 tools
│   ├── deepseek.py        — 修改：tools 透传、消息转换、流式 tool_calls 解析
│   ├── openai.py          — 修改：stream_chat 加 tools（忽略）
│   └── anthropic.py       — 修改：stream_chat 加 tools（忽略）
└── tui/
    ├── app.py             — 修改：tool_start/tool_result 处理、_confirm_tool
    └── widgets.py         — 修改：ToolCallWidget、add_tool_widget、ConfirmScreen
```

## 技术决策

| 决策点 | 选择 | 理由 |
|---|---|---|
| 工具元信息载体 | 类属性 + `to_schema()` | 工具自描述，注册中心零耦合批量导出 API 列表 |
| 只读/副作用区分 | `Tool.read_only` 布尔 | 一处声明同时决定"是否确认"与"能否并发"，简单可靠 |
| 并发实现 | 只读组 `ThreadPoolExecutor`，副作用组串行 | 串行而非加锁即可杜绝写冲突（符合"不做细粒度依赖分析"） |
| 工具调用信令 | 扩展 `StreamChunk` 新增 3 type | 复用现有 Worker+`call_from_thread` 流式管道，不引入新机制 |
| 确认交互 | `ConfirmCallback` + `push_screen_wait` | 协调层不感知 TUI；模态阻塞 Worker 而不阻塞主线程 |
| 实时计时 | 主线程 `set_interval` 自驱动 | 与 Worker 的执行阻塞解耦，并发多行各自计时 |
| arguments 解析失败 | `ToolCall.arguments=None`→结构化错误回灌 | 不崩溃、让模型重试（符合 F13/N2，呼应 DeepSeek 文档"需校验"提示） |
| 命令超时 | `subprocess.run(timeout=)` 模块常量(默认 30s) | 标准库即可，超时转结构化错误（N1） |
| 工具仅 DeepSeek 启用 | 协调层 `_tools_enabled` 按 protocol 门控 | 其余 Provider 接口兼容、行为不变（YAGNI） |
| 历史中的工具消息 | `Message` 扩展 `tool_calls`/`tool_call_id` | 复用现有 history 列表，回灌格式由 DeepSeek Provider 序列化 |
