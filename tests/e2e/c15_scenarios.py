"""
**C15（子 Agent 协作）** 的端到端场景预置。

与 `p0_scenarios.py` / `c11_scenarios.py` / `c14_scenarios.py` 的关系是
「同一套设施、另一份 checklist」。

## 为什么 C15 到现在才有场景预置

C15 的验收（`docs/c15/acceptance/live-model.md`）全靠真实模型的自然行为，
而那份记录本身写着最重要的一条结论：**模型不会主动组队**
（两轮自然场景 0 次委派、0 条共享任务，已登记为已知项 #17）。
于是「机制本身对不对」这件事，验收里只在用户明说「组一个队」时碰到过一次。

脚本化剧本正是为这种情形准备的：它不问模型愿不愿意组队，**直接让它组**，
于是能稳定地验到那些只在边界上才出现的行为——F18 的自动拒绝、
F20 的连锁上限、唤醒续跑时历史还在不在。

## ⚠ 一条硬约束：剧本必须用 `ScopedScriptedProvider`

`ScriptedProvider` 按**全局调用序号**取轮次，而队员是并发跑的——
主对话的第 2 轮与 worker 的第 1 轮谁先调模型取决于线程调度。
用它写协作剧本，同一份脚本每次跑都可能对应到不同的 Agent 身上。
`ScopedScriptedProvider` 按线程本地的 trace 作用域分派，才是确定的。

## 与 c11/c14 相同的一条约束

预置 Skill 的 `allowed-tools` 里不要写 `mcp_add_server` / `mcp_resolve_server`
（宿主会摘掉这两个工具，见 `host.EXCLUDED_TOOLS`）。
"""

from __future__ import annotations

from pathlib import Path

from tests.e2e import seeding


# ---------------------------------------------------------------------------
# 队员角色
# ---------------------------------------------------------------------------
# 两个**可待命**的角色。⚠ 刻意不声明 `isolation: worktree`——
# C15 实现期定下的边界是「隔离委派与待命互斥」（声明了隔离的队员跑完即退场，
# 不进入待命），理由是「工作区什么时候结算」没有第二个说得通的答案。
# 写协作场景时若给角色加上隔离，唤醒续跑那几条判据会**永远验不到**，
# 而失败现象是「队员叫不醒」——离根因很远。
_WORKER_BODY = """你是团队里的实现者。

工作方式：
- 从共享任务清单里认领属于自己的任务，做完把状态改成 done。
- 需要别人配合时用 send_message 按名字找人，不要自己扛。
- 做完手上的活之后**留在场上待命**，队友可能还会找你。
"""

_REVIEWER_BODY = """你是团队里的审阅者。

工作方式：
- 只读地检查别人的产出，不修改任何文件。
- 有问题就 send_message 告诉对应的人，说清楚是哪一条、问题在哪。
- 检查完把结论发给 main。
"""


def _seed_roles(workspace: Path) -> None:
    """两个协作角色：一个能写、一个只读。"""
    seeding.seed_project_agent(
        workspace,
        "worker",
        {
            "description": "需要实际改代码、写文件的活派给它。做完会留在场上待命。",
            "tools": "read_file, write_file, edit_file, glob_files, grep_content",
            "max_turns": 10,
        },
        _WORKER_BODY,
    )
    seeding.seed_project_agent(
        workspace,
        "reviewer",
        {
            "description": "需要复核别人产出的活派给它。只读，不改任何东西。",
            "tools": "read_file, glob_files, grep_content",
            "max_turns": 10,
        },
        _REVIEWER_BODY,
    )


def _seed_allow(workspace: Path) -> None:
    """
    让队员在非交互环境下真的能写。

    ⚠ **这不是把安全性关掉。** ①危险命令黑名单与②路径沙箱翻不过去，
    allow 只影响③层。不给的话，F18「判 ASK 一律自动拒绝」会让每一次写入
    都被挡下——那样验到的是「拒绝路径」而不是「协作路径」，
    而两者的失败现象长得一模一样（队员报告说做完了，文件却没变）。
    """
    seeding.seed_permissions(
        Path(workspace) / ".rhinecode",
        allow=["Write(*)", "Edit(*)", "Read(*)"],
    )


_APP_PY = '''"""一个待改造的小模块。"""


def greet(name):
    return "hi " + name


def farewell(name):
    return "bye " + name
'''

_UTIL_PY = '''"""工具函数。"""


def shout(text):
    return text.upper()
'''


def seed_team(workspace: Path, user_dir: Path) -> None:
    """
    基础协作场景：两个角色 + 两个可改的源文件 + allow 规则。

    对应判据：共享任务清单（认领/依赖）、点对点消息、唤醒续跑。

    :param workspace: 宿主的临时工作区（项目根）
    :param user_dir: 宿主的临时用户级目录
    副作用：写入角色定义、权限规则与两个源文件。
    """
    workspace = Path(workspace)
    seeding.seed_files(
        workspace,
        {"src/app.py": _APP_PY, "src/util.py": _UTIL_PY},
    )
    _seed_roles(workspace)
    _seed_allow(workspace)


def seed_team_readonly(workspace: Path, user_dir: Path) -> None:
    """
    **不给 allow 规则**的同一场景——用来验 F18「判 ASK 一律自动拒绝」。

    与 `seed_team` 的差别只有一处（没有 `_seed_allow`），这是刻意的：
    两个场景的其余部分逐字相同，跑出来的差异才能归因到权限档上。

    预期现象：队员照常认领任务、照常汇报，但**写入全部被拒**，
    文件内容一个字节都不变。这正是 CLAUDE.md 里
    「缺省配置下子 Agent 实际只能做只读的事」那条的可复核形态。
    """
    workspace = Path(workspace)
    seeding.seed_files(
        workspace,
        {"src/app.py": _APP_PY, "src/util.py": _UTIL_PY},
    )
    _seed_roles(workspace)
