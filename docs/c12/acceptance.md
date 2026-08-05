# C12 Hook 系统 验收报告

> 日期：2026-08-06
> 依据：[`checklist.md`](checklist.md)（十三节 / 约 83 项）
> 全量测试：`python -m unittest discover -s tests` → **1467 项全绿，skipped 4**

## 结论

| | 项数 | 结果 |
| --- | --- | --- |
| 🤖 无头可验 | 78 | **全部通过**（201 条 Hook 专属用例 + 全量套件） |
| 👁 需人眼 | 3 | 见下方「留给你的三项」 |
| 💰 需真实模型 | 2 | 端到端场景 9 / 10，留作手测 |

Hook 专属测试共 **201 条**：

| 文件 | 条数 | 覆盖 |
| --- | --- | --- |
| `test_hook_conditions.py` | 31 | 四种匹配形态、`all`/`any`、缺失字段语义、类型归一化 |
| `test_hook_parser.py` | 35 | 七项校验、两处整层降级、两层加载顺序、模板 |
| `test_hook_actions.py` | 25 | 四种动作 + 三条安全反证 |
| `test_hook_manager.py` | 38 | 分发、`once`、结论合并、失败语义、惰性负载、加锁不变量 |
| `test_hook_intercept.py` | 25 | Agent Loop 集成 + 四条安全反证 + 埋点保真 |
| `test_hook_dispatch_points.py` | 18 | 十二个分发点 + 真实 `build_app` 读真实 `hooks.yaml` |
| `test_hook_command.py` | 11 | `/hooks` 登记与报告 + Textual 布局安全 |
| `test_hook_zero_regression.py` | 12 | 缺省零行为 + 导入级 I/O 禁令 |
| `test_e2e_hooks.py` | 6 | **起真宿主子进程**的端到端六场景 |

## 逐节结果

### 一、规则模型与加载（AC1、AC19、AC20）——7/7

| 检查 | 证据 |
| --- | --- |
| 省略 `if` 的规则可加载且每次都触发 | `MinimalRuleTest.test_event_and_action_only` + `OnceTest` |
| 缺 `event` / 缺 `action` 被丢弃、其余照常 | `MinimalRuleTest` 两条 + `LayerDegradeTest.test_one_bad_rule_does_not_affect_others` |
| 七项校验各能独立触发、警告含规则标识 | `ValidationTest` 12 条（含 `timeout: true` 被当成 1 秒那条） |
| 三种坏配置整层降级为空 | `LayerDegradeTest` 3 条（含「不能改成跳过该字段」的反证注释） |
| 两层全生效、顺序为用户级→项目级→声明序 | `LoadAllTest.test_execution_order_user_then_project_then_declaration` |
| 一层损坏另一层照常 | `LoadAllTest.test_broken_layer_does_not_stop_the_other` |
| 模板全注释、解析为空 | `ScaffoldTest` 3 条 |

### 二、事件覆盖（AC2、AC3、AC4）——8/8

十二个事件全部有分发点护栏。**AC3 的六种「没执行」分支逐条钉住**
（`NoExecutionBranchesTest`）。

> ⚠️ **与 spec 原文的一处偏差已记入勘误**：spec F2 边界第 2 条把「权限判 DENY」
> 与「用户拒绝」也列进「不触发任何工具级事件」，那与 F6 的管线位置自相矛盾——
> `pre_tool_use` 排在五层之前，判定跑之前无从知道会不会 DENY。实现按
> 「本条只管后置事件」执行，spec 里已留勘误块，`docs/c12/README.md` 有对照表。

### 三、条件表达式（AC5、AC6）——7/7

含两条易退化性质的专项：`command: "git *"` **不**命中 `github-cli`（证明确实复用了
命令匹配算法而非退化成 `fnmatch`）；通用 glob 用 `fnmatchcase`（同一份配置在
Windows 与 POSIX 上行为一致）。

### 四、四种动作（AC7–AC11）——8/8

| 检查 | 证据 |
| --- | --- |
| stdin 收到完整负载 JSON | `CommandActionTest.test_payload_arrives_on_stdin` |
| 命令串逐字执行、无插值 | `test_command_string_is_executed_verbatim`（命令里写 `${file_path}`，断言负载值**没有**出现） |
| 危险命令被①黑名单拦下且**子进程未启动** | `BlacklistTest`（`subprocess.run` 调用数为 0） |
| `prompt` 注入只出现一次 | `InjectionChannelTest`（直接断言发给 Provider 的 system 消息） |
| 四类禁止地址**请求未发出** | `HttpActionTest.test_forbidden_urls_never_send_a_request` |
| 500 响应不影响流程 | `test_non_2xx_is_failure_but_still_no_verdict` |
| `agent` 占位 `ok=True`、挂 `pre_tool_use` 时工具照常执行 | `PromptAndAgentTest` + `EventCoverageTest` |

### 五、执行控制（AC12、AC13、AC25）——5/5

含「两条同名规则不共享 `once` 状态」——`once` 的键是 `source#index` 而非 `name`。

### 六、拦截语义（AC14、AC16）——6/7（1 项人眼）

**关键反证**：Hook 判 DENY 时 `engine.decide` 调用数为 **0**（防「先跑引擎再看 hook」
这种顺序写反但结果碰巧正确的实现）。

### 七、失败语义（AC17、AC18）——4/4

fail-closed 与 fail-open **成对验证**，端到端也各跑了一遍（场景 5/6，同一条坏 Hook
换事件）。

### 八、安全性专项——7/8（1 项人眼）

