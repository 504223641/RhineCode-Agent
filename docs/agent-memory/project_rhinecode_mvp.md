---
name: project-rhinecode-mvp
description: RhineCode MVP 对话基础已完成实现，记录当前进度和技术决策
metadata:
  type: project
---

RhineCode MVP Phase 1（纯对话，无工具调用）已实现完毕，待端到端验收。

**Why:** 第一阶段目标是搭建对话基础骨架，包括 TUI、Provider 抽象、流式输出、多轮对话。

**How to apply:** 下次对话可直接从端到端测试或 Phase 2（工具调用）开始，不需要重新了解基础架构。

## 已完成文件

- `rhinecode/__main__.py` — CLI 入口，argparse --config
- `rhinecode/config.py` — YAML 配置加载，Config dataclass
- `rhinecode/conversation.py` — ConversationManager，斜杠命令，history 维护
- `rhinecode/provider/base.py` — BaseProvider、Message、StreamChunk
- `rhinecode/provider/anthropic.py` — Anthropic SSE + Extended Thinking
- `rhinecode/provider/openai.py` — OpenAI SSE 流式
- `rhinecode/provider/factory.py` — create_provider 工厂
- `rhinecode/tui/widgets.py` — HistoryView（ScrollableContainer）、InputBar、StatusBar
- `rhinecode/tui/app.py` — RhineApp，Textual App，Worker 流式渲染

## 关键技术决策

- TUI：Textual 框架（ScrollableContainer + 动态 Static widget，支持流式更新）
- 流式渲染：Textual Worker（thread=True）+ call_from_thread 推送到 UI
- Extended Thinking：ConversationManager 持有 thinking_enabled 状态，/think 命令切换
- HistoryView 用 ScrollableContainer+Vertical+Static 而非 RichLog（RichLog 不支持行内追加）

## 启动命令

```
python -m rhinecode --config config.yaml
```

config.yaml 需填写真实 api_key 才能测试 API 通信。
