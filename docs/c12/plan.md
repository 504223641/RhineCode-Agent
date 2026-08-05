# C12 Hook 系统 Plan

> 状态：待批准（2026-08-05，第 1 轮）
>
> 依据：已批准的 [`spec.md`](spec.md)。冲突时以 spec 为准。

## 架构概览

新增一个包 `rhinecode/hooks/`，七个模块，职责自下而上：

| 模块 | 职责 | 有无 I/O |
| --- | --- | --- |
| `models.py` | 纯数据：事件枚举、字段表、规则/条件/动作/结论的数据类 | 无 |
| `conditions.py` | 条件求值：四种匹配形态识别 + `all`/`any` 组合 | 无 |
| `parser.py` | YAML 结构 → `HookRule`，含 F8 的八项集中校验 | 无 |
| `config.py` | 两层文件定位、读取、模板生成 | 读写文件 |
| `actions.py` | 四种动作的执行器 | 起子进程 / 发 HTTP |
| `manager.py` | 编排：分发、`once` 状态、结论合并、注入队列、统计、trace 埋点 | 间接（经 actions） |
| `report.py` | `/hooks` 报告与项目级启动提示的文本渲染 | 无 |

**五个模块零 I/O**，这是 spec N5「纯逻辑可测」的实现保障：条件求值、规则解析、结论合并
可以在完全不触碰文件系统与网络的前提下被单测覆盖。

### 依赖方向

```
hooks ──→ permission.matching   （复用命令/路径匹配算法）
      ──→ permission.blacklist  （F4.1：Hook 命令过①黑名单）
      ──→ permission.network    （F4.3：http 动作过结构性硬校验）
      ──→ trace                 （埋点）

agent.loop ──→ hooks   （工具级三事件 + 拦截结论合并）
conversation ──→ hooks （回合级两事件 + 注入通道 + 报告）
context.manager ──→ hooks （压缩两事件）
tui.app ──→ hooks      （消息级两事件 + notification）
bootstrap ──→ hooks    （装配、会话级两事件）
```

**`permission` 绝不反向依赖 `hooks`**——这是无环的唯一依靠，也是下面第一条技术决策的直接后果。

`hooks` 不是叶子包（它依赖 `permission` 与 `trace`），因此**不能**被 `permission` / `trace` / `skills` 依赖。

## 核心数据结构

### HookEventType

十二个事件的枚举（`str, Enum`，与 `AgentEventType` / `TraceEventType` 同风格）：

```
SESSION_START / SESSION_END
TURN_START / TURN_END
USER_MESSAGE / ASSISTANT_MESSAGE
PRE_TOOL_USE / POST_TOOL_USE / POST_TOOL_USE_FAILURE
PRE_COMPACT / POST_COMPACT / NOTIFICATION
```

### EVENT_FIELDS

`dict[HookEventType, frozenset[str]]`——每个事件的**可用字段名全集**（公共字段 + 专有字段）。

这张表是 spec F3.2 字段校验与 F8 第 4 项的**单一事实来源**：`parser` 用它校验条件里的字段名，
负载构造点用它作为「该填哪些字段」的依据。

⚠️ 新增事件或新增字段时，枚举、本表、负载构造点三处必须同步。漏改本表的后果是
**条件里写对了字段名反而被判为非法、整条规则被丢弃**——用户会以为是自己写错了。

### Matcher / Condition

```
Matcher:
    field: str            条件左侧的字段名
    negated: bool         值是否以 "!" 开头
    kind: str             "exact" | "glob" | "regex"
    pattern: str          去掉 "!" 与正则包裹后的模式原文
    regex: Pattern|None   kind == "regex" 时预编译（加载期编译，求值期不再编译）

Condition:
    combine: str                  "all" | "any"
    matchers: tuple[Matcher,...]
```

`if` 省略时规则的 `condition` 为 `None`，求值恒真。

**匹配形态在加载期一次判定完毕**，求值期只做分支执行——正则在加载期编译（F8 第 5 项校验
顺带完成），避免每次事件都重新编译。

### FIELD_MATCH_KIND

`dict[str, str]`——字段名 → glob 匹配算法（`"command"` / `"path"` / `"plain"`）。

