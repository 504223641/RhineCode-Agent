# C9 记忆系统（项目指令 · 会话存档 · 自动记忆）Plan

## 架构概览

新增独立的 **Memory 层**（`rhinecode/memory/`），对标 permission / mcp / context 的既有模式：纯逻辑模块在下、唯一的编排者 `MemoryManager` 在上、通过少量单点接入挂进现有流程。六个模块，下层不感知上层：

- **`lockfile.py`（锁原语）**——最底层，纯文件系统操作：原子创建锁（`os.open` + `O_CREAT|O_EXCL`，检查与创建一步完成）、释放、过期判定（锁内容含 PID + 时间戳）。不感知「锁的是记忆还是会话」，只提供「试着拿锁，拿不到就说不行」的原语。机制与策略分离：记忆锁和会话锁的策略（跳过 vs 拒绝载入）留在使用方。
- **`instructions.py`（RHINE.md 加载）**——纯函数：三层文件定位、@include 递归展开（深度 4、visited 防环、层边界校验、围栏代码块跳过）、拼接并标注来源。输出「拼好的文本 + 加载报告（供 /memory 展示）」。
- **`session.py`（会话存档）**——`SessionStore` 类：建档（ID 生成）、逐条追加写、扫描目录出列表（无 meta 文件）、容错载入（坏行跳过、不成对工具调用处理、逐行时间戳）、30 天清理、会话锁的持有与检查。
- **`notes.py`（记忆与索引，纯逻辑）**——记忆文件的 frontmatter 解析/渲染、四类分类常量、索引文件的重建与 200 行/25KB 截断。不做 IO 决策，只做格式转换（对标 c8 的 `summarize.py` 纯逻辑定位）。
- **`note_updater.py`（记忆 LLM）**——记忆更新的 Prompt 常量（禁用工具、要求结构化输出）、把「本轮新增对话」渲染成请求、把 LLM 响应解析为结构化的记忆动作列表（新增/更新/删除/不动）。只产出「打算做什么」，不亲自写盘——写盘权收拢在 manager 一处，配合锁的获取释放构成完整临界区，避免「拿锁的人和写盘的人不是同一个」。
- **`manager.py`（编排者）**——`MemoryManager`：唯一持 provider 引用与副作用编排。启动时加载 RHINE.md、清理过期会话、开新档；运行中提供两个槽位的注入内容、接收每条新消息做追加写、自然停止后起后台线程跑记忆更新（拿锁 → 调 LLM → 写盘 → 更新索引 → 释放锁 → 通知界面）；对外暴露 `/memory` 报告、`/resume` 列表与载入、状态查询。

**四个单点接入**（不打散既有主流程，spec N1）：

1. `__main__.py`——解析 `--continue` 参数；`ConversationManager` 构造时创建 `MemoryManager`（所有 Provider 都建，记忆能力按工具模式门控）。
2. `agent/prompt/`——`build_default_prompt` 增加两个可选参数（自定义指令、长期记忆索引），填进 c5 预留的 110 / 130 槽位。
3. **消息追加钩子**——沿用 c8 把 `context_manager` 传进 `Agent.run` 的先例，再传一个可选的「消息记录回调」：`loop.py` 三处 `history.append(...)` 之后各补一行回调调用（`conversation.py` 追加用户消息处同理），回调内部就是 SessionStore 的追加写。
4. **自然停止钩子**——`ConversationManager._run` 把 Agent 事件流包一层生成器：看到 `FINISHED(COMPLETED)` 就触发 `MemoryManager` 的异步记忆更新（仅工具模式）。

**沙箱例外**（spec F18）：`path_guard.py` 增加一个「额外只读根目录」白名单（仅注册用户级 memory 目录），新增 `resolve_readable` / `is_readable_path` 两个「工作区 **或** 白名单内」的判定入口；`read_file` 与权限引擎②沙箱层的**读类**判定改走它们，**写类判定完全不动**（写永远只限工作区）。

## 核心数据结构

### JSONL 存档行（会话存档的磁盘格式）

每行一个 JSON 对象，是 `Message` 的直接序列化再加一个时间戳：

```json
{"ts": "2026-07-12T10:30:00", "role": "user", "content": "..."}
{"ts": "...", "role": "assistant", "content": "", "tool_calls": [{"id": "...", "name": "read_file", "arguments": {...}}]}
{"ts": "...", "role": "tool", "tool_call_id": "...", "content": "..."}
```

