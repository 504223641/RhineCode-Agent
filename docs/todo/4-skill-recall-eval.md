# Skill 召回率的评测闭环

> 状态：待开工 · 建议走完整 `/spec` · 预计 2–4 天
>
> 建议分支：`skill-recall-eval`（从 `main` 起）

## 背景

Skill 作者期的 **R 系列增补**（见 [`docs/extensions/skill-authoring/spec.md`](../extensions/skill-authoring/spec.md)
末节）把「Skill 写对了却没被自动加载」这个问题改了一轮：清单表头从公告改成指令、
修掉 `load_skill` 一处压制加载的过期描述、内置样板说明改触发词前置、
超预算时保名字砍描述、体检加一条弱提示。

**但改完之后，「召回率」仍然只能靠感觉判断。** 现在验它的唯一办法是：
真实模型跑一遍端到端场景，看 trace 里 `load_skill` 有没有出现。
那是**单点抽样**——一次通过说明不了什么，一次不通过也分不清是措辞不好还是运气不好。

## 这不是我们独有的问题，官方已有成型做法

Claude Code 的 `skill-creator` 插件里有一项叫 **description tuning**，
官方文档的原话是：

> generates should-trigger and should-not-trigger prompts, measures the hit rate,
> and proposes description edits when the skill activates on the wrong requests

也就是：**给一个 Skill 生成两组提示词（该触发的 / 不该触发的），批量跑，
统计命中率，据此提出描述修改建议。** 它还配了基线对比——
同一批提示词在「Skill 可用」与「Skill 被禁用」两种条件下各跑一遍，比较差异。

同一份文档还给了一条关键的方法学要求：**每个用例必须在全新会话里跑**，
因为「作者期留下的上下文会掩盖written instructions 里的缺口」。

## 我们缺的只是最后一层

好消息是**基础设施已经齐了**：

| 已有 | 在哪 |
| --- | --- |
| 常驻宿主 + 本机控制通道，能无人驱动界面 | `tests/e2e/host.py` / `client.py` |
| 真实模型模式 | `--mode live` |
| 每次起宿主自带干净临时工作区（满足「全新会话」） | `tests/e2e/sandbox.py` |
| 行为记录 + 只读阅读器，能精确查 `load_skill` 有没有出现 | `rhinecode/trace/` |
| 断言词汇（含按类型过滤事件） | `tests/e2e/assertions.py` |

缺的是**「批量跑 + 统计命中率 + 对比」**这一层，以及**用例的存放格式**。

## 范围（建议，spec 阶段再定）

1. **用例格式**：每个 Skill 一组 `should_trigger` / `should_not_trigger` 提示词，
   连同期望存在被测 Skill 目录旁边（官方用 `evals/evals.json`，我们可以用 YAML
   以与项目其余配置一致）。
2. **批量执行器**：逐条起干净宿主 → 发提示词 → 等终态 → 读 trace 判
   `load_skill` 是否命中目标 Skill → 记录。
3. **命中率报告**：分别给出该触发组的**召回率**与不该触发组的**误触发率**，
   两个数都要——只看召回率会鼓励把描述写成「什么都匹配」。
4. **基线对比**：同一批提示词在「该 Skill 存在」与「不存在」两种条件下各跑一遍。
   没有基线的话，一个本来模型自己就会做对的任务会被误记成「Skill 生效了」。

## 判据

- 给定一个 Skill 与一组提示词，能跑出**两个数字**（召回率 / 误触发率）而不是
  一句「看起来触发了」
- 改一次描述措辞，能**用数字说明改好了还是改坏了**
- 每个用例在**全新会话**里跑——上一条用例的上下文不得污染下一条

## 已知的坑

- **别用自己写的 fixture 验自己的 spec。** 提示词应当取自**真实使用中说过的话**
  （比如「帮我创建个前端页面」就是用户真实报上来的那句），
  不是为了让测试通过而编的措辞。这条与 `4-p1b-unattended.md` 里记的取样错误同源。
- **误触发率必须一起报。** 只优化召回率的话，最省事的「改进」就是把描述写成
  一堆万能关键词，结果每个请求都加载一堆无关 Skill、把上下文烧光。
- **真实模型跑批量很贵。** 设计时要考虑用例数量的上限，以及能不能用更便宜的模型
  跑召回率（触发判断可能不需要最强的模型——但这个假设本身需要先验证）。
- **`load_skill` 出现 ≠ 加载对了。** 判据要核对**加载的是哪个 Skill**，
  否则「加载了一个错的」会被记成命中。

## 要读的文件

- `docs/extensions/skill-authoring/spec.md` 的 **R 系列增补**一节 —— 这次改了什么、为什么
- `docs/extensions/skill-authoring/acceptance.md` —— R 系列的单点抽样结果长什么样
- `rhinecode/skills/render.py` 的 `_INDEX_HEADER` —— 当前的表头措辞
- `rhinecode/tools/load_skill.py` 的 `description` —— 模型决定调不调它时读的东西
- `tests/e2e/` 整个包 —— 现成的驱动设施
- Claude Code 官方文档的 skills 页（Evaluate and iterate on a skill 一节）

---

## 一键开工 Prompt

```
先检查当前分支：`git branch --show-current`。
- 如果在 `main` 上：**先起一条新分支**再动手。
  `git checkout -b skill-recall-eval`
- 如果已经在别的分支上：确认那是本任务的分支再继续；不是的话先问我。

做 Skill 召回率的评测闭环，走完整 /spec 流程（spec → plan → task → checklist，
每份都要我审批）。

背景：Skill 作者期的 R 系列改了一轮「Skill 写对了却没被自动加载」的问题
（清单表头改指令式、修 load_skill 的过期描述、内置样板说明触发词前置、
超预算保名字、体检加弱提示），但改完之后召回率仍然只能靠感觉判断——
现在验它的唯一办法是真实模型跑一遍端到端看 trace 里有没有 load_skill，
那是单点抽样，一次通过说明不了什么。

Claude Code 的 skill-creator 插件有成型做法：生成 should-trigger /
should-not-trigger 两组提示词，批量跑，统计命中率，据此提改描述的建议；
还配基线对比（Skill 可用 vs 禁用各跑一遍）。我们的基础设施已经齐了
（tests/e2e 的常驻宿主 + live 模式 + 干净临时工作区 + trace 阅读器），
缺的只是「批量跑 + 统计 + 对比」这一层和用例格式。

⚠️ 两个必须守的判据：
1. **误触发率要和召回率一起报。** 只优化召回率的话，最省事的「改进」就是把
   描述写成一堆万能关键词，结果每个请求都加载一堆无关 Skill 把上下文烧光。
2. **load_skill 出现 ≠ 加载对了**，判据要核对加载的是哪个 Skill。

⚠️ 取样纪律：提示词取自**真实使用中说过的话**，不是为了让测试通过而编的措辞。
别用自己写的 fixture 验自己的 spec。

先读 docs/extensions/skill-authoring/spec.md 的「R 系列增补」一节，
再读 docs/todo/4-skill-recall-eval.md 的完整背景与已知的坑。

做完这条后把 docs/todo/4-skill-recall-eval.md 删掉，并重排 docs/todo/ 下其余文档的序号。
```
