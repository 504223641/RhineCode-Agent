# 审查剩余工作清单

> 配套 [`README.md`](README.md)（已发现的问题）与 [`00-baseline.md`](00-baseline.md)（机器实测数据）。
> **照着这份清单从上往下做，做完审查就完整了。**

## 怎么用这份清单

- **每一项都是独立的**，可以单开一个 session，把它的「一键 Prompt」整段复制粘贴进去即可开工。
- Prompt 里已经带好了背景与已有材料的位置，**不需要你额外交代任何上下文**。
- 每项做完就把本文对应小节标上 ✅ 并写一句结论，**不要删**——审查记录本身有追溯价值
  （这一点与 `docs/todo/` 的「做完即删」相反，因为那是待选方向，这是审查台账）。
- **R 系列是「查」**（只读调研、产出文档、纯文档改动直接提交 main，不开分支）。
- **F 系列是「修」**（改代码，**必须先 `git checkout -b`**，且动那些目录前要先加载
  `paired-maintenance` Skill）。

## 当前进度

| 阶段 | 状态 | 对应项 |
| --- | --- | --- |
| 0 建基线 | ✅ 完成 | — |
| 1 发布阻塞项 | ⚠️ R5 ✅ 完成（查清了），**修还没做** | ~~R5~~ → F1 / F4 |
| 2 正确性健壮性 | ⚠️ R3 ✅ 完成，剩 R4 | ~~R3~~、R4 |
| 3 架构冲突 | ⚠️ 依赖方向 ✅ + 交互矩阵 ✅（R1 已完成），剩 R7 | ~~R1~~、R7 |
| 4 安全复审 | ✅ 完成 | ~~R2~~ |
| 5 可维护性交付 | ❌ 未开始 | R6（⚠ CI 那一项已随 F4 落地，剩 ruff / mypy / CONTRIBUTING / 架构图） |

## 建议顺序

```
R1 → R2 → R3 → R5 → F1 → F2 → F3 → F4 → R4 → R6 → F5 → R7 → F6
 ✅    ✅    ✅    ✅
 └──── 纯查，回答你最初的问题 ────┘   └─ 消掉发布阻塞 ─┘   └─ 收尾 ─┘
```

**R1 与 R2 排最前**，因为它们直接回答你最初问的「有什么潜在问题、是否存在冲突」，
而且是纯只读、不动代码。**R5 排在 F1 之前**，因为 F4（改 textual 下界）需要 R5 验出的正确值。

**能并行的**：R1 / R2 / R4 互不重叠（分别读集成测试、读权限与安全文档、读异常处理），
可以开三个 session 同时做。**R3 与 F2 不要并行**——R3 要坐实的正是 F2 要修的那两条。

**总耗时估计**：R 系列约 3–4 天，F 系列约 2–3 天。

---

# R 系列 · 审查未完成部分（只查不改）

## R1 · 能力交互矩阵 ✅

> **已完成（2026-08-22）**，产出在 [`03-architecture.md`](03-architecture.md)。
>
> **结论**：11 个维度 55 格 —— **有护栏 34 · 只有验收记录 1 · 空 11 · 结构性不可能 9**。
> 空格子里真正要紧的是前 5 个，且其中**第 1 个不是缺测试而是真的漏了**：
> `context: fork` 的 Skill 子对话构造 Agent 时没传 `hooks`
> （`rhinecode/conversation.py:1595` vs 主对话的 `:326`），
> 于是**工具级三个 Hook 事件在 Skill 子对话里全是哑的**——
> 一条 `pre_tool_use` 拦截规则可以被「把命令包进一个 Skill」整层绕过，
> 而 `docs/c12/task.md:512` 当初就写了要传。这条建议升级进 `README.md` 的 B 组。
>
> 另两处顺带查到的：C16 分类器在**子 Agent** 与 **fork 子对话**两条路径上的接线
> 各自零测试（`principal_history` 全仓测试里 0 次出现），
> 以及 `ConversationManager.clear()` 里四条「漏掉不报错」的接线没有一条经过它本身。

**这是你最初问的「是否存在冲突」目前唯一没回答的那一半。**

已回答的是**包依赖层面**：10 个包强连通，但被 `tools/__init__.py` 的 docstring
妥善管理（见 `00-baseline.md` 第 5 节）。没回答的是**能力交互层面**——C12 Hook ×
C13 子 Agent × C14 隔离 × C15 协作 × C16 分类器 × 权限五层 × Plan Mode，
两两组合 20+ 种，哪些有测试、哪些是空格子，从来没人数过。

**为什么值得做**：项目自己的历史反复指向同一件事——**接缝处才出问题**。
`/clear` × 子 Agent 交付、严格档 × 协作工具、`system_serial` × ③规则层，
三次都是各段都验过、接缝没验，单测全绿、真机才现形。

**完成判据**：一张矩阵，每个格子标注「有护栏（测试文件:行号）/ 有验收记录 / 空」，
空格子按「出事后果」排序。

**预计**：半天到一天。

<details>
<summary><b>一键 Prompt（点开复制）</b></summary>

```
我在对 RhineCode 做一轮面向开源发布的代码审查。已有材料请先读：
- docs/review/README.md（已发现的问题清单）
- docs/review/00-baseline.md（机器实测数据）

这次做「R1 · 能力交互矩阵」，目标是回答一个还没回答的问题：
各章能力两两交互的接缝处，哪些有护栏、哪些是空的。

背景：本项目的历史反复证明「接缝处才出问题」——/clear × 子 Agent 交付、
严格档 × 协作工具、system_serial × ③规则层，三次都是各段都验过、接缝没验，
单元测试全绿、只有真实模型跑才现形（记录在 docs/c15/acceptance/live-model.md
与 docs/todo/README.md 的「顺带修掉的」一节）。

请做三件事：

1. 列出参与交互的能力维度：C12 Hook / C13 子 Agent / C14 工作区隔离 /
   C15 协作 / C16 分类器 / 权限五层（含②″保护路径）/ Plan Mode 两阶段 /
   Skill（含 context: fork）/ 上下文压缩 / todo 清单 / ask_user 面板。

2. 读现有的集成测试，搞清楚每个格子的覆盖情况：
   - tests/test_subagent_integration.py（504 行）
   - tests/test_team_integration.py（368 行）
   - tests/test_auto_plan_integration.py（393 行）
   - tests/test_todo_integration.py（430 行）
   - tests/test_hook_zero_regression.py
   - tests/test_protected_wiring.py
   以及 docs/c13/acceptance/、docs/c14/acceptance/、docs/c15/acceptance/、
   docs/c16/acceptance/ 下的真实模型验收记录。

3. 产出一张矩阵，每格标注：有护栏（给出 测试文件:行号）/ 只有验收记录（给出文档路径）/
   空。然后把空格子按「万一出事的后果」排序，每条说明具体的失效形态
   （要具体到「用户会看到什么」，不要写「可能有问题」）。

约束：
- 只读调研，不改任何产品代码，不写测试。
- 产出写到 docs/review/03-architecture.md（新建），追加到已有的依赖方向结论之后。
- 每条结论必须带 文件路径:行号。
- 纯文档改动，按项目约定直接提交 main、不开分支、不开 PR。
- 如果某个格子看起来空但其实被别的机制结构性排除了（例如「隔离委派与待命互斥」
  是 C15 明确登记的边界），要写明是「结构性不可能」而不是「没测」。
```

