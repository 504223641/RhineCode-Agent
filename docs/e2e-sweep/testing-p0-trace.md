# 测试设施：Trace 行为记录器（P0）—— 复测

> 对应 `docs/c11/testing/p0-trace/checklist.md` 第九节场景 1–9（原标注【手测】）。
> **本次复测的整个过程本身就是这个设施的实证**——C2–C11 的全部判据都是从 trace 里读出来的，
> 一次「现写脚本捞证据」都没有发生。下面只补记逐条判据。

## 总览

| 场景 | 结果 | 说明 |
| --- | --- | --- |
| 1 基本可用：跑完 + 读日志 | ✅ | 官方 reader 时间线脉络完整 |
| 2 复现立项理由：白名单外调用 | ⊘ **判据已过时** | 收窄语义随对齐改造删除，见下 |
| 3 作用域交错可读 | ✅ | `--scope main` 里子作用域事件数为 **0** |
| 4 关闭时肉眼无差别 | ✅ 部分 | 可机判部分通过；「观感是否更卡」属主观项 |
| 5 与会话恢复共存 | ✅ 一半 | `resume_command` 已验；`--continue` 来源 P1a 驱动不了 |
| 6 人在回路与拒绝路径 | ✅ | 三事件因果链完整；黑名单路径无交互事件 |
| 7 压缩动作可解释 | ✅ | 两层都在 trace 里可解释（见 [c8.md](c8.md)） |
| 8 落盘失败不阻断 | ✅ | 降级 NullRecorder、零文件、序号不占号 |
| 9 敏感产物确认 | ✅ | `api_key` 掩码、`.gitignore` 已覆盖 |

**7 条通过、1 条部分、1 条判据已过时。未发现设施缺陷。**

---

## 场景 1：基本可用

`python -m rhinecode.trace.reader <文件>` 的时间线（节选）：

```
    1  04:01:35.504 main         skill_state          白名单绑定 · Skill 4 个
    2  04:01:35.511 main         session_start        deepseek/deepseek-v4-flash · 工具 8 个 · 根 …
    4  04:01:41.329 main         user_input           [message] 用 web_fetch 抓 https://a.test:8443/x?token=abc …
    7  04:01:41.365 main         api_request          turn 1 · deepseek-v4-flash · 消息 2 条 · 工具 8 个 · thinking=off
   10  04:01:42.625 main         api_response         turn 1 · 1250ms · 工具调用 1 个
   11  04:01:42.626 main         permission_decision  web_fetch → ask（④模式） · 默认模式：无规则命中，交由用户确认
   12  04:01:54.827 main         interaction          confirm → allow_permanent · web_fetch {...}
   14  04:01:54.830 web_extract  api_request          turn 1 · …
   16  04:01:55.803 main         tool_execute         web_fetch · executed · ok=True · 984.0ms · 串行 · 抓取 a.test
```

**判断**：脉络一目了然——会话启动 → 用户输入 → 模型请求/响应 → 权限决策 → 人在回路交互
→ 工具执行 → 界面消息。`--seq 7` 展开后能看到该轮**实际发出的 8 个工具**与完整系统提示。通过。

> **顺带发现一个此前没注意的作用域**：`web_extract`。`web_fetch` 的「按提问抽取要点」
> 那一步是一次独立的模型调用，它有自己的作用域，不混进 `main`。
> 这在 CLAUDE.md 里没写，但设计是对的。

## 场景 2：判据已随对齐改造失效

原场景要求：造一个 `allowed_tools: [read_file]` 的 Skill 使**工具集收窄**，
然后让模型越界调用写文件工具，在 trace 里找结局为「白名单外」的 `tool_execute`。

**这条判据已经不成立了。** C11 的对齐改造把 `allowed-tools` 从「收窄可见工具集」
改成了「预授权免确认」，**收窄能力整体删除**——现在无论怎么写，模型能看到的工具集都不变，
「白名单外调用」这个结局在产品里已经不存在。

本次复测有直接观测佐证：`legacyfmt` Skill 声明了旧格式 `allowed_tools: Read`，
执行时它照常调用了**白名单之外**的 `glob_files` 并成功（见
[c11-align.md](c11-align.md) 场景 6）。

**处理**：记为「判据已过时」，不计入通过/失败。建议在
`docs/c11/testing/p0-trace/checklist.md` 里就地标注这一条已被对齐改造推翻
（与 CLAUDE.md 已知项 #12 那种「死代码留待清理」同类）。

## 场景 3：作用域交错可读

用官方 reader 的过滤器验：

```
python -m rhinecode.trace.reader <文件> --scope web_extract   → 4 条（全是它自己的）
python -m rhinecode.trace.reader <文件> --scope main | 含 web_extract 的行数 → **0**
python -m rhinecode.trace.reader <文件> --type api_request --scope summary → 2 条（交集正确）
```

子对话侧的实证在 [c11-align.md](c11-align.md) 场景 4：`isolated:deepreview` 作用域下有
完整的 `api_request` / `permission_decision` / `tool_execute` 序列，
而 `main` 侧只有 `load_skill` 的调用与结果**那一对**，主历史没被子对话的多次读文件撑大。

**判断**：两条时间线各自完整、互不混淆；`--type` 与 `--scope` 可取交集。通过。

## 场景 4：关闭时肉眼无差别