spec F3.3 要求 glob 形态「命令类字段复用命令匹配、路径类字段复用路径匹配、其余用通用通配」，
这张表就是那个映射。`command` 走 `permission.matching.match_command`（前缀 + 词边界），
`file_path` / `cwd` 等走 `match_path`（gitignore 风格），未登记的字段走 `fnmatch`。

### 四种动作

```
CommandAction:  command: str, timeout: int
PromptAction:   text: str
HttpAction:     url: str, method: str, headers: dict, body: str|None, timeout: int
AgentAction:    prompt: str
```

### HookRule

```
HookRule:
    name: str                  显式 name，或按 "<来源层>#<层内序号>" 生成
    source: str                "user" | "project"
    index: int                 层内声明序（用于 F9 的固定执行顺序）
    event: HookEventType
    condition: Condition|None
    action: CommandAction|PromptAction|HttpAction|AgentAction
    once: bool
    run_async: bool            （不叫 async——那是 Python 关键字）
```

`frozen=True`：规则一旦解析出来就不该被改写，`once` 的消耗状态存在 manager 里而不是规则上。

### HookPayload

```
HookPayload:
    event: HookEventType
    fields: dict[str, Any]     公共三字段 + 事件专有字段 + tool_input 逐字展开
```

`tool_input` 展开与公共字段重名时**公共字段优先**（spec F3.2），冲突在加载期给警告。

### 结论类型

```
HookDecision(Enum):  DENY | ASK | NONE

HookVerdict:
    decision: HookDecision
    reason: str            面向模型的中文原因
    rule_name: str         做出该结论的规则标识

ActionOutcome:
    ok: bool               动作自身是否成功（与拦截结论无关）
    verdict: HookDecision  该动作产出的结论（非 pre_tool_use 恒为 NONE）
    reason: str
    detail: str            stdout/stderr/响应状态的摘要，进 trace
    duration_ms: float
    injected_text: str     prompt 动作的产物，其余为空串

DispatchResult:
    verdict: HookVerdict   合并后的结论（无规则命中时 decision=NONE）
    matched: int           命中规则数
    executed: int          实际执行数（扣除 once 已消耗的）
```

## 模块设计

### `models.py`

**职责**：定义上述全部数据结构与两张表（`EVENT_FIELDS`、`FIELD_MATCH_KIND`）。零行为。

**依赖**：标准库。

### `conditions.py`

**职责**：两个纯函数。

- `parse_matcher(field, raw_value) -> Matcher | 错误`：识别 `!` 前缀与 `/.../` 包裹，
  判定形态，正则形态预编译。
- `evaluate(condition, payload) -> bool`：按 `combine` 短路求值。

**关键语义**（spec F3.3 末段，容易写错）：

- 字段值非字符串时先 `str()` 化（布尔 → `true`/`false`，数字 → 十进制）。
- **字段不存在或为 `None` 时，该匹配项判为不成立——反向匹配也不成立**。
  即「字段不存在」不等于「不匹配」。写成两条独立判断，不要让 `negated` 去翻转缺失分支。
- `all` 空匹配列表恒真，`any` 空匹配列表恒假（加载期已保证列表非空，这里是防御性定义）。

**依赖**：`models`、`permission.matching`、标准库 `re` / `fnmatch`。

### `parser.py`

**职责**：`parse_rules(data, source) -> (list[HookRule], list[str])`——把一层 YAML 解析成规则列表
与警告列表。F8 的八项校验全部在这里。

**校验失败的处理**：硬校验项（1–7）任一不通过 → **丢弃该条规则**，往警告列表追加一条中文说明，
继续解析下一条。第 8 项是提示级，不丢弃。

**整层降级的三处**（照搬 `permission/config.py` 的既有结构，理由完全相同）：
YAML 解析失败、顶层非映射、`hooks` 字段非列表 → **整层降级为空**。
⚠️ 不要把第三处改成「跳过该字段继续」——那会让一个写坏的文件变成「部分规则生效」，
而用户以为整份都生效了。

**依赖**：`models`、`conditions`。

### `config.py`

**职责**：路径定位与加载。

- `user_config_path(user_dir=None)` / `project_config_path()`——与 `permission/config.py` 同构，
  `user_dir` 必须可选（测试与端到端宿主靠它重定向到临时目录）。
