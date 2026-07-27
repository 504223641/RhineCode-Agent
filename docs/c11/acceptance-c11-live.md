# C11（Skill 系统）真实模型端到端验收报告

> 对应 `docs/c11/checklist.md` 第十一节的**场景 1 / 3 / 4**。
>
> 这三条的判据是「**真实模型是否按 SOP 行事**」，属模型行为，因此：
>
> - **必须用真实模型跑**（`--mode live`）。脚本化假模型按轮次照本宣科，
>   它「按 SOP 行事」只能证明剧本是这么写的，什么也验不了；
> - **刻意不写成自动化回归测试**。模型行为不确定，今天绿明天红的测试最终一定会被
>   skip 掉，那时它既不报警也没人再看。只验一次、留证据，就是本文件。
>
> 驱动方式：`python -m tests.e2e.host --mode live --seed tests.e2e.c11_scenarios:<预置>`，
> 预置见 `tests/e2e/c11_scenarios.py`（**只有预置、没有剧本**，理由同上）。
> 模型：`deepseek-v4-flash`，`context_window=1000000`。
>
> 每条判据分两栏写：**机器判到了什么**（记录里可复核的事实）与
> **我据此做的判断**（人对「这算不算按 SOP 行事」的裁定）。两者分开，
> 是因为前者可复核、后者可争论，混在一起就都不可信了。
>
> 真实模式产物（含真实模型往返与工具输出原文）跑完即删，本文件只留摘录。

## 总览

| 场景 | 判据 | 结果 |
| --- | --- | --- |
| 1 共享模式全流程 | 7/7 | ✅ |
| 2 独立模式隔离 | 5/5 | ✅ |
| 3 模型自主两阶段加载 | 5/5 | ✅ |
| 4 白名单收窄可观测 | 6/6 | ✅ |
| 5 热更新 | 3/3 | ✅ |
| 6 启动 fail-fast | 5/5 | ✅ |
| 7 第三方内容可见性 | 3/3 | ✅ |
| 8 短命令被内置占用 | 3/3 | ✅ |
| 9 运行中的输入反馈 | — | ⏸ P1a 结构上驱动不了（见文末） |
| 10 Plan Mode 交互 | 3/3 | ✅ |
| 11 目录型 Skill 能力包 | 3/3 | ✅ |

**已跑 10 条 / 共 43 项判据全部通过**，未发现需要修改的产品缺陷。
顺带产出三条别的结论：两处 checklist 措辞过时（已改）、一处已知工程项的
真实观测样本、一处 P1a 自身的能力缺口（推给 P1b）。

---

## 场景 1：共享模式全流程

预置：`tests.e2e.c11_scenarios:seed_commit_repo` —— 一个含 4 条提交历史的真实 git 仓库，
历史风格刻意选成**一眼可辨、且模型不会自发选用**的样子（`类型(中文范围): 中文描述`），
其上再造出未提交改动（改 2 个已跟踪文件 + 1 个未跟踪新文件）。
这样「模型有没有真的读 `git log` 学风格」才可证伪。

### ① `/skills` 看到三个内置样板

**机器判到了什么**（`ui_message` seq=24）：

```
Skill 状态

- commit（内置 · 共享）：按项目约定生成提交信息并提交
    短命令 /commit
- review（内置 · 独立）：审查当前改动并只回流结论
    短命令 /review
- test（内置 · 共享）：跑项目测试并解读失败
    短命令 /test
```

**判断**：三个样板俱全，来源标为「内置」，模式（共享/独立）正确，短命令均已注册。通过。

### ② `/commit 修复登录超时` —— Skill 指令生效

**机器判到了什么**：本次 Agent Loop 的 7 次工具调用，按发生顺序（取自 `tool_execute`）：

| seq | 调用 | 对应 SOP 步骤 |
| --- | --- | --- |
| 39 | `run_command git status` | §1 看有哪些文件变动 |
| 43 | `run_command git diff` | §1 看已跟踪文件的具体改动 |
| 47 | `run_command git log --oneline -15` | §2 **学习本仓库的提交风格**（命令与 SOP 逐字一致） |
| 57 | `read_file app/session.py` | §1 未跟踪文件 `git diff` 看不到，改用读文件 |
| 68 | `run_command git add app/auth.py app/config.py app/session.py` | §4 **只提交相关文件、没有 `git add -A`** |
| 78 | `run_command git diff --cached` | §4 提交前再确认一次暂存区 |
| 89 | `run_command git commit -m "…"` | §4 提交；**全程没有 push** |

提交信息原文：

```
fix(登录): 修复登录超时过短问题

- 将 LOGIN_TIMEOUT 从 30 秒调整为 120 秒，避免弱网下用户填表单被提前踢掉
- 新增 LOGIN_RENEW_WINDOW = 20 秒，会话到期前允许自动续期
- 新增 should_renew() 判断逻辑及 Session 管理类，实现到期前自动续期
```

**判断**：SOP 的五个步骤**逐条命中且顺序正确**，没有一步是「顺手做了」的巧合：

- `git log --oneline -15` 的 `-15` 与 SOP 写的一模一样——模型不会平白无故选这个数字；
- 提交信息同时具备**两个**预置风格特征（conventional 前缀 + **中文范围名**），
  两者叠加几乎不可能是巧合，可判定它确实照抄了 `git log` 里看到的风格；
- 正文分点写的全是「**为什么**」（弱网下被提前踢掉）而不是「是什么」，正合 SOP §3；
- `git add` 精确点名三个文件而非 `-A`，正合 SOP §4 的明确禁止项。

