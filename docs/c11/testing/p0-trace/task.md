# Trace 记录器 Tasks

> 依据已批准的 `spec.md`（F1–F30 / AC1–AC39）与 `plan.md`。
> 按 plan 的「实现分段」切成三段，**每段独立验收、独立提交**。
> 定位一律用符号名（行号会过期）。

---

## 文件清单

| 操作 | 文件 | 职责 |
| --- | --- | --- |
| 新建 | `rhinecode/trace/__init__.py` | 只导出 models 与 recorder 的公开名 |
| 新建 | `rhinecode/trace/models.py` | 枚举、常量、作用域、四个纯函数 |
| 新建 | `rhinecode/trace/recorder.py` | TraceRecorder + NullRecorder + Protocol |
| 新建 | `rhinecode/trace/tracing_provider.py` | TracingProvider 装饰器 |
| 新建 | `rhinecode/trace/reader.py` | CLI 阅读器 |
| 新建 | `rhinecode/bootstrap.py` | build_app / BuildResult / BootstrapError |
| 改 | `rhinecode/__main__.py` | 瘦身；新增 `--trace` |
| 改 | `rhinecode/conversation.py` | user_dir / recorder 参数；三处埋点 |
| 改 | `rhinecode/agent/loop.py` | recorder 参数；权限与八处工具埋点 |
| 改 | `rhinecode/permission/config.py` | user_config_path / load_all 加 user_dir |
| 改 | `rhinecode/permission/engine.py` | load 加 user_dir |
| 改 | `rhinecode/mcp/config.py` | user_config_path / load_all 加 user_dir |
| 改 | `rhinecode/context/offload.py` | 暴露「调用标识 → 落盘路径」明细 |
| 改 | `rhinecode/context/manager.py` | recorder；summary 作用域；压缩事件 |
| 改 | `rhinecode/memory/manager.py` | recorder；记忆线程作用域 |
| 改 | `rhinecode/skills/manager.py` | recorder；skill_state（锁外） |
| 改 | `rhinecode/commands/dispatcher.py` | user_input / command_dispatch |
| 改 | `rhinecode/tui/app.py` | recorder；四类界面埋点；作用域复位 |
| 改 | `rhinecode/tools/path_guard.py` | `clear_read_roots` docstring 提升 |
| 改 | `tests/test_command_startup.py` | patch 目标迁到 bootstrap；替身吃 kwargs |
| 改 | `tests/test_skill_startup.py` | 同上 |
| 新建 | `tests/test_trace_models.py` | 纯函数 |
| 新建 | `tests/test_trace_recorder.py` | 序号、并发、fail-safe、死锁护栏 |
| 新建 | `tests/test_trace_provider.py` | 请求响应成对、break 结算、轮次计数 |
| 新建 | `tests/test_bootstrap.py` | 装配、清理、异常、隔离、子进程 |
| 新建 | `tests/test_trace_hooks.py` | 十五类事件、八处工具路径、作用域 |
| 新建 | `tests/test_trace_zero_regression.py` | 黄金基线、双跑、未包装、不产文件 |
| 新建 | `tests/test_trace_reader.py` | 摘要、过滤、展开、只读 |
| 改 | `.gitignore` | 加 `**/.rhinecode/traces/` |
| 改 | `CLAUDE.md` / `AGENTS.md` / `README.md` | 架构节 + 三条成对维护点 |

> **`pyproject.toml` 不需要改**：`[tool.setuptools.packages.find] include = ["rhinecode*"]`
> 的 fnmatch 语义会自动收进 `rhinecode.trace`；`trace/` 下全是 `.py`，
> 无需 `package-data`（那段只为 `skills/builtin/*.md` 存在）。

---

# 第一段：trace 核心

> 不碰任何既有模块。本段完成后 AC5–AC8、AC17、AC20、AC22 可验。

## T1: 事件类型枚举与作用域常量

**文件：** `rhinecode/trace/models.py`（新建）
**依赖：** 无
**步骤：**
1. 模块 docstring 说明：本模块是 trace 层最底层，只依赖标准库，不感知任何上层。
2. 定义 `TraceEventType(str, Enum)`，十五个成员，取值为成员名小写：
   `SESSION_START` / `SESSION_END` / `USER_INPUT` / `COMMAND_DISPATCH` / `API_REQUEST` /
   `API_RESPONSE` / `PERMISSION_DECISION` / `INTERACTION` / `TOOL_EXECUTE` / `UI_MESSAGE` /
   `STATUS_BAR` / `AGENT_EVENT` / `CONTEXT_COMPACTION` / `SKILL_STATE` / `HISTORY_RESTORED`。
   每个成员行尾注释标注对应 spec 的哪条 F。
3. 定义 `SCOPE_MAIN = "main"` / `SCOPE_SUMMARY = "summary"` / `SCOPE_MEMORY = "memory"`，
   注释说明为什么需要后两个：`ContextManager` 与 `MemoryManager` 持有同一个 Provider 实例，
   不区分会污染主对话的轮次计数；记忆还跑在独立线程、可能与用户下一条消息并发。
4. 定义 `isolated_scope(name: str) -> str` 返回 `f"isolated:{name}"`。

**验证：** `python -c "from rhinecode.trace.models import TraceEventType as T; print(len(list(T)))"`
输出 `15`。

## T2: 截断、脱敏与默认路径

**文件：** `rhinecode/trace/models.py`
**依赖：** T1
**步骤：**
1. 常量 `MAX_FIELD_CHARS = 4000`、`MAX_MESSAGE_ITEMS = 400`、`REDACTED = "***REDACTED***"`。
2. `clip(value, limit=MAX_FIELD_CHARS)`：非字符串先 `str()`；不超限返回**原字符串**；
   超限返回 `{"text": 前 limit 个字符, "truncated": True, "original_length": 原长}`。
   docstring 写清「未截断是裸字符串、截断才变对象」的约定，以及它是全项目唯一截断入口。
3. `redact_config(cfg) -> dict`：用 `getattr(cfg, name, None)` 逐字段取值（容忍字段增减），
   `api_key` 一律换成 `REDACTED`，其余原样。
4. `default_trace_path(project_root: Path) -> Path`：返回
   `project_root / ".rhinecode" / "traces" / f"{stamp}.jsonl"`，
   **`stamp` 必须含毫秒**——形如 `f"{now:%Y%m%d-%H%M%S}-{now.microsecond // 1000:03d}"`。
   注释写清理由：AC25 要求「连续两次运行产出两个文件」，秒级时间戳在同秒内会返回同一路径，
   而记录器用追加模式打开，结果是一个文件里两段记录。**不创建目录**（建目录是 recorder 的事）。

**验证：** `python -c "from rhinecode.trace.models import clip; print(clip('ab',5)); print(clip('a'*10,5))"`
第一行 `ab`，第二行含 `truncated` 与 `original_length: 10`。

## T3: 循环事件字段白名单

**文件：** `rhinecode/trace/models.py`
**依赖：** T2
**步骤：**
1. `agent_event_payload(event) -> dict`。全部用 `getattr(event, x, None)` 取值，
   **不导入 agent 包**（保持叶子性）。
2. 白名单：`event_type`（`event.type` 的字符串值）、`iteration`、`stop_reason`、`message`、
   `tool_call_id`（从 `event.tool_call.id`）、`tool_name`（从 `event.tool_call.name`）、
   `result_ok`（从 `event.tool_result.ok`）、`text_length`（`len(event.text)`，**仅长度**）。
3. 值为 None 的键不写入返回字典。
4. docstring 明写**刻意排除**：`tool_call.arguments`、`tool_result.output`、`text` 正文本身
   ——它们各有专属事件承载（`tool_execute` / `api_response`），重复携带违反 spec F17 与 AC20。

**验证：** 构造带 `type`/`text`/`tool_call` 的假事件对象调用它，断言有 `text_length` 无 `text`、
有 `tool_name` 无 `arguments`。

## T4: TraceRecorder 的落盘与序号

**文件：** `rhinecode/trace/recorder.py`（新建）
**依赖：** T2
**步骤：**
1. `TraceRecorder.__init__(path: Path)`：`path.parent.mkdir(parents=True, exist_ok=True)`；
   `open(path, "a", encoding="utf-8")`；`self._seq = 0`；`self._lock = threading.Lock()`；
   `self._start = time.monotonic()`；`self._turns: dict[str, int] = {}`；`self._closed = False`。
   类属性 `enabled = True`。
