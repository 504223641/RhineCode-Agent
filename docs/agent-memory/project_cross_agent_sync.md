---
name: project_cross_agent_sync
description: Claude Code 与 Codex 共用同一份项目理解的接线已完成；两边私有记忆不可合并是查证过的结论，别再试
metadata:
  type: project
  modified: 2026-08-16
---

RhineCode 由 Claude Code 与 Codex 交替开发。2026-08-16 在分支 `feat/cross-agent-sync`
上做完了「双方理解一致」的接线，设计与不变量写在 `CLAUDE.md` 的「跨 Agent 协作」一节，
护栏在 `tests/test_cross_agent_sync.py`（11 条）。

## 三条查证过的硬事实（别再重新调研）

1. **两边的私有记忆目录不可合并，这是结构性的、不是没找到办法。**
   Codex 的 `MemoriesToml`（`codex-rs/core/config.schema.json`）里**一个 path/dir 键都没有**，
   记忆锁死在 `~/.codex/memories/`；Claude Code 这边的目录由 harness 固定。
   两者互不可见、都不可重定位 —— 所以共享层只能是**第三个地方**（仓库）。
   ⚠ 「把两个 Agent 的记忆目录指到同一处」这个想法看起来永远像是更优解，它不可行。

2. **Codex 的 `project_doc_max_bytes` 不是模型上下文窗口**，是产品级默认值 32768，
   与用哪个模型、窗口多大无关（换 1M 窗口的模型也仍然是 32768）。
   超出部分在 `codex-rs/core/src/agents_md.rs` 里 `data.truncate(remaining)`
   **从文件中间按字节切**，只 `tracing::warn!`、TUI 上无提示。

3. **项目级 `.codex/config.toml` 需要该项目被标记 trusted 才生效。**
   loader 原文：`repo $(git rev-parse --show-toplevel)/.codex/config.toml
   (loaded but disabled when untrusted)`。不信任时**加载但整体禁用且不显式报错** ——
   排查「配了没生效」时先查这个。桌面版与 CLI 共用同一套配置栈。

## 验收方法（第一版是错的，这里是修正后的）

⚠ **第一次验收用的问题「②″ 保护路径为什么是出口处的收紧器」是无效的**，
虽然 Codex 答得完全正确。原因：那块知识在 CLAUDE.md 里**有两处**——安全边界那节
（64%）和**架构表的 Permission 行（13%，而且更详细）**。Codex 从前一处就答全了，
而 13% 落在默认 32 KiB 切点之内。**那道题在配置完全没生效时照样能过，等于没验。**

**教训：判断一个探针够不够深，不能看「这块知识属于哪一节」，要看那个短语
在全文的实际字节偏移，并且确认它只出现一次。** 文档里同一件事被写在两处
（一处速查、一处详解）是本项目的常态，所以这个陷阱会反复出现。

正确的做法是一个**深度递进的梯子**，失败时还能据此判断截断发生在哪一段。
当前四个探针与判据钉在 `tests/test_cross_agent_sync.py::TruncationProbeTest`
（断言「只出现一次」+「偏移大于 Codex 内置默认值 32768」），
CLAUDE.md 改版后探针若上浮到切点之前会当场红：

| 深度 | 问什么 |
| --- | --- |
| 24% | 环境变量过滤为什么不能用裸 `AUTH` / 裸 `KEY` 做匹配片段 |
| 52% | 从 Git Bash 驱动 e2e 必须设什么环境变量 |
| 92% | 「模型不会主动组队」那条已知项后来怎么反转的 |
| 98.6% | 代码注释规范里的函数注释示例叫什么、复杂函数要说明哪**六**件事 |

最后一条最关键：它在 98.6% 处，答得出就说明整份文档都到了。

**2026-08-16 实跑结果：四题全中，接线成立。** 另有一条意外收获——第四题当时被
问成了「哪三件事」（把示例 docstring 的三个段落当成了规范要求的条数，原文是
六条：函数用途 / 参数 / 返回值 / 主要执行步骤 / 异常与失败 / 副作用）。
**Codex 没有顺着这个错误前提编三条，而是从原文把提问者纠正了。**
⚠ **带错误前提的探针因此比中性提问更有分辨力**：读到原文的一方会反驳，
被截断的一方最可能的行为是顺着前提编——而后者从答案表面上看毫无破绽。
下次设计这类验收题时可以刻意埋一个可证伪的错误前提。

这与 [[feedback_safety_counterproof_sampling]] 是同一类取样陷阱——**一条恒真的判据
比没有判据更坏，因为它会让你放心**。

写新记忆的格式与「什么时候该写」见 `CLAUDE.md`，
相关约定见 [[feedback_commit_after_each_change]] 与 [[feedback_branch_before_edit]]。
