# Trace 记录器 Plan

> 本文是 `spec.md`（已批准）的技术设计。范围为 P0：Trace 记录器 + 启动装配重构 + CLI 阅读器。
> 所有对现有代码的引用都在 2026-07-27 的 `c11-trace` 分支上核对过。
> **行号会漂移，实现时以符号名为准。**

---

## 架构概览

四个部分，依赖方向严格单向：

```
                    ┌──────────────────────────────────┐
   叶子层            │  rhinecode/trace/                │
                    │   models  ← recorder             │
                    │      ↑                           │
                    │   tracing_provider（N4 唯一例外） │
                    │   reader（只读，不被任何人依赖）   │
                    └──────────────────────────────────┘
                              ↑ 被以下各层单向依赖
   接入层   agent/loop · permission 调用点 · context/manager · memory/manager
            skills/manager · commands/dispatcher · tui/app · conversation
                              ↑
   装配层            rhinecode/bootstrap.py（build_app 工厂）
                              ↑
   入口层            rhinecode/__main__.py（argparse / 模板生成 / 异常转退出码）
```

**四个部分各自的职责：**

1. **`trace/` 叶子包**——事件模型、记录器、Provider 装饰器、阅读器。除 `tracing_provider`
   依赖 `provider/base`（spec N4 明示的唯一例外，因其是零副作用的纯抽象模块）外，
   全部只依赖标准库。
2. **各层埋点**——每层接收一个可选的记录器（缺省为 Null 对象），在既定收口点产出事件。
   **不新增任何跨层依赖**：每层只依赖 `trace`，`trace` 不依赖任何人。
3. **`bootstrap.py` 装配工厂**——把 `__main__.main()` 里那段按严格顺序拼装的逻辑抽出来，
   使测试进程能构造一套完整、与开发者本机环境无关的运行环境。
4. **`__main__.py` 入口**——只保留命令行解析、首次运行模板生成、配置加载、
   以及「把装配异常转成退出码与提示」。这三件事会写用户主目录或终止进程，不进工厂。

---

## 核心数据结构

### 事件类型枚举（`trace/models.py`）

十五类，与 spec F11–F16 一一对应。继承 `str` 便于直接比较与序列化：

```python
class TraceEventType(str, Enum):
    SESSION_START       = "session_start"        # F11
    SESSION_END         = "session_end"          # F11
    USER_INPUT          = "user_input"           # F12
    COMMAND_DISPATCH    = "command_dispatch"     # F12
    API_REQUEST         = "api_request"          # F13
    API_RESPONSE        = "api_response"         # F13
    PERMISSION_DECISION = "permission_decision"  # F14
    INTERACTION         = "interaction"          # F14
    TOOL_EXECUTE        = "tool_execute"         # F14
    UI_MESSAGE          = "ui_message"           # F15
    STATUS_BAR          = "status_bar"           # F15
    AGENT_EVENT         = "agent_event"          # F16
    CONTEXT_COMPACTION  = "context_compaction"   # F16
    SKILL_STATE         = "skill_state"          # F16
    HISTORY_RESTORED    = "history_restored"     # F16
```

### 作用域（`trace/models.py`）

spec F2 只要求区分主对话与独立模式子对话。但实现调查发现**必须扩到四种**：

```python
SCOPE_MAIN    = "main"      # 主对话，也是一切不隶属对话的事件的归属（F2）
SCOPE_SUMMARY = "summary"   # C8 第二层摘要的 LLM 调用
SCOPE_MEMORY  = "memory"    # C9 自动记忆的 LLM 调用
def isolated_scope(skill_name: str) -> str:      # "isolated:<name>"
```

**为什么必须有 `summary` 与 `memory`**：`ContextManager` 与 `MemoryManager` 持有的是
**同一个 Provider 实例**（二者的 `self._provider.stream_chat` 调用），它们发出的请求同样
会流经 Provider 装饰器。若不区分，摘要与笔记的请求会混进主对话的轮次计数，「第 N 轮」
这个字段立刻失真；而笔记跑在**独立的后台线程**上，还可能与用户的下一条消息并发。

### 单条事件的落盘形态

一行一个 JSON 对象，四个公共字段恒在最前，负载平铺在同层：

```json
{"seq": 42, "ts": "2026-07-27T15:04:05.123", "type": "api_request", "scope": "main",
 "turn": 3, "model": "deepseek-chat", "tool_names": ["read_file", "run_command"], ...}
```

### 截断值的表示（spec F6）

**未截断就是裸字符串；一旦截断变成一个三字段对象**：

```python
"output": "短内容原样"                                          # 未截断
"output": {"text": "前 N 字符…", "truncated": true, "original_length": 10240}  # 截断
```

常见情形零额外开销，截断情形自带原长。阅读器两种形态都认。
统一由 `models.clip(value, limit)` 产出，**全项目只有这一个截断入口**。

### 循环事件的字段白名单（spec F17）

`agent_event` 的负载**不是**把 `AgentEvent` 直接序列化——它带着 `tool_call.arguments`、
`tool_result.output`、`text` 等重负载，照直记就违反 spec F17 与 AC20。
由一个纯函数做唯一出口，字段白名单写死：

