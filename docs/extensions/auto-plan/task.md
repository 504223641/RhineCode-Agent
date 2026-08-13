# 只留 `auto` / `plan` 两个模式 Tasks

> 对应 [`spec.md`](spec.md) 与 [`plan.md`](plan.md)。共 **13 个任务**。
>
> 每个任务的「验证」都要**真的跑一遍**再标完成。本项目全量测试约 3.5 分钟，
> 所以除最后的收尾任务外，各任务只跑与自己相关的用例。

## 文件清单

| 操作 | 文件 | 职责 |
| --- | --- | --- |
| **新建** | `rhinecode/presets.py` | `Preset` 枚举、`PRESET_AXES`、循环、显示名（叶子模块） |
| **新建** | `tests/test_presets.py` | 预设层的纯逻辑护栏 |
| **新建** | `tests/test_auto_plan_integration.py` | 跨层护栏：启动档、审批归属、`/perm` 已消失 |
| **新建** | `tests/test_env_filter.py` | 环境变量过滤的护栏（含两条反证） |
| 修改 | `rhinecode/conversation.py` | `cycle_preset` / `preset` / 审批包装 / 启动档；删 `cycle_permission` |
| 修改 | `rhinecode/commands/models.py` | `ModeTarget.PLAN`→`PRESET`，删 `PERMISSION` |
| 修改 | `rhinecode/commands/builtins.py` | `/mode`（别名 `/plan`），删 `/perm` 整条 |
| 修改 | `rhinecode/tui/widgets.py` | `compose_status_text` 签名与渲染 |
| 修改 | `rhinecode/tui/app.py` | `shift+tab` 绑定、`switch_mode` 分支、`_refresh_status` |
| 修改 | `rhinecode/subagents/report.py` | 引用 `presets.MODE_LABELS` |
| 修改 | `rhinecode/tools/run_command.py` | `filtered_environ` + `Popen(env=...)` + 返回剔除条数 |
| 修改 | `rhinecode/permission/engine.py` | **仅两处 docstring** 的 `/perm` 提法 |
| 修改 | `rhinecode/permission/config.py`、`protected.py`、`agent/loop.py`、`subagents/runner.py`、`commands/models.py` | 陈旧 `/perm` 引用 |
| 修改 | 既有测试 5 个文件 | 适配签名与命令变更（见 T10） |
| 修改 | `CLAUDE.md` | 安全边界三处 + 已知项 #4 |
| 修改 | `docs/internals/capabilities.md`、`README.md` | 命令与模式说明 |
| 删除 | `docs/todo/2-perm-auto-plan.md` | 做完即删 |
| 重命名 | `docs/todo/3..8-*.md` | 序号重排 |

---

## T1: 新增预设层模块

**文件：** `rhinecode/presets.py`（新建）
**依赖：** 无

**步骤：**
1. 建模块，写模块级 docstring：说明「预设是两条既有轴上的一个命名坐标，
   本模块无状态、纯映射」，并写下 plan.md 里那段「推导退化成只看 `plan_mode`
   是因为当前两个预设共用同一档位，`PRESET_AXES` 的档位字段不是冗余」。
2. 定义 `Preset(str, Enum)`：`AUTO = "auto"`、`PLAN = "plan"`。
3. 定义 `PRESET_AXES: dict[Preset, tuple[PermissionMode, bool]]`，两项。
   注释写明「这张表是『预设=两条轴组合』的唯一落点，别当冗余删」。
4. 定义 `PRESET_CYCLE`（两态循环）与 `DEFAULT_PRESET = Preset.AUTO`。
5. 定义 `MODE_LABELS`：三档显示名，`PERMISSIVE` 映射为 `"auto"`，
   注释说明「刻意与预设同名——auto 预设内部就是这个档位」。
6. 实现 `preset_of(plan_mode: bool) -> Preset`、
   `axes_of(preset) -> tuple[PermissionMode, bool]`、
   `next_preset(preset) -> Preset`，三个都写完整 docstring。

**验证：**
```bash
python -c "from rhinecode.presets import Preset, PRESET_AXES, axes_of, next_preset, preset_of; \
print(axes_of(Preset.AUTO), axes_of(Preset.PLAN)); \
print(next_preset(Preset.AUTO), preset_of(True))"
```
期望：两个预设的档位都是 `PermissionMode.PERMISSIVE`，规划阶段分别 `False`/`True`；
`next_preset(AUTO)` 得 `PLAN`；`preset_of(True)` 得 `PLAN`。