`tool_calls` / `tool_call_id` 仅在对应形态时出现；载入时**未知字段忽略、缺 `ts` 容忍**（spec N5 向前兼容）。`ts` 服务两个需求：24 小时时间跨度判断、列表里的「最后时间」。

### 记忆文件（磁盘格式）

```markdown
---
name: prefer-chinese-comments
summary: 用户要求所有代码注释使用中文
category: preference        # preference | feedback | project | reference
---

正文……
```

### 各模块的内存数据类

```python
# instructions.py
@dataclass
class InstructionLayer:      # 一层 RHINE.md 的加载结果（供 /memory 报告）
    label: str               # "用户级" / "项目级 .rhinecode" / "项目根"
    path: Path
    loaded: bool             # 文件存在且读取成功
    size: int                # include 展开后的字符数
    errors: list[str]        # include 越界 / 超深度 / 循环等提示

@dataclass
class LoadedInstructions:
    text: str                        # 拼好的全部内容（含来源标注）；空串 = 三层全缺
    layers: list[InstructionLayer]

# session.py
@dataclass
class SessionInfo:           # /resume 列表的一项（扫描 JSONL 现算，无 meta 文件）
    session_id: str
    path: Path
    title: str               # 首条 user 消息截断
    message_count: int
    last_time: Optional[datetime]   # 最后一行 ts；无 ts 回退文件 mtime
    locked: bool             # 被新鲜锁保护（正被其它实例使用）

@dataclass
class SessionLoadResult:     # 容错载入的结果
    messages: list[Message]
    skipped_lines: int       # 跳过的坏行数
    dropped_unpaired: int    # 因工具调用不成对被丢弃的消息数
    last_time: Optional[datetime]

# notes.py
@dataclass
class Note:
    filename: str
    name: str
    summary: str
    category: str            # 四类之一：preference | feedback | project | reference

@dataclass
class NoteAction:            # note_updater.py：LLM 决定的一个记忆动作（只是意图，不含 IO）
    op: str                  # "add" | "update" | "delete"
    scope: str               # "user" | "project"
    filename: str
    note: Optional[Note]     # delete 时为 None
```

## 模块设计

### `lockfile.py`（锁原语）

**职责：** 跨进程互斥的最小机制，不含任何业务语义。
**对外接口：**
- `try_acquire(lock_path, stale_after_seconds) -> bool`——用 `os.open(O_CREAT|O_EXCL)` 原子创建，写入 PID + 时间戳；已存在时做过期判定，过期则清除后**重试一次**；任何异常返回 False（拿不到锁）。
- `release(lock_path)`——删除锁文件，失败静默。
- `touch(lock_path)`——刷新 mtime（会话锁心跳用）。
- `is_fresh(lock_path, stale_after_seconds) -> bool`——供「列表标注 locked」「清理跳过」使用。

**依赖：** 无（纯 stdlib）。过期阈值由调用方传入：**记忆锁 600 秒、会话锁 600 秒**（会话锁靠心跳保鲜，见 manager）。

### `instructions.py`（RHINE.md 加载，纯函数）

