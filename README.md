# RhineCode

RhineCode 是一个终端 AI 编程助手，使用 Python + Textual 实现，交互体验参考 Claude Code。

启动后进入多面板 TUI，可与 Anthropic Claude、OpenAI、DeepSeek 进行多轮流式对话。当前版本已支持 DeepSeek 工具调用：模型可以读取/搜索项目文件、写入或编辑文件、运行命令，并把工具结果回灌后自动生成最终回答。

## 功能

- **流式输出**：SSE 流式渲染，AI 回复逐块显示，界面不被网络请求阻塞
- **多轮对话**：自动维护对话历史，让模型持续获得上下文
- **多后端支持**：通过配置切换 Anthropic、OpenAI、DeepSeek
- **思考模式**：`/think` 在 Anthropic / DeepSeek 下切换思考强度（off / high / max）
- **DeepSeek 工具系统**：支持读文件、写文件、精确改文件、运行命令、glob 找文件、grep 搜内容
- **执行前确认**：写文件、改文件、运行命令前会在 TUI 内联弹出确认面板
- **路径安全边界**：文件类工具只能访问项目工作目录内的路径，拒绝 `..`、外部绝对路径和越界符号链接
- **Textual TUI**：历史区、命令提示、工具确认、输入框、状态栏组合成终端交互界面

> 注意：工具调用目前仅在 `protocol: deepseek` 时启用；Anthropic 和 OpenAI Provider 仍保持纯对话能力。

## 安装

**要求：** Python 3.11+

```bash
git clone <repo-url>
cd RhineCode-Agent
pip install -e .
```

## 配置

复制示例配置文件并填入 API Key：

```bash
cp config.example.yaml config.yaml
```

编辑 `config.yaml`：

```yaml
# Anthropic Claude
# protocol: anthropic
# model: claude-sonnet-4-6
# base_url: https://api.anthropic.com
# api_key: sk-ant-...

# OpenAI
# protocol: openai
# model: gpt-4o
# base_url: https://api.openai.com/v1
# api_key: sk-...

# DeepSeek（默认示例，支持工具调用）
protocol: deepseek
model: deepseek-chat
base_url: https://api.deepseek.com
api_key: YOUR_API_KEY
```

| 字段 | 说明 |
|------|------|
| `protocol` | 后端协议：`anthropic` / `openai` / `deepseek` |
| `model` | 模型名称 |
| `base_url` | API 请求地址 |
| `api_key` | 认证密钥 |

## 启动

```bash
python -m rhinecode --config config.yaml
```

也可以使用安装后的命令：

```bash
rhinecode --config config.yaml
```

## 斜杠命令

| 命令 | 说明 |
|------|------|
| `/think` | 在 off / high / max 间循环切换思考模式（Anthropic / DeepSeek 生效） |
| `/clear` | 清空当前对话历史 |
| `/exit` | 退出程序 |

## 工具能力

当使用 DeepSeek Provider 时，RhineCode 会向模型暴露以下工具：

| 工具 | 说明 | 是否需要确认 |
|------|------|--------------|
| `read_file` | 读取项目内文本文件，并带行号返回内容 | 否 |
| `glob_files` | 按 glob 模式查找项目内文件 | 否 |
| `grep_content` | 在项目内文本文件中按正则搜索内容 | 否 |
| `write_file` | 新建或覆盖项目内文件 | 是 |
| `edit_file` | 用唯一匹配的原文片段精确替换文件内容 | 是 |
| `run_command` | 在项目工作目录下执行 shell 命令 | 是 |

工具执行结果会显示在历史区中。只读工具可并发执行；有副作用工具串行执行，并且必须经过用户确认。

## 安全边界

- 文件、glob、grep 工具以启动 RhineCode 时的当前工作目录作为项目根
- 工具路径不能包含 `..`
- 绝对路径必须解析后仍位于项目根内
- 指向项目外的符号链接会被拒绝或跳过
- 命令工具显式以项目根作为 `cwd`，但不做命令沙箱；危险命令仍需要用户自行判断确认

## 项目结构

```text
rhinecode/
├── __main__.py          # CLI 入口
├── config.py            # YAML 配置加载与校验
├── conversation.py      # 对话管理、斜杠命令、工具编排
├── provider/
│   ├── base.py          # BaseProvider / Message / StreamChunk / ToolCall 抽象
│   ├── anthropic.py     # Anthropic 实现（含思考模式）
│   ├── openai.py        # OpenAI 实现
│   ├── deepseek.py      # DeepSeek 实现（含流式工具调用解析）
│   └── factory.py       # Provider 工厂
├── tools/
│   ├── base.py          # Tool / ToolResult 抽象
│   ├── registry.py      # 工具注册中心
│   ├── path_guard.py    # 项目工作目录路径守卫
│   ├── read_file.py     # 读文件工具
│   ├── write_file.py    # 写文件工具
│   ├── edit_file.py     # 精确编辑工具
│   ├── run_command.py   # 命令执行工具
│   ├── glob_files.py    # glob 找文件工具
│   └── grep_content.py  # grep 搜内容工具
└── tui/
    ├── app.py           # Textual App 主类
    └── widgets.py       # HistoryView / InputBar / StatusBar / 工具与确认组件
```

## 扩展新 Provider

1. 在 `rhinecode/provider/` 下新建实现文件，继承 `BaseProvider` 并实现 `stream_chat`
2. 在 `rhinecode/provider/factory.py` 的 `create_provider` 中添加对应分支
3. 在 `config.yaml` 中将 `protocol` 改为新值

如果新 Provider 需要支持工具调用，需要参考 `deepseek.py` 处理：

- 把 `tools` schema 传给模型 API
- 从流式响应中拼接工具调用参数
- 产出 `StreamChunk(type="tool_call")`
- 能序列化历史中的 `assistant(tool_calls)` 与 `role="tool"` 消息

## 扩展新工具

1. 在 `rhinecode/tools/` 下新建工具实现，继承 `Tool`
2. 声明 `name`、`description`、`parameters`、`read_only`
3. 在 `ToolRegistry.default()` 中注册工具
4. 文件类工具必须复用 `path_guard.py` 的路径边界校验

`read_only=True` 的工具会免确认并可并发执行；`read_only=False` 的工具会串行执行，并在执行前请求用户确认。
