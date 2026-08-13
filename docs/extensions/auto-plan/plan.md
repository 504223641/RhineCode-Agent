# 只留 `auto` / `plan` 两个模式 Plan

> 对应 [`spec.md`](spec.md)。语言：Python 3.11+。

## 架构概览

本扩展**不新增任何一层**，只加一个薄薄的**预设层**盖在既有两条轴上，
外加一处子进程环境的收口。

```
                 ┌──────────────────────────────────────────┐
   用户 ──────►  │  Shift+Tab  /  /mode（别名 /plan）        │
                 └───────────────────┬──────────────────────┘
                                     ▼
                        ┌────────────────────────┐
                        │  presets.py（新增叶子）│  纯映射，无状态
                        │  Preset ⇄ (档位, 阶段) │
                        └───────┬────────────────┘
                                ▼
        ┌───────────────────────────────────────────────┐
        │  ConversationManager（既有状态的唯一持有者）  │
        │    self._engine.mode      ← 轴一：权限档      │
        │    self.plan_mode         ← 轴二：规划阶段    │
        └───────────────────────────────────────────────┘
                                │
                                ▼（一字不改）
              ①黑名单 → ②沙箱 → ②′网络 → ③规则 → ④模式
                                ▼
                      ②″保护路径（出口收紧器）
```

**关键判断：预设层不持有状态。** `Preset` 是从既有两条轴**推导**出来的值
（spec F4/N5），系统里可变的运行期状态仍然只有 `engine.mode` 与 `plan_mode` 两个。

### 一处会让人误会的简化，必须写下来

两个预设**共用同一个权限档**（都是 `PERMISSIVE`，spec F2），所以推导实际上
**只取决于 `plan_mode`**：

```
preset = PLAN if plan_mode else AUTO      # engine.mode 不参与
```

看起来"那还叫什么两条轴"。**结构仍是两条轴，只是当前这版预设在档位那条轴上
取了同一个点。** 权威定义放在 `PRESET_AXES` 这张表里（下面）——推导函数是它的
逆向查表，将来若真出现第三个预设（比如接上分类器之后的某种形态），
改那张表即可，推导逻辑跟着走。

不写这段的后果很实际：下一个人看到推导只读 `plan_mode`，会顺手把
`PRESET_AXES` 里的档位字段删掉当冗余，而那张表正是 spec AC6a
（来回切换后两条轴逐字不变）唯一的可断言对象。

## 核心数据结构

### `rhinecode/presets.py`（新增，叶子模块）

```python
class Preset(str, Enum):
    """用户可见的两个模式。取值即显示名。"""
    AUTO = "auto"
    PLAN = "plan"


# 预设 → (权限档, 规划阶段) 的权威定义。
# ⚠ 这张表是「预设是两条轴的组合」这个结构的唯一落点，别当冗余删掉。
PRESET_AXES: dict[Preset, tuple[PermissionMode, bool]] = {
    Preset.AUTO: (PermissionMode.PERMISSIVE, False),
    Preset.PLAN: (PermissionMode.PERMISSIVE, True),
}

# Shift+Tab 的两态循环顺序。
PRESET_CYCLE: dict[Preset, Preset] = {Preset.AUTO: Preset.PLAN, Preset.PLAN: Preset.AUTO}

DEFAULT_PRESET: Preset = Preset.AUTO          # spec F3

# 权限档的显示名（spec F16）。
# ⚠ `PERMISSIVE` 显示成 "auto" 是**刻意的**：auto 预设内部就是这个档位，
#    两处叫同一个名字才不会让用户以为它们是两回事。
MODE_LABELS: dict[PermissionMode, str] = {
    PermissionMode.STRICT: "严格（strict）",
    PermissionMode.DEFAULT: "默认（default）",
    PermissionMode.PERMISSIVE: "auto",
}


def preset_of(plan_mode: bool) -> Preset: ...
def axes_of(preset: Preset) -> tuple[PermissionMode, bool]: ...
def next_preset(preset: Preset) -> Preset: ...
```

**依赖**：只依赖 `permission.models.PermissionMode`。是叶子模块，谁都能引它。

