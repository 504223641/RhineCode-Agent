# 保护路径层（②″）Plan

> 依据已批准的 `spec.md`。语言：Python 3.11+。

## 架构概览

五个组件，自下而上：

| 组件 | 位置 | 职责 |
| --- | --- | --- |
| **保护路径判定**（新增） | `permission/protected.py` | 纯函数 + 两张常量表：给定「路径 + 工作目录」回答「命不命中保护、为什么特殊」。零状态、零 IO 形态（只复用既有路径解析） |
| **收紧器**（改造） | `permission/engine.py` | 跑完既有五层拿到结论，再按判定结果**只把非 DENY 升级为 ASK**；持有会话级豁免集合 |
| **数据层**（改造） | `permission/models.py` | 新增 `Layer.PROTECTED` 与 `DecisionResult.protected_exempt` |
| **面板接线**（改造） | `tui/widgets.py` + `conversation.py` | 保护路径场景下面板去掉「永久放行」；「本会话放行」改走豁免登记 |
| **观测接线**（改造） | `agent/loop.py` + `trace/reader.py` | 埋点带上豁免标记；阅读器登记新层名并在摘要行标出豁免 |

**依赖方向不变**：`permission` 依赖 `tools.path_guard`（既有）；`tui` / `conversation` /
`agent` / `trace` 对 `permission` 的依赖方向也不变。不新增任何反向依赖。

## 为什么是「收紧器」而不是管线里的一站

spec「分歧一」已论证。落到代码上，两种写法的差别是：

```python
# ✗ 按 todo 字面：在②之后、③之前插一段并短路
if request.kind == "write_path" and protected.hit(...):
    return _verdict(Decision.ASK, Layer.PROTECTED, ...)   # ③的 deny 与④的严格档全被吞掉

# ✓ 本 plan：既有管线一字不动，出口处收紧
result = self._decide_core(request)
return self._apply_protected(request, result)
```

后者还有两个附带好处：

- **既有五层的代码一行不改**，N4「其余种类逐字不变」变成结构性成立而不是靠测试兜；
- `_decide_core` 保留原来的 `_verdict` 闭包，CLAUDE.md 那条「每条 return 都要填
  `kind`/`host`」的不变量原样有效。

⚠ 新出口 `_apply_protected` 是**第二个**构造 `DecisionResult` 的地方，
必须显式填 `kind` / `host`，并配一条护栏断言（见 checklist）。

## 核心数据结构

### `Layer.PROTECTED`（`permission/models.py`）

```python
PROTECTED = "protected"
```

展示名统一为 **`②″保护路径`**（`″` 是 U+2033，与既有 `②′` 同区块，
**不在符号护栏 `_SUSPECT` 的扫描区间内**，无需动白名单）。

三份表同步（CLAUDE.md 既有成对维护点，漏改当场红）：

| 表 | 文件 | 值 |
| --- | --- | --- |
| 枚举 | `permission/models.py` | `PROTECTED = "protected"` |
| 行为记录阅读器 | `trace/reader.py` `_LAYER_NAMES` | `"protected": "②″保护路径"` |
| 确认面板 | `tui/widgets.py` `ConfirmPanel._LAYER_LABELS` | `"protected": "②″保护路径"` |

### `DecisionResult.protected_exempt`（`permission/models.py`）

```python
protected_exempt: bool = False
```

语义：**本次命中了保护路径，但因会话级豁免而未被升级**。

为什么只加这一个布尔、而不是两个：「命中并升级」这件事**已经由 `layer == "protected"`
可见**，再加字段是重复。看不见的只有「命中了但放过了」这一种——不记的话，
时间线上只剩一条 `allow（④模式）`，读的人会以为用户切到了放行档。
与既有 `mode_downgraded` 是同一条理由的第二次。

### `ProtectedHit`（`permission/protected.py`，新增）

```python
@dataclass(frozen=True)
class ProtectedHit:
    path: Path      # 解析后的绝对路径（豁免集合的键就是它）
    reason: str     # 面向用户/模型的中文原因，已含「为什么这个文件特殊」
```

**一次解析、两处用**（升级判定 + 豁免查表），避免同一个路径解析两遍而口径漂移。

### 两张常量表（`permission/protected.py`）

