# auto / plan 两模式 —— 验收记录

> 对应 [`checklist.md`](checklist.md)。日期：2026-08-14。分支 `perm-auto-plan`。
>
> 每条分「机器判到了什么」与「据此做的判断」两栏，与
> `docs/c11/acceptance/` 同口径。

## 总览

| 类别 | 条数 | 结果 |
| --- | --- | --- |
| A 单测与探针 | 17 | **17/17 通过** |
| B 剧本式端到端 | 5 | **5/5 通过** |
| C 真实模型端到端 | 6 | **待用户授权**（会产生模型调用费用） |
| 文档 | 6 | **6/6 通过** |

全量测试：**2752 项通过，skipped 4**（改动前 2649，净增 103），耗时 215 秒。

---

## 一、实现完整性（A 类）

| # | 判据 | 机器判到了什么 | 判断 |
| --- | --- | --- | --- |
| A1 | 两个预设共用同一权限档 | `tests.test_presets` 11 项全绿，含 `test_both_presets_pin_the_same_permission_mode` | ✅ spec F2 成立 |
| A2 | 来回切换两条轴复原 | `test_axes_round_trip` + `test_cycle_round_trip_restores_both_axes` 均通过 | ✅ AC6a |
| A3 | 启动即 auto | `test_startup_preset_is_auto`：`preset_value == "auto"` **且** `engine.mode == PERMISSIVE` | ✅ AC1。两个都断言，排除「预设写死返回 auto 而档位没变」 |
| A4 | 切换命令与别名等效 | `registry.resolve("/plan") is registry.resolve("/mode")` | ✅ AC6 |
| A5 | 旧命令彻底消失 | `/perm`、`/permissions`、`/allowed-tools` 三个 `resolve` 均为 `None` | ✅ AC7 |
| A6 | 状态栏只显示预设 | `compose_status_text` 三种入参输出：`auto`→含 `[AUTO]`，`plan`→含 `[PLAN]`，`None`→两者皆无；三者**都不含**「权限模式」与 `#FFA500` | ✅ AC11 / F15 |
| A7 | 子 Agent 报告仍显示三档 | `tests.test_subagent_report` 全绿；`permissive` 渲染为 `auto（permissive）` | ✅ AC11 / F16 |
| A8 | 批准回 auto、拒绝留 plan | `test_approved_plan_exits_plan_mode` / `test_rejected_plan_stays_in_plan` | ✅ AC8 / AC9 |
| A9 | 审批不碰 engine.mode | 行为断言 + **结构断言**（源码里不得出现 `set_mode`） | ✅ 见下方「变异测试」 |
| A10 | 敏感变量被剔除 | `tests.test_env_filter` 9 项全绿 | ✅ AC12 |
| A11 | `SSH_AUTH_SOCK` 与常规变量存活 | `test_ssh_auth_sock_survives` / `test_path_like_names_survive` / `test_ordinary_vars_survive` | ✅ 反证 |
| A12 | 过滤只看名不看值 | `test_values_are_never_inspected`：`MY_NOTE="sk-ant-..."` 未被剔除 | ✅ N6 |

### ⚠ A9 的变异测试（本轮最值得记的一条）

实现期发现 **A9 原本的行为断言测不到它声称要测的东西**。

两个预设的档位本来就相同（都是 `PERMISSIVE`），所以最可能出现的错法
——在审批路径里补一句 `set_mode(PERMISSIVE)`——**在行为上看不出任何区别**。

于是补了一条结构断言，并**实际植入那句代码验证过**：

```
植入 self._engine.set_mode(presets.PRESET_AXES[presets.Preset.AUTO][0]) 之后：
  test_approval_does_not_touch_engine_mode        → 仍然 PASS   ← 行为断言测不到
  test_approval_path_does_not_mention_set_mode    → FAIL        ← 结构断言抓到了
还原后 13 项全绿。
```

结论：**这类不变量只有结构护栏挡得住**，与 `test_env_filter.py::SingleChokepointTest`
同型。已登记进 `CLAUDE.md` 成对维护点。

## 二、集成（A 类）

