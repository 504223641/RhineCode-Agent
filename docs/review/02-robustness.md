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

---
---

# R4 · 剩余异常吞噬 + flaky 逐条评估

> 2026-08-30 实测。本轮**不改任何代码，也不改任何测试**——包括那条真复现出来的
> flaky，它只被记录、没被动过。
>
> 与上面 R3 的关系：R3 坐实的是 `README.md` 已经点名的九条，R4 走的是**没人看过的
> 那一片**——121 处 `except Exception` 里「吞掉之后做了别的事」的那 67 处，以及
> 五条被点名为高风险但从未评估过的用例。
>
> ⚠ **本轮的三个数字与题面给的口径对不上，先更正**，见下一节。
> ⚠ **本轮真复现出一条 flaky**（`tests/test_bootstrap.py`），且它的**失败形态比
> 「偶尔红一下」更糟：为排查而写的那段诊断代码自己也会超时，把它要给的证据丢掉。**

## 先更正三个数字

题面（`NEXT.md` 的 R4 一键 Prompt）写的是「121 处 `except Exception`，其中 40 处
直接 `pass`，**剩下 81 处**」。用 AST 重数一遍，三个数字都要改：

| 口径 | 题面 | 实测（2026-08-30） | 差在哪 |
| --- | --- | --- | --- |
| `except Exception` 总处数 | 121 | **115** | 121 是 `grep` 行数去重后的值，**其中 6 处在 docstring / 注释里**（`conversation.py:524`、`glob_files.py:32`、`grep_content.py:36`、`tui/app.py:1444`、`web/manager.py:84`、`search_manager.py:172`——都是在**讲解**这个模式，不是在用它） |
| 直接 `pass` 的 | 40 | **50** | 50 与 `00-baseline.md` 记的 ruff `S110 = 50` 一致。40 这个数不知从何而来，且它与 81 相加也不等于 121 |
| 「吞掉后做了别的事」的 | 81 | **67** | = 115 − 50 + 2（另有 2 处 `except BaseException`：`subagents/runner.py:911`、`run_command.py:290`，`grep "except Exception"` 抓不到它们，但它们同属本轮范围） |

**这不是吹毛求疵**：81 与 67 差 14 处，照 81 去核对会一直觉得「还有十几处没找到」。
复现方式在附四。

⚠ **50 处 `pass` 的分类结论不受影响。** 逐个核过它们的所在文件，仍然全部落在题面
列的五类刻意 fail-safe 里（观测漏斗 `_safe_emit` / Hook 分发点 / TUI 渲染兜底 /
资源关闭阶段 / 进程树强杀），**没有新增可疑项**。多出来的 10 处集中在
`tui/app.py`(8) 与 `mcp/transport.py`(5) 这两个本来就是大户的文件上。
另：题面说「4 处可疑已记在 `docs/review/README.md`」——**`README.md` 里找不到这四处**
（全文搜 `except` 只有三处提及，都是 C3 / C7 的行号引用）。那份清单要么没写进去、
要么写在别处，本轮无法核对。

---

# 第一部分 · 67 处「吞掉之后做了别的事」

## 判据

题面给的两问就是判据，逐处只回答这两句：

1. **吞掉之后做的那件事，是不是让用户/模型看到了真实情况？**
2. **还是把一个真故障伪装成了正常结果？**

按回答把 67 处分成六类。**要紧的只有 E 类那 8 处**，其余 59 处的答案都是「看到了」。

| 类 | 含义 | 处数 | 结论 |
| --- | --- | --- | --- |
| **A** | 转成 `ToolResult(ok=False, ...)` 回灌模型，带具体原因 | **22** | ✅ 干净。这是 `Tool.execute` 的契约（「绝不向上抛」），模型拿到的是可据以决策的失败 |
| **B** | 转成一句可读的中文原因给**用户**（启动通知 / 系统行 / `/hooks` / `/mcp` / 结论正文） | **17** | ✅ 干净 |
| **C** | fail-safe **偏严**：判拒绝 / 不删 / 判命中 / 不覆盖 | **12** | ✅ 干净，且方向正确（错拒可恢复，错放不可） |
| **D** | 降级到一个较弱的结果，但**降级这件事本身有痕迹** | **5** | ✅ 干净 |
| **E** | **降级且无痕**——观察者看到的东西与「一切正常」无法区分 | **8** | ⚠ **本轮的全部产出在这一类** |
| **F** | 纯冗余：被吞的那个东西根本不会抛 | **3** | 无害，但也无用 |

**59 / 67 干净，是个很高的比例。** 尤其 A 类那 22 处几乎逐字相同
（`return ToolResult(ok=False, output=f"…失败：{exc}")`），说明「工具绝不向上抛」
这条契约是真的被贯彻了，不是写在 docstring 里的。

⚠ **但比例高恰恰是 E 类值得单独看的理由**：一个通篇都在「失败要说出来」的代码库里，
剩下这 8 处的沉默不像是风格，更像是漏了。

---

## R4-1 🔴 一个存成 GBK 的 `RHINE.md` 会让三层项目指令**全部**消失，而 `/memory` 里连一行都看不到

**位置**：`rhinecode/memory/manager.py:134-136`

```python
try:
    self._instructions = load_instructions(self._user_dir, self._project_root)
except Exception:
    self._instructions = LoadedInstructions(text="", layers=[])
```

### 为什么这一处与别处不同

`load_instructions`（`memory/instructions.py:74`）**本身写得很好**：三层逐层处理，
每层的读取失败进 `layer.errors`、`@include` 越界/成环/超深度也进 `layer.errors`，
缺层记 `loaded=False`。它被设计成**永远返回一个能说清楚发生了什么的对象**。

问题在于它只 `except OSError`：

```python
try:
    raw = path.read_text(encoding="utf-8")
except OSError as e:
    layer.errors.append(f"读取失败：{e}")
```

而 **`UnicodeDecodeError` 是 `ValueError` 的子类，不是 `OSError`**。于是一个非 UTF-8
的 `RHINE.md` 会让异常**穿过**这层精心设计的逐层容错，被最外面那个
`except Exception` 接住，然后连同**另外两层已经读好的内容**一起被
`LoadedInstructions(text="", layers=[])` 整个替换掉。

⚠ **`layers=[]` 是这条的关键，不是 `text=""`。** `layers` 空掉之后，`/memory` 报告里
那个负责说真话的循环（`manager.py:504`）一次都不会执行。

### 复现（实跑，三组对照）

```python
# 用户级 RHINE.md 正常 UTF-8；项目根 RHINE.md 用 GBK 存（中文 Windows 上极常见）
(user / "RHINE.md").write_text("# 用户级指令\n请始终用中文回答。\n", encoding="utf-8")
(proj / "RHINE.md").write_bytes("# 项目指令\n禁止推送到 main。\n".encode("gbk"))
mgr = MemoryManager(P(), "m", proj, user, memories_enabled=False)
mgr.startup(resume_latest=False, history=[])
```

实跑输出：

```
startup() 返回： None                 ← 没有任何提示
custom_instructions() 长度： 0        ← 用户级那份也一起没了
```

`/memory` 报告的对照——**三组的输入只差一个文件的编码**：

