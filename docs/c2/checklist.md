# RhineCode MVP 对话基础 Checklist

> 每一项通过运行代码或观察行为来验证，聚焦系统行为。

## 实现完整性

- [ ] Provider 抽象层可导入（验证：`python -c "from rhinecode.provider.base import BaseProvider"` 无报错）
- [ ] Anthropic Provider 实例化不报错（验证：传入有效 Config 构造 AnthropicProvider，无异常）
- [ ] OpenAI Provider 实例化不报错（验证：传入有效 Config 构造 OpenAIProvider，无异常）
- [ ] ConversationManager 三个斜杠命令行为正确（验证：/clear 清空 history，/think 切换状态，/exit 抛 SystemExit）
- [ ] TUI 三个组件可导入（验证：`from rhinecode.tui.widgets import HistoryView, InputBar, StatusBar` 无报错）

## 集成

- [ ] 配置文件缺字段时报错可读（验证：删除 api_key 字段，运行 load()，错误信息包含 "api_key"）
- [ ] create_provider 按 protocol 返回正确实现（验证：传入两种 protocol 各返回对应类型实例）
- [ ] TUI 启动后状态栏显示正确的模型名和 Provider 名（验证：启动程序，肉眼观察状态栏）
- [ ] 输入框提交后历史区出现用户消息（验证：输入任意文字回车，历史区显示 "You: ..." 前缀内容）

## 编译与安装

- [ ] `pip install -e .` 成功无报错
- [ ] `python -m rhinecode --config config.yaml` 启动无报错
- [ ] `python -m rhinecode --config 不存在.yaml` 打印错误后退出，不崩溃

## 端到端场景

- [ ] **AC2 流式输出**：输入问题，AI 回复逐字出现在历史区，不是一次性刷出（观察：字符是否渐进显示）
- [ ] **AC3 多轮记忆**：连续两轮对话，第二轮问"我刚才问了什么"，AI 正确引用第一轮内容
- [ ] **AC4 OpenAI 后端**：将 config.yaml 中 protocol 改为 openai，重启后对话和流式输出正常
- [ ] **AC5 Extended Thinking**：Anthropic 模式下输入 `/think`，状态栏变为"思考模式：开启"，下次回复包含思考内容
- [ ] **AC6 OpenAI 下 /think**：OpenAI 模式下输入 `/think`，历史区显示不支持提示，程序不崩溃
- [ ] **AC7 /clear**：输入 `/clear`，历史区清空，下一轮对话 AI 不记得之前内容
- [ ] **AC8 错误处理**：将 api_key 改为错误值，发起对话，历史区显示可读错误信息，程序保持运行
- [ ] **AC9 /exit**：输入 `/exit`，程序正常退出；按 Ctrl+Q 同样正常退出；Ctrl+C 不应触发退出
