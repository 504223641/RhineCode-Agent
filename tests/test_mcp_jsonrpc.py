"""JSON-RPC 层单测（c7 T15）：id 生成、消息构造、响应分类。"""

import unittest

from rhinecode.mcp import jsonrpc


class NextIdTests(unittest.TestCase):
    def test_monotonic_unique(self) -> None:
        a = jsonrpc.next_id()
        b = jsonrpc.next_id()
        c = jsonrpc.next_id()
        self.assertLess(a, b)
        self.assertLess(b, c)
        self.assertEqual(len({a, b, c}), 3)


class BuildTests(unittest.TestCase):
    def test_request_with_params(self) -> None:
        msg = jsonrpc.build_request(7, "tools/call", {"name": "echo"})
        self.assertEqual(msg["jsonrpc"], "2.0")
        self.assertEqual(msg["id"], 7)
        self.assertEqual(msg["method"], "tools/call")
        self.assertEqual(msg["params"], {"name": "echo"})

    def test_request_without_params_omits_key(self) -> None:
        msg = jsonrpc.build_request(1, "tools/list", None)
        self.assertNotIn("params", msg)

    def test_notification_has_no_id(self) -> None:
        msg = jsonrpc.build_notification("notifications/initialized", None)
        self.assertNotIn("id", msg)
        self.assertEqual(msg["method"], "notifications/initialized")


class IsResponseTests(unittest.TestCase):
    def test_result_is_response(self) -> None:
        self.assertTrue(jsonrpc.is_response({"jsonrpc": "2.0", "id": 1, "result": {}}))

    def test_error_is_response(self) -> None:
        self.assertTrue(jsonrpc.is_response({"jsonrpc": "2.0", "id": 1, "error": {"code": -1}}))

    def test_notification_is_not_response(self) -> None:
        self.assertFalse(jsonrpc.is_response({"jsonrpc": "2.0", "method": "x"}))

    def test_non_dict_is_not_response(self) -> None:
        self.assertFalse(jsonrpc.is_response("nope"))


if __name__ == "__main__":
    unittest.main()