- `load_all(user_dir=None) -> (list[HookRule], list[str])`——按「用户级 → 项目级」顺序加载并拼接，
  这个顺序**即 spec F9 的执行顺序**。
- `scaffold_user_config(path)`——首次运行生成全注释模板，与 `permissions.yaml` 同构
  （全注释解析后为空，「有模板」与「无文件」行为等价）。

**依赖**：`parser`、`yaml`、`tools.path_guard.workspace_root`。

### `actions.py`

**职责**：四个执行器，签名统一为 `(action, payload) -> ActionOutcome`。

**`run_command_action`**
1. 先过①黑名单（`permission.blacklist.check_command`）。命中 → `ok=False`，
   `detail` 写明「命中危险命令黑名单」，**不执行**。
2. 负载序列化为 JSON，经 `subprocess.run(..., input=..., shell=True, cwd=项目根, timeout=N)`
   喂给标准输入。
3. **必须自己解码 stdout/stderr**（`capture_output=True` 不加 `text=True`，拿 bytes 后按
   UTF-8 → 本地编码依次尝试）。理由与 `tools/run_command.py` 完全相同：Windows 中文环境下
   `text=True` 会在 subprocess 的读取线程里抛 `UnicodeDecodeError`，异常不在调用栈上、
   输出被静默吞成空串。**照抄那份解码逻辑，不要新写一份。**
4. 退出码 0 → 尝试把 stdout 解析为决策 JSON；2 → `DENY`，stderr 作原因；
   其它非 0 / 超时 / 启动失败 → `ok=False`。

**`run_http_action`**
1. 先过 `permission.network.check_hard(url)`。命中 → `ok=False`，不发请求。
2. 用 `httpx` 发请求，超时按 `timeout`。
3. 非 2xx / 连接失败 / 超时 → `ok=False`。响应内容只进 `detail`，**不产出任何 verdict**。

**`run_prompt_action`** —— 无 I/O，直接返回 `injected_text=action.text`、`ok=True`。

**`run_agent_action`** —— 不执行，返回 `ok=True`、`verdict=NONE`、
`detail="子 Agent 动作尚未支持（等待 SubAgent 章节接入）"`。
⚠️ **`ok` 必须为 True**：spec F4.4 要求它在 `pre_tool_use` 上按「不表态」而非「失败」处理，
否则 F7.1 的 fail-closed 会让一条占位规则把所有工具调用拦死。

**依赖**：`models`、`permission.blacklist`、`permission.network`、`httpx`、`subprocess`。

### `manager.py`

**职责**：`HookManager`——本包唯一有状态的类。

**对外接口**

| 方法 | 用途 |
| --- | --- |
| `enabled` | 是否有任何规则（`False` 时全部分发是零成本空操作） |
| `has_listeners(event)` | 该事件是否有规则监听——**分发前必须先问它** |
| `dispatch(event, payload_factory)` | 分发一次事件，返回 `DispatchResult` |
| `consume_injections()` | 取走并清空待注入文本（`prompt` 动作产物） |
| `report()` | `/hooks` 的完整报告 |
| `project_notice()` | 项目级规则的逐条启动提示（F9.1），无项目级规则时返回 `None` |
| `warnings` | 加载期警告列表 |

**内部状态**（全部受一把 `threading.Lock` 保护）：`once` 已消耗集合、
每条规则的触发统计、待注入文本队列。

**⚠ 加锁不变量（与 C11 `SkillManager` 同一课，但后果不同）**

> **锁的临界区只做纯内存读写。动作执行、trace 埋点、跨线程调度一律在锁外。**

C11 那次违反会与 Textual 的阻塞式 `call_from_thread` 组成死锁；这里违反的后果是
**一个 60 秒超时的 command 动作把整个 manager 锁死**——期间任何线程的任何事件分发全部阻塞，
表现为界面假死而调用栈上看不出原因。

具体形态：`dispatch` 写成「**持锁**筛选命中规则并标记 `once` → **出锁** → 执行动作 →
**持锁**写统计 → **出锁** → 埋点」。

