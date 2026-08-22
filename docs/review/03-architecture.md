# 阶段 3 · 架构冲突

> 2026-08-22。配套 [`README.md`](README.md)（问题清单）与 [`00-baseline.md`](00-baseline.md)（机器实测）。
> 本轮只读调研，不改任何产品代码、不写测试。每条结论带 `文件路径:行号`。

本阶段分两半：

| 半边 | 状态 | 在哪 |
| --- | --- | --- |
| **包依赖方向** | ✅ 已做 | [`00-baseline.md` 第 5 节](00-baseline.md) |
| **能力交互接缝** | 本文 | 下面全部内容 |

## 一、承前：依赖方向那一半的结论（摘要）

AST 全量扫描 171 个文件的结论是：**10 个包处于同一强连通分量**
（`agent ↔ context ↔ hooks ↔ mcp ↔ permission ↔ skills ↔ subagents ↔ tools ↔ web ↔ worktree`），
枢纽是 `tools`。这**不是隐患**——`rhinecode/tools/__init__.py` 有 2,799 字节 docstring
把这个结构、为什么不成环、违反后的报错形态逐条写明。缺口只在护栏：
同类约束在 `permission`（`tests/test_classifier_broad.py:219`）与 `trace`
（`tests/test_trace_reader.py:404`）上都有测试钉着，**`tools` 一条都没有**。

**本轮补一条**：`todo` 包声明的「绝不 import `team`」（`CLAUDE.md` 架构表列为 ⚠ 致命不变量）
**同样没有护栏**。全仓只有 4 个测试文件做 AST 解析
（`tests/test_bootstrap.py`、`tests/test_classifier_broad.py`、
`tests/test_hook_zero_regression.py`、`tests/test_tui_keybindings.py`），
没有一个覆盖 `todo`。这与 `README.md` 的 C4（`tools`）是同一形态、同一代价。

---

## 二、能力交互矩阵

### 判定口径

| 记号 | 含义 | 判据 |
| --- | --- | --- |
| **●** | **有护栏** | 存在自动化测试，**断言的是这两个能力凑在一起时的行为**，而不是各自单独的行为 |
| **◐** | **只有验收记录** | 真实模型跑过并留了记录，但没有任何自动化测试守着；下一次改动不会有人告诉你它坏了 |
| **○** | **空** | 两样都没有 |
| **▣** | **结构性不可能** | 不是「没测」，是这两样在结构上凑不到一起。理由逐条写在第四节 |

一个格子可能同时是 ● 和 ◐（既有单测又真跑过），此时记 **●**，并在证据栏里把验收记录一并列出。

### 十一个维度

`HK` C12 Hook · `SA` C13 子 Agent · `WT` C14 工作区隔离 · `TM` C15 协作 ·
`CL` C16 分类器 · `PM` 权限五层（含 ②″ 保护路径） · `PL` Plan Mode 两阶段 ·
`SK` Skill（含 `context: fork`） · `CX` 上下文压缩 · `TD` todo 清单 · `AK` `ask_user` 面板

### 矩阵

|        | SA | WT | TM | CL | PM | PL | SK | CX | TD | AK |
| ------ | -- | -- | -- | -- | -- | -- | -- | -- | -- | -- |
| **HK** | ●  | ●  | ●  | **○** | ●  | ●  | **○** | ●  | ○  | ●  |
| **SA** | —  | ●  | ●  | **◐** | ●  | ●  | ●  | **○** | ●  | ●  |
| **WT** | —  | —  | **○** | ●  | ●  | **○** | ▣  | ●  | ▣  | ▣  |
| **TM** | —  | —  | —  | ●  | ●  | ●  | ▣  | ○  | ●  | ●  |
| **CL** | —  | —  | —  | —  | ●  | **○** | **○** | **○** | ▣  | ▣  |
| **PM** | —  | —  | —  | —  | —  | ●  | ●  | ●  | ●  | ●  |
| **PL** | —  | —  | —  | —  | —  | —  | ●  | ○  | ●  | ●  |
| **SK** | —  | —  | —  | —  | —  | —  | —  | ●  | ▣  | ○  |
| **CX** | —  | —  | —  | —  | —  | —  | —  | —  | ▣  | ▣  |
| **TD** | —  | —  | —  | —  | —  | —  | —  | —  | —  | ○  |

