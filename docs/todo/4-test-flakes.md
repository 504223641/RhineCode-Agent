# 4 · 四条并行/满负载下的偶发红

> 建议分支：`test-flakes`
> 复杂度：中（**先定性，再决定改不改**；四条可能是四个独立成因，别指望一刀切完）

## 为什么值得单独立项

前三条都不是「产品坏了」，都是**测试里藏了时间假设**：断言跑在某个异步的东西还没
落位的时刻，机器一忙就跨线。危害不在于它们会红，在于**它们训练所有人忽略失败**
——「跑一遍全量确认没回归」这个动作只要被污染，之后每一次真实回归都可能被当成
「又是那几条老毛病」放过去。

⚠ **第 ④ 条是 2026-09-17 追加的，它与前三条性质不同**：根因**已定位、已在本机
构造式复现**，是**观测设施自己的一个真实健壮性缺口**——trace 阅读器读一个正在
被追加写的文件，末行断在汉字中间就抛异常。它躺在这份 flake 清单里只因为它
**表现**为偶发红；**它不需要再取样定性，可以直接修。**

⚠ **第 3 条是复发**，2026-08-20 专门立项修过一次（见下），这本身就是信号。

## 四条各是什么

### ① `tests/test_setup_screen.py::ModelStepTest::test_option_list_gets_focus`

```python
await self.fill_credentials(pilot, screen)
self.assertTrue(screen.query_one("#setup-models", OptionList).has_focus)
```

**取样（2026-09-17，Windows 11 / Python 3.11）**：单条单跑 3/3 绿；整模块单跑绿；
`run_parallel` 八分片下**干净 main 上 3 次红 2 次**；串行满量 `discover` 也复现过一次。

断言紧跟在填完凭据、切到第三屏之后，等的是 Textual 把焦点推到列表上。

⚠ **2026-09-18 复测：它在 `context-budget-realign` 分支上几乎是确定性的——
连跑 4 次红 4 次，而同一天同一台机器上 `main` 跑 1 次绿。**
两条分支的产品代码差异与本用例毫无关系（改的是 c8 与三个工具的输出预算），
唯一变的是**测试模块总数**（3756 → 3764），而 `run_parallel` 按模块分片，
于是 `test_setup_screen` 的**同片邻居换了一批**。

这条观察对修它很有用，有两层：

① **「难以复现」这个前提已经不成立了**——在那条分支上它是个稳定的红，
可以直接调试，不必再靠 `run_parallel` 连跑碰运气。
② 它同时是「坑一」（单模块跑在空闲机器上验不出来）的一个**更强的样本**：
决定它红不红的不是机器忙不忙，而是**恰好和谁跑在同一片里**。
因此修法判据除了「连跑多次全绿」，还应当包括「在那条分支上也绿」。

⚠ 别把这当成那条分支引入的回归——它在 `main` 上早就红过（见上面 2026-09-17
那次取样），分支只是把它的复现率推高了。

### ② `tests/test_e2e_ask_user.py::AskUserE2ETest::test_the_input_box_is_only_unlocked_in_the_free_text_state`

```python
self.assertFalse(bar.disabled, "自由输入态下输入框必须解禁")
```

**取样**：整模块单跑 8 项全绿；单条单跑 3/3 绿；`run_parallel` 下红过。

⚠ **2026-09-18 补：同一个类里的 `test_other_is_a_checkbox_and_typing_returns_to_the_list`
也是同一个形态**（`run_parallel` 下红一次，随后整模块单跑 3/3 全绿）。
也就是说这条不是某一个断言的问题，而是 **`AskUserE2ETest` 整个类在并行负载下
会偶发**——修的时候别只盯那一条断言，先找这个类共用的等待/时序假设。

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

### ④ `tests/test_e2e_control.py::DeadlockGuardTest::test_concurrent_snapshot_while_driving`

```
AssertionError: Lists differ:
  [UnicodeDecodeError('utf-8', b'\xe9', 0, 1, 'unexpected end of data')] != []
```

**取样（2026-09-17）**：CI 的 `ubuntu-latest / Python 3.12` 红（PR #77），
**同 commit 重跑那一格绿**；同 commit 的 `ubuntu 3.11` / `ubuntu 3.13` 与三个
windows 格全绿。

⚠ **与前三条不同，这一条不必再取样——根因已定位，且在本机构造式复现。**

**根因**：那条用例开一个线程每 10 ms 调一次 `core.snapshot()`，而 `snapshot`
会走 `_trace_seq()` → `reader.load_records()` 去读**正在被追加写**的 trace 文件。
`load_records` 是这么读的：

```python
with open(path, "r", encoding="utf-8") as fh:
    for line in fh:
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            skipped += 1          # 「半截 JSON」兜住了
```

