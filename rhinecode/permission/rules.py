"""
③可配置规则层（spec F4）：把若干条 Rule 合并成一个 RuleSet，并对一次权限请求做求值。

核心是「**deny 永远优先**」的求值哲学（spec 已定哲学 A）：
    把所有来源（会话级 + 本地级 + 项目级 + 用户级）的规则合并后统一判断——
    只要**任意一条** deny 命中即拒绝，deny 不可被任何 allow 翻案；没有 deny 命中时，
    有 allow 命中则放行；都没命中则本层不下定论（返回 None，交给上层模式兜底）。

这层是纯逻辑：输入 PermissionRequest，输出 Optional[DecisionResult]，无副作用、可单测。
具体的字符串/路径匹配委托给 matching 模块；本模块只负责「按 deny→allow 顺序扫描」。
"""

from typing import Optional

from rhinecode.permission.models import Decision, DecisionResult, Layer, PermissionRequest, Rule
from rhinecode.permission.matching import match_command, match_path


def _rule_matches(rule: Rule, request: PermissionRequest) -> bool:
    """
    判断单条规则是否命中本次请求。

    先比工具名（规则体系名，如 Bash/Read/Edit/Write），不一致直接不命中；
    再按请求种类 kind 选择匹配方式：
    - command → 命令模式匹配（match_command）
    - read_path / write_path / glob → 路径模式匹配（match_path）
    - other（未映射工具，无 specifier）→ 仅当规则是「整工具规则」（pattern 为空）时命中

    :param rule: 待检规则
    :param request: 本次权限请求
    :returns: 命中返回 True
    """
    if rule.tool != request.rule_name:
        return False
    if request.kind == "command":
        return match_command(rule.pattern, request.specifier)
    if request.kind in ("read_path", "write_path", "glob"):
        return match_path(rule.pattern, request.specifier)
    # other：没有可匹配的 specifier，只有「匹配该工具全部」的空模式规则才算命中。
    return rule.pattern == ""


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