| 检查 | 证据 |
| --- | --- |
| 翻不过①黑名单 | `AskUpgradeTest.test_ask_cannot_downgrade_a_blacklist_deny` |
| 翻不过②沙箱 | `test_ask_cannot_downgrade_a_sandbox_deny` |
| `{"decision":"allow"}` 不产生放行 | `DecisionParsingTest.test_allow_is_ignored` |
| 未知 `decision` 不崩溃 | `test_unknown_decision_is_ignored` |
| 项目级提示每次启动都出现 | `test_project_notice_appears_on_every_startup` |
| 硬校验与 `web_fetch` 复用同一实现 | `actions.py` 直接 `from rhinecode.permission.network import check_hard`，`HttpActionTest` 六类地址全覆盖 |

### 九、可观测性（AC22、AC23）——4/5（1 项人眼）

**验收期修掉两处观测缺口**（详见下节）。

### 十、缺省零行为（AC24）——4/4

含「跑完一整轮交互，trace 里零条 hook 事件」——比「既有测试仍通过」精确得多，
后者只能证明既有断言仍成立，证明不了「没有多出任何东西」。

### 十一、其余非功能（AC25–AC27、N6）——6/6

**加锁不变量护栏已做反证**：把动作执行挪进临界区后，`trace` 变成
`[False, '其它线程完成']`，与断言的 `['其它线程完成', True]` 不符——护栏确实会红。
（反证时顺带确认了 `_lock` 是普通 `Lock` 而非 `RLock`，同线程重入直接自锁。）

### 十二、编译与测试——4/4

`compileall` 无错误；全量 1467 项全绿、skipped 仍为 4；两条遍历 `Layer` 的断言通过。

### 十三、端到端场景——8/10（2 项需真实模型）

| 场景 | 结果 | 说明 |
| --- | --- | --- |
| 1 拦截生效 | ✅ 真宿主 | 命令未执行、原因回灌、**对话继续到第二轮** |
| 2 升级为问 | ✅ 真宿主 | 只读调用弹出面板、面板原文含 Hook 原因、判定层为 `hook`、同意后照常执行 |
| 3 自动化正路 | ✅ 真宿主 | 副作用文件内容取自 stdin 负载的 `path` 字段 |
| 4 上下文注入 | ✅ 单测 | **刻意不走宿主**：`InjectionChannelTest` 直接断言注入进了发给 Provider 的 system 消息且只出现一次，比经宿主观察更精确 |
| 5 fail-closed | ✅ 真宿主 | 工具未执行、文案含「Hook 自身执行失败」且**不含**「工具执行异常」 |
| 6 fail-open | ✅ 真宿主 | 同一条坏 Hook 换事件后工具照常跑完，失败仍被记录 |
| 7 项目级提示 | ✅ 真宿主 | 首屏 `ui_message` 含事件与完整命令串 |
| 8 零配置 | ✅ 单测 | `test_hook_zero_regression` 断言 trace 零条 hook 事件 |
| 9 真实模型下的拦截反应 | 💰 待手测 | 判的是模型的反应质量，无法用断言判定 |
| 10 真实模型下的注入生效 | 💰 待手测 | 同上 |

---

## 验收期发现并修掉的两处缺口

两处都属于 CLAUDE.md 记着的那类「**观测设施撒谎但不报错**」——界面上完全正确，
只有记录是错的。它们都是端到端场景**跑起来才暴露**的，单测层面看不见。

### ① `startup_notice` 绕过了埋点

`tui/app.py` 的挂载逻辑直接调 `HistoryView.append_system`，而不是 `show_message`。
两者在界面上一模一样，差别只在后者会顺带产出 `ui_message` 埋点。

**这是一个既有缺口**（不是本章引入的），但 C12 让它从「记录不完整」升级为
「安全措施不可验证」：项目级 Hook 的逐条展示是 spec F9.1 的**全部安全价值**，
而「它到底有没有出现在首屏」只能靠这条埋点判定。已改走 `show_message`，
护栏是端到端场景 7。

### ② `permission_decision` 记的是升级前的结论

埋点原本排在 `_apply_hook_ask` **之前**。于是一次「权限判 ALLOW、Hook 把它升级为
ASK」的调用，在记录里留下 `decision=allow`，而用户实际看到的是一个确认面板。
排查的人会据此断定「Hook 没生效」——而 Hook 明明生效了。

埋点已挪到升级之后（记**生效的**那个结论），并补了两条单测护栏
（`TraceFidelityTest`）——端到端用例每条要起一次宿主，不能只靠它守这条性质。

## 留给你的三项（👁 需人眼）

这三项都是「读起来对不对 / 看起来对不对」，断言只能验到「含某几个词」，
验不到「是不是真的说清楚了」：

1. **拦截回灌文案**（checklist 第六节末条）——跑一次场景 1，读一遍模型收到的那段
   `[Hook 拦截·<规则名>] ...`，对照 spec F6.3 的三项要求：说清是谁拦的、为什么、
   以及「不要改参数或换工具绕过」。
2. **项目级启动提示的呈现**（第八节）——在真实终端里看一眼首屏，判断那段逐条列表
   是否**足够醒目**、命令串是否读得清。这是本章唯一挡在「别人仓库的命令直接执行」
   前面的东西，它的可读性就是防线强度。
3. **`/hooks` 报告的终端排版**（第九节）——写几条规则后敲 `/hooks`，看分段、缩进、
   长命令串换行是否可读。

另有两项 💰 需真实模型（端到端场景 9/10），判的是模型面对拦截会不会绕路、
面对注入会不会真的照做——那是行为质量，不是代码行为。
