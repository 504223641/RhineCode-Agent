# RhineCode

终端 AI 编程助手，类似 Claude Code，基于 Python 实现。

在终端中启动后进入多面板交互界面，支持与 Anthropic Claude、OpenAI、DeepSeek 进行多轮流式对话。

## 功能

- **流式输出** — SSE 流式渲染，AI 回复逐字显示，不阻塞界面
- **多轮对话** — 自动维护完整对话历史，AI 能记住上下文
- **多后端支持** — 支持 Anthropic Claude、OpenAI、DeepSeek，通过配置文件切换
- **Extended Thinking** — 支持 Claude 的深度思考模式，运行时 `/think` 命令切换
- **Textual TUI** — 三面板布局（历史区 / 输入框 / 状态栏），交互体验接近 Claude Code

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
protocol: anthropic
model: claude-sonnet-4-6
base_url: https://api.anthropic.com
api_key: sk-ant-...

# OpenAI
# protocol: openai
# model: gpt-4o
# base_url: https://api.openai.com/v1
# api_key: sk-...

# DeepSeek
# protocol: deepseek
# model: deepseek-chat
# base_url: https://api.deepseek.com
# api_key: sk-...
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

## 斜杠命令

| 命令 | 说明 |
|------|------|
| `/think` | 切换 Extended Thinking 开启/关闭（仅 Anthropic 生效） |
| `/clear` | 清空对话历史 |
| `/exit` | 退出程序 |

## 项目结构

```
rhinecode/
├── __main__.py          # CLI 入口
├── config.py            # YAML 配置加载
├── conversation.py      # 对话管理，斜杠命令处理
├── provider/
│   ├── base.py          # BaseProvider 抽象接口
│   ├── anthropic.py     # Anthropic 实现（含 Extended Thinking）
│   ├── openai.py        # OpenAI 实现
│   ├── deepseek.py      # DeepSeek 实现（OpenAI 兼容）
│   └── factory.py       # Provider 工厂
└── tui/
    ├── app.py           # Textual App 主类
    └── widgets.py       # HistoryView / InputBar / StatusBar
```

## 扩展新 Provider

1. 在 `rhinecode/provider/` 下新建实现文件，继承 `BaseProvider` 并实现 `stream_chat`
2. 在 `rhinecode/provider/factory.py` 的 `create_provider` 中添加对应的 `elif` 分支
3. 在 `config.yaml` 中将 `protocol` 改为新值即可使用
