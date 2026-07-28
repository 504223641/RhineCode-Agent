# TUI 驱动器 P1a（交互闭环）Plan

> 对应 `docs/c11/testing/p1-driver/spec.md`（已审批）。本文回答「怎么做」。
> 语言：Python 3.11+，与产品同栈；不引入任何新的第三方依赖
> （只用标准库 `socket` / `json` / `threading` / `asyncio` / `concurrent.futures` /
> `tempfile` / `hashlib` / `subprocess`）。
>
> **本文的技术断言全部经实测**（在真实 `RhineApp` 上、在无终端的后台进程里跑通了
> 完整闭环）。凡标注「实测」的段落都是跑出来的，不是推断出来的。

---

## 0. 先讲清四个基础概念

本轮引入了四个此前项目里没有出现过的东西，先各用一段话说明，后文不再解释。

**① 常驻宿主进程（host）**
一个「跑起来就不退出」的后台进程。它内部把 RhineCode 完整装配起来并让界面一直活着，
同时开一个「接线员」等外面发指令。之所以需要它，是因为界面测试框架给的驱动上下文
（`app.run_test()`）是一次函数调用里的异步上下文——进去、跑完、出来就没了；
而外部驱动者（AI 协作方）的每次操作都是**独立进程**，不常驻就等于每次从零开一个新
Rhine，历史全丢。

**② 控制通道（control channel）与瘦客户端（client）**
宿主在本机回环地址（`127.0.0.1`）上监听一个端口，外部用一个极简命令行程序连上去，
发一条 JSON 指令、收一条 JSON 响应、断开。这就是「进程间通信（IPC）」最朴素的形态。
选 socket 而不是「写文件轮询」，是因为 socket 天然支持**阻塞等待**——
`wait` 指令可以一直挂着直到会话进入终态，而文件轮询只能高频空转。

**③ 工厂注入（factory injection）**
产品代码里 `create_provider(cfg)` 是写死的「造一个模型客户端」。要让测试塞一个假的
进去，又不能到处 `if 测试模式`，通行做法是把「怎么造」提取成一个**可传入的函数**
（工厂），默认值就是原来那个。调用方不传 = 行为一字不变；测试传一个假工厂 = 全链路
换成假模型。这与 P0 给 `build_app` 加 `user_dir` / `recorder` 是同一个套路。

**④ 事件循环与「在主线程上跑一段代码」**
Textual 应用跑在一个 asyncio **事件循环**上，这个循环独占主线程。界面的任何状态
（控件、焦点、面板）都只能在主线程上安全读写。别的线程要碰它，必须把工作
「投递」到主线程上排队执行。Textual 提供了 `app.call_from_thread(...)` 做这件事，
但它有两条**实测确认**的硬限制，直接决定本文的设计：

- **它没有超时参数，且会一直阻塞到工作跑完**。实测：主线程被一个 8 秒的回调堵住时，
  调用方整整等了 7.8 秒才返回——「先调用再 `queue.get(timeout=1)`」这种写法**毫无作用**，
  因为 `get` 是它返回之后才执行的语句。
- **它拒绝从主线程自己调用**，会抛
  `RuntimeError: The 'call_from_thread' method must run in a different thread from the app`。

因此本文一律使用**自建的带超时投递原语**（见 §2.6），只在明确处于非主线程时使用。

---

## 1. 架构概览

```
┌──────────────────────────────────────────────────────────────┐
│ 外部驱动者（Claude Code）                                      │
│   每次操作 = 一次独立进程调用                                   │
└───────────────┬──────────────────────────────────────────────┘
                │ python -m tests.e2e.client <指令> ...
                ▼
┌──────────────────────────────────────────────────────────────┐
│ client.py  瘦客户端（无状态、不 import 产品、不重试）            │
│   读发布文件找到宿主 → TCP 连接 → 发一条 JSON → 收一条 JSON      │
└───────────────┬──────────────────────────────────────────────┘
                │ 127.0.0.1:<系统分配端口>
                ▼
┌──────────────────────────────────────────────────────────────┐
│ host.py  常驻宿主进程                                          │
│  ┌────────────────┐   ┌──────────────────────────────────┐   │
│  │ accept 线程     │──▶│ control.py DriverCore             │   │
│  │ 每连接一 handler│   │  状态判据 / 等待两终态 / 应答      │   │
│  └────────────────┘   │  取消 / 超时诊断 / 退出编排        │   │
│  ┌────────────────┐   └───────────┬──────────────────────┘   │
│  │ watchdog 线程   │               │ run_on_main（带超时）     │
│  └────────────────┘   ┌───────────▼──────────────────────┐   │
│                       │ 主线程：asyncio + run_test() 常驻  │   │
│                       │   RhineApp（真实界面）             │   │
│                       │     └─ Worker 线程：Agent 循环     │   │
│                       │     └─ rhine-notes 线程（live）    │   │
│                       └──────────────────────────────────┘   │
└──────────────────────────────────────────────────────────────┘
        │ 写                                    │ 写
        ▼                                       ▼
  <系统临时目录>/rhinecode-e2e/          <临时工作区>/.rhinecode/traces/*.jsonl
      host-<pid>.json（发布文件）                     │ 读
                                                     ▼
                                          assertions.py（复用 trace.reader）
```

**启动顺序有一处关键安排**（见 §3.8）：**socket 先监听、发布文件先写，装配再开始**。
理由是装配期的致命错误必须能经通道回报（spec AC5 点名要验这个），
而装配一旦失败就永远等不到「监听开始」。

**产品侧只动三处**（都可选、缺省等于现状，详见 §3.10）：
`build_app` 增两个可选参数、`ConversationManager` 增一个可选参数、
`RhineApp` 的交互结算点把硬编码的来源字段改为可传入。

---

## 2. 核心数据结构与接口

### 2.1 控制通道协议（`protocol.py`，纯数据，零 IO）

一条指令一次往返，**行分隔的 JSON**（每条消息以 `\n` 结尾）。
**编解码一律显式 UTF-8**，与系统默认编码无关（N9）——
实测中文经通道往返正确，只有终端打印会因控制台代码页而显示为乱码，那是显示层问题。

```python
# 请求
{"cmd": "send",   "text": "/skills"}
{"cmd": "status"}
{"cmd": "wait",   "timeout": 180.0}
{"cmd": "answer", "choice": "once", "via": "channel"}   # via 可省，默认 channel
{"cmd": "cancel"}
{"cmd": "observe","since": 42, "types": ["tool_execute"]}
{"cmd": "quit"}

# 成功响应
{"ok": true, "data": {...}}
# 失败响应
{"ok": false, "error": {"code": "not_pending", "message": "...", "data": {...}}}
```

