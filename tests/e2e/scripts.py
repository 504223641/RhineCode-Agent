"""
现成的剧本与预置函数，供宿主的 `--script` / `--seed` 直接引用。

    python -m tests.e2e.host --mode scripted --script tests.e2e.scripts:CONFIRM_THEN_DONE

放在这里而不是散在各测试文件里，是为了让**手工驱动**也能直接用——
手测时不该为了起一个宿主先去写一个 Python 模块。

⚠️ `allowed-tools` 现在是**预授权**（本次执行内免确认），不是工具收窄。
写 `mcp_add_server` / `mcp_resolve_server` 没有意义——宿主会摘掉这两个工具。
"""

from __future__ import annotations

from pathlib import Path

from tests.e2e import seeding
from tests.e2e.scripted import done, stream_error, text, thinking, tool, tool_pending


# 一轮就结束：最简单的冒烟剧本
SAY_HELLO = [
    [text("你好，我是 RhineCode。"), done()],
]

# 会触发一次工具确认面板（write_file 非只读，默认模式下无规则命中 → 问用户）
CONFIRM_THEN_DONE = [
    [text("我来写个文件。"), tool("write_file", {"path": "x.txt", "content": "hi"}), done()],
    [text("写完了。"), done()],
]

# 带「参数生成中」播报的写文件：真实 Provider 在拿到工具名的第一时间就播报，
# 界面据此先挂一行状态，等确认通过后**同一行**转成执行态。
# 用来复现「模型在生成一份长文件内容时界面完全静止」那个缺口的修复效果。
PENDING_THEN_WRITE = [
    [
        text("我来写个文件。"),
        tool_pending("write_file", "e2e_pending_1"),
        tool("write_file", {"path": "x.txt", "content": "hi"}, call_id="e2e_pending_1"),
        done(),
    ],
    [text("写完了。"), done()],
]

# 正文 + 思考 + 只读工具：观察面（ui_message / tool_execute）都能拿到东西
THINK_AND_READ = [
    [
        thinking("先看看有哪些文件。"),
        text("我读一下 seed.txt。"),
        tool("read_file", {"path": "seed.txt"}),
        done(),
    ],
    [text("读完了，里面是一行中文。"), done()],
]


# 四次同类工具调用，供「四档确认各走一次」使用
FOUR_WRITES = [
    [text("第一个。"), tool("write_file", {"path": "a.txt", "content": "1"}), done()],
    [text("第二个。"), tool("write_file", {"path": "b.txt", "content": "2"}), done()],
    [text("第三个。"), tool("write_file", {"path": "c.txt", "content": "3"}), done()],
    [text("第四个。"), tool("write_file", {"path": "d.txt", "content": "4"}), done()],
    [text("都处理完了。"), done()],
]

# 一条危险命令：应当在第①层黑名单被拦下，压根走不到人在回路那一层
DANGEROUS_COMMAND = [
    [text("我来清理一下。"), tool("run_command", {"command": "rm -rf /"}), done()],
    [text("被拦下了，我换个做法。"), done()],
]

# 只读版本控制命令：验证预置的 git 历史确实可见
GIT_LOG = [
    [text("我看看提交历史。"), tool("run_command", {"command": "git log --oneline"}), done()],
    [text("历史读到了。"), done()],
]

# 第 1 轮激活 Skill、第 2 轮出结论：验「第 N 轮激活、第 N+1 轮生效」
ACTIVATE_SKILL = [
    [text("我先加载审阅流程。"), tool("load_skill", {"name": "review"}), done()],
    [text("按流程审阅完毕。"), done()],
]

# 独立模式 Skill（声明了自定义 model:）：验换模型旁路仍走假模型
ISOLATED_SKILL = [
    [text("子对话里的第一句。"), done()],
    [text("子对话的结论。"), done()],
    [text("主对话收到结论。"), done()],
]

# 流错误：循环应以「流错误」停止
STREAM_ERROR = [
    [text("我说到一半……"), stream_error("连接中断"), done()],
]

# 中文 + 字面方括号：全链路编码验证
CHINESE_MARKUP = [
    [
        text("我读一下那个文件。"),
        tool("write_file", {"path": "中文标题.md", "content": "# 中文标题\n\n带 [方括号] 的一行\n"}),
        done(),
    ],
    [text("写完了，里面有 [方括号]。"), done()],
]


