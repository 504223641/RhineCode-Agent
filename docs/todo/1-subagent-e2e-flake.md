# `test_conclusion_delivered_exactly_once` 在全量下偶发红

> 建议分支：`subagent-e2e-flake` · 复杂度：小（**先定性，再决定改不改**，估计 1–2 小时）
> 来源：web_search 扩展合并后（PR #51，2026-08-20）的全量验证
>
> ⚠ **排在第 1 位不是因为它最重要，是因为它挡在所有别的事前面**：
> 全量跑三次红两次，而它的失败信息极具误导性——以后每一次「跑一遍全量确认没回归」
> 都可能被它污染，而看到的人会先怀疑自己刚改的东西。

## 症状

```
FAIL: test_conclusion_delivered_exactly_once
      (test_subagent_e2e.BackgroundE2ETest.test_conclusion_delivered_exactly_once)
AssertionError: 0 != 1 : 结论只该被交付一次，重复会让同一段内容在历史里出现多遍
```

`0 != 1` 的意思是：**结论一次都没进最后那份请求体**（不是进了两次）。

## 已经取样过的（别重复做）

| 取样方式 | 结果 |
| --- | --- |
| 新 main（合并后）全量 3318 项 | **3 次：1 绿 2 红** |
| 合并前的 main（`ca427e9`）全量 3143 项 | 1 次绿。⚠ 那次另有 2 条 `test_e2e_sandbox_seed` 失败，是**在 worktree 里跑**造成的假阳性（仓库根路径校验），与本条无关 |
| 新 main 单跑那一条 | 10 次全绿 |
| 新 main 单跑整个 `test_subagent_e2e` 模块 | 5 次全绿 |
| 新 main 跑 `test_subagent*` 全组 | 3 次全绿 |
| 新 main 跑 `test_[a-s]*.py`（2021 项） | 1 次全绿 |

**最后一行最关键**：那个子集里「字母序在 `test_subagent_e2e` 之前跑的模块」与全量
**完全相同**，却绿了。所以它**与跑了哪些模块无关**，是真随机的时序抖动。

⟹ **不要再靠「换一组模块跑」来定位**，那条路已经证明没有信息量。

## 已经排除的两件事

**① 不是「先置终态、后入队」那种经典竞态。**
`tasks.take_deliverables` 的判据是「终态 且 未交付 且 同代」，而 `_settle` 等的正是
**终态**——两者用的是同一个信号，中间没有窗口。

**② `_settle` 没有提前放弃。** 它撞上超时会**明确失败**（那是上一次修这条测试时改的），
而失败信息是 `0 != 1` 不是超时，说明子 Agent **确实跑到了终态**。

## 值得先看的三处

按「最可能」排序：

1. **结论落在哪一次请求体里。** 测试断言的是 `main_bodies[-1]`，即**最后一次**
   主对话请求。交付有两条路径共用同一个消费型队列：闸门的**迭代级注入**、
   以及跨用户消息的兜底 `_deliver_subagent_results`。
   ⚠ **先搞清楚注入是持久进 `history` 还是只在那一轮的请求里出现**——
   若是后者，那么「交付发生在第几条用户消息上」就直接决定这条断言的成败，
   而那个时机是随机的。这是目前最像根因的一条。
2. **`epoch`。** `take_deliverables` 只取 `epoch == self._epoch` 的任务。
   若有什么在 `_settle` 与后两条用户消息之间 bump 了代号，结论会被永久跳过。
3. **测试本身的写法。** 断言 `main_bodies[-1]` 而不是「全部 body 里合计出现一次」
   可能本来就太脆。⚠ 但**别急着把断言放宽**——「恰好一次」是这条用例的全部价值
   （它防的是重复交付），放宽成「至少一次」等于把它废掉。

## ⚠ 两条不要做的

- **不要加 `sleep` 或重试。** 这条用例的历史就是「等待函数悄悄放弃」——
  `_settle` 的 docstring 里写着那次教训。再加一层等待只会把问题埋得更深。
- **不要标记 `@skip` 或 `expectedFailure`。** 它验的是 C13「结论恰好交付一次」，
  那是真实功能。关掉它等于把一条真护栏换成一句免责声明。

