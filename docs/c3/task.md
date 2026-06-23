# RhineCode 工具系统 Tasks

## 文件清单

| 操作 | 文件 | 职责 |
|---|---|---|
| 新建 | `rhinecode/tools/__init__.py` | 工具包标识 |
| 新建 | `rhinecode/tools/base.py` | `Tool` 抽象、`ToolResult` |
| 新建 | `rhinecode/tools/read_file.py` | `ReadFileTool` |
| 新建 | `rhinecode/tools/write_file.py` | `WriteFileTool` |
| 新建 | `rhinecode/tools/edit_file.py` | `EditFileTool` |
| 新建 | `rhinecode/tools/run_command.py` | `RunCommandTool` |
| 新建 | `rhinecode/tools/glob_files.py` | `GlobTool` |
| 新建 | `rhinecode/tools/grep_content.py` | `GrepTool` |
| 新建 | `rhinecode/tools/registry.py` | `ToolRegistry` + `default()` |
| 修改 | `rhinecode/provider/base.py` | `ToolCall`、`Message`/`StreamChunk` 扩展、`stream_chat` 加 `tools` |
| 修改 | `rhinecode/provider/openai.py` | `stream_chat` 加 `tools`（忽略） |
| 修改 | `rhinecode/provider/anthropic.py` | `stream_chat` 加 `tools`（忽略） |
| 修改 | `rhinecode/provider/deepseek.py` | tools 透传、消息转换、流式 `tool_calls` 解析 |
| 修改 | `rhinecode/conversation.py` | 两轮往返编排、`_execute` 并发/串行、确认回调 |
| 修改 | `rhinecode/tui/widgets.py` | `ToolCallWidget`、`add_tool_widget`、`ConfirmScreen` |
| 修改 | `rhinecode/tui/app.py` | `tool_start/tool_result` 处理、计时、`_confirm_tool` |
| 修改 | `rhinecode/__main__.py` | 构建 registry 注入 manager |

## T1: 扩展 Provider 抽象（数据结构基座）
**文件：** `rhinecode/provider/base.py` | **依赖：** 无
**步骤：**
1. 新增 `ToolCall` dataclass（`id: str`、`name: str`、`arguments: dict | None`）。
2. `Message` 增加 `tool_calls: list[ToolCall] | None = None`、`tool_call_id: str | None = None`（`content` 给默认值 `""`）。
3. `StreamChunk` 的 `content` 给默认 `""`，新增 `tool_call: ToolCall | None = None`、`tool_result` 字段（类型用字符串前向引用或 `Any`，避免与 tools 包循环导入——见注释说明）。
4. `BaseProvider.stream_chat` 抽象签名增加 `tools: list[dict] | None = None`。

**验证：** `python -c "from rhinecode.provider.base import ToolCall, Message, StreamChunk; print(StreamChunk(type='text'))"` 无报错。

## T2: Tool 抽象与结果类型
**文件：** `rhinecode/tools/base.py`、`rhinecode/tools/__init__.py` | **依赖：** 无
**步骤：**
1. 定义 `ToolResult` dataclass（`ok: bool`、`output: str`）。
2. 定义 `Tool(ABC)`：类属性 `name/description/parameters/read_only=True`，抽象 `execute(self, args: dict) -> ToolResult`，具体方法 `to_schema()` 返回 `{"type":"function","function":{"name","description","parameters"}}`。
3. 创建空 `tools/__init__.py`。

**验证：** 写临时脚本定义一个最小子类并调用 `to_schema()`，打印结构正确。

## T3: 读/写/改 文件工具
**文件：** `read_file.py`、`write_file.py`、`edit_file.py` | **依赖：** T2
**步骤：**
1. `ReadFileTool`（read_only=True）：解析路径→不存在/目录/解码错误返回 `ok=False`，否则返回内容。
2. `WriteFileTool`（read_only=False）：父目录缺失则 `os.makedirs`，写入文件，返回写入字节摘要。
3. `EditFileTool`（read_only=False）：读文件→`count(old_string)`；0 或 ≥2 返回带次数的错误且不改文件；==1 替换写回。
4. 三者 `execute` 全程 try/except 兜底为 `ToolResult(ok=False)`。

**验证：** 临时脚本在 scratchpad 建文件→write→read 内容一致；edit 唯一匹配成功、重复匹配报错且文件不变。

## T4: 命令/查找/搜索 工具
**文件：** `run_command.py`、`glob_files.py`、`grep_content.py` | **依赖：** T2
**步骤：**
1. `RunCommandTool`（read_only=False）：模块常量 `DEFAULT_TIMEOUT=30`；`subprocess.run(shell=True, cwd=os.getcwd(), capture_output=True, text=True, timeout=...)`；拼接 stdout/stderr/returncode；`TimeoutExpired`→结构化超时错误。
2. `GlobTool`（read_only=True）：用 `pathlib` 按 `pattern` 匹配，返回路径列表（无匹配=空、`ok=True`）。
3. `GrepTool`（read_only=True）：模块常量结果上限；遍历 `path`（默认 `.`）下文本文件逐行正则匹配，返回 `文件:行号:行内容`（无匹配=空、`ok=True`）。