```python
def agent_event_payload(event) -> dict:
    """AgentEvent → 只含流控信息的负载（spec F17）。

    白名单：event_type / iteration / stop_reason / message /
            tool_call_id / tool_name / result_ok / text_length
    刻意排除：tool_call.arguments、tool_result.output、text 正文本身
             —— 它们各有专属事件（tool_execute / api_response）承载。
    """
```

`text_length` 只记长度不记正文：既能在时间线上看出「这一轮模型说了 1200 字」，
又不与 `api_response` 重复。

### 装配结果（`bootstrap.py`）

```python
@dataclass(frozen=True)
class BuildResult:
    app: RhineApp
    cleanup: Callable[[], None]      # 幂等，见下方「cleanup 的五步与幂等机制」
    manager: ConversationManager     # 以下字段供测试断言，生产代码只用前两个
    tool_registry: ToolRegistry
    command_registry: CommandRegistry
    skill_manager: SkillManager
    mcp_manager: MCPManager
    recorder: TraceRecorderProtocol
```

**为什么返回数据类而不是二元组**：AC3（协调层持有原始 Provider 实例）、AC30（四类用户级
内容隔离）、AC31（白名单不污染）都需要拿到中间组件才能断言。二元组会逼测试去翻私有属性。

### 装配异常（`bootstrap.py`）

```python
class BootstrapError(Exception):
    """装配期致命错误。args[0] 是**已成文的完整 stderr 文案**，调用方原样打印。"""
```

**文案是不可变契约**（对应 AC32 与既有启动测试的 stderr 断言）。三类致命错误的文案
必须与现状**逐字一致**，且拼装责任移入工厂——`__main__` 只负责
`print(err, file=sys.stderr)` + `sys.exit(1)`，**不再自己拼前缀**：

| 致命错误 | 文案（逐字保留） |
| --- | --- |
| 命令注册冲突 | `f"命令注册冲突：{e}"` |
| Provider 初始化失败 | `f"Provider 初始化错误：{e}"` |
| Skill 白名单笔误 | `format_fatal_message(fatal_tool_names)` 的返回值 |

---

## 模块设计

### `trace/models.py`

**职责**：事件类型、作用域常量、阈值常量、以及全部「不碰 IO 的纯函数」。
**对外接口**：

```python
MAX_FIELD_CHARS   = 4000        # 单个文本字段的截断阈值
MAX_MESSAGE_ITEMS = 400         # messages 列表最多记多少条（超出记原条数）
REDACTED = "***REDACTED***"

def clip(value: str, limit: int = MAX_FIELD_CHARS) -> str | dict
def redact_config(cfg) -> dict                  # api_key 换成 REDACTED
def agent_event_payload(event) -> dict          # 循环事件字段白名单（F17）
def isolated_scope(name: str) -> str
def default_trace_path(project_root: Path) -> Path   # <根>/.rhinecode/traces/<时间戳>.jsonl
```

**依赖**：仅标准库。

### `trace/recorder.py`

**职责**：唯一持有可变状态与落盘副作用的地方。
**对外接口**：

```python
class TraceRecorder:
    def __init__(self, path: Path) -> None
    enabled: bool                                        # 恒 True
    def emit(self, type: TraceEventType, **payload) -> None
    def emit_lazy(self, type: TraceEventType, factory: Callable[[], dict]) -> None
    def scope(self, name: str) -> ContextManager[None]    # with 块内切换作用域
    def current_scope(self) -> str
    def bind_scope(self, name: str) -> None               # 线程入口处直接绑定/复位
    def next_turn(self, scope: str) -> int                # 按 scope 各自计数
    def turn_total(self) -> int                           # session_end 的「总轮数」
    def close(self) -> None

class NullRecorder:
    enabled: bool = False                                 # 恒 False
    # 同名方法全部空实现；scope() 返回 nullcontext()；next_turn/turn_total 返回 0
```

**为什么有 `emit_lazy`**：spec N1 要求昂贵负载先过开关守卫，spec F4 又要求
「**负载构造异常**」也被静默吞掉。若在调用点写 `if enabled: emit(**贵负载)`，
负载构造发生在 `emit` 的 `try` 之外，构造抛异常就会外泄。`emit_lazy` 把工厂调用
挪进 `try` 内：关闭时工厂根本不被调用（满足 N1），开启时构造异常被吞（满足 F4）。
**昂贵埋点一律走 `emit_lazy`，廉价埋点走 `emit`。**

**关键实现约束（对应 spec N2/N3/F1/F3/F4）：**

- **作用域用 `threading.local()`**，未设置时取 `SCOPE_MAIN`。读取不取锁。
- **一把独立的 `threading.Lock`。临界区内做四件事，顺序固定**：
  取候选序号 `n = self._seq + 1` → `json.dumps` 组装整行 → 写入并 flush → `self._seq = n`。
  **序列化必须在锁内**——`seq` 是 JSON 的第一个字段，锁外组装意味着锁外读 `self._seq`，
  两个并发线程会各自读到同一个 N 并各写一行 `"seq": N`，直接违反 AC8 的「无重复」。
  临界区的禁令是精确的：**不得做落盘之外的阻塞操作、不得触发任何回调、不得跨线程调度**。
  `json.dumps` 是纯 CPU、微秒级，与 `SkillManager` 那条死锁教训（临界区内跨线程阻塞调度）
  不是同一类风险。
