# 2 · 对现有 18 个工具做一轮优化（对标 Codex 与 Claude Code）

> 建议分支：`tool-surface-audit`
> 复杂度：大（多天）。范围宽，**建议分批出 PR**，别攒成一个。

## 目标

参考 **Codex 源码**（`openai/codex` 的 codex-rs workspace，开源）与
**Claude Code 的工具设计**，逐个过一遍现有工具，看能不能提升**准确性**与**效率**。
用户 2026-09-17 明确要三个方向都做：

1. **模型侧**：描述与参数设计
2. **实现侧**：性能与正确性
3. **用户侧**：界面呈现

## 对象（18 个）

`edit_file` `glob_files` `grep_content` `load_skill` `mcp_add_server`
`mcp_resolve_server` `read_file` `run_agent` `run_command` `send_message`
`task_create` `task_get` `task_list` `task_update` `todo_write`
`web_fetch` `web_search` `write_file`
（外加不进注册中心的 `ask_user` / `present_plan`，它们只在特殊路由里）

## 已经攒下的具体线索（别从零开始找）

这几条是 2026-09-17 查那个存盘死循环时**顺手量到的**，都是真问题，
可以直接当起点：

### 模型侧

- ⚠ **`read_file` 的行号前缀让内容膨胀约 15%**，而且是**每读一次膨胀一次**。
  实测：19.3K 的文件读出来 22.2K（418 行 × 约 6 字节前缀 + 文件头）。
  它同时是已知项 #21 那个死循环的放大器。
  **要核对的是**：Claude Code 的 Read 也用 `cat -n` 形态的行号，Codex 用什么？
  行号对 `edit_file` 定位确实有用，但**是不是每次都要**？
  （比如只在带 `start_line` 时给、或给稀疏行号）
- ⚠ **`read_file` 没有按体量的默认分页，于是「任何超过约 12K 字符的文件，
  整份读一次必被存盘」**。`MAX_RANGE_LINES = 2000` 是行数上限，
  `MAX_READ_BYTES = 1MB` 是拒绝线，两者之间有一大段真空——而 c8 的
  `SINGLE_RESULT_TOKENS = 4000`（约 12K 字符）落在那段真空里。
  结果是模型第一次读一个中等大小的文档，**拿到的永远是占位而不是内容**。
  Claude Code 的 Read 默认只读前 2000 行并**明确告诉你被截断了**，值得对标。
- ⚠ **`run_command` 在 Windows 上要不要改用 Git Bash——这条单独拿出来评估，
  它是本轮唯一一个「改行为」而不是「改文案」的候选。**

  **背景**：`run_shell_captured` 用 `subprocess.Popen(shell=True)`，于是
  Windows 上是 `cmd.exe`、POSIX 上是 `/bin/sh`。2026-09-17 两份真实 trace 实录，
  模型把三种 shell 都猜了一遍（POSIX / PowerShell / cmd），**4 轮迭代、197K
  输入 token** 白烧在语法试错上。已经做了便宜的那一半（环境信息报解释器名 +
  工具描述给 cmd 语法与两个陷阱，见 `tests/test_shell_environment_info.py`），
  **这一条是贵的那一半**。

  **支持改**：模型的默认直觉就是 POSIX——这不是偏好问题，是训练分布问题。
  上游印证：**Claude Code 在 Windows 上让 Bash 工具跑 Git Bash**，并在工具描述里
  逐字写明「runs Git Bash (POSIX sh), not cmd.exe or PowerShell. Use Unix shell
  syntax: `/dev/null` not `NUL`, forward slashes, `$VAR` not `%VAR%`」。
  改了之后 `tail` / 管道 / `VAR=x cmd` / `2>/dev/null` 全都直接可用，
  那份 trace 里 4 次失败有 3 次当场消失。

  **反对/成本**：① 多一个外部依赖，**必须处理「机器上没装 Git Bash」的回退**，
  而回退意味着同一份项目在两台机器上 shell 不同——那正是本项目最忌讳的
  「静默降级」形态（环境信息必须如实报出退回了 cmd，不能假装）；
  ② `filtered_environ` 与那段控制台模式的处理（`ENABLE_PROCESSED_INPUT`，
  见 `run_command.py` 的模块 docstring）都是按 cmd 写的，要重新核；
  ③ 路径形态会变（MSYS 的 `/g/xxx` vs `G:\xxx`），而工具返回的路径会进模型上下文；
  ④ ⚠ **MSYS 的路径转换本身就是个坑**——`CLAUDE.md` 里那条
  「从 Git Bash 驱动 e2e 必须 `MSYS_NO_PATHCONV=1`」记的就是它，
  换过去之后**所有以 `/` 开头的参数都会被改写**，这是新引入的一类失败。

  **判据建议**：不要凭感觉定。用调度器在 live 模式跑一组对照——同一批任务，
  一组 cmd、一组 Git Bash，量「因 shell 语法失败的工具调用次数」。
  ⚠ **两边都要跑**，只跑 Git Bash 会看不见它新引入的那类失败（③④）。

  ⚠ 若真要改，`agent/prompt/environment.py` 的 `_detect_shell` **必须同步**
  ——它现在逐条照抄 CPython 对 `shell=True` 的实现规则，改了执行方式而不改它，
  就是**在对模型撒谎**，而那比什么都不说更糟：模型会自信地写一种不生效的语法，
  且不会去怀疑这条环境信息。`tests/test_shell_environment_info.py` 有一条
  **真起子进程让它自报家门**的护栏钉着这个（变异实测过，谎报当场红）。

