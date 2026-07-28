"""
场景预置（spec F15）：把一个空的临时工作区变成「像样的项目」。

宿主的启动顺序是「建目录 → 可丢弃校验 → chdir → **预置** → 装配」，
所以本模块的函数一律在装配**之前**跑——RhineCode 启动时扫到的 Skill、读到的
RHINE.md、加载到的权限规则，都必须在那之前就落好盘。

每个函数只做一件事，由调用方自由组合。全部**显式 `encoding="utf-8"`**（spec N9）：
预置内容里有中文，依赖平台默认编码会在 Windows 上写成 GBK，然后产品按 UTF-8 读回
就是乱码——而这种乱码看起来像是产品的 bug。

## ⚠️ 预置 Skill 的一条硬约束

**`allowed_tools` 里不得写 `mcp_add_server` / `mcp_resolve_server`。**
宿主装配时会用 `build_app(exclude_tools=…)` 把这两个工具摘掉（前者会写真实用户
主目录、后者是只读工具却要访问外部包索引，两者都突破隔离）。摘除发生在 Skill 白名单
校验**之后**，所以写了它们不会 fail-fast，但那条白名单会在运行期交集时被剔空、
静默降级为「不收窄」——你以为收窄了，其实没有。详见 plan §3.10。

## git 是本设施的环境前置

`seed_git_repo` 需要本机装有 git。缺失时**明确抛错、不静默跳过**：
静默跳过会让「依赖提交历史」的场景假绿——测试通过了，可它验的那件事根本没发生。
这与 spec N3「确定性形态零外部依赖」不冲突：git 是本地可执行程序，不是网络依赖。
该前置已登记进 checklist。
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Iterable, Mapping, Sequence, Union

import yaml


PathLike = Union[str, Path]


class GitUnavailableError(RuntimeError):
    """本机没有可用的 git。见模块 docstring：明确报错优于静默跳过。"""


def seed_files(root: PathLike, mapping: Mapping[str, str]) -> None:
    """
    按「相对路径 → 内容」批量写文件，父目录自动创建。

    :param root: 基准目录（通常是工作区）
    :param mapping: 相对 POSIX 路径到文件内容的映射
    副作用：创建目录与文件。已存在的同名文件会被覆盖。
    """
    base = Path(root)
    for rel, content in mapping.items():
        target = base / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")


def seed_git_repo(root: PathLike, commits: Sequence[Mapping[str, object]]) -> None:
    """
    在 `root` 建一个真实的 git 仓库并逐条造出提交历史。

    :param commits: 每条形如 `{"message": "...", "files": {"a.py": "..."}}`，
        按顺序 add + commit。`files` 可省（表示只在既有内容上打一条提交）。
    :raises GitUnavailableError: 本机没有 git
    :raises subprocess.CalledProcessError: 某条 git 命令失败（`check=True`，不吞）

    副作用：在 `root` 下创建 `.git/`，并写入 `commits` 里声明的文件。

    ⚠️ **user.name / user.email 用的是仓库级配置（`git config` 不带 `--global`）**：
    绝不能去动运行测试这台机器的全局 git 配置。
    """
    base = Path(root)

    def run(*args: str) -> None:
        try:
            subprocess.run(
                ["git", *args],
                cwd=str(base),
                check=True,
                capture_output=True,
                encoding="utf-8",
                errors="replace",
            )
        except FileNotFoundError as e:
            raise GitUnavailableError(
                "本设施需要 git：`seed_git_repo` 要造出真实的提交历史，"
                "而依赖提交历史的验收场景在没有 git 时会假绿（测试通过但验的事没发生），"
                "所以这里明确报错而不是静默跳过。请先安装 git 并确保它在 PATH 上。"
            ) from e

    run("init")
    # 仓库级配置：不带 --global，绝不污染这台机器
    run("config", "user.name", "RhineCode E2E")
    run("config", "user.email", "e2e@rhinecode.invalid")
    # 有的环境默认分支名会触发提示，显式定一个，保证跨机器一致
    run("checkout", "-B", "main")

    for commit in commits:
        files = commit.get("files") or {}
        if files:
            seed_files(base, files)  # type: ignore[arg-type]
        run("add", ".")
        run("commit", "-m", str(commit.get("message", "e2e commit")))


def _render_skill(name: str, frontmatter: Mapping[str, object], body: str) -> str:
    """把 frontmatter + 正文渲染成一份 Skill 的 Markdown 文本。"""
    data = {"name": name, **dict(frontmatter)}
    fm = yaml.safe_dump(data, allow_unicode=True, sort_keys=False).strip()
    return f"---\n{fm}\n---\n\n{body.strip()}\n"


def seed_project_skill(
    root: PathLike, name: str, frontmatter: Mapping[str, object], body: str
) -> Path:
    """
    预置一个**项目级** Skill：`<root>/.rhinecode/skills/<name>.md`。

    :returns: 写入的文件路径
    见模块 docstring 关于 `allowed_tools` 的硬约束。
    """
    target = Path(root) / ".rhinecode" / "skills" / f"{name}.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(_render_skill(name, frontmatter, body), encoding="utf-8")
    return target


def seed_user_skill(
    user_dir: PathLike, name: str, frontmatter: Mapping[str, object], body: str
) -> Path:
    """
    预置一个**用户级** Skill：`<user_dir>/skills/<name>.md`。

    注意与项目级的路径规则不同——用户级直接在 `user_dir` 下，没有 `.rhinecode` 这一层
    （`user_dir` 本身就相当于 `~/.rhinecode`）。
    """
    target = Path(user_dir) / "skills" / f"{name}.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(_render_skill(name, frontmatter, body), encoding="utf-8")
    return target


def seed_permissions(
    target_dir: PathLike, allow: Iterable[str] = (), deny: Iterable[str] = ()
) -> Path:
    """
    在指定目录写一份 `permissions.yaml`。

    :param target_dir: **最终目录**，不是项目根。项目级要传 `<root>/.rhinecode`，
        用户级直接传 `user_dir`——两者路径规则不同，由调用方决定，
        本函数不去猜（猜错会让规则写到一个产品根本不读的位置，然后表现为「规则没生效」）。
    :returns: 写入的文件路径
    """
    directory = Path(target_dir)
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / "permissions.yaml"
    payload = {"allow": list(allow), "deny": list(deny)}
    target.write_text(
        yaml.safe_dump(payload, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )
    return target


def seed_rhine_md(root: PathLike, text: str) -> Path:
    """预置项目根的 `RHINE.md`（三层项目指令里最靠后、优先级最高的那层）。"""
    target = Path(root) / "RHINE.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")
    return target