**另有一处 SOP §0/§5 的旁证**：`app/session.py` 只被 `read_file` 读、没有被改；
最终回复里如实指出了「`.rhinecode_debug.log` 未跟踪、未纳入本次提交，建议加进
`.gitignore`」——**指出问题但没动手改**，正是 SOP §0「只提交，不改代码」+
§5「如实列出可疑之处」要求的行为。通过。

### ③ 状态栏出现 Skill 标记

**机器判到了什么**（`skill_state` seq=28 / `status_bar` seq=99）：

```
28  skill_state  激活 commit · 当前 ['commit']
99  status_bar   …| 权限模式：默认 | 上下文：0% · 4.8K/976.6K | Skill:1
```

**判断**：激活事件与状态栏 `Skill:1` 段同时出现，与 C11 设计一致。通过。

### ④ 提交成功

**机器判到了什么**（跑完后直接对预置仓库执行 `git log --oneline`）：

```
d0c7159 fix(登录): 将超时从 30 秒放宽至 120 秒并支持到期前自动续期   ← 本次
1d3bfb1 docs(说明): 补充本地启动步骤
4ef2353 fix(连接池): 修正空闲连接未被回收的问题
216bdf8 feat(登录): 增加账号密码登录接口
f2261a1 chore(初始化): 搭起服务骨架
```

`git status --short` 只剩 `?? .rhinecode_debug.log`。

**判断**：提交真的落盘了，且新提交与既有四条排在一起风格连贯。通过。

### ⑤ 追问「再改一下提交信息」——**证明常驻**

这是本场景最关键的一条：Skill 激活后应当**留在动态槽位里逐轮重建**，
而不是只在触发那一轮生效。

**机器判到了什么**（三条独立证据）：

1. 追问那一轮的 `api_request`（seq=104）里，**末条 system 消息仍完整携带 commit 的
   SOP 正文**，含 `===== Skill 指令开始：commit =====` 与 `===== Skill 指令结束：commit =====`
   两条围栏（该段全长 1303 字符，SOP 五节一节不缺）；
2. 同一条 `api_request` 的 `tool_names = ["read_file","run_command","grep_content","load_skill"]`
   —— **仍是 commit 声明的白名单**（`run_command`/`read_file`/`grep_content`）加系统级
   `load_skill`，而不是全量 7 个工具；
3. 模型的实际行为：先 `git log -1 --format="%H"` 确认要改哪条，再
   `git commit --amend -m "fix(登录): 将超时从 30 秒放宽至 120 秒并支持到期前自动续期\n\n…"`
   ——**新标题仍保持 `fix(中文范围):` 前缀 + 中文 + 多行正文**，且仍未 push。

**判断**：第 1、2 条是机器可复核的硬证据（注入与收窄都还在），第 3 条是行为层面的
印证。三者一致，可判定「Skill 常驻、跨轮生效」成立。通过。

### ⑥ `/skills off commit` —— 标记消失

**机器判到了什么**（seq=135–137）：

```
135  skill_state  卸载 commit（移除 1）· 当前 []
136  ui_message   已卸载 Skill `commit`。
137  status_bar   …| 权限模式：默认 | 上下文：1% · 5.4K/976.6K      ← 已无 Skill 段
```

**判断**：卸载后状态栏**整段消失**（不是显示 `Skill:0`），与 C11「None 即隐藏」的
设计一致。通过。

### ⑦ 人在回路未被 Skill 绕过（附带确认的安全判据）

**机器判到了什么**：本场景 7 次工具调用里的 6 次 `run_command` **每一次都弹了确认面板**，
面板原文一律是 `⚠ 确认执行：run_command(command=…) · 默认模式：无规则命中，交由用户确认`。

**判断**：Skill 的 SOP 正文里写满了「执行 `git status`」这类祈使句，
但它一次也没能跳过第⑤层人在回路——与安全边界「Skill 无权限豁免」一致。通过。

---

## 场景 3：模型自主两阶段加载

预置：同场景 1 的 `seed_commit_repo`（另起一个宿主，仓库是全新的、改动未提交）。
**全程不用短命令**，只发一句自然语言：「帮我按项目规范提交这些改动」。

### ① 第一阶段清单确实只有「名字 + 一句话」

**机器判到了什么**（第 1 轮 `api_request` 的稳定 `system` 段末尾，全段长 1938 字符）：

```
以下 Skill 可用。共享模式的 Skill 可以用 `load_skill` 工具加载其完整指令；
独立模式的 Skill 需要由用户触发，你只能建议用户执行对应命令，不能自行加载。

- commit（共享）：按项目约定生成提交信息并提交
- review（独立）：审查当前改动并只回流结论
- test（共享）：跑项目测试并解读失败
```

**判断**：整个稳定系统提示才 1938 字符，而单份 commit SOP 正文就有约 1300 字符
——清单里**装不下**任何一份正文，两阶段加载的第一阶段成立。
另外清单还正确区分了共享/独立：独立模式明说「你只能建议用户执行」。通过。

### ② 模型自己调用了加载工具

**机器判到了什么**：第 1–2 轮它先自行探路（`git status --short`、`git diff --stat`），
第 2 轮末尾调用了 `load_skill`：

```
29  skill_state   activate · skill=commit · active=['commit']
31  tool_execute  load_skill  arguments={'name': 'commit'}  ok=True  duration=15ms
```

**判断**：没有任何短命令、没有任何提示词点名 Skill，模型是**读了清单后自己决定**
去加载的。这正是两阶段加载想要的形态。通过。

### ③ 工具行显示简短确认，而非整段 SOP

**机器判到了什么**（`tool_execute` seq=31 的两个展示字段）：

```
summary = 激活 commit
output  = 已激活 Skill `commit`，其完整指令已注入你的上下文，请按其步骤执行。
```