```
==== A 三层全缺（真的没有 RHINE.md）====
RHINE.md 项目指令：
  [用户级] …\RHINE.md — 未找到
  [项目级 .rhinecode] …\RHINE.md — 未找到
  [项目根] …\RHINE.md — 未找到

==== B 一层正常 + 一层坏编码 ====
RHINE.md 项目指令：
                                      ← 整段空白，一行都没有

==== C 一层正常 + 一层 @include 越界（load_instructions 自己兜住的那类）====
RHINE.md 项目指令：
  [用户级] …\RHINE.md — 已加载（5 字符）
  [项目级 .rhinecode] …\RHINE.md — 未找到
  [项目根] …\RHINE.md — 已加载（25 字符）
    警告：@include 目标不存在，未展开：include
```

**B 是唯一什么都不说的那一组，而它恰恰是唯一「用户的指令确实存在、却没生效」的那一组。**
A 和 C 都如实说了。

### 同一个输入，别的加载器都会说话

```
permissions.yaml(GBK) -> ([], ["配置文件解析失败（…\permissions.yaml）：
                                'utf-8' codec can't decode byte 0xcf in position 4: …"])
```

四个 YAML 加载器（`permission/config.py:257`、`hooks/config.py:195`、
`mcp/config.py:213`、`mcp/auto_config.py:445`）在**完全相同的输入**下都返回一句
可读的中文原因——它们正是题面里「已确认干净、不用再看」的那批。
**`RHINE.md` 是唯一的例外**，而它是三层配置里唯一直接决定模型行为的那份。

### 影响面

| 观察点 | 看到的 |
| --- | --- |
| 启动提示 | 无 |
| 系统提示词 | 「自定义指令」槽位**整个消失**（`prompt/builder.py` 的 `build` 第 2 步：content 去空白后为空的模块直接跳过） |
| `/memory` | 「RHINE.md 项目指令：」后面**一片空白** |
| `--trace` | 无（`load_instructions` 不埋点） |
| 模型行为 | 「它怎么突然不按我写的规矩来了」 |

**没有任何一条通路会说出真相。** 而这正是 c9 存在的理由——RHINE.md 是用户表达
「这个项目要怎么做事」的唯一手段。

⚠ **触发概率不低。** 项目主力平台是中文 Windows，而记事本、旧版编辑器、
`cmd` 的 `>` 重定向默认都写 GBK/ANSI。用户**不需要做错任何事**，只需要用一个
没设 UTF-8 的编辑器新建一次 `RHINE.md`。

### 修法

**两处都要改，而且顺序有讲究**：

1. **`instructions.py:105` 的 `except OSError` 扩成 `except (OSError, ValueError)`**
   ——让编码错误落回它本来就该落的地方（`layer.errors`），报告与 A/C 两组一致。
   这一处修完，本条的主要触发路径就没了。
2. **`manager.py:136` 的兜底不要丢 `layers`**——改成保留一份「三层，全部
   `loaded=False`，第一层带一条 `errors=["加载指令时发生意外错误：…"]`」的结构，
   并让 `startup()` 把这句话放进它的返回值（那是启动提示的既有通道，
   `bootstrap.py` 已经在用）。

⚠ **只做 ① 不够**：`_expand_includes` 里还有别的可能抛非 `OSError` 的路径
（`_safe_resolve` 只吞 `OSError`，而 `Path.resolve` 在 Windows 上对畸形路径会抛
`ValueError`）。② 是纵深防御，且它才是「以后再冒出一种没想到的异常时，
用户仍然看得见」的那道。

### 代价与成对维护点

改动很小（一个 `except` 元组 + 一个兜底对象 + 一句提示），但它引入**一处新的成对维护点**：
`instructions.py` 的每层容错口径与 `manager.py` 的兜底对象**必须保持「都产出 layers」**。
建议连一条反证测试一起加：**喂一个 GBK 的 `RHINE.md`，断言 `/memory` 报告里
仍然出现三行层信息且至少一行带「警告」**——这条在今天会红。

---

## R4-2 🟠 `write_file` / `edit_file` 在「已经把文件截断了」之后，报告的是「写入失败」

**位置**：`rhinecode/tools/write_file.py:102`、`rhinecode/tools/edit_file.py:194`

两处都是标准的 A 类写法，模型拿到 `ToolResult(ok=False, output="写入文件失败: …")`。
**问题不在它说了失败，而在于它说的失败与磁盘上的真实状态对不上。**

```python
with open(abs_path, "w", encoding="utf-8") as f:   # ← 这一行就已经把文件截断成 0 字节
    f.write(content)                                # ← 抛在这里 / 或抛在退出 with 的 flush 上
```

`open(..., "w")` 是**先截断后返回**的。因此从 `open` 成功到 `write`+`flush` 完成
之间的任何失败（磁盘满 ENOSPC、配额、网络盘掉线、Windows 上被杀软锁住），
留下的都是一个**空文件或半截文件**，而模型收到的字面意思是「这次写入没有发生」。

模型据此做的下一步通常是**换个方式重试、或者放弃这一步继续往下走**——两种都建立在
「原文件还在」这个错误前提上。放弃并继续是三种里最坏的：源文件已经没了，
而对话里没有任何一句话提到过它没了。

### 与已知项 #3 的关系（不是同一条）

已知项 #3 是「`write_file` / `edit_file` 的文件系统级原子写入」，那条讲的是
**持久性**（崩溃/断电时的完整性）。本条讲的是**报告的准确性**：
即使不做原子写入，这个 `except` 也可以说实话。两条可以分开做，而且本条**便宜得多**。

### 修法（两种，都不必上原子写入）

- **便宜的**：把 `open` 从 `try` 内部单独拆出来，用一个布尔记住「文件是否已经被截断」，
  异常分支据此给两种不同的文案——
  `"写入文件失败（原文件未改动）: …"` 与
  `"⚠ 写入中途失败，{path} 已被截断/部分写入，内容不完整: …"`。
  改动约 6 行，**模型立刻能据此做对的事**（后者它会去补救，前者它会重试）。
- **彻底的**：同目录临时文件 + `os.replace`，即已知项 #3。那时异常分支可以
  **诚实地**说「原文件未改动」，因为那就是事实。

⚠ **不要只做「彻底的」而跳过文案**：原子写入之后文案仍然要改（从含糊的
「写入文件失败」改成明确的「原文件未改动」），否则模型还是不知道该不该补救。

### 代价与成对维护点

`write_file` 与 `edit_file` **必须同改**——两处同型、隔着一个文件，是典型的
「只改一处不报错」。护栏可以用 mock 让 `f.write` 抛，断言两个工具的 output
里都出现「已被截断」字样。

---

## R4-3 🟠 隔离工作区的启动清理有**三层** fail-safe，三层全部通向「什么都没发生」，**而且连一条 trace 都不产**

**位置**：`bootstrap.py:581`、`worktree/cleanup.py:165`、`worktree/cleanup.py:118`

三层套在一起：

| 层 | 代码 | 失败后 |
| --- | --- | --- |
| 最外 | `bootstrap.py:581` | `worktree_cleanup = None` → 下一行 `if ... is not None` 直接跳过通知 |
| 中间 | `cleanup.py:165` `_iter_candidates` 失败 | `return CleanupReport()`（`is_empty` 为真） |
| 最内 | `cleanup.py:118` `_guess_branch` 失败 | `return ""` |

⚠ **真正让它无法排查的是第四件事**：埋点被 `is_empty` 挡在门外——

```python
if recorder is not None and not report.is_empty:
    recorder.emit(TraceEventType.WORKTREE_CLEANUP, ...)
```

失败路径返回的正是一个 `is_empty` 的空报告，**于是 `--trace` 里也一个字节都没有**。
界面、`/agents`、trace，**三条出口同时沉默**。

