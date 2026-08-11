# C9 记忆系统（项目指令 · 会话存档 · 自动记忆）Tasks

## 文件清单

| 操作 | 文件 | 职责 |
|------|------|------|
| 新建 | `rhinecode/memory/__init__.py` | 导出 MemoryManager 等公共接口 |
| 新建 | `rhinecode/memory/lockfile.py` | 锁原语：原子创建/释放/心跳/过期判定 |
| 新建 | `rhinecode/memory/instructions.py` | RHINE.md 三层加载 + @include 展开（纯函数） |
| 新建 | `rhinecode/memory/session.py` | SessionStore：JSONL 建档/追加/扫描/载入/清理/会话锁 |
| 新建 | `rhinecode/memory/notes.py` | 记忆 frontmatter 与索引的解析/渲染/截断（纯逻辑） |
| 新建 | `rhinecode/memory/note_updater.py` | 记忆 LLM 的 Prompt 与响应解析 |
| 新建 | `rhinecode/memory/manager.py` | MemoryManager 编排 |
| 新建 | `rhinecode/agent/prompt/texts/init.py` | /init 内置指令文本 |
| 修改 | `rhinecode/tools/path_guard.py` | 额外只读根目录白名单 + resolve_readable / is_readable_path |
| 修改 | `rhinecode/tools/read_file.py` | 路径解析改走 resolve_readable |
| 修改 | `rhinecode/permission/engine.py` | ②沙箱层读类请求改走 is_readable_path |
| 修改 | `rhinecode/agent/prompt/builder.py` | build_default_prompt 增加两个槽位内容参数 |
| 修改 | `rhinecode/agent/loop.py` | run() 增加可选消息记录回调，3 处 append 后调用 |
| 修改 | `rhinecode/conversation.py` | 构造 MemoryManager；/resume /memory /init；追加写；事件流包装 |
| 修改 | `rhinecode/__main__.py` | --continue 参数；退出释放会话锁 |
| 修改 | `rhinecode/tui/widgets.py` | CommandPanel.COMMANDS 增 /resume /memory /init |
| 修改 | `rhinecode/tui/app.py` | notify 回调注入；会话锁心跳定时器；启动提示展示 |
| 新建 | `tests/test_memory_lockfile.py` | 锁原语测试 |
| 新建 | `tests/test_memory_instructions.py` | RHINE.md 加载测试 |
| 新建 | `tests/test_memory_notes.py` | 记忆/索引纯逻辑测试 |
| 新建 | `tests/test_memory_session.py` | 会话存档测试 |
| 新建 | `tests/test_memory_manager.py` | 编排测试（含 note_updater 解析） |
| 新建 | `tests/test_memory_sandbox.py` | 沙箱白名单测试 |
| 修改 | `CLAUDE.md` | 当前能力/架构/命令/测试章节补 C9 |

---

## T1: 锁原语 lockfile.py

**文件：** `rhinecode/memory/lockfile.py`（新建，同时新建空的 `rhinecode/memory/__init__.py` 占位）
**依赖：** 无
**步骤：**
1. `try_acquire(lock_path: Path, stale_after_seconds: float) -> bool`：用 `os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)` 原子创建；成功则写入 `PID + ISO 时间戳` 后关闭返回 True；`FileExistsError` 时读 mtime 判过期——过期则删除并**重试一次**原子创建；其余任何异常返回 False。
2. `release(lock_path)`：`unlink(missing_ok=True)`，异常静默。
3. `touch(lock_path)`：刷新 mtime（`os.utime`），异常静默。
4. `is_fresh(lock_path, stale_after_seconds) -> bool`：存在且 mtime 距今小于阈值；任何异常返回 False。
5. 中文注释说明：为什么必须原子创建（检查+创建两步会有竞态）、为什么过期只重试一次（避免两实例互删死循环）。

**验证：** `python -m compileall rhinecode/memory` 编译通过。

## T2: 锁原语测试

