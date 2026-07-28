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
留证据（见 `docs/c11/acceptance/skills-c11-live.md`）。

## 预置 Skill 的硬约束

`allowed-tools` 现在是**预授权**（本次执行内免确认），不是工具收窄。
写 `mcp_add_server` / `mcp_resolve_server` 没有意义——宿主会摘掉这两个工具
（`host.EXCLUDED_TOOLS`），针对它们的预授权规则永远命不中。
"""

from __future__ import annotations

import sys
from pathlib import Path

import yaml

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
# 场景 6：启动 fail-fast（白名单里的内置工具名拼错）
# ---------------------------------------------------------------------------
# 这条场景**一次模型调用都不会发生**：装配在 Provider 之前就终止了。
# 它同时要验的第二件事是「失败退出时没有留下孤儿子进程」——所以必须同场配一个
# stdio MCP Server，好让「若校验窗口挪到 connect_all 之后」这个错误变得可观测。
#
# 拼错的名字取 `read_files`（真名是 `read_file`，多一个 s）：这是最容易犯、
# 也最容易被眼睛滑过去的那种笔误，正是 fail-fast 想拦的形态。
TYPO_TOOL_NAME = "read_files"


def _write_mcp_stdio(workspace: Path) -> None:
    """
    在项目级写一份 `mcp.yaml`，声明一个 stdio MCP Server。

    用仓库自带的 `tests/fixtures/mock_mcp_server.py` 当 Server——它是本地脚本，
    不联网、不装包，起停都在毫秒级。用 `sys.executable` 而不是裸 `python`：
    宿主可能跑在虚拟环境里，裸名字未必指向同一个解释器。
    """
    server_script = Path(__file__).resolve().parents[1] / "fixtures" / "mock_mcp_server.py"
    payload = {
        "mcpServers": {
            "mock": {"command": sys.executable, "args": [str(server_script)]},
        }
    }
    target = workspace / ".rhinecode" / "mcp.yaml"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        yaml.safe_dump(payload, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )


def seed_typo_whitelist(workspace: Path, user_dir: Path) -> None:
    """
    预置一个白名单里含笔误工具名的项目级 Skill + 一个 stdio MCP Server（场景 6）。

    期望结果：装配抛 `BootstrapError`，宿主进 fatal 态并把成文文案经通道回报；
    且**没有任何 mock_mcp_server.py 子进程被拉起**。
    """
    seeding.seed_files(workspace, {"note.txt": "一行内容\n"})
    seeding.seed_project_skill(
        workspace,
        "broken",
        {
            "description": "白名单里有个拼错的工具名",
                        "allowed-tools": [TYPO_TOOL_NAME, "glob_files"],
        },
        "随便做点什么。\n\n$ARGUMENTS",
    )
    _write_mcp_stdio(workspace)


def seed_fixed_whitelist(workspace: Path, user_dir: Path) -> None:
    """
    与 `seed_typo_whitelist` **只差一个字母**的对照组：把 `read_files` 改对成
    `read_file`，其余（含那个 stdio MCP Server）一模一样。

    这是场景 6 的「改对后正常启动」那一半。两个预置刻意做成孪生，
    是为了让「启动失败」与「启动成功」之间的唯一变量就是那个笔误。
    """
    seeding.seed_files(workspace, {"note.txt": "一行内容\n"})
    seeding.seed_project_skill(
        workspace,
        "broken",
        {
            "description": "白名单里有个拼错的工具名",
                        "allowed-tools": ["read_file", "glob_files"],
        },
        "随便做点什么。\n\n$ARGUMENTS",
    )
    _write_mcp_stdio(workspace)


# ---------------------------------------------------------------------------
# 场景 7 / 8：第三方内容可见性 与 短命令被内置占用
# ---------------------------------------------------------------------------
# 两条场景共用一份预置，因为它们要的东西恰好叠得起来：
#
#   - 场景 7 要「一个别人的仓库里带着项目级 Skill」→ 任意项目级 Skill 都行；
#   - 场景 8 要「一个名字与内置命令重名的 Skill」→ `name: context`。
#
# 而 `context` 本身就是项目级的，所以它同时充当场景 7 的「第三方内容」。
# 再加一个正常名字的 `houserules`，好让「发现 N 个项目级 Skill」的 N 不是 1
# ——N=1 时数字对不对看不出来。


def seed_thirdparty_repo(workspace: Path, user_dir: Path) -> None:
    """
    预置一个「别人的仓库」：含两个项目级 Skill，其中一个的名字与内置命令 `/context` 重名。

    期望结果：启动时出现「发现 2 个项目级 Skill」提示 + 一条 `/context` 重名警告；
    `/context` 仍是上下文报告；`/skills` 里两个都在；`/skills run context` 能执行。
    """
    seeding.seed_files(
        workspace,
        {
            "README.md": "# 某个别人的仓库\n\n你刚 clone 下来。\n",
            "main.py": "def main():\n    print('hello')\n",
        },
    )
    # 与内置命令 `/context` 重名——短命令应被跳过注册，内置行为不受影响
    seeding.seed_project_skill(
        workspace,
        "context",
        {"description": "名字故意与内置命令重名", },
        "请只回复一行：`重名 Skill 已执行`。不要调用任何工具。\n\n$ARGUMENTS",
    )
    seeding.seed_project_skill(
        workspace,
        "houserules",
        {"description": "本仓库的编码约定", },
        "按本仓库约定作答。\n\n$ARGUMENTS",
    )


# ---------------------------------------------------------------------------
# 场景 5：热更新
# ---------------------------------------------------------------------------
# 热更新的三步（新增 / 改正文 / 删除）要在**运行中**做，所以预置只负责给出起点，
# 三步文件操作由驱动方在宿主活着的时候直接写工作区。
#
# 「改正文后行为随之改变」这一步需要一个**一眼可判**的行为差异，而不是让人去品
# 「回答风格好像变了」。所以正文里放一条极其机械的指令：回复必须以某个标记开头。
# 标记从 `【甲】` 换成 `【乙】`，模型跟没跟上一目了然。
HOTRELOAD_MARK_A = "【甲】"
HOTRELOAD_MARK_B = "【乙】"


def hotreload_body(mark: str) -> str:
    """生成热更新用的 Skill 正文；`mark` 是要求模型加在回复最前面的标记。"""
    return (
        f"回复时**必须**以 `{mark}` 这四个字符开头，然后再写正文。\n"
        "不要调用任何工具，直接回答。\n\n$ARGUMENTS"
    )


def seed_hotreload_base(workspace: Path, user_dir: Path) -> None:
    """
    场景 5 的起点：一个普通小项目，**不预置任何自定义 Skill**。

    第一步「运行中新建 Skill 文件」才有意义——如果文件一开始就在，
    验的就成了「启动时能扫到」，那是场景 1 已经覆盖过的事。
    """
    seeding.seed_files(
        workspace,
        {
            "app/__init__.py": "",
            "app/main.py": "def main():\n    print('hello')\n",
            "README.md": "# 热更新演示项目\n",
        },
    )


# ---------------------------------------------------------------------------
# 场景 10：Plan Mode 与独立模式的交互
# ---------------------------------------------------------------------------
# ⚠️ **不能用内置的 `review` 跑这条**（实测踩过）：`review` 是纯分析任务，
# 模型看完 diff 直接给结论就完事了，**根本没有「计划」可提**，于是审批面板一次
# 都不弹，整条场景无从验起。这不是产品的问题，是场景设计的问题。
#
# 所以要一个**必须动手改代码**的独立模式 Skill：只有当任务确实有副作用时，
# Plan Mode 的「先规划、再批准、后执行」才有意义，模型也才会去调 `present_plan`。
def seed_plan_skill(workspace: Path, user_dir: Path) -> None:
    """
    预置一个「要改代码」的独立模式 Skill（场景 10），外加一个可改的小项目。

    正文明确要求先出计划再动手，是为了让「模型没提计划」与「产品没弹面板」
    这两种失败可区分——正文都这么写了还没提计划，那才轮得到怀疑产品。
    """
    seeding.seed_files(
        workspace,
        {
            "app/__init__.py": "",
            "app/session.py": (
                '"""登录会话。"""\n\n\n'
                "class Session:\n"
                '    """一个登录会话。"""\n\n'
                "    def __init__(self, user: str) -> None:\n"
                "        self.user = user\n"
                "        self.remaining = 30\n"
            ),
            "README.md": "# 计划模式演示项目\n",
        },
    )
    seeding.seed_project_skill(
        workspace,
        "fixit",
        {
            "description": "按要求改代码（独立模式）",
            "context": "fork",
            "allowed-tools": ["read_file", "glob_files", "grep_content", "edit_file", "write_file"],
        },
        "你要按用户的要求**修改代码**。\n\n"
        "先读懂相关文件，然后**给出完整的改动计划并等待批准**，"
        "获批之后再动手改。计划里要写清改哪个文件、加什么、为什么。\n\n"
        "$ARGUMENTS",
    )


# ---------------------------------------------------------------------------
# 探索性验收：用一份**不是我们写的** Skill
# ---------------------------------------------------------------------------
# 上一轮验收（场景 1–11）有一个结构性盲区：**每个被测 Skill 都是验收者自己写的**，
# 而验收者知道契约（description 要短、要放 $ARGUMENTS、白名单该收多窄、
# 动作型与指导型要分开），于是写出来的 Skill 天然是适配好的。
# 43 项判据全过，却一条也碰不到「Skill 没适配好会怎样」。
#
# 这个预置刻意反过来：**原样搬一份外部 Skill 进来，一个字不改**。
# 它的形态与我们的样板完全不同（指导型、英文长 description、无 $ARGUMENTS），
# 正是真实用户会遇到的那种。
FOREIGN_SKILL_ENV = "RHINE_E2E_FOREIGN_SKILL"
# 缺省指向本机那份**真实的 Claude Code Skill**。
#
# 为什么用它而不是自己写一份：本预置的全部意义就是「用一份不是我们写的 Skill」——
# 我知道契约，我写的样本必然照着契约写，验不出「外部作者会怎么写」。
# 这份是 Claude Code 自己装的、英文、目录型、frontmatter 只有 name + description，
# 恰好是标准里最常见的形态。
#
# 早先指向的 `G:\Rhine-test\c11-p1a\...\frontend-design` 是一次手测留下的目录，
# 已随那次临时工作区一并删除。要换成别的，用 RHINE_E2E_FOREIGN_SKILL 指路径。
_DEFAULT_FOREIGN_SKILL = Path(
    r"C:\Users\Administrator\.claude\skills\context7-mcp\SKILL.md"
)


def seed_foreign_skill(workspace: Path, user_dir: Path) -> None:
    """
    原样搬入一份外部 Skill（目录型，项目级），**不做任何适配**。

    源路径取自环境变量 `RHINE_E2E_FOREIGN_SKILL`，缺省是本机那份 frontend-design。
    找不到就**明确抛错**——静默跳过会让这次验收变成「又测了一遍我们自己写的 Skill」，
    也就是它本来要避开的那个盲区。

    :raises FileNotFoundError: 源文件不存在
    """
    import os

    src = Path(os.environ.get(FOREIGN_SKILL_ENV) or _DEFAULT_FOREIGN_SKILL)
    if not src.is_file():
        raise FileNotFoundError(
            f"找不到外部 Skill 源文件：{src}\n"
            f"本预置的全部意义就是「用一份不是我们写的 Skill」，"
            f"找不到就没有意义了，故明确报错而不是退回内置样板。"
            f"可用环境变量 {FOREIGN_SKILL_ENV} 指定其它路径。"
        )

    target = workspace / ".rhinecode" / "skills" / src.parent.name / "SKILL.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(src.read_text(encoding="utf-8"), encoding="utf-8")

    # 一个空项目：让模型从零开始，避免既有代码干扰观察
    seeding.seed_files(workspace, {"README.md": "# 我的小项目\n\n还什么都没有。\n"})


# ---------------------------------------------------------------------------
# 场景 11：目录型 Skill 能力包
# ---------------------------------------------------------------------------
# 这条场景一箭双雕：
#   ① 目录型 Skill 的资源清单与绝对路径确实注入了；
#   ② **工作区外目录的只读放行确实生效**——用户级 Skill 目录不在项目内，
#      沙箱若没放行，模型按清单去读那份参考文档就会被拒。
#
# 判据要可证伪，所以参考文档里写的规则必须是模型**不可能猜到**的：
# 这里用一个随口编的内部命名约定（`rc_` 前缀 + 全大写 + `_V2` 后缀）。
# 模型答对了，就只可能是真读到了那份文档。
HOUSE_RULE_MARKER = "rc_"
HOUSE_RULE_SUFFIX = "_V2"


def seed_capability_pack(workspace: Path, user_dir: Path) -> None:
    """
    在**用户级**目录放一个目录型 Skill 能力包（场景 11）。

    目录结构::

        <user_dir>/skills/naming/
            SKILL.md          ← 入口（frontmatter + SOP）
            reference.md      ← 参考文档（规则的唯一出处）
            template.py       ← 模板文件（只为让随附清单不止一条）

    注意入口文件名必须是 `SKILL.md`（大写），这是目录型的识别标志。
    """
    seeding.seed_files(workspace, {"app/__init__.py": "", "app/main.py": "def main():\n    pass\n"})

    pack = Path(user_dir) / "skills" / "naming"
    pack.mkdir(parents=True, exist_ok=True)

    # 入口：正文刻意**不写规则本身**，只说「去读 reference.md」。
    # 规则若写在正文里，模型不读文档也能答对，这条场景就白验了。
    (pack / "SKILL.md").write_text(
        "---\n"
        "name: naming\n"
        "description: 按本组内部命名约定给出标识符名字\n"
        "mode: shared\n"
        "allowed-tools: [read_file, glob_files]\n"
        "---\n\n"
        "本组的命名约定写在随附的 `reference.md` 里，**你必须先把它读完**再作答。\n"
        "约定的细节不在本文件中，凭印象作答一定是错的。\n\n"
        "读完后，按其中的规则给出用户要的名字，并注明你依据的是哪一条。\n\n"
        "$ARGUMENTS\n",
        encoding="utf-8",
    )

    # 参考文档：规则的唯一出处，内容刻意反常识
    (pack / "reference.md").write_text(
        "# 内部命名约定（v2）\n\n"
        "本组所有**模块级常量**一律按下面三条命名，三条缺一不可：\n\n"
        f"1. 以 `{HOUSE_RULE_MARKER}` 前缀开头（小写，含下划线）；\n"
        "2. 前缀之后的主体部分全部**大写**，词与词之间用下划线分隔；\n"
        f"3. 以 `{HOUSE_RULE_SUFFIX}` 结尾。\n\n"
        f"例：表示重试次数的常量应写作 `{HOUSE_RULE_MARKER}RETRY_COUNT{HOUSE_RULE_SUFFIX}`。\n\n"
        "> 这三条是 v2 版约定，与 v1（全大写、无前后缀）不兼容，不要混用。\n",
        encoding="utf-8",
    )

    # 模板文件：只为让随附资源清单不止一条，验证清单确实枚举了整个目录
    (pack / "template.py").write_text(
        '"""模块级常量模板。"""\n\n'
        f"{HOUSE_RULE_MARKER}EXAMPLE_NAME{HOUSE_RULE_SUFFIX} = 0\n",
        encoding="utf-8",
    )
