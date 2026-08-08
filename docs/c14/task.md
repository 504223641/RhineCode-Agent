# C14 子 Agent 工作区隔离 Tasks

> 对应 [`spec.md`](spec.md) 与 [`plan.md`](plan.md)。共 **37 个任务**，分五个阶段。
>
> **阶段顺序不可调换。** A 阶段是纯新增（零风险、可独立跑绿），B 阶段会**大面积破坏
> 既有测试**（`path_guard` 重命名 + 签名改必填），必须一气呵成不能中途停手，
> C/D 阶段在 B 的地基上接线。

## 文件清单

### 新建

| 文件 | 职责 |
| --- | --- |
| `rhinecode/worktree/__init__.py` | 包门面，re-export 对外接口 |
| `rhinecode/worktree/models.py` | 数据结构与异常类型 |
| `rhinecode/worktree/naming.py` | 名字安全校验与生成（纯函数零 IO） |
| `rhinecode/worktree/gitcmd.py` | 唯一的 git 子进程调用点 |
| `rhinecode/worktree/provision.py` | 环境初始化（copy / link） |
| `rhinecode/worktree/lifecycle.py` | 创建 / 检查 / 三层过滤 / 删除 |
| `rhinecode/worktree/cleanup.py` | 启动时扫描清理 |
| `rhinecode/worktree/render.py` | 交付信息段渲染 |
| `tests/test_worktree_naming.py` | 名字校验穷举 + 路径遍历构造 |
| `tests/test_worktree_gitcmd.py` | git 封装（真实临时仓库） |
| `tests/test_worktree_provision.py` | 初始化的四种路径 |
| `tests/test_worktree_lifecycle.py` | 创建 / 快速恢复 / 结束决策 / 三层过滤 |
| `tests/test_worktree_cleanup.py` | 过期判定 / 残留兜底 |
| `tests/test_path_guard_root.py` | root 参数化后的边界与 N2 反证 |
| `tests/test_perm_cwd_sandbox.py` | ②层按 cwd 判定 + 隔离越界反证 |
| `tests/test_loop_cwd_dispatch.py` | 四个分发点齐全 + 并发路径漏传反证 |
| `tests/test_subagent_isolation.py` | 单向加严 / 创建失败 / 保留删除 / 交付信息 |
| `docs/c14/README.md` | 本章导航 |

### 修改

| 文件 | 改动要点 |
| --- | --- |
| `rhinecode/tools/path_guard.py` | `workspace_root` → `main_project_root`；四个判定函数 root 必传 |
| `rhinecode/tools/base.py` | `workspace_aware` 类属性 + docstring |
| `rhinecode/tools/read_file.py` | 接 `cwd` |
| `rhinecode/tools/write_file.py` | 接 `cwd` |
| `rhinecode/tools/edit_file.py` | 接 `cwd` |
| `rhinecode/tools/glob_files.py` | 接 `cwd` + F18 跳过 |
| `rhinecode/tools/grep_content.py` | 接 `cwd` + F18 跳过 |
| `rhinecode/tools/run_command.py` | 接 `cwd` |
| `rhinecode/tools/run_agent.py` | `isolation` 参数 + `description` |
| `rhinecode/permission/models.py` | `PermissionRequest.cwd`（必填） |
| `rhinecode/permission/adapter.py` | `to_request` 增 `cwd` |
| `rhinecode/permission/engine.py` | ②层两处判定传 `request.cwd` |
| `rhinecode/permission/config.py` | 改用 `main_project_root` |
| `rhinecode/agent/loop.py` | `RunOptions.cwd` + 四个分发点 |
| `rhinecode/hooks/actions.py` | shell 动作 cwd 参数化 |
| `rhinecode/hooks/manager.py` | `dispatch` 增 `cwd` |
| `rhinecode/hooks/config.py` | 改用 `main_project_root` |
| `rhinecode/mcp/config.py` | 改用 `main_project_root` |
| `rhinecode/subagents/models.py` | `isolation` 字段；`UNSUPPORTED_FIELDS` 删一项 |
| `rhinecode/subagents/parser.py` | 读取并归一 `isolation` |
| `rhinecode/subagents/report.py` | 展示隔离声明 |
| `rhinecode/subagents/tasks.py` | `TaskRecord` 两个字段 |
| `rhinecode/subagents/service.py` | `resolve_isolation` + `delegate` 接线 |
| `rhinecode/subagents/runner.py` | 生命周期接线 + `RunOptions.cwd` + F15 提示 |
| `rhinecode/subagents/gate.py` | 拼接交付信息段 |
| `rhinecode/subagents/render.py` | `_INDEX_HEADER` 同口径 |
| `rhinecode/trace/models.py` | 四个新事件类型 |
| `rhinecode/trace/reader.py` | `SUMMARIZERS` 四个新条目 |
| `rhinecode/config.py` | `worktree` 配置段 + 模板 |
| `rhinecode/conversation.py` | 改用 `main_project_root`；worktree 配置透传 |
| `rhinecode/bootstrap.py` | 改用 `main_project_root`；插入启动清理 |
| `rhinecode/__main__.py` | 改用 `main_project_root` |
| `.gitignore` | 新增 worktrees 目录 |
| `CLAUDE.md` | 能力表 / 架构表 / 成对维护点 / 安全边界 / 已知项 |

---

# 阶段 A：worktree 叶子包（T1–T11）