**判断**：回灌给模型与显示在界面上的都是这一句话（38 字），**整段 SOP 一个字都没有
出现在工具结果里**——它走的是动态提示槽位而不是工具返回值。这正是 C11「成功时不回灌
正文」的设计。通过。

### ④ **下一轮**开始按 SOP 行事（跨轮生效）

这条是场景 3 的核心。逐轮列出「本轮给了哪些工具」与「本轮的动态提醒里有没有 SOP」：

| 轮次 | reminder 含 SOP | 本轮工具集 |
| --- | --- | --- |
| 1 | 否 | read_file, write_file, edit_file, run_command, glob_files, grep_content, load_skill |
| 2 | 否 | 同上（7 个）← **`load_skill` 就是在这一轮被调用的** |
| 3 | **是** | read_file, run_command, grep_content, load_skill ← **收窄** |
| 4–7 | 是 | 同第 3 轮 |

**判断**：分界线**精确地落在第 2 轮与第 3 轮之间**——第 N 轮激活、第 N+1 轮生效，
与 C11 把 `dynamic` 改成「每轮求值一次的可调用对象」所要达成的效果完全一致。
注入与收窄**同步切换**，说明两者共用同一次每轮重建。通过。

### ⑤ 第 3 轮起的实际行为确实照着 SOP 走

**机器判到了什么**（第 3 轮起的工具调用序列）：

```
43  git diff                     ← SOP §1
47  git status                   ← SOP §1
58  git log --oneline -15        ← SOP §2（命令与 SOP 逐字一致）
69  git add app/auth.py app/config.py && git status   ← SOP §4（点名文件 + 再确认暂存区）
80  git commit -m "feat(续期): 增加登录会话自动续期机制\n\n- …"
```

**判断**：提交信息再次同时具备预置风格的两个特征（conventional 前缀 + **中文范围名**），
正文分点写「为什么」，未 push。SOP 生效成立。通过。

> **一处如实记录的模型判断（不是缺陷）**：本次它**没有**提交未跟踪的新文件
> `app/session.py`，并在最终回复里说明了理由——「未被任何代码引用或导入，
> 可能是半成品，如需使用还要补 import 与集成逻辑」。这是 SOP §4「只提交与本次改动
> 相关的文件」与 §5「指出问题但不动手改」之下的一次**模型自主判断**，可以争论对错
> （场景 1 的同一份预置下它选择了提交），但它不属于 C11 的功能判据：
> Skill 系统的职责是把 SOP 送到模型面前，而不是替模型做取舍。

---

## 场景 4：白名单收窄可观测

预置：`tests.e2e.c11_scenarios:seed_whitelist_pair` —— 一对形成对照的 Skill
加一个可供审阅的小项目：

- `audit`（**项目级**，`allowed_tools: [read_file, glob_files]`）——验「收窄真的发生」；
- `freeform`（**用户级**，**不声明** `allowed_tools`）——验「任一 Skill 未声明则整体
  塌缩为不收窄」。

两个层级各放一个，顺带覆盖三级存放里的项目级与用户级。

### ① 两层来源与第三方内容提示

**机器判到了什么**（`/skills` 的 `ui_message`，以及启动时的 `skill_state`）：

```
- audit（项目级 · 共享）：只读审阅代码，产出审阅意见但不改任何文件      短命令 /audit
- commit（内置 · 共享）…  - freeform（用户级 · 共享）…  - review（内置 · 独立）…  - test（内置 · 共享）…

发现 1 个项目级 Skill（来自 …\.rhinecode\skills）：audit
它们来自当前代码仓库，其指令可以指挥模型读写文件与执行命令，请确认它们可信。
```

启动快照 `skill_state(action=bind_tools)` 的 `allowed_tools` 映射里有
`audit: [read_file, glob_files]`，而 **`freeform` 这个键根本不存在**
（未声明白名单的 Skill 不进映射）。

**判断**：来源层级标注正确，第三方内容提示按设计出现。通过。

### ② 激活窄白名单 Skill 后，`/skills prompt` 显示工具集已收窄

**机器判到了什么**（`/audit 审阅一下 app 目录下的代码` 之后的 `/skills prompt` 输出结尾）：

```
【当前可见工具集】

glob_files、load_skill、read_file
（Plan Mode 的规划阶段还会另外附加 ask_user / present_plan，它们不受白名单约束）
```

同期 `api_request` 的 `tool_names = ["read_file","glob_files","load_skill"]`，
状态栏 `Skill:1`。

**判断**：`/skills prompt` 报的可见工具集与**实际发出去的请求**逐个一致
——这正是这条命令存在的意义（排查「为什么模型没按我的 Skill 做」时，
报告与事实不符是最坏的情况）。7 个工具收窄到 2 个 + 系统级 `load_skill`。通过。

### ③ 让模型写文件 —— **真实模型自发地越界了，并被拦下**

**机器判到了什么**（发出「把你刚才的审阅意见写进 notes.md 文件里」之后）：

```
seq=56  api_request   turn=4  tool_names=['read_file','glob_files','load_skill']
seq=61  tool_execute  write_file · ok=False · outcome=out_of_scope · duration=0ms
        summary = 不在当前工具集内
        output  = [工具不可用] write_file 不在当前 Skill 声明的工具集内，本轮未提供给你，
                  因此没有执行。
                  当前可用工具：glob_files、load_skill、read_file。
                  请改用其中之一；若确实必须用 write_file，请说明理由让用户决定。
```

`notes.md` 事后仍是预置的 46 字节原文，**一个字都没变**。

