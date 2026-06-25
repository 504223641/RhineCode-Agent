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
    "运行环境：\n"
    "- 工作目录（项目根）：{working_dir}\n"
    "- 操作系统/平台：{platform}\n"
    "- 当前日期：{date}\n"
    "- 当前 Git 分支：{git_branch}\n"
    "- 模型：{model}（protocol: {protocol}）"
)
