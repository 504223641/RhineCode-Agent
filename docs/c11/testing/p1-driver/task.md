# TUI 驱动器 P1a（交互闭环）Tasks

> 对应 `docs/c11/testing/p1-driver/spec.md`（F1–F26 / N1–N9 / AC1–AC43）与
> `docs/c11/testing/p1-driver/plan.md`（均已审批）。本文回答「按什么顺序做」。
>
> **共 72 个任务，分七段。每段开头写明「本段完成后可验哪些 AC」**——
> 这是防止验收标准在实现阶段被漏掉的结构性措施（沿用 P0 `task.md` 的做法）。
> 每段做完先跑该段的门槛命令，全绿再进下一段。
>
> **纪律**：
> - 每个任务或每组逻辑相关的任务完成后立即 `git commit`（项目既定规矩）。
> - **所有新增与修改的代码按 CLAUDE.md 的《代码注释规范》写中文注释**（spec N8）：
>   解释「为什么这么做」与「这段代码承担什么职责」，复杂函数写清参数、返回值、
>   主要步骤、失败情况与副作用。凡本文标注「注释写明」的地方是硬要求，不可省。
> - 遇到与本文描述不符的代码事实，**先核实再动手**，不要照着文档硬写。
> - 任何「跑不通就放宽断言 / 加重试」的念头都违反 spec N7，直接停下来问。

---

## 文件清单

### 新建（测试设施）

| 操作 | 文件 | 职责 |
| --- | --- | --- |
| 新建 | `tests/__init__.py` | 只允许 `#` 注释，**不放任何 import 与 docstring**；使 `python -m tests.e2e.*` 可用 |
| 新建 | `tests/e2e/__init__.py` | 同上；放重量级导入会给全量测试加装配开销 |
| 新建 | `tests/e2e/protocol.py` | 线上格式、错误码、决策取值表、`via` 取值（纯数据零 IO） |
| 新建 | `tests/e2e/discovery.py` | 发布文件读写扫描 + 两级陈旧判定 |
| 新建 | `tests/e2e/sandbox.py` | 临时目录创建 + 可丢弃校验 + 三步清理 |
| 新建 | `tests/e2e/seeding.py` | 工作区与用户级目录预置 |
| 新建 | `tests/e2e/fingerprint.py` | 代码版本标识 |
| 新建 | `tests/e2e/scripted.py` | `ScriptedProvider` + `RecordedCall` + 数据块构造器 |
| 新建 | `tests/e2e/control.py` | `run_on_main` / `Responder` / `DriverCore` / 四条不变量 |
| 新建 | `tests/e2e/host.py` | 宿主进程入口 + `ControlServer` + 退出编排 |
| 新建 | `tests/e2e/client.py` | 瘦客户端入口 |
| 新建 | `tests/e2e/assertions.py` | `TraceView` + 十一项词汇 + 失败诊断 |

### 新建（测试）

| 操作 | 文件 | 覆盖 |
| --- | --- | --- |
| 新建 | `tests/test_e2e_protocol.py` | 编解码、错误码、取值表、UTF-8 往返 |
| 新建 | `tests/test_e2e_discovery.py` | 发布文件、多宿主、两级陈旧判定 |
| 新建 | `tests/test_e2e_sandbox_seed.py` | 可丢弃校验两面、清理三步、预置生效、指纹 |
| 新建 | `tests/test_e2e_scripted.py` | 四类块、按轮次、耗尽兜底、`RecordedCall` 取值 |
| 新建 | `tests/test_e2e_assertions.py` | 十一项词汇、失败诊断含序号、复用 reader |
| 新建 | `tests/test_e2e_control.py` | 判据、等待、四类应答、取消、超时诊断、强制结算、死锁护栏 |
| 新建 | `tests/test_e2e_host.py` | 闭环、观察面、退出、超时、致命错误、权限面、注入面、隔离 |
| 新建 | `tests/test_e2e_live.py` | 真实模式（默认 skip） |

### 修改

| 操作 | 文件 | 改动 |
| --- | --- | --- |
| 修改 | `rhinecode/bootstrap.py` | `build_app` 增 `provider_factory` / `exclude_tools`，并透传前者 |
| 修改 | `rhinecode/conversation.py` | 构造函数增 `provider_factory`；`_provider_for` 改用它 |
| 修改 | `rhinecode/tui/app.py` | 来源字段可传入 + `_settle_session` 收拢两处结算 |
| 修改 | `CLAUDE.md` / `AGENTS.md` / `README.md` | 架构节 + 成对维护点 + 安全边界 + 常用命令 |
| 修改 | `docs/c11/testing/p1-driver/spec.md` | 末节补 P1b 可复用接缝与 P1a 的实际偏离 |

---

# 第一段：纯逻辑基础（T1–T13）

> **本段完成后可验**：AC18（沙箱校验两面）、AC10 的单元级一半。
> 本段全部不依赖 Textual、不依赖装配，可独立跑。

## T1: 建立测试包骨架
**文件：** `tests/__init__.py`、`tests/e2e/__init__.py`　**依赖：** 无
**步骤：**
1. 两个文件里**只允许 `#` 注释**，不放 import、不放 docstring
   （`tests/e2e/__init__.py` 会被 `unittest discover` 导入，任何重量级导入都会给
   全量测试加开销甚至副作用）。
2. 各写一行注释说明「刻意留空，理由见 plan §5」。

**验证：** `python -m unittest discover -s tests 2>&1 | tail -3` 仍为 `Ran 714 tests ... OK`

## T2: 协议编解码与错误码
**文件：** `tests/e2e/protocol.py`　**依赖：** T1
**步骤：**
1. `ERROR_CODES` 常量集合：`bad_request` / `starting` / `not_pending` / `busy` /
   `timeout` / `turn_budget` / `fatal` / `shutting_down` / `internal`。
2. `encode(obj) -> bytes`：`json.dumps(ensure_ascii=False)` + `"\n"`，
   **显式 `.encode("utf-8")`**（N9）。
3. `decode(line: bytes) -> dict`：显式 utf-8 解码 + `json.loads`，非法 JSON 不吞。
4. `ok(data)` / `err(code, message, data=None)`；`err` 的 `code` 不在 `ERROR_CODES`
   内时抛 `ValueError`（防止拼错码字导致客户端分支静默失效）。

**验证：**
```
python -c "from tests.e2e import protocol as p; print(p.decode(p.encode({'a':'中文','b':'[dim]x[/dim]'})))"
```
输出与输入字典相等。

## T3: 面板决策取值表
**文件：** `tests/e2e/protocol.py`　**依赖：** T2
**步骤：**
1. `CHOICE_TABLE`：`confirm` → `{once, session, permanent, deny}`；
   `approve` → `{yes, no}`；`clarify` → 数字字符串（用正则判定）；
   `session` → 任意会话标识或字面 `cancel`。
2. `VIA_VALUES = {"channel", "keys"}`，注释写明 `keys` 只对
   `confirm` / `approve` / `session` 有效——`ClarifyPanel` 的候选项之间夹着
   disabled 详情行，按键次数无法从选项下标稳定推出。
3. `validate_choice(kind, choice) -> Optional[str]`：返回错误消息或 None。
4. `validate_via(kind, via) -> Optional[str]`：`clarify` + `keys` 组合直接拒绝。

**验证：** T4 覆盖（下一步即跑）

## T4: 协议测试
**文件：** `tests/test_e2e_protocol.py`　**依赖：** T3
**步骤：**
1. 编解码往返：含中文、含字面 `[dim]` 标记的字符串原样往返；结果以 `\n` 结尾且为 `bytes`。
2. `ok` / `err` 结构；`err` 用越界 code 抛 `ValueError`。
3. `validate_choice` 四种面板各一条合法、各一条非法。
4. `validate_via`：`clarify` + `keys` 被拒。
5. 非法 JSON 交给 `decode` 抛 `json.JSONDecodeError`。

**验证：** `python -m unittest tests.test_e2e_protocol -v` 全绿

## T5: 发布文件写入
**文件：** `tests/e2e/discovery.py`　**依赖：** T2
**步骤：**
1. `HostInfo` 冻结数据类，八字段：`pid` / `port` / `workspace` / `user_dir` /
   `trace_path` / `fingerprint` / `mode` / `started_at`。
2. `publish_dir() -> Path` = `Path(tempfile.gettempdir()) / "rhinecode-e2e"`，
   **不在此建目录**（建目录放 `publish`，便于测试打桩）。
3. `publish(info) -> Path`：`mkdir(parents=True, exist_ok=True)` 后写
   `host-<pid>.json`，显式 UTF-8。目录建不了时**抛 `OSError` 不吞**——
   没有发布文件谁都找不到宿主，继续跑毫无意义。

**验证：**
```
python -c "import tempfile,pathlib; from tests.e2e.discovery import *; \
i=HostInfo(1,2,'w','u','t','f','scripted',0.0); p=publish(i); print(p.exists(), p.read_text('utf-8')[:40])"
```
输出 `True` 与可读 JSON 片段；跑完手工删掉该文件。

## T6: 发布文件扫描与解析
**文件：** `tests/e2e/discovery.py`　**依赖：** T5
**步骤：**
1. `list_hosts() -> list[HostInfo]`：扫 `host-*.json`，**坏文件跳过不报错**
   （残留的半截文件不该让排障命令崩掉）。
