# RhineCode 代码审查 · 总报告

> 2026-08-22。审查目标：**开源到 GitHub 公开仓库 + 个人作品集展示**。
> 本轮只报告、不改代码。每条都带 `文件路径:行号`。
> 原始数据在 [`00-baseline.md`](00-baseline.md)。
>
> ⚠ **B / C 两节的九条已由 [R3](02-robustness.md) 逐条坐实**（复现路径 / 影响面 /
> 修法 / 代价与成对维护点全部走完，11 条真跑了复现）。**那一轮推翻了本文与
> `00-baseline.md` 的七处说法**——凡本文与 `02-robustness.md` 冲突，**以后者为准**，
> 更正清单在它的「附一」。要紧的是 **C6 整条要重写**、**C7 与 C8 的结论要改**、
> **C3 的修法照原样写会既误报又漏覆盖**。

## 先说结论

**这个项目的代码质量显著高于典型的个人项目，也高于多数商业项目的同规模模块。**
这不是客套话，是几个可核验的数字：

| 指标 | 实测 | 参照 |
| --- | --- | --- |
| 测试覆盖率 | **91%**（且无一个 0% 文件） | 多数项目在 40–70% |
| ruff 全量扫描的**真问题** | **2 个** / 51,847 行 | — |
| mypy 错误 | 109 / 171 文件（**从未跑过类型检查**） | 同规模项目首次跑常见数百至数千 |
| bugbear 告警 | 2 个（其中 1 个还是误报） | — |
| 锁纪律违反 | **0**（AST 机械核验 15 处临界区） | — |
| 三次全量测试 | **3,323 / 3,323 全绿，条数一致** | — |

几条被文档反复强调的「⚠ 致命不变量」，实测**确实成立**，不是纸面规范：
`classifier` 真的没 import `permission` / `agent` / `tools`；`todo` 真的没 import `team`；
15 处锁的临界区里真的没有跨线程调度。**「踩过 5 次死锁」的教训固化进了代码。**

**所以本报告里几乎没有「代码写得差」这类条目。** 问题集中在三处它自己看不见的地方：
发布这件事一件都没做、623 次提交零机器验证、以及少数几处「已知的脆弱点缺一道本可以
很便宜就加上的护栏」。

⚠ **外加一类它结构上看不见的**：**一次缺省值变更让若干条安全承诺同时失效，
而那些承诺散在别处、没有任何东西把它们和那个缺省值关联起来。**
`auto` 成为缺省预设时，「靠第④层判 ASK 弹面板」的兑现机制没了——
B4 / B5 两条 🔴 都是这个成因，由 [R2 安全复审](04-security.md) 用**变异实测**查出。

---

## A. 发布阻塞（开源前必须做，共 6 条）

### A1 🔴 无 LICENSE 文件

- **证据**：全仓 `ls LICENSE*` 无命中；README 全文无任何许可证声明
- **后果**：法律上等于「版权全部保留」。别人 clone 后**不能合法使用、修改、分发**，
  公司法务会直接拦掉。GitHub 上无 LICENSE 的仓库，严肃使用者一律跳过。
  这不是锦上添花，是**开源这个动作本身的前提**
- **建议**：MIT（最宽松、作品集友好，别人用你的代码零心理负担）或
  Apache-2.0（多一层专利授权条款，企业更放心）。**不建议 GPL**——传染性会劝退多数使用者
- **代价**：一个文件 + `pyproject.toml` 加一行 `license`

### A2 🔴 `textual` 的版本下界是错的，且会真的让人装不上

> ✅ **已由 [R5](01-release-blockers.md) 实跑二分坐实**，但**下面两处要改**：
> ① 真实下界是 **6.2.1**（不是这里建议的 8.0），全量 3,323 条在它之上全绿；
> ② **「启动直接 `ModuleNotFoundError`」只对 < 2.0.0 成立**——2.0.0 ~ 6.2.0 之间
> **import 是过的**，失败形态是运行期行为不对，最后那一档（6.0.0 ~ 6.2.0）
> **只有「选中后按 `Ctrl+C` 复制」默默复制不全**，界面完全正常。
> **比这里描述的更隐蔽。**

- **证据**：`pyproject.toml:9` 声明 `textual>=0.80.0`；而代码 import 了
  `textual.content`（`Content`, `Span`）与 `textual.style`（`Style`）——
  见 `tui/widgets.py:32,34`。这些是 **Textual 3.0+ 才有的 API**。本机实装 **8.2.8**
- **后果**：下界与实际需求**差 7 个大版本**。用户环境里若已有一个满足 `>=0.80.0`
  的旧版本，pip **不会升级它**，启动直接
  `ModuleNotFoundError: No module named 'textual.content'`
