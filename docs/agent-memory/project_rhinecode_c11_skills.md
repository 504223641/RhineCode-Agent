---
name: project-rhinecode-c11-skills
description: C11 Skill 系统已完成；11 条端到端场景已用 P1a 真实模型验完 10 条，剩场景 9 结构上驱动不了
metadata:
  type: project
  modified: 2026-07-27T22:42:37.675Z
---

C11（Skill 系统）代码已完成，与 trace/P1a 一并合进 `c11` 分支，全量 858 测试全绿。

**验收进度（截至 2026-07-28）**：`docs/c11/checklist.md` 第十一节 11 条端到端场景
**已用 P1a 驱动设施以 `--mode live` 真实模型跑完 10 条，43/43 判据全通过、产品代码
一行未动**。报告 `docs/c11/acceptance-c11-live.md`，预置 `tests/e2e/c11_scenarios.py`
（**只有预置、没有剧本**——假模型照本宣科验不了「模型是否按 SOP 行事」）。
这些**刻意不写进 unittest**：模型行为不确定，今天绿明天红的测试最终一定被 skip 掉。

**唯一未跑的场景 9 是 P1a 的能力缺口**：`DriverCore.send` 前置检查写死「只有 idle
才能提交」，忙碌态下提交递不进产品，被拦的是驱动器自己的守卫。已由
`tests/test_skill_tui.py` 覆盖；控制通道加「强制提交」指令推给 P1b（五处成对维护点）。

**驱动真实模型的操作要点**（都踩过）：
- 客户端指令**必须走 PowerShell 工具，不能用 Bash 工具**——MSYS 路径转换会把
  `/skills` 改写成 `C:/Program Files/Git/skills`，斜杠命令变成普通消息发给模型；
- 往工作区写文件时**别用 `python -c "…$ARGUMENTS…"`**，PowerShell 会展开 `$`；
  用 Bash 工具的 heredoc 或调 `c11_scenarios` 里的预置函数；
- 应答面板用 `wait --json` + `answer once` 的 PowerShell 循环批量推进，每轮打印面板原文留证。

**验收本身踩到的两类坑**（比产品 bug 更常见）：
- **场景构造不出被测状态，长得和产品坏掉一模一样**：用内置 `/review` 验 Plan Mode
  时审批面板一次都不弹，因为纯分析任务没有「计划」可提。换成必须改代码的 Skill 才验得了；
- **checklist 措辞比实现旧**：场景 7/8 写「启动有提示/警告」，而实现有意挪进了
  `/skills`（启动 print 会被 alternate screen 盖住）。先读代码判断哪边对，别急着报 bug。

**Why:** 判据都落在「模型实际收到了什么、实际做了什么」上，而那正是界面看不见、
只有 trace 记得下的部分（逐轮 `tool_names`、dynamic reminder 里的 SOP 正文、
`outcome=out_of_scope`）。

**How to apply:** 报告按「机器判到了什么」与「我据此做的判断」两栏写，前者可复核、
后者可争论。**关键判据要配反证**（如「MCP 子进程数 0」必须配一次成功启动证明它本会 >0），
否则证明不了「没走到那一步」还是「那一步本来不存在」。真实模式产物跑完即删。
见 [[project_rhinecode_trace_p1]]、[[feedback_interactive_verification]]。