### `ModeTarget`（`commands/models.py`，改）

```python
class ModeTarget(Enum):
    THINKING = "thinking"
    PRESET = "preset"        # 原 PLAN，改名
    # PERMISSION 删除（spec F7）
```

改名理由：`switch_mode(ModeTarget.PLAN)` 现在的语义是「在两个预设间循环」，
不是「切 Plan Mode 开关」。留旧名会让下一个人以为它只管 plan 那条轴。

⚠ 这是 `CLAUDE.md` 已登记的成对维护点，三处同步：本枚举 +
`tui/app.py` 的 `switch_mode` 分支 + `conversation.py` 的领域方法。

## 模块设计

### 模块 A：`presets.py`

**职责**：预设与两条轴之间的双向映射、循环顺序、显示名。纯函数、无状态、无 IO。
**对外接口**：上面那五个名字。
**依赖**：`permission.models`。

### 模块 B：`ConversationManager`（`conversation.py`，改）

**职责**：仍是两条轴的唯一持有者。新增预设视角的读写方法。

| 改动 | 说明 |
| --- | --- |
| 删 `_PERM_CYCLE` / `_PERM_LABEL` / `cycle_permission()` | spec F7 |
| `toggle_plan()` → `cycle_preset()` | 两态循环：读当前预设 → `next_preset` → 按 `axes_of` 写回两条轴 |
| 新增 `preset` 属性 | 返回 `preset_of(self.plan_mode)`，spec N5 的单一来源 |
| 新增 `preset_value` 属性 | 供状态栏与行为记录取字符串；工具不可用的 Provider 返回 `None`（与既有 `permission_mode_value` 同口径） |
| 保留 `permission_mode_value` | **不删**：行为记录的启动快照要记真实档位（`bootstrap.py` 在用），它与"给用户看什么"是两回事 |
| 引擎启动档改为 `PERMISSIVE` | 在 `PermissionEngine.load(...)` 的调用点**显式传参**，见下方技术决策 |
| 新增 `_approve_plan_then_exit`（私有） | 包装用户的审批回调，实现 spec F12/F13 |

#### `cycle_preset()` 的写回顺序

```python
target = next_preset(self.preset)
mode, planning = axes_of(target)
self._engine.set_mode(mode)      # 当前两个预设都是 PERMISSIVE，等于空操作
self.plan_mode = planning
```

**明知 `set_mode` 现在是空操作也要写**：它是"预设 = 两条轴的组合"这个结构的
执行体。省掉它的话，将来加一个档位不同的预设时，那个预设会**静默地不生效**
（切过去了但档位没变），而界面上完全看不出来。

#### F12/F13：审批后的归属

审批回调由 TUI 注入（`self.approve_plan_callback`），在两处被交给 Agent 循环
（主对话一处、fork 子对话一处）。**包装函数只写一份**，两处都传它：

```python
def _approve_plan_then_exit(self, plan: str) -> bool:
    approved = self.approve_plan_callback(plan) if ... else False
    if approved:
        self.plan_mode = False        # F12：永久回 auto
    # 未获批 → 什么都不做（F13：留在 plan）
    return approved
```

⚠ **这不会打断正在跑的那一轮**（spec F14）：Agent 循环内部用的是自己的局部
变量 `execution_phase`，而 `plan_mode` 是循环启动时的**入参快照**——回合中途
改它，循环感知不到。这正是我们要的：本回合继续执行，下一回合起是 `auto`。

⚠ **不在这里改 `engine.mode`。** 两个预设的档位相同，本来就不需要改；
而在审批回调里改共享单例的 `mode` 正是 spec 分歧一论证过的禁止形态。

### 模块 C：命令层（`commands/builtins.py`，改）

| 改动 | 说明 |
| --- | --- |
| `/plan` 的 `CommandSpec` → 改名 `/mode`，`aliases=("/plan",)` | spec F6 |
| 删除 `/perm` 的整条 `CommandSpec`（含 `/permissions`、`/allowed-tools`）与 `_handle_perm` | spec F7 |
| `_handle_plan` → `_handle_mode`，转发 `ModeTarget.PRESET` | |