- **建议**：改成 `textual>=8.0,<9`（下界取实际验证过的最低版本，上界挡住下一个大版本）
- **代价**：一行。但**下界具体填几**需要在干净 venv 里二分验证一次

### A3 🔴 README 落后整整一个章节，且有多处事实错误

- **证据**：
  - 能力表止于 **C15**，而 C16 已是主线；全文 `classifier` / `分类器` / `C16` /
    `web_search` / `todo_write` 出现 **0 次**
  - `README.md:1146` 写测试「2235 项」，实测 **3,323**（差 1,088 条）
  - `README.md:1064-1065` 的目录树写 `memory/notes.py`、`note_updater.py`——
    **这两个文件不存在**，实际是 `memories.py`、`memory_updater.py`
  - `README.md:37` 把**已删除**的 `/perm` 当现存命令用
  - 目录树整个缺 `classifier/` 与 `todo/` 两个包；`tools/` 少列 5 个、`web/` 少列 3 个
  - `README.md:1212` 是一条 markdown 坏链（指向 `docs/todo/3-delegation-trigger-eval.md`，实际是 `2-`）
  - `README.md:1110` 写「13 条内置命令」，实际 15 条
  - `README.md:1206` 列了 5 个扩展，`docs/extensions/` 下实际有 9 个
- **后果**：**陌生人第一眼只看 README。** CLAUDE.md 维护得极好（测试数字三处自洽、
  与实测一致），README 却落后一章——因为 CLAUDE.md 每次开发都在读，README 没人读
- **建议**：**不要修补，重写。** 现在的 README 是「开发日志式」的：第一屏就是 15 行
  能力表加每条几百字。陌生人需要的顺序是——一句话讲清是什么 → 一张 GIF →
  30 秒装上跑起来 → 然后才是深度内容。现有的 1,250 行降级为 `docs/` 里的深度文档
- **代价**：半天。但这是**作品集场景下性价比最高的一件事**

### A4 🔴 一个 TUI 项目，零截图

- **证据**：全仓 `find -name "*.png" -o -name "*.gif" -o -name "*.svg"` 无命中
- **后果**：GitHub 页面上，一张 30 秒的操作 GIF 的说服力超过前 500 行文字。
  这是终端应用**唯一**能在几秒内证明「它真的能跑」的方式
- **建议**：录 3 张——① 完整对话 + 工具调用 ② 权限确认面板 ③ 子 Agent 并行活动区。
  Windows 下可用 ScreenToGif，或 asciinema 配 agg 转 GIF
- **代价**：1–2 小时

### A5 🔴 三个内置子 Agent 角色没被打进分发包，且静默为空

> 本条由 [R5 跨平台与安装实跑](01-release-blockers.md) 查出，完整论证见那份文档的 **R5-1**。

- **证据**：`pip install .`（不是 `-e`）出来的 wheel 与 sdist 里，
  `rhinecode/subagents/builtin/` **一个文件都没有**——`explorer.md` /
  `planner.md` / `general-purpose.md` 全丢。根因是
  `pyproject.toml:55-56` 的 package-data 只写了 `"rhinecode.skills"`，
  **漏了 `"rhinecode.subagents"`**（Skill 那三个 md 因此完好无损，对照鲜明）
- **后果**：**C13 的「定义式委派」在任何 pip 安装的副本里都是废的**。
  而且它**完全静默**——`discovery.py:135` 的分层扫描对缺失的层是设计上的
  静默跳过（为「用户没建 `.rhinecode/agents/`」写的），于是扫描结果是
  「0 个角色、**0 个错误**」；`render_agent_index`（`subagents/render.py:129`）
  在无角色时**返回空串**（也是刻意的），系统提示里那一段整个消失。
  全链路上唯一会说出真相的是模型猜错名字时的回灌文案
- ⚠ **它比 A2 更难被发现**：A2 会当场 `ModuleNotFoundError`，本条什么都不会说。
  而 `CLAUDE.md` 的 C13 行仍写着「内置三个角色」——那句话对 `pip install -e .`
  成立，对用户走的那条路**不成立**
- **建议**：package-data 补 `"rhinecode.subagents" = ["builtin/*.md"]`；
  **并加一条护栏**——遍历 `rhinecode/` 下全部非 `.py` 文件，断言每一个都被
  某条 package-data 模式覆盖。这与 C3 / C4 同型（「漏改一律不报错」），
  且 3,323 条测试**全部跑在源码树上**，结构性地看不见这类问题
- **代价**：一行 + 20 行护栏。**与 A2 一起做**（都在 `pyproject.toml`，都是 F4）

### A6 🔴 `pyproject.toml` 里那段 package-data 注释的结论是错的

