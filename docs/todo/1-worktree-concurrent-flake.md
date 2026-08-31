# 并发创建隔离工作区的 CI flake

> 状态：待开工 · 预计半天 · **优先级最高，但不是因为它严重**
>
> 建议分支：`worktree-concurrent-flake`（从 `main` 起）
>
> ⚠️ **这条已经有一把锁了，而它没挡住。开工第一件事是搞清楚为什么，不是再加一把锁。**

## 症状

2026-08-31，PR #63 的 CI 上 `test_subagent_isolation.ConcurrencyTest` 红了一格：

```
FAIL: test_three_isolated_delegations_get_distinct_worktrees
AssertionError: False is not true : 委派失败：该角色要求隔离的 Git 工作目录，
但创建不成功——创建隔离工作区失败：
git worktree add -b agent/worker-b9cfbcf7 <...>/.rhinecode/worktrees/worker-b9cfbcf7 e04e08d 失败：
Preparing worktree (new branch 'agent/worker-b9cfbcf7')
fatal: could not create directory of '.git/worktrees/worker-b9cfbcf7': No such file or directory
```

**6 格里只红了 1 格**（windows-latest / Python 3.11），另外两个 Windows 格跑同一份
代码全过；重跑该格即通过。那一格分片跑了 175.8 秒。

## ⚠ 为什么这条排在最前面

**不是因为它严重——它一年也未必再发作一次。是因为它现在最便宜，而放着最贵。**

CI 门禁是 2026-08-30 才装上的（F4）。一条在门禁刚装好就出现、又「重跑一下就过」的
红灯，会**当天**教会所有人一个习惯：红了先重跑。而这个项目自己在别处反复写过同一句
话——「一个没人看的检查器比没有检查器更糟」「人对噪声门禁的标准反应是放宽断言」。
等这个习惯养成，**某天它报出一条真问题时，那条也会被一起划走**。

其余三条待办都是多天的测量工作；这条是半天的。先清掉它，门禁才立得住。

## ⚠ 关键背景：已经有一把锁，而且它是专门为这件事加的

`rhinecode/worktree/lifecycle.py:73` 有一个 `_CREATE_LOCK`，上方那段注释写得很清楚，
**开工前必须完整读一遍**。摘要：

- `git worktree add` 在同一个版本库上**不是并发安全的**：它开工时会做一次隐式 prune，
  把「看起来没建完」的 `.git/worktrees/<名字>/` 清掉，而另一个正建到一半的 worktree
  恰好就长那个样子；
- 2026-08-30 CI 的 windows/3.12 那一格因此红过一次，于是把「④环境确认 ⑤挑分支
  ⑥`add`」三步收进这把进程内的锁；
- 那段注释还记着：**这个窗口只在机器够慢时才张开**——本机 8 路 × 6 轮复现不出来，
  红掉的那格分片跑了 150.9s（平时约 60s）。

**这次红的那格跑了 175.8s，比上次还慢。**

## ⚠ 但这次的报错与上次不是同一句

| | 上次（已被 `_CREATE_LOCK` 修掉） | 这次 |
| --- | --- | --- |
| 报错 | `could not open '.git/worktrees/<名字>/locked' for writing` | `could not create directory of '.git/worktrees/<名字>'` |
| 阶段 | 已经建了目录、在写里面的 `locked` 文件 | **连目录本身都没建成** |

**两句话指向的不是同一个瞬间**，所以不能假定「锁没生效」或「锁范围不够大」就完事。
`_CREATE_LOCK` 确实把 `add_worktree` 包在里面了（`lifecycle.py:329` 起），
这一点看代码可以确认。

## 首要假设（**未经验证**，请先证实或证伪）

**`remove()` 不在 `_CREATE_LOCK` 里，而它会 prune。**

`create` 的锁范围到 `add_worktree` 为止；而回收路径（`lifecycle.remove` / 启动清理）
走的是另一条线，**不持这把锁**。本条用例的形态恰好能撞上：三个隔离子 Agent 并发跑，
**先跑完的那个会因「无变更」被回收**（C14 F16：结束时无变更即回收目录与分支），
而那时第三个可能还在 `add`。一次 prune 若把当时为空的 `.git/worktrees/` 父目录一并
清掉，正在建子目录的那个就会拿到 `No such file or directory` ——**与观测到的报错吻合**。