**判断**：这条值得单独说。P0 的场景 2 用**脚本化假模型**确定性地复现了「模型调用了
本轮没发给它的工具」——那是整个 trace 项目的立项动因。这一次，**真实模型在没有任何
诱导的情况下自发做了同一件事**：它手里明明只有三个工具的 schema，却凭空发出了一个
带 2900 字符 `content` 的 `write_file` 调用。

两件事因此同时得到确认：① 这个幻觉在真实模型上**确实会发生**，不是假模型编出来的
稻草人；② 产品的兜底**真的兜住了**——`duration=0ms`、`ok=False`、文件未被改，
而且回灌给模型的不是一句「未知工具」，是一段能让它自救的说明。通过。

### ④ 模型据此如实说明并给出替代方案

**机器判到了什么**（下一轮的 assistant 正文）：

```
当前 audit Skill 只开放了只读工具，不能直接写文件。不过我可以切换到 freeform 模式
来完成写入，或者你也可以直接复制下面的内容创建 `notes.md`。
…
建议选方案一，你输入 `skill freeform 把审阅意见写入 notes.md` 即可。
```

**判断**：checklist 写的是「因看不到写工具而**改用其它方式或如实说明**」。
模型两样都做了：如实说明了限制的来源（点名了 audit Skill），并给出两条可行路径。
回灌文案设计得当——它没有把这当成故障，而是当成一条约束。通过。

### ⑤ 再激活未声明白名单的 Skill → 工具集恢复全量

**机器判到了什么**（`/freeform 把审阅意见写进 notes.md` 之后）：

```
seq=76  skill_state   activate freeform · active=['audit','freeform']
seq=78  api_request   turn=6  tool_names=['read_file','write_file','edit_file',
                                          'run_command','glob_files','grep_content','load_skill']
seq=85  status_bar    … | Skill:2
seq=97  tool_execute  write_file · ok=True · outcome=executed
```

随后 `/skills prompt` 的工具集一栏变成：

```
【当前可见工具集】

未收窄（全部已注册工具对模型可见）
```

`notes.md` 从 46 字节变成 2967 字节。

**判断**：`audit` **仍然激活着**（`active=['audit','freeform']`、状态栏 `Skill:2`），
但只要有一个 Skill 未声明白名单，收窄就**整体塌缩**——这正是 C11 的既定语义，
而且是在**激活后的第一轮请求**（turn 6）就生效的，没有滞后一轮。
`/skills prompt` 的措辞也随之从工具名列表换成「未收窄」这句明确的话。通过。

### ⑥ 恢复全量后写文件仍走确认面板

**机器判到了什么**：`write_file` 执行前弹出了
`⚠ 确认执行：write_file(path=notes.md, content=…) · 默认模式：无规则命中，交由用户确认`，
应答放行后才 `ok=True`。

**判断**：白名单放宽只影响「模型看得见什么」，不影响「允许做什么」——
与安全边界里写的「`allowed_tools` 不是安全边界」完全一致。通过。

> **顺带观察到的一处幂等行为**（不是判据，记录备查）：`/freeform` 短命令激活之后，
> 模型自己又调了一次 `load_skill('freeform')`（seq=86）。记录显示第二次
> `skill_state` 的 `active` 顺序不变、仍是 `['audit','freeform']`，
> 与「重复激活幂等：位置不动、只更新参数」的设计一致。

---

## 场景 2：独立模式隔离

预置：`seed_commit_repo`（有真实未提交改动的 git 仓库），跑内置的 `/review`。

### ① 子对话的工具调用逐个出现在界面（进度可见）

**机器判到了什么**：整场 6 轮、14 次工具执行，全部落在 `isolated:review` 作用域，
且每一步都产出了界面消息：

```
 7  isolated:review  agent_event   progress · iteration=1
15  isolated:review  ui_message    [assistant] 我先获取工作区的改动范围。
16  isolated:review  tool_execute  run_command · executed · ok=True · 退出码 0 · 输出 38 行
23  isolated:review  ui_message    [system] 🔄 第 2 轮
30  isolated:review  ui_message    [assistant] 有两个文件的未暂存改动，我来逐一读取完整上下文。
…
60  isolated:review  context_compaction  第一层存盘 3 个
62  isolated:review  ui_message    [system] 📦 已把 3 个大型工具结果存盘，历史仅保留预览与路径。
```

**判断**：子对话的每一轮推进、每一段推理正文、每一次工具调用都渲染到了界面上
——「进度可见」成立，不是跑完才吐一坨。顺带还验到了 spec 里那条
「子对话参与 C8 第一层、不参与第二层」：seq=60 的存盘发生在 `isolated:review`
作用域内，全场没有任何 `layer=summary` 的记录。通过。

### ② 主作用域干净

**机器判到了什么**：`main` 作用域在这一整场里只有 6 条事件——
`user_input` / `ui_message(user_echo)` / `command_dispatch` 三条在前，
`agent_event(finished)` / `ui_message(assistant 结论)` / `status_bar` 三条在后。
**`main` 里的 `api_request` 与 `tool_execute` 均为 0 条。**

**判断**：子对话完全不污染主作用域。通过。

### ③ `/context` 没被撑大

**机器判到了什么**：子对话读了 2 个文件、跑了 4 次 grep、2 次 glob、2 次 git 命令，
其中 3 个结果大到触发了第一层存盘。跑完之后：

```
📊 上下文用量（近似估算）
  估算 token：606
  窗口上限：1000000
  已存盘工具结果：3 个
```

**判断**：**606 token**——主上下文里只有那两条配对消息的体量，
十几次工具往返一个字都没留下。这正是独立模式存在的理由。通过。

### ④ 主历史恰好是那对配对消息

