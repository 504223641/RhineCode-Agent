# C11 Skill 系统 Plan

> 状态：已批准（2026-07-26，第 3 轮修订：补入 `_resume_stream` 的激活态清空接线——该遗漏在 task 拆解阶段暴露，见 4.9）
> 依据：已批准的 `docs/c11/spec.md`（F1–F32、N1–N10、AC1–AC38）

## 1. 架构概览

新增一个独立的 `rhinecode/skills/` 包，与 `permission/` `context/` `memory/` `commands/` 同级，遵循同一范式：**纯逻辑 + 单点接入**，不导入 Textual 与 Provider SDK，可用替身在无终端无网络环境测试（N1）。

包内六个模块严格单向依赖，下层不感知上层：

```
models.py      数据结构与枚举（只依赖标准库 + tools/policy.py）
   ↑
parser.py      单份 Skill 文本 → SkillSpec / 失败原因（纯函数）
   ↑
discovery.py   三层目录扫描、层内去重、跨层覆盖（只做 IO 与聚合）
   ↑
render.py      清单 / 激活正文 / 参数替换 / 自包含文本（纯函数）
   ↑
validation.py  白名单两段校验与空集降级（纯函数）
   ↑
manager.py     SkillManager：唯一持有可变状态与副作用编排
```

包外接入点共八处，每处都是「单点」：

| 层 | 接入内容 | 性质 |
|---|---|---|
| `tools/policy.py` | `ToolPolicy` 数据类（agent 与 skills 的共同下层） | 新增 |
| `agent/prompt/` | 新增稳定模块「可用 Skill 清单」；填充既有 120 槽位 | 扩展 |
| `agent/loop.py` | 动态段逐轮重算；工具集按策略过滤；`RunOptions` | **改造点 1** |
| `commands/registry.py` | Skill 短命令的运行时原子替换 | **改造点 2** |
| `commands/` | `/skills` 内置命令 + 动态短命令工厂 | 扩展 |
| `tools/load_skill.py` | `load_skill` 系统级工具 | 新增 |
| `conversation.py` | 构造 SkillManager、领域方法、独立模式子对话编排 | 扩展 |
| `tui/` | 控制器方法、状态栏 Skill 段、提交守卫提示 | **改造点 3** + 扩展 |

### 1.1 依赖方向裁定

`ToolPolicy` 同时被 `agent/loop.py`（消费）与 `skills/manager.py`（生产）使用。把它放进任一方都会制造反向依赖，因此**下沉到 `tools/policy.py`**——`tools` 是 agent 与 skills 的共同下层（`agent/loop.py` 本来就导入 `tools.base` 与 `tools.registry`），`tools/policy.py` 自身零依赖，不会形成环。

修正后的准确表述（原文「循环完全不认识 skills 层」措辞过强）：**Agent 循环不认识 `SkillManager`，只认识 `tools` 层的一个数据类**。`skills` 仍是不被任何层反向依赖的叶子。

完整依赖图（均单向，无环）：

```
tools/policy.py ← agent/loop.py
                ← skills/models.py
skills/*        ← commands/skill_commands.py   （经中立的 SkillCommandInfo）
                ← tools/load_skill.py           （由 __main__ 注册，tools/registry.py 不导入它）
                ← conversation.py
```

**无环性的隐含前提，必须写死**：`tools` 与 `skills` 是**包级互相依赖**（`skills.models → tools.policy`，`tools.load_skill → skills.manager`），它不成环唯一依赖「`rhinecode/tools/__init__.py` 不 re-export 任何子模块」——该文件目前只有 docstring。这与既有的 `tools ↔ mcp` 互依是同一个模式（`tools/mcp_config.py → mcp.auto_config`，`mcp/tool_adapter.py → tools.base`），已在仓库中稳定运行，因此 B3 不是新引入的风险模式。

将来若有人为图方便在 `tools/__init__.py` 加一行 `from rhinecode.tools.registry import ToolRegistry`，`skills.models → tools.policy` 就会在 `tools` 包半初始化状态下触发 `load_skill → skills` 回环并 `ImportError`。**因此须在 `tools/__init__.py` 的 docstring 里加一句注释把这条前提固化下来**（task 阶段落实）。

---

## 2. 核心数据结构

除 `SkillManager` 的内部可变状态外，全部 `@dataclass(frozen=True)`。

### 2.1 枚举（`skills/models.py`）

```python
class SkillMode(Enum):
    SHARED = "shared"       # 共享模式：注入主对话，结果留在主历史
    ISOLATED = "isolated"   # 独立模式：开子对话跑完回流结论

class SkillSource(Enum):
    PROJECT = "project"     # <项目根>/.rhinecode/skills/
    USER    = "user"        # ~/.rhinecode/skills/
    BUILTIN = "builtin"     # 随包分发

class DegradeKind(Enum):
    """注入降级的两种形态，严重程度不同，必须能被用户区分（R-h）。"""
    TRUNCATED = "truncated"   # 单体超 BODY_MAX_*，正文被切掉后半段
    DROPPED   = "dropped"     # 累加超 TOTAL_MAX_*，整段未注入 = 完全没生效
```

`SkillSource` 的成员定义顺序即优先级顺序（PROJECT 最高），`discovery` 按此顺序做跨层覆盖，不另外维护优先级表。

### 2.2 `ToolPolicy`（`tools/policy.py`）

```python
@dataclass(frozen=True)
class ToolPolicy:
    allowed: Optional[frozenset[str]]   # None = 不收窄；否则只保留集合内的注册中心工具
    exempt: frozenset[str]              # 豁免收窄、永远可见（主对话含 load_skill）
    excluded: frozenset[str]            # 无条件移除（子对话的 load_skill）
```

三个字段各自对应一条 spec 条款：`allowed` ← F14 并集与运行期自愈；`exempt` ← F8/F15；`excluded` ← F23。用一个结构承载而非三个参数，是为了让 `Agent.run` 的参数不再膨胀。

### 2.3 `SkillSpec` — 一个已成功加载的 Skill

```python
@dataclass(frozen=True)
class SkillSpec:
    name: str                          # frontmatter name，已过 F4 校验
    description: str                   # 一句话说明
    body: str                          # SOP 正文（未做参数替换）
    mode: SkillMode
    allowed_tools: Optional[tuple[str, ...]]   # None = 未声明白名单（不收窄）
    history_messages: int              # 独立模式带入条数，>= 0
    model: Optional[str]               # 仅独立模式生效
    source: SkillSource
    entry_path: Path                   # 单文件型=该 .md；目录型=SKILL.md
    resource_dir: Optional[Path]       # 仅目录型非空（F13）
    resource_files: tuple[str, ...]    # 目录型随附文件相对路径，上限 RESOURCE_LIST_MAX
```

`allowed_tools` 用 `Optional[tuple]` 而非空 tuple 表示「未声明」——空 tuple 是「声明了但被剔空」（F17 降级的输入），两者语义不同，不能合并。

### 2.4 `SkillLoadError` / `SkillCatalog`

```python
@dataclass(frozen=True)
class SkillLoadError:
    path: Path
    source: SkillSource
    reason: str        # 可读中文原因，直接进 /skills 展示

@dataclass(frozen=True)
class SkillCatalog:
    skills: tuple[SkillSpec, ...]          # 已按 name 排序，跨层覆盖后每名唯一
    errors: tuple[SkillLoadError, ...]
    warnings: tuple[str, ...]              # 非致命提示（共享模式声明 model 等，F22）
```

热更新（F26）的实现就是「造一个新 Catalog 整体替换旧的」——不可变快照使替换天然原子，不存在半更新状态。

### 2.5 `ActiveSkill` — 一条激活记录（共享模式专用）

```python
@dataclass(frozen=True)
class ActiveSkill:
    name: str
    arguments: str        # 最近一次激活传入的参数（F30 幂等）
```

**降级信息不放在这里**：`ActiveSkill` 是 frozen，`degrade` 若作为字段就无法在每轮 `active_text()` 时写回（`FrozenInstanceError`），只能整列表 `dataclasses.replace` 重建，而这个写操作又必须进锁、与 A1 的加锁范围纠缠。因此降级信息由 `SkillManager` 单独持有一个**纯派生**字典 `self._degrades: dict[str, DegradeKind]`：`active_text()` 只写它，`report()` / `status_segment()` 只读它，`ActiveSkill` 保持真正不可变（`arguments` 的幂等更新走 `replace` 重建单条）。

`DegradeKind` 用枚举而非 bool：「被切掉后半段」与「整段没注入」后果差一个量级，`/skills` 与激活警告必须能分别措辞，否则 spec F9 要求的「截断对用户可见」形同虚设。

激活列表是 `list[ActiveSkill]`，顺序即 F10 的拼接顺序。**正文不存在这里**，每轮从 catalog 现取——这样热更新后正文自动是新的（F27），无需同步两份状态。

### 2.5.1 `ActivationResult` / `ReloadOutcome`

两者出现在 `SkillManager` 的接口签名里，字段必须定死，否则 task 拆分时各处会各写各的。