> 同上，完整论证见 [`01-release-blockers.md`](01-release-blockers.md) 的 **R5-2**。

- **证据**：`pyproject.toml:30-54` 的注释带一张「实测三组」表，声称
  「整段删掉 package-data → `.md` 全部进包」，因此本段只是「**冗余保险**」。
  R5 重做四组实验（每组 `rm -rf build`，`pip wheel --no-deps`，构建隔离）：
  **有本段 = 5 个 `.md`；整段删掉 = 0 个；删掉且带 `.git` = 0 个；
  删掉 + `include-package-data = false` = 0 个**
- **后果**：那段注释是**唯一**解释「为什么要保留 package-data」的地方，
  而它给出的理由（「万一将来有人关掉 include-package-data」）让人以为
  **删掉它是安全的**——实测是删掉就全丢。与已知项 #18
  「错误的安全承诺比没有承诺更危险」同型：它让人相信一个不存在的兜底
- **建议**：把那张表换成 R5 的四组表，结论改成「**唯一开关，删掉即全丢**」
- **代价**：改一段注释。**与 A5 同一次改动**

---

## B. 产品缺陷（用户能感知，共 5 条）

### B1 🔴 「永久放行」写盘失败时静默降级成「本会话放行」

> ✅ **已由 [R3](02-robustness.md) 坐实，且门槛比这里说的低**：三种触发形态里**两种不需要任何 IO 故障**，只要用户手改过一次 `permissions.local.yaml`（坏 YAML / 顶层写成列表）。前置问题也已回答——**运行期通道存在，有四条**。

- **证据**：`conversation.py:1816-1821` + `permission/engine.py:570-574`。
  面板文案（`tui/widgets.py:3608`）明写「写入本地配置，**重启仍生效**」，而
  `persist_local_rule` 返回 False 时，代码直接改加一条 session 规则并 `return True`，
  **零提示**。错误串被塞进 `self.load_errors`，而它唯一的消费点
  `conversation.py:487` 是**启动时**渲染进 `startup_notice` 的——
  **运行期追加的条目永远不会显示**
- **后果**：用户点了「永久放行」，以为授权持久化了，重启后凭空失效，且当场没有任何信号。
  这是本报告里**唯一一条会让人不再信任这个工具**的缺陷
- ⚠ **同段 `:1799-1806` 的注释记录着同一类失效已经修过一次**（url 类规则写坏导致
  「永久放行重启后凭空失效」）。这次落在错误处理分支上，是同一个坑的第二个入口
- **建议**：写盘失败时必须**让用户当场看见**。最小改动是把失败信息走
  `startup_notice` 之外的运行期通道（系统行）
- **代价**：小。但要先确认「运行期怎么把消息送到界面」这条通道存不存在

### B2 🔴 一个安全提醒功能实现完整，但从未接线

> ✅ **已由 [R3](02-robustness.md) 坐实，但修法比这里说的复杂**：`classifier/broad.py` **没有域名那一类的判定函数**（要新造，且**不能进 `is_broad_allow` 伞函数**），且接线点**有两个**——`bootstrap.py` 与 `_grant_for_skill`，而后者那侧「不丢弃」的理由并不成立。

- **证据**：`classifier/render.py:244` 的 `render_broad_domain_warning()`，
  全仓（含 `tests/`）**总提及次数 = 1**，即只有定义处
- **后果**：它是 C16 spec F22 的「全域名放行规则会让分类器对网络访问完全不生效」的
  启动提醒——**用户永远看不到**。即用户写了一条 `allow: WebFetch(domain:*)`，
  以为分类器还在把关，实际那一整层对网络访问已经关掉了，而没有任何提示
- ⚠ 这与项目自己的**已知项 #18**（「错误的安全承诺比没有承诺更危险」）同型
- **建议**：接到启动通知链路上，与 classifier 丢弃宽泛命令规则的那批说明同一个出口
- **代价**：小（函数已写好，缺调用点）

### B3 🔴 `context: fork` 的 Skill 子对话拿不到 Hook —— 一条 `pre_tool_use` 拦截规则可以被绕过

> 本条由 [R1 能力交互矩阵](03-architecture.md) 查出，完整论证见那份文档的「空格 1」。

- **证据**：`conversation.py:1595` 构造 fork 子对话的 Agent 时是
  `Agent(sub_provider, self._registry, recorder=self._recorder)`——**没有 `hooks=` 实参**；
  对照主对话的 `conversation.py:326` 是 `Agent(..., hooks=self._hooks)`。
  `agent/loop.py:389` 的缺省是 `NullHookManager()`，而 `pre_tool_use` / `post_tool_use` /
  `post_tool_use_failure` **三个事件全部由 Agent 内部分发**（`agent/loop.py:516`、`:552`）