⚠ **这只是最说得通的一个解释，不是结论。** 开工的第一步是**证实它**，别直接照着改。
其它需要排除的可能：

- Windows 上的**目录创建瞬时失败**（杀毒软件 / Defender 扫描持有句柄），那类问题
  加锁治不了，只能重试；
- 上一条用例遗留的 daemon 子 Agent 线程还在动同一个仓库（本项目的子 Agent 线程是
  daemon，用例之间不保证已经收干净）。

## 怎么复现（本机复现不出来是常态，别据此认为它不存在）

那段既有注释已经记过：本机 8 路 × 6 轮复现不出上一次那个形态。**这类窗口只在机器
够慢时张开**，所以：

1. **优先用放大窗口的办法**——在 `add_worktree` 前后、以及回收路径的 prune 前后各插
   一个可配置的 `sleep`，把两条路径的时序摆成最坏情况。这是本项目验 C10-c 时用过的
   同一招（在截断与写入之间插 2 毫秒 sleep，把 12 次里 10 次的命中率造出来），
   **它证明的是「这个窗口真实存在且会被击中」，不是真实概率**。
2. 别指望靠反复跑撞出来。

## 修法方向（三选一，先证实成因再挑）

1. **把回收也收进 `_CREATE_LOCK`**（若首要假设成立）。代价最小，但要确认回收路径
   不会在持锁时做任何跨线程调度——那把锁的注释里专门论证过「它不参与死锁」的理由
   （模块级、只被子 Agent 工作线程走到、不持回调、不碰界面），**新收进来的代码必须
   同样满足这四条**，否则那段论证就作废了。
2. **建目录前确保父目录存在**（`.git/worktrees/` 先 `mkdir(exist_ok=True)`）。
   看起来最省事，但它治的是症状——如果成因是 prune 与 add 互相拆台，那么补一次
   mkdir 只是把窗口变窄。
3. **有限重试**（只对这一类报错、只重试一两次）。若成因是 Windows 的瞬时失败，
   这是唯一有效的一种；若成因是竞态，它会掩盖问题。

⚠ **无论选哪个，都不要把「创建失败」降级成「不隔离运行」。** C14 安全边界第 ④ 条
明写：创建失败必须明确失败，降级是本项目通篇最忌讳的形态——用户配了隔离却没隔离，
而界面上完全看不出来，等到子 Agent 与主 Agent 互相覆盖文件时谁也想不到根因。

## ⚠ 动手前必读

- **先加载 `paired-maintenance` Skill**：`worktree/` 在它的目录触发清单里。
- `worktree/` 是**全项目唯一执行 git 的地方**，架构表上带 ⚠ 致命不变量。尤其：
  **`lifecycle.remove` 是唯一删除入口**，它先过纯判定 `judge_removal` 的三层过滤
  （位置必须严格落在 `.rhinecode/worktrees/` 内且不等于它本身 / 归属必须被版本库
  登记 / 有未提交改动一律否决），**未获许可一步都不往下走**——绕过它等于把
  「空变量 rmtree 删掉整个仓库」那次事故的闸门拆了。
- 删目录一律走既有的 `_force_rmtree`，**不要用 `shutil.rmtree(path, ignore_errors=True)`**：
  撞上 git 留下的只读 `.git/objects` 会「删一半」，留下一个残骸且一个错都不报。
- ⚠ **别把 `test_three_isolated_delegations_get_distinct_worktrees` 改成串行或
  加 `skip` 来「修」它。** 那条用例是照着 C14「并发隔离委派」这个**明确支持的场景**
  写的，把它拆掉等于把那个承诺一起撤了，而 CI 会变绿——**这是最糟的一种收尾**。

## 验收