```python
class ActivationStatus(Enum):
    ACTIVATED = "activated"      # 共享模式激活成功（含幂等重复激活）
    NOT_FOUND = "not_found"      # 名字不存在
    ISOLATED  = "isolated"       # 独立模式，模型不能发起（F7）

@dataclass(frozen=True)
class ActivationResult:
    status: ActivationStatus
    name: str
    available_names: tuple[str, ...] = ()     # NOT_FOUND 时给模型的可用名字列表
    entry_hint: Optional[str] = None          # ISOLATED 时的实际可用入口："/review" 或 "/skills run review"
    degrade: Optional[DegradeKind] = None     # ACTIVATED 时本次注入是否降级，供警告文案

@dataclass(frozen=True)
class ReloadOutcome:
    added: tuple[str, ...]            # 新出现的 Skill
    removed: tuple[str, ...]          # 消失的 Skill
    auto_deactivated: tuple[str, ...] # 已激活但消失、被自动卸载的（F27）
    dropped_fatal: tuple[str, ...]    # 内置工具名笔误被丢弃的（决策 20）
    warnings: tuple[str, ...]         # 全部警告，含「下次启动会失败」提示
    errors: tuple[SkillLoadError, ...]
```

`ActivationResult` 同时服务三个消费者：`LoadSkillTool.execute` 的返回值（F7 三态文案）、短命令路径的失败提示、以及降级警告。`ReloadOutcome` 由 `/skills reload` 渲染成一段可读报告。

### 2.6 `SkillCommandInfo` — 交给命令层构造短命令的中立描述

```python
@dataclass(frozen=True)
class SkillCommandInfo:
    name: str            # 不带斜杠
    description: str
    mode: SkillMode
```

存在的意义是**切断 skills → commands 的依赖**：skills 只产出中立描述，由 `commands/skill_commands.py` 转成 `CommandSpec`。

### 2.7 `RunOptions`（`agent/loop.py`）

```python
@dataclass(frozen=True)
class RunOptions:
    max_iterations: int = MAX_ITERATIONS              # N6：子对话有独立预算
    record_usage: bool = True                         # 子对话不更新主历史估算锚点
    allow_summary: bool = True                        # 子对话只跑 C8 第一层（F21）
    tool_policy: Optional[Callable[[], ToolPolicy]] = None
```

**`tool_policy` 必须是 callable 而非值**（R-a）：与 `dynamic` 同理，模型可能在第 N 轮激活 Skill，第 N+1 轮的工具集就该收窄；若在 `run()` 调用前算一次、整轮不变，将直接违反 F14 的运行期自愈与 AC14 的逐轮断言。

`Agent.run` 现有签名已有 14 个参数，新增四个开关会失控。用 `RunOptions` 打包，**默认值即 C10 行为——不传等于零回归**，这也是 AC29 的实现保障。

---

## 3. 模块设计（skills 包）

### 3.1 `models.py`

**职责**：上述数据结构与枚举，外加模块级常量。**依赖**：标准库 + `tools/policy.py`。

```python
NAME_PATTERN         = re.compile(r"^[a-z][a-z0-9-]{0,31}$")          # F4
RESERVED_SUBCOMMANDS = frozenset({"reload", "off", "prompt", "run"})  # F4
ENTRY_FILENAME       = "SKILL.md"                                     # F2
LOAD_SKILL_TOOL      = "load_skill"                                   # F7
PLACEHOLDER          = "$ARGUMENTS"                                   # F12

INDEX_MAX_LINES, INDEX_MAX_BYTES = 200, 25 * 1024   # F6
BODY_MAX_LINES,  BODY_MAX_BYTES  = 300, 12 * 1024   # F9 单个
TOTAL_MAX_LINES, TOTAL_MAX_BYTES = 600, 24 * 1024   # F9 合计
RESOURCE_LIST_MAX = 50                               # F13
MCP_PREFIX = "mcp__"                                 # F16 判别式（双下划线）
SKILL_MAX_ITERATIONS = 15                            # N6 子对话迭代上限
```

**内置样板目录的定位（S-2）**：同处 `models.py`，供 `discovery` 与 `conversation` 共用一处实现——

```python
def builtin_skills_dir() -> Path:
    """随包分发的内置 Skill 目录（F3 第三层）。

    用 Path(__file__).parent / "builtin" 而非 importlib.resources：本项目以常规目录
    形式安装（非 zip import），该写法在开发模式（pip install -e .）与真实安装下都成立，
    前提是 pyproject 的 package-data 把 skills/builtin/*.md 打进了分发包（见第 6 节）。
    """
    return Path(__file__).resolve().parent / "builtin"
```

### 3.2 `parser.py` — 单份文本 → SkillSpec

```python
def parse_skill(
    text: str, path: Path, source: SkillSource,
    resource_dir: Optional[Path], resource_files: tuple[str, ...],
) -> tuple[Optional[SkillSpec], Optional[str], list[str]]:
    """:returns: (spec, 失败原因, 非致命警告)；spec 与失败原因恰有一个非 None"""
```

**执行步骤**：

1. 切分 frontmatter：首行须为 `---`，找第二个 `---` 行；缺失 → 失败「缺少 YAML frontmatter」。
2. `yaml.safe_load`；异常 → 失败「frontmatter 解析失败：<摘要>」；结果非 dict → 失败。
3. `name`：缺失/非 str → 失败；不匹配 `NAME_PATTERN` → 失败「名字不合法」；命中 `RESERVED_SUBCOMMANDS` → 失败「名字使用了保留子命令词」。
4. `description`：缺失/空 → 失败。
5. `allowed_tools`：缺省 `None`；非 list 或元素非 str → 失败（AC1 明确要求）；否则去重保序转 tuple。
6. `mode`：缺省 `shared`；不在枚举 → 失败。
7. `history_messages`：缺省 0；非 int 或 <0 → 失败。
8. `model`：缺省 None；非 str → 失败。**mode 为 shared 且 model 非空 → 不失败，产出警告**「共享模式忽略 model 声明」（F22 要求扫描期警告）。
9. 未知键：忽略（向前兼容，不失败不警告）。
10. 正文 = frontmatter 之后全部内容；`strip()` 为空 → 失败「SOP 正文为空」。

失败即返回原因字符串，由 discovery 包装成 `SkillLoadError`。

### 3.3 `discovery.py` — 三层扫描

```python
def discover(project_root: Path, user_dir: Path, builtin_dir: Path) -> SkillCatalog
```

1. 按 `SkillSource` 顺序（PROJECT → USER → BUILTIN）确定目录；不存在的目录跳过（不算错误）。
2. 层内：`sorted()` 列出直接子项（F29 依赖此字典序）。
   - `*.md` → 单文件型，`resource_dir=None`；
   - 目录且含 `SKILL.md` → 目录型，`resource_files` 取 `rglob("*")` 中的文件相对路径（排除 `SKILL.md`、字典序、截断到 `RESOURCE_LIST_MAX`）；
   - 目录但缺 `SKILL.md` → 记错误「目录型 Skill 缺少 SKILL.md」；
   - 读失败（`OSError` / 解码失败）→ 记错误，继续。
3. **层内去重（F29）**：本层 `seen: dict[str, Path]`；name 已存在 → 记错误「与同层 `<先到文件名>` 重名」并丢弃后者。
4. **跨层覆盖（F3）**：全局 `chosen: dict[str, SkillSpec]`，按层顺序 `setdefault`——先到（高优先层）保留，低优先层同名直接丢弃且**不记错误**（覆盖是正常行为）。
5. 汇总：`skills` 按 name 排序，errors/warnings 按发现顺序。

**副作用**：只读文件系统。**fail-safe**：任何单份失败只记录不抛出（N2）。

### 3.4 `render.py` — 全部文本产出

纯函数、无状态，集中一处便于测试与调整措辞。

```python
def render_index(skills) -> str
    # F6：每行 "- <name>（<模式>）：<description>"，头部一句说明
    #「用 load_skill 加载共享模式 Skill；独立模式请建议用户执行其命令」，
    # 超 INDEX_MAX_* 截断并追加 "（另有 N 个未列出）"

def substitute(body: str, arguments: str) -> str
    # F12：含 $ARGUMENTS 则替换全部出现处；不含且 arguments 非空 →
    # 末尾追加 "\n\n## 用户补充参数\n\n<arguments>"

def render_resources(spec) -> str
    # F13：仅目录型。绝对路径 + 相对路径清单 + 一句
    #「工作区外的资源目录不支持 glob/grep 枚举，请按上述清单直接读取」

def render_active_body(spec, arguments) -> tuple[str, Optional[DegradeKind]]
    # 单个 Skill 的注入段：边界标识（F10）+ substitute 后的正文 + render_resources；
    # 超 BODY_MAX_* 则切断并在末尾标注，返回 DegradeKind.TRUNCATED

def render_active_section(
    items: Sequence[tuple[SkillSpec, str]]
) -> tuple[str, dict[str, DegradeKind]]
    # F9/F10：按顺序拼接，逐个套 BODY_MAX_*，整体套 TOTAL_MAX_*。
    # 返回的 dict 精确区分每个受影响 Skill 是 TRUNCATED 还是 DROPPED（R-h）

def render_invocation_text(spec, arguments) -> str
    # F24 自包含语义文本，必含三项：
    # "执行 Skill /<name>（<description>）\n参数：<arguments 或「无」>"
```