- **它是被计划过的**：`docs/c12/task.md:512` 逐字写着「把 `hook_manager` 透传给
  `Agent` 构造与 `_run_forked_skill` 的子 Agent」——两处只做了第一处。
  第二处的**注入通道**倒是接上了（`conversation.py:1572`，注释还写着「两处都要接」），
  于是回合级与注入型 Hook 在子对话里正常，**只有工具级三事件是哑的**
- **后果**：用户写的 `pre_tool_use` deny 规则在主对话里验过、确实拦得住；
  之后模型加载一个 `context: fork` 的 Skill，Skill 正文里那句 `git push` **直接跑掉**，
  而 `/hooks` 报告显示这条规则「触发 0 次」。用户会去改 `if:` 条件——根因在别处
- ⚠ **这与 C13 那条安全承诺同型**：`CLAUDE.md` 子 Agent 第 ⑥ 条写「Hook 对子 Agent 全量生效
  ——不生效的话主 Agent 只要把「跑 git push」委派出去就能绕过用户写的拦截规则」。
  委派那条路堵上了（`subagents/runner.py:742` + `tests/test_subagent_integration.py:217`），
  **Skill 这条路没堵**，而它更容易走到（一次 `load_skill` 就够，不需要角色定义）
- **为什么没被发现**：`docs/c12/checklist.md:45` 那条手工验收比的是两条 `turn_start`
  的 `scope`——回合级事件，恰好是能用的那一半
- **建议**：`conversation.py:1595` 补 `hooks=self._hooks`，并加一条护栏断言 fork 子对话里
  工具级三事件照常分发
- **代价**：极小（一行实参）

---

### B4 🔴 `mcp_add_server` 写受保护配置并启动第三方程序，六层防御一层都不生效

> 本条由 [R2 安全复审](04-security.md) 查出，完整论证见那份文档的 **S1**。

- **证据**：实测缺省预设下 `mcp_add_server` 的判定是 `allow @ mode`。
  它的 `kind` 是 `"other"`（`rhinecode/permission/adapter.py:8-9` 的未映射兜底），
  于是①黑名单（`engine.py:379` 只认 `command`）、②沙箱（`:399,404`）、
  ②′网络（`:420`）、**②″保护路径**（`:283` 第一行 `if request.kind != "write_path": return result`）
  **全部原样穿过**；③层只有用户主动写下 `deny: mcp_add_server` 才拦得住；
  ④层在缺省档 `PERMISSIVE`（`presets.py:102,119`）下给 ALLOW；
  它也**没有声明 `classifier_scope`**，不进 C16 审查
- **承诺原文**：`CLAUDE.md:455`「必须保持 `read_only=False` **且先让用户确认**」；
  `rhinecode/tools/mcp_config.py:4-5`「**交给现有权限确认流程拦截**」；
  `rhinecode/permission/protected.py:118` 把 `.rhinecode/mcp.yaml` 列进②″保护路径，
  理由写着「会启动外部程序并把它的工具注册进工具中心」——**清单对，但触发不到**
- **后果**：缺省配置下，模型可以在一次工具调用里、**不弹任何面板**地写一条
  `mcpServers` 配置并立刻把它拉起来。`command` 是任意本地命令
  （`mcp/transport.py:138,174`），`scope: "user"` 还能写到 `~/.rhinecode/mcp.yaml`
  ——那在**工作区之外**，而写盘走 `path.write_text`（`mcp/auto_config.py:490`）、
  不经 `write_file`。那个子进程**继承完整 `os.environ`**（`mcp/transport.py:165`），
  **含 `DEEPSEEK_API_KEY`**
- ⚠ **对照**：模型经 `run_command` 跑命令时环境变量是**过滤过的**
  （`tools/run_command.py:154-188,267`），理由是「命令全放行之后，
  一句打印环境的命令就能拿到 API Key」。**同一条理由对 MCP 子进程完全没有落实**
- **为什么以前没暴露**：这条承诺写于 C7，那时缺省档是 `DEFAULT`、④层判 ASK，
  面板照弹。auto-plan 扩展换掉缺省档之后，兑现它的那一层没了，而文字没改
- **建议**：见 `04-security.md` 的 S1（三选一），并把 `mcp/transport.py:165`
  改用现成的 `tools/run_command.filtered_environ()`
- **代价**：半天

### B5 🔴 「MCP 工具默认每次经人在回路确认」这条承诺已经不成立

> 同上，完整论证见 [`04-security.md`](04-security.md) 的 **S2**。

