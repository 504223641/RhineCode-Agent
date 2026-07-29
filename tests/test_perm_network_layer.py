"""
②′网络边界层单测（web_fetch 扩展 T2/T4/T5，spec F5/F5a/F6/F6a）。

四组：
1. 结构性硬校验（check_hard / is_forbidden_address）—— 协议、凭据、地址范围
2. 规则求值的 url 分支（_rule_matches）
3. ②′层判定入口（network.decide）—— 硬校验优先、deny 优先、白名单语义、来源层区分
4. 白名单的来源层（has_allow_for）

**本文件全程零 I/O**：判定层是纯逻辑，不做 DNS 解析、不发网络请求（spec N4/N5）。
"""

import unittest

from rhinecode.permission import network
from rhinecode.permission.models import (
    Decision,
    Layer,
    PermissionMode,
    PermissionRequest,
    Rule,
)
from rhinecode.permission.rules import RuleSet


def _url_request(url: str, host: str = "", mode: PermissionMode = PermissionMode.DEFAULT) -> PermissionRequest:
    """构造一次 url 类权限请求。host 缺省时从 url 现算，模拟 adapter 的行为。"""
    if not host:
        try:
            _scheme, host, _port = network.split_url(url)
        except ValueError:
            host = ""
    return PermissionRequest(
        tool_name="web_fetch",
        rule_name="WebFetch",
        specifier=url,
        kind="url",
        is_read_only=False,
        mode=mode,
        host=host,
    )


def _allow(pattern: str, source: str = "project") -> Rule:
    return Rule(effect="allow", tool="WebFetch", pattern=pattern, source=source)


def _deny(pattern: str, source: str = "user") -> Rule:
    return Rule(effect="deny", tool="WebFetch", pattern=pattern, source=source)


# =============================================================================
# 一、结构性硬校验
# =============================================================================
class SplitUrlTests(unittest.TestCase):
    def test_default_ports(self) -> None:
        # 端口按协议归一化，使 https://a.com 与 https://a.com:443 在「同主机」判据下相等。
        self.assertEqual(network.split_url("https://a.com/x"), ("https", "a.com", 443))
        self.assertEqual(network.split_url("http://a.com/x"), ("http", "a.com", 80))
        self.assertEqual(network.split_url("https://a.com:443/x"), ("https", "a.com", 443))
        self.assertEqual(network.split_url("https://a.com:8443/x"), ("https", "a.com", 8443))

    def test_host_normalized(self) -> None:
        self.assertEqual(network.split_url("https://Example.COM./x")[1], "example.com")

    def test_malformed_raises(self) -> None:
        for bad in ("", "not a url", "https://", "/just/a/path"):
            with self.assertRaises(ValueError):
                network.split_url(bad)


class CheckHardSchemeTests(unittest.TestCase):
    def test_file_scheme_rejected(self) -> None:
        # file:// 会让网络工具变成文件读取工具，直接绕过②路径沙箱。
        self.assertIsNotNone(network.check_hard("file:///C:/Users/x/config.yaml"))

    def test_other_schemes_rejected(self) -> None:
        for url in ("ftp://a.com/x", "gopher://a.com/", "data:text/plain,hi"):
            self.assertIsNotNone(network.check_hard(url), url)

    def test_http_and_https_pass(self) -> None:
        self.assertIsNone(network.check_hard("http://example.com/a"))
        self.assertIsNone(network.check_hard("https://example.com/a?q=1"))


class CheckHardCredentialTests(unittest.TestCase):
    def test_embedded_credentials_rejected(self) -> None:
        self.assertIsNotNone(network.check_hard("http://user:pass@example.com/"))
        self.assertIsNotNone(network.check_hard("http://user@example.com/"))

    def test_password_only_rejected(self) -> None:
        # `http://:pw@h/` 的 username 是空串而 password 非空，两个都要看。
        self.assertIsNotNone(network.check_hard("http://:pw@example.com/"))


class CheckHardReservedNameTests(unittest.TestCase):
    def test_localhost_family_rejected(self) -> None:
        for host in ("localhost", "LOCALHOST", "localhost.localdomain", "ip6-localhost"):
            self.assertIsNotNone(network.check_hard(f"http://{host}/"), host)