**验证：** 临时脚本：run `echo hi` 拿到输出与退出码 0；glob `*.py` 命中文件；grep 已知字符串命中、不存在字符串返回空。

## T5: 工具注册中心
**文件：** `rhinecode/tools/registry.py` | **依赖：** T2, T3, T4
**步骤：**
1. `ToolRegistry`：`register`、`get(name)`、`schemas()`（聚合各工具 `to_schema()`）。
2. `default()` 注册 6 个工具并返回实例。

**验证：** `python -c "from rhinecode.tools.registry import ToolRegistry; r=ToolRegistry.default(); print(len(r.schemas()), r.get('read_file').name)"` 输出 `6 read_file`。

## T6: OpenAI / Anthropic 接口兼容
**文件：** `provider/openai.py`、`provider/anthropic.py` | **依赖：** T1
**步骤：** 两者 `stream_chat` 增加 `tools: list[dict] | None = None` 参数并在 docstring 注明忽略，逻辑不变。

**验证：** `python -c "import rhinecode.provider.openai, rhinecode.provider.anthropic"` 无报错。

## T7: DeepSeek 工具支持
**文件：** `rhinecode/provider/deepseek.py` | **依赖：** T1
**步骤：**
1. 抽出 `_to_sdk_messages(messages)`：处理 user / assistant(含 tool_calls，arguments `json.dumps` 回字符串) / tool(`tool_call_id`) 三类。
2. `stream_chat` 增加 `tools`，非空时传 `tools=tools`。
3. 流式循环累积 `delta.tool_calls`（按 `index` 拼 `id`/`name`/`arguments` 碎片）。
4. 流结束：每个调用 `json.loads(arguments)`（失败→`None`）→ `yield StreamChunk(type="tool_call", tool_call=...)`，最后 `done`；异常→`error`。

**验证：** 临时脚本调用 `_to_sdk_messages` 传入含 tool_calls 的 assistant 与 tool 消息，断言输出 dict 结构与 arguments 为 JSON 字符串；`python -c "import rhinecode.provider.deepseek"` 通过。

## T8: 协调层工具编排
**文件：** `rhinecode/conversation.py` | **依赖：** T1, T5, T7
**步骤：**
1. `__init__` 增加 `registry=None`、`confirm_callback=None`、`_tools_enabled`。
2. `_stream` 改两轮往返：收集第一轮 `tool_call`；无则按原行为返回；有则追加 assistant(tool_calls)→`_execute`→回灌 tool 消息→第二轮产出最终文本（忽略其再发的 tool_call）。
3. `_execute(tool_calls, results)`：按 `read_only` 分组；只读组 `ThreadPoolExecutor` 并发+`as_completed`；副作用组串行（None 参数错误 / confirm False 拒绝 / confirm True 执行）；各发 `tool_start`/`tool_result`，写入 `results`；未知工具结构化错误。

**验证：** 临时脚本用「假 Provider」（先吐一个 tool_call 再吐最终文本）+ `ToolRegistry.default()` + `confirm_callback=lambda *a: True`，跑 `handle_input`，断言：收到 `tool_start`/`tool_result`、history 末尾为最终 assistant 文本、含一条 role=tool 消息。

## T9: TUI 工具组件
**文件：** `rhinecode/tui/widgets.py` | **依赖：** T1
**步骤：**
1. `ToolCallWidget(Static)`：保存 tool_call、`on_mount` 起 `set_interval(1, self._tick)` 显示橘色"执行中… Ns"；`finish(ok, summary)` 停 timer、定色（绿●/红●）+ 耗时+摘要。
2. `HistoryView.add_tool_widget(tool_call)`：挂载并返回 `ToolCallWidget`。
3. `ConfirmScreen(ModalScreen[bool])`：展示工具名+关键参数；`y/enter`→`dismiss(True)`，`n/escape`→`dismiss(False)`。

**验证：** `python -c "import rhinecode.tui.widgets"` 无报错（导入级语法/引用检查）。

## T10: TUI 流式接入工具
**文件：** `rhinecode/tui/app.py` | **依赖：** T1, T8, T9
**步骤：**
1. `_do_stream` 增加分支：`tool_start`→`call_from_thread(history_view.add_tool_widget, tc)` 存 `id->widget`；`tool_result`→`call_from_thread(widget.finish, ok, summary)`。
2. 新增 `_confirm_tool(tc, tool)`：`return self.call_from_thread(self.push_screen_wait, ConfirmScreen(tc, tool))`。
3. `on_mount` 设置 `self._manager.confirm_callback = self._confirm_tool`。

**验证：** `python -c "import rhinecode.tui.app"` 无报错。

## T11: 入口装配
**文件：** `rhinecode/__main__.py` | **依赖：** T5, T8, T10
**步骤：** 构建 `ToolRegistry.default()`，`ConversationManager(provider, cfg.protocol, registry)`。

**验证：** `python -m rhinecode --config nonexist.yaml` 输出友好配置错误（证明导入链完整、入口可跑）。

## 执行顺序
```
T1 ─┬─ T6 ──────────────┐
    ├─ T7 ──────────┐   │
    └─ T9 ───────┐  │   │
T2 ─┬─ T3 ─┐     │  │   │
    └─ T4 ─┴─ T5 ┴──┴─ T8 ─ T10 ─ T11
```
