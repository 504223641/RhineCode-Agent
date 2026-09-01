# 分身 Skill 里的「派活」语义与主对话不一样，而界面上看不出来

> 状态：待开工 · 预计半天 · **优先级第五，因为它不丢结果、只是行为不一致**
>
> 建议分支：`fork-skill-delegation`（从 `main` 起）
>
> ⚠️ **开工第一件事是回答一个产品问题：`context: fork` 的 Skill 该不该能派活？**
> 答案是「不该」时的修法，和答案是「该」时的修法，改的**不是同一个文件**。

## 背景：它是修 B3 时顺手核出来的

2026-08-31 修 B3（分身子对话没拿到 Hook）时，那个 Agent 把两处
`Agent(...)` 构造的参数逐个对照完之后，多看了一步紧邻的 `agent.run(...)`，报告：

> **fork 子对话的 `run()` 不传 `subagent_gate`**（主对话是
> `subagent_gate=self.subagent_gate()`，fork 侧 `RunOptions` 里没有这一项，
> 循环退化成 `NullGate`）。而 `SkillManager.fork_excluded_tools()`
> **只排除 `load_skill`，`run_agent` 仍然可见**。

两点都已核实：`conversation.py` 里 `subagent_gate=` 只出现在主对话那一处；
`fork_excluded_tools()` 的 docstring 逐字写着「恒为 `{load_skill}`」。

## 症状

一个 `context: fork` 的 Skill **可以**派活给子 Agent，但那条对话：

- **不会在每轮迭代把子 Agent 的结论注入历史**
- **模型准备收工时不会停下来等它**

C13 的契约里，这两件事合起来叫「委派永不阻塞，但你要的结果一定等得到」。
分身这条路上，后半句没了。

⚠ **它不是「丢结果」**：结论最终应该会由主对话的 `_deliver_subagent_results` 兜底送达。
所以症状是**时序**不是**丢失** —— 分身 Skill 派出去的活，结果会在它自己已经收工之后
才回到主对话，而那时那条 Skill 的上下文已经没了。

⚠ **界面上完全看不出来。** 这正是本项目反复登记的那类失败形态：
编译过、测试绿、界面正常，只是某个行为悄悄不对了。

## 先回答这个问题

| 答案 | 修法 | 理由 |
| --- | --- | --- |
| **不该能派活** | 把 `run_agent` 加进 `fork_excluded_tools()` | 分身 Skill 的定位是「用一段独立上下文做一件事」，它自己就是被派出去的那个；再往下派一层，嵌套深度就没有上限了。⚠ 注意 `load_skill` 被排除的理由**逐字就是这个**（防无限嵌套） |
| **该能派活** | 给 fork 侧的 `RunOptions` 补 `subagent_gate` | 分身 Skill 常用于「把调研赶出主上下文」，而调研本来就是最该并行的事 |

⚠ **别两个都做。** 补了闸门又不排除工具，等于承认它该能派活；
排除了工具又补闸门，那个闸门永远不会被触发 —— 一段永远走不到的代码
比没有更糟（下一个人要花时间搞清楚它为什么在那儿）。

⚠ **倾向「不该」，但这不是结论**：`fork_excluded_tools()` 的 docstring 里
「防止 Skill 里再激活 Skill」那条理由，对 `run_agent` 同样成立 ——
但那只是类比，不是论证。真正该看的是有没有人**实际**在分身 Skill 里派过活。

## 要做什么

1. **定答案**，写清理由。
2. 按答案改**一处**（不是两处）。
3. **护栏钉住你选的那个语义**：
   - 选「不该」→ 钉 `run_agent` 不在分身子对话的工具集里，
     且**模型硬造一次调用时被循环层的兜底判定拒掉**（那道兜底是既有的，
     排除只是不把 schema 发给模型 —— 两者缺一，嵌套防线就不成立）
   - 选「该」→ 钉「分身 Skill 派出去的活，在该条 Skill 收工前结论就回来了」
4. 变异实测：撤掉修复，确认护栏真的会红。

## 判据

- 两种语义**只落地一种**，且代码里没有另一种的残留
- 护栏钉的是**语义**不是实现细节
- `python -m tests.run_parallel` 全绿

## 要读的文件

- **先读 B3 那次的改动**（PR #65 / 分支 `fork-skill-delegation` 的上游
  `fix-fork-hooks`，2 个 commit）—— 它的护栏 `HookDispatchTest` 已经把
  「跑通一条分身子对话并真的执行一次工具」这条链路搭好了，**照着改最省事**
- `rhinecode/conversation.py` 的 `_run_forked_skill`（fork 侧那次 `agent.run(...)`）
  与主对话那次（`subagent_gate=` 唯一出现的地方）
- `rhinecode/skills/manager.py` 的 `fork_excluded_tools()`（含它的 docstring 理由）
- `CLAUDE.md` 的 C13 第 ⑧ 条（「等待期间唯一的逃生口是 Esc」——
  它解释了那个闸门到底在保证什么）

## 一键开工 Prompt

```
先跑 git branch --show-current，如果在 main 上就先 git checkout -b fork-skill-delegation。
动 conversation.py 与 skills/ 之前先加载 paired-maintenance Skill。

我要做 docs/todo/2-fork-skill-delegation-semantics.md 记的这件事：
context: fork 的 Skill 子对话可以派活给子 Agent（run_agent 在它的工具集里），
但它的 run() 不传 subagent_gate，于是循环退化成 NullGate ——
不会每轮注入子 Agent 结论，模型准备收工时也不会停下来等它。
主对话那侧是传了的。结果不会丢（主对话有兜底送达），但时序不一样，
而界面上完全看不出来。

⚠ 开工第一件事是回答产品问题，不是改代码：fork 的 Skill 该不该能派活？
- 不该 → 把 run_agent 加进 skills/manager.py 的 fork_excluded_tools()
- 该   → 给 fork 侧的 RunOptions 补 subagent_gate
⚠ 别两个都做。补了闸门又排除工具，那个闸门永远走不到，
一段永远走不到的代码比没有更糟。
⚠ 倾向「不该」（fork_excluded_tools 排除 load_skill 的理由逐字是「防无限嵌套」，
对 run_agent 同样成立），但那只是类比不是论证，请自己判。

⚠ 这是 2026-08-31 修 B3 时顺手核出来的。先读那次的改动
（分支 fix-fork-hooks，2 个 commit）——它的护栏 HookDispatchTest 已经把
「跑通一条分身子对话并真的执行一次工具」这条链路搭好了，照着改最省事。

护栏要钉语义不是实现细节：
- 选「不该」→ 钉 run_agent 不在分身子对话的工具集里，且模型硬造一次调用时
  会被循环层那道既有的兜底判定拒掉（排除只是不发 schema，两者缺一防线不成立）
- 选「该」→ 钉「分身 Skill 派出去的活，在该条 Skill 收工前结论就回来了」
每条都做变异实测：撤掉修复确认真的会红，再恢复。

约束：走分支 + PR，跑 python -m tests.run_parallel 确认全绿。
```