2. `emit(self, type, **payload)`：**整体包在 `try/except Exception: pass` 内**
   （spec F4/N3——只读并发桶里抛异常会被 `future.result()` 当成工具执行异常回灌模型），
   内部调私有 `_write(type, payload)`。
3. `_write(type, payload)`：**在 `self._lock` 临界区内按固定顺序做四件事**——
   ① `n = self._seq + 1`；② 组装 `{"seq": n, "ts": ..., "type": ..., "scope": ..., **payload}`
   并 `json.dumps(..., ensure_ascii=False)`；③ 写入并 `flush()`；④ `self._seq = n`。
   注释必须写清两点：
   - **序列化必须在锁内**——`seq` 是 JSON 首字段，锁外组装等于锁外读计数，
     并发下两个线程会各写一行同号，违反 AC8 的「无重复」；
   - **序号只在 flush 成功后推进**——使 F4 的静默丢弃与 AC8 的无跳号相容。

   临界区禁令注释为「不得做落盘之外的阻塞操作、不得触发任何回调、不得跨线程调度」。
4. `ts` 用 `datetime.now().isoformat(timespec="milliseconds")`。
5. `close()`：幂等（`_closed` 守卫），关句柄，异常吞掉。

**验证：** 临时目录建 recorder，`emit` 三次后读文件：三行、每行可 `json.loads`、`seq` 为 1/2/3。

## T5: 作用域与轮次计数

**文件：** `rhinecode/trace/recorder.py`
**依赖：** T4
**步骤：**
1. 模块级 `_scope_state = threading.local()`。`current_scope()` 返回
   `getattr(_scope_state, "name", SCOPE_MAIN)`——**未设置时回退主作用域**
   （spec F2：不隶属任何对话的事件归主对话）。读取**不取锁**。
2. `bind_scope(name)`：直接 `_scope_state.name = name`。用于线程入口绑定与复位。
3. `scope(name)`：`@contextmanager`，进入保存旧值并设新值，退出 `finally` 恢复旧值。
4. `next_turn(scope)`：锁内 `self._turns[scope] = self._turns.get(scope, 0) + 1` 并返回新值。
5. `turn_total()`：锁内返回 `sum(self._turns.values())`。
6. `elapsed()`：返回 `time.monotonic() - self._start`。
7. `_write` 的 `scope` 字段取 `self.current_scope()`。

**验证：** `with rec.scope("isolated:x")` 内 `current_scope()` 为 `isolated:x`，退出后回 `main`；
`next_turn("main")` 连调两次返回 1、2，`next_turn("summary")` 返回 1。

## T6: emit_lazy、NullRecorder 与 Protocol

**文件：** `rhinecode/trace/recorder.py`
**依赖：** T5
**步骤：**
1. `emit_lazy(self, type, factory)`：**在同一个 `try/except` 内**先调 `factory()` 再 `_write`。
   docstring 写清存在理由：spec N1 要求昂贵负载先过开关守卫、F4 又要求「负载构造异常」也被吞掉；
   写成 `if enabled: emit(**贵负载)` 会把负载构造留在 `try` 之外，构造抛异常仍会外泄。
   **昂贵埋点一律走本方法，廉价埋点走 emit。**
2. `TraceRecorderProtocol(Protocol)`：声明 `enabled` 与全部方法，供各层类型标注。
3. `NullRecorder`：类属性 `enabled = False`；`emit` / `bind_scope` / `close` 空实现；
   **`emit_lazy` 不调用 factory**（零开销的兑现点）；`scope()` 返回 `contextlib.nullcontext()`；
   `current_scope()` 返回 `SCOPE_MAIN`；`next_turn` / `turn_total` 返回 `0`；`elapsed()` 返回 `0.0`。
4. 类 docstring 说明为什么不做 `TraceRecorder` 的子类：二者无共享实现，
   继承会让 Null 对象持有一个永不使用的文件句柄。
5. **`create_recorder(path: Path) -> TraceRecorderProtocol`**：构造 `TraceRecorder`，
   捕获 `OSError`（含 `PermissionError` / `FileExistsError` / `NotADirectoryError`）
   后**降级返回 `NullRecorder()`**，并往 stderr 打一行「已跳过行为记录：<原因>」。
   docstring 写清为什么需要这个工厂：`TraceRecorder.__init__` 会 `mkdir` + `open`，
   目标路径不可写时**必然抛异常**；而 spec AC22 明确把「目标目录不可写」列为三种
   必须不阻断的失败情形之一。若让调用方直接构造，进程会带 traceback 崩在装配之前——
   用户只是想开个日志，结果程序起不来。
   注意 stderr 那行大概率会被 Textual 的 alternate screen 盖住（与 C11 启动期 print
   的已知限制同源），故**主要可观测后果是「没有产出文件」**，那行只是给重定向 stderr
   的场景兜底。

**验证：** `NullRecorder().emit_lazy(T.API_REQUEST, lambda: 1/0)` 不抛异常（证明 factory 未被调用）；
`create_recorder(Path("CLAUDE.md/x.jsonl"))`（父路径是个文件）返回 `NullRecorder` 且不抛异常。

## T7: 包导出

**文件：** `rhinecode/trace/__init__.py`（新建）
**依赖：** T6
**步骤：**
1. 从 `models` 导出 `TraceEventType`、三个 `SCOPE_*`、`isolated_scope`、`clip`、
   `redact_config`、`agent_event_payload`、`default_trace_path`。
2. 从 `recorder` 导出 `TraceRecorder`、`NullRecorder`、`TraceRecorderProtocol`。
3. **刻意不导出 `tracing_provider`**，注释说明理由：它是 spec N4 允许的唯一分层例外
   （依赖 `provider/base`），不从包级导出可让这条依赖边在调用方代码里保持显式可见。

**验证：** `python -c "import rhinecode.trace as t; print(hasattr(t,'TracingProvider'))"` 输出 `False`。

## T8: TracingProvider 装饰器

**文件：** `rhinecode/trace/tracing_provider.py`（新建）
**依赖：** T7
**步骤：**
1. 模块 docstring 说明这是 spec N4 的唯一分层例外及理由（`provider/base` 是零副作用的纯抽象
   模块，只有 ABC 与数据类）。
2. `TracingProvider(BaseProvider)`，`__init__(inner, recorder, model)`，`inner` 存为**公开属性**
   （供 AC3 断言与调试）。docstring 补一句「**内层 `stream_chat` 必须是生成器**」
   ——下一步的 `contextlib.closing` 要求对象有 `close()`；现有真实 Provider 与全部假 Provider
   都是生成器函数，但将来若有人返回 `iter([...])` 会失效。
3. `stream_chat(messages, thinking_effort="off", tools=None, system=None)`：
   - 取 `scope = recorder.current_scope()`、`turn = recorder.next_turn(scope)`、`t0`；
   - `emit_lazy(API_REQUEST, factory)`：`turn`、`model`、`thinking_effort`、`system`（`clip`）、
     `tool_names`（从 schema 取 `function.name`）、`tools`（完整 schema）、
     `messages`（每条 role/content 经 `clip`、tool_calls 摘要；超 `MAX_MESSAGE_ITEMS` 条则截断
     并记原条数）；
   - `with contextlib.closing(self.inner.stream_chat(...)) as stream:` 内
     `for chunk in stream: yield chunk` **原样转发不改**，旁路累积正文/思考/工具调用/usage/错误；
   - `finally` 中 `emit_lazy(API_RESPONSE, ...)`：`turn`、正文（`clip`）、思考（`clip`）、
     工具调用列表、usage、耗时、`stream_error`。
4. 注释写清为什么必须显式 `closing`：调用方会在收到 error 块时 `break` 跳出 for 循环，
   生成器不会被耗尽；只靠 `try/finally` 在 CPython 下也能靠引用计数即时结算，但那是解释器
   实现细节。显式 `closing` 把「何时结算」变成代码事实，并保证 `api_response` 仍排在后续
   `ERROR`/`FINISHED` 之前。

**验证：** 用「yield 三块后 yield error 块」的假 provider 包装，消费方在 error 处 `break`，
断言 trace 里请求响应各一条且 `api_response` 的 `seq` 更大。

## T9: 纯函数测试

**文件：** `tests/test_trace_models.py`（新建）
**依赖：** T3
**步骤：** 覆盖——十五个枚举成员齐备且取值为小写下划线；`clip` 的未截断返回裸串 /
截断返回三字段 / **中文按字符截断不产生乱码**；`redact_config` 掩码 `api_key` 且保留其余字段
（**AC18 的正向验证在 T46，本条只验纯函数**）；`agent_event_payload` 白名单（有 `text_length`
无 `text`、有 `tool_name` 无 `arguments`、None 键不写入）；`isolated_scope` 格式；
`default_trace_path` 含时间戳、不创建目录、**连续两次调用返回不同路径**（AC25 的前置）。

