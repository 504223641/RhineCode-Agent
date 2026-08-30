# R7 · 文档漂移的根治方案

> 2026-08-31。对应 [`README.md`](README.md) 的 **D 节**（4 条漂移实例）。
> **本轮只出方案、不改任何文档里的数字**——那是 F6 的事。
> 所有结论都带 `文件路径:行号`，所有数字都是本轮现测的（测法附在文末）。

---

## 摘要

**D 节把四条实例并列成「同一个机制的产物」，这个判断只对了一半。**
逐条拆开之后，它们是 **五种成因不同的病**，其中 **只有两种能靠「数字自动生成」治**：

| 病 | 是什么 | 对应实例 | 自动生成治得了吗 |
| --- | --- | --- | --- |
| ① | 一个事实 N 份拷贝，没有一份是「源」 | D2 | ✅ 能 |
| ② | **计数口径没有定义**，两个数都对 | D1、D4 | ❌ **不能**——得先有人裁定口径 |
| ③ | 护栏的作用域比事实小，失败信息把剩下的事交回给人 | D2 | ✅ 能（扩大作用域） |
| ④ | **自检命令的匹配式与漂移的实际形态对不上** | D3 | ❌ 换一条判定规则 |
| ⑤ | 修复只落在被改的那一层，净效果是拷贝变多 | 全部 | 只能靠分层治 |

**本轮最要紧的三个实测结果**：

1. **`docs/todo/README.md:21` 那条官方自检命令，在一棵有 2 处真悬空引用的树上输出
   6 行、全部正确、退出码 0。** 也就是说 D3 的成因**不是「没人记得跑」**——
   跑了也查不出来，而且会拿到一份看起来很干净的报告。
2. **D3 的真实规模是 10 处不是 2 处**，其中 3 处在**审查报告自己**的文件里，
   1 处在 **2026-08-30 昨天新建**的 `docs/guide/roadmap.md:59`。
3. **D2 的护栏其实已经存在，而且一直是绿的**——`tests/test_trace_models.py:42`
   把枚举成员数与 docstring 里的中文数字钉在一起。它治住了 `trace/models.py`
   这一个文件，然后在失败信息里**写了一句「并且同步 CLAUDE.md」**
   （`tests/test_trace_models.py:52-54`）。那句话就是把「靠人记得」原样搬了一层。
   **CLAUDE.md 至今还写着「二十九类」。**

**方案是三件互相独立的事**，可以分三个 PR 落地：

| # | 做什么 | 治哪几条 | 代价 |
| --- | --- | --- | --- |
| **P1** | `tests/test_docs_facts.py`：事实登记表，数字**由代码现算**、文档里的说法当场比对 | ①③ | ~180 行 + 每条事实 3 行登记 |
| **P2** | `tests/test_docs_links.py`：`docs/todo/` 序号引用 + markdown 链接可解析 + 扩展索引完整 | ④ | ~110 行，**零登记** |
| **P3** | 文档分四层，给每层写明「谁维护、什么时候更新、允许落后多少」 | ⑤ | 一次搬迁，无长期成本 |

⚠ **P1 / P2 的代码草案不是纸上谈兵**：两份都在本轮**真的落进 `tests/` 跑过**
（跑完即删，不进本次提交），**合计红出 12 处真问题、零误报**——
逐处清单在 §3.3 与 §4.4。跑的过程还逼出两条第一版没有的过滤，见 §6 末的 ⚠。

**P1 之前必须先做一件人的事**：裁定 D1 与 D4 的口径（§2.2）。
不裁定就写不出断言——这不是方案的缺陷，**是这个方案把一直藏着的歧义顶到了台面上**。

---

## 一、根因：四条实例背后是五种不同的病

### 病① 一个事实 N 份拷贝，没有一份是「源」

**D2 的实测分布**（真值 = `len(TraceEventType)` = **31**）：

| 位置 | 写的是 | 对不对 |
| --- | --- | --- |
| `rhinecode/trace/models.py:27`（枚举 docstring） | 三十一类 | ✅ |
| `README.md:126` | 三十一类 | ✅ |
| `docs/guide/features.md:32` | 三十一类 | ✅ |
| `docs/guide/project-structure.md:47` | 三十一类 | ✅ |
| `CLAUDE.md:104` | **二十九类** | ❌ |
| `docs/internals/architecture.md:54` | **二十七类** | ❌ |

**同一个整数活在 6 个地方，其中 2 个过期，而且过期的是给 AI 读的那两份。**
写这个数字的人每次只改自己手上那一份——这不是不细心，是**没有任何东西告诉他还有五份**。

⚠ **同一行还藏着一处更严重的、D 节没查出来的漂移**：
`docs/internals/architecture.md:54` 写「作用域**六种**：`main` / `summary` /
**`notes`** / `web_extract` / `isolated:` / `subagent:`」。而代码里
（`rhinecode/trace/models.py:98-105`）**没有 `notes` 这个作用域**（真名是
`memory`），**也漏了 c16 新增的 `classifier`**。真实形态是 5 个固定常量
（`main` / `summary` / `memory` / `web_extract` / `classifier`）+ 2 个动态前缀。

**这一条给方案定了一个硬要求：只比对「个数」是不够的。**
一次「加一个、删一个」会让计数纹丝不动而内容全错；
而漂移最常见的形态恰恰是**新增了一个、忘了改说法**。
所以 P1 的断言要能比对**成员名清单**，不只是 `len()`。

### 病② 计数口径没有定义——这是自动生成治不了的那一半

**D1 的真相不是笔误：「七」和「八」都对，看你数什么。**

| 数什么 | 位置 | 结果 |
| --- | --- | --- |
| `_audit_one` 里调了几个检查函数 | `rhinecode/skills/audit.py:126-132` | **7** |
| `AdviceKind` 有几个成员 | `rhinecode/skills/models.py:98`（枚举 docstring 自己写「体检的**八项**检查各一个成员」） | **8** |

差在 `_check_grants` 一个函数里产出**两种**建议
（`GRANTS_ALL_DROPPED` 与 `BROAD_GRANT`，`audit.py:390` 与 `:434`）。

于是文档里那 20 多处「七项 / 八项」不是「一半写错了」，是**两派各自自洽**：
`docs/extensions/skill-authoring/plan.md:64` 写「八项检查各一个成员」（数枚举，对），
`docs/extensions/skill-authoring/acceptance.md:34` 写「七项检查」（数函数，也对），
`CLAUDE.md:98` 与 `:263` 一处八一处七——**同一份文件里两个口径都用了**。

> ⚠ **F6 的一键 Prompt 里现在写着「D1 八项/七项：改为『七项』」**
> （`NEXT.md:1013`）。照这句话执行会把 `skills/models.py:100` 那句
> 「八项检查各一个成员」变成错的——**它数的是枚举，而枚举确实是 8 个**。
> 先裁口径，再改数字。

**同一种病还有第二个实例，D 节没发现**：Hook 的加载期校验。
`rhinecode/hooks/parser.py:33` 写「加载期校验**七项**（spec F8 的 1–7）」，
而 `docs/c12/plan.md:15`、`docs/c12/task.md:16`、`docs/c12/checklist.md:21`
等处都写「**八项**校验」。同样是「实现合并了两项 / 或 spec 拆得更细」，
同样是两派各自自洽。

**D4 是这种病的极端形态：「叶子包」这个词有三种量法，三种答案都不一样。**

本轮用 AST 与运行期两种方式各量了一遍：

| 包 | 文档的说法 | 量法 A：`import <包>` 的运行期闭包 | 量法 B：包内任一模块的模块级 import |
| --- | --- | --- | --- |
| `trace` | 「叶子包（**只依赖标准库**）」（`CLAUDE.md` 架构表、`architecture.md:54`） | ✅ 无 | ❌ `provider` |
| `web` | 「叶子包」 | ✅ **无** | ❌ `permission` / `provider` / `tools` / `trace` |
| `skills` | 「叶子包」 | ❌ `permission` / `tools` / `trace` | ❌ `permission` / `trace` |
| `team` | 「叶子包」 | ⚠ `trace` | ⚠ `trace` |
| `todo` | 「只依赖标准库与 `trace`」 | ✅ 相符 | ✅ 相符 |
| `classifier` | 「只依赖 `provider.base` 与 `trace`」 | ✅ 相符 | ✅ 相符 |