## 归因还没做完

**没能确定是不是 web_search 那轮引入的。** 两个事实互相拉扯：

- ✅ **它有前科**：那条用例自己的 docstring 第一句就是「⚠ **这条原本也是偶发红**」，
  上一次的根因（`_settle` 静默返回）已经修掉，但**这次的形态可能是另一个**。
- ⚠ 但新 main 上 2/3 的红率偏高，而 PR #51 确实动了两处可能相关的：
  ① 加了 175 项测试，改变了全量的整体时序；
  ② 改了 `subagents/runner.py` 的 `network_tool_names`（加了 `web_search`）——
     它只影响子 Agent 提示词里要不要追加「外部不可信内容」那一段，
     **理论上碰不到交付路径**，但没有实测排除。

**第一步建议先把归因做完**：在合并前的 `ca427e9` 上跑 3 次全量（别用 worktree，
用 `git checkout` 或干净的克隆，否则会撞上那两条路径校验的假阳性）。
- 若那边也红 → 是既有抖动，与 web_search 无关，安心查机制。
- 若那边 3 次全绿 → 大概率是 ① 的时序放大，重点看第 1 条「结论落在哪一次请求体」。

## 判据

- 能**稳定复现**（说清在什么条件下必红），或说清为什么复现不了
- 根因写下来，且能解释**为什么单跑 18 次不红、全量却 2/3 红**
- 修完之后全量**连跑 5 次全绿**（一次绿不算数——它本来就有 1/3 的概率绿）
- 「恰好一次」这条断言的**强度不降低**

## 要读的文件

- `tests/test_subagent_e2e.py` —— `BackgroundE2ETest` 与 `_settle`
  （两处 docstring 都记着上一次修它的经过，先读那两段）
- `rhinecode/subagents/tasks.py` —— `take_deliverables` / `finish` / `begin_session`
  （`epoch` 的语义在 `begin_session` 里）
- `rhinecode/subagents/runner.py` —— `tasks.finish` 前后那段（⑧⑨两步的顺序有注释）
- `rhinecode/conversation.py` —— `_deliver_subagent_results` 与闸门的迭代级注入
- `CLAUDE.md` 的「测试」一节 —— 那三个一组的数字，若用例数变了要一起更新

---

## 一键开工 Prompt

```
先检查当前分支：`git branch --show-current`。
- 如果在 `main` 上：先起新分支再动手 `git checkout -b subagent-e2e-flake`
- 如果已在别的分支上：确认那是本任务的分支再继续；不是的话先问我。

查一条偶发红的测试：
tests/test_subagent_e2e.py::BackgroundE2ETest::test_conclusion_delivered_exactly_once
失败形态是 `AssertionError: 0 != 1`（结论一次都没进最后那份请求体）。

背景与**已经取样过的全部数据**见 docs/todo/1-subagent-e2e-flake.md，先读它，
别重复我已经做过的取样——尤其是「换一组模块跑」那条路已经证明没有信息量
（跑 test_[a-s]*.py 全绿，而那个子集里在它之前跑的模块与全量完全相同）。

⚠ 已经排除两件事：不是「先置终态、后入队」的竞态（take_deliverables 与
_settle 用的是同一个终态信号）；_settle 也没有提前放弃（它超时会明确失败，
而失败信息不是超时）。

⚠ 第一步先把归因做完：在合并前的 ca427e9 上跑 3 次全量（**别用 worktree**，
会撞上 test_e2e_sandbox_seed 那两条路径校验的假阳性）。据此判断是既有抖动
还是 PR #51 的时序放大。

⚠ 两条不要做：不要加 sleep / 重试（这条用例的历史就是「等待函数悄悄放弃」）；
不要 skip 或 expectedFailure（「恰好一次」是它的全部价值，防的是重复交付）。

修完之后全量**连跑 5 次全绿**才算数——它本来就有约 1/3 的概率绿。

做完这条后把 docs/todo/1-subagent-e2e-flake.md 删掉，并重排 docs/todo/ 下
其余文档的序号，同步 docs/todo/README.md 与全仓的编号引用
（跑一遍 `grep -rn "docs/todo/[0-9]\|第 [0-9] 条 todo" docs/todo/`）。
```
