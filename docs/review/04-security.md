# 阶段 4 · 安全复审

> 2026-08-22。**目标不是找设计漏洞**——五层管线的设计已经反复论证过、也有反证测试。
> 目标是核验**安全论证与代码是否还对得上**。
> 配套 [`README.md`](README.md)（问题清单）、[`00-baseline.md`](00-baseline.md)（机器数据）、
> [`03-architecture.md`](03-architecture.md)（能力交互矩阵）。

## 怎么复现本文的结论

本文第 1 节不是读代码读出来的，是**把代码逐条改坏、看护栏红不红**跑出来的
（变异实测 / mutation testing）。做法：备份文件 → 打一处补丁 → 只跑相关测试模块 →
无论结果如何都立刻还原。全程 `git status` 保持干净，产品代码一行未留。

```bash
# 例：把②′网络边界层挪到③规则层之后，看护栏红不红
python -m unittest tests.test_perm_network_layer tests.test_web_search_perm
```

---

## 先说结论

**八条顺序与匹配不变量的护栏，七条是真的，一条是空的。**
四条重点已知边界，**全部与代码一致**，一条不差。

但查出**两条安全承诺与代码不符**，成因相同：
**`auto` 成为缺省预设时，那些「靠第④层兜底」的承诺集体失效了，而文字没跟着改。**
其中 S1 是**六道防线一道都不生效**，是本次审查最严重的发现。

| 编号 | 级别 | 一句话 |
| --- | --- | --- |
| **S1** | 🔴 **高危** | `mcp_add_server` 写受保护配置 + 启动第三方程序，**六层防御一层都不生效**，缺省下零确认 |
| **S2** | 🔴 **高危** | 「MCP 工具默认每次经人在回路确认」这条承诺在缺省预设下已失效，三处文档仍这么写 |
| S3 | 🟠 | ①黑名单的拆分口径**没有任何护栏**——把引号感知搬进去，3,323 条测试全绿 |
| S4 | 🟠 | `permission/rules.py:121` 用 `fnmatch` 而非 `fnmatchcase`，规则匹配**在 Windows 与 Linux 上语义不同** |
| S5 | 🟡 | README 的安全须知在第 977 行，且第一条已被 C16 推翻却没有勘误 |

---

# ⚠ 高危发现（安全承诺与代码不符）

> 这两条单独成节，不与普通条目混排。判据是**项目自己写下的安全承诺，
> 在当前代码上不成立**——即已知项 #18「错误的安全承诺比没有承诺更危险」同型。

## S1 🔴 `mcp_add_server`：六层防御一层都不生效

### 承诺原文

- `CLAUDE.md:455`：「`mcp_add_server` 会写配置并启动外部 MCP，**必须保持
  `read_only=False` 且先让用户确认**」
- `rhinecode/tools/mcp_config.py:4-5`（模块 docstring）：「`mcp_add_server` 会写入配置
  并启动外部 MCP，因此标记为非只读，**交给现有权限确认流程拦截**」
- `rhinecode/tools/mcp_config.py:67-68`（类 docstring）：「因为这可能运行第三方命令
  或访问远端 URL，本工具必须保持 `read_only=False`」
- `rhinecode/permission/protected.py:118`：`.rhinecode/mcp.yaml` 已被列进②″保护路径，
  理由写着「**会启动外部程序并把它的工具注册进工具中心**」

### 实测结果

```
name: mcp_add_server | read_only: False | system_serial: False
kind: other  rule_name: mcp_add_server
缺省预设下的判定: allow @ mode | 放行模式：无规则命中，默认允许
```

**逐层核验它为什么一层都碰不到：**

| 层 | 为什么不生效 | 证据 |
| --- | --- | --- |
| ①危险命令黑名单 | 只对 `kind == "command"` 生效；本工具是 `other` | `permission/engine.py:379` |
| ②路径沙箱 | 只对 `read_path` / `write_path` / `glob` 生效 | `permission/engine.py:399,404` |
| ②′网络边界 | 只对 `kind == "url"` 生效 | `permission/engine.py:420` |
| **②″保护路径** | **第一行就是 `if request.kind != "write_path": return result`** | `permission/engine.py:283` |
| ③可配置规则 | 只有用户**主动写下** `deny: mcp_add_server` 才拦得住（缺省模板里没有） | `permission/rules.py:121` |
| ④权限档兜底 | 缺省预设 `auto` = `PERMISSIVE` → **ALLOW** | `presets.py:102,119` + `engine.py:492` |
| ⑤人在回路 | 上一层给了 ALLOW，面板根本不弹 | — |
| C16 分类器 | 本工具**没有声明 `classifier_scope`**，不进审查 | `tools/base.py:225`（缺省空串） |

