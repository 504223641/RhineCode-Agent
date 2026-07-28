# 需求交底：Trace 记录器与 TUI 驱动器

> 状态：需求交底（2026-07-27，已经独立审查修订）·**这不是 spec**
> 目标读者：接手开发的新 Claude Code 会话
>
> **定位：这是跨阶段的「测试设施」，不是新章节的产品能力。**
> C2–C11 每章都是给用户的产品能力（工具系统、权限、MCP、上下文、记忆、命令、Skill），
> 而本模块是给开发者用的——它服务**已完成的 C2–C11 与未来所有阶段**的验收与排查。
> 因此它**不占新章节号**，作为 C11 的子开发放在 `docs/c11/testing/p0-trace/`，
> 在 `c11-trace` 分支上开发、完成后合回 `c11`。
> 请不要在 spec 里把它写成「C12」或 Skill 系统的一部分。
>
> 前置：请先读 `CLAUDE.md`（尤其「架构」「成对维护点」「安全边界」三节），
> 再用 `/spec` 技能走 `spec.md → plan.md → task.md → checklist.md`（都放
> `docs/c11/testing/p0-trace/` 下）。本文只交代**要什么、为什么、地基是什么、坑在哪**，
> 不代替 spec 的需求澄清。
>
> 文中所有对代码现状的断言都在 2026-07-27 的代码库上实测核对过，并标注了
> 文件与行号。**行号会随开发漂移，动手前请自己再验一遍。**

---

## 0. 一句话

给 RhineCode 加两样东西：一个把「一次会话里真正发生了什么」完整记录成结构化日志的
**Trace 记录器（P0）**，和一个能程序化驱动 TUI（打字、回车、应答确认面板）并据 trace
判定结果的**驱动器（P1）**，使端到端验收从「人肉敲键盘 + 肉眼看屏幕」变成
「跑一条命令 + 读日志」。

**建议分两期交付**，理由见 §9。

---

## 1. 背景：为什么需要这个

### 1.1 现状是人肉验收

`docs/c11/checklist.md:148-165` 列了 11 个端到端场景，**全部标注【手测】**。实际执行方式
是人在真实终端敲命令、肉眼看输出、把屏幕内容复制给协作方判断。三个问题：

1. **不可重复**——同一场景跑两次观察到的细节不同，回归无从谈起；
2. **证据链断裂**——协作方只看到人复制过来的片段，看不到系统内部状态；
3. **判断滞后**——出了问题才回头找证据，而证据当时往往没留下。

### 1.2 会话存档不是完整上下文（关键事实）

一个很自然的想法是「读会话存档就行了」。**不行**。
`<项目根>/.rhinecode/sessions/*.jsonl` 每行**至多**六个字段
（`rhinecode/memory/session.py:367-380` 的 `_serialize`；`ts`/`role`/`content` 恒有，
其余三个有值才写）：

```
ts / role / content / tool_calls / tool_call_id / display_content
```

它记的是**模型历史**，不是「这次会话发生了什么」。

| 存档里**有** | 存档里**没有** | 为什么缺失致命 |
| --- | --- | --- |
| 用户消息、助手正文、工具调用与结果 | **绕过 Agent 的命令及其输出**（见下方术语澄清） | 存档里**连你敲过 `/skills` 都看不出来**，更别说它回了什么 |
| 提示词命令的双内容（`/init`、Skill 短命令：界面存 `display_content`、模型存展开文本） | **系统提示全文**（七个稳定模块 + RHINE.md + 记忆索引 + Skill 清单 + 动态 reminder + 已激活 SOP 正文） | 「SOP 到底注入没注入」「第 N 轮激活第 N+1 轮生效」这类 C11 核心验收项无从查证 |
| — | **每轮实际发出的 tools schema** | 「白名单是否真的收窄了」的唯一判据 |
| — | **权限决策**（命中哪层、放行/拒绝/问、用户选了什么） | 五层防御的验收全靠它 |
| — | **NOTICE 事件**（上下文压缩通知、Skill 降级警告） | 「用户可见反馈是否给到」是明确的验收项 |
| — | **c8 压缩的具体动作**（哪几条工具结果被存盘、摘要保留边界在哪） | 事后无法解释「这轮 messages 为什么突然短了」 |
| — | **状态栏与屏幕内容**（`Skill:1`、上下文用量、工具行） | 界面态验收项 |
| — | **usage / 思考内容 / 每轮耗时** | 只有 `.rhinecode_debug.log` 一行缓存命中数（`agent/cache_log.py:17-42`） |