- **序号只在写入并 flush 成功之后才推进**（spec F1）：任何失败路径都不动计数，
  故序列永远连续无空洞，与 F4 的静默丢弃相容。
- **`emit` / `emit_lazy` 整体裹 `try/except Exception: pass`**（spec F4/N3）。这是最后一道闸
  ——只读并发桶里抛出的异常会被 `future.result()` 当成「工具执行异常」回灌模型。
- 文件以 `encoding="utf-8"` 打开、`json.dumps(..., ensure_ascii=False)`（spec N8）。

**为什么 `NullRecorder` 不是 `TraceRecorder` 的子类**：二者没有共享实现，Null 对象只需
满足同一组方法名。用一个 `Protocol` 做类型标注，避免为了继承而让 Null 对象持有一个
永不使用的文件句柄。

### `trace/tracing_provider.py`

**职责**：`BaseProvider` 的装饰器，产出 `api_request` 与 `api_response` 两类事件。
**对外接口**：

```python
class TracingProvider(BaseProvider):
    def __init__(self, inner: BaseProvider, recorder, model: str) -> None
    inner: BaseProvider                      # 暴露内层，供 AC3 断言与调试
    def stream_chat(self, messages, thinking_effort="off", tools=None, system=None):
        ...
```

**行为**：

1. 进入时取当前作用域，向记录器要一个该作用域的轮次号，用 `emit_lazy` 产出 `api_request`
   （system 全文、tools 的名字列表与完整 schema、全部 messages、thinking_effort）。
2. 逐块 `yield` 转发**原样不改**，同时旁路收集正文、思考、工具调用、usage、错误与耗时。
3. **用 `contextlib.closing` 包住内层迭代器，在 `finally` 中产出 `api_response`。**

**为什么必须显式 `closing` 而不是只靠 `try/finally`**：调用方会在收到 error 块时 `break`
跳出 for 循环（`loop.py` 的既有写法），生成器不会被耗尽。CPython 下 for 语句的迭代器
引用出栈后引用计数归零，会立刻触发 `gen.close()` → `GeneratorExit` 打到 yield 点 →
`finally` **同步、同线程、就在 break 那一刻**执行，事件顺序不会错乱。但这是 CPython 的
引用计数实现细节。显式 `closing` 把「何时结算」从解释器实现细节变成代码事实。

**依赖**：`provider/base`（spec N4 唯一例外）+ `trace/models`、`trace/recorder`。

### `trace/reader.py`

**职责**：只读的命令行阅读器，`python -m rhinecode.trace.reader <文件> [选项]`。
**对外接口**（命令行）：

```
<文件>                    默认输出时间线摘要
--type <t>[,<t>...]       按事件类型过滤（F28）
--scope <s>[,<s>...]      按作用域过滤（F28）
--seq <n>                 展开单条完整负载（F29）
```

**行为**：逐行解析，坏行跳过并计数（与会话存档的容错口径一致）；摘要每行一句话关键信息
由一张 `type → 摘要函数` 的表驱动（**成对维护点**：新增事件类型必须同时在这张表里登记，
否则新事件在时间线上显示成空白且不报错）；只读打开文件，绝不写回（F30/AC37）。
**不注册控制台入口**，`pyproject.toml` 一行不动（F26/AC33）。

### `bootstrap.py`

**职责**：可复用的启动装配。
**对外接口**：

```python
def build_app(
    cfg: Config,
    *,
    user_dir: Path | None = None,         # 缺省 Path.home() / ".rhinecode"
    resume_latest: bool = False,
    recorder=None,                        # 缺省 NullRecorder()
) -> BuildResult
```

**装配顺序（照搬现有 `main()`，顺序约束一处不动）**：

```
build_builtin_registry()                          ← 冲突抛 BootstrapError
create_provider(cfg)                              ← 失败抛 BootstrapError
  └─ 仅 recorder.enabled 时包一层 TracingProvider（AC3：关闭时链路上不得有中间层）
ToolRegistry.default()
mcp_config.load_all(user_dir=user_dir)
MCPManager()
tool_registry.register(MCPAddServerTool(...))
─── 以下整段必须夹在 MCPAddServerTool 之后、connect_all 之前 ───
SkillManager(workspace_root(), user_dir, builtin_skills_dir(),
             has_short_command=..., recorder=recorder)   ← recorder 不可漏，见下
tool_registry.register(LoadSkillTool(...))        ← 必须在算 known_tools 之前
known_tools = tool_registry.names() | {"ask_user", "present_plan"}
skill_manager.startup(known_tools)                ← 笔误抛 BootstrapError；产 skill_state
─────────────────────────────────────────────────────────────
mcp_manager.connect_all(...)
skill_manager.bind_tools(...)                     ← 产 skill_state（白名单剪枝结果）
command_registry.replace_skill_commands(...)
ConversationManager(provider, cfg, tool_registry, mcp_manager=...,
                    resume_latest=resume_latest,  ← 不可漏，漏了 --continue 静默失效
                    skill_manager=..., user_dir=user_dir, recorder=recorder)
RhineApp(manager, cfg, command_registry, recorder=recorder)
emit(session_start)                               ← 此刻工具清单/MCP 状态/Skill 清单才齐全
resume_latest 且恢复成功 → emit(history_restored)  ← 启动恢复不经事件流（F16/F20）
```