| # | 判据 | 机器判到了什么 | 判断 |
| --- | --- | --- | --- |
| A13 | 过滤在唯一落点上 | `test_both_shell_callers_are_covered`：`run_shell_captured` 内含 `filtered_environ()` 与 `env=env`；`hooks/actions.py` 不含 `subprocess.Popen` | ✅ AC12a / F17b |
| A14 | 剔除条数可见 | 真机跑 `echo hello`，`dropped_env=0` 时那行提示**不出现**（无噪音）；探针注入 3 个密钥变量时 `filtered_environ()` 返回 `dropped=2`（`SSH_AUTH_SOCK` / `SSH_KEY_PATH` 未计入） | ✅ AC13 |
| A15 | 角色声明的更严档位仍生效 | `narrower_mode(PERMISSIVE, STRICT) == STRICT`、`(PERMISSIVE, DEFAULT) == DEFAULT`、`(PERMISSIVE, PERMISSIVE) == PERMISSIVE` | ✅ AC10 / F10 |
| A16 | **权限判定逻辑零改动** | 见下 | ✅ N1 / N2 / N3 |
| A17 | `Shift+Tab` 抢占成立 | `RhineApp.BINDINGS` 里该条 `action='cycle_preset'`、`priority=True`；Textual 的 `Screen` 自带那条是 `priority=False` | ✅ F5（真机侧见 B3） |

### A16 的硬证据：AST 逐函数比对

不是「看 diff 觉得只改了注释」，而是把 `main` 与本分支的 `engine.py` 解析成
AST、**去掉 docstring 之后**逐函数取哈希比对：

```
engine.py 函数数: 16 -> 16
函数体(去 docstring)有变化的: NONE
  [OK] _decide_core
  [OK] _apply_protected
  [OK] decide
  [OK] derive
  [OK] set_mode
```

`git diff main -- rhinecode/permission/engine.py` 的全部内容只有两处：
`:ivar mode:` 的说明与 `set_mode` 的 docstring。

**②″保护路径收紧器（`_apply_protected`）逐字未动**——这是 spec N2
「auto 档下模型改不了自己配置」那条承诺最强的形态。

## 三、编译与测试（A 类）

| 判据 | 结果 |
| --- | --- |
| `python -m compileall rhinecode tests` | 无错误 |
| `python -m unittest discover -s tests` | **2752 通过 / skipped 4 / 215 秒** |
| 总数不少于改动前 | 2649 → 2752（**净增 103**） |

## 四、剧本式端到端（B 类）

宿主：`--mode scripted`，真实 `build_app` + 真实 TUI + 真实权限管线，只有模型是假的。

| # | 场景 | 机器判到了什么 | 判断 |
| --- | --- | --- | --- |
| B1 | `/mode` 两态循环 | 状态栏 `… \| [PLAN] \| 上下文：0%` | ✅ 且**整行不含「权限模式」** |
| B2 | `/plan` 别名等效 | 再送一次 → `[AUTO]` | ✅ 与 B1 逐字对称 |
| B3 | `Shift+Tab` 真实按键 | `keys shift+tab` → `[PLAN]`；再按 → `[AUTO]` | ✅ **`priority=True` 真的抢到了键** |
| B3c | 焦点未被夺走 | `status` 报 `focused: "InputBar"` | ✅ F5 |
| B4 | `/perm` 成未知命令 | 历史区：`未知命令：/perm。输入 /help 查看可用命令。` | ✅ AC7，**消息不进 AI** |
| B5 | auto 下写文件不弹面板 | `terminal=idle`、`panel=null`、`quiescent=true`；`x.txt` 内容为 `hi`；判定记录 `write_file → allow @ mode`，`mode_downgraded=False` | ✅ AC2 的剧本侧预演 |

> B3 必须走 `keys` 而不是直接调命令：它要验的是抢键有没有成立，
> 调命令会绕过整个按键分发，抢没抢到都会绿。

## 五、真实模型端到端（C 类）—— **待授权**

以下六条需要有效凭据、会真的调用模型并产生费用，**留给用户决定何时跑**：

| # | 场景 | 关键取证 |
| --- | --- | --- |
| C1 | auto 下连改三个文件，一次面板都不弹 | 三条 `tool_execute` 均 executed，期间无 ASK |
| C2 | auto 下跑测试命令，不弹面板 | `run_command` 判定为 `allow @ mode` |
| C3 | **auto 下写 `.rhinecode/hooks.yaml` 仍弹面板** | 判定 `ask @ protected`，面板**无「永久放行」** |
| C4 | **auto 下 `rm -rf /` 被拒绝** | 判定 `deny @ blacklist`，循环未终止 |
| C5 | 完整 plan 生命周期 | 规划阶段零 write；获批后同回合执行；状态栏当场变 `[AUTO]`；下一条消息不再要求提计划 |
| C6 | 环境变量真机不可见 | 打印环境的命令输出里无密钥变量、有 `PATH` |