```python
PROTECTED_RELATIVE: tuple[tuple[str, ...], ...] = (
    (".rhinecode",),
    (".git",),
)

EXCLUDED_RELATIVE: tuple[tuple[str, ...], ...] = (
    (".rhinecode", "sessions"),
    (".rhinecode", "context"),
    (".rhinecode", "traces"),
)
```

⚠ `EXCLUDED_RELATIVE` 与 `path_guard._RUNTIME_ARTIFACT_RELATIVE` **取值恰好相同，
但刻意不合一**：那张表的语义是「搜索时跳过」，本表是「写入不必过人眼」。
语义不同，合一会让将来任一侧的增删误伤另一侧（新增一个「不该进搜索结果但改了
会变天」的目录时，合一的表会静默把它从保护里摘掉）。两处都写注释互相指认。

### 「为什么特殊」映射（`permission/protected.py`）

```python
_WHY: tuple[tuple[tuple[str, ...], str], ...] = (
    ((".rhinecode", "hooks.yaml"),  "Hook 动作会直接执行，不经权限管线"),
    ((".rhinecode", "mcp.yaml"),    "会启动外部程序并把它的工具注册进工具中心"),
    ((".rhinecode", "agents"),      "子 Agent 角色定义（工具白名单与权限档）"),
    ((".rhinecode", "skills"),      "会被自动加载、指挥后续行为的指令文本"),
    ((".rhinecode", "memory"),      "会经索引注入系统提示的项目知识"),
    ((".rhinecode", "worktrees"),   "子 Agent 的隔离工作区，成果应经分支交付"),
    ((".rhinecode",),               "RhineCode 的配置目录，下次启动时加载"),
    ((".git",),                     "版本库内部（写 .git/hooks/ 等于让下次提交执行任意代码）"),
)
```

`permissions.yaml` / `permissions.local.yaml` 由 `(".rhinecode",)` 兜底
（"配置目录，下次启动时加载" 对它们成立），**不单列**——多一条精确项要多一份
维护，而它们的说明与兜底那句没有实质区别。

**按最长前缀匹配**，表内自上而下即由细到粗，取第一个命中项。

## 模块设计

### `permission/protected.py`（新增，叶子）

**职责**：回答「这个写入要不要过人眼、为什么」。纯逻辑。

**对外接口**：

```python
def protected_roots_of(root: Path) -> tuple[Path, ...]
def excluded_roots_of(root: Path) -> tuple[Path, ...]
def inspect(specifier: str, cwd) -> Optional[ProtectedHit]
```

`inspect` 的执行流程：

1. `path_guard.resolve_in_workspace(specifier, cwd)` 解析。
   **任何异常 → 按命中处理**（N3 偏严），reason 写「路径无法解析，按保护路径处理」。
   ⚠ 这里必须用 `resolve_in_workspace` 而不是自己拼 `Path`：它同时做了
   「拒绝 `..`」「绝对路径按真实位置判断」「解析符号链接」三件事，
   自己拼会让 AC5 的符号链接那一条静默失效。
2. 落在任一 `excluded_roots_of(cwd)` 内 → 返回 `None`（**排除优先**，
   因为排除项是保护项的真子集，顺序反了排除永远不生效）。
3. 落在任一 `protected_roots_of(cwd)` 内（含相等）→ 查 `_WHY` 组装 reason 返回。
4. 否则 `None`。

第 2、3 步的「落在其内」一律用 `path_guard.is_inside`（**路径相等/前缀**，
不按目录名——目录名匹配会误伤用户自己叫 `.git` 之外的同名业务目录，
与 `runtime_artifact_dirs_of` 同一条理由）。

**依赖**：`rhinecode.tools.path_guard`（既有方向）、标准库。

### `permission/engine.py`（改造）

**新增状态**：

```python
self.protected_exemptions: set[Path] = set()
```

**`decide` 拆成两段**：

```python
def decide(self, request):
    return self._apply_protected(request, self._decide_core(request))

def _decide_core(self, request): ...   # 既有 body，一字不动
def _apply_protected(self, request, result): ...
```

`_apply_protected` 的判定表（这就是 F6 的全部实现）：

