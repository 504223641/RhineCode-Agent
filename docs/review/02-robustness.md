# 阶段 2 · 坐实已发现的缺陷（R3）

> 2026-08-22 实测。配套 [`README.md`](README.md)（问题清单与行号）与
> [`00-baseline.md`](00-baseline.md)（机器基线）。
>
> **本轮不改任何产品代码。** 唯一的例外是 C4 的两次变异实测——把代码改坏、跑完
> 3,323 条测试、再原样还原（`git diff` 为空已核）。这是 R2 立下的先例：
> 「护栏绿不绿」证明不了任何事，「把代码改坏之后它会不会红」才证明得了。
>
> 复现脚本全部落在 scratchpad，**每条结论后面贴的都是实跑输出**，不是推演。

## 怎么读这份文档

每条按同一个骨架走：**复现路径 → 影响面 → 修法 → 代价与成对维护点**。
「复现路径」里凡是能真跑的都真跑了；跑不了的说明为什么跑不了。

⚠ **本轮推翻了 `README.md` 与 `00-baseline.md` 里的七处说法**，逐条收在
**最后一节「附一」**。**那一节比正文更该读**——它们都不是笔误，
而是「只定位到行号、没走到底」这件事本身会产生的偏差：
定位看到的是代码长什么样，坐实看到的是它实际怎么表现，两者能差很远。

## 结论摘要

| # | 结论 | 变化 |
| --- | --- | --- |
| **B1** | ✅ 坐实，且**三种触发形态都不需要模拟 IO 故障**，最容易撞上的是「用户手改本地配置打错一个字」 | 影响面比原报告大 |
| **B2** | ✅ 坐实。另发现修它需要**先造一个不存在的判定函数**，且有**第二个接线入口**（Skill 预授权），那一侧连「不丢弃」的理由都不成立 | 修法比原报告复杂 |
| **C3** | ✅ 坐实，**当场查出一处签名与标志不一致**（`TodoWriteTool`，经核是刻意）。原报告的护栏写法会**误报它**，且只覆盖 20 个工具里的 7 个 | 修法要改 |
| **C4** | ✅ 坐实，**变异实测两次**：一次无害、一次真的产生 `ImportError`，而 **3,323 条测试两次都全绿** | 证据升级 |
| **C7** | ✅ 坐实，四类错误真跑出原文。但**「无重试退避」是错的**——SDK 已经在重试 | 结论要改 |
| **C6** | ⚠ **原报告的因果链不成立**：终端不会坏，且 catch-all 兜不住主要失败形态。**换成两条真缺口** | 重写 |
| **C8** | ✅ 坐实，**并用实测推翻了代码注释里给出的理由** | 结论要改 |
| **C10a** | ✅ 坐实，**后果比原报告严重**：不是「线程停到天亮」，是**整个进程退不掉** | 升级 |
| **C10b** | ⚠ 内存增长实测约 **3 KB / 次委派**，作为内存问题**可以忽略**。真实后果在别处 | 降级 + 改写 |
| **C10c** | ✅ 坐实，**12 次实验里 10 次留下一个被清空的文件** | 证据升级 |
| **E1** | ✅ 坐实 | — |
| **E2** | ⚠ **「原始 traceback 丢失」是错的**，`__context__` 仍在。纯观感问题 | 降级 |

---

# 前置问题：运行期把消息送到界面，这条通道存不存在？

**存在，而且有四条，各自适用的场景不同。** B1 的修法因此成立，且不必新造机制。

| # | 通道 | 谁在用 | 从哪个线程发起 | 落到界面上是什么 |
| --- | --- | --- | --- | --- |
| ① | **`AgentEventType.NOTICE` 走事件流** | c16 F21 的「预授权被丢弃」提示 | Worker 线程（Agent Loop 内） | 按 `event.level` 分三档：`notice`→`[dim]` 系统行 / `event`→正常亮度 / `warning`→「警告：」前缀 |
| ② | **挂载期注入回调** | `memory_manager.notify`、`skill_manager.notify_activation`、`manager.notify_preset_change` | 任意线程（回调内部自己 `call_from_thread`） | 一条系统行 / 状态栏刷新 |
| ③ | `startup_notice` | 记忆提示、权限加载警告、Hook 加载警告 | — | **只在 `on_mount` 读一次** |
| ④ | `show_warning` | 项目级 Hook 的逐条展示 | 主线程 | 醒目通道 |

③ 就是 B1 现在掉进去的那口井：`self.startup_notice` 在 `ConversationManager.__init__`
里**一次性拼好**（`rhinecode/conversation.py:413`），由 `rhinecode/tui/app.py:757-768`
在挂载时读**一次**。`add_startup_notice`（`:443`）只服务装配层，仍在挂载之前。
`PermissionEngine.load_errors` 全仓只有一个消费者——`_compose_startup_notice`
（`:487`）。没有 `/perm` 之类的权限报告命令（内置命令共 15 条，`/help` `/think`
`/mode` `/mcp` `/hooks` `/agents` `/tasks` `/context` `/compact` `/memory`
`/resume` `/init` `/skills` `/clear` `/exit`，**没有一条读它**）。

**所以运行期往 `load_errors` 里 append 等于扔进垃圾桶**，这一点已坐实。

## ①正是 B1 该走的那条，且时机对得上

`_wrap_events`（`rhinecode/conversation.py:2419`）在**每 `yield` 一个事件之后**
调一次 `_take_grant_notices()`，把攒下的文本转成 NOTICE 事件插进流里。
那段代码的注释把理由写死了：

> ⚠ **必须在这里发**，不能在 `_grant_for_skill` 里直接写界面：那个方法有三条触发路径，
> 其中「模型自行调 `load_skill`」发生在**事件流中途**——协调层此刻没有任何直达界面的通道，
> 唯一的出口就是本方法正在产出的这条事件流。

B1 的 `ask` 闭包处在**完全相同的位置**：它由 `rhinecode/agent/loop.py:2229` 调用，
那是 `_run_one_serial` 生成器体内；`ask` 返回之后必然会 `yield` 一个事件
（`TOOL_START`+`TOOL_RESULT`，或拒绝分支的 `TOOL_RESULT`）。因此一条在 `ask`
里 append 的提示，**在下一个事件边界就会被 `_wrap_events` 取走并送上界面**，
延迟不超过一次工具执行。

**不需要新造通道，照抄 `_grant_notices` 的攒—取模式即可。**

---

# B1 🔴 「永久放行」写盘失败时静默降级

`rhinecode/conversation.py:1817-1821` + `rhinecode/permission/engine.py:570-577`

## 复现路径（实跑，三种形态全中）

原报告说「不能复现，需要写盘失败」。**实际上三种触发形态里有两种不需要任何 IO 故障，
只需要用户手改过一次 `.rhinecode/permissions.local.yaml`。**

`append_local_allow`（`rhinecode/permission/config.py:339-374`）有三个抛出点：
坏 YAML（`:358`）、顶层不是映射（`:364`）、以及最后那次 `write_text`（`:374`）。

```
--- ① 坏 YAML（未闭合的引号）
   persist_local_rule -> False
   load_errors        -> ['写入本地权限配置失败：本地权限配置解析失败，未写入：…\n  in "<unicode string>", line 2, column 5 …']
   session_rules      -> 0
--- ② 顶层是列表（用户按 permissions.yaml 的直觉手写）
   persist_local_rule -> False
   load_errors        -> ['写入本地权限配置失败：本地权限配置顶层应为映射，未写入：…']
   session_rules      -> 0
--- ③ 文件只读 / 被独占（编辑器打开、权限不足、只读挂载）
   persist_local_rule -> False
   load_errors        -> ["写入本地权限配置失败：[Errno 13] Permission denied: '…permissions.local.yaml'"]
   session_rules      -> 0
--- ④ 对照组：一切正常
   persist_local_rule -> True
   load_errors        -> []
   session_rules      -> 1
```

三条错误串全部进了 `load_errors`，而上面已经证明**那里没有出口**。
`_build_ask` 拿到 `False` 之后改加一条会话规则、`return True`，**面板上什么都不会变**。

⚠ **形态②值得单独说**：`permissions.yaml`（用户级/项目级）与
`permissions.local.yaml`（本地级）是同一套规则体系的三层，而**只有本地级那份要求顶层是映射**。
一个用户照着 `permissions.yaml` 的样子往本地级文件里手写几条、写成 YAML 列表，
从此**这台机器上的「永久放行」永久失效**，而他一次提示都不会看到。