**验证：** `python -m unittest tests.test_trace_models -v` 全绿。

## T10: 记录器测试（含死锁与并发护栏）

**文件：** `tests/test_trace_recorder.py`（新建）
**依赖：** T6
**步骤：** 覆盖——
1. 序号自增且连续（AC8）；
2. 注入「写入抛异常」与「负载序列化异常」两种失败后，**序号序列仍连续**
   （AC8 + AC22；「目标目录不可写」不产出文件，本条不适用）；
3. `emit` / `emit_lazy` 在三种失败下均不抛异常（AC22）；
4. **并发护栏**（AC7）：起 N 个线程各 emit M 条，断言文件行数为 N×M、每行可独立
   `json.loads`、`seq` 集合恰为 1..N×M（无重复无跳号）；
5. **死锁护栏**（AC5）：另起 daemon 线程在 emit 进行中读 `current_scope()` 与 `turn_total()`，
   用 `join(timeout=…)` 汇合，**用完成计数而非布尔标志**判定；
6. `NullRecorder.emit_lazy` 不调用 factory；
7. 作用域：`scope()` 嵌套与恢复、`bind_scope` 生效、未设置时回退 `main`；
8. **构造降级**（AC22 的第三种情形）：`create_recorder` 传一个父路径是普通文件的路径
   （如 `<tmp>/somefile/x.jsonl`），断言返回 `NullRecorder`、不抛异常、不产生文件。
   注：该情形在 Windows 上抛 `FileExistsError`、在 POSIX 上抛 `NotADirectoryError`，
   故捕获范围必须是 `OSError` 而非某个具体子类。

**验证：** `python -m unittest tests.test_trace_recorder -v` 全绿；死锁护栏在超时内完成。

## T11: 装饰器测试

**文件：** `tests/test_trace_provider.py`（新建）
**依赖：** T8
**步骤：** 覆盖——正常流产出成对请求响应；**消费方 break 后仍产出响应且顺序正确**；
`tool_names` 与传入 schema 一致；轮次号**按作用域各自计数**（主对话与 `summary` 互不影响）；
`inner` 暴露原实例；转发的 chunk 与内层**逐个相等**（证明不改变行为）。

**验证：** `python -m unittest tests.test_trace_provider -v` 全绿。

## T12: 第一段收尾提交

**依赖：** T1–T11
**步骤：** `python -m compileall rhinecode tests`；全量测试确认既有 580 条一条不少；提交
（commit message 说明本段不碰既有模块）。

**验证：** `python -m unittest discover -s tests 2>&1 | tail -3` 显示 `OK`，总数为 580 + 本段新增。

---

# 第二段：装配层

> **本段是唯一会打断既有测试的一段**，单独提交便于回退。完成后 AC28–AC32、AC39 可验。

## T13: 权限配置的用户目录参数

**文件：** `rhinecode/permission/config.py`
**依赖：** 无
**步骤：**
1. `user_config_path(user_dir: Optional[Path] = None)`：给定时返回 `user_dir / 文件名`，
   否则保持现有 `Path.home() / _CONFIG_DIR_NAME / _CONFIG_FILE`。
2. `load_all(user_dir: Optional[Path] = None)`：把 `user_dir` 透传给内部的 `user_config_path()`。
3. 两处 docstring 注明「缺省等于现状，**参数必须可选**」——改成必选会让既有测试
   （全部走 `mock.patch(Path.home)` + 无参调用）成批失败。

**验证：** `python -m unittest tests.test_perm_config -v` 全绿。

## T14: 权限引擎的用户目录参数

**文件：** `rhinecode/permission/engine.py`
**依赖：** T13
**步骤：** `PermissionEngine.load(mode=..., user_dir: Optional[Path] = None)`，
透传给 `config.load_all(user_dir)`；docstring 补参数说明。

**验证：** `python -m unittest tests.test_perm_engine tests.test_perm_config -v` 全绿。

## T15: MCP 配置的用户目录参数

**文件：** `rhinecode/mcp/config.py`
**依赖：** 无
**步骤：** 与 T13 同形：`user_config_path(user_dir=None)`、`load_all(user_dir=None)`。
**`auto_config` 的写入侧不动**——spec F23 只覆盖读取侧，写入侧已在 spec「不做的事」第 3 条登记。

**验证：** `python -m unittest tests.test_mcp_config tests.test_mcp_auto_config -v` 全绿。

## T16: 协调层的用户目录参数

**文件：** `rhinecode/conversation.py`
**依赖：** T14
**步骤：**
1. `ConversationManager.__init__` 新增 `user_dir: Optional[Path] = None` 关键字参数。
2. **在构造函数体的靠前位置**（必须在调用 `PermissionEngine.load` 之前）解析：
   `user_dir = user_dir or (Path.home() / ".rhinecode")`。
3. **删除**函数体后段原有的 `user_dir = Path.home() / ".rhinecode"` 赋值语句——
   它位于 `PermissionEngine.load` 之后，若只改它则权限层拿不到 `user_dir`，
   spec F23 承诺的「权限规则不参与求值」直接落空。
4. 现有的 `PermissionEngine.load()` 调用**当前是无参的**，改为 `PermissionEngine.load(user_dir=user_dir)`
   （若该处已传 mode 则并列传入）。
5. docstring 补参数说明，注明「缺省等于现状」。

**验证：** `python -m unittest tests.test_review_fixes tests.test_resume_replay tests.test_skill_isolated tests.test_skill_sandbox -v`
全绿（这四个文件直接构造 `ConversationManager`）。

## T17: 只读根清理原语转正

**文件：** `rhinecode/tools/path_guard.py`
**依赖：** 无
**步骤：** 把 `clear_read_roots()` 的 docstring 从「仅测试用」改为正式清理原语，
说明它现在也被装配层的清理动作使用（spec F24：使同一进程内连续装配互不污染）。
**行为一行不改。**

**验证：** `python -m unittest tests.test_memory_sandbox tests.test_skill_sandbox -v` 全绿。

## T18: 三个构造函数接收记录器（**只加参数，不埋点**）

**文件：** `rhinecode/skills/manager.py`、`rhinecode/conversation.py`、`rhinecode/tui/app.py`
**依赖：** T7
**步骤：**
1. `SkillManager.__init__`：在 `notify_activation` **之后**新增 `recorder=None` 关键字参数
   （**不能插在既有位置参数之间**，会打断 `SkillManager(root, user_dir, builtin, ...)` 的调用方），
   存 `self._recorder = recorder or NullRecorder()`。
2. `ConversationManager.__init__`：新增 `recorder=None` 关键字参数，存 `self._recorder`。
3. `RhineApp.__init__`：新增 `recorder=None` 关键字参数，存 `self._recorder`。
4. **本任务只加参数与存字段，不做任何埋点、不做任何向下传递**——埋点与传递在第三段。
5. 三处 docstring 都注明「缺省等于现状（Null 对象），不传等于零回归」。

**为什么必须在第二段做**：T22 要让 `build_app` 给这三个构造函数传 `recorder=`。
若把加参数的活留到第三段，第二段收尾时每次启动都会
`TypeError: unexpected keyword argument 'recorder'`，T28 的「既有 580 条一条不少」不可能达成。

**验证：** `python -m unittest tests.test_skill_manager tests.test_command_tui tests.test_review_fixes -v` 全绿。

## T19: 装配异常与结果类型

**文件：** `rhinecode/bootstrap.py`（新建）
**依赖：** 无
**步骤：**
1. `BootstrapError(Exception)`：docstring 明写 `args[0]` 是**已成文的完整 stderr 文案**，
   调用方原样打印、不再拼前缀。
2. 三段文案登记为**不可变契约**（既有启动测试断言的正是它们），写成注释放在异常类下方：
   `f"命令注册冲突：{e}"`、`f"Provider 初始化错误：{e}"`、`format_fatal_message(...)` 的返回值。
3. `BuildResult` 冻结数据类，字段：`app` / `cleanup` / `manager` / `tool_registry` /
   `command_registry` / `skill_manager` / `mcp_manager` / `recorder`。
   docstring 说明后六个字段是为测试断言而暴露（AC3/AC30/AC31 需要中间组件）。

**验证：** `python -c "from rhinecode.bootstrap import BuildResult, BootstrapError; print('ok')"`。

## T20: 装配骨架与顺序照搬