</details>

---

## R2 · 安全复审 + SECURITY.md ✅

> **已完成（2026-08-22）**，产出在 [`04-security.md`](04-security.md)，
> 外加仓库根的 [`SECURITY.md`](../../SECURITY.md)。
>
> **结论**：八条顺序 / 匹配不变量的护栏做了变异实测（真把代码改坏跑一遍），
> **七条会红、一条全绿**；四条重点已知边界**全部与代码一致**。
> 但查出**两条安全承诺与代码不符**，成因相同——`auto` 成为缺省预设时，
> 那些「靠第④层判 ASK 弹面板」的承诺集体失效了，而文字没跟着改：
>
> - **S1 🔴 `mcp_add_server` 六层防御一层都不生效**：它写 `.rhinecode/mcp.yaml`
>   （或 `~/.rhinecode/mcp.yaml`，**工作区之外**）并启动任意第三方程序，
>   而 `kind == "other"` 让①②②′②″全部原样穿过、缺省档下④判 ALLOW、
>   也没有 `classifier_scope`。那个子进程还继承**完整的 `os.environ`**
>   （`mcp/transport.py:165`），含 API Key——而 `run_command` 的子进程是过滤过的。
>   `CLAUDE.md:455` 与两处 docstring 都写着「必须先让用户确认」。
> - **S2 🔴** 「MCP 工具默认每次经人在回路确认」在缺省预设下已失效，
>   三处文档仍这么写，且与 `CLAUDE.md:426` 自相矛盾。
>
> 另有两条无护栏 / 跨平台缺口：**S3** ①黑名单的拆分口径改坏后 3,323 条测试全绿；
> **S4** `permission/rules.py:121` 用 `fnmatch` 而非 `fnmatchcase`，
> 同一份规则在 Windows 与 Linux 上语义不同（Hook 那侧已经修过这个坑，权限侧没修）。
>
> **S1 与 S2 应当一起做**，并顺手扫一遍还有没有第三处同成因的承诺。

**这个项目最该被审的部分（原文保留）。**

它会执行任意命令、访问网络、读写用户文件，开源后别人 clone 下来就跑。
要验的**不是**「设计对不对」——五层管线的论证很扎实——而是**论证与代码是否还对得上**。

**为什么值得做**：项目自己记录过一次「变异实测：把收紧器改成短路站时顺序护栏照样通过」
——说明**护栏构造错过一次**，而那种错误只有专门去验才发现得了。另外已知项 #18 的教训是
「错误的安全承诺比没有承诺更危险」，而 B2（`classifier/render.py:244` 的安全提醒从未接线）
证明这类问题现在还在发生。

**完成判据**：① 每条安全论证标注「代码仍支持 / 已漂移 / 无护栏」；
② 一份 SECURITY.md；③ README 安全须知对陌生用户够不够用的评估。

**预计**：一天。

<details>
<summary><b>一键 Prompt（点开复制）</b></summary>

```
我在对 RhineCode 做一轮面向开源发布的代码审查。已有材料请先读：
- docs/review/README.md（已发现的问题清单）
- docs/review/00-baseline.md（机器实测数据）

这次做「R2 · 安全复审」。注意目标不是找设计漏洞——五层权限管线的设计已经过
反复论证且有反证测试。目标是验证**安全论证与代码是否还对得上**。

背景：CLAUDE.md 的「安全边界」一节记录了几十条安全论证，每条都有理由。
但项目自己记录过一次「变异实测：把②″保护路径的收紧器改成短路站时，顺序护栏
照样通过」——说明护栏曾经构造错过。而本次审查已经发现 classifier/render.py:244
的安全提醒实现完整却从未接线（用户永远看不到），属已知项 #18「错误的安全承诺」同型。

请做四件事：

1. 顺序不变量的护栏核验。CLAUDE.md 架构表 Permission 行列了几条「必须排在
   X 之后」的不变量（③规则层必须在①②之后、②′网络边界层必须在③之前、
   ②″保护路径是出口收紧器而非管线中的一站）。逐条找出钉住它的测试，
   判断那个测试的**构造方式**能不能真的抓住违反——不是看它绿不绿，
   是看把代码改坏之后它会不会红。CLAUDE.md 里明写了正确的构造方式
   （例如顺序护栏必须用「全域名 allow + 禁止地址」构造，
   用「白名单未命中」那种形态在错序下照样通过）。

2. 已知边界的现状核验。逐条检查 CLAUDE.md 里声明的「已知边界」是不是还是那个边界，
   重点这几条：deny: WebSearch 必须不带括号（带括号静默无效且无警告）、
   ask_user 无法被 deny 规则或 Hook 关掉、②″保护路径管不住 run_command、
   system_serial 工具对第④层整层免疫。每条都要在代码里找到对应实现并确认。

3. 写一份 SECURITY.md 草案。开源项目需要告诉别人漏洞往哪报，而这类工具
   （能执行任意命令、联网、读写文件）尤其需要。内容至少包含：支持的版本、
   报告渠道、响应预期、以及**用户自己要注意什么**（项目级 hooks.yaml 会直接执行、
   项目级 skills/ 与 agents/ 随仓库分发、config.yaml 含明文密钥、
   trace 产物含完整对话与文件内容）。

4. 评估 README 的安全须知对**陌生用户**够不够用。现在详细的安全边界写在
   CLAUDE.md 里（那是给 AI 读的），README 只有一节。一个陌生人 clone 一个仓库
   然后跑 rhine，他需要在 README 显眼处知道什么？

约束：
- 只读调研，不改产品代码。SECURITY.md 是新增文档，可以写。
- 产出写到 docs/review/04-security.md（新建），SECURITY.md 放仓库根。
- 每条结论必须带 文件路径:行号。
- 纯文档改动，按项目约定直接提交 main、不开分支。
- ⚠ 如果发现某条安全承诺与代码不符，那是高危发现，单独标出来，
  不要混在普通条目里。
```

</details>

---

## R3 · 坐实那 9 条缺陷 + Provider 错误分类 ✅