## 影响面

面板选项 3 的说明文字是 `写入本地配置，重启仍生效`
（`rhinecode/tui/widgets.py:3608`）。本轮用端到端驱动设施真弹了一次面板，
控制通道回读的原文逐字如此：

```
{'id': 'yes_permanent', 'label': '  3. 永久放行    [dim]写入本地配置，重启仍生效[/dim]'}
```

用户点下去，本次放行了，**面板消失、无任何异常**——他有充分理由相信这条授权已经落盘。
重启后凭空失效，且他不会把这件事和几天前手改配置文件联系起来。

⚠ 这与项目自己登记的**已知项 #18**（「错误的安全承诺比没有承诺更危险」）、以及
成对维护点里那条「**计划审批面板那句说明 ↔ `auto` 预设的档位**」是同一形态：
**面板上一句与实际行为不符的话，比没有那句话更糟**。
同段 `:1799-1806` 的注释还记着这类失效**已经修过一次**（url 类规则写坏导致
「永久放行重启后凭空失效」），那一次的教训是「问题要到下次启动才显形」——
这一次落在错误处理分支上，是同一个坑的第二个入口。

## 修法

**最小改动，三处：**

1. `permission/engine.py:570-577` 的 `persist_local_rule` 增一个出参
   （或改成返回 `str | None`，失败时给出可读原因），**不再往 `load_errors` 里塞**
   ——那个字段的语义是「加载阶段的错误」，运行期错误混进去本身就是它没人消费的原因；
2. `conversation.py:1817` 的失败分支把原因攒进一个与 `_grant_notices` 同型的列表；
3. 复用 `_take_grant_notices` 那套取走—清空的接口（或直接并进同一个列表），
   由 `_wrap_events:2419` 已有的循环发出 `NOTICE(level="warning")`。

文案要写清三件事：**没写进去**、**本次仍然放行了**、**本会话内有效但重启失效**，
外加**为什么**（原因串已经有了）与**怎么修**（去看那个文件）。

**更好的替代方案（推荐一并做）**：`_build_ask` 现在把「写盘失败」当成一次可降级的意外，
但②形态说明它更可能是**配置文件坏了**。可以在启动时就检测本地级配置是否可解析、
可解析则顶层是不是映射，坏的话在启动提示里说明「本地级权限配置无法解析，
本次运行的『永久放行』会退化成『本会话放行』」——**在用户点那个按钮之前就告诉他**，
比事后补一条提示强。这条走的是既有的 ③ 通道，零新增机制。

## 代价与成对维护点

**代价：小**（三处改动，无新机制）。**但它落在一片成对维护点密集的地方**，
`paired-maintenance` 里直接相关的有三条：

| 成对维护点 | 与本次改动的关系 |
| --- | --- |
| **「新增一种权限请求 `kind`」→ `_TOOL_MAP` + `to_allow_rule`** | 代码注释 `:1806` 明写「下面**三处**（会话级、永久级、**永久写入失败的回退**）都用它，别只改前两处」——**要改的正是第三处**，动它时别把 `to_allow_rule` 的调用绕开 |
| **「②″保护路径的『本会话放行』是成对维护点」→ `ConfirmPanel.show_for` 三选项分支 + `_build_ask` 豁免分支** | 保护路径那条路在 `:1791` 就 `return True` 了，**根本走不到 `persist_local_rule`**。改动时不要把两条路合并 |
| **「确认面板的 `no_permanent` ↔ `protected` 是两个布尔，别合并」** | 若考虑「写盘失败时把『永久放行』从面板上摘掉」，那要动 `show_for` 的选项条数——**那是 `no_permanent` 管的**，与 `protected` 管的机制不是一回事，合并会让搜索类的「本会话放行」静默不生效 |

⚠ **别顺手把面板文案改成「可能写入本地配置」**。那是把一句准确的承诺换成一句含糊的，
用户依然不知道这一次到底写没写。要么让它当场说实话，要么在启动时提前说。

---

# B2 🔴 一个安全提醒实现完整，但从未接线

`rhinecode/classifier/render.py:244`

## 复现路径（实跑）

```
规则 WebFetch(domain:*)               -> decision=allow  layer=rule
规则 WebFetch(domain:example.com)     -> decision=deny   layer=network
规则 （无规则）                        -> decision=ask    layer=mode

现有的丢弃器认不认这条全域名规则：
  is_broad_allow('WebFetch', 'domain:*') = False
  is_broad_allow('Bash',     'python *') = True

而提醒文案本身是写好的、能直接用的：
  提示：下面的域名放行规则会让分类器对网络访问完全不生效：WebFetch(domain:*)。（它未被丢弃，因为它同时用于建立域名白名单。）

全仓调用点：
rhinecode/classifier/render.py:244:def render_broad_domain_warning(rules: Iterable[str]) -> str:
```

分类器只在**结论来自第④层（`layer=mode`）**时才被调用。
`allow: WebFetch(domain:*)` 让结论停在第③层（`layer=rule`），
于是**这一整层对网络访问零次调用**——正是 `docs/c16/spec.md:346` 登记的已知边界第 6 条，
而它的对冲手段（F22 的启动提醒）不存在。

对应验收标准 **AC32**（`docs/c16/spec.md:417`）：
「网络类的域名规则不被丢弃；全域名放行时**出现一条提醒**」——前半句成立，后半句没有。
`docs/c16/checklist.md:107` 那条至今未勾。

## 影响面

用户写下 `allow: WebFetch(domain:*)` 的动机通常是「我不想每次抓网页都点面板」。
他会以为分类器还在把关（`CLAUDE.md` 明写分类器审四类动作、网络是其中一类），
**实际那一层对网络已经完全关掉**，而②′网络边界层只看得到主机名——
看不出地址的查询参数里夹带了什么。而这恰恰是 web_fetch 扩展登记的**第一条外泄链路**。

同样与已知项 #18 同型：**承诺在，兑现没了，且没有任何东西告诉用户。**

## 修法

比原报告说的「接到启动通知链路上」要多两步。

**① 先要有一个判定函数。** `classifier/broad.py` 现在只有
`is_broad_command_allow`（命令类）与 `is_broad_search_allow`（搜索类），
**没有域名那一类**。要新增 `is_broad_domain_allow(tool, pattern)`，判据是
`tool == "WebFetch"` 且 `pattern` 归一化后是 `domain:*`（裸 `*` 也要考虑）。

⚠ **它不能进 `is_broad_allow` 那个伞函数。** 伞函数的语义是「这条规则宽到**该被丢弃**」
（`bootstrap.py:510` 的丢弃循环直接用它的返回值决定丢不丢），而 F22 明说域名规则**不丢**。
混进去会让域名规则连带被丢弃，那会改变②′层「白名单是否已建立」的判定——
`permission/engine.py:421` 的 `network.decide(request, merged, self.policy_ruleset)`
读的就是它。**这是本条最容易踩的一脚。**

**② 接线点有两个，不是一个。**

- **入口一：`bootstrap.py:498-518` 的丢弃循环。** 在同一个 `for rule in
  engine.file_ruleset.rules` 里顺带收集命中的域名规则，循环后
  `manager.add_startup_notice(render_broad_domain_warning(hits))`。
  与 `render_dropped_rules` 同一个出口，零新增机制。
- **入口二：`conversation.py:2280-2299` 的 `_grant_for_skill`。**
  Skill 的 `allowed-tools` **可以写 `WebFetch(domain:*)`**
  （`skills/validation.py:54,63` 认 `WebFetch`，`:130` 的注释明写它处理域名规则），
  而那条规则同样在③层放行、同样让分类器零次调用。实测：

```
① 无任何规则                       -> ask @ mode
② Skill 预授权 WebFetch(domain:*)  -> allow @ rule
policy_ruleset 里的规则数 = 0 （回合级规则确实没进去）
```

⚠ **而且这一侧应当直接丢弃，不只是提醒。** F22「不丢弃」的唯一理由是
「域名规则同时承担建立白名单的语义」——但 `policy_ruleset` 只装
**用户级 + 项目级文件规则**（`engine.py:140,233-236`），
**回合级预授权根本不进它**（上面实测 `len == 0`）。
所以对 Skill 预授权这一侧，「不丢弃」的理由**不成立**，
可以照命令类的先例直接丢并逐条告知。