**文件：** `rhinecode/bootstrap.py`
**依赖：** T19、T15、T16
**步骤：**
1. `build_app(cfg, *, user_dir=None, resume_latest=False, recorder=None) -> BuildResult`。
   `user_dir` 缺省 `Path.home() / ".rhinecode"`；`recorder` 缺省 `NullRecorder()`。
2. **把 `__main__.main()` 的装配段整体照搬过来，顺序一处不动**。
3. **把 `__main__.py` 里那段「位置为什么卡在这个窄窗口里 / 两头都不能挪」的注释整段迁移过来**
   ——它是 C11 留下的唯一记载，迁走代码却把注释留在原地等于把知识丢了（本轮新增的成对维护点）。
4. `mcp_config.load_all(user_dir=user_dir)`、`SkillManager(..., user_dir, ...)`、
   `ConversationManager(..., user_dir=user_dir, resume_latest=resume_latest, ...)`
   —— **`resume_latest` 不可漏**，漏了 `--continue` 静默失效。
5. 本任务先**不传 recorder、不转异常**（T21/T22 各自负责），末尾返回 `BuildResult`。

**验证：** `python -m compileall rhinecode` 无错误；`python -c "from rhinecode.bootstrap import build_app; print('ok')"`。

## T21: 三处致命错误转为异常

**文件：** `rhinecode/bootstrap.py`
**依赖：** T20
**步骤：** 把照搬过来的三处 `print(...) + sys.exit(1)` 改为
`raise BootstrapError(<与原 print 逐字相同的文案>)`：
`build_builtin_registry()` 的 `CommandRegistrationError`、`create_provider()` 的 `ValueError`、
`skill_manager.startup()` 返回非空 `fatal_tool_names`。
**工厂内不得再出现 `sys.exit`**（spec F22：测试要能捕获）。

**验证：** `python -c` 里用一个必然冲突的注册表桩调 `build_app`，断言抛 `BootstrapError`
且 `str(e)` 以「命令注册冲突：」开头。

## T22: 记录器注入与 Provider 包装

**文件：** `rhinecode/bootstrap.py`
**依赖：** T21、T18、T8
**步骤：**
1. Provider 包装：**仅 `recorder.enabled` 时** `provider = TracingProvider(provider, recorder, cfg.model)`
   （AC3：关闭时链路上不得有中间层）。
2. 向下注入 recorder 的三处（**漏任一处都是静默失效**）：
   - `SkillManager(..., recorder=recorder)`——否则 `startup` / `bind_tools` 的装配期
     `skill_state` 落空、AC14 拿不到事件；
   - `ConversationManager(..., recorder=recorder)`；
   - `RhineApp(manager, cfg, command_registry, recorder=recorder)`。
3. 把 `recorder` 放进返回的 `BuildResult`。

**验证：** 见 T26（本条的断言需要 tempdir 与 cleanup，不适合 `python -c`）。

## T23: 清理动作

**文件：** `rhinecode/bootstrap.py`
**依赖：** T22
**步骤：**
1. 在 `build_app` 内定义闭包 `cleanup(reason="normal_exit")`，**五步固定顺序**：
   ① `recorder.emit(SESSION_END, ...)`（**必须在 `close()` 之前**）；
   ② `manager.memory_manager.close()`（`try` 住）；③ `mcp_manager.close_all()`；
   ④ `clear_read_roots()`；⑤ `recorder.close()`。
2. **幂等守卫**：闭包内用一个可变标志（如 `nonlocal` 布尔或单元素列表），已执行过直接返回。
   注释说明理由：其余步骤天然幂等，但 `session_end` 重复产出会破坏「一次运行一份记录」的可读性。
3. `session_end` 负载：`turn_total()`、`elapsed()`、`reason`。

**验证：** 见 T26。

## T24: 入口瘦身与 `--trace`

**文件：** `rhinecode/__main__.py`
**依赖：** T23
**步骤：**
1. 新增 argparse 参数：`--trace`，`nargs="?"`，`const=_TRACE_DEFAULT`（模块级哨兵字符串），
   `default=None`。注释写清三态语义与为什么必须有 `const`：
   不给 `const` 时「给了但无值」会拿到 `None`、与「未给」无法区分；
   写成普通选项则 `--trace` 后紧跟其它参数时会把它吞成路径。
2. 按三态构造记录器，**后两态一律经 `create_recorder` 工厂**（T6 步骤 5），
   不得直接 `TraceRecorder(...)`——目标路径不可写时构造会抛异常，直接构造会让进程
   带 traceback 崩在装配之前，违反 spec AC22：
   `None → NullRecorder()`；哨兵 → `create_recorder(default_trace_path(workspace_root()))`；
   路径 → `create_recorder(Path(该路径))`。
3. argparse、三类模板生成、配置加载、占位符拦截**原样不动**。
4. 装配段改为 `result = build_app(cfg, resume_latest=args.continue_session, recorder=recorder)`。
5. `except BootstrapError as e: print(e, file=sys.stderr); sys.exit(1)`——**不再自己拼前缀**。
6. `try: result.app.run() finally: result.cleanup()`。
7. **import 清理（两个显式清单，不要自行判断）**：
   - **删**：`CommandRegistrationError`、`build_builtin_registry`、`build_skill_command_specs`、
     `create_provider`、`ConversationManager`、`SkillManager`、`builtin_skills_dir`、
     `format_fatal_message`、`ToolRegistry`、`LoadSkillTool`、`MCPAddServerTool`、`MCPManager`、`RhineApp`；
   - **留**：`load` / `user_config_path` / `scaffold_user_config` / `PLACEHOLDER_API_KEY`、
     `perm_config`、`mcp_config`、`workspace_root`、`Path`、`argparse`、`sys`。
   ⚠️ `perm_config` 与 `mcp_config` 虽然出现在 T25 的迁移符号清单里，但**首次运行模板生成段
   仍在用它们**（`scaffold_user_config(user_config_path())`），删掉会 `NameError`；
   `workspace_root` 是 `--trace` 缺省路径要用的。

**验证：** `python -m rhinecode --help` 能看到 `--trace`；`python -m compileall rhinecode` 无错误。

## T25: 既有启动测试迁桩

**文件：** `tests/test_command_startup.py`、`tests/test_skill_startup.py`
**依赖：** T24
**步骤：**
1. 两文件都 `import rhinecode.bootstrap as bootstrap`。
2. **把 patch 目标从 `entry` 改到 `bootstrap`**，共**三处 patch 块**：
   `test_skill_startup.py` 的 `_run_main` 辅助函数（11 个用例共用，只改这一处）、
   `test_command_startup.py` 的两个 `with` 块。迁移的符号：`create_provider`、`ToolRegistry`、
   `mcp_config`、`MCPManager`、`MCPAddServerTool`、`SkillManager`、`LoadSkillTool`、
   `build_skill_command_specs`、`build_builtin_registry`、`builtin_skills_dir`、
   `ConversationManager`、`RhineApp`。
   **仍打在 `entry` 上的只有 `load`**（配置加载没迁走）。
3. **`test_skill_startup.py` 的 `fake_app` 替身必须加 `**kwargs`**：它当前是
   `def fake_app(manager, config, command_registry):`，而 `build_app` 现在会传 `recorder=`，
   不改则 `TypeError`、11 个用例全红。同文件的 `fake_conversation(*args, **kwargs)` 已吃 kwargs，
   无需改。**这不是弱化断言**——该替身只做捕获。
4. `patch.object(Path, "home", ...)` 与 `patch("sys.argv", ...)` **不动**（前者是全局补丁）。
   `test_skill_startup.py` 里那处 `patch.object(ToolRegistry, "default", side_effect=spy_default)`
   直接打在从 `rhinecode.tools.registry` 导入的类上、不经 `entry`，**无需迁移**。
5. **断言一律不改**——尤其三处 stderr 文案断言（`/ctx` `/context` `/other`；
   `read_fil` `bad.md` `版本`）必须原样通过。这是 AC32 的判据。

**验证：** `python -m unittest tests.test_command_startup tests.test_skill_startup -v` 全绿，
**13 个用例一条不少**。

## T26: 装配与清理测试

**文件：** `tests/test_bootstrap.py`（新建）
**依赖：** T25
**步骤：** 统一用「tempdir 作工作区 + `os.chdir` + 临时 `user_dir` + `finally: result.cleanup()`」
的 fixture（`build_app` 会经 `MemoryManager.startup()` 在**当前工作目录**建
`.rhinecode/sessions/` 与会话锁，不清理会留下锁靠 600 秒过期自愈）。覆盖——
1. `build_app` 可在测试进程内调用，返回可用 `app` 与 `cleanup`（AC28）；
2. `cleanup()` 后会话锁被释放、MCP 连接被回收；**连续调两次不抛异常**，
   且开启记录时 trace 里 `session_end` **恰好一条**（AC28 + T23 幂等）；