> **术语澄清（建模 `command_dispatch` 事件时必看）**：命令有三类
> （`commands/models.py::CommandType`），不要笼统叫「本地命令」——
> - `LOCAL`：`/help`、`/mcp`、`/context`、`/memory`、`/skills`、`/compact`
> - `UI`：`/think`、`/plan`、`/perm`、`/resume`、`/clear`、`/exit`（`builtins.py:257` 起）
> - `PROMPT`：`/init` 与全部 Skill 短命令
>
> 且**「LOCAL 就不进模型历史」这个简化说法有两个例外**：
> - `/skills run <name>`（`builtins.py:314` 归 LOCAL）会走 `conversation.py:614-630`，
>   共享模式提交一条 user 消息、独立模式新增两条配对消息，**会进模型历史**；
> - `/compact`（LOCAL）会调 LLM 并**重写**模型历史（CLAUDE.md 里明确写作「例外」）。
>
> 核心论点（存档看不出敲过哪些命令）依然成立，但分类别搞错。

### 1.3 实证：缺 trace 的代价

C11 手测场景 1 跑出两个真 bug，定位过程如下：

- **`run_command` 中文输出被静默吞掉**：`subprocess.run(text=True)` 按 cp936 解码 git 的
  UTF-8 输出，`UnicodeDecodeError` 死在 subprocess 的读取线程里被吞，`proc.stdout` 变空串
  而**退出码仍是 0**。存档里只看到「命令成功、无输出」，靠人工比对「哪些命令有输出」
  才发现规律是「含中文的全空」。（已修复：`tools/run_command.py:27,122` 的 `_decode`）
- **模型调用了本轮没发给它的工具**：白名单只发 4 个工具，模型却成功调用了 `edit_file`。
  要证明「schema 里确实没有它」，**只能另写探针脚本**模拟完整启动接线、用假 Provider
  记录 `tools` 参数——因为运行时没有任何地方留下这个信息。

两次都是「现写脚本捞证据」。Trace 就是把这件事一次性做掉。

---

## 2. 地基：现在已经有什么（已核实）

### 2.1 Textual 版本与可用 API

- `textual 8.2.7`
- `App.run_test()` 返回 `Pilot`，可用：`press / click / double_click / triple_click /
  hover / mouse_down / mouse_up / pause / resize_terminal / wait_for_animation /
  wait_for_scheduled_animations / exit`，以及 `pilot.app`
- ⚠️ **`App.export_text()` 不存在**（实测 `hasattr(App,'export_text') is False`）。
  App 上只有 `export_screenshot`（SVG）/ `save_screenshot` / `deliver_screenshot` /
  `deliver_text` / `deliver_binary` / `action_screenshot`。想拿「屏幕上的文字」
  不能指望它——见 §5.5。

### 2.2 已经有 Pilot 驱动的测试

`tests/test_command_tui.py` 已在用 `async with app.run_test() as pilot` 驱动真实按键
（Tab 补全、菜单回车、Esc 关菜单），配 Fake Manager，不碰真 Provider。
**「模拟往输入框打字」本身已经能做**，缺的是 trace、真实 Provider 与自动应答。

### 2.3 TUI 的并发模型与关键状态标志

`rhinecode/tui/app.py`：

- Agent 循环经 `run_worker(thread=True)` 在**独立线程**消费 AgentEvent 生成器，
  每个事件用 `call_from_thread()` 调度回主线程渲染；
- 需要用户决定的交互由循环在 **Worker 线程**调回调，回调内 `call_from_thread`
  在主线程弹面板，再用 `threading.Event` 阻塞 Worker 等结果；
- 三个标志是驱动器的等待判据与应答钩子：
  - `_stream_active: bool`（`app.py:151`）——有流式 Worker 在跑。
    **确认面板挂着时它仍为 True**（`_set_streaming(False)` 只在 `_do_stream` 的
    finally，`app.py:721`）
  - `_pending_interaction: dict | None`（`app.py:147`）——形状**恰好**是
    `{"event": threading.Event, "result": Any, "kind": str}`（`app.py:785`）
  - `_session_panel_active: bool`——会话选择面板展示中
- 确认面板与计划审批面板**不禁用输入框**（只有澄清面板 `app.py:840` 与会话面板
  `app.py:597` 禁用），提交守卫改为给可见提示（`app.py:527-542`）

