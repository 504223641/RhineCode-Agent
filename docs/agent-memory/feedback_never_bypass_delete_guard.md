---
name: feedback-never-bypass-delete-guard
description: 删除操作的路径必须先校验；曾因空变量导致 rmtree 删掉整个仓库
metadata:
  type: feedback
  modified: 2026-07-27T20:06:16.894Z
---

调用任何递归删除（`shutil.rmtree` / `Remove-Item -Recurse` / `rm -rf`）之前，
**路径必须先过校验**：非空、绝对路径、且落在预期的可丢弃范围内。
**绝不把未经校验的 shell 变量或命令替换结果直接喂给删除函数。**

**Why:** 2026-07-28 在 RhineCode 项目实际发生过一次——用 `sed` 从 JSON 里抽
`workspace` 路径，因 Windows 路径的反斜杠导致 sed 报错、变量成了空串，
传进删除函数后 `Path("")` 解析成 `"."`（当前工作目录），
**把整个代码仓库删光**。靠远端仓库 clone 才恢复（零丢失，纯属侥幸——
本次工作的 6 个提交恰好都已推送）。

根因不是「当时不够小心」，而是**闸门有旁路**：那个项目里
`sandbox.assert_disposable` 被明确定义为「rmtree 之前唯一的闸门」，
而我为图方便新写的 `force_rmtree` 刻意跳过了它，理由是「路径都来自可信来源」。
那个假设被证伪了。

**How to apply:**
1. 从 JSON / 命令输出里取路径时，**用 Python 解析，不要用 `sed`/`awk`**
   ——Windows 路径的反斜杠会让文本处理静默出错。
2. 删除前显式断言：`path` 非空、`Path(path).is_absolute()`、
   且在预期根目录之下。空串与相对路径一律拒绝。
3. 写「兜底清理」类辅助函数时**不要为它开校验旁路**。若某处确实必须绕过
   （如刻意制造的半删状态），在那一处写明理由，而不是让函数整体失去闸门。
4. 长时间工作**尽早推远端**——本地是不可靠的。

相关：[[project_rhinecode_trace_p1]]、[[feedback_commit_after_each_change]]
