---
name: project_cross_agent_sync
description: Claude Code 与 Codex 共用同一份项目理解的接线已完成；两边私有记忆不可合并是查证过的结论，别再试
metadata:
  type: project
  modified: 2026-08-16
---

RhineCode 由 Claude Code 与 Codex 交替开发。2026-08-16 在分支 `feat/cross-agent-sync`
上做完了「双方理解一致」的接线，设计与不变量写在 `CLAUDE.md` 的「跨 Agent 协作」一节，
护栏在 `tests/test_cross_agent_sync.py`（11 条）。

## 三条查证过的硬事实（别再重新调研）

1. **两边的私有记忆目录不可合并，这是结构性的、不是没找到办法。**
   Codex 的 `MemoriesToml`（`codex-rs/core/config.schema.json`）里**一个 path/dir 键都没有**，
   记忆锁死在 `~/.codex/memories/`；Claude Code 这边的目录由 harness 固定。
   两者互不可见、都不可重定位 —— 所以共享层只能是**第三个地方**（仓库）。
   ⚠ 「把两个 Agent 的记忆目录指到同一处」这个想法看起来永远像是更优解，它不可行。

2. **Codex 的 `project_doc_max_bytes` 不是模型上下文窗口**，是产品级默认值 32768，
   与用哪个模型、窗口多大无关（换 1M 窗口的模型也仍然是 32768）。
   超出部分在 `codex-rs/core/src/agents_md.rs` 里 `data.truncate(remaining)`
   **从文件中间按字节切**，只 `tracing::warn!`、TUI 上无提示。

3. **项目级 `.codex/config.toml` 需要该项目被标记 trusted 才生效。**
   loader 原文：`repo $(git rev-parse --show-toplevel)/.codex/config.toml
   (loaded but disabled when untrusted)`。不信任时**加载但整体禁用且不显式报错** ——
   排查「配了没生效」时先查这个。桌面版与 CLI 共用同一套配置栈。

## 验收方法

改完这套接线后，在 Codex 里问一个**答案分布在文件尾部**的问题来验证没被截断，
例如「②″ 保护路径为什么是出口处的收紧器而不是管线里的一站」
（它在 CLAUDE.md 约 135 KB 处，默认 32 KiB 上限下必然答不出）。
⚠ 别用文件开头的问题验 —— 那部分无论截没截都读得到，**必然通过，等于没验**。
这与 [[feedback_safety_counterproof_sampling]] 是同一类取样陷阱。

写新记忆的格式与「什么时候该写」见 `CLAUDE.md`，
相关约定见 [[feedback_commit_after_each_change]] 与 [[feedback_branch_before_edit]]。
