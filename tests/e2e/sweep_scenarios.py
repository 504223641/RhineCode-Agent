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

from pathlib import Path

from tests.e2e import seeding


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
# C9 记忆系统：场景 1（冷启动记忆注入，含 @include 展开）
# ---------------------------------------------------------------------------
# RHINE.md 里放一条**可观测且模型不会自发遵守**的指令，这样「注入有没有生效」
# 才可证伪。选「每条回复以固定暗号开头」——它既不影响任务本身，又一眼可辨。
_RHINE_MD = """# 项目指令

这是 Nebula 演示项目。

@include docs/style.md

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
