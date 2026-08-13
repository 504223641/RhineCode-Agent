# 只留两个模式：`auto` 与 `plan`

> 状态：待开工 · **建议走 `/spec`**（动的是权限档位语义，四份文档进
> `docs/extensions/auto-plan/`）· 预计 1 天
>
> 建议分支：`perm-auto-plan`（从 `main` 起）
>
> ⚠ **依赖第 2 条（保护路径层），顺序不可颠倒**，理由见「为什么必须排在 2 之后」。
> ⚠ **第 4 条（分类器）盖在本条之上**，本条**不做**分类器——先有一个能用的
> auto，分类器是之后接上去的一层。

## 目标

用户只想要两个模式。现状是**两个开关六种组合**：`/perm` 三档
（严格 / 默认 / 放行）× `/plan` 布尔量。

## 结构：两个 preset 盖在两条正交轴上

```
用户界面：   auto  ⇄  plan          （Shift+Tab 两态循环，或 /mode）

底层（不变）：权限档 × 规划阶段
            auto = PERMISSIVE 档 + 规划阶段关
            plan = 只读        + 规划阶段开
```

这是 **Codex 的做法**：它对用户只暴露 `Read Only` / `Auto` / `Full Access`
三个 preset，底下其实是 `sandbox`（能做什么）× `approval`（什么时候问）两个轴。
预设是组合，不是新概念。

⚠ **别把 `plan` 做成一个 `PermissionMode` 值。** 它是个阶段状态机
（规划 → `present_plan` → 审批 → 执行），跟「灰色地带怎么兜底」不是一个维度。
合并进枚举之后，「批准计划之后回到哪一档」会变成一个说不清的问题——
Claude Code 敢把 plan 并进 `Shift+Tab` 循环，是因为它退出 plan 时会**明确问你
「批准后进哪个模式」**（那三个选项就是在补这个维度差），本项目没有那一步。

## `auto` 到底放行什么

**`auto` 内部就是现有的 `PERMISSIVE` 档**——不新增枚举值，只改显示名。
加上第 2 条的②″保护路径层之后，实际行为是：

| 请求形态 | auto 下 | 由哪一层决定 |
| --- | --- | --- |
| 读（工作区内） | 放行 | 只读短路（既有） |
| 写（工作区内） | 放行 | ④模式兜底（既有） |
| 写（工作区外） | **拒绝** | ②路径沙箱（既有） |
| 写（保护路径） | **问** | ②″保护路径（第 2 条） |
| 命令 | 放行 | ④模式兜底（既有） |
| 命令（命中危险黑名单） | **拒绝** | ①黑名单，**不可被任何配置放开**（既有） |
| 网络 | **问** | ④对 url 类的既有例外 |
| MCP / `other` | 放行 | ④模式兜底（既有） |

**几乎全是既有行为。** 本条真正新增的只有两件事：preset 层，与显示文案。

### ⚠ 命令一律放行是用户明确要的，别自作主张改窄

评审时讨论过三种更保守的形态（已知安全命令集 / 内置 `auto_ask` 清单 /
仅放行只读命令），**用户逐条看过之后选了「一律放行」**，理由是体验要对齐
Claude Code 的 auto。实现时不要因为「这样不安全」而偷偷加回一份白名单——
真正的收窄手段是第 4 条的分类器，那是**另一条 todo**，不要提前混进来。

用户手里仍有两件硬工具，文档要写清楚让他知道：

- `permissions.yaml` 的 `deny` 规则（③层，压得过④）
- Hook 的 `pre_tool_use`（⓪层，排在整条管线之前）

## 为什么必须排在第 2 条之后

第 2 条讲的是：模型能写 `.rhinecode/permissions.yaml` / `hooks.yaml` /
`agents/` 给自己提权，而**唯一的实际拦截是默认档那次确认面板**。

本条恰恰是「把那次面板去掉」。顺序反了 = 安全性净下降。

⚠ 两条改**同一个文件**（`permission/engine.py`），要么串行（2 → 3），
要么并成一轮，**不要并行**。

## 任务清单

| # | 事项 | 备注 |
| --- | --- | --- |
| 3.1 | preset 层：两个 preset 映射到 `(权限档, 规划阶段)` | 新增领域方法，`conversation.py` |
| 3.2 | `PERMISSIVE` 对用户显示为「auto」 | 只改文案，枚举值不动（`permissions.yaml` 与角色定义里写的仍是 `permissive`，**不能破坏用户已有配置**） |
| 3.3 | `strict` / `default` 退出用户切换循环 | **保留在枚举里**——内置 `explorer` / `planner` 声明了 `permission_mode: strict`。手法对齐 Claude Code 的 `dontAsk`（永不进 `Shift+Tab` 循环，只能显式指定） |
| 3.4 | 切换入口：`Shift+Tab` 两态 + `/mode` | `/plan` 保留作别名；`/perm` 是留是废要定（倾向保留为「显式指定档位」的入口，与 3.3 配套） |
| 3.5 | 文案三处 | 状态栏 `compose_status_text`、`subagents/report.py` 的 `_MODE_LABELS`、命令描述 |
| 3.6 | **`run_command` 子进程不继承敏感环境变量** | 见下，命令全放行之后这条从「顺手」变成「该做」 |
| 3.7 | ⚠ 改 `CLAUDE.md` C13 安全边界第 ④ 条 | 见下 |
| 3.8 | ⚠ 改 `CLAUDE.md` 安全边界总述里描述缺省行为的段落 | 缺省档从「默认」变成「auto」，五层防御那段对**缺省体验**的描述整段失准 |
| 3.9 | `CLAUDE.md` 已知项 #4 补一句 | 登记「Claude Code 与 Codex **都不支持原生 Windows 沙箱**」（前者官方原话 *Native Windows is not supported*，后者文档只写 macOS Seatbelt 与 Linux Landlock/seccomp）——免得将来有人以为抄一下就行 |

