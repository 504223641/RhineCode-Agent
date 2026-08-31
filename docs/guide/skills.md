# Skill 系统

> RhineCode 用户手册 · [返回手册目录](README.md) · [返回项目 README](../../README.md)

## Skill 系统

把重复输入的提示词封装成独立的 Markdown 文件，让模型按既定流程干活。**已对齐
[Agent Skills 开放标准](https://agentskills.io)**——从 Claude Code 或 Codex 拿一个
Skill 目录复制进来就能用，不需要改任何东西。

### 写一个 Skill

**不用写代码。** 在 `<项目根>/.rhinecode/skills/` 放一个 `.md` 文件即可：

```markdown
---
description: 按项目约定生成提交信息并提交
when_to_use: 用户说「提交」「commit 一下」时
allowed-tools: [Bash(git *), Read]
---

1. 跑 `git status` 与 `git diff` 看清改了什么
2. 跑 `git log --oneline -15` 学本仓库的提交信息风格
3. 照抄那个风格写一条信息，然后提交

用户的补充要求：$ARGUMENTS
```

存成 `commit.md`，`/commit` 这条命令就有了。

**frontmatter 全部可选**——一份只有正文的 `.md` 也是合法 Skill（说明从正文第一段提取）。

| 字段 | 作用 |
| --- | --- |
| `description` | 一句话说明，进第一阶段清单让模型知道有这个东西 |
| `when_to_use` | 什么时候该用它。想让模型**自己**判断要不要用，这条比 `description` 更管用 |
| `allowed-tools` | **预授权**：列出的操作在本次执行内免于人工确认。取值如 `Bash(git *)` / `Read` / `Write` / `Edit` |
| `context: fork` | 开一条子对话跑完，只把结论回流主对话（适合「读二十个文件才得出一句结论」的活） |
| `disable-model-invocation` | 模型不得自行发起，只能由用户敲命令触发 |
| `user-invocable: false` | 不进斜杠命令菜单，但模型仍可自行发起（适合纯背景知识型） |
| `model` | 这个 Skill 用别的模型跑 |
| `$ARGUMENTS` | 正文里的占位符，承接用户在命令后面写的参数 |

### ⚠️ `allowed-tools` 是预授权，不是限制

这是最容易理解反的一点：它**放宽**权限（让列出的操作不再逐次弹确认框），
**不收窄**模型能调用什么。写 `allowed-tools: [Read]` 不会阻止模型执行命令，
只会让读文件不再打断你。

要**限制**模型能做什么，用 `permissions.yaml` 的 `deny` 规则——那才是安全边界。

预授权翻不过前两层防线，也在你发出下一条消息时失效。

### 存放位置

同名时按 **项目级 > 用户级 > 内置** 整份覆盖（不做字段合并）：

| 位置 | 用途 |
| --- | --- |
| `<项目根>/.rhinecode/skills/` | 随仓库走、可提交、团队共享 |
| `~/.rhinecode/skills/` | 跨项目的个人默认 |
| 随包内置 | commit / review / test 三个样板，外加目录型的 skill-creator（创作 / 适配 / 按建议修复 Skill） |

**命令名来自文件路径**，不是 frontmatter 里的 `name`——`foo.md` 就是 `/foo`，
目录 `bar/`（内含 `SKILL.md`）就是 `/bar`。这正是「外部 Skill 原样可用」的地基：
不必检查也不必修改别人写的 frontmatter。

目录型 Skill 可以带随附资源（模板、示例、参考文档），其绝对路径与清单会一并注入，
模型按需读取。

### 管理

| 命令 | 作用 |
| --- | --- |
| `/skills` | 列出全部 Skill：来源层级、在哪执行、激活状态、加载错误与字段提示，**以及体检建议段**（八项检查，每条都给出具体改法；无建议时整段不出现） |
| `/skills prompt` | 查看**实际注入**了什么。排查「为什么模型没按我的 Skill 做」时用这个 |
| `/skills reload` | 改完文件热更新，短命令一并重新注册 |
| `/skills off [名字]` | 卸载指定或全部激活项 |
| `/skills run <名字> [参数]` | 通用执行入口（短命令与内置命令重名被跳过时用它） |

体检**只观测、不改判定**：它不影响任何 Skill 的加载结果，也不影响权限管线的任何一层，
且**不读任何文件内容**（纯函数零 IO，输入只有已解析的定义）。看到建议想照着改时，
可以用内置的 `/skill-creator` —— 它承担创作、适配外部 Skill、按建议修复三种用途，
**全部写盘走完整权限管线**（`allowed-tools` 只预授权只读调研，写入与编辑刻意不给）。

> ⚠️ **本版本只能创建项目级 Skill。** 用户级与内置目录都在工作区之外，
> 写类判定被第②层沙箱一律拒绝（只读白名单不覆盖写类），而预授权翻不过第②层。
> 这是结构性限制，不是没做。

### 从 Claude Code / Codex 搬过来

整个目录复制进 `.rhinecode/skills/` 即可。本版本没有对应能力的字段
（`background` / `agent` / `effort` / `hooks` / `paths` / `shell`）不会让 Skill 失效，
`/skills` 会**逐条告知本版本的实际行为**——比如 `background: true` 会明说
「将同步等待子对话跑完」，而不只是「不支持」。


