"""
命令注册表测试（c10 T9–T10）：冲突校验、原子性、解析、补全、隐藏项与帮助。

全部为纯逻辑测试：不依赖真实界面、网络或磁盘（spec N8/C26/C84）。
"""

import unittest

from rhinecode.commands import (
    CommandRegistrationError,
    CommandRegistry,
    CommandSpec,
    CommandType,
)


def _noop(invocation, controller) -> None:
    """测试用空处理函数。"""


def make_spec(name: str, aliases: tuple = (), **kwargs) -> CommandSpec:
    """构造最小 CommandSpec 的测试辅助。"""
    return CommandSpec(
        name=name,
        aliases=aliases,
        description=kwargs.pop("description", f"{name} 的描述"),
        usage=kwargs.pop("usage", name),
        command_type=kwargs.pop("command_type", CommandType.LOCAL),
        handler=kwargs.pop("handler", _noop),
        **kwargs,
    )


class RegistryConflictTests(unittest.TestCase):
    """T9：冲突校验与批量注册的原子性。"""

    def test_name_vs_name_conflict(self) -> None:
        registry = CommandRegistry()
        registry.register(make_spec("/foo"))
        with self.assertRaises(CommandRegistrationError) as ctx:
            registry.register(make_spec("/foo"))
        # 错误信息含冲突标识与双方规范命令（spec F2/N4）
        self.assertIn("/foo", str(ctx.exception))

    def test_name_vs_alias_conflict(self) -> None:
        registry = CommandRegistry()
        registry.register(make_spec("/context", aliases=("/ctx",)))
        with self.assertRaises(CommandRegistrationError) as ctx:
            registry.register(make_spec("/ctx"))
        message = str(ctx.exception)
        self.assertIn("/ctx", message)
        self.assertIn("/context", message)

    def test_alias_vs_alias_conflict(self) -> None:
        registry = CommandRegistry()
        registry.register(make_spec("/context", aliases=("/c",)))
        with self.assertRaises(CommandRegistrationError) as ctx:
            registry.register(make_spec("/compact", aliases=("/c",)))
        message = str(ctx.exception)
        self.assertIn("/c", message)
        self.assertIn("/context", message)
        self.assertIn("/compact", message)

    def test_case_insensitive_conflict(self) -> None:
        """仅大小写不同的重复项视为冲突（spec F2/AC2）。"""
        registry = CommandRegistry()
        registry.register(make_spec("/help"))
        with self.assertRaises(CommandRegistrationError):
            registry.register(make_spec("/Help"))

    def test_duplicate_alias_within_same_spec(self) -> None:
        registry = CommandRegistry()
        with self.assertRaises(CommandRegistrationError):
            registry.register(make_spec("/foo", aliases=("/f", "/F")))

    def test_register_many_is_atomic(self) -> None:
        """批量注册后项冲突时，前项不写入正式注册表（plan 3 原子语义）。"""
        registry = CommandRegistry()
        with self.assertRaises(CommandRegistrationError):
            registry.register_many(
                [make_spec("/first"), make_spec("/second"), make_spec("/first")]
            )
        self.assertIsNone(registry.resolve("/first"))
        self.assertIsNone(registry.resolve("/second"))
        self.assertEqual(registry.visible_commands(), ())

    def test_identifier_must_start_with_slash(self) -> None:
        registry = CommandRegistry()
        with self.assertRaises(CommandRegistrationError):
            registry.register(make_spec("help"))

    def test_identifier_must_not_contain_whitespace(self) -> None:
        registry = CommandRegistry()
        with self.assertRaises(CommandRegistrationError):
            registry.register(make_spec("/he lp"))


class RegistryResolveCompleteTests(unittest.TestCase):
    """T10：解析、补全顺序、隐藏项与帮助文本。"""

    def _registry(self) -> CommandRegistry:
        registry = CommandRegistry()
        registry.register_many(
            [
                make_spec(
                    "/context",
                    aliases=("/ctx",),
                    description="查看上下文用量",
                    usage="/context",
                ),
                make_spec("/compact", description="手动压缩上下文"),
                make_spec(
                    "/resume",
                    aliases=("/continue",),
                    description="恢复历史会话",
                    usage="/resume [编号或ID]",
                    argument_hint="[编号或ID]",
                    command_type=CommandType.UI,
                ),
                make_spec("/secret", hidden=True, description="隐藏命令"),
            ]
        )
        return registry

    def test_resolve_case_insensitive(self) -> None:
        registry = self._registry()
        self.assertEqual(registry.resolve("/CTX").name, "/context")
        self.assertEqual(registry.resolve("/Context").name, "/context")
        self.assertEqual(registry.resolve("/CONTINUE").name, "/resume")

    def test_resolve_matched_returns_identifier(self) -> None:
        registry = self._registry()
        spec, matched = registry.resolve_matched("/CTX")
        self.assertEqual(spec.name, "/context")
        self.assertEqual(matched, "/ctx")

    def test_resolve_unknown_returns_none(self) -> None:
        self.assertIsNone(self._registry().resolve("/nope"))

    def test_hidden_command_resolvable(self) -> None:
        """隐藏命令仍可直接解析执行（spec F20）。"""
        self.assertEqual(self._registry().resolve("/secret").name, "/secret")

    def test_hidden_command_not_visible(self) -> None:
        registry = self._registry()
        names = [s.name for s in registry.visible_commands()]
        self.assertNotIn("/secret", names)
        self.assertNotIn("/secret", registry.render_help())
        self.assertEqual(registry.complete("/sec"), ())

    def test_completion_order_and_alias_flags(self) -> None:
        """候选按注册顺序、每命令先规范名后别名；别名标注规范命令（spec F21/F22）。"""
        registry = self._registry()
        items = registry.complete("/c")
        self.assertEqual(
            [item.value for item in items],
            ["/context", "/ctx", "/compact", "/continue"],
        )
        ctx_alias = items[1]
        self.assertTrue(ctx_alias.is_alias)
        self.assertEqual(ctx_alias.canonical_name, "/context")
        self.assertIn("/context", ctx_alias.description)
        self.assertFalse(items[0].is_alias)

    def test_completion_case_insensitive(self) -> None:
        items = self._registry().complete("/C")
        self.assertEqual(len(items), 4)

    def test_completion_deterministic(self) -> None:
        """相同注册内容与输入重复补全，候选顺序一致（spec N3/C20）。"""
        registry = self._registry()
        results = [tuple(i.value for i in registry.complete("/c")) for _ in range(3)]
        self.assertEqual(len(set(results)), 1)

    def test_render_help_contains_fields(self) -> None:
        """帮助含描述、用法、别名、类型与参数提示（spec F19）。"""
        text = self._registry().render_help()
        self.assertIn("/context — 查看上下文用量", text)
        self.assertIn("别名：/ctx", text)
        self.assertIn("用法：/resume [编号或ID]", text)
        self.assertIn("参数：[编号或ID]", text)
        self.assertIn("类型：本地", text)
        self.assertIn("类型：界面", text)


if __name__ == "__main__":
    unittest.main()
