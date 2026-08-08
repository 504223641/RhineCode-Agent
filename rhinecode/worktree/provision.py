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

⚠ **本版本里 `link` 实际上一律降级为 copy**（真实模型实测发现，见下方代码里的
长注释）：软链指向的目标在隔离工作区之外，而权限管线第②层明令拒绝这类符号链接
——链接建得成，子 Agent 却一个字节也读不到。降级并留痕，好过留一个建成了
却用不了的链接。要让 `link` 真正可用得动第②层的边界判定，属安全边界变更。

⚠ **本模块从不导致创建失败。** 单条条目出任何问题都只记警告、继续下一条。
清单来自配置文件，一条写错了不该让整个委派挂掉——但必须让用户看得见，
所以每条警告都要具体到是哪一条、为什么。
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Sequence

from rhinecode.tools.path_guard import PathGuardError, is_inside, resolve_in_workspace
from rhinecode.worktree.models import ProvisionEntry, ProvisionResult


def _safe_target(target_root: Path, relative: str) -> Path:
    """
    算出条目在隔离工作区内的落点，并确认它没有跳出去、也不是工作区根本身。

    :param target_root: 隔离工作区根目录
    :param relative: 相对路径（来自清单条目的 `source`）
    :returns: 解析后的绝对路径
    :raises PathGuardError: 解析后越过了 `target_root`，**或等于 `target_root`**

    为什么来源已经校验过了、这里还要再校验一次目标：`source` 通过了
    「在主项目根内」的校验，只说明它作为**来源**是安全的；把同一个字符串
    当作**相对 target_root 的路径**再拼一次，是另一次独立的拼接，
    必须独立校验。两次校验的根不同，缺一不可。

    ⚠ **「不等于根本身」这一条是验收期审计出来的漏洞，不可省。**

    `worktree.copy: ["."]` 是一条完全合法的路径写法：`resolve_in_workspace(".", root)`
    返回的正是 `root` 自己，越界检查照样通过。于是 `_copy_one` 会先
    `shutil.rmtree(工作区根)` 把刚建好的工作区整个删掉，再把**整个主项目**
    （含 `.rhinecode/worktrees/` 下的其它工作区）递归复制进去。

    它不是攻击构造——用户想「把项目里的东西都带过去」时很自然就会这么写，
    而报错信息会是一句莫名其妙的复制失败。
    """
    resolved = resolve_in_workspace(relative, target_root)
    if resolved == Path(target_root).resolve():
        raise PathGuardError(
            f"落点不能是隔离工作区根目录本身：{relative!r}"
        )
    return resolved


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

        # ① 来源必须在主项目根内，**且不能是主项目根本身**。
        #
        # 后半条与 `_safe_target` 那条同源：`"."` 解析出来就是主项目根，
        # 越界检查照样过，然后 `copytree(主项目根, ...)` 会把整个项目
        # （含其它隔离工作区）递归拷进去。验收期审计出来的。
        try:
            source = resolve_in_workspace(raw, main_root)
            if source == Path(main_root).resolve():
                raise PathGuardError(f"来源不能是项目根目录本身：{raw!r}")
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
            elif not is_inside(source, target_root):
                # ## ⚠ `link` 与权限管线第②层是互斥的（真实模型实测发现）
                #
                # 软链的落点在工作区内、**指向的目标在工作区外**，而第②层明令
                # 「拒绝指向项目外的符号链接」——于是隔离子 Agent 对这条链接下面
                # 的任何路径都会拿到「路径越界」，一个字节都读不到。
                #
                # 实测现场：`link: ["vendor"]` 之后，子 Agent 连着试
                # `read_file('vendor/bigdep/VERSION')`、`glob_files('vendor/**')`、
                # `glob_files('vendor/bigdep/*')` 全被拒，最后耗尽 12 轮预算失败；
                # 而 `worktree_provision` 记的是 `applied=2`——**系统认为它成功了**。
                # 这正是本项目最忌讳的形态：配了却没生效，且界面上看不出来。
                #
                # 这里与下面那条「建不了软链就复制」用同一套处置：降级 + 留痕。
                # 不降级的话用户拿到的是一个建成了却用不了的链接，比复制更糟。
                # 真正让 `link` 可用要动第②层的边界判定，属安全边界变更，
                # 已登记进 CLAUDE.md 的「已知后续工程项」单独立项。
                _copy_one(source, target)
                warnings.append(
                    f"环境初始化：条目 {raw!r} 的 link 指向隔离工作区之外，"
                    "工具访问它时会被路径沙箱一律拒绝，**已降级为复制**"
                    "——若该目录很大，这会明显增加磁盘占用与耗时；"
                    "想避免复制请把它移进工作区内或改为按需只读访问"
                )
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