### 这个项目自己已经写下了正确的做法

`hooks/manager.py:385-388`，同一件事，结论相反：

> **零命中也产出这条事件**：它是排查「我的 hook 为什么没跑」的第一现场。
> 看到「命中 0 条」说明事件确实触发了、是条件没匹配上；一条都看不到则说明
> 分发点压根没接上——**两种情况的排查方向完全不同**。

把「hook」换成「清理」，这段话逐字成立：看到「扫了 0 个」说明清理跑了、只是没有
过期项；一条都看不到则说明它压根没跑成——而现在这两种情况**产出完全一样**。

### 影响面

后果是**磁盘单调增长且无人知晓**。隔离工作区是**完整的源码 checkout**
（`CLAUDE.md` c14 第 ⑥ 条：「它是一份完整的源码 checkout，且环境初始化可能把本地配置
（含密钥）复制进去」），一个中等项目每个几十到几百 MB。清理静默失效之后，
`.rhinecode/worktrees/` 会一直攒——**而它在 `.gitignore` 里，`git status` 也不会提醒**。

严重性定在 🟠 而不是 🔴，因为：① 触发它需要 `_iter_candidates` 或 `scan_and_clean`
整体抛异常，这本身不常见（每个条目已经单独兜过一层，`cleanup.py:214`，
那一层是**留痕**的、写进 `kept`）；② 后果是空间不是正确性。

### 修法

**只改一处就够**：把 `cleanup.py` 末尾的埋点条件从 `not report.is_empty` 改成
无条件，并给失败路径也走一次埋点（带 `error=` 字段）。`bootstrap.py:581` 那层
可以顺手把异常文本挂进 `add_startup_notice`——**它同一个文件的 `:472`
（分类器建不起来）已经是这么做的，还写了理由**：

> ⚠ **整段 fail-safe**：分类器建不起来绝不能阻断启动。
> 但**必须说出来**——静默不启用等于静默关掉一层安全机制。

同一个文件里，109 行之隔，一处说了一处没说。

### 代价与成对维护点

小。⚠ 但注意**别顺手把 `is_empty` 这个门槛整个删掉**——它挡的是
「每次启动刷一堆『这个还新鲜』」（`cleanup.py:174` 写了理由），那个理由仍然成立。
要区分的是「空报告」与「失败」，不是「空」与「非空」。

---

## R4-4 🟠 `glob` / `grep` 的过滤器崩了，会被报成「被权限规则拒绝」

**位置**：`rhinecode/tools/glob_files.py:74`、`rhinecode/tools/grep_content.py:83`

```python
def _is_allowed(self, rel_path, base) -> bool:
    try:
        return bool(self._path_filter(rel_path, base))
    except Exception:
        return False
```

**fail-safe 的方向是对的**（宁可少返回也不错放），项目自己在
`glob_files.py:32` 的 docstring 里就把这条写清楚了，甚至预言了后果：

> 一个漏改的过滤器会**把所有文件都判成拒绝**，而工具照常返回 ok=True

**没被写下来的是文案。** 计数最后是这么呈现的（`glob_files.py:130,143`）：

```
…（跳过 N 个被权限规则拒绝的文件）
```

过滤器崩了的时候，这句话是**假的**——用户的权限规则一条都没被求值。
而用户看到这句话会去做的第一件事，就是打开 `permissions.yaml` 找那条不存在的规则。

⚠ **这与已知项 #18「错误的安全承诺比没有承诺更危险」同型**，只是方向相反：
那条是承诺了一个不存在的保护，这条是**归因给了一个不存在的原因**。两者的共同后果
一样——**把排查引向错误的地方**。

### 修法

`_is_allowed` 分两个返回值（或让调用方各记一个计数器）：正常判拒的进「被权限规则拒绝」，
异常判拒的进「过滤时出错」，两句话分开显示。异常那一支**顺手埋一次点**——
它意味着某个过滤器实现坏了，是个应该被人看见的事件。

### 代价与成对维护点

⚠ **`glob_files.py` 与 `grep_content.py` 必须同改**（两处逐字相同、`PathFilter`
是同一个协议）。这一对本身就该进 `paired-maintenance`——它们此前没进去，
是因为过去没人改到这里。

---

## R4-5 🟡 `pre_tool_use` 分发失败 = 静默 fail-open，而 `/hooks` 会告诉用户「从未触发，去查你的字段名」

**位置**：`agent/loop.py:525`、`hooks/manager.py:329`（两层套一起）

```python
# hooks/manager.py:329 —— 内层，有埋点
except Exception as exc:
    self._safe_emit(TraceEventType.HOOK_DISPATCH, ..., error=f"{type(exc).__name__}: {exc}")
    return EMPTY_DISPATCH

# agent/loop.py:525 —— 外层，纵深防御，无埋点
except Exception:
    return NO_VERDICT
```

`NO_VERDICT` / `EMPTY_DISPATCH` 的语义是「不表态」，于是一条本该 deny 的
`pre_tool_use` 规则**变成不拦**。这在方向上与 C12 那条不变量
（「Hook 只能收紧不能放宽」）是相反的——虽然它不是「被放宽」，是「整个没生效」。

**真正糟的是用户会看到什么。** `_dispatch` 里抛异常的位置若早于
`_execute`（`payload_factory()` 或那个求条件值的列表推导），**统计一次都不会写**，
于是 `/hooks` 显示（`hooks/report.py:108`）：

```
触发：0 次（本次运行内从未触发——若与预期不符，先检查条件里的字段名）
```

这句话在正常情况下是极好的排查提示，在这里**却把用户送到了完全错误的方向**——
他会反复检查 `if:` 里的字段名，而根因是分发本身抛了异常。

### 但要如实说：本轮**没有找到可达的抛出点**

逐条查过了：`evaluate` / `_match_one` 是纯函数、缺失字段提前返回；
`stringify` 的输入来自 JSON 解析结果，可序列化；`_tool_fields` 只做字典拼装。
**没能构造出一个现实的触发路径**，所以定级 🟡 而不是 🟠。

它值得记下来的理由是**后果的形态**而不是概率：两层 catch-all 叠在一起，
其中一层连埋点都没有，而唯一会说话的那句话说的是错的方向。
一旦将来 `_tool_fields` 里加进任何会抛的东西（比如给某个工具加一个需要读文件的
派生字段），这条就从 🟡 变成 🟠，而**没有任何测试会在那时候红**。

### 修法

- `agent/loop.py:525` 补一次 `_safe_emit`（它旁边 `:563` 那个 `post_tool` 的同款
  也是纯 `pass`，可以一起）。
- `/hooks` 的「从未触发」那句话，在**本次运行内出现过 dispatch 异常**时换一种说法。
  最小改动是给 manager 加一个「本次运行内分发异常次数」的计数，报告里非零就多印一行。

---

## R4-6 🟡 记忆更新**成功会通知，失败不会**

**位置**：`memory/manager.py:308`

```python
if applied:
    self._last_memory_result = f"已更新 {applied} 条记忆。"
    if self.notify is not None:
        self.notify(f"已更新记忆（{applied} 条）")     # ← 成功：主动推一条系统行
...
except Exception as e:
    self._last_memory_result = f"最近一次更新失败：{e}"   # ← 失败：只留在 /memory 里
```

`F17`「记忆是尽力而为的增强项，任何异常都静默」是**明写的设计**，本条不主张推翻它。
指出的是**不对称**：用户在会话里会习惯性地看到「已更新记忆（N 条）」，
于是「这次没看到」变成唯一的信号——而那与「本轮没什么值得记的」（同样不通知）
完全无法区分。

