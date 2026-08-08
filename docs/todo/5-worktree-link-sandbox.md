# 让 `worktree.link` 真正可用（需走 `/spec`）

> 建议分支：`worktree-link-sandbox` · 复杂度：中（要动安全边界，须走完整 spec 流程）
> 来源：C14 真实模型端到端验收（2026-08-08），记录见 `docs/c14/acceptance/live-model.md` 问题③
> 对应 `CLAUDE.md`「已知后续工程项」第 15 条末尾那条已知边界

## 现状：`link` 已被降级为 `copy`

C14 的环境初始化提供 `copy` / `link` 两种模式，`link` 的用意是
「大型依赖目录不复制、共享同一份」（500MB 的 `node_modules` 复制三份是灾难）。

**它与权限管线第②层互斥**：`link` 的源在主项目根、落点在隔离工作区，
建出来的软链**天然指向工作区之外**，而第②层明令拒绝这类符号链接。实测：

```
worktree.link: ["vendor"]

read_file('vendor/bigdep/VERSION')  → [权限拒绝·sandbox] 路径越界，超出工作目录
glob_files('vendor/**/VERSION')     → 同上
glob_files('vendor/bigdep/*')       → 同上
```

隔离子 Agent 一个字节都读不到，最后耗尽 12 轮预算失败收场。
而 `worktree_provision` 记的是 `applied=2`——**系统认为它成功了**。

C14 收尾时做的是**止血**：`provision` 在建软链之前先自检落点是否在工作区内，
不在就直接复制并记一条说清原因的警告（沿用本模块既有的「建不了软链就复制 + 留痕」）。
所以现在**不会静默失效了，但 `link` 事实上等于 `copy` 的一个慢别名**。

## 为什么不能顺手修

要让 `link` 可用，判定期必须知道「哪一些指向工作区外的软链是**用户显式声明的**」。
现成的机制都不够：

- `path_guard` 的**只读白名单**（`register_read_root`，memory / skills 目录在用）
  只对 read 类判定生效，`glob_files` / `grep_content` 的遍历完全不走它
  ——而实测里子 Agent 第一件事就是 `glob_files('vendor/**')`。
- 白名单是**进程级**的，而隔离工作区是**每次委派新建**的。要么每次注册再注销
  （并发下互相干扰），要么把它做成 per-call 参数（那就是改判定函数签名）。

也就是说这是**第②层沙箱的行为变更**——本项目全部隔离论证的落点。
按惯例（同 `docs/todo/1` 里权限层那条）应当单独立项、走完整 `/spec`，
不是一次顺手的实现。

## 立项时至少要回答的四个问题

1. **声明的粒度是什么？** 「这个软链整棵子树可读」还是「这个软链可读但不可写」？
   `link` 的语义是共享，写进去会影响主项目根——**大概率应当只放开读，写仍然拒**。
2. **怎么把「本次调用允许的额外根」传到判定函数？** 与 c14 的 `cwd` 同形态
   （显式参数、无默认值），还是挂在一个 per-worktree 的上下文对象上？
   前者要动 `resolve_in_workspace` / `is_within_workspace` / `validate_glob_pattern`
   的签名，**141 处调用点当场红**——c14 付过一次这个代价，它值得。
3. **`run_command` 怎么办？** 子进程的 cwd 是工作区，它自己走软链进去不受任何约束
   （已知边界，与 OS 级沙箱同源）。要不要在文档里说清这条不对称。
4. **值不值得。** 真实项目里 `node_modules` 这类目录确实不该复制，但也可以由用户
   在角色正文里让子 Agent 自己 `npm ci`。**先确认有真实需求再开工**——
   现在的降级行为是诚实的，不是坏的。

## 开工 Prompt

```
先跑 git branch --show-current；如果在 main 上，先 git checkout -b worktree-link-sandbox。

读 docs/todo/5-worktree-link-sandbox.md 与 docs/c14/acceptance/live-model.md 问题③。

这一条**要走 /spec 技能**：它改的是权限管线第②层的边界判定，
而第②层是本项目全部隔离论证的落点，不是一次顺手的实现。

进 /spec 之前先和用户确认第 4 个问题（值不值得做）——现在的降级行为是诚实的，
如果没有真实的大目录共享需求，这一条可以直接删掉不做。
```