**AC7 自动成立**：c10 起 `/help`、Tab 补全、输入框高亮**全部由同一份注册表驱动**，
删掉 `CommandSpec` 三者一起消失，不需要另外三处改动。

### 模块 D：TUI（`tui/app.py` + `tui/widgets.py`，改）

#### D1：`Shift+Tab` 绑定

```python
Binding("shift+tab", "cycle_preset", "", show=False, priority=True),
```

⚠ **`priority=True` 不可省**：Textual 的 `Screen` 自带一条
`shift+tab → focus_previous`，不抢占的话按下去只会移动焦点。
这与 `app.py` 里 `ctrl+q` / `ctrl+c` 两条既有绑定是同一个手法、同一个理由。

⚠ **动作里不得碰焦点**（spec F5）：只调领域方法 + 刷新状态栏 + 回显，
与 `ctrl+o` 的既有做法一致。

#### D2：状态栏（`compose_status_text`）

```python
# 改前
def compose_status_text(..., plan_mode: bool = False, permission_mode: str | None = None, ...)
# 改后
def compose_status_text(..., preset: str | None = None, ...)
```

两个参数合并成一个。渲染成既有的醒目标记形态：`\[AUTO]` / `\[PLAN]`
（复用现有的 `_MODE_*_MARKUP` 常量思路，方括号仍需转义）。

**删掉原来那段独立的「权限模式：X」**（spec F15）。它在本扩展之后恒为"放行"
——`/perm` 没了，也没有任何配置项能改启动档（已确认 `config.py` 无此字段）——
一个恒定的橘色段是纯噪音，且与 `[AUTO]` 标记重复。

⚠ 橘色高亮**随之一起去掉**。`auto` 是缺省档，把缺省状态常年标成橘色警告，
会稀释掉橘色在别处（上下文逼近上限、确认面板）的分量。

⚠ 成对维护点（`CLAUDE.md` 已登记）：`compose_status_text` 的签名改了，
`StatusBar.update_status` 与 `app.py` 的 `_refresh_status` 必须同步，
且 `_refresh_status` 那**同一份参数组**还要喂给行为记录快照——
**保持单一参数组，不要抄第二份清单**。

#### D3：审批后的状态栏刷新（AC8 的"当场"）

`plan_mode` 在**工作线程**里被 `_approve_plan_then_exit` 改掉，而状态栏在主线程。
审批面板的结算本来就发生在主线程，因此在**审批交互结算之后**顺手刷一次状态栏
即可，不新增任何跨线程推送（`CLAUDE.md` TUI 第二条不变量：活动区数据一律主线程
轮询，不新增子线程到界面的推送）。

### 模块 E：子 Agent 报告（`subagents/report.py`，改）

把本地的 `_MODE_LABELS` 换成引用 `presets.MODE_LABELS`。

**为什么合一而不是各留一份**：`CLAUDE.md` 里 `Layer` 的三份标签表刻意不合一，
理由是合并会让只依赖标准库的 `trace` 叶子包反向依赖 `permission`——**这里没有
那个约束**（`subagents` 本来就依赖 `permission`）。而不合一的代价是真实的：
状态栏说 `auto`、子 Agent 报告说"放行"，用户没法确认那是不是同一个东西。

⚠ **这一列保留三档的完整显示能力**（spec F16）：角色可以声明 `strict` /
`default`，那两个显示名原样保留，只有 `permissive` 的显示名改成 `auto`。

### 模块 F：子进程环境过滤（`tools/run_command.py`，改）

在 `run_shell_captured` 内部构造 `env` 并传给 `Popen`。

**为什么落在这个函数里**：它是全项目**唯一**起 shell 子进程的地方，
两个调用方（`run_command` 工具、`hooks/actions.py` 的命令动作）都经过它
——spec F17b 一处改即达成。

```python
_SENSITIVE_ENV_MARKERS = (
    "API_KEY", "APIKEY", "ACCESS_KEY", "PRIVATE_KEY",
    "SECRET", "TOKEN", "PASSWORD", "PASSWD",
    "CREDENTIAL", "AUTHORIZATION",
)

def filtered_environ() -> tuple[dict[str, str], int]:
    """返回 (过滤后的环境, 被剔除的条目数)。只看变量名，不看取值。"""
```