`/memory` 里能查到，但**得先想到去查**。

### 修法

两种，二选一即可：

- 失败也 `notify` 一次，但用最低的那一档（`[dim]` 系统行，与「预授权被丢弃」同档）。
- 或者反过来——**成功也别 notify**，只留在 `/memory` 里。这一种更贴合 F17 的原意
  （「尽力而为」意味着它不该在正常路径上占用户的注意力），且改动更小。

⚠ **不要两边都保持现状**：现在是「好消息主动说、坏消息等你问」，
这在任何观测设施里都是要避免的形态。

---

## R4-7 六处「看着可疑、实际干净」的（附理由，免得下一轮再查一遍）

| 位置 | 为什么第一眼可疑 | 为什么实际干净 |
| --- | --- | --- |
| `mcp/transport.py:204` | stderr drain 线程 `except Exception: return`——**线程一死，子进程写日志就会被管道写满阻塞**，正是这个线程存在的理由（`CLAUDE.md` 架构表把它列为 ⚠ 不变量） | 最可能的触发（非 UTF-8 日志行）**在上游就被堵掉了**：`Popen(..., encoding="utf-8", errors="replace")`，注释还写明了理由「个别坏字节替换而非抛异常，避免 reader 线程被一行坏数据搞死」。剩下的只有关闭期的 `ValueError: I/O operation on closed file`，那时阻塞已无意义 |
| `web/manager.py:126` | 抽取失败 → `fallback_outcome(page_text, ...)`，**把原始网页正文当成抽取结果返回**，像是拿降级结果冒充正常结果 | `degraded_reason` 一路带到 `web/render.py:119`，模型看到的是「注意：抽取未能完成（…）」。降级**留了痕**，属 D 类 |
| `permission/protected.py:259` | 保护路径层的 `except Exception` —— 安全边界上的 catch-all | **fail-closed 且写明**：解析失败按「命中保护路径」处理（升级为 ASK），且注释指出填进去的相对路径**永远匹配不上豁免集合**（那里存的是绝对路径），所以连豁免都豁免不掉。**本轮最规范的一处** |
| `run_command.py:290` | `except BaseException` —— 连 `KeyboardInterrupt` 都接 | 它**不吞**：`_terminate_process_tree(proc)` 之后 `raise`。这是「清理」不是「吞噬」，方向完全正确 |
| `agent/loop.py:877`、`conversation.py:2474` | `current_scope()` 失败 → `SCOPE_MAIN`，像是会把子 Agent 的 trace 事件**错记成主对话**（`--scope isolated:x` 过滤会静默漏掉） | **不可达**。模块级 `current_scope()`（`trace/recorder.py:48`）读的是 thread-local，未设置时自己就回退 `SCOPE_MAIN`；`NullRecorder.current_scope()` 直接 `return SCOPE_MAIN`。归 F 类（冗余） |
| `commands/dispatcher.py:147` | 吞掉之后调 `_logger.debug(..., exc_info=True)`，看起来堆栈被写进日志了 | 结论仍然干净（`show_message` + `DispatchResult.ERROR` 都到位了），**但那句 `_logger.debug` 落不到任何地方**——全仓无 `basicConfig`、无任何 handler（实测复核过，见 `README.md` 的 C5）。注释写的「不输出堆栈」是靠**日志系统不存在**兑现的，不是靠设计。C5 一旦做了，这里会突然开始输出堆栈 —— ⚠ **这是 C5 的一处成对维护点** |

---

## 全表：67 处逐条

**E 类（8 处，本轮产出）**

| 位置 | 吞掉后做了什么 | 条目 |
| --- | --- | --- |
| `memory/manager.py:135` | 指令置空，`layers` 也清掉 | **R4-1** 🔴 |
| `bootstrap.py:581` | `worktree_cleanup = None` | **R4-3** 🟠 |
| `worktree/cleanup.py:165` | 返回空报告（连埋点都被 `is_empty` 挡掉） | **R4-3** 🟠 |
| `worktree/cleanup.py:118` | `_guess_branch` 返回 `""` | R4-3 附带，低危 |
| `tools/glob_files.py:74` | 判拒绝，计入「被权限规则拒绝」 | **R4-4** 🟠 |
| `tools/grep_content.py:83` | 同上 | **R4-4** 🟠 |
| `agent/loop.py:525` | `NO_VERDICT`（无埋点） | **R4-5** 🟡 |
| `hooks/manager.py:329` | `EMPTY_DISPATCH`（有埋点，无界面出口） | **R4-5** 🟡 |

另有两处**已被别的轮次记过**，不重复计入：
`tui/app.py:1884` 与 `tui/clipboard.py:61`（复制失败静默返回 `False`）——
R5 已记「Linux 剪贴板静默失效且该分支零测试」，见
[`01-release-blockers.md`](01-release-blockers.md)。

**A 类（22 处）**——`ToolResult(ok=False, ...)` 回灌模型：
`agent/loop.py:2123`、`agent/loop.py:2272`、`mcp/auto_config.py:344`、
`mcp/tool_adapter.py:105`、`provider/deepseek.py:264`、`tools/edit_file.py:194`、
`tools/glob_files.py:149`、`tools/grep_content.py:213`、`tools/load_skill.py:225`、
`tools/mcp_config.py:144`、`tools/read_file.py:141`、`tools/run_agent.py:245`、
`tools/run_command.py:465`、`tools/send_message.py:164`、`tools/team_tasks.py:105`、
`tools/todo_write.py:348`、`tools/web_fetch.py:103`、`tools/web_search.py:126`、
`tools/write_file.py:102`、`web/fetcher.py:227`、`web/fetcher.py:322`、
`web/search_manager.py:191`。

其中三处值得点名表扬：`run_command.py:465` 把 `TimeoutExpired` **单独列了一支**
（超时与「命令失败」是两种事，模型的下一步不同）；`web/search_manager.py:191`
在失败分支里 `self._release_quota()`（一次失败的搜索不该扣会话配额，
这是很容易漏的一笔）；`provider/deepseek.py:264` 虽是 C7 记过的「所有错误压成一个
字符串」，但它至少**没有假装成功**。

**B 类（17 处）**——可读原因给用户：
`bootstrap.py:472`、`commands/dispatcher.py:147`、`context/manager.py:312`、
`hooks/actions.py:257`、`hooks/actions.py:380`、`hooks/config.py:195`、
`hooks/manager.py:419`、`mcp/config.py:213`、`mcp/manager.py:162`、
`memory/manager.py:146`、`permission/config.py:257`、`permission/engine.py:600`、
`subagents/runner.py:911`、`subagents/runner.py:959`、`trace/reader.py:559`、
`worktree/cleanup.py:214`、`worktree/provision.py:207`。

`mcp/manager.py:162` 值得单独看：它在失败分支里**逐个 `registry.unregister`
回滚本次注册的工具名**再记 `ServerState(connected=False, error=...)`——
「失败要回滚到干净状态」这一步在 catch-all 里经常被省掉，这里没省。

**C 类（12 处）**——fail-safe 偏严：
`classifier/service.py:136`、`classifier/service.py:158`、`mcp/auto_config.py:445`、
`permission/config.py:357`、`permission/protected.py:259`、`tools/display.py:273`、
`tools/glob_files.py:74`、`tools/grep_content.py:83`、`tools/path_guard.py:331`、
`tools/path_guard.py:357`、`tools/run_command.py:290`、`web/fetcher.py:186`。