**机器判到了什么**（直接读会话存档 JSONL，共 2 行）：

```
role=user       display_content=/review 审阅当前工作区的改动
                content=执行 Skill /review（审查当前改动并只回流结论） 参数：审阅当前工作区的改动
role=assistant  content=（1752 字符的审查结论）
```

**判断**：**恰好两条**，且 user 那条是「界面显示原始输入 / 模型历史存自包含文本」
的双内容形态。通过。

### ⑤ `/resume` 回放确认

**机器判到了什么**：`/clear` 开新档后 `/resume`，面板列出

```
1. 20260728-062755-n846 · 2026-07-28 06:28 · 2 条 · [dim]/review 审阅当前工作区的改动[/dim]
```

选中载入后：

```
112  skill_state       清空激活态（移除 0）
113  history_restored  resume_command · 2 条 · session 20260728-062755-n846
```

**判断**：存档里就是 2 条，标题取的是 `display_content`（用户原始输入）而非
自包含文本。另外顺带看到 `/resume` 成功分支确实清空了激活态（C11 的 N4 要求）。通过。

---

## 场景 5：热更新

在一个已经跑起来的宿主里做三步文件操作，**全程不重启**。

### ① 新建文件 → reload → 立即可用

**机器判到了什么**：

```
（写入 .rhinecode/skills/greet.md 之前）
/greet 你好   →  未知命令：/greet。输入 /help 查看可用命令。      ← 先证明它本来不存在

（写入之后）
/skills reload  →  Skill 定义已重新加载。
                   新增：greet
/help           →  /greet — 打个招呼（热更新演示）
                        类型：提示词 · 用法：/greet [参数] · 参数：[参数]
/greet 介绍一下你自己  →  【甲】我是 Rhine，一个运行在终端里的 AI 编程助手…
```

**判断**：先跑一次「还不存在」再跑「已存在」，避免把「本来就有」误当成「热更新生效」。
短命令进了注册表（`/help` 与 Tab 补全读的是同一份注册表；Tab 的按键路径本身有
`tests/test_skill_tui.py` 的自动化护栏钉着）。执行结果带 `【甲】` 标记，
说明正文确实被注入了。通过。

> 正文里那句「回复必须以 `【甲】` 开头」是刻意设计的**机械判据**：
> 让「行为有没有随正文改变」变成一个字符串比对，而不是去品「回答风格好像变了」。

### ② 改正文 → reload → 已激活的正文换新，行为随之改变

**机器判到了什么**（把标记从 `【甲】` 改成 `【乙】` 后 reload）：

```
/skills reload  →  Skill 定义已重新加载。
                   （Skill 列表无变化；已激活 Skill 的正文按最新定义生效）

/skills prompt  →  ===== Skill 指令开始：greet =====
                   回复时**必须**以 `【乙】` 这四个字符开头，然后再写正文。
                   不要调用任何工具，直接回答。

                   介绍一下你自己          ← 沿用了**上一次**的参数
                   ===== Skill 指令结束：greet =====

（随后发一条普通消息）再说一句  →  【乙】好的，有什么需要帮忙的随时说。
```

**判断**：三件事同时成立——注入正文换成了新版、`$ARGUMENTS` 沿用最后一次的参数、
模型的实际输出从 `【甲】` 变成 `【乙】`。这条同时覆盖了 checklist 第七节
「沿用最后一次的参数完成占位符替换」那一项。通过。

### ③ 删除文件 → reload → 自动卸载并提示

**机器判到了什么**：

```
278  skill_state  reload · added=[] · removed=['greet'] · auto_deactivated=['greet'] · active=[]
279  ui_message   Skill 定义已重新加载。
                  移除：greet
                  已自动卸载（定义已消失）：greet
280  status_bar   … | 权限模式：默认                              ← Skill 段消失
282  /greet 还在吗  →  未知命令：/greet。                          ← 短命令一并注销
```

**判断**：四样都对——移除、自动卸载、状态栏收段、短命令注销。
最后一条尤其值得留意：短命令的注销证明 `replace_skill_commands` 是**整体替换**
而不是只增不减。通过。

---

## 场景 10：Plan Mode 与独立模式的交互

> ⚠️ **这条场景不能用内置的 `review` 跑**（实测踩过，checklist 已据此加注）。
> `review` 是纯分析任务，模型看完 diff 直接给结论，**根本没有「计划」可提**，
> 于是审批面板一次都不弹。这不是产品的问题，是场景设计的问题——
> Plan Mode 的「先规划、再批准、后执行」只有在任务确实有副作用时才有意义。
> 故新增 `seed_plan_skill`：一个**必须动手改代码**的独立模式 Skill。

### ① 子对话先给出计划并弹审批面板

**机器判到了什么**：

```
309  isolated:fixit  progress · iteration=1
316  isolated:fixit  tool_execute  read_file · ok=True          ← 先调研
322  isolated:fixit  api_response  turn 2 · 文件很短，结构清晰。下面是执行计划。
323  isolated:fixit  agent_event   tool_start · tool_name=present_plan
面板：📋 计划已就绪，是否开始执行？   选项 yes / no
```

**判断**：子对话继承了主对话的 Plan Mode（面板确实弹在 `isolated:fixit` 这一轮里），
调研在前、`present_plan` 在后。通过。

### ② 拒绝 → 主历史得到「计划未获批准」而非半截结论

**机器判到了什么**（记录 + 会话存档双证）：

```
325  isolated:fixit  interaction   approve → False · ## 调研结论 …
327  main            agent_event   notice · 计划未获批准，本次 Skill 未执行。
328  main            ui_message    计划未获批准，本次 Skill 未执行。

会话存档末两行：
  role=user       display=/fixit 给 app/session.py 的 Session 类补一个 reset() 方法…
  role=assistant  content=计划未获批准，本次 Skill 未执行。
```

