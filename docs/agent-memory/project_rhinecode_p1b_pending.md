---
name: project-rhinecode-p1b-pending
description: P1b（无人值守回归）待做，已明确范围与必须修正的取样方法；做 Skill 作者期之后再问用户要不要开
metadata:
  type: project
  modified: 2026-07-28T11:17:29.002Z
---

**P1b 待做，用户已同意先放着**（2026-07-28 商定）：先做 Skill 作者期那几项
（`/skills` 体检、内置 `skill-author`、`/skills new` 脚手架、已激活提示），
**全部做完后主动问用户要不要开 P1b**，不要擅自开始。

## 范围

1. **`send --force`**：解锁 checklist 场景 9（运行中的输入反馈）。现在
   `DriverCore.send` 前置检查写死「只有 idle 才能提交」，忙碌态下提交递不进产品，
   被拦的是驱动器自己的守卫。**五处成对维护点齐改**：`tests/e2e/protocol.py`
   + `control.py` + `host.py` 的 dispatch 分支 + `client.py` + `test_e2e_control.py`。
2. **把 10 条 C11 场景 + 6 条 P0 场景剧本化成无人值守回归**。素材现成：
   `tests/e2e/p0_scenarios.py` 与 `tests/e2e/c11_scenarios.py`。

## ⚠️ 必须修正的取样方法（P1b 的成败在这里，不在代码）

上一轮 C11 验收 43/43 全过，却完全漏掉了「Skill 作者期」这一整层。
**根因不是漏测某一条，是取样偏了**：五个被测 Skill 全是验收者自己写的，
而验收者知道契约（description 要短、要放 `$ARGUMENTS`、白名单该多窄、
动作型 vs 指导型），于是写出来的天然适配好。用户随手导入一份外部 Skill，
四个问题当场全撞上。

**教训：用自己写的 fixture 验自己设计的 spec，验不出假设本身的问题。**

P1b 的 fixture 必须包含：
- **至少一份外来 Skill** —— `tests/e2e/c11_scenarios.py:seed_foreign_skill` 已备好
  （原样搬入、一个字不改，源路径可用环境变量 `RHINE_E2E_FOREIGN_SKILL` 指定）；
- **故意拒绝几次**，不要全程自动放行 —— 这次就是靠拒绝验出了真问题；
- **一条不带预设目标的真实需求**，看链路自然会走成什么样，而不是奔着判据去。

## 同时登记：明确不做的三项

- `/skills import` 自动适配 —— 格式差异是语义的不是语法的，代码只能猜，
  **猜错是静默的**；交给内置 `skill-author`（模型判断 + 用户确认）
- Plan Mode 规划阶段工具强校验（已知工程项 #2，已有真实物证）
- `RETAIN_TOKENS` 随窗口缩放（已知工程项 #8）

**Why:** 驱动设施本身已经够用（P1a 验完 16 条场景零产品改动），P1b 的增量价值
主要在「无人值守」，而它的风险在于把偏了的取样固化成回归测试——那会让
「测试全绿」长期掩盖同一类盲区。

**How to apply:** 开工前先重读本条的「取样方法」一节。
见 [[project_rhinecode_trace_p1]]、[[project_rhinecode_c11_skills]]。