- **证据**：实测 `mcp__everything__printEnv` 在缺省预设下是 `allow @ mode`；
  对照 `PermissionMode.DEFAULT` 才是 `ask`。而 `presets.py:119` 的
  `DEFAULT_PRESET = Preset.AUTO`、`presets.py:102` 把 `AUTO` 映射到 `PERMISSIVE`
- **仍在这么写的三处**：`CLAUDE.md:455`、`README.md:802`、`README.md:35`
- ⚠ **CLAUDE.md 内部自相矛盾**：`CLAUDE.md:426` 已写明「缺省下仍然会弹面板的
  只剩三类：②″保护路径的写入、网络访问、用户自己写的 Hook `ask` 规则」
  ——**MCP 不在这三类里**。两条相隔 29 行
- **后果**：MCP 远端 Server 被项目自己定性为「外部程序、不可信」，
  而**唯一写明的缓解手段就是那句「每次确认」**
- **建议**：三处文字统一改成「缺省预设 `auto` 下 MCP 工具直接放行」，
  并在 `CLAUDE.md:426` 那份清单旁点明 MCP 不在其中。**与 B4 一起做**
- **代价**：十分钟（但别只改文字不改 B4）

## C. 工程缺口（共 10 条）

### C1 🔴 零 CI —— 623 次提交没有一次被机器验证过

- **证据**：无 `.github/`、无任何 workflow、无 Makefile / tox.ini / noxfile.py
- **后果**：三个具体问题——(a) 只在 Windows 上跑过，Linux/macOS 全未知；
  (b) 54 个 PR 合并时无门禁；(c) 别人提 PR 你没法自动验
- **建议**：GitHub Actions，矩阵 `windows-latest` × `ubuntu-latest` × Python 3.11/3.12/3.13，
  跑 `python -m tests.run_parallel`（33 秒）+ 干净安装验证。
  **作品集视角下，README 顶上一个绿色 CI 徽章比一万字架构说明更能证明「这个人懂工程」**
- **代价**：半天。且这一件事同时解决 A2、C2、C9

### C2 🟠 `rich` 是未声明的直接依赖

- **证据**：`tui/widgets.py:23-29` 直接 import 7 个 rich 模块；`pyproject.toml`
  只声明 textual / openai / pyyaml / httpx
- **后果**：**当前不会崩**——textual 8.2.8 的 `Requires` 里有 rich。风险是将来：
  textual 一直在减少对 rich 的依赖，哪天移除，`pip install rhinecode` 装完启动即 ImportError，
  而你在自己机器上永远复现不了
- **建议**：`pyproject.toml` 加一行 `rich>=13`
- **代价**：一行

### C3 🟠 `Tool.execute` 的签名契约没有护栏

> ⚠ **修法已由 [R3](02-robustness.md) 更正**：契约是**单向**的（原写法误报 `TodoWriteTool`），且要扫 `Tool` 全部子类而不是 `ToolRegistry.default()`（后者只有 7/20，带 `plan_stage` 的 5 个一个都不在）。另：下面「两条路径表现不同」**不成立**，两条最终都兜成同一句「工具执行异常」。

- **证据**：基类签名 `tools/base.py:229` 是 `execute(self, args: dict)`，
  **既无 `cwd` 也无 `plan_stage`**；20 个实现分裂成三种签名。调用方
  `agent/loop.py:2107` 与 `:2265-2269` 靠 `tool.workspace_aware` /
  `tool.plan_safe` 两个布尔标志决定传什么。护栏 `tests/test_loop_cwd_dispatch.py`
  的 7 条用**替身工具**验分发逻辑，**不验真实工具的签名与标志是否一致**。
  全仓只有 `tests/test_todo_tool.py:99` 一处做过 `inspect.signature` 检查
- **后果**：新工具声明 `workspace_aware = True` 却忘给 `execute` 加 `cwd=None` →
  编译过、3,323 项测试全绿 → 只在该工具真被调用时 `TypeError`。
  **且两条路径表现不同**：串行路径（`:2272`）有 `except Exception` 兜成「工具执行异常」，
  并发路径（`:2107`）刻意不加 try/except
- **建议**：**20 行就能机械保证**——遍历 `ToolRegistry.default()` 的全部工具，
  用 `inspect.signature` 断言「标志与形参一一对应」
- **代价**：极小，收益极高。**这是本报告里性价比最高的一条**

### C4 🟠 `tools/__init__.py` 的致命不变量没有护栏

