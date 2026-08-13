# 只留 `auto` / `plan` 两个模式 Checklist

> 每一项通过**运行代码或观察行为**来验证，聚焦系统行为而非实现细节。
> 对应 [`spec.md`](spec.md) 的 16 条验收标准。

## 谁来验：三类，界线是「要不要花钱、要不要人眼」

本项目的既有约定（见 `docs/c11/acceptance/`）：能无头驱动的一律由实现者跑完，
只有「必须人眼」与「真实模型调用（花用户的钱）」交给用户。

| 类别 | 怎么跑 | 谁 |
| --- | --- | --- |
| **A. 单测与探针** | `unittest` / 一行 `python -c` | 实现者 |
| **B. 剧本式端到端** | `tests/e2e` 宿主 + 瘦客户端，`--mode scripted` | 实现者 |
| **C. 真实模型端到端** | `--mode live`，需有效凭据 | **用户授权后**跑 |

⚠ **C 类的五条是 todo 明确要求「真机跑」的**，其中第 3、4 条是两个方向的反证，
**一条都不能省** —— 缺任何一条就分不清「`auto`」与「什么都不管」。

---

## 一、实现完整性（A 类）

- [ ] **预设层可被调用且两个预设共用同一权限档**（spec F1/F2）
      验证：`python -m unittest tests.test_presets` 全绿，其中
      `test_both_presets_pin_the_same_permission_mode` 通过

