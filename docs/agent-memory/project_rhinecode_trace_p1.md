---
name: project_rhinecode_trace_p1
description: Trace P0+P1a 已合并进 c11（858 测试全绿）；下一步开 P1b，范围与接手方式在此登记
metadata:
  type: project
  modified: 2026-07-27T11:44:23.904Z
---

2026-07-27 起在 `c11-trace` 分支上做 Trace 设施的 **P1（TUI 驱动器）**。
需求澄清阶段发生过一次**定位修正**，又按工作量切成两轮，两件事都必须知道。

## 定位修正（比交底文档更重要）

`docs/c11/testing/p0-trace/brief.md` §5 当初设想的驱动器是「写死一批场景脚本、无人值守跑完给个
红绿灯」。用户 2026-07-27 明确纠正：**真正要的是让 Claude Code 自己坐在驱动位上**——
往 Rhine 输入框打真实请求 → 读 trace → **弹面板时由 Claude 自己判断是否放行** →
应答 → 继续观察 → 发现问题就改代码 → 重启再跑。目的是让「测试 → 发现 → 优化」
成为 Claude 能独立走完的闭环。原来的「固定策略自动应答」因此从主角降为配角。

技术后果：必须有**常驻会话宿主进程 + 本机回环 socket 控制通道 + 瘦 CLI 客户端**。
理由是 `app.run_test()` 是一次进程内的异步上下文，而 Claude 的每次工具调用都是
独立进程——不常驻就每次从零启动，历史/激活 Skill/挂起的面板全丢。

## 已定的需求决策（不要重新讨论）

- 交付形态：驱动器库 + 两种消费方式；场景以 Python 代码表达，**不做 YAML/DSL**
- 控制通道：**方案 A**（本机回环 socket + 瘦客户端），不是文件轮询、不是 ConPTY/tmux
- 工作区隔离：沿用 `os.chdir`，**不做**项目根参数化（项目里已有十余个测试文件如此）
- 确定性模式与真实模式都要，真实模式是闭环主用法
- 无人值守回归**保留**，但推到 P1b

## 下一步（新会话从这里接）

**开 P1b**。开场只需一句：「读 `docs/c11/testing/p1-driver/spec.md` 末节，开始 P1b」。
它需要的全部上下文都已经在文件里（P1b 范围清单、可复用的七个接缝、P1a 相对
plan 的四处偏离、P0 验收结论）。**换新会话优于压缩**——压缩会丢掉「为什么这么定」
的理由，而 P1b 高度依赖那些理由（如「自动放行只能取『仅本次』」的原因）。

三件已明确、别重新讨论的事：
1. **P1b 的「场景交付」本身就包含 C11 的九条场景**，不是「P1b 做完再做 C11 测试」。
2. **C11 场景 1/3/4 的模型行为判据，Claude 可以用 P1a + 真实模型跑并给结论**
   （2026-07-28 用户纠正过一次「别什么都推给用户」，见
   [[feedback_interactive_verification]]）。但**它们不能进自动化回归**——
   模型行为不确定，今天绿明天红的测试最后一定被 skip 掉。这个区别正是
   P1b「场景清单」要钉死的。
3. 真正只能人眼的只有**终端渲染观感**（流式是否逐块出字、颜色、布局）。

## 当前阶段

**P1a 已完成并已合并进 `c11`**（2026-07-28）。72 个任务七段全做完；
`c11-trace` 经 `--no-ff` 合并到 `c11`（合并提交 `e4c31e2`），已推远端。
全量 714 → **858 全绿**、零残留、零新依赖。**`c11-trace` 分支保留未删。**

合并后还用 P1a 验收了 **P0 的 9 条端到端手测场景**，6 条实跑通过、36 项判据全中
（报告 `docs/c11/testing/p0-trace/acceptance-p0-e2e.md`）。其中场景 2「白名单外调用」——
整个 trace 项目的立项动因——**从「靠运气复现」变成了确定性可重复的判据**，
因为脚本化假模型能精确造出真实模型只会偶发的行为。

