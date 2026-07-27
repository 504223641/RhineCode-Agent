"""
用 P1a 驱动设施验收 **C11（Skill 系统）** 端到端场景的预置。

对应 `docs/c11/checklist.md` 第十一节的场景 1 / 3 / 4。与 `p0_scenarios.py` 的
关系是「同一套设施、另一份 checklist」——放独立模块的理由也一样：混在一起会让
「这个 fixture 是给谁用的」说不清。

## ⚠️ 与 p0_scenarios.py 的一处本质差别：本模块**没有剧本，只有预置**

C11 这三条场景的判据是「**真实模型是否按 SOP 行事**」。脚本化假模型按轮次照本
宣科，它「按 SOP 行事」只能证明剧本是这么写的，什么也验不了。所以这三条必须用
`--mode live` 跑，本模块只提供 `--seed` 用的预置函数。

也正因为判据落在模型行为上，**这些场景不可以变成自动化回归测试**：模型行为不确定，
今天绿明天红的测试最终一定会被 skip 掉，那时它既不报警也没人再看。只验一次、
留证据（见 `docs/c11/acceptance-c11-live.md`）。

## 预置 Skill 的硬约束

`allowed_tools` 不得写 `mcp_add_server` / `mcp_resolve_server`——宿主会摘掉它们
（`host.EXCLUDED_TOOLS`），写了会在运行期交集时被剔空、静默降级为「不收窄」。
"""

from __future__ import annotations

from pathlib import Path

from tests.e2e import seeding


# ---------------------------------------------------------------------------
# 场景 1 / 3 共用：一个「有真实改动的 git 仓库」
# ---------------------------------------------------------------------------
# 两条场景验的都是内置 `commit` 样板的 SOP 是否被遵守，而那份 SOP 明确要求
# 「`git status` 看变动 → `git diff` 看具体改动 → `git log --oneline -15` 学
# 本仓库的提交风格 → 照抄既有风格写信息 → 提交」。要判「有没有照抄风格」，
# 预置的历史就必须有一种**一眼可辨、且模型不会自发选用**的风格：
#
#   - 中文正文（模型默认更可能写英文标题）
#   - `类型(范围): 描述` 的 conventional 前缀 + **中文范围名**
#
# 这样一来，「模型是否读了 git log」就有了可证伪的判据：它写出的标题若同时具备
# 这两个特征，几乎不可能是巧合。

# 仓库既有的提交历史（风格样本）
_COMMIT_HISTORY = [
    {
        "message": "chore(初始化): 搭起服务骨架",
        "files": {
            # `.rhinecode/` 必须一开始就忽略掉：应用运行时会在工作区里写
            # sessions / traces，不忽略的话 `git status` 里全是它们，
            # 既是噪音、也可能诱导模型 `git add .` 把记录一起提交。
            ".gitignore": ".rhinecode/\n__pycache__/\n",
            "README.md": "# 演示服务\n\n一个用于端到端验收的最小服务。\n",
            "app/__init__.py": "",
            "app/config.py": (
                '"""服务配置。"""\n\n'
                "# 登录会话的超时时间（秒）\n"
                "LOGIN_TIMEOUT = 30\n\n"
                "# 数据库连接池大小\n"
                "DB_POOL_SIZE = 5\n"
            ),
        },
    },
    {
        "message": "feat(登录): 增加账号密码登录接口",
        "files": {
            "app/auth.py": (
                '"""登录相关逻辑。"""\n\n'
                "from app.config import LOGIN_TIMEOUT\n\n\n"
                "def login(username: str, password: str) -> bool:\n"
                '    """校验账号密码，成功返回 True。"""\n'
                "    if not username or not password:\n"
                "        return False\n"
                "    return True\n\n\n"
                "def session_ttl() -> int:\n"
                '    """返回登录会话的存活时长（秒）。"""\n'
                "    return LOGIN_TIMEOUT\n"
            ),
        },
    },
    {
        "message": "fix(连接池): 修正空闲连接未被回收的问题",
        "files": {
            "app/db.py": (
                '"""数据库连接池。"""\n\n'
                "from app.config import DB_POOL_SIZE\n\n\n"
                "def pool_size() -> int:\n"
                "    return DB_POOL_SIZE\n\n\n"
                "def recycle_idle(connections: list) -> list:\n"
                '    """回收空闲连接，返回仍在用的那些。"""\n'
                "    return [c for c in connections if c]\n"
            ),
        },
    },
    {
        "message": "docs(说明): 补充本地启动步骤",
        "files": {
            "README.md": (
                "# 演示服务\n\n一个用于端到端验收的最小服务。\n\n"
                "## 本地启动\n\n1. 安装依赖\n2. 运行 `python -m app`\n"
            ),
        },
    },
]