**职责：** 三层定位 → 逐层读取 → @include 递归展开 → 拼接标注。
**对外接口：** `load_instructions(user_dir, project_root) -> LoadedInstructions`
**关键内部逻辑：** `_expand_includes(text, base_dir, boundary_root, depth, visited, errors)`——逐行扫描 `@路径` 引用；围栏代码块（``` 包围）内跳过；相对 `base_dir` 解析、`resolve()` 后必须仍在 `boundary_root` 内；`depth > 4` 或路径已在 `visited` 中则不展开、记入 errors。
**依赖：** 无。

### `session.py`（SessionStore）

**职责：** 会话存档的全部磁盘操作 + 会话锁的持有。
**对外接口：**
- `start_new() -> str`——生成 ID（`YYYYMMDD-HHMMSS-xxxx`）；**惰性建档**（首条消息真正落盘时才创建文件和锁，避免「开了就退」留下空档垃圾）。
- `append(msg: Message)`——序列化一行追加；顺带 `touch` 会话锁；IO 失败静默（spec F6）。
- `list_sessions(limit) -> list[SessionInfo]`——扫描目录逐个推导（spec F7），标注 locked。
- `load(session_id) -> SessionLoadResult`——容错载入：坏行跳过；`assistant(tool_calls)` 与后续 `tool` 结果不成对时**丢弃该组**（中间、结尾统一处理，spec F11②/F12）。
- `attach(session_id) -> bool`——接管会话：检查目标锁（新鲜→False；过期→清除），拿新锁，切换当前活跃档为该文件。
- `cleanup_expired(days=30) -> int`——删过期档（跳过新鲜锁），清孤儿锁（spec F13）。
- `release()`——退出时释放当前会话锁。

**依赖：** `lockfile.py`、`provider.base.Message`。

### `notes.py`（纯逻辑）

**职责：** 记忆与索引的格式转换，零 IO 决策。
**对外接口：** `parse_note(text) -> Optional[Note]`（宽松解析，坏文件返回 None）、`render_note(note) -> str`、`rebuild_index(notes) -> str`（每条一行：`- 标题（文件名）— 摘要钩子`）、`truncate_index(text) -> str`（200 行 / 25KB 先到为准）。
**依赖：** 无。

### `note_updater.py`（记忆 LLM，纯逻辑 + 请求渲染）

**职责：** 把「本轮新增对话 + 现有两级索引」渲染成一次**禁用工具**的 LLM 请求，把响应解析为 `list[NoteAction]`。
**对外接口：** `build_note_request(new_messages, user_index, project_index) -> list[Message]`、`parse_note_response(text) -> list[NoteAction]`（要求 LLM 输出 JSON 数组；不可解析 → 空列表，宽松跳过坏项，spec F17）。
**Prompt 要求：** 四类分类、归属判断（用户级 vs 项目级）、与现有索引对照去重、没有值得记的就返回空数组。
**依赖：** `notes.py`、`provider.base.Message`。

### `manager.py`（MemoryManager，唯一编排者）

**职责：** 持 provider 引用与全部副作用编排；把五个下层模块串成完整流程。
**对外接口（按调用方分组）：**

```python
class MemoryManager:
    def __init__(provider, model, project_root, user_dir, notes_enabled, notify=None)
    # __main__ / 启动
    def startup(resume_latest: bool) -> Optional[str]   # 加载 RHINE.md、清理过期档、
                                                        # 开新档或 --continue 载入；返回启动提示
    # conversation / 每次运行
    def custom_instructions() -> str        # → 110 槽位
    def memory_index() -> str               # 现读两级索引、截断 → 130 槽位
    def consume_pending_notice() -> str     # 一次性动态提醒（时间跨度等），取后即清
    def record_message(msg: Message)        # 追加写钩子（用户消息与 loop 回调共用）
    def on_natural_stop(history)            # 起后台线程跑记忆更新（notes_enabled 才动）
    def on_clear()                          # /clear：释放旧锁、开新档
    # 命令
    def resume_list() -> str                                    # /resume 无参
    def resume_into(key, history) -> tuple[bool, str]           # /resume <key>：锁检查+载入+
                                                                # 替换 history+时间提醒，返回(成败,提示)
    def memory_report() -> str                                  # /memory
    # tui
    def touch_session_lock()                # 心跳定时器（Textual set_interval，2 分钟）
    def close()                             # 退出释放会话锁
```

**依赖：** 全部五个下层模块 + `BaseProvider`。

## 模块交互

### ① 启动（`__main__` → ConversationManager → MemoryManager）

```
__main__: 解析 --continue
  → ConversationManager.__init__ 构造 MemoryManager（所有 Provider 都建；
      notes_enabled = 工具模式；provider/model/项目根/~/.rhinecode 注入）
  → memory.startup(resume_latest=args.continue)
      1. instructions.load_instructions(...)   # RHINE.md 三层 + include 展开，缓存在内存
      2. session.cleanup_expired(30 天)        # 跳过新鲜锁、清孤儿锁
      3a. --continue：按 last_time 找最近会话 → attach（锁被占则顺延下一个）→ load
          → 替换 history → 超 24h 则登记 pending 时间提醒
      3b. 普通启动：session.start_new()（惰性建档）
      → 返回启动提示（「已恢复会话 xxx（N 条消息）」等），TUI 挂载时显示
  → app 挂载后注入 notify 回调（call_from_thread 线程安全提示行），启动会话锁心跳定时器
  → 退出 finally：memory.close() 释放会话锁（与 mcp_manager.close_all 并列）