⚠ `glob_files.py:74` / `grep_content.py:83` **同时属于 C 与 E**：判定方向属 C（偏严、正确），
呈现方式属 E（归因给了错误的原因）——这正是 R4-4 的内容。

**D 类（5 处）**：`agent/loop.py:636`、`agent/prompt/environment.py:94`、
`memory/manager.py:308`、`web/manager.py:126`、`web/render.py:145`。

**F 类（3 处）**：`agent/loop.py:877`、`conversation.py:2474`、`mcp/transport.py:204`。

---

# 第二部分 · flaky 逐条评估

## 方法

**不靠读代码下结论。** 每条先静态分析给一个预测，再用两种方式验：

1. **裸跑**：连跑 N 轮，确认基线干净。
2. **加载跑**：起 **48 个纯 CPU 忙循环进程**压满 16 核（约 3× 超订），
   把机器人为地拖慢，再连跑 N 轮。

第 2 步是照着 F4 那轮 CI 的经验来的——`NEXT.md` 记着：

> Windows runner 慢到足以把一批**潜伏的时间假设**同时照出来……
> 共同点只有一个：**判据或实现里藏着「这一步会很快」的假设**。

本机跑不出 GitHub runner 的形态，但**「把机器拖慢一个数量级」这件事可以复制**。
实测拖慢倍率：纯逻辑模块 20.8s → 197.9s（**9.5×**），
`test_bootstrap` 16.3s/轮 → 110s/轮（**6.8×**）。

## 结论摘要

| 用例 | 题面的判断 | 实测结论 | 假红 / 假绿 |
| --- | --- | --- | --- |
| `test_memory_manager.py:177` | 「否定式断言配固定睡眠，机器慢时假绿。**这条最隐蔽，优先看**」 | ⚠ **前提不成立**：那 0.05 秒后面没有任何并发，断言是确定性的 | **都不是**。但护栏钉的不是它想钉的性质 |
| `test_subagent_gate.py:142` | 裸 sleep 编排跨线程时序 | ⚠ 部分成立：唯一的假红面是那句 `assertLess(…, 3.0)`，**而它防不住它想防的东西** | 理论假红；20 轮 × 9.5 倍负载未复现 |
| `test_subagent_gate.py:246` | 「无超时忙等」 | ⚠ 忙等属实，但**不会假红**（循环会等）。真问题是**一条恒真断言** | 都不是 |
| `test_subagent_gate.py:278` | 裸 sleep 编排跨线程时序 | ✅ **确定性**（循环阻塞等，睡多久都不影响结论） | 都不是 |
| `test_team_mailbox.py:84` | 「依赖 `time.time()` 单调（项目别处用 `monotonic`）」 | ⚠ **前提不成立**：`team/` 全模块用 `time.time()`，且 `sent_at` **只用于显示格式化**，`monotonic` 在这里连语义都不对 | 仅时钟回拨时假红 |
| `test_bootstrap.py:319-364` | 「起真子进程 + 60 秒轮询」 | 🔴 **实测复现**：6 轮里 1 轮红，**3 个调用点全挂**。且**诊断路径自己会二次超时** | **假红，且丢失证据** |
| `tests/e2e/c14_scenarios.py:612` | 「依赖系统时钟」 | ⚠ **它不是测试**：不被任何 `test*.py` import，不进 `discover`、不进 CI。30 天余量也远大于任何现实漂移 | 都不是。真脆弱点在别处 |

⚠ **一条应该先说的背景**：这里的前六条**已经在 F4 的 CI 上跑过 6 个格子**
（`{windows, ubuntu} × {3.11, 3.12, 3.13}`），而那几轮 CI 的 runner 慢到把
**五处**互不相干的潜伏时间假设同时照了出来——**这六条一处都不在其中**。
本轮的负载实测与那个结果一致。（第七条 `c14_scenarios.py` 从来没被 CI 跑过，
因为它压根不在 `discover` 的收集范围里。）

---

## F-1 `tests/test_memory_manager.py:177` —— 题面的前提不成立，但它确实钉错了东西

```python
def test_notes_disabled_noop(self) -> None:
    provider = FakeProvider([_action_json()])
    mgr = self._manager(provider, memories_enabled=False)
    mgr.on_natural_stop([Message(role="user", content="x")])
    time.sleep(0.05)
    self.assertEqual(provider.calls, [])  # 完全不调 LLM（F21）
```

### 为什么它不会假绿

`on_natural_stop`（`memory/manager.py:262`）的**第一行**就是：

```python
if not self.memories_enabled:
    return
```

**同步返回，线程根本没被创建。** 没有并发就没有竞态：`provider.calls` 在这条用例里
不可能在任何时刻变成非空，睡 0.05 秒和睡 0 秒的结果完全一样。

实测佐证：`test_memory_manager` 在 9.5 倍负载下连跑 20 轮，全绿。

### 但它确实有问题——**钉的不是它想钉的性质**

这条用例的意图（F21：记忆未启用时完全不调 LLM）是靠「早返回发生在
`on_natural_stop` 里」这个事实成立的，而**这个事实它一个字都没断言**。
如果哪天有人把 `memories_enabled` 的判断挪进 `_update_memories`
（线程照起、进去再 bail——这是个很自然的重构），那么：

- 用例**在快机器上仍然全绿**；
- 而它此刻才**真的**变成题面描述的那种「0.05 秒赌一把」的用例；
- 意图（不调 LLM）可能仍然满足，但**这条用例已经不能证明它了**。

即：题面担心的那个形态不是**现状**，是**一次无害重构之后的必然结果**，
而没有任何东西会在那一刻报警。

### 改法

```python
mgr.on_natural_stop([Message(role="user", content="x")])
# 早返回是同步的：线程压根不该被创建 —— 这才是 F21 真正的判据
self.assertFalse(mgr._memory_inflight.is_set())
self.assertEqual(provider.calls, [])
```

`_memory_inflight` 在 `on_natural_stop` 里是**先于**起线程被 `set()` 的
（`manager.py:270-274`），线程结束才 `clear()`。因此「从未 set」等价于
「线程从未被创建」，是一个**确定性**判据。`time.sleep(0.05)` 一并删掉。

⚠ **同文件的 `_wait_notes_done`（`:64`）是本项目的正面样板**——
`while ... is_set(): sleep(0.01)` 配一个 5 秒 deadline 与 `self.fail`。
两种写法在同一个文件里并存，`:177` 那条只是没用上它（因为它这条本来就不需要等）。

---

## F-2 `tests/test_subagent_gate.py:142` —— 唯一的假红面是一句**防不住任何东西**的断言

```python
def finish_later():
    time.sleep(0.15)
    self.tasks.finish(record.task_id, TaskStatus.COMPLETED, "好了")

threading.Thread(target=finish_later, daemon=True).start()
started = time.monotonic()
got = self.gate.wait_any(self.cancel)

self.assertTrue(got)
self.assertLess(time.monotonic() - started, 3.0)
```

**两种线程交错都是安全的**：

- 辅助线程后完成 → `wait_any` 阻塞在 `Event` 上被唤醒；
- 辅助线程先完成 → `wait_any` 走「已有可交付」的快路径立刻返回
  （同类中的 `test_returns_immediately_when_something_deliverable` 正是钉这条的）。

所以 `assertTrue(got)` 是确定性的。

### `assertLess(…, 3.0)` 防不住它想防的东西