class IsForbiddenAddressTests(unittest.TestCase):
    """
    地址范围判断。**后四条是审查阶段实测发现的漏网**——按
    「环回∪私有∪链路本地∪保留∪未指定」那套黑名单判据，前三个全部放过。
    """

    def test_loopback_rejected(self) -> None:
        self.assertIsNotNone(network.is_forbidden_address("127.0.0.1"))
        self.assertIsNotNone(network.is_forbidden_address("::1"))
        self.assertIsNotNone(network.is_forbidden_address("[::1]"))

    def test_private_ranges_rejected(self) -> None:
        for addr in ("10.0.0.1", "192.168.1.1", "172.16.0.1"):
            self.assertIsNotNone(network.is_forbidden_address(addr), addr)

    def test_link_local_and_cloud_metadata_rejected(self) -> None:
        # 169.254.169.254 是各云厂商的实例元数据地址，SSRF 最经典的目标。
        self.assertIsNotNone(network.is_forbidden_address("169.254.169.254"))

    def test_cgnat_rejected(self) -> None:
        # 100.64.0.0/10（运营商级 NAT）—— 黑名单式判据的漏网之一。
        self.assertIsNotNone(network.is_forbidden_address("100.64.0.1"))

    def test_multicast_rejected(self) -> None:
        # 224.0.0.1 的 is_global 实测为 True，必须显式补判 is_multicast。
        self.assertIsNotNone(network.is_forbidden_address("224.0.0.1"))

    def test_6to4_encapsulated_loopback_rejected(self) -> None:
        # 2002:7f00:1:: 是 6to4 封装的 127.0.0.1，其 is_global 实测为 True。
        # 不显式还原内层 IPv4 就会整个放过——等于允许访问本机。
        self.assertIsNotNone(network.is_forbidden_address("2002:7f00:1::"))

    def test_ipv4_mapped_loopback_rejected(self) -> None:
        self.assertIsNotNone(network.is_forbidden_address("::ffff:127.0.0.1"))

    def test_nat64_encapsulated_private_rejected(self) -> None:
        # 64:ff9b::/96 是 NAT64 的 well-known 前缀，末 4 字节是内层 IPv4。
        self.assertIsNotNone(network.is_forbidden_address("64:ff9b::10.0.0.1"))

    def test_unspecified_rejected(self) -> None:
        self.assertIsNotNone(network.is_forbidden_address("0.0.0.0"))

    def test_unparseable_rejected(self) -> None:
        # fail-safe 偏严：解析不出来就拒，不放行。
        self.assertIsNotNone(network.is_forbidden_address("not-an-ip"))
        self.assertIsNotNone(network.is_forbidden_address(""))

    def test_public_addresses_pass(self) -> None:
        for addr in ("8.8.8.8", "1.1.1.1", "93.184.216.34", "2606:4700:4700::1111"):
            self.assertIsNone(network.is_forbidden_address(addr), addr)


class CheckHardAddressLiteralTests(unittest.TestCase):
    def test_ip_literal_in_url_rejected(self) -> None:
        for url in (
            "http://127.0.0.1/",
            "http://10.0.0.1/",
            "http://169.254.169.254/latest/meta-data/",
            "http://[::1]/",
            "http://100.64.0.1/",
            "http://224.0.0.1/",
            "http://[2002:7f00:1::]/",
            "http://[::ffff:127.0.0.1]/",
        ):
            self.assertIsNotNone(network.check_hard(url), url)

    def test_public_ip_literal_passes(self) -> None:
        self.assertIsNone(network.check_hard("http://8.8.8.8/"))

    def test_obfuscated_ipv4_passes_at_decision_time(self) -> None:
        # 十进制/八进制形式的 127.0.0.1 在判定期**不会**被识别为 IP 字面量——
        # 标准库的地址解析对它们直接报错，于是被当成主机名放过。
        # 它们由**连接期**的域名解析兜底（spec F5a / AC6a）。
        # 这条断言存在的意义是：钉住「判定期不是完整边界」这个事实，
        # 免得有人看到它「通过」就以为漏了。
        self.assertIsNone(network.check_hard("http://2130706433/"))
        self.assertIsNone(network.check_hard("http://0177.0.0.1/"))

    def test_malformed_url_rejected(self) -> None:
        self.assertIsNotNone(network.check_hard(""))
        self.assertIsNotNone(network.check_hard("не url"))


