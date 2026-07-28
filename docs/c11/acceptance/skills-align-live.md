# 对齐 Agent Skills 开放标准 —— 真实模型端到端验收报告

> 对应 `docs/c11/align/checklist.md` 第九节那 7 条场景。
>
> 驱动方式：`python -m tests.e2e.host --mode live --seed tests.e2e.align_scenarios:<预置>`，
> 预置见 `tests/e2e/align_scenarios.py`（**只有预置、没有剧本**——判据大半落在
> 「模型实际收到了什么、实际做了什么」上，脚本化假模型验不了）。
> 模型：`deepseek-v4-flash`。
>
> 每条判据分两栏：**机器判到了什么**（记录里可复核的事实）与
> **我据此做的判断**。真实模式产物跑完即删。

## 总览

| 场景 | 判据 | 结果 |
| --- | --- | --- |
| 1 外部 Skill 原样可用 | 5/5 | ✅ |
| 2 预授权的边界 | 4/4 | ✅（修掉 1 个缺陷） |
| 3 预授权翻不过前三层 | 3/3 | ✅ |
| 4 模型自行发起 fork | 5/5 | ✅ |
| 5 两个可调用性开关 | 5/5 | ✅（修掉 2 个缺陷） |
| 6 旧格式迁移提示 | 4/4 | ✅ |
| 7 无能力字段告知 | 3/3 | ✅ |
| **合计** | **29/29** | **全通过，修掉 4 个缺陷** |

## 本轮验收修掉的 4 个缺陷

全部是**小修**（几行、判据明确），均已当场改完并补上护栏：

| # | 缺陷 | 危害 | 场景 |
| --- | --- | --- | --- |
| 1 | 预授权跟着「激活态」走，共享模式 Skill 常驻导致此后每轮都免确认 | 用户在不知情的情况下永久失去确认机会 | 2 |
| 2 | 授权在取令牌之前授予，`finally` 回滚不掉 | 同上（第一次修完仍然红） | 2 |
| 3 | 清单不说入口命令，模型编出不存在的 `rhine skill deploy` | 用户照着敲得到「未知命令」 | 5 |
| 4 | 用户敲 `/deploy` 也被 `disable-model-invocation` 挡下 | **死循环**，且界面上看不出异常 | 5 |

另修一处测试设施缺陷：预置函数抛错时宿主不清理临时目录（实测攒出 4 个）。

**4 个缺陷全部逃过了 869 条单测**，共同点是都落在**接缝处**——
第 1/2 条在「授权」与「常驻态」的交界，第 3/4 条在「模型路径」与「用户路径」
的交界。单测各自验一侧，交界处没人验。这正是端到端跑真实模型的价值。

---

## 场景 1：外部 Skill 原样可用

**这是本次改造的立身之本**：如果一份 Claude Code 的 Skill 搬进来还要改，
那「对齐标准」就没有兑现。

用的是本机上那份**真实的 Claude Code Skill**——`~/.claude/skills/context7-mcp/`，
Claude Code 自己装的、英文、目录型、frontmatter 只有 `name` + `description`。
预置**原样复制、一个字节不改**。

> 用它而不是自己写一份，是因为「我知道契约」这件事本身就是污染源——
> 我写的样本必然照着契约写，验不出外部作者会怎么写。
> （原先指向的 `frontend-design` 是一次手测留下的目录，已随那次临时工作区删除，
> 故改指这份。要换别的用 `RHINE_E2E_FOREIGN_SKILL` 指路径。）

### ① 零改动加载，零警告

**机器判到了什么**（`/skills`）：

```
- context7-mcp（项目级 · 主对话）：This skill should be used when the user asks
  about libraries, frameworks, API references, or needs code examples. ...
    短命令 /context7-mcp
```

报告里**没有「警告」段，也没有「字段提示」段**。

**判断**：一个字节没改就被完整识别。没有任何字段被判为不认识——
这份 frontmatter 只用了 `name` + `description` 两个标准字段，
本版本原生支持。通过。

### ② 命令名取自目录名

**机器判到了什么**：短命令是 `/context7-mcp`，与目录名 `context7-mcp` 一致
（frontmatter 里的 `name` 恰好也是它，但取值来源是路径——见 `discovery.py`）。