3. **关闭记录时** `result.manager` 持有的 provider **不是** `TracingProvider`（AC3 的装配侧）；
4. 三类致命错误各抛 `BootstrapError`，**文案与既有三段逐字一致**；白名单笔误场景下断言
   `connect_all` **未被调用**（AC29）；
5. 保留一组**无参**调用（只传 `cfg`），证明 T13–T16、T18 的参数确实可选。

> `create_provider` 不联网（只构造客户端对象），假 key 可用，无需桩。

**验证：** `python -m unittest tests.test_bootstrap -v` 全绿。

## T27: 隔离、子进程与命令行形态测试

**文件：** `tests/test_bootstrap.py`
**依赖：** T26
**步骤：** 覆盖——
1. **AC30**：传入临时 `user_dir` 后四类用户级内容均不生效——系统提示不含用户级项目指令与
   记忆索引、用户级 Skill 不在清单、用户级权限规则不参与求值、用户级 MCP 未被连接。
   做法：在临时 `user_dir` 下预置 `RHINE.md` / `memory/MEMORY.md` / `skills/x.md` /
   `permissions.yaml` / `mcp.yaml` 各一份内容可识别的文件，再用**另一个**空临时目录装配，
   断言这些内容都不出现。
2. **AC31**：同进程内连续两次「装配 → 清理」，第二次的只读白名单不含第一次注册的路径
   （两次用不同 `user_dir`，断言白名单内容）。
3. **AC32**：**子进程实跑**不带 `--trace` 的启动，比对退出行为与 stderr。
   ⚠️ 必须让进程**确定性退出**，否则会进 Textual 事件循环挂到超时——用
   `--config` 指向一份 `api_key: YOUR_API_KEY` 的配置（命中占位符拦截、退出码 1），
   或指向不存在的文件（配置错误、退出码 1）；`subprocess.run(..., timeout=…)` 必须带超时。
4. **AC23**：子进程分别用 `--trace` 与 `--trace <路径>` 启动（同样用上面的确定性退出条件），
   断言两种形态下目标文件均被创建且首行可解析。
   若确定性退出发生在记录器构造**之前**，则改为退出**之后**才拦截的条件
   （如合法配置 + 一个必然抛 `BootstrapError` 的 Skill 笔误），保证记录器已产出
   至少一条事件后进程才退出。

**验证：** `python -m unittest tests.test_bootstrap -v` 全绿。

## T28: 第二段收尾提交

**依赖：** T13–T27
**步骤：** 全量测试；确认既有 580 条**一条不少**（迁桩只改 patch 目标与替身签名、不改断言）；提交。

**验证：** `python -m unittest discover -s tests 2>&1 | tail -3` 显示 `OK`。

---

# 第三段：埋点铺开与阅读器

> 完成后 AC2、AC4、AC6、AC9–AC16、AC18–AC21、AC24–AC27、AC33–AC38 可验。

## T29: Agent 接收记录器

**文件：** `rhinecode/agent/loop.py`
**依赖：** T7
**步骤：** `Agent.__init__(provider, registry, recorder=None)`，缺省 `NullRecorder()`，
存 `self._recorder`；docstring 注明缺省等于零回归。

**验证：** `python -m unittest tests.test_perm_loop tests.test_skill_loop_policy -v` 全绿。

## T30: 权限决策埋点

**文件：** `rhinecode/agent/loop.py`
**依赖：** T29
**步骤：**
1. 在 `_execute` 里 `decision = engine.decide(to_request(...))` 之后 `emit(PERMISSION_DECISION, ...)`：
   工具名、规范化请求的 `kind`/`specifier`/`is_read_only`、`decision`、`layer`、`reason`。
2. 注释说明**为什么只埋这一处**：`engine.decide` 另有一个生产调用点（协调层注入给
   `glob_files`/`grep_content` 的逐文件过滤器），一次 grep 触发几百次判定，
   埋进去会淹掉整条 trace；且它判的是「文件是否出现在结果里」而非工具放行。
3. 注释说明**为什么埋在调用点而非引擎内部**：保持 `permission/`「纯判定、无副作用」的既有性质
   （`decide` 的 docstring 明写「副作用：无」）。

**验证：** 用真实引擎跑一次工具调用，断言 trace 含 `permission_decision` 且 `layer` 非空。

## T31: 工具执行埋点（八处写入点）

**文件：** `rhinecode/agent/loop.py`
**依赖：** T30
**步骤：**
1. 定义 `outcome` 取值（模块级常量或字面量 + 注释）：`executed` / `out_of_scope` /
   `unknown_tool` / `invalid_arguments` / `denied_by_permission` / `denied_by_user`。
2. **在除 `_run_special` 之外的八处 `results[tc.id]` 写入点各埋一次**
   `emit_lazy(TOOL_EXECUTE, ...)`。八处逐个列明（按方法 + 分支定位）：

   | # | 方法 | 分支 | `outcome` |
   | --- | --- | --- | --- |
   | 1 | `_execute` | `out_of_scope` 循环 | `out_of_scope` |
   | 2 | `_run_readonly_concurrent` | 参数解析失败 | `invalid_arguments` |
   | 3 | `_run_readonly_concurrent` | 线程池结果（含执行异常兜底） | `executed` |
   | 4 | `_run_one_serial` | 未知工具 | `unknown_tool` |
   | 5 | `_run_one_serial` | 参数解析失败 | `invalid_arguments` |
   | 6 | `_run_one_serial` | 权限 DENY | `denied_by_permission` |
   | 7 | `_run_one_serial` | 用户拒绝 | `denied_by_user` |
   | 8 | `_run_one_serial` | 放行执行 | `executed` |

   > `results[tc.id]` 在文件里共有 **10 处**赋值，另 2 处在 `_run_special` 内。
3. `_run_special`（`ask_user` / `present_plan`）**不产本事件**——由 `interaction` 承载
   （负载完全重叠，且它们是交互而非工具执行）。写注释记下这条裁决。
4. 负载：工具名、参数、`ok`、`summary`、`output`（经 `clip`）、耗时、`is_concurrent`、`outcome`。
   `outcome != "executed"` 时耗时记 0。
5. **第 1 处（`out_of_scope`）的注释要写明它的分量**：它回灌的
   `[工具不可用] … 当前可用工具：…` 原文，是 spec「实证」第二条（模型调用了本轮没发给它的
   工具）的**唯一物证**——既不在 `permission_decision` 里（没进引擎），
   叠加 F17 后也不在 `agent_event` 里。漏埋这条等于本模块查不出当初立项要查的那个问题。

**验证：** 分别构造六种 `outcome` 的场景，断言 trace 里各有一条对应 `tool_execute`；
`out_of_scope` 那条的 `output` 含「工具不可用」。

## T32: 并发桶的作用域传递

**文件：** `rhinecode/agent/loop.py`
**依赖：** T31
**步骤：**
1. 在 `_run_readonly_concurrent` 提交任务**之前**捕获父作用域
   `parent = self._recorder.current_scope()`。
2. 提交的可调用改为一个包装函数：入口先 `self._recorder.bind_scope(parent)`，再调 `tool.execute`。
3. 注释写清**准确的**理由（不要写成「否则 tool_execute 会被记成 main」——那是错的）：
   T31 的 `tool_execute` 是在 `as_completed` 循环里 emit 的，那段代码跑在生成器所在的
   Worker 线程上，作用域天然正确。真正跑在池线程里的埋点是
   **`tool.execute` 内部产生的事件**（今天唯一实例是 `load_skill` → `SkillManager.activate`
   → `skill_state`）。本包装是纵深防御：兜住工具内部的埋点，并为将来在工具内部埋点
   留出正确语义。`threading.local()` 不跨线程继承，不传就没有别的办法。
4. 包装函数内**不加 try/except 吞异常**——既有的 `future.result()` 兜底逻辑不能变；
   埋点本身的异常由 `emit` 内部兜住。

**验证：** 在非 `main` 作用域下让一个只读工具在其 `execute` 内部 emit 一条事件，
断言该事件的 `scope` 与父作用域一致。

## T33: 协调层的记录器传递与旁路覆盖