可机判的那部分（直接调产品代码）：

```
关闭态使用: NullRecorder
emit_lazy 刻意不调用 factory: True
emit / emit_lazy 均无副作用、无异常
```

`emit_lazy` **不调用 factory** 这一条尤其关键——它意味着关闭态下连负载构造的开销都没有。

「① 流式逐块出字 ② 工具行转色 ③ 状态栏随轮次变化 ④ 确认面板四选项可选」
这四个二元判据本次全部在**开启 trace 的情况下**观察到了（见各章报告），
逻辑上关闭态只会更快。「整体观感是否更卡」是主观项，checklist 自己也说不作红绿门。

**判断**：通过（可机判部分）。

## 场景 5：与会话恢复共存

```
   94  04:08:41.709 main  history_restored  **resume_command** · 14 条 · session 20260731-040135-9nmp
```

展开：

```json
{"type": "history_restored", "origin": "resume_command",
 "message_count": 14, "session_id": "20260731-040135-9nmp"}
```

**判断**：`/resume` 这一侧完全正确——事件存在、来源标为 `resume_command`、
消息数与会话 ID 都对。

**另一半（`--continue` 的「启动恢复」来源）P1a 驱动不了**：宿主刻意没有
`--continue` 参数（它每次都建全新临时工作区，也就没有「上一次会话」）。
记为设施限制。

## 场景 6：人在回路与拒绝路径

**三事件因果链**（同一次 `web_fetch`，从 reader 的 `--type permission_decision,interaction` 读出）：

```
   58  permission_decision  web_fetch → **ask**（④模式） · 默认模式：无规则命中，交由用户确认
   59  interaction          confirm → **deny** · web_fetch {'url': 'http://b.test/other', …}
       tool_execute         denied_by_user / denied_by_permission
```

**黑名单路径**（见 [c6.md](c6.md) 场景 1 与 [c11-align.md](c11-align.md) 场景 3）：

```
  124  permission_decision  **blacklist** 命中危险命令黑名单：递归强删：rm 同时带 -r/-R 与 -f 标志…
  125  tool_execute         denied_by_permission · ok=False
```

**这两条之间没有任何 `interaction` 事件**，驱动器全程 `terminal: idle`
——第①层直接拒绝，根本不进人在回路。

**判断**：两条路径的因果链都能从 trace 里串起来，且能清楚区分。通过。

## 场景 7：压缩动作可解释

第一层与第二层都在 [c8.md](c8.md) 里有完整证据：

- 第一层：`ui_message` 报「📦 已把 1 个大型工具结果存盘」，`.rhinecode/context/` 下
  出现 355 KB 的落盘文件，`/context` 计数同步；
- 第二层：`summary` 作用域的独立 `api_request`（**`tools=None`**）+ `context_compaction`
  事件 + `ui_message` 报「🗜 已摘要早前 28 条消息，保留近 16 条原文」。

**判断**：「这轮消息为什么突然短了」这个原本答不出的问题，现在从 trace 里能直接答出来
——摘要了哪几条、保留了几条、走的哪个作用域、发的什么请求。通过。

> checklist 里那段「原先写的 `context_window: 8192` 配方是错的」的更正，本次沿用了它的
> 新做法（32K 窗口 + 多轮 user 消息），一次就跑通，见 [c8.md](c8.md) 场景 4。

## 场景 8：落盘失败不阻断

用「父路径是普通文件」构造确定性写入失败：

```
=== 场景 8：落盘失败不阻断 ===
  create_recorder 返回: NullRecorder
  emit / emit_lazy 各调一次：未抛异常
  产生文件: False
（stderr 另有一行：已跳过行为记录：[WinError 183] 当文件已存在时，无法创建该文件。…）
```

再验负载构造异常与序号推进：

```
=== 场景 8b：负载构造异常不外抛，且序号只在成功后推进 ===
  factory 抛异常：emit_lazy 未外抛
  文件行数: 2
  各行 seq: [1, 2]
  序号连续（失败那条没占号）: True
```

**判断**：三件事都对——① 构造失败降级为 `NullRecorder`，进程照常起；
② 后续 `emit` / `emit_lazy` 全部无害；③ **序号只在 flush 成功后推进**
（失败那条没占号，seq 保持 `[1, 2]`）。第三条正是 CLAUDE.md 架构表里
trace 那一行标的致命不变量。通过。

## 场景 9：敏感产物确认

`session_start` 的配置快照（`--seq 2` 展开）：

```
  "protocol": "deepseek",
  "model": "deepseek-v4-flash",
  "base_url": "https://api.deepseek.com",
  "api_key": "***REDACTED***",
  "context_window": 1000000
```

`.gitignore` 相关行：

```
**/.rhinecode/context/
**/.rhinecode/sessions/
**/.rhinecode/memory/
**/.rhinecode/traces/
```

**判断**：配置快照的 `api_key` 已被固定掩码替换；`traces/` 目录已在忽略列表里。通过。

> **本次复测的产物处理**：所有 trace 产物落在宿主的**临时**工作区里，随宿主退出一并删除；
> 我另外归档到会话 scratchpad 的那几份**不在仓库内**，不会被提交。
> 本次没有让模型读取过含真实密钥的文件，所以工具输出里没有明文密钥
> ——但那条已登记的边界依然成立（trace 会原样记录工具输出）。