---

## T2: 预设层的纯逻辑护栏

**文件：** `tests/test_presets.py`（新建）
**依赖：** T1

**步骤：**
1. `test_both_presets_pin_the_same_permission_mode`：断言 `PRESET_AXES` 里两个
   预设的档位**都是** `PERMISSIVE`——这条钉住 spec F2（plan 不动档位）。
2. `test_cycle_is_a_two_state_loop`：从 `AUTO` 连按两次回到 `AUTO`，
   且循环覆盖全部 `Preset` 成员（遍历枚举，新增预设时当场红）。
3. `test_axes_round_trip`：`preset_of(axes_of(p)[1]) == p`，遍历全部成员。
   这条是 spec **AC6a** 的可断言对象。
4. `test_permissive_is_displayed_as_auto`：`MODE_LABELS[PERMISSIVE] == "auto"`，
   且 `STRICT` / `DEFAULT` 仍有各自的显示名（spec F16 的三档能力）。
5. `test_default_preset_is_auto`：`DEFAULT_PRESET is Preset.AUTO`（spec F3）。

**验证：** `python -m unittest tests.test_presets -v` 全绿。

---

## T3: 协调层接入预设

**文件：** `rhinecode/conversation.py`
**依赖：** T1

**步骤：**
1. 引入 `presets` 模块。
2. **删除** `_PERM_CYCLE`、`_PERM_LABEL` 两个类常量与 `cycle_permission()` 方法。
3. 把 `toggle_plan()` 改名为 `cycle_preset()`，实现改为：
   读 `self.preset` → `next_preset` → `axes_of` → 依次写回
   `self._engine.set_mode(mode)` 与 `self.plan_mode = planning` → 返回
   `f"模式：{target.value}"`。
   注释写明「`set_mode` 当前是空操作也要写，理由见 plan.md」。
   工具不可用的 Provider 仍返回原提示（保持既有分支）。
4. 新增 `preset` 属性：`return presets.preset_of(self.plan_mode)`。
   docstring 注明这是 spec N5 的**唯一推导点**。
5. 新增 `preset_value` 属性：工具不可用返回 `None`，否则 `self.preset.value`
   （与既有 `permission_mode_value` 同口径）。
6. **保留** `permission_mode_value`，在其 docstring 补一句
   「供行为记录的启动快照使用；给用户看的是 `preset_value`」。
7. 引擎构造点（`PermissionEngine.load(...)`）显式加 `mode=PermissionMode.PERMISSIVE`，
   注释写明「spec F3：启动即 auto。**刻意不改 `load` 的默认参数**——大量测试
   直接构造引擎并依赖 `DEFAULT` 语义」。

**验证：**
```bash
python -m compileall rhinecode/conversation.py rhinecode/presets.py
grep -n "cycle_permission\|_PERM_CYCLE\|_PERM_LABEL" rhinecode/conversation.py
```
期望：编译通过；grep **无输出**。

---

## T4: 命令层——`/mode` 上线，`/perm` 下线

**文件：** `rhinecode/commands/models.py`、`rhinecode/commands/builtins.py`
**依赖：** T3

**步骤：**
1. `models.py`：`ModeTarget.PLAN` 改名为 `PRESET`（取值 `"preset"`），
   **删除** `PERMISSION` 成员；更新枚举 docstring。
2. `models.py`：第 23 行那句 UI 类命令的举例里，`/plan、/perm` 改成 `/mode`。
3. `builtins.py`：`_handle_plan` 改名 `_handle_mode`，转发 `ModeTarget.PRESET`。
4. `builtins.py`：`/plan` 那条 `CommandSpec` 改为
   `name="/mode"`、`aliases=("/plan",)`、`usage="/mode"`，
   描述改成「在 auto 与 plan 两个模式间切换（等价于 Shift+Tab）」。
5. `builtins.py`：**整条删除** `/perm` 的 `CommandSpec` 与 `_handle_perm` 函数。