2. `unpublish(pid) -> bool`：删除对应文件，不存在返回 False（幂等）。
3. `resolve_host(pid=None) -> HostInfo`：`pid` 给定则精确取；不给定时
   0 个抛「没有正在运行的宿主」、多个抛「有 N 个宿主，请用 --pid 指定」。
   **异常消息即最终用户可见文案**，要写得能照做。

**验证：** T8 覆盖

## T7: 两级陈旧判定
**文件：** `tests/e2e/discovery.py`　**依赖：** T6
**步骤：**
1. 自定义 `StaleHostError`。
2. `connect(info, timeout) -> socket`：`ConnectionRefusedError` / `OSError` 时抛
   `StaleHostError`，消息含发布文件路径与 pid，并提示
   「用 `python -m tests.e2e.client hosts` 查看、手工删除该文件」。
3. `verify_pid(info, status_data)`：`status_data["pid"]` 与 `info.pid` 不一致时抛
   `StaleHostError`，消息写明「端口疑似被其它进程复用」。
4. 注释写明**不做进程存活检测**的理由：跨平台麻烦且不必要，端口只在宿主活着时被监听。

**验证：** T8 覆盖

## T8: 发布与陈旧判定测试
**文件：** `tests/test_e2e_discovery.py`　**依赖：** T7
**步骤：**
1. `unittest.mock.patch` 把 `publish_dir` 指到临时目录（不污染真实临时目录）。
2. 写入 → 扫描 → 解析往返；`unpublish` 幂等。
3. 目录里放一个坏 JSON，`list_hosts` 跳过且不抛。
4. `resolve_host()` 在 0 / 1 / 2 个时的三种行为，断言异常消息含关键提示词。
5. **AC10 的单元级一半**：造一份指向未被监听端口的发布文件，断言 `connect` **立即**抛
   `StaleHostError`——用 `time.monotonic` 断言耗时 < 1 秒（验「不超时等待」）。
6. `verify_pid` 在 pid 不一致时抛错。

**验证：** `python -m unittest tests.test_e2e_discovery -v` 全绿

## T9: 沙箱创建与可丢弃校验
**文件：** `tests/e2e/sandbox.py`　**依赖：** T1
**步骤：**
1. 常量 `MARKER = ".rhine-e2e-workspace"`。
2. `create_workspace(prefix="rhine_e2e_ws_") -> Path`：`tempfile.mkdtemp` 后写标记文件
   （内容含创建时间与创建者 pid，便于排障）。
3. `create_user_dir(prefix="rhine_e2e_user_") -> Path`：同上。
4. `assert_disposable(path)`：两条件——① `path.resolve()` 在
   `Path(tempfile.gettempdir()).resolve()` 之下；② 目录内存在标记文件。
   不满足抛 `NotDisposableError`，消息写明是哪一条不满足。
   **注释写明：与「目录是否为空」完全无关**——预置之后非空是常态（spec AC18 两条一起验）。

**验证：**
```
python -c "from tests.e2e.sandbox import *; import pathlib; \
w=create_workspace(); assert_disposable(w); print('temp ok', w); \
import shutil; shutil.rmtree(w)"
python -c "from tests.e2e.sandbox import *; import pathlib; \
try: assert_disposable(pathlib.Path('G:/RhineCode-Agent'))
except Exception as e: print('repo rejected:', e)"
```

## T10: 三步清理
**文件：** `tests/e2e/sandbox.py`　**依赖：** T9
**步骤：**
1. `cleanup_workspace(path, *, previous_cwd)`：**顺序不可调**——
   ① 调用方须先跑过 `build_app` 的 `cleanup`（关文件句柄）；② `os.chdir(previous_cwd)`；
   ③ `shutil.rmtree(path)`。
2. docstring 写清 Windows 实测理由：cwd 位于待删目录内时 `rmtree` 抛
   `PermissionError [WinError 32] 另一个程序正在使用此文件`。
3. **`rmtree` 不用 `ignore_errors=True`**——删不掉要暴露（N7），
   否则 AC24 的目录占用护栏形同虚设。

**验证：** T13 覆盖（含 Windows 条件用例）

## T11: 预置函数组
**文件：** `tests/e2e/seeding.py`　**依赖：** T9
**步骤：**
1. `seed_files(root, mapping)`：按相对路径写文件，父目录自动建，显式 UTF-8。
2. `seed_git_repo(root, commits)`：`git init` + `git config user.name/email`
   （**局部配置，不动全局**）+ 逐条 `git add . && git commit`。
   `subprocess.run` 显式 `cwd` / `encoding="utf-8"` / `check=True`；
   git 不存在时把 `FileNotFoundError` 包装成含「本设施需要 git」的异常，
   **不静默跳过**（静默跳过会让依赖提交历史的场景假绿）。
3. `seed_project_skill(root, name, frontmatter, body)` → `<root>/.rhinecode/skills/<name>.md`
4. `seed_user_skill(user_dir, name, frontmatter, body)` → `<user_dir>/skills/<name>.md`
5. `seed_permissions(target_dir, allow=(), deny=())` → `<target_dir>/permissions.yaml`
   （参数收的是**最终目录**——项目级要落在 `.rhinecode/` 下，用户级直接在 `user_dir` 下，
   两者路径规则不同，由调用方决定）。
6. `seed_rhine_md(root, text)` → `<root>/RHINE.md`
7. **模块 docstring 写明**：预置 Skill 的 `allowed_tools` 不得写
   `mcp_add_server` / `mcp_resolve_server`（宿主会摘掉它们，见 plan §3.10）。

**验证：** T13 覆盖

## T12: 代码版本标识
**文件：** `tests/e2e/fingerprint.py`　**依赖：** T1
**步骤：**
1. `compute(package_root) -> str`：遍历 `**/*.py`（跳过 `__pycache__`），
   按相对 POSIX 路径排序，`(相对路径, st_size, st_mtime_ns)` 逐个喂
   `hashlib.sha256`，返回 `hexdigest()[:12]`。
2. 注释写清取舍与**已知误报**：`pip install -e .`、切分支会改 mtime 而内容未变，
   于是会误报「代码变了」；误报只让人多重启一次宿主，漏报会让人以为改动生效了——
   后者是本条需求存在的全部理由。

**验证：** T13 覆盖

## T13: 沙箱 / 预置 / 指纹测试
**文件：** `tests/test_e2e_sandbox_seed.py`　**依赖：** T10、T11、T12
**步骤：**
1. **AC18**：临时目录通过；项目根抛 `NotDisposableError`；
   临时目录内**有预置内容**时仍通过（「与是否为空解耦」）。
2. 清理三步：chdir 进工作区后 `cleanup_workspace` 能删掉；
   直接 `rmtree` 在 Windows 下失败（`skipUnless(sys.platform == "win32")`）。
3. 预置：`seed_files` / `seed_project_skill` / `seed_user_skill` / `seed_rhine_md` /
   `seed_permissions` 各断言落盘位置与内容；`seed_git_repo` 断言 `git log --oneline`
   能读到提交（git 缺失时 `skipTest` 并说明——**环境前置已登记进 checklist**）。
4. 指纹：连算两次相同；`touch` 一个 `.py` 后值改变。

**验证：** `python -m unittest tests.test_e2e_sandbox_seed -v` 全绿

---

# 第二段：产品侧三处接缝（T14–T20）

> **本段完成后可验**：AC29（注入参数可选）。
> 本段每一步都以「全量仍 714 全绿」为硬门槛。可与第一段并行。

## T14: `build_app` 增两个可选参数
**文件：** `rhinecode/bootstrap.py`　**依赖：** 无
**步骤：**
1. 签名增 `provider_factory: Optional[Callable[[Config], BaseProvider]] = None` 与
   `exclude_tools: frozenset[str] = frozenset()`，两者都在 `*` 之后
   （既有 14 处调用点全用关键字参数，不会撞位）。
2. 第②步改为 `factory = provider_factory or create_provider`；
   `ValueError` 的捕获与 `BootstrapError` 文案**一字不改**（那是不可变契约）。
3. docstring 补两个参数说明，写明「均可选、缺省等于现状」是硬纪律
   （与 `user_dir` / `recorder` 同口径）。

**验证：** `python -m unittest tests.test_bootstrap tests.test_trace_zero_regression -v` 全绿

## T15: `exclude_tools` 的摘除位置
**文件：** `rhinecode/bootstrap.py`　**依赖：** T14
**步骤：**
1. 在 `fatal_tool_names = skill_manager.startup(known_tools)` 的 `raise` 之**后**、
   `mcp_manager.connect_all(...)` 之**前**，插入
   `for name in sorted(exclude_tools): tool_registry.unregister(name)`。
2. **注释必须写明为什么卡在这个窄窗口**（与 C11 那段「两头都不能挪」并列）：
   往前挪会让一个白名单写了 `mcp_add_server` 的**合法** Skill 被判成笔误而 fail-fast
   （实测 `fatals = [('addmcp','mcp_add_server')]`）；放在 `startup` 之后则白名单校验
   口径与真实启动逐字一致，被摘掉的名字由 Skill 的运行期工具交集自然剔除
   （实测 `tool_policy.allowed = ['read_file']`）。位置仍在 `session_start` 快照之前，
   快照与实际工具集一致。