**截断策略（F9 / 决策 10）**：先对每个 Skill 的单体正文套 `BODY_MAX_*`（超出 → 切断，记 `TRUNCATED`）；再按激活顺序累加，当累加将超 `TOTAL_MAX_*` 时，**当前这个 Skill 整段丢弃**（记 `DROPPED`），其后的也一并 `DROPPED`。理由：切半会让模型执行残缺流程（spec F9 已论证），整段丢弃至少是「这个 Skill 没生效」这种可判定状态。

### 3.5 `validation.py` — 白名单校验

```python
@dataclass(frozen=True)
class FatalToolName:
    skill_name: str; path: Path; tool_name: str

def check_builtin_tool_names(skills, known: frozenset[str]) -> list[FatalToolName]
    """F16 第一段：白名单里不以 mcp__ 开头、且不在 known 中的名字，全部作为致命项返回。"""

def collect_exempt_notices(skills, exempt: frozenset[str]) -> list[str]
    """对声明了 load_skill / ask_user / present_plan 的 Skill 产出「声明无效果，可删除」提示。"""

def prune_mcp_tool_names(skills, registered: frozenset[str]) -> tuple[list[SkillSpec], list[str]]
    """F16 第二段 + F17：剔除不存在的 mcp__ 名字；剔空的 Skill 其 allowed_tools 置回 None
       并产出降级警告。:returns: (新 spec 列表, 警告列表)"""
```

`known` 由调用方传入：「工具注册中心当前全部名字 ∪ `{ask_user, present_plan}`」。**`load_skill` 已注册在工具注册中心，天然落在 `known` 中**，无需特判。

`check_builtin_tool_names` 返回非空时由 `__main__` 打印并以非成功状态退出（F16）：

```
Skill 白名单引用了不存在的工具：
  <skill name>（<entry_path>）→ <tool name>
若确认该工具名无误，可能是 RhineCode 版本与该 Skill 不匹配。
```

### 3.6 `manager.py` — `SkillManager`

**职责**：唯一持有可变状态与副作用编排。

**状态与并发**：

```python
self._catalog: SkillCatalog             # 不可变快照，热更新整体替换
self._active: list[ActiveSkill]         # 共享模式激活列表，顺序即拼接顺序
self._degrades: dict[str, DegradeKind]  # 纯派生：上次注入的降级形态
self._runtime_warnings: list[str]       # 启动/热更新的降级警告，供 /skills 展示
self._lock: threading.Lock              # 保护 _active / _degrades / _runtime_warnings；
                                        # _catalog 是不可变快照，读引用无需持锁、替换在锁内
```

**为什么需要锁（S-g）**：`load_skill` 声明 `read_only=True`（见 4.8），因而进入 `loop._run_readonly_concurrent` 的**并发**桶。模型同一轮发起两次 `load_skill` 时，两个线程会并发执行 `activate()` 的「查在不在 → 在则改、不在则 append」读-改-写序列，GIL 保证不了这个复合操作的原子性，可能产生两条同名记录（违反 F30）。同时 `active_text()` 由主循环线程每轮调用、`reload()` / `status_segment()` 由 UI 线程调用，三方都会碰 `_active`。用一把普通 `Lock` 保护全部读写，临界区都是纯内存操作、开销可忽略。

#### 加锁不变量（A1，必须遵守，否则确定性死锁）

> **临界区只包含纯内存状态读写；一切解析、渲染、回调、IO 都在锁外。**
> 推论：持锁期间禁止调用任何回调，禁止任何形式的跨线程调度。

这条总纲同时覆盖 `activate` / `active_text` / `report` 三处，也让将来新增方法有据可依。

这不是洁癖，是一条已验证的死锁路径：

1. `activate()` 若用 `with self._lock:` 包住整个方法体，末尾的 `notify_activation` 就在锁内触发；
2. `notify_activation` → `RhineApp._notify_skill_activation` → `self.call_from_thread(self._refresh_status)`。**Textual 的 `call_from_thread` 是阻塞的**——既有代码 `app.py:541` 写的是 `thinking_widget = self.call_from_thread(history_view.begin_thinking_turn)`，取返回值，必然同步等待主线程执行完；
3. 主线程执行 `_refresh_status` → 调 `skill_status_segment()` → `SkillManager.status_segment()` → **申请同一把锁**，而锁正被工作线程持有；
4. 工作线程等主线程跑完回调，主线程等工作线程放锁 → **双向永久阻塞**。

后果远超普通 bug：主线程是 Textual 事件循环，卡死后整个 TUI 冻结、连 Esc 取消都不响应，用户只能杀进程。而触发条件极其普通——模型成功调一次 `load_skill` 即可。

**三条强制约定**：

1. `activate()` 在 `with self._lock:` 块内**只**完成状态变更并算出 `ActivationResult`，**出块之后**再调 `notify_activation`；
2. 同一约定覆盖 `activate()` 中的 `has_short_command` 回调——它跨层调进 `CommandRegistry`，虽然目前不取锁也不碰 UI 线程，但同样不得在锁内发生（因此 `ISOLATED` 早返回分支整段在锁外，见下文 `activate` 的四段式）；
3. 加固层：`status_segment()` / `report()` / `prompt_report()` 等读路径一律「**持锁取一个不可变快照 → 出锁后渲染**」。即使将来有人不慎在锁内触发回调，也不会立刻演变成死锁。

**对外接口**：

```python
# — 构造 —
@classmethod
def empty(cls) -> "SkillManager"    # 空 catalog、不扫盘，供协调层缺省注入（M3）

# — 生命周期 —
def startup(self, known: frozenset[str]) -> list[FatalToolName]
def bind_tools(self, registered: frozenset[str]) -> None
def reload(self, known, registered) -> ReloadOutcome

# — 激活 —
def activate(self, name: str, arguments: str) -> ActivationResult
def deactivate(self, name: Optional[str]) -> str
def clear_active(self) -> None

# — 注入（每轮调用）—
def index_text(self) -> str                                    # F6，进 stable
def active_text(self) -> str                                   # F9/F10，进 dynamic
def tool_policy(self, registered: frozenset[str]) -> ToolPolicy
def isolated_policy(self, spec, registered) -> ToolPolicy

# — 查询 —
def get(self, name) -> Optional[SkillSpec]
def command_infos(self) -> tuple[SkillCommandInfo, ...]
def project_skill_notice(self) -> Optional[str]                # N8
def report(self) -> str                                        # /skills
def prompt_report(self, registered) -> str                     # /skills prompt
def status_segment(self) -> Optional[str]                      # F32
```

**关键算法**：

- **`activate(name, arguments)` — 四段式，临界区收到最小（M1）**：

  | 段 | 是否持锁 | 内容 |
  |---|---|---|
  | ① 解析 | **锁外** | 读 `_catalog`（不可变快照，取引用即可）解析 spec；`get(name)` 为 None → 返回 `NOT_FOUND` 结果（附可用名字列表，供 F7 文案） |
  | ② 独立模式早返回 | **锁外** | `spec.mode is ISOLATED` → 查 `has_short_command` 得 `entry_hint`，返回 `ISOLATED` 结果（F7）。**该分支根本不动可变状态，本就不需要锁**；且它含跨层回调，按约定 ② 必须在锁外 |
  | ③ 状态变更 | **锁内** | 已在 `_active` → 原地更新 arguments、位置不动（F30 幂等，走 `dataclasses.replace` 重建单条）；否则追加 `ActiveSkill` |
  | ④ 通知与返回 | **锁外** | 调 `notify_activation`（外层已包 try/except，见 4.10），返回 `ActivationResult` |

  **只有 ③ 在锁内**。这与上文约定 ② 一致——早期版本写成「步骤 1–4 在锁内」，与「`has_short_command` 不得在锁内」自相矛盾，已修正。

  **入口判定谓词（R-b）**：`has_short_command` 在 ② 中调用，实现见下。

  构造时注入 `has_short_command: Callable[[str], bool]`，其实现**必须查询「实际被注册表接受的 Skill 短命令集合」，而不是 `registry.resolve(f"/{name}")`**。后者在 F25 场景下会命中**内置**同名命令而返回 True，于是 F7 的文案会让用户去执行内置 `/context`——这比 spec 极力避免的「指向不存在的命令」更糟：指向了一个存在但错误的命令。因此由 `CommandRegistry.has_skill_command(name)` 查 `_skill_specs` 提供（见 4.4）。

- **`active_text()`**：持锁取 `_active` 与 `_catalog` 的快照 → **出锁**后逐条取 spec（热更新后自动是新正文，F27）并交给 `render_active_section` 渲染 → 再持锁把返回的降级字典写回 `self._degrades`。渲染在锁外进行（纯函数、可能处理数万字符，不该占着锁）。每轮调用，除刷新 `_degrades` 外无副作用。

  **写回时只保留仍在 `_active` 中的名字（S-1）**：两段临界区之间存在窗口，若期间 `deactivate()` / `reload()` 摘掉了某个 Skill，无条件整体赋值会给一个已不在激活列表里的名字留下降级标记，`/skills` 会显示一条幽灵条目。实际几乎不可达（F31 已禁止循环运行期间执行 `/skills off` / `reload`，而 `active_text()` 只在循环内被调用），但一行过滤即可闭掉。

