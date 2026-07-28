"""
SkillManager：Skill 系统唯一持有可变状态与副作用编排的地方（c11 T20–T26）。

下层五个模块（models / parser / discovery / render / validation）全是纯函数或
只读 IO；一切「记住了什么」「什么时候扫盘」「谁通知谁」都收拢在本模块。
这与 `permission/engine.py`、`context/manager.py`、`memory/manager.py` 是同一范式。

════════════════════════════════════════════════════════════════════
加锁不变量（违反会导致确定性死锁，不是理论风险）
════════════════════════════════════════════════════════════════════

    **临界区只包含纯内存状态读写；一切解析、渲染、回调、IO 都在锁外。**
    推论：持锁期间禁止调用任何回调，禁止任何形式的跨线程调度。

**为什么需要锁**：`load_skill` 工具声明 `read_only=True`（这是它在默认权限模式下
免确认的前提），因而落进 Agent 循环的**只读并发桶**。模型同一轮发起两次
`load_skill` 时，两个线程会并发执行 `activate()` 里「查在不在 → 在则改、不在则
append」这个读-改-写序列，GIL 保证不了复合操作的原子性，可能产生两条同名记录
（违反 F30 幂等）。同时 `active_text()` 由主循环线程每轮调用、`reload()` 与
`status_segment()` 由 UI 线程调用——三方都会碰 `_active`。

**为什么回调绝不能在锁内**，这是一条已验证的死锁路径：

1. `activate()` 若用 `with self._lock:` 包住整个方法体，末尾的 `notify_activation`
   就在锁内触发；
2. `notify_activation` → `RhineApp._notify_skill_activation`
   → `self.call_from_thread(self._refresh_status)`。
   **Textual 的 `call_from_thread` 是阻塞的**（既有代码 `app.py` 里有
   `x = self.call_from_thread(...)` 这种取返回值的写法，必然同步等待主线程）；
3. 主线程执行 `_refresh_status` → `skill_status_segment()` → `status_segment()`
   → **申请同一把锁**，而锁正被工作线程持有；
4. 工作线程等主线程跑完回调，主线程等工作线程放锁 → **双向永久阻塞**。

后果远超普通 bug：主线程是 Textual 事件循环，卡死后整个 TUI 冻结、
连 Esc 取消都不响应，用户只能杀进程。而触发条件极其普通——模型成功调一次
`load_skill` 即可。

**三条强制约定**：
1. `activate()` 只在锁内完成状态变更，出块后再调 `notify_activation`；
2. 同一约定覆盖 `has_short_command` 回调（它跨层调进 `CommandRegistry`），
   因此独立模式早返回分支整段在锁外；
3. 加固层：所有读路径一律「持锁取不可变快照 → 出锁后渲染」。
   即使将来有人不慎在锁内触发回调，也不会立刻演变成死锁。
"""

import threading
from pathlib import Path
from typing import Callable, Optional

from rhinecode.skills.discovery import discover
from rhinecode.skills.models import (
    ActivationResult,
    ActivationStatus,
    ActiveSkill,
    DegradeKind,
    LOAD_SKILL_TOOL,
    ReloadOutcome,
    SkillCatalog,
    SkillCommandInfo,
    SkillSource,
    SkillSpec,
)
from rhinecode.skills.render import render_active_section, render_index
from rhinecode.skills.validation import grants_for
# trace 是只依赖标准库的叶子包，从 skills 依赖它不会形成环
from rhinecode.trace import NullRecorder, TraceEventType, TraceRecorderProtocol

# 豁免工具白名单收窄的工具名（spec F8/F15）。
# `load_skill` 若被白名单挡住，模型就再也没法加载其它 Skill 了；
# `ask_user` / `present_plan` 是 Plan Mode 的流程控制工具，与业务能力无关。
_EXEMPT_TOOLS = frozenset({LOAD_SKILL_TOOL, "ask_user", "present_plan"})

