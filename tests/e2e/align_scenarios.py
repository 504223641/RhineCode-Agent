"""
用 P1a 驱动设施验收**对齐 Agent Skills 开放标准**改造的端到端场景预置。

对应 `docs/c11-align/checklist.md` 第九节那 7 条场景。与 `c11_scenarios.py`
的关系同前：同一套设施、另一份 checklist，放独立模块以免「这个 fixture 是给谁用的」
说不清。

## 同样是只有预置、没有剧本

这 7 条的判据大半落在「模型实际收到了什么、实际做了什么」上，必须用
`--mode live` 跑真实模型。脚本化假模型能验的只是机制，验不了「一份外部 Skill
搬进来之后模型能不能正常按它工作」。

## ⚠️ 预授权的取值口径

`allowed-tools` 现在是**预授权**（本次执行内免确认），不是工具收窄。
预置里声明什么，就意味着「那个操作在执行期间不会弹确认面板」——
场景 2 与场景 3 的全部判据都建立在这一点上。
"""

from __future__ import annotations

import os
from pathlib import Path

from tests.e2e import seeding

# 复用 C11 那份「原样搬入外部 Skill」的实现（它读的是本机那份真实的
# frontend-design，找不到就明确抛错——静默退回内置样板会让这条场景失去意义）
from tests.e2e.c11_scenarios import FOREIGN_SKILL_ENV, seed_foreign_skill  # noqa: F401


# ---------------------------------------------------------------------------
# 场景 2 / 3：预授权的边界与安全性
# ---------------------------------------------------------------------------
# 一对形成对照的 Skill：
#
#   - `notetaker` 只授权写文件 → 写操作免确认、**跑命令仍要确认**（场景 2）；
#   - `runner` 授权全部命令 → 危险命令仍被第①层黑名单拒、**面板一次都不弹**（场景 3）。
#
# 场景 3 是本次改造最该钉住的一条：预授权是唯一扩大模型自由度的改动，
# 它翻不过前三层这件事必须有真实物证，而不只是单测里的断言。


def seed_grant_pair(workspace: Path, user_dir: Path) -> None:
    """预置场景 2 与场景 3 用的一对 Skill 和一个小项目。"""
    seeding.seed_files(
        workspace,
        {
            "README.md": "# 演示项目\n\n用来验预授权边界。\n",
            "notes.md": "# 现有笔记\n\n- 第一条\n",
        },
    )

    # 只授权「写文件」。跑命令**刻意不授权**——场景 2 靠这个对照来证明
    # 「未声明的操作照常弹面板」。
    seeding.seed_project_skill(
        workspace,
        "notetaker",
        {
            "description": "把内容整理成笔记写进文件",
            "when_to_use": "用户说「记下来」「写进笔记」时",
            "allowed-tools": ["Write", "Edit", "Read"],
        },
        "把用户给的内容整理好写进指定文件。\n\n"
        "若用户还要求确认结果，用 `run_command` 跑一条命令看看。\n\n$ARGUMENTS",
    )

    # 授权**全部命令**。这是场景 3 的关键：即便如此，危险命令仍应被第①层拦下。
    seeding.seed_project_skill(
        workspace,
        "runner",
        {
            "description": "执行用户要求的命令",
            "when_to_use": "用户要求跑某条命令时",
            "allowed-tools": ["Bash"],
        },
        "执行用户要求的命令并汇报结果。\n\n$ARGUMENTS",
    )


# ---------------------------------------------------------------------------
# 场景 4 / 5：模型自行发起 fork 与两个可调用性开关
# ---------------------------------------------------------------------------
# 三个 Skill 覆盖可调用性的三种组合：
#
#   | Skill      | context | disable-model-invocation | user-invocable |
#   |------------|---------|--------------------------|----------------|
#   | summarize  | fork    | （缺省假）               | （缺省真）     |
#   | deploy     | —       | true                     | （缺省真）     |
#   | houserules | —       | （缺省假）               | false          |
#
# 三行分别验：模型可自行发起 fork（场景 4）、被挡下但用户仍可触发、
# 不进菜单但模型仍可发起（场景 5）。


def seed_invocability_trio(workspace: Path, user_dir: Path) -> None:
    """预置场景 4 与场景 5 用的三个 Skill 和一个可分析的小项目。"""
    seeding.seed_files(
        workspace,
        {
            "app/__init__.py": "",
            "app/main.py": (
                '"""入口。"""\n\n'
                "from app.util import helper\n\n\n"
                "def main():\n"
                "    print(helper())\n"
            ),
            "app/util.py": '"""工具。"""\n\n\ndef helper():\n    return "hi"\n',
            "README.md": "# 小项目\n",
        },
    )

    # 场景 4：fork + 标准缺省（模型可自行发起）
    seeding.seed_project_skill(
        workspace,
        "summarize",
        {
            "description": "通读代码并给出一段结构小结",
            "when_to_use": "用户问「这个项目是做什么的」「帮我梳理一下结构」时",
            "context": "fork",
            "allowed-tools": ["Read", "Glob", "Grep"],
        },
        "通读项目代码，给出一段简短的结构小结（不超过 5 行）。\n\n$ARGUMENTS",
    )

    # 场景 5 上半：模型不得自行发起
    seeding.seed_project_skill(
        workspace,
        "deploy",
        {
            "description": "把当前分支部署到预发环境",
            "when_to_use": "用户说「部署」「发布到预发」时",
            "disable-model-invocation": True,
        },
        "执行部署流程。第一步：回复一行「部署流程已就绪」。\n\n$ARGUMENTS",
    )

    # 场景 5 下半：不进菜单，但模型可自行发起
    seeding.seed_project_skill(
        workspace,
        "houserules",
        {
            "description": "本项目的编码约定",
            "when_to_use": "写或改本项目代码前，需要知道命名与风格约定时",
            "user-invocable": False,
        },
        "本项目的约定：所有函数名以 `rc_` 开头。\n"
        "被问到约定时，直接引用这一条。\n\n$ARGUMENTS",
    )


# ---------------------------------------------------------------------------
# 场景 6 / 7：迁移提示与无能力字段告知
# ---------------------------------------------------------------------------
# 这两条**不需要模型参与**，判据全在 `/skills` 报告里。放在一起是因为它们
# 验的是同一件事：**改造引入的行为差异必须让用户看得见，不能静默**。


def seed_notice_pair(workspace: Path, user_dir: Path) -> None:
    """预置场景 6 与场景 7 用的两个 Skill。"""
    seeding.seed_files(workspace, {"README.md": "# 提示演示\n"})

    # 场景 6：C11 时代的写法（下划线 + 内部工具名），语义已反转
    seeding.seed_project_skill(
        workspace,
        "legacy",
        {
            "description": "C11 时代写的 Skill",
            # ⚠️ 故意用下划线写法。它在旧版本里表示「只有这两个工具可见」，
            # 现在表示「这两类操作免确认」——**语义相反**，必须有明确告知。
            "allowed_tools": ["read_file", "glob_files"],
        },
        "只读地看看项目。\n\n$ARGUMENTS",
    )

    # 场景 7：六个本版本没有对应能力的标准字段
    seeding.seed_project_skill(
        workspace,
        "fancy",
        {
            "description": "声明了一堆本版本不支持的字段",
            "background": True,
            "agent": "explorer",
            "effort": "high",
            "hooks": {},
            "paths": "src/**",
            "shell": "powershell",
        },
        "随便做点什么。回复一行「fancy 跑过了」。\n\n$ARGUMENTS",
    )
