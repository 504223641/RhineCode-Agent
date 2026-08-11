"""
c5 结构化系统提示与缓存策略的单元测试。

覆盖（对应 spec 验收标准）：
- 拼装器：七模块按序、空行分隔、空槽跳过、新增模块按 priority 插入（AC1/AC3/AC4）
- 环境信息：render 含四项字段（AC2）
- 提醒：Plan Mode 按轮节奏、<system-reminder> 标签（AC8/AC9）
- collector：usage 采集缓存命中字段（F10 上游）
"""

import unittest

from rhinecode.config import Config
from rhinecode.agent.collector import StreamCollector
from rhinecode.agent.prompt import (
    PromptModule,
    SystemPromptBuilder,
    build_default_prompt,
    build_system_reminder,
    collect_environment,
    plan_toggle_instruction,
    PLAN_FULL_INSTRUCTION,
    PLAN_BRIEF_INSTRUCTION,
)


def _env():
    """构造一份测试用环境信息（固定 model/protocol，工作目录用占位路径）。"""
    config = Config(
        protocol="deepseek", model="m-test", base_url="http://t", api_key="k", debug_log=False
    )
    return collect_environment(config, "/proj/root")


class BuilderTests(unittest.TestCase):
    def test_fixed_modules_in_order(self) -> None:
        """AC1：stable 含全部固定模块，按身份→...→文本输出顺序、以空行分隔。

        ⚠ 这些标记用 `str.find` 取**首次**出现位置。新增模块的正文若碰巧包含
        别的模块的标记词，会把那个模块的判定位置提前，造成难以排查的假失败——
        写新模块文案时要避开这张表。
        """
        assembled = build_default_prompt(_env())
        stable = assembled.stable
        # 取每个模块文本的特征片段，断言它们按既定顺序出现
        markers = [
            "你是 Rhine",              # 身份
            "<system-reminder>",        # 系统约束（讲到该标签）
            "调研 → 行动",              # 任务模式
            "交付标准",                 # 交付标准（prompt-hardening 新增，35）
            "有副作用的操作",           # 动作执行
            "工具使用准则",             # 工具使用
            "保持简洁",                 # 语气风格
            "Markdown",                 # 文本输出
        ]
        positions = [stable.find(m) for m in markers]
        self.assertTrue(all(p >= 0 for p in positions), f"有模块缺失: {positions}")
        self.assertEqual(positions, sorted(positions), "模块顺序不符合优先级")
        # 模块之间以空行分隔
        self.assertIn("\n\n", stable)

    def test_env_in_dynamic_not_stable(self) -> None:
        """AC2/AC5：环境信息进 dynamic（动态通道），不进 stable（可缓存前缀）。"""
        assembled = build_default_prompt(_env())
        self.assertIn("/proj/root", assembled.dynamic)
        self.assertNotIn("/proj/root", assembled.stable)

    def test_empty_optional_slots_skipped(self) -> None:
        """AC3：可选空槽不产生任何文本，也不产生多余空行（无连续三空行）。"""
        assembled = build_default_prompt(_env())
        self.assertNotIn("自定义指令", assembled.stable)
        self.assertNotIn("长期记忆", assembled.dynamic)
        self.assertNotIn("\n\n\n", assembled.stable)
        self.assertNotIn("\n\n\n", assembled.dynamic)

    def test_new_module_inserted_by_priority(self) -> None:
        """AC4：新增一个 priority 最小的模块，无需改已有模块即排到 stable 最前。"""
        builder = SystemPromptBuilder()
        builder.add(PromptModule(name="后", priority=50, cacheable=True, content="BBB"))
        builder.add(PromptModule(name="前", priority=5, cacheable=True, content="AAA"))
        stable = builder.build().stable
        self.assertEqual(stable, "AAA\n\nBBB")