**验证：** `python -m unittest tests.test_bootstrap tests.test_skill_startup -v` 全绿

## T16: `ConversationManager` 的 `provider_factory`
**文件：** `rhinecode/conversation.py`　**依赖：** 无
**步骤：**
1. 构造函数增 `provider_factory: Optional[Callable[[Config], BaseProvider]] = None`，
   存 `self._provider_factory = provider_factory or create_provider`。
2. `_provider_for` 里把 `create_provider(dataclasses.replace(self._config, model=model))`
   改为 `self._provider_factory(dataclasses.replace(...))`。
   **随后的 `TracingProvider` 包装逻辑不动**。
3. docstring 写明：这是**换模型旁路**的注入点，不透传则该旁路会绕过假模型
   **静默连上真实网络**（与 trace P0 的记录旁路是同一处，spec F21 点名「最容易漏」）。

**验证：** `python -m unittest tests.test_skill_isolated -v` 全绿

## T17: `build_app` 透传 `provider_factory`
**文件：** `rhinecode/bootstrap.py`　**依赖：** T14、T16
**步骤：**
1. 构造 `ConversationManager` 时加 `provider_factory=provider_factory`。
2. 注释一句：不透传则 F21 不成立——`_provider_for` 在协调层，`build_app` 里那层管不到。

**验证：** `python -m unittest discover -s tests 2>&1 | tail -3` 仍 714 全绿

## T18: 交互来源字段可传入
**文件：** `rhinecode/tui/app.py`　**依赖：** 无
**步骤：**
1. `_interact` 的待决盒增 `"source": "human"` 键；埋点处的字面量 `"source": "human"`
   改为读 `box["source"]`。
2. `_resolve_interaction(self, result, source: str = "human")`：写 `box["result"]`
   的同时写 `box["source"] = source`。
   （`box` 是局部变量，`self._pending_interaction = None` 已在其前执行，不受影响。）
3. **点名另外两个调用方**：`on_confirm_panel_cancelled` 与 `on_clarify_panel_cancelled`
   也调 `_resolve_interaction`，靠默认值 `"human"` 兜住——这是对的，
   但改的人容易以为只有一处调用方，注释里提一句。
4. 注释写明取值集合：`human`（面板按键路径，含模拟按键）/ `driver`（控制通道决策）/
   `driver_forced`（退出时强制结算）/ `policy`（P1b 预留），
   并说明「来源标注的是**结算走的哪条路径**，不是对操作者身份的断言」。

**验证：** `python -m unittest tests.test_perm_loop tests.test_skill_tui -v` 全绿

## T19: 会话面板结算收拢
**文件：** `rhinecode/tui/app.py`　**依赖：** T18
**步骤：**
1. 新增 `_settle_session(self, session_id: Optional[str], source: str = "human")`：
   **第一行是幂等守卫** `if not self._session_panel_active: return`
   （缺了它，退出时的强制结算会在没有面板时凭空多埋一条交互事件，
   破坏 trace 的「四类面板各一条」口径）；随后埋 `INTERACTION` 事件
   （`result` 为 `selected` / `cancelled`）→ `_close_session_panel()` →
   有 id 则 `resume_session(id)`。
2. `on_option_list_option_selected` 的 SessionPanel 分支塌缩为 `event.stop()` + 一行调用；
   **保留**那条「必须放在 `box is None` 守卫之前」的注释。
3. `on_session_panel_cancelled` 塌缩为一行调用。

**验证：** `python -m unittest tests.test_resume_replay tests.test_trace_hooks -v` 全绿

## T20: 第二段全量门槛
**文件：** —　**依赖：** T15、T17、T19
**步骤：** 跑全量，确认既有 714 条**一条不少**、断言语义未弱化。

**验证：** `python -m unittest discover -s tests 2>&1 | tail -3` 为 `Ran 714 tests ... OK`

---

# 第三段：脚本化假模型（T21–T24）

> **本段完成后可验**：AC32 的单元级一半、AC33 的单元级一半。

## T21: 数据块构造器
**文件：** `tests/e2e/scripted.py`　**依赖：** T1
**步骤：**
1. 六个模块级函数返回 `StreamChunk`：`text(s)` / `thinking(s)` /
   `tool(name, args, call_id=None)`（内部造 `ToolCall`，`call_id` 缺省自增）/
   `stream_error(msg)` / `usage(prompt, completion)` / `done()`。
2. 常量 `FALLBACK_MARKER = "[e2e-fallback]"` 并注释用途：上下文摘要与自动笔记会额外
   调模型，读记录时一眼能认出「这条不是脚本里写的」。

**验证：** `python -c "from tests.e2e.scripted import *; c=tool('read_file',{'p':1}); print(c.type, c.tool_call.name)"`

## T22: `RecordedCall`
**文件：** `tests/e2e/scripted.py`　**依赖：** T21
**步骤：**
1. 冻结数据类五字段：`index` / `messages` / `system` / `tools` / `thinking_effort`。
2. `tool_names` 属性：从 `tools` 取 `function.name` 组成集合；`tools` 为 None 返回空集。
3. `dynamic_reminder` 属性：`messages[-1]` 若 `role == "system"` 返回其 `content`，
   否则空串。**注释必须写清**：已激活的 Skill 正文在这里，**不在 `system` 参数里**
   （`loop.py` 把动态提醒拼成一条 system 消息追加到历史末尾）——
   混作一谈会写出永远失败的断言。

**验证：** T24 覆盖

## T23: `ScriptedProvider`
**文件：** `tests/e2e/scripted.py`　**依赖：** T22
**步骤：**
1. 继承 `BaseProvider`；`__init__(turns, fallback=None)`，`fallback` 缺省
   `[text(FALLBACK_MARKER), done()]`。
2. `stream_chat`：先在**独立锁**的临界区内 `append` 一条 `RecordedCall`
   （临界区只做 append，不做任何调度——N6），出锁后按 `index` 取脚本、
   耗尽用 `fallback`，逐块 `yield`。
3. `calls` 暴露为只读属性（返回列表副本，避免调用方误改）。

**验证：** T24 覆盖

## T24: 假模型测试
**文件：** `tests/test_e2e_scripted.py`　**依赖：** T23
**步骤：**
1. 四类数据块各构造一次并断言 `type` 与载荷。
2. 按轮次：两轮脚本，连调两次得到不同块序列。
3. 耗尽兜底：第三次调用返回含 `FALLBACK_MARKER` 的文本，**不抛错、不挂起**。
4. `tool_names` 与传入 schema 一致；`dynamic_reminder` 在「末条是 system」与
   「末条不是 system」两种情形下的取值。
5. 并发：起若干线程各调若干次，断言 `len(calls)` **精确等于**总次数（无丢失、无撕裂）。

**验证：** `python -m unittest tests.test_e2e_scripted -v` 全绿

---

# 第四段：断言层（T25–T31）

> **本段完成后可验**：AC35、AC36、AC37、AC38。

## T25: `TraceView`
**文件：** `tests/e2e/assertions.py`　**依赖：** T1
**步骤：**
1. `TraceView.load(path)`：**必须调 `rhinecode.trace.reader.load_records`**，
   不得自行 `json.loads`（F24 硬要求，T30 会用源码扫描做护栏）。存 `records` 与 `skipped`。
2. `of_type(*types)` / `in_scope(*scopes)`：**必须调 `reader.filter_records`**。
3. `by_seq(seq)` / `nth(type_, n)`：找不到时抛含可读消息的 `LookupError`。

**验证：** T30 覆盖

## T26: `CheckResult` 与失败诊断
**文件：** `tests/e2e/assertions.py`　**依赖：** T25
**步骤：**
1. `CheckResult` 冻结数据类：`ok` / `message` / `evidence_seqs`。
2. `assert_check(result, view)`：`ok` 为真则返回；否则抛 `AssertionError`，
   消息 = `result.message` + 换行 + 每个证据序号**前后各 3 条**事件的整行。
3. **整行必须用 `reader.render_timeline`，不是 `reader.summarize`**——
   后者只返回一句话摘要，`<seq> <ts> <scope> <type>` 前缀由 `render_timeline` 产出
   （源码实证：`reader.py` 里 `summarize` 与 `render_timeline` 是两个函数）。
4. 邻域去重并按 `seq` 排序，避免多个证据的邻域重叠时刷屏。

**验证：** T30 覆盖

## T27: 断言词汇 ②–⑧（取自记录）
**文件：** `tests/e2e/assertions.py`　**依赖：** T26
**步骤：**
1. `check_tool_outcome(view, tool_name, expected)` —— ②
2. `check_ui_contains(view, text)` —— ③
3. `check_status_bar(view, text, *, present=True)` —— ④（含与不含两用）
4. `check_permission(view, tool_name, layer=None, decision=None)` —— ⑤
5. `check_scope(view, seq, expected_scope)` —— ⑥
6. `check_order(view, seq_before, seq_after)` —— ⑦
7. `check_count(view, type_, expected)` —— ⑧
8. 每个函数失败时都要填 `evidence_seqs`，否则 F26 落空。

**验证：** T30 覆盖

## T28: 断言词汇 ①⑩⑪（取自假模型）
**文件：** `tests/e2e/assertions.py`　**依赖：** T26
**步骤：**
1. `check_tools_offered(provider, turn, expected: set[str])` —— ①
2. `check_stable_prompt(provider, turn, text, *, present=True)` —— ⑩
3. `check_dynamic_reminder(provider, turn, text, *, present=True)` —— ⑪
4. 三者 `evidence_seqs` 为空（来源不是记录），但 `message` 要带「第 N 轮」与实际取到的
   集合/片段，保证仍可诊断。
