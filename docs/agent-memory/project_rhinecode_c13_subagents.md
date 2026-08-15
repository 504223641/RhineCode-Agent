---
name: project-rhinecode-c13-subagents
description: "C13 子 Agent 系统已由 PR #17 合并进 main（1793 测试全绿）；含两次设计修订与真实模型抓出的五处缺陷"
metadata:
  type: project
  modified: 2026-08-07T01:22:19.644Z
---

C13 **子 Agent 系统**已于 2026-08-07 经 PR #17 合并进 `main`（1793 项测试全绿，
新增 315 项）。文档在 `docs/c13/`，进门读 `README.md` 的「开发期的两次设计修订」。

**能力**：主 Agent 用 `run_agent` 把子任务委派给独立上下文的子 Agent，只拿回结论。
角色 = Markdown + frontmatter（只有 `description` 必填），三层加载。
内置三个：`explorer`（只读调研）/ `planner`（只读方案）/ `general-purpose`（全工具）。

**这一章最该记住的三件事**：

1. **委派永不阻塞，等待交给 Agent Loop**。初版把「主 Agent 需要这个结果」实现成
   在工具调用里同步等，一个错误造成两个缺陷（多委派串行、结论要等用户插话）。
   闸门协议在 `agent/gate.py`（消费方），实现在 `subagents/gate.py`——
   反过来会撞循环导入。
2. **等待不是卡顿，是进度**。跑子 Agent 就是在执行任务，与主 Agent 自己跑一遍
   测试套件性质相同。因此**不设体验意义上的超时**，逃生口只有 `Esc`。
   （初版设过 180 秒并做过一个 `Ctrl+B`，都已删除。）
3. **真实模型抓出的五处缺陷，单元测试一条都抓不到**——四处是提示词效果，
   一处是「分支式父快照以无配对的 assistant(tool_calls) 结尾」（单元测试都从
   干净历史建快照，构造不出「跑到一半」的形态）。
   模型「先看一眼再决定」那条尤其典型：它判断对了该委派，但先探一下，
   探完就用「项目不大」证明自己不该委派——而一旦读了，委派的价值已经归零。

**遗留**（未进 todo，需要时单独立项）：子 Agent 被拒后仍会试约 3 次写入
（机械修法会误伤，因为权限判定依赖工具**和参数**）；`general-purpose` 在缺省
权限档下几乎没用武之地，要用户先配 allow 规则，开箱体验差。

人眼验收还剩三条（不阻塞）：见 `docs/c13/acceptance/manual.md`，
工作区搭建脚本是同目录的 `setup_manual.py`。

相关：[[project_rhinecode_c12_hooks]]、[[feedback_pr_format]]
