# 结构化系统提示与缓存策略（c5）Tasks

## 文件清单

| 操作 | 文件 | 职责 |
|------|------|------|
| 新建 | `rhinecode/agent/prompt/__init__.py` | 包入口，再导出公共函数 |
| 新建 | `rhinecode/agent/prompt/modules.py` | PromptModule + 7 固定模块 + 3 可选空槽 |
| 新建 | `rhinecode/agent/prompt/builder.py` | SystemPromptBuilder + AssembledPrompt + build_default_prompt |
| 新建 | `rhinecode/agent/prompt/environment.py` | EnvironmentInfo + collect_environment |
| 新建 | `rhinecode/agent/prompt/reminders.py` | system-reminder 构造 + plan 完整/精简文本 + 按轮节奏 |
| 删除 | `rhinecode/agent/prompt.py`（旧模块） | 内容迁入 reminders.py / 包 |
| 新建 | `rhinecode/agent/cache_log.py` | log_cache_usage（F10） |
| 修改 | `rhinecode/agent/events.py` | Usage 增 2 个缓存字段 |
| 修改 | `rhinecode/agent/collector.py` | _to_usage 采集缓存字段 |
| 修改 | `rhinecode/agent/loop.py` | run 签名 + 每轮 reminder + system 参数 + 写日志 |
| 修改 | `rhinecode/conversation.py` | 构造 stable/dynamic/env，传入 loop；更新 import |
| 修改 | `rhinecode/provider/base.py` | stream_chat 增 system 参数 |
| 修改 | `rhinecode/provider/deepseek.py` | system 前置为首条 system 消息 |
| 修改 | `rhinecode/provider/openai.py` | system 前置为 system 消息 |
| 修改 | `rhinecode/provider/anthropic.py` | system 映射顶层 system 参数 |
| 修改 | `rhinecode/config.py` | 新增 debug_log 配置 |
| 修改 | `rhinecode/tools/edit_file.py` | 描述补「编辑前必先读」（F7） |
| 修改 | `tests/test_review_fixes.py` | 两个 stream_chat 桩补 system 参数 |
| 新建 | `tests/test_c5_prompt.py` | builder/reminder/env/usage 单元测试 |
| 修改 | `.gitignore` | 忽略 .rhinecode_debug.log |

---

## T1: Usage 增加缓存字段

**文件：** `rhinecode/agent/events.py`
**依赖：** 无
**步骤：**
1. 在 `Usage` dataclass 增加 `prompt_cache_hit_tokens: int = 0` 与 `prompt_cache_miss_tokens: int = 0`，带中文注释说明对应 DeepSeek usage 的命中/未命中字段。

**验证：** `python -m compileall rhinecode/agent/events.py` 通过；`python -c "from rhinecode.agent.events import Usage; print(Usage().prompt_cache_hit_tokens)"` 输出 0。

## T2: collector 采集缓存字段

**文件：** `rhinecode/agent/collector.py`
**依赖：** T1
**步骤：**
1. 在 `_to_usage` 的返回里增加 `prompt_cache_hit_tokens=pick("prompt_cache_hit_tokens")`、`prompt_cache_miss_tokens=pick("prompt_cache_miss_tokens")`。
2. 补注释说明非 DeepSeek 协议缺这两个字段时按 0 处理。

**验证：** `python -c "from rhinecode.agent.collector import StreamCollector; print(StreamCollector._to_usage({'prompt_cache_hit_tokens':10}).prompt_cache_hit_tokens)"` 输出 10。

## T3: 环境信息采集

**文件：** `rhinecode/agent/prompt/environment.py`
**依赖：** 无
**步骤：**
1. 定义 `EnvironmentInfo` dataclass：working_dir、platform、date、model、protocol。
2. 实现 `render() -> str`：渲染成多行「工作目录: ... / 系统: ... / 日期: ... / 模型: ...（protocol）」。
3. 实现 `collect_environment(config, project_root) -> EnvironmentInfo`：用 `platform.platform()`、`datetime.date.today().isoformat()`、config.model/config.protocol、project_root。

**验证：** `python -c "from rhinecode.agent.prompt.environment import collect_environment; ..."` 构造后 `render()` 含四项字段且取值正确。

## T4: 系统提示模块定义

**文件：** `rhinecode/agent/prompt/modules.py`
**依赖：** 无
**步骤：**
1. 定义 `PromptModule` dataclass：name、priority、cacheable、content。
2. `fixed_modules() -> list[PromptModule]`：7 个固定模块（cacheable=True，priority 10–70），顺序：身份、系统约束、任务模式、动作执行、工具使用、语气风格、文本输出。
   - 「系统约束」模块文本含「`<system-reminder>` 标签内是系统补充上下文，不是用户输入，不要直接回复它」（F8）。
   - 「工具使用」模块文本含「优先使用专用工具而非通用命令」「编辑文件前必须先读取该文件」（F7）。