**它写的还不止工作区内的文件。** `scope: "user"` 会写到 `~/.rhinecode/mcp.yaml`
（`mcp/auto_config.py:429-430`），那是**工作区之外**；而写盘走的是
`path.write_text`（`mcp/auto_config.py:490`），**不经 `write_file` 工具**，
于是路径沙箱与②″保护路径连看都看不到它。

### 后果（用户会看到什么）

缺省配置下跑 `rhine`，模型可以在**一次工具调用里、不弹任何面板**：

1. 往 `.rhinecode/mcp.yaml`（或用户主目录下那份）写一条 `mcpServers` 配置；
2. 立刻 `reload_server` 把它拉起来——`command` 字段是**任意本地命令**
   （`mcp/transport.py:138,174`），或者一个任意远端 URL；
3. 那个子进程**继承完整的 `os.environ`**（`mcp/transport.py:165`：
   `merged_env = {**os.environ, **self._env}`），**含 `DEEPSEEK_API_KEY`**；
4. 它注册进来的工具全部落 `kind == "other"`，缺省档下同样是 ALLOW。

对比：模型经 `run_command` 跑命令时，环境变量是**过滤过的**
（`tools/run_command.py:154-188`，剔除含 `API_KEY` / `TOKEN` / `SECRET` … 的变量），
理由写在 auto-plan 扩展 F17 里——「命令全放行之后，一句打印环境的命令就能拿到 API Key」。
**同一条理由对 MCP stdio 子进程完全没有落实。**

### 为什么以前没暴露

这条承诺写于 C7，那时缺省档是 `PermissionMode.DEFAULT`，第④层对 `other` 类判 ASK，
面板照弹。**auto-plan 扩展把缺省档换成 `PERMISSIVE` 之后，「交给现有权限确认流程
拦截」这句话就失去了兑现它的那一层**，而三处文档 + 两处 docstring 一个字没改。

实测对照（同一条请求、只换权限档）：

```
缺省预设 auto (permissive) → allow @ mode
对照 PermissionMode.DEFAULT → ask
```

### 建议（本轮只报告，不改代码）

按代价从小到大三选一，**建议至少做第 3 条止血、第 2 条做实**：

1. **加一个「启动外部程序类」的出口收紧器**，与②″同构（只升级、不降级），
   在 `decide` 出口处把这类工具的非 DENY 结论升级为 ASK。
   ⚠ 不能用 `system_serial = True`——那恰好是反方向（④判 ASK 时按 ALLOW 处理）。
2. **把 `mcp_add_server` 纳入②″的判定范围**：在 `permission/adapter.py` 的
   `_TOOL_MAP` 加一条映射，把 `scope` 解析成实际写入路径、`kind` 设为 `write_path`。
   它写的正是已在保护清单里的 `mcp.yaml`（`protected.py:118`）。
   ⚠ 这会连带让它过②路径沙箱，于是 `scope: "user"` 会被拒——
   **那正是想要的**（用户级配置本来就不该由模型写），但要在文案里说清楚。
   ⚠ 动 `_TOOL_MAP` 前先加载 `paired-maintenance` Skill：新增 `kind` 是三处成对维护点。
3. **最小止血**：`permissions.example.yaml` 模板里预置一条 `deny: mcp_add_server`
   （⚠ **不带括号**，`other` 分支只认空模式），并在 README 显眼处说明。
   代价最小，但依赖用户没删掉那行。

⚠ 无论选哪条，**`mcp/transport.py:165` 的 `os.environ` 继承应当同时收窄**——
复用 `tools/run_command.filtered_environ()` 即可，那是现成的。

---

## S2 🔴 「MCP 工具默认每次经人在回路确认」已经不成立

### 承诺原文（三处，语义相同）

- `CLAUDE.md:455`：「MCP 工具一律 `read_only=False`，**默认权限模式下每次调用
  都经人在回路确认**」