**验证：**
```bash
python -c "
from rhinecode.commands.builtins import build_builtin_registry
r = build_builtin_registry()
names = {s.name for s in r._specs}
al = {a for s in r._specs for a in s.aliases}
print('/mode 在:', '/mode' in names)
print('/plan 是别名:', '/plan' in al)
print('/perm 残留:', {'/perm','/permissions','/allowed-tools'} & (names|al))
"
```
期望：前两项 `True`，第三项 `set()`。
（`build_builtin_registry()` 无参；`_specs` 是内置与 Skill 两个列表的只读拼接属性。）

---

## T5: 状态栏渲染

**文件：** `rhinecode/tui/widgets.py`
**依赖：** T1

**步骤：**
1. `compose_status_text` 签名：删 `plan_mode: bool` 与 `permission_mode: str|None`，
   新增 `preset: str|None = None`。
2. 渲染：`preset` 为 `"plan"` 时用 `\[PLAN]` 标记，否则 `\[AUTO]`
   （沿用既有 `_MODE_*_MARKUP` 的形态与转义，方括号必须 `\[`）。
   `preset is None`（工具不可用的 Provider）时**整段不显示**。
3. **删除**原来那段独立的「权限模式：X」及其橘色高亮分支。
4. 同步 `StatusBar.update_status` 的签名与转发（成对维护点）。
5. 若 `_MODE_DEFAULT_MARKUP` 这类常量名里带 `DEFAULT` 字样，改成 `AUTO` 口径。

**验证：**
```bash
python -c "
from rhinecode.tui.widgets import compose_status_text as c
print(repr(c('deepseek','m','off',preset='auto')))
print(repr(c('deepseek','m','off',preset='plan')))
print(repr(c('deepseek','m','off')))
"
```
期望：第一行含 `AUTO`、第二行含 `PLAN`、第三行两者都无；三行**都不含**「权限模式」。

---

## T6: `Shift+Tab` 绑定与 `switch_mode` 分支

**文件：** `rhinecode/tui/app.py`
**依赖：** T3、T4、T5

**步骤：**
1. `BINDINGS` 加一条
   `Binding("shift+tab", "cycle_preset", "", show=False, priority=True)`。
   注释写明「`priority=True` 不可省：Textual 的 `Screen` 自带
   `shift+tab → app.focus_previous`（`priority=False`），不抢占就只会移动焦点」。
2. 实现 `action_cycle_preset`：调 `switch_mode(ModeTarget.PRESET)` → 显示返回文本
   → `self._refresh_status()`。**不得碰焦点**（spec F5）。
3. `switch_mode`：`ModeTarget.PLAN` 分支改为 `PRESET` 并调 `cycle_preset()`；
   **删除** `PERMISSION` 分支；未知值仍明确抛错。
4. `_refresh_status`：改为取 `self._manager.preset_value` 传给
   `compose_status_text(preset=...)`，删掉 `plan_mode` 与 `permission_mode` 两个取值。
   ⚠ **保持单一参数组**——同一份参数还要喂给行为记录快照，不要抄第二份清单。

**验证：**
```bash
python -m compileall rhinecode/tui/app.py
python -c "
from rhinecode.tui.app import RhineApp
b=[x for x in RhineApp.BINDINGS if getattr(x,'key','')=='shift+tab']
print(b[0].action, b[0].priority)
"
```
期望：编译通过；输出 `cycle_preset True`。

---

## T7: 审批后的归属（F12/F13/F14）

**文件：** `rhinecode/conversation.py`
**依赖：** T3

**步骤：**
1. 新增私有方法 `_approve_plan_then_exit(plan) -> bool`：
   调用 `self.approve_plan_callback`（为 `None` 时返回 `False`）；
   返回 `True` 时置 `self.plan_mode = False`；返回 `False` 时**什么都不做**。
2. docstring 写清三条：F12（获批永久回 auto）、F13（被拒留在 plan）、
   F14（**不影响正在跑的那一轮**——循环用的是入参快照与自己的局部
   `execution_phase`），以及「**刻意不在这里改 `engine.mode`**」的理由。
3. 把主对话与 fork 子对话**两处**传给 Agent 循环的 `self.approve_plan_callback`
   改成传 `self._approve_plan_then_exit`。
   ⚠ 两处引用**同一个方法**，不要各写一份逻辑。
4. `tui/app.py`：审批交互结算之后追加一次状态栏刷新（AC8 的「当场」）。
   ⚠ 不新增任何从工作线程到界面的推送。