### 2.4 Provider 抽象（trace 的主入口，但**不是唯一入口**）

`rhinecode/provider/base.py:105-112`：

```python
stream_chat(messages, thinking_effort="off", tools=None, system=None) -> Iterator[StreamChunk]
```

全局无任何对具体 Provider 的 `isinstance` 检查（`_tools_enabled` 只看
`config.protocol`，`conversation.py:210`），所以**用装饰器包一层是安全的**。

⚠️ **但有一条旁路**：独立模式 Skill 声明 `model:` 时，`conversation.py:693` 的
`_provider_for` 会 `create_provider(dataclasses.replace(config, model=model))`
**新造一个 Provider**，在 `__main__` 里包的装饰器管不到它，trace 会出现空洞。
修法二选一：给 `_provider_for` 一个可注入的 provider 工厂，或把包装做进
`create_provider` 工厂侧。（好消息：`ContextManager`（`conversation.py:221`）与
`MemoryManager`（`conversation.py:231`）拿的都是同一个实例，装饰器覆盖得到。）

### 2.5 历史区的内容承载方式

`rhinecode/tui/widgets.py:338-366`：`HistoryView` 内层 `Vertical(id="history-messages")`，
`_add_widget` 挂 `Static(markup, markup=True)`。

⚠️ **但只有部分消息是字符串**：`append_user` / `append_system` / `append_error` 走
`_add_widget`（markup 字符串，可读）；而 `update_ai_widget`（`widgets.py:407-425`）
渲染的是 `RichGroup(RichText, RichMarkdown(content))`——renderable **不是字符串**，
取不出原文；`add_tool_widget`（`widgets.py:427-441`）挂的是 `ToolCallWidget` 而非 Static。

### 2.6 埋点位置（全部已核实存在）

| 想记的东西 | 埋点 |
| --- | --- |
| 每轮请求与响应 | `TracingProvider` 装饰 `BaseProvider`（注意 §2.4 的旁路） |
| 权限决策 | `PermissionEngine.decide`（`permission/engine.py:71`） |
| 工具执行 | `loop.py::_run_one_serial`（699）/ `_run_readonly_concurrent`（661）/ `_execute`（461） |
| **AgentEvent 全流** | **`app.py:617 _do_stream`** —— 所有事件流都经 `_consume_manager_result` → `_start_stream_worker` → `_do_stream`。⚠️ **不要用 `conversation.py::_wrap_events`**：它只有两个调用点（`conversation.py:624` 独立模式、`997` `_run()`），`manual_compact()` 与 `_resume_stream()`（含携带历史快照的 `HISTORY` 事件）都不经过它 |
| 命令分发（含 LOCAL/UI） | `commands/dispatcher.py:43 CommandDispatcher.dispatch` |
| 命令的输出 | `CommandController.show_message`（`tui/app.py` 实现） |
| 状态栏 | `widgets.py:727 compose_status_text` 或 `app.py:235 _refresh_status` |
| c8 压缩动作 | `context/manager.py::before_request` / `manual_compact` |

---

## 3. 目标与非目标

### 目标

1. 跑完一次会话后，**一个文件**里能回答：模型每轮看到了什么系统提示与工具集、
   说了什么、调了什么工具、每次调用过了哪层权限、结果是什么、界面显示了什么、
   状态栏长什么样、用户（或驱动器）敲了什么命令、命令回了什么、上下文被压缩过什么。
2. 能用一条命令跑完一个端到端场景，产出 trace 与断言结果，**不需要人坐在终端前**。
3. 确定性模式可进 CI；真实模式用于验证「模型是否真按 SOP 行事」。

### 非目标（明确不做，别扩散）

1. **不替代人眼**——「界面卡不卡、工具行是否逐个出现、面板长什么样、观感如何」
   仍要人看。驱动器解决「行为是否正确」，不解决「体验是否顺畅」。
2. 不做录制回放（VCR）真实 API 响应的机制（可列为后续项）。
3. 不做性能基准、不做多实例并发测试。
4. 不改变任何现有功能行为——trace 关闭时系统必须与现在**逐字节**一致。

---

## 4. 组件一：Trace 记录器（P0）

### 4.1 要记什么

建议每条一行 JSON，含 `seq`（自增）、`ts`、`type`、**`scope`**，与各自负载。