- `README.md:802`：「**安全默认**：MCP 工具一律视为非只读，**默认权限模式下
  每次调用都经人在回路确认**」
- `README.md:35`：同义表述（功能列表里那条）

### 实测

```
mcp__everything__printEnv  → allow @ mode   （缺省预设 auto）
对照 PermissionMode.DEFAULT → ask
```

这句话**字面上没错**——它说的是 `PermissionMode.DEFAULT`。问题在于
**`DEFAULT` 已经不是缺省了**：`presets.py:119` 的 `DEFAULT_PRESET = Preset.AUTO`，
而 `presets.py:102` 把 `AUTO` 映射到 `PermissionMode.PERMISSIVE`。
陌生读者读到「默认权限模式」，理解成「不做任何配置时的行为」，
**而那个理解现在是错的**。

### 这是 CLAUDE.md 内部的自相矛盾

`CLAUDE.md:426` 已经写明：「缺省下仍然会弹面板的只剩三类：**②″保护路径**的写入、
**网络访问**（未建域名白名单时）、以及用户自己写的 Hook `ask` 规则。」
**MCP 不在这三类里。** 两条相隔 29 行，互相打架。

### 后果

MCP 远端 Server 被项目自己定性为「外部程序、不可信」（`CLAUDE.md:455`），
而**唯一写明的缓解手段就是那句「每次确认」**。它失效之后，缺省配置下：
`mcp.yaml` 里任何 Server 提供的任何工具，模型调用时零提示、零面板。

### 建议

三处文字统一改成「**缺省预设 `auto` 下 MCP 工具直接放行；要逐次确认请写
`deny` 规则，或给子 Agent 角色声明更严的 `permission_mode`**」，
并在 `CLAUDE.md:426` 那份「仍会弹面板的三类」旁边点明 MCP 不在其中。

---

# 1. 顺序不变量的护栏核验（变异实测）

`CLAUDE.md` 架构表 Permission 行列了若干「必须排在 X 之后」「不许对调」的不变量。
下表每一行都是**真的把代码改坏跑了一遍**的结果，不是读代码推的。

| # | 被改坏的不变量 | 变异做法 | 结果 | 抓住它的用例 |
| --- | --- | --- | --- | --- |
| M1 | **②′网络边界必须排在③之前** | 在 `_decide_core` 开头对 url 类先跑一次 `merged.evaluate` | 🔴 **红** | `tests/test_perm_network_layer.py:446` `test_wildcard_allow_cannot_bypass_hard_check`（`ALLOW != DENY`，url=`http://127.0.0.1/`） |
| M2 | **②″保护路径是出口收紧器、不是管线中的一站** | 改成「②之后③之前」的短路站，并摘掉 `decide` 里的收紧器 | 🔴 **红（7 条）** | `tests/test_perm_protected.py:353` `test_layer_three_deny_survives`、`:360` `test_strict_mode_deny_survives`、`:323` `test_wide_allow_is_really_a_hit_at_layer_three`、`tests/test_protected_wiring.py:296` 等 |
| M3 | **收紧器必须处理「非 DENY」而不只是 ALLOW** | `if result.decision is DENY` 改成 `is not ALLOW` | 🔴 **红** | `tests/test_perm_protected.py:389` `test_default_mode_ask_has_its_layer_replaced`（层是 `MODE` 而非 `PROTECTED`） |
| M4 | **③规则层必须排在①②之后** | 在 `_decide_core` 开头先跑 `merged.evaluate` | 🔴 **红（3 条）** | `tests/test_perm_turn_grant.py:263` `test_blacklist_still_wins`、`:271` `test_sandbox_still_wins`、`tests/test_perm_engine.py:41` `test_blacklist_before_rules` |
| M5 | **allow 侧必须用「每一段都得命中」** | 换成 `match_command_deep` | 🔴 **红（6 条）** | `tests/test_perm_rules.py:131` `test_allow_trailing_wildcard_no_longer_spans_separators`、`:187`、`:228` |
| M6 | **deny 侧必须用「整条 + 任一段」** | 换成 `match_command_every_segment` | 🔴 **红（12 条）** | `tests/test_perm_rules.py:253` `test_deny_side_must_not_use_the_allow_matcher`、`:97`、`:286` |
| M8 | **allow 侧的拆分必须认引号** | 换成朴素 `split_commands` | 🔴 **红（3 条）** | `tests/test_perm_rules.py:164` `test_allow_does_not_split_inside_quotes`（三种引号形态各一条） |
| **M7** | **①黑名单的拆分必须是朴素的（不认引号）** | `blacklist.py:21` 改成 `import split_commands_quoted as split_commands` | 🟢 **全量 3,323 条全绿** | **一条都没有**——见 S3 |

