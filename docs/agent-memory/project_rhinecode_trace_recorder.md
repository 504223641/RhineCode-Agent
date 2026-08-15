---
name: project-rhinecode-trace-recorder
description: Trace 记录器（P0）代码已完成 710 测试全绿，待用户跑 9 个端到端手测场景
metadata:
  type: project
  modified: 2026-07-26T22:00:26.504Z
---

**Trace 记录器（P0）** 已按 `docs/c11/testing/p0-trace/task.md` 的 58 个任务全部实现，
分三段 8 个 commit 落在 `c11-trace` 分支（`068f4b9` → `b0f01c0`），
全量 **710 条测试通过**（基线 580 条一条不少，新增 130 条）。

**交付内容**：`rhinecode/trace/` 叶子包（models / recorder / tracing_provider / reader）
+ 新的 `rhinecode/bootstrap.py` 装配层 + 九个既有模块的埋点 + `--trace` 三态开关
+ `python -m rhinecode.trace.reader` 只读 CLI 阅读器。

**当前状态（2026-07-27）**：checklist 的**全部自动化项与架构不变量项已验收通过**；
第九节 **9 个端到端手测场景待用户亲自跑**（见 `docs/c11/testing/p0-trace/checklist.md`）。
其中场景 2 需先自造一个 `allowed_tools: [read_file]` 的临时 Skill，
场景 7 需一份 `context_window: 8192` 的临时配置，场景 9 结束后必须删除产出文件（含真实密钥）。

**Why:** 手测 C11 时发现会话存档只记模型历史（至多六字段），看不到系统提示、
每轮发出的 tools schema、权限决策、本地命令输出、状态栏与压缩动作，
定位 bug 两次都得现写探针脚本。P1（TUI 驱动器）已明确推到下一轮，不在本次范围。

**How to apply:** 继续 C11 手测（[[project_rhinecode_c11_skills]] 场景 2–11）前，
先按分支纪律把 `c11-trace` 合回 `c11`。给用户手测步骤时沿用
[[feedback_interactive_verification]] 的分步+预期格式。
排查 RhineCode 行为问题时**优先开 `rhine --trace` 再读产出**，
不要再现写探针脚本——那正是本模块要消灭的工作。