```python
ERROR_CODES = {
    "bad_request",    # 指令名或参数不合法
    "starting",       # 装配尚未完成（socket 先于装配就绪，见 §3.8）
    "not_pending",    # answer 时没有待决面板
    "busy",           # send 时会话不处于空闲态
    "timeout",        # wait 超时（error.data 里带诊断）
    "turn_budget",    # 触及单次会话轮次预算（N5）
    "fatal",          # 宿主已进入致命错误态（error.message 为成文文案）
    "shutting_down",  # 宿主正在退出
}
```

**`status` 的响应负载**（同时服务 F11、AC11、S12 的 pid 校验）：

```python
{
  "pid": 25364,                      # 客户端据此识别「端口被别的进程复用」
  "state": "starting" | "idle" | "busy" | "pending" | "fatal" | "shutting_down",
  "panel": None | {
      "kind": "confirm" | "clarify" | "approve" | "session",
      "display": "<面板原始字符串，含 [dim] 之类的 markup 标记，未渲染>",
      "options": [{"id": "once", "label": "<同样是原始字符串>"}, ...],
  },
  "trace_seq": 137,        # 记录里最后一条可解析事件的 seq
  "turns": 6, "turn_budget": 40,
  "fingerprint": "9f2c1ab34d0e",
  "workspace": "...", "user_dir": "...", "trace_path": "...", "mode": "scripted",
}
```

**markup 口径**（S10 / N9 / AC43）：`display` 与 `options[].label` 一律给**控件持有的
原始字符串**（含 `[dim]…[/dim]` 这类标记），**不提供渲染后文本**。
渲染态的对照由记录里的 `ui_message` / `status_bar` 事件承载——P0 已经确立了
「转义前原文」的口径，这里不另起一套。

**`wait` 的响应负载**：

```python
{"terminal": "idle" | "pending", "state": {...同 status...}}
# 超时走失败响应，error.code="timeout"，error.data 为诊断（F7）：
{"stream_active": true, "pending": false, "session_panel": false,
 "last_action": "send:/commit 修复登录超时", "waited": 180.0, "state": "busy"}
```

**`observe` 的响应负载**（S8；AC34 要的是「遍历控件取不到的那部分」，
所以必须给 payload 而不只是摘要行）：

```python
{
  "next_since": 210,                    # 下次增量的游标
  "skipped": 0,                         # 坏行数（末行半截不计入，见下）
  "timeline": ["  137 12:03:44 main  tool_execute  read_file ok 12ms", ...],
  "events": [ {完整事件记录，含被截断字段的 {text, truncated, original_length}} ],
}
```
截断口径沿用 P0，不做二次加工。
**末行可能是半截**：宿主正在写、外部正在读，`observe` 须把「最后一行解析失败」
当作正常并丢弃，**不计入 `skipped`**——否则观察指令会周期性报告一条不存在的损坏。

### 2.2 面板决策的取值表（`protocol.py` 内的常量）

| 面板 | `choice` 取值 | 结算为 |
| --- | --- | --- |
| `confirm` | `once` / `session` / `permanent` / `deny` | 四态确认枚举 |
| `approve` | `yes` / `no` | `True` / `False` |
| `clarify` | 选项序号（字符串数字） | 该选项的摘要文本 |
| `session` | 会话标识，或 `cancel` | 载入该会话 / 关闭面板不载入 |

**`via` 字段**（S7）：`channel`（默认，直接结算，来源记 `driver`）或
`keys`（模拟按键走面板自身的按键路径，来源保持 `human`）。
AC15 要求同一次运行内出现两种不同来源，`via` 是它的驱动手段；
`keys` 形态只支持把光标移到目标项再回车，故仅对 `confirm` / `approve` / `session` 有效。

### 2.3 发布文件（`discovery.py`）

位置：`<系统临时目录>/rhinecode-e2e/host-<pid>.json`
（**与工作区无关的固定可发现位置**——工作区是宿主自己新建的随机目录，
客户端启动时并不知道它在哪，把发布文件放进工作区是个死循环。）

```python
@dataclass(frozen=True)
class HostInfo:
    pid: int; port: int; workspace: str; user_dir: str
    trace_path: str; fingerprint: str; mode: str; started_at: float
```

**陈旧判定**（不做进程存活检测，跨平台麻烦）分两级：
1. 拿到 `port` 后连接，`ConnectionRefusedError` → 判定陈旧，立即报
   「宿主已不在（发布文件 X，pid N）」并提示清理，**不等待超时**（AC10）；
2. 连上后校验响应里的 `pid` 与发布文件一致，不一致 → 判定为**端口被别的进程复用**，
   同样按陈旧处理（S12）。

**发布目录不可写**：宿主启动时若建不了该目录，直接以非零退出码终止并写 stderr——
没有发布文件就没人找得到它，继续跑下去毫无意义。

### 2.4 脚本化假模型（`scripted.py`）

```python
@dataclass(frozen=True)
class RecordedCall:
    index: int                       # 第几次调用，从 0 起
    messages: list[Message]          # 完整消息列表（原样引用，不截断）
    system: Optional[str]            # 稳定系统提示全文
    tools: Optional[list[dict]]      # 本轮实际发出的工具 schema
    thinking_effort: str

    @property
    def tool_names(self) -> set[str]                 # 断言词汇 ①
    @property
    def dynamic_reminder(self) -> str                # 断言词汇 ⑪
        # messages[-1] 若 role == "system" 取其 content，否则空串。
        # 动态提醒是每轮临时拼在历史末尾的一条 system 消息，
        # 已激活的 Skill 正文在这里，不在 system 参数里。

class ScriptedProvider(BaseProvider):
    def __init__(self, turns: list[list[StreamChunk]],
                 fallback: Optional[list[StreamChunk]] = None): ...
    calls: list[RecordedCall]
    def stream_chat(self, messages, thinking_effort="off",
                    tools=None, system=None) -> Iterator[StreamChunk]: ...
```

便捷构造器（模块级函数，让脚本可读）：
`text("...")` / `thinking("...")` / `tool("read_file", {"path": "a.txt"})` /
`stream_error("连接中断")` / `usage(prompt=100, completion=20)` / `done()`。

**脚本耗尽的兜底**（F22）：默认 `fallback = [text("[e2e-fallback]"), done()]`。
取这个字面量是为了可识别——上下文摘要与自动笔记都会额外调模型，
读记录时一眼能认出「这条不是脚本里写的」。

