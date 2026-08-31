"""
RhineCode —— 终端 AI 编程助手。

用 Python + Textual 实现，交互体验参考 Claude Code。能力自下而上分层：
Agent Loop 与 Plan Mode、结构化系统提示、五层防御权限系统、MCP 客户端、
两层上下文压缩、记忆系统、斜杠命令、Skill 系统、Hook 系统、子 Agent 系统
（含工作区隔离与协作），以及命令与网络的分类器审查。

⚠ **这个文件此前是 0 字节**（C9）。空 `__init__.py` 在打包上是合法的，但它让
「这个包是什么、什么版本」在**代码里没有任何答案**——于是版本号只好在别处再
抄一份，而 `mcp/client.py` 那份（`_CLIENT_VERSION = "0.1.0"`，它是自报给外部
MCP Server 的客户端版本）就是这么来的：**一个硬编码副本，与 pyproject.toml
之间没有任何东西把它们关联起来**。发版时改了一处忘了另一处不会报错，只是
远端日志里记着一个早就不存在的版本号。

## 版本号的唯一事实源是 `pyproject.toml`

本模块**不硬编码版本**，而是走 `importlib.metadata` 从已安装的包元数据里读。
方向必须是这个：反过来（在这里写死、让 pyproject 去引）要求构建后端支持
`dynamic = ["version"]` 且读得到这个文件，绕了一圈仍然是两份。

⚠ **源码目录里直接跑（未 `pip install -e .`）时读不到元数据**，此时退回
`"0.0.0+unknown"`。这是刻意的：它一眼就能看出「这不是一份装好的包」，
比抄一个可能过期的字面量诚实。开发时 `pip install -e .` 是标准做法，
`CLAUDE.md` 的「常用命令」第一条就是它。

⚠ **本模块必须保持零第三方依赖、零兄弟包 import。** `import rhinecode` 是
所有其它 import 的必经之路，在这里拉起任何东西都会变成全项目的启动成本，
且极易造出包级循环（`tools/__init__.py` 必须保持为空正是同一条理由的极端形态）。
"""

from importlib.metadata import PackageNotFoundError, version as _version

try:
    __version__ = _version("rhinecode")
except PackageNotFoundError:
    # 源码目录里直接跑、包没装过。给一个**看得出不对劲**的值，
    # 而不是一个可能过期的字面量。
    __version__ = "0.0.0+unknown"

__all__ = ["__version__"]