- **证据**：AST 全量扫描发现 10 个包处于同一强连通分量
  （`agent` ↔ `context` ↔ `hooks` ↔ `mcp` ↔ `permission` ↔ `skills` ↔
  `subagents` ↔ `tools` ↔ `web` ↔ `worktree`）。
  `tools/__init__.py` 有 2,799 字节 docstring 把这个结构、为什么不成环、
  违反后的报错形态逐条写明，并标注「**警告：不要在此处 re-export 任何子模块**」。
  `CLAUDE.md` 架构表也把它列为「⚠ 致命不变量」。
  **但同类约束在别的包上都有护栏**（`permission/__init__.py` 的在
  `tests/test_classifier_broad.py:219`，`trace/__init__.py` 的在
  `tests/test_trace_reader.py:404`），**唯独 tools 一条都没有**
- **后果**：图方便加一行 re-export → 测试全绿 → 某个 import 顺序下崩掉，
  且报错位置离原因很远（docstring 自己写了这一点）
- **建议**：一条测试断言 `tools/__init__.py` 的 AST 里没有 `Import` / `ImportFrom` 节点
- **代价**：极小

### C5 🟠 日志设施是空的

- **证据**：产品代码里 `import logging` 只有 1 处（`commands/dispatcher.py:12`），
  唯一调用在 `:150` 的 `_logger.debug`。而全仓**无 `basicConfig`、无任何 handler**
  → 那条日志**永远不输出到任何地方**
- **后果**：这不是「日志少」，是「日志系统不存在」。用户报「它卡住了」时，你除了让他开
  `--trace`（会写下含明文 API Key 的完整对话）之外**没有别的排查手段**
- **建议**：加一个 `--log-file` 与最小的 logging 配置。**不必大改**——
  关键路径（Provider 调用、权限判定、子 Agent 启停）各加一条 INFO 就够用
- **代价**：中等

### C6 🟠 `app.run()` 没有兜底，崩溃可能把用户终端搞坏

> ⚠ **本条已由 [R3](02-robustness.md) 推翻并重写**：终端**不会**坏（textual 的 `finally` 无条件复位），且 catch-all **兜不住主要失败形态**（Textual 自己接住、`app.run()` 正常返回）。真缺口换成两条：**崩溃后进程退出码是 0**、**带 locals 的完整回溯甩给用户且无日志留存**。

- **证据**：`__main__.py` 只捕 `KeyboardInterrupt`（启动期四类错误的兜底做得很完整，
  但 TUI 运行期没有）
- **后果**：Textual 会改终端模式（raw mode、备用屏幕缓冲）。逃逸出来的异常会带完整
  traceback 崩到终端，用户可能得敲 `reset` 才能恢复
- **建议**：包一层 catch-all，打印友好信息 + 提示「完整堆栈见 xxx」，并确保终端复位
- **代价**：小

### C7 🟠 Provider 层把所有错误压成一个字符串

> ⚠ **「且无重试退避」是错的**（[R3](02-robustness.md) 实测：SDK `max_retries=2`、认 `Retry-After`，一次 429 真发了 3 次请求）。缺口是「重试不可见 / 不可配 / 文案不分类」。完整的**错误分类表**在 R3。

- **证据**：`provider/deepseek.py:264-265` 一句
  `except Exception as e: yield StreamChunk(type="error", content=str(e))`
- **后果**：401（key 填错）/ 429（限流）/ 网络断开 / 上下文超长，用户看到的是**同一坨
  SDK 原文**。且无重试退避（依赖 SDK 默认）。**这是新手用户最容易撞上的体验缺口**
- **建议**：一张错误分类表——key 无效 → 「请检查 config.yaml 的 api_key」；
  429 → 自动退避重试；上下文超长 → 提示 `/compact`
- **代价**：中等

### C8 🟠 主对话的 LLM 调用没有超时

> ⚠ **代码注释给的理由已被 [R3](02-robustness.md) 实测推翻**：2 秒超时**没有**腰斩一次 4 秒的连续生成——流式下它是「块间间隔」上限，不是总时长上限。

- **证据**：`Config.request_timeout` 默认 `None`，`provider/deepseek.py:72-73` 仅在非
  None 时传给 SDK；而它**刻意不进配置模板**，唯一赋值点是 `bootstrap.py:464`
  给**分类器**子 provider
- **后果**：分类器有超时、主对话没有，依赖 SDK 默认 600 秒，**用户无任何配置手段**
- **建议**：给主对话一个可配置的默认超时
- **代价**：小

### C9 🟠 包元数据几乎全空，且无 `__version__`

- **证据**：`[project]` 只有 name / version / requires-python / dependencies，
  无 description / readme / license / authors / classifiers / urls；
  `rhinecode/__init__.py` 是 **0 字节空文件**；
  `mcp/client.py:20` 硬编码 `_CLIENT_VERSION = "0.1.0"`，与 pyproject 无引用关系
- **后果**：PyPI 页面会是空白；用户装了包**无法知道自己装的是哪版**（无 `__version__`、
  无 `--version`、无 CHANGELOG），报 issue 时说不清版本，你也无从复现；
  升版时 `_CLIENT_VERSION` 会静默漂移