**`web` 那一行是这张表的核心**：D 节说它「实际 import `permission`/`provider`/
`tools`/`trace`」——**在量法 B 下对，在量法 A 下它是全项目最干净的叶子之一**
（`rhinecode/web/__init__.py` 不 re-export，`import rhinecode.web` 一个兄弟包都拉不起来）。
两个人各拿一种量法，会得出完全相反的结论，而**谁都没写错**。

⚠ 注意最后两行：`todo` 与 `classifier` 的说法**精确且经得起两种量法**，
因为它们没用「叶子包」这个词，而是**直接写出了依赖谁**。
**这就是修法**——不是把「叶子包」这个词的定义写清楚，是**别用这个词**。

> **R6 已经在另一个数字上撞过同一堵墙**：`05-maintainability.md` 记着
> 「强连通分量是 10 还是 11 取决于两个口径」，而 `context` 之所以进环，
> 恰恰因为 `agent → context` 那条边**只存在于 `TYPE_CHECKING` 里**——
> **作者为了不进环而做的事被扫描器算成了一条真边。**
> 这是第三种量法。R7 与 R6 撞的是同一个根因，不是两件事。

### 病③ 护栏的作用域比事实小，失败信息把剩下的事交回给人

**D2 有护栏，而且它一直是绿的。**

`tests/test_trace_models.py:42` 的 `test_docstring_count_matches_actual_members`
把中文数字与 `len(list(TraceEventType))` 现场比对——**它是本项目里已经存在的、
最接近本轮方案的东西**，而且它的立意写得很清楚（`:46-49`）：

> 这个数字漂移过两次——docstring 停在「十九类」、CLAUDE.md 停在「二十三类」……
> 它是纯文字、漏改不报错，于是每一章都往下带一次错。

然后是它的失败信息（`tests/test_trace_models.py:52-54`）：

> 读到这条用例失败时：改 `TraceEventType` 的 docstring，
> **并且**同步 `CLAUDE.md` 里「二十三类结构化事件」那句
> （那一处没有护栏，只能靠这里提醒）。

**这句话是本轮最有信息量的一处证据。** 写它的人：
① 知道 CLAUDE.md 有一份拷贝；② 知道那份没有护栏；③ 选择用一句提示文本兜底。
结果是——`models.py` 的 docstring 至今准确（护栏管着），
**`CLAUDE.md:104` 至今是「二十九类」，而且提示文本里那句「二十三类」自己也过期了。**

⚠ **教训不是「那条测试没用」**，它治住了它覆盖的那个文件。
教训是：**护栏的作用域必须等于事实的作用域。**
一条只管一个文件、却在失败信息里点名另外五个文件的断言，
等于把「靠人记得同步」从代码里挪到了报错信息里，**一步都没往前走**。

同一节里还有第二个证据：`tests/test_trace_models.py:32` 的
`assertEqual(len(members), 31)`。这是**硬编码的期望值**——它是一条
「变更检测器」（改枚举必须来这里确认一次），性质与比对不同，**要保留**。
但它说明一件事：**同一个文件里，硬编码的那条和现算的那条并存**，
而漂移出现在第三个地方——两条都管不着的 `CLAUDE.md`。

### 病④ 自检命令的匹配式与漂移的实际形态对不上（D3）

`docs/todo/README.md:14-21` 明确写了重排后的自检动作：

```bash
grep -rn "docs/todo/[0-9]\|第 [0-9] 条 todo" docs/todo/
```

**本轮在当前这棵（有 2 处真悬空的）树上原样跑了一遍**：

```
docs/todo/1-skill-recall-eval.md:121:再读 docs/todo/1-skill-recall-eval.md 的完整背景与已知的坑。
docs/todo/1-skill-recall-eval.md:123:做完这条后把 docs/todo/1-skill-recall-eval.md 删掉，……
docs/todo/2-delegation-trigger-eval.md:74:⚠ **取样方法比实现更重要**——第 3 条 todo（P1b）记着同一条教训：
docs/todo/2-delegation-trigger-eval.md:113:读 docs/todo/2-delegation-trigger-eval.md 与 CLAUDE.md ……
docs/todo/3-p1b-unattended.md:96:⚠️ 开工前必须先读 docs/todo/3-p1b-unattended.md 里……
docs/todo/3-p1b-unattended.md:117:做完这条后把 docs/todo/3-p1b-unattended.md 删掉，……
```

**6 行，逐行核对全部正确，退出码 0。而两处真悬空一处都没出现在里面。**

原因是那两处的**书写形态**不在正则的覆盖里：

| 位置 | 原文形态 | 为什么漏 |
| --- | --- | --- |
| `docs/todo/2-delegation-trigger-eval.md:6` | ``[`3-skill-recall-eval.md`](3-skill-recall-eval.md)`` | **相对链接**，没有 `docs/todo/` 前缀 |
| `docs/todo/1-skill-recall-eval.md:70` | ``这条与 `4-p1b-unattended.md` 里记的……`` | 反引号里的**裸文件名**，也没有前缀 |

**所以 D3 的诊断要改**：不是「自检不够」，也不是「没人记得跑」——
是**跑了会得到一份看起来很干净的报告**。一个认真执行了流程的人，
会拿着这 6 行确认「全对」，然后带着两处悬空提交。

⚠ 还有第二层：**那条 grep 只列行，不说什么叫对。** 即使正则覆盖到了，
人也得把每一行的数字与 `ls docs/todo/` 的结果逐个对一遍——
**判定仍然在人脑里**，命令只负责把候选行捞出来。这类「捞出来给人看」的自检，
在候选行全对的时候提供的是**虚假的安心感**。

### 病⑤ 修复只落在被改的那一层，净效果是拷贝变多

**这条病在本轮审查期间当场发作了一次，有完整证据链。**

`NEXT.md:961-966`（F5 的一键 Prompt，写于 2026-08-22）列出了要顺手清掉的错误，
其中就包括「八项检查应为七项」与「二十七类 trace 事件应为 31」。
F5 于 **2026-08-30** 执行（commit `660fbf1` / `e2a1b44`），
`docs/guide/README.md:16-19` 明确记着它「顺手清掉了当时已知的十处事实错误」。

**执行得没有问题。问题是作用域**：

- ✅ `README.md:126` 改对了（三十一类）
- ✅ 新建的 `docs/guide/features.md:32`、`project-structure.md:47` 写对了
- ❌ `CLAUDE.md:104` 没动（还是二十九类）
- ❌ `docs/internals/architecture.md:54` 没动（还是二十七类）

**净效果**：这个事实的拷贝数从 4 份变成 **6 份**，正确率从 2/4 变成 4/6。
**漂移没有被关闭，它被稀释了。**

⚠ **同一次改动还新造了一处漂移**：昨天新建的 `docs/guide/roadmap.md:59`
引用 `3-delegation-trigger-eval.md`——那个序号在 **2026-08-20** 就已经改成 `2-` 了。
**一份 24 小时前写的文档，出生时就带着 11 天前的过期引用。**

这条病解释了 D 节那句「靠人记得同步」为什么不完整：
**人记得的时候也治不了**，因为「同步」的对象集合本身不可见。
F5 的 Prompt 里那两条已经是「有人替他列好了」的最好情况，仍然只落地了一半。

### 本轮新查出的 8 条漂移

D 节四条之外，用同一批方法扫出来的（D11 / D12 是 §3.3 与 §4.4 的护栏原型实跑抓出的，本节初稿里没有）：

