# C14 子 Agent 工作区隔离 Plan

> 本文对应 [`spec.md`](spec.md) 的 25 条 F 需求与 8 条 N 需求。
> 术语沿用 spec：**主项目根**指进程启动时的当前工作目录；**隔离工作区**指
> `<主项目根>/.rhinecode/worktrees/<名字>` 下的一个 Git 工作目录。

## 架构概览

本章的形状是**一根新管道 + 一个新叶子包**：

```
                    ┌─────────────────────────────────────────┐
                    │  新叶子包 rhinecode/worktree/           │
                    │  建/查/删隔离工作区，唯一执行 git 的地方 │
                    └──────────────┬──────────────────────────┘
                                   │ 被调用
      ┌────────────────────────────┼───────────────────┐
      │                            │                   │
 bootstrap.py                subagents/runner.py   subagents/gate.py
 （启动清理 F19）            （创建/结束 F6-F16）  （交付信息 F17）
                                   │
                                   │ 产出 cwd
                                   ▼
                            RunOptions.cwd
                                   │
                    ┌──────────────┴──────────────┐
                    │        agent/loop.py        │
                    │  本章唯一的 cwd 分发中心    │
                    └──┬────────────┬──────────┬──┘
                       │            │          │
        tool.execute(..., cwd=)   to_request(..., cwd=)   hooks.dispatch(..., cwd=)
                       │            │          │
                       ▼            ▼          ▼
                  6 个路径/命令   permission   hooks/actions
                     工具         engine ②层   命令的 cwd
                       │            │
                       └─────┬──────┘
                             ▼
                   tools/path_guard.py
              （所有路径判定函数改为 root 必传）
```

**三条设计主线：**

1. **工作目录从隐式变显式。** `path_guard` 目前把「主项目根」写死成 `Path.cwd()`，
   权限引擎的第②层直接依赖它。本章把这些函数改成**必须显式传入根目录**，
   并由 Agent Loop 沿两条独立路径（工具执行 / 权限判定）把它分发下去。
2. **worktree 的全部 Git 交互收拢在一个新叶子包里。** 包外任何地方不出现 git 子进程调用。
3. **隔离与非隔离走同一条代码路径。** 非隔离子 Agent 与主对话传入的 `cwd` 就是主项目根，
   没有 `if isolated:` 分支——这是 N3（不改变未启用隔离时的行为）成立的结构性保证，
   而不是靠测试逐条比对。

## 核心数据结构

### `WorkspaceRoot`（概念，实际类型为 `pathlib.Path`）

本章刻意**不为工作目录引入新类型**。它在管道里的全部语义就是「一个绝对路径」，
包一层类只会让 `path_guard` 反向依赖某个新模块，而 `path_guard` 目前是被
`permission` / `hooks` / `mcp` / `bootstrap` 共同依赖的准叶子。

### `rhinecode/worktree/models.py`

```python
@dataclass(frozen=True)
class WorktreeHandle:
    """一个已就绪的隔离工作区。创建成功与快速恢复都产出它。"""
    name: str          # 归一化后的名字（可含 '/'）
    path: Path         # 绝对路径，<主项目根>/.rhinecode/worktrees/<name>
    branch: str        # 实际使用的分支名（冲突改名后的最终值）
    base_commit: str   # 创建时的基点提交短哈希
    recovered: bool    # True 表示走了快速恢复（F9），未调用创建命令

@dataclass(frozen=True)
class ChangeStatus:
    """一个隔离工作区的变更状态。结束决策（F16）与三层过滤③（F20）共用。"""
    dirty: bool                  # 有未提交的工作区改动（含未跟踪文件）
    commits: int                 # 相对基点的新增提交数
    files: tuple[str, ...]       # 变更文件清单（未提交 + 已提交合并去重）

    @property
    def untouched(self) -> bool:
        return not self.dirty and self.commits == 0

@dataclass(frozen=True)
class ProvisionEntry:
    """一条环境初始化条目（F10）。"""
    source: str                  # 相对主项目根的来源路径
    mode: str                    # "copy" | "link"

@dataclass(frozen=True)
class ProvisionResult:
    applied: tuple[str, ...]
    warnings: tuple[str, ...]    # 跳过的条目及原因，不阻断创建

@dataclass(frozen=True)
class RemovalVerdict:
    """三层过滤的结论（F20/F21）。唯一的删除许可来源。"""
    allowed: bool
    keep_branch: bool            # 允许删除时，是否保留分支（含新增提交则保留）
    reason: str                  # 中文原因，拒绝时用于报告

@dataclass(frozen=True)
class CleanupReport:
    """启动清理的产物（F19/F21）。"""
    removed: tuple[tuple[str, str], ...]   # (名字, 保留的分支名或空串)
    kept: tuple[tuple[str, str], ...]      # (名字, 未删除的原因)
    scanned: int
```

