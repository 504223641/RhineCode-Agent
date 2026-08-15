# 共享记忆索引

> 这份索引由 `CLAUDE.md` 的「跨 Agent 协作」一节引用，因此 **Claude Code 与 Codex
> 在每个会话开始时都会读到它**。正文按需读取——先看这里的一句话摘要，判断相关再打开。

**这是两个 Agent 共用的唯一记忆来源。** 各自 harness 的私有记忆
（`~/.claude/projects/.../memory/`、`~/.codex/memories/`）互相看不见、也不可重定位，
因此一律视为草稿：任何一条值得留下的结论都要写到这里来，跟着当次改动一起提交。

格式：每条一个文件，frontmatter 里 `name` 是它的稳定标识，`description` 是
「判断相关性」用的一句话。正文里的 `[[name]]` 指向同目录下 `name` 相同的另一条。

## 工作方式约定

用户给过的纠正与确认过的做法。**这些是两个 Agent 都必须遵守的**。

- [feedback-branch-before-edit](feedback_branch_before_edit.md) — 动任何代码前先确认当前分支，在 main 上必须先开分支再改
- [feedback-commit-after-each-change](feedback_commit_after_each_change.md) — 每次改完代码立刻提交一个 commit，不要攒着等用户来要
- [feedback_doc_sync_before_merge](feedback_doc_sync_before_merge.md) — 合并到 main 之前必须逐份核对「须同步更新的文档」是否已是最新，确认后才能合
- [feedback_interactive_verification](feedback_interactive_verification.md) — 需要人眼看渲染的 TUI 验收由用户亲自跑；但能无头驱动的交互场景应由 Claude 跑完
- [feedback-never-bypass-delete-guard](feedback_never_bypass_delete_guard.md) — 删除操作的路径必须先校验；曾因空变量导致 rmtree 删掉整个仓库
- [feedback-pr-format](feedback_pr_format.md) — RhineCode 的 PR 标题与正文格式约定；⚠ 格式不必问，但开 PR 与合并**都必须先问用户**
- [feedback_prompt_ab_scene_design](feedback_prompt_ab_scene_design.md) — 用 A/B 实测验证提示词效果时，场景必须让「违反提示词」成为省事的那条路
- [feedback-safety-counterproof-sampling](feedback_safety_counterproof_sampling.md) — 验「危险动作被某一层拦住」时，模型的自我拒绝会挡在权限管线之前，0 条判定看起来像通过、其实那一层压根没被执行到
- [spec-approval-content-must-be-final-message](feedback_spec_approval_display.md) — /spec 逐段审批时，段落正文必须放在回合末尾的最终消息里，不能和 AskUserQuestion 同回合发出
- [feedback-todo-folder-convention](feedback_todo_folder_convention.md) — 用户说「后续再做」的事项一律记进 docs/todo/，每项单独一份文档并附可一键复制的 Prompt
- [feedback-trace-no-thresholds](feedback_trace_no_thresholds.md) — trace 不许有任何截断阈值——观测设施丢内容就不再是可信证据；裁剪只能发生在「给模型看的那一份」上

## 项目进展与踩坑

各章节的完成状态、真实模型验收抓到的缺陷、以及不写下来就会重犯的教训。

- [project-rhinecode-c10-commands](project_rhinecode_c10_commands.md) — "C10 斜杠命令注册与分发已经 PR #7 合并进 main（358 测试全绿），单一 CommandSpec 注册/dispatcher 分流/双内容 Message/Tab 补全（候选不含别名）/[DEFAULT]-[PLAN] 状态栏"
- [project-rhinecode-c11-skills](project_rhinecode_c11_skills.md) — C11 Skill 系统已完成；11 条端到端场景已用 P1a 真实模型验完 10 条，剩场景 9 结构上驱动不了
- [project-rhinecode-c12-hooks](project_rhinecode_c12_hooks.md) — "C12 Hook 系统已完成开发与全部验收（含真实模型），PR #16 待合并；实跑撞出的复合命令缺陷已修，C6 同缺口已立项未修"
- [project-rhinecode-c13-subagents](project_rhinecode_c13_subagents.md) — "C13 子 Agent 系统已由 PR #17 合并进 main（1793 测试全绿）；含两次设计修订与真实模型抓出的五处缺陷"
- [project_cross_agent_sync](project_cross_agent_sync.md) — Claude Code 与 Codex 共用同一份项目理解的接线已完成；两边私有记忆不可合并是查证过的结论，别再试
- [project_rhinecode_c14_worktree](project_rhinecode_c14_worktree.md) — C14 子 Agent 工作区隔离已完成并经真实模型验收；真跑抓出 7 个单元测试抓不到的缺陷，方法论教训值得复用
- [project-rhinecode-c3-tools](project_rhinecode_c3_tools.md) — RhineCode c3 工具系统已实现（仅 DeepSeek），逻辑层验收通过，待 TUI 实时端到端验收
- [project_rhinecode_c6_permissions](project_rhinecode_c6_permissions.md) — RhineCode c6 章节——五层防御权限系统的设计决策（决策管线、求值哲学）
- [project_rhinecode_c7_mcp](project_rhinecode_c7_mcp.md) — RhineCode c7 MCP 客户端已实现并全测通过（109 测试），架构、决策与验收状态
- [project_rhinecode_c8_context](project_rhinecode_c8_context.md) — RhineCode c8 上下文管理（两层压缩）已实现并 172 测试全绿，估算锚点/存盘/摘要/熔断
- [rhinecode-c9-memory-system](project_rhinecode_c9_memory.md) — RhineCode C9 记忆系统已实现（239 测试全绿）+ /resume 交互化增强（面板选择+历史回放，254 测试）
- [project_rhinecode_deepseek_only](project_rhinecode_deepseek_only.md) — RhineCode 后续所有开发只针对 DeepSeek Provider，不再为 Anthropic/OpenAI 做兼容设计
- [project-rhinecode-mvp](project_rhinecode_mvp.md) — RhineCode MVP 对话基础已完成实现，记录当前进度和技术决策
- [project-rhinecode-p1b-pending](project_rhinecode_p1b_pending.md) — P1b（无人值守回归）待做，已明确范围与必须修正的取样方法；做 Skill 作者期之后再问用户要不要开
- [project_rhinecode_trace_p1](project_rhinecode_trace_p1.md) — Trace P0+P1a 已合并进 c11（858 测试全绿）；下一步开 P1b，范围与接手方式在此登记
- [project-rhinecode-trace-recorder](project_rhinecode_trace_recorder.md) — Trace 记录器（P0）代码已完成 710 测试全绿，待用户跑 9 个端到端手测场景
- [project-rhinecode-tui-activity-fold](project_rhinecode_tui_activity_fold.md) — "tui-activity-fold 扩展已由 PR #35 合并进 main（2645 全绿）；真机复核跑出 24 条而单测事前抓到 0 条，方法论与两条 Textual 硬事实记在这里"