| # | 级别 | 问题 | 证据 |
| --- | --- | --- | --- |
| **D5** | 🟡 | **`docs/extensions/README.md` 的索引表漏了两个扩展**——`auto-plan/` 与 `ask-user/` 各有完整的 spec/plan/task/checklist，而它们自己的索引里**一次都没出现**（全文搜 `auto-plan` / `ask-user` 均 0 命中）。表里 7 行，目录下 9 个 | `docs/extensions/README.md:43-49` vs `ls docs/extensions/` |
| **D6** | 🟡 | Hook 加载期校验「七项 vs 八项」——与 D1 同一种病的第二个实例 | `rhinecode/hooks/parser.py:33`（七项）vs `docs/c12/plan.md:15`、`task.md:16`、`checklist.md:21`（八项） |
| **D7** | 🟡 | **CI 配置的注释里也有一份测试条数**，写「3323 条」 | `.github/workflows/ci.yml:10`。今天 `discover` 实测 **3349** |
| **D8** | 🟡 | `CLAUDE.md:328` 写「3323 项」，实测 3349。⚠ 该文件同一节还挂着**一整段**关于这个数字反复对不上的自省（`:340-372`，含三组已作废的历史数字），**那段自省本身现在也过期了** | `python -m unittest discover -s tests` 现测 3349 |
| **D9** | 🟡 | trace **作用域**的名字与个数都不对：文档写「六种」并列出不存在的 `notes`，漏了 `classifier` | `docs/internals/architecture.md:54` vs `rhinecode/trace/models.py:98-105` |
| **D10** | 🟡 | **审查报告自己的 D2 描述已经过期**：写「README 两处『二十七类』」，而 README 已于 2026-08-30 改对，现在的两处错误在 `CLAUDE.md` 与 `docs/internals/architecture.md` | `README.md:423` vs 实测 |
| **D11** | 🟡 | **昨天新建的用户手册里已有一处 D1**：`docs/guide/skills.md:75` 写「七项检查」 | 由 §3.3 的护栏原型实跑抓出（写本节时没预料到） |
| **D12** | 🟡 | `docs/review/05-maintainability.md` 有 **5 处坏链**（`:815`、`:949`、`:971`、`:973`、`:975`），都是「在子目录里写了从仓库根出发的路径」 | 由 §4.4 的护栏原型实跑抓出 |

⚠ **D10 值得单独说一句**：一份 9 天前写的审查报告，已经有 3 处引用了过期的
todo 序号（`README.md:82`、`:424` 两处）、1 处描述与现状不符。
**审查报告与被审查的文档服从同一个物理规律。** 这不是讽刺，是本节论点的最强证据：
只要「拷贝 + 靠人同步」这个结构还在，**谁写都一样**。

---

## 二、Q1：哪些数字该由代码算出来？逐个判断

### 2.1 判据

一个数字值得接进护栏，要同时满足三条：

1. **有唯一的机器可读事实源**——能用一个表达式算出来，而不是「数一数代码里有几处」。
2. **口径唯一**，或者能被一句话钉死。这是最容易被跳过、也最容易翻车的一条（见 §2.2）。
3. **文档里出现 ≥ 2 次**，或**出现在会被 AI 读的文件里**。
   只出现一次、且在一份历史 spec 里的数字，接护栏是负收益。

### 2.2 ⚠ 前置：两个口径必须由人先裁定，代码替不了

**这两条不裁定，P1 就写不出来。给出建议与理由，最终由你拍板：**

**① Skill 体检是几项？**

| 选 | 含义 | 好处 | 代价 |
| --- | --- | --- | --- |
| **8（建议）** | 数 `AdviceKind` 成员 | 事实源唯一且稳定（一个 Enum）；用户在 `/skills` 报告里**能看到几种不同的建议**，数的正是这个；`skills/models.py:100` 现有说法不用改 | 要改 `CLAUDE.md:263` 等约 10 处「七项」 |
| 7 | 数 `_audit_one` 里的函数调用 | 与 `audit.py` 的实现结构对齐 | 事实源是「AST 里数函数调用」，脆弱；且这个数**对用户没有意义**——他感知不到两种建议出自同一个函数 |

**建议选 8**，理由是第二条：**文档里的计数是写给读者的，应该数读者能观察到的东西。**
用户看到的是 8 种不同的建议，不是 7 个函数。
选定后 `audit.py:126-132` 上方加一行注释说明「7 个函数产出 8 类建议，计数以枚举为准」。

**② Hook 加载期校验是几项？** 同一套逻辑，但方向相反：这里**没有枚举**，
`hooks/parser.py:33` 的「七项（spec F8 的 1–7）」是唯一的机器可近的锚点，
而 spec 的「八项」是需求侧的拆法。**建议：文档统一写「spec F8 的八项需求，
实现合并为七处校验」**，并且**不接护栏**——它没有稳定事实源，接了会变成
「数 `if` 分支」这种一改就红的假护栏。

**③ 「叶子包」怎么算？** **建议：这个词从文档里退役。**
理由在 §1 病② 那张表的最后两行已经给出：`todo` 与 `classifier` 的说法
（「只依赖标准库与 `trace`」/「只依赖 `provider.base` 与 `trace`」）
**在两种量法下都成立，因为它们写的是依赖清单而不是一个形容词**。
改法是把架构表里的「叶子包」换成实际依赖清单，并由 P1 用 AST 量法 B 钉住
（B 比 A 严格：A 只看 `__init__.py` 写没写 re-export，
一个包可以靠「`__init__.py` 留空」拿到叶子身份，而那是**打包决定不是架构性质**）。

### 2.3 逐个判断

| 数字 | 真值（本轮现测） | 事实源 | 口径 | 该不该接 | 说明 |
| --- | --- | --- | --- | --- | --- |
| **trace 事件类数** | 31 | `len(list(TraceEventType))` | 唯一 | ✅ **接，且要比对成员名清单** | D2/D9 的正主。只比个数挡不住「加一个删一个」，也挡不住 `notes` 这种名字错 |
| **trace 作用域** | 5 常量 + 2 前缀 | `SCOPE_*` 常量名 | 唯一 | ✅ **接（名字清单）** | D9。个数与名字都错过 |
| **Hook 事件数** | 12 | `len(list(HookEventType))` | 唯一 | ✅ 接 | 目前**全仓 8 处全对**，接护栏是为了它**保持**对 |
| **Skill 体检项数** | 8（枚举） | `len(list(AdviceKind))` | ⚠ **需先裁定** | ✅ 接（裁定后） | D1 |
| **斜杠命令数** | 15 | `CommandSpec(` 字面量数，或运行期构造注册表 | 唯一 | ✅ 接 | `docs/guide/README.md:26` 写「全部 15 条」，现在是对的 |
| **内置工具数** | 15 | `Tool` 子类数（AST） | 唯一 | 🟡 可接 | 文档少有明写，收益低于上面几条 |
| **内置角色数** | 3 | `ls rhinecode/subagents/builtin/*.md` | 唯一 | ✅ 接（**名字清单**，成本近乎为零） | |
| **扩展数** | 9 | `ls docs/extensions/*/` | 唯一 | ✅ **接，而且是「索引完整性」而不是计数** | D5：索引表少两行。比对「目录集合 == 表格行集合」比比对个数强得多 |
| **包依赖 / 叶子包** | 见 §1 表 | AST 扫模块级 import | ⚠ **需先裁定量法** | ✅ 接（裁定后用量法 B） | D4 |
| **测试条数** | 3349 | `discover` 的 `countTestCases()` | 唯一 | ❌ **不接，改为不写具体数** | 见 §2.4 |
| **各章测试条数**（`docs/c*/acceptance.md` 里那些「35 条 / 39 项」） | — | 无 | — | ❌ 不接 | 它们是**验收当时的快照**，是历史记录不是当前事实。改成现值反而是篡改证据 |
| **「94.7% 时间 / 620 条 / 2703 条」**（`CLAUDE.md:346-372`） | — | 口径本身不稳定 | ❌ | ❌ 不接 | `CLAUDE.md:363-372` 自己已经论证过「按耗时切」这个口径会随机器负载与性能优化漂移。**它诚实地记下了根因却没有改口径**——建议按它自己给的方案（按「是否真起进程」打标）重量一次，或整段降级为「大部分时间花在真起子进程上」的定性说法 |

### 2.4 ⚠ 测试条数：唯一一个建议「不接护栏」的高频漂移项

它满足全部三条判据，但**不该接**，理由是**变更频率**：

- 它**每次加用例都会变**。接了护栏，等于每个加测试的 PR 都要顺手改一次文档数字——
  这正是 CI 最讨厌的那种「机械噪声门禁」，而人对噪声门禁的标准反应是**放宽断言**，
  最后护栏名存实亡。
- 它现在有 **3 份拷贝**（`CLAUDE.md:328`、`.github/workflows/ci.yml:10`，
  以及 `CLAUDE.md:340-372` 那一整段自省里的历史值），**每一份都过期了**。

