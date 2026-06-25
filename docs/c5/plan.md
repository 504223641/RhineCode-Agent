# 结构化系统提示与缓存策略（c5）Plan

## 架构概览

### 核心技术决策：把「系统提示」从消息流里独立出来

c5 之后有**两类**系统内容，必须分通道处理：

| 类型 | 内容 | 通道 | 缓存 |
|------|------|------|------|
| 稳定系统提示 | 七个固定模块 | 新增的 `system` 参数（可缓存通道） | DeepSeek 自动前缀缓存命中 |
| 动态补充指令 | 环境信息、会话级开关提醒 | `messages` 里带 `<system-reminder>` 标签的 system 消息（消息通道） | 不缓存，放在历史之后 |

**决策：给 `BaseProvider.stream_chat` 增加 `system: Optional[str]` 参数**，作为「可缓存通道」的抽象入口。

- DeepSeek 落地：把 `system` 作为 SDK 消息序列的第一条 `{"role":"system",...}`，因其内容逐轮逐字节一致 → 命中自动前缀缓存。
- 扩展点（F6/N1）：将来 Anthropic 可把它映射成顶层 `system=[{...,"cache_control":{"type":"ephemeral"}}]`，无需改上层。

不沿用「塞进 messages 当 system 消息」的原因：那样无法把「稳定可缓存的系统提示」与「每轮变化的 system-reminder」在结构上分开，也没有地方给 Anthropic 的 `cache_control` 断点落脚。

### 整体数据流

```
ConversationManager（每次 run 开始）
  ├─ 构造一次「稳定系统提示」（7 模块拼装）        → 传给 loop 的 stable
  └─ 采集一次「环境信息」(cwd/os/date/model)        → 进 dynamic

Agent Loop（每一轮迭代）
  ├─ system 参数 = stable（逐轮不变 → 命中缓存）
  ├─ messages   = 历史 + 末尾追加一条 <system-reminder> 系统消息
  │                 （内含：dynamic 环境信息 + 按轮节奏的会话级开关提醒）
  ├─ tools      = 只读/全量工具 schema（稳定）
  ├─ 调用 provider.stream_chat(messages, thinking_effort, tools, system=stable)
  └─ 本轮结束：从 collector.usage 取缓存命中字段 → 写调试日志（F10）
```

`<system-reminder>` 放在历史**末尾**而非中间，是为了不破坏历史本身的前缀缓存——变化的提醒只追加成一个不缓存的「尾巴」。

## 核心数据结构

### PromptModule（modules.py）
一个系统提示模块：
```
name: str          # 模块名（身份/系统约束/.../环境信息/自定义指令...）
priority: int      # 优先级，越小越靠前；决定拼装顺序
cacheable: bool    # True→进稳定可缓存通道(system 参数)；False→进动态通道(reminder)
content: str       # 模块文本；空字符串表示「该模块本轮无内容」，拼装时跳过
```

### AssembledPrompt（builder.py）
拼装结果，按通道分离：
```
stable: str        # 所有 cacheable=True 模块按 priority 拼接（→ system 参数）
dynamic: str       # 所有 cacheable=False 模块按 priority 拼接（→ reminder）
```

### EnvironmentInfo（environment.py）
环境信息载体：
```
working_dir / platform / date / model / protocol
render() -> str    # 渲染成「工作目录: ... / 系统: ... / 日期: ... / 模型: ...」文本
```

### Usage 扩展（events.py，修改）
新增两个缓存字段（缺失按 0，兼容非 DeepSeek）：
```
prompt_cache_hit_tokens: int = 0
prompt_cache_miss_tokens: int = 0
```

## 模块设计

### modules.py
**职责：** 定义 `PromptModule` 与全部模块内容。
- `fixed_modules()`：返回 7 个固定模块（cacheable=True，priority 10–70）：身份、系统约束、任务模式、动作执行、工具使用、语气风格、文本输出。
  - 「系统约束」模块写明 `<system-reminder>` 是系统补充上下文、勿当用户输入回复（F8/AC8）。
  - 「工具使用」模块写明「优先用专用工具」「编辑前必先读」等关键规则（F7）。
- `optional_slots()`：返回 3 个可选空槽（cacheable=False，priority 110–130，content=""）：自定义指令、已激活 Skill、长期记忆。c5 恒为空 → 拼装时被跳过（F3）。
**依赖：** 无。

### environment.py
**职责：** 采集环境信息。
**对外接口：** `collect_environment(config, project_root) -> EnvironmentInfo`，用标准库取 cwd、`platform`、`date.today()`、配置里的 model/protocol。
**依赖：** config。无副作用、不跑 git。

### builder.py
**职责：** 拼装系统提示。
**对外接口：**
- `SystemPromptBuilder.add(module)` 收集模块；`build() -> AssembledPrompt` 按 priority 排序、跳过空 content、空行分隔，分别拼出 stable 与 dynamic。
- `build_default_prompt(env) -> AssembledPrompt`：注册 7 固定模块 + 环境模块（priority 100，cacheable=False，content=env.render()）+ 3 可选空槽。
**依赖：** modules、environment。新增模块只需多 `add` 一个 PromptModule（F4/AC4）。