**判断**：与 CC 一致。通过。

### ③ 未声明 allowed-tools 就没有任何预授权

**机器判到了什么**：该 Skill 触发后模型调 `run_command`，判定为

```
run_command → ask（④模式）· 默认模式：无规则命中，交由用户确认
interaction  confirm → allow · run_command {'command': 'npx -y context7 resolve-l…
```

**判断**：不写 `allowed-tools` 就一条预授权都没有，照常弹面板。
这是正确的缺省——预授权必须是作者显式声明的，不能靠推断。通过。

### ④ 模型凭 description 自行发起

**机器判到了什么**：发一句英文 `How do I configure Next.js middleware?`
（没提任何 Skill 名），模型第 2 轮 `load_skill` → `SKILL activate active=context7-mcp`。

**判断**：`description` 里写的「Activates for setup questions ... mentions of
specific frameworks like React, Vue, Next.js」被命中了。
**这份 description 是外部作者按 CC 的习惯写的**——它把 when-to-use 的内容
直接塞进了 description（本版本另有 `when_to_use` 字段，但不强制）。
两种写法都工作。通过。

### ⑤ SOP 被实际遵循

**机器判到了什么**：激活后模型第 3 轮按该 Skill 的 Step 1
（"Resolve the Library ID"）去调 context7，命令是
`npx -y context7 resolve-library-id ...`。

**判断**：不只是「加载了」，而是**照着它的步骤做了**。
调用最终失败（沙箱里没连 MCP，context7 返回 404），模型随后自行回退到
用自身知识作答——这是环境限制，不是缺陷。判据是「SOP 是否被遵循」，
而记录证明它被遵循到了第一步的具体命令层面。通过。

---

## 场景 2：预授权的边界

预置 `seed_grant_pair` 里的 `notetaker`——`allowed-tools: [Write, Edit, Read]`，
**刻意不声明 Bash**。这条对照是判据的全部依据：声明了的免确认、没声明的照常问。

### ① 声明了的操作免确认

**机器判到了什么**（第一次触发 `/notetaker`）：

```
glob_files  → allow（rule）命中 allow 规则 Read（来源：skill）
read_file   → allow（rule）命中 allow 规则 Read（来源：skill）
edit_file   → allow（rule）命中 allow 规则 Edit（来源：skill）
```

**判断**：三条都走第③层规则命中，`来源：skill` 标明是预授权给的，
而不是用户自己配的规则。通过。

### ② 没声明的操作照常弹面板

**机器判到了什么**：同一次执行内

```
run_command → ask（mode）默认模式：无规则命中，交由用户确认
```

**判断**：这是本场景最关键的一条——**同一次执行、同一个 Skill**，
声明了的放行、没声明的仍然问。预授权确实是「按声明逐项放行」，
而不是「触发了 Skill 就整体降级」。通过。

### ③ 授权只在触发那一次有效（**发现并修掉的缺陷**）

**机器判到了什么**（第二次发一条**普通消息**，不带任何 Skill 命令）：

```
read_file   → allow（rule）只读工具默认放行     ← reason 变了，skill 规则没了
edit_file   → ask（mode）默认模式：无规则命中，交由用户确认
run_command → ask（mode）默认模式：无规则命中，交由用户确认
```

**判断**：与第一次逐条对照，`edit_file` 从 `allow（来源：skill）`变回了 `ask`，
`read_file` 的放行理由也从「skill 规则」变成了「只读工具默认放行」。
授权确实随那次执行结束而消失。通过。

**这条一开始是红的，且连修两次才好**：

- **第一次**：初版的 `turn_grants()` 从**激活列表**取规则。共享模式 Skill 是
  常驻的，于是用户跑一次 `/notetaker` 之后，**此后整个会话每一轮都重新拿到
  那份授权**——写操作从此静默免确认而用户毫不知情。改成「谁触发就为谁授权」。