> **纯新增，不碰任何既有代码。** 本阶段结束时全量测试应当仍是绿的。

## T1: 数据结构与异常

**文件：** `rhinecode/worktree/models.py`、`rhinecode/worktree/__init__.py`
**依赖：** 无

**步骤：**
1. 建包目录，`__init__.py` 暂留空（T10 再填门面）。
2. 定义异常层次：`WorktreeError`（基类）、`WorktreeNameError`、`GitUnavailable`、
   `NotARepository`、`GitCommandFailed`（携带 `stderr`）。全部带中文 docstring。
3. 定义 `WorktreeHandle`（`name` / `path` / `branch` / `base_commit` / `recovered`）、
   `ChangeStatus`（`dirty` / `commits` / `files` + `untouched` 属性）、
   `ProvisionEntry`（`source` / `mode`）、`ProvisionResult`（`applied` / `warnings`）、
   `RemovalVerdict`（`allowed` / `keep_branch` / `reason`）、
   `CleanupReport`（`removed` / `kept` / `scanned`），全部 `frozen=True`。
4. 定义常量 `WORKTREES_DIR_NAME = "worktrees"`、`BRANCH_PREFIX = "agent/"`、
   `MAX_NAME_LENGTH = 64`、`DEFAULT_CLEANUP_DAYS = 7`。

**验证：** `python -c "from rhinecode.worktree.models import WorktreeHandle, ChangeStatus; print(ChangeStatus(False,0,()).untouched)"` 输出 `True`

## T2: 名字校验与生成

**文件：** `rhinecode/worktree/naming.py`
**依赖：** T1

**步骤：**
1. 实现 `validate_name(raw: str) -> str`。校验顺序：去首尾空白后非空 → 不含反斜杠 →
   不以 `/` 开头或结尾 → 总长 ≤ `MAX_NAME_LENGTH` → 按 `/` 分段，每段非空、
   仅含 `[A-Za-z0-9._-]`、且不等于 `.` 或 `..`。任一不满足抛 `WorktreeNameError`，
   消息说明**具体哪一条**不满足（不要笼统地说「名字非法」）。
2. 实现 `generate_name(agent_name: str, task_id: str) -> str`：把 `agent_name`
   中的非法字符替换为 `-`、截断，拼上 `task_id` 的短前缀，
   **结果必须自己过一遍 `validate_name`** 再返回。
3. 在模块 docstring 里写明「校验必须先于任何路径拼接」及其理由。

**验证：** `python -c "from rhinecode.worktree.naming import validate_name as v; print(v('a/b_1.x'));
[print('拒:',n) for n in ['..','a/../b','','a\\\\b','/a','a/','.','a'*65]]"` —— 合法名返回原值，
八个非法构造全部抛异常

## T3: 名字校验测试

**文件：** `tests/test_worktree_naming.py`
**依赖：** T2

**步骤：**
1. 合法用例：单段、多段嵌套、含 `.`/`_`/`-`、恰好 64 字符。
2. **路径遍历构造穷举**（每条独立断言）：`..`、`a/../b`、`../a`、`a/..`、
   `.`、`a/./b`、绝对路径形式 `/a`、盘符形式 `C:/a`、反斜杠 `a\b`、
   空串、纯空白、65 字符、含空格、含 `;`、含 `$`、含中文。
3. `generate_name` 的产出必然通过 `validate_name`（用若干个含非法字符的角色名驱动）。

**验证：** `python -m unittest tests.test_worktree_naming -v` 全绿

## T4: git 子进程封装

**文件：** `rhinecode/worktree/gitcmd.py`
**依赖：** T1

**步骤：**
1. 私有 `_run(args: list[str], cwd: Path) -> str`：`subprocess.run`，
   **`shell=False`**，`capture_output=True`，`text=True`，`encoding="utf-8"`，
   `errors="replace"`。`FileNotFoundError` → `GitUnavailable`；
   返回码非零 → `GitCommandFailed(stderr)`。
2. 实现 plan 列出的全部函数：`ensure_repository`、`head_commit`、`add_worktree`、
   `list_worktrees`、`branch_exists`、`remove_worktree`、`prune_worktrees`、
   `delete_branch`、`status`、`commits_since`、`hooks_path`。
3. `list_worktrees` 解析 `git worktree list --porcelain` 输出，取每段的
   `worktree <path>` 行，返回解析后的绝对路径元组。
4. `status` 用 `git status --porcelain`（含未跟踪文件），返回 `(dirty, 文件元组)`。
5. `commits_since` 用 `git rev-list --count <base>..HEAD` 与
   `git diff --name-only <base>..HEAD`。
6. 模块 docstring 写明「本模块是全项目唯一执行 git 的地方」及 `shell=False` 的理由。

**验证：** `python -c "from rhinecode.worktree import gitcmd; from pathlib import Path;
print(gitcmd.head_commit(Path('.'))[:7]); print(len(gitcmd.list_worktrees(Path('.'))))"`
输出当前短哈希与 1

## T5: git 封装测试

**文件：** `tests/test_worktree_gitcmd.py`
**依赖：** T4

**步骤：**
1. 建一个 `setUp` 辅助：在临时目录里 `git init` + 配好 user.name/email + 一次初始提交。
   清理走 `tests/e2e/sandbox.py` 的 `force_rmtree`（**不要**直接 `shutil.rmtree(...,
   ignore_errors=True)`——只读的 `.git/objects` 会让它「删一半且不报错」）。
