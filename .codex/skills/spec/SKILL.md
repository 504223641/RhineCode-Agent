---
name: spec
description: "Spec 驱动开发：通过协作式需求澄清，依次生成 spec.md → plan.md → task.md → checklist.md，然后指导开发和验收。在开始任何功能、模块或章节开发前使用。"
---

# Spec 驱动开发

**完整流程写在 [`.claude/commands/spec.md`](../../../.claude/commands/spec.md)，
现在就读那份文件，然后照它执行。**

## 为什么这里是个指针而不是正文

Claude Code 从 `.claude/commands/` 读斜杠命令，Codex 从 `.codex/skills/` 读技能，
两个路径都不能改。如果两边各放一份完整正文，它们会随时间分叉——而分叉的表现是
「同一个 `/spec` 在两个 Agent 里跑出不同流程」，且**没有任何东西会报错**。

所以正文只留一份（Claude Code 那份，因为它同时还是用户敲 `/spec` 时的入口），
这里只做转发。多一次文件读取换掉一整类静默分叉，是划算的。

`tests/test_cross_agent_sync.py` 钉住三件事：这个指针的目标文件存在、
两份 frontmatter 的 `name` 与 `description` 逐字相同、以及本文件确实是指针
而不是被人复制成了正文。