## 1.1 七条真护栏的**构造方式**都是对的

判据是 `CLAUDE.md` 自己写下的那条：顺序护栏必须用「**全域名 allow + 禁止地址**」
构造，用「白名单未命中」那种形态在错序下照样通过。逐条核对：

- **M1 的护栏用的正是那个形态。** `tests/test_perm_network_layer.py:446-459` 同时给
  `file_rules` 与 `policy_rules` 一条 `domain:*`，再请求 `127.0.0.1` / `file://` /
  内嵌凭据三种**禁止地址**，断言 `layer is Layer.NETWORK`，失败消息里直接写着
  「若这里变成 RULE，说明②′被挪到了③之后」。用例 docstring（`:435-443`）还留着
  当初两种形态的对照实测记录。**这是本仓库里护栏写得最好的一处。**
- **M2/M3 是「两组缺一不可」的结构。** `PipelineOrderGuardTest`（宽 allow + 保护路径）
  钉「不被③层消解」，`NoDowngradeTest`（③deny / 严格档 / 沙箱三种 DENY）钉「不降级」。
  ⚠ **实测比文档写的更强**：`tests/test_perm_protected.py:344-348` 的 docstring 说
  「`PipelineOrderGuardTest` 在短路站实现下仍是绿的」，而本次 M2 实测它**也红了**——
  因为 `:323` 那条前提校验直接调 `_decide_core` 断言「不加收紧器时确实是 ALLOW」，
  短路站把 ASK 塞进了 `_decide_core`，前提当场不成立。**那条前提校验是后加的，
  docstring 没跟着更新**。不是缺陷，但下一个照它推演的人会得到过时的结论。
- **M4 的护栏用的是「预授权 + 危险命令 / 越界路径」**，即③层**确实会命中**的形态
  （`grants_for([_spec("Bash")])` 产出的是宽 allow）。与 M1 那个形态同构，
  错序下会变成 `ALLOW @ RULE`，抓得住。
- **M5/M6 互为反证**，M6 的 `test_deny_side_must_not_use_the_allow_matcher`
  （`tests/test_perm_rules.py:253`）名字里就写着「反方向的反证」——**这是刻意
  按变异思路写的用例**，不是顺手写的。

## 1.2 结论

**除 M7 外，这批护栏经得起变异实测。** 项目那次「变异实测发现顺序护栏构造错了」
的教训不仅被吸收，还固化成了用例 docstring 里的**方法论说明**
（`test_perm_network_layer.py:435-443`、`test_perm_protected.py:305-320`），
后来的人照着写就不会再犯。这是本次审查里少见的「文档在源码里、且不会过期」的正面例子。

---

## S3 🟠 ①黑名单的拆分口径没有任何护栏

### 承诺原文（同一句话写了四遍）

- `CLAUDE.md` 架构表 Permission 行：「两侧的**拆分口径也不同**（朴素 / 认引号）
  ——合一或对调任一处都会静默放宽权限」
- `CLAUDE.md` 安全边界第 3 条：「把引号感知搬进后者**等于放宽①黑名单**」
- `rhinecode/permission/matching.py:80`：「⚠ **绝不要把本函数的引号感知
  『顺手』搬进 `split_commands`。**」
- `rhinecode/permission/rules.py:91-93`：同一条警告又写了一遍

### 实测

把 `rhinecode/permission/blacklist.py:21` 从

```python
from rhinecode.permission.matching import split_commands
```

改成

```python
from rhinecode.permission.matching import split_commands_quoted as split_commands
```

然后跑**全量**测试：

```
用例 3323/3323   墙钟 33.4s
全部通过
```

**3,323 条测试没有一条变红。**（对照：M5 / M6 / M8 三处同类不变量各有 3–12 条护栏。）

### 这个放宽是真的，只是很窄

需要构造一条判别输入才看得出来。逐一核对
`blacklist.DANGEROUS_PATTERNS`（`blacklist.py:25-73`）里 17 条正则，
**只有一条带 `$` 锚**：