> **`scope` 字段不可省**：独立模式 Skill 会开子对话
> （`conversation.py:697 _run_isolated_skill` 里 `sub_agent = Agent(sub_provider, ...)`），
> 子对话与主对话的请求会**交错**出现在同一条 trace 里。C11 场景 2 的核心断言
> 「主历史恰好新增两条配对消息」「子对话的几十次读文件没撑大主上下文」，
> 没有 `scope: main | isolated:<skill名>` 就没法表达。

| type | 负载要点 |
| --- | --- |
| `session_start` | 项目根、config（**api_key 必须脱敏**）、Provider/model、权限模式、Plan Mode、已注册工具名清单、MCP 连接状态、Skill catalog 快照。⚠️ 时机见 §7 |
| `user_input` | 敲进输入框的原始文本 |
| `command_dispatch` | 命令名/别名/参数、命中的 `CommandSpec`、`CommandType`（LOCAL/UI/PROMPT）、是否未知命令 |
| `ui_message` | 界面新增的一条消息（用户回显/AI 正文/系统提示/错误），带来源与原始文本 |
| `api_request` | 第几轮、system 全文、tools 的**名字列表 + 完整 schema**、messages（长内容可截断但要记原长）、thinking_effort |
| `api_response` | 拼接后的正文、思考内容、工具调用列表、usage、耗时、是否流错误 |
| `permission_decision` | 工具名、规范化后的权限请求、命中层、决策（ALLOW/DENY/ASK）、理由 |
| `interaction` | 面板类型（确认/澄清/计划审批/会话选择）、展示内容、应答方式（人工/自动策略）、结果 |
| `tool_execute` | 工具名、参数、ok、summary、输出（可截断，**但要记原始长度**）、耗时、是否走只读并发桶 |
| `agent_event` | 每个 AgentEvent 的类型与关键字段（尤其 `NOTICE` 与 `FINISHED` 的 `stop_reason`） |
| `context_compaction` | 第一层：被存盘的 `tool_call_id` 列表与落盘路径；第二层：摘要保留边界索引、重构前后消息条数 |
| `status_bar` | 状态栏文本快照（含 `Skill:N`、上下文用量、模式标记） |
| `skill_state` | 激活列表变化、降级（TRUNCATED/DROPPED）、白名单计算结果 |
| `session_end` | 结束原因、总轮数、总耗时 |

### 4.2 硬约束

1. **零侵入**：不开 trace 时系统行为逐字节不变，且热路径无实质开销。
   ⚠️ **注意与 §4.4 的 Null Object 的关系**：Null Object 只免去「调用点判空」，
   免不了「构造负载」——`api_request` 的负载是 system 全文 + 完整 tools schema +
   全部 messages，每轮序列化一遍再丢进空方法绝不是零开销。
   **所以规则是：调用点用 Null Object 免判空，但凡负载昂贵的埋点必须先过
   `if recorder.enabled:` 守卫。** 两者不是二选一，是分工。
2. **fail-safe**：写盘失败、序列化失败一律吞掉，绝不阻断对话
   （对齐 `SessionStore.append` 与 c8 存盘的既有做法）。
3. **不改变时序**：埋点不得引入与既有锁交叉的锁、不得在持锁期间做 IO、
   不得跨线程调度。见 §6.1 的死锁坑。
4. **脱敏**：`api_key` 必须脱敏。trace 含被读过的文件内容与命令输出，
   与会话存档同属敏感产物，**必须进 `.gitignore`**。
5. **线程安全**：埋点会在主线程、Worker 线程、只读并发桶的线程池、笔记 daemon
   线程里被调用。写入需串行化，但只能用**一把只保护「追加一行」的独立锁**，
   临界区内只做 write。

### 4.3 开关与落盘

- 命令行 `--trace [路径]`，缺省 `<项目根>/.rhinecode/traces/<时间戳>.jsonl`；默认**关闭**。
- ⚠️ **必须同时提供编程式开关**：驱动器用 `app.run_test()` 在同一进程起 App
  （§2.2 的既有做法），**根本不走 argparse**。开关只挂 CLI 会在开发中途才暴露缺口。
  建议形态：`build_app(..., recorder=TraceRecorder|None)`（见 §6.6）。
- 是否需要 `config.yaml` 开关由 spec 决定（倾向不要——测试用途，命令行 + 编程式足够）。

### 4.4 建议实现形态

参照项目既定模式（`permission/` `context/` `memory/` `skills/` 都是「纯逻辑包 + 单点接入」）：