#### ⚠ 设计期发现的一个比 `GITHUB_TOKEN` 危险得多的坑

**不能把 `AUTH` 单独作为匹配片段**——它会命中 **`SSH_AUTH_SOCK`**。

那是 ssh-agent 的 **socket 路径**，**本身不是密钥**，但它是 ssh 方式认证的
`git push` / `git clone` 能工作的**唯一依靠**。把它剔掉之后，模型跑
`git push` 会得到一句 `Permission denied (publickey)`，而**根因完全看不出来**
——用户会以为是 ssh 配置坏了，去查 `~/.ssh/`，查不到任何异常。

所以上面用的是 `AUTHORIZATION` 而不是 `AUTH`：`ANTHROPIC_AUTH_TOKEN` 这类
由 `TOKEN` 兜住，`SSH_AUTH_SOCK` 自然不命中，**不需要任何豁免名单**。

同理**不用裸的 `KEY`**：它会命中 `SSH_KEY_PATH` 这类"路径不是密钥"的变量。

这与 spec 里 `GITHUB_TOKEN` 那条调研是同一类判断——**区别是 `GITHUB_TOKEN`
过滤掉代价为零（`gh` 走 keyring），而 `SSH_AUTH_SOCK` 过滤掉会真的坏事**。

#### F17c：剔除条数进行为记录

`run_shell_captured` 是纯工具函数、不持有记录器，因此它**返回**条数，
由调用方（`run_command` 工具）填进结果、进而进入既有的 `tool_execute` 事件。
把查询留在调用方，与 `request_cancel()` 返回子 Agent 条数是同一个先例。

⚠ **只记数量，绝不记变量名**——变量名本身就能泄漏"这台机器上配了什么服务"。

## 模块交互

### 切换预设（`Shift+Tab` 或 `/mode`）

```
按键/命令 → app.switch_mode(ModeTarget.PRESET)
              → manager.cycle_preset()
                  → presets.next_preset(preset_of(plan_mode))
                  → engine.set_mode(...) + self.plan_mode = ...
                  ← 返回显示文本
              → app._refresh_status() + 回显
```

### 一次 plan 回合（AC8 的完整链路）

```
用户在 plan 下提需求
  → manager._run(plan_mode=True)          ← 入参快照
      → loop: planning=True，只发只读工具
      → 模型 present_plan
          → _approve_plan_then_exit(plan)
              → TUI 弹审批面板 → 用户批准 → True
              → self.plan_mode = False     ← F12，不影响本轮
          → loop: ctx.approved → execution_phase = True
      → loop: 后续迭代放开全部工具，engine.mode 仍是 PERMISSIVE → 放行
  → 回合结束
下一条消息 → manager._run(plan_mode=False) → auto
```

### 一次命令执行（F17 链路）

```
模型调 run_command
  → 权限管线（①…④，一字不改）→ ALLOW
  → RunCommandTool.execute
      → run_shell_captured(...)
          → filtered_environ() → (env, dropped)
          → Popen(..., env=env)
      ← 返回 (CompletedProcess, dropped)
  → ToolResult 带上 dropped → tool_execute 事件
```

## 文件组织

```
rhinecode/
├── presets.py                 ★新增 —— Preset 枚举、PRESET_AXES、循环、显示名
├── conversation.py            改 —— cycle_preset / preset / 审批包装 / 启动档
├── commands/
│   ├── models.py              改 —— ModeTarget.PLAN→PRESET，删 PERMISSION
│   └── builtins.py            改 —— /mode（别名 /plan），删 /perm 整条
├── tui/
│   ├── app.py                 改 —— shift+tab 绑定、switch_mode 分支、_refresh_status
│   └── widgets.py             改 —— compose_status_text 签名与渲染
├── subagents/report.py        改 —— 引用 presets.MODE_LABELS
└── tools/run_command.py       改 —— filtered_environ + Popen(env=...)

docs/extensions/auto-plan/     ★新增 —— spec/plan/task/checklist
CLAUDE.md                      改 —— 能力表不动；安全边界三处 + 已知项 #4
```