```

### ② 每条普通消息（注入 + 追加写 + 自然停止钩子）

```
handle_input(text)
  → history.append(user)；memory.record_message(user)        # 追加写（用户消息侧）
  → _run():
      env = collect_environment(...)
      assembled = build_default_prompt(env,
          custom_instructions = memory.custom_instructions(),  # → 110 槽位
          memory_index        = memory.memory_index())          # → 130 槽位（现读现截断）
      dynamic += memory.consume_pending_notice()                # 一次性时间跨度提醒
      events = agent.run(..., context_manager, recorder=memory.record_message)
                 # loop 内 3 处 history.append 之后各调一次 recorder（assistant / tool 侧）
      return _wrap(events):                                     # 事件流包装生成器
          逐个 yield；遇到 FINISHED(COMPLETED) → memory.on_natural_stop(history)
```

### ③ 记忆异步更新（on_natural_stop 内部，daemon 线程）

```
进程内 in-flight 标志已置？ → 是：跳过本轮（不排队不堆积）
起线程：
  new_msgs = history[高水位:]        # 高水位 = 上次记忆检查时的消息数，避免重复审视
  req = note_updater.build_note_request(new_msgs, 用户级索引, 项目级索引)
  resp = provider.stream_chat(req, tools=None)                 # 禁用工具
  actions = note_updater.parse_note_response(resp)             # 坏输出 → []
  按 scope 分组 → 对每个 memory 目录：
      lockfile.try_acquire(目录/.lock, 600s)？
        否 → 该目录整组跳过（F22 退让）
        是 → try: 执行 add/update/delete 写盘 → 扫描目录重建索引文件
             finally: lockfile.release(...)
  有实际变更 → notify("已更新记忆：xxx")（F20）；全程异常 → 记入「最近一次结果」，静默（F17）
```

### ④ /resume（事件流，走 Worker——载入后可能触发压缩这个阻塞 LLM 调用）

```
"/resume"        → memory.resume_list() 同步返回文本
"/resume <key>"  → 事件流生成器：
    memory.resume_into(key, history):
        目标锁新鲜？ → 拒绝：「该会话正被另一个 RhineCode 实例使用」
        attach（过期锁清除接管）→ load（坏行跳过/不成对丢组）→ history[:] = messages
        → 超 24h 登记 pending 提醒 → 返回 (True, "已恢复…，跳过坏行 N")
    成功且工具模式 → context_manager.reset()          # 旧锚点对新历史无效，必须先复位
                   → for notice in context_manager.before_request(history): yield NOTICE
                     # spec F11③：逼近窗口就地压一次，不逼近则无动作
    yield NOTICE(结果文本) → yield FINISHED(COMPLETED)
```

### ⑤ 其余命令与钩子

- `/memory` → `memory.memory_report()` 同步文本（纯只读）。
- `/init` → 工具模式检查 → 把内置指令文本（`prompt/texts/init.py`）作为一条 user 消息追加进 history（会被正常存档）→ 走 `_run()` 普通循环——探索与写文件全部复用现有工具与权限管线。
- `/clear` → 既有逻辑 + `memory.on_clear()`（释放旧会话锁、`start_new()`）。
- 沙箱例外：`read_file` 与权限引擎②沙箱层的**读类**判定改走 `path_guard.resolve_readable / is_readable_path`（工作区 ∪ 白名单，白名单仅含 `~/.rhinecode/memory/`）；写类判定不动。

## 文件组织

```
rhinecode/
├── memory/                    # 新增：Memory 层
│   ├── __init__.py            # 导出 MemoryManager 等公共接口
│   ├── lockfile.py            # 锁原语：原子创建/释放/过期判定
│   ├── instructions.py        # RHINE.md 三层加载 + @include 展开（纯函数）
│   ├── session.py             # SessionStore：JSONL 建档/追加/扫描/载入/清理/会话锁
│   ├── notes.py               # 记忆 frontmatter 与索引的解析/渲染/截断（纯逻辑）
│   ├── note_updater.py        # 记忆 LLM 的 Prompt 与响应解析（产出结构化动作）
│   └── manager.py             # MemoryManager：编排、持 provider、状态汇总
├── agent/
│   ├── loop.py                # 改：run() 增加可选消息记录回调，3 处 append 后调用
│   └── prompt/
│       ├── builder.py         # 改：build_default_prompt 增加两个槽位内容参数
│       └── texts/init.py      # 新增：/init 的内置指令文本
├── tools/
│   ├── path_guard.py          # 改：额外只读根目录白名单 + resolve_readable/is_readable_path
│   └── read_file.py           # 改：路径解析改走 resolve_readable
├── permission/engine.py       # 改：②沙箱层对读类请求改走 is_readable_path
├── conversation.py            # 改：构造 MemoryManager；/resume /memory /init 命令；
│                              #     用户消息追加写；事件流包装（自然停止钩子）
├── tui/widgets.py             # 改：CommandPanel.COMMANDS 增 /resume /memory /init
├── tui/app.py                 # 改：注入记忆通知回调（call_from_thread 线程安全提示行）；
│                              #     会话锁心跳定时器
└── __main__.py                # 改：--continue 参数