```
rhinecode/trace/
├── models.py      事件类型枚举与冻结数据类（只依赖标准库）
├── recorder.py    TraceRecorder：追加写 JSONL、脱敏、fail-safe、enabled 属性
│                  + NullRecorder（Null Object，所有方法空实现）
└── hooks.py       TracingProvider 等薄包装器
```

⚠️ **分层例外要写进 spec**：`hooks.py` 里的 `TracingProvider` 必须
`from rhinecode.provider.base import BaseProvider`，所以 `trace` **不是**严格意义上
「只依赖标准库」的叶子包。这是可接受的——`provider/base.py` 是零副作用的纯抽象模块
（只有 ABC 与数据类）。但要么在 spec 里明确写下这条例外，要么把 `TracingProvider`
挪出 `trace/`（例如放 `provider/tracing.py`）。**别让新会话在这里反复推翻分层。**

---

## 5. 组件二：TUI 驱动器（P1）

### 5.1 两种模式

| | A 确定性模式 | B 真实模式 |
| --- | --- | --- |
| Provider | 脚本化假 Provider | 真实 DeepSeek |
| 可复现 | 100% | 否 |
| Token 成本 | 0 | 有 |
| 进 CI | 能 | 不能 |
| 能验证 | 机制：白名单收窄、SOP 注入时机、配对消息、状态栏、拒绝路径、取消路径 | 机制 **+** 模型是否真按 SOP 行事 |
| 不能验证 | 模型行为质量（回答是我编的） | 无法断言确定值，只能断言模式/存在性 |

**两种都要**：A 是回归护栏，B 是验收工具。

### 5.2 场景脚本

建议声明式（YAML 或 Python DSL，spec 阶段定），至少能表达：

```yaml
name: 场景1-共享模式全流程
mode: live                       # or scripted
workspace: ${E2E_WORKSPACE}/c11-test    # 见 §5.3，不要硬编码盘符
steps:
  - type: "/skills"
  - expect_ui_contains: ["commit（内置", "发现 2 个项目级 Skill"]
  - type: "/commit 修复折扣计算的入参校验"
  - auto_answer: allow_readonly
  - wait_idle: 180s
  - expect_api_tools: ["run_command", "read_file", "grep_content", "load_skill"]
  - expect_no_tool_executed: ["edit_file", "write_file"]
  - expect_status_bar_contains: "Skill:1"
  - type: "/skills off commit"
  - expect_status_bar_not_contains: "Skill:"
```

（`expect_api_tools` 的 4 个值是核实过的：`commit.md` 白名单 3 个 + exempt 的
`load_skill`，见 `skills/manager.py:443-487`、`tools/policy.py:38-40`。）

**断言一律基于 trace，不基于截屏。**

### 5.3 自动应答策略（安全关键）

驱动器替人点确认面板，等于**拿掉五层防御的第⑤层（人在回路）**，必须约束：

1. 只允许在**隔离沙箱**跑。沙箱根由环境变量或场景文件声明（**不要硬编码盘符**——
   那会让这套设施只能在一台机器上跑，与 §8 的环境无关性冲突）；驱动器启动前
   校验 `workspace` 在沙箱根之下，否则拒绝启动；
2. 默认策略 `allow_readonly`：只自动放行只读工具与只读 git 子命令
   （`status`/`diff`/`log`/`show`），其余**自动拒绝并记入 trace**；
3. 可按场景放宽到 `allow_all`，但必须在场景文件里显式写出（不能是默认值）；
4. 第①层危险命令黑名单不可绕过，不受本策略影响——**这点要有测试护栏**。

### 5.4 「本轮结束」的等待判据（最容易翻车）

`pilot.pause()` **不等 Worker 线程**。基本判据：

```
idle == (not app._stream_active)
        and app._pending_interaction is None
        and not app._session_panel_active
```

四个必须注意的真实情形：

1. **确认面板挂着时 `_stream_active` 仍为 True**（`app.py:721`）——所以等待循环
   必须**既检查空闲、也检查是否有面板要应答**，否则死等到超时；
