"""
规则解析与两层加载的单元测试（c12 T9/T11，对应 checklist 第一节 / spec AC1、AC19、AC20）。

覆盖加载期七项校验、两处整层降级、两层加载顺序，以及「一条写坏不影响其余」这条
fail-safe 性质。
"""

import tempfile
import unittest
from pathlib import Path
from unittest import mock

import yaml

from rhinecode.hooks import config as hook_config
from rhinecode.hooks.models import (
    CommandAction,
    HookEventType,
    HttpAction,
    PromptAction,
)
from rhinecode.hooks.parser import parse_rules


def _one(body: dict) -> dict:
    """把一条规则包成完整的顶层结构。"""
    return {"hooks": [body]}


def _valid(**overrides) -> dict:
    """一条最小可用的合法规则，供各用例按需覆盖字段。"""
    rule = {
        "event": "post_tool_use",
        "action": {"type": "command", "command": "echo hi"},
    }
    rule.update(overrides)
    return rule


class MinimalRuleTest(unittest.TestCase):
    """三要素模型（AC1）。"""

    def test_event_and_action_only(self):
        """只写 event 与 action、省略 if 的规则能被加载，condition 为 None。"""
        rules, warnings = parse_rules(_one(_valid()), "user")
        self.assertEqual(len(rules), 1)
        self.assertEqual(warnings, [])
        self.assertIsNone(rules[0].condition, "省略 if 应得到 None 而不是空 Condition")
        self.assertEqual(rules[0].event, HookEventType.POST_TOOL_USE)

    def test_missing_event_dropped(self):
        rules, warnings = parse_rules(_one({"action": {"type": "prompt", "text": "x"}}), "user")
        self.assertEqual(rules, [])
        self.assertEqual(len(warnings), 1)
        self.assertIn("event", warnings[0])

    def test_missing_action_dropped(self):
        rules, warnings = parse_rules(_one({"event": "turn_start"}), "user")
        self.assertEqual(rules, [])
        self.assertIn("action", warnings[0])

    def test_generated_name_when_absent(self):
        rules, _ = parse_rules(_one(_valid()), "project")
        self.assertEqual(rules[0].name, "project#1")

    def test_explicit_name_kept(self):
        rules, _ = parse_rules(_one(_valid(name="我的规则")), "user")
        self.assertEqual(rules[0].name, "我的规则")

    def test_empty_document_is_not_an_error(self):
        """空文件 / 全注释文件解析成 None——这使「有模板」与「无文件」完全等价。"""
        rules, warnings = parse_rules(None, "user")
        self.assertEqual(rules, [])
        self.assertEqual(warnings, [])


class ValidationTest(unittest.TestCase):
    """加载期七项校验各一条（AC19）。"""

    def _reject(self, body: dict, *, expect: str):
        rules, warnings = parse_rules(_one(body), "user")
        self.assertEqual(rules, [], "非法规则必须被丢弃")
        self.assertEqual(len(warnings), 1)
        self.assertIn(expect, warnings[0])
        return warnings[0]

    def test_1_unknown_event(self):
        w = self._reject(_valid(event="on_monday"), expect="不是合法事件")
        self.assertIn("session_start", w, "警告里应列出合法取值")

    def test_2_unknown_action_type(self):
        self._reject(_valid(action={"type": "carrier_pigeon"}), expect="action.type")

    def test_2_action_missing_required_field(self):
        self._reject(_valid(action={"type": "command"}), expect="command")
        self._reject(_valid(action={"type": "prompt"}), expect="text")
        self._reject(_valid(action={"type": "http"}), expect="url")
        self._reject(_valid(action={"type": "agent"}), expect="prompt")

    def test_3_both_all_and_any(self):
        body = _valid(**{"if": {"all": [{"tool": "x"}], "any": [{"tool": "y"}]}})
        self._reject(body, expect="恰好写一个")

    def test_3_neither_all_nor_any(self):
        self._reject(_valid(**{"if": {"tool": "x"}}), expect="恰好写一个")

    def test_3_unknown_key_beside_combine(self):
        """多写的键是笔误，静默忽略会让用户以为它生效了。"""
        body = _valid(**{"if": {"all": [{"tool": "x"}], "mode": "strict"}})
        self._reject(body, expect="无法识别的键")

    def test_4_unknown_field_on_closed_event(self):
        body = _valid(event="session_start", **{"if": {"all": [{"command": "git *"}]}})
        w = self._reject(body, expect="没有字段")
        self.assertIn("可用字段", w)

    def test_4_open_field_set_on_tool_events(self):
        """
        三个工具级事件的字段集是**开放**的——`tool_input` 的键取决于是哪个工具，
        加载期不可能枚举（见 models.OPEN_INPUT_EVENTS）。
        """
        for event in ("pre_tool_use", "post_tool_use", "post_tool_use_failure"):
            with self.subTest(event=event):
                body = _valid(
                    event=event,
                    **{"if": {"all": [{"old_string": "x"}, {"whatever_param": "y"}]}},
                )
                rules, warnings = parse_rules(_one(body), "user")
                self.assertEqual(len(rules), 1, f"{event} 应放行未登记字段名")
                self.assertEqual(warnings, [])

    def test_5_bad_regex(self):
        body = _valid(**{"if": {"all": [{"tool": "/[/"}]}})
        self._reject(body, expect="正则")

    def test_6_async_forbidden_on_pre_tool_use(self):
        body = _valid(event="pre_tool_use", **{"async": True})
        w = self._reject(body, expect="async")
        self.assertIn("等待结果", w, "警告要解释为什么，不能只说不许")

    def test_6_async_allowed_elsewhere(self):
        rules, warnings = parse_rules(_one(_valid(**{"async": True})), "user")
        self.assertEqual(len(rules), 1)
        self.assertTrue(rules[0].run_async)
        self.assertEqual(warnings, [])

    def test_7_non_positive_timeout(self):
        self._reject(_valid(action={"type": "command", "command": "x", "timeout": 0}),
                     expect="大于 0")
        self._reject(_valid(action={"type": "command", "command": "x", "timeout": -5}),
                     expect="大于 0")

    def test_7_bool_timeout_rejected(self):
        """
        `isinstance(True, int)` 在 Python 里是 True。不挡掉的话 `timeout: true`
        会被当成 1 秒——一个几乎必然超时的 Hook，而用户看不出哪里错了。
        """
        self._reject(_valid(action={"type": "command", "command": "x", "timeout": True}),
                     expect="正整数")