### `AgentSpec` 新增字段（`subagents/models.py`）

```python
isolation: Optional[str] = None    # None = 未声明；"worktree" = 要求隔离
```

同时把 `UNSUPPORTED_FIELDS` 中的 `"isolation"` 一项**删除**——这是 CLAUDE.md
已登记的成对维护点，漏删的表现是「功能做了但用户被告知不支持」。

### `TaskRecord` 新增字段（`subagents/tasks.py`）

```python
worktree_path: str = ""     # 隔离工作区路径，非隔离任务为空
worktree_branch: str = ""   # 分支名，非隔离任务为空
```

供 `/agents` 的任务列表展示（F23）。**不放 `WorktreeHandle` 对象**：`TaskRecord`
在加锁临界区里被读写，而临界区的既有不变量是「只做纯内存读写」，放一个可能诱使
后来者调 git 的对象进去是在给自己挖坑。

### `PermissionRequest` 新增字段（`permission/models.py`）

```python
cwd: Path       # 本次调用的工作目录。**必填、无默认值**
```

**刻意不给默认值。** 给了默认值就意味着「忘记传的调用点会静默按主项目根判定」——
而那正是 spec N2 要禁止的形态：一次隔离故障会静默变成一次越权。无默认值让每个
构造点在开发期就以 `TypeError` 暴露出来。代价是既有的构造点（含测试）全部要改，
这个代价是**故意付的**。

### `RunOptions` 新增字段（`agent/loop.py`）

```python
cwd: Optional[Path] = None    # None → 主项目根
```

缺省 `None` 而不是缺省主项目根，是为了让 `RunOptions()` 保持无副作用可构造
（它在测试里被大量直接实例化）。解析发生在 `Agent.__init__`：`cwd or main_project_root()`。

### `Tool` 新增类属性（`tools/base.py`）

```python
workspace_aware: bool = False
```

语义与既有的 `plan_safe` 完全同构：**声明它的工具，其 `execute` 必须接受一个
`cwd` 关键字参数**（签名写成 `execute(self, args, cwd)`），循环会传。
未声明的工具签名不动。

## 模块设计

### 新增包 `rhinecode/worktree/`

**职责：** 隔离工作区的建、查、删，以及唯一的 Git 子进程调用点。
**依赖：** 标准库 + `tools.path_guard`（复用路径安全校验）。**不依赖**
`subagents` / `agent` / `conversation` / `tui` / `permission`。

> 依赖方向的确认：`worktree` 只被 `subagents`、`bootstrap`、`conversation` 使用，
> 它自己只向下依赖 `tools.path_guard`。`tools` 侧不 import `worktree`（工具拿到的
> 是一个 `Path` 参数，不是对象），因此**不构成 `tools ↔ worktree` 的包级互依**，
> `tools/__init__.py` 保持为空的既有不变量不受影响。

#### `worktree/naming.py`

**职责：** 名字的安全校验与生成（F7）。**纯函数、零 IO**，与 `skills/audit.py` 同风格。

```python
def validate_name(raw: str) -> str
    """校验并归一化名字。不通过时抛 WorktreeNameError（含中文原因）。

    规则：非空；总长 ≤ 64；以 '/' 分段，段数 ≥ 1；每段非空、
    仅含 [A-Za-z0-9._-]、且不等于 '.' 或 '..'；不以 '/' 开头或结尾。
    反斜杠一律拒绝（Windows 上它是路径分隔符，放行等于放开另一条遍历通道）。
    """

def generate_name(agent_name: str, task_id: str) -> str
    """委派方未给名字时生成一个必然通过校验的名字。"""
```

⚠ **校验必须先于任何路径拼接**。先拼后校验的写法在拼接那一步就已经产生了越界路径，
后续任何一处忘记检查返回值都会直接落到目标目录上。