- **`tool_policy(registered)`**（F14/F15/F17）：
  ```
  _active 为空                          → ToolPolicy(None, {LOAD_SKILL}, ∅)
  任一激活 Skill 的 allowed_tools 为 None → ToolPolicy(None, {LOAD_SKILL}, ∅)   # N10 塌缩
  否则 allowed = ⋃(各 allowed_tools) ∩ registered                              # 运行期自愈
       allowed 为空 → ToolPolicy(None, {LOAD_SKILL}, ∅)                        # F17
       否则         → ToolPolicy(allowed, {LOAD_SKILL}, ∅)
  ```

- **`isolated_policy(spec, registered)`**：同上但只看这一个 spec，且 `exempt=∅`、`excluded={LOAD_SKILL}`（F21/F23）。

- **`reload`**：与 `startup` 只差一处——第一段校验的致命项在这里**不终止进程**，而是丢弃该 Skill 并记为警告。理由：F16/N2 的定语都锚定「启动」路径，F26 只要求「重跑白名单校验」，F31 明确热更新不影响运行中的循环；运行中的会话不该被一次热更新杀掉。
  **连带告知义务（S-c）**：该警告文案必须写明「该 Skill 已被本次热更新丢弃；**下次启动时此错误会导致启动失败**，请尽快修正」——否则用户会把它当成小问题，第二天启动不来。
  另外对「已激活但在新 catalog 中消失」的 Skill 自动卸载并提示（F27）。

---

## 4. 包外接入点设计

### 4.1 `agent/prompt/` — 两个槽位

`modules.py`：`optional_slots()` 新增 `PromptModule(name="可用 Skill 清单", priority=140, cacheable=True, content="")`。
**priority 140 而非 115**：稳定段是前缀缓存的作用对象，排在最后使热更新只失效清单自己那一段，不波及 priority 130 的长期记忆索引（F6 明文要求）。「已激活 Skill」槽位（120，`cacheable=False`）保持不动。

`builder.py`：`build_default_prompt` 增两个关键字参数，与 c9 两参数完全同构：

```python
def build_default_prompt(env, custom_instructions="", memory_index="",
                         skill_index="", active_skills="") -> AssembledPrompt
```

`skill_index` 以 `cacheable=True` 进 stable（140）；`active_skills` 以 `cacheable=False` 进 dynamic（120）。空串照旧整体跳过。

### 4.2 `agent/loop.py` — 改造点 1 与工具过滤

**(a) 动态段逐轮重算。** `run()` 的 `dynamic: str` 改为 `dynamic: Callable[[], str]`，循环内每轮调用：

```python
reminder = build_system_reminder(dynamic(), toggle)
```

选 callable 而非把 `SkillManager` 传进循环，是为了让循环不认识 skills 层（只认识 `tools/policy.py` 的一个数据类，见 1.1）。**既有影响面已核实（S-f）**：源码调用点仅 `conversation.py:531`，测试调用点仅 `tests/test_perm_loop.py:59`（第 5 个位置参数传 `""`，改成 `lambda: ""` 即可）。

**(b) 工具集过滤。** `_schema_for` 增参并改为显式分支（原写法把同一条件写了两遍，可读性差）：

```python
def _schema_for(self, plan_mode, execution_phase, policy) -> Optional[list[dict]]:
    if self._registry is None:
        return None
    planning = plan_mode and not execution_phase
    base = self._registry.readonly_schemas() if planning else self._registry.schemas()
    if policy is not None:
        base = [s for s in base if self._visible(s["function"]["name"], policy)]
    # plan_schemas 在过滤之后拼接 → ask_user / present_plan 天然不受白名单影响（F15）
    return base + plan_schemas() if planning else base
```

`_visible(name, policy)`：`name in policy.excluded` → False；`name in policy.exempt` → True；`policy.allowed is None` → True；否则 `name in policy.allowed`。

`policy` 来自 `options.tool_policy`，**每轮调用求值**（R-a）。

**`registered` 也必须在 lambda 体内现取（R-3）**：调用方（`conversation._run()`）写的是 `lambda: self.skill_manager.tool_policy(self._registry.names())`，而**不是**在 `_run()` 开头取一次名字集合再被闭包捕获。若捕获旧快照，F14 明文要求的「每轮按**当前**工具注册中心取交集」就落空——而这条自愈机制存在的唯一理由，正是 C7 的 `mcp_add_server` 会在**运行中**注销旧工具、注册新工具（`mcp/manager.py:88-106`）。失效场景很具体：模型第 N 轮用 `mcp_add_server` 接入一个 Server，某已激活 Skill 的白名单正好写了该 Server 的工具名，第 N+1 轮本该可见，却因旧快照被交集掉。

**(c) `RunOptions`。** 新增参数 `options: RunOptions = RunOptions()`，三处生效：
- `range(1, options.max_iterations + 1)` 与超限文案（N6）；
- `context_manager.before_request(history, allow_summary=options.allow_summary)`（F21）；
- `if options.record_usage and context_manager is not None: record_usage(...)`。

### 4.3 `context/manager.py` — 一个开关

`before_request(self, history, allow_summary: bool = True)`。**`allow_summary` 必须放在 `and` 链最前面短路**（R-i）：

```python
notices = self._offloader.run(history)
if allow_summary and not self._circuit_broken and self._estimate(history) > self.window - self.auto_margin:
    notices.append(self._do_summary(history))
```

原因：`_estimate` 依赖的锚点 `_anchor_tokens/_anchor_len` 对应的是**主历史**，而子对话传入的是另一条短历史，用主历史锚点估算它会得到无意义的值。短路后 `_estimate` 根本不会被调用。

**共用实例的两项已评估结论**（写入代码注释）：`_offloaded` 幂等集合共享无害（键是 `tool_call_id`，全局唯一）；`_consecutive_failures` 熔断计数因子对话不摘要而不会被污染。

### 4.4 `commands/registry.py` — 改造点 2

**内部结构调整（S-d）**：把 `self._specs` 拆为 `self._builtin_specs` 与 `self._skill_specs` 两个列表，`self._specs` 改为返回两者拼接的**只读属性**（builtin 在前、skill 在后——该顺序即 `/help` 与补全候选的稳定顺序，符合 C10 N3）。这比「维护一个混合列表再用 `id()` 反查」直观，也让下面的原子替换更好写。

**连带必改（R-5）**：`register()` 与 `register_many()` 现在分别执行 `self._specs.append(spec)`（第 95 行）与 `self._specs.extend(pending)`（第 113 行）。`_specs` 一旦变成拼接属性，每次访问返回的是**新列表**，`append` 会写进临时对象后被丢弃——**静默不生效**，是最难排查的那类失败。两处的写入目标必须同步改为 `self._builtin_specs`。只读方 `visible_commands()` / `complete()` / `render_help()` 不受影响。

```python
def has_skill_command(self, name: str) -> bool:
    """该 Skill 名是否有一条实际注册成功的短命令（R-b：供 F7 文案判定入口）。"""

def replace_skill_commands(self, specs: Iterable[CommandSpec]) -> list[CommandSpec]:
    """
    原子替换全部 Skill 短命令。
    :returns: 因与内置命令（或其别名）冲突而被跳过的 spec 列表（F25，供上层警告）
    副作用：成功时替换 _skill_specs 与索引；任一步异常则注册表完全不变
    """
```

**算法**：
1. 从零重建 staged 索引：先 stage 全部 `_builtin_specs`（启动时已验证互不冲突，必然成功）。
2. 逐个处理新的 skill spec：**先 stage 进一个独立的临时字典 `probe = dict(staged)`**，成功则 `staged = probe` 并记入 `accepted`；抛 `CommandRegistrationError` 则记入 `skipped` 并**丢弃 probe**，继续下一个。
3. 全部处理完，一次性提交：`self._index = staged`、`self._skill_specs = accepted`。

**第 2 步为什么必须用独立临时字典（R-c）**：现有 `_stage` 是**边遍历 `(name, *aliases)` 边写入**、遇冲突才抛，不做回滚。`register_many` 因为「整批失败」所以无所谓；但这里要求「冲突的跳过、其余继续」，直接复用会在 `staged` 里残留指向一个不在 `_skill_specs` 里的 spec 的索引项，后续 `resolve` 能解析出一条不存在的命令。当前之所以不出事，只是因为 4.5 规定 skill spec `aliases=()`、每个只 stage 一个标识——这是个**隐含不变量**，将来给 Skill 加别名就会静默出错。用独立临时字典从机制上消除该隐患。

第 3 步之前不触碰任何实例字段，因此「中途失败保持原状」自动成立（已知改造点 2 的原子性要求）。

**为何不复用 `register_many`**：后者语义是「任一冲突则整批失败并抛异常」，而 Skill 短命令要「冲突的跳过、其余照常」（F25）。语义不同，各自独立方法更清晰。

