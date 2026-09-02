"""
配置写盘的护栏（first-run-setup 扩展 T5/T6，spec F11/F12/F16 / AC16–AC18、AC22）。

本文件里有三条最要紧的，都对应「漏改不报错」的形态：

- `CommentsSurviveTest`：写一次配置不许把模板里六十多行说明抹掉；
- `_split_inline_comment` 认引号——朴素按 `#` 切会**把密钥截断**，
  而截断后的配置照样能加载，表现为「填了正确的 key 却报 401」；
- `EmptyApiKeyTest`：空串表示「不改」，写错会**静默清空用户的密钥**。
"""

import dataclasses
import tempfile
import unittest
from pathlib import Path

import yaml

from rhinecode.config import (
    DEFAULT_CONTEXT_WINDOW,
    DEFAULT_MODEL,
    _CONFIG_TEMPLATE,
    load,
)
from rhinecode.setup.models import SetupDraft
from rhinecode.setup.writer import (
    _dump_scalar,
    _split_inline_comment,
    apply,
    read_current,
    set_scalar,
)


class ActiveLineTest(unittest.TestCase):
    """情形 1：已有生效行 → 就地替换。"""

    def test_replaces_value(self):
        out = set_scalar("model: old\nbase_url: x\n", "model", "new")
        self.assertEqual(out, "model: new\nbase_url: x\n")

    def test_keeps_trailing_comment(self):
        """
        行尾注释必须留着。

        `model: deepseek-v4-flash  # 便宜那个` 里那句说明是用户自己写的，
        替换值的时候把它一起吃掉属于「我改了你没让我改的东西」。
        """
        out = set_scalar("model: old  # 我自己的备注\n", "model", "new")
        self.assertIn("# 我自己的备注", out)
        self.assertTrue(out.startswith("model: new"))

    def test_only_first_occurrence_needed(self):
        """重复键在 YAML 里本就非法，改第一个即可，不做多余的事。"""
        out = set_scalar("model: a\nmodel: b\n", "model", "c")
        self.assertEqual(out, "model: c\nmodel: b\n")

    def test_does_not_touch_nested_keys(self):
        """
        **缩进的键不是顶层键**。

        `worktree:` 段里的 `  cleanup_days: 7` 如果被当成顶层键改掉，
        YAML 照样解析得通，只是配置含义整个变了——这正是「漏改不报错」。
        """
        text = "worktree:\n  cleanup_days: 7\n"
        out = set_scalar(text, "cleanup_days", 99)
        self.assertIn("  cleanup_days: 7", out, "嵌套键被当成顶层键改掉了")
        # 走的是情形 3（追加到末尾），而不是改了那一行
        self.assertIn("cleanup_days: 99", out)

    def test_prefix_key_is_not_confused(self):
        """
        `model_alias:` 不能被当成 `model:`。

        前缀误伤是这类正则最典型的坑，且同样静默。
        """
        text = "model_alias: x\nmodel: y\n"
        out = set_scalar(text, "model", "z")
        self.assertIn("model_alias: x", out)
        self.assertIn("model: z", out)


class CommentedLineTest(unittest.TestCase):
    """情形 2：只有被注释掉的模板行 → 在其后插入，注释行留着。"""

    def test_inserts_after_commented_template_line(self):
        text = "# 说明第一行\n# 说明第二行\n# context_window: 1000000\n"
        out = set_scalar(text, "context_window", 65536)
        lines = out.split("\n")
        self.assertIn("# context_window: 1000000", lines)
        self.assertIn("context_window: 65536", lines)
        # 生效行紧跟在注释行之后
        self.assertEqual(
            lines.index("context_window: 65536"),
            lines.index("# context_window: 1000000") + 1,
        )

    def test_keeps_the_comment_line(self):
        """
        注释行**不是废行**——它往上还带着整段依据说明。

        删掉它等于把说明和值一起搬走了，而这正是本模块存在的理由。
        """
        text = "# context_window: 1000000\n"
        out = set_scalar(text, "context_window", 12345)
        self.assertIn("# context_window: 1000000", out)

    def test_indented_comment_is_not_a_top_level_key(self):
        """
        **缩进的注释键不算顶层。**

        模板里 `worktree:` / `classifier:` 段下面全是 `#   enabled: true`
        这种缩进注释。把它们认成顶层键的话，`set_scalar(text, "enabled", ...)`
        会在那段注释中间插一行顶层 `enabled:`——一个凭空出现的、
        位置还很误导人的配置项。
        """
        text = "# classifier:\n#   enabled: true\n"
        out = set_scalar(text, "enabled", False)
        # 走情形 3 追加到末尾，而不是插在那段注释中间
        self.assertNotIn("#   enabled: true\nenabled:", out)
        self.assertIn("enabled: false", out)