> **已完成（2026-08-22）**，产出在 [`02-robustness.md`](02-robustness.md)。
>
> **结论**：九条全部走完「复现路径 → 影响面 → 修法 → 代价与成对维护点」，
> 其中 **11 条真跑了复现脚本**（含一次本机 SSE 服务器、一次端到端驱动、
> 两次变异实测），产品代码零改动。
>
> **前置问题已回答**：运行期把消息送到界面的通道**存在，且有四条**。
> B1 该走的是 `AgentEventType.NOTICE` + `_wrap_events` 的攒—取模式
> （c16 F21 的 `_grant_notices` 是现成先例），`ask` 闭包与它在同一条生成器里，
> 提示会在下一个事件边界送达。**不必新造机制。**
>
> **两条证据升级**：
> - **C4** 变异实测两次——re-export 一个牵扯 `mcp` 的子模块会产生**真实可复现的
>   `ImportError`**（报错形态与 docstring 的预言逐字一致），而 **3,323 条测试全绿**。
> - **C10-a** 后果比原报告严重一档：不是「线程停到天亮」，是
>   **`app.run()` 永不返回**（asyncio 默认执行器的线程非 daemon，
>   3.11 的 `shutdown_default_executor` 无超时），连带 `cleanup` 五步一步都跑不到。
>
> ⚠ **本轮推翻了 `README.md` / `00-baseline.md` 的七处说法**，逐条收在
> `02-robustness.md` 的「附一」。要紧的三条：**C6 的因果链不成立**
> （终端不会坏，且 catch-all 兜不住主要形态，换成「退出码恒为 0」与
> 「带 locals 的回溯 + 无日志」两条真缺口）；**C7「无重试退避」是错的**
> （SDK 已在重试，实测一次 429 发了 3 次请求）；**C8 代码注释给的理由被实测推翻**
> （2 秒超时没有腰斩一次 4 秒的连续生成——流式下它是块间间隔上限）。
>
> **C3 的修法必须按新文档改**：原写法既会误报 `TodoWriteTool`（那是 `paired-maintenance`
> 明令保护的「刻意」），又只覆盖 `ToolRegistry.default()` 里的 7/20 个工具，
> **而带 `plan_stage` 的 5 个一个都不在里面**。正确的契约是**单向**的。

**原文保留：** `README.md` 的 B、C 两节列了 9 条具体缺陷（B1、B2、C3、C4、C6、C7、C8、C10 三条），
目前只**定位到了行号**，没有走完「复现路径 → 影响面 → 修法与代价」。

**为什么值得做**：定位 ≠ 坐实。修之前要知道改动面有多大、会不会牵动成对维护点。
尤其 B1（永久放行静默降级）要先确认「运行期把消息送到界面」这条通道存不存在，
否则修法根本不成立。

**完成判据**：每条给出复现步骤（能用 `--trace` 或端到端驱动复现的就实跑一次）、
修法、代价、以及牵动了哪些成对维护点。

**预计**：一天。

<details>
<summary><b>一键 Prompt（点开复制）</b></summary>

```
我在对 RhineCode 做一轮面向开源发布的代码审查。已有材料请先读：
- docs/review/README.md（已发现的问题清单）
- docs/review/00-baseline.md（机器实测数据）

这次做「R3 · 坐实已发现的缺陷」。docs/review/README.md 的 B、C 两节列了 9 条
缺陷并给了行号，但只是定位，没有走完整流程。请逐条补齐。

要坐实的 9 条（按优先级）：
1. B1 「永久放行」写盘失败静默降级（conversation.py:1816-1821 +
   permission/engine.py:570-574）
2. B2 classifier/render.py:244 的安全提醒从未接线
3. C3 Tool.execute 的签名契约无护栏（tools/base.py:229 vs 20 个实现的三种签名）
4. C4 tools/__init__.py 的致命不变量无护栏
5. C7 Provider 层把所有错误压成 str(e)（provider/deepseek.py:264-265）
6. C6 app.run() 无 catch-all（__main__.py）
7. C8 主对话 LLM 调用无超时（provider/deepseek.py:72-73）
8. C10 三条：tui/app.py:2815 无超时 wait、subagents/tasks.py:237 任务表无界增长、
   memory/manager.py:273 记忆线程 daemon 化
9. E1/E2 两个 ruff 发现的小问题（conversation.py:1727 悬空注解、config.py:347 漏 from）

每条要产出：
- **复现路径**：具体到「怎么让它发生」。能用 --trace 或 tests/e2e/ 的驱动设施
  实际复现的，就实跑一次并贴出证据；不能复现的说明为什么（例如需要写盘失败）。
- **影响面**：谁会踩到、踩到之后看到什么。
- **修法**：具体改哪里，以及有没有更好的替代方案。
- **代价**：改动面多大、会不会牵动 paired-maintenance Skill 里的成对维护点
  （动 permission/、agent/、tools/、conversation.py 之前先加载那个 Skill 查一遍）。

B1 有一个前置问题必须先回答：**「运行期把消息送到界面」这条通道存不存在？**
load_errors 只在启动时经 conversation.py:487 渲染进 startup_notice，
如果运行期没有等价通道，修法就不是「把错误塞进 load_errors」那么简单。

C7 额外要产出一张**错误分类表**：把 openai SDK 会抛的异常类型（401 认证失败、
429 限流、连接错误、超时、400 上下文超长）映射到「用户该看到什么文案 /
该不该自动重试 / 该不该记日志」。这是新手用户最容易撞上的体验缺口——
现在填错 key 看到的是一坨 SDK 原文。

约束：
- 只调研与实跑复现，**不改产品代码**。
- 产出写到 docs/review/02-robustness.md（新建）。
- 纯文档改动，按项目约定直接提交 main、不开分支。
```

</details>

---

## R4 · 剩余异常吞噬 + flaky 逐条评估 ⬜

两件规模不大但没做完的事：

- **异常吞噬**：121 处 `except Exception` 里，40 处直接 `pass` 的已经分类完
  （36 处刻意 fail-safe、4 处可疑），**剩下 81 处「吞掉后做了别的事」的没过一遍**。
- **flaky**：`README.md` 提到了几条高风险用例，但只是列出来，没逐条评估。
  最隐蔽的是 `tests/test_memory_manager.py:177`——「睡 0.05 秒后断言线程没启动」，
  **否定式断言配固定睡眠，机器慢时会假绿而不是假红**。

**为什么值得做**：假绿的测试比没有测试更糟——它让你以为验过了。

**预计**：半天。

<details>
<summary><b>一键 Prompt（点开复制）</b></summary>

```
我在对 RhineCode 做一轮面向开源发布的代码审查。已有材料请先读：
- docs/review/README.md
- docs/review/00-baseline.md

这次做「R4 · 剩余异常吞噬 + flaky 评估」，两部分。

第一部分：异常吞噬的剩余 81 处。
全仓有 121 处 except Exception，其中 40 处后面直接 pass 的已经分类完了
（36 处是有据可查的刻意 fail-safe——观测设施漏斗、Hook 分发点、TUI 渲染兜底、
资源关闭阶段、进程树强杀；4 处可疑已记在 docs/review/README.md）。
**剩下 81 处是「吞掉之后做了别的事」的**，还没过一遍。请逐处判断：
- 吞掉之后做的那件事，是不是让用户/模型看到了真实情况？
- 还是把一个真故障伪装成了正常结果？
重点看 Provider 调用、文件写入、权限判定、配置写盘这几条路径上的。
已确认干净、不用再看的：四个 YAML 加载器（它们返回警告而非静默降级）、
工具参数校验（19 个 execute 入口零 args["key"] 直接索引）。

第二部分：flaky 逐条评估。
以下用例已被识别为高风险，请逐条判断「它会假红还是假绿」，并给出改法：
- tests/test_memory_manager.py:177 —— sleep(0.05) 后断言 provider.calls == []
  （否定式断言配固定睡眠，机器慢时假绿。这条最隐蔽，优先看）
- tests/test_subagent_gate.py:142/246/278 —— 三处裸 sleep 编排跨线程时序，
  其中 :246 是 while provider.calls < 2: sleep(0.01) 的无超时忙等
- tests/test_team_mailbox.py:84 —— 依赖 time.time() 单调（项目别处用的是 time.monotonic()）
- tests/test_bootstrap.py:319-364 —— 起真子进程 + 60 秒轮询
- tests/e2e/c14_scenarios.py:612 —— 用 time.time() - 30*86400 造过期时间戳，依赖系统时钟

⚠ 以下常数项目明说「不要动」，各有理由，只评估不建议改：
tests/test_subprocess_timeout.py 的 CHILD_SLEEP=6 / THRESHOLD=4.0
（那是判别余量，缩小换速度会引入 flaky，且它对应一个真实产品缺陷）。
背景见 CLAUDE.md 的「测试」一节与 docs/internals/testing.md。

约束：
- 只读调研，不改代码也不改测试。
- 产出追加到 docs/review/02-robustness.md（若 R3 已建则追加，否则新建）。
- 纯文档改动，直接提交 main。
```