⚠️ **`SkillManager` 的 `recorder=` 绝不能漏**：`startup` 与 `bind_tools` 正是 spec F20 要求
产出装配期 `skill_state` 的那两步。漏传则它们跑在 `NullRecorder` 上，AC14 拿不到任何事件。

**装配期 `history_restored` 的三项负载来源**（AC13 要求「来源、载入条数、会话标识」）：

| 字段 | 取值 |
| --- | --- |
| `origin` | 固定 `"startup"`（运行中恢复那条取 `"resume_command"`） |
| `message_count` | `len(result.manager.history)` |
| `session_id` | `result.manager.memory_manager.session_id` |

**`cleanup` 的五步与幂等机制**：

```python
def cleanup() -> None:
    if _done: return          # 幂等守卫：session_end 重复产出会破坏「一次运行一份记录」的可读性
    _done = True
    recorder.emit(SESSION_END, ...)   # ← 必须排在 recorder.close() 之前
    try: manager.memory_manager.close()
    except Exception: pass
    mcp_manager.close_all()
    clear_read_roots()                # F24：复位只读路径白名单
    recorder.close()
```

`session_end` 的负载：`turn_total()` 取总轮数（记录器按作用域计数之和）、
总耗时取记录器构造时刻起的单调时钟差、结束原因由调用方传入（正常退出 / 装配后异常）。

**依赖**：现有全部装配组件 + `trace`。**被依赖**：只有 `__main__.py` 与测试。

### `__main__.py`（瘦身后）

保留：argparse、三类模板生成、配置加载与占位符拦截、`build_app` 调用、
`BootstrapError → print(err, file=sys.stderr) + exit(1)`、`try/finally` 里调 `cleanup()`。

**`--trace` 的三态语义**（AC23 要验「带路径」与「不带路径」两种形态）：

```python
parser.add_argument("--trace", nargs="?", const=_TRACE_DEFAULT, default=None,
                    help="开启行为记录；可选携带落盘路径（缺省写 .rhinecode/traces/）")
```

- 未给 `--trace` → `None` → 用 `NullRecorder()`（默认关闭，F7）；
- 给了 `--trace` 但无值 → 哨兵 → `default_trace_path(workspace_root())`；
- 给了 `--trace <路径>` → 该路径。

`nargs="?"` + `const` 是必需的：写成 `nargs="?"` 而不给 `const`，无值时会拿到 `None`，
与「未给」无法区分；写成普通选项则 `--trace` 后紧跟其它参数时会把它吞成路径。

---

## 模块交互

### 埋点总表（十五类事件 → 收口点）

| 事件 | 收口点 | 备注 |
| --- | --- | --- |
| `session_start` | `bootstrap.build_app` 末尾 | 必须在 `connect_all` + `bind_tools` **之后**（F11） |
| `session_end` | `bootstrap` 的 `cleanup` 第一步 | 排在 `recorder.close()` 之前 |
| `user_input` | `CommandDispatcher.dispatch` 在判定非 EMPTY **之后** | EMPTY 输入零副作用（沿用 C10 spec F4），不产事件 |
| `command_dispatch` | `CommandDispatcher.dispatch` 的**四条斜杠出口** | 未知 / 缺参 / handler 异常 / 正常命中。普通消息走 `user_input`，不产本事件 |
| `api_request` | `TracingProvider.stream_chat` 入口 | 见下方「为什么不埋在 loop」 |
| `api_response` | `TracingProvider.stream_chat` 的 `finally` | |
| `permission_decision` | `agent/loop.py` 的 `engine.decide(...)` 调用点 | **只此一处**，见下方说明 |
| `interaction` | `RhineApp._interact`（确认/澄清/审批三类共用）+ 会话面板的展示与结算两处 | F14 要求四类口径一致；`ask_user`/`present_plan` 也由本事件承载 |
| `tool_execute` | `loop._execute` 内**所有**向 `results[tc.id]` 写入的位置 | 七条路径，见下方说明 |
| `ui_message` | `RhineApp.show_user_input` / `show_message` / `_do_stream` 的系统行与错误行 / 每轮 AI 正文收尾 | 记 markup 转义前原文（F15） |
| `status_bar` | `RhineApp._refresh_status` 内**直接调一次** `compose_status_text` 取文本 | 见下方说明 |
| `agent_event` | `RhineApp._do_stream` 的事件循环，经 `agent_event_payload` 白名单 | 唯一漏斗，见下方说明 |
| `context_compaction` | `ContextManager.before_request` / `manual_compact` | 需两层各自的细节，故埋在内部 |
| `skill_state` | `SkillManager` 的激活/卸载/热更新/白名单计算 | **一律在锁外**，见「风险与护栏」 |
| `history_restored` | 启动恢复：`bootstrap` 装配后；运行中恢复：`ConversationManager._resume_stream` | 两条来源（F16/F20） |

### `tool_execute` 的七条路径与 `outcome` 字段

