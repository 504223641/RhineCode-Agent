# `Esc` 不会取消正在跑的子 Agent

> 建议分支：`subagent-cancel-semantics` · 复杂度：小（实现半天）
> **但必须先由用户拍板要哪种语义** —— 它改的是 C13 的对外契约，不是纯实现活
> 来源：C14 真实模型端到端验收（2026-08-08），记录见 `docs/c14/acceptance/live-model.md` 第四节 A

## 问题

按 `Esc`（真人取消入口）只停主循环，**子 Agent 线程继续跑到底**。

实测现场：委派一个大活（补 docstring + 改 README + 跑测试 + 提交），
25 秒后按 `Esc`。主循环立刻回到 idle，而子 Agent **又跑了 7 轮**，
写了文件、跑了测试、`git commit`、留下一个新的隔离工作区与分支：

```
（Esc 之后才发生的事件）
worktree_settle  scope=subagent:dev  removed=false dirty=false commits=1
subagent_end     scope=subagent:dev  status=completed turns=7 usage_tokens=44921
```

## 为什么它要紧

`conversation.request_cancel()` 只置 `self._cancel_event`——那是主循环的信号。
子 Agent 的 `TaskRecord.cancel_event` 只有三处会置：`/agents cancel`、
`/clear`、`/resume`（`cancel_all_for_session_switch`）。**`Esc` 一处都不碰。**

两条后果，第二条更严重：

1. **用户的心智模型是「Esc = 停」**，而实际是「Esc = 我不等了」。
   之后还会有 token 在烧、还会多出一个他没要的工作区与分支。
2. **非隔离子 Agent 会继续往主项目根里写。** 隔离让这件事不至于造成损害
   （写的都在工作区里），但缺省的委派是不隔离的——那时用户按了 Esc，
   文件还在被改。

## 这不是「显然的 bug」，所以要先决策

C13 的设计里有一条明确的对外契约：**委派永不阻塞，后台任务自己跑完**
（CLAUDE.md 安全边界 c13 第⑧条：「等待期间唯一的逃生口是 `Esc`」——
那句话说的是**从等待里出来**，它兑现了）。

所以现状是**契约内的行为**，只是与用户直觉不符。三种可选语义：

| 方案 | 语义 | 代价 |
| --- | --- | --- |
| **A** 保持现状 | `Esc` = 「我不等了」 | 直觉不符照旧；但可以在按下 Esc 时**明确提示**「N 个子 Agent 仍在后台运行，用 `/agents cancel all` 停止」——**成本最低，且立刻消除误解** |
| **B** `Esc` 连带取消全部子 Agent | `Esc` = 「全停」 | 与「委派永不阻塞」冲突：一次 `Esc` 会杀掉用户可能还想要的后台任务（`background=true` 那些是模型明说过不等的） |
| **C** 只取消 `awaited=True` 的 | `Esc` = 「停掉我在等的那些」 | 语义最贴，但引入「哪些会被 Esc 杀掉」这个用户看不见的区分 |

**倾向 A + 提示**：它不动契约、不动任何并发路径，而 90% 的困惑来自「不知道它还在跑」。
B/C 都要重新论证 C13 的委派契约。

## 要改哪里

- **方案 A**：`tui/app.py` 的取消键处理 + `conversation.request_cancel()`
  ——取消时查一次 `subagent_service.tasks` 里 `RUNNING` 的条数，非零就发一条
  系统消息。⚠ 注意 `TaskManager` 的加锁不变量：临界区只做纯内存读写，
  消息推送必须在锁外。
- **方案 B/C**：`request_cancel` 里连带置 `record.cancel_event`
  （`Event.set()` **必须在锁外**——它唤醒等待线程，属跨线程调度，
  这是 `TaskManager` 的既有硬不变量）。

护栏至少两条：按 Esc 后正在跑的子 Agent 的**去留**符合所选语义；
以及「Esc 之后不再有新的 `worktree_create`」（方案 B/C）或
「提示里的条数与实际 RUNNING 数一致」（方案 A）。

## 开工 Prompt

```
先跑 git branch --show-current；如果在 main 上，先 git checkout -b subagent-cancel-semantics。

读 docs/todo/2-subagent-cancel-semantics.md，以及 docs/c14/acceptance/live-model.md
第四节 A（实测现场）。

⚠ 动手前先问用户要 A / B / C 哪种语义——这一条改的是 C13 的对外契约
（「委派永不阻塞」），不是纯实现活，别自己替他选。

选定后按文档「要改哪里」实现，注意 TaskManager 的两条既有硬不变量：
加锁临界区只做纯内存读写、Event.set() 一律在锁外。

补护栏并跑全量 unittest。做完删掉这份 todo。
```