### 4.5 `commands/skill_commands.py`（新增）

```python
def build_skill_command_specs(infos: Sequence[SkillCommandInfo]) -> list[CommandSpec]:
    """把中立描述转成 CommandSpec：name=f"/{info.name}"、aliases=()、
       command_type=PROMPT、argument_hint="[参数]"、
       handler=闭包调 controller.run_skill(name, arguments, display)。"""
```

`command_type` 选 `PROMPT` 而非新增枚举值：Skill 短命令语义与 `/init` 同类（把预设内容作为用户请求交给 Agent 路径），沿用既有枚举可避免触发「新增枚举值 → 三处同步」的成对维护点。

**`aliases=()` 是当前的显式不变量**（见 4.4 第 2 步的说明），如将来放开需同步复查 `replace_skill_commands`。

handler 闭包从 `CommandInvocation.raw_text.strip()` 取 `display`，与 `builtins._handle_init` 的既有写法一致。

### 4.6 `commands/builtins.py` — `/skills`

新增 `CommandSpec`：`name="/skills"`、`aliases=()`、`command_type=CommandType.LOCAL`、`usage="/skills [reload|off [name]|run <name> [参数]|prompt]"`、`argument_hint="[子命令]"`。

处理函数按首个空白切分参数得到子命令词（沿用 C10 的 `split(maxsplit=1)` 口径）：

| 子命令 | 调用 |
|---|---|
| 空 | `controller.query_report(ReportTarget.SKILLS)` |
| `prompt` | `controller.query_report(ReportTarget.SKILLS_PROMPT)` |
| `reload` | `controller.reload_skills()` → 显示 → `refresh_status()` |
| `off [name]` | `controller.deactivate_skill(name or None)` → 显示 → `refresh_status()` |
| `run <name> [args]` | 再切一刀取 `<name>`，**其后全部内容原样**作为参数（F26，不做 shell 分词）；缺 `<name>` → 显示用法 |
| 其它 | 显示「未知子命令」+ 用法 |

**`off` 分支的切分口径（S-h）**：`off` 之后 `strip()` 非空即视为 name（其内部空白不再切分——Skill 名按 F4 不含空白，多词输入必然不匹配任何 Skill，走「未找到」分支）。名字命中保留词已在 F4 层拦掉，此处无需再判。

### 4.7 `commands/models.py` — 协议扩展

`ReportTarget` 新增 `SKILLS = "skills"`、`SKILLS_PROMPT = "skills_prompt"`。
`CommandController` 协议新增三个方法：

```python
def run_skill(self, name: str, arguments: str, display: str) -> None: ...
def reload_skills(self) -> str: ...
def deactivate_skill(self, name: Optional[str]) -> str: ...
```

**`display` 参数不可省（B1）**：spec F24 要求「界面与会话回放显示用户原始输入」、AC24 要求「界面显示原始输入，而模型历史含 Skill 名、说明与参数三项」。若签名只有 `name` 与 `arguments`，`Message.display_content` 无从设置，`/resume` 回放时用户会看到「执行 Skill /commit（…）参数：…」这段机器文本而不是自己敲的 `/commit 修复登录超时`。`display` 沿链路透传到 `conversation.run_skill` → `submit_user_message(..., display_content=display)` / `_run_isolated_skill(spec, arguments, display)`。

### 4.8 `tools/load_skill.py`（新增）

```python
class LoadSkillTool(Tool):
    name = "load_skill"
    read_only = True     # 只改会话内存状态，不碰文件系统与外部世界
    parameters = {"type": "object",
                  "properties": {"name": {...}, "arguments": {...}},
                  "required": ["name"]}
    def __init__(self, manager: SkillManager): ...
```

**`read_only=True` 的关键后果**（已核对 `permission/engine.py:106-108`）：权限管线有「只读简化分支」——规则未命中时只读工具直接 ALLOW，不进模式层。因此 `load_skill` 在默认权限模式下**免确认**，不会每次激活都弹面板。这是它能好用的前提，必须保持。

**代价与对策**：`read_only=True` 同时使它进入只读**并发**桶，因此 `SkillManager` 必须加锁（见 3.6 的并发说明）。原文「无并发风险」的判断是错的。

**权限映射：有意不加。** 不在 `permission/adapter.py` 的 `_TOOL_MAP` 中登记，让它落到 `other` 分支——按 CLAUDE.md，`other` 分支仍走规则层与模式层，不会漏过权限检查；而它没有可映射的 Bash/Read/Edit/Write 语义，强行映射反而制造误导。此处显式记录该决定，避免 review 反复追问。

`execute` 依 `manager.activate()` 结果返回：成功 → `ToolResult(ok=True, output="已激活 Skill <name>，其指令已注入上下文。")`；独立模式/不存在 → `ok=False` 加结构化说明（F7）。

### 4.9 `conversation.py` — 编排

**构造**（`__init__`）：

```python
# 参数注入（与 mcp_manager 同）+ Null Object 兜底；真实实例由 __main__ 构造（见 4.11）
def __init__(self, ..., skill_manager: "Optional[SkillManager]" = None):
    self.skill_manager = skill_manager or SkillManager.empty()
    register_read_root(user_dir / "skills")
    register_read_root(builtin_skills_dir())    # N5：只读面扩大，写/搜索面不动
```

**协调层绝不自行扫盘（M3）**。早期版本写的是 `skill_manager or SkillManager(...)`，有三个问题，全部由 `SkillManager.empty()` 解决：

1. **那一行物理上写不出来**——`SkillManager` 的构造需要 `has_short_command=command_registry.has_skill_command`，而 `ConversationManager.__init__` 里没有命令注册表，参数无从提供。
2. **会给既有测试引入隐式 IO**——兜底自建会在构造时执行 `discover()`，扫描项目级、用户级 `~/.rhinecode/skills/` 与内置三个目录并读盘解析。既有大量测试直接构造 `ConversationManager`，等于让它们全部依赖开发机的主目录内容，既拖慢又使结果不可复现，与 N1「无终端无网络可独立验证」相悖。
3. **与自称的既有形态不符**——`mcp_manager` 是 `self._mcp_manager = mcp_manager` 不自建（`conversation.py:134`），各使用点判空降级。

`SkillManager.empty()` 是一个类方法：持一个**空 catalog、不扫盘**，`has_short_command` 取恒 False 的桩。它使各使用点无需散落判空（`index_text()`/`active_text()` 自然返回空串、`tool_policy()` 自然返回不收窄策略、`/skills` 自然报告零 Skill），语义与「没有任何 Skill」完全一致。

**与 `tools_enabled` 的门控关系（S-c）**：本章**不**照搬 C8 `ContextManager` 的「仅工具模式构造」策略。Skill 的加载、清单注入、短命令注册与 `/skills` 报告**全部无条件生效**——它们不依赖工具能力；只有 `load_skill` 工具本身（非工具模式下工具注册中心不暴露给模型）与 `tool_policy` 的收窄（无工具可收窄）自然失效。鉴于本项目后续只针对 DeepSeek 工具模式，这条区分实际不会被触发，此处写明只为让实现者不必再猜。

**`ask` 闭包抽取（R-d）**：把 `_run()` 里就地构造的 `ask` 闭包提为方法 `_build_ask() -> AskFn`，主对话与子对话共用同一实现，避免两份。

**`_run()` 改造——动态段闭包**：

```python
assembled = build_default_prompt(env, custom_instructions=..., memory_index=...,
                                 skill_index=self.skill_manager.index_text(),
                                 active_skills="")   # 槽位由闭包每轮填
base_dynamic = assembled.dynamic                      # 环境信息（本次运行固定）
pending = self.memory_manager.consume_pending_notice()  # 一次性，取走即清

def dynamic_provider() -> str:
    parts = [base_dynamic, self.skill_manager.active_text(), pending]
    return "\n\n".join(p for p in parts if p and p.strip())
```

顺序为 环境信息 → 已激活 Skill → 一次性提醒，满足 F9「排在环境信息之后」。

**`pending` 的位置是刻意的**：在闭包**外**取一次、闭包内复用同一变量，逐字节复现 c9 现行语义——`conversation.py:484-487` 现在就是取一次拼进字符串，`loop.py:213` 每轮重新包一次，所以「同一次运行的每一轮都带这条提醒」本来就是既有行为；「取走即清」指的是「本次运行消费掉、不带到下一条用户消息」，不是「只在第 1 轮出现」。若改成闭包内调用 `consume_pending_notice()`，第 2 轮起会变空，反而破坏现状。

`build_default_prompt` 的 `active_skills` 传空串——激活正文走闭包路径，不经 builder，使 builder 保持纯函数、不引入回调。代价是 120 槽位的顺序由闭包拼接顺序手工保证；动态段只有三段，顺序显式可读，可接受。

**新增领域方法**：

```python
def run_skill(self, name, arguments, display) -> "Iterator[AgentEvent] | str"
def reload_skills(self) -> str
def deactivate_skill(self, name) -> str
def skills_report(self) -> str
def skills_prompt_report(self) -> str
def skill_status_segment(self) -> Optional[str]
```