**`payload_factory` 是惰性的**（spec N7）：`has_listeners` 为假时**根本不调用它**。
这与 `trace` 的 `emit_lazy` 是同一形态、同一理由——`pre_tool_use` 的负载要展开整个
`tool_input`（一次 `write_file` 就是整份文件内容），零命中时白构造一遍不可接受。

**结论合并**（F6.2）：按 `HookDecision` 的偏严序取最严——`DENY > ASK > NONE`，
并记住做出该结论的第一条规则名。

**失败处理**（F7）：`event == PRE_TOOL_USE` 且 `outcome.ok == False` → 记为 `DENY`，
原因文案带「Hook 自身执行失败」前缀；其余事件 → 只记 trace 与界面提示。

**`async` 动作**：`threading.Thread(daemon=True)` 起一个，不 join。它的 `ActionOutcome`
不参与结论合并（`pre_tool_use` 已在加载期禁止 `async`，所以不存在「异步却要拿结论」的情形）。

**trace 埋点**：`dispatch` 入口产 `HOOK_DISPATCH`（**零命中也产**），每个动作产 `HOOK_EXECUTE`。
埋点一律经本类的受保护漏斗（同 `agent/loop.py` 的 `_safe_emit`），任何异常吞掉。

**依赖**：`models`、`conditions`、`actions`、`report`、`trace`。

### `report.py`

**职责**：两个纯函数——`render_report(rules, warnings, stats)` 与
`render_project_notice(project_rules)`。

`render_project_notice` 是 spec F9.1 的实现：**逐条**列出事件与动作原文，
`command` 展示完整命令串、`http` 展示完整 URL，**不折叠、不截断、不只给计数**。

⚠️ 两个函数的输出都会进 Textual markup 通道，**含字面 `[` 的内容必须用
`tui/widgets.py` 的 `escape`**，绝不用 rich 那版。命令串里出现 `[` 是常事
（`jq '.[]'`），落单的 `[` 会在布局阶段主线程抛 `MarkupError`，没有 try/except 兜得住。

**依赖**：`models`。（转义在调用方 TUI 侧完成，本模块保持零依赖。）

### 空实现：`NullHookManager`

与 `trace.NullRecorder` 同形态：`enabled=False`、`has_listeners` 恒 `False`、
`dispatch` 恒返回空 `DispatchResult`、`consume_injections` 恒返回空串。

**存在理由**：让全部分发点写成无条件调用，不必在五个文件里各写一次 `if manager is not None`。
spec N1「缺省零回归」由它保证。

## 决策管线的接入

### 位置：Agent Loop，不在权限引擎里

```
agent/loop.py  _execute 决策预扫
    │
    ├─ hook_verdict = hooks.dispatch(PRE_TOOL_USE, payload_factory)
    │
    ├─ verdict == DENY  → 不调 engine，产结构化拒绝结果回灌模型
    │
    └─ 否则 decision = engine.decide(request)
           └─ verdict == ASK 且 decision == ALLOW → 改判为 ASK，layer=HOOK
              （decision 已是 DENY 或 ASK 时**原样保留**）
```

**「ASK 只能把 ALLOW 升级、不能把 DENY 降级」是 spec AC15 的实现要点。**
写成「命中 ASK 就置为 ASK」会让一条 Hook 把黑名单的 DENY 变成一次可点「同意」的确认面板——
这正是决策 1A 要杜绝的那件事。

### 新增 `Layer.HOOK`

`ASK` 升级后要有一个层标，供确认面板与 trace 展示。这触发**三份刻意不合一的表**：

- `permission/models.py` 的 `Layer` 枚举
- `trace/reader.py` 的 `_LAYER_NAMES`
- `tui/widgets.py` 的 `ConfirmPanel._LAYER_LABELS`

三份不合一是既有决定（合并会让只依赖标准库的 `trace` 叶子包反向依赖 `permission`）。
一致性由 `tests/test_trace_reader.py` 与 `tests/test_web_bootstrap.py` 里两条遍历 `Layer`
的断言钉住，**漏改当场红**。

## 十二个事件的分发点

