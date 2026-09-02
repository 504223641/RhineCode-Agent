"""
向导触发判定的护栏（first-run-setup 扩展 T4，spec F1 / AC1–AC4）。

四条正路径 + 两条反证。反证钉的是两个「改了不报错」的方向：
判据被换成「跑过没跑过」、以及异常口径被放宽到裸 `except`。
"""

import tempfile
import unittest
from pathlib import Path

from rhinecode.config import PLACEHOLDER_API_KEY, _CONFIG_TEMPLATE
from rhinecode.setup.models import TriggerReason
from rhinecode.setup.trigger import classify

_GOOD = (
    "protocol: deepseek\n"
    "model: deepseek-v4-flash\n"
    "base_url: https://api.deepseek.com\n"
    "api_key: sk-real-looking-key\n"
)


class ClassifyTest(unittest.TestCase):
    """四种情形各一条（AC1–AC4）。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.path = self.root / "config.yaml"

    def tearDown(self):
        self._tmp.cleanup()

    def test_missing_file(self):
        """AC1：文件不存在 → MISSING。"""
        self.assertEqual(classify(self.path), TriggerReason.MISSING)

    def test_placeholder_api_key(self):
        """AC2：占位符 key → PLACEHOLDER，而不是「配置可用」。"""
        self.path.write_text(
            _GOOD.replace("sk-real-looking-key", PLACEHOLDER_API_KEY), encoding="utf-8"
        )
        self.assertEqual(classify(self.path), TriggerReason.PLACEHOLDER)

    def test_broken_yaml(self):
        """AC3：解析不了的 YAML → INVALID。"""
        self.path.write_text("protocol: [unclosed\n  : :\n", encoding="utf-8")
        self.assertEqual(classify(self.path), TriggerReason.INVALID)

    def test_missing_required_field(self):
        """缺必填字段也归 INVALID——它同样是「这份配置没法用」。"""
        self.path.write_text("protocol: deepseek\nmodel: deepseek-v4-flash\n", encoding="utf-8")
        self.assertEqual(classify(self.path), TriggerReason.INVALID)

    def test_good_config_returns_none(self):
        """AC4：配置可用时返回 None——**不该打扰用户**。"""
        self.path.write_text(_GOOD, encoding="utf-8")
        self.assertIsNone(classify(self.path))

    def test_generated_template_is_placeholder_not_invalid(self):
        """
        刚生成的模板必须判 PLACEHOLDER，**不能判 INVALID**。

        这是最常走到的一条路（用户跑了一次 `rhine`，模板生成了，他还没填 key），
        而两者给用户的第一屏文案完全不同：一个是「来，填个 key」，
        一个是「你的配置坏了」。模板显然没坏。
        """
        self.path.write_text(_CONFIG_TEMPLATE, encoding="utf-8")
        self.assertEqual(classify(self.path), TriggerReason.PLACEHOLDER)


class NoCompletionMarkerTest(unittest.TestCase):
    """
    **反证**：判据必须是「配置当前可不可用」，不能是「跑过没跑过」。

    做成后者（落一个「已完成首次配置」的标记位）之后，用户手工把 key 删掉
    就再也引导不出向导了——他手上只剩一个起不来的程序和一句
    「请填入 api_key」。这条用例就是那个场景。
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.path = self.root / "config.yaml"

    def tearDown(self):
        self._tmp.cleanup()

    def test_reverting_key_to_placeholder_triggers_again(self):
        # 先是一份好配置：不该打扰
        self.path.write_text(_GOOD, encoding="utf-8")
        self.assertIsNone(classify(self.path))

        # 用户把 key 换回占位符（或删掉重填）——必须**再次**引导
        self.path.write_text(
            _GOOD.replace("sk-real-looking-key", PLACEHOLDER_API_KEY), encoding="utf-8"
        )
        self.assertEqual(
            classify(self.path),
            TriggerReason.PLACEHOLDER,
            "判据被换成「跑过没跑过」了——配置已经不可用却引导不出来",
        )

    def test_classify_writes_nothing(self):
        """
        `classify` 不许留下任何痕迹。

        它一旦开始写标记位，上一条用例的行为就会变——所以这条盯的是
        「目录里除了配置文件本身，什么都没多出来」。
        """
        self.path.write_text(_GOOD, encoding="utf-8")
        before = sorted(p.name for p in self.root.iterdir())
        classify(self.path)
        after = sorted(p.name for p in self.root.iterdir())
        self.assertEqual(before, after, "classify 写了东西——它应当是只读的")


class ExceptionScopeTest(unittest.TestCase):
    """
    异常口径的边界。

    ⚠ **本类的第一条用例是实测推翻自己的假设之后写的，值得连原委一起留着。**
    原本以为「文件不是合法 UTF-8」不属于 `ValueError`、会一路抛出去，
    实际上 **`UnicodeDecodeError` 是 `ValueError` 的子类**
    （`UnicodeDecodeError` → `UnicodeError` → `ValueError`），
    所以它已经落在 `INVALID` 那一支里了。

    结果本身是对的（读都读不出来的配置确实没法用），但它带来一个**写盘侧的
    义务**：Windows 中文环境下一份 GBK 编码的 `config.yaml` 正是这个形态，
    而它的内容对用户有价值。`writer.apply` 因此绝不覆盖一份读不出来的文件。
    两处缺一不可——只有这里判 INVALID 而写盘直接覆盖，等于把用户的配置悄悄删了。
    """

    def test_non_utf8_file_is_invalid_because_decode_error_is_a_valueerror(self):
        """非 UTF-8 编码的配置 → INVALID（因为 UnicodeDecodeError 属于 ValueError）。"""
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "config.yaml"
            # GBK 编码的中文注释——Windows 中文环境下手写配置的真实形态
            path.write_bytes("# 配置\nprotocol: deepseek\n".encode("gbk"))
            self.assertEqual(classify(path), TriggerReason.INVALID)

    def test_decode_error_really_is_a_valueerror(self):
        """
        把上面那条依赖的类型关系单独钉住。

        它不是 `trigger` 自己的行为，而是**这段代码之所以正确的前提**。
        Python 哪天改了继承关系（不会，但），上面那条会静默地换一种方式通过
        （异常抛到外面 → 测试报错），而这条会直接指出根因。
        """
        self.assertTrue(issubclass(UnicodeDecodeError, ValueError))

    def test_permission_error_is_not_swallowed(self):
        """
        **反证**：口径不能放宽成裸 `except Exception`。

        `PermissionError` 这种「真的出问题了」的情形必须抛出去。吞掉它的话
        用户拿到一个配置向导、填完还是起不来，而真正的原因一个字都没显示。
        """
        import unittest.mock as mock

        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "config.yaml"
            path.write_text(_GOOD, encoding="utf-8")
            with mock.patch(
                "rhinecode.setup.trigger.load", side_effect=PermissionError("拒绝访问")
            ):
                with self.assertRaises(PermissionError):
                    classify(path)


if __name__ == "__main__":
    unittest.main()
