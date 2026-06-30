# C6 五层防御权限系统 Plan

## 架构概览

把五层防御实现为一个**纯逻辑的 `PermissionEngine`**：输入是「规范化后的一次权限请求」，输出是「决定（放行/拒绝/问）+ 命中层 + 原因」。所有与具体工具相关的知识（工具→规则名映射、哪个参数是路径/命令）集中在一个 **adapter**，引擎本身对工具一无所知——这样引擎可脱离 TUI、脱离真实文件/命令被纯单测覆盖（N5 可测试、N4 解耦）。

```
                  loop._execute（唯一工具执行入口）
                          │  对每个工具调用，规范化
                          ▼
            adapter.to_request(tool, args, mode)
                          │  → PermissionRequest(rule_name, specifier, kind, ...)
                          ▼
                  engine.decide(request)
            ①黑名单 → ②沙箱 → ③规则(deny优先) → ④模式
                          │
        ┌─────────────────┼──────────────────┐
        ▼                 ▼                  ▼
      ALLOW             DENY                ASK
   直接执行       结构化错误结果        调 ask 回调（HITL 面板）
 (只读并发/        回灌模型、不停loop    4选1→ 本次/本会话/永久/拒绝
  副作用串行)                          （会话/永久 由回调登记进引擎）
```

## 核心数据结构

`models.py`，纯数据 + 枚举，无逻辑。

```python
class Decision(Enum):          # 单次判断的三种结局
    ALLOW = "allow"            # 放行
    DENY  = "deny"             # 拒绝
    ASK   = "ask"              # 交人工确认

class PermissionMode(Enum):    # 第④层三档权限模式
    STRICT     = "strict"      # 严格：灰色地带→拒
    DEFAULT    = "default"     # 默认：灰色地带→问
    PERMISSIVE = "permissive"  # 放行：灰色地带→允

class Layer(Enum):             # 决定由哪一层做出（用于结构化原因/调试）
    BLACKLIST = "blacklist"    # ①
    SANDBOX   = "sandbox"      # ②
    RULE      = "rule"         # ③
    MODE      = "mode"         # ④

@dataclass
class DecisionResult:          # 决策的完整结果
    decision: Decision
    layer: Layer               # 在第几层定的论
    reason: str                # 面向模型/用户的中文可读原因

@dataclass(frozen=True)
class Rule:                    # 一条解析后的规则
    effect: str                # "allow" | "deny"
    tool: str                  # 规则工具名：Bash / Read / Edit / Write
    pattern: str               # 括号内模式，如 "git *"；"" 表示匹配该工具所有调用
    source: str                # 来源：user/project/local/session（用于原因展示与调试）

@dataclass
class PermissionRequest:       # 规范化后的一次权限请求（引擎输入，已与具体工具脱钩）
    tool_name: str             # 真实工具名，如 "run_command"
    rule_name: str             # 规则体系工具名，如 "Bash"
    specifier: str             # 待匹配串：命令字符串 或 路径/glob 模式
    kind: str                  # "command" | "read_path" | "write_path" | "glob" | "other"
    is_read_only: bool         # True 走只读简化分支（F7：不进④模式层）
    mode: PermissionMode       # 当前权限模式
```

## 模块设计

### blacklist.py（①层）
**职责：** 危险命令黑名单。
**对外接口：** `check_command(command: str) -> Optional[str]`——先用 `matching.split_commands` 拆段，逐段匹配固定编译正则集 `DANGEROUS_PATTERNS`，任一段命中返回中文原因，否则 `None`。
**依赖：** matching。
**覆盖类别（F2）：** 递归强删（`rm -rf` 等）、磁盘/分区破坏、git 破坏性（`reset --hard` / `push --force` / `clean -fd`）、fork 炸弹、写关键系统路径。

### matching.py（①③共用匹配算法）
**职责：** 命令/路径模式匹配与命令拆分。
**对外接口：**
- `split_commands(command) -> list[str]`：按 `&&`、`||`、`;`、`|`、`|&`、`&`、换行拆分。
- `match_command(pattern, command) -> bool`：前缀 + glob；末尾 ` *` / `:*` 带词边界（`npm:*` 不误伤 `npmx`），中间 `*` 通配。
- `match_path(pattern, path) -> bool`：gitignore 风格——精确、`*` 段内、`**` 跨目录、裸文件名任意深度；Windows 路径先归一化为 POSIX 再比（N8）。
**依赖：** 无（纯字符串/正则）。