### reminders.py
**职责：** 构造动态注入文本（迁入原 build_plan_prompt 内容）。
**对外接口：**
- `plan_toggle_instruction(iteration, active) -> Optional[str]`：按轮节奏（F9）返回完整版/精简版/None。规则：`active=False`→None；`iteration==1` 或 `(iteration-1)%3==0`（第 4、7、10…轮）→完整版；其余→精简版。
- `build_system_reminder(dynamic_text, toggle_instruction) -> str`：合并后包进 `<system-reminder>...</system-reminder>`，返回追加到消息末尾的文本。
**依赖：** 无。

### cache_log.py（新建）
**职责：** F10 调试日志。
**对外接口：** `log_cache_usage(usage, model, path)` 向日志文件追加一行（时间戳、模型、prompt_tokens、cache_hit、cache_miss），try/except 安全降级（N6）。
**依赖：** 无（接收已转好的 Usage）。

## 模块交互

**一次 run 开始时（ConversationManager._run）：**
```
collect_environment(config, root) → EnvironmentInfo
build_default_prompt(env)         → AssembledPrompt(stable, dynamic)
把 stable、dynamic 传给 Agent.run（替代原 system_prompt 参数）
```

**每一轮迭代内（Agent.run 循环体）：**
```
1. toggle = plan_toggle_instruction(iteration, active=plan_mode and not execution_phase)
2. reminder = build_system_reminder(dynamic, toggle)
3. req_messages = history + [Message(role="system", content=reminder)]   # 追加在末尾
4. provider.stream_chat(req_messages, thinking_effort, tools, system=stable)
5. 流结束后：collector.usage 非空 → log_cache_usage(usage, model, debug_path)
```

`stable` 逐轮逐字节一致 → 命中前缀缓存（F5/N4）；变化的 `reminder` 只追加在历史末尾。

## Provider 接口改动

`BaseProvider.stream_chat` 新增 `system: Optional[str] = None`：

| Provider | 落地方式 |
|----------|---------|
| DeepSeek | `system` 非空时作为 SDK 消息列表第一条 `{"role":"system",...}`；依赖自动前缀缓存；usage 原样上抛 |
| OpenAI | `system` 非空时前置为一条 system 消息 |
| Anthropic | `system` 映射到 SDK 顶层 `system=` 参数；预留将来加 cache_control 的位置（F6） |

`collector._to_usage` 增采 `prompt_cache_hit_tokens` / `prompt_cache_miss_tokens`。

## 文件组织

```
rhinecode/agent/prompt/              （prompt.py 升级为包）
├── __init__.py        — 再导出 build_default_prompt / build_system_reminder 等入口
├── modules.py         — PromptModule + 7 固定模块 + 3 可选空槽
├── builder.py         — SystemPromptBuilder + AssembledPrompt + build_default_prompt
├── environment.py     — EnvironmentInfo + collect_environment
└── reminders.py       — <system-reminder> 构造 + plan 完整/精简文本 + 按轮节奏
rhinecode/agent/cache_log.py         — 新建：log_cache_usage（F10）
rhinecode/agent/events.py            — 改：Usage 增 2 个缓存字段
rhinecode/agent/collector.py         — 改：_to_usage 采集缓存字段
rhinecode/agent/loop.py              — 改：run 签名 + 每轮 reminder + system 参数 + 日志
rhinecode/conversation.py            — 改：构造 stable/dynamic/env，传入 loop
rhinecode/provider/base.py           — 改：stream_chat 增 system 参数
rhinecode/provider/deepseek.py       — 改：system 前置
rhinecode/provider/openai.py         — 改：system 前置
rhinecode/provider/anthropic.py      — 改：system 顶层参数
rhinecode/config.py                  — 改：新增 debug_log 配置
rhinecode/tools/edit_file.py         — 改：描述补「编辑前必先读」（F7 双重强化）
```

## 技术决策

| 决策点 | 选择 | 理由 |
|--------|------|------|
| 系统提示传递 | 新增 `system` 参数，而非塞进 messages | 稳定可缓存提示与动态 reminder 分通道；为 Anthropic cache_control 预留落脚点（F6/N1） |
| reminder 位置 | 追加在历史末尾 | 变化内容只成不缓存的尾巴，不破坏前缀缓存（N4）；末尾最显著 |
| 环境信息通道 | 走 reminder（消息通道） | 遵循 F5；date 等会变，放缓存前缀会反复失效 |
| 缓存日志开关 | config 新增 `debug_log: bool`（默认 true），路径固定 `<项目根>/.rhinecode_debug.log` | 默认开启便于验证；每请求仅一行、IO 异常 try/except 降级（N6）；可关闭。建议加进 .gitignore |
| plan 文本归属 | 迁入 reminders.py，删除旧 prompt.py 模块 | 集中管理动态注入；完整/精简两版同处 |
