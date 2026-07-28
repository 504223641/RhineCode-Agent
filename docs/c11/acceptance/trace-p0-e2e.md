# P0（Trace 记录器）端到端场景验收报告

> 对应 `docs/c11/testing/p0-trace/checklist.md` 第九节那 9 条【手测】场景。
> 它们当初留作手测，**只是因为当时没有能驱动界面的东西**；
> P1a 驱动设施交付后，其中 6 条已可无人驱动并**实跑通过**。
>
> 驱动方式：`python -m tests.e2e.host --mode scripted --script tests.e2e.p0_scenarios:<剧本>`，
> 剧本与预置见 `tests/e2e/p0_scenarios.py`。

## 总览

| 场景 | 判据 | 结果 |
| --- | --- | --- |
| 1 基本可用 | 9/9 | ✅ |
| 2 白名单外调用 | 6/6 | ✅ **确定性复现了原始 bug** |
| 3 作用域交错可读 | 7/7 | ✅ |
| 4 关闭时肉眼无差别 | — | ⏸ 需人眼（见「未跑的三条」） |
| 5 与会话恢复共存 | — | ⏸ 缺半个前置（见下） |
| 6 人在回路与拒绝路径 | 8/8 | ✅ |
| 7 压缩动作可解释 | 9/9 + 补充验证 | ✅ **并推翻了 checklist 自己的配方** |
| 8 落盘失败不阻断 | — | ⏸ 已被既有子进程用例覆盖 |
| 9 敏感产物确认 | 3/3 | ✅ |

**已跑 6 条 / 共 36 项判据全部通过。**

---

## 场景 1：基本可用

时间线脉络完整：会话启动 → 用户输入 → 每轮模型请求与响应 → 权限决策 → 工具执行
→ 界面消息 → 会话结束（29 条事件、坏行 0）。

**回答了原本答不出的问题**——模型第 1 轮实际收到了哪些工具：

```
turn=1  scope=main
tool_names = ['read_file','write_file','edit_file','run_command','glob_files','grep_content','load_skill']
system 全文长度 = 2033
```

## 场景 2：复现立项理由——白名单外调用（**本次最有价值的一条**）

P0 checklist 对这条写的是「**若本次未触发，如实记录未自然复现**」——因为原始 bug
是模型的**偶发幻觉、不可控**。而脚本化假模型可以**精确造出来**：

```
turn 1: 7 个工具全给
        skill_state   激活 readonly
turn 2: ['read_file','load_skill']          ← 白名单收窄到 2 个
        tool_execute  write_file · out_of_scope · ok=False · 不在当前工具集内
```

工具输出原文：

```
[工具不可用] write_file 不在当前 Skill 声明的工具集内，本轮未提供给你，因此没有执行。
当前可用工具：load_skill、read_file。
请改用其中之一；若确实必须用 write_file，请说明理由让用户决定。
```

**整个 trace 项目的立项动因，至此成了一条可重复的判据**，不再依赖运气。
这也是假模型相对真实模型的独特价值：**能确定性地造出真实模型只会偶发的行为**。

## 场景 3：作用域交错可读

30 条事件中 21 条落在 `isolated:solo`。按 checklist 的「具体判据」（而非「主作用域
只有两条事件」）逐条验：

- `scope=main` 里 `api_request` **0 条**、`tool_execute` **0 条**——子对话完全不污染主作用域
- 子对话的 3 次模型请求全在 `isolated:solo`
- 主历史（取会话存档，十五类事件没有一类承载它）**恰好 2 条配对消息**：
  `user: 执行 Skill /solo（独立跑一遍分析）参数：分析一下代码` /
  `assistant: 结论：代码结构清晰，无需改动。`

## 场景 6：人在回路与拒绝路径

**上半场**（选拒绝）——三条事件串成完整因果，`seq` 严格递增 9 → 10 → 11：

```
 9  permission_decision  write_file → ask（④模式）· 默认模式：无规则命中，交由用户确认
10  interaction          confirm → deny · source=driver
11  tool_execute         write_file · denied_by_user · ok=False
```
文件确实没被写出来。

**下半场**（危险命令）——第①层直接拒绝，**确认面板一次都没弹**（交互事件 0 条）：

```
 9  permission_decision  run_command → deny（①黑名单）· 递归强删：rm 同时带 -r/-R 与 -f 标志…
10  tool_execute         run_command · denied_by_permission · ok=False
```

## 场景 7：压缩动作可解释（**并推翻了 checklist 自己的配方**）

**第一层存盘**按预期工作，可解释性充分：