**文件：** `tests/test_memory_lockfile.py`（新建）
**依赖：** T1
**步骤：** 在 `tempfile.TemporaryDirectory` 中断言：
1. 首次 `try_acquire` 成功，第二次（未释放、未过期）失败。
2. `release` 后可再次获取。
3. 把锁文件 mtime 改到过期阈值前（`os.utime(path, times=(old, old))`），`try_acquire` 成功接管且旧锁被清除。
4. `is_fresh`：新锁 True、过期锁 False、不存在 False。
5. 锁路径的父目录不存在（不可创建）时 `try_acquire` 返回 False 不抛异常。

**验证：** `python -m unittest tests.test_memory_lockfile` 全绿。

## T3: RHINE.md 加载 instructions.py

**文件：** `rhinecode/memory/instructions.py`（新建）
**依赖：** 无
**步骤：**
1. 定义 `InstructionLayer` / `LoadedInstructions` 数据类（字段见 plan）。
2. `load_instructions(user_dir: Path, project_root: Path) -> LoadedInstructions`：按 用户级 `user_dir/RHINE.md` → 项目级 `project_root/.rhinecode/RHINE.md` → 项目根 `project_root/RHINE.md` 顺序逐层读取；每层内容做 include 展开后，以 `# 来源：<路径>` 标注头拼接；缺层跳过。
3. `_expand_includes(text, base_dir, boundary_root, depth, visited, errors) -> str`：逐行处理；维护「是否在围栏代码块内」状态（行首 ``` 翻转），块内不解析；用正则匹配行内 `@路径` token；相对 `base_dir` 解析并 `resolve()`，不在 `boundary_root` 内→不展开并记 errors；`depth >= 4` 或已在 `visited`→不展开记 errors；否则读文件递归展开（读失败记 errors 保留原文）。
4. 层边界：用户级层 `boundary_root = user_dir`；两个项目级层 `boundary_root = project_root`。

**验证：** `python -m compileall rhinecode/memory` 编译通过。

## T4: RHINE.md 加载测试

**文件：** `tests/test_memory_instructions.py`（新建）
**依赖：** T3
**步骤：** 在临时目录构造假的 user_dir / project_root，断言：
1. 三层齐备时 text 按序含三段且各带来源标注；只有部分层时其余正常。
2. `@子文件` 被展开；4 层嵌套时第 5 层保持原文且 errors 有记录。
3. A 引 B、B 引 A：加载正常结束（不递归爆栈），errors 记录循环。
4. `@../外部文件`（解析后越界）不展开且 errors 有记录。
5. 围栏代码块内的 `@路径` 保持原文。
6. include 指向不存在文件：原文保留 + errors 记录，整体不抛异常。

**验证：** `python -m unittest tests.test_memory_instructions` 全绿。

## T5: 记忆纯逻辑 notes.py

**文件：** `rhinecode/memory/notes.py`（新建）
**依赖：** 无
**步骤：**
1. 常量 `CATEGORIES = ("preference", "feedback", "project", "reference")` 与中文标签映射。
2. `Note` 数据类；`parse_note(text) -> Optional[Note]`：宽松解析 frontmatter（`---` 包围的 `key: value` 行），缺 name/summary/category 或格式坏 → None；**未知 frontmatter 字段忽略**（N5）。
3. `render_note(note) -> str`：往返一致（render 后 parse 得到等价 Note）。
4. `rebuild_index(notes: list[Note]) -> str`：首行 `# 记忆索引`，每条一行 `- {name}（{filename}）— {summary}`。
5. `truncate_index(text, max_lines=200, max_bytes=25*1024) -> str`：先按行截、再按 UTF-8 字节截（不切断多字节字符），先到为准。

**验证：** `python -m compileall rhinecode/memory` 编译通过。

## T6: 记忆纯逻辑测试

**文件：** `tests/test_memory_notes.py`（新建）
**依赖：** T5
**步骤：** 断言：parse/render 往返一致；坏 frontmatter → None；未知字段被忽略且不报错；非法 category → None；`rebuild_index` 行格式正确；201 行索引截到 200 行；构造 >25KB 文本截到 ≤25KB 且不产生非法 UTF-8。

**验证：** `python -m unittest tests.test_memory_notes` 全绿。

## T7: 会话存档 session.py

