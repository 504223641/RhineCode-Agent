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
    - self._builtin_specs：内置命令，启动时一次性注册，此后不变；
    - self._skill_specs：Skill 短命令（c11），可被 `replace_skill_commands` 整体替换；
    - self._specs：上面两者的拼接（只读属性），注册顺序即帮助与候选顺序（spec F19/F22）；
    - self._index：casefold 索引键 → (CommandSpec, 实际标识)，其中「实际标识」是
      定义里的规范名或别名原文，用于构造 matched_name 与冲突信息。

    **为什么把 specs 拆成两个列表**（c11 T37）：Skill 短命令要支持热更新
    （`/skills reload` 后整批替换），而内置命令必须原样保留。
    拆开后「替换 Skill 命令」就是替换一个列表，不需要在混合列表里做筛选删除。
    """

    def __init__(self) -> None:
        self._builtin_specs: list[CommandSpec] = []
        self._skill_specs: list[CommandSpec] = []
        self._index: dict[str, tuple[CommandSpec, str]] = {}

    @property
    def _specs(self) -> list[CommandSpec]:
        """
        全部命令，内置在前、Skill 短命令在后。

        拼接顺序即 `/help` 与补全候选的稳定顺序（符合 C10 N3 的顺序稳定要求）：
        内置命令是用户最常用也最熟悉的，排在前面；Skill 短命令数量可变，排在后面。

        **注意：本属性每次返回一个新列表。**对它 `append` 会写进临时对象随即被丢弃，
        表面上什么都没发生——这是最难排查的失败形态（不报错、不生效）。
        写入必须直接操作 `_builtin_specs` 或 `_skill_specs`。
        """
        return self._builtin_specs + self._skill_specs

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
        self._builtin_specs.append(spec)

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
        self._builtin_specs.extend(pending)

    # ------------------------------------------------------------------ #
    # Skill 短命令的运行时替换（c11 T38，改造点 2）
    # ------------------------------------------------------------------ #
    def has_skill_command(self, name: str) -> bool:
        """
        判断某个 Skill 名是否**真的**注册成了斜杠短命令（c11 F7/F25）。

        :param name: Skill 名（不带斜杠）
        :returns: 已注册为 Skill 短命令时 True

        **为什么不能用 `resolve(f"/{name}")`**：那会把内置命令也算进去。
        考虑一个名叫 `context` 的 Skill——它与内置 `/context` 重名，短命令
        **没有**被注册（F25 跳过冲突项）。此时 `resolve("/context")` 会命中
        **内置**命令返回 True，于是给用户的入口提示变成「请执行 /context」，
        而那条命令跑的是上下文用量报告，跟这个 Skill 毫无关系。

        指向一个存在但错误的命令，比指向一个不存在的命令更糟——后者用户立刻
        知道出问题了，前者会让用户以为 Skill 坏了。所以这里只查 `_skill_specs`。
        """
        target = f"/{name}".casefold()
        return any(spec.name.casefold() == target for spec in self._skill_specs)

    def replace_skill_commands(
        self, specs: Iterable[CommandSpec]
    ) -> list[CommandSpec]:
        """
        用给定的一批 Skill 短命令**整体替换**现有的（c11 F25/F26）。

        :param specs: 新的 Skill 短命令列表
        :returns: 因与既有命令冲突而**被跳过**的那些 spec，供上层打印警告

        与 `register_many` 的语义差异，这也是不复用它的原因：
        `register_many` 是「整批失败」——任一冲突则全批不注册。而这里要求
        **冲突的那条跳过、其余照常注册**（F25：一个 Skill 与内置命令重名，
        不该连累其它九个 Skill 都没有短命令）。

        三步：
        1. 从零重建暂存索引——先 stage 全部内置命令，等于把 Skill 命令清空；
        2. 逐个 stage 新的 Skill 命令，成功则接受、冲突则跳过；
        3. 全部处理完，一次性提交。

        **第 2 步为什么必须用独立的临时字典 `probe`**：现有的 `_stage` 是
        「边遍历边写入、遇冲突才抛、不回滚」。一个 spec 若有多个标识，
        前几个已经写进去了、后一个才冲突，抛异常时前几个仍留在字典里——
        那些索引项指向一个**不在 `_skill_specs` 中**的 spec，`resolve` 会
        解析出一条实际不存在的命令。当前 Skill 短命令 `aliases=()` 只有一个
        标识，问题暂时不显现，但这是个隐含不变量（见 `skill_commands.py`）。

        **中途失败自动保持原状**：提交前不触碰任何实例字段（改造点 2 的原子性）。

        副作用：替换 `_index` 与 `_skill_specs`。
        """
        # 第 1 步：从零重建，只含内置命令。
        staged: dict[str, tuple[CommandSpec, str]] = {}
        for spec in self._builtin_specs:
            self._stage(spec, staged)

        accepted: list[CommandSpec] = []
        skipped: list[CommandSpec] = []

        # 第 2 步：逐个试探。
        for spec in specs:
            probe = dict(staged)
            try:
                self._stage(spec, probe)
            except CommandRegistrationError:
                skipped.append(spec)
                continue
            staged = probe
            accepted.append(spec)

        # 第 3 步：一次性提交。
        self._index = staged
        self._skill_specs = accepted
        return skipped

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
        返回匹配 prefix 的补全候选，只匹配可见命令的**规范名**。

        别名不参与补全候选（避免菜单被别名撑大），但不影响别名的其它能力：
        仍可经 resolve 解析执行、完整命中时仍高亮、/help 仍列出别名。
        候选顺序稳定（spec N3）：按命令注册顺序排列；隐藏命令不参与候选
        （spec F20）。

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