- **建议**：补全元数据 + `__version__` 从包元数据读（`importlib.metadata.version`），
  `_CLIENT_VERSION` 改为引用它
- **代价**：小

### C10 🟠 三条同源的资源与生命周期问题

| 位置 | 问题 | 后果 |
| --- | --- | --- |
| `tui/app.py:2815` | `box["event"].wait()` 无超时，三类面板共用 | 项目已修过它的一个实例（`conversation.py:1971-1980` 记录的「半夜队友消息唤起主对话 → 面板弹出 → 没人在 → 线程停到天亮」），但**修的是触发条件不是 wait 本身**——任何新的「能弹面板但没人看」的路径都会重现 |
| `subagents/tasks.py:237` | `TaskRegistry._tasks` 只增不删（全文件无 `del` / `pop` / `clear`），每条持有完整 `task_text` 与 `conclusion` | 长会话内存单调增长。对比同文件 `recent_tools`（`:366-368`）是有界的——「一切展示都有界」只落到了列表层没落到字典层 |
| `memory/manager.py:273` | 记忆更新线程 `daemon=True`，全仓零 `.join()` | 进程退出时线程被硬停，**可能在写盘中途**。子 Agent 线程 daemon 化有明确取舍记录（`runner.py:1005-1006`），但那写的是临时状态，记忆写的是**持久化数据**，取舍不同却用了同一策略 |

---

## D. 文档漂移（共 4 条）

**这些不是孤立笔误，是「同一个事实写在 N 处、靠人记得同步」这个结构的必然产物。**

| # | 级别 | 问题 | 证据 |
| --- | --- | --- | --- |
| D1 | 🟡 | CLAUDE.md 自相矛盾：Skill 体检「**八项**」vs「**七项**」 | `CLAUDE.md:98` vs `:263`；`skills/audit.py:126-132` 实际调 **7** 个函数（README 两处也写「八项」） |
| D2 | 🟡 | trace 事件类数三处不一致 | README 两处「二十七类」、CLAUDE.md「二十九类」、`trace/models.py` 实际 **31** 个常量 |
| D3 | 🟡 | `docs/todo/` 内部两处悬空引用 | `2-delegation-trigger-eval.md:6` 指向不存在的 `3-skill-recall-eval.md`；`1-skill-recall-eval.md:70` 指向不存在的 `4-p1b-unattended.md`。**讽刺的是** `docs/todo/README.md:102` 自己记着「⚠ 此处**四次**成为悬空引用……它是这个文件夹里最容易过期的一处」——这是第五次 |
| D4 | 🟡 | 架构表的「叶子包」表述在包级别不成立 | `skills` 被称为叶子包但 import `permission`（`skills/audit.py:59`）；README 把 `web` 标为叶子包，实际它 import `permission` / `provider` / `tools` / `trace`。真实结构写在 `tools/__init__.py` 的 docstring 里，架构表那一层的表述过于简化 |

**建议**：把「测试条数」「事件类型数」「检查项数」这类**可以由代码算出来的数字**
改成生成的，而不是手写。这是唯一能根治的办法——D1 / D2 都是同一个机制的产物。

---

## E. 清理项（共 6 条，都很小）

| # | 问题 | 证据 |
| --- | --- | --- |
| E1 | F821 悬空类型注解：`-> "AskFn"` 而 `AskFn` 从未导入 | `conversation.py:1727`（`AskFn` 定义在 `agent/loop.py:211`）。**ruff 与 mypy 用不同机制各自独立报了这一条** |
| E2 | B904 异常链丢失 | `config.py:347` 漏了 `from`，而**紧邻的 349 行就写了 `from e`** |
| E3 | 45 个未使用 import + 5 个未使用变量 + 6 个无占位符 f-string | `ruff check --select F401,F841,F541`，全部可 `--fix` 自动修 |
| E4 | 77 个 `unused-noqa` | 写了但实际不需要的 `noqa`，说明是防御性加的 |
| E5 | `tests/eval/` 是残留空目录 | 只含 `__pycache__`，`git ls-files` 返回空，文档 0 次提及 |
| E6 | `.gitignore` 缺常见条目 | 无 `.vscode` / `.idea` / `.DS_Store` / `.coverage` / `htmlcov` / `.mypy_cache`——开源后不同 IDE 的贡献者会污染仓库 |

---

## F. 唯一一处真正的测试盲区

**`agent/prompt/texts/` 下 10 个提示词正文文件。**