**线程安全**：`calls` 会被 Agent 工作线程追加、被断言层在主/测试线程读取，
用一把只保护追加的独立锁；临界区内只做 `append`，不做任何调度（N6）。

### 2.5 应答者接缝（`control.py`，落实 spec F6）

```python
@dataclass(frozen=True)
class PanelSnapshot:
    kind: str                    # confirm / clarify / approve / session
    display: str
    options: list[dict]

class Responder(Protocol):
    """决定「这个面板该怎么答」。本轮只实现外部通道一种。"""
    source: str                  # 写进记录的来源标识
    def decide(self, panel: PanelSnapshot) -> str: ...   # 返回 CHOICE_TABLE 取值

class ExternalResponder:
    """本轮唯一实现：阻塞等待控制通道送来的 answer 指令。"""
    source = "driver"
```

P1b 的固定策略应答者与脚本预设应答者只是换一个 `Responder` 实现
（`source = "policy"`），`DriverCore` 一行不动。

### 2.6 跨线程投递原语（`control.py`，M1 的正确写法）

```python
def run_on_main(loop, coro, timeout: float):
    """
    在 Textual 的事件循环（主线程）上执行一个协程并带超时取回结果。

    为什么不用 app.call_from_thread：它没有超时参数、且会一直阻塞到工作跑完
    （实测主线程被 8 秒回调堵住时，调用方等满 7.8 秒），还会拒绝从主线程调用。

    :raises TimeoutError: 超时。**注意：超时只让调用方脱身，不取消已排队的工作。**
                          这正是 N7「失败即失败」要的语义——超时就是超时，
                          不能假装成功，也不能重试掩盖。
    """
    fut = asyncio.run_coroutine_threadsafe(coro, loop)   # 返回 concurrent.futures.Future
    return fut.result(timeout=timeout)                   # ← 真正的超时在这里
```

同步工作一律包成 `async def` 再走这个原语，**全项目只保留这一个跨线程入口**。

### 2.7 驱动内核（`control.py`）

```python
class SessionState(str, Enum):
    STARTING="starting"; IDLE="idle"; BUSY="busy"; PENDING="pending"
    FATAL="fatal"; SHUTTING_DOWN="shutting_down"

class DriverCore:
    def __init__(self, app, pilot, loop, build_result, responder, *,
                 turn_budget: int, trace_path: Path): ...

    # —— 只读，随时可答，不持驱动锁 ——
    def snapshot(self) -> dict
    def observe(self, since: int, types) -> dict

    # —— 改变状态，四段式加锁（见不变量①）——
    def send(self, text: str) -> dict
    def answer(self, choice: str, via: str = "channel") -> dict
    def cancel(self) -> dict

    # —— 阻塞，不持驱动锁 ——
    def wait(self, timeout: float) -> dict

    # —— 只能在主线程调用（见 M3 / §3.8）——
    async def shutdown_on_main(self, reason: str) -> None
```

### 2.8 断言层（`assertions.py`）

```python
class TraceView:
    """对一份记录产物的只读查询视图。加载与过滤一律复用 rhinecode.trace.reader。"""
    @classmethod
    def load(cls, path: Path) -> "TraceView"        # 内部调 reader.load_records
    def of_type(self, *types: str) -> list[dict]    # 内部调 reader.filter_records
    def in_scope(self, *scopes: str) -> "TraceView"
    def by_seq(self, seq: int) -> dict
    def nth(self, type_: str, n: int) -> dict
    skipped: int
```

十一项断言词汇实现为模块级函数，签名统一为 `check_xxx(...) -> CheckResult`
（含 `ok` / `message` / `evidence_seqs`）。失败时由 `assert_check(result, view)` 抛
`AssertionError`，消息里附 `evidence_seqs` 前后各 3 条事件的摘要行
（复用 `reader.summarize`），落实 F26 与 AC37。

| # | 词汇 | 事实来源 |
| --- | --- | --- |
| ① | 某轮发出的工具名集合 | `RecordedCall.tool_names`（真实模式退化到 `api_request`） |
| ② | 某工具是否执行过及结局 | 记录 `tool_execute` |
| ③ | 界面消息含某文本 | 记录 `ui_message` |
| ④ | 状态栏含 / 不含某段 | 记录 `status_bar` |
| ⑤ | 权限决策的层与结果 | 记录 `permission_decision` |
| ⑥ | 事件的作用域归属 | 任意事件的 `scope` |
| ⑦ | 两事件先后 | `seq` 比较 |
| ⑧ | 某类事件条数 | 记录计数 |
| ⑨ | 主历史消息条数 | **会话存档 JSONL** |
| ⑩ | 稳定系统提示含某文本 | `RecordedCall.system` |
| ⑪ | 动态提醒含某文本 | `RecordedCall.dynamic_reminder` |

---

## 3. 模块设计

### 3.1 `tests/e2e/protocol.py`
**职责**：控制通道的线上格式——编解码、错误码、面板决策取值表、`via` 取值。
**对外接口**：`encode` / `decode` / `ok(data)` / `err(code, msg, data=None)`、
`ERROR_CODES`、`CHOICE_TABLE`。**纯数据、零 IO**，可脱离宿主单测。

### 3.2 `tests/e2e/discovery.py`
**职责**：发布文件的写入、扫描、读取与两级陈旧判定（见 2.3）。
**对外接口**：`publish(info) -> Path`、`unpublish(pid)`、`list_hosts()`、
`resolve_host(pid=None)`（0 个报错、多个要求 `--pid`）。

### 3.3 `tests/e2e/sandbox.py`
**职责**：临时工作区与临时用户级目录的创建、F8 第一条的校验、结束时清理。
```python
def create_workspace(prefix="rhine_e2e_ws_") -> Path      # tempfile.mkdtemp
def create_user_dir(prefix="rhine_e2e_user_") -> Path
def assert_disposable(path: Path) -> None
def cleanup_workspace(path: Path, *, previous_cwd: Path) -> None
```
**校验判据**（M6 的修正）：`path.resolve()` 必须落在
`Path(tempfile.gettempdir()).resolve()` 之下，**且目录内存在本设施写下的标记文件**
（`create_workspace` 创建时落一个 `.rhine-e2e-workspace`）。
用标记文件而不是「本进程创建的集合」，是因为宿主进程与测试进程可能不是同一个；
两者等价地表达了「这是本设施造出来、可以随时丢弃的目录」。
**目录可以非空**——校验与「是否为空」完全解耦（AC18 的两条一起验）。