</details>

---

## R5 · 跨平台与安装实跑 ✅

> **已完成（2026-08-29）**，产出在 [`01-release-blockers.md`](01-release-blockers.md)。
>
> **结论**：四个完成判据全部有实测答案，并**顺手查出一条比 A2 更严重的发布阻塞项**。
>
> - **R5-1 🔴 三个内置子 Agent 角色（`explorer` / `planner` / `general-purpose`）
>   一个都没进分发包**，`pip install .` 之后扫描结果是「0 个角色、**0 个错误**」——
>   完全静默。根因是 `pyproject.toml` 的 package-data 只写了 `rhinecode.skills`，
>   漏了 `rhinecode.subagents`。**C13 的「定义式委派」在任何 pip 安装的副本里都是废的。**
> - **R5-2 🔴 `pyproject.toml:30-54` 那段注释的「实测三组表」是错的**：
>   package-data **不是「冗余保险」，是唯一开关**——删掉它 `.md` 一个都不进包
>   （四组实验，含带 `.git` 的一组）。
> - **R5-3 🔴 `textual` 的真实下界是 6.2.1**，不是声明的 0.80.0，也不是 A2 猜的 8.0。
>   二分实测 20 个版本，边界用全量 3,323 条复核过（6.2.0 红 5 条 / 6.2.1 全绿）。
>   卡住下界的**不是 API 而是一个功能 bug**——「焦点在 Input 上时选不到别处的文本」，
>   低于 6.2.1 装得上、界面正常，**只有 `Ctrl+C` 复制默默复制不全**。
> - **Linux（Ubuntu 24.04 / Python 3.12）全量 3322 / 3323，26.9 秒**。唯一失败的是
>   `test_subprocess_timeout` 的**反证半边**——产品代码在两个平台上都是对的，
>   **是判据量错了东西**（它量时间差，而时间差只是 Windows 上的症状）。
> - 另有四条：**openai 无上界**（干净装出来是 3.6.0、开发机是 1.109.1，实测行为等价）、
>   **Linux 剪贴板静默失效且该分支零测试**、**Windows 生成的 `mcp.yaml` 单向不可移植**、
>   4 条测试硬编码 `python`。
> - **PyPI 的 `rhinecode` 未被占用**，可以直接注册；但 `rhinocode` 是 McNeel Rhino 8
>   官方 CLI 的名字，`rhodecode` 是 PyPI 上活跃的同领域包。建议保持名字不变、
>   在 README 第一句撇清。
> - **LICENSE 推荐 MIT**（依赖全是宽松许可证，没有任何约束），理由与 Apache-2.0
>   的两种改推情形见那份文档的 R5-10。
>
> ⚠ **它推翻/修正了七处既有说法**，收在那份文档的「附一」，其中要紧的是
> **A2 的后果描述过于响亮**——2.0.0 ~ 6.2.0 之间 import 是过的，失败形态是
> 运行期行为不对，比 A2 写的更隐蔽。

**全项目唯一从未被验证过的维度（原文保留）。** 开发机是 Windows，而代码里有几处不等价的平台分支。
另外 F4 要改 `textual` 的版本下界，**正确的值只能靠实跑二分验出来**。

**为什么值得做**：`pip install -e .`（你一直用的）与 `pip install .`（真实用户走的）
是两条不同的路径。包数据打没打进去、入口点在干净环境里能不能跑、Linux 上装不装得上，
全是未知。

**完成判据**：① `textual` 的真实最低可用版本；② 干净 venv 安装 + 启动的结果；
③ WSL 里跑一遍全量测试的结果；④ PyPI 名字可用性 + LICENSE 选型说明。

**预计**：半天到一天（多数时间在等安装）。

<details>
<summary><b>一键 Prompt（点开复制）</b></summary>

```
我在对 RhineCode 做一轮面向开源发布的代码审查。已有材料请先读：
- docs/review/README.md
- docs/review/00-baseline.md

这次做「R5 · 跨平台与安装实跑」。这是全项目唯一从未被验证过的维度
（开发机一直是 Windows，且一直用 pip install -e . 而不是真实用户走的 pip install .）。

请做五件事：

1. **验出 textual 的真实最低可用版本**。现在 pyproject.toml:9 写的是
   textual>=0.80.0，而代码 import 了 textual.content（Content, Span）与
   textual.style（Style）——见 tui/widgets.py:32,34——这些是 Textual 3.0+ 的 API，
   本机实装的是 8.2.8。下界差 7 个大版本。
   请在干净 venv 里二分测试：装不同版本的 textual，跑 `python -m compileall rhinecode`
   加一次能覆盖 TUI 的测试子集（例如 python -m unittest tests.test_tui_layout），
   找出真正能跑的最低版本。产出一个准确的版本区间建议（含上界）。

2. **干净 venv 的完整安装验证**：新建 venv → pip install .（注意不是 -e）→
   rhine --help → 确认内置 Skill 样板（rhinecode/skills/builtin/ 下的 md 文件）
   真的被打进包了。pyproject.toml 里那段 package-data 的注释说它是「冗余保险」，
   顺便验证一下那个结论还成立。

3. **WSL（或任意 Linux）里跑一遍**：pip install . + python -m tests.run_parallel。
   重点关注这几处平台分支在 Linux 下的行为：
   - tools/run_command.py:100 与 :259（两条不等价的 os.name 分支，
     而这是「用户可触发任意命令」的路径，进程组/信号/超时杀进程语义差异最大）
   - tui/clipboard.py:56,58（只显式枚举了 win32 与 darwin，Linux 落 else 兜底）
   - mcp/transport.py:54 + mcp/auto_config.py:132（同一个 Windows .cmd 假设的两处实现）

4. **PyPI 名字可用性**：查 rhinecode 这个名字在 PyPI 上是否已被占用，
   顺便看看有没有明显的重名/商标冲突。

5. **LICENSE 选型说明**：给 MIT / Apache-2.0 / GPL-3.0 一份三选一的取舍说明
   （目标是开源到 GitHub + 个人作品集展示），给出推荐并说明理由。不要直接写 LICENSE 文件，
   那是 F1 的事。

约束：
- 不改产品代码（改 pyproject.toml 是 F4 的事，这里只给建议值）。
- 产出写到 docs/review/01-release-blockers.md（新建）。
- 实跑结果要贴原始输出，不要只写结论。
- 纯文档改动，直接提交 main。
```