**判断**：这一条是本场景的核心。子对话里模型已经写出了一大段「调研结论 + 计划」，
界面上也显示了（seq=330），**但回流进主历史的不是那段文字，而是那句哨兵**。
如果实现偷懒回流「最后一条非空 assistant 正文」，主历史里就会躺着一段
看起来像结论、其实计划根本没被批准的文字——那是最坏的形态，因为它在
`/resume` 之后完全看不出来。配对结构仍成立（恰好两条）。通过。

### ③ 再跑一次并批准 → 正常执行并回流结论

**机器判到了什么**：

```
面板 approve → yes
面板 confirm → ⚠ 确认执行：edit_file(path=app/session.py, old_string=…, new_string=…)
会话存档 assistant：**改动摘要**：在 `app/session.py:11-13` 为 `Session` 类新增了
                    `reset()` 方法，将 `self.remaining` 重置为初始值 `30`…
```

文件确实被改了：

```python
    def reset(self) -> None:
        """将会话剩余时间重置为初始值。"""
        self.remaining = 30
```

**判断**：获批后进入执行阶段，写工具照常走第⑤层确认，结论正常回流，改动真的落盘。通过。

---

## 场景 11：目录型 Skill 能力包

预置：`seed_capability_pack` —— 在**用户级**目录放一个目录型 Skill：

```
<user_dir>/skills/naming/
    SKILL.md      ← 入口。正文刻意**不写规则本身**，只说「去读 reference.md」
    reference.md  ← 规则的唯一出处
    template.py   ← 模板，只为让随附清单不止一条
```

规则刻意做成**模型不可能猜到**的样子（`rc_` 小写前缀 + 主体全大写 + `_V2` 后缀）。
模型若答对了，就只可能是真读到了那份文档——这是这条场景可证伪的关键。

### ① 资源目录绝对路径与随附文件清单确实注入了

**机器判到了什么**（`/skills prompt` 的激活正文段末尾）：

```
### 本 Skill 的随附资源

资源目录：`C:\…\rhine_e2e_user_dww24g0y\skills\naming`

可用文件（相对资源目录）：
- reference.md
- template.py

注意：该目录在项目工作区之外，**不支持 glob/grep 枚举**，请按上述清单用绝对路径直接 read_file 读取。
```

**判断**：绝对路径、文件清单、以及「为什么不能 glob」的说明三样俱全。
最后那句尤其重要——没有它，模型会先去 glob 一遍、失败、再猜，白烧轮次
（CLAUDE.md 里记着的那条实测教训的同型问题）。通过。

### ② 模型按清单读到了工作区外的文档，并据以作答

**机器判到了什么**（两次 `read_file` 的参数，都是工作区外的绝对路径）：

```
{'path': 'C:\…\rhine_e2e_user_dww24g0y\skills\naming\reference.md'}
{'path': 'C:\…\rhine_e2e_user_dww24g0y\skills\naming\template.py'}
```

最终回答：

```
根据 `reference.md` 的命名约定（v2），模块级常量的命名必须满足三条：
1. 以 `rc_` 前缀开头
2. 主体部分全部大写，词间用下划线分隔
3. 以 `_V2` 结尾

「最大重试次数」对应的英文是 `MAX_RETRY_COUNT`。
组合起来，名字应为：  rc_MAX_RETRY_COUNT_V2

**依据**：`reference.md` 第 5-7 行的三条规则，以及第 9 行的示例 `rc_RETRY_COUNT_V2`。
```

**判断**：`rc_MAX_RETRY_COUNT_V2` 三条规则全中，还引了行号。这个名字的形态
（小写前缀 + 大写主体 + 版本后缀）**违反一切通行的 Python 命名习惯**，
不可能是模型的先验偏好，只能来自那份文档。**工作区外目录的只读放行确实生效。**通过。

### ③ 只读放行的边界没有被放宽（**反证**）

上一条只证明了「能读到」，没证明「没读多」。所以补一条反证：
让模型去读**同一个 `user_dir` 下、但在 `skills/` 之外**的一个文件。

**机器判到了什么**：

```
permission_decision  read_file → deny（sandbox）
                     路径越界，超出项目工作目录：C:\…\rhine_e2e_user_dww24g0y\.rhine-e2e-workspace
tool_execute         read_file · denied_by_permission · ok=False
```

**判断**：白名单放行的是 **Skill 资源目录**，不是整个用户目录——
差一层目录就被第②层沙箱拦下。与安全边界所述「只对 read 类判定生效、
不扩大沙箱其它面」一致。通过。

---

## 场景 6：启动 fail-fast

预置一对**只差一个字母**的孪生工作区，唯一变量就是那个笔误：

- `seed_typo_whitelist` —— `allowed_tools: [read_files, glob_files]`（真名是 `read_file`）；
- `seed_fixed_whitelist` —— `allowed_tools: [read_file, glob_files]`。

两者都同场配了一个 stdio MCP Server（`tests/fixtures/mock_mcp_server.py`，本地脚本、
不联网），这是验第二件事「失败退出时无孤儿子进程」的必要条件。

### ① 装配终止且退出码非零

**机器判到了什么**：宿主进入 `fatal` 态，进程最终以**退出码 1** 结束。

**判断**：与「以非成功状态终止」一致。通过。

### ② 错误信息指明了哪个文件、哪个工具名

**机器判到了什么**（经控制通道回报的完整文案，逐字）：

