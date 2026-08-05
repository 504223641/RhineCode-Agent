# C12 Hook 系统 Tasks

> 状态：**已批准**（2026-08-05，第 1 轮）
>
> 依据：已批准的 [`spec.md`](spec.md) 与 [`plan.md`](plan.md)。

## 文件清单

### 新建

| 文件 | 职责 |
| --- | --- |
| `rhinecode/hooks/__init__.py` | 对外导出 + `NullHookManager` |
| `rhinecode/hooks/models.py` | 枚举、两张表、全部数据类（零 I/O） |
| `rhinecode/hooks/conditions.py` | 匹配形态识别与条件求值（零 I/O） |
| `rhinecode/hooks/parser.py` | YAML → `HookRule` + 八项校验（零 I/O） |
| `rhinecode/hooks/config.py` | 两层路径、加载、模板生成 |
| `rhinecode/hooks/actions.py` | 四种动作执行器 |
| `rhinecode/hooks/manager.py` | `HookManager` |
| `rhinecode/hooks/report.py` | 报告与项目级提示渲染（零 I/O） |
| `tests/test_hook_conditions.py` | 四种匹配形态、`all`/`any`、缺失字段语义 |
| `tests/test_hook_parser.py` | 八项校验、整层降级、两层加载顺序 |
| `tests/test_hook_actions.py` | 四种动作，含黑名单拦截与网络硬校验 |
| `tests/test_hook_manager.py` | 分发、`once`、结论合并、失败语义、惰性负载、加锁不变量 |
| `tests/test_hook_intercept.py` | Agent Loop 集成：拦截、ASK 升级、AC15 反证 |
| `tests/test_hook_dispatch_points.py` | 十二个事件分发点的结构护栏 |
| `tests/test_hook_trace.py` | 两类 trace 事件与阅读器摘要 |
| `tests/test_hook_command.py` | `/hooks` 报告与项目级启动提示 |
| `tests/test_hook_zero_regression.py` | 无配置时零行为 |

### 修改

| 文件 | 改动 |
| --- | --- |
| `rhinecode/permission/models.py` | `Layer` 新增 `HOOK` |
| `rhinecode/trace/models.py` | `TraceEventType` 新增两类 |
| `rhinecode/trace/reader.py` | `_LAYER_NAMES` 加 `HOOK`；`SUMMARIZERS` 加两行 |
| `rhinecode/tui/widgets.py` | `ConfirmPanel._LAYER_LABELS` 加 `HOOK` |
| `rhinecode/agent/loop.py` | `pre_tool_use` 分发与结论合并；两处 `post_tool_use*` |
| `rhinecode/conversation.py` | 回合级两事件、注入通道、会话级两处、`hooks_report` |
| `rhinecode/context/manager.py` | 压缩两事件 |
| `rhinecode/tui/app.py` | 消息级两事件、`notification` 四处、`query_report` 分支 |
| `rhinecode/commands/models.py` | `ReportTarget` 新增 `HOOKS` |
| `rhinecode/commands/builtins.py` | 登记 `/hooks` + 处理函数 |
| `rhinecode/bootstrap.py` | 装配 `HookManager`、会话级两事件、项目级提示 |
| `rhinecode/__main__.py` | 首次运行生成 `hooks.yaml` 模板 |
| `.gitignore` | 无需改动（`hooks.yaml` 是用户要提交的配置，刻意不忽略） |
| `CLAUDE.md` | 能力表加一行、架构表加一层、成对维护点加五条、安全边界加一节 |
| `docs/internals/*.md` | 架构 / 能力 / 测试 / 配置四份分册各补 Hook 章节 |

---

## 阶段一：纯数据层

### T1: 事件枚举与字段表

**文件：** `rhinecode/hooks/models.py`
**依赖：** 无
**步骤：**
1. 定义 `HookEventType(str, Enum)`，十二个成员，取值为 spec F2 的事件名字符串。
2. 定义 `COMMON_FIELDS: frozenset` = `{"event", "session_id", "cwd"}`。
3. 定义 `EVENT_FIELDS: dict[HookEventType, frozenset[str]]`，每个事件的值 =
   `COMMON_FIELDS | 该事件专有字段`，逐条对照 spec F2 的表填写。
4. 定义 `FIELD_MATCH_KIND: dict[str, str]`：`command` → `"command"`；
   `file_path` / `cwd` / `path` / `pattern` → `"path"`；其余不登记（默认 `"plain"`）。
5. 在模块 docstring 与 `EVENT_FIELDS` 上方写明成对维护点：新增事件须同步枚举、
   本表、负载构造点，并写清漏改后果（条件写对反被判非法）。

**验证：** `python -c "from rhinecode.hooks.models import EVENT_FIELDS, HookEventType; assert set(EVENT_FIELDS) == set(HookEventType); print(len(HookEventType))"` 输出 `12`。

### T2: 条件与动作数据类