class ActionDefaultsTest(unittest.TestCase):
    """四种动作的可选字段与缺省值。"""

    def test_command_default_timeout(self):
        rules, _ = parse_rules(_one(_valid()), "user")
        self.assertIsInstance(rules[0].action, CommandAction)
        self.assertEqual(rules[0].action.timeout, 60)

    def test_http_defaults(self):
        body = _valid(action={"type": "http", "url": "https://example.com/x"})
        rules, _ = parse_rules(_one(body), "user")
        action = rules[0].action
        self.assertIsInstance(action, HttpAction)
        self.assertEqual(action.method, "POST")
        self.assertEqual(action.headers, ())
        self.assertIsNone(action.body)
        self.assertEqual(action.timeout, 10)

    def test_http_headers_become_hashable(self):
        """HttpAction 是 frozen dataclass，要可哈希，而 dict 不可哈希。"""
        body = _valid(action={
            "type": "http",
            "url": "https://example.com/x",
            "headers": {"X-Token": "abc"},
            "method": "put",
        })
        rules, _ = parse_rules(_one(body), "user")
        action = rules[0].action
        self.assertEqual(action.headers, (("X-Token", "abc"),))
        self.assertEqual(action.method, "PUT", "method 应归一化为大写")
        hash(action)  # 不抛即通过

    def test_prompt_action(self):
        body = _valid(action={"type": "prompt", "text": "记得跑测试"})
        rules, _ = parse_rules(_one(body), "user")
        self.assertEqual(rules[0].action, PromptAction("记得跑测试"))


class LayerDegradeTest(unittest.TestCase):
    """两处整层降级 + 单条级跳过（AC19）。"""

    def test_top_level_not_mapping_degrades_whole_layer(self):
        rules, warnings = parse_rules(["not", "a", "mapping"], "user")
        self.assertEqual(rules, [])
        self.assertIn("顶层", warnings[0])

    def test_hooks_not_list_degrades_whole_layer(self):
        """
        ⚠ 反证：这一处**不能**改成「记下警告后继续」。

        一个 `hooks:` 写成映射（少写了列表的 `-`）的文件，若按「跳过该字段继续」处理，
        会变成「零条规则生效但用户以为全都生效了」。整层降级至少让规则数明确为 0，
        配合警告能立刻看出问题。
        """
        rules, warnings = parse_rules({"hooks": {"event": "turn_start"}}, "user")
        self.assertEqual(rules, [])
        self.assertEqual(len(warnings), 1)
        self.assertIn("列表", warnings[0])

    def test_missing_hooks_key_is_silent(self):
        rules, warnings = parse_rules({"something_else": 1}, "user")
        self.assertEqual(rules, [])
        self.assertEqual(warnings, [], "没写 hooks 键不是错误")

    def test_one_bad_rule_does_not_affect_others(self):
        """单条级失败只跳过该条（AC19 / spec F8）。"""
        data = {"hooks": [
            _valid(name="好规则一"),
            _valid(name="坏规则", event="nonsense"),
            _valid(name="好规则二"),
        ]}
        rules, warnings = parse_rules(data, "user")
        self.assertEqual([r.name for r in rules], ["好规则一", "好规则二"])
        self.assertEqual(len(warnings), 1)
        self.assertIn("坏规则", warnings[0], "警告必须点名是哪一条")

    def test_non_mapping_rule_skipped(self):
        rules, warnings = parse_rules({"hooks": ["just a string", _valid()]}, "user")
        self.assertEqual(len(rules), 1)
        self.assertEqual(len(warnings), 1)