```
启动失败：Skill 的 allowed_tools 白名单中存在不存在的工具名。

  Skill `broken`（C:\…\rhine_e2e_ws_ky0n4961\.rhinecode\skills\broken.md）
    未知工具名：read_files

请检查上述工具名是否拼写正确。
若确认该工具名无误，可能是 RhineCode 版本与该 Skill 不匹配（该 Skill 是为其它版本编写的），
请升级 RhineCode 或删除该白名单项。
```

**判断**：三样都在——**Skill 名 + 文件绝对路径 + 具体工具名**，外加版本错配的提示。
拿着这段文案能直接去改那一行，不需要再猜。通过。

### ③ 终止发生在任何 MCP 子进程创建之前

**机器判到了什么**：fatal 态期间按命令行匹配 `mock_mcp_server.py` 的进程数 = **0**。

**判断**：与「校验窗口夹在 `MCPAddServerTool` 注册之后、`connect_all` 之前」一致。通过。

### ④ 这个「0」不是空洞的（**反证**）

单看上一条其实证明不了什么——万一 MCP 配置压根没生效，那 0 只是因为它从来就没起过。
所以必须有对照组。

**机器判到了什么**（把 `read_files` 改成 `read_file`，其余一字不改）：

```
state = idle                                  ← 正常启动
tool_names 含 mcp__mock__echo / mcp__mock__boom  ← 远端工具已注册
/mcp  →  mock (stdio) ✓ 已连接 · 2 工具
匹配 mock_mcp_server.py 的进程数 = 2          ← 这次真的起来了
```

**判断**：同一份 `mcp.yaml`，成功路径下子进程**确实会被拉起**。
因此失败路径下的 0 是「没走到那一步」，而不是「那一步本来就不存在」。
这条反证让 ③ 从「看起来对」变成「确实成立」。通过。

### ⑤ 正常退出后子进程被回收

**机器判到了什么**：对照组 `quit` 之后，匹配进程数回到 **0**，临时目录零残留。

**判断**：`cleanup` 的 `mcp_manager.close_all()` 生效。通过。

---

## 场景 7：第三方内容可见性

预置：`seed_thirdparty_repo` —— 一个「你刚 clone 下来的别人的仓库」，
带**两个**项目级 Skill（`context` 与 `houserules`）。
刻意放两个而不是一个：N=1 时「发现 N 个」里的 N 对不对根本看不出来。

> ⚠️ **checklist 这条的措辞已过时**（本次验收顺带发现，已改）。它写的是
> 「启动 → 观察到『发现项目级 Skill』提示」，而实现**有意把这条提示从启动打印
> 挪进了 `/skills` 报告**。`bootstrap.py` 那段注释写明了理由：启动阶段的 `print`
> 发生在 Textual 接管屏幕之前，会被 alternate screen 整个盖住，用户要等到退出程序
> 才在终端里看见——**那时早已失去意义**。挪进 `/skills` 之后零状态语义不变。
> 这是实现比 checklist 更对的一处，改的是 checklist。

### ① 提示出现，且计数正确

**机器判到了什么**（`/skills` 报告结尾）：

```
发现 2 个项目级 Skill（来自 C:\…\rhine_e2e_ws_f9_84641\.rhinecode\skills）：context、houserules
它们来自当前代码仓库，其指令可以指挥模型读写文件与执行命令，请确认它们可信。
```

**判断**：计数、名字、目录绝对路径三样都对，且把「可以指挥模型读写文件与执行命令」
这个风险点说明白了。通过。

### ② 退出重进，提示仍在

**机器判到了什么**：完整退出宿主后重新起一个（新工作区 `…_2pdrm1t6`），
`/skills` 报告里同一段提示**逐字再次出现**。

**判断**：通过。但单凭这条其实弱——两次都是全新的临时目录，
即使真有「已确认过就不再提示」的持久化状态，它也会随目录一起消失。所以补了下一条。

### ③ 无处可存状态（**反证**）

**机器判到了什么**：第一次运行用 `--keep-workspace` 保住了产物，退出后逐个列出
程序写过的**全部**文件：

```
工作区 .rhinecode/  →  sessions/20260728-062249-z757.jsonl
                       skills/context.md      ← 预置的
                       skills/houserules.md   ← 预置的
                       traces/host.jsonl      ← 驱动设施自己开的
用户目录            →  .rhine-e2e-workspace   ← 驱动设施的可丢弃标记
```

**判断**：整个运行期间程序只写了会话存档一个文件，**没有任何一处可以承载
「这个仓库的 Skill 我已经确认过了」这种状态**。结合 ②，可判定零状态语义确实成立
——不是「状态恰好没生效」，而是**根本不存在这个状态**。
与 `project_skill_notice` 的 docstring 所述一致：有状态的话新增 Skill 时状态不失效，
新来的那几个就被静默吞掉了。通过。

---

## 场景 8：边界——短命令被内置占用

预置同场景 7（`context` 这个 Skill 的名字与内置命令 `/context` 重名）。

> ⚠️ **checklist 这条的措辞同样过时**（已改）：原文写「启动有警告」，
> 而实现把「短命令冲突」与上面那条提示一并收进了 `/skills` 报告，理由相同。
> `tests/test_skill_startup.py::test_skill_colliding_with_builtin_command_is_skipped`
> 已经把这个口径钉死（断言 `/skills` 报告里含 `/skills run clear`）。

### ① 短命令未注册，且给出了**正确的**替代入口

**机器判到了什么**（`/skills` 列表，注意两行的差别）：

```
- context（项目级 · 共享）：名字故意与内置命令重名
    需用 /skills run context          ← 没有短命令，直接给替代入口
- houserules（项目级 · 共享）：本仓库的编码约定
    短命令 /houserules                ← 正常注册
```

