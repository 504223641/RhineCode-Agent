"""
环境信息渲染模板文案（供 environment.py 的 EnvironmentInfo.render 引用）。

只存放模板字符串常量，不含逻辑。采集逻辑、字段定义仍在
rhinecode/agent/prompt/environment.py。

这里用 str.format 风格的占位符（{working_dir} 等），由 render() 调用 .format(**…) 填充，
从而把「文案措辞」与「字段如何取值/拼接」分离。
"""

# 环境信息模板：渲染成多行文本注入到 <system-reminder>，占位符与 EnvironmentInfo 字段一一对应。
# git_branch 由采集逻辑填充：在 git 仓库内为当前分支名，非 git 仓库/获取失败时为「无」。
ENVIRONMENT_TEMPLATE = (
    "当前运行环境：\n"
    "- 工作目录（项目根）：{working_dir}\n"
    "- 操作系统/平台：{platform}\n"
    "- 命令解释器：{shell}（`run_command` 的命令交给它执行）\n"
    "- 当前日期：{date}\n"
    "- 当前 Git 分支：{git_branch}\n"
    "- 模型：{model}（protocol: {protocol}）"
)

# ⚠ **「命令解释器」这一行为什么必须有，以及为什么只写一个名字。**
#
# 2026-09-17 两份真实 trace 实录：模型不知道 `run_command` 用的是哪个 shell，
# 于是把三种都猜了一遍——POSIX（`| tail -40`、`VAR=x cmd`、`cmd &`）、
# PowerShell（`| Select-Object`）、cmd（`set X=0 && cmd`）。一次任务里
# **4 轮迭代、197K 输入 token** 白烧在语法试错上（占那条用户消息的 23%）。
#
# 原先这里只有「操作系统/平台：Windows-10-...」，而 `environment.py` 的字段注释
# 逐字写着它「影响模型生成命令时的语法假设」——**作者知道这一行是干这个用的，
# 只是 `platform.platform()` 的输出不够**：Windows 上 cmd / PowerShell / Git Bash
# 都说得通，而开发机上三种通常都装着。
#
# ⚠ **这里刻意只写解释器的名字，语法细节放在 `run_command.description` 里。**
# 环境信息走**消息通道**（被包进 <system-reminder> 动态注入），**不进可缓存的
# 稳定前缀**——写在这里的每个字都要按轮次重复付费；而工具描述在稳定前缀里，
# 写多少只付一次。两处是一对成对维护点，见 `paired-maintenance`。