**统计**：55 格 —— **● 34 格 · ◐ 1 格 · ○ 11 格 · ▣ 9 格**。

粗体标出的 7 格是第三节要展开的（按后果排序）。

### 有护栏的格子 · 逐条证据

| 格 | 断言的是什么 | 证据 |
| --- | --- | --- |
| HK×SA | 一条 `pre_tool_use` 拦截规则对**子 Agent 内**的同类调用同样生效 | `tests/test_subagent_integration.py:217-321`；接线 `rhinecode/subagents/runner.py:742` |
| HK×WT | Hook 负载里的 `cwd` 跟着**触发者**走，不是恒等于主项目根 | `tests/test_hook_manager.py:518-546`；真跑 `docs/c14/acceptance/logic-and-e2e.md:138-148` |
| HK×TM | `pre_tool_use` 看得见协作工具（否则「发消息让队员去做」就绕过拦截规则） | `tests/test_team_integration.py:287-368`、`tests/test_team_tools.py:508`、`:540` |
| HK×PM | Hook 判 DENY 时**不调** `engine.decide`；Hook 的 ASK **降不动**①黑名单与②沙箱的 DENY | `tests/test_hook_intercept.py:161-232`、`:233-301` |
| HK×PL | 规划阶段被挡下的工具**不触发**任何工具级 Hook 事件 | `tests/test_hook_intercept.py:488-551`（`test_plan_blocked` 在 `:535`） |
| HK×CX | 压缩前后各分发一次 `pre_compact` / `post_compact`；第一层存盘**不**分发 | `tests/test_hook_dispatch_points.py:244-283` |
| HK×AK | `ask_user` 既不进权限引擎、也不触发 `pre_tool_use`，**且同一次运行里普通工具两样都经过**（反证） | `tests/test_ask_user_loop.py:407-469`、`tests/test_hook_intercept.py:543` |
| SA×WT | 隔离单向加严、创建失败不降级、结算三种结局、并发三份互不相同 | `tests/test_subagent_isolation.py:137-169`、`:170-243`、`:295-403`；真跑 `docs/c14/acceptance/live-model.md:18-26` |
| SA×TM | 待命 / 唤醒续跑 / 待命不占并发名额；协作工具到得了子 Agent 而委派工具到不了 | `tests/test_team_wake.py:130-200`、`tests/test_subagent_toolset.py:180-282` |
| SA×PM | 派生引擎的 `mode` 独立、`turn_rules` 不继承、`session_rules` 共享；严格档角色在 auto 下仍收紧 | `tests/test_perm_derive.py:53-135`、`tests/test_auto_plan_integration.py:237-273` |
| SA×PL | 规划阶段只许委派**全只读**角色；含写工具 / 继承全集 / 分支式一律拒，且拒时不起线程不发 API | `tests/test_subagent_plan_stage.py:280-397` |
| SA×SK | `load_skill` 在全局禁用表里，显式白名单也拿不回来；回合级预授权不跟着委派跑出去 | `tests/test_subagent_toolset.py:46-85`、`tests/test_perm_derive.py:69-89` |
| SA×TD | `todo_write` 对子 Agent 全局禁用，继承式与显式白名单两条路都拿不到 | `tests/test_todo_integration.py:170-201` |
| SA×AK | 没有澄清回调时 `ask_user` **压根不出现在工具集里**；子 Agent 硬造调用时有专属文案且循环继续 | `tests/test_ask_user_loop.py:114-199`、`:200-252`；接线 `rhinecode/subagents/runner.py:774` |
| WT×CL | 本次运行的 `cwd` 如实传给分类器（隔离子 Agent 靠它判「这条命令在哪跑」） | `tests/test_classifier_loop.py:452-474` |
| WT×PM | 沙箱边界按**调用者的** `cwd` 算；越界写、相对上级、指向外面的符号链接、`cwd` 缺失一律拒；黑名单与 `allow` 规则都翻不过 | `tests/test_perm_cwd_sandbox.py:54-128`、`:206-273`；②″ 侧 `tests/test_perm_protected.py:185-229`、`:532-609` |
| WT×CX | 存盘目录恒为主项目根（`rhinecode/conversation.py:1256`），且 `.rhinecode/context` 在保护层的**排除表**里 | `tests/test_perm_protected.py:256-290` |
| TM×CL | 队友消息经分类器；拦下时**不投递**且告知发送方「没送出去」；具体理由不给模型 | `tests/test_classifier_message.py:122-176`、`:216-247`、`:248-265`；真跑 `docs/c16/acceptance/live-realistic.md:157-189` |
| TM×PM | 协作工具仍过一次 `engine.decide`；`deny` 拦得住、带括号的写法不命中、严格档关不掉它们 | `tests/test_team_tools.py:327-571` |
| TM×PL | 规划阶段只能发给 `main` 或工具集全只读的队员，拒绝时**什么都没投递** | `tests/test_team_tools.py:268-291` |
| TM×TD | 两段提示词的槽位次序（先「自己怎么管进度」，后「什么时候找别人」） | `tests/test_todo_integration.py:235-248` |
| TM×AK | 无人值守轮拿不到澄清回调 → 一个面板都不弹；文案区分「用户不在」与「有人拒绝」 | `tests/test_team_unattended.py:114-165`、`tests/test_ask_user_loop.py:231` |
| CL×PM | 用户写的 `allow` **零次调用**地短路分类器、`deny` 压得过它；只读短路天然不进④层 | `tests/test_classifier_loop.py:192-266` |
| PM×PL | 规划阶段的工具过滤**不靠权限档**——放行档也放不过去 | `tests/test_plan_stage_guard.py:94-176` |
| PM×SK | `allowed-tools` 预授权翻不过①黑名单、②沙箱、③显式 deny；也消解不掉 ②″ 保护路径 | `tests/test_perm_turn_grant.py:256-298`、`tests/test_perm_protected.py:405-412` |
| PM×CX | 第一层存盘的落点在保护层排除表内（否则每次存盘都要弹面板） | `tests/test_perm_protected.py:256-290` |
| PM×TD | `todo_write` 落 `other` 分支：无规则时不弹面板、`deny` 拦得住、带括号的写法不命中 | `tests/test_todo_integration.py:46-169` |
| PM×AK | 同 HK×AK（同一组断言覆盖两格） | `tests/test_ask_user_loop.py:407-469` |
| PL×SK | fork 子对话继承 Plan Mode，且衔接语**只在** plan 模式下出现 | `tests/test_skill_isolated.py:266-347`（`:334`） |
| PL×TD | 待办工具在规划阶段不可用、不进规划期 schema、获批后才能用 | `tests/test_todo_plan_stage.py:92-129` |
| PL×AK | `Esc` 的语义按阶段分岔：规划阶段整轮停止，其余阶段继续跑 | `tests/test_ask_user_loop.py:300-347` |
| SK×CX | fork 子对话**只跑 C8 第一层**（存盘生效、LLM 摘要不生效） | `tests/test_skill_isolated.py:445-478` |