class AppendTest(unittest.TestCase):
    """情形 3：两者都没有 → 追加到末尾。"""

    def test_appends_with_provenance_note(self):
        """
        追加时带一行来源说明。

        一个用户回头看到文件末尾凭空多出几行配置时，「这是谁写的」
        是他的第一个问题。
        """
        out = set_scalar("protocol: deepseek\n", "model", "x")
        self.assertIn("model: x", out)
        self.assertIn("RhineCode 配置向导", out)

    def test_appended_result_is_loadable(self):
        out = set_scalar("protocol: deepseek\n", "model", "x")
        self.assertEqual(yaml.safe_load(out)["model"], "x")


class DumpScalarTest(unittest.TestCase):
    """值的序列化：能裸写就裸写，有歧义一律加引号。"""

    def test_plain_model_name(self):
        self.assertEqual(_dump_scalar("deepseek-v4-flash"), "deepseek-v4-flash")

    def test_url_stays_plain(self):
        """`https://api.deepseek.com` 是合法的平铺标量，不必加引号。"""
        self.assertEqual(_dump_scalar("https://api.deepseek.com"), "https://api.deepseek.com")

    def test_int(self):
        self.assertEqual(_dump_scalar(1_000_000), "1000000")

    def test_value_with_hash_is_quoted(self):
        """
        值里含 `#` 必须加引号，否则从 `#` 起会被当成注释——**密钥被截断**。
        """
        dumped = _dump_scalar("sk-abc#def")
        self.assertEqual(yaml.safe_load(f"k: {dumped}")["k"], "sk-abc#def")

    def test_yaml_keyword_is_quoted(self):
        """裸写 `no` 会被解析成布尔假，而用户要的是字符串。"""
        dumped = _dump_scalar("no")
        self.assertEqual(yaml.safe_load(f"k: {dumped}")["k"], "no")

    def test_numeric_looking_string_is_quoted(self):
        dumped = _dump_scalar("123")
        self.assertEqual(yaml.safe_load(f"k: {dumped}")["k"], "123")

    def test_embedded_quote_round_trips(self):
        dumped = _dump_scalar("it's a key")
        self.assertEqual(yaml.safe_load(f"k: {dumped}")["k"], "it's a key")

    def test_backslash_round_trips(self):
        """
        反斜杠必须原样活下来。

        这就是本函数用**单引号**而不是双引号的理由：YAML 双引号串里
        `\\n` 是换行，而单引号串不解析任何转义。
        """
        dumped = _dump_scalar("a\\nb")
        self.assertEqual(yaml.safe_load(f"k: {dumped}")["k"], "a\\nb")

    def test_leading_dash_is_quoted(self):
        dumped = _dump_scalar("-weird")
        self.assertEqual(yaml.safe_load(f"k: {dumped}")["k"], "-weird")


class SplitInlineCommentTest(unittest.TestCase):
    """
    行尾注释的切分**必须认引号**。

    朴素地按第一个 `#` 切会把 `api_key: 'abc#def'` 截断成 `'abc`，
    而截断后的配置**照样能加载**（只是 key 短了一截）——表现为
    「我填了正确的 key，却一直报 401」，几乎不可能自己查出来。
    """

    def test_plain_value_and_comment(self):
        self.assertEqual(_split_inline_comment(" abc  # 说明"), ("abc", "# 说明"))

    def test_no_comment(self):
        self.assertEqual(_split_inline_comment(" abc"), ("abc", ""))

    def test_hash_inside_single_quotes_is_not_a_comment(self):
        value, comment = _split_inline_comment(" 'abc#def'")
        self.assertEqual(value, "'abc#def'")
        self.assertEqual(comment, "")

    def test_hash_inside_double_quotes_is_not_a_comment(self):
        value, comment = _split_inline_comment(' "abc#def"  # 真注释')
        self.assertEqual(value, '"abc#def"')
        self.assertEqual(comment, "# 真注释")

    def test_hash_without_preceding_space_is_not_a_comment(self):
        """YAML 的规则：`#` 前面必须有空白才算注释起始。"""
        value, comment = _split_inline_comment(" abc#def")
        self.assertEqual(value, "abc#def")
        self.assertEqual(comment, "")