class HardCheckSharedByBothPhasesTests(unittest.TestCase):
    """
    判定期与连接期共用同一份实现（spec F5a 硬要求）。

    这里用「同一个地址在两个入口给出同源原因」来钉住它——两处各写一套的话，
    这两段文本会漂移。
    """

    def test_reason_text_is_same_source(self) -> None:
        via_address = network.is_forbidden_address("127.0.0.1")
        via_url = network.check_hard("http://127.0.0.1/")
        self.assertEqual(via_address, via_url)


# =============================================================================
# 二、规则求值的 url 分支
# =============================================================================
class UrlRuleMatchingTests(unittest.TestCase):
    def test_domain_deny_matches(self) -> None:
        rs = RuleSet([_deny("domain:*.evil.com")])
        hit = rs.evaluate(_url_request("https://a.evil.com/x"))
        self.assertIsNotNone(hit)
        self.assertEqual(hit.decision, Decision.DENY)

    def test_domain_allow_matches(self) -> None:
        rs = RuleSet([_allow("domain:github.com")])
        hit = rs.evaluate(_url_request("https://github.com/x"))
        self.assertIsNotNone(hit)
        self.assertEqual(hit.decision, Decision.ALLOW)

    def test_bare_tool_rule_matches_everything(self) -> None:
        # spec F12：不带括号的 `WebFetch` 等价于 `WebFetch(domain:*)`。
        rs = RuleSet([_deny("")])
        hit = rs.evaluate(_url_request("https://anything.example/x"))
        self.assertIsNotNone(hit)
        self.assertEqual(hit.decision, Decision.DENY)

    def test_missing_domain_prefix_does_not_match(self) -> None:
        # 漏写 `domain:` 的规则在加载期就该被处理掉；这里是第二道保险。
        rs = RuleSet([_allow("github.com")])
        self.assertIsNone(rs.evaluate(_url_request("https://github.com/x")))

    def test_other_tool_name_does_not_match(self) -> None:
        rs = RuleSet([Rule(effect="allow", tool="Read", pattern="domain:github.com", source="user")])
        self.assertIsNone(rs.evaluate(_url_request("https://github.com/x")))

    def test_matches_host_not_full_url(self) -> None:
        # 规则匹配的是 host，不是完整 URL——路径与查询参数不参与。
        rs = RuleSet([_allow("domain:github.com")])
        hit = rs.evaluate(_url_request("https://github.com/a/b?token=abc"))
        self.assertIsNotNone(hit)


class HasAllowForTests(unittest.TestCase):
    def test_detects_allow(self) -> None:
        self.assertTrue(RuleSet([_allow("domain:github.com")]).has_allow_for("WebFetch"))

    def test_deny_does_not_count(self) -> None:
        self.assertFalse(RuleSet([_deny("domain:evil.com")]).has_allow_for("WebFetch"))

    def test_other_tool_does_not_count(self) -> None:
        rs = RuleSet([Rule(effect="allow", tool="Read", pattern="src/**", source="user")])
        self.assertFalse(rs.has_allow_for("WebFetch"))

    def test_empty_ruleset(self) -> None:
        self.assertFalse(RuleSet([]).has_allow_for("WebFetch"))


