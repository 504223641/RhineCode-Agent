---
name: project_rhinecode_c8_context
description: RhineCode c8 上下文管理（两层压缩）已实现并 172 测试全绿，估算锚点/存盘/摘要/熔断
metadata:
  type: project
---

RhineCode **c8 上下文管理（两层压缩）** 已完成开发，`python -m unittest discover -s tests` **172 通过**（140 既有零回归 + 32 新增）。走完 /spec 四文档流程，文档在 `docs/c8/{spec,plan,task,checklist}.md`。分支 `c8`。

**新增 `rhinecode/context/` 层**（对标 permission/mcp：纯逻辑 + 单点接入）：
- `estimate.py` — 近似 token 估算：锚点（上次 API `usage.prompt_tokens`，精确）+ 增量（锚点后新增消息按字符 `CHARS_PER_TOKEN=3.0` 估）。误差限于增量，无需 tokenizer。
- `offload.py` — 第一层预防：`Offloader.run` 两趟（单结果 >4K 存盘 / 合计 >16K 挑大依次存盘），幂等键 `tool_call_id`，只动 `role="tool"`，存盘到 `<项目根>/.rhinecode/context/<id>.txt`，历史留「预览+路径」占位。写盘失败保留原文。
- `summarize.py` — 第二层纯逻辑：`compute_retain_index`（尾部 10K token 或 ≥5 条，snap 回最近 user 保证不拆散 `assistant(tool_calls)`↔`tool`）、`render_transcript`（待摘要段渲成一条 user 转录，规避裸 tool 缺配对）、`parse_summary`（取 `<<<正式摘要>>>` 后正文丢草稿）、`reconstruct`（`[user(摘要), assistant(边界提示), *retained]`，边界 assistant 兼作 F12 提示+恢复角色交替）。
- `manager.py` — `ContextManager` 编排 + LLM 摘要调用（`stream_chat` tools=None）+ 熔断（连续 3 次失败）+ 会话状态（锚点/熔断/存盘集合）。`before_request`（自动，13K 余量）/`manual_compact`（/compact，3K 余量，未达阈值报 noop）/`record_usage`/`usage_report`/`reset`。

**接线点**：`loop.py` run() 增 `context_manager=None` 末位参数，每轮请求前 `before_request`（yield NOTICE）+ `sent_len=len(history)`（append reminder 前）+ usage 后 `record_usage`。`conversation.py` 仅 `_tools_enabled` 构造 ctx，加 `/context`（str）`/compact`（事件流走 Worker，因摘要阻塞不能卡 UI 线程）分支，clear() 调 reset()。`config.py` 加 `context_window`（默认 65536，`_parse_int` fail-safe）。`events.py` 加 `AgentEventType.NOTICE`，`app.py` `_do_stream` 渲染为系统行，`widgets.py` 补全列表。

仅 DeepSeek 工具模式生效。端到端 5 场景（真实 LLM 摘要 + TUI）留手测，同 [[project_rhinecode_c7_mcp]] 的 HTTP 手测惯例。关联 [[project_rhinecode_c6_permissions]]。