2. ⚠️ **`_pending_interaction` 在面板挂载之前就被置位**（`app.py:785-789`）：
   ```python
   box = {...}
   self._pending_interaction = box      # ← Worker 线程，先置位
   self._busy_hint_shown = False
   self.call_from_thread(show_fn)       # ← 才去主线程挂面板
   ```
   驱动器若一看到它非 None 就 `pilot.press(...)`，可能在面板尚未挂载/未聚焦时按键，
   **按键被丢掉、然后死等超时**。等待判据里要额外确认面板已 display 且已聚焦，
   或者干脆不模拟按键、直接在主线程调 App 的解析交互方法；
3. 独立模式 Skill 会开子对话，期间主循环仍在跑；
4. 笔记钩子是自然停止后的**异步 daemon 线程**，不影响 `_stream_active`，
   但之后仍会写盘/调 LLM——断言「笔记已触发」需要单独信号。

### 5.5 读屏

`App.export_text()` 不存在（§2.1）。优先级：

1. **首选**：从 trace 的 `ui_message` / `status_bar` 事件断言——这本就是记录器的职责；
2. 备选**仅对部分消息可行**：遍历 `#history-messages` 下的 `Static` 读 renderable
   只覆盖 `append_user` / `append_system` / `append_error`；AI 正文是 Rich 渲染对象、
   工具行是 `ToolCallWidget`，取不出原文（见 §2.5）。**别把它当通用手段**；
3. 仅在需要视觉留档时用 `export_screenshot()` 存 SVG（不用于断言）。

---

## 6. 已知的坑（务必读完再动手）

1. **持锁跨线程回调 = 确定性死锁**。`SkillManager` 是硬教训：临界区内做回调 →
   Textual 阻塞式 `call_from_thread` → 主线程要同一把锁 → 整个 TUI 冻结。
   Trace 埋点会出现在**所有线程**，绝不能在任何既有锁的临界区内做 IO 或跨线程调度。
   `tests/test_skill_manager.py:192-226` 有一条「另起线程读状态并 `join(timeout)`」的
   死锁护栏，本次照做——**同线程版本在 `RLock` 下会静默通过，不可简化**。
2. **只读并发桶里抛异常 = 被当成「工具执行异常」回灌模型**。埋点必须 `try/except` 包住。
3. **Textual markup**：状态栏/历史区文本里的字面 `[` 会被当标签吞掉，必须转义 `\[`。
   trace 记录时注意区分「原始文本」与「渲染文本」。
4. **Windows 编码**：`subprocess` 不显式指定 `encoding` 会按 cp936 解码，UTF-8 输出
   直接变空串且退出码 0（已修，`tools/run_command.py:27,122`）。trace 写盘一律
   `encoding="utf-8"`；`.ps1` 脚本要带 UTF-8 BOM，否则 Windows PowerShell 5.1 按 ANSI
   解码会把中文字符串的收尾引号吞进双字节字符，导致后续代码行变成字符串内容
   （这条是手测中实际踩到的，当前仓库内无 `.ps1` 可比对，但 `G:\Rhine-test` 下有）。
5. **进程内连跑多场景的全局状态残留**（不只是会话锁）：
   - `tools/path_guard.py:24-26`：`workspace_root()` 就是 `Path.cwd().resolve()`，
     **每次实时取值**。换工作区必须 `os.chdir`，而 chdir 是进程级的，跑到一半改
     会连带改掉沙箱根；
   - `tools/path_guard.py:58-78`：`_EXTRA_READ_ROOTS` 是**模块级列表**，
     只在 `register_read_root` 追加，清理只有一个「仅测试用」的 `clear_read_roots()`。
     连跑多场景会累积上一个场景的只读白名单；
   - 会话锁 `.rhinecode/sessions/*.lock` 有 600 秒过期自愈，但连跑时要么显式释放、
     要么换工作区；
   - `.rhinecode_debug.log` 写在 `workspace_root()` 下（`conversation.py:974-975`）。
6. **`main()` 是一坨不可复用的启动接线**（**最可能导致返工，必须先解决**）：
   `__main__.py:39-217` 从 argparse 一路写到 `app.run()`，中间没有任何可复用的工厂。
   驱动器要起真实 App 就得逐字复刻这 90 行——`build_builtin_registry` →
   `create_provider` → `ToolRegistry.default()` → `MCPManager` + `MCPAddServerTool` →
   `SkillManager` + `LoadSkillTool` → `known_tools` → `startup` → `connect_all` →
   `bind_tools` → `replace_skill_commands` → `ConversationManager` → `RhineApp`，
   **外加 `finally` 里的 `memory_manager.close()` 与 `mcp_manager.close_all()`**
   （`__main__.py:210-217`，不复刻就会漏放会话锁，正是上一条抱怨的症状）。
   **必须把 `main()` 抽成 `build_app(...) -> (app, cleanup)` 工厂**并写进交付物，
   否则顺序约束一改，复刻版就静默错位。