2. 断言：`ensure_repository` 在非仓库目录抛 `NotARepository`；`add_worktree` 后
   `list_worktrees` 返回两项；`status` 在干净工作区返回 `(False, ())`，写个文件后
   返回 `(True, (...))`；`commits_since` 在无新提交时返回 `(0, ())`，提交后返回
   `(1, ('file',))`；`branch_exists` 对已存在/不存在分支返回正确值。
3. 一条 **F11 护栏**：`hooks_path(主仓库)` 与 `hooks_path(worktree)` 相等。

**验证：** `python -m unittest tests.test_worktree_gitcmd -v` 全绿

## T6: 环境初始化

**文件：** `rhinecode/worktree/provision.py`
**依赖：** T1

**步骤：**
1. 实现 `provision(main_root, target, entries) -> ProvisionResult`。
2. 每条依次校验：来源经 `path_guard.resolve_in_workspace(entry.source, main_root)`
   成功（越界跳过并记警告）→ 来源存在（不存在跳过并记警告）→
   目标 `target / entry.source` 解析后仍在 `target` 内（否则跳过记警告）。
3. `copy` 模式：文件用 `shutil.copy2`，目录用 `shutil.copytree`。
4. `link` 模式：先试 `Path.symlink_to`（目录传 `target_is_directory=True`）；
   `OSError` / `NotImplementedError` 时降级为复制，**并记一条警告说明降级原因**。
5. 单条任何异常都只记警告、继续下一条，绝不向上抛。
6. 目标父目录不存在时先建。

**验证：** `python -m unittest tests.test_worktree_provision`（T7 完成后）

## T7: 环境初始化测试

**文件：** `tests/test_worktree_provision.py`
**依赖：** T6

**步骤：**
1. copy 一个文件：目标出现，**改目标后源文件不变**（AC13 的独立性）。
2. link 一个目录：目标是链接或（降级时）副本，且警告里含降级说明。
3. 来源指向主项目根之外（用 `../` 与绝对路径两种写法）：跳过 + 警告，其余条目照常生效。
4. 来源不存在：跳过 + 警告，返回值仍是成功的 `ProvisionResult`。
5. 空清单：返回空结果，不建任何东西。

**验证：** `python -m unittest tests.test_worktree_provision -v` 全绿

## T8: 生命周期

**文件：** `rhinecode/worktree/lifecycle.py`
**依赖：** T2、T4、T6

**步骤：**
1. 私有 `_worktrees_root(main_root) -> Path` = `main_root / ".rhinecode" / "worktrees"`。
2. `create`：按 plan 的七步。第 2 步的位置校验用「解析后必须落在 `_worktrees_root` 内
   且不等于它本身」，**不复用 `path_guard`**（那是以 main_root 为界，太宽）。
   第 3 步快速恢复**全程不调 git**：目标目录存在 且 其下 `.git` 是文件 且
   内容以 `gitdir:` 开头 且 指向路径解析后落在 `main_root / ".git" / "worktrees"` 内
   → 返回 `recovered=True`。第 5 步分支冲突时依次试 `-2`、`-3`……上限 50 次后放弃并抛错。
3. `inspect(handle) -> ChangeStatus`：调 `status` 与 `commits_since`，
   文件清单合并去重后按字典序返回。
4. `judge_removal(main_root, path, status) -> RemovalVerdict`：**纯判定零副作用**。
   ①位置：`path` 非空、解析成功、落在 `_worktrees_root` 内且不等于它；
   ②归属：`path` 出现在 `list_worktrees(main_root)` 中；
   ③变更：`status.dirty` 为真则拒绝。
   通过时 `keep_branch = status.commits > 0`。每条拒绝都填中文 `reason`。
5. 私有 `_force_rmtree(path)`：处理只读文件（git 的 `.git/objects` 在 Windows 上
   是只读的，直接 `shutil.rmtree` 会「删一半且不报错」）——`onerror` 回调里
   `chmod` 后重试。
   ⚠ **不要 import `tests/e2e/sandbox.py` 的同名函数**：那是测试设施、不进产品包，
   产品代码依赖它会在真实安装后直接 `ImportError`。两份实现是必要的重复，
   在 docstring 里互相指一下即可。
6. `remove(main_root, path, branch, status) -> RemovalVerdict`：先 `judge_removal`，
   未获许可**立即返回、不碰文件系统**。获许可后依次：`remove_worktree` →
   `prune_worktrees` → 目录若仍存在则 `_force_rmtree` 兜底 →
   `keep_branch` 为假时 `delete_branch`。
7. 在 `remove` 的 docstring 里写明「本包唯一删除入口」及其理由。

**验证：** `python -m unittest tests.test_worktree_lifecycle`（T9 完成后）

## T9: 生命周期测试

**文件：** `tests/test_worktree_lifecycle.py`
**依赖：** T8

**步骤：**
1. `create` 正常路径：目录出现、分支存在、`base_commit` 等于当时的 HEAD、
   `recovered` 为假。
2. **主项目根的未提交改动不进隔离工作区**（AC10）：主仓库改一个文件不提交，
   create 后在隔离工作区读同一文件，内容是旧版本。
3. 分支冲突：先手工建 `agent/x`，再 create 名为 `x` 的工作区，
   `handle.branch` 为 `agent/x-2`。