**建议改成「不写具体数」**：`CLAUDE.md:328` 的注释改为
`# 全量；条数以 discover 输出为准，skipped 4`，CI 注释同理。
**要看条数的人跑一次就有，而写在文档里的那份从定义上就是过期的。**

> ⚠ **这一条与 P1 的关系要说清楚**：P1 的价值来自「数字很少变、拷贝很多」
> ——枚举成员数几个月才动一次，而拷贝有 6 份。
> 测试条数正好相反（天天变、拷贝 3 份），**同一个机制在它身上是负收益**。
> 判断一个数字要不要接护栏，看的是 **变更频率 ÷ 拷贝数**，不是「能不能算出来」。

---

## 三、Q2：生成方式怎么选

### 3.1 三个候选逐个评

**候选 A：跑一个脚本改文档（生成式）**

- ❌ **否决。** 它要求脚本能定位并重写文档里的一段中文，
  等于给 200 份 markdown 装一个自动改写器。改写器出错时**没人会发现**
  （它改的是文档，而文档没有测试），而本项目通篇最忌讳的形态就是「静默降级」。
- 还有一个更实际的理由：**CLAUDE.md 的价值在于它的行文**——
  那些「⚠ 这一条已被 XX 推翻，原文保留在下面供追溯」的段落是人写的判断，
  不是可生成的模板。往里面塞生成块会让它变成一份不敢手改的文件。

**候选 B：文档里放占位符 + CI 校验**

- ❌ **否决，主因是 CLAUDE.md 的读者是模型。**
  `<!-- fact:trace_types -->三十一类<!-- /fact -->` 在渲染后不可见，
  但 **CLAUDE.md 是被原样塞进上下文的**——每处标记都要模型多读十几个 token，
  而这份文件的第一句话就是「主文件是索引 + 必须不请自来的内容」，它对篇幅极敏感。
- 次因：占位符要求**先改文档才能接护栏**。而漂移最严重的正是那两份最长的文件，
  一次性给它们打上几十个标记，本身就是一次高风险的大改。

**候选 C：写一条测试，断言「文档里的数字与代码一致」**

- ✅ **采纳。** 三个候选里唯一**不需要改任何文档就能上线**的。
  接完之后现有文档一个字不用动，测试直接红——**红的那一刻就是问题清单**
  （本轮的原型已经把这份清单打出来了，见 §3.3）。
- 落在 `tests/` 里，天然进 `discover`、进 `run_parallel`、进 CI 六格矩阵，
  **不需要新增任何基础设施**。

### 3.2 从 `run_parallel.py` 搬什么过来

`tests/run_parallel.py` 的机制（`:100-131` 与 `:226-228`）值得逐条拆，
因为**它真正可迁移的东西不是「跑完比对」，是比对的两端各是什么**：

| `run_parallel` | 搬到文档上 |
| --- | --- |
| 期望值来自**当场 discover**，不是硬编码常量（`:131` `suite.countTestCases()`） | 期望值来自**当场 import 枚举**，文档里的数字**从不进断言的左边** |
| 两端用**同一套收集口径**（`:104-107` 明确写了「口径不同的话自检本身就没有意义」） | 两端都用「中文数字」这一种表示；转换函数只有一份 |
| **漏跑要能被发现**：跑了多少 ≠ 期望多少就 rc=2（`:226-228`） | **漏登记要能被发现**：登记了某文件却一处都没匹配上 → 红（见 §3.3 的断言②） |
| 它**不替代** `discover`，只是快速通道（`CLAUDE.md:333`） | 它**不替代**人写文档，只在数字上兜底 |

**第三行是最容易被漏掉、也是最关键的一条。**
一个只会「找到数字 → 比对」的检查，在**文案被改写导致正则不再匹配**时会**静默变绿**——
这正是候选 A 那种静默降级的翻版。本轮的原型实测撞上了这个情况：
把 `docs/internals/architecture.md` 登记进去之后，
正则因为 `**二十七类**事件枚举` 里那对加粗星号而没匹配上，
**如果没有断言②，这个文件会被当成「检查过且通过」**。

### 3.3 方案 P1：`tests/test_docs_facts.py`

**设计要点（每条都对应上面某个实测教训）**：

1. **登记的是「事实 → (计算式, 正则, 文件清单)」**，不是一个全仓大正则。
   ——原型实测：全仓扫「N 类事件」会把 `docs/c14/checklist.md:133` 的
   「四类（worktree 新增的那批）」、`docs/c16/spec.md:294` 的「一类」
   全部误报成漂移。**子集说法与总数说法在文本层面长得一模一样**，
   只能靠「登记文件 + 上下文词」区分。
2. **文件清单显式列出**。好处不只是准确——**那份清单本身就是「这个事实住在哪几处」的文档**，
   也就是 `paired-maintenance` Skill 里那类条目的**可执行版本**。
3. **三个断言，缺一不可**：
   - ① 每处匹配到的数字 == 代码现算值
   - ② 登记的每个文件都**至少匹配上一处**（防文案改写导致静默变绿）
   - ③ **成员名清单**也比对（防「加一个删一个」与 D9 那种名字错）
4. **不扫描历史 spec**（`docs/c*/`、`docs/extensions/*/`、`docs/review/`）。
   那些是**当时的快照**，改它们等于篡改记录。只登记「描述当前状态」的文件：
   `CLAUDE.md` / `README.md` / `docs/guide/` / `docs/internals/`。

**代码草案**（本轮已跑通，实测结果附在后面）：

