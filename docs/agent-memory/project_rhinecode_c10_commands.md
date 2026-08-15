---
name: project-rhinecode-c10-commands
description: "C10 斜杠命令注册与分发已经 PR #7 合并进 main（358 测试全绿），单一 CommandSpec 注册/dispatcher 分流/双内容 Message/Tab 补全（候选不含别名）/[DEFAULT]-[PLAN] 状态栏"
metadata:
  type: project
---

C10（斜杠命令注册与分发）已于 2026-07-15 经 PR #7（merge commit 1e1a601）合并进 main，`python -m unittest discover -s tests` 358 个测试全绿（其中 C10 十个目标模块 150+）。

后续行为变更（2026-07-15，同随 PR #7 合入）：补全候选去掉别名——`registry.complete()` 只匹配规范名，`CompletionItem.is_alias` 字段已删除；别名仍可执行/高亮/`/help` 可见。spec F21/AC11、plan 4.5、task T8 的旧描述属历史记录，以 docs/c10/checklist.md C36 为准。副作用：仅别名覆盖的前缀（/q、/new、/reset、/allowed-tools）失去 Tab 补全。

要点：
- 新增 `rhinecode/commands/`（models/parser/registry/dispatcher/builtins/__init__），12 条规范命令 + 8 别名单一注册；`build_builtin_registry()` 在 `__main__` 的 Provider/MCP 之前构建，冲突抛 `CommandRegistrationError` 退出码 1。
- `ConversationManager.handle_input` 已删除，拆成 `submit_user_message`/`cycle_thinking`/`toggle_plan`/`cycle_permission`/`mcp_report`/`context_report`/`memory_report`/`manual_compact`/`resume`/`clear`（返回确认文本）+ `tools_enabled` property。
- `RhineApp` 实现 `CommandController` 协议；提交入口唯一走 `dispatcher.dispatch(text, self)`；SessionPanel 直调 `resume_session(id)`。
- `Message` 新增 `display_content`（双内容）；session.py JSONL 兼容读写、标题优先显示内容；`build_replay_items` user 分支优先 display_content。
- 已批准的偏差：`INIT_PROMPT` 从 `agent/prompt/texts/init.py` 迁入 `commands/builtins.py`（原文件删除、texts/__init__ 同步），按 task T18「迁入」+T52「清未使用常量」执行。
- 尚未做：docs/c10/checklist.md 的手工验收（E01–E06 端到端、双主题状态栏 C57/C58 等需真实 TUI）；验收报告未产出。相关 [[project-rhinecode-c9-memory]]。
