# 记忆文件的编码洞 —— 与 B6 同一个形态，还剩四处

> 状态：待开工 · 预计半天 · **优先级第二，因为它最便宜且修法已在仓库里**
>
> 建议分支：`memory-encoding-remaining`（从 `main` 起）
>
> ⚠️ **不要重新设计。B6 那次已经把「怎么修」和「怎么钉」都做完了，
> 这条是把同一套办法搬到隔壁四处。** 开工第一件事是读 B6 那个 PR。

## 背景：它是怎么被发现的

2026-08-31 修 B6（坏编码的 `RHINE.md` 会静默吞掉三层项目指令）时，
那个 Agent 顺手核了一遍同一个包里的其它读文件点，报告：

> `manager.py` 里读记忆文件的四处 `except OSError`（`memory_index` / `_read_index` /
> 两处 `parse_memory`）是同一个异常类别的洞。

**根因与 B6 逐字相同**：`UnicodeDecodeError` 继承自 `ValueError`，**不是 `OSError`**，
所以 `except OSError` 接不住它。

## 症状与 B6 有一处关键差别

| | B6（已修） | 本条 |
| --- | --- | --- |
| 读的是谁写的文件 | **用户**手写的 `RHINE.md` | RhineCode **自己**写的 UTF-8 记忆文件 |
| 触发难度 | 低——中文 Windows 的记事本、旧编辑器、`cmd` 的 `>` 默认就写 GBK | 较高——要用户手工用 GBK 编辑器改过 `MEMORY.md` |
| 失败形态 | **静默**吞掉三层指令 | **异常直接冒出来** |

⚠ **别因为「触发难度较高」就把它当成不要紧的。** `memory_index()` 在
**每次请求构建系统提示时**都会被调用，未捕获的 `UnicodeDecodeError` 会从那里冒出来
——也就是说一旦踩上，**每一轮对话都炸**，而不是某个功能失效。

而「用户手工改过 `MEMORY.md`」并不是什么怪异操作：那份文件的用途就是给人看、
让人改的，`docs/guide/memory.md` 也是这么说的。

## 要做什么

1. **四处 `except OSError` 逐个判断该扩成什么。** ⚠ **不要机械替换成
   `(OSError, UnicodeDecodeError)` 了事**——B6 那次的结论是「写法刻意不统一」：
   窄写法用在「try 块里只有一次 `read_text`、失败形态只有两种」的地方；
   宽的 `ValueError` 只用在 `_safe_resolve` 那种「失败形态本身不是解码」的地方。
   逐处照这个判据判，并把理由写进注释。
2. **想清楚接住之后做什么。** 这是本条真正的设计点，B6 的答案未必能照搬：
   `RHINE.md` 那边的答案是「落回该层的 `layer.errors`，另外两层照常生效」；
   记忆这边一份坏文件是该**跳过它继续**（记忆是「有更好、没有也能跑」的东西），
   还是该**明确告诉用户这条记忆读不出来**？倾向前者 + 一条启动提示，
   但请自己核一遍 `docs/guide/memory.md` 里对失败的既有说法再定。
3. **护栏**：至少钉「一份 GBK 的 `MEMORY.md` 不会让请求炸掉」与
   「用户看得见发生了什么」两条，并**各做一次变异实测**（撤掉修复确认真的会红）。
4. **成对维护点**：B6 已在 `paired-maintenance` Skill 里登记了
   「`instructions.py` 的三个 `except` ↔ `manager.py` `startup()` 的兜底对象」。
   本条修完要看那条要不要扩写——如果记忆这侧也有「已知失败 vs 没想到的失败」
   两层结构，就是同一条的第二个落点。

## 判据

- 一份 GBK 编码的 `MEMORY.md` 存在时：请求正常完成，且用户看得到一条说明
- 一切正常时**不多冒任何提示**（反向反证，防「无条件报一句」的实现）
- 四处的 `except` 各有一条注释说明为什么是这个宽度
- `python -m tests.run_parallel` 全绿

## 要读的文件

- **先读 B6 那次的改动**（PR #68 / 分支 `fix-rhine-md-encoding`，4 个 commit）——
  修法、护栏写法、变异实测的做法全在里面
- `docs/review/02-robustness.md` 的 **R4-1**（B6 的原始分析，含三组对照实跑输出）
- `rhinecode/memory/manager.py` 那四处
- `docs/guide/memory.md`（对失败的既有说法）

## 一键开工 Prompt

```
先跑 git branch --show-current，如果在 main 上就先 git checkout -b memory-encoding-remaining。
动 memory/ 之前先加载 paired-maintenance Skill。

我要修 docs/todo/2-memory-encoding-remaining.md 记的这件事：
rhinecode/memory/manager.py 里读记忆文件的四处 except OSError
（memory_index / _read_index / 两处 parse_memory）接不住 UnicodeDecodeError
——它继承自 ValueError，不是 OSError。

⚠ 这是 2026-08-31 修 B6 时顺手查出来的同型问题，**修法已经在仓库里了**：
先去读 PR #68（分支 fix-rhine-md-encoding）那 4 个 commit，
把那次的修法、护栏写法、变异实测做法照搬过来。
原始分析在 docs/review/02-robustness.md 的 R4-1。

⚠ 三条别踩的坑：
1. 不要机械替换成 (OSError, UnicodeDecodeError)。B6 的结论是「写法刻意不统一」
   ——窄写法用在「try 里只有一次 read_text」的地方，宽的 ValueError 只用在
   「失败形态本身不是解码」的地方（比如 Path.resolve 在 Windows 上遇畸形路径）。
   逐处判，理由写进注释。
2. 接住之后做什么是本条真正的设计点，B6 的答案未必能照搬。记忆是「有更好、
   没有也能跑」的东西，倾向「跳过坏的那份继续 + 一条启动提示」，
   但先核一遍 docs/guide/memory.md 里对失败的既有说法。
3. 别因为「要用户手工用 GBK 改过 MEMORY.md 才触发」就当成不要紧——
   memory_index() 在每次构建系统提示时都会调，踩上就是**每一轮对话都炸**。

护栏至少两条（GBK 的 MEMORY.md 不让请求炸掉 / 用户看得见发生了什么），
外加一条反向反证（一切正常时不多冒提示）。每条都要做变异实测：
撤掉修复确认测试真的会红，再恢复。一条「改坏了也不红」的测试等于没写。

做完看一眼 paired-maintenance Skill 里 B6 登记的那条要不要扩写。

约束：走分支 + PR，跑 python -m tests.run_parallel 确认全绿。
```