4. **快速恢复**（AC12）：对同一名字连调两次 create，第二次 `recovered=True`；
   用一个假的 `gitcmd.add_worktree` 断言第二次**没有被调用**。
5. 名字非法：create 抛错，且 `_worktrees_root` 下**没有产生任何目录**（AC8）。
6. `judge_removal` 穷举：空路径 / `..` / 指向工作区根之外 / 未登记的普通目录 /
   dirty / 干净无提交 / 干净有提交，七条各自断言 `allowed` 与 `keep_branch`。
7. **`remove` 的反证**：构造一个 dirty 的工作区调 `remove`，断言返回未许可
   **且目录仍在**。
8. `remove` 有提交时：目录消失、分支仍在、`git show <branch>` 能取回内容（AC29）。

**验证：** `python -m unittest tests.test_worktree_lifecycle -v` 全绿

## T10: 清理、渲染与包门面

**文件：** `rhinecode/worktree/cleanup.py`、`rhinecode/worktree/render.py`、
`rhinecode/worktree/__init__.py`
**依赖：** T8

**步骤：**
1. `cleanup.scan_and_clean(main_root, max_age_days) -> CleanupReport`：
   目录不存在返回空报告；逐个子目录算「目录内文件最近修改时间」
   （`os.walk` 时跳过 `.git`，取 mtime 最大值；无文件时用目录自身 mtime）；
   未过期跳过；过期项 `inspect` + `remove`，按结论填 `removed` / `kept`。
   **整个函数对任何异常 fail-safe**：`try/except Exception` 包住每个子目录的处理，
   失败只计入 `kept` 并附原因。
2. `render.render_delivery(handle, status) -> str`：输出 plan 里那段固定格式
   （分支 / 基点 / 路径 / 提交数 / 改动文件 / 未提交改动）。文件清单超过 10 项时
   截断并标注「等 N 个文件」。路径用相对主项目根的形式展示。
3. `__init__.py` re-export：`create`、`inspect`、`remove`、`scan_and_clean`、
   `render_delivery`、以及全部模型与异常。

**验证：** `python -c "from rhinecode.worktree import create, inspect, remove, scan_and_clean, render_delivery; print('ok')"`

## T11: 清理测试

**文件：** `tests/test_worktree_cleanup.py`
**依赖：** T10

**步骤：**
1. 目录不存在：返回空报告，不报错。
2. 未过期的干净工作区：不被删除。
3. 过期 + 干净 + 无提交：被删除，`removed` 里分支名为空串。
4. 过期 + 干净 + 有提交：目录删除、分支保留，`removed` 里带分支名（AC29）。
5. 过期 + dirty：不删除，`kept` 里附原因（AC28）。
6. 目录下一个手工建的普通目录（未被 git 登记）：不被碰（AC27）。
7. **残留兜底**（AC30）：先 `git worktree remove` 使其注销但手工保留目录，
   再跑清理，目录被删除且不抛异常。
8. 制造一个会让 `inspect` 抛异常的条目，断言清理整体不抛、其余条目照常处理。

**验证：** `python -m unittest tests.test_worktree_cleanup -v` 全绿；
`python -m unittest discover -s tests` 仍全绿（本阶段零回归）

---

# 阶段 B：cwd 管道（T12–T22）

> ⚠ **本阶段一开始就会破坏大量既有测试，中途不可停手。** T12 改完签名后，
> 到 T21 跑完之前，全量测试都不会是绿的。逐任务的验证以「目标文件编译通过 +
> 本任务的针对性断言」为准，全量回归在 T22 收口。

## T12: path_guard 参数化

**文件：** `rhinecode/tools/path_guard.py`
**依赖：** 无（可与阶段 A 并行）

**步骤：**
1. `workspace_root()` 重命名为 `main_project_root()`，docstring 改写为
   「进程启动时的当前工作目录。**只用于定位与调用者无关的位置**（配置文件、
   存档目录），**不用于路径边界判定**」。
2. `resolve_in_workspace(path, root)`、`resolve_readable(path, root)`、
   `is_within_workspace(path, root)`、`is_readable_path(path, root)`
   四个函数增加 **无默认值** 的 `root` 参数，内部不再调 `main_project_root()`。
3. 在模块 docstring 里写明「root 为什么必传」：有默认值等于漏传的地方静默按
   主项目根判定，一次隔离故障会静默变成一次越权（spec N2）。
4. `validate_glob_pattern` 不变；`_EXTRA_READ_ROOTS` 与 `register_read_root` /
   `clear_read_roots` 不变，并补一句注释说明它与 root 正交。

**验证：** `python -c "import rhinecode.tools.path_guard as p; import inspect;
print(inspect.signature(p.resolve_in_workspace))"` 显示两个必填参数

## T13: 修复 path_guard 的既有调用点

**文件：** `rhinecode/permission/config.py`、`rhinecode/hooks/config.py`、
`rhinecode/hooks/actions.py`、`rhinecode/mcp/config.py`、`rhinecode/conversation.py`、
`rhinecode/bootstrap.py`、`rhinecode/__main__.py`
**依赖：** T12

**步骤：**
1. 全项目搜索 `workspace_root`，逐个复核：**这里要的是主项目根，还是调用者的
   工作目录？**
2. 定位配置文件与目录的（`permissions.yaml`、`hooks.yaml`、`mcp.yaml`、
   `.rhinecode/context`、`.rhinecode/agents`、trace 默认路径、环境信息、
   调试日志路径）一律改为 `main_project_root()`。
