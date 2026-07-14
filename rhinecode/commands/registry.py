"""
命令注册表（c10 T6–T8）：原子注册、冲突校验、名称解析、补全候选与帮助文本。

设计要点（plan 第 6 节）：
- 单一事实来源（spec F1/F3）：执行、帮助、补全全部读同一份 CommandSpec 列表；
- 大小写不敏感：索引键统一 casefold()，显示保留定义中的小写形式（spec F6）；
- 原子注册（plan 3）：register_many 先在临时映射整批验证，全部通过才提交——
  批量注册中途冲突不会留下「前半批已注册」的半成品状态；
- fail-fast（spec F2/N4）：任何名称/别名冲突抛 CommandRegistrationError，
  错误信息含冲突标识与双方规范命令，由启动入口捕获后以退出码 1 结束；
- 本模块不执行命令（执行在 dispatcher），也不依赖 TUI / Conversation。
"""

from typing import Iterable, Optional

from rhinecode.commands.models import CommandSpec, CommandType, CompletionItem


class CommandRegistrationError(Exception):
    """命令注册冲突或格式非法时抛出；启动入口捕获后打印错误并以退出码 1 结束。"""


# 帮助文本里命令类型的中文标签（spec F19 要求帮助展示类型）。
_TYPE_LABELS = {
    CommandType.LOCAL: "本地",
    CommandType.UI: "界面",
    CommandType.PROMPT: "提示词",
}