#### `worktree/gitcmd.py`

**职责：** 本章**唯一**执行 git 子进程的地方。所有函数返回结构化结果，不抛裸异常。

```python
class GitUnavailable(Exception)      # git 不可执行
class NotARepository(Exception)      # 当前目录不是 git 仓库
class GitCommandFailed(Exception)    # 命令返回非零，携带 stderr

def ensure_repository(root: Path) -> None
def head_commit(root: Path) -> str
def add_worktree(root: Path, path: Path, branch: str, base: str) -> None
def list_worktrees(root: Path) -> tuple[Path, ...]     # 解析 --porcelain 输出
def branch_exists(root: Path, branch: str) -> bool
def remove_worktree(root: Path, path: Path) -> None
def prune_worktrees(root: Path) -> None
def delete_branch(root: Path, branch: str) -> None
def status(path: Path) -> tuple[bool, tuple[str, ...]]   # (dirty, 未提交变更文件)
def commits_since(path: Path, base: str) -> tuple[int, tuple[str, ...]]
def hooks_path(root: Path) -> Path                       # 仅供护栏断言 F11 用
```

**为什么用子进程而不是某个 Git 库：** 本项目依赖清单里没有 Git 绑定库，
为本章引入一个是不成比例的；而 `--porcelain` 系列输出正是为机器解析设计的、
跨版本稳定。

**这些 git 调用不经权限管线**，与 C8 的工具结果存盘、C9 的笔记落盘同一先例：
它们不是模型发起的工具调用，而是系统为了兑现用户配置而执行的内部操作。
安全性由三点保证：命令与参数全部由代码构造（模型只能影响「名字」这一个值，
且它已过 `validate_name`）、不经 shell（`shell=False`，参数以列表传递）、
删除类操作一律先过三层过滤。

#### `worktree/provision.py`

**职责：** 环境初始化（F10）。

```python
def provision(main_root: Path, target: Path,
              entries: Sequence[ProvisionEntry]) -> ProvisionResult
```

**执行流程：** 逐条处理，单条失败只记警告不中断（spec F10）。每条先做三项校验：
① 来源路径经 `path_guard` 解析后必须仍在主项目根内；② 来源必须存在；
③ 目标路径（`target / entry.source`）解析后必须仍在 `target` 内。任一不满足则跳过并记警告。

`copy` 用文件/目录复制，`link` 优先建符号链接、失败（Windows 无权限是常见情形）
时降级为目录联接或复制并记警告——**降级要记警告**，否则「我配了 link 结果它复制了
500MB」这件事无处可查。

#### `worktree/lifecycle.py`

**职责：** 创建、检查、结束决策、删除。本章的核心。

```python
def create(main_root: Path, name: Optional[str], agent_name: str,
           task_id: str, entries: Sequence[ProvisionEntry]
           ) -> tuple[WorktreeHandle, ProvisionResult]
    """F6-F10。失败时抛 WorktreeError 的子类，由调用方转成委派失败（F12）。

    流程：
      1. 名字校验或生成（naming）
      2. 算出目标路径并做位置校验（必须落在 <main_root>/.rhinecode/worktrees/ 内）
      3. 快速恢复判定（F9）：**全程不调 git**。目标目录存在，且其下的 `.git`
         是一个文件（不是目录）、内容形如 `gitdir: <主项目根>/.git/worktrees/<名字>`
         且该指向落在主项目根的版本库内 → 直接返回 recovered=True。
         归属确认靠读那个指针文件本身，见下方说明。
      4. ensure_repository / head_commit
      5. 分支名 agent/<name>，冲突时追加 -2、-3 …… 直到不冲突
      6. add_worktree
      7. provision
    """

def inspect(handle: WorktreeHandle) -> ChangeStatus
    """查询变更状态。结束决策与三层过滤共用同一份实现，避免两处口径漂移。"""

def judge_removal(main_root: Path, path: Path, status: ChangeStatus
                  ) -> RemovalVerdict
    """三层过滤（F20/F21）。**纯判定、零副作用**，可被单测穷举。"""

def remove(main_root: Path, handle_or_path, status: ChangeStatus) -> RemovalVerdict
    """**本包唯一的删除入口**（F20/F22）。

    先调 judge_removal，未获许可直接返回、不碰文件系统。获许可后：
      1. remove_worktree
      2. prune_worktrees（处理「已注销但目录还在」的残留，F22）
      3. 目录若仍存在，走独立的强制删除兜底
      4. keep_branch=False 时 delete_branch
    """
```