这与成对维护点里那条**「丢弃宽泛放行规则要覆盖两个入口（c16 F20）」**是同一个结构，
而那条的注记写着「**第二处是真机验收才发现的**」——B2 是它的第三个实例，
现在有机会在真机验收之前就做对。

## 代价与成对维护点

**代价：小到中**（一个纯函数 + 两处接线 + 各一条护栏）。牵动：

- **「丢弃宽泛放行规则要覆盖两个入口」** —— 直接命中，见上。
- **「`classifier/broad.py` 的两张清单与『刻意不含 `git *`』」** —— 新增判定函数时
  别顺手往 `INTERPRETERS` / `PACKAGE_RUNNERS` 里加料；域名那条是**第三类**，
  独立成函数，不进伞函数。
- **「新增一类要经分类器审查的动作（c16）→ 四处齐改」** —— **不适用**：
  本条不新增审查范围，只补一条启动提醒。别被邻近条目带跑。

护栏怎么写：断言「配了 `WebFetch(domain:*)` 时启动提示里出现该规则原文」，
外加一条**反证**——配 `WebFetch(domain:example.com)`（窄规则）时提示**不出现**。
只写正例的话，一个「无条件提醒所有域名规则」的实现会全绿，而那会让每个正常配置的用户
每次启动都看到一条无意义的警告，几次之后他就不看启动提示了——那比不做还糟。

---

# C3 🟠 `Tool.execute` 的签名契约没有护栏

`rhinecode/tools/base.py:229` vs 20 个实现

## 复现路径（实跑，当场查出一处）

扫全部 `Tool` 具体子类，逐个比对「标志 ↔ `execute` 形参」：

```
模块                  类                     ws  cwd  plan  pstage  一致
agent.plan_tools    AskUserTool           0   0    0     0       OK
agent.plan_tools    PresentPlanTool       0   0    0     0       OK
tools.edit_file     EditFileTool          1   1    0     0       OK
tools.glob_files    GlobTool              1   1    0     0       OK
tools.grep_content  GrepTool              1   1    0     0       OK
tools.load_skill    LoadSkillTool         0   0    0     0       OK
tools.mcp_config    MCPAddServerTool      0   0    0     0       OK
tools.mcp_config    MCPResolveServerTool  0   0    0     0       OK
tools.read_file     ReadFileTool          1   1    0     0       OK
tools.run_agent     RunAgentTool          0   0    1     1       OK
tools.run_command   RunCommandTool        1   1    0     0       OK
tools.send_message  SendMessageTool       0   0    1     1       OK
tools.team_tasks    TaskCreateTool        0   0    1     1       OK
tools.team_tasks    TaskGetTool           0   0    0     0       OK
tools.team_tasks    TaskListTool          0   0    0     0       OK
tools.team_tasks    TaskUpdateTool        0   0    1     1       OK
tools.todo_write    TodoWriteTool         0   0    0     1       *** XX ***
tools.web_fetch     WebFetchTool          0   0    0     0       OK
tools.web_search    WebSearchTool         0   0    0     0       OK
tools.write_file    WriteFileTool         1   1    0     0       OK

共 20 个具体 Tool 子类；不一致：['rhinecode.tools.todo_write.TodoWriteTool']
签名分布 (有cwd, 有plan_stage)： {(False, False): 9, (True, False): 6, (False, True): 5}
```

**那一处经核是刻意的**，`rhinecode/tools/todo_write.py:334-340` 逐字写着：

> ⚠ **`plan_safe` 改成 False 之后，循环已经不会传这个参数了**……
> **保留它是刻意的**，不是漏删：签名多一个带缺省值的关键字参数零成本，
> 而删掉它意味着将来若把 `plan_safe` 改回 True，循环传进来时会直接抛 `TypeError`
> ——那是个只在 Plan Mode 里才复现的运行期崩溃，而改标志的人不会想到还要改签名。

**这直接推翻了原报告给的护栏写法。** `README.md` C3 写的是
「断言『标志与形参**一一对应**』」——那条断言会把 `TodoWriteTool` 判红，
而它是 `paired-maintenance` 里明令保护的「刻意」（见
「`todo_write` 的 `plan_safe` 是 False，别当成漏声明改回去」那条）。

**正确的契约是单向的**：

```
workspace_aware == True  ⟹  execute 必须接受 cwd
plan_safe       == True  ⟹  execute 必须接受 plan_stage
```

反方向不成立、也**不该**成立——多一个带缺省值的关键字参数是零成本的前向兼容。
按单向口径核，**20 个工具全部通过**。

## 失败形态的实跑

```
① 并发路径（agent/loop.py:2107 附近）：
   TypeError: BrokenReadTool.execute() got an unexpected keyword argument 'cwd'
② 串行路径（agent/loop.py:2272 的 except Exception）：
   模型收到： 工具执行异常: BrokenWriteTool.execute() got an unexpected keyword argument 'cwd'
```

⚠ **两条路径的最终表现是一样的，不是原报告说的「不同」。**
并发路径的内层 `_run` 确实不加 try/except，但外层 `future.result()`
（`rhinecode/agent/loop.py:2121-2124`）照样 `except Exception` 兜成
`ToolResult(ok=False, output=f"工具执行异常: {e}")`——与串行路径 `:2273-2274` 逐字相同。
两条路径随后都照常 `_trace_tool(..., OUTCOME_EXECUTED, ...)` 与 `_dispatch_post_tool(...)`。

**这让缺口更隐蔽而不是更轻**：模型收到的是一句普普通通的「工具执行异常」，
它会当成一次可重试的失败去改参数重试——**而参数永远不是问题所在**，
于是整轮迭代烧在一个必然失败的调用上。

## 影响面

谁会踩到：**下一个加工具的人**。声明 `workspace_aware = True` 却忘了给 `execute`
加 `cwd=None` → 编译过、3,323 条测试全绿 → 只在那个工具真被调用时才出事，
而出事的样子是「模型反复调它、反复失败、说不清为什么」。

现有护栏 `tests/test_loop_cwd_dispatch.py` 那 7 条用的是**替身工具**，验的是
「loop 有没有按标志正确传参」——**不验真实工具的签名与自己的标志一致**。
全仓只有 `tests/test_todo_tool.py:99` 一处对单个工具做过 `inspect.signature` 检查。

## 修法

原报告说「遍历 `ToolRegistry.default()` 的全部工具」。**那只覆盖 7 个。**
`ToolRegistry.default()`（`rhinecode/tools/registry.py:136-154`）只注册六个核心工具
加 `MCPResolveServerTool`；另外 13 个是装配层按条件注册的
（`bootstrap.py:227,247,307,341,564,565,579,644`），
**而带 `plan_stage` 的那 5 个全在这一批里**——照原写法写，护栏会**一条都覆盖不到**
真正有风险的那类。

**该扫的是 `Tool` 的全部具体子类**，做法就是本轮那份脚本：
`pkgutil.iter_modules` 强制导入 `rhinecode.tools.*`（外加 `agent.plan_tools`
与 `mcp.tool_adapter`），递归 `__subclasses__()`，跳过抽象类，
`inspect.signature(cls.execute)` 逐个断言**单向**契约。核心逻辑不到 20 行。

⚠ 加两条**反证**：造两个故意写坏的假工具（声明标志、不带形参），断言护栏对它们**判红**。
不加反证的话，一个把断言写成恒真的实现会全绿，而那正是本条要防的形态。

## 代价与成对维护点

**代价：极小，收益极高——这仍然是本报告里性价比最高的一条**，只是写法要改。

牵动的成对维护点**恰好就是它要机械化的那两条**：

- **「新增『规划阶段仍可用』的工具（c13）」** → 声明 `plan_safe = True`
  + `execute` 必须接受 `plan_stage`。这条护栏正是它的机器化版本。
- **「`cwd` 的四个分发点（c14）」** → 本护栏只覆盖其中一处（工具侧的签名），
  **另外三处（并发桶 / `to_request` / hook 分发）它管不到**，
  `tests/test_loop_cwd_dispatch.py` 仍然必须留着。**两条护栏是互补的，别合并。**

---

# C4 🟠 `tools/__init__.py` 的致命不变量没有护栏

`rhinecode/tools/__init__.py`（2,799 字节 docstring，非注释代码 0 行）

