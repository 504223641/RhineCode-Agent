---
name: rhinecode-c9-memory-system
description: RhineCode C9 记忆系统已实现（239 测试全绿）+ /resume 交互化增强（面板选择+历史回放，254 测试）
metadata:
  type: project
---

C9 记忆系统（分支 c9）设计阶段完成于 2026-07-12，`docs/c9/` 四份文档逐段评审通过，commit bfa6a05。

关键已锁定决策（澄清阶段用户拍板）：
- 三套机制：RHINE.md 三层项目指令（用户级→项目 .rhinecode→项目根，**项目级排后面**利用近因效应，同 Claude Code）、JSONL 会话存档（`.rhinecode/sessions/`，无 meta 文件、惰性建档）+ `/resume`+`rhine --continue`、四类自动笔记 + 索引注入（200 行/25KB）。
- 自动笔记**每次自然停止都调 LLM**（用户明确不加门槛）；LLM 只产 JSON 动作、程序写盘（文件名白名单防注入）。
- 用户后加需求：**锁文件并发防护**（F22-F24）——`O_CREAT|O_EXCL` 原子创建、非阻塞退让、mtime 过期自愈；会话锁靠 TUI 心跳（2 分钟 touch、10 分钟过期）。
- `/init` 从「不做」改为「要做」（F25）：内置指令走普通 Agent Loop + 权限管线。
- 生效范围按依赖分级：RHINE.md/会话 全 Provider；笔记与 /init 仅 DeepSeek 工具模式。
- 技术要点：存档记录 c8 压缩**前**的原始消息流；两个 prompt 槽位（110/130）改 cacheable=True 进 stable 通道；用户级 `~/.rhinecode/memory/` 走 path_guard 只读白名单；resume 后必须 `context_manager.reset()`。

开发完成于 2026-07-13：T1-T20 全部完成，4 个 feat 提交（ab768ea 基础四模块 / 4a41ff1 笔记+编排 / 2340405 沙箱+既有层 / 26ec79c 接入+文档），**239 测试全绿**（新增 67 个 test_memory_*，既有 172 无回归）。开发中的实现层决策：@include 尾随标点剥离（`(@b.md)` 括号问题）、`.gitignore` 补了 c8 遗漏的 `.rhinecode/context/` 连同 sessions/memory。待办：checklist 的【手测】项与 6 个端到端场景需真实 LLM/TUI 验收（用户执行）。相关：[[rhinecode-c8-context]]。

2026-07-14 增强：/resume 交互化——无参弹 SessionPanel（OptionList 面板，全量会话、锁定/当前项 disabled、option.id 直携 session_id）；载入成功产 HISTORY 事件（压缩**前**快照）→ TUI 清屏后 `render_history` 批量回放（工具行用简化静态行，**不复用 ToolCallWidget**——其 on_mount 计时器会覆盖终态）；`--continue` 启动同样回放；`handle_input` 新增第三种返回类型 `SessionListRequest`。254 测试全绿（+15），Textual Pilot 无头冒烟验证过面板+回放端到端（Pilot 里取 Static 内容要用 `.content` 属性，`render()` 返回 Visual 包装）。已提交（a6da731 功能 / 60badbc 文档同步），**PR #6 已合并进 main（cd0c029，merge commit 方式，与 c6-c8 一致）**——c9 章节整体收尾。docs/c9 设计文档按约定冻结不随增强改写。
