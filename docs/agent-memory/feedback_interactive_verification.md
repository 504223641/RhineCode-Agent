---
name: feedback_interactive_verification
description: 需要人眼看渲染的 TUI 验收由用户亲自跑；但能无头驱动的交互场景应由 Claude 跑完
metadata:
  type: feedback
  modified: 2026-07-27T20:07:26.244Z
---

需要**人眼判断**的验证（TUI 渲染观感、tmux 里真人按键、面板美不美观、
工具行是不是逐个出现）由**用户亲自操作**，Claude 只给分步步骤 + 每步预期。

**但边界在「是否必须人眼」，不在「是否叫做手测」。**

**Why:** 原始反馈来自 RhineCode c6 验收（用户打断了 Claude 的 Pilot 自动驱动，
要「给我步骤我来操作测试」）。但 2026-07-28 P1a 交付时我把这条**过度套用**了——
把 6 条端到端场景全列成「交给用户跑」，用户当场反问「你可以来测试啊，为什么让我测试」。
他是对的：P1a 这套驱动设施（`tests/e2e/`）**存在的全部意义**就是把这类交互场景
变成可无人驱动的，用它自己的产物去要求用户手动跑，是自相矛盾的。
实跑那 5 条时还当场抓到 2 个真缺陷。

**How to apply:** 交付验收场景时先逐条问「这条**必须**人眼吗」：
- **必须人眼**（渲染观感、颜色、布局、流畅度）→ 给步骤，用户跑。
- **花用户的钱或用其真实凭据**（真实模型调用）→ 给步骤，**先征求授权**再跑。
- **其余一律 Claude 跑完并贴实际输出作证据**——尤其是本项目已经有
  `tests/e2e/` 驱动设施可用的场景（`python -m tests.e2e.host` +
  `python -m tests.e2e.client`，见 [[project_rhinecode_trace_p1]]）。

参见 [[project_rhinecode_c6_permissions]]。