**验证：**
```bash
grep -n "_approve_plan_then_exit" rhinecode/conversation.py
```
期望：**3 处**（1 处定义 + 2 处传参）。

---

## T8: 子 Agent 报告的档位标签合一

**文件：** `rhinecode/subagents/report.py`
**依赖：** T1

**步骤：**
1. 删除本地的 `_MODE_LABELS`，改为 `from rhinecode.presets import MODE_LABELS`。
2. 两处 `.get(...)` 调用点改用新名字。
3. 加注释说明「与状态栏共用一份显示名是刻意的：不一致的话，同一个档位在两处
   叫两个名字，用户没法确认它们是不是同一个东西」。

**验证：**
```bash
python -m unittest tests.test_subagent_report -v 2>&1 | tail -5
```
期望：全绿（若有用例逐字断言「放行」，按 T10 一并适配）。

---

## T9: 子进程环境过滤

**文件：** `rhinecode/tools/run_command.py`
**依赖：** 无（可与 T1–T8 并行）

**步骤：**
1. 定义模块级常量 `_SENSITIVE_ENV_MARKERS`，取值为：
   `API_KEY`、`APIKEY`、`ACCESS_KEY`、`PRIVATE_KEY`、`SECRET`、`TOKEN`、
   `PASSWORD`、`PASSWD`、`CREDENTIAL`、`AUTHORIZATION`。
2. 常量上方写⚠注释，**两条都要写**：
   - **不用裸 `AUTH`**——会命中 `SSH_AUTH_SOCK`，那是 ssh-agent 的 socket
     **路径**不是密钥，剔掉会让 ssh 方式的 `git push` 报
     `Permission denied (publickey)` 而**根因不可见**；
   - **不用裸 `KEY`**——会命中 `SSH_KEY_PATH` 这类"路径不是密钥"的变量。
3. 实现 `filtered_environ() -> tuple[dict[str, str], int]`：
   按**变量名**大写后做子串匹配（spec N6：不看取值），返回过滤后的环境与剔除条数。
4. `run_shell_captured`：内部调 `filtered_environ()`，把 env 传给 `Popen`；
   返回值改为 `tuple[CompletedProcess, int]`（附带剔除条数）。
   在 docstring 里补一节说明这条过滤及其理由。
5. 更新**两个**调用方：`run_command.py` 自己（把条数填进 `ToolResult`，
   进而进入既有的 `tool_execute` 事件）与 `hooks/actions.py`（解包，条数暂不消费）。
   ⚠ **只记数量，绝不记变量名**——变量名本身就泄漏「这台机器配了什么服务」。

**验证：**
```bash
python -c "
import os
os.environ['MY_API_KEY']='x'; os.environ['SSH_AUTH_SOCK']='/tmp/s'; os.environ['GITHUB_TOKEN']='y'
from rhinecode.tools.run_command import filtered_environ
env,n = filtered_environ()
print('剔除数:', n)
print('API_KEY 已剔:', 'MY_API_KEY' not in env)
print('TOKEN 已剔:', 'GITHUB_TOKEN' not in env)
print('SSH_AUTH_SOCK 保留:', 'SSH_AUTH_SOCK' in env)
print('PATH 保留:', 'PATH' in env)
"
```
期望：剔除数 ≥2；前两项 `True`；**后两项 `True`**（这两条是反证）。

---

## T10: 环境过滤的护栏

**文件：** `tests/test_env_filter.py`（新建）
**依赖：** T9

**步骤：**
1. `test_secret_like_names_are_dropped`：遍历 `_SENSITIVE_ENV_MARKERS`，
   为每个片段造一个变量名，断言都被剔除（新增片段自动被覆盖）。
2. `test_ssh_auth_sock_survives`（**反证**）：设 `SSH_AUTH_SOCK`，断言它**还在**。
   用例注释写明「它是 socket 路径不是密钥，剔掉会让 ssh 的 git push 失败且
   根因不可见」——这条是本任务的分辨力所在。
3. `test_ordinary_vars_survive`（**反证**）：`PATH` / `HOME` / `SSH_KEY_PATH` 仍在。
4. `test_values_are_never_inspected`：造一个名字普通、取值长得像密钥的变量
   （如 `MY_NOTE="sk-ant-xxxx"`），断言它**没被剔除**（spec N6）。