**清理顺序不可调**（S3，实测过）：
```
build_app 的 cleanup()   →  os.chdir(previous_cwd)  →  rmtree(...)
```
理由：① 记录文件句柄由 `cleanup` 第⑤步关闭，不先关就删不掉；
② Windows 下当前工作目录**位于**待删目录内时 `rmtree` 必抛
`PermissionError [WinError 32] 另一个程序正在使用此文件`（实测），
换回原目录后才能删。这正是 AC24 那条「连续运行若干次不因目录占用失败」要抓的东西。

### 3.4 `tests/e2e/seeding.py`
**职责**：F15 的预置。每个函数只做一件事，可自由组合。
```python
def seed_files(root, mapping: dict[str, str]) -> None
def seed_git_repo(root, commits: list[dict]) -> None
def seed_project_skill(root, name, frontmatter, body) -> Path
def seed_user_skill(user_dir, name, frontmatter, body) -> Path
def seed_permissions(target_dir, allow=(), deny=()) -> Path
def seed_rhine_md(root, text) -> Path
```
**约束**：
- 一律显式 `encoding="utf-8"`；`seed_git_repo` 的 `subprocess` 调用显式指定编码与 `cwd`。
- **git 是本设施的环境前置**（S16）：缺少 git 时 `seed_git_repo` 明确抛错、
  不静默跳过——静默跳过会让依赖提交历史的场景假绿。这与 spec N3「零外部依赖」不冲突：
  git 是本地可执行程序，不是网络依赖。此前置须写进 checklist。
- **预置 Skill 的 `allowed_tools` 不得写 `mcp_add_server` / `mcp_resolve_server`**
  ——见 §3.10 的 `exclude_tools` 说明。

### 3.5 `tests/e2e/fingerprint.py`
**职责**：F9 的代码版本标识。**对外接口**：`compute(package_root) -> str`（sha256 前 12 位）。
**算法**：遍历 `rhinecode/**/*.py`（排除 `__pycache__`），按相对路径排序，
把 `(相对路径, 字节数, mtime_ns)` 喂进哈希。
**为什么用 mtime 而不是文件内容**：快，且「改了就变」是本条唯一要保证的性质。
**已知代价（不回避）**：`pip install -e .`、切分支、某些编辑器的保存都会改 mtime 而
内容未变，于是会**误报「代码变了」**。AC11 的用法（改代码 → 重启 → 比对不同）不受影响，
但日常观察会时不时看到它变化。之所以仍选它：误报只会促使你多重启一次宿主，
漏报会让你以为改动生效了其实没有——后者是本条需求存在的全部理由。

### 3.6 `tests/e2e/scripted.py`
见 2.4。**依赖**：`rhinecode.provider.base`。测试依赖产品，方向正确（N2）。

### 3.7 `tests/e2e/control.py` —— 驱动内核

**四条不变量（写进模块 docstring，违反即死锁、静默失败或随机红）**：

1. **加锁四段式**：临界区内**只做纯内存读写**（取快照、改 `_last_action` / `_turns` /
   `_state`），**一切跨线程调度必须在锁外**。
   反例（不可写）：持锁 → `run_on_main` → 主线程被慢回调堵住 → 锁被无限期占住 →
   同样要锁的 `cancel` 排不进去，**恰恰在最需要取消的时候失去响应**。
   这与 C11 `SkillManager` 的四段式加锁完全同型（先在锁内决定，出锁后再驱动界面）。
2. **`wait` 不持驱动锁**。它只轮询只读快照，否则 `wait` 期间 `status` 就答不了，违反 N4。
3. **应答前必须复核「面板已展示且已获得焦点」**，判据写死为：
   目标面板控件 `.display is True` **且** `type(app.focused) is 该面板控件类`。
   注意 `ConfirmPanel` 被 `confirm` 与 `approve` 两种交互复用，
   **面板类型必须取自待决盒的 `kind`，不能从控件类反推**。
   实测抢跑窗口占比 8869/8870（待决态置位早于面板挂载），不复核几乎必然应答在空处。
4. **跨线程只走 `run_on_main`**（§2.6），一律带超时。禁止直接使用 `call_from_thread`
   ——它没有超时且拒绝主线程调用，两条都会以最难排查的方式发作。

**关键方法实现要点**：

- `snapshot()`：一次 `run_on_main` 读齐全部字段（忙碌态、待决盒是否存在与其 `kind`、
  会话面板态、面板 `display`、`app.focused` 的类型、面板选项原始文本、
  记录最后一条可解析事件的 `seq`）。实测忙碌期该调用耗时 **0.21–0.36 ms**，
  N4 的一秒上限余量三个数量级。
- `send(text)`：锁内校验 `state == IDLE`（否则 `busy`）与轮次预算（否则 `turn_budget`）、
  记 `_last_action`；**出锁后**投递提交协程：
  ```python
  async def _submit() -> None:
      bar = app.query_one(InputBar)
      bar.focus()                 # 不可省：上一次交互结束后焦点未必在输入框
      bar.value = text            # InputBar 没有 set_value，value 是 reactive，直接赋值
      await pilot.press("enter")  # 必须 await；只设 value 不会触发提交
  run_on_main(loop, _submit(), timeout=...)
  ```
  **必须走 `pilot.press("enter")`**，即真人提交入口（F2），不得直接调
  `dispatcher.dispatch`。实测：只设 `value` 不按回车，文本躺在输入框里、
  `stream_chat` 调用数保持 0；把 `pilot.press` 塞进 lambda 元组则只会造出一个
  从未被 await 的协程对象、驱动器返回 ok 而什么也没发生——**这是本设施最不能有的
  失败形态**（N7）。
- `wait(timeout)`：循环 `snapshot()`，命中 `IDLE` 或 `PENDING` 即返回。
  轮询间隔从 30 ms 起、退避到 100 ms 封顶（够灵敏，长等待时也不至于打出几万次调度）。
- `answer(choice, via)`：不变量③ 复核（不满足则短暂重试至上限）→ 按 `via` 分派：
  - `channel`：三类走 `run_on_main(app._resolve_interaction(结算值, responder.source))`；
    会话面板走 `app._settle_session(session_id_or_None, responder.source)`；
  - `keys`：投递 `pilot.press(...)` 序列（上下键移动 + 回车），来源保持默认 `human`。
- `cancel()`：锁外投递 `manager.request_cancel`。
- `shutdown_on_main(reason)`：**只能在主线程调用**，内部**不使用** `run_on_main`
  （从主线程调 `call_from_thread` 或 `run_coroutine_threadsafe(...).result()` 都会死
  ——前者直接抛 RuntimeError，后者自己等自己）。直接同步调用界面方法即可。见 §3.8。