3. `hooks/actions.py` 里 shell 动作的 `cwd=` 暂时也用 `main_project_root()`，
   T22 再改成参数化。
4. **逐处留一行注释**说明为什么这里是主项目根而不是调用者工作目录。

**验证：** `python -m compileall rhinecode` 通过；
`grep -rn "workspace_root" rhinecode/ | grep -v __pycache__` 无结果

## T14: path_guard 测试

**文件：** `tests/test_path_guard_root.py`
**依赖：** T12

**步骤：**
1. 同一个相对路径在两个不同 root 下解析到两个不同绝对路径。
2. 一个落在 root A 内的绝对路径，用 root B 判定时被拒。
3. **N2 反证**：`root` 传 `None` / 空字符串 / 不存在的路径时，
   `is_within_workspace` 与 `is_readable_path` 返回 `False`（**不是**按主项目根放行）。
4. 只读白名单（F5）：注册一个白名单目录后，在**任意** root 下 `is_readable_path`
   对该目录内文件为真、`is_within_workspace` 为假。
5. `..` 与指向 root 外的符号链接在任何 root 下都被拒。

**验证：** `python -m unittest tests.test_path_guard_root -v` 全绿

## T15: PermissionRequest.cwd 与 adapter

**文件：** `rhinecode/permission/models.py`、`rhinecode/permission/adapter.py`
**依赖：** T12

**步骤：**
1. `PermissionRequest` 增 `cwd: Path` 字段，**无默认值**，放在 `mode` 之后、
   `host` 之前（`host` 有默认值必须在后）。docstring 写明「无默认值是刻意的」。
2. `to_request(tool, args, mode, cwd)` 增 `cwd` 必填参数，原样写入请求。
3. 检查 `to_allow_rule` 等同文件其它函数是否受影响（应当不受）。

**验证：** `python -c "from rhinecode.permission.models import PermissionRequest;
PermissionRequest('a','a','x','read_path',True,None)"` 抛 `TypeError`（缺 cwd）

## T16: 引擎②层按 cwd 判定

**文件：** `rhinecode/permission/engine.py`
**依赖：** T15

**步骤：**
1. 第②层两处判定改为 `is_readable_path(request.specifier, request.cwd)` 与
   `is_within_workspace(request.specifier, request.cwd)`。
2. 拒绝文案里的「项目工作目录」改为能体现实际根的措辞（隔离子 Agent 看到
   「超出工作目录」时应能意识到那是它的隔离工作区）。
3. **层序一字不动**——补一行注释重申①②②′③④的顺序不因本章改变。

**验证：** `python -m compileall rhinecode/permission` 通过；
`grep -n "is_within_workspace\|is_readable_path" rhinecode/permission/engine.py`
两处都带 `request.cwd`

## T17: 权限层测试

**文件：** `tests/test_perm_cwd_sandbox.py` + 既有权限测试同步
**依赖：** T16

**步骤：**
1. 新测试：同一个 `write_path` 请求，`cwd` 为主项目根时放行、
   `cwd` 为某隔离工作区时被②层拒（AC2）。
2. **隔离越界反证**：隔离子 Agent 以绝对路径请求主项目根内、工作区外的文件，
   判定为 DENY 且 `layer` 为 `SANDBOX`。
3. **AC3 反证**：`cwd` 为 `None` 时判定为 DENY，而**不是**按主项目根放行。
4. 只读白名单在隔离 cwd 下仍对 read 生效、对 write 不生效（AC5）。
5. 修既有的 `tests/test_perm_*.py`：所有 `PermissionRequest(...)` 与
   `to_request(...)` 构造点补 `cwd`。**统一补主项目根**，保证既有断言语义不变。

**验证：** `python -m unittest discover -s tests -p "test_perm_*.py"` 全绿

## T18: Tool 标志与六个工具

**文件：** `rhinecode/tools/base.py` 及六个工具文件
**依赖：** T12

**步骤：**
1. `base.py` 加 `workspace_aware: bool = False` 类属性，docstring 照 `plan_safe`
   的写法说明：**声明它的工具，其 `execute` 必须接受 `cwd` 关键字参数**。
2. 六个工具（`read_file` / `write_file` / `edit_file` / `glob_files` /
   `grep_content` / `run_command`）声明 `workspace_aware = True`，
   签名改为 `execute(self, args, cwd)`，内部把 `cwd` 传给对应的 path_guard 调用。
3. `run_command` 的 `subprocess` 参数 `cwd=cwd`。
4. `glob_files` 的 `base = cwd`；`grep_content` 的 `base = cwd`。

**验证：** `python -c "from rhinecode.tools.read_file import ReadFileTool;
from pathlib import Path; t=ReadFileTool();
print(t.workspace_aware, t.execute({'path':'CLAUDE.md'}, cwd=Path.cwd()).ok)"`
输出 `True True`

## T19: 搜索工具跳过隔离工作区

**文件：** `rhinecode/tools/glob_files.py`、`rhinecode/tools/grep_content.py`
**依赖：** T18

**步骤：**
1. 在两个工具的目录遍历中，跳过**解析后等于** `cwd / ".rhinecode" / "worktrees"`
   的目录。写成路径相等判断，**不要**按目录名匹配（避免误伤用户自己叫
   `worktrees` 的业务目录）。
2. `grep_content` 在既有的 `SKIP_DIRS` 过滤之后追加这条判断（`SKIP_DIRS` 是按名字的，
   两者语义不同，不要合并）。