</details>

---

## R6 · 交付物草案（CI + 工具链 + CONTRIBUTING + 架构图）⬜

阶段 5 的全部内容。**只出草案，装不装由你定。**

**为什么值得做**：CI 那一件事同时解决三个盲区——只在 Windows 验过、PR 无门禁、
别人提 PR 没法自动验。而且作品集视角下，README 顶上一个绿色徽章比一万字架构说明管用。

**预计**：半天。

<details>
<summary><b>一键 Prompt（点开复制）</b></summary>

```
我在对 RhineCode 做一轮面向开源发布的代码审查。已有材料请先读：
- docs/review/README.md
- docs/review/00-baseline.md（尤其第 3 节 ruff 与 3b 节 mypy 的原始数据）

这次做「R6 · 交付物草案」。只产出草案与建议，不实际安装到仓库。

1. **GitHub Actions 配置草案**。矩阵 windows-latest × ubuntu-latest ×
   Python 3.11/3.12/3.13。跑 python -m tests.run_parallel（33 秒）
   加一次干净安装验证（pip install . 后 rhine --help）。
   注意两个坑：① 测试需要本机装 git（有预置依赖真实提交历史，缺 git 是硬失败不是跳过）；
   ② run_parallel 给每个分片派独立临时目录靠的是 TEMP/TMP/TMPDIR 环境变量，
   在 CI 环境里要确认这套隔离仍然成立（背景见 tests/run_parallel.py 的 docstring）。

2. **ruff 落地建议**。实测数据在 00-baseline.md 第 3 节。关键约束：
   RUF001/002/003（ambiguous-unicode）对中文标点全量触发，一口气 29,867 条，
   **必须先禁用这三条否则工具完全不可用**。请给一个渐进方案：
   第一步开哪几组（建议 E/F/B，总共只有 78 个问题且其中 2 个是真 bug）、
   哪些规则要 ignore 及理由（例如 RUF012 的 35 个全是 parameters JSON schema
   与 Textual BINDINGS，属误报）、以及 pyproject.toml 里 [tool.ruff] 该怎么写。
   **建议保守**——一次开满会产出几千条告警然后没人看。

3. **mypy 落地建议**。首次跑是 109 错 / 19 文件（数据在 00-baseline.md 3b 节）。
   给一个「先只管新代码」的渐进方案，以及那 16 个 union-attr
   （Optional 未判空就取属性，最可能是真 AttributeError）该不该先清。

4. **CONTRIBUTING.md 草案**。这个项目有一套很特别的开发约定需要写给贡献者：
   spec 驱动开发（/spec 生成四份文档）、成对维护点（paired-maintenance Skill）、
   强制详尽中文注释、章节 vs 扩展的判据、测试怎么跑。
   材料在 CLAUDE.md 与 docs/extensions/README.md。

5. **一张架构图**。CLAUDE.md 那个 20 行的分层表格对人类不如一张图。
   建议用 mermaid（GitHub 原生渲染）。注意真实的依赖结构不是干净分层——
   有 10 个包处于强连通分量（见 00-baseline.md 第 5 节），
   图要如实反映而不是画一个好看但不对的分层图。

约束：
- 产出写到 docs/review/05-maintainability.md（新建），配置文件作为代码块附在文档里，
  **不要直接写进仓库根**（装不装由用户定）。
- 纯文档改动，直接提交 main。
```

</details>

---

## R7 · 文档漂移的根治方案 ⬜

`README.md` 的 D 节列了 4 条漂移实例，但**没给根治方案**。

**为什么值得做**：那些不是孤立笔误，是「同一个事实写在 N 处、靠人记得同步」的必然产物。
`docs/todo/README.md:102` 自己记着某处引用「**四次**成为悬空引用」——而本次审查发现
那是第五次。人再细心也治不了这个。

**预计**：两三小时。

<details>
<summary><b>一键 Prompt（点开复制）</b></summary>

```
我在对 RhineCode 做一轮面向开源发布的代码审查。已有材料请先读 docs/review/README.md
（尤其 D 节「文档漂移」的 4 条实例）。

这次做「R7 · 文档漂移的根治方案」。

已发现的 4 条漂移：
- D1 CLAUDE.md 第 98 行说 Skill 体检「八项检查」，第 263 行说「七项」，
  skills/audit.py:126-132 实际调 7 个函数（README 两处也写「八项」）
- D2 trace 事件类数：README 两处「二十七类」、CLAUDE.md「二十九类」、
  trace/models.py 实际 31 个常量
- D3 docs/todo/ 内部两处悬空引用（且该目录 README:102 自己记着同一处
  已经「四次」悬空——本次是第五次）
- D4 「叶子包」表述在包级别不成立（skills 被称叶子包但 import permission）

请分析根因并给出根治方案，重点回答：
1. 哪些数字**应该由代码算出来而不是手写**？（测试条数、trace 事件类型数、
   Skill 体检项数、命令数、扩展数……）逐个判断可行性。
2. 生成方式怎么选：跑一个脚本改文档？还是文档里放占位符 + CI 校验？
   还是干脆写一条测试断言「文档里的数字与代码一致」？
   本项目已经有先例可参考——tests/run_parallel.py 用「先 discover 拿期望条数、
   跑完比对」来防漏跑，同一个思路能不能搬到文档上？
3. docs/todo/ 的编号引用问题：那个目录的 README 自己规定了重排后要跑
   grep -rn "docs/todo/[0-9]\|第 [0-9] 条 todo" docs/todo/ 自检，
   但仍然第五次失效。是自检命令不够，还是没人记得跑？给一个不依赖记性的做法。
4. 3.4 MB 文档（171 个 markdown，比 1.8 MB 代码还多）的分层建议：
   哪些该留、哪些该合并、哪些该降级。注意 CLAUDE.md 是给 AI 读的、
   README 是给人读的，两者的维护频率天然不同（这正是 README 落后一章的成因）。

约束：
- 只出方案，不实际改任何文档里的数字（那是 F6 的事）。
- 产出追加到 docs/review/03-architecture.md，或新建 docs/review/06-docs-drift.md。
- 纯文档改动，直接提交 main。
```

</details>

---

# F 系列 · 已发现问题的修复（改代码）

> ⚠ **F 系列全部要先开分支**（`git checkout -b <名字>`），
> 且动 `agent/`、`permission/`、`tools/`、`conversation.py`、`bootstrap.py` 等目录前
> **先加载 `paired-maintenance` Skill**。

## F1 · 第一批：机械清理 ✅

**内容**：A1 LICENSE、C2 `rich` 声明、E3 ruff 自动修、E5 删残留空目录、E6 补 `.gitignore`。
**前置**：R5 的 LICENSE 选型结论。
**预计**：半天。分支名建议 `release-prep`。

<details>
<summary><b>一键 Prompt（点开复制）</b></summary>