class CommentsSurviveTest(unittest.TestCase):
    """
    **本文件的核心判据**：拿真实模板跑一遍，注释一行不少（AC16）。

    这条盯的是「有人图省事把实现换成 `yaml.safe_dump`」——那样功能测试
    全都还能过（值确实写对了），只是模板里六十多行说明凭空消失，
    而**多数用户只会读那份模板**。
    """

    def _comment_lines(self, text: str) -> int:
        return sum(1 for line in text.split("\n") if line.lstrip().startswith("#"))

    def test_template_comments_all_survive_four_writes(self):
        before = self._comment_lines(_CONFIG_TEMPLATE)
        text = _CONFIG_TEMPLATE
        text = set_scalar(text, "protocol", "deepseek")
        text = set_scalar(text, "model", "deepseek-v4-pro")
        text = set_scalar(text, "base_url", "https://proxy.example.com")
        text = set_scalar(text, "api_key", "sk-test")
        text = set_scalar(text, "context_window", 1_000_000)
        after = self._comment_lines(text)
        self.assertGreaterEqual(
            after, before, f"注释被吃掉了：写前 {before} 行，写后 {after} 行"
        )

    def test_template_still_loads_after_writes(self):
        text = _CONFIG_TEMPLATE
        text = set_scalar(text, "model", "deepseek-v4-pro")
        text = set_scalar(text, "api_key", "sk-test")
        text = set_scalar(text, "context_window", 1_000_000)
        data = yaml.safe_load(text)
        self.assertEqual(data["model"], "deepseek-v4-pro")
        self.assertEqual(data["api_key"], "sk-test")
        self.assertEqual(data["context_window"], 1_000_000)

    def test_untouched_sections_are_byte_identical(self):
        """
        没被点名的部分**一个字节都不许变**。

        判据故意取得很硬：把写前写后按行比对，只允许目标那几行不同。
        """
        text = set_scalar(_CONFIG_TEMPLATE, "model", "deepseek-v4-pro")
        before = _CONFIG_TEMPLATE.split("\n")
        after = text.split("\n")
        self.assertEqual(len(before), len(after), "行数变了")
        diff = [i for i, (a, b) in enumerate(zip(before, after)) if a != b]
        self.assertEqual(len(diff), 1, f"动了不止一行：{diff}")


if __name__ == "__main__":
    unittest.main()


