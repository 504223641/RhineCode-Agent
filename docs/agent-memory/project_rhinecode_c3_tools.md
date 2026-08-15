---
name: project-rhinecode-c3-tools
description: RhineCode c3 工具系统已实现（仅 DeepSeek），逻辑层验收通过，待 TUI 实时端到端验收
metadata:
  type: project
---

c3「工具系统」已按 spec 流程开发完成（docs/c3 四份文档），让模型从纯对话变为能调用工具的 Agent。仅 DeepSeek Provider 启用工具（OpenAI 兼容 tool_calls）。

**Why:** 这是迈向 Agent 的核心一步，建立了 Tool 抽象/注册中心/两轮往返编排，为后续章节（跨轮 Agent Loop、其它 Provider 工具支持）打基础。

**How to apply:** 下次可直接做端到端验收或开发下一章；新增工具只需在 `tools/` 继承 Tool 并在 `registry.default()` 登记一行。

## 范围与关键决策（来自需求澄清）
- 仅 DeepSeek 支持工具；anthropic/openai 仅接口加 `tools` 参数但忽略
- 写/改/命令为有副作用工具，执行前确认；只读工具免确认。确认 UI 已从模态弹窗改为**内联面板** `ConfirmPanel`（镜像 CommandPanel，输入框上方弹出，方向键选择+回车+Esc，默认高亮「执行」，确认时焦点临时移到面板）
- 单轮往返：调工具→执行→结果回灌→自动再请求一次给最终回答→停。**不做跨轮 Agent Loop**（第二轮的 tool_call 被忽略）
- 并发：只读工具 ThreadPoolExecutor 并发，有副作用工具串行（避免写冲突）
- 工具行实时计时：橘色执行中→绿成功/红失败（ToolCallWidget 用 set_interval 主线程计时）
- 确认交互用 push_screen+回调+threading.Event 阻塞 Worker（非 push_screen_wait，避免线程 worker 上下文问题）

## 新增/修改文件
- 新增 `rhinecode/tools/`：base.py(Tool/ToolResult)、registry.py、read_file/write_file/edit_file/run_command/glob_files/grep_content.py
- 改 `provider/base.py`：新增 ToolCall，Message 加 tool_calls/tool_call_id，StreamChunk 加 tool_call/tool_result + 新 type(tool_call/tool_start/tool_result)，stream_chat 加 tools
- 改 `provider/deepseek.py`：_to_sdk_messages 三类消息转换、流式 tool_calls 按 index 拼接、arguments 非法→None
- 改 `conversation.py`：两轮往返编排、_execute 并发/串行、confirm_callback
- 改 `tui/widgets.py`：ToolCallWidget、HistoryView.add_tool_widget、ConfirmScreen
- 改 `tui/app.py`：_do_stream 处理 tool_start/tool_result、_confirm_tool、_summarize_result
- 改 `__main__.py`：注入 ToolRegistry.default()

## 验收状态
逻辑层全部通过（AC1-AC10、AC13、AC14 并发证据 0.5s<1.0s、AC16）+ AC9 用模拟流式分片验证拼接/非法JSON + AC11/12/13 确认回调触发逻辑。
**待人工验收**：AC15(配色)、AC17(可视化)、3 个端到端场景——需交互式 TUI + 真实 DeepSeek key（config.yaml 已存在）。

详见 [[project-rhinecode-mvp]]。