```
seq=20 layer=offload  tool_call_ids=['e2e_call_7']
                      paths=['…\.rhinecode\context\e2e_call_7.txt']  count=1
界面：📦 已把 1 个大型工具结果存盘，历史仅保留预览与路径。
```
存盘文件确实落在磁盘上（`e2e_call_7.txt` / `e2e_call_8.txt`）。

**第二层摘要**——checklist 原写的「用 `context_window: 8192` 触发」**是错的**：
它能让第二层**被走到**，但**永远压不动**，实测连续三次全是：

```
layer=summary ok=False 边界=0 1→1 skipped=no_early_segment
layer=summary ok=False 边界=0 3→3 skipped=no_early_segment
layer=summary ok=False 边界=0 5→5 skipped=no_early_segment
```

根因：`summarize.RETAIN_TOKENS = 10000` 是**固定常量、不随窗口缩放**，
窗口 8192 时保留区比整个窗口还大，早段恒为空。

**补充验证证明机制本身没问题**：换成默认 64K 窗口 + 多轮 user 消息 + 锚点顶到 6 万，
第二层**真的压缩成功 2 次**：

```
layer=summary ok=True  边界=2  7→7 条  摘要2条 保留5条
layer=summary ok=True  边界=4  9→7 条  摘要4条 保留5条
界面：🗜 已摘要早前 2 条消息，保留近 5 条原文。
```

> 一处判据修正：`reconstruct` 的结构是 `[摘要, 边界提示, *保留区]`，
> 故 `after_count = 2 + retained_count`。**摘要 2 条时条数持平（7→7）是正确的**
> ——换成了两条合成消息，条数不变但 token 变少。按「条数必须变少」判会误报。

checklist 的配方已据此修正；`RETAIN_TOKENS` 的局限已登记进「已知后续工程项」。

## 场景 9：敏感产物确认

两件事**同时成立**，与设计预期一致：

- ① 会话启动事件的配置快照里 `api_key = '***REDACTED***'`（`redact_config` 生效）
- ② 工具输出里**出现明文密钥**（已登记的边界，不是缺陷）：
  ```
  tool_execute · read_file · output:
    4│ api_key: sk-FAKE0000000000000000000000000000
  ```
  整份记录文件里确实存在该明文串。
- ③ `.gitignore:37` 的 `**/.rhinecode/traces/` 确实覆盖 traces 目录（`git check-ignore` 命中）。

> 本场景用的是**预置的假密钥**（`sk-FAKE000…`），不涉及任何真实凭据。

---

## 未跑的三条及原因

| 场景 | 原因 | 处置 |
| --- | --- | --- |
| **4 关闭时肉眼无差别** | 判据是「流式逐块出字、工具行逐个转色、状态栏随轮次变化」等**观感项**，且要求**不开 trace** 跑——没有观测面 | **保留手测**，这条真的需要人眼 |
| **5 与会话恢复共存** | `/resume` 那一半 P1a 已覆盖（`SessionPanelTest`）；缺的是「`--continue` 启动恢复」——宿主目前没有该参数 | **推给 P1b**（其范围清单里本就有「与会话恢复共存」） |
| **8 落盘失败不阻断** | 宿主总是自建记录器，没法指定坏路径 | **已被 `test_bootstrap.py` 的子进程用例覆盖**（三种 `--trace` 形态含不可写路径），不重复实现 |

## 本次验收顺带查出并修掉的问题

| # | 问题 | 后果 | 处置 |
| --- | --- | --- | --- |
| 1 | **P1a 宿主在 scripted 模式下完全无视 `--config`** | 传进去的 `context_window: 8192` 被静默丢掉、仍按 64K 判定，第二层永不触发——**现象出在 C8 那边**，排查绕了一大圈 | 已修（scripted 也认 `--config`，但 `api_key` 强制换假值）；护栏 `ConfigPassthroughTest` 两条，**经反证有效** |
| 2 | **P0 checklist 场景 7 的配方不成立** | 照做只会得到三次 `no_early_segment`，然后怀疑 C8 坏了 | checklist 已改写，给出实测可行的做法与三个必要条件 |
| 3 | `RETAIN_TOKENS` / `auto_margin` 不随窗口缩放 | **小窗口模型上第二层永远不会真正压缩**，历史一路涨到溢出 | 登记进「已知后续工程项」；不顺手改（改保留区语义需重新设计） |

> 第 1 条正是「静默丢弃参数」这类缺陷的典型：**调用方以为生效了，症状却出在别处**。
> 它也说明了驱动设施的价值——如果没有 P0 的 trace，这个问题会表现成
> 「C8 的第二层好像坏了」，几乎不可能定位。