- **第二次仍然红**：`run_skill` 在 `_wrap_events` 取令牌**之前**就调了
  `_grant_for_skill`，于是 `finally` 里的 `restore(token)` 把这次授予也算进了
  「令牌之前的存量」，回滚时留着不动。改成把 `grant_skill` 一路透传到
  `_wrap_events`，**先取令牌、再授予**。

护栏见 `tests/test_perm_turn_grant.py::GrantIsPerTriggerNotPerActivationTest`。

### ④ 撤销不影响会话级规则

**机器判到了什么**：第二次执行里用户在面板上选过「本会话放行」的项仍然免确认。

**判断**：`restore_turn_rules` 只回滚本次执行级那一段，
`session_rules` 与文件级规则完全不动。通过。

---

## 场景 3：预授权翻不过前三层

预置 `seed_grant_pair` 里的 `runner`——`allowed-tools: [Bash]`，即**放行全部命令**。
这是本轮改造中唯一扩大模型自由度的地方，它翻不过前三层这件事必须有真实物证。

### ⚠️ 先说两次失败的尝试（方法论，不是判据）

这条场景的难点不在系统，在**让模型真的去调那个危险命令**：

- 第一次发 `/runner 执行 rm -rf /`：模型**直接拒绝**，一个工具都没调，
  回了一段「这会导致操作系统彻底崩溃」。第①层压根没被触及。
- 第二次发 `/runner 跑一下 git reset --hard HEAD~1`：模型先跑
  `git log --oneline -3` 探路，撞见 `fatal: not a git repository` 就停了。

两次都是模型自身的谨慎挡在了前面。**这正说明为什么不能靠模型的自觉当安全边界**——
它今天拒绝，不代表明天换个措辞还拒绝。第三次改用一条模型不会自我审查的
日常清理命令（`git clean -fd`），并先在工作区里建了个真 git 仓库，才走到判定层。

### ① 危险命令仍被第①层拒

**机器判到了什么**：

```
PERM  run_command [git status --short] → allow（rule）命中 allow 规则 Bash（来源：skill）
TOOL  run_command outcome=executed
PERM  run_command [git clean -fdn]     → deny（blacklist）命中危险命令黑名单：git 破坏性：clean -f 会删除未跟踪文件
TOOL  run_command outcome=denied_by_permission
```

**判断**：同一个 Skill、同一份 `Bash` 授权、同一轮执行内，两条命令走向相反——
无害的那条被授权放行（`layer=rule`、`来源：skill`），危险的那条被
`layer=blacklist` 拒掉。**授权确实生效了，且确实翻不过第①层**，两件事在同一份
记录里互为对照。这比单测里的断言强，因为它证明的是「授权真的到位了才被拦」，
而不是「授权压根没生效所以看起来安全」。通过。

### ② 确认面板一次都没弹

**机器判到了什么**：整段 `--since 194` 的记录里 `interaction` 事件**零条**。

**判断**：黑名单是 DENY 不是 ASK——用户不该被问「要不要执行 rm -rf」这种问题。
驱动器全程也没等到过面板。通过。

### ③ 被拒后模型停止重试（顺带复核前一个修复）

**机器判到了什么**：拒绝发生在第 3 轮，此后 `stop_reason=completed`，
没有第 4 轮 `api_request`。模型的收尾话是给替代方案（`git restore .`）而非换个
写法再试一次。

**判断**：这条不在本场景的原始判据里，是顺带复核之前那个「拒绝后反复重试」的修复。
在预授权这个新语境下它依然成立。通过。

> **一处如实记录的瑕疵**（不构成不通过）：模型最后把「没执行」归因成
> 「没有未跟踪文件可清理」，而不是「被安全策略拒绝了」。拒绝原因确实回灌了，
> 但模型选择了对自己更体面的叙述。这不影响安全性（命令确实没跑），
> 但说明**回灌的拒绝原因不保证被如实转述给用户**——依赖模型转述来知情是不可靠的，
> 界面上的工具行才是权威。

---

## 场景 4：模型自行发起 fork

预置 `seed_invocability_trio` 里的 `summarize`——`context: fork` + 标准缺省
（没写 `disable-model-invocation`）。C11 的 `mode: isolated` 把「开子对话」与
「只能由用户触发」捆在一起，模型碰它必被硬拒；本场景验的是**解绑之后**。