def seed_typo_skill(workspace: Path, user_dir: Path) -> None:
    """
    预置一个**白名单笔误**的 Skill：触发启动期 fail-fast（AC5）。

    `no_such_tool` 不是 `mcp__` 前缀，所以走第一段的严格校验、直接终止启动。
    """
    seeding.seed_project_skill(
        workspace,
        "broken",
        {"description": "白名单写错了", "allowed-tools": ["read_file", "no_such_tool"]},
        "这个 Skill 起不来。",
    )


def seed_with_git(workspace: Path, user_dir: Path) -> None:
    """基础预置 + 一段真实的 git 提交历史（AC23）。"""
    seed_basic(workspace, user_dir)
    seeding.seed_git_repo(
        workspace,
        [
            {"message": "初始提交", "files": {"README.md": "# 演示项目\n"}},
            {"message": "修复登录超时", "files": {"auth.py": "TIMEOUT = 30\n"}},
        ],
    )


def seed_builtin_agents(workspace: Path, user_dir: Path) -> None:
    """
    验收三个内置角色的预置（c13）。

    造一个**够分量**的小项目：光靠一次全局搜索答不上来，必须真的读几个文件、
    把调用链串起来才行。这是三个角色能拉开差距的前提——项目太简单的话，
    explorer 和 planner 会给出几乎一样的答案，什么也验不出来。

    重试机制刻意做成「散在四处、写法各不相同、还带一处坏味道」：

    - `src/config.py`   —— 常量定义（`RETRY_LIMIT` / `RETRY_DELAY`）
    - `src/http_client.py` —— 固定间隔重试
    - `src/job_runner.py`  —— 倒计数重试，**自己又硬编码了一个 3**（坏味道）
    - `src/uploader.py`    —— 重试次数乘 2（另一处坏味道）
    - `tests/test_retry.py` —— 空壳测试，注释写着「改了要跟着改」
    - `docs/design.md`     —— 文档里写死了「最多重试三次」（改代码不改它就会不一致）

    于是「把重试改成指数退避」这个问题有真实的层次：要动哪几处、顺序如何、
    哪些是连带影响（文档、测试）、哪里有坑（两处硬编码）。
    """
    seeding.seed_files(
        workspace,
        {
            "src/config.py": (
                '"""全局配置。"""\n'
                "\n"
                "RETRY_LIMIT = 3\n"
                "RETRY_DELAY = 1.0\n"
                "TIMEOUT_SECONDS = 30\n"
                "POOL_SIZE = 8\n"
            ),
            "src/http_client.py": (
                "import time\n"
                "\n"
                "from src.config import RETRY_DELAY, RETRY_LIMIT\n"
                "\n"
                "\n"
                "def fetch(url):\n"
                '    """固定间隔重试。"""\n'
                "    for attempt in range(RETRY_LIMIT):\n"
                "        try:\n"
                "            return _do_fetch(url)\n"
                "        except OSError:\n"
                "            time.sleep(RETRY_DELAY)\n"
                "    raise RuntimeError('fetch failed')\n"
                "\n"
                "\n"
                "def _do_fetch(url):\n"
                "    return url\n"
            ),
            "src/job_runner.py": (
                "import time\n"
                "\n"
                "from src.config import RETRY_DELAY\n"
                "\n"
                "\n"
                "def run_job(job):\n"
                '    """倒计数重试。注意这里没有用 RETRY_LIMIT，而是自己写死了 3。"""\n'
                "    remaining = 3\n"
                "    while remaining > 0:\n"
                "        if job():\n"
                "            return True\n"
                "        remaining -= 1\n"
                "        time.sleep(RETRY_DELAY)\n"
                "    return False\n"
            ),
            "src/uploader.py": (
                "from src.config import RETRY_LIMIT\n"
                "\n"
                "\n"
                "def upload(blob):\n"
                '    """上传比抓取更值得多试几次，所以这里乘了 2。"""\n'
                "    tries = RETRY_LIMIT * 2\n"
                "    for _ in range(tries):\n"
                "        if _put(blob):\n"
                "            return True\n"
                "    return False\n"
                "\n"
                "\n"
                "def _put(blob):\n"
                "    return True\n"
            ),
            "tests/test_retry.py": (
                "def test_retry_count():\n"
                "    # RETRY_LIMIT 改成别的值之后这条要跟着改\n"
                "    assert True\n"
            ),
            "docs/design.md": (
                "# 设计说明\n"
                "\n"
                "## 重试策略\n"
                "\n"
                "网络请求失败后**最多重试三次**，每次间隔 1 秒。\n"
                "上传任务比抓取更重要，重试次数翻倍。\n"
            ),
            "README.md": "# 演示项目\n\n一个带重试机制的小服务。\n",
        },
    )
    seeding.seed_rhine_md(workspace, "# 本项目\n\n用中文回答。\n")


