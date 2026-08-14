"""
护栏：分类器的三个配置项与「关掉之后逐字回到本章之前」（c16 F25/F26、F20/F21）。

⚠ 两个字段**刻意用两种口径**，本文件把这个不对称钉住：

| 字段 | 非法值 | 为什么 |
| --- | --- | --- |
| `enabled` | **抛错** | 它是**安全开关**。写成 "maybe" 是明确的配置错误，静默回退会让用户以为自己关掉了分类器而实际上没关（或反过来） |
| `timeout` | **回退默认** | 它是调优项，写错了最坏是超时时长不对，不该阻断启动 |

合并成一种口径看起来更整齐，但那会在两个方向上各错一次。
"""

import tempfile
import unittest
from pathlib import Path

from rhinecode.classifier.broad import is_broad_command_allow
from rhinecode.classifier.render import render_dropped_rules
from rhinecode.config import Config, load

_BASE = """\
protocol: deepseek
model: deepseek-chat
base_url: https://api.deepseek.com
api_key: k
"""


def _load(extra: str = "") -> Config:
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "config.yaml"
        path.write_text(_BASE + extra, encoding="utf-8")
        return load(str(path))


class DefaultsTest(unittest.TestCase):
    """AC35：缺省值。"""

    def test_whole_section_can_be_omitted(self) -> None:
        """整段缺省 = 「开、跟主模型、10 秒」。"""
        cfg = _load()
        self.assertTrue(cfg.classifier_enabled)
        self.assertEqual(cfg.classifier_model, "")
        self.assertEqual(cfg.classifier_timeout, 10.0)

    def test_empty_model_means_same_as_main(self) -> None:
        """
        `model` 留空的语义是「跟主对话同一个模型」。

        留这个口子是因为跑命令是高频操作，每次多一次往返有感，
        将来可以换更便宜的。
        """
        cfg = _load("classifier:\n  model:\n")
        self.assertEqual(cfg.classifier_model, "")

    def test_request_timeout_defaults_to_none(self) -> None:
        """
        ⚠ `request_timeout` 缺省 None = **不传给 SDK**，行为逐字不变。

        显式传 `None` 在这个 SDK 里的语义是「不设超时」，
        与「用默认值」不是一回事。
        """
        self.assertIsNone(_load().request_timeout)


class ExplicitValuesTest(unittest.TestCase):
    def test_all_three_fields(self) -> None:
        cfg = _load(
            "classifier:\n"
            "  enabled: false\n"
            "  model: cheap-model\n"
            "  timeout: 3.5\n"
        )
        self.assertFalse(cfg.classifier_enabled)
        self.assertEqual(cfg.classifier_model, "cheap-model")
        self.assertEqual(cfg.classifier_timeout, 3.5)

    def test_string_booleans_are_understood(self) -> None:
        for text, expected in (("'false'", False), ("'no'", False), ("'on'", True)):
            with self.subTest(text=text):
                cfg = _load(f"classifier:\n  enabled: {text}\n")
                self.assertIs(cfg.classifier_enabled, expected)


class InvalidValueTest(unittest.TestCase):
    """⚠ 两种口径的不对称——本文件的要害。"""

    def test_invalid_enabled_raises(self) -> None:
        """
        安全开关写错 → **抛错**，与 `web_fetch_enabled` 同口径。

        静默回退会让用户以为自己关掉了分类器而实际上没关。
        """
        with self.assertRaises(ValueError) as ctx:
            _load("classifier:\n  enabled: maybe\n")
        self.assertIn("classifier.enabled", str(ctx.exception))

    def test_invalid_timeout_falls_back(self) -> None:
        """调优项写错 → 回退默认，**不抛错**，与 `context_window` 同口径。"""
        for bad in ("abc", "-1", "0", "true"):
            with self.subTest(bad=bad):
                cfg = _load(f"classifier:\n  timeout: {bad}\n")
                self.assertEqual(cfg.classifier_timeout, 10.0)

    def test_bool_is_not_accepted_as_a_timeout(self) -> None:
        """
        ⚠ `True` 是 int 的子类，不先排除的话会被静默当成 **1.0 秒**
        ——那会让每一次判定都超时，而配置文件看起来「写了个值」。
        """
        cfg = _load("classifier:\n  timeout: true\n")
        self.assertEqual(cfg.classifier_timeout, 10.0)

    def test_malformed_section_falls_back_to_defaults(self) -> None:
        """整段写成非映射时回退默认，不阻断启动。"""
        cfg = _load("classifier: 一句话\n")
        self.assertTrue(cfg.classifier_enabled)
        self.assertEqual(cfg.classifier_timeout, 10.0)


class TemplateTest(unittest.TestCase):
    def test_template_documents_the_section(self) -> None:
        """
        AC35 的另一半：模板里要有这三个字段的说明。

        用户看不到的配置项等于不存在——尤其是**缺省开**的那种，
        他得知道有这么个东西、以及怎么关。
        """
        from rhinecode.config import _CONFIG_TEMPLATE

        self.assertIn("classifier:", _CONFIG_TEMPLATE)
        for field in ("enabled", "model", "timeout"):
            self.assertIn(field, _CONFIG_TEMPLATE)

    def test_request_timeout_is_not_in_the_template(self) -> None:
        """
        ⚠ `request_timeout` **刻意不进模板**：它不是给用户调的旋钮，
        而是装配层给分类器专用的那个 Provider 副本设超时用的。

        给主对话设超时会把正常工作腰斩——它的一次请求可能生成几分钟
        （模型在吐一份大文件的内容）。
        """
        from rhinecode.config import _CONFIG_TEMPLATE

        self.assertNotIn("request_timeout", _CONFIG_TEMPLATE)


class DroppedRulesReportTest(unittest.TestCase):
    """AC31：被丢弃的规则要逐条告知（F21）。"""

    def test_report_lists_rule_source_and_reason(self) -> None:
        """
        ⚠ 静默丢弃会让「我明明配了为什么还弹」无从查起。

        用户写下那条规则是一次明确的决定，我们推翻了它，
        就必须当面说清楚是哪一条、为什么、以及他有哪两个选择。
        """
        text = render_dropped_rules(
            [("Bash(python *)", "project", "`python` 后面跟任意参数就能执行任意代码")]
        )
        self.assertIn("Bash(python *)", text)
        self.assertIn("project", text)
        self.assertIn("任意代码", text)
        # 两个选择都要给出来。
        self.assertIn("改窄", text)
        self.assertIn("classifier.enabled", text)

    def test_empty_report_when_nothing_dropped(self) -> None:
        """没丢东西时整段不出现——不制造无信息量的启动噪音。"""
        self.assertEqual(render_dropped_rules([]), "")


class DisabledEquivalenceTest(unittest.TestCase):
    """
    AC30/AC36：关掉分类器 = 逐字回到本章之前。

    ⚠ 装配层的「丢弃宽泛规则」整段**在 `classifier_enabled` 之内**——
    关掉之后那些规则原样生效。这条用例验的是判定函数本身与开关无关，
    真正的「关掉就不丢」由装配层的条件保证（见 `bootstrap.py`），
    端到端形态在 `docs/c16/checklist.md` 的场景 6。
    """

    def test_broad_judgement_is_independent_of_the_switch(self) -> None:
        """判定是纯函数，不读任何配置——「要不要用它」由装配层决定。"""
        import inspect

        from rhinecode.classifier import broad

        source = inspect.getsource(broad)
        self.assertNotIn("classifier_enabled", source)
        self.assertTrue(is_broad_command_allow("Bash", "python *"))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
