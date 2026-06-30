# C6 五层防御权限系统 Tasks

## 文件清单

| 操作 | 文件 | 职责 |
|------|------|------|
| 新建 | `rhinecode/permission/__init__.py` | 导出公共符号 |
| 新建 | `rhinecode/permission/models.py` | 数据结构与枚举 |
| 新建 | `rhinecode/permission/matching.py` | 命令/路径匹配 + 命令拆分 |
| 新建 | `rhinecode/permission/blacklist.py` | ①危险命令正则集 |
| 新建 | `rhinecode/permission/rules.py` | ③RuleSet + deny 优先求值 |
| 新建 | `rhinecode/permission/config.py` | 三层 YAML 定位/加载/容错/回写 |
| 新建 | `rhinecode/permission/adapter.py` | ToolCall→PermissionRequest 规范化 |
| 新建 | `rhinecode/permission/engine.py` | PermissionEngine 四层管线 |
| 修改 | `rhinecode/tools/path_guard.py` | 暴露 `is_within_workspace` |
| 修改 | `rhinecode/agent/loop.py` | `_execute` 插决策；`confirm`→`ask` |
| 修改 | `rhinecode/conversation.py` | 构建 engine / ask 闭包 / 解析 `/perm` |
| 修改 | `rhinecode/tui/widgets.py` | 确认面板 2 按钮→4 选项 |
| 修改 | `.gitignore` | 忽略 `*.local.yaml` |
| 新建 | `tests/test_perm_*.py` | 各层单测 |
| 新建 | 示例 `permissions.yaml` | 示例配置（注释说明语法） |

## T1: 数据结构与枚举
**文件：** `permission/models.py`、`permission/__init__.py`
**依赖：** 无
**步骤：**
1. 定义枚举 `Decision`(ALLOW/DENY/ASK)、`PermissionMode`(STRICT/DEFAULT/PERMISSIVE)、`Layer`(BLACKLIST/SANDBOX/RULE/MODE)。
2. 定义 dataclass `DecisionResult`、`Rule`(frozen)、`PermissionRequest`，字段按 plan。
3. `__init__.py` 导出上述符号。
**验证：** `python -c "import rhinecode.permission.models"` 无错；`python -m compileall rhinecode/permission`。

## T2: 匹配算法
**文件：** `permission/matching.py`、`tests/test_perm_matching.py`
**依赖：** 无
**步骤：**
1. `split_commands(command)`：按 `&&`/`||`/`;`/`|`/`|&`/`&`/换行拆分并 strip。
2. `match_command(pattern, command)`：实现前缀+glob，末尾 ` *`/`:*` 词边界，中间 `*` 通配。
3. `match_path(pattern, path)`：gitignore 风格（精确、`*` 段内、`**` 跨目录、裸名任意深度），Windows 路径归一化为 POSIX。
4. 写单测：`npm:*` 不匹配 `npmx`、`git *` 匹配 `git push origin main`、`a && b` 拆成两段、`.env` 匹配 `src/.env`。
**验证：** `python -m unittest tests.test_perm_matching`。

## T3: 危险命令黑名单
**文件：** `permission/blacklist.py`、`tests/test_perm_blacklist.py`
**依赖：** T2
**步骤：**
1. 定义 `DANGEROUS_PATTERNS`（编译正则）覆盖：`rm -rf` 递归强删、磁盘破坏（`mkfs`/`dd of=`）、git 破坏性（`reset --hard`/`push --force`/`clean -fd`）、fork 炸弹、写关键系统路径。
2. `check_command(command)`：`split_commands` 后逐段匹配，命中返回中文原因，否则 None。
3. 单测：`rm -rf /` 命中；`echo ok && rm -rf .` 因第二段命中；普通 `ls` 不命中。
**验证：** `python -m unittest tests.test_perm_blacklist`。

## T4: 规则求值（deny 优先）
**文件：** `permission/rules.py`、`tests/test_perm_rules.py`
**依赖：** T2, T1
**步骤：**
1. `RuleSet` 持有规则列表；`evaluate(request)`：先扫 deny 任一命中→DENY，再扫 allow 命中→ALLOW，否则 None；按 `kind` 选 `match_command`/`match_path`。
2. 命中时 `DecisionResult.reason` 标明命中规则与 source。
3. 单测：deny `Bash(git push *)` + allow `Bash(git *)` → `git push` 得 DENY（AC5）；只 allow 命中→ALLOW；无命中→None。
**验证：** `python -m unittest tests.test_perm_rules`。

## T5: 配置加载与容错
**文件：** `permission/config.py`、`tests/test_perm_config.py`
**依赖：** T1
**步骤：**
1. `parse_rule_string("Bash(git *)")`→`Rule`（含无括号 `Bash` = 全匹配）。
2. 三层文件定位函数（`Path.home()`/`workspace_root()`）。
3. `load_all()`：逐层读 YAML→Rule→合并；缺失=空；YAML 解析异常→该层空 + 收集可读错误（fail-safe）。
4. `append_local_allow(rule_string)`：追加写本地级 YAML（目录不存在则创建）。
5. 单测：解析规则字符串；坏 YAML 不抛异常且降级为空（AC10）；缺失文件=空集。
**验证：** `python -m unittest tests.test_perm_config`。