5. 模块注释写明**权威来源裁决**（plan §2.8 的表）：为什么这三项不取记录
   （记录受截断；稳定段与动态段分处请求的两个位置）。

**验证：** T30 覆盖

## T29: 断言词汇 ⑨（取自会话存档）
**文件：** `tests/e2e/assertions.py`　**依赖：** T26
**步骤：**
1. `check_history_len(sessions_dir, expected, *, role=None)`：找最新的 `.jsonl`，
   逐行 `json.loads`，按 `role` 过滤后计数。
   （**本函数是 `assertions.py` 里唯一允许出现 `json.loads` 的地方**——它读的是会话存档
   不是记录；T30 的源码护栏要把它排除在外，见下。）
2. 坏行跳过（与会话存档自身的容错口径一致：JSON 解析失败 / 非对象 / role 非法均跳过）。
3. 注释写明：十五类事件中没有任何一类承载「当前主历史多少条」，
   记录里的请求消息列表受条数与长度双重截断，故只能取存档。

**验证：** T30 覆盖

## T30: 断言层测试
**文件：** `tests/test_e2e_assertions.py`　**依赖：** T27、T28、T29
**步骤：**
1. **AC36**：手工造一份小记录文件（含各类事件各若干条），十一个 check 函数逐个
   各断言一次通过、一次失败。
2. **AC37**：断言 `AssertionError` 消息里**含证据序号**（正则匹配）且含邻域整行；
   再断言整行格式含 `scope` 与 `type` 字段（即确实走了 `render_timeline`）。
3. **AC38**：往记录里插一行非法内容，断言 `TraceView.load(...).skipped` 与
   `reader.load_records(...)[1]` 相等；再用 `inspect.getsource` 断言
   `assertions.py` 里除 `check_history_len` 函数体之外**不出现** `json.loads`
   （硬护栏，防止后来者绕过 reader 自己解析记录）。
4. **AC35**：类型 + 作用域两者组合后条数逐步收窄。

**验证：** `python -m unittest tests.test_e2e_assertions -v` 全绿

## T31: 第四段门槛
**文件：** —　**依赖：** T30
**步骤：** 跑第一至四段的全部新测试 + 全量回归。

**验证：**
`python -m unittest tests.test_e2e_protocol tests.test_e2e_discovery tests.test_e2e_sandbox_seed tests.test_e2e_scripted tests.test_e2e_assertions -v`
全绿；`python -m unittest discover -s tests` 全绿

---

# 第五段：驱动内核（T32–T47）

> **本段完成后可验**：AC3、AC13、AC14、AC15、AC20、AC21、AC22，以及 AC6 的内核级一半。
> **段级依赖**：本段需要 T19（产品侧来源字段与 `_settle_session`）、T23（假模型）、
> T30（断言层）三者均已完成。

## T32: 跨线程投递原语
**文件：** `tests/e2e/control.py`　**依赖：** T1
**步骤：**
1. `run_on_main(loop, coro, timeout)`：
   `asyncio.run_coroutine_threadsafe(coro, loop).result(timeout=timeout)`。
2. **docstring 写清为什么不用 `app.call_from_thread`**（实测结论）：
   它没有 timeout 参数、会一直阻塞到工作跑完（主线程被 8 秒回调堵住时调用方等满
   7.8 秒，「先调用再 `queue.get(timeout=)`」完全无效）、且拒绝从主线程调用
   （抛 `RuntimeError: must run in a different thread from the app`）。
3. **写死一条使用规则**：**同步工作一律先包成 `async def` 再走本原语**。
   反例（会写出 bug）：`run_on_main(loop, app.some_sync_method(x), t)` ——
   这会在**调用方线程**上先执行 `some_sync_method`（违反「界面状态只能在主线程读写」），
   再把返回的 `None` 喂给 `run_coroutine_threadsafe` 抛 `TypeError`。
4. 超时语义：**超时只让调用方脱身，不取消已排队的工作**——这正是 N7 要的语义。

**验证：**
```
写一个临时脚本：起 asyncio 循环 → 主线程塞一个 sleep(5) 的协程 →
另一线程 run_on_main(..., timeout=1) → 断言约 1 秒时抛 TimeoutError
```

## T33: 应答者接缝
**文件：** `tests/e2e/control.py`　**依赖：** T32
**步骤：**
1. `PanelSnapshot` 冻结数据类：`kind` / `display` / `options`。
2. `Responder` 协议：`source: str` 属性 + `decide(panel) -> str`。
3. `ExternalResponder`：`source = "driver"`，`decide` 用 `threading.Event` + 一个槽位
   阻塞等待外部送入的 choice。
4. 注释写明 P1b 只需换一个实现（`source = "policy"`），`DriverCore` 一行不动
   （落实 spec F6 的「接缝本轮就要留出来」）。

**验证：** T45 覆盖

## T34: 面板原文与选项的提取
**文件：** `tests/e2e/control.py`　**依赖：** T33
**步骤：**
1. 实现 `extract_panel(app, kind) -> PanelSnapshot`（协程，在主线程执行）。
2. **⚠️ 命名陷阱必须写进注释**：Textual 的 `widget.display` 是**可见性布尔值**，
   与 `PanelSnapshot.display`（面板展示原文）**同名不同义**。
   面板原文**不在** `widget.display` 里，而在 `OptionList` 的 **0 号 disabled 表头**的
   `prompt` 中（`ConfirmPanel.show_for` / `show_prompt` 都是先 `add_option(Option(表头,
   disabled=True))` 再加可选项）。
3. `display` 取 `panel.get_option_at_index(0).prompt`；
   `options` 取其余 `Option` 的 `(id, prompt)`——**跳过所有 `disabled` 项**
   （`ClarifyPanel` 在候选项之间夹着 disabled 详情行）。
4. 一律返回**原始字符串**（含 `[dim]` 之类的 markup 标记），**不做渲染、不去标记**
   （plan §2.1 的 markup 口径：渲染态由记录的 `ui_message` / `status_bar` 承载）。

**验证：** T45 覆盖

## T35: 界面状态读取
**文件：** `tests/e2e/control.py`　**依赖：** T34
**步骤：**
1. `DriverCore.__init__` 存 `app` / `pilot` / `loop` / `build_result` / `responder` /
   `turn_budget` / `trace_path`，以及 `_lock` / `_last_action` / `_state`。
2. 实现 `_read_ui_state()`（协程，主线程执行）：**一次**读齐——忙碌态、
   待决盒是否存在与其 `kind`、会话面板态、目标面板 `.display`（可见性）、
   `app.focused` 的类型、以及经 T34 提取的 `PanelSnapshot`。
3. **不在这里读记录文件**（见 T36）。

**验证：** T45 覆盖

## T36: `snapshot` 与三态推导
**文件：** `tests/e2e/control.py`　**依赖：** T35
**步骤：**
1. `snapshot()`：一次 `run_on_main(_read_ui_state())` 拿界面态，
   **随后在调用方线程（非主线程）读 `trace_seq`**。
2. **⚠️ `trace_seq` 绝不能放进 `run_on_main`**：`wait` 每 30–100 ms 调一次 `snapshot`，
   把「全量重读记录文件」放到 Textual 主线程上等于每秒十几次文件 IO 打在 UI 线程，
   长会话下界面会明显卡顿并可能顶穿 N4 的一秒预算。它是纯文件 IO、与主线程无关。
3. 三态推导：待决盒非空或会话面板展示中 → `PENDING`；忙碌态为真 → `BUSY`；
   否则 `IDLE`。**顺序不能颠倒**——忙碌态在面板挂着时仍为真（实测）。
4. 组装 `status` 响应负载的**界面部分**；宿主级字段（`pid` / `fingerprint` /
   `workspace` / `user_dir` / `trace_path` / `mode` / `turns` / `turn_budget`）
   由 T49 在宿主侧补齐。

**验证：** T45 覆盖

## T37: `send`
**文件：** `tests/e2e/control.py`　**依赖：** T36
**步骤：**
1. 四段式：**锁内**校验 `state == IDLE`（否则回 `busy`）与轮次预算
   （`recorder.turn_total()` 超 `turn_budget` 回 `turn_budget`）、记 `_last_action`；
   **出锁后**投递。
2. 提交协程**必须写成**：
   ```python
   async def _submit() -> None:
       bar = app.query_one(InputBar)
       bar.focus()                 # 不可省
       bar.value = text            # InputBar 无 set_value，value 是 reactive
       await pilot.press("enter")  # 必须 await
   run_on_main(loop, _submit(), timeout=...)
   ```
3. 注释记下实测教训：只设 `value` 不按回车 → 文本躺在输入框里、模型一次没被调；
   把 `pilot.press` 塞进 lambda 元组 → 只造出一个从未被 await 的协程对象，
   **驱动器返回 ok 而什么都没发生**——这是本设施最不能有的失败形态（N7）。
4. 再记一条边界：确认/审批面板挂起期间焦点在面板上，`bar.focus()` 会把焦点抢走；
   故 `send` 与 `via=keys` 的应答**不可交错使用**（`send` 在非 IDLE 时本就被拦，
   这里只是把理由写明）。