```
\brm\b[^\n]*\s/(\s|$)     # blacklist.py:60  「rm 直接以根目录 / 为目标」
```

`check_command`（`blacklist.py:93`）的候选串是「整条 + 各拆段」，而
`pattern.search` 是子串匹配——**拆段只会增加候选、不会减少**，所以对没有锚的
16 条正则来说拆法根本无关紧要。唯有 `$` 锚会因为「拆出来的段尾」多一个匹配位置。

实测判别输入：

```
'sh -c "rm /;ls"'
   朴素拆分  : 系统破坏：rm 直接以根目录 / 为目标      ← 现状，DENY
   认引号拆分: None                                    ← 变异后，放行
```

（`;` 在引号内，认引号的拆分不切它，于是「`rm /` 作为一整段结尾」这个匹配位置消失。）

### 为什么仍要报

单看今天的影响面确实小。但：

1. **它是一条被写了四遍的成对维护点，却没有任何机器护栏。** 与
   `00-baseline.md` 第 5 节记的 C4（`tools/__init__.py` 的不变量无护栏）**同型**：
   文档强调得越狠，越说明有人想「顺手统一一下」。
2. **今天窄，只是因为模式表恰好只有一条锚点正则**，而 `blacklist.py:15` 明写
   「新增危险模式：在 `DANGEROUS_PATTERNS` 追加即可，无需改动其它层」。
   **下一条带 `^` 或 `$` 的正则一加进来，这个缺口就同步变宽，
   而没有任何东西会提醒加它的人。**

### 建议

一条用例，二十行以内：断言 `blacklist.check_command('sh -c "rm /;ls"')` 非 None
（判别输入本身就是护栏），外加断言 `blacklist` 模块 import 的是 `split_commands`
而不是 `split_commands_quoted`。后者是 AST 级的机械判定，与
`tests/test_classifier_broad.py:219` 钉 `permission/__init__.py` 那条的手法同构。

---

## S4 🟠 权限规则的工具名匹配在 Windows 与 Linux 上语义不同

### 证据

`rhinecode/permission/rules.py:121`（`other` 分支——MCP 工具与七个协作 / 系统工具
的唯一匹配路径）：

```python
return rule.pattern == "" and fnmatch.fnmatch(request.rule_name, rule.tool)
```

`fnmatch.fnmatch` 会先把两边过一遍 `os.path.normcase`：

```
本机（Windows）  normcase('WebSearch') = 'websearch'
POSIX            normcase('WebSearch') = 'WebSearch'
fnmatch('WebSearch','websearch')     = True      ← 仅 Windows
fnmatchcase('WebSearch','websearch') = False     ← 两个平台都是 False
```

实测（本机 Windows）：`deny: websearch`（全小写）**命中并拒绝**。
同一份 `permissions.yaml` 拿到 Linux 上，那条规则**静默失效**。

### 项目自己在别处踩过并修好了这一条

`rhinecode/hooks/conditions.py:169-173` 逐字写着：

> **通用分支用 `fnmatchcase` 而不是 `fnmatch`** ——后者会先过 `os.path.normcase`，
> 在 Windows 上退化成大小写不敏感、在 POSIX 上保持敏感。同一份 `hooks.yaml`
> 在两个平台上行为不同，而配置和界面上都看不出任何异常。

**Hook 那侧修了，权限那侧没修。** 两处是同一个坑的两个入口。

### 后果（哪个方向危险）

- **Windows → Linux**：一条大小写不完全对的 `deny` 规则**静默不再生效**。
  这是危险的方向——用户在自己机器上验过「拦住了」，部署到 CI / 容器里就没了。
- **Linux → Windows**：一条 `allow` 规则会比作者意图**多匹配**一些工具名。

MCP Server 名常带大写（`mcp__GitHub__*`、`mcp__Context7__*`），
命中这个坑的概率不低。而项目**没有任何 Linux CI**（`README.md` 的 C1），
所以这个差异在开源之前不可能被自动发现。

### 建议

`permission/rules.py:121` 改用 `fnmatch.fnmatchcase`，并加一条护栏断言
「大小写不一致的整工具规则**不命中**」。
⚠ 这是**行为变更**（Windows 用户现有的小写规则会失效），属产品改动而非文档改动，
要走 F 系列并在 CHANGELOG 里说明。