**文件：** `rhinecode/hooks/models.py`
**依赖：** T1
**步骤：**
1. 定义 `Matcher`（`frozen=True`）：`field` / `negated` / `kind` / `pattern` / `regex`。
2. 定义 `Condition`（`frozen=True`）：`combine` / `matchers`。
3. 定义四个动作数据类（均 `frozen=True`）：`CommandAction`、`PromptAction`、
   `HttpAction`、`AgentAction`，字段按 plan「四种动作」一节。
4. 定义类型别名 `HookAction = Union[CommandAction, PromptAction, HttpAction, AgentAction]`。
5. 定义常量 `DEFAULT_COMMAND_TIMEOUT = 60`、`DEFAULT_HTTP_TIMEOUT = 10`。

**验证：** `python -m compileall rhinecode/hooks/models.py` 通过；
构造一个 `CommandAction("ls", 30)` 并确认 `dataclasses.replace` 可用、赋值抛 `FrozenInstanceError`。

### T3: 规则、负载与结论数据类

**文件：** `rhinecode/hooks/models.py`
**依赖：** T2
**步骤：**
1. 定义 `HookRule`（`frozen=True`）：`name` / `source` / `index` / `event` /
   `condition` / `action` / `once` / `run_async`。字段名用 `run_async` 而非 `async`。
2. 定义 `HookPayload`：`event` + `fields: dict[str, Any]`。
3. 定义 `HookDecision(str, Enum)`：`DENY` / `ASK` / `NONE`。
4. 定义 `HookVerdict`（`frozen=True`）：`decision` / `reason` / `rule_name`；
   提供一个模块级常量 `NO_VERDICT` 作为「无结论」的单例。
5. 定义 `ActionOutcome`、`DispatchResult`（字段按 plan）。
6. 定义 `SEVERITY: dict[HookDecision, int]` = `{NONE: 0, ASK: 1, DENY: 2}`，
   供结论合并取最严；写明「新增结论取值须同步本表」。

**验证：** `python -c "from rhinecode.hooks.models import SEVERITY, HookDecision; assert set(SEVERITY) == set(HookDecision)"` 无输出即通过。

---

## 阶段二：条件求值（零 I/O）

### T4: 匹配形态识别

**文件：** `rhinecode/hooks/conditions.py`
**依赖：** T3
**步骤：**
1. 实现 `parse_matcher(field: str, raw: str) -> tuple[Matcher | None, str | None]`
   （返回「匹配项，错误说明」，二者恰有一个非 None）。
2. 先剥 `!` 前缀置 `negated`（**只剥一个**，`!!x` 视为匹配字面量 `!x`）。
3. 再判 `/.../` 包裹 → `kind="regex"`，剥掉首尾 `/` 后 `re.compile`，
   编译失败返回错误说明。
4. 否则含 `*` → `kind="glob"`；不含 `*` → `kind="exact"`。
5. 空模式（剥完为空串）返回错误说明。

**验证：** 交互式确认五种输入的解析结果：`"run_command"` → exact 非反向；
`"!run_command"` → exact 反向；`"git *"` → glob；`"/^git/"` → regex 且 `regex` 非 None；
`"/[/"` → 返回错误说明而非抛异常。

### T5: 单个匹配项求值

**文件：** `rhinecode/hooks/conditions.py`
**依赖：** T4
**步骤：**
1. 实现 `_match_one(matcher, fields) -> bool`。
2. **先处理缺失**：字段不在 `fields` 中或值为 `None` → **直接返回 False**，
   `negated` 不参与。在此处写注释说明「字段不存在 ≠ 不匹配」。
3. 值非字符串时 `str()` 化；布尔转成小写 `true` / `false`（不要用 Python 的 `True`）。
4. 按 `kind` 分支：`exact` 用 `==`；`regex` 用 `matcher.regex.search`；
   `glob` 查 `FIELD_MATCH_KIND` 选 `match_command` / `match_path` / `fnmatch.fnmatch`。
5. 最后按 `negated` 取反（缺失分支已在第 2 步提前返回，不会被取反）。

**验证：** 见 T6 的测试任务；本任务先跑 `python -m compileall`。

### T6: 条件组合求值 + 测试

**文件：** `rhinecode/hooks/conditions.py`、`tests/test_hook_conditions.py`
**依赖：** T5
**步骤：**
1. 实现 `evaluate(condition, fields) -> bool`：`condition is None` 恒真；
   `all` 用 `all()` 短路，`any` 用 `any()` 短路；空列表按 plan 的防御性定义。
2. 写测试：四种匹配形态各一条命中 + 一条不命中。
3. 写测试：`all` 与 `any` 的组合语义各两条。
4. 写测试：**缺失字段的三条**——普通匹配不成立、反向匹配**也**不成立、
   值为 `None` 时同缺失。
5. 写测试：布尔字段 `is_read_only=True` 能被 `"true"` 精确命中。
6. 写测试：glob 形态下 `command: "git *"` **不**命中 `github-cli`（词边界语义确实
   复用了 `match_command`，而不是退化成 `fnmatch`）。