**文件：** `rhinecode/memory/session.py`（新建）
**依赖：** T1
**步骤：**
1. `SessionInfo` / `SessionLoadResult` 数据类。
2. `SessionStore(sessions_dir: Path)`；内部持有 `当前会话 ID`、`当前档路径`、`锁路径`、`已建档标志`。
3. `start_new() -> str`：`datetime.now().strftime("%Y%m%d-%H%M%S")` + 4 位随机小写字母数字；**不建文件**（惰性）。
4. `append(msg: Message)`：首次调用时创建 sessions 目录、JSONL 文件与会话锁（`try_acquire`）；序列化 `{ts, role, content, tool_calls?, tool_call_id?}` 一行追加（`open(..., "a")`）；顺带 `touch` 锁；全程 try/except 静默（F6）。
5. `list_sessions(limit=10) -> list[SessionInfo]`：扫 `*.jsonl` 按 mtime 倒序取前 limit；逐个解析出标题（首条 `role=="user"` 行 content 截 30 字）、消息数、last_time（末行 ts，无则 mtime）；`locked = is_fresh(对应锁, 600)`。坏文件跳过。
6. `load(session_id) -> SessionLoadResult`：逐行 `json.loads`，坏行计数跳过；未知字段忽略；重建 `Message`（含 tool_calls 反序列化）；**不成对处理**：收集完后扫描——`assistant(tool_calls)` 的每个 id 都有对应 `role="tool"` 行才保留该组，否则丢弃该 assistant 与其已有的部分 tool 行（计入 dropped_unpaired）；孤儿 `role="tool"`（无对应 assistant）同样丢弃。
7. `attach(session_id) -> bool`：目标锁 `is_fresh` → False；过期 → `release` 清除；`try_acquire` 新锁成功则把当前活跃档切到该文件（已建档标志置真），失败 → False。切换前释放旧会话锁。
8. `cleanup_expired(days=30) -> int`：删 mtime 超期且锁不新鲜的 `.jsonl`（连带其锁）；清孤儿 `.lock`（无同名 jsonl）。
9. `release()`：释放当前会话锁。

**验证：** `python -m compileall rhinecode/memory` 编译通过。

## T8: 会话存档测试

**文件：** `tests/test_memory_session.py`（新建）
**依赖：** T7
**步骤：** 临时目录中断言：
1. ID 匹配 `^\d{8}-\d{6}-[a-z0-9]{4}$`；`start_new` 后目录仍为空（惰性）；首次 `append` 后出现 jsonl 与锁。
2. append N 条后文件 N 行、每行可 `json.loads`、含 ts；`load` 还原的 Message 字段与原始一致（含 tool_calls 往返）。
3. 中间插入坏行：load 成功、skipped_lines=1、其余消息完整。
4. 结尾/中间构造「assistant(tool_calls) 缺 tool 行」：该组被丢弃、前后消息保留、dropped_unpaired 正确；孤儿 tool 行被丢弃。
5. 带未知字段的行正常载入（N5）。
6. `list_sessions`：标题=首条 user 截断、消息数、倒序；新鲜锁标注 locked=True。
7. `cleanup_expired`：31 天前的档被删、30 天内保留、新鲜锁保护的不删、孤儿锁被清。
8. `attach`：新鲜锁 → False；过期锁 → 接管成功且旧锁清除；接管后 append 追加进该文件。

**验证：** `python -m unittest tests.test_memory_session` 全绿。

## T9: 记忆 LLM 请求与解析 note_updater.py

**文件：** `rhinecode/memory/note_updater.py`（新建）
**依赖：** T5
**步骤：**
1. `NoteAction` 数据类。
2. `NOTE_SYSTEM_PROMPT` 常量：说明四类分类含义、用户级/项目级归属标准、对照现有索引去重、没有值得记的输出空数组 `[]`；要求**只输出 JSON 数组**，每项 `{op, scope, filename, name, summary, category, body}`；明确「你不能调用任何工具」。
3. `build_note_request(new_messages, user_index, project_index) -> tuple[str, list[Message]]`：返回 (system 文本, 单条 user 消息)——user 消息把两级索引与新增对话渲染成转录文本（工具结果截断到前 500 字，避免请求过大；参考 c8 `render_transcript` 手法）。
4. `parse_note_response(text) -> list[NoteAction]`：截取首个 `[` 到末个 `]` 后 `json.loads`；非数组 → `[]`；逐项校验 op/scope/category 合法、filename 只允许 `[a-z0-9_-]+\.md`（防路径注入），坏项跳过。