覆盖率显示它们接近 100%——但那是假的：它们是模块级字符串常量，**import 就算覆盖**，
而内容对不对完全没被验证。而这个项目的行为**很大程度上由提示词决定**
（项目自己的记录：todo-list 那轮「三个静态规则杠杆加满仍是 0 次调用」、
分类器转录污染那次「改法只花了一次提示词改动」）。

即：3,323 项测试守住了所有代码路径，**唯独没守住决定行为的那部分**。
这也解释了为什么历次验收都要靠真实模型跑——那是唯一能验提示词的手段。

**建议**：`docs/todo/` 里现存的第 1、2 条（Skill 召回率评测、委派触发率评测）
正是在补这个洞。它们的优先级应该比现在更高。

---

## 建议的处理顺序

| 批次 | 内容 | 耗时 | 理由 |
| --- | --- | --- | --- |
| **第一批** | A1 LICENSE、C2 rich、E3 自动修、E5 / E6 清理 | 半天 | 纯机械动作，做完消掉一个 🔴 |
| **第二批** | **B4 `mcp_add_server` 补闸 + B5 三处文案**、B1 永久放行、B2 F22 接线、**B3 fork 子对话补 `hooks`** | 1 天 | 五条用户能感知的缺陷。**B4 排第一**——它是本报告里唯一一条「六层防御一层都不生效」；B3 是安全边界被绕过，只有一行实参 |
| **第三批** | C3 + C4 两条护栏 | 1 小时 | **性价比最高**——20 行代码钉住两个「漏改一律不报错」 |
| **第四批** | **A5 / A6 package-data** + A2 textual 下界（值已验出：`>=6.2.1,<9`）+ C1 CI | 1 天 | 二分验证已由 R5 做完，**A2 不再需要实验，直接改**；A5 是 🔴 且只有一行 |
| **第五批** | A3 README 重写 + A4 截图 | 1 天 | 作品集门面 |
| **第六批** | C5–C10、D1–D4 | 按需 | 不阻塞发布 |

**如果只做一件事**：**B4**。它不是「护栏不够」，是一条写在四处的安全承诺
在缺省配置下**完全不成立**，而后果是「模型不经任何确认就能启动一个继承了
你的 API Key 的第三方进程」。

**如果只做一天**：第二批 + 第三批。（B3 只有一行实参，别跳过。）

---

## ⚠ 这份报告只覆盖了「已经查出来的」

上面六批是**修**的顺序，不是**查**的清单。五个审查阶段里，阶段 0、**1**、3、4 完成，
阶段 2 完成约一半、**阶段 5 完全没开始**。

**R1（能力交互矩阵）已完成**，产出在 [`03-architecture.md`](03-architecture.md)——11 个维度 55 格，有护栏 34 / 只有验收记录 1 / 空 11 / 结构性不可能 9，空格子按后果排了序（上面的 B3 就是从那里查出来的）。

**R3（坐实缺陷）已完成**，产出在 [`02-robustness.md`](02-robustness.md)——
B/C 两节的九条全部走完「复现路径 → 影响面 → 修法 → 代价与成对维护点」，
11 条真跑了复现（含本机 SSE 服务器、端到端驱动、两次变异实测），产品代码零改动。
**推翻了本文与 `00-baseline.md` 的七处说法**，并回答了 B1 的前置问题
（运行期通道**存在，有四条**）。**冲突时以那份为准。**

**R2（安全复审）已完成**，产出在 [`04-security.md`](04-security.md) 与仓库根的
[`SECURITY.md`](../../SECURITY.md)——八条顺序 / 匹配不变量做了**变异实测**
（真把代码改坏跑一遍），七条会红、一条全绿；四条重点已知边界全部与代码一致；
查出两条安全承诺与代码不符（上面的 **B4 / B5**），外加两条无护栏 / 跨平台缺口
（S3 / S4，留在那份文档里）。

**R5（跨平台与安装实跑）已完成**，产出在
[`01-release-blockers.md`](01-release-blockers.md)——`textual` 二分实测 20 个版本
（真实下界 **6.2.1**）、干净 venv 的 `pip install .` 全流程、Linux 全量测试
（**3322 / 3323**）、PyPI 名字与 LICENSE 选型。**新查出两条 🔴（上面的 A5 / A6）**，
并推翻/修正了七处既有说法（含本文 A2 的后果描述），更正清单在它的「附一」。

**其余剩余工作与每项的一键 Prompt 见 [`NEXT.md`](NEXT.md)**——照着从上往下做，
审查就完整了。五个 R 里 ~~R1~~ / ~~R2~~ / ~~R3~~ / ~~R5~~ 已完成，
**查的部分只剩 R4（异常处理）与 R7（架构冲突收尾）**；
剩下最该先做的是 **F1（LICENSE）与 F4（`pyproject.toml`）**——
它们要用的值 R5 全部验出来了。