---

## 三、空格子 · 按「万一出事的后果」排序

排序依据是**后果**，不是修复难度。每条都写到「用户会看到什么」。

### ⚠️ 空格 1 —— HK × SK：`context: fork` 的 Skill 子对话**拿不到 Hook**

> **这一格不只是没测，是真的漏了。**

- **证据**：`rhinecode/conversation.py:1595` 构造 fork 子对话的 Agent 时是
  `Agent(sub_provider, self._registry, recorder=self._recorder)` —— **没有 `hooks=` 实参**。
  对照主对话的 `rhinecode/conversation.py:326`：`Agent(..., hooks=self._hooks)`。
  `rhinecode/agent/loop.py:389` 的缺省是 `NullHookManager()`，
  而 `pre_tool_use` / `post_tool_use` / `post_tool_use_failure`
  **三个事件全部由 Agent 内部分发**（`rhinecode/agent/loop.py:516`、`:552`）。
- **它是被计划过的**：`docs/c12/task.md:512` 逐字写着
  「把 `hook_manager` 透传给 `Agent` 构造与 `_run_forked_skill` 的子 Agent」——
  两处里只做了第一处。第二处的**注入通道**倒是接上了
  （`rhinecode/conversation.py:1572` 调 `consume_injections`，注释还写着「两处都要接」），
  于是回合级 Hook 与注入型 Hook 在子对话里正常，**只有工具级三事件是哑的**。
