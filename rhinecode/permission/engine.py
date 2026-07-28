"""
PermissionEngine（spec F1）：把五层防御组装成一条决策管线，是权限系统的中枢。

它持有可变运行状态（当前权限模式、会话级临时规则），并暴露唯一的判定入口
decide(request)。decide 是纯判定——给定请求与当前状态即可复现结果，不读模型输出、
不依赖 TUI，因此能被纯单测覆盖（spec N2/N5）。

四层短路顺序（第一个能下定论的层即返回，spec 决策管线）：
    ① 黑名单（仅命令类）   命中→DENY，不可被任何配置/模式放开
    ② 沙箱（仅路径/glob）  越界→DENY
    ③ 规则（deny 优先）    会话级规则并入文件级规则统一求值；命中→ALLOW/DENY
       └ 只读简化分支     ③未命中且是只读工具→ALLOW（不进④，spec F7）
    ④ 模式兜底           仅副作用工具、③未命中：严格→DENY / 默认→ASK / 放行→ALLOW
"""

from pathlib import Path
from typing import Optional

from rhinecode.permission import blacklist, config
from rhinecode.permission.models import (
    Decision,
    DecisionResult,
    Layer,
    PermissionMode,
    PermissionRequest,
    Rule,
)
from rhinecode.permission.rules import RuleSet
from rhinecode.tools.path_guard import is_readable_path, is_within_workspace


