---
name: feedback_doc_sync_before_merge
description: 合并到 main 之前必须逐份核对「须同步更新的文档」是否已是最新，确认后才能合
metadata:
  type: feedback
  modified: 2026-07-29T14:23:49.072Z
---

2026-07-29 用户要求：**每次合并到 main 之前，一定要先检查所有必须同步更新的文档是否是最新的，确认了再合并。**

**Why:** 文档与代码脱节**不会编译报错、不会测试变红**——它只会在下一个人（包括未来的我）照着过期文档推理时才显形，而那时已经晚了。`CLAUDE.md` 尤其严重：它是每个 session 无条件加载的上下文，写错一句话等于给后续所有对话装了一个错误前提。PR 一旦合进 main，这些错误就正式生效了。

**How to apply:** 开 PR **之前**（不是之后）逐份走一遍下面这张表，把「本次是否需要动」与「是否已动」两栏都填了再合。表里的项不是「可能要改」，是**必须逐份确认过**——确认「不需要改」也算确认。

| 文档 | 什么时候必须动 |
| --- | --- |
| `CLAUDE.md` 能力表 | 新增章节，或新增不占章节号的扩展 |
| `CLAUDE.md` 架构分层速查表 | 新增包/层，或某层多了致命不变量（⚠ 列） |
| `CLAUDE.md` 成对维护点 | **只要新增了任何「漏改不报错」的配对**（这一节最容易漏） |
| `CLAUDE.md` 安全边界 | 新增外部面、新增已知边界 |
| `CLAUDE.md` 已知后续工程项 | 兑现了其中某条（划掉），或新增一条 |
| `CLAUDE.md` 常用命令 / 斜杠命令说明 | 命令行为变化、新增例外 |
| `CLAUDE.md` 测试章的**测试项数** | **每次测试数变化**（现写死了具体数字） |
| `docs/internals/capabilities.md` | 任何「实际行为与边界」的变化（阈值、降级路径、生效范围） |
| `docs/internals/architecture.md` | 新增包/模块，或某层内部分工变化 |
| `docs/internals/config.md` | **新增/修改任何配置项**（含模板） |
| `docs/internals/testing.md` | 新增测试文件或覆盖面 |
| `README.md` | 面向用户的简版：新增配置项、新增命令、新增能力 |
| `docs/extensions/README.md` | 扩展状态变化 |
| `docs/todo/` | 做完某项要删并**重排全部序号**，README 清单同步 |

**核对方式**：别凭印象。用 `grep` 去搜那些写死的事实（数字、"七个固定模块"这类计数、旧的举例、已过时的说明），比逐份通读快且不会漏。

**教训来源**：web_fetch 扩展那次（PR #11）已经合了才想起来核对，回头查出 4 处遗漏——`CLAUDE.md` 测试章的项数还写着旧值、`internals/config.md` 没登记新配置项、`internals/architecture.md` 没有新包、`README.md` 的配置示例缺新字段。四处都不会让任何测试变红。

相关：[[feedback_pr_format]]、[[feedback_commit_after_each_change]]。