## 复现路径（变异实测两次，全部还原）

**变异①：`from rhinecode.tools.registry import ToolRegistry`**（docstring 里点名的那个例子）

```
用例 3323/3323   墙钟 33.2s
全部通过
```

导入链也没断——`registry` 不牵扯 `mcp`，所以这一行**当下无害**。

**变异②：`from rhinecode.tools.mcp_config import MCPAddServerTool`**（牵扯 `mcp` 的那一侧）

```
FAIL rhinecode.mcp.auto_config
  File "G:\RhineCode-Agent\rhinecode\tools\mcp_config.py", line 13, in <module>
    from rhinecode.mcp.auto_config import (
ImportError: cannot import name 'resolve_mcp_query' from partially initialized module
'rhinecode.mcp.auto_config' (most likely due to a circular import)
OK   rhinecode.mcp.tool_adapter
OK   rhinecode.skills.manager
OK   rhinecode.subagents.runner
OK   rhinecode.subagents.service
OK   rhinecode.web.fetcher
OK   rhinecode.tools.base
```

**报错形态与 docstring 预言的逐字一致**（`partially initialized module` + `ImportError`）。
然后：

```
用例 3323/3323   墙钟 34.3s
全部通过
```

`import rhinecode.bootstrap` 也照样成功。

**这是本轮最干净的一条证据：一个真的、可复现的 `ImportError` 藏在 3,323 条全绿的测试后面。**
之所以绿，是因为**每一条测试恰好都以某个不触发它的顺序导入**——
只有「第一个被导入的是 `rhinecode.mcp.auto_config`」这一种顺序才会炸。
那种顺序今天没人走，明天多一个测试模块或一个新入口就可能走上。

已还原，`git diff` 为空。

## 影响面

谁会踩到：**任何一个觉得「re-export 一下方便点」的人**，包括三年后的作者本人。
docstring 已经把话说尽了（「排查成本极高，所以在这里写死这条约束」），
**但 docstring 不会在 CI 里变红**。

同类约束在别的包上都有护栏：`permission/__init__.py` 的在
`tests/test_classifier_broad.py:214-240`（`LeafPackageTest`），
`trace/__init__.py` 的在 `tests/test_trace_reader.py:398-425`。
**唯独 `tools` 一条都没有**，而它是那 10 个包强连通分量的枢纽。

## 修法

照 `tests/test_classifier_broad.py::LeafPackageTest` 的先例，
一条测试断言 `rhinecode/tools/__init__.py` 的 AST 里**没有任何 `Import` / `ImportFrom` 节点**。

⚠ **必须只看 import 语句、不做全文搜索。** 那份 docstring 里大量提到
`rhinecode.tools.registry` / `mcp.auto_config` 这些名字——那正是「为什么不 import 它们」的说明。
按全文搜索的话这条用例会因为一段**正确的注释**而红，
于是下一个人的第一反应是把注释删掉。这条陷阱在
`tests/test_classifier_broad.py:219-232` 里已经写过一次，照抄它的注释即可。

**顺带一条可选加强**：既然变异②的失败只在一种导入顺序下出现，可以补一条
「以 `rhinecode.mcp.auto_config` 为**第一个**导入的子进程冒烟」，
它对**任何**引入该环的改动都敏感，不只是 `tools/__init__.py` 这一处。
代价是起一个子进程（约 1 秒）。

## 代价与成对维护点

**代价：极小**（约 15 行）。牵动：

- **「`rhinecode/tools/__init__.py` 不得 re-export 任何子模块」** —— 本条就是给它补护栏。
- **「`tools/web_fetch.py` 是 `tools ↔ web` 包级互依的第三个依赖方」** ——
  提醒这个约束的依赖方在增加（`mcp` / `skills` / `subagents` / `web` / `team` / `todo`），
  护栏的价值随之上升。

---

# C7 🟠 Provider 层把所有错误压成一个字符串

`rhinecode/provider/deepseek.py:264-265`

## 复现路径（实跑四类，真发请求）

用一个明显无效的 key 对真实端点各发一次，取 `StreamChunk(type="error")` 的 `content`：

```
--- ① key 填错（401）
      Error code: 401 - {'error': {'message': 'Authentication Fails, Your api key: ****0000 is invalid', 'type': 'authentication_error', 'param': None, 'code': 'invalid_request_error'}}
--- ② base_url 写错 / 断网（连接错误）
      Connection error.
--- ③ 超时（request_timeout=0.001）
      Request timed out.
--- ④ 模型名写错
      Error code: 401 - …（与①逐字相同：401 先于模型校验发生）
```

那个字符串原样进 `AgentEventType.ERROR`（`rhinecode/agent/loop.py:1224`），
再原样进 `HistoryView.append_error`（`rhinecode/tui/widgets.py:2043-2050`），
渲染成红色粗体 + 「错误：」前缀。**紧接着**循环以 `StopReason.STREAM_ERROR` 结束，
界面再挂一条警告级「因流错误已停止」（`rhinecode/tui/app.py:2659`）。

所以一个刚装好、key 填错一位的新用户，第一次敲回车看到的是：

```
错误：Error code: 401 - {'error': {'message': 'Authentication Fails, Your api key: ****0000 is invalid', 'type': 'authentication_error', 'param': None, 'code': 'invalid_request_error'}}
警告：因流错误已停止
```

**两行都不告诉他去改哪个文件的哪一行。**

## ⚠ 一处必须更正：「无重试退避」是错的

原报告写「且无重试退避（依赖 SDK 默认）」。实测 SDK 默认**已经在重试**：

```
INITIAL_RETRY_DELAY = 0.5      MAX_RETRY_DELAY = 8.0
openai 1.109.1 默认 max_retries = 2
默认 timeout = Timeout(connect=5.0, read=600, write=600, pool=600)
```

拿一个只回 429 的本机服务器实测：

```
用户看到 : Error code: 429 - {'error': {'message': 'Rate limit reached', 'type': 'rate_limit_error'}}
实际发出的 HTTP 请求次数 : 3   总耗时 1.7s
```

SDK 的 `_should_retry` 重试 408 / 409 / 429 / ≥500，外加连接错误与超时；
退避是 `0.5 × 2ⁿ`（上限 8 秒）带抖动，且**服务端给了 `Retry-After` 就听它的**（≤60 秒）。

**所以 C7 的缺口不是「不重试」，是三件别的事：**

1. **重试完全不可见。** 3 次尝试 + 退避在长响应时可能耗掉十几秒，
   界面上什么都不显示，用户以为卡死了；
2. **重试次数不可配。** 想调只能改代码；
3. **最终的错误文案不分类。**

## 错误分类表

⚠ **分类的轴必须是异常类型 / `status_code`，不能用 `.code`。**
实测 DeepSeek 在一次 401 上返回的 `.code` 是 `'invalid_request_error'`、
`.type` 才是 `'authentication_error'`——**`.code` 与 HTTP 语义对不上**，
拿它做分派会把认证失败判成参数错误。可用字段：
`type(e)`（SDK 已按 status 分好类）、`e.status_code`、`e.body["message"]`、
`e.response.headers`。