# 来源层级的中文标签，`/skills` 报告用。
_SOURCE_LABEL = {
    SkillSource.PROJECT: "项目级",
    SkillSource.USER: "用户级",
    SkillSource.BUILTIN: "内置",
}

# 两种降级形态在报告里的措辞。必须分开写——一个是「做了一半」，
# 一个是「完全没生效」，用户的应对完全不同。
_DEGRADE_LABEL = {
    DegradeKind.TRUNCATED: "正文被截断（超单体上限），可能只执行前半部分流程",
    DegradeKind.DROPPED: "未注入（超总量上限），本次完全不会生效",
}


class SkillManager:
    """
    Skill 系统的编排者。

    生命周期：`__main__` 在启动早期构造它 → `startup()` 扫盘并做第一段白名单校验
    → MCP 连接完成后 `bind_tools()` 做第二段剪枝 → 交给 `ConversationManager` 持有，
    此后每轮请求调 `index_text()` / `active_text()`，执行前调 `turn_grants()`。
    """

    def __init__(
        self,
        project_root: Optional[Path],
        user_dir: Optional[Path],
        builtin_dir: Optional[Path],
        has_short_command: Callable[[str], bool],
        notify_activation: Optional[Callable[[], None]] = None,
        recorder: "Optional[TraceRecorderProtocol]" = None,
    ) -> None:
        """
        :param project_root: 项目根（扫描 `<root>/.rhinecode/skills`）
        :param user_dir: 用户级配置目录（扫描 `<dir>/skills`）
        :param builtin_dir: 随包分发的内置样板目录
        :param has_short_command: 判定某个 Skill 名**是否真的注册成了斜杠短命令**。
            必须查询「实际被注册表接受的 Skill 短命令集合」，
            **不能用 `registry.resolve(f"/{name}")`**——后者在「Skill 与内置命令
            重名导致短命令未注册」的场景下会命中**内置**命令并返回 True，
            于是给用户的入口提示会指向一个存在但完全错误的命令。
            这比指向一个不存在的命令更糟。
        :param notify_activation: 激活成功后的通知回调（TUI 用它立刻刷新状态栏）。
            可在构造后作为属性注入，与 c9 `memory_manager.notify` 同形态。
        :param recorder: 行为记录器（trace 设施）。缺省用 `NullRecorder()`，
            **不传等于零回归**。注意它必须排在 `notify_activation` **之后**——
            插进既有位置参数之间会打断
            `SkillManager(root, user_dir, builtin, has_short_command)` 这类调用方。

        本构造函数**不扫盘**——扫盘发生在 `startup()`。这样构造是廉价且无副作用的。
        """
        self._project_root = project_root
        self._recorder: TraceRecorderProtocol = recorder or NullRecorder()
        self._user_dir = user_dir
        self._builtin_dir = builtin_dir
        self._has_short_command = has_short_command
        self.notify_activation = notify_activation

        # 不可变快照。读只需取引用（引用赋值本身原子），替换在锁内完成。
        self._catalog: SkillCatalog = SkillCatalog(skills=(), errors=(), warnings=())
        # 共享模式的激活列表，顺序即注入拼接顺序（F10）。
        self._active: list[ActiveSkill] = []
        # 纯派生状态：上一次注入时各 Skill 的降级形态。只由 active_text() 写。
        self._degrades: dict[str, DegradeKind] = {}
        # 启动/热更新产生的降级警告，供 /skills 展示。
        self._runtime_warnings: list[str] = []

        # 保护 _active / _degrades / _runtime_warnings。
        # _catalog 是不可变快照，读引用无需持锁、替换在锁内。
        self._lock = threading.Lock()

    # ────────────────────────── 构造 ──────────────────────────

    @classmethod
    def empty(cls) -> "SkillManager":
        """
        构造一个什么都没有的 SkillManager（Null Object）。

        :returns: 空 catalog、不扫盘、`has_short_command` 恒为 False 的实例

        **供协调层缺省注入**。`ConversationManager` 的 `skill_manager` 参数是可选的，
        缺省时用它兜底，于是各使用点不必散落 `if self.skill_manager is not None`。

        为什么不让协调层自己 `SkillManager(...)` 兜底：
        1. 它**拿不到** `has_short_command`——那需要 `CommandRegistry` 实例，
           而 `ConversationManager.__init__` 没有它；
        2. 更要命的是会**扫盘**——每次构造 `ConversationManager` 都去读
           `~/.rhinecode/skills/`，等于给既有的一整套测试引入隐式的用户主目录 IO，
           违背 N1「可在无终端无网络环境测试」。

        副作用：无（不读文件、不建目录）。
        """
        return cls(
            project_root=None,
            user_dir=None,
            builtin_dir=None,
            has_short_command=lambda _name: False,
        )

    # ──────────────────────── 生命周期 ────────────────────────

    def startup(self) -> None:
        """
        启动时扫盘。

        **不再有「白名单校验」这一步**（对齐改造 F13）：白名单收窄能力已移除，
        `allowed-tools` 现在是预授权声明，其无法识别项只警告不致命，
        因此启动不会再因为一个 Skill 的工具名写错而退出。

        副作用：读三层目录（扫盘在锁外）、替换 `_catalog`、重置 `_runtime_warnings`。
        """
        # 扫盘在锁外：它要读几十个文件，不该占着锁。
        catalog = discover(self._project_root, self._user_dir, self._builtin_dir)
        # 预授权声明里无法识别的项，在这里一次性收集成警告。
        _, grant_warnings = grants_for(catalog.skills)

        with self._lock:
            self._catalog = catalog
            self._runtime_warnings = list(catalog.warnings) + grant_warnings

    def bind_tools(self, registered: frozenset[str]) -> None:
        """
        MCP 连接完成后记一次工具集快照。

        :param registered: 连接完成后注册中心的全部工具名

        **不再做白名单剪枝**（对齐改造 F13）：收窄能力已移除，
        `allowed-tools` 是预授权声明，它指向哪个工具、那个工具连没连上，
        都不影响其它 Skill 能不能用——未命中的规则只是永远不生效而已。

        保留本方法是为了那条 trace 快照：读记录时需要知道「装配完成那一刻
        注册中心里有哪些工具」，这是排查「模型为什么没看到某个工具」的起点。

        副作用：产出一条 skill_state 事件。不改任何状态。
        """
        catalog = self._catalog  # 不可变快照，取引用即可
        self._trace_state(
            "bind_tools",
            skills=[s.command_name for s in catalog.skills],
            granted_tools={
                s.command_name: list(s.granted_tools)
                for s in catalog.skills
                if s.granted_tools
            },
            registered=sorted(registered),
        )

    def reload(self) -> ReloadOutcome:
        """
        热更新：重新扫盘、同步激活列表。

        :returns: `ReloadOutcome`，由上层渲染成可读报告

        **不再有白名单校验这一步**（对齐改造 F13）：收窄能力已移除，
        `allowed-tools` 的无法识别项只警告不致命，因此热更新也不会再丢弃 Skill。
        C11 时那条「下次启动时此错误会导致启动失败」的连带告知随之作废——
        现在启动本来就不会因为它失败。

        副作用：读三层目录、替换 `_catalog`、重置 `_runtime_warnings`、
        可能移除 `_active` 中的条目。
        """
        new_catalog = discover(self._project_root, self._user_dir, self._builtin_dir)
        _, warnings = grants_for(new_catalog.skills)
        warnings = list(new_catalog.warnings) + warnings

        old_names = {s.command_name for s in self._catalog.skills}
        new_names = {s.command_name for s in new_catalog.skills}

        with self._lock:
            self._catalog = new_catalog
            self._runtime_warnings = warnings

            # 已激活但在新 catalog 中消失的 Skill 自动卸载（F27）。
            # 不自动卸载的话，它的正文会在下一轮 active_text() 里静默消失
            # （因为按名字取 spec 取不到），而 /skills 仍显示它处于激活状态。
            auto_deactivated = [a.name for a in self._active if a.name not in new_names]
            if auto_deactivated:
                self._active = [a for a in self._active if a.name in new_names]
                for name in auto_deactivated:
                    self._degrades.pop(name, None)

            # 仍存在的激活项保持激活，位置与 arguments 都不动。
            # 正文的更新由 active_text() 每轮从 catalog 现取自动完成（F27），
            # 这里什么都不用做——这正是「ActiveSkill 不存正文」的收益。

        self._trace_state(
            "reload",
            added=sorted(new_names - old_names),
            removed=sorted(old_names - new_names),
            auto_deactivated=sorted(auto_deactivated),
            warning_count=len(warnings),
            active=self._active_snapshot(),
        )

        return ReloadOutcome(
            added=tuple(sorted(new_names - old_names)),
            removed=tuple(sorted(old_names - new_names)),
            auto_deactivated=tuple(sorted(auto_deactivated)),
            dropped_fatal=(),
            warnings=tuple(warnings),
            errors=new_catalog.errors,
            # 作者期体检（1.1）：reload 是作者的**编辑循环**——改文件、reload、
            # 看反馈、再改。体检结果必须出现在这里，否则作者要专门去敲 `/skills`
            # 才看得到，而那时他多半已经以为自己写对了。
            lint=tuple(lint_skills(new_catalog.skills, registered)),
        )

    # ────────────────────── 行为记录埋点（trace）──────────────────────
    #
    # ⚠️ **全部 skill_state 埋点必须在 `self._lock` 临界区之外。**
    # 这不是风格偏好，而是本模块的加锁不变量（见模块 docstring）：临界区内做回调
    # → 回调走 Textual 的阻塞式 `call_from_thread` → 主线程醒来后要申请同一把锁
    # → 双向等待、整个界面冻结。记录器的 emit 本身不回调，但把它放进临界区
    # 就等于给「临界区内只做纯内存读写」这条不变量开了一个口子，下一个人照抄时
    # 很容易把回调也塞进去。宁可结构上不给这个机会。
    #
    # **明确排除 `turn_grants()`**：它每次执行前都被调用，埋进去会把时间线淹掉。
    # 工具集快照只在 `bind_tools`（启动一次）记一次。

    def _trace_state(self, action: str, **extra) -> None:
        """
        记一条 `skill_state` 事件。**只能在锁外调用。**

        :param action: 动作名（activate / deactivate / clear_active / reload / bind_tools）
        :param extra: 该动作特有的字段

        副作用：产出一条 trace 事件；失败被 recorder 内部吞掉。
        """
        self._recorder.emit(
            TraceEventType.SKILL_STATE,
            action=action,
            **extra,
        )

    def _active_snapshot(self) -> list[str]:
        """当前激活列表的名字快照（顺序即注入顺序）。只读引用，不持锁。"""
        return [a.name for a in list(self._active)]

    # ────────────────────────── 激活 ──────────────────────────

    def activate(self, name: str, arguments: str) -> ActivationResult:
        """
        激活一个共享模式 Skill（spec F7/F30）。

        :param name: Skill 名（不带斜杠）
        :param arguments: 用户参数，原样保留
        :returns: 三态 `ActivationResult`

        **严格四段式，只有第 ③ 段持锁**（见模块 docstring 的加锁不变量）：

        副作用：可能修改 `_active`；成功时调用 `notify_activation` 回调。
        """
        # ── ① 解析（锁外）──
        # _catalog 是不可变快照，取引用即可，无需持锁。
        spec = self.get(name)
        if spec is None:
            return ActivationResult(
                status=ActivationStatus.NOT_FOUND,
                name=name,
                available_names=tuple(s.name for s in self._catalog.skills),
            )

        # ── ② 独立模式早返回（锁外）──
        # 本分支根本不动可变状态，本就不需要锁；且它含 has_short_command 这个
        # 跨层回调，按加锁约定 ② 必须在锁外。
        if spec.forked:
            hint = (
                f"/{spec.command_name}"
                if self._has_short_command(spec.command_name)
                else f"/skills run {spec.command_name}"
            )
            return ActivationResult(
                status=ActivationStatus.ISOLATED, name=name, entry_hint=hint
            )

        # ── ③ 状态变更（锁内，且只有这一段）──
        with self._lock:
            for i, item in enumerate(self._active):
                if item.name == name:
                    # 幂等重复激活：只更新参数，**列表位置不动**（F30）。
                    # 位置即注入顺序，重复激活不该改变多个 Skill 的先后关系。
                    self._active[i] = ActiveSkill(name=name, arguments=arguments)
                    break
            else:
                self._active.append(ActiveSkill(name=name, arguments=arguments))
            degrade = self._degrades.get(name)

        # ── ④ 埋点、通知与返回（锁外）──
        self._trace_state(
            "activate",
            skill=name,
            arguments=arguments,
            degrade=degrade.value if degrade is not None else None,
            active=self._active_snapshot(),
        )
        # 回调失败绝不能影响激活结果：激活本身已经成功了，
        # 一次状态栏没刷新不值得把成功报告成失败（何况 _do_stream 的 finally
        # 里还会再刷一次）。
        if self.notify_activation is not None:
            try:
                self.notify_activation()
            except Exception:
                pass

        return ActivationResult(
            status=ActivationStatus.ACTIVATED, name=name, degrade=degrade
        )

    def deactivate(self, name: Optional[str]) -> str:
        """
        卸载已激活的 Skill。

        :param name: 要卸载的名字；**None 表示全部卸载**
        :returns: 供界面显示的结果文本

        副作用：修改 `_active` 与 `_degrades`；产出一条 skill_state 事件。

        结构说明：本方法原先四个 `return` **全在 `with self._lock:` 内**。
        为满足「埋点必须在锁外」的不变量，改成「锁内算出结果 → 出锁 → 埋点 → 返回」。
        返回值语义一字未变。
        """
        with self._lock:
            if name is None:
                count = len(self._active)
                self._active.clear()
                self._degrades.clear()
                removed = count
                message = (
                    "当前没有已激活的 Skill。"
                    if count == 0
                    else f"已卸载全部 {count} 个 Skill。"
                )
            else:
                removed = 0
                message = f"Skill `{name}` 当前未激活。"
                for i, item in enumerate(self._active):
                    if item.name == name:
                        del self._active[i]
                        self._degrades.pop(name, None)
                        removed = 1
                        message = f"已卸载 Skill `{name}`。"
                        break

        self._trace_state(
            "deactivate",
            skill=name,
            removed_count=removed,
            active=self._active_snapshot(),
        )
        return message

    def clear_active(self) -> None:
        """
        清空全部激活态（供 `/clear` 与会话恢复，spec F11/N4）。

        与 `deactivate(None)` 的区别只在于不产出文本——调用方是流程而非用户命令。

        副作用：清空 `_active` 与 `_degrades`；产出一条 skill_state 事件。

        **本方法的埋点不可漏**：`/clear` 与 `/resume` 都调它清激活态，
        而那正是 trace spec F16 要记的「激活列表变化」。
        """
        with self._lock:
            count = len(self._active)
            self._active.clear()
            self._degrades.clear()

        self._trace_state("clear_active", removed_count=count, active=[])

    # ────────────────────── 注入（每轮调用）──────────────────────

    def index_text(self) -> str:
        """
        第一阶段清单文本，进系统提示的**稳定通道**（spec F6）。

        :returns: 清单文本；无任何 Skill 时为空串（槽位随之整体跳过）

        副作用：无。
        """
        return render_index(self._catalog.skills)

    def active_text(self) -> str:
        """
        已激活 Skill 的完整正文段，进系统提示的**动态通道**（spec F9/F10）。

        :returns: 拼接后的注入文本；无激活时为空串

        **分段加锁**（见模块 docstring）：
        持锁取快照 → 出锁渲染（纯函数，可能处理数万字符，不该占着锁）
        → 再持锁写回降级字典。

        正文**每轮从 catalog 现取**，所以热更新改了 SOP 后下一轮自动生效（F27）。

        副作用：刷新 `_degrades`。除此之外无副作用——本方法每轮都被调用，
        任何额外副作用都会被放大 N 倍。
        """
        with self._lock:
            snapshot = list(self._active)
        catalog = self._catalog  # 不可变，锁外取引用安全

        by_name = {s.name: s for s in catalog.skills}
        items: list[tuple[SkillSpec, str]] = []
        for item in snapshot:
            spec = by_name.get(item.name)
            if spec is not None:
                items.append((spec, item.arguments))

        text, degrades = render_active_section(items)

        with self._lock:
            # 只保留仍在 _active 中的名字：两段临界区之间存在窗口，
            # 若期间 deactivate()/reload() 摘掉了某项，无条件整体赋值会给一个
            # 已不在激活列表里的名字留下降级标记，/skills 会显示一条幽灵条目。
            live = {a.name for a in self._active}
            self._degrades = {k: v for k, v in degrades.items() if k in live}

        return text

    def turn_grants(self, extra: "Optional[SkillSpec]" = None) -> tuple[list, list[str]]:
        """
        取本次执行应当授予的预授权规则（对齐改造 F11/F12）。

        :param extra: 本次额外触发的 Skill（`context: fork` 走子对话时它不进激活列表，
                      但它的 `allowed-tools` 同样该生效）
        :returns: `(规则列表, 警告列表)`

        取的是**当前全部激活 Skill** 的声明并集，加上 `extra`。
        一次执行中可能有多个 Skill 先后触发，各自的授权叠加。

        遵守加锁不变量：**持锁只取名字快照，翻译在锁外做**——`grants_for` 要解析
        字符串、构造规则对象，那是纯计算但没有理由占着锁；更重要的是，
        把任何「不只是内存读写」的东西放进临界区，都是在给后来者开一个口子。

        副作用：无。
        """
        with self._lock:
            names = [a.name for a in self._active]

        by_name = {s.command_name: s for s in self._catalog.skills}
        specs = [by_name[n] for n in names if n in by_name]
        if extra is not None:
            specs.append(extra)
        return grants_for(specs)

    def fork_excluded_tools(self) -> frozenset[str]:
        """
        子对话中要排除的工具名（防止 Skill 里再激活 Skill）。

        :returns: 恒为 `{load_skill}`

        **这是 C11 的 `ToolPolicy` 三元组塌缩后唯一留下的用途。**
        排除只是不把 schema 发给模型；模型仍可能凭训练先验硬造出一次调用，
        因此循环层还有一道「调用了本轮未提供的工具就拒绝」的兜底判定——
        两者缺一，嵌套防线就不成立。

        副作用：无。
        """
        return frozenset({LOAD_SKILL_TOOL})

    # ────────────────────────── 查询 ──────────────────────────

    def get(self, name: str) -> Optional[SkillSpec]:
        """
        按名查找 Skill。

        :param name: Skill 名（不带斜杠）
        :returns: 对应的 SkillSpec；不存在时 None

        副作用：无（读不可变快照，无需持锁）。
        """
        for spec in self._catalog.skills:
            if spec.command_name == name:
                return spec
        return None

    def command_infos(self) -> tuple[SkillCommandInfo, ...]:
        """
        产出供命令层构造斜杠短命令的中立描述（spec F25）。

        :returns: 每个 Skill 一条，按名字排序（catalog 已排好）

        返回的是中立结构而不是 `CommandSpec`——这样 skills 包不必认识 commands 包，
        依赖方向保持单向。

        副作用：无。
        """
        return tuple(
            SkillCommandInfo(name=s.name, description=s.description, mode=s.mode)
            for s in self._catalog.skills
        )

    def runtime_warnings(self) -> tuple[str, ...]:
        """
        启动/热更新累积的全部警告（供 `__main__` 在启动时打印）。

        :returns: 警告文本元组

        与 `report()` 里那段的区别只是「不带其它上下文」——启动时还没有 TUI，
        只能往 stderr 打，需要一个干净的列表。

        副作用：无。
        """
        with self._lock:
            return tuple(self._runtime_warnings)

    def project_skill_notice(self) -> Optional[str]:
        """
        项目级 Skill 的启动提示（spec N8 的信任模型告知）。

        :returns: 提示文本；没有项目级 Skill 时 None

        **零状态方案：每次启动都提示，不做「只提示一次」的持久化。**

        理由：项目级 Skill 来自代码仓库，`git pull` 之后可能凭空多出几个——
        它们会自动进入模型的可用清单，其 SOP 正文可以指挥模型读写文件、执行命令。
        如果做「已确认过就不再提示」的持久化状态，新增 Skill 时这个状态不会失效，
        新来的那几个就被静默吞掉了，用户完全不知情。

        每次启动多一行输出，换的是「仓库里多了能指挥模型的东西时你一定会看到」。

        副作用：无。
        """
        project = [s for s in self._catalog.skills if s.source is SkillSource.PROJECT]
        if not project:
            return None
        names = "、".join(s.name for s in project)
        location = (
            str(self._project_root / ".rhinecode" / "skills")
            if self._project_root
            else "项目级 Skill 目录"
        )
        return (
            f"发现 {len(project)} 个项目级 Skill（来自 {location}）：{names}\n"
            f"它们来自当前代码仓库，其指令可以指挥模型读写文件与执行命令，"
            f"请确认它们可信。"
        )

    def report(self, registered: frozenset[str] = frozenset()) -> str:
        """
        `/skills` 的完整只读报告。

        :param registered: 注册中心当前工具名，供作者期体检判断「白名单是否等于全集」。
                           **有缺省值**是为了不打断既有调用点（测试里大量直接调
                           `report()`）；不传时那一条检查自动跳过，其余三条照常。
        :returns: 多行报告文本

        遵守「持锁取快照 → 出锁渲染」：`has_short_command` 是跨层回调，
        必须在锁外调用（加锁约定 ②）。

        体检结果**每次现算**（而不是像 warnings 那样存在 `_runtime_warnings` 里）：
        它是纯函数、成本极低，而存起来就要考虑何时失效——多一处状态就多一处
        「reload 之后忘了更新」的机会。

        副作用：无（纯只读）。
        """
        with self._lock:
            active_map = {a.name: a.arguments for a in self._active}
            degrades = dict(self._degrades)
            warnings = list(self._runtime_warnings)
        catalog = self._catalog

        lines: list[str] = ["Skill 状态", ""]

        if not catalog.skills:
            lines.append("（未发现任何 Skill）")
        else:
            for spec in catalog.skills:
                mode = "子对话" if spec.forked else "主对话"
                flags: list[str] = []
                if spec.command_name in active_map:
                    flags.append("已激活")
                # 锁外调用跨层回调。
                if self._has_short_command(spec.command_name):
                    flags.append(f"短命令 /{spec.command_name}")
                else:
                    flags.append(f"需用 /skills run {spec.command_name}")
                degrade = degrades.get(spec.command_name)
                if degrade is not None:
                    flags.append(_DEGRADE_LABEL[degrade])
                lines.append(
                    f"- {spec.command_name}（{_SOURCE_LABEL[spec.source]} · {mode}）"
                    f"：{spec.description}"
                )
                lines.append(f"    {' · '.join(flags)}")

        if catalog.errors:
            lines.extend(["", "加载失败："])
            for err in catalog.errors:
                lines.append(f"- {err.path}：{err.reason}")

        if warnings:
            lines.extend(["", "警告："])
            lines.extend(f"- {w}" for w in warnings)

        # 作者期体检（1.1）。**单列一段、排在警告之后**：
        # 警告说的是「这次运行发生了什么」，体检说的是「你的文件可以写得更好」，
        # 混排会让两者互相稀释。
        lint = lint_skills(catalog.skills, registered)
        if lint:
            lines.extend(["", "体检建议（不影响运行，但值得改）："])
            lines.extend(f"- {item}" for item in lint)

        # 项目级 Skill 的信任模型告知（spec N8）。**从启动打印挪到了这里**：
        # 启动时 `print()` 发生在 Textual 接管屏幕之前，内容被 alternate screen
        # 盖住，用户直到退出程序才在终端里看到它——等于没提示。放在 `/skills`
        # 里，用户查看 Skill 状态时必定看到，且 catalog 每次 reload 都重算，
        # 仍然没有「确认过就不再提示」的持久化状态（新增的 Skill 不会被吞掉）。
        notice = self.project_skill_notice()
        if notice:
            lines.extend(["", notice])

        return "\n".join(lines)

    def prompt_report(self, registered: frozenset[str]) -> str:
        """
        `/skills prompt` 的报告：当前**实际注入**了什么。

        :param registered: 注册中心当前工具名，用于推导可见工具集
        :returns: 三段报告文本

        这是给用户排查「为什么模型没按我的 Skill 做」用的——直接把
        第一阶段清单、已激活正文、当前可见工具集三样东西摊开。

        副作用：会刷新 `_degrades`（因为调了 `active_text()`）。
        这是可接受的：它与下一轮请求时的刷新结果一致。
        """
        index = self.index_text()
        active = self.active_text()
        rules, grant_warnings = self.turn_grants()

        lines = ["Skill 注入内容", "", "【第一阶段清单（稳定通道）】", ""]
        lines.append(index if index else "（空——未发现任何 Skill）")

        lines.extend(["", "【已激活 Skill 正文（动态通道）】", ""])
        lines.append(active if active else "（空——当前没有已激活的 Skill）")

        # **可见工具集不再随 Skill 变化**（对齐改造 F13）：收窄能力已移除，
        # 模型任何时候都能看到注册中心的全部工具。这一段保留，是因为它仍然回答
        # 「模型这一刻究竟能看到什么」——那是本报告的唯一职责。
        lines.extend(["", "【当前可见工具集】", ""])
        lines.append("、".join(sorted(registered)) if registered else "（无）")
        lines.append(
            "（Skill 不再收窄工具集；Plan Mode 的规划阶段会另外只保留只读工具"
            "并附加 ask_user / present_plan）"
        )

        # 预授权是现在真正影响行为的那样东西，必须在同一份报告里看得到——
        # 否则用户排查「为什么这个操作没弹确认面板」时无从下手。
        lines.extend(["", "【已激活 Skill 授予的免确认操作】", ""])
        if rules:
            lines.extend(
                f"- {r.tool}({r.pattern})" if r.pattern else f"- {r.tool}"
                for r in rules
            )
            lines.append("（这些操作在本次执行内免于人工确认，用户下一条消息后失效）")
        else:
            lines.append("（无——所有有副作用的操作都会照常弹确认面板）")
        if grant_warnings:
            lines.append("")
            lines.extend(f"⚠ {w}" for w in grant_warnings)

        return "\n".join(lines)

    def status_segment(self) -> Optional[str]:
        """
        底部状态栏的 Skill 段（spec F32）。

        :returns: 形如 `Skill:2`；没有激活时 **None**（状态栏随之隐藏该段）

        **不含方括号**是刻意的：Textual 会把 `[...]` 当作 markup 标签吞掉，
        既有代码里凡是含字面量 `[` 的状态栏文本都得转义成 `\\[`。
        直接不用方括号就绕开了这个坑。

        副作用：无。
        """
        with self._lock:
            count = len(self._active)
        return f"Skill:{count}" if count else None