| 事件 | 分发位置 | 备注 |
| --- | --- | --- |
| `session_start` | `bootstrap.build_app` 末尾（trace `session_start` 之后）；`conversation.clear()`；`conversation._resume_stream` | `source` 三取值分别对应三处 |
| `session_end` | `bootstrap.cleanup`；`clear()`；`_resume_stream` | 与上一行成对 |
| `turn_start` | `conversation._wrap_events` 起点（取令牌之后、首次 `yield` 之前） | **唯一包装点**，三条执行路径都过它 |
| `turn_end` | `conversation._wrap_events` 的 `finally` | `stop_reason` 由循环中捕获的 FINISHED 事件带出 |
| `user_message` | `tui/app.py` 的真人提交入口，**命令分发之前** | `is_command` 由命令解析结果填 |
| `assistant_message` | `tui/app.py._do_stream` 的 `reset_text_widgets()` 全部调用点 | 与 `ui_message` 埋点同位置，产出条件一致 |
| `pre_tool_use` | `agent/loop.py._execute` 决策预扫 | 串行段，不在只读并发桶内 |
| `post_tool_use` / `post_tool_use_failure` | `_run_one_serial` 与 `_run_readonly_concurrent` 里 `outcome == EXECUTED` 的两处 | **只挂在 `OUTCOME_EXECUTED` 上**——这是 spec AC3 六种「没执行」分支不触发的实现依据 |
| `pre_compact` / `post_compact` | `context/manager.py._do_summary` 首尾 | 只有第二层，第一层存盘不挂 |
| `notification` | `tui/app.py._interact` 三处交互入口 + `_do_stream` 的 FINISHED 分支 | `kind` 四取值 |

**`turn_start` / `turn_end` 挂在 `_wrap_events` 上是本设计最省事的一处**：它已经是
「每一次 Agent 执行的唯一包装点」，主对话 / 用户触发的子对话 / 模型自行发起的子对话
三条路径都经过，`try/finally` 的语义还顺带保证了取消与出错时 `turn_end` 照样产出。

## `prompt` 动作的注入通道

```
hooks.dispatch(...) → ActionOutcome.injected_text
    → HookManager 内部队列
    → conversation._run 的 dynamic_provider() 里 consume_injections()
    → build_system_reminder 拼进 <system-reminder>
    → 下一次 API 请求
```

**一次性由 `consume` 语义保证**：取走即清，队列为空则该段不出现。

⚠️ 注入段的位置：排在「环境信息 → 已激活 Skill → 一次性提醒」之后，作为最后一段。
`dynamic_provider` 是**每轮求值**的 callable（C11 改造点 1），因此第 N 轮触发的 Hook
注入能在第 N+1 轮被看到——这正是 `prompt` 动作在 `pre_tool_use` 上仍然有意义的原因。

## 文件组织

```
rhinecode/
├── hooks/                      ← 新增包
│   ├── __init__.py             对外导出 + NullHookManager
│   ├── models.py               枚举、两张表、全部数据类
│   ├── conditions.py           匹配形态识别与条件求值（纯函数）
│   ├── parser.py               YAML → HookRule + 八项校验（纯函数）
│   ├── config.py               两层路径、加载、模板生成
│   ├── actions.py              四种动作执行器
│   ├── manager.py              HookManager
│   └── report.py               报告与项目级提示渲染（纯函数）
├── permission/models.py        ← 改：Layer 新增 HOOK
├── agent/loop.py               ← 改：三个工具级事件 + 拦截结论合并
├── conversation.py             ← 改：回合级两事件、会话级两处、注入通道、报告方法
├── context/manager.py          ← 改：压缩两事件
├── bootstrap.py                ← 改：装配 HookManager、会话级两事件、项目级提示
├── commands/builtins.py        ← 改：登记 /hooks
├── tui/app.py                  ← 改：消息级两事件 + notification + 报告展示
├── tui/widgets.py              ← 改：ConfirmPanel._LAYER_LABELS 加 HOOK
├── trace/models.py             ← 改：两个新事件类型
└── trace/reader.py             ← 改：_LAYER_NAMES 加 HOOK + SUMMARIZERS 加两行
```

## 技术决策