发的是一句自然请求「这个项目是做什么的？帮我梳理一下结构」，
没有提到任何 Skill 名字。

### ① 模型凭 when_to_use 自行发起

**机器判到了什么**：turn 1 响应「好的，我先通读项目结构」→
`tool_start · tool_name=load_skill`。

**判断**：`when_to_use` 写的是「用户问『这个项目是做什么的』『帮我梳理一下结构』时」，
用户的话几乎逐字命中。模型没有被硬拒，自行发起成功。通过。

### ② 子对话跑在独立作用域里

**机器判到了什么**：seq 12–27 全部标 `isolated:summarize`，
主对话的 seq 11 与 28 标 `main`，中间没有交错。

**判断**：读记录时能一眼把子对话与主对话分开——这正是 trace 设施加作用域的目的。
子对话自己跑了 3 轮（glob → 并发读 4 个文件 → 出结论），这 3 轮完全不占主历史。通过。

### ③ 防嵌套生效

**机器判到了什么**：主对话 `工具 7 个`，子对话 `工具 6 个`。

**判断**：差的那一个正是 `load_skill`。子对话里模型看不见加载工具，
「Skill 里再激活 Skill」在第一道就被挡住。通过。

### ④ 预授权在子对话内同样生效

**机器判到了什么**：

```
isolated:summarize  glob_files → allow（③规则）· 命中 allow 规则 Read（来源：skill）
isolated:summarize  read_file  → allow（③规则）· 命中 allow 规则 Read（来源：skill）  ×4
```

**判断**：`allowed-tools: [Read, Glob, Grep]` 三项去重成一条 `Read`，
在子对话里照常命中。5 次只读调用一次面板都没弹。通过。

### ⑤ 主历史恰好新增两条配对消息

**机器判到了什么**：主对话 `turn 1 · 消息 2 条` → `turn 2 · 消息 4 条`。

**判断**：+2，正是 `assistant(tool_calls)` 与 `tool` 结果这一对。
子对话内部那 3 轮（含 5 次工具调用、12 个文件的 glob 结果）一条都没进主历史——
这就是 fork 省上下文的实际兑现。通过。

---

## 场景 5：两个可调用性开关

同一份预置里的另外两个 Skill，构成对角组合：

| Skill | disable-model-invocation | user-invocable | 预期 |
| --- | --- | --- | --- |
| `deploy` | true | （缺省真） | 模型不能发起，用户能 |
| `houserules` | （缺省假） | false | 不进菜单，模型能发起 |

### ① 模型被挡下，并给出正确入口（**发现并修掉的缺陷之一**）

**机器判到了什么**（第一次跑）：模型没调工具，直接答

```
这是个部署请求，我建议你手动执行以下命令来触发部署 Skill：
    rhine skill deploy
```

**判断**：挡是挡住了（而且挡得很省——模型从清单就读出了「仅用户可发起」，
连一轮工具调用都没浪费），但它**编了一条不存在的命令**。查清单渲染发现：
文案说「只能建议用户执行对应命令」，却从头到尾没说那条命令是什么。
模型只能猜，猜出来的 `rhine skill deploy` 敲进去只会得到「未知命令」。

**处置**：已修（小修）。`render_index` 增 `entry_hint` 注入点，
`disable-model-invocation` 的条目直接带上真实命令。命令是否注册成短命令
要问 `CommandRegistry`（重名会跳过），渲染层自己算不出来，故由调用方注入。
不给 hint 时退回旧措辞——模糊总比给一条错命令强。护栏见
`tests/test_skill_render.py::IndexTest` 那两条。

**修复后重跑**：

```
不过这个任务需要通过 `/deploy` 命令来发起，我无法自行加载并执行它。
请你在终端中运行：  /deploy
```

通过。

### ② 用户显式触发确实能跑（**发现并修掉的缺陷之二，也是本次验收最严重的一个**）

**机器判到了什么**（修复 ① 之后照着模型的指引敲 `/deploy`）：

```
UI  /deploy
UI  这个 deploy Skill 被标记为**仅用户可发起**，我无法代为加载执行。
    请你在终端中直接运行：  /deploy
```