```
先跑 git branch --show-current，如果在 main 上就先 git checkout -b release-prep。

我在按 docs/review/README.md 的清单修问题，这次做「第一批：机械清理」，
五件小事，都不涉及逻辑改动：

1. A1：加 LICENSE 文件。选型结论见 docs/review/01-release-blockers.md
   （如果那份还没有，先问我要用哪个），同时在 pyproject.toml 的 [project] 加 license 字段。

2. C2：pyproject.toml 的 dependencies 加一行 rich>=13。
   理由：rhinecode/tui/widgets.py:23-29 直接 import 了 7 个 rich 模块，
   但它只是 textual 的传递依赖，从未显式声明。现在不会崩（textual 8.2.8 的
   Requires 里有 rich），但 textual 一直在减少对 rich 的依赖。

3. E3：跑 ruff check rhinecode tests --select F401,F841,F541,C4 --fix
   自动清理 45 个未使用 import、5 个未使用变量、6 个无占位符 f-string、1 个 C408。
   ⚠ 修完必须跑 python -m tests.run_parallel 确认 3323 项仍然全绿——
   有些 import 可能是为副作用而存在的。

4. E5：删掉 tests/eval/ 这个残留空目录（只含 __pycache__，git ls-files 返回空，
   文档 0 次提及）。

5. E6：.gitignore 补上 .vscode/、.idea/、.DS_Store、.coverage、htmlcov/、
   .mypy_cache/、.ruff_cache/。理由是开源之后不同 IDE 的贡献者会污染仓库。

约束：
- 每件事一个独立 commit，不要攒在一起。
- 全部做完跑一次 python -m tests.run_parallel，确认 3323/3323。
- 按项目约定，代码改动走分支 + PR。
```

</details>

---

## F2 · 第二批：两个产品缺陷 ✅

**内容**：B1「永久放行」静默降级、B2 安全提醒未接线。
**前置**：R3 坐实（尤其 B1 的「运行期消息通道存不存在」这个问题）。
**预计**：半天。分支名建议 `fix-permanent-allow-silent-fallback`。

<details>
<summary><b>一键 Prompt（点开复制）</b></summary>

```
先跑 git branch --show-current，如果在 main 上就先开分支
（建议名 fix-permanent-allow-silent-fallback）。
动 permission/ 与 conversation.py 之前先加载 paired-maintenance Skill。

我在按 docs/review/README.md 的清单修问题，这次做「第二批：两个产品缺陷」。
详细的复现路径与修法分析在 docs/review/02-robustness.md（若还没有，先做 R3）。

1. B1：「永久放行」写盘失败时静默降级成「本会话放行」。
   位置：conversation.py:1816-1821 + permission/engine.py:570-574。
   现状：面板文案（tui/widgets.py:3608）明写「写入本地配置，重启仍生效」，
   而 persist_local_rule 返回 False 时代码直接改加一条 session 规则并 return True，
   零提示。错误串被塞进 self.load_errors，而它唯一的消费点 conversation.py:487
   是**启动时**渲染的，运行期追加的条目永远不会显示。
   净效果：用户以为授权持久化了，重启后凭空失效，当场无任何信号。
   ⚠ 同段 :1799-1806 的注释记录着同一类失效已经修过一次（url 类规则写坏那次），
   这是同一个坑的第二个入口——修的时候看一下那次是怎么修的。

2. B2：classifier/render.py:244 的 render_broad_domain_warning() 实现完整
   但从未被调用（全仓含 tests 总提及次数 = 1，只有定义处）。
   它是 C16 spec F22 的「全域名放行规则会让分类器对网络访问完全不生效」的启动提醒。
   请接到启动通知链路上，与 classifier 丢弃宽泛命令规则的那批说明同一个出口
   （在 bootstrap.py 里找那段）。
   ⚠ 这条属于「错误的安全承诺」类问题（已知项 #18 同型）——
   文档承诺了会提醒而实际不会，比不承诺更危险。

两条都要补护栏测试：B1 要钉「写盘失败时用户能看到」，B2 要钉「宽泛域名规则会产生警告」。

约束：
- 两条分开 commit。
- 跑 python -m tests.run_parallel 确认没回归。
- 走分支 + PR。
```

</details>

---

## F3 · 第三批：两条护栏（性价比最高）✅

**内容**：C3 `Tool.execute` 签名契约、C4 `tools/__init__.py` 不变量。
**前置**：无。
**预计**：1 小时。分支名建议 `add-contract-guards`。

**这是整份清单里性价比最高的一项**——20 行代码钉住两个项目自己列为「⚠ 致命不变量」
却没有任何测试保护的地方。

<details>
<summary><b>一键 Prompt（点开复制）</b></summary>

```
先跑 git branch --show-current，如果在 main 上就先 git checkout -b add-contract-guards。

我在按 docs/review/README.md 的清单修问题，这次加两条护栏。
两处都是项目自己在 CLAUDE.md 里列为「⚠ 致命不变量」、却没有任何测试钉着的地方，
失败形态都是本项目定义的那种「漏改一律不报错」。

1. C3：Tool.execute 的签名契约。
   基类签名 tools/base.py:229 是 execute(self, args: dict)，既无 cwd 也无 plan_stage；
   而 20 个实现分裂成三种签名（8 个 execute(args)、6 个 execute(args, cwd=None)、
   6 个 execute(args, plan_stage=False)）。调用方 agent/loop.py:2107 与 :2265-2269
   靠 tool.workspace_aware / tool.plan_safe 两个布尔标志决定传什么。
   现有护栏 tests/test_loop_cwd_dispatch.py 的 7 条用**替身工具**验分发逻辑，
   不验真实工具的签名与标志是否一致。全仓只有 tests/test_todo_tool.py:99
   一处做过 inspect.signature 检查。
   失败形态：新工具声明 workspace_aware = True 却忘给 execute 加 cwd=None
   → 编译过、3323 项测试全绿 → 只在该工具真被调用时 TypeError。
   而且两条路径表现还不一样：串行路径 :2272 有 except Exception 兜成「工具执行异常」，
   并发路径 :2107 刻意不加 try/except。
   **请写一条测试**：遍历 ToolRegistry.default() 的全部工具，用 inspect.signature
   断言「声明 workspace_aware 的必须接受 cwd 形参、声明 plan_safe 的必须接受
   plan_stage 形参」，反之亦然（没声明的不该有那个形参）。
   注意 MCP 工具是动态注册的，判断一下要不要排除。

2. C4：tools/__init__.py 必须不 import 任何子模块。
   那个文件有 2799 字节 docstring 把互依结构、为什么不成环、违反后的报错形态
   逐条写明，并标注「警告：不要在此处 re-export 任何子模块」。
   CLAUDE.md 架构表也列为致命不变量。AST 扫描确认有 10 个包处于强连通分量，
   这条约定是它们不成环的唯一依靠。
   但同类约束在别的包上都有护栏（permission/__init__.py 的在
   tests/test_classifier_broad.py:219，trace/__init__.py 的在
   tests/test_trace_reader.py:404），唯独 tools 一条都没有。
   **请写一条测试**：解析 rhinecode/tools/__init__.py 的 AST，
   断言里面没有 Import / ImportFrom 节点。

两条测试都要写清楚 docstring：违反会发生什么、为什么这条不能简化。
参照 tests/test_classifier_broad.py:219 与 tests/test_trace_reader.py:404 的写法。

约束：
- 只加测试，不改产品代码。如果新测试当场就红了，那说明发现了真问题，先告诉我。
- 跑 python -m tests.run_parallel 确认全绿。
- 走分支 + PR。
```