- **为什么没被发现**：`docs/c12/checklist.md:45` 那条手工验收比的是两条 `turn_start` 的 `scope`
  ——回合级事件，恰好是能用的那一半。
- **失效形态（用户会看到什么）**：用户写了一条
  `pre_tool_use` + `if: tool == run_command && command contains "git push"` → `deny` 的规则，
  在主对话里试过、确实拦住了。之后模型加载一个 `context: fork` 的 Skill
  （或用户自己敲 `/deploy` 这类短命令），Skill 正文里那句 `git push` **直接跑掉**，
  而 `/hooks` 报告显示这条规则「触发 0 次」。用户会认为规则写错了，去改 `if:` 条件——
  而根因在一个跟条件毫无关系的地方。
- ⚠ **这与 C13 那条安全承诺是同一形态**：`CLAUDE.md` 的子 Agent 第 ⑥ 条写
  「Hook 对子 Agent 全量生效——不生效的话主 Agent 只要把「跑 git push」委派出去就能绕过用户写的拦截规则」。
  委派那条路堵上了（`rhinecode/subagents/runner.py:742` + `tests/test_subagent_integration.py:217`），
  **Skill 这条路没堵**，而它比委派更容易走到（一次 `load_skill` 就够，不需要角色定义）。
- **归类**：本条应当从 R1 升级进 `README.md` 的 **B 组（产品缺陷）**，与 B2 同型
  ——功能实现完整、只差一处接线，而缺的那处是安全边界。

### 🔴 空格 2 —— CL × SK：fork 子对话的分类器接线**零测试**

- **证据**：`rhinecode/conversation.py:1626-1634` 传了 `classifier=self.classifier` 与
  `classifier_principal_history=self.history`，注释逐字写着
  「否则「把跑命令包进一个 Skill」就能整层绕过」。
  而 `classifier` 在 `tests/test_skill_isolated.py`、`tests/test_skill_invocation.py`、
  `tests/test_skill_manager.py` 里**一次都没出现**。
- **失效形态**：谁把这两行删掉（或把 `principal_history` 换成子对话自己的 `sub_history`，
  那是最自然的「就近取值」写法），3,323 项测试全绿。之后用户在主对话里说
  「这次改动先别提交」，模型加载一个含 `git push` 的 Skill——
  分类器要么整层没跑，要么跑了但**看不到用户那句话**（子对话的第一条 user 消息是 Skill 正文，
  不是真人说的话），命令照跑。用户看到的是「我明明说了别提交」。
- **空格 1 与 2 是同一处代码的两面**：`rhinecode/conversation.py:1595-1634`
  这三十几行里，Hook 漏了、分类器接上了但没人守。

### 🔴 空格 3 —— CL × SA：子 Agent 的分类器共享**只有验收记录**（本表唯一的 ◐）

- **证据**：`rhinecode/subagents/runner.py:799` 传 `classifier=runtime.classifier`（F23），
  `:803-806` 传 `classifier_principal_history=runtime.principal_history()`（F9）。
  而全仓测试里 **`principal_history` 出现 0 次**，
  11 处 `SubAgentRuntime(...)` 构造**没有一处**带 `classifier`。