### 3.8 `tests/e2e/host.py` —— 宿主进程

**命令行**：
```
python -m tests.e2e.host --mode scripted|live
                         [--script MOD:ATTR] [--seed MOD:FUNC]
                         [--idle-timeout S] [--max-turns N]
                         [--config P] [--keep-workspace]
```
**没有 `--workspace` / `--user-dir`**（M6）：工作区与用户级目录一律由宿主自建，
否则 §3.3 的可丢弃校验要么必然失败、要么形同虚设。事后要读产物就加 `--keep-workspace`，
路径在发布文件与 `status` 响应里。

- `--mode scripted`：用 `--script` 指定的脚本构造 `ScriptedProvider`；
  装配后立刻 `manager.memory_manager.notes_enabled = False`
  （F16 裁决：确定性形态关自动笔记，它本身就是不确定性来源。
  实测该属性是普通实例属性、门控点每次调用现读，赋值即生效：
  关掉时一轮对话模型被调 1 次，不关是 2 次）。
- `--mode live`：真实配置构造真实 Provider；自动笔记**保留**，退出前等待收敛。
  无凭据时**明确报错退出**，不静默降级（F23）。

**启动顺序（M5 的修正，顺序不可调）**：
```
1. 建发布目录（建不了直接非零退出）
2. 建临时工作区与临时用户目录 → assert_disposable → os.chdir 进工作区
3. 跑 --seed 预置（F15：预置在校验之后、装配之前）
4. bind + listen + publish + 起 accept 线程     ← 先于装配
     此时 DriverCore 尚不存在，一切指令回 {"code": "starting"}
5. asyncio.run(_serve())
     └─ 装配 build_app(...)
          失败 → 进 fatal 态，error.message = BootstrapError.args[0]（P0 已成文），
                 继续服务一个有界的宽限窗口让客户端读到，然后以退出码 1 终止
     └─ async with app.run_test(size=(120, 40)) as pilot:
            DriverCore 就绪 → state 从 starting 转 idle
            await stop_event.wait()
            await core.shutdown_on_main(reason)      ← 在主线程，见 M3
6. cleanup(reason) → os.chdir 回去 → 按需 rmtree → unpublish
```
为什么 socket 要先于装配：spec AC5 点名要验「**装配期**致命错误经通道回报」。
若先装配再监听，装配失败就永远等不到监听，AC5 无法通过。
socket 与装配零依赖，顺序可自由调整（实测确认）。

**`stop_event` 的置位**（S15）：`asyncio.Event` 非线程安全，socket 线程必须走
`loop.call_soon_threadsafe(stop_event.set)`。裸 `set()` 实测也能退出，
但那是靠 Textual 的定时器恰好把循环叫醒，属侥幸，不可依赖。

**退出流程**（F14；step1 与 step2 **交织**，S4）：
```python
async def shutdown_on_main(self, reason):
    deadline = time.monotonic() + LIMIT
    while time.monotonic() < deadline:
        if app._pending_interaction is not None:
            app._resolve_interaction(安全默认值(kind), "driver_forced")
        elif app._session_panel_active:
            app._settle_session(None, "driver_forced")
        elif not app._stream_active:
            break
        await asyncio.sleep(0.03)
    if live_mode:
        等待名为 "rhine-notes" 的线程 join（带超时）
    app.exit()
```
**不做强制结算，进程永远退不出去**——实测：`quit` 时挂着确认面板，
`HOST_EXITED_CLEAN` 日志已打出、进程仍活着，40 秒后被外部超时杀掉。
挂死点在 `asyncio.run()` 收尾的 `shutdown_default_executor`：它去 join 那个阻塞在
`box["event"].wait()` 的工作线程，而 **Python 3.11 的该方法没有超时参数、永不返回**
（3.12 才加了 5 分钟默认超时）。加上强制结算后同一场景 0.1 秒干净退出。
**为什么两步要交织**：结算一个面板后循环可能立刻弹下一个（模型一轮发多个工具调用就是
这样），分成「先结算 N 次、再等忙碌态转假」会在第二段又挂住。

**自动笔记的收敛**（S 项已验）：线程名 `rhine-notes`、`daemon=True`，
用 `threading.enumerate()` 按名字找并 `join(timeout)`，**零产品改动**。
顺序天然安全：笔记钩子在产出结束事件**之前**触发，而忙碌态在事件流耗尽后才转假，
故「先等忙碌态转假、再 join 该线程」不存在「线程还没起就以为收敛了」的竞态。
它是 daemon，不 join 就会被进程退出截断——这一步是必需的。

**空闲超时**：watchdog 线程每秒比对 `now - last_command_at`，超阈值置位 `stop_event`
（默认 1800 秒，`--idle-timeout` 可调；AC7 用极短值验）。

**轮次预算的口径**（S6，必须写清否则会被误读为硬顶）：
预算是**跨 `send` 累计**的，计数取自 `recorder.turn_total()`
（含 `main` / `isolated:*` / `summary` / `notes` 四种作用域——这正是要的，
子对话和摘要也花钱）。它在 `send` 的**前置检查**处生效，
**挡不住单次 send 内的循环**；单次的上界由产品既有的迭代上限（25 轮）兜底。
故最坏烧掉 `budget + 25` 轮。这是刻意接受的口径，不为它增加第四处产品改动。

### 3.9 `tests/e2e/client.py` —— 瘦客户端
**职责**：找到宿主 → 发一条指令 → 打印响应。**无状态、无重试**（N7）。
```
python -m tests.e2e.client status [--pid N] [--json]
python -m tests.e2e.client send "/skills"
python -m tests.e2e.client wait --timeout 180
python -m tests.e2e.client answer once [--via keys]
python -m tests.e2e.client observe --since 42 --types tool_execute
python -m tests.e2e.client cancel
python -m tests.e2e.client quit
python -m tests.e2e.client hosts                # 列出发布文件（排障用）
```
**三种失败要翻译成人话**（S11/S12 与失败模式补漏）：
- 连不上 → 「宿主已不在（发布文件 X，pid N）」+ 清理提示，立即返回；
- `pid` 不一致 → 「端口已被其它进程占用，发布文件疑似陈旧」；
- **读到 EOF（空响应）** → 「宿主在处理本指令期间退出了」，
  而不是抛 `JSONDecodeError`。`wait` 期间宿主退出正是这个形态。

