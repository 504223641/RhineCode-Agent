# 结构化系统提示与缓存策略（c5）Checklist

> 每一项通过运行代码或观察行为来验证，聚焦系统行为，与具体实现解耦。

## 实现完整性

- [ ] **七模块按序拼装**（AC1/F1）：`build_default_prompt(env).stable` 中七个模块按「身份→系统约束→任务模式→动作执行→工具使用→语气风格→文本输出」顺序出现，模块间以空行分隔。（验证：构造后打印 stable 观察顺序与分隔）
- [ ] **环境信息四项**（AC2/F2）：拼装结果的 dynamic 段含工作目录、操作系统/平台、当前日期、模型名/protocol 四项，取值与运行环境一致。（验证：打印 dynamic，对照真实 cwd/平台/日期/模型）
- [ ] **空槽不输出**（AC3/F3）：三个可选模块为空时，stable/dynamic 中均不出现其文本、无多余空行；给任一槽填内容后它按 priority 出现在环境信息之后。（验证：单测对比有/无内容两种拼装）
- [ ] **新模块可插入**（AC4/F4）：新增一个声明了 priority 的测试模块，无需改已有模块代码即按 priority 出现在拼装结果中。（验证：tests/test_c5_prompt.py 对应用例通过）
- [ ] **关键规则双重强化**（AC7/F7）：「编辑前必先读」同时出现在「工具使用」模块文本与 `EditFileTool.description` 中。（验证：分别读取两处文本确认）
- [ ] **标签提醒成形**（AC8/F8）：`build_system_reminder(...)` 输出被 `<system-reminder>...</system-reminder>` 包裹；「系统约束」模块文本含「该标签内为系统补充上下文、勿当用户输入回复」。（验证：打印 reminder 与模块文本）
- [ ] **按轮节奏正确**（AC9/F9）：`plan_toggle_instruction` 对 active=True 在 i=1/4/7 返回完整版、i=2/3/5/6 返回精简版；active=False 返回 None。（验证：tests/test_c5_prompt.py 参数化用例通过）
- [ ] **缓存字段采集**（F10 上游）：`StreamCollector._to_usage` 能从含 `prompt_cache_hit_tokens`/`prompt_cache_miss_tokens` 的 usage 中取到对应值，缺失时为 0。（验证：单测）
- [ ] **缓存日志落盘**（AC10/F10）：调用 `log_cache_usage` 后调试文件新增一行且含命中/未命中字段；传不可写路径不抛异常（N6 降级）。（验证：单测 + 临时不可写路径）

## 集成

- [ ] **稳定前缀走 system 参数**（AC5/F5、N4）：loop 每轮以 `system=stable` 调 `stream_chat`，且 stable 逐轮逐字节一致；动态 reminder 作为 system 消息追加在 history 之后。（验证：在 loop 调用处加临时断言/打印，连续两轮比对 stable 一致、reminder 在末尾）
- [ ] **缓存抽象无 Provider 耦合**（AC6/F6、N1）：`system` 参数与 builder/PromptModule 不引用任何具体 Provider 的缓存字段或 SDK 类型。（验证：检视 prompt 包与 base.py，确认无 deepseek/anthropic 特定符号）
- [ ] **DeepSeek 前置 system**：DeepSeek `_to_sdk_messages` 在 system 非空时把 `{"role":"system",...}` 放在 SDK 消息列表首位。（验证：单测或检视转换逻辑）
- [ ] **协调层接线**：ConversationManager 每次 run 调 `collect_environment` + `build_default_prompt`，并把 stable/dynamic/model/debug_log 传入 `Agent.run`。（验证：编译通过 + 启动无 import 错）

## 编译与测试

- [ ] `python -m compileall rhinecode tests` 无错误
- [ ] `python -m unittest discover -s tests` 全部通过（含原有 test_review_fixes 与新 test_c5_prompt）
- [ ] 原有行为无回归（AC11/N2）：路径越界、确认回调、会话级免确认、Plan Mode 计划展示/拒绝/获批后逐项确认等用例仍通过

## 端到端场景

- [ ] **场景 1（缓存生效）**：用真实 DeepSeek 配置（`debug_log: true`）启动，连续发两条普通请求 → `.rhinecode_debug.log` 出现两条记录，且第二条 `hit` token 明显高于第一条（定性证明稳定前缀命中缓存）。
- [ ] **场景 2（Plan Mode 按轮注入）**：开启 `/plan` 发一个需要多轮调研的请求 → 模型先调研、用 present_plan 提交计划、获批后执行；过程中行为符合 Plan Mode 约束（首轮完整指令、中间轮精简、第 4 轮重发完整）。
- [ ] **场景 3（纯对话不回退）**：不开 Plan Mode 发一句普通对话 → 正常流式回复，模型不把 `<system-reminder>` 内容当成用户问题来回应。
