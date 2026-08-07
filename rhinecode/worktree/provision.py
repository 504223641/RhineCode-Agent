"""
隔离工作区的环境初始化（c14 T6，spec F10）。

**它解决的问题**（一条实测结论）：隔离工作区是 `git checkout` 出来的，
**凡被忽略规则排除的文件一概没有**。实测本仓库的隔离工作区里缺 `.rhinecode/`
与 `config.yaml`；换成 Node 项目就是缺 `node_modules/`、`.env`。
其中有些是「运行必需但不该进版本库」的，要显式补进去。

**规则由用户显式声明，本模块不做任何猜测。** 这是 spec 明确判掉启发式方案的
理由：自动识别会把含明文 API Key 的 `config.yaml` 复制进多个临时目录，
而猜错的代价由用户承担、且他不会知道。

两种模式的语义差别对用户是有意义的选择：

| 模式 | 行为 | 适用 |
| --- | --- | --- |
| `copy` | 各自独立一份，改副本不影响源 | **配置文件**——子 Agent 改坏了不牵连主目录 |
| `link` | 共享同一份 | **大型依赖目录**——500MB 复制三份是灾难 |

⚠ **本模块从不导致创建失败。** 单条条目出任何问题都只记警告、继续下一条。
清单来自配置文件，一条写错了不该让整个委派挂掉——但必须让用户看得见，
所以每条警告都要具体到是哪一条、为什么。
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Sequence

from rhinecode.tools.path_guard import PathGuardError, resolve_in_workspace
from rhinecode.worktree.models import ProvisionEntry, ProvisionResult


def _safe_target(target_root: Path, relative: str) -> Path:
    """
    算出条目在隔离工作区内的落点，并确认它没有跳出去。

    :param target_root: 隔离工作区根目录
    :param relative: 相对路径（来自清单条目的 `source`）
    :returns: 解析后的绝对路径
    :raises PathGuardError: 解析后越过了 `target_root`

    为什么来源已经校验过了、这里还要再校验一次目标：`source` 通过了
    「在主项目根内」的校验，只说明它作为**来源**是安全的；把同一个字符串
    当作**相对 target_root 的路径**再拼一次，是另一次独立的拼接，
    必须独立校验。两次校验的根不同，缺一不可。
    """
    return resolve_in_workspace(relative, target_root)


def _copy_one(source: Path, target: Path) -> None:
    """
    复制一个文件或目录。

    副作用：写文件系统。目标已存在时先删除再复制（保证「独立一份」的语义
    在重复初始化时依然成立）。
    """
    if target.exists() or target.is_symlink():
        if target.is_dir() and not target.is_symlink():
            shutil.rmtree(target)
        else:
            target.unlink()
    target.parent.mkdir(parents=True, exist_ok=True)
    if source.is_dir():
        shutil.copytree(source, target)
    else:
        shutil.copy2(source, target)


def provision(
    main_root: Path,
    target_root: Path,
    entries: Sequence[ProvisionEntry],
) -> ProvisionResult:
    """
    按清单初始化一个刚建好的隔离工作区。

    :param main_root: 主项目根，清单条目的来源基准
    :param target_root: 隔离工作区根目录
    :param entries: 清单条目（缺省为空 = 什么都不做）
    :returns: `ProvisionResult`，**永远是「成功」的**——它没有失败态，
              跳过与降级都表现为 `warnings` 里的一条中文说明

    每条条目依次过三道校验，任一不过就跳过并记警告：

    1. **来源在主项目根内**——用 `path_guard` 判定，挡住 `../` 与绝对路径。
       用户在配置里写 `../../.ssh/id_rsa` 不该被照办
    2. **来源存在**——不存在时跳过（用户可能配了一个只在别的机器上有的路径）
    3. **目标在隔离工作区内**——独立的第二次拼接，独立校验

    `link` 模式建符号链接失败时**降级为复制并记警告**。降级必须留痕：
    Windows 上建符号链接常需额外权限，静默降级会让「我配了 link 结果它复制了
    500MB」这件事无处可查。

    副作用：向 `target_root` 内写文件、建目录或建符号链接。
    """
    applied: list[str] = []
    warnings: list[str] = []

    for entry in entries:
        raw = (entry.source or "").strip()
        if not raw:
            warnings.append("环境初始化：跳过一条来源为空的条目")
            continue

        mode = (entry.mode or "").strip().lower()
        if mode not in ("copy", "link"):
            warnings.append(
                f"环境初始化：条目 {raw!r} 的模式 {entry.mode!r} 无法识别"
                "（只支持 copy / link），已跳过"
            )
            continue

        # ① 来源必须在主项目根内。
        try:
            source = resolve_in_workspace(raw, main_root)
        except PathGuardError as exc:
            warnings.append(f"环境初始化：条目 {raw!r} 超出项目目录，已跳过（{exc}）")
            continue

        # ② 来源必须存在。
        if not source.exists():
            warnings.append(f"环境初始化：条目 {raw!r} 在项目中不存在，已跳过")
            continue

        # ③ 目标必须落在隔离工作区内（独立的第二次拼接，独立校验）。
        try:
            target = _safe_target(target_root, raw)
        except PathGuardError as exc:
            warnings.append(
                f"环境初始化：条目 {raw!r} 的落点超出隔离工作区，已跳过（{exc}）"
            )
            continue

        try:
            if mode == "copy":
                _copy_one(source, target)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                if target.exists() or target.is_symlink():
                    # 重复初始化时先清掉旧的，保证语义一致。
                    if target.is_dir() and not target.is_symlink():
                        shutil.rmtree(target)
                    else:
                        target.unlink()
                try:
                    target.symlink_to(source, target_is_directory=source.is_dir())
                except (OSError, NotImplementedError) as exc:
                    # ⚠ 降级必须留痕，理由见函数 docstring。
                    _copy_one(source, target)
                    warnings.append(
                        f"环境初始化：条目 {raw!r} 无法建立软链（{exc}），"
                        "已降级为复制——若该目录很大，这会明显增加磁盘占用与耗时"
                    )
            applied.append(raw)
        except Exception as exc:  # noqa: BLE001 —— 单条失败不得影响其余条目
            warnings.append(f"环境初始化：条目 {raw!r} 处理失败，已跳过（{exc}）")

    return ProvisionResult(applied=tuple(applied), warnings=tuple(warnings))


__all__ = ["provision"]