```python
# tests/test_docs_facts.py
"""
文档里的计数与代码现算值的一致性护栏。

## 为什么要为几个数字写测试

同一个整数活在 6 个地方（trace 事件类数实测），没有一处是「源」。
写它的人每次只改手上那一份——不是不细心，是没有任何东西告诉他还有五份。

## 与 tests/run_parallel.py 同构

期望值**当场从代码算**，绝不硬编码：断言的左边永远是 `len(list(SomeEnum))`，
右边才是文档。硬编码期望值的护栏只能发现「代码变了」，发现不了「文档没跟上」。

## ⚠ 成对维护点

新增一处对这些数字的表述 → 加进对应 Fact 的 `sites`；
否则那一处**不受保护**（漏改不报错）。
改这些数字的措辞 → 断言②会红（登记了却没匹配上），那是**刻意的**：
文案改写会让正则静默失效，而静默失效的护栏比没有护栏更糟。
"""
import pathlib
import re
import unittest

from rhinecode.hooks.models import HookEventType
from rhinecode.skills.models import AdviceKind
from rhinecode.trace.models import TraceEventType

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]

_DIGITS = "零一二三四五六七八九"


def to_chinese(n: int) -> str:
    """把 0–99 的整数写成中文。文档里一律用中文数字，故只需这一个方向。"""
    if n < 10:
        return _DIGITS[n]
    if n < 20:
        return "十" + (_DIGITS[n % 10] if n % 10 else "")
    return _DIGITS[n // 10] + "十" + (_DIGITS[n % 10] if n % 10 else "")


class Fact:
    """
    一个「代码里算得出、文档里写了 N 遍」的事实。

    :param calc: 现算真值的函数（**唯一的期望值来源**）
    :param pattern: 从文档行里抓中文数字的正则，**必须带上下文词**——
                    不带的话会把「某一章新增四类」这种子集说法一起抓进来（实测踩过）
    :param sites: 这个事实住在哪几份文档里，相对仓库根
    """

    def __init__(self, calc, pattern: str, sites: list[str]) -> None:
        self.calc = calc
        self.pattern = re.compile(pattern)
        self.sites = sites


FACTS: dict[str, Fact] = {
    "trace 事件类数": Fact(
        calc=lambda: len(list(TraceEventType)),
        pattern=r"([零一二三四五六七八九十]+)类(?:\*\*)?(?:结构化)?事件",
        sites=[
            "CLAUDE.md",
            "README.md",
            "docs/guide/features.md",
            "docs/guide/project-structure.md",
            "docs/internals/architecture.md",
        ],
    ),
    "Hook 事件数": Fact(
        calc=lambda: len(list(HookEventType)),
        pattern=r"([零一二三四五六七八九十]+)个事件",
        sites=[
            "CLAUDE.md",
            "docs/guide/features.md",
            "docs/guide/hooks.md",
            "docs/internals/architecture.md",
            "docs/internals/capabilities.md",
        ],
    ),
    # ⚠ 口径：数 AdviceKind 成员（8），不是数 _audit_one 里的函数调用（7）。
    #    _check_grants 一个函数产出两类建议，故两个数都「对」——
    #    选枚举是因为用户在 /skills 报告里看到的正是 8 种不同的建议。
    "Skill 体检项数": Fact(
        calc=lambda: len(list(AdviceKind)),
        pattern=r"([零一二三四五六七八九十]+)项检查",
        sites=["CLAUDE.md", "docs/guide/skills.md", "docs/internals/capabilities.md"],
    ),
}


class DocsFactsTest(unittest.TestCase):
    """三个断言：值对、登记有效、名字清单也对。"""

    def test_documented_counts_match_code(self) -> None:
        """
        断言①：登记文件里每一处该计数，都等于代码现算值。

        ⚠ **必须先收集完再断言一次，不能在循环里 assert。**
        循环里断言会在第一处不一致时中止，于是修的人只看得见 1 处、
        改完再跑又冒出第 2 处——既看不到规模，也没法一次改完。
        实测：循环内断言只报出 `CLAUDE.md:104` 一处，收集式报出 4 处。
        """
        wrong = []
        for name, fact in FACTS.items():
            want = to_chinese(fact.calc())
            for rel in fact.sites:
                lines = (REPO_ROOT / rel).read_text(encoding="utf-8").splitlines()
                for lineno, line in enumerate(lines, 1):
                    for got in fact.pattern.findall(line):
                        if got != want:
                            wrong.append(
                                f"{rel}:{lineno} 写「{got}」，应为「{want}」"
                                f"（{name} 现算 {fact.calc()}）"
                            )
        self.assertEqual(wrong, [], "文档计数与代码不一致：\n" + "\n".join(wrong))

    def test_every_registered_site_still_matches(self) -> None:
        """
        断言②：登记的每个文件都至少匹配上一处。

        ⚠ **这条比断言①更容易被忽略，但缺了它整套护栏会静默失效。**
        文案改写（哪怕只是加一对加粗星号）会让正则不再匹配，
        于是断言①在那个文件上「零处需要检查」——**全绿，而文件是错的**。
        实测撞过：`**二十七类**事件枚举` 里的星号让正则漏了整个文件。
        """
        for name, fact in FACTS.items():
            for rel in fact.sites:
                text = (REPO_ROOT / rel).read_text(encoding="utf-8")
                self.assertTrue(
                    fact.pattern.search(text),
                    f"{rel} 登记了「{name}」却一处都没匹配上——"
                    f"要么措辞改了（同步改 pattern），要么该处已删（从 sites 移除）",
                )

    def test_trace_scope_names_are_documented(self) -> None:
        """
        断言③：**名字清单**也要对得上，不只是个数。

        只比个数挡不住两种真实发生过的漂移：
        ① 加一个、删一个 → 计数纹丝不动而内容全错；
        ② 名字本身写错 → `architecture.md` 曾写作用域叫 `notes`，
           而代码里那个常量叫 `memory`，且漏了 c16 新增的 `classifier`。
        """
        from rhinecode.trace import models

        actual = {
            v
            for k, v in vars(models).items()
            if k.startswith("SCOPE_") and isinstance(v, str)
        }
        text = (REPO_ROOT / "docs/internals/architecture.md").read_text(encoding="utf-8")
        missing = sorted(s for s in actual if f"`{s}`" not in text)
        self.assertEqual(missing, [], f"架构文档没提到这些作用域：{missing}")
```

**实测结果**——上面这份代码本轮**真的落到 `tests/` 下跑过**
（`python -m unittest tests.test_docs_facts`，跑完即删，不进本次提交）：

| 断言 | 结果 | 具体 |
| --- | --- | --- |
| ① 计数一致 | **红，4 处** | `CLAUDE.md:104`「二十九」→ 三十一<br>`docs/internals/architecture.md:54`「二十七」→ 三十一<br>`CLAUDE.md:263`「七项」→ 八项<br>**`docs/guide/skills.md:75`「七项」→ 八项** |
| ② 登记仍有效 | 绿 | 3 条事实的 13 个登记点全部匹配得上 |
| ③ 作用域名字 | **红** | 架构文档没提到 `classifier` |
| （其中 Hook 事件数） | 绿 | 8 个登记点全对——护栏在这里的作用是让它**保持**对 |

**接上去当天红出 5 处，零误报。**

⚠ **第 4 处是跑之前没预料到的**：`docs/guide/skills.md:75` 写「七项」，
而那份文件是 **2026-08-30 昨天**建的。**病⑤ 的又一个现形**——
F5 那轮清掉了 trace 的数字，没清 Skill 体检的。

⚠ 上表**只覆盖了登记的那几份「描述当前状态」的文档**。
放开到全仓时，「七项 / 八项」还有约 12 处、「N 类事件」还有约 6 处——
但那些全在 `docs/c*/` 与 `docs/extensions/*/` 的历史 spec 里，
**按 §3.3 第 4 条不该动它们**。

### 3.4 明确不做的三件事

1. **不做全仓扫描。** 原型实测证明会大量误报在子集说法上，
   而一个会误报的门禁的结局是被整体关掉。
2. **不给 `docs/c*/` 与 `docs/extensions/*/` 接护栏。** 它们是验收快照，
   数字随实现演进而与现状不符是**正确的**，不是漂移。
3. **不为「每章新增几类」这种子集说法建登记。** 收益极低，
   且它们本来就该跟着那一章冻结。

---

## 四、Q3：`docs/todo/` 的编号引用——一个不依赖记性的做法

### 4.1 先把诊断改对

题面问「是自检命令不够，还是没人记得跑？」——**实测答案是第三种：跑了也查不出来**
（§1 病④ 的六行输出）。这个区别很重要，因为它决定了修法：
如果是「没人记得跑」，修法是把它接进 CI；
但**把一条查不出问题的命令接进 CI，只会得到一个永远绿的门禁**。

### 4.2 换一条判定规则：不查「引用存不存在」，查「序号是不是过期的」

**关键设计**：`docs/todo/` 的文件名是 `<序号>-<slug>.md`，
而**重排只改序号、不改 slug**（这是那个目录的既定约定，`README.md:10-12` 的
「用 `git mv` 保住历史」正是这个意思）。于是有一条**零误报**的判定：

> 全仓任何一处形如 `N-<slug>.md` 的引用，
> 若 `<slug>` 对应的文件**当前存在**且序号 ≠ `N` → **序号过期**。

这条规则的三个性质：

- **不需要任何登记表**——事实源就是 `ls docs/todo/`。
- **对「已删除的待办」天然免疫**。`docs/todo/README.md:24-85` 那一大段
  重排历史里提到 `1-web-search.md`、`2-classifier.md`、`5-todo-list.md` 等
  **10 个已删除的文件**，它们的 slug 不在当前集合里，规则直接跳过。
  ——这一点是关键：一个朴素的「链接必须能解析」检查会把这 10 处**全部误报**
  （本轮原型实测过）。**历史记录必须提到已删除的文件，那是它的职责。**
- **覆盖全部书写形态**：markdown 链接、反引号裸文件名、正文里直接写的文件名，
  因为它只认 `N-slug.md` 这个字符串本身。

### 4.3 实测：真实规模是 10 处，不是 2 处

全仓（含 `docs/review/`）跑的结果，去掉同一行被匹配两次的重复：

| 位置 | 引用了 | 当前应为 |
| --- | --- | --- |
| `docs/todo/2-delegation-trigger-eval.md:6` | `3-skill-recall-eval.md` | `1-skill-recall-eval.md` |
| `docs/todo/1-skill-recall-eval.md:70` | `4-p1b-unattended.md` | `3-p1b-unattended.md` |
| `docs/guide/roadmap.md:59` | `3-delegation-trigger-eval.md` | `2-delegation-trigger-eval.md` |
| `docs/extensions/skill-authoring/acceptance.md:126` | `2-p1b-unattended.md` | `3-p1b-unattended.md` |
| `docs/extensions/skill-authoring/task.md:498` | `2-p1b-unattended.md` | `3-p1b-unattended.md` |
| `docs/extensions/skill-authoring/task.md:506` | `2-p1b-unattended.md` | `3-p1b-unattended.md` |
| `docs/review/NEXT.md:961` | `3-delegation-trigger-eval.md` | `2-delegation-trigger-eval.md` |
| `docs/review/README.md:82` | `3-delegation-trigger-eval.md` | `2-delegation-trigger-eval.md` |
| `docs/review/README.md:424` | `3-skill-recall-eval.md` | `1-skill-recall-eval.md` |
| `docs/review/README.md:424` | `4-p1b-unattended.md` | `3-p1b-unattended.md` |

