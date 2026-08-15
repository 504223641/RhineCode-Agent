---
name: feedback-commit-after-each-change
description: 每次改完代码立刻提交一个 commit，不要攒着等用户来要
metadata:
  type: feedback
  modified: 2026-07-29T18:01:47.222Z
---

在 RhineCode 项目里，每完成一次代码修改就立刻 `git commit`，不要攒成一批、
也不要等用户开口要。

**Why:** 用户在 2026-07-26 的 C11 手测中明确要求「每次修改代码以后都 commit 一下」。
手测期间改动密集且分散（修 bug、调内置 Skill 正文、改测试），攒着提交会让
「哪次改动导致行为变化」无从追溯——而手测正是靠逐条对比行为来验收的。

**How to apply:** 改完代码 → 跑 `python -m unittest discover -s tests` 确认全绿 →
立即提交，一次改动一个 commit，提交信息说清「问题是什么、为什么这么改」。
无需再问「要我提交吗」。文档类改动同理。参见 [[project-rhinecode-c11-skills]]。