**判断**：重名那条没有注册短命令，且提示的是**一条真实存在的命令**。
这正是 `has_short_command` 必须只查 `_skill_specs` 的原因——用 `resolve` 的话
重名时会命中内置命令、于是把入口提示指向一条存在但错误的命令。通过。

### ② 内置 `/context` 行为不变

**机器判到了什么**：

```
📊 上下文用量（近似估算）
  估算 token：0
  窗口上限：1000000
  距上限余量：1000000
  已存盘工具结果：0 个
```

**判断**：仍是上下文报告，不是那个 Skill。通过。

### ③ 该 Skill 仍完全可用

**机器判到了什么**：`/skills run context 这是参数` → 模型回复 `重名 Skill 已执行`
（该 Skill 的正文要求「只回复这一行」）。

**判断**：短命令被占用**不等于** Skill 不可用，通用入口跑得通。通过。

---

## 未跑的一条：场景 9（运行中的输入反馈）

**原因是 P1a 自身的结构限制，不是产品问题。**

这条场景要验的是「Agent 循环运行期间 / 确认面板挂着时提交输入 → 出现可见提示
而非毫无反应」。可 `DriverCore.send` 的**前置检查就写死了「只有 idle 时才能提交」**：

```python
if state != SessionState.IDLE.value:
    return protocol.err("busy", f"会话当前处于 {state} 态，只有 idle 时才能提交", …)
```

于是驱动器在忙碌态下**根本递不进去**那次提交，产品侧的守卫自然也就无从触发
——被拦下的是驱动器自己的守卫，不是被测的那个。控制通道也没有通用的按键注入
指令（只有 `answer --via keys`，且它只按上下键与回车），所以绕不过去。

**处置**：
- 该行为**已有自动化护栏**：`tests/test_skill_tui.py` 用 Pilot 直接驱动键盘，
  覆盖了「流式期间提交有可见提示」「确认面板挂着时提交提示『正在等待你的确认』」
  「同一次忙碌期只提示一次」三条；
- **给 P1b 记一笔能力缺口**：控制通道可以加一条「强制提交」（绕过 idle 前置检查，
  专用于触发产品侧守卫）。它是五处成对维护点的改动（`protocol` / `control` /
  `host.dispatch` / `client` / 测试），属于要动协议契约的改动，不在本轮验收里顺手做。

---

## 一处已知工程项的真实观测样本（不是新缺陷）

验场景 10 时，先用内置 `review` 试了一次（后来才换成 `fixit`），过程中撞见一件事，
值得记下来：**Plan Mode 的规划阶段，模型调用了一个本轮没发给它的非只读工具，
而它被执行了。**

```
turn 7  scope=isolated:review
        tool_names = ['read_file', 'glob_files', 'grep_content', 'ask_user', 'present_plan']
                                     ↑ run_command 不在其中（它 read_only=False，被规划阶段滤掉了）

seq=136 tool_execute  run_command · outcome=executed · ok=True
        args={'command': 'cd … && git diff -- app/auth.py'}
```

**为什么没被 `out_of_scope` 拦下**（读代码确认过，不是猜的）：两处过滤的口径不同——

- `loop._schema_for()` 算「本轮发什么 schema」时，规划阶段用
  `registry.readonly_schemas()`，非只读工具在这里被滤掉；
- `loop._execute()` 的越界判据是 `self._visible(tc.name, policy)`，
  只查 **Skill 白名单**（`review` 的白名单里恰好有 `run_command`），
  **不查规划阶段的只读过滤**。

所以这是 C11 的 `out_of_scope` 守卫**职责之外**的事，不是它漏了。

**它属于已登记的「已知后续工程项」第 2 条**——
「Plan Mode 规划阶段的工具阶段强校验，防止模型同轮夹带副作用工具」。
本次的价值是把那条从**理论隐患**变成了**有物证的真实观测**：
真实模型确实会在规划阶段夹带非只读工具，而且参数完全合法。

**风险有多大**：这次夹带的恰好是 `git diff`（无副作用），但同一条路径上完全可能是
`git checkout --` 或别的写命令。缓解在于**五层管线一层没少**：它照样在第④层判为
「问用户」并弹出了确认面板，是驱动器替人选了放行。真人在这里会看到
`⚠ 确认执行：run_command(command=…)` 并有机会拒绝。

**处置**：不在本轮改（改它要重新设计规划阶段的强校验口径，属「大改」），
只把这次观测补进已知工程项第 2 条作为佐证。

---

## 本次验收没有发现需要修改的产品缺陷

十条场景 43 项判据全部通过，没有出现「小修」或「大改」级别的产品问题。
产品代码**一行未动**；新增的只有测试设施侧的 `tests/e2e/c11_scenarios.py`
（只有预置、没有剧本）与两处 checklist 措辞订正。

三条值得单独记住的收获：

1. **场景 4 ③**：「白名单外调用」从**假模型的确定性复现**升级成了**真实模型的自发
   复现**。P0 立项时那个「模型调用了本轮没发给它的工具」的动因，至此在两种模型上
   都有了物证。
2. **场景 7 / 8**：checklist 写的「启动有提示/警告」与实现不符，而**实现是对的那一边**
   ——启动阶段的 `print` 会被 alternate screen 盖住，等于没提示。改的是 checklist。
3. **场景 10**：用内置 `review` 验 Plan Mode 是**场景设计错误**——纯分析任务没有
   「计划」可提，面板永远不弹。这类「场景本身构造不出被测状态」的失败，
   和产品坏掉长得一模一样，值得在 checklist 里写死做法（已加注）。