**前两处是 D3 已知的。后八处是新的**，而且分布说明了问题：
`docs/todo/README.md:14-17` 那句「重排时要改的不止本文件那张表」**方向反了**——
它以为要改的都在 `docs/todo/` 里，所以自检命令的搜索路径写死成 `docs/todo/`。
**实际上一半以上的过期引用在这个目录之外**，而那些恰恰是**最要紧的**：
`docs/extensions/skill-authoring/task.md:498` 与 `docs/review/README.md:424`
都在「一键开工 Prompt」段落里——那是要被**原样粘进新 session** 的文本，
粘过去照着读文件会直接读不到。
（讽刺的是，`docs/todo/README.md:14-17` 自己就记着 2026-08-19 那次
「六处悬空引用**全部落在 Prompt 段落里**」——症状看对了，
但由此收窄搜索路径的结论正好反了。）

> ⚠ `docs/guide/roadmap.md:59` 那一处再说一次：**那份文件是 2026-08-30 建的**，
> 引用的是 2026-08-20 就已作废的序号。**新写的文档不会自动是对的**——
> 它是从旧文档复制过来的，于是把旧文档的过期引用一起继承了。

⚠ **上表最后三行（`docs/review/` 那 4 处）不该被护栏管，这是跑出来才发现的。**
第一版把全仓都扫了，结果**本报告自己被红 16 次**——它的 §4.3 这张表逐字引用了
每一处过期编号，那正是它作为证据要做的事。
**审查台账（L3）的职责就是逐字记录当时的错误**，给它接一致性护栏是自相矛盾。
故 §4.4 的代码把 `docs/review/` 整个排除。
**排除之后在管辖范围内仍有 6 处**（`docs/todo/` 3 处 + `docs/extensions/` 3 处 +
`docs/guide/` 1 处，其中一行被匹配两次）。

### 4.4 方案 P2：`tests/test_docs_links.py`

```python
# tests/test_docs_links.py
"""
文档链接与 docs/todo/ 序号引用的完整性护栏。

## 为什么不用 docs/todo/README.md 里那条 grep

实测：在一棵有 2 处真悬空引用的树上，那条命令输出 6 行、逐行核对全部正确、
退出码 0——**两处真悬空一处都没出现**。它的正则只认 `docs/todo/N` 前缀形态，
而真实的悬空写成相对链接 `(3-skill-recall-eval.md)` 与反引号裸文件名
`` `4-p1b-unattended.md` ``。

**跑了会拿到一份看起来很干净的报告**，所以「没人记得跑」这个诊断是错的。

## 判定规则：查「序号过期」，不查「文件存不存在」

后者会把 docs/todo/README.md 那一大段重排历史全部误报——
它**必须**提到 10 个已删除的文件名，那是它的职责。
前者只在 slug 仍存在、而序号对不上时报错，对历史记录天然免疫。
"""
import pathlib
import re
import unittest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
TODO_DIR = REPO_ROOT / "docs" / "todo"

# `N-slug.md`：覆盖 markdown 链接、反引号裸名、正文直写三种形态
_TODO_REF = re.compile(r"([0-9]+)-([0-9A-Za-z][-0-9A-Za-z_]*\.md)")

# ⚠ docs/review/ 整个排除：审查台账的职责就是**逐字引用**过期的引用作为证据。
# 不排除的话，一份写着「这里引用了 3-skill-recall-eval.md（应为 1-）」的报告
# 自己就会红——而它写的是对的。
# 实测：本报告一份文件被误报 16 次，`05-maintainability.md` 的 5 处坏链同理。
_EXCLUDE_DIRS = ("docs/review/",)


def _iter_docs():
    """全部纳入检查的 markdown（含仓库根的两份），跳过 L3 审查台账。"""
    for path in sorted(REPO_ROOT.glob("docs/**/*.md")):
        rel = path.relative_to(REPO_ROOT).as_posix()
        if not any(rel.startswith(d) for d in _EXCLUDE_DIRS):
            yield path
    for name in ("CLAUDE.md", "README.md"):
        path = REPO_ROOT / name
        if path.exists():
            yield path


class TodoNumberingTest(unittest.TestCase):
    def test_no_stale_todo_numbers(self) -> None:
        """
        全仓不得引用过期的待办序号。

        重排 docs/todo/ 之后**不需要记得跑任何命令**——这条用例会红，
        并且逐处给出「引用了什么 / 当前应为什么」。
        """
        current = {}
        for f in sorted(TODO_DIR.glob("[0-9]*-*.md")):
            num, slug = f.name.split("-", 1)
            current[slug] = num

        stale = []
        for f in _iter_docs():
            lines = f.read_text(encoding="utf-8").splitlines()
            for lineno, line in enumerate(lines, 1):
                for num, slug in _TODO_REF.findall(line):
                    # slug 不在当前集合里 = 该待办已完成删除，
                    # 历史记录提到它是正常的，不报
                    if slug in current and current[slug] != num:
                        rel = f.relative_to(REPO_ROOT).as_posix()
                        stale.append(
                            f"{rel}:{lineno} 引用 {num}-{slug}，"
                            f"当前应为 {current[slug]}-{slug}"
                        )
        self.assertEqual(stale, [], "待办序号过期：\n" + "\n".join(stale))


class MarkdownLinkTest(unittest.TestCase):
    def test_relative_links_resolve(self) -> None:
        """
        文档里的相对链接必须能解析到真实文件。

        ⚠ 只查相对链接，**不查反引号裸文件名**——后者常常是「用户会创建的文件」
        （`RHINE.md`）或「计划中的文件」（`CONTRIBUTING.md`），
        本轮原型实测那条规则误报 100+ 处，是典型的会被整体关掉的噪声门禁。
        """
        link = re.compile(r"\[[^\]]*\]\(([^)#\s]+)(?:#[^)]*)?\)")
        broken = []
        for f in _iter_docs():
            lines = f.read_text(encoding="utf-8").splitlines()
            for lineno, line in enumerate(lines, 1):
                for target in link.findall(line):
                    if target.startswith(("http://", "https://", "mailto:")):
                        continue
                    # 代码块里的 JSON/字典字面量会被链接正则误抓
                    # （实测 `{'query':'abc'}`），只放行长得像路径的目标
                    if not re.fullmatch(r"[-\w./]+", target):
                        continue
                    if not (f.parent / target).resolve().exists():
                        rel = f.relative_to(REPO_ROOT).as_posix()
                        broken.append(f"{rel}:{lineno} -> {target}")
        self.assertEqual(broken, [], "坏链：\n" + "\n".join(broken))


class ExtensionIndexTest(unittest.TestCase):
    def test_every_extension_is_indexed(self) -> None:
        """
        `docs/extensions/README.md` 的索引表必须覆盖目录下每一个扩展。

        实测漏了两个（`auto-plan/` 与 `ask-user/`，全文 0 次提及），
        而它们各有完整的 spec/plan/task/checklist。
        ⚠ 比对的是**集合**不是**个数**：个数相等而内容不同的情况真实存在过。
        """
        ext_root = REPO_ROOT / "docs" / "extensions"
        dirs = {p.name for p in ext_root.iterdir() if p.is_dir()}
        index = (ext_root / "README.md").read_text(encoding="utf-8")
        missing = sorted(d for d in dirs if f"{d}/" not in index)
        self.assertEqual(missing, [], f"扩展目录未进索引：{missing}")
```

**实测结果**——这份代码同样真的落到 `tests/` 下跑过
（`python -m unittest tests.test_docs_links`，跑完即删）：

| 用例 | 结果 | 具体 |
| --- | --- | --- |
| `TodoNumberingTest` | **红，6 处** | `docs/todo/` 3 · `docs/extensions/skill-authoring/` 3 · `docs/guide/roadmap.md` 1（一行被匹配两次） |
| `MarkdownLinkTest` | **红，1 处** | `docs/todo/2-delegation-trigger-eval.md:6`（唯一一处真坏链） |
| `ExtensionIndexTest` | **红** | `['ask-user', 'auto-plan']` |

