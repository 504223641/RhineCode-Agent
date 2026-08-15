---
name: project_rhinecode_c6_permissions
description: RhineCode c6 章节——五层防御权限系统的设计决策（决策管线、求值哲学）
metadata:
  type: project
---

RhineCode **c6 章节**：给 Agent 装一套**五层防御权限系统**，分支 `c6`。`/spec` 四份文档落盘在 `docs/c6/`。**T1–T12 已全部实现，66 个单测全绿（含 7 个 test_perm_*）**。参考 [[project_rhinecode_c3_tools]] 的工具系统。

**落地文件**：新增 `rhinecode/permission/`（models/matching/blacklist/rules/config/adapter/engine + __init__）；改 `tools/path_guard.py`（加 `is_within_workspace`）、`agent/events.py`（ConfirmDecision 四态：ALLOW/ALLOW_SESSION/ALLOW_PERMANENT/DENY）、`agent/loop.py`（`_execute` 决策预扫，`confirm`→`ask`，run 加 engine/ask 参数）、`conversation.py`（建 engine、ask 闭包、`/perm` 命令、删 `_always_allow`）、`tui/app.py` + `tui/widgets.py`（确认面板 4 选项 + 展示原因）；`.gitignore` 忽略 `*.local.yaml`；`permissions.example.yaml` 示例。

**架构（plan.md）**：新增独立 `rhinecode/permission/` 包（models/matching/blacklist/rules/config/adapter/engine），核心是纯逻辑 `PermissionEngine.decide(request)`；adapter 收口工具→规则映射；在 `loop._execute` 单点接入（DENY→结构化错误不停loop、ALLOW 副作用直接执行不再无条件确认、ASK→升级版 4 选 1 确认面板）。沙箱复用 `path_guard`（新增 `is_within_workspace`）。`confirm` 回调升级为 `ask`。新斜杠命令 `/perm` 循环切档。三层 YAML：`~/.rhinecode/permissions.yaml`、`<根>/.rhinecode/permissions.yaml`、`.local.yaml`（永久放行写本地、gitignore）。

**调研结论**：该设计本质是 Claude Code 权限模型的精炼版。核心原则——权限由工具层（代码）强制，不由模型/prompt 强制，可抵抗 prompt 注入。

**已锁定的核心决策（决策管线，从上到下，第一个下定论的关卡说了算）**：
1. **黑名单**：正则命中已知高危命令 → 拒。**不可被任何配置/模式放开**（circuit breaker）。
2. **沙箱**：文件路径越界（解析符号链接后做项目根前缀判断）→ 拒。复用/扩展现有 `path_guard.py`。
3. **可配置规则**：`Tool(pattern)` 语法（如 `Bash(git *)`），结果只有 allow/deny。**求值哲学选定「哲学 A：deny 永远优先」**——跨用户/项目/本地三层合并后，任意层 deny 命中即拒，deny 不可被 allow 翻案（不按近层覆盖远层）。
4. **权限模式**（严格/默认/放行）：**只兜底「第③层规则未命中」的灰色地带**，不能凌驾于黑名单/沙箱/deny 之上。严格→拒，默认→问用户(HITL)，放行→允许。放行模式也翻不了黑名单和 deny。
5. **HITL 人在回路**：弹确认面板，三档放行——本次/本会话/永久。

**关键行为**：权限被拒不终止 Agent Loop，把拒绝原因作为工具结果回灌给模型，让它调整策略。

**本阶段不做**：网络请求限制、资源配额、审计日志（留给后续章节）。

**配置格式**：规则写成 `工具名(模式)`，allow/deny 两种结果；三层 YAML（用户级 / 项目级 / 本地级），但求值用哲学 A 而非层级覆盖。
