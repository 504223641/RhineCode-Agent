# 全阶段真实模型端到端复测（2026-07-31）

> **这次复测是干什么的**：C2–C11 的端到端场景，此前大多是在没有 trace 记录器、
> 没有驱动设施的年代靠人眼在终端里过的。P0（trace）与 P1a（驱动设施）建成之后，
> 这批场景第一次具备了「机器可复核」的条件。本次扫描的目的是**在开新功能之前，
> 确认既有能力仍然稳定、正确、健壮**，而不是验收某个新特性。

## 方法

- **驱动**：`tests/e2e/host.py --mode live` 常驻宿主 + `tests/e2e/client.py` 瘦客户端，
  经本机回环通道驱动真实界面。所有输入走真人提交入口，所有面板应答走第⑤层人在回路。
- **取证**：全程开 trace 记录器，判据一律引用 trace 事件（`tool_execute` / `api_request` /
  `api_response` / `ui_message` / `status_bar` / `tool_decision` 等十五类）的**原文**。
- **模型**：`deepseek-v4-flash`，`context_window=1000000`，真实凭据。
- **不改产品代码**：本次只观测与记录。发现的问题一律记进报告，不在本分支修复。

## 每条判据分两栏写

沿用 `docs/c11/acceptance/` 的体例：**机器判到了什么**（trace 里可复核的事实）与
**据此做的判断**（人对「这算不算通过」的裁定）。前者可复核、后者可争论，
混在一起就都不可信了。

## 报告导航

| 报告 | 覆盖 |
| --- | --- |
| [c2.md](c2.md) | 对话基础（流式、多轮记忆、/clear、/think、错误处理、退出） |
| [c3.md](c3.md) | 工具系统（读取并回答、确认拒绝、edit_file 错误路径） |
| [c4.md](c4.md) | Agent Loop 与 Plan Mode |
| [c5.md](c5.md) | 结构化系统提示与缓存策略 |
| [c6.md](c6.md) | 五层防御权限系统 |
| [c7.md](c7.md) | MCP 客户端 |
| [c8.md](c8.md) | 上下文管理（两层压缩） |
| [c9.md](c9.md) | 记忆系统 |
| [c10.md](c10.md) | 斜杠命令系统 |
| [c11-align.md](c11-align.md) | Skill 系统（**对齐改造后的现行口径**，不测旧版设计） |
| [ext-web-fetch.md](ext-web-fetch.md) | 网络访问扩展 |
| [ext-skill-authoring.md](ext-skill-authoring.md) | Skill 作者期扩展 |
| [testing-p0-trace.md](testing-p0-trace.md) | 测试设施：trace 记录器自身 |
| [testing-p1a-driver.md](testing-p1a-driver.md) | 测试设施：驱动设施自身 |
| **[summary.md](summary.md)** | **综合报告**：总览、问题清单、未覆盖项、开工前结论 |

## 范围裁定（开跑前与用户确认）

1. **C11 只测 `align/` 现行口径**，旧版 11 条场景不测——冲突处以 align 为准，
   测旧版等于验一个已经被推翻的口径。
2. **点名 Anthropic / OpenAI 后端的场景一律记「不适用 · 已改口径」**
   （C2 AC4/AC5/AC6、C4 场景 13、C9 AC20）。项目已定「只针对 DeepSeek 开发」，
   这几条验的是明确不再维护的路径，不计入通过/失败，只记「未覆盖及原因」。
