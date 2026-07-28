"""
Skill 的三层目录扫描（c11 T9/T10）。

**职责**：把三个目录里的文件变成一份 `SkillCatalog`——扫目录、读文件、
调 `parser.parse_skill`、做层内去重与跨层覆盖。

**只读文件系统，全程 fail-safe**（spec N2）：任何单个文件的问题（坏 YAML、
缺必填字段、读不出来、目录缺入口）都只影响它自己，其余 Skill 照常可用；
目录不存在直接当空层，不算错误。理由很直接——Skill 是可选增强，
不该因为某人往目录里丢了个记事本文件就让整个 RhineCode 起不来。

对应 spec 条款见 `docs/c11/align/spec.md`：F2（命令名来自路径）、
F3（三层优先级覆盖）、F4（命令名最低限度校验）、F5（单文件失败不阻断）。
"""

from pathlib import Path
from typing import Optional

from rhinecode.skills.models import (
    ENTRY_FILENAME,
    RESERVED_SUBCOMMANDS,
    RESOURCE_LIST_MAX,
    SkillCatalog,
    SkillLoadError,
    SkillSource,
    SkillSpec,
)
from rhinecode.skills.parser import parse_skill


def _collect_resource_files(directory: Path) -> tuple[str, ...]:
    """
    收集目录型 Skill 的随附文件相对路径清单（spec F13）。

    :param directory: Skill 目录本身
    :returns: 相对 `directory` 的路径字符串元组，按字典序，
              **不含入口文件 `SKILL.md` 自身**，条数上限 RESOURCE_LIST_MAX

    路径分隔符统一成 `/`：清单是给模型看的，跨平台保持一致的写法能避免
    模型在 Windows 上拿到 `templates\\a.md` 这种需要转义的字符串。

    截断而不是全量列出：资源目录可能塞了几百个文件，清单只是索引，
    真正的内容要模型自己去 read_file。塞爆上下文得不偿失。

    副作用：读目录（rglob）。任何 OSError 都被吞掉并返回已收集到的部分——
    清单不完整只是少列几个文件，不该让整个 Skill 加载失败。
    """
    try:
        # 只要文件，跳过子目录本身；排除入口文件（它不是"随附资源"）。
        rels = sorted(
            p.relative_to(directory).as_posix()
            for p in directory.rglob("*")
            if p.is_file() and p.name != ENTRY_FILENAME
        )
    except OSError:
        return ()
    return tuple(rels[:RESOURCE_LIST_MAX])


def _scan_layer(
    directory: Optional[Path],
    source: SkillSource,
) -> tuple[list[SkillSpec], list[SkillLoadError], dict[str, list[str]]]:
    """
    扫描单个层级目录。

    :param directory: 该层目录；None 或不存在时当作空层
    :param source: 该层的来源标记，写进产出的 SkillSpec 与 SkillLoadError
    :returns: `(specs, errors, warnings_by_name)` 三元组。
              第三项按 Skill 名归类警告，**而不是拉平成一个列表**——
              低优先层的 Skill 可能被高优先层覆盖，它的警告就不该发出去
              （否则用户会被指去修改一个根本没生效的文件）。
              归类后由 `discover` 只取生效那份的警告。

    识别规则：
    - `*.md` 文件 → 单文件型 Skill；
    - 含 `SKILL.md` 的子目录 → 目录型 Skill，目录内其余文件是随附资源；
    - 缺 `SKILL.md` 的子目录 → 记一条错误（用户大概率是想做目录型但忘了入口）；
    - 其它文件（`.txt` / `.yaml` / 无后缀等）→ **静默跳过**，不记错误。
      理由：用户可能在 skills 目录里放 README、草稿或编辑器临时文件，
      为这些东西刷一堆错误只会让 `/skills` 报告变成噪音。

    **命令名来自路径**（对齐改造 F2）：目录型取目录名、单文件型取去扩展名的文件名。
    因此同一层内不可能出现重名，C11 的「层内同名去重」整段删除。
    仍用 `sorted(iterdir())` 遍历，是为了让扫描顺序（进而是错误与警告的顺序）
    在任何机器上都确定。

    副作用：读目录与读文件。不写任何东西。
    """
    specs: list[SkillSpec] = []
    errors: list[SkillLoadError] = []
    warnings: dict[str, list[str]] = {}

    if directory is None:
        return specs, errors, warnings

    try:
        if not directory.is_dir():
            # 目录不存在是**正常情况**（用户没建过 skills 目录），不是错误。
            return specs, errors, warnings
        entries = sorted(directory.iterdir())
    except OSError as exc:
        errors.append(
            SkillLoadError(directory, source, f"读取 Skill 目录失败：{exc}")
        )
        return specs, errors, warnings

    for entry in entries:
        entry_path: Path
        resource_dir: Optional[Path]
        resource_files: tuple[str, ...]
        # **命令名来自文件系统路径**（对齐改造 F2）：目录型取目录名，
        # 单文件型取去扩展名的文件名。这是「外部 Skill 原样可用」的基础——
        # 从任何来源拉一个目录丢进去，命令名就是目录名，不必检查 frontmatter。
        command_name: str

        if entry.is_file() and entry.suffix == ".md":
            entry_path = entry
            resource_dir = None
            resource_files = ()
            command_name = entry.stem
        elif entry.is_dir():
            candidate = entry / ENTRY_FILENAME
            if not candidate.is_file():
                errors.append(
                    SkillLoadError(
                        entry, source, f"目录型 Skill 缺少入口文件 {ENTRY_FILENAME}"
                    )
                )
                continue
            entry_path = candidate
            resource_dir = entry
            resource_files = _collect_resource_files(entry)
            command_name = entry.name
        else:
            continue

        # 命令名的最低限度校验（对齐改造 F4）。字符集与长度**不再校验**——
        # 文件系统已经保证名字合法，再叠一层自定义规则只会让本可直接使用的
        # 外部 Skill 目录（含大写、下划线、超长名）被无理由拒绝。
        if command_name in RESERVED_SUBCOMMANDS:
            errors.append(
                SkillLoadError(
                    entry_path,
                    source,
                    f"命令名 `{command_name}` 是 /skills 的保留子命令词，"
                    f"请改名（否则 `/skills {command_name}` 会产生解析二义）",
                )
            )
            continue

        try:
            text = entry_path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            # 读不出来的原因可能是权限、坏符号链接，或者根本不是 UTF-8 文本
            # （比如有人把二进制文件改名成 .md）。都只影响这一个文件。
            errors.append(SkillLoadError(entry_path, source, f"读取失败：{exc}"))
            continue

        spec, reason, spec_warnings = parse_skill(
            text, entry_path, source, command_name, resource_dir, resource_files
        )
        if spec is None:
            errors.append(SkillLoadError(entry_path, source, reason or "未知解析错误"))
            continue

        # **层内重名的检测已整段删除**（对齐改造 F8）：命令名现在来自文件系统，
        # 同一目录下不可能有两个同名条目，这个失败原因不再可能发生。
        specs.append(spec)
        if spec_warnings:
            warnings[spec.command_name] = spec_warnings

    return specs, errors, warnings