| SDK 异常 | 触发场景 | 用户该看到什么 | 自动重试 | 记日志 |
| --- | --- | --- | --- | --- |
| `AuthenticationError`(401) | api_key 填错 / 过期 / 用了别家的 key | 「API Key 无效。请检查 **`<实际生效的配置文件路径>`** 的 `api_key`。」**必须打印那个路径**——`--config` 与用户级两条来源，用户常改错文件 | **不重试**（重试只是重复失败三次） | INFO 即可，**别记 body**（含掩码 key，但格式随服务端变） |
| `PermissionDeniedError`(403) | key 有效但无权用该模型 / 账号欠费 | 「这个 Key 没有调用 `<model>` 的权限，或账户余额不足。」把 `model` 值带上 | 不重试 | INFO |
| `NotFoundError`(404) | `model` 写错 / `base_url` 指到了错误路径 | 「模型 `<model>` 不存在。也请检查 `base_url` 是否多写/少写了 `/v1`。」 | 不重试 | INFO |
| `RateLimitError`(429) | 限流 | 重试期间状态行显示「限流，正在重试（第 n/3 次）」；三次仍失败 → 「已达到调用频率上限，稍后再试。」若响应头有 `Retry-After` 就把秒数说出来 | **重试（SDK 已做）**，只需把过程显示出来 | INFO |
| `BadRequestError`(400) | 上下文超长 / 参数非法 | 需按 `body["message"]` 二次分流：命中长度类关键词 → 「本次对话已超出模型上下文窗口，**试试 `/compact`**」；否则原样给出服务端的 message + 「这多半是一个 bug，请带上这条信息报 issue」 | 不重试 | **WARNING，记完整 body**——这是唯一需要作者看到原文的一类 |
| `InternalServerError`(≥500) | 服务端故障 | 「模型服务暂时不可用。」 | 重试（SDK 已做） | WARNING |
| `APITimeoutError` | 首字节迟迟不来 / 流中途卡住 | 「等待模型响应超时。网络不稳或响应过慢，可以直接重试。」⚠ 与 C8 联动：**现在主对话根本设不了超时** | 重试（SDK 已做） | INFO |
| `APIConnectionError` | 断网 / DNS 失败 / 代理没开 / `base_url` 域名写错 | 现在只有一句 `Connection error.`，**必须补上排查方向**：「连不上 `<base_url 的主机名>`。请检查网络、代理，以及 `base_url` 是否写对。」把主机名打出来是最省事也最有效的一步 | 重试（SDK 已做） | INFO |
| 其它 `Exception` | 解析异常、SDK 内部错 | 「与模型服务通信时发生未预期的错误。」+ 类型名 | 不重试 | **ERROR，记完整堆栈** |

**两条贯穿全表的原则：**

- **每条都要给出「下一步做什么」**，哪怕只是「可以直接重试」。
  现在这九类全都收敛成同一句「因流错误已停止」，那句话不含任何行动信息。
- **别把服务端 message 丢掉。** 分类文案是**前缀**不是**替换**——
  服务端偶尔会说出你没预料到的原因（配额、地区限制、内容审核），
  盖掉它会让排查变成猜谜。做成「一句中文说明 + 原文附在后面」。

⚠ 表里 400 那一行的「上下文超长」二次分流**没有实测过**——触发它要真发一次超长请求，
需要有效凭据且花钱。**落地前应当先用一次真实超长请求确认 DeepSeek 返回的
`body["message"]` 长什么样**，再决定关键词怎么写；在那之前它应当落到「原样给出 + 报 issue」那一支，
**不要凭 OpenAI 的文案去猜 DeepSeek 的措辞**。

## 修法

`stream_chat` 的 `except Exception` 改成按类型分派，返回一个结构化的错误
（至少 `kind` + `message` + `raw`），`StreamChunk` 增一个字段或让 `content`
承载「说明 + 原文」两段。**分类表本身放在 `provider/` 内**——它是 SDK 的知识，
不该漏到 `agent/` 或 `tui/`。

**更好的替代方案**：把重试从 SDK 默认接管过来，改成显式的
`openai.OpenAI(max_retries=0)` + 自己在 `stream_chat` 外层重试，
好处是**能在重试之间发一条 NOTICE 事件**（那条通道见本文开头），
用户看得见「正在重试」。代价是要自己实现退避与 `Retry-After` 解析。
**不建议第一版就做**——先把文案分类做掉，那是 90% 的体验收益。

## 代价与成对维护点

**代价：中等**（一张表 + 一处分派 + 若干测试；用本机 mock 服务器可完整覆盖，
无需真实凭据——本轮的复现脚本已经证明这条路走得通）。

**成对维护点：`paired-maintenance` 里没有一条涉及 `provider/`。**
这是本轮九条里耦合最低的一条，可以独立做。两处邻接约束：

- 若新增错误级别的展示通道，要看 **「新增 `_interact` 的交互种类」** 与
  tui-display 的 **F19/F20 三档分级**——`append_error` 是独立通道、不经 `_history_channel`，
  别把新错误塞进 `NOTICE` 的 `level` 里；
- **符号白名单（F29）**：文案里不要引入新的图形符号，
  错误靠「错误：」文字前缀辨认，那是它脱离颜色也能读的唯一依靠。

---

# C6 🟠 `app.run()` 没有兜底 —— ⚠ 原报告的因果链不成立

`rhinecode/__main__.py:160-170`

## 复现路径（实跑，两种形态）

```
=== ① 异常发生在消息处理器里（on_mount）===
   app.run() 正常返回，异常没有逃出来
   _shutdown 是否执行过：True
=== ② 异常发生在 _process_messages 上 ===
   逃出来的异常：RuntimeError: 模拟 _process_messages 阶段炸掉
   _shutdown 是否执行过：True
```

**两条结论，都与原报告相反：**

**① 终端不会坏。** `App.run_async`（textual 8.2.8）里那段
`finally: await asyncio.shield(app._shutdown())` 无条件执行——
两种形态下 `_shutdown` 都跑了，备用屏幕缓冲与 raw mode 都被复位。
「用户可能得敲 `reset`」这个后果**没有复现出来**。

**② catch-all 兜不住主要的那一类。** 绝大多数崩溃发生在消息处理器 / 事件回调里
（`on_mount`、`on_key`、Worker 的 done callback……），Textual 的 `_handle_exception`
会把它接住、走 `_fatal_error()`、把 app 关掉——**`app.run()` 正常返回，
外面包一层 `except Exception` 一个字都收不到**。
只有形态②（异常发生在 `_process_messages` 自身）才逃得出来，而那罕见得多。

**所以原报告的修法（`app.run()` 包 catch-all）对准的是次要形态。**

## 换成两条真缺口

### 缺口一：崩溃后进程退出码是 0

```
app.return_code = 1   （Textual 认为这是一次失败）
而 rhinecode/__main__.py 的 main() 在 app.run() 之后直接走 finally 结束，
既没读 return_code 也没 sys.exit —— 进程退出码 = 0
进程退出码: 0
```

`__main__.py` 在 `app.run()` 之后只有 `finally: result.cleanup()`
（`:169-170`），**从不读 `app.return_code`**。于是：

- CI、包装脚本、`&&` 链、systemd/supervisor 一律认为这次运行**成功了**；
- 上游那 5 处 `sys.exit(1)`（配置错误、装配错误）都做对了，
  **唯独运行期崩溃这一条漏了**——而它恰恰是最需要被上游知道的一种。

### 缺口二：崩溃时把带 locals 的完整回溯甩给用户，且没有日志

`App._fatal_error()` 用的是 `rich.traceback.Traceback(show_locals=True, …)`。
本轮实跑的输出里，`locals` 面板原样打印了 app 实例的内部状态。

**这在本项目有一个具体的安全含义**：`show_locals` 会渲染**每一帧**的局部变量。
若崩溃发生时栈上有 `DeepSeekProvider.__init__`（局部变量 `config` 含明文 `api_key`）、
或任何持有文件内容的帧，那些内容会被打进终端。
项目对 trace 的配置快照做了逐字段掩码（`redact_config`），
**而这条路径上没有任何掩码**。

同时 C5（日志设施是空的）在这里叠加：崩溃信息只在终端里、
用户一 `clear` 就没了，**没有任何地方留下副本**。

## 修法

1. **读退出码**：`app.run()` 之后 `if result.app.return_code: sys.exit(result.app.return_code)`，
   放在 `finally: result.cleanup()` **之后**（`sys.exit` 抛 `SystemExit`，
   写在 try 里会跳过清理）。**一行，收益立竿见影。**
2. **接管崩溃展示**：重写 `RhineApp._handle_exception(error)`——
   它是 Textual 明确留给应用的钩子，且**正是形态①真正经过的那个点**：
   把完整堆栈（不带 locals，或带但先掩码）写进一个文件，
   终端上只留「RhineCode 遇到未预期的错误已退出，完整堆栈见 `<路径>`」。
   与 C5 的 `--log-file` 是同一件事，**建议合并做**。
3. **形态②仍值得包一层 catch-all**，但它是补漏不是主菜：
   打印一行友好信息 + `sys.exit(1)`，别甩 traceback。

⚠ **别去掉 `except KeyboardInterrupt: pass`**。`:162-168` 的注释写明了
它覆盖的是「SIGINT 守卫还没装上 / 已经卸掉」的两个窄窗口，
那两个窗口里没有界面可提示，而**用户按的是 Ctrl+C，不是程序出了错**。

## 代价与成对维护点

**代价：小**（缺口一一行；缺口二与 C5 合并，中等）。牵动：