class DeliveryModuleTests(unittest.TestCase):
    """
    「交付标准」模块的四层意思逐条钉住（prompt-hardening 扩展）。

    为什么要逐条断言而不是只查模块在不在：这四条各自对应一类**真实的模型失败**，
    而它们看起来都像「废话」，后来的人很容易在精简文案时顺手删掉其中一两条。
    删了不报错——提示词照常拼装、测试照常绿，只是模型重新开始把 70% 说成完成。

    ⚠ 断言用「意思」而不是整句原文：措辞是要反复打磨的东西，逐字断言会让
    每次调措辞都碎一批测试，而人的第一反应是把断言放宽成谁都能过。
    """

    def _delivery(self) -> str:
        """取出交付标准模块的正文（按 priority=35 定位，不依赖它在列表里的位置）。"""
        from rhinecode.agent.prompt.modules import fixed_modules

        matched = [m for m in fixed_modules() if m.priority == 35]
        self.assertEqual(len(matched), 1, "priority=35 的交付标准模块应恰好有一个")
        return matched[0].content

    def test_scope_is_the_deliverable(self) -> None:
        """第一层：范围就是交付物，不许私自收窄——防「三件做两件」那类静默缩范围。"""
        text = self._delivery()
        self.assertIn("范围", text)
        self.assertIn("收窄", text)

    def test_blocked_part_must_be_named(self) -> None:
        """第二层：被阻塞时其余全做完 + 明确说出哪些没做，且缩范围是用户的决定。"""
        text = self._delivery()
        self.assertIn("阻塞", text)
        self.assertIn("没做", text)
        self.assertIn("用户的决定", text)

    def test_objection_then_still_deliver(self) -> None:
        """第三层：有异议先说顾虑再照做，用户重申后不反复劝说。

        缺了它模型会停在「我建议不要这么做」，把决定权从用户手里拿走。
        """
        text = self._delivery()
        self.assertIn("异议", text)
        self.assertIn("重申", text)

    def test_report_failures_honestly(self) -> None:
        """第四层前半：测试失败要说失败并附输出，跳过要说跳过。"""
        text = self._delivery()
        self.assertIn("失败就说失败", text)
        self.assertIn("跳过", text)

    def test_no_hedging_on_verified_work(self) -> None:
        """第四层后半（反方向）：做完并验证过的要直接陈述，不加对冲词。

        ⚠ 这条最容易被当成冗余删掉——它和上一条**看起来矛盾**（一个要求
        承认不确定、一个禁止表达不确定）。实际是一对：只留前半会让模型对
        做对的事也加「应该可以」，满篇对冲等于没有信号。
        """
        text = self._delivery()
        self.assertIn("对冲词", text)
        self.assertIn("应该可以", text)


class EnvironmentTests(unittest.TestCase):
    def test_render_contains_fields(self) -> None:
        """AC2：环境信息 render 含工作目录、平台、日期、Git 分支、模型等字段。"""
        text = _env().render()
        self.assertIn("工作目录", text)
        self.assertIn("操作系统/平台", text)
        self.assertIn("当前日期", text)
        self.assertIn("当前 Git 分支", text)
        self.assertIn("m-test", text)
        self.assertIn("deepseek", text)

    def test_git_branch_absent_says_none(self) -> None:
        """无分支信息时（_env 用不存在的 /proj/root，非 git 仓库）分支字段渲染为「无」。"""
        text = _env().render()
        self.assertIn("当前 Git 分支：无", text)


class ReminderTests(unittest.TestCase):
    def test_plan_cadence(self) -> None:
        """AC9：i=1/4/7→完整版，i=2/3/5/6→精简版，active=False→None。"""
        for i in (1, 4, 7):
            self.assertEqual(plan_toggle_instruction(i, True), PLAN_FULL_INSTRUCTION)
        for i in (2, 3, 5, 6):
            self.assertEqual(plan_toggle_instruction(i, True), PLAN_BRIEF_INSTRUCTION)
        self.assertIsNone(plan_toggle_instruction(1, False))

    def test_reminder_wrapped_in_tag(self) -> None:
        """AC8：reminder 被 <system-reminder> 包裹；都为空时返回 None。"""
        text = build_system_reminder("环境信息", "提醒")
        self.assertTrue(text.startswith("<system-reminder>"))
        self.assertTrue(text.endswith("</system-reminder>"))
        self.assertIn("环境信息", text)
        self.assertIn("提醒", text)
        self.assertIsNone(build_system_reminder("", None))


class CollectorCacheTests(unittest.TestCase):
    def test_to_usage_picks_cache_fields(self) -> None:
        """F10 上游：从含缓存字段的 usage 字典里取到命中/未命中，缺失按 0。"""
        usage = StreamCollector._to_usage(
            {"prompt_tokens": 100, "prompt_cache_hit_tokens": 64, "prompt_cache_miss_tokens": 36}
        )
        self.assertEqual(usage.prompt_cache_hit_tokens, 64)
        self.assertEqual(usage.prompt_cache_miss_tokens, 36)
        # 缺失字段按 0
        self.assertEqual(StreamCollector._to_usage({}).prompt_cache_hit_tokens, 0)


if __name__ == "__main__":
    unittest.main()