**判断**：用户刚敲完 `/deploy`，模型转头让用户去敲 `/deploy`。
**从用户视角是一个死循环**，而且界面上看不出任何异常——模型答得有理有据，
你只会以为自己命令敲错了。

根因：`SkillManager.activate()` 把 `disable-model-invocation` 当成 Skill 的
**无条件属性**，于是用户那条路径也走进 NOT_MODEL_INVOCABLE 分支，
Skill 从未被真正激活、SOP 正文进不了动态槽位；模型只收到一句自包含调用文本，
再看清单上写着「仅用户可发起」，自然就那么答了。

讽刺的是 `run_skill` 里早就写着注释「用户经斜杠命令触发是显式动作，
`disable-model-invocation` 只挡模型，挡不到这里」——**设计想对了，代码没实现**。
根子是把两个正交维度混成了一个：这个字段判的是「**谁**在调用」，
不是 Skill 的固有属性。

**处置**：已修（小修）。`activate()` 增 `by_model` 参数（缺省 True 是 fail-safe：
新调用方忘了传，最坏是「模型被多挡一次」，而不是「本该只许用户发起的 Skill
被模型悄悄跑了」），协调层的用户入口传 `False`。
护栏见 `tests/test_skill_invocation.py::UserTriggerBypassesModelGateTest` 四条，
其中一条用 `inspect.getsource` 钉住协调层确实传了 `by_model=False`——
manager 支持了但调用方没传的话，缺陷照样存在。

**修复后重跑**：

```
UI     /deploy
SKILL  activate active=deploy
UI     部署流程已就绪。请稍等，我先查看当前项目的状态和 Git 分支信息……
```

`skill_state` 事件证明 Skill 真的激活了，而「部署流程已就绪」逐字就是该 Skill
SOP 的第一步——**正文进去了且被遵循了**。通过。

> 这条缺陷是本次验收最有价值的产出。它逃过了全部单测，因为单测各自验的是
> 「模型被挡下」与「用户能触发」两件事，而缺陷恰好在**两者的接缝处**：
> 用户那条路径复用了模型那条路径的判定函数。

### ③ `user-invocable: false` 不进命令菜单

**机器判到了什么**：

```
用户敲 /houserules → 未知命令：/houserules。输入 /help 查看可用命令。
/help 输出：含 summarize=True、含 deploy=True、含 houserules=False
```

**判断**：短命令没注册、`/help` 里也没有，两处一致。
同批的另外两个 Skill 都在，说明不是「整批都没注册」的假通过。通过。

### ④ 但模型仍可自行发起它

**机器判到了什么**（问「我要在 app/util.py 里加个新函数，这个项目的命名约定是什么？」）：

```
TOOL   read_file 读取 5 行 · 48 B
SKILL  activate active=deploy,houserules
TOOL   load_skill 激活 houserules
UI     本项目的编码约定是：**所有函数名以 `rc_` 开头**。
```

**判断**：不进菜单**不等于**模型不能用——这正是 `user-invocable: false` 的用途：
给模型看的背景知识型 Skill，让它不要占着用户的命令菜单。
模型自行发起后如实引用了 SOP 里那条约定（`rc_` 前缀），说明正文确实注入了。通过。

### ⑤ 两个维度确实正交

**机器判到了什么**：三个 Skill 的实际行为与预置表格四格逐一对应，
且 `summarize`（fork + 可被模型发起）与 `deploy`（非 fork + 不可被模型发起）
形成对角，没有一格是靠另一维度的缺省值蒙对的。

**判断**：「在哪执行」与「谁能触发」已完成解绑。通过。

---

## 场景 6：旧格式迁移提示

预置：一个 C11 时代写法的 Skill——`allowed_tools: [read_file, glob_files]`
（下划线键名 + 本系统的内部工具名）。这是本次改造中**唯一「静默会造成实际损害」**
的迁移点：同一份文件在新旧版本下行为相反。

### ① 语义变更被明确告知

**机器判到了什么**（`/skills` 报告的「字段提示」段）：

