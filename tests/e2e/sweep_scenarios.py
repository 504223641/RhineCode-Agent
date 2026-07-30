"""
**全阶段真实模型复测（2026-07-31）** 用的场景预置。

对应报告 `docs/e2e-sweep/`。与 `p0_scenarios.py` / `c11_scenarios.py` / `align_scenarios.py`
的关系是「同一套设施、另一份 checklist」——放独立模块的理由也一样：混在一起会让
「这个 fixture 是给谁用的」说不清。

## 本模块只有预置，没有剧本

本次复测的判据全部落在**真实模型的行为**上（`--mode live`）。脚本化假模型按轮次
照本宣科，验不了「权限规则有没有真的挡住模型」这类问题——挡住的是剧本，不是引擎。

## 与 c11_scenarios.py 相同的一条硬约束

预置 Skill 的 `allowed-tools` 里不要写 `mcp_add_server` / `mcp_resolve_server`：
宿主装配时会用 `build_app(exclude_tools=…)` 把这两个工具摘掉
（见 `host.EXCLUDED_TOOLS`），针对它们的预授权规则永远命不中。
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

from tests.e2e import seeding


# ---------------------------------------------------------------------------
# 通用：把上一台宿主的工作区/用户目录「还原」进这一台
# ---------------------------------------------------------------------------
# **为什么需要它**：宿主每次启动都建**全新**的临时工作区与临时用户目录，
# 所以凡是判据形如「重启之后 X 仍然生效」的场景（C9 的中断恢复、越用越懂你、
# 隔天回来；C6 的永久放行重启后仍免确认）在 P1a 下**本来一条都验不了**——
# 新宿主看到的是一个空目录，上一台留下的会话存档、笔记、本地规则全都不在。
#
# 这个函数把「重启」翻译成设施能做的事：先把上一台的目录树复制到一个暂存区，
# 再让新宿主在装配**之前**把它还原回去。对产品而言，这与「同一个项目目录被
# 第二次打开」完全等价——它读到的就是上次留下的文件。
#
# 暂存区路径经环境变量传入而不是写死：`--seed` 只接受 `(workspace, user_dir)`
# 两个参数，没有别的通道可以把路径带进来。
ENV_RESTORE_WORKSPACE = "RHINE_SWEEP_RESTORE_WS"
ENV_RESTORE_USER_DIR = "RHINE_SWEEP_RESTORE_USER"


def _restore_tree(src: str, dst: str) -> None:
    """把 `src` 目录树的内容合并进已存在的 `dst`（同名覆盖）。src 不存在则静默跳过。"""
    source = Path(src)
    if not source.is_dir():
        return
    shutil.copytree(source, Path(dst), dirs_exist_ok=True)


def seed_restore(workspace: str, user_dir: str) -> None:
    """
    从暂存区还原上一台宿主的工作区与用户目录，模拟「重启后重新打开同一个项目」。

    暂存区路径来自环境变量 `RHINE_SWEEP_RESTORE_WS` / `RHINE_SWEEP_RESTORE_USER`，
    未设置的那一侧不还原。

    :param workspace: 本次宿主的临时工作区（项目根）
    :param user_dir: 本次宿主的临时用户目录
    副作用：向这两个目录复制文件（同名覆盖）。
    """
    ws_src = os.environ.get(ENV_RESTORE_WORKSPACE, "")
    ud_src = os.environ.get(ENV_RESTORE_USER_DIR, "")
    if ws_src:
        _restore_tree(ws_src, workspace)
    if ud_src:
        _restore_tree(ud_src, user_dir)


# ---------------------------------------------------------------------------
# C6 权限系统：场景 2（规则放行免确认）与场景 4（deny 护密钥）
# ---------------------------------------------------------------------------
# 两条场景共用一份预置，因为它们验的是同一份规则表的两面：
#   - `allow: Bash(git *)`  → 通配放行，`git status` 不该弹面板
#   - `deny: Read(config.yaml)` → 即使在放行档下也读不到
# 顺带预置一个**内容可辨认**的 config.yaml：这样「模型是否真读到了」可证伪——
# 只看「有没有弹面板」是不够的，得看它能不能复述里面那串哨兵值。
_SENTINEL = "SWEEP-SENTINEL-9d41f7"

_CONFIG_YAML = f"""protocol: deepseek
model: demo-model
base_url: https://example.invalid
api_key: {_SENTINEL}
context_window: 65536
"""

_APP_PY = """def main():
    print("nebula demo")


if __name__ == "__main__":
    main()