---

# 2. 已知边界的现状核验

四条重点边界**全部与代码一致**。逐条给出实现位置与实测。

## 2.1 ✅ `deny: WebSearch` 必须不带括号（带括号静默无效且无警告）

| 项 | 结论 |
| --- | --- |
| 承诺 | `CLAUDE.md` 安全边界 web_search 第 ② 条 |
| 实现 | `permission/rules.py:121`——`search` 类落 `other` 分支，该分支只认 `rule.pattern == ""` |
| 有没有加载期警告 | **没有**。`permission/config.py` 全文只对 `WebFetch(...)` 的 `domain:` 语法做校验（`:176-184`），`WebSearch` 一个分支都没有 |
| 唯一缓解 | `permission/config.py:72-81` 模板注释里那段说明 |

实测四种写法：

```
'WebSearch'           → deny @ rule   ✅ 生效
'WebSearch(*)'        → ask  @ mode   ❌ 静默不生效
'WebSearch(domain:x)' → ask  @ mode   ❌ 静默不生效
'websearch'           → deny @ rule   ⚠ 仅 Windows 生效（见 S4）
```

**边界与文档完全一致。** 注意实测顺带暴露了 S4——这条边界在 Linux 上还多一个坑。

## 2.2 ✅ `ask_user` 无法被 deny 规则或 Hook 关掉

| 机制 | 为什么关不掉 | 证据 |
| --- | --- | --- |
| `deny` 规则 | 它不在工具注册中心。`ASK_USER = "ask_user"` 只是一个常量，全仓无任何 `registry.register` | `agent/plan_tools.py:20`；`rhinecode/tools/` 与 `bootstrap.py` 里 grep 无注册点 |
| `pre_tool_use` Hook | 特殊工具的分流**在 Hook 分发点的上游**：`if tc.name in (ASK_USER, PRESENT_PLAN): special.append(tc); continue` | 分流在 `agent/loop.py:1437`，`_dispatch_pre_tool` 调用点在 `agent/loop.py:1522`——**相隔 85 行，分流先执行** |

**真实可用的三样**（都不是开关，与文档一致）：

- **可见性判据**：`agent/loop.py:1165` 的 `can_ask_user=clarify is not None`；
  子 Agent 与 C15 无人值守轮拿不到 `clarify` 回调，因此**看不到**这个工具。
- **跳过熔断**：`agent/loop.py:1063,1307,1313` 的 `clarify_skips` 累计。
- **`notification` 事件**：只能观测、不能拦截。

**边界与文档完全一致**，`CLAUDE.md` 那句「这一条必须原样留着、不许含糊过去」是对的。

## 2.3 ✅ ②″保护路径管不住 `run_command`（以及 MCP 工具）

实现只有一行：

```python
# rhinecode/permission/engine.py:283
if request.kind != "write_path":
    return result
```

`run_command` 映射成 `kind="command"`（`permission/adapter.py:34`），MCP 工具与
未映射工具落 `kind="other"`（`permission/adapter.py:8-9`）——两者都在第一行就原样穿过。

`echo >> .rhinecode/hooks.yaml` 绕得过，与 `CLAUDE.md` 的②″第 ⑤ 条逐字一致。
文档说「真正的堵法是分类器审查（C16，已落地）」——核验通过：`run_command` 声明了
`classifier_scope = "command"`（`tools/run_command.py:364`）。

⚠ **但 `mcp_add_server` 既不在②″里、也没有 `classifier_scope`**——那是 S1，
它不是这条已知边界的一部分，而是这条边界之外**一个没被登记过的**缺口。

## 2.4 ✅ `system_serial` 工具对第④层整层免疫

实现在 `agent/loop.py:1576-1597`：

```python
if tool.system_serial:
    request = to_request(tool, tc.arguments, engine.mode, cwd)
    raw = engine.decide(request)                       # ← 仍然真的调引擎
    mode_downgraded = (raw.decision != Decision.ALLOW and raw.layer is Layer.MODE)
    if raw.decision != Decision.ALLOW and not mode_downgraded:
        system_decision = raw                          # ← ①②③ 的 DENY 原样保留
    else:
        system_decision = DecisionResult(Decision.ALLOW, raw.layer, ...)
```

逐条对上文档：