class CommandRegistry:
    """
    命令注册表。内部维护：
    - self._specs：有序 CommandSpec 列表（注册顺序即帮助与候选顺序，spec F19/F22）；
    - self._index：casefold 索引键 → (CommandSpec, 实际标识)，其中「实际标识」是
      定义里的规范名或别名原文，用于构造 matched_name 与冲突信息。
    """

    def __init__(self) -> None:
        self._specs: list[CommandSpec] = []
        self._index: dict[str, tuple[CommandSpec, str]] = {}

    # ------------------------------------------------------------------ #
    # 注册与冲突校验（T6）
    # ------------------------------------------------------------------ #
    @staticmethod
    def _validate_identifier(identifier: str, spec: CommandSpec) -> None:
        """校验单个标识（规范名或别名）：必须以 "/" 开头且不含空白。"""
        if not identifier.startswith("/") or len(identifier) < 2:
            raise CommandRegistrationError(
                f"invalid command identifier: {identifier!r} declared by {spec.name}"
                "（命令名与别名必须以 / 开头且非空）"
            )
        if any(ch.isspace() for ch in identifier):
            raise CommandRegistrationError(
                f"invalid command identifier: {identifier!r} declared by {spec.name}"
                "（命令名与别名不得包含空白字符）"
            )

    def _stage(
        self,
        spec: CommandSpec,
        staged: dict[str, tuple[CommandSpec, str]],
    ) -> None:
        """
        把一条命令的规范名与全部别名写入 staged 暂存映射，途中做完整冲突检查。

        冲突覆盖（spec F2）：规范名/规范名、规范名/别名、别名/别名、
        同一命令内部重复别名、仅大小写不同的重复项——staged 已含正式映射的
        全部键，因此新旧命令之间与本批命令之间的冲突都能查出。

        :raises CommandRegistrationError: 标识格式非法或与已有标识冲突
        """
        for identifier in (spec.name, *spec.aliases):
            self._validate_identifier(identifier, spec)
            key = identifier.casefold()
            if key in staged:
                existing_spec, existing_ident = staged[key]
                raise CommandRegistrationError(
                    f"command name collision: {identifier} is declared by "
                    f"{spec.name} and {existing_spec.name}"
                    f"（已有标识 {existing_ident}）"
                )
            staged[key] = (spec, identifier)

    def register(self, spec: CommandSpec) -> None:
        """
        注册单条命令；冲突时抛异常且注册表不发生任何变化。

        :raises CommandRegistrationError: 标识非法或冲突
        """
        staged = dict(self._index)
        self._stage(spec, staged)
        self._index = staged
        self._specs.append(spec)

    def register_many(self, specs: Iterable[CommandSpec]) -> None:
        """
        原子批量注册：先在临时映射验证整批，全部通过后一次性提交（plan 3）。

        任一命令冲突时抛异常，且**整批都不写入**正式注册表——
        不会出现「前半批已注册、后半批失败」的半成品状态。

        :raises CommandRegistrationError: 任一标识非法或冲突
        """
        staged = dict(self._index)
        pending: list[CommandSpec] = []
        for spec in specs:
            self._stage(spec, staged)
            pending.append(spec)
        # 整批验证通过，一次性提交
        self._index = staged
        self._specs.extend(pending)

    # ------------------------------------------------------------------ #
    # 名称解析与可见命令（T7）
    # ------------------------------------------------------------------ #
    def resolve(self, name_or_alias: str) -> Optional[CommandSpec]:
        """
        按规范名或别名解析命令，大小写不敏感（spec F6）。

        隐藏命令同样可解析——hidden 只影响帮助/补全的可发现性，不影响直接执行
        （spec F20）。

        :returns: 命中的 CommandSpec；未命中返回 None
        """
        entry = self._index.get(name_or_alias.casefold())
        return entry[0] if entry else None

    def resolve_matched(self, name_or_alias: str) -> Optional[tuple[CommandSpec, str]]:
        """
        与 resolve 相同，但额外返回实际命中的标识原文（规范名或别名的注册形式），
        供 dispatcher 构造 CommandInvocation.matched_name。

        :returns: (CommandSpec, 命中的标识)；未命中返回 None
        """
        return self._index.get(name_or_alias.casefold())

    def visible_commands(self) -> tuple[CommandSpec, ...]:
        """按注册顺序返回全部非隐藏命令（帮助与候选的数据来源）。"""
        return tuple(spec for spec in self._specs if not spec.hidden)

    # ------------------------------------------------------------------ #
    # 补全与帮助（T8）
    # ------------------------------------------------------------------ #
    def complete(self, prefix: str) -> tuple[CompletionItem, ...]:
        """
        返回匹配 prefix 的补全候选，同时匹配可见命令的规范名与别名（spec F21）。

        候选顺序稳定（spec F22/N3）：按命令注册顺序排列；每条命令先规范名、
        后按声明顺序排列别名。别名作为独立候选出现并标注其规范命令（plan 4.5）。
        隐藏命令及其全部别名不参与候选（spec F20）。

        :param prefix: 用户已输入的命令字段前缀（如 "/co"）；匹配大小写不敏感
        """
        key = prefix.casefold()
        items: list[CompletionItem] = []
        for spec in self._specs:
            if spec.hidden:
                continue
            if spec.name.casefold().startswith(key):
                items.append(
                    CompletionItem(
                        value=spec.name,
                        canonical_name=spec.name,
                        description=spec.description,
                        is_alias=False,
                    )
                )
            for alias in spec.aliases:
                if alias.casefold().startswith(key):
                    items.append(
                        CompletionItem(
                            value=alias,
                            canonical_name=spec.name,
                            description=f"{spec.name} 的别名",
                            is_alias=True,
                        )
                    )
        return tuple(items)

    def render_help(self) -> str:
        """
        生成 /help 的帮助文本（spec F19）：按注册顺序列出全部非隐藏命令的
        规范名、别名、简短描述、用法、类型及参数提示。纯文本分组输出，
        不引入复杂渲染依赖（plan 技术决策）。
        """
        lines: list[str] = ["可用命令："]
        for spec in self.visible_commands():
            lines.append(f"  {spec.name} — {spec.description}")
            detail_parts = []
            if spec.aliases:
                detail_parts.append("别名：" + "、".join(spec.aliases))
            detail_parts.append(f"类型：{_TYPE_LABELS[spec.command_type]}")
            detail_parts.append(f"用法：{spec.usage}")
            if spec.argument_hint:
                detail_parts.append(f"参数：{spec.argument_hint}")
            lines.append("      " + " · ".join(detail_parts))
        return "\n".join(lines)