`client.py` **刻意不 import `rhinecode`**：宿主挂掉时它仍要能起来报错，
少一层导入少一处失败面。

### 3.10 产品侧三处改动

| 文件 | 改动 | 缺省行为 |
| --- | --- | --- |
| `rhinecode/bootstrap.py` | `build_app` 增 `provider_factory: Optional[Callable[[Config], BaseProvider]] = None`、`exclude_tools: frozenset[str] = frozenset()`；**并把 `provider_factory` 透传给 `ConversationManager`**（S9——`_provider_for` 在协调层，不透传 F21 不成立） | 均等于现状 |
| `rhinecode/conversation.py` | 构造函数增 `provider_factory=None`；`_provider_for` 改用它 | 为 None 时取 `create_provider`，逐字等于现状 |
| `rhinecode/tui/app.py` | `_interact` 的待决盒增 `"source": "human"`、埋点改读盒子；`_resolve_interaction(result, source="human")` 写入盒子；会话面板两处结算收拢为 `_settle_session(session_id_or_None, source="human")` | 全走默认值时行为与现状逐字一致 |

**`exclude_tools` 的位置**（S1，实测定的）：放在 `skill_manager.startup(known_tools)`
**之后**、`connect_all` 之前，逐个 `unregister`。

- 为什么不放在 `known_tools` 计算之前：实测那样会让一个**在真实启动下完全合法**的
  工作区起不来——白名单写了 `mcp_add_server` 的 Skill 会被判成笔误而 fail-fast
  （`fatals = [('addmcp', 'mcp_add_server')]`）。放在 `startup` 之后则白名单校验口径与
  真实启动**逐字一致**，被摘掉的名字由 Skill 的运行期工具交集自然剔除
  （实测 `tool_policy.allowed = ['read_file']`），模型照样调不到。
- 位置仍在 `session_start` 快照之前，AC19 的两条断言（工具清单不含它、快照与之一致）
  都成立。
- 三个内置样板（commit / review / test）都没写这两个名字，默认路径不受影响。

**摘除哪两个、为什么**：`mcp_add_server` 会**写真实用户主目录**且不吃 `user_dir`
（F8 第二条）；`mcp_resolve_server` 是 `read_only=True` 却会访问外部包索引，
而只读且被放行的工具**根本不弹面板**、应答者拦不住它（F19）。

**`_settle_session` 的形状**（S2，必须带幂等守卫）：
```python
def _settle_session(self, session_id: Optional[str], source: str = "human") -> None:
    if not self._session_panel_active:   # 幂等守卫：无面板时静默返回
        return                            # 缺了它，退出时的强制结算会凭空多埋一条
    self._recorder.emit(TraceEventType.INTERACTION, kind="session",
                        display=str(session_id) if session_id is not None else "",
                        source=source,
                        result="selected" if session_id is not None else "cancelled")
    self._close_session_panel()
    if session_id is not None:
        self.resume_session(session_id)
```
`on_option_list_option_selected` 的 SessionPanel 分支与 `on_session_panel_cancelled`
各塌缩成一行调用；前者仍须保留 `event.stop()` 与那条「必须放在 `box is None` 守卫
**之前**」的注释。

**来源字段取值集合**：`human`（面板按键路径，含模拟按键）/ `driver`（控制通道决策）/
`driver_forced`（退出时的强制结算，仍属外部驱动者，单列以便审计）/ `policy`（P1b 预留）。
spec F5 禁止的是给「脚本预设应答」单开取值，`driver_forced` 不在此列。

**`status.trace_seq` 的来源**（S5）：取自 `observe` 读到的记录**末条可解析事件**的 `seq`，
**不新增产品接口**。记录器的序号是私有的，为它加一个只读访问器会让产品改动从三处变四处，
不值得——而末条 seq 与内部计数在「成功落盘才推进序号」的语义下本就等价（P0 F1）。

**零回归依据**（已核实）：`build_app` 的 14 处既有调用点（`__main__` 1 处 +
`test_bootstrap.py` 11 处 + `test_trace_zero_regression.py` 2 处）全部使用关键字参数，
新参数在 `*` 之后不会撞位；`_resolve_interaction` 在测试里无调用方，
唯一相关的 `test_skill_tui.py` 是直接给 `_pending_interaction` 赋值，不受签名变化影响。

---

## 4. 模块交互

### 4.1 一次完整闭环（AC1 的时序）

```
client: send "/commit 修复登录超时"
  └─ handler 线程 → DriverCore.send
       ├─ 锁内：校验 IDLE + 预算、记 _last_action           （纯内存）
       └─ 锁外：run_on_main(_submit())                     （带超时）
            └─ 主线程：bar.focus(); bar.value=text; await pilot.press("enter")
                 └─ on_input_bar_input_submitted → dispatcher.dispatch
                      └─ Worker 线程启动 Agent 循环
client: wait --timeout 180
  └─ handler 线程（不持锁）轮询 snapshot，30ms→100ms 退避
       └─ 循环命中需确认的工具 → Worker 线程 _interact 置位待决盒
            → call_from_thread 挂面板 → 面板 display + focus
       └─ snapshot 看到 PENDING 且已展示已聚焦 → {"terminal":"pending"}
client: status                     # 读面板原始文本与四个选项
client: answer once
  └─ DriverCore.answer → 不变量③ 复核
       → run_on_main(app._resolve_interaction(ALLOW, "driver"))
            └─ 主线程写 box["result"] / box["source"]、event.set()
                 └─ Worker 被唤醒 → 埋 interaction 事件（source="driver"）→ 执行工具
client: wait  → {"terminal":"idle"}
client: observe --since 0          # 读这一轮到底发生了什么
client: quit
  └─ handler → loop.call_soon_threadsafe(stop_event.set)
       └─ 主线程：shutdown_on_main → 强制结算+等收敛 → app.exit()
            → cleanup → chdir 回去 → rmtree → unpublish
```

### 4.2 依赖方向（单向，无环）

```
tests/e2e/host.py ──▶ control.py ──▶ protocol.py
        │                 │
        │                 └────▶ rhinecode.tui.app / rhinecode.conversation（只读状态 + 结算）
        ├──▶ sandbox.py / seeding.py / discovery.py / fingerprint.py
        ├──▶ scripted.py ──▶ rhinecode.provider.base
        └──▶ rhinecode.bootstrap.build_app

tests/e2e/client.py ──▶ protocol.py + discovery.py     （不 import 任何 rhinecode 模块）
tests/e2e/assertions.py ──▶ rhinecode.trace.reader     （F24：复用，不自行解析）

rhinecode/** ─X─▶ tests/**                             （产品绝不反向依赖）
```
未触碰 `rhinecode/tools/__init__.py`，不引入新的包级环。