</details>

---

## F4 · 第四批：textual 下界 + CI ✅

> **已完成（2026-08-30）**，见 PR #58（分支 `add-ci`）。
>
> **结论：CI 一装上就抓出五处问题（两个产品缺陷 + 三处判据问题），
> 而它们都不是「跨平台适配」那一类。**
> 矩阵 6 个格子（`{windows, ubuntu} × {3.11, 3.12, 3.13}`）加两个干净安装 job，
> 最终**全绿，六格各跑满 3348/3348**（分片完整性自检也过，没有模块被漏掉）。
> 3.13 与 Linux 都是**头一回**被验。
>
> 逐条归因（全程没有为了让它绿而 skip 任何用例）：
>
> | 轮次 | 红在哪 | 归因 | 处置 |
> | --- | --- | --- | --- |
> | 1 | 三个 Linux 格子，全红在 `test_subprocess_timeout` 的**反证半边** | **判据的问题**。同文件另外三条在 Linux 上全绿（立刻返回 / 进程树真的死了），产品是对的。反证量的是「朴素写法必须慢」，而那只是 Windows 那一侧的症状——POSIX 上该缺陷表现为「泄漏一棵进程树」，判据看不见 | 判据换成两条性质的组合（正面两条都要满足 / 反证至少破一条），平台无关且更严。**变异实测过** |
> | 2 | `windows/3.11` 一格，`test_clear_empties_the_activity_area` | **产品真缺陷**，看起来像 flaky。`/clear` 同步清了活动区一次，但界面每 0.5 秒拿 `activity_rows()` 重画，而它**不认会话代**——下一轮轮询就把上一段对话的残影刷回去，正是 tui-display F8 要挡的那件事，它只挡住了第一帧 | `activity_rows` 补上代号过滤（机制早有，`take_deliverables` 一直在用，只是活动区这侧漏了）。补两条护栏含反证 |
> | 3 | `windows/3.11` 一格，`test_ctrl_o_reveals_recent_calls_and_folds_back` | **判据的问题**。AC15 拿切档前后的完整文本做全等比较，而活动行里有一个一直在走的钟，跨过整秒边界就红 | 只抹掉秒数那一个数字再比，其余逐字保留（验证过：多出第三态 / 工具行少了 / token 变了，三种仍然不相等） |
> | 5 | `windows/3.11` 一格，`test_bootstrap_fatal_reported_over_channel` | **判据的问题**。那个场景里宿主本来就会自己退（用例写的就是 `expect_exit=1`），「看到 fatal」与「发 quit」之间有个窗口，宿主先走一步 socket 就被重置 | `quit` 改成尽力而为，退出码与名片消失仍是真判据。反证过：把 quit 换成「假装送到了」，正常场景当场红 |
> | 4 | `windows/3.12` 一格，`test_three_isolated_delegations_get_distinct_worktrees`（⚠ **那一轮只改了文档**） | **产品真缺陷**。`git worktree add` 在同一版本库上不是并发安全的（它开工时隐式 prune，会清掉另一个正建到一半的工作区目录），而 C14 明确支持并发隔离委派，`lifecycle.create` 却对动版本库的那几步零串行 | ④环境确认 / ⑤挑分支 / ⑥add 收进一把模块级锁。**护栏钉的是性质不是症状**（本机复现不出来，照症状写等于永远绿）：断言任何时刻至多一个创建在临界区内。变异实测去掉锁 → 峰值 8 |
>
> **四条都只在慢机器上现形。** 那几个格子的分片分别跑了 174s / 150.9s / 188.9s，
> 平时约 60s——本机再跑多少遍也撞不上，这正是「623 次提交零机器验证」这件事的成本。
> ⚠ 尤其是第 4 轮：**那一轮只改了文档**，它照样红了——缺陷此前一直都在，
> 只是没有任何东西会去撞它。
>
> ⚠ **值得记的一条观察**：Windows runner 慢到足以把一批**潜伏的时间假设**同时照出来
> ——五轮红出在五个互不相干的地方（超时反证 / 活动区 / 活动区文本 / worktree 并发 /
> e2e 宿主退出握手），共同点只有一个：**判据或实现里藏着「这一步会很快」的假设**。
> 最后连续两轮全绿，但这类假设未必已经清干净，后续再红优先按这个方向看。
>
> ⚠ **一条方法论**：几条修法里有两条的护栏**不能照症状写**。超时那条的症状
> 只在 Windows 上成立，工作区那条的症状本机根本复现不出来——照症状写的判据
> 会在开发机上永远绿，等于没有。两条都改成钉**性质**（「至少违反一条」/
> 「至多一个在临界区内」），并各做一次变异实测确认它真的会红。
>
> ⚠ **一处对 R5 的更正**：R5-8 判断「4 条硬编码 `python` 的用例会让 Linux runner
> 稳定红」，**实际没有**——`actions/setup-python` 会在 PATH 上提供 `python`。
> 那条建议（换成 `sys.executable`）仍然值得做，但它不是 CI 的阻塞项。
>
> ⚠ **前置材料缺了一份**：本项 `前置` 写着「R6（要用它的 CI 草案）」，而 R6 尚未开工、
> `docs/review/05-maintainability.md` 不存在。CI 配置是照本文 R6 那份一键 Prompt 里
> 列的要求直接写的（矩阵 / `run_parallel` / 干净安装 / 那两个坑），**R6 的其余四项
> （ruff、mypy、CONTRIBUTING、架构图）仍然没做**。

**内容**：A2 改版本约束、C1 装 CI。
**前置**：R5（要用它验出的正确版本值）、R6（要用它的 CI 草案）。
**预计**：一天。分支名建议 `add-ci`。

<details>
<summary><b>一键 Prompt（点开复制）</b></summary>

```
先跑 git branch --show-current，如果在 main 上就先 git checkout -b add-ci。

我在按 docs/review/README.md 的清单修问题，这次做「第四批：依赖约束 + CI」。
前置材料：docs/review/01-release-blockers.md（textual 真实下界的实测结论）
与 docs/review/05-maintainability.md（CI 配置草案）。

1. A2：按 01-release-blockers.md 实测出的值修正 pyproject.toml 的 textual 约束。
   现状是 textual>=0.80.0，而代码用了 textual.content 与 textual.style
   （Textual 3.0+ 的 API），下界差 7 个大版本，装到旧版启动即 ModuleNotFoundError。
   顺便给其余三个依赖（openai / pyyaml / httpx）也加上界。

2. C1：装 GitHub Actions。用 05-maintainability.md 里的草案。
   矩阵 windows-latest × ubuntu-latest × Python 3.11/3.12/3.13。
   ⚠ 两个坑：① 测试需要 runner 上有 git（有预置依赖真实提交历史，缺 git 是硬失败）；
   ② run_parallel 的分片隔离靠 TEMP/TMP/TMPDIR 环境变量，CI 环境里要确认仍然成立。
   第一次跑大概率会红——那正是这件事的价值（Linux 从未验过）。
   红了就逐条归因：是产品的跨平台问题，还是测试对 Windows 的隐含假设。

3. 顺便给 README 加 CI 徽章。

约束：
- CI 配置与依赖约束分开 commit。
- 如果 CI 在 Linux 上跑红，**先归因再修**，不要为了让它绿而 skip 用例。
- 走分支 + PR。
```