**零误报**——但那是**加了两处过滤之后**的结果，两处都是跑出来才知道要加的：

1. **`docs/review/` 排除**（见 §4.3 末的 ⚠）。
2. **链接目标要长得像路径**。不加的话
   `docs/extensions/web-search/task.md:455` 代码块里的 `{'query':'abc'}`
   会被 markdown 链接正则抓成一个「坏链」。

⚠ 另有 5 处**真坏链**在 `docs/review/05-maintainability.md`
（`:815`、`:949`、`:971`、`:973`、`:975`，都是「在子目录里写了从仓库根出发的路径」）。
它们被 `_EXCLUDE_DIRS` 挡在管辖之外，**但它们是真问题**，
请在 F6 里顺手改掉——护栏不管，不等于没错。

### 4.5 顺带把 `docs/todo/README.md` 的那段自检说明改掉

现在的第 14–21 行说「重排时要改的不止本文件那张表」并给出一条 grep。
**建议整段替换成一句话**：

> 重排后**什么都不用记**——`tests/test_docs_links.py::TodoNumberingTest`
> 会把全仓所有过期序号逐处列出来。它查的是「slug 还在、序号对不上」，
> 所以本文件里那些提到已删除待办的历史记录不受影响。

**理由**：留着那条 grep 比删掉更糟。它是一条**会给出虚假安心感**的自检，
而人一旦跑过一次拿到干净输出，就不会再去想这件事——
`README.md:102` 那句「此处四次成为悬空引用」正是在这种安心感下写出来的。

---

## 五、Q4：文档分层建议

### 5.1 先看实测

**体量**（`git ls-files`，2026-08-31）：

| | 文件数 | 体积 |
| --- | --- | --- |
| markdown | **207** | **4.15 MB** |
| `rhinecode/`（产品代码） | — | 2.42 MB |
| `tests/` | — | 2.79 MB |

> ⚠ 题面写的「171 个 / 3.4 MB」已经过期——审查当日（2026-08-22）实测是
> **190 个 / 3.88 MB**，九天后是 207 个 / 4.15 MB。
> **文档以约每天 +2 个文件的速度增长**，本节的任何具体数字都会很快过期，
> 所以下面的建议按**比例与结构**给，不按绝对量。

**维护频率**（近 90 天触及该路径的提交数）——这是本节最有信息量的一组数：

| 路径 | 提交数 | 最后改动 | 体积 |
| --- | --- | --- | --- |
| `CLAUDE.md` | **117** | 08-30 | 90 KB |
| `docs/extensions/` | 105 | 08-20 | 1.18 MB |
| `docs/todo/` | 45 | 08-20 | 66 KB |
| `README.md` | 44 | 08-30 | 32 KB |
| `docs/c11/` | 32 | 08-11 | 805 KB |
| `docs/internals/` | 29 | 08-30 | 151 KB |
| `docs/c2`–`c10` 合计 | 20 | 08-11 | 490 KB |
| `docs/c12`–`c16` 合计 | 61 | 08-11~20 | 726 KB |
| `docs/e2e-sweep/` | 7 | 08-11 | 145 KB |
| **`docs/guide/`** | **1** | 08-30 | 120 KB |

**两个数字直接回答了题面**：

1. **`CLAUDE.md` 117 次 vs `docs/guide/` 1 次 = 117 : 1。**
   题面说「CLAUDE.md 给 AI 读、README 给人读，维护频率天然不同」——
   实测下来 README 自己是 44 次（不低），**真正的低频层是新拆出来的用户手册**。
   而它是**昨天**建的，1 次就是它的创建提交，**至今零维护**。
2. **`docs/c*/` 的 14 个章节目录合计 2.02 MB（占 markdown 总量的 49%），
   近 90 天合计 113 次提交，且全部在 08-20 之前——它们已经事实上冻结了。**

### 5.2 四层，以及每层的「允许落后多少」

漂移的本质是**不同更新频率的内容混在同一层**。分层的目的不是整理美观，
是**让每一层的过期是被允许的、被声明的**，从而把「需要同步」的集合缩到最小。

| 层 | 内容 | 谁维护 | 更新时机 | **允许落后多少** | 谁来兜底 |
| --- | --- | --- | --- | --- | --- |
| **L0 事实源** | 代码里的枚举、常量、目录结构 | 代码作者 | 每次改代码 | **零** | 编译 / 测试 |
| **L1 活文档** | `CLAUDE.md`、`README.md`、`docs/internals/`、`docs/guide/` | 改代码的人 | **随代码同一个 PR** | **零** | **P1 + P2**（本轮方案） |
| **L2 设计档案** | `docs/c*/`、`docs/extensions/*/`（spec/plan/task/checklist/acceptance） | 立项时写，**之后冻结** | 只在「原设计被推翻」时追加**勘误块** | **无限**——它记的是当时的判断 | 无（刻意） |
| **L3 过程台账** | `docs/review/`、`docs/todo/`、`docs/e2e-sweep/` | 做那件事的人 | 做完标记，**不删** | 到「那件事完成」为止 | P2 的序号检查 |

**这张表最要紧的一格是 L2 的「允许落后：无限」。**
本项目已经在做这件事了，而且做得很好——`docs/c12/spec.md` 的 F2 勘误块、
`CLAUDE.md` 里那些「⚠ 这一条已被 XX 推翻，原文保留在下面供追溯」，
都是「不改旧文，追加勘误」的正确姿势。
**但这个规矩从来没有被写下来过**，于是每一轮新章节都会有人不确定
「`docs/c13/spec.md` 里这个过期数字要不要改」——
**答案是不要改，而现在没有一份文档这么说。**

### 5.3 逐个处置

**该留、且要接护栏的（L1，共约 22 份 / 390 KB）**

`CLAUDE.md` + `README.md` + `docs/internals/`（5 份）+ `docs/guide/`（15 份）。
**P1 的登记表只覆盖这一层**，理由在 §3.3 第 4 条。

⚠ 这一层现在有一个隐患：**`docs/guide/` 的 15 份是昨天从旧 README 拆出来的，
内容与 `docs/internals/` 有实质重叠**（例如 `guide/permissions.md` 与
`internals/capabilities.md` 的权限小节）。它现在正确，
但它的**更新触发条件没人定义**——改权限系统的人会去改 `CLAUDE.md` 和 `internals/`，
**大概率想不起 `guide/`**。`docs/guide/roadmap.md:59` 那处出生即过期的引用
是第一个信号。
**建议**：给 `docs/guide/README.md` 加一句「本层由 P1/P2 兜底数字与链接，
语义更新跟随 `internals/` 同一个 PR」，并在 `paired-maintenance` Skill 里加一条
「改 `internals/<X>.md` → 检查 `guide/` 有没有对应章节」。

**该合并的**

- **`docs/e2e-sweep/`（16 份 / 145 KB）→ 合成 1 份。** 它是一次性的全章节
  端到端复扫记录，`summary.md` 已经是它的结论。近 90 天 7 次提交、
  全部在 08-11 之前。**建议**：保留 `summary.md`，其余 15 份的**结论**并入其中，
  原文如需追溯由 git 历史提供。这是全仓性价比最高的一次合并（-15 份）。
- **`docs/c11/`（23 份 / 805 KB，占总量 19%）→ 拆开归位。** 它现在同时装着
  三样东西（Skill 系统的原始设计 + 对齐改造 + **跨阶段测试设施** + 5 份验收记录），
  而 `docs/c11/README.md` 自己就花了大篇幅解释这个目录为什么这么复杂。
  **建议**：`docs/c11/testing/`（P0 trace + P1a 驱动）与 Skill 系统**没有任何关系**
  ——它自己都写了「不占章节号、不属于 Skill 系统」——应移到 `docs/testing-infra/`。
  这一步同时消掉 `architecture.md:54` 那种
  「trace 的理由见 `docs/c11/testing/...`」的怪异指路。

**该降级的**

- **`docs/c2`–`c10`（36 份 / 490 KB）→ 明确标注为 L2 冻结。**
  近 90 天 20 次提交、最后一次 08-11。**不用动文件**，
  只在每个 `docs/c<N>/README.md` 顶部加一行
  「⚠ **本章已冻结**（L2 设计档案）。此处数字与说法反映立项当时的判断，
  与当前实现不符是**正常的**；当前状态见 `docs/internals/`」。
  **这一行的价值是把「过期」从缺陷变成声明**——之后没人需要再问「这个数字要不要改」。
