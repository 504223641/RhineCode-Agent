"""
全局配置引导单测：用户级配置路径、模板生成、占位符检测的前提。

覆盖 config.py 新增的 user_config_path / scaffold_user_config / PLACEHOLDER_API_KEY，
验证首次运行「自动生成模板 + 引导填 api_key」这条路径的关键前提成立：
- 缺省配置指向 ~/.rhinecode/config.yaml；
- scaffold 能建目录、写模板，且绝不覆盖已有文件（幂等、防误伤）；
- 生成的模板能被 load() 解析成功，且 api_key 恰是占位符（占位符检测得以触发）。
"""

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from rhinecode import config


class TempHome(unittest.TestCase):
    """把 Path.home() 指向临时目录，隔离真实用户配置，保证测试确定性。"""

    def setUp(self) -> None:
        self._home = tempfile.TemporaryDirectory()
        # config.py 内部用 Path.home() 定位配置目录，这里打桩到临时目录。
        self._home_patch = mock.patch(
            "rhinecode.config.Path.home", return_value=Path(self._home.name)
        )
        self._home_patch.start()

    def tearDown(self) -> None:
        self._home_patch.stop()
        self._home.cleanup()


class UserConfigPathTests(TempHome):
    def test_points_to_rhinecode_dir(self) -> None:
        # 缺省全局配置应落在 ~/.rhinecode/config.yaml。
        path = config.user_config_path()
        self.assertEqual(path, Path(self._home.name) / ".rhinecode" / "config.yaml")


class ScaffoldTests(TempHome):
    def test_creates_dir_and_writes_template(self) -> None:
        # 目录不存在时，scaffold 应建目录并写入含占位符的模板，返回 True。
        path = config.user_config_path()
        self.assertFalse(path.exists())

        created = config.scaffold_user_config(path)

        self.assertTrue(created)
        self.assertTrue(path.exists())
        self.assertIn(config.PLACEHOLDER_API_KEY, path.read_text(encoding="utf-8"))

    def test_does_not_overwrite_existing(self) -> None:
        # 已有配置时必须原样保留，返回 False（防止覆盖用户真实 api_key）。
        path = config.user_config_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("protocol: deepseek\napi_key: REAL_KEY\n", encoding="utf-8")

        created = config.scaffold_user_config(path)

        self.assertFalse(created)
        self.assertIn("REAL_KEY", path.read_text(encoding="utf-8"))

    def test_generated_template_loads_with_placeholder(self) -> None:
        # 生成的模板应能被 load() 成功解析，且 api_key 恰为占位符——
        # 这是 __main__ 里「占位符检测」得以触发的前提。
        path = config.user_config_path()
        config.scaffold_user_config(path)

        cfg = config.load(str(path))

        self.assertEqual(cfg.protocol, "deepseek")
        self.assertEqual(cfg.api_key, config.PLACEHOLDER_API_KEY)


if __name__ == "__main__":
    unittest.main()