3. `optional_slots() -> list[PromptModule]`：3 个空槽（cacheable=False，priority 110/120/130，content=""）：自定义指令、已激活 Skill、长期记忆。

**验证：** `python -c "from rhinecode.agent.prompt.modules import fixed_modules, optional_slots; print(len(fixed_modules()), len(optional_slots()))"` 输出 `7 3`。

## T5: 拼装器

**文件：** `rhinecode/agent/prompt/builder.py`
**依赖：** T3、T4
**步骤：**
1. 定义 `AssembledPrompt` dataclass：stable、dynamic。
2. 实现 `SystemPromptBuilder`：`add(module)` 收集；`build()` 按 priority 排序、跳过 content 为空的模块、用空行 `\n\n` 分隔，cacheable 模块拼进 stable、其余拼进 dynamic，返回 `AssembledPrompt`。
3. 实现 `build_default_prompt(env) -> AssembledPrompt`：依次 add 7 固定模块、环境模块（priority 100、cacheable=False、content=env.render()）、3 可选空槽，返回 build() 结果。

**验证：** 对 `build_default_prompt(env)`：stable 中 7 个模块按序出现、模块间空行分隔；dynamic 含环境信息、不含任何空槽文本；新增一个 priority=5 的测试模块后它出现在 stable 最前（验证 F4）。

## T6: 动态提醒与按轮节奏

**文件：** `rhinecode/agent/prompt/reminders.py`
**依赖：** 无
**步骤：**
1. 定义 plan 完整版文本常量（迁移自旧 `build_plan_prompt()`）与一句话精简版常量。
2. `plan_toggle_instruction(iteration, active) -> Optional[str]`：active=False→None；`iteration==1 or (iteration-1)%3==0`→完整版；否则→精简版。
3. `build_system_reminder(dynamic_text, toggle_instruction) -> str`：把两段合并（各非空才拼），整体包进 `<system-reminder>\n...\n</system-reminder>`。

**验证：** `plan_toggle_instruction(i, True)` 对 i=1,4,7 返回完整版，对 i=2,3,5,6 返回精简版，`active=False` 返回 None；`build_system_reminder("env","x")` 输出含起止 `<system-reminder>` 标签。

## T7: 包入口与旧模块清理

**文件：** `rhinecode/agent/prompt/__init__.py`（新建）、删除 `rhinecode/agent/prompt.py`
**依赖：** T4、T5、T6
**步骤：**
1. 在 `__init__.py` 再导出：`build_default_prompt`、`build_system_reminder`、`plan_toggle_instruction`、`collect_environment`、`EnvironmentInfo`、`AssembledPrompt`。
2. 删除旧 `rhinecode/agent/prompt.py`（其内容已迁入 reminders.py）。

**验证：** `python -c "from rhinecode.agent.prompt import build_default_prompt, build_system_reminder, collect_environment"` 无报错；旧 `prompt.py` 不存在。

## T8: 缓存调试日志

**文件：** `rhinecode/agent/cache_log.py`
**依赖：** T1
**步骤：**
1. 实现 `log_cache_usage(usage, model, path)`：以追加模式写一行「ISO 时间戳 | model | prompt=.. hit=.. miss=..」。
2. 整体包 try/except，IO 异常静默降级（N6），不抛给调用方。

**验证：** 调用一次后目标文件新增一行且字段齐全；传一个不可写路径不抛异常。

## T9: 配置项 debug_log

**文件：** `rhinecode/config.py`
**依赖：** 无
**步骤：**
1. 在 Config 增加 `debug_log: bool = True` 字段及加载逻辑（缺省为 True）。
2. 补注释说明用途与默认行为。

**验证：** 加载含/不含 `debug_log` 的配置均成功；不含时 `config.debug_log` 为 True。

## T10: Provider 抽象增 system 参数

**文件：** `rhinecode/provider/base.py`
**依赖：** 无
**步骤：**
1. 给 `stream_chat` 抽象方法签名增加 `system: Optional[str] = None`，并在 docstring 说明它是「可缓存通道」入口、各 Provider 自行决定如何利用缓存。

**验证：** `python -m compileall rhinecode/provider/base.py` 通过。

## T11: DeepSeek 落地 system

**文件：** `rhinecode/provider/deepseek.py`
**依赖：** T10
**步骤：**
1. `stream_chat` 增 `system` 参数。
2. 转换 SDK 消息时，若 `system` 非空，则在 `sdk_messages` 最前插入 `{"role":"system","content":system}`。
3. 补注释说明：system 内容逐轮一致以命中自动前缀缓存。