这句话看起来是「防止无限等待」的护栏，**但它写在 `wait_any` 返回之后**。
如果 `wait_any` 真的不醒，代码根本走不到这一行——用例会**挂住**，
由 unittest 之外的东西（CI 的 `timeout-minutes: 30`，或人按 Ctrl+C）来终结。
它唯一能做的事，就是在一台足够慢的机器上**把一次成功变成一次失败**。

即：**它承担 0 的护栏价值，100% 的 flaky 面。**

### 实测

20 轮 × 9.5 倍负载，未复现。`0.15s` 与 `3.0s` 之间有 20 倍余量，
要撞上需要线程调度被拖到 20 倍以上——本机的 9.5 倍还不够。
但 F4 那几个 CI 格子的分片跑到了平时的 3 倍，叠加 runner 本身更慢，
**这个余量不像看上去那么厚**。

### 改法

删掉 `assertLess` 那一行。若确实想防「永远醒不过来」，正确的做法是给
`wait_any` 传一个测试用的超时上限、或把整条用例包进
`concurrent.futures` 的 `result(timeout=...)`——那样超时才会变成
一条**说得清原因**的失败，而不是一次莫名其妙的红。

---

## F-3 `tests/test_subagent_gate.py:246` —— 忙等属实，但真问题是一条**恒真断言**

```python
def finish_soon():
    while provider.calls < 2:
        time.sleep(0.01)
    tasks.finish(record.task_id, TaskStatus.COMPLETED, "子结论：42 处。")
...
hits = [i for i, b in enumerate(provider.bodies, 1) if "42 处" in b]
self.assertTrue(hits, "结论必须在同一次运行内进入某一轮的请求体")
self.assertLessEqual(hits[0], len(provider.bodies))
```

### 它不会假红

看起来的风险是「`finish_soon` 醒得太晚，循环已经结束，结论没赶上」。
**实际赶得上**：这个任务不是 `background`，因此模型准备收工时循环会
**停下来等它**（C13 第 ⑧ 条；同文件下一条 `test_loop_waits_before_finishing`
钉的就是这个）。无论 `finish_soon` 多晚醒，循环都在那儿等着。

实测 20 轮 × 9.5 倍负载，全绿。

### 但有两个真问题

**① `assertLessEqual(hits[0], len(provider.bodies))` 恒真。**
`hits` 的元素来自 `enumerate(provider.bodies, 1)`，取值范围就是 `1..len(bodies)`。
`hits[0] <= len(bodies)` **在任何输入下都成立**，它一次都不可能红。
上一行的注释解释了为什么不断言「第几轮」（那个数字是竞态产物），
这一行大概是想补一句更弱的判据——但补出来的是个恒等式。

真正想说的应该是「结论必须**在模型给出最终答复的那一轮之前或当轮**到手」，
而在当前的数据结构下这句话**无法表达**：要表达它得知道「哪一轮是模型给出最终文本
的那一轮」，而 `provider.bodies` 里没有这个信息（最终答复那一轮恰好就是最后一轮，
于是判据退化成恒等式）。**诚实的做法是删掉这一行**，把上一行注释里
「刻意不断言第几轮」的理由留下。

**② 忙等没有 deadline，而它是 daemon 线程。**
若 `provider.calls` 永远到不了 2（比如将来 gate 的契约变了、循环不再等待），
这个线程会**空转到进程结束**。在一个分片里跑几百条用例的场景下，
那是一整个核被白烧掉，而**表现出来只是「这个分片今天特别慢」**。

改法照抄同仓库现成的样板：`_wait_notes_done`（`test_memory_manager.py:64`）
那种「deadline + `self.fail`」的形态；或者更简单——给循环加一个
`for _ in range(500)` 的上限，超了就直接 `return`（让主断言去报错，
那个错误信息比线程里的 fail 有用）。

---

## F-4 `tests/test_subagent_gate.py:278` —— 确定性，`sleep` 只是成本

```python
def finish_soon():
    time.sleep(0.2)
    tasks.finish(record.task_id, TaskStatus.COMPLETED, "子结论：42 处。")
```

与 F-3 同理：循环会等。睡 0.2 秒还是 2 秒，结论都一样——
`provider.calls >= 2`、最后一轮请求体里有「42 处」、有一条「等待子 Agent」的提示。

**没有假红也没有假绿，只有 0.2 秒的净成本。** 三条用例合计约 0.35 秒，
在 `test_subagent_gate` 里不算什么，列在这里只是为了给出完整判断。

若要削：把 `sleep(0.2)` 换成 0 也不会改变任何断言的结论——因为「先完成」
和「后完成」两条路径都被覆盖（前者走快路径、后者走 Event）。
但**保留一点延迟是有意义的**：它让这条用例实际走的是「后完成」那条路径，
删掉之后它多半会退化成只覆盖快路径，与 `test_returns_immediately_...` 重复。
**建议不动。**

---

## F-5 `tests/test_team_mailbox.py:84` —— 题面的前提不成立

```python
before = time.time()
envelope = self.mailbox.send("worker-a", "worker-b", "x").envelope
self.assertGreaterEqual(envelope.sent_at, before)
```

题面说「项目别处用的是 `time.monotonic()`」，所以这里用 `time.time()` 是个隐患。
**实际查下来是反过来的**：

- `team/` 模块**全部**用 `time.time()`：`models.py:229,230,277,328`（`created_at` /
  `updated_at` / `sent_at` / `last_active`）、`board.py:263,320,382,408`、
  `roster.py:253,297`。测试与产品**同源**，这正是它该做的。
- `sent_at` 的**唯一**消费点是 `team/render.py:213`：
  `time.strftime("%H:%M:%S", time.localtime(env.sent_at))`。
  它是**给人看的时钟时间**，`monotonic`（一个无意义的开机以来秒数）
  在这里连语义都不对。
- 信箱的**顺序**不由 `sent_at` 决定——全仓搜 `sent_at` 只有三处，没有任何排序用到它。
  （用例的 docstring 写「信箱是按到达顺序排的」，这句话是对的，
  但那个顺序来自列表的追加次序，与 `sent_at` 无关。）

### 剩下的那点风险

只有一种：`before` 与 `send()` 之间**系统时钟被回拨**（NTP 校正、虚拟机快照恢复、
用户手动改表）。窗口是微秒级，且真发生时**产品也一起错了**（消息上会显示一个
比前一条更早的时间）。属**假红**，概率可忽略。

### 改法

不必改。若要更严，可以把断言换成「`sent_at` 不是模型给的」这个真正的意图——
比如让 `send()` 收一个带 `sent_at` 的伪造载荷，断言它被忽略。
docstring 说的正是这件事（「让模型给时间戳等于让它有机会给出一个假的顺序」），
而现在的断言只验到了「系统填了个不早于调用前的值」。

---

## F-6 🔴 `tests/test_bootstrap.py:319-364` —— **实测复现**，而且诊断路径自己会二次超时

**这是本轮唯一真复现出来的 flaky，也是唯一建议尽快改的一条。**

### 实测

| 条件 | 轮数 | 结果 |
| --- | --- | --- |
| 裸跑 | 1 | 18 条全绿，16.3 秒 |
| 48 进程压满 16 核（6.8×） | **6** | **1 轮红，`FAILED (errors=3)`** |

红的那一轮里，`_launch_until_trace` 的**三个调用点全部失败**
（`:435`、`:454`、`:474`——文件里正好只有这三处）。

### 失败形态比预期糟

