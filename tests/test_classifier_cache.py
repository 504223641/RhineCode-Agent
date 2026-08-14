"""
护栏：网络判定缓存（c16 F18/F19）。

三条要害：

1. **只有网络类进缓存。** 命令类进了的话，用户中途说的「先别提交」会对一条
   已被缓存为放行的 `git commit` 完全失效——**缓存会在最需要边界的时候把它废掉**。
2. **放行与拒绝的有效期不同。** 放行到「有新内容进入对话」为止（下一轮迭代），
   拒绝到本回合结束（本对象销毁）。
3. **缓存键只有主机与端口。** 带上路径等于不缓存（每个地址都不同），
   而且会把地址里可能夹带的令牌存进内存里的键上。
"""

import unittest

from rhinecode.classifier.cache import VerdictCache, cache_key
from rhinecode.classifier.models import (
    SCOPE_COMMAND,
    SCOPE_MESSAGE,
    SCOPE_URL,
    ReviewAction,
    Verdict,
    VerdictKind,
)


def _url(host: str = "docs.example.com", port: int = 443, spec: str = "") -> ReviewAction:
    return ReviewAction(
        scope=SCOPE_URL,
        tool_name="web_fetch",
        specifier=spec or f"https://{host}/a",
        host=host,
        port=port,
    )


ALLOW = Verdict(VerdictKind.ALLOW, "没问题")
BLOCK = Verdict(VerdictKind.BLOCK, "地址里像是一段密钥")
FAILED = Verdict(VerdictKind.FAILED, "连不上")


class CacheKeyTest(unittest.TestCase):
    def test_only_url_scope_gets_a_key(self) -> None:
        """
        AC27 的依据：命令类与消息类的键是**空串**（调用方据此跳过缓存）。

        「哪些类别参与缓存」收在这一个函数里，而不是让调用方各写一次
        `if scope == SCOPE_URL`——后者会在将来新增类别时产生两处判断，
        漏改其中一处不报错，只表现为「某个类别悄悄开始复用判定」。
        """
        self.assertTrue(cache_key(_url()))
        self.assertEqual(
            cache_key(ReviewAction(SCOPE_COMMAND, "run_command", "git push")), ""
        )
        self.assertEqual(
            cache_key(ReviewAction(SCOPE_MESSAGE, "send_message", "喂", recipient="b")), ""
        )

    def test_key_has_no_path_or_query(self) -> None:
        """
        ⚠ 同一主机的两个不同地址命中同一条缓存。

        带上路径的话每个地址都不同、缓存等于不存在；更要紧的是查询参数里
        可能夹带令牌，那会被存进内存里的键上——`to_allow_rule` 踩过同一个坑。
        """
        a = _url(spec="https://docs.example.com/a?token=secret-abc")
        b = _url(spec="https://docs.example.com/b")
        self.assertEqual(cache_key(a), cache_key(b))
        self.assertNotIn("secret-abc", cache_key(a))
        self.assertNotIn("/a", cache_key(a))

    def test_port_is_part_of_the_key(self) -> None:
        """不同端口是不同的目标，不该共享判定。"""
        self.assertNotEqual(cache_key(_url(port=443)), cache_key(_url(port=8080)))

    def test_missing_host_disables_caching(self) -> None:
        """
        主机解析不出来时不缓存——偏严方向。

        用一个空键去缓存会让所有解析失败的地址共享同一条结论。
        """
        self.assertEqual(cache_key(_url(host="")), "")


class LifetimeTest(unittest.TestCase):
    """两种有效期。"""

    def setUp(self) -> None:
        self.cache = VerdictCache()
        self.key = cache_key(_url())

    def test_allow_hits_within_the_same_iteration(self) -> None:
        """AC24：同一轮内第二次访问命中缓存。"""
        self.cache.put(self.key, ALLOW)
        hit = self.cache.get(self.key)
        self.assertIsNotNone(hit)
        self.assertIs(hit.kind, VerdictKind.ALLOW)

    def test_allow_expires_on_new_iteration(self) -> None:
        """AC25：有新内容进入对话（下一轮迭代）→ 放行结论作废，重新判定。"""
        self.cache.put(self.key, ALLOW)
        self.cache.begin_iteration()
        self.assertIsNone(self.cache.get(self.key))

    def test_deny_survives_iterations(self) -> None:
        """
        AC26 的另一半：拒绝结论**跨迭代仍然有效**，只随本回合结束而消失。

        目的是让模型在同一回合里「换个写法反复试同一个主机」不划算。
        """
        self.cache.put(self.key, BLOCK)
        self.cache.begin_iteration()
        self.cache.begin_iteration()
        hit = self.cache.get(self.key)
        self.assertIsNotNone(hit)
        self.assertIs(hit.kind, VerdictKind.BLOCK)

    def test_deny_is_gone_with_a_new_cache(self) -> None:
        """AC26：新的一次运行 = 新的缓存对象 = 拒绝结论消失。"""
        self.cache.put(self.key, BLOCK)
        self.assertIsNone(VerdictCache().get(self.key))

    def test_failed_is_never_cached(self) -> None:
        """
        ⚠ 失败不缓存：它是环境问题不是判定结论。

        缓存它会让一次网络抖动在整个回合里持续生效——而那时接口可能早就恢复了。
        """
        self.cache.put(self.key, FAILED)
        self.assertIsNone(self.cache.get(self.key))

    def test_empty_key_is_a_no_op(self) -> None:
        """空键（命令类/消息类）读写都不生效。"""
        self.cache.put("", ALLOW)
        self.assertIsNone(self.cache.get(""))


class HitMetadataTest(unittest.TestCase):
    def test_hit_is_marked_cached_with_zero_elapsed(self) -> None:
        """
        命中时 `cached=True` 且耗时归零。

        不改的话行为记录里会把一次零成本的命中记成「又花了 800 毫秒」，
        观测设施撒谎且不报错——而「为什么这个地址没被重新审查」
        的答案恰恰要靠这个标记。
        """
        cache = VerdictCache()
        key = cache_key(_url())
        cache.put(key, Verdict(VerdictKind.ALLOW, "没问题", elapsed_ms=800))
        hit = cache.get(key)
        self.assertTrue(hit.cached)
        self.assertEqual(hit.elapsed_ms, 0)
        self.assertEqual(hit.reason, "没问题")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
