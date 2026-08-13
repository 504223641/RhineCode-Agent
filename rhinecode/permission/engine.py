"""
PermissionEngine（spec F1）：把五层防御组装成一条决策管线，是权限系统的中枢。

它持有可变运行状态（当前权限模式、会话级临时规则），并暴露唯一的判定入口
decide(request)。decide 是纯判定——给定请求与当前状态即可复现结果，不读模型输出、
不依赖 TUI，因此能被纯单测覆盖（spec N2/N5）。

短路顺序（第一个能下定论的层即返回，spec 决策管线）：
    ① 黑名单（仅命令类）   命中→DENY，不可被任何配置/模式放开
    ② 沙箱（仅路径/glob）  越界→DENY
    ②′ 网络边界（仅 URL）  硬校验命中→DENY（不可放开）；域名规则求值；
                           白名单已建立但未命中→DENY（web_fetch 扩展 F5/F6）
    ③ 规则（deny 优先）    会话级规则并入文件级规则统一求值；命中→ALLOW/DENY
       └ 只读简化分支     ③未命中且是只读工具→ALLOW（不进④，spec F7）
    ④ 模式兜底           仅副作用工具、③未命中：严格→DENY / 默认→ASK / 放行→ALLOW
                           **URL 类例外：放行档降级为 ASK**（web_fetch 扩展 F7）

## ②″保护路径：一个**出口处的收紧器**，不在上面那条短路序列里

    _decide_core(request)  ← 上面五层，一字不动
            ↓
    _apply_protected(request, result)  ← 只把**非 DENY** 的结论升级为 ASK

判定表（这就是全部内容）：

| 条件 | 结论 |
| --- | --- |
| `kind != "write_path"` | 原样返回 |
| 结论是 DENY | **原样返回**（绝不降级） |
| 未命中保护路径 | 原样返回 |
| 命中但已被本会话豁免 | 原样返回，置 `protected_exempt=True` |
| 命中未豁免 | `ASK @ Layer.PROTECTED` |

**为什么不做成「②之后③之前」的一站**（protected-paths spec 分歧一）：那样会把
③层的 `deny: Write(.rhinecode/hooks.yaml)` 与④层严格档的 DENY 一起短路吞掉，
两处都是**放宽**。「必须排在③之前」这句话的实质是「本层的升级效力不被③层的
allow 规则消解」，而不是「代码位置在③上面」。语义与 C12 的 Hook ASK 同型。

好处还有一条：`_decide_core` 是既有 body 的原样改名，因此「其余种类的请求逐字
不变」是**结构性成立**的，不靠测试兜。
"""

from pathlib import Path
from typing import Optional

from rhinecode.permission import blacklist, config, network, protected
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

# ── 权限模式的「严格程度」排序（c13）──
#
# 数字越小越严。存在的理由是 c13 的 F16：子 Agent 的实际生效档位取
# min(主对话档位, 角色声明档位)——角色可以把自己限得更严，但**声明放行档不产生
# 任何提权效果**。
#
# 为什么不给 `PermissionMode` 加序号或让它继承 `IntEnum`：那会让「档位可比较」
# 变成枚举本身的性质，而它在别处（配置解析、界面展示、trace 负载）只是个标识串。
# 单独放一张表，比较语义就只在需要它的这一处成立。
_MODE_ORDER = {
    PermissionMode.STRICT: 0,
    PermissionMode.DEFAULT: 1,
    PermissionMode.PERMISSIVE: 2,
}


def narrower_mode(a: PermissionMode, b: PermissionMode) -> PermissionMode:
    """
    取两个权限模式中**更严**的那一档（c13 spec F16）。

    :param a: 其一（通常是主对话当前档位）
    :param b: 其二（通常是角色 frontmatter 声明的档位）
    :returns: 更严的那一档；两者相同则返回该档

    严格程度：STRICT < DEFAULT < PERMISSIVE。

    这是「子 Agent 只能收紧不能放宽」这条安全承诺的**全部实现**——
    与 C12「Hook 只能收紧不能放宽」是同一条原则。有了它，
    `.rhinecode/agents/` 里一个随代码仓库分发的角色文件**无法自行提权**：
    它写 `permission_mode: permissive`，在默认档的主对话下实际拿到的仍是默认档。

    副作用：无（纯函数）。
    """
    return a if _MODE_ORDER[a] <= _MODE_ORDER[b] else b