**验证：** `python -m compileall rhinecode/memory` 编译通过。

## T10: 编排者 manager.py 与包导出

**文件：** `rhinecode/memory/manager.py`、`rhinecode/memory/__init__.py`
**依赖：** T1 T3 T5 T7 T9
**步骤：**
1. `MemoryManager.__init__(provider, model, project_root, user_dir, notes_enabled, notify=None)`：构造 SessionStore（`project_root/.rhinecode/sessions`）、记录两个 memory 目录路径、加载态占位；`_note_inflight = threading.Event()`、高水位 `_note_watermark = 0`、`_last_note_result: str`。
2. `startup(resume_latest, history) -> Optional[str]`：`load_instructions` 缓存结果 → `cleanup_expired` → resume_latest 时按 list_sessions 顺序找第一个未锁定会话 attach+load 进 history（登记时间提醒），否则 `start_new`；返回启动提示。全程 try/except 兜底（N2）。
3. 注入三方法：`custom_instructions()`（缓存的 text）、`memory_index()`（现读两目录索引文件 + `truncate_index`，两级拼接）、`consume_pending_notice()`（取后即清）。
4. `record_message(msg)` → `session.append(msg)`。
5. `on_natural_stop(history)`：`notes_enabled` 为 False 或 in-flight 已置 → 返回；置标志，起 daemon 线程执行 `_update_notes(snapshot)`（`history[_note_watermark:]` 的浅拷贝，随后水位推到当前长度）。
6. `_update_notes(new_msgs)`：build 请求 → `provider.stream_chat(..., tools=None)` 收集全文 → parse → 按 scope 分组 → 逐目录 `try_acquire(dir/".lock", 600)`，拿不到整组跳过；拿到后执行动作（add/update 写 `render_note`、delete 删文件）→ 扫描目录全部 `.md`（除索引）`parse_note` 重建索引写盘 → finally `release`；有变更时 `notify(...)`；异常记 `_last_note_result`；finally 清 in-flight 标志。
7. `resume_list()`：渲染编号列表（含 locked 标注）；`resume_into(key, history)`：编号或 ID 定位 → attach（False 时返回占用提示）→ load → `history[:] = messages` → 超 24h 登记 pending 提醒 → 返回结果文本（含跳过坏行/丢组统计）；同时更新记忆高水位为新历史长度。
8. `on_clear()`：release 旧锁 + `start_new()` + 高水位归零；`memory_report()`：汇总各层/索引/记忆数/最近结果/会话/锁状态；`touch_session_lock()`；`close()`。
9. `__init__.py` 导出 `MemoryManager`。

**验证：** `python -m compileall rhinecode/memory` 编译通过。

## T11: 编排测试（含 note_updater 解析）

**文件：** `tests/test_memory_manager.py`（新建）
**依赖：** T10
**步骤：** 用假 provider（预置响应、断言收到 `tools=None`）与临时目录断言：
1. `parse_note_response`：合法数组解析、坏 JSON→[]、非法 filename/scope/category 项被跳过。
2. 自然停止流程：假 provider 返回含 add 动作的 JSON → 对应目录出现记忆文件、索引重建含该条、notify 被调用、`_last_note_result` 为成功。
3. 目录锁被占（预置新鲜 .lock）→ 该目录无新文件、不阻塞（同步调 `_update_notes` 验证）。
4. 假 provider 抛异常 → 静默、`_last_note_result` 记录失败、in-flight 标志被清。
5. in-flight 已置时 `on_natural_stop` 直接返回（假线程不启动）。
6. 高水位：两次更新，第二次请求只含新增消息。
7. `startup(resume_latest=True)`：最近会话被锁 → 顺延到下一个；全被锁/无会话 → 开新档。
8. `memory_report()` 含 RHINE.md 层状态、记忆数、会话 ID、锁状态字样。
9. `memory_index()`：构造 >200 行索引文件 → 返回内容 ≤200 行。

**验证：** `python -m unittest tests.test_memory_manager` 全绿。