**两处必须清空激活态**（spec F11 与 N4/AC36 各要一处，缺一不可）：

1. `clear()` 追加 `self.skill_manager.clear_active()`（F11）。
2. `_resume_stream` 在 `memory_manager.resume_into` **成功**、产出 `HISTORY` 事件**之前**调 `self.skill_manager.clear_active()`（N4/AC36）；载入**失败**时不清空——此时仍停留在原会话，激活态应保持。

第 2 条是本轮修订补入的（第 1 轮 plan 只写了 `clear()`，遗漏了 resume 路径，在 task 拆解阶段由「测试在验一件代码里不存在的事」这一线索暴露）。理由：激活态是**进程内存**状态，而 `/resume` 换的是历史、不是进程。不主动清空的话，用户在会话 A 激活的 Skill 会跟着进入会话 B——其 SOP 正文继续每轮注入、白名单继续收窄工具集，而会话 B 的历史里没有任何激活过它的痕迹，用户只会看到「模型莫名其妙按某个 SOP 行事」，且 `/skills` 里列着一个自己在这个会话从未启用过的 Skill。spec N4 明确要求「恢复历史会话后激活列表为空、槽位为空」。

**`run_skill` 两条路径**：

```
共享模式：activate(name, args) → 失败返回提示文本
        → submit_user_message(content=render_invocation_text(spec, args),
                              display_content=display)
独立模式：→ 返回 self._wrap_events(self._run_isolated_skill(spec, args, display))
```

**`_take_tail` — 尾部历史截取（B2 / 决策 15 修正）**

原设计说「复用 C8 `compute_retain_index`」是**错的**，会写出反向 bug。核对 `context/summarize.py:55-99` 后的事实：

- 该函数无 N 参数，边界由模块常量 `RETAIN_TOKENS` / `MIN_RETAIN_MESSAGES` 驱动，无法表达「取最近 N 条」；
- 它找不到 user 边界时 `return 0`——在 C8 语境里 0 = 「保留区从头开始 = 全部保留」是安全的，但在取尾语境里起始下标 0 = **带入整个主历史**。用户写 `history_messages: 3`，遇到没有 user 边界的历史会把几百条主历史全灌进子对话，既违背独立模式的目的，又因决策 14 关掉了子对话的第二层摘要而当场撑爆窗口。

**修正后的设计**：只共享**「user 边界回退」这一个子步骤**，抽成

```python
def snap_back_to_user(history: Sequence[Message], idx: int) -> Optional[int]:
    """从 idx 向前找最近的 role=="user" 下标；找不到返回 None（由调用方决定语义）。
       空序列或 idx 越界一律返回 None——两个调用方的 None 语义都已定义，天然安全（S-b）。"""
```

两个调用方各自定义 `None` 的含义：
- C8 `compute_retain_index`：`None` → 返回 0（全部保留，语义完全不变）；
- `_take_tail`：`None` → **返回不带入任何历史**（等价 `history_messages=0`，最保守）。

并写明：**实际带入条数可能因回退而多于 `history_messages`**——这是 F20「截取边界会回退调整」明确允许的。

**`_run_isolated_skill(spec, arguments, display)` 生成器**（F19–F23）：

1. `invocation = render_invocation_text(spec, arguments)`。
2. 主历史追加 `Message(role="user", content=invocation, display_content=display)` 并 `record_message`。
3. 子历史 = `_take_tail(self.history[:-1], spec.history_messages)` + `[Message("user", invocation)]`。
4. Provider：`spec.model` 为空 → 复用 `self._provider`；否则 `self._provider_for(spec.model)`（`dataclasses.replace(self._config, model=...)` + `create_provider`，按模型名缓存）。
5. 子系统提示：`stable` 与主对话同一份；子动态段闭包返回 `环境信息 + render_active_body(spec, arguments)[0]`（只此一个 Skill，不带主对话其它已激活正文，F21）。
   **Plan Mode 衔接语**：因决策 18 继承 Plan Mode，子循环每轮会把 `plan_toggle_instruction(...)` 与 `dynamic()` 合并进同一条 `<system-reminder>`（`loop.py:210-213`），于是「Skill 的 SOP 让你按第 1、2 步做」与「Plan Mode 让你先别动手、先提交计划」两段指令会在同一上下文里互相拉扯——模型可能直接开干，也可能把 SOP 原样复述成计划。因此 `plan_mode` 为真时，子动态段须追加一句衔接语把两者串成一条流程：「当前处于计划模式：请先依据上述 Skill 指令拟出执行计划并提交审批，获批后再按该指令执行。」这属 render 层措辞，成本极低但对实际效果影响不小。
6. **`sub_agent.run` 的完整参数（R-d）**：

   | 参数 | 取值 | 理由 |
   |---|---|---|
   | `history` | 步骤 3 的子历史 | — |
   | `thinking_effort` | `self.thinking_effort` | 用户设的偏好应一致适用 |
   | `plan_mode` | **继承** `self.plan_mode` | 与用户显式设定的全局姿态保持一致（详见决策 18；注意 Plan Mode 不是安全机制，安全由 C6 五层管线独立保证）。`execution_phase` 是 `run()` 的局部变量（`loop.py:174`），子对话内的批准不会泄漏到主对话 |
   | `stable` / `dynamic` | 步骤 5 | — |
   | `model` / `debug_log_path` | 沿用主对话取值 | — |
   | `engine` | `self._engine` | F21 复用权限引擎 |
   | `ask` | `self._build_ask()` | 抽取后共用，避免两份实现 |
   | `clarify` / `approve_plan` | `self.clarify_callback` / `self.approve_plan_callback` | F21 复用人在回路 |
   | `cancel_event` | **重建一个新的 `threading.Event()` 并赋给 `self._cancel_event`** | `request_cancel()` 置的是 `self._cancel_event`，它只在 `_run()` 里重建；不重建会让上次运行的残留置位使子对话**开局即被取消** |
   | `context_manager` | `self._context_manager` | 需要 C8 第一层 |
   | `recorder` | `None` | 子对话不写会话存档（F21） |
   | `options` | `RunOptions(max_iterations=SKILL_MAX_ITERATIONS, record_usage=False, allow_summary=False, tool_policy=lambda: self.skill_manager.isolated_policy(spec, self._registry.names()))` | N6 / F21。**名字在 lambda 体内现取，与 4.2(b) 同口径（M2）**——早期版本这里写的是裸的自由变量 `registered`，照抄会 `NameError`，或诱导实现者在外层取一次快照而重蹈 R-3。子对话同样可能遇到工具集变化：只要 Skill 白名单含 `mcp_add_server`，子循环内接入新 Server 后下一轮就该看见新工具 |

7. 逐个 `yield` 子循环事件，但**拦下 FINISHED**：记下 `stop_reason`，不向外产出（外层自己收尾）。
8. 从子历史反向找最后一条 `role=="assistant"` 且 `content.strip()` 非空的消息取正文；**找不到，或 `stop_reason != COMPLETED`** → 用「未产出结果」文案（含原因）。

   `stop_reason != COMPLETED` 这个并列条件不可省：计划被拒时子历史的最后一条 assistant 消息可能带非空前言正文，只按「找最后一条非空 assistant」取值会把前言误当结论回流。

   **原因映射表**（决策 18 继承 Plan Mode 后比 spec F19 枚举的四种多出第五种，R-4）：

   | `StopReason` | 回流文案 |
   |---|---|
   | `USER_CANCELLED` | 已取消，本次 Skill 未产出结果 |
   | `MAX_ITERATIONS` | 达到子任务迭代上限，本次 Skill 未产出结果 |
   | `STREAM_ERROR` | 模型请求出错（含上下文超限），本次 Skill 未产出结果 |
   | `UNKNOWN_TOOL` | 连续调用未知工具已停止，本次 Skill 未产出结果 |
   | `PLAN_REJECTED` | 计划未获批准，本次 Skill 未执行 |
9. 主历史追加 `Message(role="assistant", content=结论)` 并 `record_message`。
10. **未产出时补一条可见反馈（R-e）**：若走了「未产出结果」分支，`yield AgentEvent(NOTICE, message=该文案)`。
    **为什么必须补**：步骤 7 拦下了子循环的全部 FINISHED，而 `_do_stream` 对 `FINISHED(COMPLETED)` 的 `_finish_line` 返回空串、**不渲染任何东西**（`tui/app.py:604-624`）。成功路径无碍（结论已由子循环的 `TEXT` 事件流式渲染），但失败路径会变成——用户按 Esc 取消，界面什么都不发生。`NOTICE` 是 `_do_stream` 已支持的系统行通道，是现成落点。
11. `yield AgentEvent(FINISHED, stop_reason=COMPLETED)`。

**子对话触达上下文上限的兜底链路（R-f，兑现 spec F21 的说明义务）**：决策 14 关闭了子对话的第二层摘要，因此超窗时的链路是——请求超限 → API 报错 → `chunk.type=="error"` → 循环产出 `ERROR` 事件（`_do_stream` 会渲染红色错误行）→ `FINISHED(STREAM_ERROR)` → 步骤 8 判定为「未产出结果」→ 步骤 10 的 `NOTICE` 给出可读原因 → 步骤 9 把同样的说明写进主历史。用户可见反馈有两处（红色错误行 + 系统提示行），历史中也留下可追溯记录。