### 3.6 单独说：环境变量

`run_command` 现在**原样继承 rhine 进程的环境变量**。用
`ANTHROPIC_API_KEY=xxx rhine` 这类方式启动时，那个 key 对**每一条被执行的
命令**都是可见的。命令还要弹面板时这只是个隐患，命令全放行之后它是常态。

修法很轻：构造子进程 `env` 时按黑名单过滤（`*_API_KEY` / `*_TOKEN` /
`*_SECRET` 之类）。**不需要任何 OS 能力**，与已知项 #4 无关。

⚠ 别做成白名单——那会让一大批正常命令（走代理的 `git`、认 `PATH` 的一切）
突然坏掉，而且坏得莫名其妙。

### 3.7 单独说：子 Agent 的行为变了

`CLAUDE.md` 安全边界 C13 第 ④ 条现在写着：

> **缺省配置下子 Agent 实际只能做只读的事**。判 ASK 自动拒绝意味着
> 写文件、跑命令都会被挡下。

**auto 成为缺省之后这句话不成立了。** 子 Agent 的生效档位是
`min(主对话档, 角色声明档)` = auto，于是它能写文件、能跑命令——后台、并行、
非交互、用户不在场。

这**不是 bug 是设计后果**，但必须写下来，否则下一个人会当成回归。
文档要同时给出保持旧行为的办法：给角色声明 `permission_mode: strict`
或 `default`，`narrower_mode` 会取更严的那个。

## 验证

单测之外必须真机跑五条：

1. auto 下连改三个文件 —— **一次面板都不弹**
2. auto 下跑 `python -m unittest` —— **不弹**（这是本条与「acceptEdits 方案」的分野）
3. auto 下让模型写 `.rhinecode/hooks.yaml` —— **弹面板**（第 2 条的落点）
4. auto 下让模型跑 `rm -rf /` —— **拒绝**（①黑名单仍在，不可被档位放开）
5. 切 plan → 提一个需求 → 确认规划阶段一个文件都没改 → 批准 → **自动回到 auto**

⚠ 第 3、4 条是两个方向的反证，缺一条就分不清「auto」与「什么都不管」。

## 一键开工 Prompt

```
先 git branch --show-current，在 main 上就 git checkout -b perm-auto-plan。

⚠ 先确认 docs/todo/2-perm-protected-paths.md 已经做完（或决定两条一起做）。
顺序不可颠倒：本条让"写文件不弹面板"，而那次面板正是第 2 条那个缺口的唯一
实际拦截。两条又改同一个文件 permission/engine.py，不要并行。

读 docs/todo/3-perm-auto-plan.md，然后走 /spec，四份文档进
docs/extensions/auto-plan/。

目标：用户界面上只留两个模式 auto 与 plan（Shift+Tab 两态 + /mode）。
底层保持两条正交轴不动，preset 只是它们的组合：
auto = PERMISSIVE 档 + 规划阶段关；plan = 只读 + 规划阶段开。

⚠ 别把 plan 做成 PermissionMode 值，它是阶段状态机不是兜底档位。
⚠ strict / default 保留在枚举里（内置 explorer/planner 声明了 strict），
只是退出用户切换循环——手法对齐 Claude Code 的 dontAsk。
⚠ 命令一律放行是用户明确选的，别偷偷加回白名单；真正的收窄是第 4 条的
分类器，那是另一条 todo。

连带三件：run_command 子进程按黑名单过滤敏感环境变量（命令全放行之后
每条命令都能看到 API Key）；改 CLAUDE.md 的 C13 安全边界第④条
（"缺省下子 Agent 只能只读"不再成立）与安全边界总述里描述缺省行为的段落；
已知项 #4 补一句"两家都不支持原生 Windows 沙箱"。

验证五条都要真机跑，第 3 条（写 hooks.yaml 仍弹面板）与第 4 条
（rm -rf / 仍被①黑名单拒绝）是两个方向的反证，不可省。
做完这条后把 docs/todo/3-perm-auto-plan.md 删掉，
并重排 docs/todo/ 下其余文档的序号。
```