def discover(
    project_root: Optional[Path],
    user_dir: Optional[Path],
    builtin_dir: Optional[Path],
) -> SkillCatalog:
    """
    扫描三层目录，产出一份完整的 Skill 快照。

    :param project_root: 项目根；实际扫描的是 `<project_root>/.rhinecode/skills`
    :param user_dir: 用户级配置目录（通常是 `~/.rhinecode`）；扫描 `<user_dir>/skills`
    :param builtin_dir: 内置样板目录，通常来自 `models.builtin_skills_dir()`
    :returns: `SkillCatalog` 不可变快照

    **跨层覆盖（F3）**：按 `SkillSource` 的成员定义顺序（PROJECT → USER → BUILTIN）
    逐层扫描，用 `setdefault` 让**先到者胜出**。低优先层的同名 Skill 被直接丢弃，
    **且不记错误**——覆盖是这套三层设计的正常用法（项目定制版盖掉内置样板），
    不是失败。若把它记成错误，每个定制过样板的项目启动时都会刷一条无意义的告警。

    **覆盖是整份替换，不做字段合并**：项目级的 `deploy` 完全取代用户级的 `deploy`，
    不会出现「正文取项目级、白名单取用户级」这种缝合结果。字段合并看似贴心，
    实际会让人完全无法预测最终生效的是什么。

    副作用：读三个目录及其中的文件。不写任何东西、不改全局状态。
    """
    # 三层目录的定位。用 SkillSource 的定义顺序驱动，与 models.py 中
    # 「成员定义顺序即优先级顺序」的注释是同一件事的两半。
    layers: list[tuple[Optional[Path], SkillSource]] = [
        (
            (project_root / ".rhinecode" / "skills") if project_root else None,
            SkillSource.PROJECT,
        ),
        ((user_dir / "skills") if user_dir else None, SkillSource.USER),
        (builtin_dir, SkillSource.BUILTIN),
    ]

    chosen: dict[str, SkillSpec] = {}
    all_errors: list[SkillLoadError] = []
    # (来源层, Skill 名) → 警告列表。带上来源层是因为同名 Skill 可能在多层都存在，
    # 只有最终胜出的那一份的警告才该发出去。
    warning_index: dict[tuple[SkillSource, str], list[str]] = {}

    for directory, source in layers:
        specs, errors, warnings_by_name = _scan_layer(directory, source)
        all_errors.extend(errors)
        for spec in specs:
            # setdefault：高优先层已占用的名字，低优先层直接落空，静默丢弃。
            chosen.setdefault(spec.command_name, spec)
        for name, items in warnings_by_name.items():
            warning_index[(source, name)] = items

    ordered = sorted(chosen.values(), key=lambda s: s.command_name)

    # 只发出生效那份的警告，按 Skill 名排序（与 skills 列表同序，便于对照）。
    all_warnings: list[str] = []
    for spec in ordered:
        all_warnings.extend(warning_index.get((spec.source, spec.command_name), []))

    return SkillCatalog(
        skills=tuple(ordered),
        errors=tuple(all_errors),
        warnings=tuple(all_warnings),
    )