3. 留注释说明：隔离工作区内部不存在该路径，故这条规则对子 Agent 是空操作。

**验证：** 手工建一个 `.rhinecode/worktrees/probe/x.py` 内含某唯一字符串，
`grep_content` 搜该字符串只返回主项目根内的命中

## T20: RunOptions.cwd 与四个分发点

**文件：** `rhinecode/agent/loop.py`
**依赖：** T15、T18

**步骤：**
1. `RunOptions` 增 `cwd: Optional[Path] = None`，docstring 说明「None → 主项目根」
   及为什么不直接缺省成主项目根。
2. `Agent` 解析并持有 `self._cwd = options.cwd or main_project_root()`。
3. 分发点一——**并发只读桶** `_run` 内：按 `tool.workspace_aware` 决定是否传 `cwd`。
4. 分发点二——**串行桶** `_run_one_serial` 内：同上，与既有 `plan_stage` 的传递
   并存（两个标志各自独立判断，不要写成 if/elif）。
5. 分发点三——调 `to_request` 处补 `cwd=self._cwd`。
6. 分发点四——工具级 Hook 分发处补 `cwd=self._cwd`（T22 消费）。
7. 在 `_run` 上方留一段注释：**`plan_stage` 只在串行传、`cwd` 两条都要传**，
   并写明漏掉并发路径的后果（隔离子 Agent 的读落到主项目根、写却是对的，
   界面上看不出来）。

**验证：** `python -m compileall rhinecode/agent` 通过；
`grep -c "workspace_aware" rhinecode/agent/loop.py` ≥ 2

## T21: 分发点测试

**文件：** `tests/test_loop_cwd_dispatch.py`
**依赖：** T20

**步骤：**
1. 造两个假工具：一个 `read_only=True` + `workspace_aware=True`（走并发路径）、
   一个 `read_only=False` + `workspace_aware=True`（走串行路径），
   各自记录收到的 `cwd`。
2. 用 `RunOptions(cwd=<某目录>)` 跑一轮，断言**两个工具都收到了那个 cwd**——
   这条就是「并发路径漏传」的反证。
3. 造一个 `workspace_aware=False` 的工具，断言它**没有**收到 `cwd` 关键字
   （签名不受影响）。
4. 断言 `to_request` 收到的 `cwd` 与 `RunOptions.cwd` 一致（用假引擎捕获请求）。
5. `RunOptions()` 不传 cwd 时，工具收到的是主项目根（N3）。

**验证：** `python -m unittest tests.test_loop_cwd_dispatch -v` 全绿

## T22: Hook 的工作目录

**文件：** `rhinecode/hooks/actions.py`、`rhinecode/hooks/manager.py`
**依赖：** T20

**步骤：**
1. `actions` 里 shell 动作的执行函数增 `cwd: Path` 参数，替换写死的
   `main_project_root()`。
2. `HookManager.dispatch`（及其内部转发链）增 `cwd: Path` 参数，缺省主项目根。
3. Agent Loop 的工具级三事件传 `self._cwd`；其余事件（会话级、回合级、消息级、
   系统级）传主项目根。
4. 留注释说明这条区分的理由（F25）。

**验证：** `python -m unittest discover -s tests -p "test_hook_*.py"` 全绿；
`python -m unittest discover -s tests` 全绿（**阶段 B 的回归收口**）

---

# 阶段 C：subagents 接线（T23–T30）

## T23: isolation 字段

**文件：** `rhinecode/subagents/models.py`、`rhinecode/subagents/parser.py`
**依赖：** T11

**步骤：**
1. `AgentSpec` 增 `isolation: Optional[str] = None`。
2. **从 `UNSUPPORTED_FIELDS` 删除 `"isolation"` 一项**（CLAUDE.md 已登记的成对
   维护点；漏删的表现是「功能做了但用户被告知不支持」）。
3. `parser` 读取 `isolation`：值为 `worktree`（大小写不敏感）时保留，
   为 `none` / 空 / 缺省时归一为 `None`，其它值**记警告并按未声明处理**
   （不阻断加载，与本章其余字段一致）。
4. 连字符/下划线两种写法都要认（走既有的 `_normalize_keys`）。

**验证：** `python -m unittest tests.test_subagent_parser -v` 全绿
（其中遍历 `UNSUPPORTED_FIELDS` 的那条断言会自动覆盖新表）

## T24: 角色报告展示

**文件：** `rhinecode/subagents/report.py`
**依赖：** T23

**步骤：** 在角色条目里展示隔离声明（声明了显示「隔离：worktree」，未声明不显示该行）。

**验证：** `python -m unittest tests.test_subagent_report -v` 全绿

## T25: TaskRecord 字段

**文件：** `rhinecode/subagents/tasks.py`
**依赖：** T11

**步骤：**
1. `TaskRecord` 增 `worktree_path: str = ""` 与 `worktree_branch: str = ""`。
2. 任务列表渲染处展示这两项（为空则不显示）。
3. **确认既有加锁不变量未被破坏**：这两个字段是纯字符串，赋值仍在临界区内是安全的。

**验证：** `python -m unittest tests.test_subagent_tasks -v` 全绿

## T26: 单向加严与委派接线

**文件：** `rhinecode/subagents/service.py`
**依赖：** T23、T25