- **「`bootstrap.build_app` 的装配顺序」**：`cleanup` 的五步顺序与幂等守卫
  一个字都不要动，退出码的判断要排在它**之后**；
- **「`build_app` 新增参数 → `bootstrap.py` + `tests/e2e/host.py`」**：
  如果给日志路径加一个 CLI 参数并透传进 `build_app`，
  **e2e 宿主是它的第二个真实调用方**，漏改会让驱动设施与真实启动行为分叉。

⚠ **一条实测出来的验收限制**：e2e 宿主用的是 `app.run_test()`（`tests/e2e/host.py:606`），
**不是 `app.run()`**，两者的收尾语义不同。本轮试图用宿主复现「面板挂着时连按两次
Ctrl+C」的场景，宿主**干净退出了**——那不能证明真实入口也会。
**C6 与 C10-a 的判据都不能只靠 e2e 宿主，必须另跑真实 `app.run()`。**

---

# C8 🟠 主对话的 LLM 调用没有超时 —— 并推翻了代码注释给的理由

`rhinecode/provider/deepseek.py:72-73` + `rhinecode/config.py:232`

## 复现路径（实跑，本机 SSE 服务器三组对照）

`rhinecode/config.py:222-232` 的注释给出了「主对话刻意不设超时」的理由：

> ⚠ 也刻意**不给主对话用**：主对话的一次请求可能生成几分钟
> （模型在吐一份大文件的内容），给它设超时会把正常工作腰斩。

起一个会流式吐 SSE 的本机服务器，三组对照：

```
--- A 长但连续的生成（4 秒，每 0.2s 一块），超时设 2 秒
    超时设置 : 2.0
    耗时     : 4.2s   收到块数: 20
    错误     : （无）
--- B 中途卡住（发 2 块后停 30 秒），超时设 2 秒
    超时设置 : 2.0
    耗时     : 2.2s   收到块数: 2
    错误     : timed out
--- C 同 B，但不设超时（= 当前主对话的行为）
    超时设置 : None
    耗时     : 30.4s   收到块数: 2
    错误     : （无）
```

**A 是关键那一格：2 秒的超时没有腰斩一次 4 秒的生成。**

原因是 httpx 的 `read` 超时是**每次读操作**的预算，不是整次请求的总预算。
流式响应下，只要**两个数据块之间的间隔**没超过它就不会触发。
一个吐了五分钟的正常回答，块与块之间的间隔通常在毫秒级。

**所以那句注释描述的风险不存在**——它把「总时长上限」与「块间间隔上限」当成一回事了。
这不是笔误，是一个很自然的直觉错误（**本项目在 `run_shell_captured` 那次踩过
一个同类的、方向相反的坑**：以为 `timeout` 管用，实际是假的）。

同时 C 那一格坐实了当前行为的代价：**卡住的流会一直等下去**。
SDK 默认是 `read=600`，也就是**最长 10 分钟界面完全静止**，
而用户唯一的手段是按 `Esc`——但他大概率会先以为程序死了。

⚠ **一条尚未量化的边界**：`read` 超时同样管**首字节**（time-to-first-token）。
长提示词 + 高负载时段的 TTFT 可能有若干秒，所以取值不能定得太小。
本轮没有量过真实 TTFT 的分布，**定值之前应当先量一次**。

## 影响面

- **分类器有超时（10 秒，`bootstrap.py:464`），主对话没有**，同一个 Provider 类、
  同一个 `request_timeout` 字段，只是没传；
- `request_timeout` **刻意不进配置模板**（`config.py:225-228`），
  所以用户**没有任何配置手段**；
- 后果不是「慢」，是**分不清「模型在想」和「连接死了」**。
  这两种状态在界面上长得一模一样。

## 修法

1. `Config` 增一个**主对话专用**的超时字段（别复用 `request_timeout`——
   那个字段的注释明写它是给分类器的，两者的合理取值差一个数量级），
   缺省给一个宽松值（**60–120 秒**：它是「块间间隔」不是「总时长」，
   给 60 秒都极其宽松，而 10 分钟毫无意义）；
2. 进配置模板，注释里**必须写清它是「两个数据块之间的最长间隔」**——
   不写的话下一个人会照着「总时长」去调它，然后把它设成 3600；
3. 超时触发时走 C7 分类表里 `APITimeoutError` 那一行的文案。

**更好的替代方案**：分开 `connect` 与 `read` 两个值
（`httpx.Timeout(connect=10, read=90, write=90, pool=90)`）。
连接阶段该短（连不上就是连不上），读阶段该长。SDK 直接接受 `httpx.Timeout` 对象。

## 代价与成对维护点

**代价：小**（一个配置字段 + 一处传参 + 模板一段说明）。牵动：

- `paired-maintenance` 里**没有** `provider/` 或 `config.py` 的条目；
- 但有一条**没被登记、本轮才看清的**成对关系：
  **`config.py` 的字段 ↔ `docs/internals/config.md` ↔ 配置模板**。
  `request_timeout` 现在正是靠「刻意不进模板」维持一致的，
  一旦新增一个进模板的超时字段，这三处就成了新的成对维护点。
  **建议在 `paired-maintenance` 里补一条。**

---

# C10 🟠 三条同源的资源与生命周期问题

## C10-a `tui/app.py:2815` 的 `box["event"].wait()` 无超时

### 复现路径（实跑）——⚠ 后果比原报告严重

原报告说后果是「线程停到天亮」。实测是**整个进程退不掉**：

```
Python 3.11.9
⚠ app.run() 在 25 秒后仍未返回 —— 退出被那个 wait() 挡住了
ThreadPoolExecutor 线程 daemon = False
3.11 的 shutdown_default_executor 签名: (self)
```

链条是这样的：

1. `run_worker(thread=True)` 最终走 `loop.run_in_executor(None, …)`
   （textual `Worker._run_threaded`），用的是 **asyncio 的默认线程池**；
2. `ThreadPoolExecutor` 的线程**不是 daemon**（实测 `daemon = False`）；
3. `asyncio.run()` 收尾时调 `loop.shutdown_default_executor()`，
   而 **Python 3.11 的这个方法没有 timeout 参数**（3.12 才加），
   于是它**无限期等待**那个停在 `Event.wait()` 上的线程。

结果：`app.run()` 永不返回 → `__main__.py:170` 的 `finally: result.cleanup()`
**永不执行** → MCP 子进程不回收、会话锁不释放、trace 文件句柄不关闭。
用户看到的是「按了退出，程序卡死了」。

### 触发路径

`action_request_quit`（`rhinecode/tui/app.py:1763-1810`）在连按两次时直接
`self.exit()`，**不检查 `_pending_interaction`**；而 `ctrl+c` 那条绑定是
`priority=True`（`:182`），面板持有焦点时照样能触发。
`on_unmount`（`:860-868`）也不结算待决交互。
所以「面板挂着 → 连按两次 Ctrl+C」这条路径在真实入口下会踩中。

⚠ **e2e 宿主复现不了这一条**（它走 `app.run_test()`，收尾语义不同，
本轮实测宿主干净退出了）。上面的证据来自真实 `app.run(headless=True)`。

项目**已经修过它的一个实例**——`conversation.py:1966-1983` 记录的
「半夜队友消息唤起主对话 → 面板弹出 → 没人在 → 线程停在 `Event.wait()` 上，
**没有超时**」。但修的是**触发条件**（无人轮不传 `ask` / `clarify`），
**不是 `wait` 本身**。任何新的「能弹面板但没人应答」的路径都会重现。

### 修法

**首选（成本最低、覆盖最全）：让退出路径负责结算。**
`action_request_quit` 在 `self.exit()` 之前，若 `_pending_interaction is not None`
就先走一次 `_resolve_interaction(None, source="shutdown")`——
`None` 在三类面板上的语义都已经是「取消 / 拒绝 / 跳过」，语义现成。
**注意它必须走 `_resolve_interaction`**，那是唯一会 `box["event"].set()` 的地方。

**次选（纵深防御）：给 `wait` 一个循环 + 短超时。**
`while not box["event"].wait(0.5): if <退出中> or <取消信号>: break`。
它能覆盖「将来某条没想到的路径」，代价是引入一个轮询。
⚠ **不要写成裸的 `wait(timeout=N)` 然后按超时返回一个默认结果**——
那等于「用户没答，我替他答了」，在确认面板上就是**替用户点了放行或拒绝**。
超时只能用来**跳出等待**，不能用来**编造结果**。

