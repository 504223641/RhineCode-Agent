"""
兜底模型清单的护栏（first-run-setup 扩展 T3）。

本文件里最要紧的不是「清单里有几项」，而是 `PairedWithConfigTest`——
它钉住「兜底清单说的默认模型」与「配置模板说的默认模型」是同一个。
这两处**已经分家过一次**（模板写 `deepseek-chat` 而实测用
`deepseek-v4-flash`，分了一个多月没人发现），代价是任何一个照模板配置的
新用户第一句话就报模型不存在。
"""

import unittest

from rhinecode.config import DEFAULT_CONTEXT_WINDOW, DEFAULT_MODEL, _CONFIG_TEMPLATE
from rhinecode.setup import catalog


class FallbackShapeTest(unittest.TestCase):
    """兜底清单本身的形状。"""

    def test_not_empty(self):
        """兜底清单不能是空的——它是「拉不到清单」时用户唯一能选的东西。"""
        self.assertGreater(len(catalog.FALLBACK_OPTIONS), 0)

    def test_exactly_one_recommended(self):
        """
        推荐项**恰好一个**。

        零个的话，一个正在做首次配置的人面对两个陌生名字没有任何依据；
        两个以上的话「推荐」这个词就没有意义了。
        """
        recommended = [o for o in catalog.FALLBACK_OPTIONS if o.recommended]
        self.assertEqual(len(recommended), 1, f"推荐项应恰好一个，实际 {recommended}")

    def test_every_option_has_a_blurb(self):
        """兜底清单里的每一项都必须有说明文字——这份是我们自己写的，没有借口留空。"""
        for option in catalog.FALLBACK_OPTIONS:
            self.assertTrue(option.blurb, f"{option.model_id} 缺说明文字")

    def test_vision_model_excluded(self):
        """
        实验性多模态模型**不进兜底清单**（catalog 里写明的刻意为之）。

        ⚠ 这是一条反证：它容易被后来的人当成「漏了一个」顺手加回来，
        而本项目只发送文本，把它摆在首次配置的人面前只会制造一个
        他没有依据去做的选择。
        """
        ids = [o.model_id for o in catalog.FALLBACK_OPTIONS]
        self.assertNotIn("deepseek-v4-flash-vision-exp", ids)


class WindowForTest(unittest.TestCase):
    """`window_for` 的两条路径。"""

    def test_known_model_returns_table_value(self):
        self.assertEqual(catalog.window_for("deepseek-v4-flash"), 1_000_000)
        self.assertEqual(catalog.window_for("deepseek-v4-pro"), 1_000_000)

    def test_vision_model_is_in_window_table_even_though_not_in_fallback(self):
        """
        在窗口表里、不在兜底清单里——这两件事不矛盾，且都要成立。

        表回答「如果用了它，窗口该填多少」；清单回答「拉不到时推荐用什么」。
        服务端返回它时我们照常列出来，那时就需要这张表给出正确的值。
        """
        self.assertEqual(catalog.window_for("deepseek-v4-flash-vision-exp"), 1_000_000)

    def test_unknown_model_falls_back_to_default(self):
        """
        认不出的模型**返回缺省值，不猜**。

        猜大了 c8 会一直不压缩、直到服务端报超长；猜小了白丢上下文。
        返回缺省值等价于「用户没写这一项」，是唯一诚实的行为。
        """
        self.assertEqual(catalog.window_for("some-future-model"), DEFAULT_CONTEXT_WINDOW)


class DescribeTest(unittest.TestCase):
    """`describe` 认不出时不能抛。"""

    def test_known_model(self):
        blurb, recommended = catalog.describe(DEFAULT_MODEL)
        self.assertTrue(blurb)
        self.assertTrue(recommended)

    def test_unknown_model_returns_empty_and_not_recommended(self):
        """
        服务端返回我们没见过的新模型是**正常现象**，不是错误。

        照常列出来、只是没有说明文字，也不会被标成推荐。
        """
        self.assertEqual(catalog.describe("some-future-model"), ("", False))


class OptionsFromIdsTest(unittest.TestCase):
    """服务端返回的 id 序列 → 候选项。"""

    def test_preserves_server_order(self):
        """
        顺序保持服务端给的那个，**不按推荐与否重排**。

        服务端的顺序本身可能有含义（新模型在前），我们没有依据去否定它。
        """
        options = catalog.options_from_ids(["deepseek-v4-pro", DEFAULT_MODEL])
        self.assertEqual([o.model_id for o in options], ["deepseek-v4-pro", DEFAULT_MODEL])

    def test_does_not_filter_unknown_models(self):
        """
        **不过滤**服务端结果——过滤等于又一次把「我们认为有哪些模型」写死，
        而那正是本扩展要治的病。
        """
        options = catalog.options_from_ids(["brand-new-model"])
        self.assertEqual(len(options), 1)
        self.assertEqual(options[0].model_id, "brand-new-model")
        self.assertEqual(options[0].blurb, "")

    def test_empty_input(self):
        self.assertEqual(catalog.options_from_ids([]), ())


class PairedWithConfigTest(unittest.TestCase):
    """
    **成对维护点护栏**：兜底清单 ↔ `config.DEFAULT_MODEL` ↔ `_CONFIG_TEMPLATE`。

    三处都在声明「我们认为当前该用哪个模型」。它们已经分家过一次
    （模板写 `deepseek-chat`、实测用 `deepseek-v4-flash`，一个多月没人发现），
    后果是任何一个照模板配置的新用户第一句话就报模型不存在。
    """

    def test_recommended_option_is_the_config_default(self):
        """兜底清单里那个推荐项，必须就是 `config.DEFAULT_MODEL`。"""
        recommended = [o for o in catalog.FALLBACK_OPTIONS if o.recommended][0]
        self.assertEqual(
            recommended.model_id,
            DEFAULT_MODEL,
            "兜底清单的推荐项与 config.DEFAULT_MODEL 分家了——"
            "这正是 2026-07 那次「模板写了个已停用模型、一个多月没发现」的形态",
        )

    def test_config_template_writes_the_same_model(self):
        """配置模板里那行 `model:` 必须写的也是同一个。"""
        self.assertIn(
            f"model: {DEFAULT_MODEL}",
            _CONFIG_TEMPLATE,
            "配置模板里的 model 与 config.DEFAULT_MODEL 分家了",
        )

    def test_template_does_not_ship_a_retired_alias(self):
        """
        模板里不能再有**生效的**已停用别名。

        ⚠ 判据刻意只看「生效的配置行」而不是「文本里出现过」——模板的注释里
        专门写着「老别名已于 2026-07-24 停用」，那句话必须留着，
        而一条粗糙的 `assertNotIn` 会把它一起判红。
        """
        active_lines = [
            line.strip()
            for line in _CONFIG_TEMPLATE.splitlines()
            if line.strip() and not line.strip().startswith("#")
        ]
        for retired in ("deepseek-chat", "deepseek-reasoner"):
            for line in active_lines:
                self.assertNotIn(
                    retired, line, f"模板里还有一条生效的已停用别名：{line}"
                )


if __name__ == "__main__":
    unittest.main()