**验证：** `python -m unittest tests.test_hook_conditions` 全绿。

---

## 阶段三：解析与加载

### T7: 动作解析

**文件：** `rhinecode/hooks/parser.py`
**依赖：** T6
**步骤：**
1. 实现 `_parse_action(raw: dict) -> tuple[HookAction | None, str | None]`。
2. 按 `type` 分四支，各自校验必填字段存在且类型正确（`command`/`text`/`url`/`prompt`）。
3. 可选字段填默认值：`timeout` 缺省取常量、`method` 缺省 `"POST"`、`headers` 缺省空 dict。
4. `timeout` 非正数 → 错误说明（F8 第 7 项）。
5. 未知 `type` → 错误说明，列出四个合法取值。

**验证：** 交互式构造四种合法动作与三种非法动作，确认合法的返回实例、非法的返回中文说明。

### T8: 规则解析与八项校验

**文件：** `rhinecode/hooks/parser.py`
**依赖：** T7
**步骤：**
1. 实现 `_parse_rule(raw, source, index) -> tuple[HookRule | None, list[str]]`。
2. 依次校验 F8 的第 1、3、4、6 项：`event` 合法；`if` 下 `all`/`any` 恰好一个；
   每个字段名属于 `EVENT_FIELDS[event]`；`pre_tool_use` 未声明 `async: true`。
3. 调 `_parse_action`（第 2、7 项）与 `parse_matcher`（第 5 项）。
4. 第 8 项（非 `pre_tool_use` 挂了会产决策的动作）产出**提示级**警告，**不丢弃**。
5. `name` 缺省生成为 `f"{source}#{index + 1}"`。
6. 任一硬校验失败 → 返回 `(None, [中文说明])`；说明里必须包含规则标识与具体哪一项不通过。

**验证：** 见 T9。

### T9: 层解析 + 整层降级 + 测试

**文件：** `rhinecode/hooks/parser.py`、`tests/test_hook_parser.py`
**依赖：** T8
**步骤：**
1. 实现 `parse_rules(data, source) -> (list[HookRule], list[str])`。
2. **三处整层降级**（照搬 `permission/config.py` 结构）：YAML 顶层非映射、
   `hooks` 字段存在但非列表、列表元素非映射时该条跳过——前两者整层返回空。
3. 写测试覆盖八项校验各一条。
4. 写测试：一条规则写坏时**其余规则照常加载**、警告非空。
5. 写测试：`hooks` 字段非列表时**整层降级为空**（附一条反证注释说明
   为什么不能改成「跳过该字段继续」）。

**验证：** `python -m unittest tests.test_hook_parser` 全绿。

### T10: 配置文件加载

**文件：** `rhinecode/hooks/config.py`
**依赖：** T9
**步骤：**
1. 实现 `user_config_path(user_dir=None)`（`user_dir` 必须可选）与 `project_config_path()`。
2. 实现 `_load_layer(path, source)`：不存在返回空；读取解析异常 → 空 + 一条警告。
3. 实现 `load_all(user_dir=None)`：按「用户级 → 项目级」拼接，`index` 在层内从 0 递增。
4. 实现 `scaffold_user_config(path)`：已存在返回 False；否则写入全注释模板。
5. 模板内容要含一条完整可用的示例（注释掉），并写明「项目级 hooks 会被逐条展示」。

**验证：** 在临时目录写两层文件后调 `load_all`，确认规则顺序为「用户级全部 → 项目级全部」，
且 `source` 字段正确。

### T11: 两层加载顺序测试

**文件：** `tests/test_hook_parser.py`
**依赖：** T10
**步骤：**
1. 写测试：两层都有规则时，返回顺序为用户级在前、项目级在后、层内按声明序。
2. 写测试：只有项目级时正常加载。
3. 写测试：用户级文件损坏时该层降级为空、项目级照常加载、警告非空。

**验证：** `python -m unittest tests.test_hook_parser` 全绿。

---

## 阶段四：动作执行器

### T12: prompt 与 agent 动作

**文件：** `rhinecode/hooks/actions.py`
**依赖：** T3
**步骤：**
1. 实现 `run_prompt_action(action, payload) -> ActionOutcome`：
   `ok=True`、`verdict=NONE`、`injected_text=action.text`。
2. 实现 `run_agent_action(action, payload) -> ActionOutcome`：
   **`ok=True`**、`verdict=NONE`、`detail="子 Agent 动作尚未支持（等待 SubAgent 章节接入）"`。
3. 在 `run_agent_action` 上方写注释说明 `ok=True` 不可改成 `False` 的理由
   （F7.1 的 fail-closed 会让占位规则拦死所有工具调用）。

**验证：** `python -c` 构造两个动作调用，确认 `ok` 均为 True、`injected_text` 符合预期。

### T13: command 动作