- 各工具 `description` 的触发口径是否一致、`primary_arg` 登记是否齐全。
- ⚠ 有几处**成对维护点**专管「两处措辞必须同口径」（Skill 清单 ↔ `load_skill`、
  角色清单 ↔ `run_agent`、交付信息 ↔ 委派工具、消息标记块 ↔ `send_message`、
  待办提示 ↔ `todo_write`、澄清三处），**改描述前一律先加载
  `paired-maintenance` Skill**，这类漏改一个字都不报错。

### 实现侧

- ⚠ **本次实测过一个 38% 的热点**：`path_guard.is_inside` 对两个入参各做一次
  `Path.resolve()`，单次 **1.0 ms**，而 `read_file` 整体才 2.7 ms。
  改成纯内存比较后是 0.030 ms。**同样的形态很可能还在别处**——
  凡是「已经解析过的路径又被 resolve 一遍」的地方都值得量。
  ⚠ **先量再改**（`CLAUDE.md` 有专门一段讲这条教训，两次量出来的头号热点
  一次是产品缺陷、一次是判据本身没验到东西）。
- 边界条件与错误分支：空文件 / 二进制 / 超长行 / 非 UTF-8 / 并发读写。
- `fail-safe` 与 `fail-closed` 的口径是否一处一个样（本次就发现
  `is_offload_store_path` 需要 fail-open 而路径沙箱必须 fail-closed，
  两者理由不同、都对，但**得写下来**）。

### 用户侧

- 工具行的标签与摘要、`primary_arg` 登记、`FOLD_GROUPS` 归并分组是否齐全。
- 确认面板上的信息够不够用户判断放不放行（`summarize_args` 只留 30 字符，
  而要放行的内容完全可能在第 31 个字符之后——这条已有成对维护点记着）。

## ⚠ 三条纪律

1. **别一个 PR 打包 18 个工具。** 按方向或按工具分批，每批能独立回滚。
2. **每条改动要有判据。** 「看起来更好」不算——描述类的改动要么有对标出处
   （Codex/Claude Code 的原文），要么有真机对照（用调度器跑 A/B）。
   ⚠ 提示词类改动的 A/B 场景设计有坑，见记忆里那条「提示词 A/B 场景设计」：
   **场景必须让「违反提示词」成为省事的那条路**，否则测不出东西。
3. **别顺手统一「刻意不一致」的地方。** 项目里有好几处措辞相近但**成本结构
   相反**因而刻意分叉的（`skills/render.py` 保持 pushy vs `subagents/render.py`
   保持保守；`todo/render.py` 比委派积极）。`paired-maintenance` 与
   `docs/internals/known-issues.md` 的 #14 / #17 逐条记着理由。

---

## 一键开工 Prompt

```
先跑 `git branch --show-current`。如果在 main 上，先 `git checkout -b tool-surface-audit` 再动手。

读 `docs/todo/2-tool-surface-audit.md`，按它做。

这是一轮**审查**，不是一次改动——**先出一份清单，再动代码**：

1. 逐个过 18 个工具，按三个方向（模型侧描述与参数 / 实现侧性能与正确性 /
   用户侧界面呈现）列出候选改动，每条标上「判据是什么」。
2. 把清单给用户过一遍再开工。⚠ **别直接开改**，范围太宽，改完再讨论就晚了。
3. 获准后分批出 PR，每批能独立回滚。

⚠ 动 tools/ 之前先加载 `paired-maintenance` Skill——工具描述里有六处
「两边措辞必须同口径」的成对维护点，漏改一个字都不报错。

⚠ 性能类改动**先量再改**（`CLAUDE.md` 的「测试」一节有专门一段讲这条教训：
两次量出来的头号热点，一次是产品缺陷、一次是判据本身没验到东西）。

⚠ 描述类改动要有判据：要么有 Codex / Claude Code 的对标出处，要么用调度器
（`tests/e2e/`）跑真机 A/B。A/B 场景设计有坑——场景必须让「违反提示词」成为
省事的那条路，否则测不出东西。

文档里「已经攒下的具体线索」那一节有 6 条实测过的起点（read_file 的行号膨胀
15%、超过 12K 字符必被存盘、is_inside 的 1ms 热点等），直接从那里开始。

⚠ 其中「run_command 在 Windows 上要不要改用 Git Bash」是**唯一一个改行为而不是
改文案**的候选，风险与其余几条不在一个量级（新外部依赖 + 必须处理没装的回退 +
MSYS 路径转换这一类新失败）。**它要单独拿给用户拍板**，别混在批量 PR 里，
而且判据必须是两边对照的真机数据，不能凭感觉。

做完把这份文档删掉，并按 `docs/todo/README.md` 的命名规则重排剩下的序号。
```
