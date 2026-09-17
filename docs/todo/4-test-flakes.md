# 4 · 三条并行/满负载下的偶发红

> 建议分支：`test-flakes`
> 复杂度：中（**先定性，再决定改不改**；三条可能是三个独立成因，别指望一刀切完）

## 为什么值得单独立项

三条都不是「产品坏了」，都是**测试里藏了时间假设**：断言跑在某个异步的东西还没
落位的时刻，机器一忙就跨线。危害不在于它们会红，在于**它们训练所有人忽略失败**
——「跑一遍全量确认没回归」这个动作只要被污染，之后每一次真实回归都可能被当成
「又是那几条老毛病」放过去。

⚠ **第 3 条是复发**，2026-08-20 专门立项修过一次（见下），这本身就是信号。

## 三条各是什么

### ① `tests/test_setup_screen.py::ModelStepTest::test_option_list_gets_focus`

```python
await self.fill_credentials(pilot, screen)
self.assertTrue(screen.query_one("#setup-models", OptionList).has_focus)
```

**取样（2026-09-17，Windows 11 / Python 3.11）**：单条单跑 3/3 绿；整模块单跑绿；
`run_parallel` 八分片下**干净 main 上 3 次红 2 次**；串行满量 `discover` 也复现过一次。

断言紧跟在填完凭据、切到第三屏之后，等的是 Textual 把焦点推到列表上。

### ② `tests/test_e2e_ask_user.py::AskUserE2ETest::test_the_input_box_is_only_unlocked_in_the_free_text_state`

```python
self.assertFalse(bar.disabled, "自由输入态下输入框必须解禁")
```

**取样**：整模块单跑 8 项全绿；单条单跑 3/3 绿；`run_parallel` 下红过。

### ③ `tests/test_subagent_e2e.py::ForegroundE2ETest::test_conclusion_returned_as_tool_result`

```python
self.assertIn("a.py", self.provider.main_bodies[-1])
# AssertionError: 'a.py' not found in '找一下\n\n已启动子 Agent finder，任务标识 701c70。…'
```

**取样**：CI 的 `ubuntu-latest / Python 3.13` 红过一次（PR #75），**同一 commit 重跑绿**；
同一批代码在 PR #76 的同一格绿；本机 Windows 单模块连跑 **20/20 绿**。

⚠ 同一条用例里**前三个断言都过了**——任务 `completed`、结论里有 `a.py`。
失败的只是「结论回灌进了下一轮请求体」：最后一次**主对话**请求体还停在
「已启动子 Agent」那一轮。

## ⚠ 三条必读的坑（都有实测依据，别重新踩）

### 坑一：单模块跑在空闲机器上，是**验不出来**的条件

上一次修第 ③ 类问题时，那份文档自己记着：

> 单跑时机器空闲，记忆线程往往赶在下一轮主请求之前跑完（故 **18 次全绿**）；
> 全量跑几十个线程抢 CPU，它就经常落到后面（故 **3 次里红 2 次**）。

本轮那 20/20 绿正是同一个形态，**条件比「18 次全绿」还宽松**。

⟹ **判据只能是 `python -m tests.run_parallel` 满负载连跑多次**，
且**第 ③ 条要在 Linux 上验**（它只在 `ubuntu-latest / 3.13` 上红过）。

### 坑二：第 ③ 条的**旧根因已被结构性排除**，别当成同一个 bug

2026-08-20 那次（PR #52 / commit `89a43da`）修的是
`test_conclusion_delivered_exactly_once`，根因是**记忆线程被测试替身当成了主对话**
——判据原先是「**不是**子 Agent 就算主对话」这种反向写法。修法把它改成正向：

```python
is_main = stable.startswith(_MAIN_STABLE_HEAD)
if is_main:
    self.main_bodies.append(body)
```

**记忆线程现在进不了 `main_bodies` 了。** 所以本轮这条红的是**另一个成因**，
不是那次没修干净。硬套旧结论会浪费一整轮。

⚠ **那份 135 行的分析已随「做完即删」删掉了，但还在 git 历史里**，开工前读一遍：

```bash
git show f036fe2:docs/todo/1-subagent-e2e-flake.md
```

它里面「已经取样过的（别重复做）」那张表尤其有用——**「换一组模块跑」那条路
已经被证明没有信息量**，别再走一遍。

### 坑三：护栏要钉**性质**，不要钉**症状**

别用「加大等待值 / 多 `pause()` 几次 / `sleep`」把它盖过去——那只是把阈值往上挪，
CI 上更慢的机器照样会红。判据要换成**等那个状态本身**。
这条在 `CLAUDE.md` 与 commit `00a087a`（修 setup 那格 CI 偶发）里都有先例。

## 顺带值得想的一件事

`docs/todo/` 的约定是「做完即删」，于是上一次那 135 行分析在复发时**已经不在
仓库里**了——本文档是靠 `README.md` 的重排记录反推出来才找回的。
README 自己写着同一条教训（「取消/做完的东西只剩那一段，而被删的文档只能靠
**知道它存在**才找得回来」）。**第 ③ 条复发正好给了这条一个真实样本**，
做完这轮时值得顺手想想：flake 这类「可能复发」的事项，删掉之前要不要在
`docs/internals/known-issues.md` 留一条带 git 指针的存根。

---

## 一键开工 Prompt

```
先跑 `git branch --show-current`。如果在 main 上，先 `git checkout -b test-flakes` 再动手。

读 `docs/todo/4-test-flakes.md`，按它做。

⚠ 开工前先做两件事，顺序别反：

1. `git show f036fe2:docs/todo/1-subagent-e2e-flake.md` —— 上一次修同类问题的
   135 行分析（文档已随「做完即删」删除，只在 git 历史里）。里面「已经取样过的
   （别重复做）」那张表能省掉一整轮无效取样。
2. 用 `python -m tests.run_parallel` 连跑 5 次，确认三条各自的复现率——**别用
   单模块单跑**，那是上一次漏掉它的条件（实测：单跑 18 次全绿，满负载 3 次红 2 次）。

三条可能是三个独立成因，别指望一刀切完。⚠ 第 ③ 条的**旧根因已被结构性排除**
（记忆线程现在进不了 `main_bodies`），硬套旧结论会浪费一整轮。

⚠ 修法要钉**性质**不钉**症状**：改成等那个状态本身，不要加大等待值/加 sleep/
多 pause 几次——那只是把阈值往上挪，CI 上更慢的机器照样会红。

⚠ 动 tui/、tests/e2e/、subagents/ 之前先加载 `paired-maintenance` Skill。

验收：`run_parallel` 连跑至少 5 次全绿，且第 ③ 条要在 Linux 上验过
（它只在 ubuntu-latest / Python 3.13 上红过——开个 PR 让 CI 跑即可）。

做完把这份文档删掉，并按 `docs/todo/README.md` 的命名规则重排剩下的序号。
⚠ 删之前先读本文档末尾「顺带值得想的一件事」，决定要不要留存根。
```