class ReadCurrentTest(unittest.TestCase):
    """`read_current`：给 `/setup` 预填用（AC21/AC22）。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "config.yaml"

    def tearDown(self):
        self._tmp.cleanup()

    def test_reads_model_and_base_url(self):
        self.path.write_text(
            "protocol: deepseek\nmodel: deepseek-v4-pro\n"
            "base_url: https://proxy.example.com\napi_key: sk-secret\n"
            "context_window: 4096\n",
            encoding="utf-8",
        )
        draft = read_current(self.path)
        self.assertEqual(draft.model, "deepseek-v4-pro")
        self.assertEqual(draft.base_url, "https://proxy.example.com")
        self.assertEqual(draft.context_window, 4096)

    def test_api_key_is_always_empty(self):
        """
        **绝不把真实密钥读进草稿**（AC21）。

        两个理由各自独立成立：空串在本扩展里就是「不改」的语义；
        以及读进来就意味着它会被摆到界面上、进到某个 widget 的属性里。
        """
        self.path.write_text(
            "protocol: deepseek\nmodel: m\nbase_url: u\napi_key: sk-super-secret\n",
            encoding="utf-8",
        )
        draft = read_current(self.path)
        self.assertEqual(draft.api_key, "")

    def test_missing_file_returns_none(self):
        self.assertIsNone(read_current(self.path))

    def test_broken_yaml_returns_none(self):
        """
        预填读不出来就不填，**绝不因此把向导拦住**。

        预填是锦上添花；让它有能力阻断流程等于给自己加了一个新的失败点。
        """
        self.path.write_text("[unclosed\n :: :\n", encoding="utf-8")
        self.assertIsNone(read_current(self.path))

    def test_non_utf8_returns_none(self):
        self.path.write_bytes("model: 中文\n".encode("gbk"))
        self.assertIsNone(read_current(self.path))

    def test_absent_fields_fall_back_to_defaults(self):
        self.path.write_text("protocol: deepseek\n", encoding="utf-8")
        draft = read_current(self.path)
        self.assertEqual(draft.model, DEFAULT_MODEL)
        self.assertEqual(draft.context_window, DEFAULT_CONTEXT_WINDOW)


class ApplyTest(unittest.TestCase):
    """`apply`：真正落盘（AC16–AC18、AC22）。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "config.yaml"
        self.draft = SetupDraft(
            api_key="sk-brand-new",
            base_url="https://api.deepseek.com",
            model="deepseek-v4-pro",
            context_window=1_000_000,
        )

    def tearDown(self):
        self._tmp.cleanup()

    def test_creates_from_template_when_absent(self):
        """AC17：文件不存在时以模板起手，写完能被 config.load 正常加载。"""
        written = apply(self.path, self.draft)
        self.assertEqual(written, (self.path,))
        cfg = load(str(self.path))
        self.assertEqual(cfg.model, "deepseek-v4-pro")
        self.assertEqual(cfg.api_key, "sk-brand-new")
        self.assertEqual(cfg.base_url, "https://api.deepseek.com")
        self.assertEqual(cfg.context_window, 1_000_000)

    def test_creates_parent_directory(self):
        nested = Path(self._tmp.name) / "sub" / "dir" / "config.yaml"
        apply(nested, self.draft)
        self.assertTrue(nested.exists())

    def test_preserves_user_written_sections(self):
        """
        用户手写的其它段落**原样还在**。

        `/setup` 的基底是文件现有内容而不是模板——不然一次「换个模型」
        就会把 worktree / classifier / search 三段整个抹掉。
        """
        self.path.write_text(
            "protocol: deepseek\nmodel: old\nbase_url: u\napi_key: sk-old\n"
            "\nworktree:\n  cleanup_days: 3\n  copy:\n    - .env.local\n",
            encoding="utf-8",
        )
        apply(self.path, self.draft)
        text = self.path.read_text(encoding="utf-8")
        self.assertIn("worktree:", text)
        self.assertIn("cleanup_days: 3", text)
        self.assertIn(".env.local", text)

    def test_empty_api_key_keeps_existing_one(self):
        """
        **AC22：空 api_key 表示「不改」，绝不清空原值。**

        写成 `api_key: ''` 会静默清空用户的密钥，而他要到下次启动才发现——
        那时看到的是「api_key 为空」，完全联想不到是上次在 /setup 里
        换了个模型导致的。这条单独命名就是为了让它在测试输出里显眼。
        """
        self.path.write_text(
            "protocol: deepseek\nmodel: old\nbase_url: u\napi_key: sk-original\n",
            encoding="utf-8",
        )
        apply(self.path, dataclasses.replace(self.draft, api_key=""))
        cfg = load(str(self.path))
        self.assertEqual(cfg.api_key, "sk-original", "空 api_key 把用户的密钥清掉了")
        self.assertEqual(cfg.model, "deepseek-v4-pro", "其它字段仍应被更新")

    def test_backs_up_unreadable_file_instead_of_overwriting(self):
        """
        **绝不覆盖一份读不出来的文件**（T4 实测撞出来的边界）。

        Windows 中文环境下手写的 GBK 配置正是这个形态：`config.load` 读不出来，
        `trigger.classify` 判 INVALID、向导被拉起来——但内容对用户有价值。
        """
        original = "# 我自己写的说明\nmodel: 老配置\n".encode("gbk")
        self.path.write_bytes(original)

        written = apply(self.path, self.draft)

        backup = self.path.with_name(self.path.name + ".bak")
        self.assertIn(backup, written, "备份路径没有交回去，界面就没法告诉用户存哪了")
        self.assertTrue(backup.exists())
        self.assertEqual(backup.read_bytes(), original, "备份内容被动过了")
        # 新文件是好的
        self.assertEqual(load(str(self.path)).model, "deepseek-v4-pro")

    def test_repeated_backups_do_not_clobber_each_other(self):
        """
        重名时递增编号，**不覆盖已有备份**。

        用户反复走向导时，第二次备份把第一次的覆盖掉，等于备份没做。

        ⚠ **这里用写死的非法字节，而不是 `"...".encode("gbk")`，是实测踩出来的。**
        本条一开始用 `"first: 一\\n".encode("gbk")` 造样本，结果没触发备份——
        `一` 的 GBK 编码是 `D2 BB`，而 `D2` 恰好是合法的 UTF-8 双字节前导、
        `BB` 恰好是合法的后续字节，**整段 GBK 字节流是合法 UTF-8**
        （解出来是乱码，但一个错都不报）。`中` 的 `D6 D0` 就会报错。

        这条事实同时说明「GBK 配置文件」其实有**两种**形态，而本扩展只处理得了一种：
        解不出来的那种走备份路径；**解得出来但是乱码的那种根本无从察觉**——
        配置会带着一堆乱码值正常加载。后者不在本扩展范围内（也没有可靠的检测手段），
        记在这里免得下一个人以为已经覆盖到了。
        """
        self.path.write_bytes(b"\xff\xfe first invalid\n")
        apply(self.path, self.draft)
        self.path.write_bytes(b"\xff\xfe second invalid\n")
        apply(self.path, self.draft)

        first = self.path.with_name(self.path.name + ".bak")
        second = self.path.with_name(self.path.name + ".bak1")
        self.assertTrue(first.exists() and second.exists())
        self.assertNotEqual(first.read_bytes(), second.read_bytes())

    def test_template_comments_survive_a_real_apply(self):
        """AC16 的落盘版：真写一次文件，注释仍在。"""
        apply(self.path, self.draft)
        text = self.path.read_text(encoding="utf-8")
        comment_lines = sum(1 for ln in text.split("\n") if ln.lstrip().startswith("#"))
        self.assertGreater(comment_lines, 30, f"模板注释被吃掉了，只剩 {comment_lines} 行")