**文件：** `rhinecode/conversation.py`
**依赖：** T29、T18
**步骤：**
1. 向下传递 `self._recorder`（**漏传是静默失效**）：`Agent(...)`、`ContextManager(...)`、
   `MemoryManager(...)`，以及 `_run_isolated_skill` 里的子 `Agent(...)`。
2. `_provider_for`：**仅 `self._recorder.enabled` 时**把新建的 provider 包一层 `TracingProvider`
   （spec F18 的旁路覆盖 + AC3 的关闭时无中间层）。包装后再存进 `self._provider_cache`，
   保证同名模型复用同一个包装实例。

**验证：** 构造带自定义模型的独立模式 Skill，断言其子对话请求出现在 trace 中；
关闭记录时断言 `_provider_for` 返回的**不是** `TracingProvider`。

## T34: 独立模式的作用域

**文件：** `rhinecode/conversation.py`
**依赖：** T33
**步骤：**
1. `_run_isolated_skill` 用 `with self._recorder.scope(isolated_scope(spec.name)):` 包住
   **子 Agent 的整个运行**（含驱动子事件流的那个 for 循环）。
2. 注释说明两件事：① 正常路径为什么有效（生成器体由 Worker 线程首次 `next()` 时 `__enter__`，
   子对话驱动循环也在生成器体内，耗尽后 `__exit__` 复位）；
   ② **为什么还需要 `_do_stream` 的兜底复位**（见 T42）——异常与放弃路径下 `__exit__`
   可能不跑，而 Textual 复用池化线程，泄漏会污染后续运行。

**验证：** 跑一次独立模式 Skill，断言其间的 `api_request` / `tool_execute` / `agent_event`
作用域均为 `isolated:<name>`，结束后新事件回到 `main`。

## T35: 运行中恢复的历史事件

**文件：** `rhinecode/conversation.py`
**依赖：** T33
**步骤：** 在 `_resume_stream` 的成功分支（产出 `HISTORY` 事件处）
`emit(HISTORY_RESTORED, origin="resume_command", message_count=..., session_id=...)`。
注释说明 `origin` 区分两条来源（另一条是装配期的启动恢复，见 T46）。

**验证：** 触发 `/resume` 成功载入，断言 trace 含 `history_restored` 且 `origin` 为 `resume_command`。

## T36: 第一层存盘暴露明细

**文件：** `rhinecode/context/offload.py`
**依赖：** 无
**步骤：**
1. `_offload_one(msg)` 的返回类型从 `bool` 改为 `Optional[Path]`——成功返回落盘路径，
   写盘失败返回 `None`。调用方原先判 `True/False` 的地方改判 `is not None`。
2. `Offloader` 新增公开属性 `last_run_details: list[tuple[str, str]]`（调用标识 → 落盘路径字符串），
   在 `run()` 开头清空、每次成功存盘时追加。
3. **`run()` 的返回值语义一行不改**（仍是 `list[CompactionNotice]`）——它是
   `before_request` 的 notices 契约，动它会牵连 C8 的既有测试。
4. 注释说明为什么需要这一步：spec F16 要求 `context_compaction` 记「第一层存盘的**调用标识
   列表与落盘路径**」，而现状下 `tool_call_id` 是 `_key()` 的局部返回、落盘路径是
   `_offload_one()` 的局部变量，`ContextManager` 层完全拿不到。

**验证：** `python -m unittest tests.test_context_offload tests.test_context_manager -v` 全绿；
手工构造一次存盘后断言 `last_run_details` 非空且路径存在。

## T37: 上下文压缩埋点

**文件：** `rhinecode/context/manager.py`
**依赖：** T36、T7
**步骤：**
1. `__init__` 新增 `recorder=None` 关键字参数，缺省 `NullRecorder()`。
2. 第一层：调 `self._offloader.run(history)` 之后读 `self._offloader.last_run_details`，
   `emit(CONTEXT_COMPACTION, layer="offload", tool_call_ids=[...], paths=[...])`。
3. 第二层：`emit(CONTEXT_COMPACTION, layer="summary", retain_index=..., before_count=...,
   after_count=..., ok=...)`；失败路径记 `ok=False` 与失败计数、熔断标志。
4. 摘要的 `stream_chat` 调用用 `with self._recorder.scope(SCOPE_SUMMARY):` 包住。
   注释说明理由：它与主对话共用同一个 Provider 实例，不区分会混进主对话的轮次计数。

**验证：** `python -m unittest tests.test_context_manager tests.test_context_summarize -v` 全绿；
触发一次手动压缩后断言 trace 含 `layer="summary"` 的事件，且该次 `api_request` 的 scope 为 `summary`。

## T38: 记忆线程的作用域

**文件：** `rhinecode/memory/manager.py`
**依赖：** T7
**步骤：**
1. `__init__` 新增 `recorder=None` 关键字参数，缺省 `NullRecorder()`。
2. 在 **`MemoryManager._update_notes` 的 `try:` 之前、函数体第一行**
   `self._recorder.bind_scope(SCOPE_MEMORY)`。
   ⚠️ **不要绑在 `on_natural_stop`**——那是 Worker 线程（由 `_wrap_events` 调用），
   绑在那里会把主对话线程永久标成 `memory`。`_update_memories` 才是 daemon 线程的目标函数。
3. 注释说明：记忆跑独立线程且可能与用户的下一条消息并发，thread-local 天然隔离它，
   线程入口绑定一次即可，无需 `with`。

**验证：** `python -m unittest tests.test_memory_manager -v` 全绿；触发一次记忆钩子后
断言该次 `api_request` 的 scope 为 `memory`，且**主对话后续事件仍为 `main`**。

## T39: Skill 状态埋点（锁外）

**文件：** `rhinecode/skills/manager.py`
**依赖：** T18
**步骤：**
1. 埋点方法**五个**：`activate` / `deactivate` / `clear_active` / `reload` / `bind_tools`。
   负载：动作、Skill 名、激活列表快照、降级形态（TRUNCATED / DROPPED）、白名单计算结果。
   - `clear_active` 不可漏——`/clear` 与 `/resume` 都调它清激活态，这正是 spec F16 的
     「激活列表变化」；
   - **明确排除 `tool_policy()`**：它被 `RunOptions.tool_policy` 回调**每轮**调用，
     埋进去会每轮刷一条 `skill_state`、把时间线淹掉。白名单结果只在 `bind_tools`（启动一次）记。
2. **⚠️ 全部埋点必须在 `self._lock` 临界区之外。** `activate` 是四段式结构、只有第③段持锁，
   埋点放在第③段之外即可。
3. **`deactivate` 需要小重构**：它当前的四个 `return` **全在 `with self._lock:` 内**，
   要「埋在锁外」必须改成「锁内算出结果 → 出锁 → emit → return」。这不改变任何返回值语义。
4. 注释引用 C11 的教训：临界区内做回调 → Textual 阻塞式 `call_from_thread` → 主线程要
   同一把锁 → 确定性死锁、整个界面冻结。

**验证：** `python -m unittest tests.test_skill_manager -v` 全绿（含既有的跨线程死锁护栏）；
激活、卸载、清空、热更新各触发一次，断言 trace 里 `skill_state` 各有一条。

## T40: 命令分发埋点

**文件：** `rhinecode/commands/dispatcher.py`
**依赖：** T7
**步骤：**
1. `__init__(registry, recorder=None)`，缺省 `NullRecorder()`。
2. `user_input` 埋在**判定非 EMPTY 之后**——EMPTY 输入零副作用（沿用 C10 spec F4），
   不产任何事件。写注释说明。
3. `command_dispatch` **每次斜杠输入恰好一条，且必须在 handler 执行之前 emit**，两个位置：
   - 未知命令分支（此时无 `invocation`，用 `command_token`，`is_unknown=True`）；
   - **`invocation` 构造完成之后、必需参数校验之前**（`is_unknown=False`）。

   ⚠️ **不要埋在 `return DispatchResult(kind=COMMAND)` 处**：那在 `spec.handler(...)` 之后，
   于是 `/skills` 这类 LOCAL 命令的 `ui_message`（handler 内部调 `show_message` 产生）
   序号会**小于**它自己的 `command_dispatch`，读时间线时命令输出出现在命令分发之前。
4. 负载：命令名、`typed_name` 与 `matched_name`（别名）、参数、`CommandType`、`is_unknown`。
   普通消息只产 `user_input`、不产本事件。

**验证：** `python -m unittest tests.test_command_dispatcher -v` 全绿；
四种情形（未知 / 缺参 / handler 异常 / 正常命中）各触发一次，断言 `command_dispatch`
**恰好四条**、`command_type` 可区分三类、且每条的 `seq` 都**小于**该命令产生的 `ui_message`。