tests/
├── test_memory_instructions.py   # include 展开/防环/越界/代码块跳过/三层拼接
├── test_memory_session.py        # 追加写/扫描/坏行/不成对丢组/清理/ID 格式
├── test_memory_notes.py          # frontmatter 解析渲染/索引截断
├── test_memory_lockfile.py       # 原子性/过期自愈/不可写降级
└── test_memory_manager.py        # 编排：假 provider 记忆流程/锁被占跳过/失败静默
```

## 技术决策

| 决策点 | 选择 | 理由 |
|--------|------|------|
| 锁实现 | `os.open(O_CREAT\|O_EXCL)` 原子创建 + mtime 过期判定 | 跨平台一行代码（Windows/Linux 同语义），无 `fcntl`/`msvcrt` 平台分支；非阻塞退让天然契合「尽力而为」 |
| 会话锁保鲜 | TUI 定时器每 2 分钟 `touch`，过期阈值 10 分钟 | 「进程活着锁就新鲜」；比写 PID 再探测进程存活跨平台简单得多 |
| 建档时机 | 惰性——首条消息落盘才建文件与锁 | 「打开看一眼就退」不留空档垃圾 |
| 存档内容 | 原始消息流（c8 压缩**前**） | c8 会原地改写 history（占位/重构）；存档保全量原文，恢复后由 c8 重新压缩（spec F11③） |
| 两个槽位的缓存通道 | 「自定义指令」与「长期记忆」都改 `cacheable=True` 进 stable 通道（覆盖 c5 空槽的 False 预设） | dynamic 通道内容每轮作为不缓存尾巴重发——RHINE.md 数百行、索引最大 25KB，每轮重发太贵；进 stable 可被 DeepSeek 前缀缓存命中。索引若在会话中途变化，只失效它自己那段尾部缓存（它按 priority 排在 stable 最末），前面模块缓存不受影响 |
| 记忆 LLM 输出 | JSON 数组（每项一个 NoteAction） | 结构化易解析；宽松解析跳过坏项，整体不可解析按「无动作」处理 |
| 记忆写盘权 | 程序写盘，LLM 只产内容与归类 | 写入路径由代码锁死在两个 memory 目录内，模型输出注入不了任意路径（spec N6） |
| 时间跨度提醒 | pending notice 并入**下一次**请求的 dynamic reminder，一次性 | 不污染持久历史——它是「此刻的环境事实」，不该被存档、更不该在恢复时再次出现 |
| 不成对工具调用 | **丢弃该组**（assistant(tool_calls) 连同已有的部分 tool 行），中间/结尾统一 | 比「从断点截断到结尾」保住更多历史；组内消息离开彼此本就没有意义 |
| 恢复后的 c8 锚点 | `context_manager.reset()` 后再压缩 | 估算锚点对应的是旧 history，换历史不复位会严重低估用量 |
| 进程内记忆并发 | 单 in-flight 标志，占用即跳过 | 不排队不堆积；快速连发消息时最多漏记一轮，下轮补上 |
| 记忆高水位 | 记录上次记忆检查时的消息数，只审视新增段 | 避免每轮把全量历史重发给记忆 LLM（成本）且防重复记忆 |