**文件：** `rhinecode/hooks/actions.py`
**依赖：** T12
**步骤：**
1. 先调 `permission.blacklist.check_command(action.command)`，命中 → `ok=False`，
   `detail` 写明命中黑名单，**不执行**。
2. `json.dumps(payload.fields, ensure_ascii=False)` 作为 stdin。
3. `subprocess.run(action.command, shell=True, cwd=workspace_root(), input=<bytes>,
   capture_output=True, timeout=action.timeout)`。
4. **手工解码 stdout/stderr**：把 `tools/run_command.py` 的解码函数抽出来复用
   （不要复制第二份），UTF-8 失败时回退本地编码。
5. 退出码分支：0 → 试解析 stdout 决策 JSON；2 → `DENY` + stderr 作原因；
   其它 → `ok=False`。
6. `TimeoutExpired` / `OSError` → `ok=False`，`detail` 写明原因。

**验证：** 手工跑一条 `echo hi` 的动作确认 `ok=True`；跑一条 `exit 2` 确认 `verdict=DENY`；
跑一条 `rm -rf /` 确认被黑名单拦下且未执行。

### T14: 决策 JSON 解析

**文件：** `rhinecode/hooks/actions.py`
**依赖：** T13
**步骤：**
1. 实现 `_parse_decision(stdout) -> tuple[HookDecision, str]`。
2. stdout 为空或非 JSON 对象 → `(NONE, "")`，**不算失败**（spec F6.1「不表态」）。
3. `decision` 取值为 `"deny"` / `"ask"` → 对应枚举，`reason` 取同名字段。
4. `decision` 为 `"allow"` 或其它未知值 → **按 `NONE` 处理**并在 `detail` 里注明
   「Hook 无法放行，该结论已忽略」。这是决策 1A 的最后一道闸。

**验证：** 交互式确认 `{"decision":"allow"}` 得到 `NONE` 而非放行语义。

### T15: http 动作

**文件：** `rhinecode/hooks/actions.py`
**依赖：** T14
**步骤：**
1. 先调 `permission.network.check_hard(action.url)`，命中 → `ok=False`，不发请求。
2. 用 `httpx.Client` 发请求，`timeout=action.timeout`；`body` 省略时发
   `json.dumps(payload.fields)`，并默认带 `Content-Type: application/json`。
3. 非 2xx / `httpx` 异常 / 超时 → `ok=False`，`detail` 记状态码或异常。
4. **`verdict` 恒为 `NONE`**——响应不参与任何决策（spec F4.3）。在此处写注释说明理由。
5. 客户端工厂可注入（`client_factory` 参数，缺省真 `httpx.Client`），
   形态照抄 `web/manager.py`——测试要能离线跑。

**验证：** 见 T16。

### T16: 动作层测试

**文件：** `tests/test_hook_actions.py`
**依赖：** T15
**步骤：**
1. 写测试：`command` 动作能从 stdin 收到完整负载 JSON（用一个把 stdin 回显到 stdout 的命令）。
2. 写测试：危险命令被①黑名单拦下、`ok=False`、**子进程未启动**。
3. 写测试：退出码 2 → `DENY`；退出码 1 → `ok=False`；超时 → `ok=False`。
4. 写测试：stdout 输出 `{"decision":"allow"}` → 结论为 `NONE`（AC15 的动作层反证）。
5. 写测试：`http` 动作指向 `127.0.0.1`、`localhost`、`file://`、内嵌凭据四种地址各被拒且未发请求。
6. 写测试：`http` 动作返回 500 时 `ok=False` 但 `verdict` 仍为 `NONE`。
7. 写测试：`agent` 动作 `ok=True`。

**验证：** `python -m unittest tests.test_hook_actions` 全绿。

---

## 阶段五：编排

### T17: 报告渲染

**文件：** `rhinecode/hooks/report.py`
**依赖：** T3
**步骤：**
1. 实现 `render_report(rules, warnings, stats) -> str`：三段——已加载规则、
   加载警告、本次运行执行统计。无警告时该段整体不出现。
2. 实现 `render_project_notice(project_rules) -> str | None`：无项目级规则返回 None；
   否则**逐条**列出 `事件 → 动作`，`command` 展示完整命令串、`http` 展示完整 URL。
3. 在 `render_project_notice` 上方写注释：**不折叠、不截断、不只给计数**，
   它是 F9.1 的全部安全价值。
4. 动作摘要用一个 `_describe_action` 分支函数，四种类型各一支；
   写明「新增动作类型须同步本函数，漏改不报错只是显示空白」。

**验证：** 交互式渲染一份含两层规则 + 一条警告的报告，肉眼确认三段齐全。

### T18: HookManager 骨架