- [ ] **来回切换后两条轴逐字复原**（spec F4/**AC6a**）
      验证：`test_axes_round_trip` 与
      `test_cycle_round_trip_restores_both_axes` 均通过

- [ ] **启动即 `auto`**（spec F3/**AC1**）
      验证：`test_startup_preset_is_auto` 通过 —— 断言
      `preset_value == "auto"` 且 `permission_mode_value == "permissive"`

- [ ] **切换命令与别名等效**（spec F6/**AC6**）
      验证：`/mode` 在注册表内、`/plan` 是它的别名，二者产生相同结果

- [ ] **旧的权限档命令彻底消失**（spec F7/**AC7**）
      验证：注册表里 `/perm`、`/permissions`、`/allowed-tools` 三个名字都查不到；
      且源码树内无残留引用（`grep` 无输出）

- [ ] **状态栏只显示预设、不再显示权限档**（spec F15/**AC11**）
      验证：`compose_status_text` 三种入参（`auto` / `plan` / `None`）的输出里，
      前两者分别含 `AUTO` / `PLAN`，三者**都不含**「权限模式」字样

- [ ] **子 Agent 报告仍能显示三个档位**（spec F16/**AC11**）
      验证：`tests.test_subagent_report` 全绿；声明 `strict` 的角色其声明值与
      生效值都显示得出来，且「放行」的显示名已变为 `auto`

- [ ] **审批后回 `auto`、拒绝后留 `plan`**（spec F12/F13/**AC8/AC9**）
      验证：`test_approved_plan_exits_plan_mode` 与
      `test_rejected_plan_stays_in_plan` 均通过

- [ ] **审批不触碰共享引擎的档位**（**反证**，spec 分歧一的论证）
      验证：`test_approval_does_not_touch_engine_mode` 通过 —— 审批前后
      `engine.mode` 都是 `PERMISSIVE`

- [ ] **敏感环境变量被剔除**（spec F17/**AC12**）
      验证：`python -m unittest tests.test_env_filter` 全绿

- [ ] **`SSH_AUTH_SOCK` 与常规变量存活**（**反证**，spec F17a）
      验证：`test_ssh_auth_sock_survives` 与 `test_ordinary_vars_survive` 通过
      —— 没把环境变量删干净

- [ ] **过滤只看变量名、不看取值**（spec N6）
      验证：`test_values_are_never_inspected` 通过 —— 一个名字普通、取值长得像
      密钥的变量**没被剔除**

## 二、集成（A 类）

- [ ] **过滤发生在起子进程的唯一落点上**（spec F17b/**AC12a**）
      验证：`test_both_shell_callers_are_covered` 通过 —— `run_shell_captured`
      内部调用了过滤，且两个调用方都不自己起 `Popen`

- [ ] **剔除条数可见**（spec F17c/**AC13**）
      验证：跑一条命令后，其工具结果里带得出剔除条数，且该数字进入
      `tool_execute` 事件；**行为记录里不出现任何被剔除的变量名**

- [ ] **角色声明的更严档位仍然生效**（spec F9/F10/F11/N4/**AC10**）
      验证：`test_strict_role_still_narrows` 通过 —— auto 主对话下，
      声明 `permission_mode: strict` 的角色其生效档位仍是 `strict`

- [ ] **权限判定逻辑零改动**（spec N1/N2/N3）
      验证：`git diff main -- rhinecode/permission/engine.py` 的改动**只落在
      注释与 docstring 行**，`decide` / `_decide_core` / `_apply_protected` /
      `derive` 四个函数体逐字不变

- [ ] **`Shift+Tab` 能抢占 Textual 的默认焦点绑定**（spec F5）
      验证：`RhineApp.BINDINGS` 里该条的 `action` 为切换预设且 `priority` 为真

## 三、编译与测试（A 类）

- [ ] **全项目编译无错**
      验证：`python -m compileall rhinecode tests` 无 error

- [ ] **全量测试通过**
      验证：`python -m unittest discover -s tests`，
      期望「全绿、skipped 4」，且**总数不少于改动前**（2649 + 本次新增）

## 四、剧本式端到端（B 类）

> 用 `tests/e2e` 的常驻宿主 + 瘦客户端跑，不花钱、可重复。
> ⚠ 从 Git Bash 驱动时必须 `MSYS_NO_PATHCONV=1`，否则 `/mode` 会被 MSYS
> 改写成一个路径、**根本送不到应用**（`CLAUDE.md` 已登记的坑）。

- [ ] **场景 B1：`/mode` 两态循环**
      操作：`send "/mode"` → `screen` 看状态栏 → 再 `send "/mode"` → 再看
      预期：状态栏在 `[AUTO]` 与 `[PLAN]` 之间来回，且**始终不含**「权限模式」

- [ ] **场景 B2：`/plan` 别名等效**
      操作：`send "/plan"` 后观察状态栏
      预期：与 B1 第一次切换的结果**逐字相同**

- [ ] **场景 B3：`Shift+Tab` 走真实按键路径**
      操作：`keys shift+tab` → `screen`
      预期：模式切换发生，**且输入框仍持有焦点**（spec F5 —— 这条是
      `priority=True` 有没有真的抢到手的唯一证据）

- [ ] **场景 B4：`/perm` 已成未知命令**
      操作：`send "/perm"`
      预期：界面提示未知命令并指向 `/help`，**消息不进 AI**

- [ ] **场景 B5：确认面板在 `auto` 下不弹**
      操作：用剧本让模型写一个工作区内的普通文件 → `wait --until quiescent`
      预期：全程 `status` 从未进入 `pending`，文件已落盘

## 五、真实模型端到端（C 类，todo 指定的五条）

> **需用户授权**（会真的调用模型、产生费用）。
> 建议开 `--trace` 跑，事后用阅读器取证。

- [ ] **场景 C1：`auto` 下连改三个文件 —— 一次面板都不弹**（**AC2**）
      取证：三次 `tool_execute` 的 outcome 均为 executed，
      且期间 `permission_decision` 无一条为 ASK

- [ ] **场景 C2：`auto` 下跑测试命令 —— 不弹面板**（**AC3**）
      取证：该次 `run_command` 的判定为 ALLOW @ MODE（④层），无确认交互
      > 这条是本扩展与「只自动批准编辑」那类方案的**分野**

- [ ] **场景 C3：`auto` 下写 `.rhinecode/hooks.yaml` —— 仍然弹面板**（**AC4**，**反证**）
      取证：`permission_decision` 为 **ASK @ PROTECTED**，确认面板真的出现，
      且面板上**没有「永久放行」**选项
      > ②″保护路径收紧器的落点。**这条证明 `auto` 不等于"什么都不管"**

- [ ] **场景 C4：`auto` 下执行 `rm -rf /` —— 被拒绝**（**AC5**，**反证**）
      取证：`permission_decision` 为 **DENY @ BLACKLIST**（①层），
      且 Agent Loop **未终止**，拒绝原因结构化回灌了模型
      > ①黑名单不可被任何档位放开，这是它从 C6 起的既有性质

- [ ] **场景 C5：完整 plan 生命周期**（**AC8/AC9**）
      操作：切 `plan` → 提一个要改文件的需求 → 观察规划阶段 → 批准 → 观察
      预期与取证：
      1. 规划阶段**一个文件都没被修改**（无 write 类 `tool_execute`）
      2. 模型走 `present_plan` 提交计划
      3. 批准后**同一回合内**继续执行并真的改了文件（F14）
      4. **状态栏当场变回 `[AUTO]`**（F12）
      5. 回合结束后再发一条消息，它**不再要求先提计划**

- [ ] **场景 C6：环境变量真的看不见了**（**AC12** 的真机版）
      操作：让模型执行一条打印全部环境变量的命令
      预期：输出里**不含**任何疑似密钥的变量名，但**含** `PATH`

## 六、文档（**AC14**）

- [ ] **三处失准的描述已改**（spec F18）
      验证：`grep -n "缺省配置下子 Agent 实际只能做只读" CLAUDE.md` **无输出**；
      C13 安全边界第 ④ 条已改写为「缺省即 auto，子 Agent 能写文件、能跑命令」，
      **并给出了保持旧行为的办法**（角色声明更严的档位）

- [ ] **安全边界总述里描述缺省体验的段落已据实改写**
      验证：五层防御那段不再声称缺省是「灰色地带交人工确认」

- [ ] **已知项 #4 补上了 Windows 那句**
      验证：该条包含「Claude Code 与 Codex 都不支持原生 Windows 沙箱」

- [ ] **命令清单与扩展索引已同步**
      验证：`CLAUDE.md`、`README.md`、`docs/internals/capabilities.md` 里
      `/perm` 已消失、`/mode` 已登记；扩展清单里有 auto-plan 一行

- [ ] **成对维护点已登记新条目**
      验证：`CLAUDE.md` 成对维护点含「新增预设 → `PRESET_AXES` + `PRESET_CYCLE`」，
      并说明 `PRESET_AXES` 的档位字段不是冗余

- [ ] **todo 收尾完成**
      验证：`docs/todo/2-perm-auto-plan.md` 已删除；
      `docs/todo/` 序号连续无缺口；`README.md` 的清单表格、
      「权限模式两条」小节、「哪些能同时开工」表里的编号引用全部同步

---

## 覆盖对照表

| spec 验收标准 | 落在哪一项 |
| --- | --- |
| AC1 启动即 auto | 一、启动即 `auto` |
| AC2 连改三文件不弹 | 五、C1 |
| AC3 跑命令不弹 | 五、C2 |
| AC4 写 hooks.yaml 仍弹（反证） | 五、C3 |
| AC5 危险命令被拒（反证） | 五、C4 |
| AC6 切换入口等效 | 一、切换命令与别名等效；四、B1/B2/B3 |
| AC6a 来回切换轴复原 | 一、来回切换后两条轴逐字复原 |
| AC7 旧命令消失 | 一、旧的权限档命令彻底消失；四、B4 |
| AC8 批准后回 auto | 一、审批后回 auto；五、C5 |
| AC9 拒绝后留 plan | 一、审批后回 auto（同条用例的反证半边） |
| AC10 既有配置继续生效 | 二、角色声明的更严档位仍然生效 |
| AC11 文案统一 | 一、状态栏 + 子 Agent 报告两项 |
| AC12 敏感变量被剔 | 一、敏感环境变量被剔除；五、C6 |
| AC12a 落点唯一 | 二、过滤发生在起子进程的唯一落点上 |
| AC13 条数可见 | 二、剔除条数可见 |
| AC14 文档同步 | 六、全部六项 |

⚠ **AC4 与 AC5 是全表分辨力最高的两条。** 其余各条只能证明「功能做出来了」，
只有这两条能证明「`auto` 仍然有边界」。它们都在 C 类，**不能用剧本模式替代**
—— 剧本里的模型行为是我们自己写的，用它验「模型真的会去碰保护路径」等于
自己给自己出题（`docs/c11/acceptance/` 记过这个教训）。
