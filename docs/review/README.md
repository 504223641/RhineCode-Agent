# RhineCode 代码审查 · 总报告

> 2026-08-22。审查目标：**开源到 GitHub 公开仓库 + 个人作品集展示**。
> 本轮只报告、不改代码。每条都带 `文件路径:行号`。
> 原始数据在 [`00-baseline.md`](00-baseline.md)。

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

---

## A. 发布阻塞（开源前必须做，共 4 条）

### A1 🔴 无 LICENSE 文件

- **证据**：全仓 `ls LICENSE*` 无命中；README 全文无任何许可证声明
- **后果**：法律上等于「版权全部保留」。别人 clone 后**不能合法使用、修改、分发**，
  公司法务会直接拦掉。GitHub 上无 LICENSE 的仓库，严肃使用者一律跳过。
  这不是锦上添花，是**开源这个动作本身的前提**
- **建议**：MIT（最宽松、作品集友好，别人用你的代码零心理负担）或
  Apache-2.0（多一层专利授权条款，企业更放心）。**不建议 GPL**——传染性会劝退多数使用者
- **代价**：一个文件 + `pyproject.toml` 加一行 `license`

### A2 🔴 `textual` 的版本下界是错的，且会真的让人装不上

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

---

## B. 产品缺陷（用户能感知，共 3 条）

### B1 🔴 「永久放行」写盘失败时静默降级成「本会话放行」

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

- **证据**：`__main__.py` 只捕 `KeyboardInterrupt`（启动期四类错误的兜底做得很完整，
  但 TUI 运行期没有）
- **后果**：Textual 会改终端模式（raw mode、备用屏幕缓冲）。逃逸出来的异常会带完整
  traceback 崩到终端，用户可能得敲 `reset` 才能恢复
- **建议**：包一层 catch-all，打印友好信息 + 提示「完整堆栈见 xxx」，并确保终端复位
- **代价**：小

### C7 🟠 Provider 层把所有错误压成一个字符串

- **证据**：`provider/deepseek.py:264-265` 一句
  `except Exception as e: yield StreamChunk(type="error", content=str(e))`
- **后果**：401（key 填错）/ 429（限流）/ 网络断开 / 上下文超长，用户看到的是**同一坨
  SDK 原文**。且无重试退避（依赖 SDK 默认）。**这是新手用户最容易撞上的体验缺口**
- **建议**：一张错误分类表——key 无效 → 「请检查 config.yaml 的 api_key」；
  429 → 自动退避重试；上下文超长 → 提示 `/compact`
- **代价**：中等

### C8 🟠 主对话的 LLM 调用没有超时

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
| **第二批** | B1 永久放行、B2 F22 接线、**B3 fork 子对话补 `hooks`** | 半天 | 三条用户能感知的缺陷；B3 是**安全边界被绕过**，优先做 |
| **第三批** | C3 + C4 两条护栏 | 1 小时 | **性价比最高**——20 行代码钉住两个「漏改一律不报错」 |
| **第四批** | A2 textual 下界 + C1 CI | 1 天 | 需要干净 venv 二分验证，与 CI 一起做 |
| **第五批** | A3 README 重写 + A4 截图 | 1 天 | 作品集门面 |
| **第六批** | C5–C10、D1–D4 | 按需 | 不阻塞发布 |

**如果只做一件事**：第三批。20 行测试，钉住两个项目自己列为「⚠ 致命不变量」
却没有护栏的地方。

**如果只做一天**：第一批 + 第二批 + 第三批。（B3 只有一行实参，别跳过。）

---

## ⚠ 这份报告只覆盖了「已经查出来的」

上面六批是**修**的顺序，不是**查**的清单。五个审查阶段里，阶段 0 完成、
阶段 2 与 3 各完成约一半、**阶段 1 只出了清单没做动作、阶段 4 与 5 完全没开始**。

**R1（能力交互矩阵）已完成**，产出在 [`03-architecture.md`](03-architecture.md)——11 个维度 55 格，有护栏 34 / 只有验收记录 1 / 空 11 / 结构性不可能 9，空格子按后果排了序（上面的 B3 就是从那里查出来的）。

**其余剩余工作与每项的一键 Prompt 见 [`NEXT.md`](NEXT.md)**——照着从上往下做，
审查就完整了。其中最该先做的两项是 R1（能力交互矩阵）与 R2（安全复审）：
它们是纯只读、不动代码，且回答的正是本次审查最初的两个问题
（有什么潜在问题、是否存在冲突）。