⚠ **C3 与 C4 是两个方向的反证，一条都不能省** —— 其余各条只能证明「功能做出来了」，
只有这两条能证明 **`auto` 仍然有边界**。它们**不能用剧本模式替代**：剧本里的
模型行为是我们自己写的，用它验「模型真的会去碰保护路径」等于自己给自己出题。

B5 已在剧本侧预演了 C1/C2 的机制（`allow @ mode`、无面板），
但**不能替代 C1–C6** —— 真机要看的是真实模型在这个配置下会做什么。

## 六、文档（AC14）

| 判据 | 机器判到了什么 | 判断 |
| --- | --- | --- |
| C13 第 ④ 条已改写 | `n=1, m=2`（该短语的唯一一次出现被「原文」标记包住） | ✅ 见下方说明 |
| 安全边界总述已改 | 五层防御第 4 条整段重写，写明缺省是放行档、仍会弹面板的只剩三类 | ✅ |
| 已知项 #4 补 Windows | 含「Claude Code 与 Codex 都不支持原生 Windows 沙箱」 | ✅ |
| 命令清单与扩展索引 | `CLAUDE.md` / `README.md` / `capabilities.md` 里 `/perm` 已消失、`/mode` 已登记；扩展清单有 auto-plan 一行 | ✅ |
| 成对维护点新增 | 含 `PRESET_AXES`、`set_mode` 结构护栏、环境变量片段三条 | ✅ |
| todo 收尾 | `docs/todo/` 序号 1–7 连续无缺口；`2-perm-auto-plan.md` 已删 | ✅ |

### ⚠ 一条判据在实施期被改过

AC14 原判据是 `grep "缺省配置下子 Agent 实际只能做只读" CLAUDE.md` **无输出**。

实际实现时把那句原文**作为「已被推翻的原文」引用保留了**——本项目通篇的做法
是留下「当初为什么那样」（`docs/c11/` 保留原始四份文档是同一个理由），
直接删掉等于把这段历史扔了。

于是那条 grep 会命中一次。**要求它无输出就等于要求删掉历史**，因此判据改成
看语义：那句话不得作为**当前有效的陈述**出现，机器侧等价判据是「该短语的
每一次出现，上方 3 行内都有『原文』标记」。判 `m >= n` 而非 `m == n`
——`-B3` 取的是上下文块，块里可能有不止一行带「原文」二字（实测 2 比 1），
要求相等会红在一个与判据无关的地方。

## 实施期的四处偏离与发现

记在这里是因为它们都改变了「按文档做会得到什么」，看历史的人有理由问。

1. **`plan` 不动权限档**（spec F2，与 todo 原文分歧）。除了「权限档里没有
   『只读』这个值」之外还有条硬的：**获批发生在回合中途**，动档位就要从
   Agent 循环里改单实例共享的引擎，那是 `CLAUDE.md` 明令禁止的形态。

2. **`MODE_LABELS` 三档统一带上 YAML 值**（`auto（permissive）`）。原设计只让
   `permissive` 显示成 `auto`，但该表**唯一的消费者**是子 Agent 报告，
   而那份报告的用途正是帮用户对照角色定义——只显示 `auto` 的话，
   用户不知道 `permission_mode:` 该填什么（YAML 里没有 `auto` 这个取值）。

3. **环境变量片段不能用裸 `AUTH`**（plan 期发现）。它会命中 `SSH_AUTH_SOCK`
   ——ssh-agent 的 socket **路径**、不是密钥，却是 ssh 方式 `git push` 的唯一
   依靠。剔掉后报 `Permission denied (publickey)` 而根因完全不可见。
   ⚠ **这是推演出的隐患，不是实测复现的**：本机 Windows 用命名管道，
   该变量未设置。匹配规则本身是确定的，一到 Git Bash + ssh-agent 环境就会中招。

4. **修掉一处既有的假绿**：`test_e2e_protected._permissive()` 靠送两次 `/perm`
   切档，命令删除后它什么也没做，却**没有变红**——`send` 本身照常成功
   （未知命令只被本地提示掉），且放行档恰好成了新的启动缺省。
   已改为起宿主时 `--permission-mode permissive`，前提写在命令行上、看得见。

另有一处**改动前就已过期**的注释顺手修了：`commands/builtins.py` 的 docstring
写着「12 条规范命令 + 8 个别名」，实测早已是 15 条（c12/c13/c15 加命令都没更新它）。
改成不写条数——一个会静默漂移的计数比没有计数更容易误导人。