**步骤：**
1. 加纯函数 `resolve_isolation(spec_declared, call_requested) -> bool`：
   角色声明了 `worktree` 恒返回 True（调用方关不掉）；未声明时取调用方意愿。
2. `delegate` 增 `isolation` 参数，调用上面的函数得出最终值。
3. 需要隔离时调 `worktree.create`；抛任何 `WorktreeError` 时转成**委派失败**的
   `DelegateOutcome`，消息包含具体原因（非 git 仓库 / git 不可用 / 名字非法 /
   创建命令失败），**不降级**（F12）。
4. 成功时把 `handle` 传给 runner，并把路径与分支写进 `TaskRecord`。

**验证：** `python -c "from rhinecode.subagents.service import resolve_isolation as r;
print(r('worktree', False), r('worktree', None), r(None, True), r(None, None))"`
输出 `True True True False`

## T27: 运行器生命周期接线

**文件：** `rhinecode/subagents/runner.py`
**依赖：** T20、T26

**步骤：**
1. `run_subagent` 增 `handle: Optional[WorktreeHandle]` 参数。
2. `cwd = handle.path if handle else main_project_root()`，传进
   `RunOptions(cwd=cwd, ...)`。
3. `_build_prompts`：`handle` 非空时追加一段隔离说明（F15），内容为
   「你运行在一个隔离的 Git 工作目录中，路径 X，分支 Y，成果请提交到该分支」。
   ⚠ 这段与 `SUBAGENT_CONVENTIONS` **并列追加**，不要写进角色正文
   （写进正文的话用户自己写的角色一个都盖不到）。
4. `finally` 里：`handle` 非空时 `inspect` → `untouched` 则 `remove`、
   否则保留；结果存起来交给 gate 渲染。
5. ⚠ 结束处理**整段用 try/except 兜住**，绝不向上抛（它跑在后台线程上）。
6. ⚠ `inspect` / `remove` 是文件系统与子进程操作，**必须在 `TaskManager`
   的锁外**（C13 硬不变量）。

**验证：** `python -m unittest tests.test_subagent_runner -v` 全绿

## T28: 交付信息拼接

**文件：** `rhinecode/subagents/gate.py`
**依赖：** T10、T27

**步骤：**
1. `render_subagent_message` 在结论正文之后追加 `worktree.render_delivery(...)`
   的产出（仅隔离任务）。
2. ⚠ 只改这一个函数——CLAUDE.md 的成对维护点要求「子 Agent 结论的渲染只有一份」，
   闸门的迭代级交付与协调层的兜底交付共用它。

**验证：** `python -m unittest tests.test_subagent_gate -v` 全绿

## T29: 委派工具与角色清单表头

**文件：** `rhinecode/tools/run_agent.py`、`rhinecode/subagents/render.py`
**依赖：** T26

**步骤：**
1. `run_agent` 的 `parameters` 增可选 `isolation`（布尔），
   `description` 补一段：涉及写文件、且主对话手上可能有未提交改动时应要求隔离；
   隔离子 Agent 的成果通过分支交付。
2. `render.py` 的 `_INDEX_HEADER` 补**同口径**的一句。
3. ⚠ 两处必须同口径——这是 CLAUDE.md 已登记的成对维护点，一处强一处弱等于白改。

**验证：** `python -m unittest tests.test_subagent_tool -v` 全绿
（含既有的 `SameVoiceTest`）

## T30: 隔离集成测试

**文件：** `tests/test_subagent_isolation.py`
**依赖：** T29

**步骤：**
1. **单向加严**（AC19）：角色声明隔离 + 调用要求不隔离 → 仍创建；
   角色未声明 + 调用要求隔离 → 创建。
2. **创建失败不降级**（AC16/AC17）：在非 git 仓库里委派声明隔离的角色 →
   `DelegateOutcome` 失败、消息含「不是 Git 仓库」、**未创建任何目录**；
   同一环境下委派未声明隔离的角色 → 成功。
3. **结束保留/删除**（AC21/AC22）：子 Agent 无变更 → 目录与分支都不存在；
   有变更 → 目录仍在。
4. **交付信息准确性**（AC23）：让假模型在正文里写一个**错误**的分支名，
   断言交付信息段里是**实际**分支名。
5. **Hook 对隔离子 Agent 生效**（AC34）：一条拦截规则对隔离子 Agent 同样命中。
6. **并发**（AC35）：三个隔离子 Agent 同时委派，三个目录三个分支互不干扰。

**验证：** `python -m unittest tests.test_subagent_isolation -v` 全绿

---

# 阶段 D：配置、观测与启动（T31–T35）

## T31: 配置段

**文件：** `rhinecode/config.py`、`rhinecode/conversation.py`
**依赖：** 无

**步骤：**
1. `Config` 增 `worktree_cleanup_days: int = 7`、
   `worktree_copy: tuple[str, ...] = ()`、`worktree_link: tuple[str, ...] = ()`。
2. 从 YAML 的 `worktree:` 段读取，缺省全部走默认值；非法值（负数、非列表）
   记警告并回退默认。
3. 模板生成（`config.example.yaml` 与首次运行生成的模板）加上该段与注释。
4. `conversation.py` 把清单转成 `ProvisionEntry` 序列，透传给 `SubAgentService`。

**验证：** `python -m unittest tests.test_config -v` 全绿

## T32: trace 事件

**文件：** `rhinecode/trace/models.py`、`rhinecode/trace/reader.py`
**依赖：** T10