def seed_subagents(workspace: Path, user_dir: Path) -> None:
    """
    C13 真实模型验收的预置（子 Agent 系统）。

    放了三样东西，各对应一条待验判据：

    1. **一个够真实的代码库**——同一个符号 `RETRY_LIMIT` 散落在三个文件里。
       这是「该委派而不该自己硬翻」的典型任务：要看清全貌得读好几个文件，
       而那些内容对主对话毫无价值。用于验 AC6b / 场景 7（模型会不会**主动**委派）
       与场景 8（结论是否自包含、可直接用）。
    2. **一个项目级角色 `auditor`**——用于验 AC4（项目级角色每次启动都提示）。
       它刻意**只读**，好让缺省档下也能真的跑起来。
    3. **不预置任何 allow 规则**——于是场景 3（权限边界）成立：
       委派一个要写文件的任务时，写入会在非交互环境下被自动拒绝，
       观察子 Agent 是就此收敛并在结论里说明，还是反复重试。
    """
    seeding.seed_files(
        workspace,
        {
            "src/config.py": "# 全局配置\nRETRY_LIMIT = 3\nTIMEOUT_SECONDS = 30\n",
            "src/client.py": (
                "from src.config import RETRY_LIMIT\n"
                "\n"
                "def fetch(url):\n"
                "    for attempt in range(RETRY_LIMIT):\n"
                "        pass\n"
            ),
            "src/worker.py": (
                "from src.config import RETRY_LIMIT\n"
                "\n"
                "def run_job(job):\n"
                "    remaining = RETRY_LIMIT\n"
                "    while remaining > 0:\n"
                "        remaining -= 1\n"
            ),
            "tests/test_client.py": (
                "def test_retry():\n"
                "    # RETRY_LIMIT 改成 5 之后这条要跟着改\n"
                "    assert True\n"
            ),
            "README.md": "# 演示项目\n\n一个用来验证子 Agent 委派的小项目。\n",
        },
    )
    seeding.seed_rhine_md(workspace, "# 本项目\n\n用中文回答。\n")
    seeding.seed_project_agent(
        workspace,
        "auditor",
        {
            "description": (
                "需要检查代码里是否存在硬编码常量、重复定义、缺少测试覆盖这类问题时用它。"
                "只读，不会修改任何东西。"
            ),
            "tools": "read_file, glob_files, grep_content",
            "permission_mode": "strict",
            "max_turns": 10,
        },
        "你是代码审查员。只读地检查问题，最后一段给出自包含的结论，"
        "写清每个问题的文件路径与具体位置。不要尝试修改任何文件。",
    )


def seed_isolated_skill(workspace: Path, user_dir: Path) -> None:
    """
    预置一个**独立模式**且**指定了模型**的 Skill（AC30）。

    `model:` 是关键——它会走协调层的 `_provider_for` 换模型旁路，
    那条路径不透传 `provider_factory` 时会静默连上真实网络。
    """
    seeding.seed_project_skill(
        workspace,
        "solo",
        {
            "description": "独立模式跑一遍",
            "context": "fork",
            "model": "deepseek-reasoner",
            "allowed-tools": ["read_file", "glob_files"],
        },
        "在子对话里完成分析并给出结论。\n\n$ARGUMENTS",
    )


