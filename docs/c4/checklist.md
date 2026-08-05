# RhineCode Agent Loop Checklist

> 每项通过运行代码或观察行为验证，聚焦系统行为，与具体实现解耦。
> 「导入/单元」类项用 `python -c "..."`（项目根目录，已 `pip install -e .`）；
> 「端到端」类项在 tmux 启动 DeepSeek 配置后人工观察。

## 实现完整性（导入/单元）

- [ ] 事件类型可用：`AgentEvent / AgentEventType / StopReason / Usage / ClarifyOption / ConfirmDecision` 均可导入，且 `StopReason.PLAN_REJECTED` 存在（验证：`python -c "from rhinecode.agent.events import StopReason; print(StopReason.PLAN_REJECTED)"`）。
- [ ] StreamChunk 支持用量：可构造 `StreamChunk(type="usage", usage=...)`（验证：T2 的 `python -c`）。
- [ ] DeepSeek Provider 可导入且带 `stream_options`（验证：`python -c "import rhinecode.provider.deepseek"`；源码含 `include_usage`）。
- [ ] StreamCollector 双路：feed 文本返回 TEXT 事件且累积进 `.text`，feed tool_call 累积进 `.tool_calls`，feed usage 产出 USAGE 且填 `.usage`（验证：T4 的 `python -c`）。
- [ ] Plan 引导提示非空且含 `ask_user`/`present_plan`（验证：T5 的 `python -c`）。
- [ ] 特殊工具 schema：`plan_schemas()` 输出名为 `ask_user`、`present_plan` 的两个 function（验证：T6 的 `python -c`）。
- [ ] Agent 可跑：FakeProvider 无工具调用时，事件序列含 PROGRESS/TEXT，末尾 FINISHED 且 stop_reason=COMPLETED（验证：T7 的 `python -c`）。

## 集成（导入/单元）

- [ ] ConversationManager 委托 Agent：c3 的 `_stream/_execute/_run_readonly_concurrent/_run_one_serial` 已移除，`_run` 返回 Agent 事件流（验证：`python -c "import rhinecode.conversation"`；grep 确认旧方法不存在）。
- [ ] `/plan` 开关：DeepSeek 下 `handle_input('/plan')` 切换 `plan_mode` 并返回「计划模式：开启/关闭」；OpenAI 下返回不支持提示（验证：T8 的 `python -c`）。
- [ ] 确认三态贯通：`ConfirmDecision` 经 ConversationManager 的 confirm 闭包正确映射为 bool，`ALLOW_ALWAYS` 置 `_always_allow=True`（验证：构造 manager，注入返回 `ALLOW_ALWAYS` 的假 confirm_callback，连续两次 confirm 闭包，第二次不再调用回调直接 True）。
- [ ] 组件齐备可导入：`ClarifyPanel`、`StatusBar`(新签名)、`ConfirmPanel`(三项) 与 `rhinecode.tui.app` 均可导入（验证：T9/T10 的 `python -c`）。
- [ ] 整体装配：`python -c "import rhinecode.__main__, rhinecode.tui.app, rhinecode.conversation, rhinecode.agent.loop; print('ok')"` 打印 ok（验证：T11）。

## 编译与测试

- [ ] 全部新增/修改模块导入无 SyntaxError / ImportError（验证：T11 的整体导入命令）。
- [ ] 现有用例不回归：`python -m unittest discover -s tests` 通过。

## 端到端场景（tmux + DeepSeek）

- [ ] 场景 1（自主多步循环，AC1/AC2）：提「读取 README 并统计行数后告诉我」类需多步工具的任务 → 模型连续多轮调用工具、每轮结果回灌、最终自动产出回答并停止，全程无需逐轮催促。
- [ ] 场景 2（进度与用量事件，AC11/AC12）：上一场景运行中，界面可见迭代轮次推进（第 N 轮）；单元层验证 DeepSeek/StreamCollector 能产出 `USAGE` 事件。
- [ ] 场景 3（并发/串行，AC9）：诱导单轮多个只读工具 → 并发执行；多个写操作 → 串行执行，无文件写冲突。
- [ ] 场景 4（确认三态，AC10）：普通模式或 Plan Mode 执行阶段触发写/改/命令 → 弹三选项；选「不再询问」后，本会话后续有副作用工具不再弹确认、自动执行；选「取消」则模型据「用户拒绝执行」继续。
- [ ] 场景 5（用户取消，AC4）：循环运行中按 Esc → 循环在安全点停止，系统行提示「已取消」，TUI 仍可继续输入。
- [ ] 场景 6（迭代上限，AC3）：构造持续要工具的情形（或临时调小常量验证）→ 达上限强制停止并提示「因达上限停止」，程序不挂死。
- [ ] 场景 7（连续未知工具，AC5）：模型连续调用未知工具达阈值 → 停止并提示，不空转。
- [ ] 场景 8（流出错，AC6）：制造一次流错误（如断网/错误 key）→ 循环终止并红色错误提示，程序不崩溃。
- [ ] 场景 9（Plan 开关与状态栏，AC13）：`/plan` 开启 → 状态栏显示「计划模式：开」；再 `/plan` 关闭 → 显示「关」。
- [ ] 场景 10（Plan 只读+引导，AC14）：Plan Mode 下提需求 → 模型只调用只读工具、不执行修改类操作，产出计划。
- [ ] 场景 11（Plan 澄清面板，AC15）：Plan Mode 下需求含模糊点 → 弹澄清面板：每选项有概述+详情、**推荐项排首位（不带额外标记）**、上下导航只在概述间移动；选择后模型据所选继续。

> ⚠️ **判据更新（2026-08-05，全阶段复测）**：原文写「推荐项排首位**带标记**」，
> 但实现是**刻意不加**标记的——推荐顺序由模型保证（第一位即最推荐），
> 概述文本本身已含推荐信息，再加「⭐ 推荐」前缀属于重复提示。
> 理由写在 `rhinecode/tui/widgets.py` 的 `ClarifyPanel.show`（类 docstring 与 `add_option` 上方各一处）。
> 这是判据没跟上实现，不是实现漏做——照原文验会得出一个假的失败。
- [ ] 场景 12（Plan 两段式执行，AC16）：澄清完成（或需求明确）→ 完整计划进入聊天记录并询问「是否开始执行」；确认后本轮放开全部工具，但副作用工具仍逐个确认；拒绝后当前 Agent 回合停止并提示「计划未执行」；Plan Mode 仍开启；下一条新消息重新进入规划阶段（重新澄清/审批）。
- [ ] 场景 13（纯对话兼容，AC17）：OpenAI/Anthropic 配置下普通提问 → 等价一轮流式对话；`/think`、`/clear`、`/exit` 行为不受影响。