**步骤：**
1. `TraceEventType` 增 `WORKTREE_CREATE`、`WORKTREE_PROVISION`、
   `WORKTREE_SETTLE`、`WORKTREE_CLEANUP`。
2. `reader.py` 的 `SUMMARIZERS` 表**同步加四个条目**（⚠ 成对维护点，
   漏了不报错，只会在阅读器里显示成「（未登记类型）」）。
3. 在 `lifecycle` / `cleanup` 的对应位置埋点，**全部走可选 recorder**
   （为 `None` 时不埋），且埋点异常一律吞掉。

**验证：** `python -m unittest tests.test_trace_reader -v` 全绿
（其中遍历事件类型的断言会自动覆盖新增项）

## T33: 启动清理

**文件：** `rhinecode/bootstrap.py`
**依赖：** T10、T31

**步骤：**
1. 在角色目录扫描附近插入 `scan_and_clean(main_project_root(), cfg.worktree_cleanup_days)`。
2. 位置约束：必须在配置加载**之后**、任何子 Agent 可能启动**之前**。
   ⚠ 既有约束（`exclude_tools` 摘除必须在 `session_start` 快照之前）不受影响，
   但**不要**把清理插进那个窄窗口里。
3. `CleanupReport` 非空时随其它启动提示一并呈现（删除项列名字与保留的分支名，
   未删除项列原因）。
4. 整段 fail-safe：清理抛异常不阻断启动。
5. **在那段插入位置留注释说明为什么卡在这里**（CLAUDE.md 要求装配顺序的理由
   注释随代码走）。

**验证：** `python -m unittest tests.test_bootstrap -v` 全绿；
手工在 `.rhinecode/worktrees/` 放一个过期空目录，启动后被清理并有提示

## T34: 忽略规则

**文件：** `.gitignore`
**依赖：** 无

**步骤：** 加 `**/.rhinecode/worktrees/`，并按既有风格写注释说明它是一份
完整源码 checkout、可能含被复制进去的本地配置（N8）。

**验证：** `git status --short` 在建了隔离工作区之后仍然干净

## T35: 全量回归

**文件：** 无
**依赖：** T34

**步骤：**
1. `python -m compileall rhinecode tests`
2. `python -m unittest discover -s tests`
3. 逐条核对失败项，修到全绿。
4. 记录最终测试数与 skipped 数。

**验证：** 全量测试全绿，skipped 数与改造前一致（4 项）

---

# 阶段 E：文档（T36–T37）

## T36: CLAUDE.md 更新

**文件：** `CLAUDE.md`
**依赖：** T35

**步骤：**
1. **能力表**加 C14 一行。
2. **架构分层表**加 `worktree/` 一层，⚠ 列写「`remove` 是唯一删除入口，
   三层过滤必须先于任何文件系统操作」。
3. **成对维护点**新增五条：
   - 新增路径判定函数 → root 必须无默认值
   - `cwd` 分发点：并发桶 + 串行桶 + `to_request` + hook 分发，**四处齐改**
   - 新增 worktree trace 事件 → `models.py` 枚举 + `reader.py` 的 `SUMMARIZERS`
   - `run_agent.description` ↔ `render._INDEX_HEADER` 的隔离口径（既有条目补充）
   - 新增删除路径 → 必须走 `lifecycle.remove`
4. **安全边界**新增「子 Agent 工作区隔离（c14）」小节：隔离只收紧不放宽、
   git 调用不过权限管线的理由、三层过滤、隔离工作区内产物与主项目同级敏感。
5. **已知后续工程项**新增一条，登记 plan 里那三条已知边界（MCP 工具与
   `run_command` 子进程不受 cwd 约束）。
6. **常用命令**不需要改（本章不新增命令）。

**验证：** 通读一遍，确认新增条目与实际代码一致

## T37: 本章导航

**文件：** `docs/c14/README.md`
**依赖：** T36

**步骤：**
1. 一句话说明本章做什么。
2. 四份文档的导航表。
3. **九个由用户拍板的设计决策**列表（含四条与 Claude Code 刻意不一致的地方及理由）。
4. **五条实测结论**（`.git` 是 51 字节指针文件、gitignore 的文件缺席、
   Python 模块解析天然正确、git hooks 天然共享、Windows 上的删除残留形态）。
5. 开发期发现的问题记录（留空，验收后补）。

**验证：** 链接可达，与四份文档内容一致

---

## 执行顺序

```
阶段 A（纯新增，零回归）
T1 → T2 → T3
 ↓     ↓
T4 → T5
 ↓
T6 → T7
 ↓
T8 → T9 → T10 → T11
                  ↓
阶段 B（一气呵成，中途测试不绿）
T12 → T13 → T14
  ↓
 T15 → T16 → T17
  ↓
 T18 → T19
  ↓
 T20 → T21 → T22 ←── 阶段 B 回归收口
                ↓
阶段 C
T23 → T24
 ↓
T25 → T26 → T27 → T28 → T29 → T30
                                ↓
阶段 D
T31 → T33
T32 → T33
T34
        ↓
       T35 ←── 全量回归
        ↓
阶段 E
T36 → T37
```

**可并行的：** T12–T14（path_guard）与阶段 A 的 T1–T11 互不依赖，可并行。
T31、T32、T34 三者互不依赖。

**关键路径：** T1 → T4 → T8 → T10 → T23 → T26 → T27 → T28 → T30 → T35 → T36。
