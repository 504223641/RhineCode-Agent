# RhineCode MVP 对话基础 Plan

## 架构概览

整体分为四层：

```
┌─────────────────────────────┐
│         TUI 层              │  Textual 框架，三面板布局，处理用户输入和流式渲染
├─────────────────────────────┤
│       对话管理层             │  维护消息历史，解析斜杠命令，协调 TUI 与 Provider
├─────────────────────────────┤
│      Provider 抽象层         │  统一接口定义，工厂函数按配置选择实现
├─────────────────────────────┤
│   Anthropic / OpenAI 实现   │  各自处理协议差异、SSE 流式解析、Extended Thinking
└─────────────────────────────┘
```

配置层横切所有层：启动时读取 YAML，传入对话管理层和 Provider 层。

## 核心数据结构

### Config
```python
class Config:
    protocol: str       # "anthropic" | "openai"
    model: str
    base_url: str
    api_key: str
```

### Message
```python
class Message:
    role: str           # "user" | "assistant"
    content: str
```

### StreamChunk
```python
class StreamChunk:
    type: str           # "text" | "thinking" | "done" | "error"
    content: str
```

### BaseProvider（抽象接口）
```python
class BaseProvider(ABC):
    def stream_chat(
        self,
        messages: list[Message],
        thinking: bool = False
    ) -> Iterator[StreamChunk]: ...
```

### ConversationManager
```python
class ConversationManager:
    history: list[Message]
    thinking_enabled: bool

    def handle_input(self, text: str) -> None: ...
    def clear(self) -> None: ...
```

### 工厂函数
```python
def create_provider(config: Config) -> BaseProvider: ...
```

## 模块设计

### `__main__`
**职责：** 解析 CLI 参数（`--config`），加载配置，创建 Provider 和 ConversationManager，启动 TUI
**对外接口：** 命令行入口 `python -m rhinecode`
**依赖：** config, provider.factory, conversation, tui.app

### `config`
**职责：** 读取 YAML 文件，校验四个核心字段，返回 Config 对象；缺字段时抛出可读异常
**对外接口：** `load(path: str) -> Config`
**依赖：** 无

### `provider.base`
**职责：** 定义 BaseProvider 抽象类、Message、StreamChunk 数据类
**对外接口：** 供所有 provider 实现和上层调用
**依赖：** 无

### `provider.anthropic`
**职责：** 用官方 anthropic SDK 实现流式对话；当 thinking=True 时启用 Extended Thinking；将 SDK 事件转换为 StreamChunk
**对外接口：** `AnthropicProvider(config: Config)` 实现 BaseProvider
**依赖：** provider.base, anthropic SDK

### `provider.openai`
**职责：** 用官方 openai SDK 实现流式对话；将 SDK 事件转换为 StreamChunk；忽略 thinking 参数
**对外接口：** `OpenAIProvider(config: Config)` 实现 BaseProvider
**依赖：** provider.base, openai SDK

### `provider.factory`
**职责：** 根据 config.protocol 返回对应的 Provider 实例；未知 protocol 抛出可读异常
**对外接口：** `create_provider(config: Config) -> BaseProvider`
**依赖：** provider.anthropic, provider.openai

### `conversation`
**职责：** 维护 history 列表和 thinking_enabled 状态；识别并处理 `/think`、`/clear`、`/exit` 斜杠命令；普通消息追加到 history 并调用 provider.stream_chat()
**对外接口：** `ConversationManager(provider: BaseProvider)`
**依赖：** provider.base

### `tui.app`
**职责：** Textual App 主类；组合三个面板；在 Worker 中运行流式请求；接收 StreamChunk 推送到历史区；响应 ConversationManager 的命令结果更新状态栏
**对外接口：** `RhineApp(manager: ConversationManager)`
**依赖：** tui.widgets, conversation

### `tui.widgets`
**职责：** 定义三个自定义 Widget——HistoryView（RichLog 可滚动）、InputBar（单行输入框）、StatusBar（显示模型/Provider/思考状态）
**对外接口：** 供 tui.app 组合使用
**依赖：** Textual

## 模块交互

```
__main__
  → config.load()         → Config
  → create_provider()     → BaseProvider
  → ConversationManager(provider)
  → RhineApp(manager).run()

用户输入 → RhineApp → manager.handle_input()
  → 斜杠命令：直接处理，通知 TUI 更新状态栏/历史区
  → 普通消息：provider.stream_chat() → Iterator[StreamChunk]
             → TUI Worker 逐块渲染到历史区
```

## 文件组织

```
rhinecode/
├── __main__.py
├── config.py
├── conversation.py
├── provider/
│   ├── __init__.py
│   ├── base.py
│   ├── anthropic.py
│   ├── openai.py
│   └── factory.py
└── tui/
    ├── __init__.py
    ├── app.py
    └── widgets.py

config.yaml          # 用户配置文件（不入库）
pyproject.toml       # 依赖声明
```

## 技术决策

| 决策点 | 选择 | 理由 |
|--------|------|------|
| TUI 框架 | Textual | 原生支持多面板布局、异步事件、可滚动组件，最接近 Claude Code 体验 |
| 流式渲染 | Textual Worker + 异步生成器 | 在后台线程运行 SSE，通过消息机制推送到 UI，避免阻塞主线程 |
| Anthropic SDK | 官方 `anthropic` Python SDK | 原生支持流式和 Extended Thinking，无需手写 HTTP |
| OpenAI SDK | 官方 `openai` Python SDK | 原生支持流式，与 Anthropic SDK 风格对称 |
| 配置解析 | PyYAML | 轻量，无需 schema 校验库，手动校验四个核心字段即可 |
| Extended Thinking 状态 | ConversationManager 持有 thinking_enabled | 状态集中在对话层，TUI 只负责渲染，职责清晰 |
| 斜杠命令解析 | 字符串前缀匹配 | 本阶段命令少（3 个），无需引入命令框架 |
| 依赖管理 | pyproject.toml + pip | 标准工具链，无额外复杂度 |

**依赖清单：**
```toml
[project]
dependencies = [
    "textual>=0.80.0",
    "anthropic>=0.40.0",
    "openai>=1.50.0",
    "pyyaml>=6.0",
]
```