**快速恢复为什么能不调 git（F9）：** 实测确认隔离工作区里的 `.git` 是一个 51 字节的
文本文件，内容为 `gitdir: <主仓库>/.git/worktrees/<名字>`。「这个目录是不是本仓库的
工作目录」这件事**物理上就写在那个文件里**，读它即可确认归属，无需启动 git 子进程。
这条快路径的收益在批量场景下才明显（一次 git 子进程在 Windows 上是几十毫秒量级），
但它同时消除了「恢复路径也可能因 git 不可用而失败」这个失败模式。

⚠ 这里**刻意不复用 `judge_removal` 的第②层**（那一层用 `list_worktrees`，是真的调 git）：
两处对「归属」的确认强度不同是有意的——恢复是读操作、判错的代价是多建一个目录；
删除是写操作、判错的代价是删掉不该删的东西，必须问 git 要权威答案。

⚠ **`remove` 是唯一删除入口**，与 `tui/app.py` 的 `_settle_session` 同一先例：
将来任何第二条删除路径都必须走它，否则三层过滤形同虚设。CLAUDE.md 里那条
「曾因空变量 rmtree 删掉整个仓库」的教训在这里由 `judge_removal` 的第①层兜住。

#### `worktree/cleanup.py`

**职责：** 启动时扫描一次（F19/F21）。

```python
def scan_and_clean(main_root: Path, max_age_days: int) -> CleanupReport
```

**执行流程：** 目录不存在 → 返回空报告；逐个子目录判定「过期」（取目录内文件
最近修改时间，遍历时跳过 `.git`）；未过期跳过；过期项走 `lifecycle.inspect`
+ `lifecycle.remove`；结果汇总成 `CleanupReport`。**整个函数对任何异常
fail-safe**——清理失败绝不能阻断启动。

#### `worktree/render.py`

**职责：** 交付信息段的文本渲染（F17）。纯函数。

```python
def render_delivery(handle: WorktreeHandle, status: ChangeStatus) -> str
```

放在本包而不是 `subagents/gate.py`，是为了保住那条既有的成对维护点
「子 Agent 结论的渲染只有一份」——`gate.py` 仍是结论渲染的唯一出口，
它只是把本函数的产出拼进去。

### 改造 `tools/path_guard.py`

**这是本章风险最高的一处改动。** 现状是所有判定函数隐式取 `Path.cwd()`。

**改法：把「主项目根」与「本次判定的根」在命名上彻底分开。**

```python
def main_project_root() -> Path                     # 原 workspace_root，重命名
    """进程启动时的当前工作目录。只用于定位配置文件、存档目录等
    **与调用者无关**的位置，不用于路径边界判定。"""

def resolve_in_workspace(path: str, root: Path) -> Path       # root 必传
def resolve_readable(path: str, root: Path) -> Path           # root 必传
def is_within_workspace(path: str, root: Path) -> bool        # root 必传
def is_readable_path(path: str, root: Path) -> bool           # root 必传
def validate_glob_pattern(pattern: str) -> None               # 不变（只查模式本身）
```

**为什么要重命名而不是加个可选参数：**

- 可选参数意味着漏传不报错、静默按主项目根判定——正是 N2 禁止的形态。
- 重命名会让全部 15 个既有调用点在开发期就断掉，**强制逐个复核**「你这里要的
  到底是主项目根，还是调用者的工作目录」。这 15 处里有 12 处（配置文件、
  存档目录、hooks.yaml 定位）确实要主项目根，3 处（引擎②层、glob/grep 的 base）
  要调用者的工作目录——不复核就分不出来。

`_EXTRA_READ_ROOTS` 只读白名单**保持为模块级全局**（F5）：它的语义是
「不论在哪个工作目录下都可读」，与 root 正交。`resolve_readable` 的白名单分支不动。

### 改造 `permission/engine.py`

第②层的两处判定改为：

```python
if not is_readable_path(request.specifier, request.cwd):
elif not is_within_workspace(request.specifier, request.cwd):
```