</details>

---

## F5 · 第五批：README 重写 + 截图 ⬜

**内容**：A3 README 重写、A4 录 GIF。
**前置**：建议在 F4 之后（这样能带上 CI 徽章）。
**预计**：一天。分支名建议 `readme-rewrite`。

<details>
<summary><b>一键 Prompt（点开复制）</b></summary>

```
先跑 git branch --show-current，如果在 main 上就先 git checkout -b readme-rewrite。

我在按 docs/review/README.md 的清单修问题，这次重写 README。
⚠ 是**重写不是修补**——现有的 1250 行是「开发日志式」的，
第一屏就是 15 行能力表加每条几百字，而陌生人第一眼只看 README。

现有 README 的已知错误（顺便一次性清掉）：
- 能力表止于 C15，而 C16 已是主线；全文 classifier / 分类器 / C16 /
  web_search / todo_write 出现 0 次
- :1146 写测试「2235 项」，实际 3323
- :1064-1065 目录树写 memory/notes.py、note_updater.py，这两个文件不存在
  （实际是 memories.py、memory_updater.py）
- :37 把已删除的 /perm 当现存命令用
- 目录树整个缺 classifier/ 与 todo/ 两个包，tools/ 少列 5 个、web/ 少列 3 个
- :1212 是坏链（指向 docs/todo/3-delegation-trigger-eval.md，实际是 2-）
- :1110 写「13 条内置命令」，实际 15 条
- :1206 列了 5 个扩展，docs/extensions/ 下实际有 9 个
- 「八项检查」应为七项（skills/audit.py:126-132 实际调 7 个函数）
- 「二十七类」trace 事件应为 31（trace/models.py 的常量数）

新 README 的结构建议（面向陌生人 + 作品集）：
1. 一句话讲清这是什么
2. 一张 GIF（见下面第 2 件事）
3. 30 秒装上跑起来
4. 核心特性 5–8 条，每条两行以内，链到深度文档
5. 安全须知（这是个会执行任意命令、联网、读写文件的工具，陌生用户要知道风险）
6. 架构一图流（用 docs/review/05-maintainability.md 里的 mermaid 图）
7. 文档索引 → 现有的 1250 行内容降级到 docs/ 下

第二件事：录 3 张 GIF 放 docs/assets/：
① 完整对话 + 工具调用 ② 权限确认面板 ③ 子 Agent 并行活动区。
Windows 下可用 ScreenToGif。⚠ 录之前把 config.yaml 里的真实 api_key 换成假值，
并确认画面里不出现任何真实路径/密钥。

约束：
- 旧 README 的内容不要丢，降级到 docs/ 下相应位置。
- 走分支 + PR。
```

</details>

---

## F6 · 第六批：其余 ⬜

**内容**：C5–C10（日志设施、`app.run()` 兜底、Provider 错误分类、LLM 超时、
包元数据与 `__version__`、三条资源生命周期）+ D1–D4（文档漂移的具体数字）+ E1/E2/E4。
**前置**：R3（修法分析）、R7（漂移根治方案）。
**预计**：按需，可拆成多个小 PR。

<details>
<summary><b>一键 Prompt（点开复制）</b></summary>

```
先跑 git branch --show-current，如果在 main 上就先开分支。

我在按 docs/review/README.md 的清单修问题，这次做「第六批：其余」。
建议拆成多个小 PR 而不是一次做完。修法分析在 docs/review/02-robustness.md。

代码类（C 系列，每条一个 commit）：
- C5 日志设施：现在 import logging 只有 1 处（commands/dispatcher.py:12），
  唯一调用 :150 的 _logger.debug 因为全仓无 handler 而永远不输出。
  加 --log-file 与最小配置，关键路径各补一条 INFO。
- C6 app.run() 加 catch-all（__main__.py），确保终端复位——
  Textual 改过终端模式，逃逸异常会让用户得敲 reset。
- C7 Provider 错误分类（provider/deepseek.py:264-265），按 02-robustness.md 里那张表。
- C8 主对话 LLM 超时（provider/deepseek.py:72-73，Config.request_timeout 默认 None）。
- C9 包元数据 + __version__（rhinecode/__init__.py 现在是 0 字节空文件，
  mcp/client.py:20 的 _CLIENT_VERSION 硬编码副本改为引用它）。
- C10 三条：tui/app.py:2815 无超时 wait、subagents/tasks.py:237 任务表无界增长、
  memory/manager.py:273 记忆线程 daemon 化的半写风险。
  ⚠ 这三条各有取舍，改之前先读 02-robustness.md 里的分析——
  尤其 memory 那条，子 Agent 线程 daemon 化是有明确理由的（runner.py:1005-1006），
  不要一起改。

文档类（D 系列 + E）：
- D1 八项/七项：CLAUDE.md:98 与 README 两处改为「七项」（audit.py:126-132 是 7 个）
- D2 trace 事件类数：README 两处「二十七类」、CLAUDE.md「二十九类」→ 实际 31
- D3 docs/todo/ 两处悬空引用：2-delegation-trigger-eval.md:6 与
  1-skill-recall-eval.md:70
- D4 「叶子包」表述：按 00-baseline.md 第 5 节的实测结果修正
- E1 conversation.py:1727 的 -> "AskFn" 悬空注解（AskFn 定义在 agent/loop.py:211，
  从未导入）
- E2 config.py:347 漏了 from（紧邻的 349 行就写了 from e）
- E4 77 个 unused-noqa

⚠ D 系列改完之后，如果 R7 给出了「数字自动生成」的方案，考虑一并落地——
否则这批数字下次还会漂。

约束：
- 代码类走分支 + PR；纯文档改动按项目约定可直接提交 main。
- 每批做完跑 python -m tests.run_parallel。
```

</details>

---

## 全部做完之后

审查完整结束的标志是这三件事都成立：

1. **`docs/review/` 下六份报告齐全**（00 基线 / 01 发布阻塞 / 02 健壮性 /
   03 架构 / 04 安全 / 05 可维护性），每条结论带 `文件路径:行号`。
2. **README.md 的问题清单里每条都有归宿**——修了、或明确记为「知道但不做」
   （后者要写进 `docs/internals/known-issues.md`，那是项目登记长期工程债的地方）。
3. **CI 绿灯**，且至少在 Windows 与 Linux 两个平台上跑过。

剩下的那件事不在本次审查范围内，但值得记一笔：**`agent/prompt/texts/` 那 10 个
提示词文件是唯一真正的测试盲区**（覆盖率显示接近 100% 但那是假的，模块级常量
import 即覆盖）。补它的办法是 `docs/todo/` 里现存的第 1、2 条（Skill 召回率评测、
委派触发率评测），那属于产品待办而非审查项。