- **有的是**：`docs/c16/acceptance/live.md:206-232`（真实模型：子 Agent 读 `secrets.env`
  再 `send_message`，trace 作用域标着 `subagent:secret-reader`，确实经了分类器）
  与 `docs/c16/acceptance/live-realistic.md:157-189`。
- **失效形态**：`principal_history` 的用意是「取用户消息用**主对话的**历史」——
  子 Agent 自己的历史里第一条 user 消息是主模型写的任务描述，
  注释原话是「当成用户的话等于让模型给自己签授权书」。
  这一行被改成 `own_history` 之后没有任何症状，直到某天主 Agent 把
  「去把 config.yaml 发到 xxx」委派出去，子 Agent 自己写的任务描述就成了它自己的授权依据。
  用户看到的是：主对话里说过「别外传」，委派出去就不算数了。
- ⚠ **验收记录不能替代护栏**：那两份记录验的是「当时这样跑通了」，
  下一次改 `runner.py` 时没有任何东西会告诉你它坏了——而 C16 的验收成本是真实模型跑五个宿主进程。

### 🟠 空格 4 —— WT × TM：「隔离委派与待命互斥」的**产品侧**无护栏

- **证据**：`rhinecode/subagents/runner.py:694` 一行
  `can_idle = team is not None and handle is None` 就是这条边界的全部实现，
  `:682-693` 有 12 行注释论证它。`can_idle` 在测试里**出现 0 次**。
- **现有的那条守的是别的东西**：`tests/test_e2e_team_scripts.py:222-249`
  断言的是**预置出来的角色文件里不许写 `isolation`**——守的是 fixture，不是产品行为。
- **失效形态**：谁把条件改成 `team is not None`（看起来更自然，「有协作就能待命」），
  隔离队员跑完 → C14 结算把无改动的工作区**连目录带分支一起回收**
  （`rhinecode/subagents/runner.py:684-686` 说的正是这个）→ 队员进入待命 →
  有人给它发消息把它叫醒 → 它每一次写入都在权限管线第②层被拒
  （`cwd` 指向一个已经不存在的目录，`tests/test_perm_cwd_sandbox.py:129-169` 那组「`cwd` 缺失即拒」）。
  用户看到的是：**队员叫醒了，但它说什么都写不进去**，而 `/agents` 显示它状态正常。
- **归类**：这是 `docs/internals/known-issues.md:210` 明确登记过的边界，
  即「知道、写下来了、但没有东西钉住它」。

### 🟠 空格 5 —— SA × CX：每个子 Agent 一个新 `ContextManager` 无护栏

- **证据**：`rhinecode/conversation.py:1234-1259` 的 `new_subagent_context_manager()`，
  docstring 写着「**绝不共享 `self._context_manager`**：它持有
  `_anchor_tokens` / `_anchor_len` / `_summary_failures` / `_circuit_broken`
  这些可变状态且**没有锁**，而子 Agent 是并发的」。
  测试里只有 `tests/test_subagent_integration.py:84` 把这个工厂**接进去**，没有一条断言它。
- **失效形态**：谁把 `new_context_manager=manager.new_subagent_context_manager`
  改成 `new_context_manager=lambda: manager._context_manager`（省一次构造，看起来是优化），
  三个并发子 Agent 与主对话共用一份无锁状态。
  用户看到的是：**主对话的用量估算突然失准**——`/compact` 在还剩一大半余量时就触发，
  或者反过来撞到上下文上限才动。全程零报错，trace 里也只看得到「估算值是多少」，
  看不出那个值是被谁改的。
- ⚠ 这一格与「加锁临界区」那五次死锁是同一族问题（无锁可变状态 × 并发），
  而本项目对那一族的其余成员都有护栏（`CLAUDE.md` 架构表里 6 个包各写了一条）。

### 🟠 空格 6 —— CL × CX：压缩会摘掉用户声明的边界（已登记的 ⑦a，无护栏）