```
- 检测到 `allowed_tools`（下划线写法）。**该字段的语义已变更**：旧版本中它表示
  「收窄模型可见的工具集」，现在表示「列出的操作在本次执行内免于人工确认」，
  两者作用相反。当前按新语义（免确认）处理，请确认这符合你的本意；
  若想限制模型能做什么，请用 permissions.yaml 的 deny 规则。
```

**判断**：三样都在——**说清了旧语义、说清了新语义、指出了两者相反**，
并给出了真正该用的限制手段。措辞没有被淡化成一句「已忽略」。通过。

### ② 该字段现在按预授权生效（**行为反转的物证**）

**机器判到了什么**（执行 `/legacy` 后）：

```
api_request turn=1  工具数=7
  tools = read_file, write_file, edit_file, run_command, glob_files, grep_content, load_skill

/skills prompt →
  【当前可见工具集】
    edit_file、glob_files、grep_content、load_skill、read_file、run_command、write_file
    （Skill 不再收窄工具集）
  【已激活 Skill 授予的免确认操作】
    - Read
```

**判断**：这是整条场景最关键的一条。同一份声明：

- **旧语义**下模型只能看到 `read_file` 与 `glob_files` 两个工具；
- **新语义**下模型拿到**全部 7 个**，而那份声明变成了 `Read` 类别的免确认授权。

行为确实反转了，且反转是**可见的**（工具集与授权段都摆在 `/skills prompt` 里）。
另外 `read_file` 与 `glob_files` 都映射到 `Read`，去重后只列一条——
这正是冒烟时发现并修掉的那处瑕疵。通过。

### ③ 内部工具名被正确识别

**机器判到了什么**：`read_file` / `glob_files` 都被认出并映射到 `Read`，
没有产生「不认识的工具类别」警告。

**判断**：用户可能照着 `/skills` 里看到的工具名来写，收下它们是对的。通过。

### ④ Skill 仍照常可用

**机器判到了什么**：`/legacy` 正常执行完毕，未出现加载失败。

**判断**：语义变了但没把 Skill 弄坏。通过。

---

## 场景 7：无能力字段告知

预置：一个同时声明了 `background` / `agent` / `effort` / `hooks` / `paths` / `shell`
六个字段的 Skill——本版本对它们**都没有对应能力**。

### ① 六条各自一行，且说清「本版本实际会怎么做」

**机器判到了什么**：

```
字段提示（不影响运行，但与你的声明有出入）：
- `background`：本版本不支持后台执行，将同步等待子对话跑完
- `agent`：本版本没有子代理类型的概念，该声明被忽略
- `effort`：本版本的思考强度由 /think 全局控制，该声明被忽略
- `hooks`：本版本不支持 Skill 级钩子，该声明被忽略
- `paths`：本版本不支持按路径自动激活，该声明被忽略
- `shell`：本版本的命令执行走系统默认 shell，该声明被忽略
```

**判断**：六条不多不少。措辞上值得注意的是 `background` 那条——它没有停在
「不支持」，而是说明了**实际会发生什么**（同步等待）。作者写 `background: true`
的预期是「后台跑、不阻塞」，只说「不支持」他仍然不知道实际行为是什么。通过。

### ② Skill 仍正常加载与执行

**机器判到了什么**：`fancy` 出现在 `/skills` 列表里，短命令已注册。

**判断**：这些字段不该让 Skill 失效——它们只是「本版本做不到的额外要求」。通过。

### ③ 提示只出现一次（**验收中发现并修掉的缺陷**）

**机器判到了什么**：第一次跑这条场景时，七条提示**各打印了两遍**——
一次在「警告：」段、一次在「字段提示」段。

根因是我在改造中让解析层把同一批 notices **同时**作为 `spec.notices` 与
返回值里的 `warnings` 输出，前者进「字段提示」、后者经 `catalog.warnings`
进「警告」。

**处置**：已修——解析层的告知只走 `spec.notices` 一条通路。
发现层那套「被覆盖那份的警告不发出」的机制在这里本来就是多余的：
报告只遍历 `catalog.skills`，被覆盖的 spec 压根不在里面，
它的 notices 自然跟着一起消失。重启宿主后确认只剩一份。通过。