### rules.py（③层）
**职责：** 三层合并规则的 deny 优先求值。
**对外接口：** `RuleSet`（持有 allow/deny 规则，每条带 source）；`evaluate(request) -> Optional[DecisionResult]`——先扫 deny（任一命中→DENY），再扫 allow（命中→ALLOW），都无→None；按 `request.kind` 选 `match_command` / `match_path`。
**依赖：** matching、models。

### config.py（③层支撑）
**职责：** 三层 YAML 定位、加载、容错、回写。
**对外接口：**
- `parse_rule_string("Bash(git *)") -> Rule`。
- `load_all() -> (RuleSet, list[str])`：逐层读 YAML→Rule→合并；缺失视为空；解析失败该层降级为空并收集可读错误（F9 / N1）。
- `append_local_allow(rule_string)`：永久放行追加写入本地级 YAML（F6）。
**依赖：** pyyaml、models、path_guard（定位 workspace_root）。
**文件定位（Windows-aware）：** 用户级 `Path.home()/".rhinecode"/"permissions.yaml"`；项目级 `workspace_root()/".rhinecode"/"permissions.yaml"`；本地级 `workspace_root()/".rhinecode"/"permissions.local.yaml"`。

### adapter.py（②③用，收口全部工具知识）
**职责：** 把真实工具调用规范化为 `PermissionRequest`。
**对外接口：** `to_request(tool, args, mode) -> PermissionRequest`。映射：
`run_command`→`("Bash", command, "command")`；`read_file`/`grep_content`→`("Read", path, "read_path")`；`glob_files`→`("Read", pattern, "glob")`；`write_file`→`("Write", path, "write_path")`；`edit_file`→`("Edit", path, "write_path")`；未映射工具→`kind="other"`（引擎只按工具名匹配规则 + 走模式兜底）。
**依赖：** models、tools.base（读 read_only）。

### engine.py（组装四层 + 持有可变状态）
**职责：** 决策管线主体。
**对外接口：**
- `PermissionEngine`：持有 `file_ruleset`（config 启动加载一次）、`session_rules`（可变）、`mode`（可变，默认 DEFAULT）。
- `decide(request) -> DecisionResult`：依次跑 ①→②→③→④，第一个定论即返回（见模块交互）。
- `set_mode(mode)` / `add_session_rule(rule)` / `persist_local_rule(rule_string)`（委托 config 落盘）。
**依赖：** blacklist、rules、config、path_guard、models。

### path_guard.py（②层，强化复用）
**职责：** 沙箱边界判定。
**改动：** 抽出可被引擎调用的纯判定（如 `is_within_workspace(path) -> bool`，复用 `resolve_in_workspace` 逻辑）；文件工具内现有 path_guard 调用保留不动（防御纵深、N6）。

## 模块交互

### engine.decide() 完整调用链（四层短路）

```python
def decide(self, req: PermissionRequest) -> DecisionResult:
    # ① 黑名单（仅命令类）——不可被任何配置/模式放开
    if req.kind == "command":
        reason = blacklist.check_command(req.specifier)
        if reason:
            return DecisionResult(DENY, Layer.BLACKLIST, reason)

    # ② 沙箱（仅路径/glob 类）——越界即拒
    if req.kind in ("read_path", "write_path", "glob"):
        if not path_guard.is_within_workspace(req.specifier):
            return DecisionResult(DENY, Layer.SANDBOX, f"路径越界：{req.specifier}")

    # ③ 规则（deny 优先）——会话级规则并入合并集统一求值
    merged = RuleSet(self.session_rules + self.file_ruleset.rules)
    hit = merged.evaluate(req)
    if hit is not None:
        return hit

    # 只读简化分支（F7）：规则没拦 → 直接放行，不进④
    if req.is_read_only:
        return DecisionResult(ALLOW, Layer.RULE, "只读工具默认放行")

    # ④ 模式兜底（仅副作用工具、③未命中时）
    if self.mode is STRICT:     return DecisionResult(DENY,  Layer.MODE, "严格模式：默认拒绝")
    if self.mode is PERMISSIVE: return DecisionResult(ALLOW, Layer.MODE, "放行模式：默认允许")
    return DecisionResult(ASK, Layer.MODE, "默认模式：交用户确认")   # DEFAULT
```

①②在③④之前 → 放行档/任何 allow 翻不了它们（AC2/AC6）；deny 在③内最先扫 → allow 翻不了 deny（AC5）；只读永不进④（AC8）。

