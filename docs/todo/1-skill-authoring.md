# 1 · Skill 作者期

> **优先级最高。** 这是唯一由**真实使用**暴露出来的缺口，不是推演出来的。
>
> 状态：待开工 · 建议走完整 `/spec` · 预计 1–2 天

## 背景

对齐改造让 Skill **可导入**了（从 Claude Code 拿一个目录复制进来就能用），
但仍然**不可创作**：系统只会读 `.rhinecode/skills/`，不会帮你写、不会检查你写得对不对。

缺口是用户实际用 RhineCode 做一个「番茄闹钟」时暴露的 —— 那次之前 C11 的
43/43 判据全过，却完全没覆盖到「用户想创建/导入一个 Skill」这条路径。

## ⚠️ 这件事要**重做**，不是接着做

`c11-authoring` 分支上曾做过一版「Skill 体检」（`validation.lint_skill` /
`lint_skills`），四条检查里**有三条是针对已废止的「白名单收窄」语义写的**，
随对齐改造一起删掉了。`tests/test_skill_validation.py::ObsoleteApiRemovedTest`
现在还钉着这两个函数**必须不存在**。

新语义下「该体检什么」是个全新问题，别去翻旧实现照抄。

## 范围（建议）

三件事，可拆可合：

1. **`/skills` 体检** —— 加载时给出可操作建议，而不只是报错。候选检查项：
   - `description` 过长（旧版实测有人写了 218 字符，把 `/skills` 列表撑成三行）
   - 正文里没有 `$ARGUMENTS`（参数会被追加到末尾，作者以为没生效）
   - **预授权给得过宽**（如 `allowed-tools: [Bash]` 等于所有命令免确认）
   - 声明了 `when_to_use` 但 `description` 为空（模型判断不了何时该用）
   - 声明的工具名一个都认不出（现在只警告，容易被淹没）
2. **内置 `skill-author` Skill** —— 用户说「帮我把刚才那套流程做成 Skill」或
   「我下了个 Skill 帮我适配一下」时，由它引导：模型判断 + 用户确认。
   **刻意不做 `/skills import` 自动适配** —— 格式差异是语义的不是语法的，
   代码只能猜，而**猜错是静默的**。
3. **`/skills new <名字>` 脚手架** —— 生成一份带注释的模板文件。

## 判据

- 一个不懂 frontmatter 的用户，说一句「把这个流程存成 Skill」就能得到一份能跑的 Skill
- 写得有问题的 Skill 在 `/skills` 里能看到**可操作的**建议（不是「警告：xxx」而是「建议改成 xxx」）
- 体检是纯函数、零 IO，可单测

## 要读的文件

- `docs/c11/align/spec.md` —— 现行 Skill 语义（**不要读顶层 `spec.md`，那是已废止的旧设计**）
- `rhinecode/skills/validation.py` —— 预授权翻译，体检大概率放这里
- `rhinecode/skills/manager.py` 的 `report()` —— `/skills` 报告的渲染处
- `rhinecode/skills/builtin/*.md` —— 三个内置样板，新 Skill 加在这里
- `CLAUDE.md` 的「成对维护点」—— 新增内置样板要确认 `pyproject.toml` 的 package-data

## 已知的坑

- `SkillManager` 的**加锁不变量**：临界区只做纯内存读写，回调与 IO 一律在锁外。
  违反会与 Textual 阻塞式 `call_from_thread` 组成确定性死锁、整个 TUI 冻结。
- 新增内置样板要确认 `pyproject.toml` 的 `[tool.setuptools.package-data]` 仍覆盖
  `builtin/*.md` —— 漏了的话 `pip install -e .` 正常，但真安装后样板凭空消失且不报错。
- 体检结果**每次现算**，不要存起来 —— 存了就要考虑何时失效，多一处「reload 之后忘了更新」的机会。

---

## 一键开工 Prompt

```
做 RhineCode 的「Skill 作者期」，走完整 /spec 流程（spec → plan → task → checklist，每份都要我审批）。

背景：对齐 Agent Skills 开放标准的改造让 Skill 可导入了，但仍然不可创作——
系统只会读 .rhinecode/skills/，不会帮用户写、不会检查写得对不对。这个缺口是
真实使用（做番茄闹钟那次）暴露的，此前 43/43 端到端判据全过却完全没覆盖到。

范围（可在 spec 阶段与我讨论调整）：
1. /skills 体检：加载时给出**可操作建议**而不只是报错。候选检查项见
   docs/todo/1-skill-authoring.md
2. 内置 skill-author Skill：用户说「帮我把这套流程做成 Skill」或「我下了个
   Skill 帮我适配」时引导完成。**刻意不做 /skills import 自动适配**——格式
   差异是语义的不是语法的，代码只能猜，猜错是静默的
3. /skills new <名字> 脚手架

⚠️ 这件事要**重做不是接着做**：c11-authoring 分支上那版体检的四条检查里有三条
针对已废止的「白名单收窄」语义，已随对齐改造删除，
tests/test_skill_validation.py::ObsoleteApiRemovedTest 还钉着它们必须不存在。
别去翻旧实现照抄。

先读 docs/c11/align/spec.md 了解现行语义（**不要读 docs/c11/spec.md，那是已废止
的旧设计**），再读 rhinecode/skills/validation.py 与 manager.py 的 report()。

注意 SkillManager 的加锁不变量：临界区只做纯内存读写，回调与 IO 一律在锁外，
违反会与 Textual 的 call_from_thread 组成确定性死锁。

做完这条后把 docs/todo/1-skill-authoring.md 删掉，并重排 docs/todo/ 下其余文档的序号。
```