**管线的层序一字不动。** ②仍在①之后、②′之前、③之前。本章不新增层、不改层序，
CLAUDE.md 里那条「③必须排在①②之后」及②′的顺序论证原样成立。

### 改造 `permission/adapter.py`

```python
def to_request(tool, args, mode, cwd: Path) -> PermissionRequest    # cwd 必填
```

### 改造 `agent/loop.py`——本章唯一的 cwd 分发中心

`Agent` 持有 `self._cwd`（由 `RunOptions.cwd` 解析而来）。四个分发点：

| 分发点 | 位置 | 说明 |
| --- | --- | --- |
| 并发只读桶 | `_run` 内的 `tool.execute` | **不可遗漏**——`read_file`/`glob_files`/`grep_content` 都是只读工具，走的是这条路 |
| 串行桶 | `_run_one_serial` 内的 `tool.execute` | 已有 `plan_stage` 的传递先例，照抄 |
| 权限判定 | 调 `to_request` 处 | |
| 工具级 Hook | 调 hook 分发处 | F25 |

⚠ **`plan_stage` 只在串行路径传递，`cwd` 必须两条路都传。** 二者不同构：
`plan_stage` 只对非只读工具有意义，而 `cwd` 对只读工具同样有意义——
漏掉并发路径的后果是隔离子 Agent 的**读**全部落到主项目根上，
而它的写是对的，界面上完全看不出来。

传递方式照 `plan_safe` 先例：

```python
if tool.workspace_aware:
    res = tool.execute(tc.arguments, cwd=self._cwd)
else:
    res = tool.execute(tc.arguments)
```

### 改造六个工具

| 工具 | 改动 |
| --- | --- |
| `read_file` | `resolve_readable(path, cwd)` |
| `write_file` | `resolve_in_workspace(path, cwd)` |
| `edit_file` | `resolve_in_workspace(path, cwd)` |
| `glob_files` | `base = cwd`；解析用 `cwd`；**新增跳过隔离工作区目录**（F18） |
| `grep_content` | `base = cwd`；解析用 `cwd`；**新增跳过隔离工作区目录**（F18） |
| `run_command` | `cwd=cwd` 传给子进程 |

六者一律声明 `workspace_aware = True`。

**F18 的实现：** 在两个搜索工具的目录遍历中，跳过解析后等于
`<cwd>/.rhinecode/worktrees` 的目录。写成「等于」而不是「名字叫 worktrees」，
是为了不误伤用户自己叫这个名字的业务目录。隔离工作区内部不存在该路径
（它随忽略规则被排除在 checkout 之外），因此这条规则对子 Agent 天然是空操作。

### 改造 `hooks/actions.py` 与分发链（F25）

`shell` 动作当前写死 `cwd=workspace_root()`。改为由分发方传入：
`HookManager.dispatch(...)` 增加 `cwd` 参数，工具级三事件从 Agent Loop 的
`self._cwd` 取值，其余事件（会话级、回合级、消息级、系统级）一律传主项目根。

### 改造 `subagents/`

| 文件 | 改动 |
| --- | --- |
| `models.py` | `AgentSpec.isolation` 字段；`UNSUPPORTED_FIELDS` 删 `isolation` |
| `parser.py` | 读取并归一 `isolation`（认 `worktree`；其它值警告并按未声明处理） |
| `report.py` | `/agents` 展示隔离声明 |
| `tasks.py` | `TaskRecord` 增两个字段 |
| `service.py` | `delegate` 增 `isolation` 参数；**单向加严**的合并逻辑；创建失败转 `DelegateOutcome` 失败（F12） |
| `runner.py` | 创建/结束隔离工作区；`RunOptions.cwd`；隔离说明注入系统提示（F15） |
| `gate.py` | 把 `render_delivery` 的产出拼进结论（F17） |

**单向加严的判定（F14）** 写成一个纯函数放在 `service.py`：

```python
def resolve_isolation(spec_declared: Optional[str], call_requested: Optional[bool]) -> bool:
    """角色声明了就恒为 True（调用方关不掉）；未声明时取调用方意愿。"""
    if spec_declared == "worktree":
        return True
    return bool(call_requested)
```

**运行器的生命周期接线：**

