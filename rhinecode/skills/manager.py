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
    SkillMode,
    SkillSource,
    SkillSpec,
)
from rhinecode.skills.render import render_active_section, render_index
from rhinecode.skills.validation import (
    FatalToolName,
    check_builtin_tool_names,
    collect_exempt_notices,
    prune_mcp_tool_names,
)
from rhinecode.tools.policy import ToolPolicy

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
    此后每轮请求调 `index_text()` / `active_text()` / `tool_policy()`。
    """

    def __init__(
        self,
        project_root: Optional[Path],
        user_dir: Optional[Path],
        builtin_dir: Optional[Path],
        has_short_command: Callable[[str], bool],
        notify_activation: Optional[Callable[[], None]] = None,
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

        本构造函数**不扫盘**——扫盘发生在 `startup()`。这样构造是廉价且无副作用的。
        """
        self._project_root = project_root
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

    def startup(self, known: frozenset[str]) -> list[FatalToolName]:
        """
        启动时扫盘并做第一段白名单校验（spec F16 第一段）。

        :param known: 已知工具名全集
                      （注册中心当前全部名字 ∪ `{ask_user, present_plan}`）
        :returns: 致命项列表。**本方法不自行退出进程**——是否 fail-fast 由
                  `__main__` 决定，因为只有它知道此刻该做哪些清理

        副作用：读三层目录（扫盘在锁外）、替换 `_catalog`、追加 `_runtime_warnings`。
        """
        # 扫盘在锁外：它要读几十个文件，不该占着锁。
        catalog = discover(self._project_root, self._user_dir, self._builtin_dir)

        fatals = check_builtin_tool_names(catalog.skills, known)
        notices = collect_exempt_notices(catalog.skills, _EXEMPT_TOOLS)

        with self._lock:
            self._catalog = catalog
            self._runtime_warnings = list(catalog.warnings) + notices

        return fatals

    def bind_tools(self, registered: frozenset[str]) -> None:
        """
        MCP 连接完成后做第二段白名单剪枝（spec F16 第二段 / F17）。

        :param registered: 连接完成后注册中心的全部工具名

        副作用：替换 `_catalog`（其中部分 spec 的 `allowed_tools` 被改写）、
        追加 `_runtime_warnings`。
        """
        catalog = self._catalog  # 不可变快照，取引用即可
        pruned, warnings = prune_mcp_tool_names(catalog.skills, registered)

        with self._lock:
            self._catalog = SkillCatalog(
                skills=tuple(pruned),
                errors=catalog.errors,
                warnings=catalog.warnings,
            )
            self._runtime_warnings.extend(warnings)

    def reload(
        self, known: frozenset[str], registered: frozenset[str]
    ) -> ReloadOutcome:
        """
        热更新：重新扫盘、重跑两段校验、同步激活列表（spec F26/F27）。

        :param known: 同 `startup`
        :param registered: 同 `bind_tools`
        :returns: `ReloadOutcome`，由上层渲染成可读报告

        **与 `startup` 只差一处，但这一处很关键**：第一段校验的致命项在这里
        **不终止进程**，而是把对应 Skill 从新 catalog 中丢弃并记一条警告。

        依据：F16 与 N2 的定语都锚定「**启动**时 fail-fast」；F26 只要求热更新时
        「重跑白名单校验」；F31 明确热更新不影响运行中的循环。用户正在进行的会话，
        不该因为顺手改了个 Skill 文件、打错一个工具名就被杀掉。

        **连带告知义务**：警告文案必须写明「下次启动时此错误会导致启动失败」。
        否则用户会把它当成一条无关紧要的提示，第二天启动不来还完全想不起来
        昨天见过这句话。

        副作用：读三层目录、替换 `_catalog`、重置 `_runtime_warnings`、
        可能移除 `_active` 中的条目。
        """
        catalog = discover(self._project_root, self._user_dir, self._builtin_dir)

        # 第一段校验：致命项对应的 Skill 被丢弃，而不是退出进程。
        fatals = check_builtin_tool_names(catalog.skills, known)
        fatal_names = {f.skill_name for f in fatals}
        warnings: list[str] = list(catalog.warnings)
        for f in fatals:
            warnings.append(
                f"Skill `{f.skill_name}`（{f.path}）的白名单含不存在的工具名 "
                f"`{f.tool_name}`，该 Skill 已被本次热更新丢弃；"
                f"**下次启动时此错误会导致启动失败**，请尽快修正"
            )
        surviving = [s for s in catalog.skills if s.name not in fatal_names]

        warnings.extend(collect_exempt_notices(surviving, _EXEMPT_TOOLS))

        # 第二段：MCP 剪枝。
        pruned, prune_warnings = prune_mcp_tool_names(surviving, registered)
        warnings.extend(prune_warnings)

        new_catalog = SkillCatalog(
            skills=tuple(pruned), errors=catalog.errors, warnings=catalog.warnings
        )

        old_names = {s.name for s in self._catalog.skills}
        new_names = {s.name for s in new_catalog.skills}

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

        return ReloadOutcome(
            added=tuple(sorted(new_names - old_names)),
            removed=tuple(sorted(old_names - new_names)),
            auto_deactivated=tuple(sorted(auto_deactivated)),
            dropped_fatal=tuple(sorted(fatal_names)),
            warnings=tuple(warnings),
            errors=new_catalog.errors,
        )

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
        if spec.mode is SkillMode.ISOLATED:
            hint = (
                f"/{spec.name}"
                if self._has_short_command(spec.name)
                else f"/skills run {spec.name}"
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

        # ── ④ 通知与返回（锁外）──
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

        副作用：修改 `_active` 与 `_degrades`。
        """
        with self._lock:
            if name is None:
                count = len(self._active)
                self._active.clear()
                self._degrades.clear()
                if count == 0:
                    return "当前没有已激活的 Skill。"
                return f"已卸载全部 {count} 个 Skill。"

            for i, item in enumerate(self._active):
                if item.name == name:
                    del self._active[i]
                    self._degrades.pop(name, None)
                    return f"已卸载 Skill `{name}`。"
            return f"Skill `{name}` 当前未激活。"

    def clear_active(self) -> None:
        """
        清空全部激活态（供 `/clear` 与会话恢复，spec F11/N4）。

        与 `deactivate(None)` 的区别只在于不产出文本——调用方是流程而非用户命令。

        副作用：清空 `_active` 与 `_degrades`。
        """
        with self._lock:
            self._active.clear()
            self._degrades.clear()

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

    def tool_policy(self, registered: frozenset[str]) -> ToolPolicy:
        """
        主对话的工具收窄策略（spec F14/F15/F17）。

        :param registered: 注册中心**当前**的全部工具名
        :returns: 本轮生效的 `ToolPolicy`

        四个分支，其中三个都是「不收窄」：

        1. 没有激活任何 Skill → 不收窄（等价于 C10 行为，N3 零回归）；
        2. 任一激活 Skill 未声明白名单 → **整体塌缩为不收窄**。
           理由：白名单取并集，一个「不限制」与任何集合取并都是「不限制」。
           这不是妥协，是并集语义的必然结果（N10 已说明白名单不是安全边界，
           塌缩不带来安全问题）；
        3. 否则取各白名单并集，**再与 `registered` 取交集**——这就是「运行期自愈」：
           MCP 运行时重载让某个远端工具消失后，它自动不再出现在可见集里，
           不需要任何额外的同步机制；
        4. 交集为空 → 不收窄（F17，理由同 `prune_mcp_tool_names` 的降级）。

        `exempt` 恒含 `load_skill`：否则模型激活第一个 Skill 之后就再也
        加载不了第二个了。

        副作用：无。
        """
        with self._lock:
            names = [a.name for a in self._active]

        if not names:
            return ToolPolicy(None, _EXEMPT_TOOLS, frozenset())

        by_name = {s.name: s for s in self._catalog.skills}
        union: set[str] = set()
        for name in names:
            spec = by_name.get(name)
            if spec is None:
                continue
            if spec.allowed_tools is None:
                # 塌缩：并集里出现「不限制」，整体就是不限制。
                return ToolPolicy(None, _EXEMPT_TOOLS, frozenset())
            union.update(spec.allowed_tools)

        allowed = union & set(registered)
        if not allowed:
            return ToolPolicy(None, _EXEMPT_TOOLS, frozenset())
        return ToolPolicy(frozenset(allowed), _EXEMPT_TOOLS, frozenset())

    def isolated_policy(
        self, spec: SkillSpec, registered: frozenset[str]
    ) -> ToolPolicy:
        """
        独立模式子对话的工具收窄策略（spec F21/F23）。

        :param spec: 本次执行的 Skill
        :param registered: 注册中心当前的全部工具名
        :returns: 子对话专用的 `ToolPolicy`

        与 `tool_policy` 的两处不同：

        1. **只看这一个 spec**——子对话是为这个 Skill 开的，主对话里激活的
           其它 Skill 与它无关；
        2. **`excluded` 含 `load_skill`、`exempt` 为空**——禁止子对话再嵌套激活
           Skill（F23）。嵌套激活会让「独立」这个语义失去意义，也让上下文预算
           完全不可控。

        副作用：无。
        """
        excluded = frozenset({LOAD_SKILL_TOOL})
        if spec.allowed_tools is None:
            return ToolPolicy(None, frozenset(), excluded)
        allowed = set(spec.allowed_tools) & set(registered)
        if not allowed:
            return ToolPolicy(None, frozenset(), excluded)
        return ToolPolicy(frozenset(allowed), frozenset(), excluded)

    # ────────────────────────── 查询 ──────────────────────────

    def get(self, name: str) -> Optional[SkillSpec]:
        """
        按名查找 Skill。

        :param name: Skill 名（不带斜杠）
        :returns: 对应的 SkillSpec；不存在时 None

        副作用：无（读不可变快照，无需持锁）。
        """
        for spec in self._catalog.skills:
            if spec.name == name:
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

    def report(self) -> str:
        """
        `/skills` 的完整只读报告。

        :returns: 多行报告文本

        遵守「持锁取快照 → 出锁渲染」：`has_short_command` 是跨层回调，
        必须在锁外调用（加锁约定 ②）。

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
                mode = "共享" if spec.mode is SkillMode.SHARED else "独立"
                flags: list[str] = []
                if spec.name in active_map:
                    flags.append("已激活")
                # 锁外调用跨层回调。
                if self._has_short_command(spec.name):
                    flags.append(f"短命令 /{spec.name}")
                else:
                    flags.append(f"需用 /skills run {spec.name}")
                degrade = degrades.get(spec.name)
                if degrade is not None:
                    flags.append(_DEGRADE_LABEL[degrade])
                lines.append(
                    f"- {spec.name}（{_SOURCE_LABEL[spec.source]} · {mode}）"
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
        policy = self.tool_policy(registered)

        lines = ["Skill 注入内容", "", "【第一阶段清单（稳定通道）】", ""]
        lines.append(index if index else "（空——未发现任何 Skill）")

        lines.extend(["", "【已激活 Skill 正文（动态通道）】", ""])
        lines.append(active if active else "（空——当前没有已激活的 Skill）")

        lines.extend(["", "【当前可见工具集】", ""])
        if policy.allowed is None:
            lines.append("未收窄（全部已注册工具对模型可见）")
        else:
            visible = sorted(policy.allowed | policy.exempt)
            lines.append("、".join(visible))

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
