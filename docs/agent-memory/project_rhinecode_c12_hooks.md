---
name: project-rhinecode-c12-hooks
description: "C12 Hook 系统已完成开发与全部验收（含真实模型），PR #16 待合并；实跑撞出的复合命令缺陷已修，C6 同缺口已立项未修"
metadata:
  type: project
  modified: 2026-08-05T19:26:17.554Z
---

C12 Hook 系统（2026-08-06 完成）：在生命周期节点上挂用户声明的自动化动作。
**已经 PR #16 合并进 main、分支已删**（2026-08-06）。全量 1478 项全绿，Hook 专属 213 条。
文档在 `docs/c12/`（四份 + README + acceptance）。

**四个开工前拍板的决策，改任一条都要重走 spec**：
1A Hook 只能收紧不能放宽（无 allow，ask 只升级 ALLOW 不降级 DENY）；
2C 支持项目级 hooks.yaml，靠启动逐条列出对冲；
3A 拦截类 fail-closed 其余 fail-open；
3B 上下文只经 stdin JSON，配置里不做插值。

**真实模型实跑撞出的最重要一条**：条件 `command: "git push *"` 被复合命令
`git add x && git commit && git push origin main` 整个绕过——`match_command`
是整串匹配、不拆段，而①黑名单是拆的。**这不是攻击者构造的，是模型自然写出的形态**。
已在 Hook 侧补 `_match_command_field`（整条+逐段）。
**C6 的 ③规则层有同一缺口、未修**，见已知项 #12 与 `docs/todo/1-perm-compound-command.md`
（待办首位；关键取舍：只对 deny 拆段，allow 保持整串——拆 allow 是放宽）。

**另三处「单测看不见、跑起来才暴露」的坑**（都属于「观测设施撒谎但不报错」）：
- `startup_notice` 原本直接调 `append_system`，绕过 `_trace_ui_message` —— 界面对、记录空
- `permission_decision` 埋点原本排在 Hook 升级之前 —— 记 allow 而用户看到面板
- spec F2 边界第 2 条自相矛盾 —— 已留勘误块，不静默改写

**人眼三项已验完**：拦截文案删了末句「请他决定是否调整这条规则」（它把削弱防线摆上桌面）；
项目级提示**原来不合格**——走 `append_system` 的 `[dim]` 比正文还暗、且 `**粗体**` 在
Textual 里显示成字面星号，已新增 `append_warning` / `show_warning` 醒目通道；`/hooks` 排版保持现状。

相关：[[project_rhinecode_c11_skills]]、[[feedback_pr_format]]、[[feedback_todo_folder_convention]]
