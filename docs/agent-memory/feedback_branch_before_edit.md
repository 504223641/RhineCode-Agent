---
name: feedback-branch-before-edit
description: 动任何代码前先确认当前分支，在 main 上必须先开分支再改
metadata:
  type: feedback
  modified: 2026-07-29T15:01:06.128Z
---

在 RhineCode 项目里，**做任何代码修改之前先跑一次 `git branch --show-current`**。
如果当前在 `main`，**必须先 `git checkout -b <type>/<短描述>` 再动文件**，
不允许在 main 上直接编辑、提交。

**Why:** 用户在 2026-07-29 明确提出这条要求。触发场景：修 trace 阅读器的 GBK 崩溃时，
我在 main 上直接改完文件、要提交前才想起来开分支——虽然那次没出事（改动还没提交，
`git checkout -b` 会把工作区改动带过去），但顺序反了就是隐患：一旦顺手 commit 上去，
main 上就多出一个没走 PR 的提交，而项目的既定流程是 PR → merge → 删分支。

**How to apply:** 改代码的第一个动作是查分支，不是打开文件。分支名沿用提交信息的
type：`fix/...`、`feat/...`、`docs/...`、`chore/...`。这条与
[[feedback-commit-after-each-change]] 连着用——先开分支，再改一处提交一次。
PR 格式见 [[feedback-pr-format]]。

**注意这是 prompt 级约定，不是强制。** 用户已在讨论用 PreToolUse hook 做代码级强制
（与项目自身「权限由代码强制、不由 prompt 决定」的哲学一致）。若 `.claude/settings.json`
里已存在该 hook，则以 hook 为准，这条记忆只是它的说明。