**文件：** `rhinecode/hooks/manager.py`
**依赖：** T17
**步骤：**
1. 定义 `HookManager.__init__(rules, warnings, recorder=None, client_factory=None)`。
2. 按 `event` 建索引 `dict[HookEventType, list[HookRule]]`，供 `has_listeners` O(1) 查询。
3. 实现 `enabled` 属性、`has_listeners(event)`、`warnings` 属性。
4. 初始化 `threading.Lock`、`_once_consumed: set[str]`、
   `_stats: dict[str, dict]`、`_injections: list[str]`。
5. 实现四个受保护埋点漏斗（`_safe_emit` / `_safe_emit_lazy`），照抄 `agent/loop.py` 的形态。

**验证：** 构造一个含三条不同事件规则的 manager，确认 `has_listeners` 对有规则的事件为 True、
对其余九个事件为 False。

### T19: dispatch 四段式

**文件：** `rhinecode/hooks/manager.py`
**依赖：** T18
**步骤：**
1. 实现 `dispatch(event, payload_factory) -> DispatchResult`。
2. **第一步就查 `has_listeners`**，为假直接返回空结果，**不调用 `payload_factory`**。
3. **持锁**：筛出条件求值前的候选规则，跳过 `once` 已消耗的，把本次要跑的
   `once` 规则标记为已消耗。
4. **出锁**：构造负载（调 `payload_factory` 一次）、逐条求值条件、执行动作。
5. **持锁**：写统计。
6. **出锁**：埋点。
7. 在方法 docstring 里写明加锁不变量与违反后果（超时动作锁死 manager → 界面假死）。

**验证：** 见 T22。

### T20: 结论合并与失败处理

**文件：** `rhinecode/hooks/manager.py`
**依赖：** T19
**步骤：**
1. 实现 `_merge(outcomes, event) -> HookVerdict`：按 `SEVERITY` 取最严，
   记住做出该结论的第一条规则名。
2. `event == PRE_TOOL_USE` 且某条 `outcome.ok == False` → 该条计为 `DENY`，
   原因加前缀「Hook 自身执行失败」。
3. 其余事件的失败不参与合并，只进统计与埋点。
4. 拦截原因文案按 spec F6.3 写全：哪条规则、什么原因、以及「不要改参数或换工具绕过」。

**验证：** 见 T22。

### T21: async 执行与注入队列

**文件：** `rhinecode/hooks/manager.py`
**依赖：** T20
**步骤：**
1. `rule.run_async` 为真时用 `threading.Thread(daemon=True)` 执行动作，不 join，
   其 `ActionOutcome` 不参与合并。
2. 异步线程内的动作失败照常记 trace 与统计（持锁写统计）。
3. 实现 `consume_injections() -> str`：持锁取出并清空 `_injections`，
   多条按触发顺序用 `\n\n` 连接。
4. 同步动作产出的 `injected_text` 在出锁后经一次短临界区追加进队列。

**验证：** 见 T22。

### T22: manager 层测试

**文件：** `tests/test_hook_manager.py`
**依赖：** T21
**步骤：**
1. 写测试：零命中时 `payload_factory` **一次都没被调用**（用计数闭包断言）。
2. 写测试：`once: true` 的规则触发两次事件只执行一次。
3. 写测试：结论合并——`DENY + ASK + NONE` → `DENY`；`ASK + NONE` → `ASK`；
   全 `NONE` → `NONE`；结论里含正确的规则名。
4. 写测试：`pre_tool_use` 上动作失败 → 结论为 `DENY`；同一失败挂在 `post_tool_use` 上
   → 结论为 `NONE` 且不影响任何返回值。
5. 写测试：`consume_injections` 取走即清（连调两次，第二次为空串）。
6. **加锁不变量护栏**：用一个在执行期回调 manager 另一个方法的假动作，
   确认不死锁。⚠ 必须用**两个线程 + 完成计数**构造，不要用同线程调用或布尔标志——
   `RLock` 与同线程重入会让护栏静默通过（`docs/internals/testing.md` 有这条记载）。

**验证：** `python -m unittest tests.test_hook_manager` 全绿。

### T23: 包导出与空实现

**文件：** `rhinecode/hooks/__init__.py`
**依赖：** T22
**步骤：**
1. 实现 `NullHookManager`：`enabled=False`、`has_listeners` 恒 False、
   `dispatch` 恒返回空 `DispatchResult`、`consume_injections` 恒返回空串、
   `report()` 返回「未启用」、`project_notice()` 返回 None、`warnings` 为空列表。
2. 导出 `HookManager`、`NullHookManager`、`HookEventType`、`HookDecision`、
   `HookVerdict`、`DispatchResult`、`load_all`。
3. 写模块 docstring 说明本包的依赖方向与「不是叶子包」。

**验证：** `python -c "from rhinecode.hooks import NullHookManager, HookEventType as E; m=NullHookManager(); assert not m.enabled and not m.has_listeners(E.PRE_TOOL_USE)"` 通过。

---

## 阶段六：既有系统接线

### T24: Layer.HOOK 三处同步