```
run_subagent
  ├─ 需要隔离？
  │    ├─ 是 → lifecycle.create(...)         失败 → 返回委派失败（F12）
  │    │        └─ cwd = handle.path
  │    └─ 否 → cwd = 主项目根
  ├─ _build_prompts：隔离时追加一段隔离说明（F15）
  ├─ Agent.run(options=RunOptions(cwd=cwd, ...))
  └─ finally:
       └─ 隔离时：status = inspect(handle)
                  status.untouched → remove(...)（F16）
                  否则保留，并把 handle+status 交给 gate 渲染（F17）
```

⚠ **结束处理必须在 `finally` 里**，且**本身不得抛出**——它跑在子 Agent 的后台
线程上，抛出会让任务无声消失（既有的 `TaskManager` 加锁不变量要求
`Event.set()` 在锁外，结束处理同理必须排在它之前完成或被完全兜住）。

⚠ **`inspect` 与 `remove` 是文件系统与子进程操作，一律在 `TaskManager` 的锁外。**
这是 C13 已有的硬不变量（临界区只做纯内存读写），本章不得违反。

### 改造 `tools/run_agent.py`

新增可选参数 `isolation`（布尔或字符串 `"worktree"`），并在 `description` 中说明
何时该要求隔离。

⚠ **成对维护点**：`subagents/render.py` 的 `_INDEX_HEADER` 与本工具的 `description`
必须同口径。本章要在两处同时加上「涉及写文件且主对话手上有未提交改动时应要求隔离」
这一层意思，一处强一处弱等于白改。

### 改造 `bootstrap.py`（F19）

在装配序列中插入一次 `worktree.cleanup.scan_and_clean`，产出的 `CleanupReport`
随其它启动提示一并呈现。

**位置约束：** 必须在**任何子 Agent 可能启动之前**（否则可能删掉正在建的目录），
且在配置加载**之后**（要读 `max_age_days`）。放在角色目录扫描（`discover_agents`）
附近。装配顺序的既有约束（`exclude_tools` 摘除必须在 `session_start` 快照之前）
不受影响——清理与工具集无关。

### 改造 `config.py`

新增配置段：

```yaml
worktree:
  cleanup_days: 7          # F19，过期阈值
  copy: []                 # F10，复制清单
  link: []                 # F10，软链清单
```

三项均可缺省。模板生成一并更新。

### 改造 `trace/`（F24）

新增事件类型：`worktree_create`、`worktree_provision`、`worktree_settle`、
`worktree_cleanup`。

⚠ **成对维护点**：`trace/models.py` 的枚举 + `trace/reader.py` 的 `SUMMARIZERS`
表必须同时改，漏了后者不报错，只会让新事件在阅读器里显示成「（未登记类型）」。

### 改造 `.gitignore`

新增 `**/.rhinecode/worktrees/`，与既有的 `context/` `sessions/` `memory/`
`traces/` 同口径（N8）。

## 模块交互

### 一次隔离委派的完整调用链

```
模型调用 run_agent(agent="reviewer", isolation=true)
   │
   ▼
SubAgentService.delegate
   │  resolve_isolation(spec.isolation, call.isolation) → True
   ▼
worktree.lifecycle.create(main_root, name, ...)
   │  naming.validate_name        → 名字合法
   │  位置校验                     → 落在 .rhinecode/worktrees/ 内
   │  快速恢复判定                 → 未命中
   │  gitcmd.ensure_repository / head_commit / add_worktree
   │  provision.provision          → 按清单复制/软链
   ▼  WorktreeHandle(path=..., branch="agent/reviewer", base_commit="b225368")
subagents.runner.run_subagent
   │  _build_prompts → 追加隔离说明（路径、分支、成果要提交）
   ▼
Agent.run(options=RunOptions(cwd=handle.path, interactive=False, ...))
   │
   │  每次工具调用：
   │    to_request(tool, args, mode, cwd=handle.path)
   │       → engine.decide → ②层用 handle.path 做根 → 工作区外一律拒绝
   │    tool.execute(args, cwd=handle.path)
   │       → read/write/edit 解析到 handle.path 内
   │       → run_command 的子进程 cwd = handle.path
   │    hooks.dispatch(..., cwd=handle.path)
   ▼
finally:
   │  status = lifecycle.inspect(handle)
   │  untouched → lifecycle.remove(...)  ／ 否则保留
   ▼
subagents.gate.render_subagent_message
   │  模型结论全文 + worktree.render.render_delivery(handle, status)
   ▼
注入主对话历史
```

