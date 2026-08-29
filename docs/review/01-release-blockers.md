# 阶段 1 · 跨平台与安装实跑（R5）

> 2026-08-29 实测。配套 [`README.md`](README.md)（问题清单与行号）与
> [`00-baseline.md`](00-baseline.md)（机器基线）。
>
> **本轮不改任何产品代码，也不改 `pyproject.toml`**（那是 F4 的事，这里只给建议值）。
> 打包实验全部在 scratchpad 的**源码副本**里做，仓库工作区 `git status` 全程为空。
>
> 这是全项目**唯一从未被验证过的维度**：开发机一直是 Windows，且一直用
> `pip install -e .`——那条路径**根本不打包**，它只是在 site-packages 里放一个
> 指回源码目录的链接。真实用户走的 `pip install .` 会**真的把文件复制进包**，
> 而「哪些文件会被复制」由 `pyproject.toml` 决定，从来没人验过。
>
> 结论先说：**这一验就查出一条比 A2 更严重的发布阻塞项**——C13 的三个内置子
> Agent 角色**一个都没进包**，而且**静默为空、没有任何报错**。

## 实测环境

| | Windows（开发机） | Linux（WSL2） |
| --- | --- | --- |
| 系统 | Windows 11 Pro 22621 | Ubuntu 24.04.4 LTS |
| Python | 3.11.9 | 3.12.3 |
| 装法 | `pip install .`（干净 venv） | `pip install .`（干净 venv，源码复制到原生 fs） |
| 装出来的依赖 | textual 8.2.8 / rich 15.0.0 / **openai 3.6.0** / httpx 0.28.1 | 同左 |
| 开发机上原有的 | textual 8.2.8 / rich 15.0.0 / **openai 1.109.1** / httpx 0.27.2 | — |

