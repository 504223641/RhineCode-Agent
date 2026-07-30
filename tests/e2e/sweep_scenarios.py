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