7. **确认面板不禁用输入框**是 C11 特意保留的行为（提交守卫给可见提示）。
   驱动器模拟「面板挂着时敲回车」时要预期到提示文本，而不是预期无反应。
8. **prompt 缓存与 token 成本**：真实模式每个场景都是真金白银；Skill 清单进的是
   稳定通道（会被前缀缓存命中），反复重启会让命中率变化，别当异常。
9. **`--trace` 与 `--continue` / `/resume` 的交互**：恢复会话会重放历史、重置锚点，
   trace 要能表达「这是恢复来的历史，不是本次新产生的」。

---

## 7. 分层与依赖方向要求

- 新增 `trace/` 尽量做叶子包，**唯一允许的例外**是 `hooks.py` 依赖
  `provider/base.py`（零副作用纯抽象模块）——见 §4.4，方向要在 spec 里定死。
- `rhinecode/tools/__init__.py` **不得 re-export 任何子模块**：`tools ↔ skills` 与
  `tools ↔ mcp` 都是包级互相依赖，不成环唯一靠这个文件是空的。别破坏它。
- 驱动器属于测试设施，建议放 `tests/e2e/` 或独立 `scripts/`，**不进 `rhinecode/` 包**
  （它不是产品功能；若要随包分发需在 spec 阶段明确）。
- 新增命令行参数 → `__main__.py` 的 argparse + 接线。⚠️ **两个时机要分开**：
  - `TraceRecorder` 的**构造**可以放最前面（它只需要一个路径，谁都不依赖）；
  - 但 `session_start` **事件的负载**（Provider/model、已注册工具名清单、MCP 连接
    状态、Skill catalog 快照）必须在 `connect_all` + `bind_tools` **之后**才拿得全
    （`__main__.py:181-188`）。混为一谈会记出一份半空的快照。
- C11 留下的顺序约束不可动：`LoadSkillTool` 必须在算 `known_tools` **之前**注册
  （`__main__.py:171-176`）、Skill 校验必须夹在 `MCPAddServerTool` 注册之后、
  `connect_all` 之前（`__main__.py:143-181`，源码注释写着「两头都不能挪」的理由）。

---

## 8. 验收标准建议（spec 阶段细化）

**P0（trace）必须有的硬护栏：**

1. **零回归**：不开 trace 时，系统提示的稳定段与动态段、发出的工具 schema、
   会话存档内容与改造前**逐字节一致**（对齐 C11 AC29 的写法，
   见 `docs/c11/checklist.md:115-117`、`tests/test_skill_loop_policy.py:116`）。
2. **链路上不存在包装器**：不开 trace 时断言 `manager` 持有的就是原始 provider 实例
   （未被 `TracingProvider` 包过）——把「零侵入」变成可测的。
3. **不阻断**：trace 写盘失败（目录只读/磁盘满/序列化异常）时对话正常继续。
4. **不死锁**：跨线程读状态的护栏测试（§6.1）。
5. **脱敏**：trace 中不出现 `api_key` 明文。
6. **覆盖旁路**：独立模式 Skill 声明 `model:` 时，子对话的请求**也在 trace 里**
   （§2.4 的旁路，最容易漏）。

**P1（驱动器）：**

7. 能跑通 C11 场景中**用 `run_test` 可驱动的那些**。⚠️ 逐条看
   `docs/c11/checklist.md:148-165` 会发现**场景 6（启动 fail-fast，要求进程以退出码 1
   终止）与场景 7（在「别人的仓库」里启动）不能用 `run_test` 驱动**，必须另起子进程跑
   `python -m rhinecode`。所以覆盖目标建议写成「场景 1–5 + 8–11，共 9 条；
   6、7 若要覆盖需子进程驱动，列为可选」，不要笼统写「11 个里的 8 个」。
8. **自动应答不越界**：危险命令黑名单仍拦截；非白名单工具在 `allow_readonly` 下
   被自动拒绝且记入 trace。