**文件：** `rhinecode/permission/models.py`、`rhinecode/trace/reader.py`、`rhinecode/tui/widgets.py`
**依赖：** 无（可与阶段一并行）
**步骤：**
1. `Layer` 枚举加 `HOOK = "hook"`，docstring 里补一行说明。
2. `trace/reader.py` 的 `_LAYER_NAMES` 加一行中文名。
3. `tui/widgets.py` 的 `ConfirmPanel._LAYER_LABELS` 加一行。

**验证：** `python -m unittest tests.test_trace_reader tests.test_web_bootstrap` 全绿
（两条遍历 `Layer` 的断言会在漏改时当场红）。

### T25: trace 两类事件

**文件：** `rhinecode/trace/models.py`、`rhinecode/trace/reader.py`
**依赖：** T24
**步骤：**
1. `TraceEventType` 加 `HOOK_DISPATCH = "hook_dispatch"` 与 `HOOK_EXECUTE = "hook_execute"`。
2. `reader.py` 的 `SUMMARIZERS` 表加对应两个摘要函数：
   dispatch 摘要含事件名与命中数；execute 摘要含规则名、动作类型、结论、耗时。

**验证：** `python -m unittest tests.test_trace_reader` 全绿（它有一条「每个事件类型都要有摘要函数」的断言）。

### T26: Agent Loop 的 pre_tool_use 拦截

**文件：** `rhinecode/agent/loop.py`
**依赖：** T23、T25
**步骤：**
1. `Agent.__init__` 接收 `hooks` 参数，缺省 `NullHookManager()`。
2. 在 `_execute` 的决策预扫里、`to_request` 之前插入分发：
   构造惰性 `payload_factory`（展开 `tool_input`，公共字段优先）。
3. `verdict.decision == DENY` → 进 `serial` 桶的一条新分支，产结构化拒绝结果，
   `outcome` 记为新增的 `OUTCOME_BLOCKED_BY_HOOK`，**不调 `engine.decide`**。
4. 否则照常 `engine.decide`；`verdict.decision == ASK` **且** `decision.decision == ALLOW`
   → 改判为 `DecisionResult(ASK, Layer.HOOK, ...)`。
5. 在第 4 步上方写注释：**只升级 ALLOW，DENY 原样保留**，附 AC15 的理由。

**验证：** `python -m compileall rhinecode/agent/loop.py`；见 T29 的测试。

### T27: Agent Loop 的 post_tool_use 两事件

**文件：** `rhinecode/agent/loop.py`
**依赖：** T26
**步骤：**
1. 在 `_run_one_serial` 与 `_run_readonly_concurrent` 里 `outcome == OUTCOME_EXECUTED`
   的两处 `_trace_tool` 调用旁，按 `res.ok` 分发 `POST_TOOL_USE` 或 `POST_TOOL_USE_FAILURE`。
2. **只挂在 `OUTCOME_EXECUTED` 上**，其余六个 outcome 一个都不挂；在此处写注释说明
   （spec AC3 的实现依据）。
3. 并发桶里的分发发生在池线程——确认 manager 线程安全（T18 已加锁）。

**验证：** 见 T29。

### T28: 拦截结果的回灌与事件

**文件：** `rhinecode/agent/loop.py`
**依赖：** T27
**步骤：**
1. 为 `OUTCOME_BLOCKED_BY_HOOK` 分支产出 `TOOL_START` + `TOOL_RESULT` 事件对
   （与 `plan_blocked` 分支同形态）。
2. 拒绝文案用 manager 给出的 `verdict.reason`，前缀 `[Hook 拦截·<规则名>]`。
3. 确认该分支**不递增** `ctx.user_denied`（那是人的决定，Hook 拦截不是）。

**验证：** `python -m compileall`；见 T29。

### T29: Agent Loop 集成测试

**文件：** `tests/test_hook_intercept.py`
**依赖：** T28
**步骤：**
1. 写测试：Hook 判 DENY → 工具未执行、结果回灌、**循环不终止**（后续轮次照常）。
2. 写测试：Hook 判 ASK 且权限判 ALLOW → 弹确认面板（`ask` 回调被调用）。
3. 写测试：Hook 判 ASK 且权限判 **DENY** → 仍为 DENY，**面板不弹**（AC15 反证）。
4. 写测试：Hook 判 DENY 时 `engine.decide` **一次都没被调用**（用计数假引擎）。
5. 写测试：只读并发工具与系统级串行工具**都触发** `pre_tool_use`。
6. 写测试：`ask_user` / `present_plan` **不触发**任何工具级事件。
7. 写测试：六种「没执行」的分支各不产生 `post_tool_use*`（AC3）。

**验证：** `python -m unittest tests.test_hook_intercept` 全绿。

### T30: 协调层回合级两事件

**文件：** `rhinecode/conversation.py`
**依赖：** T23
**步骤：**
1. `ConversationManager.__init__` 接收 `hook_manager` 参数，缺省 `NullHookManager()`。
2. `_wrap_events` 起点（取令牌之后、首次 `yield` 之前）分发 `TURN_START`，
   `scope` 取自 `recorder.current_scope()`，`trigger` 由新增参数传入。