### 启动清理的调用链

```
bootstrap.build_app
   ▼
worktree.cleanup.scan_and_clean(main_root, cleanup_days)
   │  遍历 .rhinecode/worktrees/ 的子目录
   │  过期判定（目录内文件最近修改时间）
   │  lifecycle.inspect → ChangeStatus
   │  lifecycle.remove  → judge_removal 三层过滤
   │       ①位置 → ②归属（list_worktrees）→ ③变更（dirty 即否决）
   │       通过：commits>0 → 删目录留分支 ／ commits==0 → 删目录删分支
   ▼  CleanupReport → 启动提示
```

## 文件组织

```
rhinecode/
├── worktree/                  【新增包】
│   ├── __init__.py            — 门面：re-export create/inspect/remove/scan_and_clean/render_delivery
│   ├── models.py              — WorktreeHandle、ChangeStatus、ProvisionEntry/Result、
│   │                            RemovalVerdict、CleanupReport、异常类型
│   ├── naming.py              — validate_name、generate_name（纯函数零 IO）
│   ├── gitcmd.py              — 唯一的 git 子进程调用点
│   ├── provision.py           — 环境初始化（copy / link）
│   ├── lifecycle.py           — create / inspect / judge_removal / remove
│   ├── cleanup.py             — scan_and_clean
│   └── render.py              — render_delivery
├── tools/
│   ├── path_guard.py          【改】main_project_root 重命名；四个判定函数 root 必传
│   ├── base.py                【改】workspace_aware 类属性
│   ├── read_file.py           【改】cwd
│   ├── write_file.py          【改】cwd
│   ├── edit_file.py           【改】cwd
│   ├── glob_files.py          【改】cwd + F18 跳过
│   ├── grep_content.py        【改】cwd + F18 跳过
│   ├── run_command.py         【改】cwd
│   └── run_agent.py           【改】isolation 参数 + description
├── permission/
│   ├── models.py              【改】PermissionRequest.cwd（必填）
│   ├── adapter.py             【改】to_request 增 cwd
│   └── engine.py              【改】②层两处判定传 request.cwd
├── agent/
│   └── loop.py                【改】RunOptions.cwd；四个分发点
├── hooks/
│   ├── actions.py             【改】shell 动作的 cwd 参数化
│   └── manager.py             【改】dispatch 增 cwd
├── subagents/
│   ├── models.py              【改】isolation 字段；UNSUPPORTED_FIELDS 删一项
│   ├── parser.py              【改】读取 isolation
│   ├── report.py              【改】展示隔离声明
│   ├── tasks.py               【改】TaskRecord 两个字段
│   ├── service.py             【改】resolve_isolation；delegate 接线
│   ├── runner.py              【改】生命周期接线；RunOptions.cwd；F15 提示
│   ├── gate.py                【改】拼接交付信息段
│   └── render.py              【改】_INDEX_HEADER 同口径
├── trace/
│   ├── models.py              【改】四个新事件类型
│   └── reader.py              【改】SUMMARIZERS 四个新条目
├── config.py                  【改】worktree 配置段 + 模板
└── bootstrap.py               【改】插入启动清理

tests/
├── test_worktree_naming.py    【新】名字校验穷举（含遍历路径遍历构造）
├── test_worktree_gitcmd.py    【新】真实小仓库上的 git 封装
├── test_worktree_lifecycle.py 【新】创建/快速恢复/结束决策/三层过滤（含反证）
├── test_worktree_provision.py 【新】copy/link/越界跳过/单条失败不中断
├── test_worktree_cleanup.py   【新】过期判定/三层过滤/残留兜底
├── test_path_guard_root.py    【新】root 参数化后的边界（含 N2 的拒绝反证）
├── test_perm_cwd_sandbox.py   【新】②层按 cwd 判定（含隔离越界反证）
├── test_loop_cwd_dispatch.py  【新】四个分发点齐全（含「并发路径漏传」的反证）
├── test_subagent_isolation.py 【新】单向加严/创建失败/结束保留删除/交付信息
└── （既有测试按新签名同步）

docs/c14/                      spec.md / plan.md / task.md / checklist.md
.gitignore                     【改】worktrees 目录
```

## 技术决策