- **证据**：`rhinecode/classifier/prompt.py:287-325` 的 `build_transcript`
  从 `principal_history` 里逐条取 user 消息；C8 第二层把早段历史换成一条结构化摘要。
  `CLAUDE.md` 的 C16 第 ⑦a 条已登记这一条（「要硬保证只能写一条 `deny` 规则」）。
  测试里没有任何一条构造「摘要之后再调分类器」的场景。
- **失效形态**：长对话开头用户说「这个仓库里的任何东西都不许发到外网」，
  两小时后触发一次自动压缩，那句话变成摘要里的一行转述（或干脆没进摘要）。
  之后模型发起一次外发请求，分类器放行。用户看到的是：**同一条边界，早上有效、下午失效**，
  而界面上压缩是静默发生的。
- **为什么排在这里而不是更高**：它是 spec 明写的取舍（官方文档记录了同一条），
  且硬手段（`deny` 规则）存在。但**「已登记」不等于「有人会在改动时想起来」**——
  一条断言「摘要之后分类器还看得见早期用户消息吗」的测试会把这个取舍从注释变成可执行的事实。

### 🟠 空格 7 —— CL × PL：规划阶段唯一进分类器的工具没被测过

- **证据**：`rhinecode/tools/send_message.py:58` `plan_safe = True` **且** `:66`
  `classifier_scope = "message"`。它是**唯一**同时满足这两条的工具
  （`run_command` / `web_fetch` / `web_search` 都不是 `plan_safe`）。
  规划阶段模型调 `send_message` → 走 `system_serial` 分支
  （`rhinecode/agent/loop.py:1618`）→ 分类器照常审查。
  `tests/test_classifier_message.py` 与 `tests/test_classifier_loop.py` 里
  `plan_stage` 出现 **0 次**。
- **失效形态**：规划阶段还有另一层拒绝
  （`tests/test_team_tools.py:268-291` 的「只能发给 `main` 或只读队员」），
  两条拒绝路径的文案与副作用没人比对过。**具体风险是次序**：
  若哪天有人把分类器审查挪到规划阶段判定之前，一条**本来就会被规划阶段拒掉**的消息
  会先花一次模型调用去审查它——用户看到的是规划阶段莫名其妙变慢，
  且账单上多出一批分类器调用；反过来，若规划阶段那层被绕过而只剩分类器，
  一条本该被「批准前不动手」挡下的消息会以「分类器觉得它无害」的名义投递出去。

### 🟡 空格 8 —— HK × CL：Hook 的 ASK 不该被分类器接管（有实现、无断言）

`rhinecode/agent/loop.py:1611-1615` 注释写明这条性质，机制是
`_apply_hook_ask`（`:590-596`）把 `layer` 换成 `Layer.HOOK`，
而分类器的触发条件要求 `layer is Layer.MODE`（`:735-741`）。
谁把 `_apply_hook_ask` 改成保留原 layer（看起来更「不修改历史」），
用户写下的那条 `ask` 规则会被分类器接管：**该弹面板的地方变成了问模型一遍**。
用户看到的是「我写的 Hook 规则不弹面板了」。

### 🟡 空格 9 —— HK × PM 的 ②″ 那一半：Hook 不得覆写保护路径的 layer

`rhinecode/tui/widgets.py:3593` 的确认面板靠 `layer_value == "protected"`
决定要不要摘掉「永久放行」那一项。`_apply_hook_ask`（`rhinecode/agent/loop.py:588-589`）
的 `if decision.decision != Decision.ALLOW: return decision` 保证了
一个已经是 ASK 的保护路径结论不会被换成 `Layer.HOOK`。
去掉那两行，用户在写 `.rhinecode/permissions.yaml` 时会看到「永久放行」按钮，
点下去写出一条**永远不会被求值**的③层规则——`CLAUDE.md` 里管这叫「骗人的按钮」，
是已知项 #18 的同型。这条性质没有任何测试。

### 🟡 空格 10 —— WT × PL：规划阶段可以委派隔离角色，于是**批准前**磁盘上就多了一个分支