## T41: 界面层的记录器接线

**文件：** `rhinecode/tui/app.py`
**依赖：** T40、T18
**步骤：** `CommandDispatcher(command_registry, recorder=self._recorder)`
（`RhineApp` 的 `recorder` 参数已在 T18 加好）。

**验证：** `python -m unittest tests.test_command_tui tests.test_skill_tui tests.test_tui_keybindings -v` 全绿。

## T42: 事件流埋点与作用域复位

**文件：** `rhinecode/tui/app.py`
**依赖：** T41、T3
**步骤：**
1. `_do_stream` 的事件循环里对每个事件
   `emit_lazy(AGENT_EVENT, lambda e=event: agent_event_payload(e))`。
   ⚠️ **必须用默认参数绑定**——循环内直接写 `lambda: agent_event_payload(event)` 会捕获
   变量而非当轮值，全部闭包指向最后一个事件。
2. **`_do_stream` 的 `finally` 第一行**无条件 `self._recorder.bind_scope(SCOPE_MAIN)`。
   注释写清这是**作用域泄漏的唯一可靠防护**：Textual 的 thread worker 用默认线程池
   （`Worker._run_threaded` 末行 `run_in_executor(None, ...)`），**线程会被复用**；
   而 `_do_stream` 循环体全是 `call_from_thread`，应用退出竞态下会抛 `RuntimeError`
   （`_notify_memory` 与 `_notify_skill_activation` 两处既有代码为此包了 try/except，
   说明这不是理论风险），此时生成器被放弃、`with` 的 `__exit__` 可能压根不跑。
   一次泄漏的 `isolated:<name>` 会污染后续复用该线程的主对话运行，概率性且极难复现。
   **放在 `finally` 首行**是因为其后的 `call_from_thread` 也可能抛。
3. `HISTORY` 分支**不重复产** `history_restored`（已由 T35 在协调层产出）。

**验证：** 断言 `agent_event` 负载**不含** `output`/`arguments`/正文（AC20）；
构造一次子对话中途抛 `RuntimeError` 的场景，断言之后的事件 scope 回到 `main`。

## T43: 界面消息埋点

**文件：** `rhinecode/tui/app.py`
**依赖：** T41
**步骤：** `ui_message` 埋四处，**均记 markup 转义前的原始文本**（spec F15）：
`show_user_input`（`source="user_echo"`）、`show_message`（`source="system"`）、
`_do_stream` 的系统行与错误行（`source="system"` / `"error"`）、
每轮 AI 正文收尾（`source="assistant"`，在 `reset_text_widgets` 之前把已累积的
`response_chunks` 记一次）。

**验证：** 显示一条含字面 `[` 的系统消息，断言 trace 里是转义前原文（AC19）。

## T44: 状态栏埋点

**文件：** `rhinecode/tui/app.py`
**依赖：** T41
**步骤：**
1. 在 `_refresh_status` 里**先把九个值组成一个局部变量组（或 dict）**，
   **同时**传给 `StatusBar.update_status` 与纯函数 `compose_status_text`，用后者的返回值 emit。
   ⚠️ **不要手抄第二份参数清单**——`update_status` 与 `compose_status_text` 是同名九参数，
   抄一遍会让「新增状态栏字段」的维护点从两处涨到三处。
2. 注释说明为什么不能只记散字段：真正的文本由 `compose_status_text` 在
   `StatusBar.update_status` 内部组装，只记字段的话 spec F15 要求的「文本快照」不成立；
   该函数是纯函数、无副作用，重复调一次零代价。
3. **记 markup 原文**（含 `\[` 转义与颜色标签），与 `ui_message` 同口径。

**验证：** 激活一个 Skill 后断言 `status_bar` 事件的文本含 `Skill:1`。

## T45: 交互面板埋点（四类各一条）

**文件：** `rhinecode/tui/app.py`
**依赖：** T41
**步骤：**
1. **给 `_interact` 增加一个 `display: str` 参数**——展示内容目前全被闭进 `show_fn` 里、
   在 `_interact` 内拿不到。三个调用方（`_confirm_tool` / `_clarify` / `_approve_plan`）
   各自把摘要传进来（它们分别持有 `tool_call` / `question` / `plan`）。
2. `_interact` 在**阻塞等待返回之后**（即结算时刻）emit **一条** `INTERACTION`：
   `kind`、`display`、`source="human"`、`result`。
3. 会话选择面板走另一条路径（主线程发起、无 Worker 阻塞等待），在**结算处**埋：
   选中分支与 Esc 取消分支各一条——**二者互斥，故一次交互仍只有一条**。
   ⚠️ **不要在 `_show_session_panel` 也埋**——那会让一次会话选择产出两条，
   与 AC16「四类面板**各**产出一条」不符。
4. `ask_user` / `present_plan` 的记录由本事件承载（T31 已裁决它们不产 `tool_execute`）。

**验证：** 四类面板各触发一次，断言 trace 里 `interaction` **恰好四条**且
`kind`/`display`/`result` 三项俱全（AC16）。

## T46: 装配期事件

**文件：** `rhinecode/bootstrap.py`
**依赖：** T22、T39
**步骤：**
1. `build_app` 末尾（`RhineApp` 构造之后）`emit_lazy(SESSION_START, ...)`：项目根、
   `redact_config(cfg)`、协议与模型、权限模式、Plan Mode、已注册工具名清单、
   MCP 连接状态、Skill 清单快照。注释重申时机约束（必须在 `connect_all` + `bind_tools`
   之后，否则记出半空快照）。
2. `resume_latest` 且恢复成功时 `emit(HISTORY_RESTORED, origin="startup",
   message_count=len(manager.history), session_id=manager.memory_manager.session_id)`。
   注释说明**启动恢复不经任何事件流**（它在 `ConversationManager` 构造期间原地改写 history），
   故必须在装配阶段单独产出，否则 trace 里会凭空多出一段历史而无事件解释来源。
3. MCP 连接结果**不单独产事件**——由 `session_start` 的快照承载（plan 的裁决），写注释。

**验证：** 开启记录跑一次装配，断言 `session_start` 的工具清单非空、MCP 状态字段存在、
`api_key` 为掩码（AC18）；预置存档后带 `resume_latest=True` 装配，
断言 `history_restored` 的 `origin` 为 `startup`（AC13）。

## T47: 阅读器骨架与时间线摘要

**文件：** `rhinecode/trace/reader.py`（新建）
**依赖：** T7
**步骤：**
1. argparse：位置参数 `path`；`--type`、`--scope`（逗号分隔列表）；`--seq`（整数）。
2. 逐行 `json.loads`，**坏行跳过并计数**（与会话存档容错口径一致），末尾报告跳过条数。
3. 摘要模式：每事件一行 `seq / ts / scope / type / 一句话关键信息`。关键信息由一张
   `type → 摘要函数` 的表驱动。**未登记的类型输出显式「未登记类型」而非空白**
   ——这是新增事件类型时的自检信号（成对维护点）。
4. **只以读模式打开文件**，绝不写回。
5. `if __name__ == "__main__": main()`，供 `python -m rhinecode.trace.reader` 调用。

**验证：** `python -m rhinecode.trace.reader <一份真实 trace>` 输出每事件一行的时间线。

## T48: 阅读器过滤与展开

**文件：** `rhinecode/trace/reader.py`
**依赖：** T47
**步骤：**
1. `--type` 与 `--scope` 可组合（取交集）。
2. `--seq n`：找到该条，人可读排版输出完整负载；**被截断的字段显式标出原长**。

**验证：** `--type api_request --scope main` 能缩小结果；`--seq 3` 展开单条并显示原长标注。

## T49: 忽略清单

**文件：** `.gitignore`
**依赖：** 无
**步骤：** 在既有 `**/.rhinecode/context|sessions|memory/` 三行旁加 `**/.rhinecode/traces/`，
并加注释说明 trace 与会话存档同属敏感产物（含被读过的文件内容与命令输出，
模型读过配置文件时还可能含明文密钥）。**必须用 `**/` 前缀**与既有三行同口径，
否则嵌套项目路径下失效。

**验证：** 在子目录里造 `.rhinecode/traces/x.jsonl`，`git status --porcelain` 不显示它（AC26）。

## T50: 埋点测试（模型交互 / 权限 / 工具）