---

## 5. 文件组织

```
tests/
├── __init__.py                （新建，**必须留空**；使 python -m tests.e2e.* 可用。
│                               已实测：加它之后 714 条测试仍全绿）
├── e2e/
│   ├── __init__.py            （**必须留空**：加了 tests/__init__.py 后 unittest
│   │                            discover 会导入本包，放重量级导入会给全量测试
│   │                            平白加上装配开销甚至副作用）
│   ├── protocol.py            线上格式、错误码、决策取值表、via 取值（纯数据）
│   ├── discovery.py           发布文件读写、扫描、两级陈旧判定
│   ├── sandbox.py             临时目录创建 + 可丢弃校验 + 三步清理
│   ├── seeding.py             F15 预置（文件 / git / Skill / 权限 / 项目指令）
│   ├── fingerprint.py         代码版本标识
│   ├── scripted.py            ScriptedProvider + RecordedCall + 数据块构造器
│   ├── control.py             run_on_main、Responder、DriverCore、四条不变量
│   ├── host.py                宿主进程入口 + ControlServer + 退出编排
│   ├── client.py              瘦客户端入口
│   └── assertions.py          TraceView + 十一项词汇 + 失败诊断
├── test_e2e_protocol.py       编解码、错误码、取值表、UTF-8 往返
├── test_e2e_discovery.py      发布文件、多宿主、两级陈旧判定
├── test_e2e_sandbox_seed.py   可丢弃校验两面、清理三步顺序、预置生效
├── test_e2e_scripted.py       四类块、按轮次、耗尽兜底、RecordedCall 取值
├── test_e2e_assertions.py     十一项词汇各一次、失败诊断含序号、复用 reader
├── test_e2e_control.py        状态判据、等待两终态、四类面板应答（含 via=keys）、
│                              取消、超时诊断、跨线程死锁护栏
├── test_e2e_host.py           进程内起宿主：闭环序列、待决时退出、空闲超时、
│                              装配期致命错误经通道回报、连跑不污染
└── test_e2e_live.py           真实模式（默认 skip，须显式开启）

rhinecode/
├── bootstrap.py               （改：两个可选参数 + 透传 provider_factory）
├── conversation.py            （改：构造函数增 provider_factory，_provider_for 用它）
└── tui/app.py                 （改：交互结算点的来源字段可传入 + _settle_session）
```

**测试用例的兜底清理**（S14）：`test_e2e_host.py` 的每个用例用 `addCleanup` 注册
「强杀宿主 + `unpublish` + `chdir` 回去 + `rmtree`」，且该清理对「宿主已自行退出」
必须幂等。断言中途失败时若不兜底，会留下进程、临时目录与发布文件，AC42 直接红。

---

## 6. 技术决策

| 决策点 | 选择 | 理由 |
| --- | --- | --- |
| 进程间通信 | 本机回环 TCP + 行分隔 JSON（显式 UTF-8） | 唯一能天然支持**阻塞等待**的朴素方案；文件轮询只能空转。不引入依赖 |
| 连接模型 | 一条指令一次连接，每连接一 handler 线程 | `wait` 阻塞时 `status` 仍要能答（N4）。串行处理会让二者互斥 |
| 启动顺序 | **socket 先于装配** | 装配期致命错误必须能经通道回报（AC5）；先装配再监听则失败时永远等不到监听 |
| 跨线程投递 | 自建 `run_on_main`（`run_coroutine_threadsafe` + `Future.result(timeout)`） | **实测**：`call_from_thread` 无超时参数、阻塞到工作跑完（主线程被堵 8 秒时调用方等满 7.8 秒）、且拒绝主线程调用。「先调用再 `queue.get(timeout=)`」完全无效 |
| 超时语义 | 超时只让调用方脱身，不取消已排队工作 | N7「失败即失败」：不能假装成功，也不能重试掩盖 |
| 加锁 | 四段式，跨线程调度一律在锁外 | 持锁跨线程 → 主线程堵住 → 锁被无限占 → **最需要取消时取消不了**。同 C11 SkillManager 教训 |
| 提交输入 | `bar.focus(); bar.value=…; await pilot.press("enter")` | **实测**：`InputBar` 无 `set_value`；只设 value 不触发提交；`pilot.press` 是协程，塞进 lambda 只会造出未 await 的协程对象、驱动器返回 ok 而什么都没发生 |
| 退出时的强制结算 | 与「等忙碌态转假」交织在同一个带上限的循环里 | **实测**：不结算则 `asyncio.run` 收尾去 join 阻塞的工作线程，Python 3.11 的 `shutdown_default_executor` **无超时、永不返回**；分两段则结算后新弹的面板会在第二段再次挂住 |
| `shutdown` 的调用位置 | 只在主线程同步调用界面方法，不经 `run_on_main` | **实测**：从主线程调 `call_from_thread` 直接抛 RuntimeError；`run_coroutine_threadsafe(...).result()` 则是自己等自己 |
| 发布文件位置 | 系统临时目录下固定子目录，文件名带 pid | 与工作区无关，破解「要先找到工作区才能找到端口」的死循环；带 pid 支持多宿主并存 |
| 陈旧判定 | 连不上即陈旧 + 响应 pid 校验 | 端口只在宿主活着时被监听；pid 校验补上「端口被别的进程复用」这个漏洞 |
| 工作区来源 | 一律宿主自建，**不提供 `--workspace`** | 外部传入的路径永远过不了「可丢弃」校验，否则校验形同虚设 |
| 可丢弃判据 | 位于系统临时目录之下 **且** 含本设施的标记文件 | 宿主与测试可能不是同一进程，「本进程创建过的集合」不通用；与「目录是否为空」解耦 |
| 清理顺序 | `cleanup()` → `chdir` 回去 → `rmtree` | **实测**：Windows 下 cwd 在待删目录内时 `rmtree` 抛 WinError 32；记录文件句柄由 `cleanup` 关闭 |
| 模型注入 | `build_app` 与 `ConversationManager` 各一个 `provider_factory`，由前者透传 | 一个接缝同时覆盖主 Provider 与换模型旁路（F20/F21），Provider 抽象一行不动 |
| 危险工具处理 | 装配时 `exclude_tools` 摘除，位置在 `startup` **之后** | 真实模式下调什么工具由模型决定，唯一闸门是可能误判的应答者，摘掉是唯一硬保证；放在 `startup` 之前会让合法工作区 fail-fast（实测） |
| 自动笔记 | scripted 关、live 保留并 join `rhine-notes` 线程 | 它是不确定性来源，与「可复现」冲突；但也是被验收对象，live 下不能关。按线程名等待，零产品改动 |
| 轮次预算 | 跨 send 累计，`send` 前置检查；单次上界靠产品既有 25 轮兜底 | 不为它增加第四处产品改动；最坏 `budget + 25`，口径写明避免被当成硬顶 |
| `trace_seq` 来源 | 记录末条可解析事件的 `seq` | 不新增产品接口；「成功落盘才推进序号」使二者等价 |
| 代码版本标识 | 路径+大小+mtime 的哈希 | 快；误报（多重启一次）比漏报（以为改生效了）安全。代价已在 §3.5 写明 |
| 面板文本口径 | 只给原始字符串（含 markup 标记），不给渲染文本 | 渲染态由记录的 `ui_message` / `status_bar` 承载，P0 已确立口径，不另起一套 |
| 断言层读记录 | 复用 `trace.reader.load_records` / `filter_records` / `summarize` | F24 硬要求。两边各写一份解析必然漂移 |
| 词汇①⑩⑪ 的来源 | 假模型留存的原始参数，而非记录 | 记录里 system 与 messages 都受截断；且 SOP 在**动态提醒**里不在 `system` 参数里 |
| 词汇⑨ 的来源 | 会话存档 JSONL | 十五类事件没有一类承载「当前主历史多少条」 |
| 测试包位置 | `tests/e2e/` + 新建空的 `tests/__init__.py` | 已实测：加它之后 714 条仍全绿；使 `python -m tests.e2e.client` 可用 |
| 客户端不 import 产品 | 刻意 | 宿主挂掉时客户端仍要能起来报错，少一层导入少一处失败面 |