# =============================================================================
# 三、②′层判定入口
# =============================================================================
class NetworkDecideTests(unittest.TestCase):
    def test_hard_check_beats_any_allow(self) -> None:
        """硬校验优先于一切规则——这是本层安全性的全部依据。"""
        merged = RuleSet([_allow("domain:*")])
        policy = RuleSet([_allow("domain:*")])
        for url in ("file:///C:/x", "http://127.0.0.1/", "http://user:pass@a.com/"):
            verdict = network.decide(_url_request(url), merged, policy)
            self.assertIsNotNone(verdict, url)
            self.assertEqual(verdict.decision, Decision.DENY, url)
            self.assertEqual(verdict.layer, Layer.NETWORK, url)

    def test_deny_beats_allow(self) -> None:
        """deny 优先，不看层级远近（沿用③层的哲学 A）。"""
        merged = RuleSet([_deny("domain:*.example.com", "user"), _allow("domain:*.example.com", "project")])
        policy = merged
        verdict = network.decide(_url_request("https://api.example.com/x"), merged, policy)
        self.assertIsNotNone(verdict)
        self.assertEqual(verdict.decision, Decision.DENY)

    def test_allow_hit_passes(self) -> None:
        merged = RuleSet([_allow("domain:github.com")])
        verdict = network.decide(_url_request("https://github.com/x"), merged, merged)
        self.assertIsNotNone(verdict)
        self.assertEqual(verdict.decision, Decision.ALLOW)

    def test_whitelist_established_rejects_unlisted(self) -> None:
        """
        白名单语义：user/project 层存在 allow 域名规则时，未命中即拒（而非弹确认）。

        这是「限制不可被权限模式放开」的实现根基——③层未命中是「不下结论」，
        本层未命中是「结论」。
        """
        policy = RuleSet([_allow("domain:github.com", "project")])
        verdict = network.decide(_url_request("https://example.com/x"), policy, policy)
        self.assertIsNotNone(verdict)
        self.assertEqual(verdict.decision, Decision.DENY)
        self.assertEqual(verdict.layer, Layer.NETWORK)

    def test_whitelist_survives_permissive_mode(self) -> None:
        """上一条在放行档下结论不变——本层根本不看 mode。"""
        policy = RuleSet([_allow("domain:github.com", "project")])
        req = _url_request("https://example.com/x", mode=PermissionMode.PERMISSIVE)
        verdict = network.decide(req, policy, policy)
        self.assertIsNotNone(verdict)
        self.assertEqual(verdict.decision, Decision.DENY)

    def test_no_whitelist_no_verdict(self) -> None:
        """没有任何 allow 域名规则时本层不下结论，交由④模式兜底。"""
        empty = RuleSet([])
        self.assertIsNone(network.decide(_url_request("https://example.com/x"), empty, empty))

    def test_session_level_allow_does_not_establish_whitelist(self) -> None:
        """
        **来源层区分**（spec F6a）：只存在于 merged 而不在 policy_ruleset 的 allow
        （模拟会话级 / 本次执行级授权）**只放行、不建立白名单**。

        不做这个区分会出两个问题：用户点一次「永久放行」就把自己锁死；
        Skill 的 allowed-tools 反向收紧其它域名。
        """
        merged = RuleSet([_allow("domain:github.com", "session")])
        policy = RuleSet([])  # user/project 两层是空的
        # 命中的那个照常放行
        hit = network.decide(_url_request("https://github.com/x"), merged, policy)
        self.assertIsNotNone(hit)
        self.assertEqual(hit.decision, Decision.ALLOW)
        # 未命中的**不下结论**（不是 DENY）——白名单没被建立
        self.assertIsNone(network.decide(_url_request("https://example.com/x"), merged, policy))

    def test_local_level_allow_does_not_establish_whitelist(self) -> None:
        """本地级（「永久放行」自动写入的授权记录）同样不建立白名单。"""
        merged = RuleSet([_allow("domain:github.com", "local")])
        policy = RuleSet([])  # policy_ruleset 只含 user + project，不含 local
        self.assertIsNone(network.decide(_url_request("https://example.com/x"), merged, policy))

    def test_verdict_carries_kind_and_host(self) -> None:
        """本层产出的结论要带上 kind/host，供确认面板与行为记录使用。"""
        policy = RuleSet([_allow("domain:github.com", "project")])
        verdict = network.decide(_url_request("https://example.com/a"), policy, policy)
        self.assertEqual(verdict.kind, "url")
        self.assertEqual(verdict.host, "example.com")

    def test_reason_distinguishes_two_categories(self) -> None:
        """
        拒绝原因要能让模型区分两类（spec F24）：
        「该地址本身不被允许访问」（硬校验，不该重试）与
        「该域名不在允许范围内」（白名单，可以换来源）。
        """
        empty = RuleSet([])
        hard = network.decide(_url_request("http://127.0.0.1/"), empty, empty)
        policy = RuleSet([_allow("domain:github.com", "project")])
        listed = network.decide(_url_request("https://example.com/x"), policy, policy)
        self.assertNotEqual(hard.reason, listed.reason)
        self.assertIn("不在允许范围内", listed.reason)


# =============================================================================
# 四、管线插入与模式例外（T8）
# =============================================================================
def _engine(file_rules=None, policy_rules=None, mode=PermissionMode.DEFAULT):
    from rhinecode.permission.engine import PermissionEngine

    return PermissionEngine(
        RuleSet(list(file_rules or [])),
        mode=mode,
        policy_ruleset=RuleSet(list(policy_rules or [])),
    )