- **证据**：`rhinecode/subagents/service.py:232-237` 的规划阶段判定**只看工具集是否含写工具**；
  隔离工作区的创建在 `:270-282`，**位置在它之后**，条件只有 `want_isolation`。
  因此一个 `tools: [read_file, grep_content]` + `isolation: worktree` 的角色，
  在规划阶段可以被委派，并真的跑 `git worktree add`。
  `tests/test_subagent_plan_stage.py:280-397` 里 `isolation` 出现 0 次。
- **失效形态**：用户在 Plan Mode 里让模型「先调研一下」，模型委派了隔离调研员。
  用户此时敲 `git branch`，看到一个自己没批准过的 `agent/reader-a1b2`，
  以及 `.rhinecode/worktrees/` 下一份完整 checkout。
  **实际损害有限**（只读角色写不进去，无改动时结算会连目录带分支一起回收），
  但 Plan Mode 的承诺字面上是「批准前不动手」，而 `git worktree add` 改的是 `.git/`。
- **它有可能是刻意的**（调研本来就是规划阶段最需要的事），但 `docs/c14/` 与
  `docs/extensions/auto-plan/` 都没写过这一条 —— 属于**没人想过**而不是**想过并接受**。

### 🟡 空格 11 —— 五处低后果的空格

| 格 | 现状 | 后果 |
| --- | --- | --- |
| HK×TD | `todo_write` 走普通工具路径，被 `EventCoverageTest`（`tests/test_hook_intercept.py:441-487`）泛化覆盖，但没有针对它的断言 | 一条 `post_tool_use` + `if: tool == todo_write` 的 Hook 若失效，用户只是少一条自动化通知 |
| TM×CX | 注入进历史的队友消息之后会被摘要 | 队友说过的话在长会话里失真；与空格 6 同源，但没有安全含义 |
| PL×CX | 规划阶段同样会触发压缩，无专门断言 | 长规划轮里早期需求被摘要掉；同上 |
| SK×AK | Skill 正文可以指挥模型调 `ask_user`，没有任何判据说它该不该 | 无安全含义；`ask_user` 的可见性判据是「有没有人可问」，与 Skill 正交 |
| TD×AK | 两者都在主对话、都不进权限管线，没有交互断言 | 无安全含义 |

---

## 四、结构性不可能的格子（**不是「没测」**）

这 9 格看起来空，实际上这两样凑不到一起。逐条给出**机制**而不是「大概不会」。

| 格 | 为什么凑不到一起 | 依据 |
| --- | --- | --- |
| WT×SK | 隔离只存在于子 Agent；而 `load_skill` 在 `GLOBAL_DENIED_TOOLS` 里，白名单也拿不回来 | `tests/test_subagent_toolset.py:46-85` |
| WT×TD | 同上，`todo_write` 也在那张全局禁用表里（且刻意如此：子 Agent 的进度已有活动区） | `tests/test_todo_integration.py:170-201` |
| WT×AK | 隔离只存在于子 Agent；子 Agent 的 `clarify` 恒为 `None`，于是 `ask_user` **根本不出现在工具集里** | `rhinecode/subagents/runner.py:774`、`tests/test_ask_user_loop.py:144` |
| TM×SK | 队员之间「能说话但不能招人」：`run_agent` 与 `load_skill` 对子 Agent 关闭 | `tests/test_subagent_toolset.py:203`、`:271` |
| CL×TD | `todo_write` 没有 `classifier_scope`（`rhinecode/tools/todo_write.py` 全文无该字段），四类审查范围一类都不落 | 分类器触发条件第 2 条，`rhinecode/agent/loop.py:702` |
| CL×AK | `ask_user` 在 `rhinecode/agent/loop.py:1437` 就被分流走，**根本不到 `engine.decide`**；而分类器只作用于「结论来自第④层」的调用 | `tests/test_ask_user_loop.py:407-469` 是这条的物证 |
| CX×TD | 待办不在 `history` 里（存在 `TodoStore`），压缩只动 `history`；每轮的提醒走 `<system-reminder>` 动态通道，不进历史 | `rhinecode/todo/render.py:304-326` |
| CX×AK | 面板问答的结果作为**工具结果**进历史，与任何普通工具同路径，没有独立于压缩的第二条通道 | `rhinecode/agent/loop.py:1937-2005` |
| SK×TD | Skill 正文只是「发给模型的文本」，`todo_write` 是普通工具；两者之间没有任何接线 | `rhinecode/tools/todo_write.py` 全文无 skill 引用 |