预期的失败是「60 秒轮询超时 → `self.fail` 给出一条带子进程 stderr 的信息」。
**实际拿到的是这个**：

```
ERROR: test_unknown_granted_tool_starts_normally_in_subprocess
Traceback (most recent call last):
  File "tests\test_bootstrap.py", line 435, in test_unknown_granted_tool_starts_normally_in_subprocess
    path = self._launch_until_trace(
  File "tests\test_bootstrap.py", line 354, in _launch_until_trace
    out, err = proc.communicate(timeout=10)
subprocess.TimeoutExpired: Command '[... '-m', 'rhinecode', '--config', ..., '--trace']'
                           timed out after 10 seconds
```

即：**为排查而写的那一行自己也超时了**，`self.fail(...)` 那句带 stderr 的信息
**一个字都没出现**。

### 根因：那句 `self.fail` 在主要失败路径上不可达

```python
deadline = time.time() + 60
while time.time() < deadline:
    ...
    if proc.poll() is not None:
        break
    time.sleep(0.2)
# 进程提前退出或超时：把 stderr 带进失败信息，便于定位
out, err = proc.communicate(timeout=10)
self.fail("未在超时内看到有内容的记录文件；" f"returncode={...} stderr={...}")
```

这个 `while` 有**两个**出口，注释也写明了是两种情况，但后面只有一条处理：

| 出口 | 子进程状态 | `communicate(timeout=10)` |
| --- | --- | --- |
| `break`（进程提前退出） | 已退出，管道已关 | ✅ 立刻返回，`self.fail` 正常给出 stderr |
| 循环走完（超时） | **还活着**（那是个 TUI，它就是不会自己退） | ❌ 管道永不关闭，10 秒后抛 `TimeoutExpired` |

而**超时才是需要诊断信息的那一种**。于是这段代码的效果是：
**最需要证据的时候，恰恰是它唯一拿不到证据的时候。**

⚠ 这与 R4-4 / R4-5 是同一个形态，只是发生在测试里：
**排查信息把人引向错误的方向**——`TimeoutExpired` 的消息里只有那条命令行，
看到它的人会先去怀疑「是不是 rhinecode 启动挂了」，
而真相是「机器慢，60 秒没跑完启动」。

### 顺带：`deadline` 用的是 `time.time()`

`deadline = time.time() + 60` 与 `while time.time() < deadline`——**这里才是**
题面说的那个「该用 `monotonic` 却用了 `time`」的地方（不是 `test_team_mailbox`）。
后果很轻（时钟回拨只是让它多等或少等一会儿），但**改起来是一个词**，
而且它与上面那条 bug 在同一个函数里，顺手改掉即可。

### 改法

```python
        try:
            deadline = time.monotonic() + 60          # ① 换单调钟
            while time.monotonic() < deadline:
                found = probe()
                if found is not None and found.exists() and found.stat().st_size > 0:
                    return found
                if proc.poll() is not None:
                    break
                time.sleep(0.2)
            # ② 先把它停掉，管道才会关，communicate 才回得来
            if proc.poll() is None:
                proc.terminate()
            try:
                _out, err = proc.communicate(timeout=10)
                detail = err.decode("utf-8", "replace")[:500]
            except subprocess.TimeoutExpired:
                proc.kill()
                _out, err = proc.communicate()
                detail = "（子进程未在终止后 10 秒内退出）" + err.decode("utf-8", "replace")[:500]
            self.fail(f"未在超时内看到有内容的记录文件；returncode={proc.returncode} stderr={detail}")
        finally:
            ...
```

要点是 **②：`terminate()` 必须在 `communicate()` 之前**。
现在的顺序（`communicate` 在 `try` 里、`terminate` 在 `finally` 里）
恰好是反的，而这正是它拿不到证据的原因。

### 顺带一提：另外两个调用点缺一个已经踩过的教训

`:435` 那条的 `probe` 有一段很详细的注释，记录着一个真实踩过的竞态：

> ⚠ 不能只等「文件非空」——`session_start` **不是文件里的第一条事件**……
> 这个竞态一直都在，但窗口很窄，只在**全量测试的并发负载**下才偶尔命中（单跑必绿）

它的修法是：读出来、`json` 解析、确认目标事件真的在里面，解析失败就继续等
（`except Exception: return None`）。

**而 `:454` 与 `:474` 两条仍是老写法**——`probe` 只看文件存不存在，
拿到之后直接 `json.loads(found.read_text().splitlines()[0])`。
`_launch_until_trace` 的 `st_size > 0` 挡住了空文件，但挡不住**写了一半的行**
（`trace/recorder.py:241` 是 `write(line + "\n")` 后 `flush()`，
大负载下的一次大 flush 可能被拆成多次 write）。窗口比 `:435` 那条**窄得多**
（首条事件是很小的 `skill_state`），所以定性为「同类隐患，未复现」而不是缺陷。

**修法就是把 `:435` 那条的 `try/except → return None` 模式抽成一个共用的 probe 辅助函数**，
三处都用它。⚠ 这三处是一组成对维护点：**一处踩过坑改了，另外两处没跟上。**

### 代价

小（一个函数内的十来行），但**它必须真跑过才算改完**——
建议照本轮的方式验：起 48 个忙循环压住机器，连跑 6 轮，看那三条是否还红，
以及红的时候错误信息里**有没有子进程的 stderr**。

---

## F-7 `tests/e2e/c14_scenarios.py:612` —— 它不是测试，真脆弱点在别处

```python
old = time.time() - 30 * 86400
for name in ("clean", "committed", "wip"):
    for root, _dirs, files in os.walk(base / name):
        for fname in files:
            try:
                os.utime(Path(root) / fname, (old, old))
            except OSError:
                pass
    os.utime(base / name, (old, old))
```

### 先说清楚它是什么

`tests/e2e/c14_scenarios.py` **不被任何 `test*.py` import**（全仓核过：
只有三处文档与两处兄弟模块 docstring 提到它）。`unittest discover` 的默认
收集模式是 `test*.py`，所以它**不在那 3000 多条里，也从来没被 CI 跑过**。

它是 P1a 驱动设施的**手工验收预置**——由宿主用
`--seed tests.e2e.c14_scenarios:seed_xxx` 显式调起，服务于
`docs/c14/acceptance/live-model.md` 那八个真实模型会话。

因此「假红 / 假绿」这个问法在这里要换成：**它会不会让一次人工验收得出错误的结论？**

### 时钟依赖不是问题

`old` 比现在早 **30 天**，而产品的 `cutoff = time.time() - max_age_days * 86400`
默认是 7 天。**余量 23 天。** 任何现实中的时钟漂移、NTP 校正、夏令时都不在这个量级。
（真要 23 天量级的跳变，那台机器上坏掉的东西远不止这个场景。）

### 真正的脆弱点是那个 `except OSError: pass`

——**又是一处异常吞噬，只是这次在测试设施里。**

`os.utime` 在 Windows 上会因为文件被占用（杀软扫描、索引服务、
`git worktree add` 刚写完还没放手）而抛 `PermissionError`（`OSError` 的子类）。
被吞掉之后：

1. 那个文件保持**新鲜**的 mtime；
2. 产品的 `_latest_mtime`（`cleanup.py:43`）取的是「目录内文件的**最近**修改时间」，
   一个新鲜文件就够了；
3. 该工作区被判**未过期** → 连报告都不进（`cleanup.py:174`：「未过期：连报告都不进」）；
4. 验收的人看到的是「**清理没生效**」，于是去查 c14 的清理逻辑；
5. **而真正的原因是预置代码里一次被吞掉的 `utime`，它在任何地方都没留下痕迹。**