class PermissionEngine:
    """
    决策管线引擎。每个会话持有一个实例（由 ConversationManager 构建并透传给 Agent）。

    :ivar file_ruleset: 启动时从三层 YAML 加载的规则集（本会话内不变）
    :ivar session_rules: 会话级临时规则（「本会话放行」登记于此，关程序即失效）
    :ivar turn_rules: **本次执行**级临时规则（Skill 的 `allowed-tools` 预授权登记于此）。
        与 `session_rules` 同型、同求值逻辑，只是命更短——用户发出下一条消息即清空。
    :ivar mode: 当前权限模式（默认 DEFAULT，可经 /perm 运行时切换）
    :ivar load_errors: 配置加载阶段收集的可读错误（供上层提示，不阻断启动）
    :ivar protected_exemptions: ②″保护路径的**会话级豁免**（protected-paths 扩展）。
        存的是**解析后的绝对路径**，精确到单个文件、不扩展到目录。

        ⚠ **它刻意不是一条③层规则、也刻意不落盘。** 保护路径的升级效力不被③层
        消解，所以写成③层规则的话那条规则永远不会被求值——用户会看到「点了永久
        放行，下次还是弹」，那比不做还糟；而落盘的豁免本身就是一份「能改变以后
        会发生什么」的配置，绕一圈又回到本扩展要解决的原问题。

        生命周期与 `session_rules` 一致：关程序即失效，不随 /clear、/resume 清空。

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
        policy_ruleset: RuleSet | None = None,
    ):
        """
        :param file_ruleset: 三层文件规则合并后的 RuleSet
        :param mode: 启动权限模式，默认「默认」档（spec F5）
        :param load_errors: 配置加载错误列表（可为 None）
        :param policy_ruleset: **仅用户级 + 项目级**的规则集，用于判断「域名白名单
            是否已被建立」（web_fetch 扩展 spec F6a）。**必须可选**——缺省为空规则集，
            使既有构造点（大量测试直接 `PermissionEngine(RuleSet([...]))`）不改也能跑。

            缺省为空的语义是安全的：白名单未建立 → ②′层不下结论 → 交由模式兜底，
            与本扩展之前的行为一致。
        """
        self.file_ruleset = file_ruleset
        self.policy_ruleset = policy_ruleset if policy_ruleset is not None else RuleSet([])
        self.session_rules: list[Rule] = []
        self.turn_rules: list[Rule] = []
        self.mode = mode
        self.load_errors: list[str] = load_errors or []
        self.protected_exemptions: set[Path] = set()

    def derive(self, mode: PermissionMode) -> "PermissionEngine":
        """
        派生一个供**子 Agent** 使用的引擎视图（c13 spec F16/F17）。

        :param mode: 派生实例的权限模式，调用方应当已用 `narrower_mode` 与主档取过更严的一档
        :returns: 新的 `PermissionEngine` 实例

        共享与独立的分界（这是本方法的全部内容，逐条都有理由）：

        | 成员 | 处理 | 理由 |
        | --- | --- | --- |
        | `file_ruleset` / `policy_ruleset` | **共享同一对象** | 配置在本会话内不变，复制没有意义 |
        | `session_rules` | **共享同一个列表对象** | 用户在主对话确认面板上选的「本会话放行」是**明确授予**，理应对子 Agent 生效 |
        | `turn_rules` | **全新空列表** | 回合级预授权（Skill 的 `allowed-tools`）绑在某一次执行上，不该跟着委派跑出去（spec F17） |
        | `mode` | 取参数 | 见下 |
        | `load_errors` | 共享 | 只读，供报告展示 |
        | `protected_exemptions` | **共享同一个集合对象** | 与 `session_rules` 同口径：用户在确认面板上对某个文件的明确授予，理应对子 Agent 也生效。共享后子 Agent 拿到的豁免仍不多于主对话，C13「能力只会更小」原样成立 |

        ## 为什么必须派生，而不是直接改 `self.mode`

        引擎是**单实例共享**的：协调层构造一个，主对话与全部子 Agent 都拿它做判定。
        而 `mode` 与 `turn_rules` 都是**可变字段**。子 Agent 若为了落实自己的档位
        去写 `engine.mode`，改掉的是**主对话的档位**——而子 Agent 跑在后台线程里，
        这意味着用户正在对话的主循环的权限档位被一个后台线程静默改掉了，
        **界面上完全看不出来**，等到下一次弹（或不弹）确认面板时才显形。

        ## 调用方约定

        派生实例是**只读视图**：不得在它上面调 `grant_turn_rules` /
        `register_session_rule` 之类的登记方法。子 Agent 不弹确认面板（spec F15），
        因此本来也没有登记的入口——这条约定是给将来改动的人看的。

        副作用：无（只构造新对象，不修改 self）。
        """
        derived = PermissionEngine(
            self.file_ruleset,
            mode,
            self.load_errors,
            self.policy_ruleset,
        )
        # 构造函数会给 session_rules / turn_rules 各建一个新空列表，这里按上表覆写：
        # session_rules 换成同一个对象（共享），turn_rules 保持构造函数给的新空列表。
        derived.session_rules = self.session_rules
        # load_errors 也必须显式赋值：构造函数写的是 `load_errors or []`，
        # 传进去的若是**空列表**（最常见的情况——没有加载错误），`or` 会取右边
        # 新建一个，共享就静默地不成立了。显式赋值让行为不依赖这个偶然性。
        derived.load_errors = self.load_errors
        # ⚠ 与 `load_errors` 同一个坑：构造函数会给它新建一个空 `set()`，
        # 不显式赋值的话共享**静默地不成立**——表现为「用户在主对话面板上放行过的
        # 文件，子 Agent 仍然写不了」，而两边配置看起来一模一样。
        derived.protected_exemptions = self.protected_exemptions
        return derived

    @classmethod
    def load(
        cls,
        mode: PermissionMode = PermissionMode.DEFAULT,
        user_dir: Optional[Path] = None,
        *,
        web_fetch_enabled: bool = True,
    ) -> "PermissionEngine":
        """
        从三层 YAML 配置构建引擎（启动时调用）。

        委托 config.load_all() 读取并合并用户/项目/本地三层规则；加载错误一并带入实例，
        由上层决定如何提示（fail-safe：即便配置有错也照常返回可用引擎，spec N1/F9）。

        :param mode: 启动权限模式
        :param user_dir: 用户级目录，透传给 `config.load_all()`。**必须可选**——
                         缺省等于现状（读真实主目录）。给定时用户级权限规则改从该目录读，
                         使装配层能把整套用户级内容重定向到临时目录（trace spec F23）。
        :param web_fetch_enabled: 网络访问能力是否启用。关闭时跳过 WebFetch 的域名语法校验，
                         使「关闭后行为与本扩展之前逐字一致」成立（web_fetch 扩展 spec F4）。
                         由 `ConversationManager` 从 `Config.web_fetch_enabled` 传入。
        :returns: 已加载规则的 PermissionEngine

        副作用：读取三层配置文件（若存在）。
        """
        ruleset, policy_ruleset, errors = config.load_all(
            user_dir, web_fetch_enabled=web_fetch_enabled
        )
        return cls(ruleset, mode=mode, load_errors=errors, policy_ruleset=policy_ruleset)

    def decide(self, request: PermissionRequest) -> DecisionResult:
        """
        对一次权限请求跑完决策管线，返回最终决定。**这是全系统唯一的判定入口。**

        :param request: 规范化后的权限请求（由 adapter.to_request 产出）
        :returns: DecisionResult（decision + 命中 layer + 中文 reason + kind/host）

        两段：既有五层（`_decide_core`）→ ②″保护路径收紧器（`_apply_protected`）。
        收紧器只会把结论变严，永远不会变松，因此既有的全部安全论证原样成立。

        副作用：无（纯判定）。
        """
        return self._apply_protected(request, self._decide_core(request))

    def _apply_protected(
        self, request: PermissionRequest, result: DecisionResult
    ) -> DecisionResult:
        """
        ②″保护路径收紧器（protected-paths 扩展）：写入配置类文件必须过人眼。

        :param request: 本次权限请求
        :param result: 既有五层给出的结论
        :returns: 收紧后的结论；不适用时**原样返回传入的 result**

        判定表见模块 docstring。三处要点，每一处都对应一种「写错了不报错」的形态：

        ⚠ **① DENY 一律原样返回，绝不降级。** 这是本层「只收紧不放宽」的全部落点：
        用户写下的 `deny: Write(.rhinecode/hooks.yaml)` 与严格档的兜底 DENY 都必须
        原样保留。把它们降级成「问一下」是放宽，与本层目标正相反。

        ⚠ **② 结论「非 DENY」就要换层，不能只处理 ALLOW。** 默认档下写 `hooks.yaml`
        的既有结论是 `ASK @ Layer.MODE`。那一支若原样返回，确认面板看到的层是
        `mode`，就会照常显示**四个**选项、含「永久放行」——用户点下去写出一条③层
        allow 规则，而下一次那条规则又会被本层升级回 ASK。
        **骗人的按钮原样存在，只是换了个入口。**

        ⚠ **③ 本方法是 `DecisionResult` 的第二个构造出口，必须显式填 `kind` / `host`。**
        `_decide_core` 里的 `_verdict` 闭包管不到这里。漏填的后果是确认面板的
        URL 专用分支永不进入——虽然本层只对 write_path 生效、当前不会触发，
        但那条不变量的价值恰恰在于「不留特例」（CLAUDE.md 成对维护点）。

        副作用：无（只读 `self.protected_exemptions`）。
        """
        # 本层只管写入类。命令 / 读取 / glob / URL / 未映射一律原样穿过——
        # 这一行就是 N4「其余种类的请求逐字不变」的实现。
        if request.kind != "write_path":
            return result

        # ⚠ 见上要点①。位置也是刻意的：排在 inspect 之前，
        # 使一次已经确定被拒的请求连保护路径判定都不必跑。
        if result.decision is Decision.DENY:
            return result

        hit = protected.inspect(request.specifier, request.cwd)
        if hit is None:
            return result

        # 本会话豁免命中：不升级，但把这件事记下来。
        # 不记的话时间线上只剩一条 `allow（④模式）`，读的人会以为用户切到了放行档，
        # 而真实原因是他此前在面板上点过一次「本会话放行」。
        if hit.path in self.protected_exemptions:
            return DecisionResult(
                result.decision,
                result.layer,
                result.reason,
                kind=result.kind,
                host=result.host,
                protected_exempt=True,
            )

        # 见上要点②：ALLOW → ASK，ASK → ASK（**层被换掉**）。
        return DecisionResult(
            Decision.ASK,
            Layer.PROTECTED,
            hit.reason,
            kind=request.kind,
            host=request.host,
        )

    def grant_protected_exemption(self, request: PermissionRequest) -> bool:
        """
        为一次请求登记②″保护路径的**会话级豁免**（用户在确认面板上选「本会话放行」）。

        :param request: 本次权限请求
        :returns: 是否真的登记了（未命中保护路径时不登记，返回 False）

        接收**请求**而不是路径，是为了让「路径怎么解析成豁免键」这件事只有一处口径
        ——协调层不必自己去调 `protected.inspect`，也就不可能跟这里算出不同的键。

        豁免精确到**单个文件**，不扩展到目录：与既有确认面板「本会话放行」的最小
        授权口径一致。目录级豁免等于一次把整个 `.rhinecode/` 交出去。

        副作用：向 `protected_exemptions` 添加一项（该集合由派生实例共享）。
        """
        hit = protected.inspect(request.specifier, request.cwd)
        if hit is None:
            return False
        self.protected_exemptions.add(hit.path)
        return True

    def is_protected_exempt(self, path: Path) -> bool:
        """
        某个**解析后的绝对路径**是否已被本会话豁免。

        :param path: 解析后的绝对路径
        :returns: 已豁免返回 True

        供测试与将来的报告命令使用；判定路径本身不经过它（`_apply_protected`
        直接查集合，避免多一层间接让人以为还有别的判据）。
        """
        return path in self.protected_exemptions

    def _decide_core(self, request: PermissionRequest) -> DecisionResult:
        """
        既有五层决策管线（本扩展之前的 `decide`，**body 一字未改**）。

        :param request: 规范化后的权限请求（由 adapter.to_request 产出）
        :returns: DecisionResult（decision + 命中 layer + 中文 reason + kind/host）

        ⚠ **不要直接调用它。** 全系统的判定入口是 `decide`——绕过它就绕过了
        ②″保护路径收紧器，而那正是「模型改不了自己的配置」这条承诺的全部依据。
        唯一的合法调用方是 `decide` 自己，以及验证「其余种类逐字不变」的那条对照护栏。

        ⚠ **每一条 return 路径都必须填 `kind`（url 类另填 `host`）。**
        「带默认值所以既有构造点不动」只保证编译过、不保证功能对：确认面板只在
        判定为 ASK 时弹，而对 url 请求 ASK 只可能来自④模式兜底——漏填的后果是
        面板的 URL 专用分支永不进入、长地址仍被截断，而这在真机弹面板之前
        完全看不出来。为此下面用了一个统一的 `_verdict` 闭包，避免逐条手写时漏掉。

        副作用：无（纯判定）。
        """

        def _verdict(decision: Decision, layer: Layer, reason: str) -> DecisionResult:
            """本方法内所有结论的唯一出口——保证 kind/host 一条都不会漏填。"""
            return DecisionResult(decision, layer, reason, kind=request.kind, host=request.host)

        # 规则合并提到最前面：②′与③层用同一个对象，避免合并两次。
        # 它没有副作用，提前构造不改变任何行为。
        merged = RuleSet(self.turn_rules + self.session_rules + self.file_ruleset.rules)

        # ① 黑名单：仅命令类；命中即拒，且这层不可被③④放开（circuit breaker）。
        if request.kind == "command":
            reason = blacklist.check_command(request.specifier)
            if reason:
                return _verdict(Decision.DENY, Layer.BLACKLIST, "命中危险命令黑名单：" + reason)

        # ② 沙箱：仅路径/glob 类；越界即拒。复用 path_guard 的边界判定。
        # read 类走「工作区 ∪ 只读白名单」（c9 放行用户级记忆目录的只读访问，F18）；
        # write / glob 类仍严格限定工作区内，白名单对它们完全不可见（N6③）。
        #
        # ⚠ **c14：边界的「工作区」是 `request.cwd`，不是进程的当前工作目录。**
        # 主对话与非隔离子 Agent 的 cwd 是主项目根（行为与 c14 之前逐字一致）；
        # 隔离子 Agent 的 cwd 是它自己的隔离工作区，于是它读写主项目根内、
        # 工作区之外的路径会在这里被拒——**这就是隔离的物理实现**，
        # 不是靠约定、也不是靠模型自觉。
        #
        # cwd 缺失或非法时 `path_guard` 一律返回 False（拒绝），**不会退回按
        # 主项目根判定**（spec N2）——回退会把一次隔离故障静默变成一次越权。
        #
        # 管线层序**一字未动**：①黑名单 → ②沙箱 → ②′网络 → ③规则 → ④模式。
        # c14 不新增层、不改层序，既有的顺序论证原样成立。
        if request.kind == "read_path":
            if not is_readable_path(request.specifier, request.cwd):
                return _verdict(
                    Decision.DENY, Layer.SANDBOX, f"路径越界，超出工作目录：{request.specifier}"
                )
        elif request.kind in ("write_path", "glob"):
            if not is_within_workspace(request.specifier, request.cwd):
                return _verdict(
                    Decision.DENY, Layer.SANDBOX, f"路径越界，超出工作目录：{request.specifier}"
                )

        # ②′ 网络边界：仅 URL 类（web_fetch 扩展 spec F5/F6）。
        #
        # ⚠️ **必须排在③之前，理由是「硬校验必须先于任何 allow 规则」。**
        # 若②′晚于③，一条 `allow: WebFetch(domain:*)` 会在③层先行放行，
        # `file://` 与 `127.0.0.1` 就整个跳过了硬校验。
        #
        # 注意**不要**把理由写成「白名单会失效」——那是错的：能在③命中 allow 的域名
        # 本来就在白名单内，两种顺序结论相同。正因如此，顺序护栏也必须用
        # 「全域名 allow + 禁止地址」构造，用「白名单未命中」构造发现不了顺序错误。
        # 护栏见 `tests/test_perm_network_layer.py::PipelineOrderGuardTests`。
        if request.kind == "url":
            verdict = network.decide(request, merged, self.policy_ruleset)
            if verdict is not None:
                return verdict
            # 本层不下结论 → 直接进④。**刻意跳过③**：②′内部已经用同一个 merged
            # 求过一次值，再求一次必然还是 None，重复求值只会让读代码的人怀疑自己看漏了。
        else:
            # ③ 规则：本次执行级 → 会话级 → 文件级，依次合并；deny 优先求值。
            #
            # ⚠️ 本层位于①黑名单与②沙箱**之后**，这个顺序是预授权安全性的全部依据：
            # 一个声明「放行全部命令」的 Skill 也翻不过前两层。改动合并点位置之前，
            # 先看 `tests/test_perm_turn_grant.py` 里那两条护栏。
            hit = merged.evaluate(request)
            if hit is not None:
                return _verdict(hit.decision, hit.layer, hit.reason)

        # 只读简化分支（spec F7）：③未命中 → 只读工具直接放行，不进④模式层。
        # （url 类走不到这里——web_fetch 是 read_only=False。仍然经 _verdict 返回，
        #  是为了「每条 return 都填 kind/host」这条不变量本身成立，不留特例。）
        if request.is_read_only:
            return _verdict(Decision.ALLOW, Layer.RULE, "只读工具默认放行")

        # ④ 模式兜底：仅副作用工具、③未命中时。
        if request.mode is PermissionMode.STRICT:
            return _verdict(Decision.DENY, Layer.MODE, "严格模式：无规则放行，默认拒绝")
        if request.mode is PermissionMode.PERMISSIVE:
            # ⚠️ **URL 类的例外**（web_fetch 扩展 spec F7）：放行档对它降级为「仍然问」。
            #
            # 放行档的语义是「灰色地带别再烦我」。对文件工具而言其后果被②路径沙箱兜住
            # （再放行也出不了项目目录），但 URL 类请求在未建立域名白名单时
            # **没有等价的兜底边界**，「别烦我」会直接等于「任意域名任意抓取」。
            #
            # 这个例外只影响 URL 类，其余工具在放行档下的行为逐字不变——
            # 对照组护栏见 `tests/test_perm_network_layer.py` 里那条 write_file 用例。
            if request.kind == "url":
                return _verdict(
                    Decision.ASK,
                    Layer.MODE,
                    "放行模式对网络访问不生效：未建立域名白名单时仍交由用户确认",
                )
            return _verdict(Decision.ALLOW, Layer.MODE, "放行模式：无规则命中，默认允许")
        return _verdict(Decision.ASK, Layer.MODE, "默认模式：无规则命中，交由用户确认")

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