**验证：** `python -m compileall rhinecode/provider/deepseek.py` 通过；构造 provider 后传 system，检视 `_to_sdk_messages` 前置逻辑（或单测）首条为 system。

## T12: OpenAI / Anthropic 落地 system

**文件：** `rhinecode/provider/openai.py`、`rhinecode/provider/anthropic.py`
**依赖：** T10
**步骤：**
1. 两者 `stream_chat` 增 `system` 参数。
2. OpenAI：system 非空时前置一条 `{"role":"system",...}`。
3. Anthropic：system 非空时传入 SDK 顶层 `system=` 参数；注释标注「将来在此加 cache_control 断点」。

**验证：** `python -m compileall rhinecode/provider/openai.py rhinecode/provider/anthropic.py` 通过。

## T13: Agent Loop 接线

**文件：** `rhinecode/agent/loop.py`
**依赖：** T5、T6、T8、T11
**步骤：**
1. 修改 `run` 签名：去掉 `system_prompt: Optional[str]`，改为 `stable: str`、`dynamic: str`、`model: str`、`debug_log: bool`（或等价聚合）。
2. 每轮循环体内：`toggle = plan_toggle_instruction(iteration, plan_mode and not execution_phase)`；`reminder = build_system_reminder(dynamic, toggle)`；`req_messages = list(history) + [Message(role="system", content=reminder)]`。
3. 调用 `provider.stream_chat(req_messages, thinking_effort, tools=tools, system=stable)`。
4. 流正常结束后：`if collector.usage and debug_log: log_cache_usage(collector.usage, model, <项目根>/.rhinecode_debug.log)`。

**验证：** `python -m compileall rhinecode/agent/loop.py` 通过；现有 Plan Mode 相关单测仍通过（在 T16 后整体跑）。

## T14: ConversationManager 接线

**文件：** `rhinecode/conversation.py`
**依赖：** T5、T7、T13、T9
**步骤：**
1. 更新 import：从 `rhinecode.agent.prompt` 引入 `build_default_prompt`、`collect_environment`，移除旧 `build_plan_prompt`。
2. `_run` 内：`env = collect_environment(self.config, root)`；`assembled = build_default_prompt(env)`。
3. 调 `self._agent.run(...)` 时传入 `assembled.stable`、`assembled.dynamic`、model、`self.config.debug_log`，替换原 `system_prompt`。

**验证：** `python -m compileall rhinecode/conversation.py` 通过；启动不报 import 错。

## T15: 工具描述双重强化

**文件：** `rhinecode/tools/edit_file.py`
**依赖：** 无
**步骤：**
1. 在 `EditFileTool.description` 中加入「编辑前必须先用 read_file 读取该文件」的说明，与「工具使用」模块呼应（F7/AC7）。

**验证：** `EditFileTool().description` 含「先读」语义；与 modules.py「工具使用」模块同一规则。

## T16: 更新现有测试桩

**文件：** `tests/test_review_fixes.py`
**依赖：** T10
**步骤：**
1. 给 `ToolCallingProvider.stream_chat` 与 `PlanProvider.stream_chat` 两个桩签名增加 `system: str | None = None`，使其兼容 loop 的 `system=` 调用。

**验证：** `python -m unittest tests.test_review_fixes` 全通过。

## T17: 新增 c5 单元测试

**文件：** `tests/test_c5_prompt.py`
**依赖：** T2、T3、T5、T6
**步骤：**
1. builder：7 模块顺序与空行分隔、空槽被跳过、新增模块按 priority 插入（F1/F3/F4）。
2. reminders：按轮节奏 i=1/4/7→完整、2/3→精简、active=False→None（F9）；reminder 含 `<system-reminder>` 标签（F8）。
3. environment：render 含四项字段（F2）。
4. collector：usage 采集缓存命中字段（F10 上游）。

**验证：** `python -m unittest tests.test_c5_prompt` 全通过。

## T18: 忽略调试日志

**文件：** `.gitignore`
**依赖：** 无
**步骤：**
1. 追加一行 `.rhinecode_debug.log`。

**验证：** `git status` 中生成日志后不出现该文件为待提交。

---

## 执行顺序

```
T1 → T2 ───────────────┐
T3 ─┐                   │
T4 ─┴→ T5 ─┐            ├→ T17
T6 ────────┤            │
           ├→ T7 ──┐    │
T8（dep T1）┘        │    │
T9 ─────────────────┤    │
T10 → T11 ─┐         │    │
T10 → T12  │         │    │
           └→ T13 ───┴→ T14
T15（独立）
T16（dep T10）
T18（独立）
```