**验证：** T45 覆盖

## T38: `wait`
**文件：** `tests/e2e/control.py`　**依赖：** T36
**步骤：**
1. **不持驱动锁**（不变量②）。循环 `snapshot()`，命中 `IDLE` 或 `PENDING` 即返回
   `{"terminal": ...}`。
2. 轮询间隔 30 ms 起、退避到 100 ms 封顶。
3. 超时返回 `err("timeout", ..., data=诊断)`，诊断含三个判据分量的取值、
   `last_action`、`waited`、当前 `state`。

**验证：** T45 覆盖

## T39: `choice` → 结算值映射表
**文件：** `tests/e2e/control.py`　**依赖：** T33
**步骤：**
1. 实现 `settlement_for(kind, choice, app) -> Any`，映射如下（**三套标识必须分清**：
   线上 `choice` / 产品 `option.id` / 结算值）：

   | kind | choice | option.id | `via=channel` 的结算值 |
   | --- | --- | --- | --- |
   | confirm | `once` | `yes` | `ConfirmDecision.ALLOW` |
   | confirm | `session` | `yes_session` | `ConfirmDecision.ALLOW_SESSION` |
   | confirm | `permanent` | `yes_permanent` | `ConfirmDecision.ALLOW_PERMANENT` |
   | confirm | `deny` | `no` | `ConfirmDecision.DENY` |
   | approve | `yes` / `no` | `yes` / `no` | `True` / `False` |
   | clarify | 数字串 `idx` | 无（走 App 私有列表） | `app._clarify_options[idx].summary` |
   | session | 会话标识 / `cancel` | 会话标识 | 交给 `_settle_session(id_or_None, source)` |

2. `clarify` 的结算值要读 App 的 `_clarify_options`——注释写明这是**刻意读私有属性**
   （产品侧的结算路径本身就是这么算的，见 `on_option_list_option_selected` 的 clarify 分支），
   并说明若产品改了该属性名，本处会一起失效（登记为成对维护点）。
3. 越界 `idx` / 未知 `choice` 抛可读异常，由 `answer` 转成 `bad_request`。

**验证：** T45 覆盖

## T40: `answer`（channel 路径）
**文件：** `tests/e2e/control.py`　**依赖：** T39
**步骤：**
1. `choice` 先经 `protocol.validate_choice` / `validate_via` 校验，非法回 `bad_request`。
2. **不变量③ 复核**：目标面板 `.display is True`（可见性）**且**
   `type(app.focused) is 该面板控件类`；不满足则 30 ms × N 重试至上限，
   仍不满足回 `not_pending` 并附诊断。
   **面板类型取自待决盒的 `kind`，不能从控件类反推**——`ConfirmPanel` 被
   `confirm` 与 `approve` 两种交互复用。
3. 结算（**注意 T32 的使用规则：同步工作包成协程**）：
   ```python
   async def _settle() -> None:
       if kind == "session":
           app._settle_session(session_id_or_None, responder.source)
       else:
           app._resolve_interaction(settlement_for(kind, choice, app), responder.source)
   run_on_main(loop, _settle(), timeout=...)
   ```

**验证：** T45 覆盖

## T41: `answer`（keys 路径）
**文件：** `tests/e2e/control.py`　**依赖：** T40
**步骤：**
1. `via == "keys"` 时投递按键序列：`pilot.press("down")` × N + `pilot.press("enter")`。
2. **N 的推导不是选项下标**：面板首项是 disabled 表头，`ConfirmPanel` 初始高亮在
   `_YES_INDEX`（即第一个可选项）；`ClarifyPanel` 还在候选项之间夹 disabled 详情行，
   而 `OptionList` 的上下导航会自动跳过 disabled 项。
   **正确做法**：按「可选项序列」（T34 已过滤掉 disabled）算目标在其中的下标，
   减去初始高亮在该序列中的下标，得到下移次数（负数则用 `up`）。
3. `clarify` + `keys` 在 T3 已被拒（详情行使推导不稳定），此处不必再处理。
4. 该路径**不指定 source**，走产品默认 `"human"`——这正是 AC15 需要的另一半来源。

**验证：** T45 覆盖

## T42: `cancel` 与 `observe`
**文件：** `tests/e2e/control.py`　**依赖：** T36
**步骤：**
1. `cancel()`：锁外投递 `manager.request_cancel`（包成协程），记 `_last_action`。
2. `observe(since, types)`：`reader.load_records` →
   **末行解析失败视为正常并丢弃、不计入 `skipped`**（宿主正在写、外部正在读）→
   按 `since` 与 `types` 过滤 → 返回 `next_since` / `skipped` /
   `timeline`（`reader.render_timeline` 的行）/ `events`（**完整记录**，
   含 `ui_message` 与 `tool_execute` 的 payload——AC34 要的正是遍历控件取不到的那部分）。

**验证：** T45 覆盖

## T43: `shutdown_on_main`
**文件：** `tests/e2e/control.py`　**依赖：** T40
**步骤：**
1. `async def shutdown_on_main(self, reason)`，**只能在主线程调用**，
   内部**绝不使用** `run_on_main` 或 `call_from_thread`（前者自己等自己、
   后者直接抛 RuntimeError）——直接同步调用界面方法。
2. 单个带上限的**交织循环**：
   ```
   while 未超期:
       if 待决盒非空:      app._resolve_interaction(安全默认值(kind), "driver_forced")
       elif 会话面板展示:   app._settle_session(None, "driver_forced")
       elif not 忙碌态:     break
       await asyncio.sleep(0.03)
   ```
   安全默认值：`confirm` → `ConfirmDecision.DENY`、`approve` → `False`、`clarify` → `None`。
3. live 模式：用 `threading.enumerate()` 找名为 `rhine-notes` 的线程并 `join(timeout)`。
4. 最后 `app.exit()`。
5. 注释记下实测：不做强制结算则 `asyncio.run` 收尾去 join 阻塞在
   `box["event"].wait()` 的工作线程，**Python 3.11 的 `shutdown_default_executor`
   无超时参数、永不返回**（3.12 才加了 5 分钟默认值）；两步分开做则结算后新弹的面板
   会在第二段再次挂住，故必须交织。

**验证：** T44 就地验（**不推到宿主测试**）

## T44: `shutdown_on_main` 的就地验证
**文件：** `tests/test_e2e_control.py`　**依赖：** T43
**步骤：**
1. **不起宿主进程**：真实 `RhineApp` + `run_test` + 假模型触发一次确认面板。
2. 在主线程 `await core.shutdown_on_main("test")`，断言——
   在上限内返回、记录里存在一条 `source == "driver_forced"` 的交互事件、
   工作线程已退出（`threading.enumerate()` 里没有仍阻塞的循环线程）。
3. 再加一条反证：**跳过强制结算**直接等忙碌态转假，断言在上限内**等不到**
   （用短上限，证明这一步确实是必需的，不是保险性质的多余代码）。

**验证：** `python -m unittest tests.test_e2e_control -v -k shutdown` 全绿
（**这是 plan 里唯一被实测证明「写错就永久挂死」的地方，必须在此就地验，
不能等到 11 步之后的宿主测试**）

## T45: 内核测试（判据 / 等待 / 取消 / 死锁）
**文件：** `tests/test_e2e_control.py`　**依赖：** T44
**步骤：**
1. `run_on_main` 的超时确实生效（主线程塞长回调，断言在上限附近抛 `TimeoutError`）。
2. **AC13**：三态判据各造一次；重点断言「面板挂着时忙碌态仍为真、但判定必须是 PENDING」。
3. **AC3**：`wait` 两终态各返回一次且 `terminal` 值不同；超时诊断字段齐备。
4. **AC21**：超时用例的假模型必须是「**耗时超过等待上限**」而**不是「永不结束」**——
   非守护线程会在解释器退出时被汇合，真「永不结束」会让测试进程挂死。
   注释里写明这条约束。
5. **AC20**：`cancel` 后循环以「用户取消」结束、会话回到空闲且仍可再 `send`。
6. **AC22 / N6 死锁护栏**：另起线程反复 `snapshot()`，主线程同时驱动交互，
   用**完成计数**（不是布尔标志）+ `join(timeout)` 判定。
   **同线程版本会静默通过，不可简化**。

**验证：** `python -m unittest tests.test_e2e_control -v` 全绿

## T46: 内核测试（四类面板应答）
**文件：** `tests/test_e2e_control.py`　**依赖：** T45
**步骤：**
1. **AC14**：工具确认、需求澄清、计划审批、会话选择各构造一次并应答，
   逐类断言——① 应答**前** `display`（可见性）与 `focused` 两个前置态均成立；
   ② 各产出**恰好一条**交互事件；
   ③ **前三类断言被阻塞的循环得以继续**（后续仍有模型请求或工具执行）；
   ④ **会话选择断言历史被载入**（不能用「循环继续」判定——它不隶属任何循环）。
2. **AC15**：同一次运行内 `via=channel` 与 `via=keys` 各应答一次，
   断言两条交互事件的 `source` 分别为 `driver` 与 `human`。
3. 面板原文与选项：断言 `PanelSnapshot.display` 取到的是 0 号表头文本、
   `options` 不含任何 disabled 项、且均为**含 markup 标记的原始字符串**。

