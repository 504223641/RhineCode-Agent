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
from rhinecode.permission.matching import match_command, match_domain, match_path

# 域名规则的 specifier 前缀。`WebFetch(domain:example.com)` 里括号内容必须以它开头，
# 否则该条在加载期就已按 spec F13 处理掉（allow 丢弃 / deny 降级为整工具拒绝）。
_DOMAIN_PREFIX = "domain:"


def _rule_matches(rule: Rule, request: PermissionRequest) -> bool:
    """
    判断单条规则是否命中本次请求。

    按请求种类 kind 选择匹配方式：
    - command → 工具名须精确等于规则体系名（Bash），再做命令模式匹配（match_command）
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
        return match_command(rule.pattern, request.specifier)
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
        3. 都没命中 → 返回 None（本层不下定论，交给调用方的模式层兜底）。

        :param request: 规范化后的权限请求
        :returns: 命中则返回 RULE 层的 DecisionResult；未命中返回 None

        副作用：无。
        """
        # 第一遍：deny 优先。
        for rule in self.rules:
            if rule.effect == "deny" and _rule_matches(rule, request):
                return DecisionResult(Decision.DENY, Layer.RULE, "命中 " + _describe(rule))
        # 第二遍：allow。
        for rule in self.rules:
            if rule.effect == "allow" and _rule_matches(rule, request):
                return DecisionResult(Decision.ALLOW, Layer.RULE, "命中 " + _describe(rule))
        return None

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