### 代价与成对维护点

**代价：小**（首选方案约五行）。牵动：

- **「产品侧交互结算点新增来源取值 → `tui/app.py` 三处 + `tests/e2e/protocol.py`
  的取值集合 + 断言词汇」** —— 新增 `source="shutdown"` **正好命中这条**，
  五处要一起改，漏了 e2e 那侧的表现是「驱动设施认不出这个来源」；
- **「`_settle_session` 是会话面板结算的唯一入口」** —— 会话选择面板走的是
  另一条结算路径（`_settle_session`，`:2212`），**它同样在退出时不被结算**。
  修的时候两条路都要看，只修一条是半个修复；
- **「新增 `_interact` 的交互种类 → `_NOTIFY_KINDS`」** —— 本次不新增种类，不适用。

---

## C10-b `subagents/tasks.py:237` 任务表无界增长 —— ⚠ 作为内存问题应当降级

### 复现路径（实跑，量了增速）

```
空表                :        64 字节
  1 条委派之后     :    11,253 字节   条数=1
 10 条委派之后     :    39,624 字节   条数=10
 50 条委派之后     :   161,376 字节   条数=50
200 条委派之后     :   624,018 字节   条数=200

/clear（begin_session）之后：条数=200  占用=624,018 字节
全文件里的删除操作：一处都没有
```

（每条按「一段长任务描述 + 20 次工具调用 + 一段 30 行结论」构造。）

**约 3 KB / 次委派。** 一次一千次委派的超长会话也只有 3 MB。
**作为内存问题它可以忽略**，原报告「长会话内存单调增长」的措辞
让它听起来比实际严重。真实后果在别的两处：

### 真实后果一：`/agents` 的输出无界，且含已清空会话的任务

`subagents/report.py:212-216`：

```python
if tasks:
    lines.append(f"本次运行的任务（{len(tasks)} 个）：")
    for record in tasks:
        lines.append(_task_line(record))
```

**每条任务一行，没有上限、没有过滤代号。** 一次跑过 60 次委派的会话，
`/agents` 会刷出 60 行任务，其中大半来自 `/clear` 之前——
而用户敲 `/agents` 想知道的是「**现在**有谁在跑」。

对照同文件的 `recent_tools`（`:365-367`）是有界的
（`[-ACTIVITY_RECENT_LIMIT:]`）。**「一切展示都有界」这条只落到了列表层，没落到字典层。**

### 真实后果二：`/clear` 之后，上一段对话的任务原文与结论原文仍在内存里

`begin_session`（`:276-310`）只做 `self._epoch += 1`，
让旧记录**不再被交付**，但记录本身一条不少。
`TaskRecord.task_text` 与 `.conclusion` 都是**对话原文**，
而结论里常常复述子 Agent 读过的文件内容。

⚠ **表述要谨慎**：`CLAUDE.md` 的 C15 安全边界第 ⑥ 条列举的是
「花名册、共享清单、未读消息、待命队员保管的完整对话历史」，
**任务表不在那张清单里**——所以这不是一条被违反的承诺，
而是**那张清单可能列漏了一项**。值得在修 C10-b 时一并确认是有意还是遗漏。

### 修法

**不要按条数简单裁剪。** 有三条约束：

1. **运行中的记录一条都不能删**（`gate.py:121,133,161` 在读它判断「还要不要等」）；
2. **终态但未交付的不能删**（`take_deliverables` 靠它），
   ⚠ 成对维护点「新增『会话切换』入口」那条明确警告过：
   **别改成「切换时把当前任务标成已交付」**——取消是异步的，
   被取消的子 Agent 可能在切换返回之后才走到终态，那种写法覆盖不到它；
3. **删掉记录会让完成通知失去成本数字**——成对维护点
   「活动区终态行与历史区完成通知的成本数字必须同源 → `TaskManager.row_of`」，
   两处都从记录上取。

**建议的最小做法（推荐）：先只治展示，不治存储。**
`render_report` 只列**当前代**的任务，并对历史任务给一行汇总
（「另有 N 条属于此前的会话，已不再交付」）。
零风险、零成对维护点牵动、解决了用户真能看到的那个问题。

**若确实要治存储**：在 `begin_session` 里淘汰「**非当前代 且 终态 且 已交付**」的记录，
三个条件缺一不可。⚠ 那会新增一条成对维护点（淘汰条件 ↔ `take_deliverables` 的判据），
**且必须补一条反证**：断言一条「终态但未交付」的旧记录**不会**被淘汰。

### 代价

**展示侧：极小。存储侧：中等**（三个条件 + 一条反证 + 与三处读取方对账）。
**建议只做展示侧**——存储那 3 MB 不值得为它引入一个新的成对维护点。

---

## C10-c `memory/manager.py:273` 记忆线程 daemon 化

### 复现路径（实跑，12 次里 10 次留下一个空文件）

```
原文件: '---\nname: 老记忆\n---\n这条记忆很重要。\n'
open(mode='w') 之后、还没写入时: ''
→ 只要线程停在 open 与 write 之间，这条记忆就没了

主进程退出后，被写的文件内容 = '完整内容 145'
12 次里有 10 次落在「已截断、未写入」的窗口内
```

机制：`Path.write_text` 先以 `"w"` 打开（**当场截断**）再写。
CPython 在解释器终结开始后，daemon 线程一旦尝试获取 GIL 就会被直接结束——
**可以停在这两步之间**。

`_apply_one`（`rhinecode/memory/manager.py:386`）写单条记忆、
`_rebuild_index_file`（`:409`）写索引，**两处都是 `write_text`**。

⚠ 上面那 10/12 的命中率是**刻意放大过的**（子线程在截断与写入之间 `sleep(0.002)`），
它证明的是**这个窗口真实存在且会被击中**，不是真实概率——见下面「窗口有多宽」。

### 影响面

- **丢失的是持久化数据，而且不可恢复。** 被截断的是一条**已经存在的**记忆
  （更新场景），或整份 `MEMORY.md` 索引；
- **锁也不会释放。** `_apply_actions`（`:350-367`）的
  `finally: lockfile.release(lock)` 同样跑不到，`.lock` 文件留在原地。
  `MEMORY_LOCK_STALE = 600.0`（`:47`），所以**下一次运行的记忆更新会被挡最多 10 分钟**，
  而用户看到的现象是「记忆功能好像不工作了，过一会儿又好了」；
- **窗口有多宽？** `_update_memories` 的时间绝大部分花在 `_decide_actions`
  的那次 LLM 调用（秒级），写盘只有毫秒级。所以**真实命中概率低**——
  但它是「低概率 × 不可恢复」，与「高概率 × 可恢复」不是一回事。

`subagents/runner.py:1005-1006` 对子 Agent 线程的 daemon 化有明确的取舍记录，
**但那里写的是临时状态**（跑一半的子任务丢了就丢了），
记忆写的是**持久化数据**。**取舍不同，策略却抄了同一个。**

### 修法

**首选：在 `cleanup` 里等它一小会儿。**
`MemoryManager` 保留线程引用，`close()` 里 `thread.join(timeout=5)`。
`bootstrap.py:752-755` 已经把 `memory_manager.close()` 单独 try 住了，
挂点现成。⚠ **必须给 timeout**，否则一次卡住的 LLM 调用会让程序退不掉
——那正好变成 C10-a 那个形态。

**次选（正交，建议一并做）：把写盘改成原子替换。**
写到同目录的临时文件再 `os.replace`。
`os.replace` 在同一文件系统内是原子的，被中断只会留下一个孤儿临时文件，
**原文件一个字节都不会少**。⚠ 这与**已知项 #3**
（`write_file` / `edit_file` 的文件系统级原子写入）是同一件事的两个落点，
**建议合并立项**——写一次 `atomic_write_text` 辅助函数，两处都用。

**⚠ 不建议把线程改成非 daemon。** 那会让「记忆线程正在等一个卡住的 LLM 调用」
直接演变成「程序退不掉」，与 C10-a 同型。**daemon + join(timeout) 才是对的组合。**

### 代价与成对维护点

**代价：小**（join 五行；原子写入是独立立项，中等）。牵动：