def seed_chinese(workspace: Path, user_dir: Path) -> None:
    """预置含中文与字面方括号的内容（AC43）。"""
    seeding.seed_files(
        workspace,
        {
            "笔记.md": "# 中文标题\n\n这一行有 [方括号] 和 `代码`。\n",
            "seed.txt": "一行中文内容\n",
        },
    )
    seeding.seed_rhine_md(workspace, "# 项目 [约定]\n\n用中文回答。\n")


def seed_basic(workspace: Path, user_dir: Path) -> None:
    """
    一份「像样的项目」：几个源文件 + 项目指令 + 一个项目级 Skill。

    宿主 `--seed tests.e2e.scripts:seed_basic` 即可使用。
    """
    seeding.seed_files(
        workspace,
        {
            "seed.txt": "一行中文内容\n",
            "src/app.py": "def main():\n    print('hi')\n",
            "src/util.py": "def helper():\n    return 1\n",
        },
    )
    seeding.seed_rhine_md(workspace, "# 本项目\n\n用中文回答，改动前先跑测试。\n")
    seeding.seed_project_skill(
        workspace,
        "review",
        {
            "description": "审阅改动并给出结论",
            # 只读白名单里 read_file 必须配上 glob_files，否则模型没法发现目录里有什么
            # （CLAUDE.md 里记着的实测教训）
            "allowed-tools": ["read_file", "glob_files"],
        },
        "第一步：列出改动。第二步：逐个读。第三步：给结论。\n\n$ARGUMENTS",
    )


# ---------------------------------------------------------------------- #
# Hook 系统（c12）
# ---------------------------------------------------------------------- #

# 一条会被 pre_tool_use Hook 拦下的命令；第 2 轮模型停下来说话。
HOOK_BLOCKED_PUSH = [
    [text("我来推上去。"), tool("run_command", {"command": "git push origin main"}), done()],
    [text("被规则拦下了，我先跟你确认。"), done()],
]

# 一次只读调用：用于验「Hook 把 ALLOW 升级为 ASK」——正常情况下它不该弹面板。
HOOK_ASK_READ = [
    [text("我读一下。"), tool("read_file", {"path": "seed.txt"}), done()],
    [text("读到了。"), done()],
]

# 一次写入：用于验 post_tool_use 的自动化动作（格式化/后处理）真的跑起来了。
HOOK_POST_WRITE = [
    [text("我写个文件。"), tool("write_file", {"path": "out.txt", "content": "hello"}), done()],
    [text("写完了。"), done()],
]


def _write_hooks(workspace: Path, body: str) -> None:
    """把一份项目级 hooks.yaml 写进工作区。"""
    path = workspace / ".rhinecode" / "hooks.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")


def _helper(workspace: Path, name: str, source: str) -> str:
    """
    把一段 Python 源码落成工作区里的脚本，返回可直接交给 shell 的命令串。

    **不在命令串里嵌 Python 代码**：那样在 cmd.exe 与 POSIX shell 上的引号转义
    规则不同，预置只会在一个平台上成立。
    """
    import sys

    path = workspace / name
    path.write_text(source, encoding="utf-8")
    return f'"{sys.executable}" "{path}"'


def seed_hook_block_push(workspace: Path, user_dir: Path) -> None:
    """预置一条拦住 `git push` 的 pre_tool_use Hook（checklist 场景 1）。"""
    seed_basic(workspace, user_dir)
    cmd = _helper(
        workspace,
        "hook_block.py",
        "import sys\n"
        "sys.stderr.write('请走 PR，不要直接 push 到 main')\n"
        "sys.exit(2)\n",
    )
    _write_hooks(
        workspace,
        "hooks:\n"
        "  - name: 禁止直接 push\n"
        "    event: pre_tool_use\n"
        "    if:\n"
        "      all:\n"
        "        - tool: run_command\n"
        "        - command: \"git push *\"\n"
        "    action:\n"
        "      type: command\n"
        f"      command: '{cmd}'\n",
    )


def seed_hook_ask(workspace: Path, user_dir: Path) -> None:
    """预置一条把只读调用升级为「问用户」的 Hook（checklist 场景 2）。"""
    seed_basic(workspace, user_dir)
    cmd = _helper(
        workspace,
        "hook_ask.py",
        "import sys\n"
        'sys.stdout.write(\'{"decision": "ask", "reason": "这个文件比较敏感"}\')\n',
    )
    _write_hooks(
        workspace,
        "hooks:\n"
        "  - name: 读敏感文件要确认\n"
        "    event: pre_tool_use\n"
        "    if:\n"
        "      all:\n"
        "        - tool: read_file\n"
        "    action:\n"
        "      type: command\n"
        f"      command: '{cmd}'\n",
    )


