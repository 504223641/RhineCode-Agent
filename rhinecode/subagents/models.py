"""
子 Agent 系统的数据结构与常量表（c13 T1）。

本模块是 `subagents` 包的最底层：只定义「一个角色定义长什么样」「加载结果长什么样」，
不做任何 IO、不依赖包内其它模块。解析在 `parser.py`，扫盘在 `discovery.py`。

术语（第一次接触本章时先看这里）：

- **角色（agent）**：一份 Markdown 文件。frontmatter 是配置（工具白黑名单、模型、
  轮次上限、权限档位），正文是该角色的**系统提示**——它伴随子 Agent 的整个生命周期。
- **定义式（role）**：从空白对话起步，加载一个预定义角色。
- **分支式（branch）**：继承父对话历史与工具集，不需要角色定义。

对应 spec 条款见 `docs/c13/spec.md`：F1（定义格式）、F2（三层加载）、
F3（单文件失败不阻断）、F5（内置 explorer）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Optional

from rhinecode.permission.models import PermissionMode


class AgentSource(Enum):
    """
    角色定义的来源层级（spec F2）。

    **成员定义顺序即优先级顺序**（PROJECT 最高、BUILTIN 最低）：
    `discovery.discover_agents()` 按本枚举的迭代顺序逐层扫描，先扫到的同名角色生效。
    与 C11 的 `SkillSource` 同一套做法——调整优先级只需调整这里的成员顺序，
    但改动前务必确认 discovery 的注释仍成立。

    **本项目没有插件层**，因此只有三层（Claude Code 另有 managed 与 plugin 两层）。
    """

    PROJECT = "project"   # <项目根>/.rhinecode/agents/
    USER = "user"         # ~/.rhinecode/agents/
    BUILTIN = "builtin"   # 随包分发


# 来源层的中文展示名，`/agents` 报告用。单独一张表而不是塞进枚举值，
# 是因为枚举值同时是落盘/日志里的稳定标识，不该跟着界面措辞变。
SOURCE_LABELS = {
    AgentSource.PROJECT: "项目级",
    AgentSource.USER: "用户级",
    AgentSource.BUILTIN: "内置",
}


# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

# 角色定义文件的后缀。目录里其它文件（README、草稿、编辑器临时文件）静默跳过。
ENTRY_SUFFIX = ".md"

# 子 Agent 的缺省迭代上限。与 C11 的 `SKILL_MAX_ITERATIONS` 同值、同理由：
# 子任务应当聚焦，一个跑偏的子 Agent 不该把预算烧到主对话那个量级。
DEFAULT_MAX_TURNS = 15

# 角色能声明的迭代上限硬顶。超过就夹到这个值并记警告。
#
# ⚠ **它必须 ≤ `agent/loop.py` 的 `MAX_ITERATIONS`（当前 25）**。
# 为什么不直接 import 那个常量：`subagents` 的数据层若依赖 `agent` 包，
# 本模块就从「只依赖 permission 的数据类」变成「拉起整个 Agent Loop」，
# 而 `parser` / `discovery` 这两个纯解析模块会跟着背上这份依赖。
# 代价是两个数字要人工对齐——`tests/test_subagent_parser.py` 有一条断言钉住
# 「HARD_MAX_TURNS 不大于 MAX_ITERATIONS」，改大了当场红。
HARD_MAX_TURNS = 25

# 同时运行的子 Agent 上限（含前台正在等待的那个）。
# 超限时委派**立即失败**而不排队——排队会让模型拿到一个「成功了但不知道
# 什么时候开始」的结果，比明确失败更难处理（spec F20）。
MAX_CONCURRENT = 3

# 前台等待的超时秒数。超过即自动转后台，委派工具立即返回（spec F19 第二种方式）。
FOREGROUND_TIMEOUT = 60.0


# 本章不支持、但 Claude Code 的角色定义里存在的字段。
#
# 值是给用户看的中文说明——**警告必须具名**（「本项目不支持 memory 字段」），
# 而不是笼统地说「有未知字段」：用户是从别处复制来的定义，他需要知道
# 具体哪一项没生效、以及为什么。
#
# ⚠ **成对维护点**：将来支持了其中某一项，要从这张表里删掉并在 `AgentSpec`
# 加字段、在 `parser.py` 加读取、在 `report.py` 加展示。漏删的表现是
# 「功能做了但用户被告知不支持」。
UNSUPPORTED_FIELDS = {
    "skills": "预加载 Skill（子 Agent 的工具集里排除了 Skill 加载工具，见 spec F13）",
    "memory": "跨会话持久记忆（属 C9 记忆系统的范畴）",
    "isolation": "Worktree 文件隔离（本章不做）",
    "color": "界面显示颜色（本章的任务行不着色）",
    "hooks": "角色专属 Hook（Hook 规则统一从 hooks.yaml 加载）",
    "mcp_servers": "角色专属 MCP Server（MCP 连接在装配期统一建立）",
    "background": "强制后台（改用委派时的 background 参数，见 spec F19）",
    "effort": "思考强度（子 Agent 继承主对话的设置）",
}


# ---------------------------------------------------------------------------
# 数据结构
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AgentSpec:
    """
    一个角色定义的解析结果（spec F1）。

    frozen=True：定义一旦从文件解析出来就不该被改写，不可变也让它能安全地
    在多个后台线程之间共享——本章会有并发的子 Agent 同时读同一份 spec。

    :param name: 角色标识。frontmatter 的 `name`，**缺省取文件名**（去扩展名）。
        与 C11「命令名来自文件系统路径」的口径**刻意不同**，理由见 spec F2：
        Claude Code 的角色文件把身份放在 `name` 上、允许文件名不匹配，
        若强行改用路径，从官方生态复制来的定义会变成另一个角色。
    :param description: 什么时候该委派给它。这是主 Agent 选择角色的**唯一依据**，
        也是本章唯一的必填字段。
    :param body: 正文，即该角色的系统提示。
    :param tools: 工具白名单。**`None` 表示未声明 = 继承主对话工具集**，
        与空元组（声明了但一个都没有）语义不同——后者会让最终工具集为空、
        委派直接失败（spec F14）。
    :param disallowed_tools: 工具黑名单，在白名单结果上再减。
    :param model: 模型标识；`None` 表示继承主对话（frontmatter 写 `inherit` 也归一为 None）。
    :param max_turns: 本角色的迭代上限，已夹在 `[1, HARD_MAX_TURNS]`。
    :param permission_mode: 声明的权限档位；`None` 表示继承。
        **实际生效档位不是这个值**——见 spec F16，取 min(主对话档, 本值)。
    :param source: 来源层。
    :param path: 定义文件路径，报告与排错用。
    :param warnings: 加载期产生的可读提示（未支持字段、越界被夹的数值等）。
        它们**不阻断加载**，只在 `/agents` 里展示。
    """

    name: str
    description: str
    body: str
    source: AgentSource
    path: Path
    tools: Optional[tuple[str, ...]] = None
    disallowed_tools: tuple[str, ...] = ()
    model: Optional[str] = None
    max_turns: int = DEFAULT_MAX_TURNS
    permission_mode: Optional[PermissionMode] = None
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True)
class AgentLoadError:
    """
    单个角色定义的加载失败记录（spec F3）。

    失败**只影响它自己**：其余角色照常可用、程序照常启动。错误在 `/agents` 里可见。

    :param path: 出问题的文件
    :param source: 它所在的层
    :param message: 可读原因（中文，直接展示给用户）
    """

    path: Path
    source: AgentSource
    message: str


@dataclass(frozen=True)
class ShadowedAgent:
    """
    一份**未生效**的角色定义（spec F3）。

    两种成因：被高优先层同名角色覆盖；或同层内重名而字典序靠后。

    为什么要单独记录而不是静默丢弃：用户看到的现象会是
    「我明明写了这个角色，配置却是另一套」，而文件就在那里、没有任何报错。
    列出来他才知道去哪儿找那份生效的。

    :param name: 角色名
    :param source: 这份**未生效**定义所在的层
    :param path: 这份未生效定义的路径
    :param winner_source: 实际生效的那份在哪一层
    """

    name: str
    source: AgentSource
    path: Path
    winner_source: AgentSource


@dataclass(frozen=True)
class AgentCatalog:
    """
    三层扫描的最终产物（spec F2）。

    :param specs: 生效的角色，键为 `name`。**插入顺序即扫描顺序**（项目→用户→内置），
        报告与清单据此展示，保证任何机器上的顺序一致。
    :param errors: 加载失败记录。
    :param shadowed: 未生效的定义。
    """

    specs: dict[str, AgentSpec] = field(default_factory=dict)
    errors: tuple[AgentLoadError, ...] = ()
    shadowed: tuple[ShadowedAgent, ...] = ()

    def project_names(self) -> tuple[str, ...]:
        """
        取全部**项目级**角色名（spec F4 的启动提示用）。

        :returns: 按目录顺序的角色名元组

        项目级角色随代码仓库分发——`git clone` 一个仓库再启动，就会多出几个
        主 Agent 可以委派的角色。每次启动都要提示，让用户有机会去看一眼。
        """
        return tuple(
            name
            for name, spec in self.specs.items()
            if spec.source is AgentSource.PROJECT
        )


def builtin_agents_dir() -> Path:
    """
    返回随包分发的内置角色目录（spec F2 的第三层）。

    :returns: `<本模块所在目录>/builtin` 的绝对路径。目录**可能不存在**
              （例如打包遗漏），`discovery` 会把「目录不存在」当空层处理、不报错。

    写法与 `skills/models.py` 的 `builtin_skills_dir()` 一致，理由也相同：
    本项目以常规目录形式安装（不是 zip import），`Path(__file__).parent` 在
    开发模式与真实安装下都成立，且返回真实文件系统路径，可直接 `iterdir()`。

    打包：本项目用纯 pyproject.toml 配置，setuptools≥61 下 `include-package-data`
    默认为真，包目录内的非 `.py` 文件本来就会一并打包（见 `pyproject.toml` 的注释）。
    """
    return Path(__file__).resolve().parent / "builtin"


__all__ = [
    "AgentSource",
    "SOURCE_LABELS",
    "ENTRY_SUFFIX",
    "DEFAULT_MAX_TURNS",
    "HARD_MAX_TURNS",
    "MAX_CONCURRENT",
    "FOREGROUND_TIMEOUT",
    "UNSUPPORTED_FIELDS",
    "AgentSpec",
    "AgentLoadError",
    "ShadowedAgent",
    "AgentCatalog",
    "builtin_agents_dir",
]