| 条件 | 结论 |
| --- | --- |
| `request.kind != "write_path"` | 原样返回 |
| `result.decision is DENY` | **原样返回**（绝不降级——这是 N1 的落点） |
| `inspect(...)` 返回 `None` | 原样返回 |
| 命中，且 `hit.path in self.protected_exemptions` | 原样返回，但**置 `protected_exempt=True`** |
| 命中，未豁免 | 返回 `ASK @ Layer.PROTECTED`，reason 取 `hit.reason` |

⚠ **第二行为什么不是「只处理 ALLOW」**：默认档下写 `hooks.yaml` 时既有结论是
`ASK @ Layer.MODE`。若那一支原样返回，面板看到的层是 `mode`、会显示四个选项、
含「永久放行」——用户点下去写出一条③层 allow 规则，而下一次那条规则会被本层
升级回 ASK。**骗人的按钮原样存在，只是换了个入口。** 所以「非 DENY 一律换层」，
ASK→ASK 也要换。

**新增方法**：

```python
def grant_protected_exemption(self, request: PermissionRequest) -> bool
def is_protected_exempt(self, path: Path) -> bool          # 供测试与报告
```

`grant_protected_exemption` 接收**请求**而不是路径，使解析口径只有一处
（协调层不必自己调 `protected.inspect`）。未命中保护路径时不登记、返回 `False`。

**`derive()` 改造**：豁免集合与 `session_rules` **同口径共享同一个对象**。
理由与既有那条一致——用户在面板上的明确授予理应对子 Agent 生效；
且共享后子 Agent 拿到的豁免仍不多于主对话，C13 的「能力只会更小」原样成立。
⚠ 与 `load_errors` 同一个坑：构造函数里 `set()` 会新建，**必须在 `derive` 里显式赋值**。

### `conversation.py`（改造）

`_build_ask` 闭包里，`ALLOW_SESSION` 分支前置一个判断：

```python
if decision.layer is Layer.PROTECTED:
    # 保护路径的「本会话放行」**不产生任何③层规则、不写任何文件**
    self._engine.grant_protected_exemption(req)
    return True
```

`ALLOW_PERMANENT` 分支同样前置（**防御性**）：保护路径场景下面板压根不提供
「永久放行」，但若将来有第二条结算路径送进这个值，也必须落到豁免而不是落盘——
写盘会写出一条永远不被求值的规则，正是本扩展要消灭的那个按钮。

⚠ `req` 的构造点已在既有代码里（`to_request(tool, args, mode, main_project_root())`），
**位置不动**：协调层这条路径服务主对话，主对话的工作目录是不变量（C14 F4）。

### `tui/widgets.py`（改造）

`ConfirmPanel.show_for` 里，取 `layer_value`（沿用 `_url_detail_lines` 已有的
`getattr(layer, "value", layer)` 写法），保护路径时改用三项：

```python
("yes", "本次放行", "仅执行本次"),
("yes_session", "本会话放行", "本会话内对该文件不再询问，不写入配置"),
("no", "拒绝", "让模型据此调整（Esc）"),
```

「本会话放行」的说明**必须写明「不写入配置」**——用户对这个选项的既有心智是
「登记一条会话规则」，而这里换了机制。

`_LAYER_LABELS` 加一行。`app.py` 的 `option.id → ConfirmDecision` 映射**不动**
（少一个 id 不影响那张表）。

### `agent/loop.py`（改造）

普通工具那处 `permission_decision` 埋点加一个字段：

```python
protected_exempt=decision.protected_exempt,
```

**系统级工具（`system_serial`）那处不加**：那七个工具落 `other` 分支，
`kind != "write_path"`，本层对它们恒不生效，加进去就是一个恒为假的字段，
会稀释掉 `mode_downgraded` 那条真正有信息量的标记。
（阅读器用 `.get()` 取值，字段缺席即假，不会出错。）

### `trace/reader.py`（改造）

- `_LAYER_NAMES` 加 `"protected": "②″保护路径"`。
- `_s_permission_decision` 的标记位加一支：`protected_exempt` 为真时标出
  「保护路径已豁免」。判据是 CLAUDE.md 那条——「排查时第一眼要不要看到它」：
  用户问「它怎么没弹面板就把 hooks.yaml 改了」时，第一件要确认的就是这个。

## 模块交互

