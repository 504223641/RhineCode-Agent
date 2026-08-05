# ③规则层对复合命令不拆段

> 建议分支：`perm-compound-command` · 复杂度：小（半天）
> 来源：C12 Hook 系统验收期，真实模型端到端实测发现（2026-08-06）
> 对应 `CLAUDE.md`「已知后续工程项」第 12 条

## 问题

`permission/rules.py` 的 command 分支对**整串**做匹配，不拆复合命令：

```python
if request.kind == "command":
    if rule.tool != request.rule_name:
        return False
    return match_command(rule.pattern, request.specifier)   # ← 没有 split_commands
```

实测：

```
deny: Bash(git push *)

  git push origin main                → deny   ✅
  git status && git push origin main  → 未命中  ❌
```

## 为什么它要紧

**①危险命令黑名单是拆的**（`blacklist.check_command` 内部走 `split_commands`，
「复合命令逐段 + 整条双重检查」，正是为了防 `safe && rm -rf`）。③规则层不是。
于是**用户手写的 deny 规则**享受不到同一层保护。

**而这不需要刻意规避。** C12 场景 9 首跑时，真实模型（deepseek-v4-flash）在一次
普通的「改完提交推上去」请求里，自己产出了：

```
git add auth.py && git commit -m "..." && echo "=====PUSH=====" && git push origin main
```

没有任何规避意图，只是自然的写法。

危害不止「少拦一次」：用户以为自己拦住了某类命令，实际没有，**且界面上完全看不出来**。

## 已经做了什么

C12 在 **Hook 侧**补齐了同一口径——`hooks/conditions.py` 的 `_match_command_field`
对命令类字段做「整条 + 逐段」双重检查，护栏见
`tests/test_hook_conditions.py::CompoundCommandTest`（7 条，含分隔符矩阵与
「拆段后词边界仍在」的反证）。

**权限层没有动**，理由：那会改变 C6 的规则语义（更多命令会被 deny 命中），
属于安全边界的行为变更，应当单独立项、单独评审。

## 要改哪里

一处：`permission/rules.py` 的 `_rule_matches` 里 `request.kind == "command"` 那支。
形态可以直接抄 `hooks/conditions.py` 的 `_match_command_field`（或把它提到
`permission/matching.py` 里让两边共用——**后者更好**，避免第三次出现同一个坑）。

要顺带想清楚的两件事：

1. **allow 规则要不要也拆？** 拆了会让 `allow: Bash(npm *)` 命中
   `npm ci && rm -rf x`——那是**放宽**，方向错了。
   **结论：只对 deny 拆段，allow 保持整串匹配。** 这个不对称必须写进注释，
   否则下一个人会「顺手统一」掉。
2. deny 拆段后，`has_allow_for`（域名白名单判定）不受影响，但要跑一遍
   `test_perm_*` 确认没有既有用例依赖「复合命令不被 deny 命中」。

## 开工 Prompt

```
先跑 git branch --show-current；如果在 main 上，先 git checkout -b perm-compound-command。

读 docs/todo/1-perm-compound-command.md 与 CLAUDE.md 已知后续工程项第 12 条。

任务：让 permission/rules.py 的 deny 命令规则对复合命令逐段检查，与①危险命令
黑名单同口径。要点：
- 只对 deny 拆段，allow 保持整串匹配（拆 allow 是放宽，方向错）。这个不对称
  必须写进注释，否则下一个人会顺手统一掉。
- 优先把「整条 + 逐段」的判定提到 permission/matching.py，让 hooks 与 permission
  共用一份实现——同一个坑已经出现两次了。
- 补护栏：deny 命中复合命令的各种分隔符；allow **不**被复合命令命中的反证；
  拆段后词边界语义仍在（`git *` 不命中 `github-cli`）。
- 跑全量 unittest，修掉一切因语义变严而变红的既有用例（逐条判断是「护栏对了」
  还是「实现错了」，不要为了绿而放宽断言）。

做完删掉这份 todo，并在 CLAUDE.md 里把已知项第 12 条划掉。
```
