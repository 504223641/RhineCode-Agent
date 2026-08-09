# `system_serial` 的工具绕过③可配置规则层

> 建议分支：`perm-system-serial-bypass` · 复杂度：小（半天，**但要走安全评审**）
> 来源：C15 验收期按 checklist AC34 逐条实测发现（2026-08-09）
> 对应 `CLAUDE.md`「已知后续工程项」第 18 条
> ⚠ 与 [`1-perm-compound-command.md`](1-perm-compound-command.md) **同源**（都是③规则层被绕过），
> 建议一起做、一起评审

## 问题

`agent/loop.py` 的决策预扫里，`system_serial=True` 的工具**直接拿到一个 ALLOW
决策并 `continue`**，根本不调 `engine.decide`：

```python
if tool.system_serial:
    serial.append((
        tc, tool,
        self._apply_hook_ask(
            DecisionResult(Decision.ALLOW, Layer.RULE, "系统级工具，免确认"),
            hook_verdict,
        ),
    ))
    continue          # ← ③规则层完全不参与
```

于是 `permissions.yaml` 里写的 deny 规则**一条都不生效**。实测（C15 验收）：

```
deny: Bash-无关，直接写工具名
  deny: send_message

  无规则          → 工具执行，消息送达
  deny: send_message → 工具照样执行，消息照样送达   ❌
```

**影响 7 个工具**：`run_agent`、`load_skill`、以及 C15 的五个协作工具
（`task_create` / `task_list` / `task_get` / `task_update` / `send_message`）。

## 为什么它要紧

**危害不在「这些工具很危险」——它们其实不危险**：都不读写文件、不执行命令，
副作用限于起一条子对话或改进程内存。真正有副作用的是它们**引发**的工具调用，
而那些逐个过完整五层管线。

危害在于**文档一度承诺了「仍可被 deny 规则整个禁掉」，那是错的**。
用户照着写一条 `deny: run_agent` 会以为自己关掉了委派能力，实际没有，
**且界面上完全看不出来**。错误的安全承诺比没有承诺更危险。

**这是个从 C13 起就存在的错误**：`tools/run_agent.py` 的注释从那时起就写着
「未登记的工具落进 `other` 分支，仍可用 `deny: run_agent` 整个禁掉」。
C15 照抄了它，验收时才被实测戳穿。

## 已经做了什么

C15 只**修正了文档**，没动代码：

- `tools/run_agent.py`、`tools/send_message.py`、`tools/team_tasks.py`、
  `permission/adapter.py` 四处注释改成如实说明「deny 无效，唯一手段是 Hook」；
- `CLAUDE.md` 安全边界与 `docs/c15/spec.md` 同步；
- 加了两条护栏 `tests/test_team_tools.py::DenyRuleIneffectiveTest`：
  一条钉住**当前的错误行为**，一条钉住**Hook 仍然拦得住**。

**⚠ 修好这个之后，那两条护栏会红**——那正是它们的用途：提醒你回来把四处注释
和 `CLAUDE.md` 一起改回来。

## 唯一仍然有效的收窄手段

Hook 的 `pre_tool_use`（它排在预扫更前面）。实测拦得住：

```
hook 返回 DENY → 工具未执行，消息未送达   ✅
```

## 要改哪里

一处：`agent/loop.py` 那个 `if tool.system_serial:` 分支。让它也过一次
`engine.decide`，但**把 ASK 结果当 ALLOW 处理**——保住「这类工具不弹确认面板」
这条既有性质（那是 `system_serial` 存在的理由之一：它可能开一整条子对话，
在只读并发桶里弹面板会出事）。

形态大致是：

```python
if tool.system_serial:
    decision = engine.decide(to_request(tool, tc.arguments, engine.mode, cwd))
    if decision.decision == Decision.DENY:
        # 走既有的权限拒绝分支，回灌结构化原因
        ...
    else:
        # ALLOW 与 ASK 都按放行处理（不弹面板）
        ...
```

要顺带想清楚的三件事：

1. **这是安全边界的行为变更**：`deny: run_agent` 从 C13 起一直没生效，
   改完之后突然生效。有用户可能写了这条规则却一直在正常委派——
   对他们来说这是个「突然坏了」的变化。**该不该发变更说明？**
2. **`to_request` 对这些工具会走 `other` 分支**（它们不在 `_TOOL_MAP` 里），
   `specifier` 为空。确认 `deny: send_message`（不带括号 = 匹配整个工具）
   在那条路径上真的能命中。
3. **`load_skill` 与 Skill 的 `allowed-tools` 预授权有交互吗？** 预授权是
   turn 级 allow 规则，而 deny 永远优先——理论上没冲突，但要跑一遍确认。

## 必须补的护栏

- `deny: send_message` **生效**（把现有那条「不生效」的断言反过来）；
- 但**仍然不弹确认面板**（缺省档下 ASK 被当成 ALLOW）；
- Hook 的 `pre_tool_use` 依然能拦（不因这次改动而失效）；
- `run_agent` / `load_skill` 各一条同形用例。

## 开工 Prompt

```
先跑 git branch --show-current；如果在 main 上，先 git checkout -b perm-system-serial-bypass。

读 docs/todo/1-perm-system-serial-bypass.md 与 CLAUDE.md 已知后续工程项第 18 条。
建议连同 docs/todo/2-perm-allow-wildcard-spans-separators.md 一起做——两者都在
③规则层上、都要走安全评审，一起评比分两次更省事。

任务：让 system_serial=True 的工具也过一次 engine.decide，使 permissions.yaml 的
deny 规则对它们生效。要点：
- **ASK 结果按 ALLOW 处理**，保住「这类工具不弹确认面板」这条既有性质——
  那是 system_serial 存在的理由之一（它可能开一整条子对话，在并发桶里弹面板会出事）。
- 这是安全边界的行为变更（deny: run_agent 从 C13 起一直没生效，改完突然生效），
  先想清楚要不要写变更说明。
- 改完之后 tests/test_team_tools.py::DenyRuleIneffectiveTest 里那条
  「deny 不生效」的断言会红 —— 那是**设计好的提醒**：把断言反过来，
  并同步改回四处注释（tools/run_agent.py、tools/send_message.py、
  tools/team_tasks.py、permission/adapter.py）与 CLAUDE.md 安全边界 c15 ①。
- 补护栏：deny 生效 / 仍然不弹面板 / Hook 仍能拦 / run_agent 与 load_skill 各一条。
- 跑全量 unittest。

做完删掉这份 todo，并在 CLAUDE.md 里把已知项第 18 条划掉。
```