class PermissionEngine:
    """
    决策管线引擎。每个会话持有一个实例（由 ConversationManager 构建并透传给 Agent）。

    :ivar file_ruleset: 启动时从三层 YAML 加载的规则集（本会话内不变）
    :ivar session_rules: 会话级临时规则（「本会话放行」登记于此，关程序即失效）
    :ivar turn_rules: **本次执行**级临时规则（Skill 的 `allowed-tools` 预授权登记于此）。
        与 `session_rules` 同型、同求值逻辑，只是命更短——用户发出下一条消息即清空。
    :ivar mode: 当前权限模式（默认 DEFAULT，可经 /perm 运行时切换）
    :ivar load_errors: 配置加载阶段收集的可读错误（供上层提示，不阻断启动）

    ## 三级规则的优先级

        turn_rules（本次执行）→ session_rules（本会话）→ file_ruleset（三层配置）

    三者合并后仍由 `RuleSet.evaluate` 统一求值，**deny 依旧优先**。

    预授权之所以做成「第③层的又一级规则」而不是新造一层，是因为它在语义上
    **恰好等价于**用户在确认面板上选「本会话放行」，只是有效期更短。复用同一套
    求值逻辑之后，「预授权不得绕过黑名单与路径沙箱」这条自动成立——
    那两层排在第③层**之前**，压根轮不到规则说话。
    """

    def __init__(
        self,
        file_ruleset: RuleSet,
        mode: PermissionMode = PermissionMode.DEFAULT,
        load_errors: list[str] | None = None,
    ):
        """
        :param file_ruleset: 三层文件规则合并后的 RuleSet
        :param mode: 启动权限模式，默认「默认」档（spec F5）
        :param load_errors: 配置加载错误列表（可为 None）
        """
        self.file_ruleset = file_ruleset
        self.session_rules: list[Rule] = []
        self.turn_rules: list[Rule] = []
        self.mode = mode
        self.load_errors: list[str] = load_errors or []

    @classmethod
    def load(
        cls,
        mode: PermissionMode = PermissionMode.DEFAULT,
        user_dir: Optional[Path] = None,
    ) -> "PermissionEngine":
        """
        从三层 YAML 配置构建引擎（启动时调用）。

        委托 config.load_all() 读取并合并用户/项目/本地三层规则；加载错误一并带入实例，
        由上层决定如何提示（fail-safe：即便配置有错也照常返回可用引擎，spec N1/F9）。

        :param mode: 启动权限模式
        :param user_dir: 用户级目录，透传给 `config.load_all()`。**必须可选**——
                         缺省等于现状（读真实主目录）。给定时用户级权限规则改从该目录读，
                         使装配层能把整套用户级内容重定向到临时目录（trace spec F23）。
        :returns: 已加载规则的 PermissionEngine

        副作用：读取三层配置文件（若存在）。
        """
        ruleset, errors = config.load_all(user_dir)
        return cls(ruleset, mode=mode, load_errors=errors)

    def decide(self, request: PermissionRequest) -> DecisionResult:
        """
        对一次权限请求跑完四层决策管线，返回最终决定。

        :param request: 规范化后的权限请求（由 adapter.to_request 产出）
        :returns: DecisionResult（decision + 命中 layer + 中文 reason）

        副作用：无（纯判定）。
        """
        # ① 黑名单：仅命令类；命中即拒，且这层不可被③④放开（circuit breaker）。
        if request.kind == "command":
            reason = blacklist.check_command(request.specifier)
            if reason:
                return DecisionResult(Decision.DENY, Layer.BLACKLIST, "命中危险命令黑名单：" + reason)

        # ② 沙箱：仅路径/glob 类；越界即拒。复用 path_guard 的边界判定。
        # read 类走「工作区 ∪ 只读白名单」（c9 放行用户级记忆目录的只读访问，F18）；
        # write / glob 类仍严格限定工作区内，白名单对它们完全不可见（N6③）。
        if request.kind == "read_path":
            if not is_readable_path(request.specifier):
                return DecisionResult(
                    Decision.DENY, Layer.SANDBOX, f"路径越界，超出项目工作目录：{request.specifier}"
                )
        elif request.kind in ("write_path", "glob"):
            if not is_within_workspace(request.specifier):
                return DecisionResult(
                    Decision.DENY, Layer.SANDBOX, f"路径越界，超出项目工作目录：{request.specifier}"
                )

        # ③ 规则：本次执行级 → 会话级 → 文件级，依次合并；deny 优先求值。
        #
        # ⚠️ 本层位于①黑名单与②沙箱**之后**，这个顺序是预授权安全性的全部依据：
        # 一个声明「放行全部命令」的 Skill 也翻不过前两层。改动合并点位置之前，
        # 先看 `tests/test_perm_turn_grant.py` 里那两条护栏。
        merged = RuleSet(
            self.turn_rules + self.session_rules + self.file_ruleset.rules
        )
        hit = merged.evaluate(request)
        if hit is not None:
            return hit

        # 只读简化分支（spec F7）：③未命中 → 只读工具直接放行，不进④模式层。
        if request.is_read_only:
            return DecisionResult(Decision.ALLOW, Layer.RULE, "只读工具默认放行")

        # ④ 模式兜底：仅副作用工具、③未命中时。
        if request.mode is PermissionMode.STRICT:
            return DecisionResult(Decision.DENY, Layer.MODE, "严格模式：无规则放行，默认拒绝")
        if request.mode is PermissionMode.PERMISSIVE:
            return DecisionResult(Decision.ALLOW, Layer.MODE, "放行模式：无规则命中，默认允许")
        return DecisionResult(Decision.ASK, Layer.MODE, "默认模式：无规则命中，交由用户确认")

    # ------------------------------------------------------------------ #
    # 可变状态操作
    # ------------------------------------------------------------------ #
    def set_mode(self, mode: PermissionMode) -> None:
        """切换当前权限模式（/perm 命令调用）。"""
        self.mode = mode

    def add_session_rule(self, rule: Rule) -> None:
        """登记一条会话级规则（「本会话放行」调用）；仅存内存，关程序即失效。"""
        self.session_rules.append(rule)

    def grant_turn_rules(self, rules: "list[Rule]") -> int:
        """
        授予一批**本次执行内有效**的规则（Skill 的 `allowed-tools` 预授权）。

        :param rules: 待追加的规则，通常 `effect="allow"`、`source="skill"`
        :returns: 一个**令牌**，交给 `restore_turn_rules` 回滚到本次授予之前的状态

        追加而非替换：一次执行中可能有多个 Skill 先后被触发（用户敲了短命令，
        模型又自行加载了另一个），各自的授权应当叠加。

        ## 为什么返回令牌而不是配一个「整体清空」

        **授权会嵌套**：模型在主对话里自行发起一个 `context: fork` 的 Skill 时，
        外层那次执行已经授过权，子对话又要为自己授一次。若撤销是「整体清空」，
        子对话结束时会顺手把**外层的授权也清掉**——外层剩下的轮次突然开始弹
        它本不该弹的确认面板，而这种偏差在界面上看不出任何异常。

        令牌即「授予前的长度」，回滚只截掉自己那一段，天然可嵌套。

        副作用：修改 `turn_rules`。**调用方必须把 `restore_turn_rules` 放进
        `finally`**——否则一次异常终止就会让授权泄漏到下一次执行。
        """
        token = len(self.turn_rules)
        self.turn_rules.extend(rules)
        return token

    def restore_turn_rules(self, token: int) -> None:
        """
        回滚到 `token` 对应的状态，即撤销该次 `grant_turn_rules` 之后追加的全部规则。

        :param token: `grant_turn_rules` 返回的令牌

        **幂等**：重复调用只会反复截到同一长度。这一点很重要——异常路径上
        「已经回滚过但又走了一次 finally」是完全可能的，那时不该再出事。

        副作用：截短 `turn_rules`。
        """
        del self.turn_rules[token:]

    def revoke_turn_rules(self) -> None:
        """
        清空全部本次执行级规则。

        这是**最外层的兜底**，等价于 `restore_turn_rules(0)`。会话被清空
        （`/clear`）或恢复（`/resume`）时用它，确保不把上一段对话的授权带过去。

        副作用：清空 `turn_rules`。
        """
        self.turn_rules.clear()

    def persist_local_rule(self, rule_string: str) -> bool:
        """
        永久放行：把一条 allow 规则写入本地级配置，并同步登记为会话规则使其本次立即生效。

        :param rule_string: 形如 "Bash(git *)" 的规则字符串
        :returns: 是否成功写入本地级配置

        副作用：写入本地级 permissions.local.yaml；向 session_rules 追加一条等价规则。
        """
        try:
            config.append_local_allow(rule_string)
        except Exception as exc:  # noqa: BLE001 - UI should not crash if local permission write fails.
            self.load_errors.append(f"写入本地权限配置失败：{exc}")
            return False
        rule = config.parse_rule_string(rule_string, "allow", "local")
        if rule is not None:
            self.session_rules.append(rule)
        return True