- **「写盘权收拢在 manager 的锁临界区内——拿锁的人就是写盘的人」**
  （`CLAUDE.md` 架构表 Memory 行的致命不变量）——改写盘实现时
  **必须仍在 `_apply_actions` 的锁内闭合**，别把原子替换挪到锁外；
- **「`bootstrap.build_app` 的装配顺序」**——`cleanup` 五步顺序不能动，
  join 加在第②步 `memory_manager.close()` 里面，不要新加一步。

---

# E1 / E2 两个 ruff 小问题

## E1 ✅ 坐实：`conversation.py:1727` 悬空类型注解

```
E1 复现 -> NameError: name 'AskFn' is not defined
```

`typing.get_type_hints(ConversationManager._build_ask)` 当场 `NameError`。
`AskFn` 定义在 `rhinecode/agent/loop.py:211`，`conversation.py` 全文
**只在这一行出现过这个名字**（`grep` 一处命中，即注解本身）。

**影响面**：运行时无害（字符串注解不求值），但任何做 introspection 的东西都会炸——
`get_type_hints`、运行期反射、以及**未来若要给协调层加一层运行期校验**。
ruff 与 mypy 用不同机制**各自独立报了它**。

**修法**：`if TYPE_CHECKING:` 块里 `from rhinecode.agent.loop import AskFn`。
⚠ **别改成 `Callable[..., bool]` 之类的泛化写法**——同文件 `:112` 已有语义相近的
`ConfirmCallback`，再引入第三种表达同一件事的写法只会让下一个人更困惑。
**代价：一行。无成对维护点。**

## E2 ⚠ 降级：`config.py:347` 漏 `from` —— 「原始 traceback 丢失」不成立

```
E2 复现 -> FileNotFoundError.__cause__ = None   __context__ = FileNotFoundError
对照（同一 try 的下一分支）-> ValueError.__cause__ = ParserError
```

`00-baseline.md` 写「后果是原始异常的 traceback 丢失」——**不成立**。
Python 的**隐式**异常链仍然生效：`__context__` 指向原始异常，
traceback 照样打印 `During handling of the above exception, another exception occurred`。

**真实差别只有一句话**：显式 `from e` 打印的是
`The above exception was the direct cause of the following exception`
（这是**因果**），隐式的打印
`During handling of the above exception, another exception occurred`
（这是**巧合**）。**信息一个字都没少，只是语气不对。**

**它仍然值得改**，理由不是「丢信息」而是**同一个 try 的两个分支风格不一致**
（`:347` 没写，`:349` 写了），那是笔误的典型痕迹。

**修法**：`except FileNotFoundError as e: raise FileNotFoundError(...) from e`。
**代价：一行。无成对维护点。**

---

# 附一：本轮对已有报告的更正（共七处）

**这七处都不是笔误。** 它们的共同点是：**看代码得到的结论，与让代码真跑一遍得到的结论不一样。**
定位（`README.md` 做的）看到的是代码长什么样；坐实（本轮做的）看到的是它实际怎么表现。

| # | 原文说法 | 实测 | 为什么会差 |
| --- | --- | --- | --- |
| 1 | **C3**：「两条路径表现不同」（串行有 try/except、并发刻意不加） | **表现相同**。并发路径外层 `future.result()`（`loop.py:2121-2124`）照样兜成同一句「工具执行异常」 | 只读了内层 `_run` 的注释，没往下读十行 |
| 2 | **C3**：护栏应断言「标志与形参**一一对应**」 | 会**误报** `TodoWriteTool`——它保留 `plan_stage` 是 `paired-maintenance` 明令保护的「刻意」。正确契约是**单向**的 | 没跑过就不知道有反例 |
| 3 | **C3**：遍历 `ToolRegistry.default()` | 只有 **7 / 20**，且**带 `plan_stage` 的 5 个一个都不在里面** | `default()` 的名字听起来像「全部」 |
| 4 | **C6**：「崩溃可能把用户终端搞坏」 | **不复现**。textual `run_async` 的 `finally` 无条件 `_shutdown()`，两种形态下终端都复位了。且 catch-all **兜不住主要形态**（Textual 自己接住、`app.run()` 正常返回） | 「TUI 崩溃会搞坏终端」是个很强的通用直觉，但这个框架处理了 |
| 5 | **C7**：「且无重试退避」 | SDK **已经在重试**：`max_retries=2`、`0.5×2ⁿ` 退避、认 `Retry-After`。实测一次 429 发了 **3 次** HTTP 请求 | 代码里看不到重试，就以为没有 |
| 6 | **C8**：代码注释称「设超时会把正常工作腰斩」 | **实测推翻**：2 秒超时没有腰斩一次 4 秒的连续生成。流式下它是**块间间隔**上限，不是总时长上限 | 「超时」这个词天然指向总时长 |
| 7 | **E2**：「原始异常的 traceback 丢失」 | **没丢**，`__context__` 仍在，只是链接语气从「因果」变成「巧合」 | `from e` 的作用被高估了 |

**外加两处不算错、但坐实之后要改口的：**

- **B1** 的复现门槛比原报告低得多：**两种形态不需要任何 IO 故障**，
  只要用户手改过一次 `permissions.local.yaml`。「不能复现」应改成「三种形态全部可复现」。
- **C10-b** 作为**内存**问题可以忽略（3 KB / 次委派）。它真正的后果在
  **`/agents` 输出无界**与**`/clear` 之后任务原文仍在**这两处，应当改写而不是删掉。

# 附二：修的顺序（在 `README.md` 那张表上的调整）

`README.md` 的六批分批大体成立，本轮建议三处调整：

1. **C3 与 C4 仍是性价比最高的一批**（约 35 行代码钉住两个「漏改一律不报错」），
   但 **C3 的写法必须按本文改**，照原写法写出来的护栏既误报又漏覆盖 13 个工具；
2. **C6 应当拆开**：「读退出码」是一行、立刻可做；「接管崩溃展示」应当**并进 C5**
   （日志设施）一起做，单独做会造出一个只有一个用户的日志文件；
3. **C10-c 的原子写入应当并进已知项 #3**（`write_file` / `edit_file` 的原子写入）——
   两处要的是同一个 `atomic_write_text`，分开做等于写两遍。

**若只做一件事（在 R3 覆盖的这九条里）**：**C10-a**。
它是这九条里唯一一条会让用户**按了退出、程序不退**的，
而且顺带让 `cleanup` 的五步一步都跑不到（MCP 子进程、会话锁、trace 句柄全都留着）。
修法是五行，且首选方案不引入任何轮询。

# 附三：复现脚本清单

全部在本次会话的 scratchpad 下，**产品代码零改动**（C4 的两次变异已还原，`git diff` 为空）。

| 脚本 | 对应条目 | 是否需要网络 |
| --- | --- | --- |
| `repro_b1b.py` | B1 三种触发形态 | 否 |
| `repro_b2.py` / `repro_b2b.py` | B2 文件规则侧 / Skill 预授权侧 | 否 |
| `repro_c3b.py` | C3 全量扫 20 个 `Tool` 子类 | 否 |
| `repro_c3c.py` | C3 两条路径的失败形态 | 否 |
| （变异实测，无脚本） | C4 两次 re-export + 全量测试 | 否 |
| `repro_c7.py` | C7 四类错误的用户可见原文 | **是**（真发四次请求，用无效 key） |
| `repro_c6.py` / `repro_c6b.py` | C6 异常逃逸 / 退出码 | 否 |
| `repro_c8.py` | C8 本机 SSE 服务器三组对照 | 否（回环） |
| `repro_c10a.py` | C10-a 无超时 wait 挡住退出 | 否 |
| `repro_c10b.py` | C10-b 任务表增速 | 否 |
| `repro_c10c.py` | C10-c daemon 线程写盘窗口 | 否 |

⚠ **除 `repro_c7.py` 外全部不需要凭据**，可以直接重跑核对。
`repro_c7.py` 用的是一个明显无效的 key（`sk-invalid-key-for-testing-0000…`），
四次请求全部在认证阶段被拒，**不消耗额度**。

⚠ **`repro_c8.py` 那份本机 SSE 服务器值得留下来**——它让「Provider 层在各种网络异常
下的行为」第一次变得可测，而 C7 的错误分类表若要写护栏，需要的正是这个东西
（本项目此前验 Provider 只能靠真实凭据或剧本 Provider，两者都验不到网络层的异常）。
