# 3 · P1b 无人值守回归

> 状态：待开工 · 用户已同意推后 · 预计多天
>
> ⚠️ **这件事的成败在取样方法，不在代码。** 开工前务必读完下面「必须修正的取样方法」。

## 背景

P1a 交付了端到端驱动设施（常驻宿主 + 本机回环控制通道 + 瘦客户端），已经用它
验完 16 条场景、产品代码零改动。P1b 的增量只有一件事：**把这些场景变成无人值守
跑得动的回归测试**。

代码量不大，素材现成（`tests/e2e/p0_scenarios.py`、`c11_scenarios.py`、
`align_scenarios.py` 三份预置都在）。

## 范围

1. **`send --force`** —— 解锁 checklist 场景 9（运行中的输入反馈）。
   现在 `DriverCore.send` 的前置检查写死「只有 idle 才能提交」，忙碌态下提交
   递不进产品，被拦的是驱动器**自己的**守卫而不是产品行为。

   **五处成对维护点必须齐改**（漏一处是静默失效）：
   `tests/e2e/protocol.py`（取值）+ `control.py`（`DriverCore` 方法）+
   `host.py` 的 `dispatch` 分支 + `client.py`（子命令）+ `test_e2e_control.py`

2. **把已跑通的场景剧本化**成无人值守回归。

## ⚠️ 必须修正的取样方法

**这一节比代码重要，请完整读完。**

C11 那轮验收 43/43 全过，却完全漏掉了「Skill 作者期」这一整层。
**根因不是漏测某一条，是取样偏了**：五个被测 Skill 全是验收者（我）自己写的，
而我知道契约（description 要短、要放 `$ARGUMENTS`、预授权该给多窄、
动作型 vs 指导型），于是写出来的样本天然适配。

用户随手导入一份外部 Skill，四个问题当场全撞上。

> **教训：用自己写的 fixture 验自己设计的 spec，验不出假设本身的问题。**

对齐改造那轮也复现了同一模式 —— 4 个缺陷全部逃过 869 条单测，共同点是落在
**两个模块的接缝处**：单测各自验一侧，交界处没人验。

**P1b 的 fixture 必须包含：**

- **至少一份外来 Skill** —— `tests/e2e/c11_scenarios.py:seed_foreign_skill` 已备好
  （原样搬入本机那份真实的 Claude Code Skill，源路径可用环境变量
  `RHINE_E2E_FOREIGN_SKILL` 指定）
- **故意拒绝几次**，不要全程自动放行 —— 「拒绝后模型反复重试」那个真缺陷
  就是靠拒绝验出来的
- **一条不带预设目标的真实需求**，看链路自然会走成什么样，而不是奔着判据去

## 风险

P1b 最大的风险不是做不出来，是**把偏了的取样固化成回归测试** ——
那会让「测试全绿」长期掩盖同一类盲区，比没有这些测试更糟。

## 要读的文件

- `docs/c11/testing/p1-driver/spec.md` **末节** —— P1b 范围清单、可复用的七个接缝、
  P1a 相对 plan 的四处偏离、P0 验收结论。开工需要的上下文全在里面
- `docs/c11/acceptance/driver-p1a.md` —— P1a 自身的验收
- `tests/e2e/control.py` 的**四条不变量**（违反后果分别是确定性死锁、静默失败、随机红）

## 已知的坑

- 测试里删沙箱目录一律走 `tests/e2e/sandbox.py` 的 `force_rmtree`，
  **不要写 `shutil.rmtree(path, ignore_errors=True)`** —— 撞上 git 留下的只读
  `.git/objects` 会「删一半」，留下残骸且一个错都不报（只在全量测试的并发负载下出现）
- 自动放行只能取「仅本次」，理由见 p1-driver spec
- 真实模型场景**不要变成自动化回归**：模型行为不确定，今天绿明天红的测试最后一定被 skip 掉

---

## 一键开工 Prompt

```
开始做 RhineCode 端到端驱动设施的 P1b（无人值守回归）。

第一步：读 docs/c11/testing/p1-driver/spec.md 的**末节**——P1b 范围清单、
可复用的七个接缝、P1a 相对 plan 的四处偏离、P0 验收结论都在那里，是开工的全部上下文。

⚠️ 开工前必须先读 docs/todo/3-p1b-unattended.md 里「必须修正的取样方法」一节。
摘要：C11 那轮 43/43 全过却漏掉整层，根因是五个被测 Skill 全是我自己写的，
而我知道契约，写出来的样本天然适配。**用自己写的 fixture 验自己设计的 spec，
验不出假设本身的问题。**

所以 P1b 的 fixture 必须包含：
- 至少一份**外来** Skill（tests/e2e/c11_scenarios.py:seed_foreign_skill 已备好）
- **故意拒绝几次**，不要全程自动放行
- 一条不带预设目标的真实需求，看链路自然会走成什么样

范围：
1. send --force（解锁 checklist 场景 9）。五处成对维护点必须齐改，漏一处是静默失效：
   tests/e2e/protocol.py + control.py + host.py 的 dispatch 分支 + client.py +
   test_e2e_control.py
2. 把已跑通的场景剧本化成无人值守回归（素材在 p0_scenarios.py / c11_scenarios.py /
   align_scenarios.py 三份预置里）

注意 tests/e2e/control.py 的四条不变量，违反后果分别是确定性死锁、静默失败、随机红。
删沙箱目录一律走 sandbox.force_rmtree，不要用 shutil.rmtree(ignore_errors=True)。
真实模型场景不要变成自动化回归——模型行为不确定，今天绿明天红的测试最后一定被 skip 掉。

做完这条后把 docs/todo/3-p1b-unattended.md 删掉，并重排 docs/todo/ 下其余文档的序号。
```