3. 循环中捕获 `FINISHED` 事件的 `stop_reason` 与最后一段正文。
4. `finally` 里分发 `TURN_END`，带上捕获到的停止原因。
5. 把 `hook_manager` 透传给 `Agent` 构造与 `_run_forked_skill` 的子 Agent。

**验证：** 见 T35。

### T31: 注入通道

**文件：** `rhinecode/conversation.py`
**依赖：** T30
**步骤：**
1. 在 `_run` 的 `dynamic_provider()` 内，于「环境信息 → Skill 正文 → 一次性提醒」
   之后追加 `self._hook_manager.consume_injections()` 的结果（非空才追加）。
2. `_run_forked_skill` 的 `sub_dynamic()` 同样处理（两处都要）。
3. 写注释说明「一次性由 consume 语义保证」，以及为什么它必须在**每轮求值**的
   callable 里而不是外层取一次。

**验证：** 见 T35。

### T32: 会话级两事件与报告方法

**文件：** `rhinecode/conversation.py`
**依赖：** T31
**步骤：**
1. `clear()` 里先分发 `SESSION_END(reason="clear")`，清空后分发 `SESSION_START(source="clear")`。
2. `_resume_stream` 里同理，`reason`/`source` 取 `"resume"`。
3. 新增 `hooks_report() -> str`，转调 `self._hook_manager.report()`。

**验证：** 见 T35。

### T33: 上下文压缩两事件

**文件：** `rhinecode/context/manager.py`
**依赖：** T23
**步骤：**
1. `ContextManager.__init__` 接收 `hook_manager` 参数，缺省 `NullHookManager()`。
2. `_do_summary` 入口分发 `PRE_COMPACT`（`trigger` 由调用方区分 auto/manual）。
3. `_do_summary` 每条 return 路径之前分发 `POST_COMPACT`，带 `ok`。
   用一个内部包装函数保证「每条 return 都过它」，形态照抄 `engine.decide` 的 `_verdict` 闭包。
4. **只挂第二层**，第一层存盘不挂（写注释说明理由）。

**验证：** 见 T35。

### T34: TUI 消息级与通知事件

**文件：** `rhinecode/tui/app.py`
**依赖：** T23
**步骤：**
1. 真人提交入口在命令分发**之前**分发 `USER_MESSAGE`（`is_command` 由解析结果填）。
2. `_do_stream` 的 `reset_text_widgets()` 全部调用点分发 `ASSISTANT_MESSAGE`
   （与 `ui_message` 埋点同位置、同产出条件）。
3. `_interact` 的三处交互入口分发 `NOTIFICATION`（`awaiting_confirm` /
   `awaiting_clarify` / `awaiting_plan`）。
4. `_do_stream` 的 `FINISHED` 分支分发 `NOTIFICATION(kind="agent_finished")`。

**验证：** 见 T35。

### T35: 分发点结构护栏

**文件：** `tests/test_hook_dispatch_points.py`
**依赖：** T34
**步骤：**
1. 用一个记录全部 `dispatch` 调用的假 manager，跑一次完整的假 Agent 流程。
2. 断言十二个事件各至少出现一次，且顺序符合预期
   （`session_start` → `user_message` → `turn_start` → … → `turn_end`）。
3. 断言 `turn_start`/`turn_end` 在**取消**与**出错**两种终止下都产出。
4. 断言子对话产出的 `turn_start` 带 `scope` 前缀 `isolated:`。
5. 断言注入文本出现在**下一次**请求的 reminder 里且**只出现一次**（AC9）。

**验证：** `python -m unittest tests.test_hook_dispatch_points` 全绿。

---

## 阶段七：命令与装配

### T36: /hooks 命令

**文件：** `rhinecode/commands/models.py`、`rhinecode/commands/builtins.py`、`rhinecode/tui/app.py`
**依赖：** T32
**步骤：**
1. `ReportTarget` 加 `HOOKS = "hooks"`。
2. `builtins.py` 登记 `CommandSpec`：名 `/hooks`、无别名、类型 UI、
   handler 调 `controller.query_report(ReportTarget.HOOKS)`。
3. `tui/app.py` 的 `query_report` 加分支，转调 `manager.hooks_report()`。
4. 展示时用 `tui/widgets.py` 的 `escape` 转义——命令串里出现 `[` 是常事。

**验证：** `python -m unittest tests.test_commands_registry` 全绿；`/help` 里能看到 `/hooks`。

### T37: 装配接线

