---
name: project_rhinecode_deepseek_only
description: RhineCode 后续所有开发只针对 DeepSeek Provider，不再为 Anthropic/OpenAI 做兼容设计
metadata:
  type: project
  modified: 2026-07-25T15:55:20.393Z
---

2026-07-25 用户明确：RhineCode 后续开发只考虑 DeepSeek 一个 Provider，其它模型供应商暂不考虑，**该约束适用于整个项目的所有后续开发**（不限于当前章节）。

**Why:** 项目里 anthropic.py / openai.py 目前只有纯对话能力，工具调用、Plan Mode、权限系统、上下文压缩（c8）、自动笔记（c9）都只在 `protocol: deepseek` 下生效。继续为另外两个 Provider 铺兼容分支，等于为不会走的路径付设计与测试成本。

**How to apply:** 新功能直接以 DeepSeek 工具模式为唯一目标写 spec/plan，不再增加「当前 Provider 不支持 X」这类多 Provider 分支设计；已有的 `tools_enabled` 判断保留即可（改动成本高于收益），但不要为它扩展新的能力降级路径。相关章节见 [[project_rhinecode_c11_skills]]。