**文件：** `tests/test_trace_hooks.py`（新建）
**依赖：** T31、T37
**步骤：** 覆盖——`api_request` 的 `tool_names` 与假 Provider 收到的 schema 一致（AC10）；
`api_response` 关键字段非空；`permission_decision` 的 `layer` 与 `decision` 正确；
**六种 `outcome` 的 `tool_execute` 各一条**，`out_of_scope` 那条的 `output` 含「工具不可用」；
截断记原长（AC17）；`context_compaction` 两层各自的字段（第一层的调用标识与路径、
第二层的保留边界与前后条数）。

**验证：** `python -m unittest tests.test_trace_hooks -v` 全绿。

## T51: 埋点测试（作用域与不阻断）

**文件：** `tests/test_trace_hooks.py`
**依赖：** T50、T32、T34、T38、T42
**步骤：** 覆盖——
1. 子对话**全部事件**（模型请求响应、权限、工具、循环）作用域为 `isolated:<name>`（AC11）；
2. 摘要调用 scope 为 `summary`、记忆调用 scope 为 `memory`，且**主对话后续事件仍为 `main`**；
3. 不隶属任何对话的事件（会话启停、命令分发、状态栏）作用域为 `main`（AC21）；
4. **作用域泄漏防护**：`_do_stream` 抛异常后，后续事件 scope 回到 `main`；
5. **AC6**：用一个 `emit` / `emit_lazy` **必抛**的 recorder 跑一轮含只读并发工具的循环，
   断言 `ToolResult.ok is True` 且 output **不含**「工具执行异常」——
   这是 plan 风险表里「只读并发桶抛异常被当成工具执行异常回灌模型」的护栏。

**验证：** `python -m unittest tests.test_trace_hooks -v` 全绿。

## T52: 埋点测试（界面与命令）

**文件：** `tests/test_trace_hooks.py`
**依赖：** T51、T40、T43、T44、T45
**步骤：** 覆盖——命令三类（LOCAL/UI/PROMPT）可区分且 `command_dispatch` 恰好四条、
每条 `seq` 小于对应 `ui_message`（AC15）；四类面板各一条 `interaction`（AC16）；
`ui_message` 记转义前原文（AC19）；`status_bar` 文本含 `Skill:N`；
`agent_event` 不带重负载（AC20）。

**验证：** `python -m unittest tests.test_trace_hooks -v` 全绿。

## T53: 埋点测试（装配期与恢复）

**文件：** `tests/test_trace_hooks.py`
**依赖：** T52、T46、T35
**步骤：** 覆盖——十五类事件**均可产出且关键字段非空**（AC9，本条做总清点）；
启动恢复的 `history_restored`（`origin="startup"`，AC13）与运行中恢复
（`origin="resume_command"`，AC12）；手动压缩的事件流被记录（AC12）；
装配期 `skill_state`（AC14）；`session_start` 的 `api_key` 为掩码（AC18）；
**一次运行内 `/clear` + `/resume` 记录文件不切换、两个动作各自留下事件**（AC27）。

**验证：** `python -m unittest tests.test_trace_hooks -v` 全绿；AC9 的清点断言覆盖十五个类型。

## T54: 零回归测试（黄金基线与双跑）

**文件：** `tests/test_trace_zero_regression.py`（新建）
**依赖：** T53
**步骤：**
1. **AC1**：关闭记录时系统提示的**稳定段**与**工具 schema** 与固化黄金基线逐字节相等。
   ⚠️ **必须在临时工作区 + 临时空 `user_dir` 下取基线**：稳定段含项目指令（RHINE.md）
   与 Skill 清单两个槽位，若在仓库根跑会把本仓库的 `RHINE.md` 与 `.rhinecode/skills/`
   内容固化进基线，之后任何文档改动都让 AC1 变红。基线只覆盖七个固定模块 + 工具 schema。
2. **AC2**：同一输入同一环境下开关双跑，系统提示**动态段**与**会话存档内容**两侧逐字节相等。
   **不对基线做逐字节断言**——动态段带当日日期、工作目录绝对路径、git 分支，
   存档每行首字段是写入时刻时间戳，固化必然跨天/换目录即红；对基线只断言字段集合与结构。

**验证：** `python -m unittest tests.test_trace_zero_regression -v` 全绿。

## T55: 零回归测试（无中间层与格式一致）

**文件：** `tests/test_trace_zero_regression.py`
**依赖：** T54
**步骤：**
1. **AC3**：关闭记录时协调层持有的 provider **不是** `TracingProvider`，
   `_provider_for` 返回的也不是。
2. **AC4**：关闭记录时跑一次完整装配，不产生记录文件、不创建 `traces` 目录。
3. **AC24**：编程注入产出的事件**字段集合**与固定契约一致（与 T27 里 `--trace` 子进程
   途径产出的同类事件对比，或与一份显式的字段集合契约对比），证明两条途径格式一致。

**验证：** `python -m unittest tests.test_trace_zero_regression -v` 全绿。

## T56: 阅读器测试

**文件：** `tests/test_trace_reader.py`（新建）
**依赖：** T48
**步骤：** 覆盖——摘要每事件一行且含序号与类型（AC34）；`--type` 与 `--scope` 分别与组合过滤
的条数正确（AC35）；`--seq` 展开含完整负载与截断字段原长标注（AC36）；
**运行前后文件哈希不变**（AC37）；坏行被跳过并计入报告；未登记类型输出「未登记类型」；
**AC33**：断言 `pyproject.toml` 的 `[project.scripts]` 只有 `rhine` 一项，
且 `python -m rhinecode --help` 输出不含阅读器相关选项。

**验证：** `python -m unittest tests.test_trace_reader -v` 全绿。

## T57: 文档同步

**文件：** `CLAUDE.md`、`AGENTS.md`、`README.md`
**依赖：** T56
**步骤：**
1. `CLAUDE.md` 架构节新增 **Trace 层**条目，措辞明确它是**跨阶段测试设施**，
   **不挂在 Skill 系统名下、不占章节号**。
2. 「成对维护点」备忘新增**三条**：
   - 新增 trace 事件类型 → `trace/models.py` 枚举 + `trace/reader.py` 的
     「type → 摘要函数」表（漏了就是新事件在阅读器里显示成空白且不报错）；
   - `bootstrap.build_app` 的装配顺序 → 那段「两头都不能挪」的理由注释必须随代码走；
   - 新增状态栏字段 → 原有两处（`compose_status_text` 渲染 + `_refresh_status` 取值）
     之外，`_refresh_status` 现在**同时**把参数组喂给 `compose_status_text` 做 trace 快照，
     保持单一参数组、不要抄第二份。
3. 「常用命令」节补 `--trace` 与阅读器调用方式。
4. 「安全边界」节补一条：trace 产物含被读过的文件内容与命令输出，模型读过配置文件时
   可能含明文密钥，已进 `.gitignore`、勿提交、勿外传。
5. `AGENTS.md` / `README.md` 同步（沿用 C11 T66 做法）。

**验证：** `grep -n "traces" .gitignore CLAUDE.md` 均有命中；通读三份文档无遗漏。

## T58: 第三段收尾与全量验收

**依赖：** T29–T57
**步骤：** `python -m compileall rhinecode tests`；全量测试；确认既有 580 条一条不少；
按 `checklist.md` 逐项验收；提交。

**验证：** `python -m unittest discover -s tests 2>&1 | tail -3` 显示 `OK`。

---

## 执行顺序

```
第一段（低风险，不碰既有模块）
T1 → T2 → T3
      ↓     ↓
      T4 → T5 → T6 → T7 → T8
                            ↓
   T9（←T3）   T10（←T6）   T11（←T8）
                            ↓
                           T12 收尾提交

第二段（高风险，唯一打断既有测试的一段）
T13 → T14 → T16 ─┐
T15 ─────────────┤
T17 ─────────────┤
T18（←T7）───────┤
T19 ─────────────┴→ T20 → T21 → T22（←T18/T8）→ T23 → T24 → T25 → T26 → T27
                                                                          ↓
                                                                        T28 收尾提交

第三段（面广，每处独立）
T29 → T30 → T31 → T32
T33（←T29/T18）→ T34
T35（←T33）
T36 → T37
T38 · T39 · T40（互相独立）
T41（←T40/T18）→ T42 · T43 · T44 · T45
T46（←T22/T39）
T47 → T48
T49（独立）
      ↓
T50（←T31/T37）→ T51（←T32/T34/T38/T42）→ T52（←T40/T43/T44/T45）→ T53（←T46/T35）
      ↓
T54 → T55 → T56（←T48）→ T57 → T58 全量验收
```

**跨段依赖**：第二段的 T18 与 T22 依赖第一段的 T7/T8；第三段的 T46 依赖第二段的 T22。
其余段内自洽，**无循环依赖**。