**这里必须逐条列全，漏一条就丢一类证据。** spec「实证」第二条（模型调用了本轮没发给它的
工具）的唯一物证，就是第 1 条路径回灌的那段 `[工具不可用] … 当前可用工具：…` 原文——
它既不在 `permission_decision` 里（没进引擎），叠加 F17 后也不在 `agent_event` 里。

| # | 路径 | `outcome` 取值 |
| --- | --- | --- |
| 1 | `_execute` 的 `out_of_scope` 分支（被工具白名单挡下） | `out_of_scope` |
| 2 | `_run_readonly_concurrent` 的参数解析失败 | `invalid_arguments` |
| 3 | `_run_readonly_concurrent` 的线程池结果（含执行异常兜底） | `executed` |
| 4 | `_run_one_serial` 的未知工具 | `unknown_tool` |
| 5 | `_run_one_serial` 的参数解析失败 | `invalid_arguments` |
| 6 | `_run_one_serial` 的权限 DENY | `denied_by_permission` |
| 7 | `_run_one_serial` 的用户拒绝 / 放行执行 | `denied_by_user` / `executed` |

`_run_special`（`ask_user` / `present_plan`）**显式裁决为不产 `tool_execute`**，由
`interaction` 事件承载——它们是交互而非工具执行，且负载完全重叠。

其余字段：工具名、参数、`ok`、`summary`、`output`（经 `clip`）、耗时、`is_concurrent`。
`outcome != "executed"` 时耗时记 0。

### 四个必须讲清的位置选择

**① `api_request` 埋在 Provider 装饰器，而不是 `loop.run` 内部。**
`loop.run` 里能直接拿到 `iteration`、`stable`、`tools`、`req_messages`，看起来更方便。
但 C8 摘要与 C9 笔记的 LLM 调用**根本不经过 `loop`**——它们直接调
`self._provider.stream_chat`。埋在 loop 会让这两条路径成为盲区，违反 spec F18。
代价是装饰器拿不到 `iteration`，故轮次号改由记录器**按作用域各自计数**。

**② `permission_decision` 只埋 `loop.py` 那一处调用点。**
`engine.decide` 有两个生产调用点。另一处是 `conversation.py` 注入给
`glob_files` / `grep_content` 的**逐文件过滤器**——一次 grep 会触发几百次判定，
埋进去会用无意义的记录淹掉整条 trace，且它判的是「这个文件要不要出现在结果里」，
不是「这次工具调用放不放行」。**明确排除它**。

埋点放在**调用点**而非 `PermissionEngine.decide` 内部，是为了保持 `permission/` 既有的
「纯逻辑、无副作用」性质——`decide` 的文档字符串明写「副作用：无」。

**③ `agent_event` 埋在 `RhineApp._do_stream`，不是 `conversation._wrap_events`。**
`_wrap_events` 只有两个调用点（独立模式、`_run()`），而 `manual_compact()` 与
`_resume_stream()`（含携带历史快照的恢复事件）都不经过它。`_do_stream` 是
`_consume_manager_result → _start_stream_worker → _do_stream` 这条链的终点，
**所有事件流的唯一必经之处**（spec F19）。`_consume_manager_result` 恰好三分支
（字符串走 `show_message`、会话列表走面板、其余走 Worker），迭代器无第二条出路。

**④ `status_bar` 在 `_refresh_status` 里直接调 `compose_status_text` 取文本。**
`_refresh_status` 本身只是把九个字段丢给 `StatusBar.update_status`，真正的文本由
`compose_status_text` 在其内部组装。只记散字段的话 spec F15 要求的「文本快照」不成立。
`compose_status_text` 是纯函数、无副作用，重复调一次零代价。**记 markup 原文**
（含 `\[` 转义与颜色标签），与 `ui_message` 同口径。

### 作用域的设置点与泄漏防护

| 作用域 | 在哪里设置 | 手段 |
| --- | --- | --- |
| `main` | 默认值 | thread-local 未设置时的回退 |
| `isolated:<name>` | `ConversationManager._run_isolated_skill` 包住子 Agent 的整个运行 | `with recorder.scope(...)` |
| `summary` | `ContextManager` 调 `stream_chat` 前后 | `with recorder.scope(...)` |
| `memory` | `MemoryManager` 的记忆 daemon 线程入口 | `bind_scope(SCOPE_MEMORY)` |
| 只读并发桶 | `loop._run_readonly_concurrent` 提交任务前捕获父作用域，worker 入口重新绑定 | `bind_scope` |

**⚠️ 必须有的泄漏防护：在 `_do_stream` 的 `finally` 里无条件 `bind_scope(SCOPE_MAIN)`，
且放在该 `finally` 的第一行。**

理由（这是一条会造成概率性静默错误的真实陷阱）：`_run_isolated_skill` 的
`with recorder.scope(...)` 写在生成器里，正常路径没问题——生成器体由 worker 线程首次
`next()` 时 `__enter__`，子对话的驱动循环也在生成器体内，耗尽后 `__exit__` 复位。
**但异常与放弃路径会泄漏**：`_do_stream` 循环体里全是 `call_from_thread`，应用退出竞态下
会抛 `RuntimeError`（现有代码已有两处为此包了 try/except，说明不是理论风险），
此时生成器被放弃，`__exit__` 可能在别的线程跑、也可能压根不跑。而
**Textual 的 thread worker 用的是默认线程池**（`Worker._run_threaded` 末行
`run_in_executor(None, runner, self._work)`），**线程会被复用**——一次泄漏的
`isolated:<name>` 会污染后续复用该线程的主对话运行，AC11/AC21 概率性失败且极难复现。
`_do_stream` 的 `finally` 跑在 worker 线程上，是唯一能可靠复位的位置。