| # | 决策点 | 选择 | 理由 |
| --- | --- | --- | --- |
| D1 | 工作目录怎么到达工具 | 显式 `cwd` 关键字参数，只传给声明 `workspace_aware` 的工具 | 线程局部变量（`threading.local` / `contextvars`）能做到零签名改动，但它是**隐式**的：并发只读桶用的是 `ThreadPoolExecutor`，线程局部不跨线程继承，必须像 trace 作用域那样显式绑定——绕一圈仍是显式传递，却多了一层看不见的状态。用户的需求也明确要求「显式传到每个工具调用里」 |
| D2 | 漏传怎么办 | `path_guard` 的判定函数 **root 必传、无默认值**；`PermissionRequest.cwd` **无默认值** | 这是本章防「漏改不报错」的核心手段。有默认值 = 忘记传的地方静默按主项目根判定 = 隔离静默失效。无默认值让遗漏在开发期变成 `TypeError` |
| D3 | 是否重命名 `workspace_root` | 重命名为 `main_project_root` | 15 个既有调用点里，12 处要主项目根、3 处要调用者工作目录。不重命名就无法强制逐个复核，而这两者一旦混淆就是隔离失效 |
| D4 | 隔离与非隔离是否分支 | **不分支**，非隔离传主项目根 | N3（不改变未启用隔离时的行为）由结构保证，而不是靠测试逐条比对。也避免了「两条路径慢慢漂移」 |
| D5 | git 交互方式 | 子进程 + `--porcelain`，收拢在 `gitcmd.py` | 不为本章引入 Git 绑定库依赖；`--porcelain` 是为机器解析设计的稳定契约。收拢成单一模块让「哪些 git 操作会被执行」可被穷举审计 |
| D6 | git 调用是否过权限管线 | 不过 | 同 C8 存盘、C9 笔记落盘先例：系统内部操作，不是模型发起的工具调用。安全性由「参数全部代码构造 + `shell=False` + 模型只能影响已校验的名字 + 删除必过三层过滤」保证 |
| D7 | 删除入口 | `lifecycle.remove` 唯一入口，内部先调纯判定 `judge_removal` | 同 `_settle_session` 先例。纯判定可被单测穷举，且第①层位置校验兜住「空变量删整个仓库」那类事故 |
| D8 | 名字校验时机 | 拼接路径**之前** | 先拼后验时越界路径已经产生，任何一处漏检返回值就直接落盘 |
| D9 | 交付信息渲染放哪 | `worktree/render.py` 产出，`subagents/gate.py` 拼接 | 保住既有成对维护点「子 Agent 结论的渲染只有一份」——gate 仍是唯一出口 |
| D10 | 清理时机 | `bootstrap` 中一次，在任何子 Agent 可能启动之前 | 时机安全：启动时不可能有子 Agent 正在用某个工作区，竞态从根上不存在（spec 已否决后台定时器） |
| D11 | 软链失败怎么办 | 降级为复制，**并记警告** | Windows 上建符号链接常需额外权限。静默降级会让「配了 link 却复制了 500MB」无处可查 |
| D12 | `TaskRecord` 存什么 | 只存路径与分支名两个字符串 | 它在加锁临界区里被读写，而临界区的既有不变量是「只做纯内存读写」。放一个能调 git 的对象进去是给后来者挖坑 |

## 已知边界（本章不解决，需在 CLAUDE.md 登记）

1. **MCP 工具不受工作目录隔离约束。** 它们在权限管线里落 `kind="other"`（无路径判定），
   且 MCP Server 是在装配期以主项目根为工作目录启动的外部进程。隔离子 Agent 调用
   MCP 工具时，该工具的文件访问仍发生在主项目根。这与既有的已知项 #4（OS 级沙箱）
   是同一个缺口的另一面。
2. **`run_command` 子进程内部的路径访问不受约束。** 子进程的 `cwd` 是隔离工作区，
   但它自己用绝对路径访问主项目根仍然可行——与既有已知项 #4 完全同源。
3. **隔离工作区内的 `.rhinecode/` 缺席。** 子 Agent 在隔离工作区里读不到项目级
   `permissions.yaml` / `hooks.yaml`，但这不影响它们生效——那些配置在装配期就已从
   主项目根加载进内存，子 Agent 共享同一份引擎与 Hook 编排者。