def _write_request(mode: PermissionMode) -> PermissionRequest:
    """一次普通的写文件请求，用作「模式例外只作用于 URL 类」的对照组。"""
    return PermissionRequest(
        tool_name="write_file",
        rule_name="Write",
        specifier="out.txt",
        kind="write_path",
        is_read_only=False,
        mode=mode,
    )


class ModeFallbackTests(unittest.TestCase):
    """三档模式对「无任何 user/project 域名规则」的 url 请求（spec F7 / AC13）。"""

    def _decide(self, mode: PermissionMode):
        eng = _engine(mode=mode)
        return eng.decide(_url_request("https://example.com/x", mode=mode))

    def test_strict_denies(self) -> None:
        r = self._decide(PermissionMode.STRICT)
        self.assertEqual(r.decision, Decision.DENY)
        self.assertEqual(r.layer, Layer.MODE)

    def test_default_asks(self) -> None:
        r = self._decide(PermissionMode.DEFAULT)
        self.assertEqual(r.decision, Decision.ASK)

    def test_permissive_still_asks(self) -> None:
        """
        **放行档对网络访问不生效**——这是判据「限制不可被权限模式放开」的一半。

        放行档的语义是「灰色地带别再烦我」，对文件工具其后果被②路径沙箱兜住，
        但 URL 类在未建立白名单时没有等价的兜底边界。
        """
        r = self._decide(PermissionMode.PERMISSIVE)
        self.assertEqual(r.decision, Decision.ASK)
        self.assertEqual(r.layer, Layer.MODE)
        self.assertIn("放行模式对网络访问不生效", r.reason)

    def test_permissive_control_group_write_file_still_allowed(self) -> None:
        """
        **对照组**：同一放行档下，一次写文件调用仍被直接放行。

        证明这个例外只作用于 URL 类，没有污染其它工具的模式语义。
        """
        eng = _engine(mode=PermissionMode.PERMISSIVE)
        r = eng.decide(_write_request(PermissionMode.PERMISSIVE))
        self.assertEqual(r.decision, Decision.ALLOW)
        self.assertEqual(r.layer, Layer.MODE)


class PipelineOrderGuardTests(unittest.TestCase):
    """
    ⚠ **②′必须排在③之前** —— 本文件最重要的一条护栏。

    ## 为什么护栏必须是这个形态

    直觉上会写成「`allow: domain:github.com` + 请求 `example.com` → 断言 DENY」，
    但把②′挪到③之后推演一遍：③无命中返回 None，接着②′的白名单判定照样给出
    DENY/NETWORK —— **结论完全相同，那条断言在错序下照样通过**，什么也钉不住。

    只有「**全域名 allow + 禁止地址**」能发现：
    - 正确顺序：②′先跑，硬校验命中 → DENY/NETWORK
    - 错误顺序：③先跑，`domain:*` 命中 → ALLOW/RULE，硬校验被整个跳过

    ## 这两种形态的差别是实测出来的，不是推演出来的

    实现完成后跑过一次「把②′挪到③之后」的模拟，结果：

        形态A『白名单未命中』   错序结果 deny/network   → 断言仍通过（假护栏）
        形态B『全域名allow+127.0.0.1』 错序结果 allow/rule → 断言会红（真护栏）

    所以本类只用形态 B。**改动本用例前请先重跑一遍那个模拟**——
    看起来更自然的写法很可能什么都钉不住。
    """

    def test_wildcard_allow_cannot_bypass_hard_check(self) -> None:
        eng = _engine(
            file_rules=[_allow("domain:*")],
            policy_rules=[_allow("domain:*")],
            mode=PermissionMode.PERMISSIVE,
        )
        for url in ("http://127.0.0.1/", "file:///C:/x", "http://user:pass@a.com/"):
            r = eng.decide(_url_request(url, mode=PermissionMode.PERMISSIVE))
            self.assertEqual(r.decision, Decision.DENY, url)
            self.assertEqual(
                r.layer,
                Layer.NETWORK,
                f"{url}：命中层必须是网络边界。若这里变成 RULE，说明②′被挪到了③之后",
            )


