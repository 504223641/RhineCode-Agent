# RhineCode

RhineCode 是一个用 Python + Textual 实现的终端 AI 编程助手，交互体验参考 Claude Code。

当前版本以 DeepSeek Provider 为主实现了 C4 Agent Loop：模型可以在一次用户请求中循环读取项目、搜索代码、执行工具、回灌结果并继续下一轮，直到自然完成或命中停止条件。Anthropic / OpenAI Provider 目前保持纯对话能力。

## 功能

- **ReAct Agent Loop**：自动执行“调用模型 → 执行工具 → 回灌结果 → 再调用模型”的多轮循环。
- **流式输出**：正文与思考内容逐块渲染，后台 Worker 不阻塞 TUI 主线程。
- **DeepSeek 工具系统**：支持读文件、glob 找文件、grep 搜内容、写文件、精确编辑文件、运行命令。
- **Plan Mode**：`/plan` 开启后先只允许只读调研和需求澄清，完整计划进入聊天记录，经用户批准后才进入执行阶段。
- **逐项执行确认**：写文件、改文件、运行命令等副作用工具会弹出内联确认面板；计划获批不等于免确认，除非用户选择“执行且不再询问”。
- **明确停止原因**：支持自然完成、迭代上限、用户取消、计划拒绝、连续未知工具、流错误等停止路径。
- **路径安全边界**：文件类工具只能访问项目工作目录内路径，拒绝 `..`、越界绝对路径和指向项目外的符号链接。
- **Textual TUI**：历史区、命令提示、工具行、彩色 diff、确认/澄清面板、输入框和状态栏组合成终端界面。

> 工具调用与 Plan Mode 目前仅在 `protocol: deepseek` 且启用默认工具注册中心时可用。

## 安装

要求：Python 3.11+

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

示例：

```yaml
protocol: deepseek
model: deepseek-chat
base_url: https://api.deepseek.com
api_key: YOUR_API_KEY
```

可选协议：

| 字段 | 说明 |
|------|------|
| `protocol` | `anthropic` / `openai` / `deepseek` |
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
| `/plan` | 切换 Plan Mode：先规划、澄清和审批，再执行（DeepSeek 工具模式生效） |
| `/clear` | 清空当前对话历史 |
| `/exit` | 退出程序 |

运行中按 `Esc` 会请求取消当前 Agent Loop；如果正在等待确认或澄清，则由当前面板处理取消。

## 工具能力

DeepSeek 工具模式会向模型暴露以下工具：

| 工具 | 说明 | 是否需要确认 |
|------|------|--------------|
| `read_file` | 读取项目内文本文件，并带行号返回内容 | 否 |
| `glob_files` | 按 glob 模式查找项目内文件 | 否 |
| `grep_content` | 在项目内文本文件中按正则搜索内容 | 否 |
| `write_file` | 新建或覆盖项目内文件，并生成 diff | 是 |
| `edit_file` | 用唯一匹配的原文片段精确替换；支持 `edits` 批量替换 | 是 |
| `run_command` | 在项目工作目录下执行 shell 命令 | 是 |

只读工具可并发执行；有副作用工具串行执行。确认面板支持“执行”“执行且不再询问”“取消”三态。

## Plan Mode

开启 `/plan` 后，单条用户消息会从规划阶段开始：

1. 只暴露只读工具与两个特殊交互工具：`ask_user` 和 `present_plan`。
2. 模型可以读取/搜索项目，并通过澄清面板向用户提多选问题。
3. 模型提交计划时，完整计划会先作为普通助手消息进入聊天记录。
4. 用户选择“开始执行”后，本轮进入执行阶段并开放全部工具。
5. 执行阶段的写文件、改文件、运行命令仍逐个确认；只有选择“执行且不再询问”才会本会话免确认。
6. 用户选择“暂不执行”时，本轮以“计划未执行”停止，不再让模型继续推进。

Plan Mode 开关会保持开启；下一条用户消息会重新从规划阶段开始。

## 安全边界

- 文件、glob、grep 工具以启动 RhineCode 时的当前工作目录作为项目根。
- 工具路径不能包含 `..`。
- 绝对路径必须解析后仍位于项目根内。
- 指向项目外的符号链接会被拒绝或跳过。
- 命令工具显式以项目根作为 `cwd`，但不做命令沙箱；危险命令仍需要用户判断确认。
- `config.yaml` 可能包含真实 API Key，请勿提交到版本库。

## 项目结构

```text
rhinecode/
├── __main__.py          # CLI 入口
├── config.py            # YAML 配置加载与校验
├── conversation.py      # 对话管理、斜杠命令、Agent 回调封装
├── agent/
│   ├── events.py        # AgentEvent / StopReason / ConfirmDecision 等事件类型
│   ├── collector.py     # StreamCollector 双路收集
│   ├── loop.py          # Agent Loop 核心
│   ├── plan_tools.py    # ask_user / present_plan 特殊工具 schema
│   └── prompt.py        # Plan Mode system prompt
├── provider/
│   ├── base.py          # BaseProvider / Message / StreamChunk / ToolCall 抽象
│   ├── anthropic.py     # Anthropic 纯对话实现
│   ├── openai.py        # OpenAI 纯对话实现
│   ├── deepseek.py      # DeepSeek 流式对话、思考与工具调用解析
│   └── factory.py       # Provider 工厂
├── tools/
│   ├── base.py          # Tool / ToolResult 抽象
│   ├── diff.py          # 结构化 diff 构造
│   ├── registry.py      # 工具注册中心
│   ├── path_guard.py    # 项目工作目录路径守卫
│   ├── read_file.py
│   ├── write_file.py
│   ├── edit_file.py
│   ├── run_command.py
│   ├── glob_files.py
│   └── grep_content.py
└── tui/
    ├── app.py           # Textual App 主类
    └── widgets.py       # HistoryView / InputBar / StatusBar / 面板组件
```

## 测试

```bash
python -m compileall rhinecode tests
python -m unittest discover -s tests
```

当前测试覆盖路径越界防护、确认回调、会话级免确认、Plan Mode 完整计划展示、拒绝计划停止、计划获批后仍逐项确认等关键行为。

## C4 文档

C4 的规格、实现计划、任务拆解和验收清单位于：

- `docs/c4/spec.md`
- `docs/c4/plan.md`
- `docs/c4/task.md`
- `docs/c4/checklist.md`

这些文档描述当前 Agent Loop 与 Plan Mode 的实际行为。

## 扩展新 Provider

1. 在 `rhinecode/provider/` 下新建实现文件，继承 `BaseProvider` 并实现 `stream_chat`。
2. 在 `rhinecode/provider/factory.py` 的 `create_provider` 中添加对应分支。
3. 在 `config.yaml` 中将 `protocol` 改为新值。

如果新 Provider 要支持工具调用，需要参考 `deepseek.py`：

- 把 `tools` schema 传给模型 API。
- 从流式响应中拼接工具调用参数。
- 产出 `StreamChunk(type="tool_call")`。
- 能序列化历史中的 `assistant(tool_calls)` 与 `role="tool"` 消息。

## 扩展新工具

1. 在 `rhinecode/tools/` 下新建工具实现，继承 `Tool`。
2. 声明 `name`、`description`、`parameters`、`read_only`。
3. 在 `ToolRegistry.default()` 中注册工具。
4. 文件类工具必须复用 `path_guard.py` 的路径边界校验。

`read_only=True` 的工具免确认并可并发执行；`read_only=False` 的工具串行执行，并在执行前请求用户确认。
