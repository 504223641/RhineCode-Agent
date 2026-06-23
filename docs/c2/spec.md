# RhineCode MVP 对话基础 Spec

## 背景

RhineCode 是一个从零构建的终端 AI 编程助手，对标 Claude Code。当前阶段目标是搭建可运行的对话基础——用户在终端启动后进入多面板 TUI，输入问题，AI 流式逐字回复，支持多轮对话记忆。本阶段不涉及工具调用和文件操作，专注对话管道和界面骨架。

## 目标

- 用户可通过终端命令启动 RhineCode 并进入交互界面
- 支持与 Anthropic Claude / OpenAI 两种后端的流式对话
- 支持 Claude Extended Thinking，通过 `/think` 命令运行时切换
- Provider 层抽象为统一接口，为后续扩展留口
- 通过 YAML 配置文件管理 API 信息，无需改代码切换后端

## 功能需求

- F1（启动入口）：提供一个命令行入口，读取 YAML 配置文件，初始化 Provider，启动 TUI 界面
- F2（多面板 TUI）：界面分为三个区域——顶部对话历史区（可滚动）、底部输入框、状态栏（显示当前模型/Provider/思考模式状态）
- F3（流式输出）：AI 回复通过 SSE 流式逐字渲染到历史区，不等全部生成完再显示
- F4（多轮对话）：维护完整的对话历史，每次请求携带全部上下文，AI 能记住本轮之前所有内容
- F5（Provider 抽象）：定义统一的 Provider 接口，Anthropic 和 OpenAI 各自实现，通过配置文件的 `protocol` 字段选择
- F6（YAML 配置）：支持 `protocol`、`model`、`base_url`、`api_key` 四个核心字段，配置文件路径可通过启动参数指定
- F7（Extended Thinking）：用户在对话中输入 `/think` 切换 Extended Thinking 开启/关闭，仅对 Anthropic Claude 生效，状态栏实时反映当前状态；对 OpenAI 使用该命令时给出提示
- F8（斜杠命令）：支持 `/think`、`/exit`（退出程序）、`/clear`（清除对话历史）三个内置命令

## 非功能需求

- N1（响应延迟）：流式首字符延迟不超过 2 秒（网络正常情况下）
- N2（错误处理）：API 错误、网络超时、配置缺失时，在 TUI 内给出可读的错误提示，不崩溃退出
- N3（配置安全）：api_key 不打印到界面或日志中
- N4（可扩展性）：Provider 接口设计使得新增第三方后端只需新增一个实现类，无需改动核心逻辑

## 不做的事

- 不实现工具调用（Tool Use）、文件读写、代码编辑等 Agent 功能
- 不做对话历史持久化（退出后历史清空）
- 不做多配置文件/多 Profile 切换
- 不做用户认证、会话管理
- 不做 `/think` 以外的模型参数运行时调整（如温度、max_tokens）

## 验收标准

- AC1（启动）：执行 `python -m rhinecode --config config.yaml` 后，TUI 界面正常渲染，状态栏显示当前模型和 Provider 名称
- AC2（对话）：输入任意问题并回车，AI 回复在历史区逐字流式出现，不是一次性全部刷出
- AC3（多轮记忆）：连续对话两轮，第二轮提问「我刚才问了什么」，AI 能正确引用第一轮内容
- AC4（OpenAI 后端）：修改配置文件将 `protocol` 改为 openai 后重启，对话功能正常，流式输出正常
- AC5（Extended Thinking）：Anthropic 模式下输入 `/think`，状态栏切换为「思考模式：开启」，下次回复包含思考过程；再次输入 `/think` 关闭
- AC6（OpenAI 下 /think）：OpenAI 模式下输入 `/think`，历史区显示「当前 Provider 不支持 Extended Thinking」，不崩溃
- AC7（/clear）：输入 `/clear` 后历史区清空，下一轮对话不携带之前上下文
- AC8（错误处理）：配置文件缺少 api_key 或 API 请求失败时，历史区显示可读错误信息，程序保持运行
- AC9（/exit）：输入 `/exit` 或按 Ctrl+C，程序正常退出，不留残余进程