```
agent/loop.py
   └─ engine.decide(request)
        ├─ _decide_core(request)           既有五层，一字不动
        └─ _apply_protected(request, r)
             ├─ protected.inspect(spec, cwd)     纯函数
             │    └─ path_guard.resolve_in_workspace / is_inside
             └─ self.protected_exemptions        内存集合（derive 共享）

tui/app.py ─ ConfirmPanel.show_for(decision)
                └─ decision.layer.value == "protected" → 三选项

conversation.py ask 闭包
   └─ ALLOW_SESSION / ALLOW_PERMANENT 且 layer 是 PROTECTED
        └─ engine.grant_protected_exemption(req)
             └─ protected.inspect(...)  → 把 hit.path 塞进集合

trace/reader.py ← permission_decision 负载（layer + protected_exempt）
```

## 文件组织

```
rhinecode/
├── permission/
│   ├── protected.py     ★新建 — 两张常量表 + _WHY + ProtectedHit + inspect
│   ├── models.py        改 — Layer.PROTECTED、DecisionResult.protected_exempt
│   └── engine.py        改 — _decide_core / _apply_protected / 豁免集合 / derive
├── conversation.py      改 — ask 闭包两个分支前置豁免登记
├── agent/loop.py        改 — 埋点加 protected_exempt
├── trace/reader.py      改 — _LAYER_NAMES + 摘要标记
└── tui/widgets.py       改 — _LAYER_LABELS + show_for 三选项

tests/
├── test_perm_protected.py    ★新建 — 判定层（范围 / cwd / 顺序护栏 / 两条反证 / 豁免语义 / derive 共享）
└── test_protected_wiring.py  ★新建 — 接线（面板三选项 / ask 闭包不落盘 / 埋点字段 / 阅读器摘要）

docs/extensions/protected-paths/   spec / plan / task / checklist
docs/extensions/README.md          登记本扩展
CLAUDE.md                          架构表 ⚠ 列 + 成对维护点 + 安全边界
docs/todo/                         删 2-*，其余重排序号并修交叉引用
```

## 技术决策

| 决策点 | 选择 | 理由 |
| --- | --- | --- |
| 层的形态 | 出口处的**收紧器**，不是管线中的一站 | 短路会把③的 deny 与④严格档的 DENY 一起吞掉，两处都是放宽（spec 分歧一） |
| 升级的触发条件 | 结论**非 DENY** 即换层，不只是 ALLOW | 只处理 ALLOW 的话，默认档那次 ASK 的层仍是 `mode`，面板照样给出「永久放行」，骗人的按钮换个入口原样存在 |
| 判定基准 | `request.cwd` | 按主项目根判定会让隔离子 Agent 的每次写入都命中，而它非交互、判 ASK 即自动拒绝——表现为静默失败（spec 坑 1） |
| 清单方向 | 黑名单式（默认保护 + 显式排除三项） | 遗漏方向偏严：漏排除是「多弹一次面板」（看得见），漏保护是静默的洞 |
| 排除表 | 与 `_RUNTIME_ARTIFACT_RELATIVE` **不合一** | 语义不同（搜索跳过 vs 写入免审）。合一会让任一侧增删误伤另一侧 |
| 路径解析 | 复用 `resolve_in_workspace` | 自己拼 `Path` 会丢掉「拒绝 `..`」「绝对路径按真实位置」「解析符号链接」三件事，AC5 静默失效 |
| 排除优先于保护 | 是 | 排除项是保护项的真子集，顺序反了排除永远不生效 |
| 豁免的键 | 解析后的**绝对路径**，精确到文件 | 与既有面板「本会话放行」的最小授权口径一致；目录级豁免等于把整个 `.rhinecode/` 交出去 |
| 豁免的存放 | 引擎实例上的内存集合，`derive` 共享 | 与 `session_rules` 同生命周期与共享口径；落盘会造出一份「能改变以后会发生什么」的新配置，绕回原问题 |
| 面板改动 | 保护路径场景**去掉**「永久放行」 | spec F8（选项 C）。留着它就是一个点了没用的按钮 |
| trace 字段 | 只加 `protected_exempt` 一个布尔 | 「命中并升级」已由 `layer` 可见；看不见的只有「命中但放过了」 |
| 埋点位置 | 只在普通工具那处加字段 | 系统级工具落 `other` 分支，本层恒不生效，加进去是恒假字段 |
| 层名符号 | `②″`（U+2033） | 与既有 `②′` 同族；两者都不在符号护栏的扫描区间内，无需动白名单 |