**它兜住了「半截 JSON」，没兜住「半截汉字」。** 解码发生在 `for line in fh`
那一步，**比 `json.loads` 更早**，所以那个 `except` 压根轮不到；而本项目的
trace 负载里全是中文（工具摘要、`ui_message` 正文），写入被拆成两次系统调用时
断点落在一个汉字的三个字节中间是常态（`0xe9` 就是某个汉字的首字节）。
调用方 `_trace_seq` 只捕 `OSError`，而 `UnicodeDecodeError` 是 `ValueError`
的子类——于是它一路逃到轮询线程的 `errors` 里。

**本机复现**（产出的异常与 CI 那条逐字相同）：把下面这段存成脚本跑，
**别用 shell heredoc**——实测 heredoc 会把 `\x` 转义吃掉，复现不出来：

```python
import tempfile, pathlib
from rhinecode.trace import reader

p = pathlib.Path(tempfile.mkdtemp()) / "t.jsonl"
whole = b'{"seq": 1, "type": "x", "payload": {"text": "\xe8\xaf\xbb"}}\n'
half = b'{"seq": 2, "type": "x", "payload": {"text": "\xe9'   # 半个汉字，后两字节还没落盘
p.write_bytes(whole + half)
reader.load_records(p)   # → UnicodeDecodeError('utf-8', b'\xe9', 0, 1, ...)
```

**建议修法**：照 `memory/session.py` 的 `_iter_archive_lines` 先例——**按二进制读、
逐行自己解码**，解不出来的那一行 `skipped += 1`（与现有「半截 JSON 跳过并计数」
**同语义**，不静默丢内容）。

⚠ **别用 `open(..., errors="replace")` 简化。** `paired-maintenance` 里
`session.py` 那条记着为什么：JSONL 的结构部分全是 ASCII，替换字符不影响
`json.loads` 成功，于是会产出一条**内容是替换字符的记录、并当成好记录读出来**
——对 trace 更糟，它是证据。

⚠ **护栏要钉性质不钉症状**：用例应当直接构造「末行断在汉字中间」的文件去调
`load_records`（像上面那段那样），**不要靠并发碰运气**。并且要有一条断言钉住
`skipped` 计到了 1——否则一个「出错就返回空列表」的 fail-open 实现照样全绿，
而那是把整份证据丢掉。

⚠ 顺带核一遍三个调用方的容错口径：`_trace_seq`（只捕 `OSError`）、
`observe`（docstring 写着「末行半截不算损坏」，那句话现在只对一半成立）、
以及 CLI 入口。

## ⚠ 三条必读的坑（都有实测依据，别重新踩）

### 坑一：单模块跑在空闲机器上，是**验不出来**的条件

上一次修第 ③ 类问题时，那份文档自己记着：

> 单跑时机器空闲，记忆线程往往赶在下一轮主请求之前跑完（故 **18 次全绿**）；
> 全量跑几十个线程抢 CPU，它就经常落到后面（故 **3 次里红 2 次**）。

本轮那 20/20 绿正是同一个形态，**条件比「18 次全绿」还宽松**。

⟹ **判据只能是 `python -m tests.run_parallel` 满负载连跑多次**，
且**第 ③ 条要在 Linux 上验**（它只在 `ubuntu-latest / 3.13` 上红过）。

⚠ **这条坑只管 ①②③；第 ④ 条正相反**：它有确定的构造式复现，拿 `run_parallel`
去碰它出现的概率，反而是这四条里最没有信息量的一种判据。

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

四条可能是四个独立成因，别指望一刀切完。⚠ 第 ③ 条的**旧根因已被结构性排除**
（记忆线程现在进不了 `main_bodies`），硬套旧结论会浪费一整轮。

⚠ **第 ④ 条不必取样，可以先修掉**（根因已定位，文档里带了构造式复现与建议修法）。
先把它拿掉，CI 那一格不再随机红，后面三条取样时的噪声也少一个。

⚠ 修法要钉**性质**不钉**症状**：改成等那个状态本身，不要加大等待值/加 sleep/
多 pause 几次——那只是把阈值往上挪，CI 上更慢的机器照样会红。

⚠ 动 tui/、tests/e2e/、subagents/ 之前先加载 `paired-maintenance` Skill。

验收：`run_parallel` 连跑至少 5 次全绿，且第 ③ 条要在 Linux 上验过
（它只在 ubuntu-latest / Python 3.13 上红过——开个 PR 让 CI 跑即可）。

做完把这份文档删掉，并按 `docs/todo/README.md` 的命名规则重排剩下的序号。
⚠ 删之前先读本文档末尾「顺带值得想的一件事」，决定要不要留存根。
```