## T12: 沙箱白名单（path_guard + read_file + engine）

**文件：** `rhinecode/tools/path_guard.py`、`rhinecode/tools/read_file.py`、`rhinecode/permission/engine.py`、`tests/test_memory_sandbox.py`
**依赖：** 无（可与 T1–T11 并行）
**步骤：**
1. path_guard：模块级 `_EXTRA_READ_ROOTS: list[Path]` + `register_read_root(path)`（resolve 后登记，重复忽略）+ `clear_read_roots()`（测试用）；`resolve_readable(path) -> Path`：先试 `resolve_in_workspace`，`PathGuardError` 时若 resolve 后落在任一白名单根内则放行（此分支允许绝对路径但仍拒 `..`），否则重抛；`is_readable_path(path) -> bool` 布尔版。
2. read_file：`resolve_in_workspace` 调用点改为 `resolve_readable`。
3. permission/engine.py：②沙箱层中 `kind == "read_path"` 的判定改用 `is_readable_path`，其余 kind 不动。
4. 测试：注册临时目录为 read root 后——read_file 可读其中文件；write/edit 该目录仍被拒；未注册的工作区外路径读仍被拒；glob/grep 的 path_filter 行为不变；`clear_read_roots` 后恢复原状。

**验证：** `python -m unittest tests.test_memory_sandbox` 全绿，且既有 `tests.test_perm_*` 全绿（沙箱行为未回归）。

## T13: 系统提示槽位接线（builder）

**文件：** `rhinecode/agent/prompt/builder.py`
**依赖：** 无
**步骤：**
1. `build_default_prompt(env, custom_instructions: str = "", memory_index: str = "") -> AssembledPrompt`：两个新参数非空时，构造 `PromptModule(name="自定义指令", priority=110, cacheable=True, content=...)` 与 `PromptModule(name="长期记忆", priority=130, cacheable=True, content=memory_index 包裹说明头)`，替代 `optional_slots()` 中对应空槽（「已激活 Skill」空槽保留原样）。
2. 注释说明为何覆盖 c5 的 cacheable=False 预设（见 plan 技术决策：dynamic 通道每轮重发不缓存，两块内容体积大、会话内稳定，进 stable 尾部更省）。

**验证：** `python -m compileall rhinecode` 通过；`python -m unittest discover -s tests` 既有用例全绿（缺省参数行为不变）。

## T14: 循环消息记录回调（loop）

**文件：** `rhinecode/agent/loop.py`
**依赖：** 无
**步骤：**
1. `run(...)` 末尾新增 `recorder: Optional[Callable[[Message], None]] = None` 参数。
2. 三处 `history.append(...)`（自然完成的 assistant、带 tool_calls 的 assistant、每条 tool 结果）之后各加 `if recorder is not None: recorder(msg)`，包 try/except 静默（记录失败不影响循环，N2）。

**验证：** `python -m unittest discover -s tests` 既有用例全绿（recorder 缺省 None 行为不变）。

## T15: /init 内置指令文本

**文件：** `rhinecode/agent/prompt/texts/init.py`（新建）
**依赖：** 无
**步骤：** 定义 `INIT_PROMPT` 常量：要求模型探索项目（glob/grep/read）总结结构、技术栈、构建/测试命令、代码约定；若项目根已存在 `RHINE.md` 则读取后仅输出改进建议、不覆盖；否则用 `write_file` 写项目根 `RHINE.md`（≤200 行）。

**验证：** `python -m compileall rhinecode` 通过。

## T16: 协调层接入（conversation）