⚠ **`WT×TM` 刻意记成 ○ 而不是 ▣**（见空格 4）：结构上确实互斥，
但那个「结构」是一行没有护栏的布尔表达式。
**「有一行代码保证它」与「有一条测试保证那行代码还在」是两件事**，
而本项目的历史（`system_serial` × ③规则层那次）正是前者成立、后者缺失的产物。

---

## 五、本轮顺带查到的两处「不是空格子，是真的漏了」

| # | 问题 | 证据 | 建议归类 |
| --- | --- | --- | --- |
| 1 | **fork 子对话拿不到 Hook**（空格 1 展开） | `rhinecode/conversation.py:1595` vs `:326`；计划见 `docs/c12/task.md:512` | 升级进 `README.md` 的 **B 组** |
| 2 | **`ConversationManager.clear()` 里四条「漏掉不报错」的接线全部只有单元级测试** | `rhinecode/conversation.py:625-636` 四行（`skill_manager.clear_active()` / `_clear_team()` / `_clear_todo()` / `web_search_manager.reset_quota()`），四行的注释各自写着「漏掉不报错」 | 归入 **C 组工程缺口**（与 C3 / C4「20 行就能机械保证」同型） |

第 2 条要展开一句：`tests/test_todo_integration.py:292` 测的是 `TodoStore.clear()`、
`tests/test_team_wake.py:291` 测的是 `TeamService.clear()`、
`tests/test_web_search_manager.py:180` 测的是 `reset_quota()` ——
**没有一条经过 `ConversationManager.clear()`**。
唯一真正走那条路径的是 `tests/test_clear_stale_deliverables.py:163`、`:183`
与 `tests/test_subagent_integration.py:171-216`，而两者都只验子 Agent 那一条。

失效形态很具体：`/clear` 之后用户发现**搜索配额还卡在 50/50**，
或者**上一段对话的待办清单还挂在历史区底部**。两者都不报错，
而 `clear()` 是一个每次新增会话级状态就要加一行的函数——
它是本项目里最典型的「加了新东西却忘了这里」的落点，却是唯一没有整体护栏的那种。

---

## 六、这张表怎么用

**优先级建议**（只谈本节，不与 `README.md` 的六批混排）：

| 批次 | 做什么 | 代价 |
| --- | --- | --- |
| **先做** | 空格 1（fork 传 `hooks`）—— 它是**缺陷**不是缺测试，一行实参 + 一条护栏 | 极小 |
| **接着** | 空格 2、3、5（三条接线断言：fork 的 classifier、runtime 的 classifier + `principal_history`、每次委派一个新 `ContextManager`）——都是「断言构造参数」形态，共约 40 行 | 小，收益高 |
| **然后** | 空格 4、8、9（三条不变量断言：`can_idle` 互斥、Hook 的 ASK 不被分类器接管、Hook 不覆写 protected 的 layer） | 小 |
| **再议** | 空格 10（规划阶段的隔离委派）—— **先决定它是不是刻意的**，再谈测不测 | 需要一次设计判断 |
| **登记即可** | 空格 6、7、11 | — |

⚠ **别把这张表当成「测试覆盖率报告」**。34 个 ● 里有相当一部分是**反证式**的
（「同一次运行里普通工具两样都经过了」「不在规划阶段时含写工具的角色照常可委派」），
那正是本项目测试质量高于平均的原因；而 11 个 ○ 里真正要紧的只有前 5 个。
**这张表的价值不在数字，在于它把「哪些接缝从来没有人看过」变成了一份可以一条条划掉的清单。**