5. `test_both_shell_callers_are_covered`：结构护栏——断言 `run_shell_captured`
   的实现里出现 `filtered_environ`，且 `hooks/actions.py` 与 `run_command.py`
   都不自己调 `subprocess.Popen`（spec F17b 的落点唯一性）。

**验证：** `python -m unittest tests.test_env_filter -v` 全绿。

---

## T11: 跨层护栏

**文件：** `tests/test_auto_plan_integration.py`（新建）
**依赖：** T3、T4、T6、T7

**步骤：**
1. `test_startup_preset_is_auto`：造一个工具可用的管理器，断言
   `preset_value == "auto"` 且 `permission_mode_value == "permissive"`（spec F3/AC1）。
2. `test_perm_command_is_gone`：断言注册表里 `/perm`、`/permissions`、
   `/allowed-tools` 三个名字**都不存在**（spec F7/AC7）。
3. `test_cycle_round_trip_restores_both_axes`：切两次回到出发点，断言
   `engine.mode` 与 `plan_mode` 都与出发时**逐字相同**（spec AC6a）。
4. `test_approved_plan_exits_plan_mode`：置 `plan_mode=True`，注入一个返回 `True`
   的审批回调，调 `_approve_plan_then_exit`，断言 `plan_mode` 变 `False`（F12）。
5. `test_rejected_plan_stays_in_plan`（**反证**）：同上但回调返回 `False`，
   断言 `plan_mode` **仍为 `True`**（F13）。
6. `test_approval_does_not_touch_engine_mode`（**反证**）：上面两条前后
   `engine.mode` 都是 `PERMISSIVE`——钉住「审批回调不改共享单例的档位」。
7. `test_strict_role_still_narrows`：一个声明 `permission_mode: strict` 的角色，
   在 auto 主对话下生效档位仍是 `strict`（spec F10/AC10——`dontAsk` 手法下
   那个档位仍可经角色定义抵达）。

**验证：** `python -m unittest tests.test_auto_plan_integration -v` 全绿。

---

## T12: 适配既有测试与清理陈旧引用

**文件：** `tests/test_command_tui.py`、`test_command_builtins.py`、`test_skill_tui.py`、
`test_tool_display.py`、`test_tui_detail_level.py`、`test_tui_quit.py`；
`rhinecode/permission/engine.py`、`config.py`、`protected.py`、
`rhinecode/agent/loop.py`、`rhinecode/subagents/runner.py`
**依赖：** T3–T9

**步骤：**
1. 测试侧：把 `compose_status_text(plan_mode=..., permission_mode=...)` 的调用
   改成 `preset=...`；`cycle_permission` / `toggle_plan` 的断言改为 `cycle_preset`；
   `ModeTarget.PERMISSION` / `.PLAN` 的引用改为 `.PRESET`。
2. `permission/engine.py`：**仅两处 docstring**（`:ivar mode:` 与 `set_mode`）
   里的 `/perm` 提法改掉，说明档位现在由预设层与角色定义决定。
   ⚠ **不得改动任何可执行语句**——`decide` / `_decide_core` / `_apply_protected`
   / `derive` 逐字不变（spec N1/N2/N3）。
3. `permission/config.py:71`、`permission/protected.py:31`：把「`/perm` 切严格档」
   的表述改成「角色声明 `permission_mode: strict`」——那是本扩展之后**仍然成立**
   的抵达路径，论证本身不变。
4. `agent/loop.py:1172`：那句「`/perm 严格` 不是这个用途」改写——命令没了，
   但结论（想禁用协作工具要写 `deny` 规则）不变。
5. `subagents/runner.py:179`：那段「用户可能在排队期间按 `/perm` 切档，
   所以取值型会拿到陈旧档位」的理由**需要重新评估**——`/perm` 删除后主对话档位
   在运行期不再变化。**若结论仍成立就改措辞、若不再成立就如实写明**，
   ⚠ 但**不要顺手把 callable 改回取值型**（那是另一件事，超出本扩展范围）。

**验证：**
```bash
grep -rn -- "/perm\b" rhinecode/ tests/ | grep -v __pycache__ | grep -v "permissions.yaml\|permissions.local"
python -m unittest tests.test_command_tui tests.test_command_builtins tests.test_skill_tui \
    tests.test_tool_display tests.test_tui_detail_level tests.test_tui_quit 2>&1 | tail -5
```
期望：grep 无残留（`permissions.yaml` 那类路径不算）；六个测试文件全绿。

