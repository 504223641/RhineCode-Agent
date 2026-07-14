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
    :ivar mode: 当前权限模式（默认 DEFAULT，可经 /perm 运行时切换）
    :ivar load_errors: 配置加载阶段收集的可读错误（供上层提示，不阻断启动）
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
        self.mode = mode
        self.load_errors: list[str] = load_errors or []

    @classmethod
    def load(cls, mode: PermissionMode = PermissionMode.DEFAULT) -> "PermissionEngine":
        """
        从三层 YAML 配置构建引擎（启动时调用）。

        委托 config.load_all() 读取并合并用户/项目/本地三层规则；加载错误一并带入实例，
        由上层决定如何提示（fail-safe：即便配置有错也照常返回可用引擎，spec N1/F9）。

        :param mode: 启动权限模式
        :returns: 已加载规则的 PermissionEngine

        副作用：读取三层配置文件（若存在）。
        """
        ruleset, errors = config.load_all()
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

        # ③ 规则：会话级规则在前、文件级在后合并；deny 优先求值。
        merged = RuleSet(self.session_rules + self.file_ruleset.rules)
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