class LoadAllTest(unittest.TestCase):
    """两层加载与顺序（AC19 / AC20）。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.user_dir = root / "userdir"
        self.project_root = root / "project"
        (self.project_root / ".rhinecode").mkdir(parents=True)
        self.user_dir.mkdir(parents=True)
        patcher = mock.patch.object(
            hook_config, "main_project_root", return_value=self.project_root
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self._tmp.cleanup)

    def _write_user(self, data):
        hook_config.user_config_path(self.user_dir).write_text(
            yaml.safe_dump(data, allow_unicode=True), encoding="utf-8"
        )

    def _write_project(self, data):
        hook_config.project_config_path().write_text(
            yaml.safe_dump(data, allow_unicode=True), encoding="utf-8"
        )

    def test_no_files_is_silent_empty(self):
        rules, warnings = hook_config.load_all(self.user_dir)
        self.assertEqual(rules, [])
        self.assertEqual(warnings, [])

    def test_execution_order_user_then_project_then_declaration(self):
        self._write_user({"hooks": [_valid(name="u1"), _valid(name="u2")]})
        self._write_project({"hooks": [_valid(name="p1"), _valid(name="p2")]})
        rules, warnings = hook_config.load_all(self.user_dir)
        self.assertEqual([r.name for r in rules], ["u1", "u2", "p1", "p2"])
        self.assertEqual([r.source for r in rules], ["user", "user", "project", "project"])
        self.assertEqual([r.index for r in rules], [0, 1, 0, 1], "index 是层内序")
        self.assertEqual(warnings, [])

    def test_project_only(self):
        self._write_project({"hooks": [_valid(name="p1")]})
        rules, _ = hook_config.load_all(self.user_dir)
        self.assertEqual([r.name for r in rules], ["p1"])

    def test_broken_layer_does_not_stop_the_other(self):
        hook_config.user_config_path(self.user_dir).write_text(
            "hooks: [ { unclosed", encoding="utf-8"
        )
        self._write_project({"hooks": [_valid(name="p1")]})
        rules, warnings = hook_config.load_all(self.user_dir)
        self.assertEqual([r.name for r in rules], ["p1"], "另一层必须照常加载")
        self.assertEqual(len(warnings), 1)
        self.assertIn("解析失败", warnings[0])


class ScaffoldTest(unittest.TestCase):
    """模板生成：全注释、幂等、不覆盖。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = Path(self._tmp.name) / "sub" / "hooks.yaml"

    def test_template_parses_to_nothing(self):
        """「有模板」与「无文件」对运行时必须完全等价（spec F13）。"""
        self.assertTrue(hook_config.scaffold_user_config(self.path))
        data = yaml.safe_load(self.path.read_text(encoding="utf-8"))
        self.assertIsNone(data, "模板必须全部是注释")
        rules, warnings = parse_rules(data, "user")
        self.assertEqual(rules, [])
        self.assertEqual(warnings, [])

    def test_does_not_overwrite(self):
        self.path.parent.mkdir(parents=True)
        self.path.write_text("hooks: []\n", encoding="utf-8")
        self.assertFalse(hook_config.scaffold_user_config(self.path))
        self.assertEqual(self.path.read_text(encoding="utf-8"), "hooks: []\n")

    def test_template_documents_the_two_safety_properties(self):
        """
        模板是多数用户唯一会读的文档。两条安全性质必须写在里面，
        否则用户会按 Claude Code 的习惯去写 `allow`，然后困惑于它为什么不生效。
        """
        hook_config.scaffold_user_config(self.path)
        text = self.path.read_text(encoding="utf-8")
        self.assertIn("只能收紧不能放宽", text)
        self.assertIn("fail-closed", text)
        self.assertIn("直接执行", text, "项目级风险要写明")


if __name__ == "__main__":
    unittest.main()
