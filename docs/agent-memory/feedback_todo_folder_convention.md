---
name: feedback-todo-folder-convention
description: 用户说「后续再做」的事项一律记进 docs/todo/，每项单独一份文档并附可一键复制的 Prompt
metadata:
  type: feedback
  modified: 2026-07-28T18:23:09.989Z
---

用户说「这个后续再做」「先记下」的任何事项，**一律写进仓库的 `docs/todo/`**，
不要只记在我的记忆里、也不要只塞进 CLAUDE.md 的「已知后续工程项」。

规则（2026-07-29 用户明确要求）：

- **每个事项单独一份文档**，不要合并成一个大清单
- **文件名以序号前缀表示优先级**：`1-xxx.md`、`2-xxx.md`……**数字越小优先级越高**。
  每次新增 TODO 时**动态重排全部序号**（新事项可能插在中间），不是简单追加到末尾。
  重排后要 `git mv` 保住历史，并同步 `docs/todo/README.md` 里的清单
- 每份文档**必须含一段可一键复制的 Prompt**，让用户在新 session 里粘贴即可开工
  （Prompt 要自包含：背景、范围、判据、要读哪些文件、已知的坑）
- **Prompt 必须以分支检查开头**：先 `git branch --show-current`，在 `main` 上就先
  `git checkout -b <建议名>` 再动手，已在别的分支则先确认是不是本任务的分支。
  建议分支名要出现在**三处**：文档抬头的状态块、Prompt 里的 `checkout -b`、
  以及 `docs/todo/README.md` 表格的「建议分支」列——只写在 Prompt 里的话，
  用户扫一眼清单时看不见
- 这些是**做完就删**的临时文档，只用来提醒用户，不是长期设计资料
- 用户会从 `docs/todo/` 里挑下一个方向

与 CLAUDE.md「已知后续工程项」的分工：那份是**长期登记**（spec 明确不做的、
跨章节的工程债），`docs/todo/` 是**近期待选**（用户随时可能挑来做的下一件事）。
同一件事可以两边都有，但 `docs/todo/` 里那份要带 Prompt 和可执行细节。

**Why:** 用户要的是「打开文件夹就能挑一个开工」，而记忆和长清单都做不到——
记忆他看不见，长清单里的条目没有足够上下文直接开工。

**How to apply:** 听到「后续再做 / 先记下 / 暂时不改」时，除了当场答复，
还要在 `docs/todo/` 建一份带 Prompt 的文档。见 [[feedback_commit_after_each_change]]。