交付：`tests/e2e/` 十二个模块（protocol / discovery / sandbox / seeding /
fingerprint / scripted / assertions / control / host / client / scripts）+ 八个测试文件。
产品侧只动三个文件（`bootstrap.py` 加 `provider_factory`/`exclude_tools`、
`conversation.py` 加 `provider_factory`、`tui/app.py` 来源字段可传入 + `_settle_session`），
另在 `tui/app.py` 补了一处 P0 遗留的 `ui_message` 埋点缺口（共四个改动点）。

验收报告在 `docs/c11/testing/p1-driver/acceptance-p1a.md`：6 条场景 5 条已实跑通过，
**只剩场景 2（真实模型）待用户授权**——它花钱、走网络、用真实凭据。

### P1a 期间实测抓到的 8 个缺陷（全属「不报错但行为不对」）

值得记住的三个：① venv shim 使 `Popen.pid ≠ 宿主 pid`，既让名片匹配不上又让
`proc.kill()` 杀错进程导致宿主泄漏；② 客户端只处理 EOF 不处理
`ConnectionResetError`，宿主硬死时那句好文案永远不出现；
③ `rmtree` 撞上 git 只读 `.git/objects` 会「删一半」留下空壳且不报错。
完整清单见验收报告的缺陷表。

### 一次严重事故（教训已单列成 [[feedback-never-bypass-delete-guard]]）

手测时因空变量导致 `rmtree` **删掉整个仓库**，靠远端 clone 恢复（零丢失）。
根因是我给 `force_rmtree` 开了校验旁路。**长时间工作要尽早推远端。**

## P1b 范围（**这是本条记忆存在的主要理由，别丢**）

P1a 交付并经真实驱动磨稳后另开一份 spec，范围已在 `p1/spec.md` 的
「推后到 P1b 的内容」一节完整登记，要点：

1. **固定策略应答者**：默认只放行只读工具与只读 git 子命令，其余自动拒绝并记录；
   放宽为全放行必须场景显式写出、不能是缺省值；**自动放行一律取「仅本次」**
   （不得用「本会话」或「永久」，会写盘改变后续权限求值）。该约束**不适用于**
   外部决策应答者——四档都是要被验收的产品能力。
2. **脚本预设应答者**：来源归入「固定策略」，不新增第四个取值。
3. **场景交付**：C11 checklist 场景 1–5、8–11 共九条；场景 6、7 不重复实现但要在清单里
   点名覆盖来源（`tests/test_skill_startup.py` 与 `tests/test_bootstrap.py` 里的具体
   方法）并注明口径差异；P0 checklist §9 里可自动化的四条（作用域交错、拒绝路径与
   黑名单直拒、压缩动作可解释、会话恢复共存）。
4. **场景清单**：标明每条的运行模式与「已被替代 / 仅部分覆盖不得撤销」。
   C11 场景 1、3、4 的判据本身是模型行为，确定性模式只能验机制，必须标后者。
5. F25 十一项断言词汇的**使用完备性**验收挪到 P1b（P1a 只验「均已实现」）。

## 实测结论（省得重做一遍）

子 agent 在真实 `RhineApp` 上实测过，结论全部记在 `p1/spec.md` 里：
`call_from_thread` 可跨线程驱动 `pilot.press`（含中文逐字符）、可接协程并取返回值；
面板 `display` 与 `app.focused` 运行期可读；忙碌期跨线程调用延迟 0.2–0.3ms；
`permissions.local.yaml` 路径随 cwd 走。
**两个坑是实测出来的**：① 待决交互未结算时宿主**无法退出**（worker 卡在
`box["event"].wait()`，`asyncio.run` 收尾去 join 它）；② 面板抢跑窗口占比 8869/8870
（`_pending_interaction` 置位早于面板挂载）。

**How to apply:** 新会话接手时先读 `docs/c11/testing/p1-driver/spec.md`（含 P1b 清单），
再读 `docs/c11/testing/p0-trace/brief.md`（但注意 §5 的驱动器设想已被上述定位修正推翻）。
沿用 [[feedback_subagent_review_before_approval]] 与 [[feedback_commit_after_each_change]]。
相关：[[project_rhinecode_trace_recorder]]（P0）、[[project_rhinecode_c11_skills]]。
