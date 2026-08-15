---
name: spec-approval-content-must-be-final-message
description: /spec 逐段审批时，段落正文必须放在回合末尾的最终消息里，不能和 AskUserQuestion 同回合发出
metadata:
  type: feedback
---

在 /spec 流程逐段呈现文档给用户审批时，曾把长段正文写在文本里、紧接着同一回合调 AskUserQuestion 弹确认——结果正文没有渲染给用户，用户在没看到内容的情况下就被要求审批（C9 spec 第 1/2 段发生过，用户事后问「你有显示什么内容给我吗」）。

**Why:** 工具调用之间的文本可能不展示给用户；只有回合最终消息（其后无工具调用）保证可见。

**How to apply:** 需要用户审阅的长内容一律作为回合的最终消息输出、以纯文本提问结尾等用户回复；不要在同一回合里「贴正文 + 调 AskUserQuestion」。AskUserQuestion 只用于短小自包含的选择题（选项 description 里写全上下文）。
