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
| 3 模型自主两阶段加载 | 5/5 | ✅ |
| 4 白名单收窄可观测 | 6/6 | ✅ |

**18/18 判据全部通过。**

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

## 本次验收没有发现需要修改的产品缺陷

三条场景 18 项判据全部一次通过，没有出现「小修」或「大改」级别的问题。
新增的只有测试设施侧的 `tests/e2e/c11_scenarios.py`（预置，无剧本）。

**一处值得记在这里的收获**（不是缺陷）：场景 4 ③ 让「白名单外调用」这条
从**假模型的确定性复现**升级成了**真实模型的自发复现**。P0 立项时那个
「模型调用了本轮没发给它的工具」的动因，至此在两种模型上都有了物证。