**文件：** `rhinecode/bootstrap.py`
**依赖：** T36
**步骤：**
1. 在 Skill 第一阶段之后、`connect_all` 之前加载 hooks 配置并构造 `HookManager`。
2. 透传给 `ConversationManager`（它再透传给 `Agent` 与 `ContextManager`）与 `RhineApp`。
3. `session_start` trace 事件之后分发 `HookEventType.SESSION_START(source="startup")`。
4. `cleanup` 里在 `recorder.emit(SESSION_END)` 之后、`recorder.close()` 之前分发
   `SESSION_END`，`reason` 取 cleanup 的入参。
5. 装配位置理由写成注释随代码走。

**验证：** `python -c "from rhinecode.bootstrap import build_app"` 导入成功；见 T39。

### T38: 项目级启动提示

**文件：** `rhinecode/bootstrap.py`、`rhinecode/tui/app.py`
**依赖：** T37
**步骤：**
1. `build_app` 把 `hook_manager.project_notice()` 与 `warnings` 收进 `BuildResult`
   （或经协调层的启动提示通道）。
2. 在 TUI 首屏展示——**不能用 `print`**：启动期 print 会被 Textual 的 alternate screen
   整个盖住，用户要到退出程序才看得见（C11 踩过，理由在 `bootstrap.py` 里有记载）。
   走既有的 `_compose_startup_notice` 通道。
3. **每次启动都提示**，不做「只提示一次」的持久化。

**验证：** 见 T39。

### T39: 命令与装配测试

**文件：** `tests/test_hook_command.py`
**依赖：** T38
**步骤：**
1. 写测试：`/hooks` 输出三段（规则 / 警告 / 统计）。
2. 写测试：存在项目级 `hooks.yaml` 时启动提示**逐条**含事件与完整命令串、完整 URL。
3. 写测试：无项目级规则时不产生该提示。
4. 写测试：连续两次装配都产出提示（无持久化）。
5. 写测试：报告文本里的 `[` 已被转义（防 `MarkupError`）。

**验证：** `python -m unittest tests.test_hook_command` 全绿。

### T40: 模板生成

**文件：** `rhinecode/__main__.py`
**依赖：** T37
**步骤：**
1. 首次运行的模板生成流程里加一步 `hooks.scaffold_user_config`。
2. 与既有三份模板同形态：已存在不覆盖、生成失败不阻断启动。

**验证：** 在临时 HOME 下跑一次首次运行流程，确认生成了 `hooks.yaml` 且内容全注释。

---

## 阶段八：零回归与文档

### T41: 零回归测试

**文件：** `tests/test_hook_zero_regression.py`
**依赖：** T40
**步骤：**
1. 写测试：两层 `hooks.yaml` 都不存在时，装配出的 manager `enabled` 为 False。
2. 写测试：跑一次完整假 Agent 流程，trace 里**零条** `hook_dispatch` 事件。
3. 写测试：`payload_factory` 一次都没被构造（计数闭包）。

**验证：** `python -m unittest tests.test_hook_zero_regression` 全绿。

### T42: 全量测试

**文件：** —
**依赖：** T41
**步骤：**
1. `python -m compileall rhinecode tests`
2. `python -m unittest discover -s tests`
3. 修掉一切因新增枚举/字段而变红的既有用例。

**验证：** 全量绿，skipped 仍为 4。

### T43: 文档回填

**文件：** `CLAUDE.md`、`docs/internals/{architecture,capabilities,testing,config}.md`
**依赖：** T42
**步骤：**
1. `CLAUDE.md` 能力表加 C12 一行；架构分层表加 `hooks/` 一层（含 ⚠ 不变量：
   加锁临界区只做内存读写 / Hook 只能收紧不能放宽）。
2. 成对维护点加 plan 末节的五条。
3. 安全边界加一节：项目级 hooks 的风险、Hook 命令只过①黑名单、
   trace 产物含 hook 负载、http 动作是第二条外发链路。
4. 常用命令加 `/hooks`；测试条数更新为实跑 `discover` 的口径。
5. 四份分册各补 Hook 章节。

**验证：** 通读一遍，确认没有「待补」占位。

---

## 执行顺序

```
T1 → T2 → T3 ─┬─→ T4 → T5 → T6                    （条件求值）
              │                    ↘
              ├─→ T12 → T13 → T14 → T15 → T16     （动作执行器）
              │                    ↗
              ├─→ T7 → T8 → T9 → T10 → T11        （解析与加载）
              │
              └─→ T17 ──────────────────────────↘
                                                 T18 → T19 → T20 → T21 → T22 → T23
T24 → T25 ───────────────────────────────────────────────────────────────────↘
                                                                     T26 → T27 → T28 → T29
                                                                     T30 → T31 → T32
                                                                     T33
                                                                     T34 → T35
                                                                            ↓
                                                              T36 → T37 → T38 → T39
                                                                            T40
                                                                            ↓
                                                                   T41 → T42 → T43
```

**可并行**：T24/T25（既有系统的枚举同步）与阶段一–五完全无关，随时可做。
T26–T29（Agent Loop）、T30–T32（协调层）、T33（上下文）、T34–T35（TUI）
四组彼此独立，都只依赖 T23。