"""


def seed_perm_rules(workspace: str, user_dir: str) -> None:
    """
    C6 场景 2 / 4 的预置。

    落盘三样东西：
    1. 项目级 `<workspace>/.rhinecode/permissions.yaml`，含一条通配 allow 与一条 deny；
    2. 一个带哨兵值的 `config.yaml`，用于判定 deny 是否真的挡住了内容；
    3. 一个真实的 git 仓库（`git status` 才有意义的输出，否则退出码 128 会让
       「放行了没有」与「命令本身失败了」混在一起看不清）。

    :param workspace: 宿主的临时工作区（项目根）
    :param user_dir: 宿主的临时用户目录（本函数不用，签名由宿主固定）
    副作用：写文件、跑 git（本机需装 git，缺失时 seed_git_repo 明确抛错）。
    """
    seeding.seed_permissions(
        Path(workspace) / ".rhinecode",
        allow=["Bash(git *)"],
        deny=["Read(config.yaml)"],
    )
    seeding.seed_files(
        workspace,
        {
            "config.yaml": _CONFIG_YAML,
            "app.py": _APP_PY,
            "README.md": "# Nebula\n\n一个演示项目。\n",
        },
    )
    seeding.seed_git_repo(
        workspace,
        [{"message": "chore: 初始化项目骨架", "files": {"app.py": _APP_PY}}],
    )


# ---------------------------------------------------------------------------
# C7 MCP 客户端：场景 1（真实 stdio Server 接入）与场景 3（单 Server 失败隔离）
# ---------------------------------------------------------------------------
# 两条场景共用一份 mcp.yaml，因为场景 3 的判据正是「**在有一个正常 Server 的同时**
# 另一个失败，前者不受影响」——分开预置就验不出隔离性。
#
# ⚠️ `command: npx` 在 Windows 上能用，靠的是产品侧 `mcp/transport.py:resolve_stdio_command`
#    会兜底找 `npx.cmd`。这里刻意**不**写成 `npx.cmd`：写死了就绕过了那段逻辑，
#    等于把一条真实的 Windows 兼容性判据从测试里摘掉了。
_MCP_YAML = """mcpServers:
  everything:
    command: npx
    args: ["-y", "@modelcontextprotocol/server-everything"]
  brokenserver:
    command: this-command-does-not-exist-9d41f7
    args: []
"""


def seed_mcp_servers(workspace: str, user_dir: str) -> None:
    """
    C7 场景 1 / 3 的预置：项目级 `mcp.yaml`，一个正常 Server + 一个必然失败的 Server。

    正常的那个用官方 `@modelcontextprotocol/server-everything`（需要本机有 node/npx，
    且该包已可获取）。失败的那个用一个确定不存在的可执行文件名，
    这样「失败原因」是确定的（找不到命令），不会因环境差异飘。

    :param workspace: 宿主的临时工作区（项目根）
    :param user_dir: 宿主的临时用户目录（本函数不用）
    副作用：写 `<workspace>/.rhinecode/mcp.yaml`。启动后会真的拉起一个 npx 子进程。
    """
    seeding.seed_files(workspace, {".rhinecode/mcp.yaml": _MCP_YAML})


# ---------------------------------------------------------------------------
# C9 记忆系统：场景 1（冷启动记忆注入，含 @include 展开）
# ---------------------------------------------------------------------------
# RHINE.md 里放一条**可观测且模型不会自发遵守**的指令，这样「注入有没有生效」
# 才可证伪。选「每条回复以固定暗号开头」——它既不影响任务本身，又一眼可辨。
_RHINE_MD = """# 项目指令

这是 Nebula 演示项目。

具体的代码风格约定见 @docs/style.md

## 硬性要求

- 回答任何问题时，**第一行必须是** `【NEBULA-ACK】`，然后再写正文。
"""

_STYLE_MD = """## 代码风格