### loop 接入数据流（改造 `_execute`）

在现有「分流 special / readonly / side_effect」之后、执行之前，插入决策预扫，把每个已知工具调用分到结局桶：

```
对每个已知工具调用（参数已解析成功）：
    req = adapter.to_request(tool, args, engine.mode)
    result = engine.decide(req)
    ├─ DENY  → 立即 ToolResult(ok=False, output=f"[权限拒绝·{result.layer}] {result.reason}")
    │          产出 TOOL_START+TOOL_RESULT，不执行（F8：回灌模型、不停 loop）
    ├─ ALLOW + 只读 → 并发桶（原只读并发路径，无确认）
    ├─ ALLOW + 副作用 → 串行执行桶（直接执行，不再弹确认 ← AC4 关键变化）
    └─ ASK（只可能副作用）→ 串行确认桶：
            allowed = ask(tc, tool, result)   # ask 闭包弹 4 选 1
            allowed ? 执行 : ToolResult(ok=False, "用户拒绝执行该工具。")
```

- **`confirm` 升级为 `ask`**：签名 `(ToolCall, Tool, DecisionResult) -> bool`。4 选 1（本次/本会话/永久/拒绝）、「本会话→`engine.add_session_rule`」「永久→`engine.persist_local_rule`」逻辑全部封装在 **ConversationManager 构建的 `ask` 闭包**，loop 只看 bool（侵入最小）。
- **special / unknown / 参数解析失败**：保持现有处理，不过引擎。
- **Plan Mode 正交**：规划阶段只暴露只读+特殊工具，无副作用工具，④自然不参与；引擎对只读照跑②③，行为一致无冲突。

### 模式切换接入
`ConversationManager` 解析新斜杠命令 **`/perm`**（无参循环 严格→默认→放行，沿用 `/think` 范式），调用 `engine.set_mode(...)`，并在状态栏/聊天反馈当前档位。

## 文件组织

```
rhinecode/
├── permission/                  ← 新增包
│   ├── __init__.py              — 导出公共符号
│   ├── models.py                — Decision/DecisionResult/PermissionMode/Layer/Rule/PermissionRequest
│   ├── blacklist.py             — ①固定危险命令正则 + 复合命令逐段匹配
│   ├── matching.py              — 命令/路径匹配 + 命令分隔符拆分
│   ├── rules.py                 — RuleSet：合并 + deny 优先求值
│   ├── config.py                — 三层 YAML 定位/加载/容错/回写
│   ├── adapter.py               — ToolCall+Tool+args → PermissionRequest
│   └── engine.py                — PermissionEngine：四层管线 + 可变状态
├── tools/
│   └── path_guard.py            — 强化：暴露 is_within_workspace 供引擎调用
├── agent/
│   └── loop.py                  — _execute 插入 decide；confirm 升级 ask
├── conversation.py              — 启动构建 engine；构建 ask 闭包；解析 /perm
└── tui/
    └── widgets.py               — 确认面板 2 按钮 → 4 选项
```

## 技术决策

| 决策点 | 选择 | 理由 |
|--------|------|------|
| 规则求值哲学 | deny 永远优先（跨三层合并后统一求值） | spec 哲学 A；安全 > 灵活，deny 不可被翻案 |
| 引擎与工具解耦 | adapter 收口工具知识，engine 纯逻辑 | N5 可测试 / N4 解耦；新增工具只改 adapter |
| 沙箱实现 | 复用 path_guard，引擎调用其判定，不重写 | N6 不破坏既有边界；DRY |
| 沙箱双重执行 | 引擎前置判 + 文件工具内保留 path_guard | 防御纵深；工具被直接调用（单测）时仍安全 |
| 黑名单可配置性 | 固定编译正则，代码内置 | spec「不可放开」；fail-safe（N1） |
| 模块组织 | 独立 permission/ 包 | 横切关注点，与 tools/agent 平级最清晰 |
| HITL 4 选 1 封装位置 | ConversationManager 的 ask 闭包 | 沿用现有 confirm 封装范式；loop 仅看 bool |
| 永久放行落盘 | 写本地级 .local.yaml | F6；不污染可提交的项目级 |
| 模式切换交互 | 斜杠命令 /perm | 沿用 /think、/plan 范式 |
| 配置格式 | YAML（allow/deny 字符串列表） | spec 定；人类可读可手编；复用已有 pyyaml |
| 启动默认档 | DEFAULT | spec F5 |
```