## 技术决策

| 决策点 | 选择 | 理由 |
| --- | --- | --- |
| 预设放哪 | 新增顶层叶子模块 `presets.py` | 只依赖 `permission.models`，谁都能引；放进 `permission/` 的话，那个包会开始知道"规划阶段"这个与权限无关的概念 |
| 预设是否存状态 | **不存**，从 `plan_mode` 推导 | spec N5。存了就会出现"状态栏说 auto、实际在规划阶段" |
| `PRESET_AXES` 里的档位字段 | **保留**，即便当前两个预设取值相同 | 它是 AC6a 唯一的可断言对象，也是将来加预设时的落点 |
| 启动档怎么改成 `PERMISSIVE` | 在 `conversation.py` 的 `PermissionEngine.load(...)` **调用点显式传参** | **不改 `load` 的默认参数**：大量测试直接构造引擎并依赖 `DEFAULT` 语义，改默认值会静默翻掉一批基线的前提（改动看起来只有一行，实则改了所有人的地基） |
| `/perm` 怎么删 | 删 `CommandSpec` + handler | c10 单一注册来源，`/help`/补全/高亮三处自动跟随 |
| `ModeTarget.PLAN` 改名 | 改成 `PRESET` | 语义变了（从"切一个开关"变成"在两个预设间循环"），留旧名会误导 |
| `permission_mode_value` 是否删 | **保留** | 行为记录的启动快照要记真实档位；"记录什么"与"给用户看什么"是两件事 |
| 状态栏权限模式段 | **删** | 恒为"放行"，与 `[AUTO]` 重复；连带去掉橘色，避免稀释橘色在别处的分量 |
| 子 Agent 报告的档位标签 | **与 `presets.MODE_LABELS` 合一** | 这里没有 `Layer` 三份表那种架构约束，而不一致的代价（同一个档位两个名字）是真的 |
| 环境过滤放哪 | `run_shell_captured` 内部 | 全项目唯一起 shell 的地方，一处改覆盖两个调用方（F17b） |
| 匹配片段用 `AUTHORIZATION` 不用 `AUTH` | 避开 `SSH_AUTH_SOCK` | 剔掉它会让 ssh 方式的 `git push` 失败且根因不可见；用精确片段就不必维护豁免名单 |
| 环境过滤是否可配置 | **不可配置** | spec 未要求；一个"关掉密钥过滤"的开关本身就是个坏东西 |
| 剔除条数怎么进记录 | 由 `run_shell_captured` **返回**，调用方填 | 纯函数不持记录器；与 `request_cancel()` 返回条数同先例 |

## spec 覆盖自检

| spec 需求 | 归属 |
| --- | --- |
| F1 / F2 / F4 | 模块 A（`PRESET_AXES`） |
| F3 | 模块 B（`load` 调用点显式传 `PERMISSIVE`）+ `DEFAULT_PRESET` |
| F5 | 模块 D1 |
| F6 | 模块 C |
| F7 | 模块 C（删 `CommandSpec`）+ 模块 B（删 `cycle_permission`） |
| F8 | 模块 D1 / D2 |
| F9 / F10 / F11 | 不改 `PermissionMode` 枚举与解析路径；模块 E 保留三档显示 |
| F12 / F13 / F14 | 模块 B 的 `_approve_plan_then_exit` |
| F15 / F16 | 模块 D2 / 模块 E |
| F17 / F17a / F17b / F17c | 模块 F |
| F18 | `CLAUDE.md` 三处（见文件组织） |
| N1 | 全程不改 `permission/` 下任何判定逻辑（`engine.py` **一行不动**） |
| N2 | 同上——`_apply_protected` 不在改动范围内 |
| N3 | 同上——①黑名单不在改动范围内 |
| N4 | 不改枚举取值、不改配置解析 |
| N5 | 模块 B 的 `preset` 属性是唯一推导点 |
| N6 | 模块 F 只匹配变量名 |

⚠ **`permission/engine.py` 在本扩展里一行都不改**，这是 N1/N2/N3 最强的保证形态
——不是"我们小心地没改坏"，是"那个文件根本不在改动清单里"。