def seed_hook_post_action(workspace: Path, user_dir: Path) -> None:
    """预置一条「写完文件就跑后处理」的 post_tool_use Hook（checklist 场景 3）。"""
    seed_basic(workspace, user_dir)
    cmd = _helper(
        workspace,
        "hook_post.py",
        "import json, pathlib, sys\n"
        "data = json.load(sys.stdin)\n"
        "pathlib.Path('hook_ran.txt').write_text(data['path'], encoding='utf-8')\n",
    )
    _write_hooks(
        workspace,
        "hooks:\n"
        "  - name: 写完就后处理\n"
        "    event: post_tool_use\n"
        "    if:\n"
        "      all:\n"
        "        - tool: write_file\n"
        "    action:\n"
        "      type: command\n"
        f"      command: '{cmd}'\n",
    )


def seed_hook_broken_pre(workspace: Path, user_dir: Path) -> None:
    """预置一条**跑不起来**的 pre_tool_use Hook：验 fail-closed（场景 5）。"""
    seed_basic(workspace, user_dir)
    _write_hooks(
        workspace,
        "hooks:\n"
        "  - name: 写坏的检查\n"
        "    event: pre_tool_use\n"
        "    action:\n"
        "      type: command\n"
        "      command: 'rhine_no_such_command_12345'\n",
    )


def seed_hook_broken_post(workspace: Path, user_dir: Path) -> None:
    """同一条坏 Hook 改挂 post_tool_use：验 fail-open（场景 6）。"""
    seed_basic(workspace, user_dir)
    _write_hooks(
        workspace,
        "hooks:\n"
        "  - name: 写坏的后处理\n"
        "    event: post_tool_use\n"
        "    action:\n"
        "      type: command\n"
        "      command: 'rhine_no_such_command_12345'\n",
    )


# ---------------------------------------------------------------------- #
# 真实模型场景（checklist 场景 9 / 10）——只在 live 模式下用
# ---------------------------------------------------------------------- #


def seed_live_block_push(workspace: Path, user_dir: Path) -> None:
    """
    场景 9：真实 git 仓库 + 一条拦住 `git push` 的 Hook。

    判的是**模型的反应质量**——收到拦截后会不会停止绕路（不改参数、不换工具、
    转而向用户说明），而不是代码行为。故这里只负责把场面搭好。
    """
    seed_with_git(workspace, user_dir)
    cmd = _helper(
        workspace,
        "hook_block.py",
        "import sys\n"
        "sys.stderr.write('本仓库禁止直接 push 到 main，请改走 Pull Request 流程')\n"
        "sys.exit(2)\n",
    )
    _write_hooks(
        workspace,
        "hooks:\n"
        "  - name: 禁止直接 push 到 main\n"
        "    event: pre_tool_use\n"
        "    if:\n"
        "      all:\n"
        "        - tool: run_command\n"
        "        - command: \"git push *\"\n"
        "    action:\n"
        "      type: command\n"
        f"      command: '{cmd}'\n",
    )


def seed_live_inject(workspace: Path, user_dir: Path) -> None:
    """
    场景 10：一条 `turn_start` 的注入型 Hook。

    判的是注入的提示**有没有真的影响模型行为**——它被要求「先说出当前分支名再动手」，
    那句话只可能来自注入文本（工作区里没有别的地方写着它）。
    """
    seed_basic(workspace, user_dir)
    _write_hooks(
        workspace,
        "hooks:\n"
        "  - name: 开工前先声明分支\n"
        "    event: turn_start\n"
        "    if:\n"
        "      all:\n"
        "        - scope: main\n"
        "    action:\n"
        "      type: prompt\n"
        "      text: |\n"
        "        当前 git 分支是 `release-2026`。在做任何事之前，"
        "你必须先在回复的第一句话里原样说出这个分支名，然后再继续。\n",
    )