---

## 7. 对 spec 需求的覆盖对照

### 功能需求

| Spec | 归属 |
| --- | --- |
| F1 运行内核 | `control.DriverCore` + `host` 的启动/退出编排 |
| F2 输入走真人入口 | `DriverCore.send`（`pilot.press("enter")`） |
| F3 三态判据 | `DriverCore.snapshot` |
| F4 应答时机 | 不变量③（`display` + `focused` 类型双条件，`kind` 取自待决盒） |
| F5 来源三态记录 | 产品侧 §3.10 三处 + 四取值集合 |
| F6 应答者接缝 | §2.5 `Responder` 协议 + `ExternalResponder`；P1b 换实现即可 |
| F7 取消与诊断超时 | `DriverCore.cancel` / `wait` 的超时负载 |
| F8 沙箱校验 + 工具排除 | `sandbox.assert_disposable` + `build_app(exclude_tools=…)` |
| F9 宿主（指纹、不热重载） | `host` + `fingerprint` |
| F10 控制通道 | `host.ControlServer` + `discovery` 两级陈旧判定 |
| F11 指令集 | `protocol` + `DriverCore` 七个方法 |
| F12 等待两终态 | `DriverCore.wait` |
| F13 观察面基于记录 | `DriverCore.observe`（读记录文件，不遍历控件；末行半截丢弃） |
| F14 退出与强制结算 | `shutdown_on_main` 的交织循环 + `cleanup` |
| F15 预置 | `seeding`，在校验之后、装配之前执行 |
| F16 独立工作区 + 收敛裁决 | `sandbox` + `host` 的 mode 分支（scripted 关笔记 / live 等收敛） |
| F17 独立用户级目录 | `host` 自建临时目录并传 `build_app(user_dir=…)` |
| F18 连跑不污染 | P0 的 `cleanup` 复位 + `sandbox` 的三步清理 |
| F19 确定性形态无外部连接 | 假模型 + 空 MCP 配置 + `exclude_tools` 摘掉联网只读工具 |
| F20/F21 模型注入与旁路 | `provider_factory` 两处 + `build_app` 透传 |
| F22 脚本化假模型 | `scripted` |
| F23 真实模式 | `host --mode live`，无凭据明确报错 |
| F24 事实来源与查询 | `assertions.TraceView` |
| F25 十一项词汇 | `assertions` 的十一个 check 函数 |
| F26 失败可诊断 | `assert_check` 附证据序号与摘要行 |

### 非功能需求

| Spec | 归属 |
| --- | --- |
| N1 产品行为零变化 | §3.10 的缺省值表 + 零回归依据（14 处调用点已核对） |
| N2 驱动设施不进产品包 | §4.2 依赖方向；`client.py` 不 import 产品 |
| N3 确定性形态零外部依赖 | 假模型 + 空 MCP + `exclude_tools`；git 作为**本地环境前置**登记（§3.4） |
| N4 查询一秒内返回 | 不变量②（`wait` 不持锁）+ 连接模型（每连接一线程）；实测 0.21–0.36 ms |
| N5 轮次预算与缓存 | §3.8 的预算口径 + `turn_budget` 错误码；前缀缓存随重启波动已在 spec 记为已知现象 |
| N6 不引入交叉锁、不死锁 | 不变量①③④ + `scripted.calls` 的独立追加锁 + `test_e2e_control.py` 的跨线程护栏 |
| N7 失败即失败 | 超时语义（不取消、不重试）+ 客户端无重试 + `assert_check` 不降级；**禁止任何形式的重试与「跑不通就放宽断言」** |
| N8 中文注释 | 全部新增/修改代码按项目注释规范；四条不变量与三处实测教训必须写进代码注释 |
| N9 编码与 markup | 通道显式 UTF-8（§2.1）；`seeding` 显式 UTF-8；面板文本给原始字符串、渲染态由记录承载（§2.1 markup 口径） |

---

## 8. 已登记的环境前置与已知代价

| 项 | 说明 |
| --- | --- |
| **git 可执行** | `seed_git_repo` 依赖它，缺失时明确抛错而非静默跳过（假绿更糟）。属本地程序、非网络依赖，不违反 N3 |
| **指纹误报** | mtime 变化即报「代码变了」，切分支/`pip install -e .` 都会触发。见 §3.5 的取舍论证 |
| **轮次预算非硬顶** | 最坏 `budget + 25` 轮，见 §3.8 |
| **宿主强杀后的残留** | 临时工作区与发布文件由使用者清理（spec「不做的事」第 13 条）；测试内由 `addCleanup` 兜底 |
| **`observe` 全量重读** | 长会话下每次观察重读整个记录文件。当前规模不触及 N4 的一秒上限，若成为瓶颈再加文件偏移缓存 |