**文件：** `rhinecode/conversation.py`
**依赖：** T10 T13 T14 T15
**步骤：**
1. `__init__` 新增 `resume_latest: bool = False` 参数；构造 `MemoryManager(provider, config.model, workspace_root(), Path.home()/".rhinecode", notes_enabled=self._tools_enabled)`；调 `startup(resume_latest, self.history)` 把返回提示存 `self.startup_notice`；工具模式且恢复了历史时 `context_manager` 无需特殊处理（锚点本就为空）。
2. 注册用户级 memory 目录：`path_guard.register_read_root(user_dir/"memory")`（仅工具模式需要，但注册无副作用可无条件做）。
3. `handle_input`：追加用户消息处补 `self._memory.record_message(msg)`；新增 `/memory`（同步返回 `memory_report()`）、`/resume`（无参同步列表；带参返回 `_resume_stream(key)` 事件流：`resume_into` → 成功且有 context_manager 时 `reset()` + `before_request` 产 NOTICE → NOTICE(结果) + FINISHED）、`/init`（非工具模式返回不支持；否则把 INIT_PROMPT 作为 user 消息追加（含 record）后走 `_run()`）。
4. `_run()`：`build_default_prompt(env, custom_instructions=..., memory_index=...)`；`dynamic` 拼接 `consume_pending_notice()`；`agent.run(..., recorder=self._memory.record_message)`；返回值包 `_wrap_events(events)` 生成器——逐个 yield，见 `FINISHED` 且 `stop_reason == COMPLETED` 时先调 `self._memory.on_natural_stop(self.history)` 再 yield。
5. `clear()` 补 `self._memory.on_clear()`；暴露 `self.memory_manager` 属性供 TUI（心跳/notify 注入）。

**验证：** `python -m compileall rhinecode` 通过；既有测试全绿。

## T17: 入口接线（__main__）

**文件：** `rhinecode/__main__.py`
**依赖：** T16
**步骤：**
1. argparse 新增 `--continue`（`action="store_true"`，`dest="continue_session"`——`continue` 是 Python 关键字）。
2. `ConversationManager(..., resume_latest=args.continue_session)`。
3. `finally` 中 `manager.memory_manager.close()`（与 `mcp_manager.close_all()` 并列，各自 try 住互不影响）。

**验证：** `python -m compileall rhinecode` 通过；`rhine --help` 显示 `--continue`。

## T18: TUI 接线（widgets + app）

**文件：** `rhinecode/tui/widgets.py`、`rhinecode/tui/app.py`
**依赖：** T16
**步骤：**
1. widgets：`CommandPanel.COMMANDS` 追加 `("/resume", "恢复历史会话")`、`("/memory", "查看记忆系统状态")`、`("/init", "分析项目生成 RHINE.md")`。
2. app：挂载时若 `manager.startup_notice` 非空则作为系统提示行显示；注入 `manager.memory_manager.notify = <闭包>`（内部 `call_from_thread` 往历史区加系统提示行，注意转义字面 `[`）；`set_interval(120, ...)` 定时调 `touch_session_lock()`。
3. 检查提交处理的状态栏刷新白名单：本章命令不改状态栏字段，无需加入；确认 `/init` 走普通消息路径（事件流）而非命令短路。

**验证：** `python -m compileall rhinecode` 通过；tmux 启动冒烟——输入 `/` 补全面板出现三个新命令，`/memory` 有输出。

## T19: 全量回归

**文件：** 无（运行验证）
**依赖：** T1–T18
**步骤：** `python -m compileall rhinecode tests` → `python -m unittest discover -s tests`；失败则修复后重跑至全绿。

**验证：** 两条命令全部通过，无失败用例。

## T20: 文档更新

**文件：** `CLAUDE.md`
**依赖：** T19
**步骤：**
1. 「当前能力」补 C9 段（三套机制 + 锁）；「架构」补 Memory 层条目与 conversation/__main__/tui 的接入描述；「常用命令」补 `--continue` 与 `/resume` `/memory` `/init`；「测试」补五个新测试文件；「安全边界」补：会话存档/记忆含对话原文勿提交、用户级 memory 只读白名单、记忆内部可信写盘。
2. 「成对维护点」备忘补一行：新增 RHINE.md 相关行为 → instructions.py + /memory 报告。
3. 确认 `.gitignore` 覆盖 `.rhinecode/`（sessions/memory 随之忽略），不足则补。

**验证：** 通读 diff 与 spec/plan 一致；`git diff --stat` 确认只动预期文件。

## 执行顺序

```
T1 → T2 ──┐
T3 → T4 ──┤
T5 → T6 ──┼→ T9 → T10 → T11 ──┐
T1 → T7 → T8 ─┘               │
T12（独立，可并行）────────────┼→ T16 → T17 → T19 → T20
T13 / T14 / T15（独立，可并行）┘        T18 ─┘
```
