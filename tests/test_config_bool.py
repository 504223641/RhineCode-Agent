import tempfile
import unittest
from pathlib import Path

from rhinecode.config import load


class ConfigBoolTests(unittest.TestCase):
    def _write_config(self, text: str) -> Path:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / "config.yaml"
        path.write_text(text, encoding="utf-8")
        return path

    def test_debug_log_quoted_false_is_false(self) -> None:
        path = self._write_config(
            """
protocol: deepseek
model: test-model
base_url: https://example.test
api_key: test-key
debug_log: "false"
"""
        )

        cfg = load(str(path))

        self.assertFalse(cfg.debug_log)

    def test_debug_log_rejects_non_boolean_string(self) -> None:
        path = self._write_config(
            """
protocol: deepseek
model: test-model
base_url: https://example.test
api_key: test-key
debug_log: maybe
"""
        )

        with self.assertRaises(ValueError):
            load(str(path))