def seed_commit_repo(workspace: Path, user_dir: Path) -> None:
    """
    预置一个含真实未提交改动的 git 仓库（checklist 场景 1 与场景 3 共用）。

    落盘顺序：先造 4 条提交历史（风格样本），**再**在其上写出未提交改动——
    顺序不可颠倒，颠倒了改动会被最后一条提交吃掉，`git status` 就是干净的，
    整条场景无从谈起。

    未提交改动刻意造成**两种形态**，好让「模型是否真的读了 diff」可判：

    - 已跟踪文件的修改：`app/config.py` 的 `LOGIN_TIMEOUT` 30 → 120，
      `app/auth.py` 增加一个「超时后自动续期」的分支；
    - 未跟踪的新文件：`app/session.py`。

    副作用：在 workspace 下创建 `.git/` 与若干源文件。
    :raises seeding.GitUnavailableError: 本机没有 git
    """
    seeding.seed_git_repo(workspace, _COMMIT_HISTORY)

    # ↓↓↓ 提交历史之后的「工作区现有改动」——这正是 /commit 要提交的东西
    seeding.seed_files(
        workspace,
        {
            # ① 修改已跟踪文件：把登录超时从 30 秒放宽到 120 秒
            "app/config.py": (
                '"""服务配置。"""\n\n'
                "# 登录会话的超时时间（秒）\n"
                "# 30 秒过短，弱网下用户刚填完表单就被踢掉，放宽到 120 秒\n"
                "LOGIN_TIMEOUT = 120\n\n"
                "# 会话到期前多久允许自动续期（秒）\n"
                "LOGIN_RENEW_WINDOW = 20\n\n"
                "# 数据库连接池大小\n"
                "DB_POOL_SIZE = 5\n"
            ),
            # ② 修改已跟踪文件：登录逻辑里增加续期分支
            "app/auth.py": (
                '"""登录相关逻辑。"""\n\n'
                "from app.config import LOGIN_RENEW_WINDOW, LOGIN_TIMEOUT\n\n\n"
                "def login(username: str, password: str) -> bool:\n"
                '    """校验账号密码，成功返回 True。"""\n'
                "    if not username or not password:\n"
                "        return False\n"
                "    return True\n\n\n"
                "def session_ttl() -> int:\n"
                '    """返回登录会话的存活时长（秒）。"""\n'
                "    return LOGIN_TIMEOUT\n\n\n"
                "def should_renew(remaining: int) -> bool:\n"
                '    """剩余时长进入续期窗口时，允许自动续期。"""\n'
                "    return 0 < remaining <= LOGIN_RENEW_WINDOW\n"
            ),
            # ③ 未跟踪的新文件
            "app/session.py": (
                '"""登录会话的存续管理。"""\n\n'
                "from app.auth import session_ttl, should_renew\n\n\n"
                "class Session:\n"
                '    """一个登录会话。"""\n\n'
                "    def __init__(self, user: str) -> None:\n"
                "        self.user = user\n"
                "        self.remaining = session_ttl()\n\n"
                "    def tick(self, seconds: int) -> None:\n"
                '        """走过 seconds 秒；进入续期窗口则自动续满。"""\n'
                "        self.remaining -= seconds\n"
                "        if should_renew(self.remaining):\n"
                "            self.remaining = session_ttl()\n"
            ),
        },
    )


# ---------------------------------------------------------------------------
# 场景 4：白名单收窄可观测
# ---------------------------------------------------------------------------
# 需要两个 Skill 形成对照：
#
#   - `audit`：声明窄白名单（只读两件），用来验「收窄真的发生了」；
#   - `freeform`：**不声明** `allowed_tools`，用来验「任一 Skill 未声明则整体
#     塌缩为不收窄」——这是 C11 的既定语义，也是这条场景后半段的判据。
#
# ⚠️ `audit` 的白名单里 `glob_files` 是**必须**的，不是顺手加的：
# CLAUDE.md 记着的实测教训——只给 `read_file` 的话模型没有任何办法发现目录里
# 有哪些文件，会退化成盲猜文件名，白烧好几轮。那样这条场景验到的就不是
# 「收窄可观测」而是「收窄过度有多糟」了。


def seed_whitelist_pair(workspace: Path, user_dir: Path) -> None:
    """
    预置场景 4 的一对 Skill 与一个可供审阅的小项目。

    `audit` 放**项目级**、`freeform` 放**用户级**，顺带覆盖两个存放层级。
    副作用：写 `.rhinecode/skills/audit.md`、`<user_dir>/skills/freeform.md`
    与几个源文件。
    """
    seeding.seed_files(
        workspace,
        {
            "app/__init__.py": "",
            "app/config.py": (
                '"""服务配置。"""\n\n'
                "LOGIN_TIMEOUT = 30\n"
                "DB_POOL_SIZE = 5\n"
                "DEBUG = True\n"
            ),
            "app/auth.py": (
                '"""登录相关逻辑。"""\n\n'
                "from app.config import LOGIN_TIMEOUT\n\n\n"
                "def login(username, password):\n"
                "    # 注意：这里没有做任何密码强度校验\n"
                "    return bool(username) and bool(password)\n\n\n"
                "def session_ttl():\n"
                "    return LOGIN_TIMEOUT\n"
            ),
            "notes.md": "# 待办\n\n- 补充登录失败次数限制\n",
        },
    )

    # ① 窄白名单：只读两件
    seeding.seed_project_skill(
        workspace,
        "audit",
        {
            "description": "只读审阅代码，产出审阅意见但不改任何文件",
            "mode": "shared",
            "allowed_tools": ["read_file", "glob_files"],
        },
        "以只读方式审阅代码，把发现写成一段意见。\n\n"
        "**本流程不修改任何文件**：即使发现问题，也只在回复里说明，"
        "不要动手改，也不要把意见写进文件。\n\n"
        "$ARGUMENTS",
    )

    # ② 不声明白名单：用来验「整体塌缩为不收窄」
    seeding.seed_user_skill(
        user_dir,
        "freeform",
        {
            "description": "不限定工具的自由协助",
            "mode": "shared",
        },
        "按用户要求自由地完成任务，可以使用任何可用工具。\n\n$ARGUMENTS",
    )