- **仍过引擎**（不是「不进管线」）：`agent/loop.py:1577-1578`。✅
- **只对第④层免疫**（判据是 `raw.layer is Layer.MODE`）：`agent/loop.py:1583`。✅
- **Hook 的 ASK 不被吞掉**（刻意与④区别对待）：`_apply_hook_ask` 在降级**之后**调用，
  `agent/loop.py:1598`，理由注释在 `:1598-1601`。✅
- **`deny: send_message`（不带括号）拦得住**：③层的 DENY 是 `Layer.RULE`，
  不满足 `mode_downgraded`，原样保留。✅
- **收紧权限档关不掉它们**：④层的 DENY / ASK 一律降级为 ALLOW。✅

实测（缺省预设）：`run_agent` / `send_message` → `allow @ mode`，与文档一致。
护栏：`tests/test_perm_system_serial.py`、`tests/test_trace_system_serial.py`。

**边界与文档完全一致**，包括那句「此处一度写着『不进权限管线』，那是一个真实缺陷，
已于 perm-system-serial-bypass 修掉」——代码里确实是「先 `decide` 再降级」的形态。

---

# 3. 顺带核验通过的其它安全承诺

不是本次重点，但都在代码里找到了对应实现，一并记录（**都没问题**）：

| 承诺 | 实现位置 | 结论 |
| --- | --- | --- |
| `run_command` 子进程过滤敏感环境变量 | `tools/run_command.py:154-188`（10 个标记词），调用点 `:267` | ✅ 已接线 |
| `<untrusted-content>` 提示模块按开关注入 | `agent/prompt/modules.py:50,86`；调用点 `conversation.py:574,1347,1552,1906` | ✅ 已接线（**三个调用点都传了**） |
| C15 队友消息正文无害化 | `team/render.py:82-95` | ✅ |
| 记忆文件名白名单 `[a-z0-9_-]+\.md` | `memory/memory_updater.py:24` | ✅ |
| C16 分类器 `review` 绝不外抛异常 | `classifier/service.py:113,134-137`（一切异常收敛为 `_fail`） | ✅ |
| C16 熔断不按 `scope` 分桶 | `classifier/breaker.py:18,67` | ✅ |
| ②″保护路径清单含 `mcp.yaml` | `permission/protected.py:76,118` | ✅ 清单对，但**触发不到**（S1） |
| `.gitignore` 覆盖 traces / sessions / memory / context / worktrees | `.gitignore:29-46` | ✅ 五项齐全，都带 `**/` 前缀 |
| 仓库里没有真实密钥文件 | `git ls-files` 只有 `permissions.example.yaml` | ✅ |

⚠ **一处与 S1 相关的不对称**：`run_command` 的子进程环境是**过滤过的**
（`tools/run_command.py:267`），而 MCP stdio 子进程拿的是**完整 `os.environ`**
（`mcp/transport.py:165`）。理由（「一句打印环境的命令就能拿到 API Key」）
对两者同样成立。

另：`classifier/render.py:244` 的 `render_broad_domain_warning()` 从未接线
（本次复核确认全仓提及次数仍为 1），已由 `README.md` 的 **B2** 登记，此处不重复。

---

# 4. README 的安全须知对陌生用户够不够用

## 4.1 现状

- 安全边界一节在 **`README.md:977`**，即 1,250 行文档的 **78% 处**——
  在完整的能力巡礼、配置手册、目录树之后。
- 内容本身**很扎实**：25 条，逐条有理由，覆盖沙箱边界、trace 敏感性、
  `hooks.yaml` 攻击面、Skill 信任模型、子 Agent 提权论证、工作区隔离。
- **但它是写给「已经决定用这个项目的人」的**，不是写给「正在决定要不要跑起来」的人的。

## 4.2 四个具体问题

**① 第一条已经被 C16 推翻，README 没有勘误。**

`README.md:979`：「权限决定由工具层代码强制，不由模型/prompt 决定（可抵抗 prompt 注入）。」

`CLAUDE.md` 对同一句话保留了原文并加了 ⚠ 勘误（「这句话在 C16 之前是……
改的只是第④层那四类动作」）。README 里那句**原封不动地作为现状陈述**。
只读 README 的人会以为「模型完全不参与安全判定」，而实际上跑命令、访问网络、
发消息、上网搜索四类动作在④层的裁量**由一个模型在做**，
官方公布的拦截率是 89%。