9. **确定性模式可进 CI 且与开发机环境无关**。
   ⚠️ 这条不能写成「不读用户主目录」——按当前代码**做不到**：
   `__main__.py:158-163` 的 `SkillManager(..., Path.home()/".rhinecode", ...)` 与
   `conversation.py:230` 的 `user_dir = Path.home()/".rhinecode"` 会把用户级
   RHINE.md、笔记索引、skills、permissions.yaml 全带进系统提示与权限求值——
   同一场景在两台机器上 system 全文会不同。
   **正确写法是「驱动器可注入 user_dir，或用 HOME 重定向做隔离」**，
   并把它列进 P1 的必做项。

---

## 9. 交付物（建议分两期）

**P0 —— Trace 记录器**（自洽、可独立验收、立刻有价值：终于能看到每轮实际的
tools schema 与 system 全文，而不是靠 `/skills prompt` 事后猜）

1. `rhinecode/trace/`（models / recorder + NullRecorder / hooks）
2. 各层埋点（provider 包装 + `_provider_for` 旁路、权限、工具、`_do_stream` 事件流、
   命令分发、状态栏、c8 压缩）
3. `__main__.py` 的 `--trace` 接线 + **`build_app(...) -> (app, cleanup)` 工厂重构**（§6.6）
4. 单元测试：trace 纯逻辑、脱敏、fail-safe、死锁护栏、零回归逐字节对比、
   「未开启时 provider 未被包装」
5. `.gitignore` 加 `.rhinecode/traces/`

**P1 —— 驱动器**

6. 驱动器（场景脚本格式、自动应答策略、等待判据、断言器、user_dir/HOME 隔离）
7. 覆盖 C11 场景 1–5 + 8–11 的场景脚本（6、7 需子进程驱动，可选）
8. 确定性模式的 CI 接入

**两期共同**

9. `docs/c11/testing/p0-trace/` 的 spec / plan / task / checklist
10. `CLAUDE.md` 的架构节与「成对维护点」备忘更新——注意措辞要把它写成
    **跨阶段测试设施**，别挂到 Skill 系统名下

> 分期理由：驱动器依赖 trace 完成，且要先解决启动接线重构（§6.6）与 HOME 隔离（§8.9）
> 两件事。合成一期的风险是驱动器的时序问题把 trace 上线一起拖住。

---

## 10. 给新会话的启动提示词（可直接复制）

```
读 G:\RhineCode-Agent\docs\c11\trace\brief.md，这是需求交底。
先确认当前在 c11-trace 分支上（git branch --show-current），不在就先切过去。

然后用 /spec 技能走 spec 驱动开发流程：先跟我澄清需求，再依次产出
docs/c11/testing/p0-trace/ 下的 spec.md → plan.md → task.md → checklist.md，每份都要我
确认后才进入下一份。不要直接开始写代码。

几个约束先说在前面：
- 这个模块是**跨阶段的测试设施**，不是新章节、也不属于 Skill 系统。
  它服务 C2–C11 已完成的能力与未来所有阶段的验收，所以不要占新章节号，
  文档一律放 docs/c11/testing/p0-trace/；
- 项目所有回答用中文，代码注释按 CLAUDE.md 的「代码注释规范」写；
- 我是第一次独立做这类项目，涉及新概念要先解释「是什么、为什么用、解决什么问题」；
- 改完代码立刻 git commit，一次改动一个 commit，不用问我；
- 文档给我审批前，先让子 agent 独立审查一遍；
- 只针对 DeepSeek 开发，不为 Anthropic/OpenAI 做兼容设计；
- 交底文档里标的行号会漂移，凡是要依赖的代码事实，动手前自己再核一遍。
```

---

## 附：一个真实的失败案例（供理解 trace 要记什么）

C11 手测场景 1 第一次跑，模型的行为是：读 diff（空）→ 读 git log（空）→ 断定
「这是首次提交，没有既有风格可参考」→ 读文件 → 跑测试 → 看到失败 → **动手改代码** →
用 PowerShell `Set-Content` 把源文件写成乱码 → `git checkout app/calc.py` 把用户
未提交的改动删了 → 一连串 python 单行脚本试图写回 → 55 条消息后被人打断。

从会话存档只能看出「命令退出码 0 但没有 stdout」，看不出**为什么**。真正的原因
（cp936 解码 UTF-8 失败、异常死在 subprocess 读取线程里）要靠人猜规律 + 写复现脚本。

如果当时有 trace，`tool_execute` 事件里记着「stdout 原始字节长度 1024、解码后长度 0」，
一眼就能定位。**这就是这个模块要解决的问题。**