---

## T13: 文档同步与 todo 收尾

**文件：** `CLAUDE.md`、`docs/internals/capabilities.md`、`README.md`、`docs/todo/`
**依赖：** T1–T12 全部完成

**步骤：**
1. `CLAUDE.md` 斜杠命令清单：删 `/perm` 那条，`/plan` 那条改写为 `/mode`
   （说明两态循环与 `Shift+Tab` 等价）。
2. `CLAUDE.md` 安全边界 **C13 第 ④ 条**：「缺省配置下子 Agent 实际只能做只读的事」
   **不再成立**，改写为：缺省档是 auto，子 Agent 生效档位是
   `min(主对话档, 角色声明档)` = auto，因此它能写文件、能跑命令，且是后台、
   并行、非交互、用户不在场。**同时给出保持旧行为的办法**：给角色声明
   `permission_mode: strict` 或 `default`。注明这是设计后果不是回归。
3. `CLAUDE.md` 安全边界总述里描述**缺省体验**的段落（五层防御那段的第 4、5 条）：
   缺省不再是「灰色地带交人工确认」，据实改写。
4. `CLAUDE.md` **已知项 #4**：补一句「Claude Code 与 Codex **都不支持原生
   Windows 沙箱**」（前者官方原话 *Native Windows is not supported*，
   后者文档只写 macOS Seatbelt 与 Linux Landlock/seccomp）。
5. `CLAUDE.md` 扩展清单：加一行 auto-plan 扩展，指向本目录。
6. `CLAUDE.md` 成对维护点：新增一条——「新增预设 → `presets.py` 的
   `PRESET_AXES` + `PRESET_CYCLE`」，并说明 `PRESET_AXES` 的档位字段不是冗余。
7. `docs/internals/capabilities.md` 与 `README.md`：模式与命令说明同步。
8. **删除** `docs/todo/2-perm-auto-plan.md`。
9. `docs/todo/` 序号重排：`3`→`2`、`4`→`3`、…、`8`→`7`，
   **用 `git mv` 保住历史**；同步 `docs/todo/README.md` 的清单表格、
   「权限模式两条」小节、「哪些能同时开工」表里的编号引用。

**验证：**
```bash
grep -rn "/perm" CLAUDE.md README.md docs/internals/ | grep -v permissions.yaml
grep -n "缺省配置下子 Agent 实际只能做只读" CLAUDE.md
ls docs/todo/
python -m compileall rhinecode tests && python -m unittest discover -s tests 2>&1 | tail -5
```
期望：前两个 grep 无输出；`docs/todo/` 序号连续无缺口；**全量测试全绿**。

---

## 执行顺序

```
T1（presets.py）
 ├─→ T2（预设护栏）
 ├─→ T3（协调层）──┬─→ T4（命令层）──┐
 │                 └─→ T7（审批归属）│
 ├─→ T5（状态栏）───────────────────┼─→ T6（Shift+Tab）
 └─→ T8（子Agent报告标签）           │
                                     ▼
T9（环境过滤）─→ T10（环境护栏）    T11（跨层护栏）
                    │                 │
                    └────────┬────────┘
                             ▼
                      T12（适配既有测试 + 清陈旧引用）
                             ▼
                      T13（文档 + todo 收尾）
```

**T9/T10 与 T1–T8 无依赖**，可任意穿插。
**T12 必须在 T3–T9 全部落地之后**——它要一次性对齐所有签名变更。

## 提交节奏

按 `CLAUDE.md` 的约定，一次改动一个 commit：

| commit | 覆盖 |
| --- | --- |
| 1 | T1 + T2（预设层与它的护栏） |
| 2 | T3（协调层接入） |
| 3 | T4（命令层 `/mode` 上线、`/perm` 下线） |
| 4 | T5 + T6（状态栏与 `Shift+Tab`） |
| 5 | T7（审批归属） |
| 6 | T8（子 Agent 报告标签合一） |
| 7 | T9 + T10（环境过滤与护栏） |
| 8 | T11（跨层护栏） |
| 9 | T12（适配既有测试 + 清陈旧引用） |
| 10 | T13（文档 + todo 收尾） |
