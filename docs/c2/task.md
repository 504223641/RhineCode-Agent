# RhineCode MVP 对话基础 Tasks

## 文件清单

| 操作 | 文件 | 职责 |
|------|------|------|
| 新建 | `pyproject.toml` | 依赖声明 |
| 新建 | `config.yaml` | 示例配置文件 |
| 新建 | `rhinecode/__init__.py` | 包入口（空） |
| 新建 | `rhinecode/__main__.py` | CLI 入口 |
| 新建 | `rhinecode/config.py` | YAML 配置加载 |
| 新建 | `rhinecode/conversation.py` | 对话管理器 |
| 新建 | `rhinecode/provider/__init__.py` | 包入口（空） |
| 新建 | `rhinecode/provider/base.py` | 抽象接口、数据类 |
| 新建 | `rhinecode/provider/anthropic.py` | Anthropic 实现 |
| 新建 | `rhinecode/provider/openai.py` | OpenAI 实现 |
| 新建 | `rhinecode/provider/factory.py` | Provider 工厂 |
| 新建 | `rhinecode/tui/__init__.py` | 包入口（空） |
| 新建 | `rhinecode/tui/widgets.py` | TUI 组件 |
| 新建 | `rhinecode/tui/app.py` | Textual App 主类 |

## T1：项目脚手架

**文件：** `pyproject.toml`、`config.yaml`、各 `__init__.py`
**依赖：** 无
**步骤：**
1. 创建 `pyproject.toml`，声明项目名 rhinecode、Python>=3.11、依赖 textual>=0.80.0、anthropic>=0.40.0、openai>=1.50.0、pyyaml>=6.0
2. 创建 `config.yaml` 示例，包含 protocol、model、base_url、api_key 四字段，api_key 填占位符 YOUR_API_KEY
3. 创建 `rhinecode/__init__.py`、`rhinecode/provider/__init__.py`、`rhinecode/tui/__init__.py`（均为空文件）

**验证：** `pip install -e .` 执行成功，无报错

## T2：配置加载

**文件：** `rhinecode/config.py`
**依赖：** T1
**步骤：**
1. 定义 `Config` dataclass，含 protocol、model、base_url、api_key 四个字符串字段
2. 实现 `load(path: str) -> Config`，用 PyYAML 读取文件，逐一校验四字段存在且非空，缺字段时抛 `ValueError` 并在消息中指明缺少哪个字段

**验证：** 用完整 config.yaml 调用 `load()` 返回 Config 对象；删除任意字段后调用抛出含字段名的 ValueError

## T3：Provider 基础类型

**文件：** `rhinecode/provider/base.py`
**依赖：** T1
**步骤：**
1. 定义 `Message` dataclass，含 role（str）、content（str）两个字段
2. 定义 `StreamChunk` dataclass，含 type（str）、content（str）两个字段；type 取值为 "text"、"thinking"、"done"、"error"
3. 定义 `BaseProvider` 抽象类（继承 ABC），声明抽象方法 `stream_chat(self, messages: list[Message], thinking: bool = False) -> Iterator[StreamChunk]`

**验证：** `python -c "from rhinecode.provider.base import BaseProvider, Message, StreamChunk"` 无报错

## T4：Anthropic Provider

**文件：** `rhinecode/provider/anthropic.py`
**依赖：** T3
**步骤：**
1. 实现 `AnthropicProvider(config: Config)`，构造函数用 config.api_key 和 config.base_url 初始化 `anthropic.Anthropic` 客户端
2. 实现 `stream_chat`：构造 messages 列表（将 Message 转为 SDK 格式）；thinking=True 时在请求参数中加入 `thinking={"type": "enabled", "budget_tokens": 8000}`，同时将 temperature 设为 1（Anthropic 要求）
3. 用 `client.messages.stream()` 上下文管理器迭代事件，映射规则：文本增量（text delta）→ StreamChunk(type="text", content=delta)；thinking 块内容 → StreamChunk(type="thinking", content=thinking_text)；流正常结束 → yield StreamChunk(type="done", content="")；捕获所有异常 → yield StreamChunk(type="error", content=str(e))

**验证：** 配置真实 Anthropic api_key 后，在 Python REPL 中实例化并调用 `stream_chat`，打印每个 chunk，能看到 type="text" 的流式输出

## T5：OpenAI Provider

**文件：** `rhinecode/provider/openai.py`
**依赖：** T3
**步骤：**
1. 实现 `OpenAIProvider(config: Config)`，构造函数用 config.api_key 和 config.base_url 初始化 `openai.OpenAI` 客户端
2. 实现 `stream_chat`：thinking 参数忽略；将 Message 列表转为 OpenAI messages 格式；调用 `client.chat.completions.create(model=..., messages=..., stream=True)`
3. 迭代 stream，将每个 delta.content 非空时 yield StreamChunk(type="text", content=delta)；流结束 yield StreamChunk(type="done", content="")；捕获异常 yield StreamChunk(type="error", content=str(e))

**验证：** 配置真实 OpenAI api_key 后，在 Python REPL 中实例化并调用 `stream_chat`，能看到流式文本输出

## T6：Provider 工厂