## T6: 强化 path_guard
**文件：** `rhinecode/tools/path_guard.py`
**依赖：** 无
**步骤：**
1. 新增 `is_within_workspace(path) -> bool`：内部复用 `resolve_in_workspace` 逻辑，越界/含 `..`/越界符号链接返回 False，否则 True（不抛异常，供引擎布尔判定）。
2. 保留现有 `resolve_in_workspace`/`validate_glob_pattern` 不变。
**验证：** `python -m unittest`（现有 path_guard 测试仍通过）+ 新增小测试断言越界路径返回 False。

## T7: 工具规范化 adapter
**文件：** `permission/adapter.py`、`tests/test_perm_adapter.py`
**依赖：** T1
**步骤：**
1. `to_request(tool, args, mode)`：按 plan 映射 6 个工具→`(rule_name, specifier, kind)`，读 `tool.read_only` 填 `is_read_only`，未映射→`kind="other"`。
2. 单测：`run_command`→Bash/command；`read_file`→Read/read_path；`edit_file`→Edit/write_path；`glob_files`→Read/glob。
**验证：** `python -m unittest tests.test_perm_adapter`。

## T8: PermissionEngine 四层管线
**文件：** `permission/engine.py`、`tests/test_perm_engine.py`
**依赖：** T3, T4, T5, T6, T7, T1
**步骤：**
1. `PermissionEngine` 构造：从 `config.load_all()` 取 `file_ruleset`，`session_rules=[]`，`mode=DEFAULT`。
2. `decide(request)`：按 plan 四层短路（①黑名单→②沙箱→③规则→只读分支→④模式）。
3. `set_mode` / `add_session_rule` / `persist_local_rule`。
4. 单测覆盖 AC1（短路顺序）、AC2（放行档下黑名单仍拒）、AC5（deny 优先）、AC6（三档兜底）、AC8（只读不进模式、deny 可拦读）、AC12（不读模型输出）。
**验证：** `python -m unittest tests.test_perm_engine`。

## T9: loop 接入决策
**文件：** `rhinecode/agent/loop.py`、`tests/test_perm_loop.py`
**依赖：** T8, T7
**步骤：**
1. `Agent.run`/`_execute` 新增 `engine` 参数；`ConfirmFn` 升级为 `AskFn = (ToolCall, Tool, DecisionResult) -> bool`。
2. `_execute` 决策预扫：DENY→结构化错误结果不执行；ALLOW 只读→并发；ALLOW 副作用→直接执行（去掉无条件 confirm）；ASK→串行调 `ask`。
3. DENY 的 `ToolResult.output` 形如 `[权限拒绝·{layer}] {reason}`。
4. 单测：DENY 工具回灌 `ok=False` 且 loop 继续（AC9）；ALLOW 命中 allow 规则的副作用工具不触发 ask（AC4）。
**验证：** `python -m unittest tests.test_perm_loop` + 现有 loop/确认相关测试仍通过。

## T10: conversation 接线
**文件：** `rhinecode/conversation.py`
**依赖：** T8, T9
**步骤：**
1. 启动构建 `PermissionEngine`（加载配置；把加载错误以提示展示）。
2. 构建 `ask` 闭包：弹 4 选 1 面板→本次=True / 本会话=`add_session_rule`+True / 永久=`persist_local_rule`+True / 拒绝=False；本会话/永久生成的 allow 规则用「该工具+本次 specifier 前缀」。
3. 解析 `/perm`：循环 严格→默认→放行，调 `set_mode`，反馈当前档。
4. 把 engine 透传进 `Agent.run`。
**验证：** `python -m unittest`（新增 conversation 相关断言：`/perm` 切档、ask 闭包对四选项的映射）。

## T11: 确认面板 4 选项
**文件：** `rhinecode/tui/widgets.py`
**依赖：** T10（约定回调契约）
**步骤：**
1. 确认面板由「执行/拒绝」改为四按钮：本次放行 / 本会话放行 / 永久放行 / 拒绝，并展示 `DecisionResult.reason`。
2. 把所选映射为 `ask` 闭包约定的返回。
**验证：** `python -m compileall rhinecode/tui`；端到端留待阶段六 tmux 验收。

## T12: 收尾（示例配置 / gitignore / 全量测试）
**文件：** `.gitignore`、示例 `permissions.yaml`、`tests`
**依赖：** T1–T11
**步骤：**
1. `.gitignore` 追加 `**/.rhinecode/*.local.yaml`。
2. 提供示例 `permissions.yaml`（注释说明 allow/deny 语法）。
3. 补全跨平台单测（AC11：Windows 盘符越界、命令拆分）。
**验证：** `python -m compileall rhinecode tests` + `python -m unittest discover -s tests` 全绿。

## 执行顺序

```
T1 ─┬─ T2 ─┬─ T3 ─┐
    │      └─ T4 ─┤
    ├─ T5 ───────┤
    ├─ T7 ───────┼─ T8 ─ T9 ─ T10 ─ T11 ─ T12
T6 ─────────────┘
```