**验证：** `python -m unittest tests.test_e2e_control -v` 全绿

## T47: 四条不变量写进模块文档
**文件：** `tests/e2e/control.py`　**依赖：** T46
**步骤：**
1. 模块 docstring 顶部完整写下四条不变量与各自的**违反后果**（照 plan §3.7），
   措辞要让第一次读的人明白「为什么不能图省事」。
2. 逐条在对应代码处放简短的呼应注释（只标「见模块 docstring 不变量①」，不重复长文）。

**验证：** `python -m compileall tests/e2e` 无错误；人工通读一遍

---

# 第六段：宿主与客户端（T48–T66）

> **本段完成后可验**：AC1、AC4、AC5、AC6、AC7、AC8、AC9、AC10、AC11、AC12、
> AC16、AC17、AC19、AC23、AC24、AC25、AC26、AC27、AC28、AC30、AC34、AC39、AC40。
> **段级依赖**：需要第一段（沙箱/预置/发布/指纹）、第二段（产品接缝）、
> 第三段（假模型）、第四段（断言层）、第五段（内核）全部完成。

## T48: 控制服务器
**文件：** `tests/e2e/host.py`　**依赖：** T47、T7
**步骤：**
1. `ControlServer`：`socket` 绑 `("127.0.0.1", 0)`（系统分配端口）、`listen`、
   起 daemon 的 accept 线程；**每连接一个 handler 线程**
   （`wait` 阻塞时 `status` 仍要能答——N4）。
2. handler：读一行 → `protocol.decode` → 派发 → `protocol.encode` 写回 → 关闭。
3. **客户端中途断开**：写回时的 `ConnectionResetError` / `BrokenPipeError` 一律吞掉
   并结束该 handler，**不得影响 accept 线程**。
4. `DriverCore` 未就绪时一律回 `err("starting", ...)`。

**验证：** 手工起一个只跑 `ControlServer` 的最小脚本，用 python socket 打一条 `status`，
收到 `starting`

## T49: `status` 响应组装
**文件：** `tests/e2e/host.py`　**依赖：** T48、T36
**步骤：**
1. 在宿主侧把 `DriverCore.snapshot()` 的界面部分与**宿主级字段**合并成完整 `status`
   负载：`pid` / `fingerprint`（T12 算出，启动时算一次并缓存）/ `workspace` /
   `user_dir` / `trace_path` / `mode` / `turns`（`recorder.turn_total()`）/ `turn_budget`。
2. 字段名与 plan §2.1 逐字一致——**AC9 与 AC11 都从这里读**。

**验证：** T56 覆盖

## T50: 宿主启动顺序
**文件：** `tests/e2e/host.py`　**依赖：** T48、T9、T10、T11、T12
**步骤：**
1. argparse：`--mode` / `--script` / `--seed` / `--idle-timeout` / `--max-turns` /
   `--config` / `--keep-workspace`。
   **刻意没有 `--workspace` / `--user-dir`**：外部传入的路径永远过不了可丢弃校验，
   否则该校验形同虚设（注释写明）。
2. 顺序（不可调）：建发布目录 → `create_workspace` + `create_user_dir` →
   `assert_disposable` 两者 → `os.chdir` 进工作区 → 跑 `--seed` 预置 →
   **bind + listen + publish + 起 accept 线程** → 才进 `asyncio.run(_serve())`。
3. 注释写明**为什么 socket 先于装配**：spec AC5 要验「装配期致命错误经通道回报」，
   先装配再监听则失败时永远等不到监听。

**验证：** 手工起一次，确认发布文件存在且 `status` 回 `starting` 或 `idle`

## T51: 装配接线与记录器校验
**文件：** `tests/e2e/host.py`　**依赖：** T50、T17
**步骤：**
1. `_serve()` 内：`create_recorder(工作区/.rhinecode/traces/host.jsonl)`。
2. **立刻断言 `recorder.enabled` 为真**，为假则写 stderr 并以非零退出码终止。
   注释写明理由：`create_recorder` 在路径不可写时会**静默降级为 NullRecorder**
   （P0 的有意设计），而 spec F9 要求宿主一律开启记录——真降级了，
   你会看到所有断言以「记录里没有这条事件」的形式失败，而根因是磁盘或权限。
3. `build_app(cfg, user_dir=…, recorder=…, provider_factory=…, exclude_tools=…)`。
4. `exclude_tools = frozenset({"mcp_add_server", "mcp_resolve_server"})`，
   注释写清两者各自的排除理由：前者**写真实用户主目录**且不吃 `user_dir`（F8）；
   后者 `read_only=True` 却会访问外部包索引，而只读且被放行的工具**根本不弹面板**、
   应答者拦不住它（F19）。

**验证：** T56 覆盖

## T52: 运行模式分支
**文件：** `tests/e2e/host.py`　**依赖：** T51、T23
**步骤：**
1. `--mode scripted`：从 `--script MOD:ATTR` 导入脚本构造 `ScriptedProvider`，
   工厂返回它；装配后立刻 `result.manager.memory_manager.notes_enabled = False`
   （F16 裁决：确定性形态关自动笔记，它本身就是不确定性来源。
   实测该属性是普通实例属性、门控点每次调用现读，赋值即生效）。
2. `--mode live`：`provider_factory=None`（走真实）；`api_key` 缺失或为占位符时
   **明确报错退出**，不静默降级（F23）。自动笔记**保留**。

**验证：** 两种模式各手工起一次

## T53: 致命错误态
**文件：** `tests/e2e/host.py`　**依赖：** T52
**步骤：**
1. `BootstrapError` → 进 `fatal` 态，`error.message` 取 `e.args[0]`
   （P0 已把它做成成文的完整 stderr 文案，**不要再拼前缀**）。
2. 继续服务一个有界宽限窗口（默认 60 秒，或收到 `quit` 提前结束），
   期间所有指令一律回 `{"code": "fatal", "message": 文案}`，
   然后以退出码 1 终止。
3. 注释写明：这是为了让 AC5「装配期致命错误经通道回报」可验——
   直接退出的话客户端只会收到连接失败。

**验证：** T58 覆盖

## T54: 常驻与退出编排
**文件：** `tests/e2e/host.py`　**依赖：** T53、T43
**步骤：**
1. `async with app.run_test(size=(120, 40)) as pilot:` → 构造 `DriverCore` →
   `state` 转 `idle` → `await stop_event.wait()` → `await core.shutdown_on_main(reason)`。
2. `quit` 指令与 watchdog 一律用 `loop.call_soon_threadsafe(stop_event.set)`
   （`asyncio.Event` 非线程安全；裸 `set()` 能跑通是靠定时器恰好唤醒循环，属侥幸）。
3. 退出 `run_test` 上下文后：`cleanup(reason)` → `os.chdir` 回原目录 →
   非 `--keep-workspace` 时 `cleanup_workspace` → `unpublish(pid)`。

**验证：** T57 覆盖

## T55: 空闲超时与轮次预算
**文件：** `tests/e2e/host.py`　**依赖：** T54
**步骤：**
1. watchdog daemon 线程每秒比对 `now - last_command_at`，超 `--idle-timeout`
   （默认 1800 秒）则置位 `stop_event`。每条指令处理完刷新 `last_command_at`。
2. 轮次预算在 `send` 的前置检查处生效（T37 已实现），`status` 暴露 `turns` / `turn_budget`。
3. 注释写明口径：预算**跨 `send` 累计**、计数取自 `recorder.turn_total()`
   （含 `main` / `isolated:*` / `summary` / `notes` 四种作用域）、
   **挡不住单次 send 内的循环**——单次上界由产品既有的 `MAX_ITERATIONS = 25` 兜底，
   故最坏烧 `budget + 25`；**独立模式子对话另有独立预算，实际上界更高**。
   这是刻意接受的口径，不为它增加第四处产品改动。

**验证：** T57 覆盖

## T56: 瘦客户端
**文件：** `tests/e2e/client.py`　**依赖：** T49
**步骤：**
1. argparse 子命令：`status` / `send` / `wait` / `answer` / `cancel` / `observe` /
   `quit` / `hosts`，全局 `--pid` 与 `--json`。
2. `resolve_host(pid)` → `connect` → `verify_pid` → 发一条 → 读一行 → 打印。
   **无状态、无重试**（N7）。
3. **三种失败翻译成人话**：连不上 → 「宿主已不在（发布文件 X，pid N）」+ 清理提示；
   pid 不一致 → 「端口已被其它进程占用，发布文件疑似陈旧」；
   **读到 EOF（空响应）** → 「宿主在处理本指令期间退出了」，
   而不是抛 `JSONDecodeError`（`wait` 期间宿主退出正是这个形态）。
4. **刻意不 import `rhinecode`**：宿主挂掉时它仍要能起来报错，
   少一层导入少一处失败面。写一行注释说明。

**验证：** `python -m tests.e2e.client hosts` 在无宿主时给出可读提示且退出码非零

## T57: 宿主子进程夹具
**文件：** `tests/test_e2e_host.py`　**依赖：** T56
**步骤：**
1. 一个基类：`subprocess.Popen` 起宿主（可传 `--mode` / `--script` / `--seed` /
   `--idle-timeout` / `--max-turns`），轮询发布文件就绪（带超时）。
2. 提供 `cmd(...)` 辅助：走 `discovery.connect` 直接发指令收响应（不经 client 进程，
   快且能拿结构化数据）。