**已知且可接受的口径不一致**：`with` 跨 yield 期间，作用域会溢出到消费方栈帧——
`_do_stream` 在 worker 线程记的 `agent_event` 会被标成 `isolated`（这正是 AC11 想要的），
而 `show_message` 在**主线程**记的 `ui_message` 仍是 `main`。AC11 只点名了模型请求响应、
权限、工具、循环四类，界面类事件归 `main` 是符合 F2「不隶属任何对话的事件归主对话」的。

### recorder 的向下传递链

这四个对象**都不在装配层构造**，漏传是静默失效（拿到 `NullRecorder`，什么都不报）：

| 对象 | 构造者 | 传递路径 |
| --- | --- | --- |
| `Agent`（主） | `ConversationManager.__init__` | `build_app` → `ConversationManager(recorder=)` → `Agent(recorder=)` |
| `Agent`（子对话） | `ConversationManager._run_isolated_skill` | 同上，用 `self._recorder` |
| `ContextManager` | `ConversationManager.__init__` | 同上 |
| `MemoryManager` | `ConversationManager.__init__` | 同上 |
| `CommandDispatcher` | `RhineApp.__init__` | `build_app` → `RhineApp(recorder=)` → `CommandDispatcher(recorder=)` |
| `SkillManager` | `build_app` 直接构造 | 构造参数 |

### 「一次运行一份记录」不变量（spec F10）

**记录器在 `__main__` 只构造一次，整个进程生命周期内只有这一个实例、只写这一个文件。**
`/clear`（`ConversationManager.clear()`）与 `/resume` 都**不得**复位或替换记录器——
它们换的是对话历史与会话存档，而 trace 记的是「这次进程运行发生了什么」，
切换历史本身就是要被记录的事实。`clear()` 是个「顺手复位一切」的地方
（它已经复位了上下文锚点、熔断状态、Skill 激活态），实现时**不要顺手加上
`recorder.reset()`**。

### 用户级目录参数化的六处改动（spec F23）

**全部实现为「可选参数 + 缺省等于现状」**。这是硬纪律：改成必选会把回归面从 0 推到
五个测试文件、十余个调用点（既有测试都用 `mock.patch(Path.home)` + 无参调用）。

| 位置 | 改法 |
| --- | --- |
| `permission/config.user_config_path()` | 加可选 `user_dir` |
| `permission/config.load_all()` | 加可选 `user_dir` 并透传 |
| `permission/engine.PermissionEngine.load()` | 加可选 `user_dir` 并透传 |
| `mcp/config.user_config_path()` | 加可选 `user_dir` |
| `mcp/config.load_all()` | 加可选 `user_dir` 并透传 |
| `conversation.ConversationManager.__init__` | 函数体内的 `user_dir = Path.home()/...` 提升为构造参数；同处的 `PermissionEngine.load(mode)` 改为传参 |

---

## 文件组织

```
rhinecode/
├── trace/                          【新建】叶子包
│   ├── __init__.py                 只导出 models 与 recorder 的公开名；
│   │                               刻意不导出 tracing_provider，使
│   │                               「trace → provider」这条唯一依赖边保持显式可见
│   ├── models.py                   枚举、常量、作用域、clip / redact_config /
│   │                               agent_event_payload / 默认路径
│   ├── recorder.py                 TraceRecorder + NullRecorder + Protocol
│   ├── tracing_provider.py         TracingProvider（N4 例外）
│   └── reader.py                   CLI 阅读器（python -m rhinecode.trace.reader）
├── bootstrap.py                    【新建】build_app / BuildResult / BootstrapError
├── __main__.py                     【改】瘦身为 argparse + 模板 + 配置 + 异常转退出码
├── conversation.py                 【改】user_dir 与 recorder 两个可选参数；
│                                        _provider_for 按 enabled 包装；
│                                        _run_isolated_skill 设作用域；
│                                        _resume_stream 产 history_restored
├── agent/loop.py                   【改】Agent 接收 recorder；权限决策与七条工具路径埋点；
│                                        并发桶作用域传递
├── permission/config.py            【改】user_config_path / load_all 加可选 user_dir
├── permission/engine.py            【改】load 加可选 user_dir
├── mcp/config.py                   【改】user_config_path / load_all 加可选 user_dir
├── context/manager.py              【改】recorder 可选参数；摘要作用域；压缩事件
├── memory/manager.py               【改】recorder 可选参数；笔记线程绑定作用域
├── skills/manager.py               【改】recorder 可选参数；skill_state（一律锁外）
├── commands/dispatcher.py          【改】recorder 可选参数；user_input / command_dispatch
├── tui/app.py                      【改】recorder 可选参数；ui_message / status_bar /
│                                        agent_event / interaction；_do_stream 的
│                                        finally 首行复位作用域
└── tools/path_guard.py             【改】clear_read_roots 的 docstring 从「仅测试用」
                                         提升为正式清理原语（行为不变）

tests/
├── test_trace_models.py            截断、脱敏、循环事件白名单、作用域、默认路径
├── test_trace_recorder.py          序号连续与无重复、并发不交错、fail-safe 三态、死锁护栏
├── test_trace_provider.py          请求响应成对、break 后仍产响应且顺序正确、
│                                   轮次按作用域计数
├── test_trace_hooks.py             十五类事件产出、七条工具路径、作用域正确、
│                                   去重裁决、markup 原文、作用域泄漏防护
├── test_trace_zero_regression.py   黄金基线、开关双跑、Provider 未被包装、不产文件
├── test_bootstrap.py               装配可调用、cleanup 五步与幂等、三类异常与 stderr 文案、
│                                   用户目录四类隔离、白名单不污染、子进程实跑
└── test_trace_reader.py            摘要、过滤、展开、文件未被修改

docs / 规范
├── CLAUDE.md                       【改】架构节加 Trace 层；成对维护点加两条（见下）
├── AGENTS.md / README.md           【改】与 CLAUDE.md 同步（沿用 C11 的 T66 做法）
└── .gitignore                      【改】加 **/.rhinecode/traces/

【本轮新增的两条「成对维护点」，必须写进 CLAUDE.md】
1. 新增 trace 事件类型 → `trace/models.py` 的枚举 + `trace/reader.py` 的
   「type → 摘要函数」表（漏了就是新事件在阅读器里显示成空白，且不报错）
2. `bootstrap.build_app` 的装配顺序 → `__main__.py` 里那段「两头都不能挪」的
   理由注释必须**随代码一起迁走**（它是 C11 留下的唯一记载，迁走代码却留下注释
   等于把知识丢在原地）
```