`_run_isolated_skill` 必须经 `_wrap_events` 包装返回，与 `_run()` 一致——步骤 11 的自然完成会触发 C9 记忆钩子针对主历史跑一次（F21 明确要求，不要一并关掉）。

### 4.10 `tui/` — 控制器、状态栏、守卫

`app.py`：

- 三个新控制器方法 `run_skill(name, arguments, display)` / `reload_skills` / `deactivate_skill`，前者走 `_consume_manager_result`（可能是事件流或提示文本），后两者直接返回字符串。
- `query_report` 增 `SKILLS` / `SKILLS_PROMPT` 两个分支。
- `_refresh_status` 增传 `skill_status=self._manager.skill_status_segment()`。
- **模型路径的状态栏刷新（F32 / R-g）**：`load_skill` 在 Worker 线程执行，不能直接刷 UI。**照抄 c9 `_notify_memory` 的范式**——新增绑定方法：

  ```python
  def _notify_skill_activation(self) -> None:
      try:
          self.call_from_thread(self._refresh_status)
      except Exception:
          pass   # 应用正在退出等边缘情况：刷新丢弃即可
  ```

  `on_mount` 时注入 `self._manager.skill_manager.notify_activation = self._notify_skill_activation`。`SkillManager.activate` 调该回调时**再包一层 try/except**（双保险，与 `loop._record` 对 recorder 的处理同构）。
  **为什么不能用裸 lambda**：`activate()` 跑在 `LoadSkillTool.execute()` 里、又在只读并发桶的 `ThreadPoolExecutor` 里，`future.result()` 外层的 `except Exception` 会把回调异常转成 `ToolResult(ok=False, "工具执行异常")`——退出竞态下一次本已成功的激活会被报告成工具失败回灌给模型。
  补充事实：`_do_stream` 的 `finally` 已**无条件** `call_from_thread(self._refresh_status)`（`app.py:597-601`），所以状态栏最终一定会刷新；`notify_activation` 的价值只在「激活当下立刻刷新」。

- **改造点 3 — 提交守卫提示（F31 / R-j）**。现有守卫同时拦三种状态，且**三种的可达性不同**（已核对源码）：

  | 分支 | 可达性 | 处理 |
  |---|---|---|
  | `_pending_interaction` | **可达且最常见**——只有澄清面板禁用 InputBar（`app.py:715`，其 docstring 明写 confirm/approve 不做此限制）；确认面板与计划审批面板期间用户点回输入框敲回车即命中此分支 | 提示「正在等待你的确认，请先在面板上做出选择」 |
  | `_stream_active` | 可达 | 提示「正在运行中，可按 Esc 取消后再执行命令」 |
  | `_session_panel_active` | **不可达**——`_show_session_panel` 已把 InputBar `disabled = True`（`app.py:474`） | 保留裸 return，加注释说明原因 |

  守卫按 `_pending_interaction` 在前的顺序短路，因此**提示若只挂在流式分支上，最常见的场景反而没有提示**——正是 spec F31「该提示对运行期间的所有输入生效」要覆盖的情形。
  **防刷屏（S-e）**：`_busy_hint_shown` 标志，复位规则写死为三条——**进入流式时复位一次**；**每次「新」面板弹出时复位一次**（一次循环里确认面板可能连弹多次，每个副作用工具一次，用户每次敲回车都该得到提示）；**面板关闭时不复位**（否则同一次流式内提示会重复）。

`widgets.py`：`compose_status_text` 增 `skill_status: Optional[str]` 形参，无内容不渲染该段（与 MCP 段同构）。标记形如 `Skill:2`——**不用方括号**，直接绕开 CLAUDE.md 记载的 Textual markup 需转义 `[` 的坑。

### 4.11 `__main__.py` — 启动顺序

```
113  command_registry = build_builtin_registry()          # 既有
120  provider = create_provider(cfg)                       # 既有
128  tool_registry = ToolRegistry.default()                # 既有
134  mcp_manager = MCPManager()                            # 既有
137  tool_registry.register(MCPAddServerTool(...))         # 既有
──── 新增 A：全部内置工具注册完成之后、MCP 连接之前 ────
     skill_manager = SkillManager(..., has_short_command=command_registry.has_skill_command)
     tool_registry.register(LoadSkillTool(skill_manager))
     known = tool_registry.names() | {"ask_user", "present_plan"}
     fatal = skill_manager.startup(known)                  # F16 第一段
     if fatal: 打印 → sys.exit(1)                          # 此刻无子进程、无连接
138  mcp_manager.connect_all(...)                          # 既有
──── 新增 B ────
     skill_manager.bind_tools(registered=tool_registry.names())             # F16 第二段
     skipped = command_registry.replace_skill_commands(
                   build_skill_command_specs(skill_manager.command_infos()))  # F24/F25
     打印 skipped 警告 + skill_manager 全部警告 + project_skill_notice()（N8）
142  manager = ConversationManager(..., skill_manager=skill_manager)
```

**`ToolRegistry` 需新增 `names()`（R-2）**：现有公开 API 只有 `register` / `get` / `unregister` / `schemas` / `readonly_schemas` / `default`，**没有 `__iter__` 也没有名字访问器**，`for t in tool_registry` 会直接 `TypeError`。而 `known`、`registered` 两处都需要它，`tool_policy` 的运行期自愈（F14）更是每轮都要。故新增：

```python
def names(self) -> frozenset[str]:
    """返回当前已注册的全部工具名（供 Skill 白名单校验与运行期自愈取交集）。"""
    return frozenset(self._tools)
```

`tools/registry.py` 因此进入本章的**修改**清单（见第 6 节）。

**新增 A 的位置是 F16 明文要求的窄窗口**：必须在 `MCPAddServerTool` 注册**之后**（它是单下划线 `mcp_add_server`，落在第一段严格校验里，且比其它内置工具晚注册），且在 `connect_all` **之前**（此时无子进程，退出干净）。这是本章最容易放错的一行。

---

## 5. 模块交互

### 5.1 启动

```
__main__ ──startup(known)──▶ SkillManager ──discover──▶ discovery ──parse_skill──▶ parser
                                    └──check_builtin_tool_names──▶ validation ──▶ 致命项 → exit(1)
__main__ ──bind_tools(registered)──▶ SkillManager ──prune_mcp_tool_names──▶ validation
__main__ ──command_infos()──▶ build_skill_command_specs ──▶ registry.replace_skill_commands
```

### 5.2 一轮主对话请求

```
loop.run 第 N 轮
   ├─ dynamic()      ──▶ conversation 闭包 ──active_text()──▶ SkillManager ──▶ render
   ├─ tool_policy()  ──▶ SkillManager.tool_policy(tool_registry.names()) ──▶ ToolPolicy
   │                     （名字在 lambda 体内现取，保证 F14 运行期自愈）
   └─ _schema_for(plan, exec, policy) ──▶ 过滤后的 schemas
模型调 load_skill → LoadSkillTool.execute → SkillManager.activate（持锁）→ notify_activation
   ↓
第 N+1 轮：dynamic() 与 tool_policy() 重新求值 → 正文与白名单同时生效（F9/F14）
```

### 5.3 短命令执行（独立模式）

```
InputBar 提交 "/review 最近改动"
   → dispatcher → skill 短命令 handler → controller.run_skill("review", "最近改动", "/review 最近改动")
   → conversation.run_skill → _wrap_events(_run_isolated_skill(...)) → Worker
        ├─ 主历史 += user(自包含文本, display_content="/review 最近改动")
        ├─ 子 Agent.run(子历史, RunOptions(...)) ──▶ 事件流透传到 UI（进度可见）
        ├─ 主历史 += assistant(结论)
        ├─ 未产出时额外 yield NOTICE（否则界面零反馈）
        └─ yield FINISHED(COMPLETED) → _wrap_events 触发 C9 记忆钩子
```

---

## 6. 文件组织