**文件：** `rhinecode/provider/factory.py`
**依赖：** T4, T5
**步骤：**
1. 实现 `create_provider(config: Config) -> BaseProvider`
2. config.protocol == "anthropic" 返回 AnthropicProvider(config)；config.protocol == "openai" 返回 OpenAIProvider(config)；其他值抛 `ValueError(f"不支持的 protocol: {config.protocol}")`

**验证：** 传入 protocol="anthropic" 返回 AnthropicProvider 实例；传入 protocol="openai" 返回 OpenAIProvider 实例；传入未知值抛 ValueError

## T7：对话管理器

**文件：** `rhinecode/conversation.py`
**依赖：** T6
**步骤：**
1. 实现 `ConversationManager(provider: BaseProvider, provider_protocol: str)`，初始化 `history: list[Message] = []`、`thinking_enabled: bool = False`，保存 provider_protocol 用于判断是否支持 thinking
2. 实现 `clear()`：将 history 清空为空列表
3. 实现 `handle_input(text: str)`，返回值为 `str | Iterator[StreamChunk]`：
   - 输入为 "/exit" → 抛 `SystemExit`
   - 输入为 "/clear" → 调用 clear()，返回字符串 "对话历史已清空"
   - 输入为 "/think" → 若 provider_protocol != "anthropic" 返回字符串 "当前 Provider 不支持 Extended Thinking"；否则切换 thinking_enabled，返回字符串 "思考模式：开启" 或 "思考模式：关闭"
   - 其他 → 将 Message(role="user", content=text) 追加到 history，调用 provider.stream_chat(history, thinking_enabled)，将返回的生成器直接返回（assistant 回复完整后再追加到 history，由 TUI 层完成）

**验证：** 用 Mock provider 测试：/clear 后 history 为空；/think 在 anthropic 模式下切换状态；/think 在 openai 模式下返回不支持提示；普通输入后 history 长度加一

## T8：TUI 组件

**文件：** `rhinecode/tui/widgets.py`
**依赖：** T1
**步骤：**
1. 实现 `HistoryView`（继承 Textual `RichLog`）：添加 `append_user(text: str)` 方法（蓝色前缀 "You: "）、`append_assistant_chunk(text: str)` 方法（追加到当前行）、`append_system(text: str)` 方法（灰色系统提示）、`begin_assistant_turn()` 方法（换行并打印 "AI: " 前缀）
2. 实现 `InputBar`（继承 Textual `Input`）：定义内部消息类 `InputSubmitted(Message)`，含 text 字段；重写 `on_input_submitted` 在非空输入时 post 该消息并清空输入框
3. 实现 `StatusBar`（继承 Textual `Static`）：实现 `update_status(provider: str, model: str, thinking: bool)` 方法，刷新显示文本为 `[provider] {model} | 思考模式：{'开启' if thinking else '关闭'}`

**验证：** `python -c "from rhinecode.tui.widgets import HistoryView, InputBar, StatusBar"` 无报错

## T9：Textual App 主类

**文件：** `rhinecode/tui/app.py`
**依赖：** T7, T8
**步骤：**
1. 实现 `RhineApp(manager: ConversationManager, config)`，继承 Textual `App`，CSS 中 HistoryView 设 `height: 1fr`，InputBar 和 StatusBar 固定在底部
2. `compose()` 方法 yield HistoryView、InputBar、StatusBar
3. 监听 `InputBar.InputSubmitted` 事件：调用 `manager.handle_input(event.text)`；若返回 str 则调用 `history_view.append_system()`；若返回生成器则启动 Textual Worker 运行流式处理
4. Worker 函数中迭代 StreamChunk：type="text" 时调用 `call_from_thread(history_view.append_assistant_chunk, chunk.content)`；type="thinking" 时追加灰色思考内容；type="error" 时追加红色错误信息；type="done" 时将完整回复追加到 manager.history
5. 提交输入前调用 `history_view.append_user(text)` 和 `history_view.begin_assistant_turn()`
6. `on_mount` 时调用 `status_bar.update_status()` 初始化；/think 命令返回后刷新状态栏
7. 捕获 `SystemExit` 调用 `self.exit()`；绑定 Ctrl+C 到退出

**验证：** `python -m rhinecode --config config.yaml` 启动后，三个区域正常渲染，状态栏显示模型名和 Provider 名

## T10：CLI 入口

**文件：** `rhinecode/__main__.py`
**依赖：** T2, T6, T7, T9
**步骤：**
1. 用 `argparse` 解析 `--config` 参数，默认值为 `"config.yaml"`
2. 调用 `config.load(args.config)`，捕获 `ValueError` 和 `FileNotFoundError`，打印错误信息后 `sys.exit(1)`
3. 调用 `create_provider(cfg)` 创建 provider，创建 `ConversationManager(provider, cfg.protocol)`，创建 `RhineApp(manager, cfg)` 并调用 `.run()`

**验证：** `python -m rhinecode --config config.yaml` 启动 TUI 无报错；`python -m rhinecode --config 不存在.yaml` 打印错误后退出

## 执行顺序

```
T1 → T2
T1 → T3 → T4 ──→ T6 → T7 ──→ T9 → T10
              T5 ──↗         ↗
T1 ──────────────→ T8 ───────↗
```