**② 缺省档是「放行」这件事，位置太靠后、说得太轻。**

README 确实写了（`:39`、`:627`、`:934`），但都夹在长段落中间。
对陌生用户而言这是**最重要的一句话**：
「装完直接跑，模型在你的工作区里写文件、跑命令都不会问你。」
它现在没有出现在第一屏的任何位置。

**③ `README.md:37` 引用了已删除的 `/perm`。**

「域名用 `WebFetch(domain:...)` 规则控制，**且该限制翻不过 `/perm` 的放行档**」
——`/perm` 已删除（`CLAUDE.md` 与 `README.md:934` 都这么说）。
与 `README.md`（review）的 A3 同类。

**④ MCP 那条（`README.md:802`）是 S2，已在上面单列。**

## 4.3 一个陌生人 clone 之后跑 `rhine` 之前，必须在第一屏知道的五件事

按「不知道会出什么事」排序：

1. **缺省不问。** `auto` 是缺省预设，工作区内的写文件与跑命令都不弹面板。
   要逐次确认，只能写 `permissions.yaml` 的 `deny` 规则。
2. **克隆别人的仓库前先看 `.rhinecode/hooks.yaml`。** 里面的命令会在启动时
   **直接执行**，不经模型、不经面板——这是本项目最大的攻击面（项目自己这么定性）。
   同目录的 `skills/` 与 `agents/` 是「发给模型的指令」，危险程度低一个量级，
   但也应当同等评审。
3. **`config.yaml` 里是明文 API Key**，别提交。`.gitignore` 只在**本仓库**生效——
   去别的项目跑之前要给那个项目的 `.gitignore` 补 `.rhinecode/`。
4. **`--trace` 的产物含完整对话与被读过的文件原文**（不做任何截断），
   模型读过配置文件时含明文 Key。别贴进 issue。
5. **沙箱管得住文件工具，管不住 `run_command` 跑起来的程序。**
   没有 OS 级沙箱（已知项 #4，且 Windows 上游也没有）。

## 4.4 建议

- README 顶部（安装说明**之前**）加一个五到八行的 `> ⚠️ 开始之前` 引用块，
  只放上面那五条，每条一行 + 一个指向 `SECURITY.md` 的链接。
- 现有的 `## 安全边界`（`:977`）整节保留，改为深度参考。
- 新增的 [`SECURITY.md`](../../SECURITY.md) 承担「怎么报漏洞 + 用户自己要注意什么」，
  README 只留指针——这也符合 GitHub 惯例（仓库首页会自动显示 `SECURITY.md` 徽章，
  安全报告入口也从它读）。

---

# 5. 待办（按建议顺序）

| # | 内容 | 类型 | 代价 |
| --- | --- | --- | --- |
| 1 | **S1**：给 `mcp_add_server` 补一道闸（三选一），并把 MCP stdio 子进程的环境改用 `filtered_environ()` | 🔴 产品改动 | 半天 |
| 2 | **S2**：三处「默认权限模式下每次确认」的文字改掉 | 🔴 纯文档 | 十分钟 |
| 3 | **S3**：给①黑名单的拆分口径补一条护栏（判别输入 + AST 断言 import） | 🟠 加测试 | 半小时 |
| 4 | **S5**：README 顶部加「开始之前」块；`:979` 加 C16 勘误；`:37` 去掉 `/perm` | 🟡 纯文档 | 一小时 |
| 5 | **S4**：`rules.py:121` 改 `fnmatchcase` | 🟠 产品改动（行为变更） | 一小时 + CHANGELOG |
| 6 | `tests/test_perm_protected.py:344-348` 的 docstring 已过时（说 `PipelineOrderGuardTest` 在短路站下仍绿，实测会红） | 🟡 注释 | 五分钟 |

⚠ **S1 与 S2 建议一起做**：它们是同一个成因（`auto` 成为缺省时，
那些「靠④层判 ASK 弹面板」的承诺集体失效），分开做容易只改文字不改代码。
**也应当顺手扫一遍还有没有第三处**——判据是「某条安全承诺的兑现机制是
『④层判 ASK 会弹面板』」。本次已核验过的其余 `other` 类工具
（七个 c13/c15 协作工具、`load_skill`）都有独立论证，不受影响。
