---
name: feedback-pr-format
description: RhineCode 的 PR 标题与正文格式约定；⚠ 格式不必问，但开 PR 与合并**都必须先问用户**
metadata:
  type: feedback
  modified: 2026-08-05T19:49:24.111Z
---

PR 的**格式**不用再问，按下面写（沿用 PR #8/#9 的体例）。
但**开 PR 和合并这两个动作本身必须先问用户**——见文末。

**标题**：`<type>(<scope>): <中文一句话>`，与提交信息同风格。
章节用 `feat(c11): …`；纯维护轮用 `fix(...)` 或 `chore(...)`。

**正文骨架**：

```markdown
## 概要

一段话：这次做了什么、**为什么**。若是语义反转或行为变化，用一张
「改前 / 改后」对照表——这是读者最需要的东西。

**分组小标题（加粗，不用 ###）**

- 每组下面用 bullet，写**行为**与**理由**，不是文件清单
- 涉及缺陷时写清「原来会怎样错、错在哪、现在怎样」

## 测试

- `python -m compileall rhinecode tests` 通过
- `python -m unittest discover -s tests`：**N 项全绿**（skipped 4 为默认跳过的 live/slow）
- 端到端验收结论（跑了几条、多少判据、有没有改产品代码）

## 文档

- 文档变更要点

🤖 Generated with [Claude Code](https://claude.com/claude-code)

https://claude.ai/code/session_<当前 session id>
```

**内容要求**（这几条是用户反复认可的写法）：

- 讲**为什么**比讲改了什么重要；缺陷要写「危害是什么、界面上看不看得出来」
- 有反证 / 护栏时明确点出（「没有这条，一个错误实现也会全绿」）
- 数字要准（测试条数、场景数、判据数），别写「若干」
- 用对照表呈现语义反转，用代码块呈现证据原文
- 不堆文件清单——那是 diff 的事

## ⚠ 开 PR 与合并**都必须先问**（2026-08-06 用户明确纠正）

**不请自来的只有「格式」，不是「动作」。** 具体：

- **不要自己 `gh pr create`** —— 分支推上去、把 PR 正文准备好，然后**问用户要不要开**。
- **更不要自己 `gh pr merge`** —— 合并进 main 永远要用户明确点头。
- 提交（`git commit`）仍然不用问，见 [[feedback_commit_after_each_change]]。

用户原话：「每次要合 PR 必须先问我，不能自己直接开 PR」。

**流程**（每一步之间都要用户点头）：
写好正文 → **问** → `gh pr create` → **问** → `gh pr merge <n> --merge` →
合并后删分支（本地 `git branch -d` + `git push origin --delete`），
删前确认「领先 main 0 个提交」。

**Why:** 格式是重复劳动，不必每次交代；但**开 PR 与合并是对外动作**，
用户要保留决定权——尤其在验收项还没走完的时候。

**How to apply:** 格式直接按上面写，**动作一律先问**。