3. `addCleanup` 注册「强杀宿主 + `unpublish` + `chdir` 回去 + `rmtree`」，
   且该清理对「宿主已自行退出」**幂等**。
   缺了它，任何一条用例断言失败都会留下进程、临时目录与发布文件，AC42 直接红。

**验证：** 起停一次不报错、无残留

## T58: 闭环序列（AC1）
**文件：** `tests/test_e2e_host.py`　**依赖：** T57
**步骤：**
1. scripted 模式 + 一个会触发确认面板的两轮脚本。
2. 走完完整序列：`send` → `wait`（得 `pending`）→ `status` 读面板**展示原文与可选项列表**
   → `answer` → `wait`（得 `idle`）→ **再 `send`** → `quit`，**全程不重启宿主**。
3. 断言**会话存档**里含两轮用户输入（用 T29 的 `check_history_len`）——
   **不是从记录里数**，权威来源是存档（F24 的裁决）。

**验证：** `python -m unittest tests.test_e2e_host -v -k closed_loop` 全绿

## T59: 输入入口与观察面（AC12 / AC34 / AC4）
**文件：** `tests/test_e2e_host.py`　**依赖：** T58
**步骤：**
1. **AC12**：驱动一次普通消息与一次命令（如 `/skills`），断言记录里各自产出
   `user_input` 与 `command_dispatch` 事件——证明走的是真人提交入口而非内部方法。
2. **AC34**：跑一轮含 AI 正文与工具调用的对话，断言 `observe` 返回的 `events` 里
   **同时含** `ui_message`（富文本 AI 正文）与 `tool_execute`（工具行）的完整 payload
   ——这正是遍历界面控件取不到的那部分。
3. **AC4**：在一次长循环运行期间连续发若干次 `status` 与 `observe`，
   逐次断言**单次返回耗时 < 1 秒**且状态反映为忙碌。

**验证：** `python -m unittest tests.test_e2e_host -v -k observe` 全绿

## T60: 退出与强制结算（AC6）
**文件：** `tests/test_e2e_host.py`　**依赖：** T58
**步骤：**
1. 驱动到弹出确认面板后**直接发 `quit`**，断言——进程在上限内退出（退出码 0）、
   记录中存在一条 `source == "driver_forced"` 的交互事件。
2. **这条是实测挂死路径的护栏，不可省**（T44 是内核级、本条是进程级，两条都要）。

**验证：** `python -m unittest tests.test_e2e_host -v -k quit_with_pending` 全绿

## T61: 空闲超时与清理（AC7 / AC8）
**文件：** `tests/test_e2e_host.py`　**依赖：** T60
**步骤：**
1. **AC7（跨进程可观测项）**：`--idle-timeout 3` 起宿主、不发任何指令，断言——
   进程自行退出、会话锁文件消失、记录文件可完整解析且**末条为 `session_end`**、
   临时工作区可被删除。
2. **AC8（进程内项）**：在**测试进程内**直接 `build_app(user_dir=临时)` → 调 `cleanup`
   → 断言只读路径白名单已复位、MCP 连接已回收。
   注释写明为什么拆两条：白名单与连接都是**进程内内存状态**，宿主进程都退出了，
   外部无从检查。

**验证：** `python -m unittest tests.test_e2e_host -v -k idle` 全绿

## T62: 致命错误、发现与指纹（AC5 / AC9 / AC10 / AC11）
**文件：** `tests/test_e2e_host.py`　**依赖：** T61
**步骤：**
1. **AC5**：预置一个白名单笔误的 Skill 起宿主，断言客户端 `status` 收到
   `code == "fatal"` 且 `message` 与既有启动测试的文案一致（**而不是超时**）。
2. **AC9**：断言监听地址为回环；发布文件含 `port` / `workspace` / `pid` 等字段；
   客户端据此**一步连上**（不需要先知道工作区）。
3. **AC10**：伪造一份指向未监听端口的发布文件，断言客户端**立即**报陈旧（耗时 < 1 秒）。
4. **AC11**：取一次 `fingerprint` → `touch` 一个产品 `.py` → 重启宿主 → 再取 →
   断言两者不同。

**验证：** `python -m unittest tests.test_e2e_host -v -k fatal or discovery` 全绿

## T63: 权限面护栏（AC16 / AC17）
**文件：** `tests/test_e2e_host.py`　**依赖：** T62
**步骤：**
1. **AC16**：脚本化四次同类工具调用，外部驱动者依次选 `once` / `session` /
   `permanent` / `deny` 四档，逐档断言结果；
   对 `permanent` **额外断言工作区内的 `.rhinecode/permissions.local.yaml` 新增了条目**。
2. **AC17**：脚本化一条**危险命令**（黑名单命中，如 `rm -rf` 类），
   外部驱动者选择**放行**，断言——权限决策事件的命中层为**黑名单**、
   且**没有**对应的工具执行成功事件。
3. 注释写明这两条的分量：spec 目标 5「不扩大权限面」全靠它俩——
   驱动器替人应答等于换了第⑤层的执行者，第①层必须证明仍然拦得住。

**验证：** `python -m unittest tests.test_e2e_host -v -k permission` 全绿

## T64: 模型注入面（AC28 / AC30）
**文件：** `tests/test_e2e_host.py`　**依赖：** T63
**步骤：**
1. **AC28**：断言注入假模型后应用其余部分**为真**——权限引擎、Skill 管理器、
   命令注册表、记录器均为真实实例，工具注册中心含真实工具
   （经 `status` 的工具清单 + 记录的 `session_start` 快照断言）。
2. **AC30**：预置一个**声明了自定义 `model:` 的独立模式 Skill**，驱动它执行，断言——
   ① 全程无外部连接（假模型仍是唯一被调用的实现，可用假工厂的调用计数证明）；
   ② 子对话的模型请求事件出现在记录中且**作用域为 `isolated:<skill名>`**。
   注释写明：这是 spec F21 自己标注「**最容易漏的一条**」——不透传 `provider_factory`
   时该旁路会静默连真实网络，而现象是「测试很慢且偶尔失败」，极难定位。

**验证：** `python -m unittest tests.test_e2e_host -v -k injection` 全绿

## T65: 隔离、预置与无外部连接（AC19 / AC23 / AC25 / AC27）
**文件：** `tests/test_e2e_host.py`　**依赖：** T64
**步骤：**
1. **AC19**：断言 `status` 的工具清单与记录的 `session_start` 快照**均不含**
   `mcp_add_server` / `mcp_resolve_server`，且两者一致。
2. **AC23**：用 `--seed` 预置一个项目级 Skill **与一段 git 历史**，断言——
   该 Skill 出现在 `/skills` 的输出里（经 `send` + `observe` 读界面消息）；
   **且只读版本控制命令能读到提交历史**（脚本化一次 `git log` 类调用并断言其输出非空）。
3. **AC25**：断言 `status` 的 `user_dir` 位于系统临时目录之下、不是真实用户目录；
   **且系统提示中不含真实用户级项目指令与笔记索引**
   （用 `check_stable_prompt(..., present=False)` 对一个只可能出现在真实用户目录里的
   标记串做否定断言）。
4. **AC27**：断言记录的 `session_start` 快照里 MCP 状态为空；
   **且假模型是唯一被调用的模型实现**（假工厂的调用计数 == 记录里的模型请求数）。

**验证：** `python -m unittest tests.test_e2e_host -v -k isolation` 全绿

## T66: 连跑不污染与全量回归（AC24 / AC26 / AC39 / AC40）
**文件：** `tests/test_e2e_host.py`　**依赖：** T65
**步骤：**
1. **AC26**：同一进程内连跑两次「装配 → 清理」（用不同用户目录），断言第二次的
   只读白名单不含第一次注册的路径、工作目录已切回、会话锁已释放。
2. **AC24 专项护栏**（`@unittest.skipUnless(慢速开关)`，**不进全量**）：
   连续起停宿主若干次，无一次因目录占用失败。
3. **AC24 的 live 一半**：live 模式退出前确实等待过 `rhine-notes` 收敛
   （断言退出后该线程已不在 `threading.enumerate()` 里；此条随 T67 的 live 用例一起跑）。
4. **AC39 / AC40**：`python -m compileall rhinecode tests`；
   `python -m unittest discover -s tests` 全绿，既有 714 条一条不少。

**验证：** 两条命令均无错误，测试总数 ≥ 714 + 新增

---

# 第七段：真实模式与收尾（T67–T72）

> **本段完成后可验**：AC2、AC31 的端到端一半、AC32 的消费侧一半、AC33 的端到端一半、
> AC41、AC42、AC43。

## T67: 真实模式用例（AC2 / AC41）
**文件：** `tests/test_e2e_live.py`　**依赖：** T66
**步骤：**
1. `@unittest.skipUnless(os.environ.get("RHINE_E2E_LIVE") == "1", "真实模式需显式开启")`
   ——**默认跳过**（AC41）。再检查配置里的 `api_key` 是否为占位符，是则 `skipTest`。
2. **AC2**：`--mode live` 起宿主，走完 AC1 的完整序列（含一次面板决策），断言——
   记录里出现真实模型的请求与响应事件、至少一条工具执行成功。
3. 断言只针对模式与存在性，**不断言模型措辞**。
4. 退出后断言 `rhine-notes` 线程已收敛（AC24 的 live 一半）。

