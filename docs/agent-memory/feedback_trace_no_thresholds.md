---
name: feedback-trace-no-thresholds
description: trace 不许有任何截断阈值——观测设施丢内容就不再是可信证据；裁剪只能发生在「给模型看的那一份」上
metadata:
  type: feedback
  modified: 2026-08-09T00:59:14.066Z
---

**trace 的内容必须完整，不许有任何阈值。** 用户在 2026-08-09 明确否掉了我
「给 system 字段单开一个更大阈值」的折中方案，要求所有字段一律不截断。

**Why:** 观测设施一旦自作主张地丢内容，从它得出的每个结论都要打折——读的人
永远无法判断被切掉的那部分里有没有答案。实测证据：结构化系统提示在最小配置下
已有 3886 字符、贴着当时 4000 的线，而稳定通道尾部依次是 134 组队协作 /
135 角色清单 / 140 Skill 清单，任何一份真实 RHINE.md 一进来，被切掉的正好是
那三段清单——也就是「模型到底看没看到这个能力」唯一的证据。

**How to apply:**
- 记录层零阈值。`trace/models.py` 现在只有 `full_text()`，`MAX_FIELD_CHARS` /
  `MAX_MESSAGE_ITEMS` / `clip()` 已删除并有反证用例钉住不得复活。
- **裁剪只允许发生在「给模型看的那一份」上**，且必须同时另存完整原文进 trace
  （`ToolResult.full_output` / `ActionOutcome.full_detail`）。这条是成对维护点，
  漏填不报错、内容永久消失且无痕迹。
- 记录文件因此显著变大（`api_request` 每轮含完整历史，长会话数十 MB），
  这是刻意付的代价；连带后果是产物比以前更敏感（大配置文件现在逐字全量落盘），
  见 [[project-rhinecode-trace-recorder]]。
- 同一条原则推广到「任何操作都要有能追溯的完整记录」：发现某个操作在时间线上
  没有对应事件时，那是缺口而不是设计（如 `system_serial` 的七个工具此前
  一条 `permission_decision` 都不产）。
