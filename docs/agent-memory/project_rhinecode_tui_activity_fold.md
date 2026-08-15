---
name: project-rhinecode-tui-activity-fold
description: "tui-activity-fold 扩展已由 PR #35 合并进 main（2645 全绿）；真机复核跑出 24 条而单测事前抓到 0 条，方法论与两条 Textual 硬事实记在这里"
metadata:
  type: project
  modified: 2026-08-13T15:56:56.697Z
---

**tui-activity-fold（工具活动归并 + 三级信息密度 + 回合状态行）已于 2026-08-13
由 PR #35 合并进 main**，分支已删。全量 2504 → **2645**（skipped 4），29 个提交。

文档在 `docs/extensions/tui-activity-fold/`，**最值得读的是 `acceptance.md`
而不是 spec**：真机复核跑出 **24 条**问题，而单元测试事前抓到 **0** 条。

## 两条 Textual 硬事实（读源码才看得出来，都不报错）

1. **选区功能只对 `Content` 成立。** Rich 渲染对象经 `RichVisual` 走，
   三处一起失效：不画高亮 / 拿不到字符偏移（`meta["offset"]` 是 `Content`
   才写的）/ `get_selection` 默认返回 None。
   ⚠ **只补第三条是陷阱**：「全选 + 复制」会通、测试会全绿，
   而用户看到的仍是「连选择都不行」。
2. **`background: transparent` 只让颜色透下来，字符照画不误。**
   padding 那几列画的是**空格**，会把下层的框线一个个擦掉。

另：`Click` 事件只判 `mouse_up_widget is mouse_down_widget`，**不看鼠标
有没有移动**——拖选也会触发它。

## 这一轮最贵的三条方法论

- **判据要选在「能分辨出用户实际状态」的那一层。** 补完 `get_selection`
  之后所有复制类测试全绿，而用户看到的一点没变。换成「问不问得出字符偏移」
  才有分辨力。
- **注释里的「因为 X 所以安全」要当成待验证的假设。** 本轮有两条是自己
  写下的理由被实测推翻。
- **一条失败可以伪装成「机器很慢」。** 驱动测试的收尾写在用例最后一行，
  断言失败时不执行 → 工作线程醒不过来 → `shutdown_default_executor()`
  （Python 3.11 无超时）永久挂住 → 看到的是「跑十几分钟只有一串点和一个 F」。
  已立项 `docs/todo/6-driver-test-cleanup.md`。

## 遗留

- **`◈` 一符两用**：用户消息前缀（`#99FFFF`）vs 旋转标记第二、四帧
  （`#7AEEFF`），两处都在行首、两种青色几乎分不出。**待决，刻意不默认它合理。**
- 真机的字形宽度那条通过**不等于**风险不存在——验的是这一台机器、
  这一个终端，换终端 / 字体 / locale 都可能重新暴露，退路在 spec F16
  （只需替换 `SPINNER_FRAMES` 一张常量表）。

相关：[[feedback_interactive_verification]]、[[feedback_pr_format]]