```
rhinecode/
├── skills/                      ← 新增包
│   ├── __init__.py              — 对外导出 SkillManager 与关键类型
│   ├── models.py                — 枚举、数据类、全部常量
│   ├── parser.py                — parse_skill（纯函数）
│   ├── discovery.py             — discover 三层扫描
│   ├── render.py                — 全部文本产出（纯函数）
│   ├── validation.py            — 两段校验与降级（纯函数）
│   ├── manager.py               — SkillManager 编排（持锁）
│   └── builtin/                 — F28 三个样板（随包分发）
│       ├── commit.md  review.md  test.md
├── tools/policy.py              ← 新增：ToolPolicy（agent 与 skills 的共同下层）
├── tools/load_skill.py          ← 新增：系统级加载工具
├── tools/registry.py            ← 修改：新增 names() 名字访问器
├── tools/__init__.py            ← 修改：加注释固化「不 re-export 子模块」这一无环前提
├── commands/skill_commands.py   ← 新增：SkillCommandInfo → CommandSpec
├── commands/registry.py         ← 改造点 2：_builtin/_skill 拆分 + replace_skill_commands
│                                            + has_skill_command
├── commands/models.py           ← ReportTarget 两个新值 + 协议三个新方法（含 display）
├── commands/builtins.py         ← /skills 一条命令
├── agent/loop.py                ← 改造点 1：dynamic callable、RunOptions、_schema_for
├── agent/prompt/modules.py      ← 新增 priority 140 稳定槽位
├── agent/prompt/builder.py      ← build_default_prompt 两个新参数
├── context/summarize.py         ← 抽出 snap_back_to_user 共享辅助
├── context/manager.py           ← before_request 增 allow_summary（最前短路）
├── conversation.py              ← SkillManager、_build_ask 抽取、六个领域方法、
│                                  _take_tail、_run_isolated_skill
├── tui/app.py                   ← 控制器方法、报告分支、状态栏、_notify_skill_activation、
│                                  改造点 3（三分支提示）
└── tui/widgets.py               ← compose_status_text 增 skill 段

pyproject.toml                   ← package-data 打包 skills/builtin/*.md
.gitignore                       ← 说明中登记项目级 skills/ 可提交
tests/
├── test_skill_parser.py    test_skill_discovery.py   test_skill_render.py
├── test_skill_validation.py test_skill_manager.py    test_skill_commands.py
├── test_skill_isolated.py   test_skill_loop_policy.py test_skill_startup.py
```

**既有测试的精确影响面（S-f，已核实，供 task 阶段直接引用）**：
`Agent.run` 调用点 = `conversation.py:531` + `tests/test_perm_loop.py:59`（第 5 个位置参数 `""` → `lambda: ""`）；
`build_default_prompt` 测试调用点 = `tests/test_c5_prompt.py:38,58`（都用默认参数，新增关键字参数不影响）；
`before_request` 调用点 = `loop.py:199` + `conversation.py:418`。

---

## 7. 技术决策

| # | 决策点 | 选择 | 理由 |
|---|---|---|---|
| 1 | 动态段如何逐轮重算 | `dynamic` 由 `str` 改 `Callable[[], str]` | 循环不认识 `SkillManager`，只认识 `tools` 层一个数据类；既有影响面仅两处一行 |
| 2 | 工具收窄策略如何传入循环 | `ToolPolicy` + **callable**，装进 `RunOptions` | `run()` 已 14 参；打包后默认值即 C10 行为。必须 callable，否则第 N+1 轮不收窄，违反 F14/AC14 |
| 3 | `ToolPolicy` 定义在哪 | `tools/policy.py` | agent 与 skills 的共同下层，零依赖，不成环；避免「循环不认识 skills」与依赖图自相矛盾 |
| 4 | 独立模式的 `model` 覆盖 | `dataclasses.replace(config)` 造临时 Provider，按模型名缓存 | 不动 `BaseProvider.stream_chat`，三个 Provider 一行不改。缓存**有界**（= Skill 声明的不同模型数）、**不关闭**，与主 Provider 今天的处理同口径 |
| 5 | 独立模式子对话放在哪一层 | `conversation.py` 的生成器方法 | 需同时用到 provider/registry/engine/四类回调/memory_manager，全在协调层；与 `_resume_stream` 同构 |
| 6 | 激活列表存不存正文 | 不存，每轮从 catalog 现取 | 热更新后正文自动更新（F27），无需同步两份状态 |
| 7 | Catalog 可变还是快照 | 不可变快照，热更新整体替换 | 替换天然原子 |
| 8 | Skill 短命令的 `CommandType` | 复用 `PROMPT` | 语义与 `/init` 同类；避免触发「新增枚举值 → 三处同步」 |
| 9 | skills 与 commands 的依赖方向 | `commands → skills.models`，skills 不认识 commands | 经中立的 `SkillCommandInfo` |
| 10 | 短命令冲突时是否抛异常 | 不抛，跳过并返回列表 | F25 要求内置优先且不阻断；与 `register_many` 整批失败语义不同 |
| 11 | 正文超总量上限 | **整段丢弃**当前及其后 | 切半会让模型执行残缺流程；整段丢弃是可判定状态。两种降级用 `DegradeKind` 区分，措辞不同 |
| 12 | `load_skill` 的 `read_only` | `True` | 据 `engine.py:106-108` 只读简化分支，这是默认模式下免确认的唯一前提。代价是进并发桶 → SkillManager 必须加锁 |
| 13 | `load_skill` 的权限映射 | **有意不加** `_TOOL_MAP` | 落 `other` 分支仍走规则层与模式层，不漏检；它没有可映射的 Bash/Read/Edit/Write 语义 |
| 14 | 清单槽位 priority | 140（稳定段最后） | 热更新只失效清单自己那段，不波及 130 记忆索引（F6 明文要求） |
| 15 | 激活正文如何进 dynamic | conversation 闭包拼接，不经 builder | builder 保持纯函数；动态段只三段，顺序显式可读 |
| 16 | 子对话的 C8 接入 | 传 `context_manager`，`allow_summary=False` 且**最前短路**、`record_usage=False` | 拿到第一层零成本存盘（F21），又不用主历史锚点去估算子历史、不污染锚点 |
| 17 | 尾部历史截取 | **只共享 user 边界回退子步骤**，`None` 在取尾语境下 = 不带入任何历史 | 直接复用 `compute_retain_index` 会因其「找不到边界返回 0」而带入整个主历史 |
| 18 | 子对话是否继承 Plan Mode | **继承** | 与用户显式设定的全局姿态保持一致——开着 Plan Mode 却让一个 Skill 悄悄绕过，才是会被投诉的行为。**安全性不是理由**：Plan Mode 不是安全边界（同 spec N10 对白名单的定性），即使不继承，子对话里每个副作用工具在默认模式下照样走 C6 的人在回路确认。代价是多一次审批面板，`approve_plan` 回调已复用、实现零额外成本。连带影响两处已覆盖：新增 `PLAN_REJECTED` 终止路径（4.9 步骤 8 原因映射表）、SOP 与 Plan 提醒的提示词冲突（4.9 步骤 5 衔接语） |
| 19 | 子对话的 `cancel_event` | **重建** | `request_cancel()` 置的是 `self._cancel_event`，不重建会让残留置位使子对话开局即被取消 |
| 20 | `reload` 遇内置工具名笔误 | 丢弃该 Skill + 警告，不退出 | F16/N2 的定语锚定「启动」；运行中会话不该被热更新杀掉。警告须写明「下次启动会失败」 |
| 21 | 状态栏标记文本 | `Skill:2`，不含方括号 | 绕开 Textual markup 需转义 `[` 的坑 |
| 22 | 提交守卫提示 | 三分支分别措辞，`_pending_interaction` 也要提示 | 确认/审批面板不禁用输入框，该分支可达且最常见 |

---

## 8. 对 spec 的覆盖自检

| spec 条款 | 归属 |
|---|---|
| F1 F4 F5 F22(警告) F29 | `parser.py` + `discovery.py` |
| F2 F3 F13(清单采集) | `discovery.py` |
| F6 F9(截断) F10 F12 F13(渲染) F24(文本) | `render.py` |
| F16 F17 | `validation.py` + `__main__.py` 4.11 |
| F7 F8 | `tools/load_skill.py` + `SkillManager.activate`（入口判定见 R-b 修正） |
| F9(时机) | `agent/loop.py` 改造点 1 + `conversation._run` 闭包 |
| F11 F27 F30 | `SkillManager`（持锁） |
| F14 F15 F21(工具集) F23 | `SkillManager.tool_policy/isolated_policy` + `loop._schema_for` |
| F18 F19 F20 F21 F22(生效) | `conversation._run_isolated_skill` + `_take_tail` + `RunOptions` |
| F24 F25 | `commands/skill_commands.py` + `registry.replace_skill_commands` + 协议 `display` 参数 |
| F26 | `commands/builtins.py` + `SkillManager.report/prompt_report` |
| F28 | `skills/builtin/*.md` + `pyproject.toml` package-data |
| F31 | `tui/app.py` 改造点 3（三分支） |
| F32 | 数据：`SkillManager.status_segment`；渲染：`widgets.compose_status_text` + `app._refresh_status`；模型路径刷新：`_notify_skill_activation` |
| N1 N2 | 包结构（1.1 依赖裁定）与 fail-safe 约定 |
| N3 | `RunOptions` 默认值 + 空槽位跳过 |
| N4 | 激活态只存内存、不进 `record_message`；`clear()` 与 `_resume_stream`（成功分支）各清空一次（见 4.9） |
| N5 | `register_read_root` 两次调用 |
| N6 | `RunOptions.max_iterations = SKILL_MAX_ITERATIONS` |
| N7 | 新增的 `skills/` 六个模块与三个改造点，模块级 docstring 须说明「职责 + 依赖方向 + 在 spec 中的对应条款」，与 `permission/` `context/` `memory/` `commands/` 的既有风格一致；task.md 的每个任务另把「补齐函数级注释」列入完成条件 |
| N8 | `SkillManager.project_skill_notice` + `__main__` 4.11 新增 B 打印 |
| N9 N10 | 无代码——由「不给任何豁免路径」保证；AC37 反证 |