---

## 技术决策

| 决策点 | 选择 | 理由 |
| --- | --- | --- |
| 作用域的传递方式 | `threading.local()` + 并发桶显式绑定 + `_do_stream` 的 finally 无条件复位 | 参数透传要改十余个签名且污染每层接口；单个可变字段在笔记 daemon 与主对话并发时会串场。thread-local 天然隔离笔记线程；但 Textual 复用池化线程，故必须有一处可靠的复位点 |
| 作用域取值 | 四种（`main`/`isolated:<名>`/`summary`/`memory`），而非 spec 字面的两种 | 摘要与笔记共用同一个 Provider 实例，不区分就会混进主对话的轮次计数；笔记还跑在独立线程、可能与用户下一条消息并发 |
| 轮次号来源 | 记录器按作用域各自计数 | Provider 装饰器拿不到 `loop` 的 `iteration`；而埋在 loop 会漏掉摘要与笔记两条路径 |
| Provider 埋点形态 | 装饰器包装，不改 `stream_chat` 签名 | 与 C11 的 `_provider_for` 同一思路——`BaseProvider` 接口一行不动。全局无任何 `isinstance` 检查，provider 对象也只被访问 `stream_chat` 一个成员，包一层是安全的 |
| 装饰器的结算时机 | 显式 `contextlib.closing` + `finally` | 调用方会 `break` 提前退出。只靠 `try/finally` 正确但依赖 CPython 引用计数；显式 `closing` 把它变成代码事实 |
| 自定义模型旁路的覆盖 | `_provider_for` 内部按 `recorder.enabled` 包装 | 比注入 provider 工厂少一个参数；加 `enabled` 守卫使关闭时链路上不多任何一层（AC3） |
| 权限埋点位置 | `loop` 的调用点，不进 `PermissionEngine` | 保持 `permission/` 「纯判定、无副作用」的既有性质；同时天然排除逐文件过滤器那个高频调用点 |
| 逐文件权限过滤 | **不记录** | 一次 grep 触发几百次判定，会淹掉整条 trace；且它判的是「文件是否出现在结果里」，不是工具调用的放行 |
| 事件流埋点位置 | `RhineApp._do_stream` | 唯一必经之处；`_wrap_events` 漏掉手动压缩与会话恢复两条路径 |
| 循环事件的负载 | 显式字段白名单纯函数 `agent_event_payload` | `AgentEvent` 带着工具参数、工具输出、正文三类重负载，照直记违反 F17/AC20。做成纯函数便于单测，与 `clip` 的「单一入口」同口径 |
| 工具执行埋点口径 | `_execute` 内**所有**写 `results[tc.id]` 的七条路径 | 只埋「执行成功」那条会丢掉六类证据，其中 `out_of_scope` 那条正是本模块立项的第二个实证 bug 的唯一物证 |
| 记录器关闭态 | 独立的 `NullRecorder`，不是 `TraceRecorder` 的子类 | 二者无共享实现；继承会让 Null 对象持有永不使用的文件句柄 |
| 零开销的实现 | Null 对象免判空 + 昂贵负载走 `emit_lazy` | spec N1 与 F4 的交集：`if enabled: emit(**贵负载)` 会把负载构造留在 try 之外，构造异常仍会外泄。`emit_lazy` 让工厂调用发生在 try 内 |
| 序号分配与序列化的锁边界 | **序列化、写入、flush、自增全部在同一临界区内** | `seq` 是 JSON 首字段，锁外组装等于锁外读计数，并发下必然产出重复序号（违反 AC8）。`json.dumps` 是微秒级纯 CPU，与死锁风险不同类 |
| 序号推进时机 | 写入并 flush 成功后才自增 | 使 F4（失败静默丢弃）与 AC8（序号无跳号）相容 |
| 截断值形态 | 未截断=裸字符串；截断=三字段对象 | 常见情形零开销，截断情形自带原长（F6）。全项目单一截断入口 `models.clip` |
| 装配结果形态 | `BuildResult` 冻结数据类 | AC3/AC30/AC31 都要断言中间组件；二元组会逼测试翻私有属性 |
| 装配失败表达 | 抛 `BootstrapError`，携带**已成文的完整 stderr 文案** | 测试要能捕获（F22）；三段文案是既有启动测试断言的对象，必须逐字保留，故拼装责任移入工厂、`__main__` 只负责打印 |
| `cleanup` 幂等 | `_done` 布尔守卫 | 其余步骤天然幂等，但 `session_end` 重复产出会破坏「一次运行一份记录」的可读性 |
| 用户级目录参数 | 六处**可选**参数，缺省等于现状 | 实测受影响的既有测试为 0 条；改成必选立刻涨到五个测试文件、十余个调用点 |
| 阅读器位置 | `trace/reader.py`，`python -m` 调用 | 与记录格式强耦合，同处一处避免漂移；随包分发但不注册控制台入口，`pyproject.toml` 一行不动 |
| `trace/__init__.py` 的导出 | 只导出 models 与 recorder，**不导出** `tracing_provider` | 让「trace → provider/base」这条 N4 唯一例外的依赖边在代码里保持显式可见，而不是被包导出隐藏掉 |