⚠ 最后两行请对照着看：**干净装出来的 openai 是 3.x，而开发机上是 1.x**——
`openai>=1.50.0` 没有上界，中间隔了两个大版本。详见 [R5-4](#r5-4--openai-没有上界干净环境装出来的是-3x)。

---

## 结论摘要

| # | 级别 | 结论 | 与既有报告的关系 |
| --- | --- | --- | --- |
| **R5-1** | 🔴 | **三个内置子 Agent 角色没进分发包**，`pip install` 之后 `explorer` / `planner` / `general-purpose` 全部消失，且**扫描结果是「0 个角色、0 个错误」**——静默 | **新查出**，README 的 A 组要加一条 |
| **R5-2** | 🔴 | `pyproject.toml:30-54` 那段注释**结论是错的**：package-data **不是「冗余保险」，是唯一的开关**。删掉它 `.md` 一个都不进包 | **推翻**现有代码注释里写着「实测」的一张表 |
| **R5-3** | 🔴 | `textual` 的真实下界是 **6.2.1**（不是声明的 0.80.0，也不是 A2 猜的 8.0）。二分实测 20 个版本 | **坐实并修正** A2 |
| **R5-4** | 🟠 | `openai` 没有上界，干净环境装出来的是 **3.6.0**，开发机是 1.109.1。实测流式路径**行为等价**，但这条漂移从未被验过 | **新查出**，与 C2（rich 未声明）同源 |
| **R5-5** | 🟠 | Linux 全量 **3322 / 3323**。唯一失败的是 `test_subprocess_timeout` 的**反证半边**——产品行为在 Linux 上是对的，**是判据在 POSIX 上量错了东西** | **新查出** |
| **R5-6** | 🟠 | Linux 上剪贴板会**静默失效**（没装 wl-copy/xclip/xsel 时），而 `_copy_linux` **零测试**、唯一的读回验证被 Windows 门禁挡着 | **新查出** |
| **R5-7** | 🟠 | Windows 上生成的 `mcp.yaml` 带 `npx.cmd`，**拿到 Linux 上一定起不来**；反方向没问题。不可移植是**单向**的 | **新查出** |
| **R5-8** | 🟡 | 4 条测试硬编码 `python`，在只提供 `python3` 的发行版上失败（产品代码干净，只有测试有这个假设） | **新查出** |
| **R5-9** | 🟡 | `rhinecode` 在 PyPI **未被占用**。但 `rhinocode` 是 **McNeel Rhino 8 官方 CLI 的名字**，`rhodecode` 是 PyPI 上活跃的**同领域**包（一字之差） | 回答 R5 的第 4 问 |
| **R5-10** | — | LICENSE 三选一：**推荐 MIT**，Apache-2.0 是有理由的次选，GPL-3.0 与目标不符 | 回答 R5 的第 5 问，供 F1 用 |

---

# R5-1 🔴 三个内置子 Agent 角色没进分发包

**这是本轮最重要的一条，而且它比 A2 更难被发现**：A2（textual 下界）会当场
`ModuleNotFoundError`，本条**什么都不会说**。

## 实测：源码树里有，包里没有

```
=== 源码包里所有非 .py 文件 ===
rhinecode/skills/builtin/commit.md
rhinecode/skills/builtin/review.md
rhinecode/skills/builtin/skill-creator/SKILL.md
rhinecode/skills/builtin/skill-creator/reference.md
rhinecode/skills/builtin/test.md
rhinecode/subagents/builtin/explorer.md
rhinecode/subagents/builtin/general-purpose.md
rhinecode/subagents/builtin/planner.md
```

```
=== wheel 里的非 .py 文件 ===
   rhinecode-0.1.0.dist-info/METADATA
   rhinecode-0.1.0.dist-info/RECORD
   rhinecode-0.1.0.dist-info/WHEEL
   rhinecode-0.1.0.dist-info/entry_points.txt
   rhinecode-0.1.0.dist-info/top_level.txt
   rhinecode/skills/builtin/commit.md
   rhinecode/skills/builtin/review.md
   rhinecode/skills/builtin/skill-creator/SKILL.md
   rhinecode/skills/builtin/skill-creator/reference.md
   rhinecode/skills/builtin/test.md
```

`subagents/builtin/` **一个文件都没有**。sdist 同样（`python -m build --sdist`
的产物里 `rhinecode/subagents/` 目录在、`builtin/` 整个不在）。

## 实测：运行期表现是「0 个角色、0 个错误」

在干净 venv 里（**cd 到临时目录，避免本地源码遮蔽 site-packages**）：

```
内置角色目录: ...\venv-clean\Lib\site-packages\rhinecode\subagents\builtin
  存在吗: False
  扫出的角色: []
  errors: ()
内置 Skill 目录: ...\venv-clean\Lib\site-packages\rhinecode\skills\builtin
  存在吗: True
  文件: ['commit.md', 'review.md', 'skill-creator', 'test.md']
```

Linux 上完全一致：

```
[4] 内置角色/Skill 是否随包分发
    包路径: /home/administrator/r5venv/lib/python3.12/site-packages/rhinecode
    subagents/builtin 存在: False | 扫出角色: []
    skills/builtin   存在: True | 文件: ['commit.md', 'review.md', 'skill-creator', 'test.md']
```

**`errors` 是空元组**——这不是 bug，是设计：`subagents/models.py:226` 的 docstring
明写「目录**可能不存在**」，`discovery.py:135` 的分层扫描对缺失的层是**静默跳过**的
（「任何一层缺失都不影响其余层」）。那个容错是为「用户没建 `.rhinecode/agents/`」
写的，**恰好把「内置层整个没打进包」也一起吞了**。

## 用户会看到什么

不是报错，是**三件事同时安静地不见了**：

1. `render_agent_index`（`subagents/render.py:129`）在 `catalog.specs` 为空时
   **返回空串**，而不是空清单——这本身是刻意的（注释写得很清楚：给模型看一个
   「你有委派能力但一个角色都没有」的清单会让它反复试探）。代价是**系统提示里
   那一段整个消失**，模型看不出少了什么。
2. 模型若凭训练先验硬调 `run_agent(agent="explorer")`，拿到的是
   `subagents/service.py:518` 的回灌文案，其中 `available` 会渲染成
   **「（当前一个角色都没有加载）」**——这是全链路上**唯一**一处会说出真相的地方，
   而它只在模型猜错名字时才出现。
3. `/agents` 报告里是空的。

`RunAgentTool` 本身**照常注册**（`bootstrap.py:644` 无条件注册），
`type: branch`（分支式委派）也照常可用——它不需要角色定义。
所以损失精确地是：**C13 的「定义式委派」在任何 pip 安装的副本里都是废的**。

⚠ 对照 `CLAUDE.md` 的 C13 行：「**内置三个角色**：`explorer`（只读调研）/
`planner`（只读方案）/ `general-purpose`（全工具执行）」。这句话对
`pip install -e .` 成立，对 `pip install .` **不成立**——而后者才是用户走的路。

## 修法（F4 做）

`pyproject.toml` 的 package-data 补一条：

```toml
[tool.setuptools.package-data]
"rhinecode.skills" = ["builtin/*.md", "builtin/*/*.md"]
"rhinecode.subagents" = ["builtin/*.md"]
```

⚠ **两条写法不一样是对的**：Skill 有目录型（`builtin/skill-creator/SKILL.md`），
所以要两条模式；角色定义**一个角色就是一个 md 文件、不嵌套**，一条就够。
将来若角色也支持目录型，这里要跟着加——**这是一处新的成对维护点**
（`subagents/builtin/` 的目录结构 ↔ package-data 的模式），
建议连同下面那条护栏一起加进 `paired-maintenance`。

**顺带建议一条护栏**（成本极小，性价比与 C3/C4 同级）：
一条测试，遍历 `rhinecode/` 下全部非 `.py` 文件，断言每一个都被
`[tool.setuptools.package-data]` 的某条模式覆盖。这类问题的共同点正是
「漏改一律不报错」——3,323 条测试**全部**跑在源码树上，
而源码树里那些文件一直都在。

---

# R5-2 🔴 package-data 那段注释的结论是错的

`pyproject.toml:30-54` 有一段很长的注释，中间是一张「实测三组」的表，
声称 `include-package-data` 在 setuptools≥61 下默认为真、
因此**整段删掉 `.md` 也照样全部进包**，本段只是「冗余保险」。

**实测四组，结论相反。** 做法：把 `pyproject.toml` + `rhinecode/` 复制到
scratchpad，每组之前 `rm -rf build *.egg-info`，用 `pip wheel --no-deps` 出包
（pip 默认**构建隔离**，所以用的是最新 setuptools，不是本机那个 65.5.0）：

| 组 | 配置 | wheel 里的 `.md` |
| --- | --- | --- |
| **A** | 现状（有 package-data，只覆盖 `rhinecode.skills`） | **5 个**（全是 skills 的） |
| **B** | 整段 package-data 删掉 | **0 个** |
| **B2** | 整段删掉，**且把仓库的 `.git` 一起复制过去** | **0 个** |
| **C** | 整段删掉 + `include-package-data = false` | **0 个** |

```
### A 组：现状（有 package-data 段，只覆盖 rhinecode.skills）
   .md 数量: 5
      rhinecode/skills/builtin/commit.md
      rhinecode/skills/builtin/review.md
      rhinecode/skills/builtin/skill-creator/SKILL.md
      rhinecode/skills/builtin/skill-creator/reference.md
      rhinecode/skills/builtin/test.md
### B 组：整段删掉 package-data
   .md 数量: 0
### C 组：删掉 package-data + include-package-data = false
   .md 数量: 0
### B2 组（有 .git、无 package-data）.md 数量: 0
```

**B2 组是专门为了堵一个可能的辩解加的**：`include-package-data` 的语义是
「把 **MANIFEST.in 指定的**、或**版本控制插件登记的**文件一并打包」。
本仓库**没有 MANIFEST.in**（已核），构建环境里**没有 setuptools-scm**（已核），
所以 `.git` 在不在都一样——实测确认 0 个。

## 原来那次实验为什么会得出相反结论

无法百分百复原，但注释自己给了线索：它写着「每组都先 `rm -rf build`，
否则 setuptools 会复用 `build/lib/` 里上一次的产物，验的其实是上一次的配置
——**第一次就是这么得到假结论的**」。也就是说那次已经踩过一次这个坑并自认修正了，
**但最终留下的表仍然是错的**。合理推测是：三组实验中「删掉 package-data」
那一组的 `rm -rf build` 没有真正生效，或者验的是 `pip install -e .` 的产物
——那条路径根本不打包，看到的 `.md` 是源码树里的原件。

⚠ **这一条比它看起来重要**：那段注释是**唯一**解释「为什么要保留 package-data」
的地方，而它给出的理由是「万一将来有人关掉 include-package-data」。
按这个理由，一个想精简配置的人会认为**删掉它是安全的**——而实测是删掉就全丢。
**错误的注释在这里的后果与已知项 #18「错误的安全承诺」同型**：
它让人相信一个不存在的兜底。

## 修法（F4 做）

改写那段注释，把表换成上面这张四组表，并把结论从「冗余保险」改成
「**唯一开关，删掉即全丢**」。补 `rhinecode.subagents` 那条（见 R5-1）。

---

# R5-3 🔴 textual 的真实下界是 6.2.1

## 方法

一个干净 venv，装齐 openai / pyyaml / httpx，然后 `pip install --no-deps .`
把项目本身装进去（不装它会让 4 条起子进程的用例报 `No module named rhinecode`，
那是环境噪音不是版本问题——第一次跑就撞上了）。接着逐版本换 textual，
每次跑两样：

- **快筛**：`import rhinecode.tui.widgets, rhinecode.tui.app`
- **实跑**：12 个 TUI 模块共 **239 条**用例
  （`test_tui_layout` / `test_tui_markup_escape` / `test_tui_panels` /
  `test_tui_batch` / `test_tui_selection` / `test_tui_status_line` /
  `test_tui_history_view` / `test_tui_activity` / `test_tui_detail_level` /
  `test_tui_quit` / `test_todo_tui` / `test_ask_user_panel`）

⚠ **`python -m compileall rhinecode` 在这件事上没有任何鉴别力**——它只做语法编译，
不 import 任何第三方包，20 个版本全过。R5 原始 Prompt 里把它列进步骤是合理的
（它能挡住语法错误），但**它一次都不会因为 textual 版本不对而红**。

## 结果

| textual | import | 239 条 TUI 用例 | 挡住它的是什么 |
| --- | --- | --- | --- |
| 0.80.0（**当前声明的下界**） | ❌ | — | `ModuleNotFoundError: textual.content` |
| 0.89.1 | ❌ | — | `ModuleNotFoundError: textual.style` |
| 1.0.0 | ❌ | — | `ModuleNotFoundError: textual.style` |
| 2.0.0 | ✅ | FAILED (failures=12, errors=13) | `'Static' object has no attribute 'content'` |
| 2.1.2 | ✅ | FAILED (failures=8, errors=13) | 同上 |
| 3.0.0 | ✅ | FAILED (failures=8, errors=13) | 同上 |
| 3.7.1 | ✅ | 未单独实跑（3.0.0 已失败） | — |
| 4.0.0 | ✅ | FAILED (failures=7, **errors=49**) | 上面那条 + `LookupError: ContextVar 'active_app'` |
| 5.0.0 | ✅ | FAILED (failures=7, errors=49) | 同上 |
| 6.0.0 | ✅ | FAILED (**failures=5**) | 只剩选区那 5 条 |
| 6.1.0 | ✅ | FAILED (failures=5) | 同上 |
| 6.2.0 | ✅ | FAILED (failures=5) | 同上 |
| **6.2.1** | ✅ | **OK** | — |
| 6.3.0 / 6.6.0 / 6.9.0 / 6.12.0 | ✅ | OK | — |
| 7.0.0 / 8.0.0 / 8.2.8 | ✅ | OK | — |

**边界又用全量测试复核了一遍**（不只是那 239 条）：

```
### textual 6.2.0
185 个模块 / 期望 3323 条用例 -> 8 个分片
  分片2: 失败（34 模块）
FAILED (failures=5)
错误：分片 [2] 有失败用例
用例 3323/3323   墙钟 46.1s

### textual 6.2.1
用例 3323/3323   墙钟 46.4s
全部通过
```

## 6.2.0 → 6.2.1 之间到底修了什么

5 条失败全在 `tests/test_tui_selection.py`，症状是 `get_selected_text()`
拿回来的文本**被截断或整段缺失**：

```
AssertionError: '文件的第二行' not found in '● 读取 1 个文\n● Read(path: a.py)\n  ⎿  文件的第一行\n封闭批次\n' : 展开后的原文必须可复制
AssertionError: '秘密路径.py' not found in '● 读取 2 个文件\n● Read(秘密路径\n● Read(另一个.\n封闭批次\n' : 展开之后就该能复制了
AssertionError: '读取 1 个文件' not found in '● 读取 1 个文\n封闭批次\n' : 聚合语必须可复制
AssertionError: '新增的一行' not found in '● Update(b.py)\n  ⎿  Added 1 line\n' : 差异块的内容行要可复制
AssertionError: '读取 12 行' not found in '● Read(a.py)\n' : 结果摘要必须可复制
```

Textual 的 CHANGELOG 里 **6.2.1（2025-10-01）** 只有两条，第一条正是它：

> - Fix inability to copy text outside of an input/textarea when it was focused
> - Fix issue when copying text after a double click

对上了：RhineCode 的主输入框**一直是焦点**，于是「焦点在 Input 上时选不到别处的文本」
这个 bug 精确命中本项目的 `Ctrl+C` 复制路径。**换句话说，下界不是被某个 API 卡住的，
是被一个功能 bug 卡住的**——低于 6.2.1 装得上、跑得起来、界面正常，
**只有「选中后按 Ctrl+C 复制」这一件事默默复制不全**。

⚠ 这也是为什么下界**必须靠实跑二分**，不能靠「找出用了哪些 API、查它们哪版引入的」
推：那种推法只会得到 **2.0.0**（`textual.style` 的引入版本），而 2.0.0 上
239 条里错 25 条。

## 建议值（F4 用）

```toml
dependencies = [
    "textual>=6.2.1,<9",
    "rich>=13",            # C2：直接 import 了 7 个 rich 模块却没声明
    "openai>=1.50.0,<4",   # R5-4
    "pyyaml>=6.0",
    "httpx>=0.27",
]
```

**下界为什么取 6.2.1 而不是 8.0**：6.2.1 是**实测全量 3,323 条全绿**的最低版本，
把下界抬到「我自己在用的那版」会无谓地拦掉环境里已有 6.x/7.x 的用户，
而 pip 在那种情况下会去装一个新的 textual（可能与用户其它项目冲突）。
**如果你更希望「只承诺我真跑过的」**，`>=8.0,<9` 也站得住，代价如上——
这是个取舍，两个值都不是错的。

**上界 `<9` 是防御性的，它不是保证。** 本轮实测已经证明
**破坏性变化可以发生在 patch 位**（6.2.0 → 6.2.1 那次是修复方向，
反方向同样可能）。`<9` 挡得住的只是「下一个大版本一上来就改掉
`textual.content` / `textual.style` 的形状」这一类——而那恰好是本项目
**最脆弱**的依赖面（这两个 import 在 0.89 / 2.0 两处都断过）。
真正的兜底是 CI（C1）：加一个**定时任务**用 textual 最新版跑一遍测试，
上界负责保证用户装不到坏的，定时任务负责让你比用户先知道。

⚠ **`requires-python` 的验证范围**：本轮只验了 **3.11.9（Win）** 与
**3.12.3（Linux）**。声明的是 `>=3.11`，**3.13 / 3.14 一次都没跑过**。
CI 矩阵应当把它们覆盖上——textual 6.3.0 的 CHANGELOG 里就有
「Dropped support for Python3.8 / Added support for Python3.14」，
说明这条线在动。

---

# R5-4 🟠 openai 没有上界，干净环境装出来的是 3.x

## 事实

```
开发机（pip install -e . 长期使用）：openai 1.109.1 / httpx 0.27.2
干净 venv（pip install .）：       openai 3.6.0   / httpx 0.28.1
```

`pyproject.toml:14` 写的是 `openai>=1.50.0`，无上界。**中间隔了两个大版本**，
而 DeepSeek Provider 的**全部**能力都建立在这个 SDK 上。

## 实测：目前是等价的

起一个最小的 OpenAI 兼容 SSE 服务器（本机回环，8 个 chunk 覆盖
thinking / 正文增量 / 工具调用分片 / usage 块 / `[DONE]`），
用同一段代码分别在两个环境里跑 `DeepSeekProvider.stream_chat`：

```
openai SDK: 3.6.0 | httpx: 0.28.1
  thinking     '想一下'
  text         '你好'
  text         '，世界'
  tool_pending id='call_1' name='read_file' args=None
  usage        CompletionUsage(completion_tokens=7, prompt_tokens=11, total_tokens=18, completion_tokens_details=None, compute_units=None, prompt_tokens_details=None)
  tool_call    id='call_1' name='read_file' args={'path': 'a.py'}
  done         ''

openai SDK: 1.109.1 | httpx: 0.27.2
  thinking     '想一下'
  text         '你好'
  text         '，世界'
  tool_pending id='call_1' name='read_file' args=None
  usage        CompletionUsage(completion_tokens=7, prompt_tokens=11, total_tokens=18, completion_tokens_details=None, prompt_tokens_details=None)
  tool_call    id='call_1' name='read_file' args={'path': 'a.py'}
  done         ''
```

**逐块一致**，唯一差别是 3.x 的 `CompletionUsage` 多了一个 `compute_units=None`
字段。已核：全项目对 usage 的读取都是**按名字取属性**（`agent/collector.py:82`
转成项目自己的 `Usage`，`tui/app.py:2493-2494` 与 `subagents/runner.py:818`
用 `getattr(..., 0)`），多一个字段不影响任何一处。

## 所以这是一条「暂时没危害、但从未被验过」的漂移

三点要记下来：

1. **3,323 条测试证明不了这件事**。它们几乎全用替身 Provider，
   真正碰 SDK 的只有上面这条手工探针。
2. **本轮跑的全量测试都是在 openai 3.6.0 下跑的**（干净 venv 与 WSL 都是），
   全绿——这至少说明 3.x 不会在 import / 装配期炸掉。
3. **建议上界取 `<4`** 而不是 `<2`：实测 3.x 可用，`<2` 会把已经验过能跑的版本挡掉。
   与 textual 那条同理，真正的兜底是 CI。

⚠ 这条与 **C2（rich 是未声明的直接依赖）同源**：都是「我机器上装的那版」
与「用户装出来的那版」之间没有任何东西把关。本轮顺带印证了 C2 的现状——
干净装出来的 rich 是 15.0.0，由 textual 传递带进来的；`pyproject.toml` 里
仍然没有 rich。

---

# R5-5 🟠 Linux 全量 3322 / 3323，唯一的失败是判据本身

## 实跑

```
$ cd ~/r5src && export PATH=$HOME/r5venv/bin:$PATH && python -m tests.run_parallel
185 个模块 / 期望 3323 条用例 -> 8 个分片
...
用例 3323/3323   墙钟 26.9s
real	0m27.641s

FAIL: test_helper_returns_promptly_while_the_naive_form_does_not
  (tests.test_subprocess_timeout.TimeoutIsARealBoundTest)
AssertionError: 1.0019549890000121 not greater than 4.0 : 朴素写法本应被孙子进程拖住却只用了 1.0s——若平台行为已变，本文件的整套论证需要重新核对
```

**26.9 秒**（Windows 上 33.7 秒），分片完整性自检通过，条数一致。

⚠ **第一次跑是 5 条失败**，多出的 4 条全在 `tests/test_trace_full_output.py`，
根因见 [R5-8](#r5-8--4-条测试硬编码-python)（硬编码 `python`）。把 venv 的 `bin`
加进 PATH 之后剩这 1 条。

## 这条失败说明什么

`tools/run_command.py` 的两条平台分支是这样的：

- **`:259`（POSIX）**：`start_new_session=True`，让子进程自成 session/进程组
- **`:100`（分岔点）**：Windows 用 `taskkill /F /T`，POSIX 用 `os.killpg(pgid, 9)`，
  并带一道「pgid 不等于自己的 pgid 才杀」的双保险

那条失败的用例是**正反两跑**：正面验 `run_shell_captured` 超时后立刻返回，
反面验「朴素 `subprocess.run` 会被孙子进程拖住」——**反面这一半在 Linux 上不成立**。

为了搞清楚是产品坏了还是判据错了，写了一个探针：同一个「睡 6 秒然后落一个标记文件」
的脚本，两种写法各跑一次、**各用各的标记文件**（第一版探针两边共用一个标记，
结果被互相污染，得出了「Linux 上没杀干净」的错误结论）：

```
### Linux（Ubuntu 24.04 / Python 3.12.3）
run_shell_captured : 返回 1.00s | 孙子进程标记存在=False  (存在=没杀干净)
朴素 subprocess.run: 返回 1.00s | 孙子进程标记存在=True

### Windows（对照，Python 3.11.9）
run_shell_captured : 返回 1.11s | 孙子进程标记存在=False
朴素 subprocess.run: 返回 6.05s | 孙子进程标记存在=True
```

读法：

- **产品代码在两个平台上都是对的**——`run_shell_captured` 都是 1 秒返回、
  **进程树都真的死了**（同文件的 `test_the_child_is_really_dead_afterwards`
  在 Linux 上也是绿的）。
- **朴素写法的失败形态在两个平台上不一样**：Windows 上是**又慢又漏**（6.05s +
  孙子进程活着），Linux 上是**快但漏**（1.00s + 孙子进程活着）。
- 而那条判据量的是**时间差**——**时间差只是 Windows 上的症状**。
  在 POSIX 上，「泄漏了一棵进程树」才是那个缺陷的表现，而它量不到。

## 修法建议（不属本轮，登记给 F 系列）

把反证的判据从「朴素写法耗时 > 4 秒」换成「**朴素写法之后那个标记文件存在，
`run_shell_captured` 之后不存在**」——那是平台无关的，而且**更贴近这条护栏
真正想守住的东西**（不是「快」，是「别留孤儿进程」）。

⚠ 顺带修掉一处口径问题：`CLAUDE.md` 的测试一节把
`test_subprocess_timeout` 的 `CHILD_SLEEP=6 / THRESHOLD=4.0` 列进
「**明确不要动**」，理由是「那是判别余量，缩小换速度会引入 flaky」。
那条理由**在 Windows 上成立**，但它同时把这条用例钉成了**只在 Windows 上有意义**。
换成标记文件之后，`CHILD_SLEEP` 还是要保留（要给它一个「本该活着」的窗口），
但 `THRESHOLD` 就不再是判据了。**这是一处成对维护点**：
改判据要同步改 `CLAUDE.md` 里那句「不要动」。

## 两处 Linux 上多出来的 skip

| skip 理由 | 位置 |
| --- | --- |
| `WinError 32 是 Windows 特有行为` | `tests/test_e2e_sandbox_seed.py:135` |
| `读回验证只在 Windows 上做（其余平台要装外部工具）` | `tests/test_tui_selection.py:267` |

Windows 上 skip 4 条，Linux 上 skip 6 条。第二条见 R5-6。

## 顺带确认：真实 Textual app 在 Linux 上跑得起来

```
$ python -m unittest tests.test_e2e_host
.s..........................
Ran 28 tests in 24.726s
OK (skipped=1)
```

`tests/e2e/host` 会起**真实子进程**跑一个完整的 Textual app 并经回环通道驱动它，
28 条全过。加上 `rhine --help` 与首次运行模板生成都验过，
**「Linux 上装得上、起得来」是有实证的**：

```
$ HOME=/tmp/fakehome ~/r5venv/bin/rhine
已在 /tmp/fakehome/.rhinecode 生成配置模板（config.yaml / permissions.yaml / mcp.yaml / hooks.yaml），请在 config.yaml 填入真实 api_key 后重新运行 rhine。
退出码=0
/tmp/fakehome/.rhinecode/permissions.yaml
/tmp/fakehome/.rhinecode/mcp.yaml
/tmp/fakehome/.rhinecode/config.yaml
/tmp/fakehome/.rhinecode/hooks.yaml
```

（Windows 干净 venv 里同一条路径行为一致；第二次运行、api_key 仍是占位符时
退出码 1。）

---

# R5-6 🟠 Linux 上剪贴板会静默失效，而那条分支零测试

## 实测

```
[1] 剪贴板（tui/clipboard.py:56,58 的 else 分支）
    which wl-copy  -> None
    which xclip    -> None
    which xsel     -> None
    which pbcopy   -> None
    copy_text('hello') -> False
    _copy_linux('hello') -> False
```

`copy_text` 的分支（`tui/clipboard.py:56,58`）只显式枚举 `win32` 与 `darwin`，
其余落 `_copy_linux`（`:129`），而它按 wl-copy → xclip → xsel 顺序试，
**一个都没有就返回 False**。裸装的 Ubuntu（以及绝大多数服务器 / 容器 / WSL）
三个都没有。

## 后果比「少一个功能」重一点

`tui/app.py:1875-1882` 是**两条路一起走**：

```python
self.copy_to_clipboard(selected)   # Textual 的 OSC 52
copy_text(selected)                # 直接调操作系统剪贴板
```

`tests/test_tui_selection.py:244` 的注释解释了为什么要两条：OSC 52 由终端代劳，
天然支持 SSH，**但很多终端出于安全默认关闭它，而应用这一端只是往 stdout 写了
几个字节、成没成功根本不知道**。所以第二条是为第一条兜底的。

**在 Linux 上这个兜底默认不存在**，于是 `Ctrl+C` 复制退回成「只有 OSC 52 一条路」
——正是那段注释所描述的、当初促使他们加第二条的那个状态：
**选中了、按了 Ctrl+C、什么也没发生，且无任何报错**。

## 而且这条分支一条测试都没有

全仓与剪贴板相关的断言只有两条，都在 `tests/test_tui_selection.py`：

- `:267` 的读回验证 —— `if sys.platform != "win32": self.skipTest(...)`
- `:292` 的空串短路 —— 平台无关，但它验的是「不去动剪贴板」

`_copy_linux` 与 `_copy_via_command` **零覆盖**。这与 `00-baseline.md` 里
`tui/clipboard.py` 覆盖率 **67%**（21 行未覆盖）对得上——没覆盖的就是这一片。

## 建议（不属本轮）

三选一，成本递增：

1. **只改文档**：README 的安装说明里写明「Linux 下要装 `wl-clipboard` 或 `xclip`
   才有系统剪贴板兜底，否则只剩 OSC 52」。**最低成本，且诚实**。
2. **加一条可跑的测试**：把 `_copy_via_command` 的命令替身化，
   验「三个命令按序试、前一个失败才试下一个、全失败返回 False」——
   纯逻辑，不需要真剪贴板，能挡住「顺手把顺序改了」这类漏改。
3. **界面上给一次提示**：复制失败时在状态栏说一句。⚠ 但要小心
   `CLAUDE.md` 里那条「`Ctrl+C` 用于复制场景**不计入退出计数**」的约定，
   在那条路径上加东西属于要读 `paired-maintenance` 的改动。

---

# R5-7 🟠 Windows 生成的 mcp.yaml 拿到 Linux 上一定起不来

两处实现（`mcp/transport.py:38` 的 `resolve_stdio_command` 与
`mcp/auto_config.py:125` 的 `default_npx_command`）**各自都是对的**，
问题在**它们的产物会跨平台流动**：

```
[3] MCP 的 Windows .cmd 假设
    default_npx_command() ->  'npx'              # Linux 上
    resolve_stdio_command('npx')     -> '/mnt/c/Program Files/nodejs/npx'
    resolve_stdio_command('npx.cmd') -> '/mnt/c/Program Files/nodejs/npx.cmd'
    Popen(['npx.cmd']) -> OSError: [Errno 8] Exec format error: 'npx.cmd'
```

⚠ **上面第 3、4 行是 WSL 特有的假象，别照抄**：WSL 默认把 Windows 的 PATH
接进来，所以 `shutil.which('npx.cmd')` 真的找到了一个 Windows 批处理文件，
然后 `Popen` 拿 `Exec format error` 挂掉。**在一台纯 Linux 上**，
`which('npx.cmd')` 返回 `None`，`resolve_stdio_command` 按设计**原样返回
`'npx.cmd'`**（docstring：「若仍找不到则返回原始值，让底层错误信息保持可诊断」），
然后 `Popen` 报 `FileNotFoundError`。**两条路都失败，只是错误文案不同。**

## 不可移植是单向的

| 配置生成于 | 写进 mcp.yaml 的 | 在 Windows 上 | 在 Linux/macOS 上 |
| --- | --- | --- | --- |
| Windows | `npx.cmd` | ✅ | ❌ **起不来** |
| Linux/macOS | `npx` | ✅（`resolve_stdio_command` 会补 `.cmd`） | ✅ |

反方向没事，因为 `resolve_stdio_command` 在 Windows 上会主动试
`.cmd/.exe/.bat`。**只有 Windows→其它平台这一个方向坏**。

## 为什么这值得记

`.rhinecode/mcp.yaml` 是**项目级**配置，随代码仓库分发
（`CLAUDE.md` 的配置一节：「`permissions.yaml` / `mcp.yaml` 用户级 + 项目级」）。
一个在 Windows 上开发、把 `mcp.yaml` 提交进仓库的项目，
Linux 上的协作者 clone 下来就是起不来——**而错误信息是
`FileNotFoundError: npx.cmd`，看不出根因是「这份配置是在别的操作系统上生成的」**。

## 建议（不属本轮）

最小改动是让 `resolve_stdio_command` 在**非 Windows** 上多一步：
裸命令名带 `.cmd` / `.bat` / `.exe` 后缀且找不到时，**去掉后缀再试一次**，
并在报告里说明「这份配置像是在 Windows 上生成的」。
比「生成时就写 `npx`」好，因为**已经存在的 mcp.yaml 也能救**。

⚠ 这是一处**新的成对维护点**：`auto_config.default_npx_command`（写入侧）
与 `transport.resolve_stdio_command`（读取侧）**共用同一个 `.cmd` 假设**，
改任一侧都要看另一侧。原始 R5 Prompt 把这两行并排列出正是这个原因。

---

# R5-8 🟡 4 条测试硬编码 `python`

Ubuntu 24.04 只提供 `python3`，不提供 `python`。第一次在 WSL 跑全量时：

```
FAIL: test_short_output_leaves_full_output_unset (tests.test_trace_full_output.RunCommandFullOutputTest)
AssertionError: False is not true : $ python -c "[print('L%04d' % i) for i in range(3)]"
退出码: 127
stderr（1 行）:
/bin/sh: 1: python: not found
```

四条全在 `tests/test_trace_full_output.py`（`:58`、`:101`、`:112`），
把 venv 的 `bin` 目录加进 PATH 后全部转绿。

**产品代码是干净的**——全仓扫下来，`rhinecode/` 里对 `python` 的提及只有
`classifier/broad.py:65`（那是一张**解释器名单**，用来识别过宽的放行规则，
不执行任何东西）与 `trace/reader.py:715`（argparse 的 `prog=`，只是帮助文本）。
另两处 `tests/test_classifier_broad.py:96` 与 `test_classifier_service.py:283`
里的 `"python -c ..."` 是**纯字符串**，不执行。

## 建议

换成 `sys.executable`（同仓库已有 10 个测试文件是这么写的，
`tests/test_subprocess_timeout.py` 的 `ScriptMixin` 就是现成的样板）。
不改也行——但 CI 上 Linux runner 会稳定红 4 条，那时更容易被误当成产品缺陷。

---

# R5-9 🟡 名字：PyPI 干净，但真实世界里有两个近邻

## PyPI 可用性（实测 `https://pypi.org/pypi/<name>/json`）

| 名字 | 结果 |
| --- | --- |
| **`rhinecode`** | **404 —— 未被占用** ✅ |
| `rhine-code` / `rhine_code` | 404 —— 未被占用 |
| `rhinecode-cli` / `rhinocode` / `rhine-agent` | 404 —— 未被占用 |
| `rhine` | **已占用** —— 1.0.2，Speare Inc.「Rhine Python Client」 |
| `rhino` | 已占用 —— 0.0.5，一个 REST 微框架 |
| `rhodecode` | **已占用** —— 2.2.6，活跃 |

**`rhinecode` 可以直接注册。**

## 两个需要知道的近邻

**① `rhinocode` 是 McNeel Rhino 8 官方 CLI 的名字。** Rhino3D ≥ 8.11 随产品分发
一个叫 `rhinocode` 的命令行工具，用来列出运行中的 Rhino 实例、在其中跑脚本、
打包插件；还有配套的 VS Code 扩展（`mcneel/rhinocodevscode`）。
它**没有**发到 PyPI（上表已核），所以不构成安装冲突，但：

- **搜索会互相干扰**：「rhinocode」「rhino code」这类查询会被 McNeel 的
  开发者文档占满；
- **Rhino / Rhinoceros 是 Robert McNeel & Associates 的商标**。
  `RhineCode`（**莱茵**，i）与 `RhinoCode`（**犀牛**，o）差一个字母，
  而且**都是开发者工具**。这不是法律意见，但作品集场景下值得知道：
  被读成对方的可能性不低。

**② `rhodecode` 是同领域的活跃 PyPI 包**——「a fast and powerful management tool
for Mercurial and GIT with a built in push/pull server, full text search and
code-review」。`rhinecode` 与 `rhodecode` 也是一字之差，且同属「代码 + 工具」域。

## 命令名 `rhine` 呢

PyPI 上的 `rhine` 是 2015 年的一个 Python 2/3 客户端库，只有 sdist，
`setup.py` 里**没有 `console_scripts`**（已下载核对），因此**不会与 `rhine`
这个命令冲突**；它占的是 import 包名 `rhine`，而本项目的 import 包名是 `rhinecode`。

不过 `rhine` 作为全局命令**很短、很通用**，撞上用户自己的脚本别名的概率不低。
这不是阻塞项，只是 README 里值得提一句「装完暴露的命令是 `rhine`」
（现在只有 `pyproject.toml:23` 的注释里有）。

## 建议

**保持 `rhinecode` 不变。** 名字已经贯穿全部文档、包名、`.rhinecode/` 目录名，
改名的代价远大于收益，而冲突都是「可能被读错」级别、不是「装不上」级别。
唯一建议：README 第一句就把它和犀牛撇清——例如
「RhineCode（莱茵河，Rhine —— 与 Rhino / RhinoCode 无关）」。
一句话解决掉一个会反复被问的问题。

**来源**：
[PyPI JSON API](https://pypi.org/pypi/rhinecode/json)、
[Rhino - RhinoCode Command Line Interface](https://developer.rhino3d.com/guides/scripting/advanced-cli/)、
[mcneel/rhinocodevscode](https://github.com/mcneel/rhinocodevscode)、
[RhodeCode on PyPI](https://pypi.org/project/rhodecode/)。

---

# R5-10 LICENSE 三选一

> 只给取舍与推荐，**不写 LICENSE 文件**（那是 F1）。

## 先确认一件事：依赖没有给你任何限制

干净 venv 里逐个读元数据：

| 包 | 许可证 |
| --- | --- |
| textual | MIT |
| rich | MIT |
| pyyaml | MIT |
| pydantic | MIT |
| markdown-it-py | MIT |
| httpx | BSD-3-Clause |
| pygments | BSD-2-Clause |
| **openai** | **Apache-2.0** |

**全部是宽松许可证，没有一个 copyleft。** 三个候选都不会与它们冲突，
而且你**只是依赖**它们、没有把它们的源码复制进仓库，所以连
「Apache-2.0 的 NOTICE 要不要带」这类问题都不涉及。**选哪个完全由你的目标决定。**

## 三者的实质差别

| | MIT | Apache-2.0 | GPL-3.0 |
| --- | --- | --- | --- |
| 长度 | ~170 词，一屏 | ~10 页 | ~15 页 |
| 别人能闭源商用吗 | 能 | 能 | **不能**（衍生作品必须同样 GPL 开源） |
| 专利授权 | **无明文** | **有明文**（§3），且带专利报复终止条款 | 有 |
| 商标 | 未提 | **明确不授予**（§6） | 未提 |
| 改动需声明吗 | 否 | **是**（§4b，要标注改过哪些文件） | 是 |
| 贡献者的授权 | 靠惯例 | **明文**（§5：贡献即按本许可证授权） | 明文 |
| 生态里的常见度 | 最高 | 高（大公司偏好） | 中，且在下降 |

## 逐个对照「开源到 GitHub + 个人作品集」这个目标

**MIT —— 推荐。**

- **作品集场景下，读者是人不是法务。** MIT 一屏读完，没有任何需要理解的条款；
  Apache-2.0 十页，一个来看你项目的人不会读，而**看不懂的许可证会产生
  一点点犹豫**——恰恰是作品集最不想要的东西。
- **零心理负担地被使用/借鉴**，这正是作品集想要的结果：别人拿去用、
  在自己的项目里提一句你的名字，比一个没人敢碰的仓库有价值得多。
- **生态一致**：你最重的依赖 textual 与 rich 都是 MIT，
  同类的 TUI / CLI 工具也是 MIT 占多数。
- **`§4b` 那条对你是负担不是保护**：Apache-2.0 要求改动者标注改了哪些文件，
  这在一个还没有外部贡献者的项目上只会增加 PR 的摩擦。

**Apache-2.0 —— 有理由的次选，两种情况下我会改推它。**

- **如果你希望这个项目将来被公司采用**：企业法务对 Apache-2.0 的接受度
  略高于 MIT，因为 §3 的**明文专利授权**消除了一个理论风险
  （MIT 没写专利，一般认为隐含授予，但「一般认为」不是「写着」）。
- **如果 R5-9 那两个近邻让你在意商标**：Apache-2.0 §6 明确说
  「本许可证不授予商标权」。这不能阻止别人把你的项目读成 McNeel 的东西，
  但它至少让**你自己**的立场是清楚的——你没有在授权任何人用「RhineCode」
  这个名字做别的事。
- 另外 §5 把「贡献即按本许可证授权」写进了许可证本身，
  省掉将来可能要搞的 CLA。**如果你预期会有外部贡献者，这条有实际价值。**

**GPL-3.0 —— 与目标不符，不推荐。**

- **传染性会劝退绝大多数使用者**：任何链接/衍生的作品都必须同样以 GPL 开源。
  对一个「希望别人拿去用、顺便看看我怎么写的」的作品集项目，这是反效果。
- **对本项目尤其不合适**：RhineCode 是**终端应用**，用户的典型用法是
  「装上、在自己的项目里跑」——GPL 保护的是「别人拿你的**代码**去闭源」，
  而这个项目最可能的「被拿走」形态是**借鉴设计**（五层权限管线、
  两层上下文压缩、分类器接在④层），**而 GPL 对设计思路一点保护作用都没有**。
  你付出了传染性的代价，换来的保护恰好不覆盖你真正的产出。
- 唯一会让我改口的情形：你的目标是「防止别人拿去做闭源商业产品」。
  但那与「个人作品集展示」是两个目标，你说的是后者。

## 推荐

**MIT。** 理由一句话：**作品集的目标是被读、被用、被记住，MIT 在这三件事上
的摩擦都最小**，而它放弃的那点保护（专利明文、商标声明、改动标注）
在这个阶段对你没有实际价值。

**如果将来出现外部贡献者、或有公司来问能不能用**，再改 Apache-2.0 也不晚——
只要贡献者不多，重新授权的成本很低（每个贡献者点头即可）；
反过来从 GPL 换到宽松许可证要难得多。**先 MIT 是可逆的那一步。**

给 F1 的具体动作：

1. 仓库根加 `LICENSE`（MIT 全文，`Copyright (c) 2026 <你的名字或 GitHub ID>`）
2. `pyproject.toml` 的 `[project]` 加 `license = "MIT"`（SPDX 表达式）与
   `license-files = ["LICENSE"]`——⚠ 现在的 sdist / wheel 里**一个 LICENSE 都没有**
   （已核），加了这两行才会打进去
3. README 顶部加一行许可证徽章，底部加一节 License

---

# 附一 · 本轮推翻或修正的既有说法

| # | 原说法 | 出处 | 实测结论 |
| --- | --- | --- | --- |
| 1 | 「删掉 package-data 那段，`.md` 照样**全部进**包」 | `pyproject.toml:30-54` 的注释表格 | **错**。删掉 = **0 个**。四组实验（含带 `.git` 的一组） |
| 2 | 「这段是**冗余保险**，不是必需品」 | 同上 | **错**。它是唯一开关，且**它自己也是漏的**——少了 `rhinecode.subagents` |
| 3 | 「内置三个角色：`explorer` / `planner` / `general-purpose`」 | `CLAUDE.md` 的 C13 行 | 对 `pip install -e .` 成立，对 **`pip install .` 不成立**（一个都没有） |
| 4 | 「`textual>=0.80.0`」 | `pyproject.toml:10`（⚠ README 的 A2 与 R5 原始 Prompt 都写成 `:9`，差一行） | 真实下界 **6.2.1**，差 **6 个大版本**（不是 A2 估的 7 个——A2 是按「本机 8.2.8」倒推的） |
| 5 | A2 建议「改成 `textual>=8.0,<9`」 | `README.md` 的 A2 | 8.0 偏高。**6.2.1 起全量 3,323 条全绿**；两个值都能用，取舍已在 R5-3 写明 |
| 6 | 「`test_subprocess_timeout` 的 `CHILD_SLEEP=6 / THRESHOLD=4.0` **明确不要动**」 | `CLAUDE.md` 测试一节 | 那条理由**只在 Windows 上成立**。判据量的是时间差，而时间差是 Windows 特有的症状——**在 Linux 上它量错了东西** |
| 7 | 「A2 的后果是：用户环境里若已有满足 `>=0.80.0` 的旧版本，启动直接 `ModuleNotFoundError`」 | `README.md` 的 A2 | **只对 < 2.0.0 成立**。2.0.0 ~ 6.2.0 之间 **import 是过的**，失败形态是**运行期行为不对**（最后那一档只有「复制不全」，界面完全正常）——**比 A2 描述的更隐蔽** |

⚠ 第 7 条值得单独看一眼：A2 把这条问题的后果写成「装不上 / 起不来」，
所以它读起来像个**响亮**的故障。实测下来，**0.80 ~ 1.0 才是响亮的**，
2.0 ~ 6.2 是**安静的**——而 pip 在「用户环境里已有 textual」时最可能停在哪一档，
取决于那个环境，没法一概而论。**下界写对了两种都不会发生；写错了两种都可能。**

---

# 附二 · 怎么复现

全部实验在 scratchpad 里做，仓库工作区未被改动（`git status --porcelain` 全程为空）。

```bash
# ── ① textual 二分（Windows）───────────────────────────────────────────
python -m venv <SP>/venv-bisect
<SP>/venv-bisect/Scripts/python -m pip install "openai>=1.50.0" "pyyaml>=6.0" "httpx>=0.27"
<SP>/venv-bisect/Scripts/python -m pip install --no-deps .   # 不装它，4 条起子进程的用例会假红
for V in 0.80.0 0.89.1 1.0.0 2.0.0 ... 8.2.8; do
  <SP>/venv-bisect/Scripts/python -m pip install -q "textual==$V"
  <SP>/venv-bisect/Scripts/python -c "import rhinecode.tui.widgets, rhinecode.tui.app"   # 快筛
  <SP>/venv-bisect/Scripts/python -m unittest tests.test_tui_layout tests.test_tui_markup_escape \
      tests.test_tui_panels tests.test_tui_batch tests.test_tui_selection tests.test_tui_status_line \
      tests.test_tui_history_view tests.test_tui_activity tests.test_tui_detail_level \
      tests.test_tui_quit tests.test_todo_tui tests.test_ask_user_panel                 # 239 条
done
# 边界再用全量复核：<SP>/venv-bisect/Scripts/python -m tests.run_parallel

# ── ② 干净安装 + 打包实验（Windows）──────────────────────────────────
python -m venv <SP>/venv-clean
<SP>/venv-clean/Scripts/python -m pip install .          # 注意不是 -e
<SP>/venv-clean/Scripts/rhine --help
cd <SP> && <SP>/venv-clean/Scripts/python -c "import rhinecode, pathlib; \
  root=pathlib.Path(rhinecode.__file__).parent; \
  print(sorted(str(p.relative_to(root)) for p in root.rglob('*.md')))"
# 必须 cd 出仓库根，否则 import 到的是本地源码树，看到的 .md 是原件不是包里的
<SP>/venv-clean/Scripts/python -m pip wheel --no-deps -w <SP>/wheelout .   # 每组之前 rm -rf build

# ── ③ Linux（WSL2 / Ubuntu 24.04）──────────────────────────────────────
wsl -e bash -lc "python3 -m venv --without-pip ~/r5venv && \
  curl -sS -o /tmp/get-pip.py https://bootstrap.pypa.io/get-pip.py && \
  ~/r5venv/bin/python /tmp/get-pip.py"      # 24.04 无 python3-pip 且 sudo 要密码，这样绕开
wsl -e bash -lc "cd /mnt/g/RhineCode-Agent && tar --exclude=.git --exclude=build \
  --exclude=__pycache__ --exclude=.rhinecode -cf - . | (mkdir -p ~/r5src && cd ~/r5src && tar xf -)"
# 一定要复制到 Linux 原生 fs 再跑；直接在 /mnt/g 上跑会被 9p 文件系统拖慢一个量级
wsl -e bash -lc "cd ~/r5src && ~/r5venv/bin/python -m pip install . && \
  export PATH=\$HOME/r5venv/bin:\$PATH && python -m tests.run_parallel"
# PATH 里要有 venv 的 bin，否则 4 条硬编码 `python` 的用例会红（R5-8）
```

探针脚本（`linux_probe.py` / `probe2.py` / `fake_openai.py` + `provider_smoke.py`）
留在本次会话的 scratchpad 里，逻辑都很短，按上面的描述重写一遍比找回来更快。

⚠ **三个踩过的坑，复现时会撞上**：

1. **不装项目本身就跑测试**，会有 4 条起子进程的用例报 `No module named rhinecode`
   ——那是环境噪音，不是版本问题。用 `pip install --no-deps .`。
2. **在仓库根里 import rhinecode**，拿到的是本地源码树而不是 site-packages，
   于是「包里有没有那些 .md」这个问题**永远得到肯定回答**。必须 `cd` 出去。
3. **探针里两条路径共用一个标记文件**，会互相污染
   （R5-5 第一版就是这么得出「Linux 上没杀干净」的错误结论的）。

---

# 附三 · 给 F 系列的动作清单

| 归属 | 动作 | 依据 |
| --- | --- | --- |
| **F4**（pyproject） | package-data 补 `"rhinecode.subagents" = ["builtin/*.md"]` | R5-1 🔴 |
| **F4** | 改写 `pyproject.toml:30-54` 的注释，把错误的三组表换成正确的四组表 | R5-2 🔴 |
| **F4** | `textual>=6.2.1,<9`（或 `>=8.0,<9`，取舍见 R5-3） | R5-3 🔴 |
| **F4** | `rich>=13` 补上 | C2 + 本轮印证 |
| **F4** | `openai>=1.50.0,<4` 补上界 | R5-4 |
| **F1**（LICENSE） | MIT + `license = "MIT"` + `license-files = ["LICENSE"]` | R5-10 |
| **新增护栏** | 一条测试：`rhinecode/` 下每个非 `.py` 文件都被某条 package-data 模式覆盖 | R5-1（与 C3 / C4 同型，都是「漏改不报错」） |
| **测试修正** | `test_subprocess_timeout` 的反证判据换成标记文件（并同步 `CLAUDE.md` 那句「不要动」） | R5-5 |
| **测试修正** | `test_trace_full_output` 的 `python` 换 `sys.executable` | R5-8 |
| **R6**（CI） | 矩阵至少 `ubuntu-latest × windows-latest` × Python 3.11/3.12/**3.13**；外加一个用 textual 最新版跑的定时任务 | R5-3 / R5-5 |
| **README** | ① Linux 剪贴板依赖说明 ② 「与 Rhino / RhinoCode 无关」一句 ③ 命令名是 `rhine` | R5-6 / R5-9 |
| **待评估** | `resolve_stdio_command` 在非 Windows 上剥掉 `.cmd` 后缀重试 | R5-7 |