| 决策点 | 选择 | 理由 |
| --- | --- | --- |
| Hook 层放在哪 | **Agent Loop 的决策预扫里**，不在 `permission/engine.py` | ① `permission/` 全包的既有性质是「纯判定、零 I/O、副作用：无」，Hook 要起子进程发网络请求，塞进去会让那句话变成假话，而权限层是安全边界，可预测性优先（与 trace「埋在调用点而非引擎内部」同一先例）。② `engine.decide` 还有第二个调用点——glob/grep 的逐文件过滤器，一次 grep 几百次判定；放引擎里会跑几百次 Hook |
| 破环方式 | `hooks → permission` 单向 | Hook 需要复用匹配算法、黑名单、网络硬校验；反向依赖一条都没有，天然无环 |
| 缺省关闭的表达 | **Null 对象**（`NullHookManager`），不用 `Optional` + 判空 | 五个分发点各写一次判空是「漏一处不报错」的典型形态；对齐 `trace.NullRecorder` 的既有先例 |
| 负载构造 | **惰性 `payload_factory`** + `has_listeners` 前置判断 | spec N7。`pre_tool_use` 负载含整个 `tool_input`，零命中时白构造不可接受；对齐 `emit_lazy` |
| 并发保护 | 一把 `Lock`，**临界区只做内存读写** | C11 加锁不变量同一课。这里违反的后果是「一个超时的 hook 锁死整个 manager，界面假死」 |
| `once` 状态存哪 | `HookManager` 内部集合，**不在 `HookRule` 上** | 规则 `frozen=True` 不可变；且 spec 明确不持久化，状态天然属于「本次运行」而非「规则定义」 |
| 正则编译时机 | **加载期**编译并存进 `Matcher` | 顺带完成 F8 第 5 项校验；求值期零编译开销 |
| 匹配形态判定时机 | **加载期**判定并存进 `Matcher.kind` | 求值期只做分支，条件求值保持零 I/O 零解析 |
| 字段校验的事实来源 | `EVENT_FIELDS` 一张表 | 校验与「该填哪些字段」共用，避免两份清单漂移 |
| 子进程输出解码 | **照抄 `tools/run_command.py` 的手工解码** | `text=True` 在 Windows 中文环境下会在 subprocess 读取线程抛 `UnicodeDecodeError`，异常不在调用栈上、输出被静默吞空 |
| `agent` 占位的 `ok` 值 | **`True`**（不表态，非失败） | 否则 F7.1 的 fail-closed 会让一条占位规则拦死所有工具调用 |
| ASK 升级的边界 | **只把 ALLOW 升级为 ASK**，DENY 原样保留 | spec AC15。写成无条件置 ASK 会让 Hook 把黑名单 DENY 变成可点同意的面板 |
| `/hooks` 报告的转义 | 调用方（TUI）用 `tui/widgets.py` 的 `escape` | rich 那版放过被截断的 `[`，会在布局阶段主线程抛 `MarkupError` 拆掉整个 app |

## 本章新增的成对维护点

实现完成后要补进 `CLAUDE.md`：

1. **新增 Hook 事件** → `hooks/models.py` 的 `HookEventType` + 同文件 `EVENT_FIELDS` + 负载构造点。
   漏改 `EVENT_FIELDS` 的后果是**条件里写对了字段名反被判非法、整条规则丢弃**，用户会以为是自己写错了。
2. **新增 `Layer` 枚举值** → 既有三处（`permission/models.py` + `trace/reader.py` 的 `_LAYER_NAMES`
   + `tui/widgets.py` 的 `_LAYER_LABELS`）。本章新增 `HOOK` 即为一例，漏改当场红。
3. **新增 trace 事件类型** → `trace/models.py` 枚举 + `trace/reader.py` 的 `SUMMARIZERS` 表。
   本章新增两类。
4. **新增 Hook 动作类型** → `hooks/models.py`（数据类）+ `hooks/parser.py`（校验分支）
   + `hooks/actions.py`（执行器）+ `hooks/report.py`（展示分支）。**漏改 report 不报错**，
   只是 `/hooks` 与项目级启动提示里那条动作显示成空白——而项目级提示正是 F9.1 的全部安全价值所在。
5. **新增可 glob 匹配的字段** → `hooks/models.py` 的 `FIELD_MATCH_KIND`。漏改不报错，
   只是该字段从「命令/路径语义匹配」悄悄退化成通用通配（`Bash(git *)` 的词边界语义丢失，
   `git *` 会连 `github-cli` 一起命中）。
