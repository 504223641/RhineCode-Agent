"""
现成的剧本与预置函数，供宿主的 `--script` / `--seed` 直接引用。

    python -m tests.e2e.host --mode scripted --script tests.e2e.scripts:CONFIRM_THEN_DONE

放在这里而不是散在各测试文件里，是为了让**手工驱动**也能直接用——
手测时不该为了起一个宿主先去写一个 Python 模块。

⚠️ 预置 Skill 的 `allowed_tools` 不得写 `mcp_add_server` / `mcp_resolve_server`
（宿主会摘掉它们，见 `host.EXCLUDED_TOOLS`）。
"""

from __future__ import annotations

from pathlib import Path

from tests.e2e import seeding
from tests.e2e.scripted import done, stream_error, text, thinking, tool


# 一轮就结束：最简单的冒烟剧本
SAY_HELLO = [
    [text("你好，我是 RhineCode。"), done()],
]

# 会触发一次工具确认面板（write_file 非只读，默认模式下无规则命中 → 问用户）
CONFIRM_THEN_DONE = [
    [text("我来写个文件。"), tool("write_file", {"path": "x.txt", "content": "hi"}), done()],
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
        {"description": "白名单写错了", "allowed_tools": ["read_file", "no_such_tool"]},
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
            "mode": "isolated",
            "model": "deepseek-reasoner",
            "allowed_tools": ["read_file", "glob_files"],
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
            "allowed_tools": ["read_file", "glob_files"],
        },
        "第一步：列出改动。第二步：逐个读。第三步：给结论。\n\n$ARGUMENTS",
    )