**验证：** `python -m unittest tests.test_e2e_live -v` 报告 skipped；
设 `RHINE_E2E_LIVE=1` 后手工跑一次通过

## T68: 端到端补齐（AC31 / AC32 / AC33）
**文件：** `tests/test_e2e_host.py`　**依赖：** T66
**步骤：**
1. **AC31 端到端**：两轮脚本——第 1 轮调 `load_skill` 激活一个预置 Skill、
   第 2 轮出结论；断言**第 1 轮的动态提醒不含该 Skill 正文、第 2 轮含**
   （用 `check_dynamic_reminder`）。这是「第 N 轮激活、第 N+1 轮生效」的判据。
2. **AC32 消费侧**：正文与思考块各断言一次界面渲染结果（经 `observe` 的 `ui_message`）；
   **流错误块断言循环以流错误停止**（记录里的结束事件停止原因）。
3. **AC33 端到端**：脚本只给一轮，触发一次**上下文摘要**调用，
   断言会话正常回到空闲、且记录里出现含 `FALLBACK_MARKER` 的响应（兜底可识别）。

**验证：** `python -m unittest tests.test_e2e_host -v -k e2e_semantics` 全绿

## T69: 残留兜底与编码（AC42 / AC43）
**文件：** `tests/test_e2e_host.py`　**依赖：** T68
**步骤：**
1. **AC42**：加一条用例，在全部宿主用例之后检查发布目录没有本次留下的文件、
   系统临时目录没有 `rhine_e2e_ws_*` 残留。
   发现残留说明某条用例的 `addCleanup` 漏了——**修那条用例而不是放宽本条**（N7）。
2. **AC43**：让预置文件内容与一条界面消息都含中文与字面 `[`，断言——
   记录以 UTF-8 完整解析、中文原样可读；`status` 的面板 `display` 是**含 markup
   标记的原始字符串**；诊断输出（超时 / 断言失败）里的中文不乱码。

**验证：** `python -m unittest tests.test_e2e_host -v -k cleanup or encoding` 全绿

## T70: `CLAUDE.md` 同步
**文件：** `CLAUDE.md`　**依赖：** T69
**步骤：**
1. 「架构」节新增一段描述驱动设施，**措辞必须写清**：它是 P0 Trace 设施的续作、
   **跨阶段测试设施、不占章节号、不属于任何产品章节、不进产品包**；
   并写明 P1a 与 P1b 的分界。
2. 「成对维护点」新增**五条**：
   - 新增控制通道指令 → `protocol.py`（取值/错误码）+ `control.py`（`DriverCore` 方法）
     + `client.py`（子命令）+ `test_e2e_control.py`（四处齐改，漏一处是**静默失效**）；
   - 产品侧交互结算点新增来源取值 → `app.py` 三处 + `protocol.py` 取值集合 + 断言词汇；
   - `build_app` 新增参数 → `bootstrap.py` + `tests/e2e/host.py` 的装配调用
     （**宿主是它的第二个真实调用方**，漏改会让驱动器与真实启动行为分叉）；
   - **`_settle_session` 是会话面板结算的唯一入口** —— 将来任何第三条会话结算路径
     都必须走它，否则丢埋点、丢幂等守卫；
   - **`assertions.py` 的十一项断言词汇 ↔ spec F25 清单** —— 增删要同步
     spec / `assertions.py` / `test_e2e_assertions.py` 三处。
3. **扩写既有那条 bootstrap 窄窗口约束**：现有条目只写了 Skill 校验的位置，
   现在 `exclude_tools` 的摘除也卡在同一个窗口且理由同样是「两头都不能挪」——
   必须补进**同一处**，否则下一个人重排 bootstrap 只会看到 Skill 那半段。
4. 「安全边界」节补：控制通道不鉴权的边界（回环、短生命周期、真实模式下宿主持有真实
   凭据，故驱动期间本机应视为可信环境）。
5. 「常用命令」节补宿主与客户端的用法。

**验证：** 通读一遍；`grep -n "驱动器\|tests/e2e\|_settle_session" CLAUDE.md` 能读到上述各处

## T71: `AGENTS.md` / `README.md` / spec 交接
**文件：** `AGENTS.md`、`README.md`、`docs/c11/testing/p1-driver/spec.md`　**依赖：** T70
**步骤：**
1. `AGENTS.md` 与 `CLAUDE.md` 保持一致口径；`README.md` 只提一句
   「附带一套端到端驱动设施，见 `docs/c11/testing/p1-driver/`」。**不要写成产品功能**。
2. 在 spec 末节「推后到 P1b 的内容」补一小段「P1a 交付后 P1b 可直接复用的接缝」：
   `Responder` 协议（换实现即可）、`--seed` 预置函数组、`TraceView` 与十一项词汇、
   `sandbox` 的三步清理、宿主的 `--mode` 分支、`choice` → 结算值映射表。
3. 记下 P1a 实际交付时**偏离 plan 的地方**（若有），避免 P1b 照着过期设计做。

**验证：** 通读一遍

## T72: 按 checklist 验收
**文件：** `docs/c11/testing/p1-driver/checklist.md`　**依赖：** T71
**步骤：**
1. **该文件由 `/spec` 流程的第四阶段产出**（在本 task.md 审批之后、开发开始之前），
   本任务只负责**执行**它。它须覆盖 AC1–AC43 与两条环境前置
   （**本机需装 git**、真实模式需 `RHINE_E2E_LIVE=1` 与有效凭据）。
2. 逐项执行，记录**实际结果与证据**（命令输出片段），不是预期结果。
3. 有不通过的：修复 → 重新验证 → 如实报告。
4. 手测项按既定规矩交给用户亲自跑，只提供分步操作与预期。

**验证：** 产出完整验收报告

---

## 执行顺序

```
第一段（纯逻辑）
  T1 → T2 → T3 → T4
  T2 → T5 → T6 → T7 → T8
  T1 → T9 → T10 ┐
  T1 → T11      ├→ T13
  T1 → T12      ┘

第二段（产品接缝，可与第一段并行）
  T14 → T15 ┐
  T16 ──────┼→ T17 → T20
  T18 → T19 ┘

第三段  T21 → T22 → T23 → T24            （依赖 T1）
第四段  T25 → T26 → {T27, T28, T29} → T30 → T31   （依赖 T1）

第五段（依赖 T19、T23、T30）
  T32 → T33 → T34 → T35 → T36 → {T37, T38, T42}
  T33 → T39 → T40 → T41
  T40 → T43 → T44 → T45 → T46 → T47

第六段（依赖第一~五段全部）
  T47 ┐
  T7  ┴→ T48 → T49 → T56
      T48 → T50 → T51 → T52 → T53 → T54 → T55
      T56 → T57 → T58 → {T59, T60} → T61 → T62 → T63 → T64 → T65 → T66

第七段  T66 → {T67, T68} → T69 → T70 → T71 → T72
```

**关键路径**：`T19 → T36 → T43 → T44 → T48 → T54 → T58 → T66`。
第一、三、四段可在等待期并行推进。

## 每段的门槛

| 段 | 门槛命令 | 可验 AC |
| --- | --- | --- |
| 一 | `python -m unittest tests.test_e2e_protocol tests.test_e2e_discovery tests.test_e2e_sandbox_seed` | AC18、AC10(单元) |
| 二 | `python -m unittest discover -s tests` 仍 **714 全绿** | AC29 |
| 三 | `python -m unittest tests.test_e2e_scripted` | AC32(单元)、AC33(单元) |
| 四 | `python -m unittest tests.test_e2e_assertions` | AC35–AC38 |
| 五 | `python -m unittest tests.test_e2e_control` | AC3、AC13–AC15、AC20–AC22 |
| 六 | `python -m unittest tests.test_e2e_host` + 全量 | AC1、AC4–AC12、AC16–AC17、AC19、AC23–AC28、AC30、AC34、AC39–AC40 |
| 七 | `compileall` + 全量 + checklist 验收报告 | AC2、AC31–AC33(端到端)、AC41–AC43 |

## AC 覆盖总表

| AC | 任务 | AC | 任务 |
| --- | --- | --- | --- |
| AC1 | T58 | AC23 | T65 |
| AC2 | T67 | AC24 | T66、T67 |
| AC3 | T45 | AC25 | T65 |
| AC4 | T59 | AC26 | T66 |
| AC5 | T53、T62 | AC27 | T65 |
| AC6 | T44、T60 | AC28 | T64 |
| AC7 | T61 | AC29 | T14、T17、T20 |
| AC8 | T61 | AC30 | T64 |
| AC9 | T49、T62 | AC31 | T24、T68 |
| AC10 | T8、T62 | AC32 | T24、T68 |
| AC11 | T62 | AC33 | T24、T68 |
| AC12 | T59 | AC34 | T59 |
| AC13 | T45 | AC35 | T30 |
| AC14 | T46 | AC36 | T30 |
| AC15 | T46 | AC37 | T30 |
| AC16 | T63 | AC38 | T30 |
| AC17 | T63 | AC39 | T66 |
| AC18 | T13 | AC40 | T66 |
| AC19 | T65 | AC41 | T67 |
| AC20 | T45 | AC42 | T69 |
| AC21 | T45 | AC43 | T69 |
| AC22 | T45 | | |