---

## 实现分段

本轮改动面较大（新包 4 模块 + 15 类事件埋进 9 个既有模块 + 装配层重构 + 6 处签名参数化
+ 既有启动测试迁桩 + 阅读器 + 7 个测试文件）。按风险切成三段，**每段可独立验收、
独立提交**：

| 段 | 内容 | 可验收的 AC | 风险 |
| --- | --- | --- | --- |
| **一、trace 核心** | `trace/` 四模块 + 三个纯测试文件 | AC1(部分)、AC5–AC8、AC17、AC20、AC22 | 低：不碰任何既有模块 |
| **二、装配层** | `bootstrap.py` + 六处 `user_dir` + `clear_read_roots` + 既有启动测试迁桩 | AC28–AC32、AC39 | **高**：唯一会打断既有测试的一段，单独提交便于回退 |
| **三、埋点铺开 + 阅读器** | 九个模块的埋点 + `reader.py` + 文档同步 | AC2–AC4、AC9–AC16、AC18–AC21、AC23–AC27、AC33–AC38 | 中：面广但每处独立 |

第二段是风险最集中的一段：既有的 `tests/test_command_startup.py`（2 个用例）与
`tests/test_skill_startup.py`（11 个用例）共 **13 个用例、20 余个 `patch.object` 目标**
都是按名字打在 `__main__` 模块上并直调 `main()` 的。装配逻辑迁走后这些符号不在原处，
`patch.object` 会直接 `AttributeError`。**这批迁桩属 AC39 明示允许的范围，
但断言语义不得弱化**——尤其那三段 stderr 文案断言必须原样通过。

---

## 风险与护栏

| 风险 | 后果 | 护栏 |
| --- | --- | --- |
| 埋点在既有锁的临界区内做 IO 或回调 | 与 Textual 阻塞式跨线程调度组成**确定性死锁**，整个界面冻结 | `SkillManager` 的埋点一律写在锁外；死锁护栏测试另起线程读状态并 `join(timeout)`，用完成计数而非布尔标志 |
| 埋点在只读并发桶抛异常 | 被 `future.result()` 当成「工具执行异常」回灌模型 | `emit`/`emit_lazy` 整体 `try/except`；AC6 注入必然抛异常的记录器断言工具结果正常 |
| 作用域泄漏到复用的池化线程 | 后续主对话运行被标成 `isolated`，概率性且极难复现 | `_do_stream` 的 `finally` 首行无条件 `bind_scope(SCOPE_MAIN)` |
| 并发下序号重复 | AC8 失败，且是概率性失败 | 序列化与自增同在一个临界区；并发压力测试断言行数与序号集合 |
| 装配入口迁移打断既有启动测试 | 13 个用例的 `patch.object` 目标失效 | 第二段单独提交；task 阶段逐条登记迁桩；三段 stderr 文案逐字保留 |
| 用户目录参数误做成必选 | 回归面从 0 涨到五个测试文件 | 全部带默认值；`test_bootstrap.py` 保留一组无参调用 |
| `tools/__init__.py` 被误加导出 | `tools ↔ skills`、`tools ↔ mcp` 的包级互依变成真环 | 本次不碰该文件；沿用 CLAUDE.md 已登记的成对维护点 |
| 黄金基线含时变量 | 测试跨午夜/换目录必红 | AC1 只覆盖稳定段与工具 schema（内容恒定）；动态段与存档走 AC2 的同环境双跑对比 |
| 新增事件类型漏登记阅读器摘要表 | 新事件在时间线上显示成空白且不报错 | 列为成对维护点写进 CLAUDE.md；阅读器对未登记类型输出显式「未登记类型」而非空白 |