class WhitelistThroughEngineTests(unittest.TestCase):
    """白名单语义经完整管线的表现（spec F6 / AC11）。"""

    def test_listed_domain_allowed(self) -> None:
        rules = [_allow("domain:github.com", "project")]
        eng = _engine(file_rules=rules, policy_rules=rules)
        r = eng.decide(_url_request("https://github.com/x"))
        self.assertEqual(r.decision, Decision.ALLOW)

    def test_unlisted_domain_denied_not_asked(self) -> None:
        rules = [_allow("domain:github.com", "project")]
        eng = _engine(file_rules=rules, policy_rules=rules)
        r = eng.decide(_url_request("https://example.com/x"))
        self.assertEqual(r.decision, Decision.DENY)
        self.assertEqual(r.layer, Layer.NETWORK)

    def test_unlisted_domain_denied_in_permissive_mode(self) -> None:
        rules = [_allow("domain:github.com", "project")]
        eng = _engine(file_rules=rules, policy_rules=rules, mode=PermissionMode.PERMISSIVE)
        r = eng.decide(_url_request("https://example.com/x", mode=PermissionMode.PERMISSIVE))
        self.assertEqual(r.decision, Decision.DENY)

    def test_session_level_allow_does_not_lock_out_others(self) -> None:
        """
        会话级授权（模拟「本会话放行」）**只放行、不建立白名单**。

        它进 file_rules 参与命中判断，但不进 policy_rules —— 于是其它域名
        仍然走到模式兜底（ASK），而不是被硬拒。
        """
        eng = _engine(file_rules=[_allow("domain:github.com", "session")], policy_rules=[])
        self.assertEqual(eng.decide(_url_request("https://github.com/x")).decision, Decision.ALLOW)
        other = eng.decide(_url_request("https://example.com/x"))
        self.assertEqual(other.decision, Decision.ASK)
        self.assertEqual(other.layer, Layer.MODE)


class VerdictCarriesKindAndHostTests(unittest.TestCase):
    """
    ⚠ `decide()` 的**每一条** return 路径都要带上 kind（url 类另带 host）。

    漏填的后果是确认面板的 URL 专用分支永不进入、长地址仍被截断，
    而这在真机弹面板之前完全看不出来。
    """

    def test_network_layer_verdict(self) -> None:
        eng = _engine()
        r = eng.decide(_url_request("http://127.0.0.1/"))
        self.assertEqual(r.kind, "url")
        self.assertEqual(r.host, "127.0.0.1")

    def test_rule_layer_verdict(self) -> None:
        rules = [_deny("domain:example.com")]
        eng = _engine(file_rules=rules)
        r = eng.decide(_url_request("https://example.com/x"))
        self.assertEqual(r.kind, "url")
        self.assertEqual(r.host, "example.com")

    def test_mode_layer_verdict_all_three_modes(self) -> None:
        for mode in (PermissionMode.STRICT, PermissionMode.DEFAULT, PermissionMode.PERMISSIVE):
            eng = _engine(mode=mode)
            r = eng.decide(_url_request("https://example.com/x", mode=mode))
            self.assertEqual(r.kind, "url", mode)
            self.assertEqual(r.host, "example.com", mode)

    def test_non_url_kinds_carry_kind_too(self) -> None:
        eng = _engine(mode=PermissionMode.PERMISSIVE)
        r = eng.decide(_write_request(PermissionMode.PERMISSIVE))
        self.assertEqual(r.kind, "write_path")
        self.assertEqual(r.host, "")

    def test_blacklist_layer_verdict(self) -> None:
        eng = _engine()
        req = PermissionRequest(
            tool_name="run_command",
            rule_name="Bash",
            specifier="rm -rf /",
            kind="command",
            is_read_only=False,
            mode=PermissionMode.DEFAULT,
        )
        r = eng.decide(req)
        self.assertEqual(r.layer, Layer.BLACKLIST)
        self.assertEqual(r.kind, "command")

    def test_readonly_shortcut_verdict(self) -> None:
        eng = _engine()
        req = PermissionRequest(
            tool_name="read_file",
            rule_name="Read",
            specifier="README.md",
            kind="read_path",
            is_read_only=True,
            mode=PermissionMode.DEFAULT,
        )
        r = eng.decide(req)
        self.assertEqual(r.decision, Decision.ALLOW)
        self.assertEqual(r.kind, "read_path")


if __name__ == "__main__":
    unittest.main()