- 放大窗口的复现脚本能**稳定**触发原症状（先有可复现的红，再谈修）；
- 修完之后同一个脚本 20 轮全绿；
- `python -m tests.run_parallel`（**看退出码，不要只看最后一行的条数**）；
- 连跑 5 次确认不是偶然——并行最怕的就是偶发，别只跑一次就下结论；
- 若最终结论是「Windows 瞬时失败、加重试」，请把**为什么不是竞态**的证据写进
  `_CREATE_LOCK` 上方那段注释里。那段注释是这件事唯一的知识载体，**下一个人会先读它**。

---

<details>
<summary><b>一键 Prompt（点开复制）</b></summary>

```
先检查当前分支：`git branch --show-current`。
- 如果在 `main` 上：**先起一条新分支**再动手，不要直接在 main 上改。
  `git checkout -b worktree-concurrent-flake`
- 如果已经在别的分支上：确认那是本任务的分支再继续；不是的话先问我。

修 RhineCode 的一条 CI flake：并发创建隔离工作区偶发失败。

⚠️ 开工前必须先完整读两处：
1. docs/todo/1-worktree-concurrent-flake.md（本任务的全部上下文）
2. rhinecode/worktree/lifecycle.py 第 47~73 行 _CREATE_LOCK 上方那段注释
   ——这条问题**已经修过一次**，那把锁就是为它加的。

⚠️ 动 worktree/ 之前先加载 paired-maintenance Skill。

症状：test_subagent_isolation.ConcurrencyTest 在 CI 上偶发失败，6 格里红 1 格
（windows-latest/3.11），重跑即过。git 的真实报错是：
  fatal: could not create directory of '.git/worktrees/<名字>': No such file or directory

⚠️ 注意它与上次那条**不是同一句**（上次是 could not open '.../locked' for writing），
指向的不是同一个瞬间，所以别假定「锁没生效」就完事。

首要假设（**未经验证，第一步是证实或证伪它，不要直接照着改**）：
lifecycle.remove（回收路径）不在 _CREATE_LOCK 里，而它会 prune。三个隔离子 Agent
并发跑时，先跑完的那个会因「无变更」被回收，而第三个可能还在 add——prune 若把当时
为空的 .git/worktrees/ 父目录清掉，正在建子目录的那个就拿到 No such file or directory。
其它要排除的：Windows 上的目录创建瞬时失败（杀毒扫描持句柄）、上一条用例遗留的
daemon 子 Agent 线程还在动同一个仓库。

复现方法：本机直接跑复现不出来（既有注释记着 8 路×6 轮无果，这类窗口只在机器够慢
时张开）。**用放大窗口的办法**：在 add_worktree 前后与回收路径 prune 前后各插一个
可配置的 sleep，把时序摆成最坏情况。这是本项目验 C10-c 时用过的同一招——它证明的是
「窗口真实存在且会被击中」，不是真实概率。

三条硬约束：
1. **不要把创建失败降级成「不隔离运行」**（C14 安全边界第 ④ 条：降级是本项目通篇
   最忌讳的形态，用户配了隔离却没隔离而界面上看不出来）。
2. **不要把那条并发用例改成串行或 skip 来「修」它**。它是照着 C14 明确支持的
   「并发隔离委派」场景写的，拆掉它等于把那个承诺一起撤了，而 CI 会变绿——
   那是最糟的一种收尾。
3. 若把回收也收进 _CREATE_LOCK，**新收进来的代码必须同样满足那段注释里论证过的
   四条**（模块级、只被子 Agent 工作线程走到、不持任何回调、不碰界面、不做跨线程
   调度），否则「这把锁不参与死锁」那段论证就作废了。

验收：放大窗口的脚本能**稳定**触发原症状（先有可复现的红，再谈修）→ 修完同一脚本
20 轮全绿 → python -m tests.run_parallel（**看退出码，别只看最后一行的条数**）→
连跑 5 次确认不是偶然。
若结论是「Windows 瞬时失败、加重试」，把「为什么不是竞态」的证据写进 _CREATE_LOCK
上方那段注释——那段注释是这件事唯一的知识载体，下一个人会先读它。

做完这条后把 docs/todo/1-worktree-concurrent-flake.md 删掉，并重排 docs/todo/ 下
其余文档的序号（tests/test_docs_links.py 会把全仓过期引用逐处列出来，跑一遍即可）。
```

</details>