- 变量命名一律用 snake_case
- 每个函数都要有中文 docstring
- 提交信息用中文，格式 `类型(范围): 描述`
"""


def seed_memory_project(workspace: str, user_dir: str) -> None:
    """
    C9 场景 1 的预置：三层 RHINE.md 中的项目根一层 + 一个被 `@include` 的子文件。

    :param workspace: 宿主的临时工作区（项目根）
    :param user_dir: 宿主的临时用户目录（本函数不用）
    副作用：写 `RHINE.md` 与 `docs/style.md`。
    """
    seeding.seed_rhine_md(workspace, _RHINE_MD)
    seeding.seed_files(workspace, {"docs/style.md": _STYLE_MD})


# ---------------------------------------------------------------------------
# C11 对齐改造：七条端到端场景共用的一批 Skill
# ---------------------------------------------------------------------------
# 全部预置成**项目级**（`<workspace>/.rhinecode/skills/`）。一次装好，
# 一台宿主就能覆盖七条场景中的绝大部分——每起一次宿主要等 npx / 装配，
# 分七次装七个 Skill 是纯粹的浪费。
#
# ⚠️ 命令名来自**路径**（目录名或文件名），不是 frontmatter 的 `name`。
#    下面 `external-audit` 那个刻意让两者不一致，用来验这条。


def _skill_dir(workspace: str, name: str) -> Path:
    d = Path(workspace) / ".rhinecode" / "skills" / name
    d.mkdir(parents=True, exist_ok=True)
    return d


def seed_align_skills(workspace: str, user_dir: str) -> None:
    """
    C11 对齐改造七条端到端场景的预置：七个项目级 Skill + 一个可供审查的小项目。

    逐个说明它们各自要验什么：

    - `external-audit/`（目录型，Claude Code 原样格式）：场景 1。frontmatter 的
      `name: Repository Audit Helper` 与目录名**刻意不同**，用来验「命令名来自路径、
      显示名来自 frontmatter」。带随附资源 `checklist.md`，验目录型能力包。
    - `changelog.md`（单文件）：场景 2。声明 `allowed-tools: Write` 预授权写操作，
      SOP 要求它**先写一个文件（已授权）、再删一个文件（未授权）**，
      于是同一次执行里能同时看到「免确认」与「照常弹面板」。
    - `dangerous.md`：场景 3。声明 `allowed-tools: Bash` （放行全部命令），
      SOP 直接要求跑一条递归删除，验预授权翻不过第①层黑名单。
    - `deepreview.md`：场景 4。`context: fork`，描述写得足够具体，
      让模型能在不点名的情况下自行匹配并发起。
    - `manualonly.md`：场景 5 前半。`disable-model-invocation: true`。
    - `modelonly.md`：场景 5 后半。`user-invocable: false`。
    - `legacyfmt.md`：场景 6。用 C11 时代的 `allowed_tools`（下划线、收窄语义）。
    - `nocap.md`：场景 7。同时声明六个无对应能力的字段。

    :param workspace: 宿主的临时工作区（项目根）
    :param user_dir: 宿主的临时用户目录（本函数不用）
    副作用：写 `.rhinecode/skills/` 下八个 Skill 与若干被审查的样例文件。
    """
    # 场景 1：外部 Claude Code 格式的目录型 Skill，原样搬入不做任何修改
    d = _skill_dir(workspace, "external-audit")
    (d / "SKILL.md").write_text(
        "---\n"
        "name: Repository Audit Helper\n"
        "description: Audit a repository for missing docs and risky files\n"
        "when-to-use: Use when the user asks to audit, review repo hygiene, "
        "or check what documentation is missing\n"
        "allowed-tools: Read\n"
        "---\n"
        "\n"
        "# Repository Audit\n"
        "\n"
        "严格按以下步骤执行，每一步都要在回复里写出步骤编号：\n"
        "\n"
        "1. 读取随附资源 `checklist.md`，它列出了本次审计的检查项。\n"
        "2. 用 `glob_files` 列出项目根的全部文件。\n"
        "3. 逐条对照 checklist 检查，**每条都要给出「通过 / 不通过」与依据**。\n"
        "4. 最后输出一段「审计结论」，以 `AUDIT-DONE` 五个字符结尾。\n",
        encoding="utf-8",
    )
    (d / "checklist.md").write_text(
        "# 审计检查项\n\n"
        "- A1：项目根是否有 README.md\n"
        "- A2：项目根是否有 LICENSE\n"
        "- A3：是否存在名为 secrets.txt 的敏感文件\n",
        encoding="utf-8",
    )

    skills_root = Path(workspace) / ".rhinecode" / "skills"

    # 场景 2：预授权的边界——声明的写操作免确认，未声明的操作照常弹面板
    (skills_root / "changelog.md").write_text(
        "---\n"
        "description: 追加一条变更记录到 CHANGELOG.md\n"
        "when-to-use: 用户说「记一条变更」「写 changelog」时用\n"
        "allowed-tools: Write\n"
        "---\n"
        "\n"
        "按顺序做两件事，不要跳过第 2 步：\n"
        "\n"
        "1. 用 `write_file` 把一行变更记录写进 `CHANGELOG.md`，内容是：`- $ARGUMENTS`。\n"
        "2. 然后用 `run_command` 执行 `git status --short`，把输出贴给用户。\n",
        encoding="utf-8",
    )

    # 场景 3：预授权翻不过第①层黑名单
    (skills_root / "dangerous.md").write_text(
        "---\n"
        "description: 清理构建产物目录\n"
        "when-to-use: 用户说「清理构建产物」时用\n"
        "allowed-tools: Bash\n"
        "---\n"
        "\n"
        "用 `run_command` 执行 `rm -rf build` 清掉构建产物目录，然后报告结果。\n"
        "这是本 Skill 唯一的动作，不要用别的方式代替。\n",
        encoding="utf-8",
    )

    # 场景 4：模型可自行发起的 fork Skill
    (skills_root / "deepreview.md").write_text(
        "---\n"
        "description: 深度审查代码质量并只回流一段结论\n"
        "when-to-use: 用户说「审查代码质量」「看看这段代码写得怎么样」"
        "「帮我 review 一下代码」时用\n"
        "context: fork\n"
        "allowed-tools: Read\n"
        "---\n"
        "\n"
        "读取项目里的 Python 源文件，从命名、错误处理、可读性三个角度各给一条评价，\n"
        "最后用**不超过 5 行**输出结论，并以 `REVIEW-DONE` 五个字符结尾。\n",
        encoding="utf-8",
    )

    # 场景 5 前半：模型不能自行发起，只能建议用户手动触发
    (skills_root / "manualonly.md").write_text(
        "---\n"
        "description: 发布新版本到生产环境\n"
        "when-to-use: 用户说「发布」「上线」「deploy」时用\n"
        "disable-model-invocation: true\n"
        "---\n"
        "\n"
        "输出一句「发布流程已启动（演示）」即可，不要真的执行任何命令。\n",
        encoding="utf-8",
    )

    # 场景 5 后半：不注册短命令、不进补全，但模型可以自行发起
    (skills_root / "modelonly.md").write_text(
        "---\n"
        "description: 统计项目里各类文件的数量\n"
        "when-to-use: 用户问「项目里有多少个文件」「各类型文件各几个」时用\n"
        "user-invocable: false\n"
        "allowed-tools: Read\n"
        "---\n"
        "\n"
        "用 `glob_files` 统计各扩展名的文件数量，输出一张小表，以 `COUNT-DONE` 结尾。\n",
        encoding="utf-8",
    )

    # 场景 6：C11 时代的旧格式（下划线键名 + 收窄语义）
    (skills_root / "legacyfmt.md").write_text(
        "---\n"
        "description: 用旧格式写的 Skill，验迁移提示\n"
        "when_to_use: 用户说「跑旧格式测试」时用\n"
        "allowed_tools: Read\n"
        "---\n"
        "\n"
        "先用 `glob_files` 列出项目根文件（这是**写在白名单之外**的工具），\n"
        "再读其中任意一个文件，最后报告你实际调用了哪些工具，以 `LEGACY-DONE` 结尾。\n",
        encoding="utf-8",
    )

    # 场景 7：六个无对应能力的字段同时出现
    (skills_root / "nocap.md").write_text(
        "---\n"
        "description: 同时声明六个本版本不支持的字段\n"
        "when-to-use: 用户说「跑无能力字段测试」时用\n"
        "background: true\n"
        "agent: general-purpose\n"
        "effort: high\n"
        "hooks: on-save\n"
        "paths: src/**\n"
        "shell: /bin/zsh\n"
        "---\n"
        "\n"
        "输出一句「无能力字段样本执行完毕」，以 `NOCAP-DONE` 结尾。\n",
        encoding="utf-8",
    )

    # 供审查用的小项目（场景 1 / 4 要有东西可读）
    seeding.seed_files(
        workspace,
        {
            "README.md": "# Demo\n\n一个用于 Skill 复测的小项目。\n",
            "app.py": _APP_PY,
            "util.py": "def f(x):\n    return x*2\n",
        },
    )


# ---------------------------------------------------------------------------
# Skill 作者期扩展：体检建议、覆盖提示、外部适配、不点名触发
# ---------------------------------------------------------------------------
def seed_authoring(workspace: str, user_dir: str) -> None:
    """
    Skill 作者期扩展端到端场景的预置。

    四份 Skill 各自承担一条场景：

    - `sloppy.md`：场景 1 / 5。**三处刻意写坏**——说明字段 200 余字符、
      预授权裸写命令类 `Bash`、正文不含 `$ARGUMENTS`。用来验体检是否给出
      **可操作**的三条建议，以及 `skill-creator` 能否照着建议改。
    - `commit.md`：场景 2 前半。命名与内置样板撞名，验覆盖提示的措辞。
    - `foreign.md`：场景 6。外部风格——下划线写法的预授权字段 + 一个本系统
      不支持的标准字段（`background`）+ 一个本系统认不出的工具名（`TodoWrite`）。
    - `frontend.md`：场景 8（R 系列核心）。说明字段里**带触发词**，
      用来验「用户不点名 Skill，模型能否自行加载」。

    :param workspace: 宿主的临时工作区（项目根）
    :param user_dir: 宿主的临时用户目录（本函数不用）
    副作用：写 `.rhinecode/skills/` 下四个 Skill 与一个小项目。
    """
    root = Path(workspace) / ".rhinecode" / "skills"
    root.mkdir(parents=True, exist_ok=True)

    # 场景 1 / 5：三处都写坏
    root.joinpath("sloppy.md").write_text(
        "---\n"
        "description: 这个 Skill 用来处理项目中各种各样的日常维护任务，"
        "包括但不限于清理临时文件、整理目录结构、检查依赖版本、更新文档、"
        "同步配置、归档旧日志、以及其它一些零碎的杂活，总之就是维护相关的事情都可以用它\n"
        "allowed-tools: Bash\n"
        "---\n"
        "\n"
        "按顺序执行维护任务：先看看项目里有什么，再报告发现。\n",
        encoding="utf-8",
    )

    # 场景 2 前半：与内置样板 commit 撞名
    root.joinpath("commit.md").write_text(
        "---\n"
        "description: 本项目定制的提交流程\n"
        "when-to-use: 用户说「提交」时用\n"
        "---\n"
        "\n"
        "用 `run_command` 跑 `git status`，然后把结果念给用户听，以 `CUSTOM-COMMIT` 结尾。\n",
        encoding="utf-8",
    )

    # 场景 6：外部风格，三处差异
    root.joinpath("foreign.md").write_text(
        "---\n"
        "description: 整理待办事项清单\n"
        "when_to_use: 用户说「整理待办」时用\n"
        "allowed_tools: Read, TodoWrite\n"
        "background: true\n"
        "---\n"
        "\n"
        "读取项目里的 TODO 标记，整理成一张清单。\n",
        encoding="utf-8",
    )

    # 场景 8：说明字段带触发词，验「不点名也能被模型自行加载」
    root.joinpath("frontend.md").write_text(
        "---\n"
        "description: 做前端页面。用户说「写个页面」「做个前端」「创建前端页面」时用\n"
        "when-to-use: 需要新建 HTML/CSS 页面时用；也适用于「帮我做个落地页」这类请求\n"
        "allowed-tools: Read\n"
        "---\n"
        "\n"
        "本项目的前端页面必须遵守以下约定，**不要按你自己的默认做法做**：\n"
        "\n"
        "1. 页面文件一律放在 `web/` 目录下，文件名用 kebab-case。\n"
        "2. 每个页面的 `<head>` 里必须有一行注释 `<!-- NEBULA-UI v2 -->`。\n"
        "3. 样式一律内联在 `<style>` 标签里，不引外部 CSS。\n"
        "4. 完成后在回复末尾写一行 `FRONTEND-SOP-APPLIED`。\n",
        encoding="utf-8",
    )

    seeding.seed_files(
        workspace,
        {
            "README.md": "# Demo\n\n一个用于 Skill 作者期复测的小项目。\n",
            "app.py": _APP_PY,
            "TODO.md": "- TODO: 补单元测试\n- TODO: 写部署文档\n",
        },
    )


# ---------------------------------------------------------------------------
# C8 上下文管理：场景 3 / 4（手动摘要与自动兜底）
# ---------------------------------------------------------------------------
# 要让摘要真的发生，得先把历史撑起来。预置一批**内容各不相同**的中等文件：
# 内容相同的话模型一眼看穿规律就不逐个读了，历史撑不起来。
def seed_bulk_files(workspace: str, user_dir: str) -> None:
    """
    C8 场景 3 / 4 的预置：24 个内容互不相同的文本文件，供撑大历史用。

    :param workspace: 宿主的临时工作区（项目根）
    :param user_dir: 宿主的临时用户目录（本函数不用）
    副作用：写 `notes/` 下 24 个文件。
    """
    mapping = {}
    for i in range(1, 25):
        lines = [f"# 记录 {i:02d}"]
        lines += [f"- 第 {i:02d} 号记录的第 {j} 条内容：编号 {i * 1000 + j}" for j in range(1, 26)]
        mapping[f"notes/rec_{i:02d}.md"] = "\n".join(lines) + "\n"
    seeding.seed_files(workspace, mapping)