- **`docs/c12`–`c16` 与 `docs/extensions/*/` 同样处理**，
  但 `docs/extensions/README.md` 是**索引**、属 L1，
  要接 `ExtensionIndexTest`（§4.4）。

**结构性的一处欠账**

`docs/extensions/` 1.18 MB / 47 份，**是全仓最大的一块**，
而它的索引表现在漏了 2 个扩展（D5）。
9 个扩展 × 5 份文档已经逼近「章节」的体量了——
`docs/extensions/README.md` 里那条「该开新章节还是算扩展」的判据
（`CLAUDE.md` 能力表要不要多一行）仍然合理，**但索引本身需要护栏**，
否则下一个扩展照样会不进表。

### 5.4 ⚠ 一条不建议做的

**不要把 `CLAUDE.md` 再拆薄。** 它已经拆过一轮（分册在 `docs/internals/`），
而留下的那些是它自己论证过的「**不请自来才有用**」的内容——
安全边界、致命不变量、成对维护点的加载触发块。
本轮实测支持这个判断：`CLAUDE.md` 是全仓改得最勤的文件（117 次 / 90 天），
**它高频不是因为它太长，是因为它是唯一每次都会被读的那一份**。
拆薄它只会把漂移搬到分册里，而分册**没人每次都读**。

L1 的正确治法是 P1/P2 那种**机器兜底**，不是继续拆。

---

## 六、落地顺序与代价

| 批次 | 内容 | 前置 | 代价 | 立刻能消掉 |
| --- | --- | --- | --- | --- |
| **①** | **裁定两个口径**（Skill 体检 7 还是 8；「叶子包」退役与否） | 无 | 一次决定 | 解锁 P1；**修正 F6 的 D1 指示** |
| **②** | **P2**（`test_docs_links.py`，三个用例都能直接上） | 无 | ~110 行，零登记 | D3（管辖内 6 处）、D5 |
| **③** | **P1**（`test_docs_facts.py`） | ① | ~180 行 + 3 条登记 | D1、D2、D9 |
| **④** | F6 的 D 系列按护栏的报错清单逐处改 | ②③ | 半天 | 全部 D |
| **⑤** | **P3 分层**：L2 冻结声明 + `e2e-sweep` 合并 + `c11/testing` 移出 | 无 | 一天 | 病⑤ 的长期成因 |

**两份测试落地后立刻会红的，共 12 处**（都在本轮实跑里逐处确认过）：
P1 的 4 处计数 + 1 处作用域名，P2 的 6 处过期序号 + 1 处坏链，外加 D5 那两个未进索引的扩展。

**如果只做一件事**：**②**。它零登记、零前置、零口径争议，
而且是三件里唯一**永远不需要维护**的——事实源就是 `ls docs/todo/`。
它红出的 6 处里有 3 处落在「一键开工 Prompt」段落里，
那是要被原样粘进新 session 的文本，**粘过去会让人读不到文件**。

**如果只做一天**：① + ② + ③。

> ⚠ **落地时别只抄代码，也把两条「跑出来才知道」的过滤一起抄走**：
> `docs/review/` 排除（不排除的话审查台账逐字引用错误编号会被误报，
> 本报告实测被红 16 次）与「链接目标要长得像路径」（代码块里的
> `{'query':'abc'}` 会被抓成坏链）。**这两条都是第一版没有、跑完才补上的**
> ——这也是为什么本轮坚持把草案真的跑一遍，而不是只写在文档里。

⚠ **P1 与 P2 都不阻塞开源发布**，它们治的是长期成本。
但它们**应该在 F6 之前落地**——F6 要改的正是这些数字，
先有护栏的话，F6 的工作方式就从「照着 9 天前的清单改」变成
「跑测试、照报错改、绿了就完了」，
**而那份 9 天前的清单本身已经有 4 处过期**（D10）。

---

## 附一：对 `README.md` D 节的更正

| 原说法 | 更正 |
| --- | --- |
| D1「实际调 7 个函数」，F6 据此要求「改为七项」 | **两个数都对**：7 个检查函数 / 8 个 `AdviceKind` 成员，差在 `_check_grants` 产出两类建议。**这是口径问题不是笔误**，F6 那条指示照做会把 `skills/models.py:100` 改错 |
| D2「README 两处『二十七类』」 | **已过期**。README 于 2026-08-30 改对（`README.md:126` 三十一类）。当前的两处错误在 `CLAUDE.md:104`（二十九）与 `docs/internals/architecture.md:54`（二十七） |
| D3「两处悬空引用」 | **实际 10 处**，其中 8 处在 `docs/todo/` **之外**（`docs/guide/`、`docs/extensions/`、`docs/review/` 各有分布），4 处落在「一键开工 Prompt」段落里 |
| D3「该目录 README 自己规定了自检命令……仍然第五次失效」 | 成因**不是没人跑**。实测那条命令在当前这棵树上输出 6 行、全部正确、退出码 0——**两处真悬空都不在它的正则覆盖内** |
| D4「`skills` 被称叶子包但 import `permission`；README 把 `web` 标为叶子包，实际它 import ……」 | 对，**但「叶子包」有三种量法**。`web` 在「`import` 运行期闭包」这个量法下**是**干净的叶子（`web/__init__.py` 不 re-export）。结论随量法反转，得先定义量法 |
| D 节结语「D1 / D2 都是同一个机制的产物」 | 只对了一半。D2 是「N 份拷贝」，**D1 是「口径未定义」——后者自动生成治不了**，得先有人裁定 |
| （新增） | **D5–D12 八条**，见 §1 末表 |
| `README.md:82`、`:424` 三处待办序号引用 | 已过期，见 §4.3 表 |
| `NEXT.md` 的 **F5 仍标 ⬜** | F5（README 重写）已于 2026-08-30 由 commit `660fbf1` 等完成 |

---

## 附二：本轮用到的一次性脚本

三个原型都是一次性的、跑完即弃（正式版是上面那两份测试文件）。
**列在这里是为了让上面每个数字都能被复跑核对**：

```bash
# ① 包依赖真相（量法 B：包内任一模块的模块级 import）
python - <<'EOF'
import ast, pathlib, collections
root = pathlib.Path('rhinecode')
pkgs = sorted(p.name for p in root.iterdir() if p.is_dir() and (p / '__init__.py').exists())
deps = collections.defaultdict(set)
for pkg in pkgs:
    for f in (root / pkg).rglob('*.py'):
        for n in ast.walk(ast.parse(f.read_text(encoding='utf-8'))):
            mods = []
            if isinstance(n, ast.Import):
                mods = [a.name for a in n.names]
            elif isinstance(n, ast.ImportFrom) and n.module and n.level == 0:
                mods = [n.module]
            for m in mods:
                parts = m.split('.')
                if parts[0] == 'rhinecode' and len(parts) > 1 \
                        and parts[1] in pkgs and parts[1] != pkg:
                    deps[pkg].add(parts[1])
for pkg in pkgs:
    print(f"{pkg:12s} -> {', '.join(sorted(deps[pkg])) or '(叶子)'}")
EOF

# ② 量法 A（`import <包>` 的运行期闭包）—— 与 ① 对照，看口径差异
python -c "
import importlib, sys
importlib.import_module('rhinecode.web')
print(sorted({m.split('.')[1] for m in sys.modules
              if m.startswith('rhinecode.') and m.count('.')}))"

# ③ 文档维护频率（§5.1 那张表）
for g in CLAUDE.md README.md docs/guide docs/internals docs/extensions docs/todo docs/e2e-sweep; do
  echo "$(git log --since='90 days ago' --oneline -- $g | wc -l)  $(git log -1 --format=%ad --date=short -- $g)  $g"
done

# ④ 当前测试条数（对照 CLAUDE.md:328 与 ci.yml:10）
python -c "
import unittest
print(unittest.defaultTestLoader.discover('tests').countTestCases())"

# ⑤ 官方自检命令的现状复现（§1 病④：6 行全对、两处真悬空一处不在）
grep -rn "docs/todo/[0-9]\|第 [0-9] 条 todo" docs/todo/
```