这与 R4-3 / R4-4 是同一个形态：吞掉之后做的那件事，**把排查引向了错误的地方**。
放在人工验收里尤其贵——那是要花真实模型额度的场景。

### 改法

```python
failed: list[str] = []
for ...:
    try:
        os.utime(Path(root) / fname, (old, old))
    except OSError as exc:
        failed.append(f"{fname}: {exc}")
if failed:
    raise RuntimeError("预置失败：以下文件的 mtime 没能拨回去，"
                       "本场景的过期判定不成立：\n  " + "\n  ".join(failed))
```

**预置失败必须硬失败。** 这与 `tests/e2e/seeding.py` 已有的口径一致——
`architecture.md` 记着它「git 缺失明确抛错不静默跳过——静默跳过会让依赖提交历史的
场景**假绿**」。同一个模块家族里，一处做对了，这处没有。

---

## F-8 `tests/test_subprocess_timeout.py` 的 `CHILD_SLEEP=6` —— 只评估，不建议改

按题面要求只做评估。**结论：`CHILD_SLEEP=6` 应当保留，理由与文档一致且仍然成立。**

- 它给孙子进程一个「本该还活着」的窗口，标记文件的判定完全依赖它。
  缩短它会让「进程树有没有被真的杀干净」这个判据**失去分辨力**——
  而那对应一个真实产品缺陷。
- 它是**正向余量**（等得久 = 更保守），不是负向余量。慢机器让它更容易通过，
  不是更容易失败。**这与本轮其它几条的风险方向相反**，因此它不属于
  「潜伏的时间假设」那一类。

⚠ **但 `THRESHOLD=4.0` 已经不在那份「不要动」清单里了**（2026-08-30 摘掉）。
`CLAUDE.md` 的「测试」一节记了原委：它当时同时充当「立刻返回」与
「反证朴素写法必须慢」两半的判据，而**后一半只在 Windows 上成立**——
装 CI 之后三个 Linux 格子全红在它上面，而产品行为在两个平台上都是对的。
现在反证改看「朴素写法有没有把命令留在后台继续跑」（标记文件），
`THRESHOLD` 只留下「立刻返回」那一半。

**这正好印证了本轮方法的价值**：一个「不要动、有理由」的常数，它的理由可以是
**平台局部的**，而在开发机上无论跑多少遍都发现不了。

---

## 附一 · 本轮对已有材料的更正（共五处）

| # | 材料 | 原说法 | 更正 |
| --- | --- | --- | --- |
| 1 | `NEXT.md` R4 的 Prompt | 「121 处 `except Exception`，40 处 `pass`，剩 81 处」 | **115 处（另 2 处 `BaseException`），50 处 `pass`，剩 67 处**。121 含 6 处 docstring 里的提及 |
| 2 | `NEXT.md` R4 的 Prompt | 「4 处可疑已记在 `docs/review/README.md`」 | **`README.md` 里没有这四处**，无法核对 |
| 3 | `NEXT.md` R4 的 Prompt | `test_memory_manager.py:177`「机器慢时假绿，**这条最隐蔽，优先看**」 | **前提不成立**：早返回是同步的，没有并发。它的问题是「钉错了性质」，不是假绿 |
| 4 | `NEXT.md` R4 的 Prompt | `test_team_mailbox.py:84`「依赖 `time.time()` 单调（项目别处用 `monotonic`）」 | **`team/` 全模块都用 `time.time()`，且 `sent_at` 只用于显示格式化**。该用 `monotonic` 而没用的地方是 `test_bootstrap.py:345` |
| 5 | `NEXT.md` R4 的 Prompt | 把 `tests/e2e/c14_scenarios.py:612` 与其余四条并列为「用例」 | **它不是用例**：不被任何 `test*.py` import，不进 `discover`、从未被 CI 跑过 |

## 附二 · 建议的处理顺序

| 优先 | 条目 | 代价 | 理由 |
| --- | --- | --- | --- |
| 1 | **R4-1**（RHINE.md 编码） | 小 | 唯一的 🔴。触发不需要用户做错任何事，后果是「用户写的项目指令整个不生效且查不出来」 |
| 2 | **F-6**（`_launch_until_trace`） | 小 | 唯一实测复现的 flaky，且**修的是排查能力本身**——不修的话，下次 CI 红在这里会浪费一轮 |
| 3 | **R4-2**（写入失败的文案） | 小 | 模型会据此做错事，而正确文案只要 6 行 |
| 4 | R4-3 / R4-4 | 小 | 两条都是「归因给了错误的原因」，改的是一句话 + 一次埋点 |
| 5 | F-3 的恒真断言、F-2 的 3 秒断言、F-7 的预置硬失败 | 极小 | 三条都是「删掉/改掉一行」，且都能消掉 flaky 面或假判据 |
| 6 | R4-5 / R4-6 | 小 | 🟡，不阻塞任何事 |

**如果只做一件事**：**R4-1**。它是本轮唯一一处「用户完全没做错、系统完全没提示、
而他明确写下的指令没有生效」。

**如果只做一天**：1 + 2 + 3。

## 附三 · 本轮没做的

- **67 处里的 A 类没有逐条跑复现**。它们的判据是静态可判的（返回值类型 +
  文案里有没有 `{exc}`），逐条跑 22 次没有额外信息量。
- **R4-5 没能构造出可达的抛出点**，因此它是按「后果形态」定级的，不是按概率。
- **flaky 只在本机拖慢，没有在 GitHub runner 上复跑**。F-6 的修法建议里写了
  该怎么验；本轮不改代码，所以没验。
- **`tests/` 目录下的 33 处 `except Exception` 未纳入**（题面限定产品代码）。
  F-7 那处是个例外——它是顺着 flaky 那条线撞见的。

## 附四 · 复现脚本

| 脚本 | 用途 | 要凭据？ |
| --- | --- | --- |
| `scan_except.py` | AST 全量枚举 `except Exception/BaseException`，按处理体分 PASS / RETURN / RAISE / LOG / OTHER | 否 |
| `scan2.py` | 对每处非-`pass` 的打印 `try` 体 + handler 体全文（67 处逐条判定的输入） | 否 |
| `repro_r4_1.py` | R4-1 三组对照（三层全缺 / 坏编码 / include 越界）的 `/memory` 输出 | 否 |
| `stress.py` | 指定模块连跑 N 轮，记录失败轮次与最后 1500 字符 stderr | 否 |
| `load.py` | 起 N 个纯 CPU 忙循环进程压满机器（本轮用 48 个 / 16 核） | 否 |

⚠ **全部不需要凭据，可以直接重跑核对。**
⚠ `load.py` 会**真的**把机器压满，跑之前先存盘。它按秒数自己退出，不需要手动清理。

**核对 R4-1 最快的一条**（不需要脚本，在仓库根跑）：

```bash
python -c "import pathlib,tempfile,sys; sys.path.insert(0,'.'); \
from rhinecode.memory.instructions import load_instructions; \
d=pathlib.Path(tempfile.mkdtemp()); u=d/'u'; p=d/'p'; u.mkdir(); p.mkdir(); \
(p/'RHINE.md').write_bytes('# 项目指令'.encode('gbk')); load_instructions(u,p)"
```

它会抛 `UnicodeDecodeError`——而**产品里接住它的那个 `except Exception`
不会留下任何痕迹**，这就是 R4-1 的全部。
