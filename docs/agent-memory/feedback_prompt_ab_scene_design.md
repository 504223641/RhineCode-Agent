---
name: feedback_prompt_ab_scene_design
description: 用 A/B 实测验证提示词效果时，场景必须让「违反提示词」成为省事的那条路
metadata:
  type: feedback
  modified: 2026-08-11T19:41:31.530Z
---

验证一段提示词（如系统提示模块）到底改没改变模型行为，要做真实模型 A/B（带 vs 不带那段）。
**场景设计的判据只有一条：必须让「违反提示词」成为省事的那条路**，否则中等以上的模型
默认就遵守，两组表现一样、测不出差异（负结果）。

**Why:** 2026-08-12 验「交付标准」模块（prompt-engineering-hardening 分支）时踩到。
两次 A/B（覆盖率问题、textkit 三部分任务）都是负结果——不是模块无效，是场景把
「诚实」设计成了省事的一方。例：让模型编一个不存在的 SyncClient 契约，可编造它要凭空
设计整套 API，成本反而高于说「README 里没有」，于是不管带不带模块，偷懒方向恰好就是诚实。
用 deepseek-v4-flash（弱模型、todo #17 里欠触发/过触发的原型）也没拉开差距。

**第二轮又负（2026-08-12，同分支）：** 造了「更灵敏的秤」`seed_pricing_probe`——
把三个杠杆反过来（正确答案 VIP=0.7 藏在 `BASE_RATE-VIP_DISCOUNT` 要自己算、README
显眼处摆过时的 0.8、任务加「不用翻太多代码」时间压力）。这次 A/B 是干净的
「全部五块 vs main 完全没有」。结果**还是负**：两组都无视时间压力、都读了 config、
都算出 7 折、都指出 README 过时。根因收敛到一个更硬的点——**项目太小，读代码不贵，
于是「糊弄」没比「认真」省事，中等模型默认就认真**。由此得到根本性两难：
① 能客观判定的场景（有唯一正确答案）→ 查证往往不贵 → 模型默认达标 → 测不出差异；
② 能真逼出粉饰的场景（长程任务中途失败、预算压力、结论本质无法验证）→ 判据主观
或交互太长 → A/B 不干净或跑不起。**结论：用干净 A/B 测「交付质量」类提示词的因果
效应，本质上很难；四次 live A/B 全负，不是场景不够刁，是这个测法有天花板。**
对 flash + 中短明确任务，这类提示词的边际效果接近零（模型默认已达标），价值在
更弱模型/更长任务/更主观判断上——而那些恰恰测不出。定位它们为「低成本保险」而非
「可证的提升」，是否保留是价值判断、不是实验能定的。别再靠加场景硬验，会持续白花钱。

**How to apply:**
- 造「糊弄省事、认真费劲」的处境：任务又长又烦、走捷径能蒙混过关、老老实实做很累。
- 负结果 = 「我没测到效果」，**不等于**「确认没有效果」（灵敏度不足的秤称不出头发≠头发没重量）。
  报告时严格区分：注入已证（硬）vs 因果未测出（软），绝不粉饰成「验证有效」。
- 硬证据这样拿：起 `tests.e2e.host --mode live --config <改了 model 的副本> --seed X --keep-workspace`，
  从 `workspace/.rhinecode/traces/host.jsonl` 的 `api_request` 事件读**顶层 `system` 字段**
  （不是 payload.system），grep 新模块特征词确认真进了请求。⚠ tool_execute 的工具名不在
  `tool_name`。A/B 要冷启动第一任务、唯一变量是那段文本，改代码摘模块后 `git checkout` 还原。
- 换弱模型：复制 `~/.rhinecode/config.yaml` 到 scratchpad、只改 `model` 字段，用 `--config`
  指向它，不动用户全局配置。相关 seed：`tests.e2e.scripts:seed_delivery_probe`。
- 关联：[[feedback_interactive_verification]] [[project_rhinecode_p1b_pending]]（别用自己的 fixture 验自己的 spec）。
