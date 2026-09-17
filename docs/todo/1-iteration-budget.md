# 1 · 固定 25 轮迭代上限换成别的策略

> 建议分支：`iteration-budget`
> 复杂度：中（1–2 天）。代码不多，**难在拿定产品判断**——上游两家都不用固定轮数。

## 起因

用户 2026-09-17 反馈「25 轮有点少」，并给了一份真实 trace
（`G:\Splendor-Agent\.rhinecode\traces\20260917-104145-939.jsonl`）。

那份 trace 里第二条用户消息（「帮我创建一个新的 agent」）**正好跑满 25 轮**、
撞 `max_iterations` 停止，用户只好手打「继续」再跑 20 轮。

⚠ **但那 25 轮里有 9 轮（36% 的迭代、35% 的输入 token）是被一个 bug 白烧掉的**
——c8 存盘占位把模型指向存盘文件，形成一个不收敛的循环。**那个 bug 已于
2026-09-17 修复**（见 `docs/internals/known-issues.md` 的 #21），所以：

> **开工第一件事不是改上限，是先量一遍修复之后 25 轮到底够不够。**
> 修复前的可用率是 64%，修完接近 100%。拿修复前的观测去论证「上限要加大」
> 是错的——很可能加大之后只是让它多烧 25 轮在别的地方。

## 上游两家怎么做的（2026-09-17 查证，附出处）

| | 固定轮数上限 | 真正的停止条件 |
| --- | --- | --- |
| **Claude Code / Agent SDK** | `max_turns` / `maxTurns` 存在，**默认 No limit** | 模型这一轮不再要工具（主要）；可选 `max_budget_usd`（默认也无限制）；自动压缩让长任务**继续跑**而不是撞墙 |
| **Codex** | `--max-turns` 的提案 **closed as not planned** | 同上；95% 上下文自动压缩；用户 Esc |

出处：
[Claude Code 的 agent-loop 文档](https://code.claude.com/docs/en/agent-sdk/agent-loop)
（「Turns and budget」那张表逐字写着 Default = No limit）、
[openai/codex#12336](https://github.com/openai/codex/issues/12336)。

**结论：固定轮数在两家那里都不是主要护栏，只是一个可选的成本闸门。**
真正的护栏是三样：① 模型自然收工 ② 预算/成本上限 ③ 用户随时可打断。

这值得想清楚原因：**固定轮数把「预算」和「进展」混为一谈了。**
一个健康的 10 轮任务和一个空转 10 轮的死循环，在计数器眼里一模一样。

## 三个方向（按性价比排，不必全做）

### ① 撞上限不硬停，改成问用户（最小改动，收益最大）

现状是 `agent/loop.py` 走完 `range(1, max_iterations + 1)` 后直接 yield
`FINISHED(MAX_ITERATIONS)`，界面出一条警告级系统行，**上下文原样留着但循环结束**。
用户唯一的出路是手打「继续」——而那一句要重新热身（trace 里「继续」之后
第一轮请求的 messages 已有 95 条）。

改成弹一次面板「已跑 25 轮，继续 / 停止」。项目已有 `ask_user` 的澄清面板与
`Esc`，接线成本低。

⚠ **两处要想清楚**：
- **可见性判据**（ask-user 扩展）——没有澄清回调时不能弹面板。子 Agent 与
  C15 无人值守轮都拿不到回调，它们必须维持现在的硬停行为。
- **别把它做成无限续**——续跑要有次数上限，否则等于把上限删了。

### ② 加「原地打转」检测（这才是对症的）

连续 N 轮工具调用的 `(工具名, 参数)` 与之前重复、或连续 N 轮没有任何新信息进来
→ 打断并把这件事**直接告诉模型**（不是静默停止）。

本次那三条链会在第 2 次就被掐掉，省下 7 轮。

⚠ 参考 [openai/codex#44909](https://github.com/openai/codex/issues/44909)
（measured cost of false goal continuations across 3,808 sessions）——
同一类问题上游也在量。
⚠ **判据要选得住**：纯粹「参数逐字相同」会漏掉「换了个 start_line 接着空转」，
而太宽会误伤合法的重试（比如 `run_command` 跑测试 → 改代码 → 再跑同一条命令，
那是**正常**的）。**先拿现有 trace 回放验一遍误报率再定阈值。**

### ③ 判据从「轮数」换成「预算」

token 或花费比轮数更贴近真实成本。数据是现成的：c8 已有估算，
每轮 `api_response.usage` 有精确值，trace 里都记着。

⚠ 这条与 ① 不冲突，可以叠加（Claude Code 就是两个都有）。

## ⚠ 开工前必读的一处成对约束

`MAX_ITERATIONS = 25`（`agent/loop.py`）**同时是子 Agent 的硬顶**：
`subagents/models.py` 的 `HARD_MAX_TURNS = 25` 上方逐字写着「它必须 ≤
`agent/loop.py` 的 `MAX_ITERATIONS`」，两个数字**人工对齐**
（刻意不 import，否则 `subagents` 数据层会拉起整个 Agent Loop），
`tests/test_subagent_parser.py` 有断言钉住，改大了当场红。

另外 `skills/models.py` 的 `SKILL_MAX_ITERATIONS = 15` 是独立预算，
改主上限时**不要顺手一起改**——那是「子任务应当聚焦」的刻意取值。

## 验收怎么做

用**调度器**（`tests/e2e/`，见 `CLAUDE.md` 的术语提醒）真跑，别只写单测：
这三个方向改的都是「什么时候停下来」，而那件事**单测里看不出体感**。
真实模式（`--mode live`）跑一个明确十几步的任务，看它现在要几轮、
撞不撞上限、撞上之后用户看到的是什么。

---

## 一键开工 Prompt

```
先跑 `git branch --show-current`。如果在 main 上，先 `git checkout -b iteration-budget` 再动手。

读 `docs/todo/1-iteration-budget.md`，按它做。

⚠ 三件事按顺序，别跳：

1. **先量，别先改。** 那份立项文档里「25 轮不够」的观测来自一个**已经修掉的
   bug**（存盘占位死循环，已知项 #21，36% 的迭代白烧）。先用调度器跑几个真实
   的多步任务，看修复之后 25 轮的实际可用率是多少。**如果够用，这条待办的正确
   结局可能是「只做方向 ②，不动上限」——那也是个合格的产出。**

2. **动 agent/ 之前先加载 `paired-maintenance` Skill。** `MAX_ITERATIONS` 是一处
   成对维护点（`subagents/models.py` 的 `HARD_MAX_TURNS` 必须 ≤ 它，两个数字人工
   对齐、有断言钉着）。

3. **方向 ② 的阈值要用现有 trace 回放验误报率再定。** 「连续 N 轮重复调用」看着
   简单，但 `run_command` 跑同一条测试命令在改代码前后重复是**正常**的，
   一刀切会误伤。

验收用 `tests/e2e/` 真跑（项目里管它叫「调度器」），别只写单测——这三个方向改的都
是「什么时候停下来」，单测里看不出体感。⚠ 从 Git Bash 驱动必须 `MSYS_NO_PATHCONV=1`。

做完把这份文档删掉，并按 `docs/todo/README.md` 的命名规则重排剩下的序号
（`git mv` 保历史 + 同步 README 的表）。
```
