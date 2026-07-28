# C11 文档导航

本目录下有**三块内容**，它们的性质不同，别混着读：

| 目录 | 是什么 | 占章节号吗 |
| --- | --- | --- |
| 顶层四份 + `align/` | **产品能力**：Skill 系统 | 是（C11） |
| `testing/` | **跨阶段测试设施**：Trace 记录器与端到端驱动器 | **否** |
| `acceptance/` | 上面两块的**实跑验收记录** | — |

```
docs/c11/
├── README.md                       ← 你在这里
│
├── spec.md  plan.md  task.md  checklist.md     ← C11 原始设计（Skill 系统）
├── align/                          ← 对齐 Agent Skills 开放标准的改造
│   └── spec.md  plan.md  task.md  checklist.md
│
├── testing/                        ← 跨阶段测试设施（不占章节号、不属于 Skill 系统）
│   ├── brief.md                    ← 需求交底，同时覆盖下面两期
│   ├── p0-trace/                   ← P0：行为记录器（`--trace`）
│   │   └── spec.md  plan.md  task.md  checklist.md
│   └── p1-driver/                  ← P1a：端到端驱动设施（`tests/e2e/`）
│       └── spec.md  plan.md  task.md  checklist.md
│
└── acceptance/                     ← 四份实跑验收记录
    ├── skills-c11-live.md          ← C11 原始设计的真实模型验收（10 场景 43/43）
    ├── skills-align-live.md        ← 对齐改造的真实模型验收（7 场景 29/29）
    ├── trace-p0-e2e.md             ← 用 P1a 驱动设施验收 P0（6 场景）
    └── driver-p1a.md               ← P1a 自身的验收
```

---

## ⚠️ 冲突时以谁为准

**`align/` 覆盖顶层四份。**

顶层那四份是 C11 的原始设计，`align/` 是**对同一章节的改造**（不是新章节）。
两者在若干处语义相反，改造后的行为以 `align/` 为准：

| 议题 | 原始 C11（已废止） | 现行（`align/`） |
| --- | --- | --- |
| `allowed-tools` | **收窄**模型可见的工具集 | **预授权**——列出的操作免确认，**不限制**模型能调什么 |
| 命令名来源 | frontmatter 的 `name` | **文件系统路径**（目录名 / 去扩展名的文件名） |
| 执行模式 | `mode: shared` / `mode: isolated` | `context: fork`；不写即留在主对话 |
| 「谁能触发」 | 与「在哪执行」**捆在一起**（isolated 隐含只许用户发起） | 正交两维：`disable-model-invocation` / `user-invocable` |
| 带入历史 | `history_messages: N` | 已删除（标准无此概念，子对话只带那条自包含调用消息） |
| frontmatter 必填项 | `name` / `description` 必填 | **全部可选**，纯正文也是合法 Skill |
| 白名单笔误 | 启动 fail-fast | 跳过 + 警告，**不 fail-fast** |

**为什么不把原始那四份删掉**：它们记录了「当初为什么那样设计」，
而改造文档只讲「现在改成什么」。真正想知道 `allowed-tools` 为什么会有
两种相反语义的人，需要两份都读。删掉等于把这段历史扔了。

顶层 `checklist.md` 中与上表冲突的条目已不再适用，
**以 `align/checklist.md` 为准**。

---

## `testing/` 为什么在 C11 底下

Trace 记录器与端到端驱动设施**不是产品功能、不占章节号、不属于 Skill 系统**。
它们服务 C2–C11 已完成能力与未来所有阶段的验收，专治那类
「界面上看不出、但行为确实不对」的问题。

放在 `docs/c11/` 底下只是因为它们在 C11 期间开发——**挨着当前主线章节，
但层级上独立**。请不要在任何 spec 里把它们写成「C12」或 Skill 系统的一部分。

`p0-trace/` 与 `p1-driver/` 是同一个设施的**两期**，不是父子关系：
P0 解决「看清实际发生了什么」，P1a 解决「让 Claude 自己把交互跑起来」。
`brief.md` 同时覆盖两期（其 §4 是组件一 Trace、§5 是组件二驱动器），
所以它放在 `testing/` 这一层而不是任何一期里面。

**P1b（无人值守回归）尚未开工**，其范围清单在
`testing/p1-driver/spec.md` 的末节。

---

## 从哪份开始读

| 你想知道 | 读这份 |
| --- | --- |
| Skill 系统现在是什么行为 | `align/spec.md` |
| 怎么写一个 Skill / 怎么从 CC 搬一个过来 | 仓库根 [`README.md` 的「Skill 系统」一节](../../README.md#skill-系统) |
| 某条设计当初为什么那么定 | `spec.md`（原始）→ `align/spec.md`（改造理由） |
| 实跑时到底发现了什么问题 | `acceptance/` 下四份，每条判据都分「机器判到了什么」与「据此做的判断」 |
| 怎么用 `--trace` 排查行为问题 | `testing/p0-trace/spec.md` + 根 `CLAUDE.md` 的「常用命令」 |
| 怎么让 Claude 自己驱动界面跑场景 | `testing/p1-driver/spec.md` + 根 `CLAUDE.md` 的「常用命令」 |

> 验收记录尤其值得读：四份加起来找出的十几个缺陷里，**绝大多数逃过了全部单测**，
> 共同点是落在两个模块的**接缝处**——单测各自验一侧，交界处没人验。
