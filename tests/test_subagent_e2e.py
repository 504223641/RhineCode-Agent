"""
子 Agent 的端到端验收（c13，覆盖 AC17a / AC19a-c / AC21b / AC23）。

## 与 `test_subagent_integration.py` 的分工

那个文件手工拼装协调层与服务，验的是**模块之间的接线**。
本文件走**真实装配链路 `build_app`**，验的是「装配层有没有把它们接对」——
这是单元测试证明不了的一类问题：每个零件都对，组装顺序错了照样跑不起来。

具体来说，本文件能抓到而单元测试抓不到的：

- `run_agent` 有没有真的注册进工具中心、且在 `session_start` 快照之前；
- 角色清单有没有真的进到发给模型的系统提示里；
- 子 Agent 的 `stable` 是不是角色正文（而不是主对话的八模块）；
- 结论有没有真的出现在**后续几轮**的请求体里。

## 判据取自「发给模型的东西」

断言对象是假 Provider 收到的 `system` 与 `messages` 原文，而不是中间状态。
理由：模型最终看到什么，才是这一章是否成立的唯一判据。
"""

from __future__ import annotations

import json
import tempfile
import threading
import time
import unittest
from pathlib import Path

from rhinecode.agent.prompt.modules import IDENTITY
from rhinecode.bootstrap import build_app
from rhinecode.config import Config
from rhinecode.provider.base import BaseProvider, StreamChunk, ToolCall
from rhinecode.trace import create_recorder

_ROLE = """---
name: finder
description: 需要在项目里查找信息时用它。
tools: read_file, glob_files
permission_mode: strict
---
你是查找员。最后一段必须是自包含的结论。
"""

# 子 Agent 的 stable 就是角色正文，用它的开头判断「这一轮是谁发的」。
_ROLE_BODY_HEAD = "你是查找员"

# 主对话的 stable 一定以身份模块（八模块的第一个）开头，用它**正向**认出主对话。
#
# ⚠ **刻意取自产品里的那份常量，不是抄一句字面量。** 抄字面量的话，
# 身份模块哪天改了措辞，这里会静默退化成「谁都不算主对话」——
# `main_bodies` 变成空列表，下游断言全部报 IndexError，
# 而报错位置离真正的原因隔着整个文件。
_MAIN_STABLE_HEAD = IDENTITY[:24]

# 子 Agent 闸门的兜底上限（秒）。**不是判据，是防死锁的保险**。
#
# 判据是「主对话返回时子 Agent 还没跑完」这个**顺序**，与机器快慢无关。
# 但如果哪天产品退化成「委派阻塞主对话」，主对话会等子 Agent、子 Agent 会等
# 这个闸门、而闸门要等主对话返回之后测试才放行——三方互等就是死锁。
# 有了这个上限，那种情况下子 Agent 会自己走出来，用例**当场红**（而不是挂住）。
# 挂住的测试比失败的测试难查得多：它没有失败信息，只有一个超时的 CI。
_SUB_GATE_TIMEOUT = 5.0


class _ScriptedProvider(BaseProvider):
    """
    主对话第 1 轮委派，之后说话；子 Agent 直接给结论。

    记下每一轮的 `system` / `messages` / `tools`，供断言。
    """

    def __init__(self, background: bool = False, gate_sub: bool = False) -> None:
        self.turns = 0
        self.systems: list[str] = []
        self.bodies: list[str] = []
        # 主对话那些轮次的请求体，**与 `bodies` 分开收**。
        #
        # ⚠ `bodies` 由**三个**线程共同追加，因此 `bodies[-1]` 不一定是主对话
        # 最后那一轮。用 `bodies[-1]` 断言会得到一个**间歇性失败**的测试，
        # 而失败信息看起来像产品出了问题（「结论怎么不在请求里」），极易误判。
        #
        # 三个生产者分别是：
        #   ① 主对话线程    —— stable 是八模块，以 `_MAIN_STABLE_HEAD` 开头
        #   ② 子 Agent 线程 —— stable 是角色正文，以 `_ROLE_BODY_HEAD` 开头
        #   ③ `rhine-memory` 线程（c9 自动记忆）—— stable 是记忆管理器提示词
        #
        # ⚠ **③ 是 `test_conclusion_delivered_exactly_once` 那次偶发红的根因。**
        # 原判据是「**不是**子 Agent 就算主对话」这种反向写法，于是记忆线程的
        # 请求被当成了主对话那一轮。记忆线程由 `on_natural_stop` 在每轮自然结束后
        # 异步起（daemon 线程，见 `memory/manager.py`），落点时刻**完全随机**：
        # 它落在最后一次主对话请求之后时，`main_bodies[-1]` 就变成那份记忆请求体
        # ——里面当然没有 `<subagent-result`，于是报 `AssertionError: 0 != 1`，
        # 看起来像交付逻辑坏了，而交付其实一次不多一次不少地发生过。
        # 单跑时机器空闲，记忆线程往往赶在下一轮主请求之前跑完（故 18 次全绿）；
        # 全量跑几十个线程抢 CPU，它就经常落到后面（故 3 次里红 2 次）。
        #
        # 判据因此改成**正向**：只有以八模块身份段开头的才算主对话。
        # 反向判据的毛病是「将来多一个生产者就又错一次」，正向的不会。
        # 反证见 `ProviderStubClassificationTest`。
        self.main_bodies: list[str] = []
        self.tool_names: list[list[str]] = []
        self._background = background
        # 记录用的锁。**不是为了性能，是为了让四个平行列表的下标对得上。**
        #
        # `systems[i]` 与 `tool_names[i]` 必须指同一次请求——`sub_turn_index()`
        # 拿前者算下标、断言拿后者取值，全靠这个对应关系。三个线程各自
        # 「append 完 systems 再 append tool_names」时可以交错，一旦交错，
        # 下标就错位到别人的请求上，而**列表长度仍然相等**、断言仍然跑得通，
        # 只是验的对象悄悄换了人——正是本文件反复踩过的那类无声失败。
        self._record_lock = threading.Lock()
        # 子 Agent 的**闸门**：它在这里阻塞，直到测试显式放行。
        #
        # ⚠ 这里刻意**不用 `time.sleep`**。原写法是让子 Agent 睡 0.15 秒，
        # 再断言主对话在 0.12 秒内返回——那是拿**挂钟时间**当判据，
        # 而 0.12 这个数字是在一台空闲机器上量出来的。全量跑（2200+ 项、
        # 几十个线程 + 真实子进程 + 文件 IO）时一次线程调度延迟就能顶穿它，
        # 于是用例**偶发**变红。偶发失败比稳定失败更坏：它训练所有人忽略失败。
        #
        # 换成事件之后，判据从「主对话在 0.12 秒内返回」变成
        # 「**子 Agent 还没跑完，主对话就已经返回了**」——那才是这条用例
        # 真正想说的话，而且与机器快慢完全无关。
        self._sub_gate = threading.Event()
        if not gate_sub:
            # 不需要卡子 Agent 的用例：闸门一开始就是开的，一秒都不等。
            self._sub_gate.set()

    def stream_chat(self, messages, thinking_effort="off", tools=None, system=None):
        stable = system or ""
        body = "\n".join(str(getattr(m, "content", "") or "") for m in messages)
        is_sub = stable.startswith(_ROLE_BODY_HEAD)
        is_main = stable.startswith(_MAIN_STABLE_HEAD)

        # 四个列表在同一个临界区里一起追加，保证下标一一对应（见 `_record_lock`）。
        with self._record_lock:
            # `turns` 只数**主对话**的轮次——「第 1 轮委派、之后说话」这条脚本
            # 说的就是主对话。子 Agent 与记忆线程的请求不该把它往前推。
            if is_main:
                self.turns += 1
                self.main_bodies.append(body)
            self.systems.append(stable)
            self.bodies.append(body)
            self.tool_names.append(
                sorted(t["function"]["name"] for t in (tools or []))
            )
            turns = self.turns

        if is_sub:
            # 卡在闸门上，直到测试放行；`_SUB_GATE_TIMEOUT` 只是防死锁的兜底。
            self._sub_gate.wait(timeout=_SUB_GATE_TIMEOUT)
            yield StreamChunk(type="text", content="结论：在 a.py 与 b.py 各有一处。")
            yield StreamChunk(type="done")
            return

        if turns == 1:
            yield StreamChunk(
                type="tool_call",
                tool_call=ToolCall(
                    id="c1",
                    name="run_agent",
                    arguments={
                        "type": "role",
                        "agent": "finder",
                        "task": "找出所有 X",
                        "background": self._background,
                    },
                ),
            )
            yield StreamChunk(type="done")
            return

        yield StreamChunk(type="text", content="收到。")
        yield StreamChunk(type="done")

    # ---- 便捷查询 ----

    def release_sub(self) -> None:
        """放行卡在闸门上的子 Agent。对未设闸门的替身是空操作（幂等）。"""
        self._sub_gate.set()

    def sub_turn_index(self) -> int:
        for i, s in enumerate(self.systems):
            if s.startswith(_ROLE_BODY_HEAD):
                return i
        return -1


class E2EBase(unittest.TestCase):
    def _build(self, provider, recorder=None):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        user_dir = root / "user"
        (user_dir / "agents").mkdir(parents=True)
        (user_dir / "agents" / "finder.md").write_text(_ROLE, encoding="utf-8")

        cfg = Config(protocol="deepseek", model="m", api_key="k", base_url="")
        result = build_app(
            cfg,
            user_dir=user_dir,
            provider_factory=lambda c: provider,
            recorder=recorder,
        )
        self.addCleanup(result.cleanup, "normal_exit")
        return result, root

    def _settle(self, manager, timeout: float = 10.0) -> None:
        """
        等到全部子 Agent 走到终态。

        ⚠ **超时必须明确失败，绝不能静默返回。** 原写法撞上超时就直接 `return`，
        于是「子 Agent 压根没跑完」会一路飘到下游，表现成
        `test_conclusion_delivered_exactly_once` 里一句
        `AssertionError: 0 != 1`——读的人完全看不出真正发生了什么，
        只会以为交付逻辑坏了，而实际是这个等待函数提前放弃了。

        这与「观测设施绝不能撒谎」是同一条纪律：一个悄悄放弃的等待函数，
        等于把「没发生」伪装成「发生了但结果不对」。
        """
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            tasks = manager.subagent_service.tasks.snapshot()
            if tasks and all(t.status.is_terminal for t in tasks):
                return
            time.sleep(0.01)

        tasks = manager.subagent_service.tasks.snapshot()
        self.fail(
            f"等了 {timeout} 秒，子 Agent 仍未走到终态："
            f"{[(t.label, t.status.value) for t in tasks] or '一个任务都没登记'}"
        )


class ForegroundE2ETest(E2EBase):
    """前台委派：装配 → 委派 → 子 Agent 跑完 → 结论作为工具结果回灌。"""

    def setUp(self) -> None:
        self.provider = _ScriptedProvider()
        self.result, _ = self._build(self.provider)
        self.manager = self.result.manager
        list(self.manager.submit_user_message("找一下"))

    def test_run_agent_registered(self) -> None:
        self.assertIn("run_agent", self.result.tool_registry.names())

    def test_agent_index_reaches_the_model(self) -> None:
        """
        角色清单要真的出现在发给模型的**系统提示**里。

        只断言 `_agent_index_text()` 非空是不够的——那证明不了它被拼进了
        `build_default_prompt` 的产物、更证明不了它进了 `system` 参数。
        """
        self.assertIn("finder", self.provider.systems[0])
        # 表头的标志句。2026-08-10 触发口径反转后由「而不是自己动手做」改成本句，
        # 理由见 `test_subagent_tool.py::SameVoiceTest` 的类 docstring。
        self.assertIn("默认不要委派", self.provider.systems[0])

    def test_subagent_stable_is_the_role_body_only(self) -> None:
        """AC7a：子 Agent 的 stable 是角色正文，不含主对话的八模块。"""
        idx = self.provider.sub_turn_index()
        self.assertGreaterEqual(idx, 0, "子 Agent 应当真的跑过")
        self.assertEqual(self.provider.systems[idx], "你是查找员。最后一段必须是自包含的结论。")

    def test_subagent_toolset_is_the_whitelist_plus_collaboration(self) -> None:
        """
        角色白名单决定「它能做什么」，**协作工具除外**。

        ⚠ **本条断言在 c15 被修改过**，这是 C13 契约的一次真实变更，
        不是替身跟进——原文断言最终工具集**恰好等于**白名单。

        改的理由：c15 spec F22 要求协作工具对**全部**子 Agent 可见，
        而 `explorer` / `planner` 这类只读角色都声明了白名单，
        交集之后一个协作工具都不剩——「只读调研员 + 执行者」这种最自然的
        分工因此根本跑不通（调研员连「我查完了」都说不出口）。
        真实模型验收实测撞到过。

        白名单**本身仍然生效**：下面那条 `write_file` 的断言钉住这一点，
        豁免只覆盖协作工具，不是把白名单整个作废。
        """
        idx = self.provider.sub_turn_index()
        names = set(self.provider.tool_names[idx])
        self.assertLessEqual({"glob_files", "read_file"}, names, "白名单里的照常给")
        self.assertLessEqual(
            {"task_create", "task_list", "task_get", "task_update", "send_message"},
            names,
            "协作工具豁免白名单（c15 F22）",
        )
        self.assertNotIn("write_file", names, "白名单之外的非协作工具仍然拿不到")
        self.assertNotIn("run_command", names)

    def test_delegation_tool_visible_to_main_not_to_sub(self) -> None:
        idx = self.provider.sub_turn_index()
        self.assertIn("run_agent", self.provider.tool_names[0])
        self.assertNotIn("run_agent", self.provider.tool_names[idx])
        self.assertNotIn("load_skill", self.provider.tool_names[idx])

    def test_conclusion_returned_as_tool_result(self) -> None:
        tasks = self.manager.subagent_service.tasks.snapshot()
        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0].status.value, "completed")
        self.assertIn("a.py", tasks[0].conclusion)
        # 工具结果回灌进了第 2 轮的请求体
        self.assertIn("a.py", self.provider.main_bodies[-1])

    def test_foreground_waits_for_the_subagent(self) -> None:
        """
        **`test_background_does_not_block_main` 的反证。**

        同一个判据（「主对话返回时，子 Agent 跑完了没有」），相反的结论：
        `background` 缺省为假时是「我要这个结果」，循环准备收工前会停下来等它，
        因此主对话返回时子 Agent **必然已经终态**。

        为什么必须有这条：少了它，一个「委派之后永远不等」的错误实现
        照样能让 background 那条通过——那条只断言「没等」，
        而「永远不等」当然也满足「没等」。两条合起来才说明判据真的分得清。

        ⚠ 这条反证**不需要改产品代码**。用「改坏产品代码看它红不红」来验判据是
        一次性的手工动作，做完就没了；把反证写成一条常驻用例，
        判据的分辨力才会被长期钉住。
        """
        tasks = self.manager.subagent_service.tasks.snapshot()
        self.assertEqual(len(tasks), 1)
        self.assertTrue(
            tasks[0].status.is_terminal,
            "前台委派（awaited）时主对话应当等到子 Agent 跑完才收工",
        )

    def test_main_engine_mode_unchanged(self) -> None:
        """
        AC14b 的端到端侧判据：角色声明 strict，**主引擎的档位不受影响**。

        auto-plan 扩展起启动缺省档是 `permissive`（auto 预设），此前是 `default`。
        判据本身一个字没变——要看的是「子 Agent 的声明档没有反过来改主引擎」，
        而不是主引擎具体是哪一档。
        """
        self.assertEqual(self.manager.permission_engine.mode.value, "permissive")

    def test_agents_report_renders(self) -> None:
        report = self.manager.agents_report()
        self.assertIn("finder", report)
        self.assertIn("严格", report)
        self.assertIn(
            self.manager.subagent_service.tasks.snapshot()[0].task_id, report
        )


class BackgroundE2ETest(E2EBase):
    """AC17a / AC19：后台委派 → 通知 → 交付 → 后续几轮仍能引用。"""

    def setUp(self) -> None:
        # 子 Agent 卡在闸门上，由每条用例决定什么时候放行——**时序因此是确定的**。
        self.provider = _ScriptedProvider(background=True, gate_sub=True)
        self.result, _ = self._build(self.provider)
        self.manager = self.result.manager

    def test_background_does_not_block_main(self) -> None:
        """
        `background=true` 时主对话不等子 Agent，且**循环也不为它停留**。

        c13 修订注记：这条原本断言回灌文本含「后台」。新语义下委派**永远**
        立即返回（所以「转入后台」这个说法本身没了），`background` 表达的
        是「这次我不要这个结果」——文案随之改成「本轮不会为它停留」。

        ⚠ **判据是顺序，不是时间。** 原写法让子 Agent 睡 0.15 秒再断言主对话
        在 0.12 秒内返回，在全量并发下偶发失败（实测 `0.203 not less than 0.12`）。
        那个数字量自一台空闲机器，一次线程调度延迟就能顶穿它。
        **不能靠调大阈值解决**：那只降低偶发概率，而且阈值一旦放宽到 1 秒，
        这条用例就再也验不出「委派阻塞了主对话」——一次真的阻塞往往就是几百毫秒。

        现在的判据是「主对话已经返回，而子 Agent 还卡在闸门上没跑完」，
        它是一个**顺序**事实，与机器快慢无关。

        反证在 `ForegroundE2ETest.test_foreground_waits_for_the_subagent`：
        同一个判据、相反的结论。少了它，一个「永远不等」的错误实现
        也能让本条通过。
        """
        list(self.manager.submit_user_message("找一下"))

        # 主对话已经返回了。此刻子 Agent 必然还没跑完——它卡在闸门上，
        # 而放行动作在下面，还没执行。
        tasks = self.manager.subagent_service.tasks.snapshot()
        self.assertEqual(len(tasks), 1, "委派应当已经登记了任务")
        self.assertFalse(
            tasks[0].status.is_terminal,
            "主对话不该等到子 Agent 跑完才返回（background=true）",
        )
        self.assertIn("不会为它停留", self.provider.main_bodies[-1])

        self.provider.release_sub()
        self._settle(self.manager)

    def test_two_consumption_lines_are_independent(self) -> None:
        list(self.manager.submit_user_message("找一下"))
        self.provider.release_sub()
        self._settle(self.manager)

        self.assertEqual(len(self.manager.drain_subagent_notifications()), 1)
        self.assertEqual(self.manager.drain_subagent_notifications(), ())
        # 通知取走了，交付线照样能拿到
        list(self.manager.submit_user_message("继续"))
        self.assertIn("在 a.py 与 b.py", self.provider.main_bodies[-1])

    def test_conclusion_survives_to_the_round_after_next(self) -> None:
        """
        **AC19c——本章最容易做错的一条。**

        只验「下一轮能引用」是不够的：用一次性的系统提醒实现也能过那一条，
        但第三轮就会失败。这条断言是「结论必须进历史」这个决策的唯一有效判据。
        """
        list(self.manager.submit_user_message("找一下"))
        self.provider.release_sub()
        self._settle(self.manager)

        list(self.manager.submit_user_message("继续"))
        self.assertIn("在 a.py 与 b.py", self.provider.main_bodies[-1], "第二轮应含结论")

        list(self.manager.submit_user_message("再继续"))
        self.assertIn("在 a.py 与 b.py", self.provider.main_bodies[-1], "第三轮仍应含结论")

    def test_conclusion_delivered_exactly_once(self) -> None:
        """
        结论只该进历史一次。

        ⚠ **这条原本也是偶发红**（`AssertionError: 0 != 1`），而且失败信息
        极具误导性——看起来像「交付逻辑坏了」，实际是 `_settle` 撞上超时后
        **静默返回**，子 Agent 压根还没跑完就往下走了。根因已在 `_settle`
        里修掉（超时改成明确失败），这里再用闸门把时序定死：
        子 Agent 只在放行之后才结束，因此「该交付」这件事必然已经发生。

        交付有两条路径，共用同一个**消费型**队列（`take_deliverables`）：
        闸门的迭代级注入、以及跨用户消息的兜底 `_deliver_subagent_results`。
        「恰好一次」正是靠那个队列取走即置位来保证的。
        """
        list(self.manager.submit_user_message("找一下"))
        self.provider.release_sub()
        self._settle(self.manager)
        list(self.manager.submit_user_message("继续"))
        list(self.manager.submit_user_message("再继续"))

        self.assertEqual(
            self.provider.main_bodies[-1].count("<subagent-result"),
            1,
            "结论只该被交付一次，重复会让同一段内容在历史里出现多遍",
        )


class ProviderStubClassificationTest(unittest.TestCase):
    """
    替身的「这一轮是谁发的」判据本身的反证。

    ## 为什么要给一个测试替身写测试

    因为本文件几乎所有断言都写成 `main_bodies[-1]`，而那个下标的**含义**
    完全由 `_ScriptedProvider` 的分类判据决定。判据一旦错，断言不会报错，
    只会**换一个对象去验**——`test_conclusion_delivered_exactly_once` 那次
    偶发红就是这么来的：记忆线程的请求被算成了主对话，
    `main_bodies[-1]` 于是指向一份记忆请求体，报出 `0 != 1`。

    下面三条各钉一个生产者，**都不起线程、不看时序**：直接把三份真实的
    system 提示喂给替身，看它分到哪个桶里。
    """

    def _stub(self):
        return _ScriptedProvider()

    def _feed(self, provider, system: str) -> None:
        """喂一次请求并把流消费干净（替身是生成器，不消费就什么都不会发生）。"""
        list(provider.stream_chat([], system=system))

    def test_main_prompt_counts_as_main(self) -> None:
        """正向：八模块提示词算主对话。"""
        provider = self._stub()
        self._feed(provider, IDENTITY + "\n（后面还有别的模块）")
        self.assertEqual(len(provider.main_bodies), 1)

    def test_role_body_does_not_count_as_main(self) -> None:
        """反向①：子 Agent 的角色正文不算主对话（C13 起就有的判据）。"""
        provider = self._stub()
        self._feed(provider, "你是查找员。最后一段必须是自包含的结论。")
        self.assertEqual(provider.main_bodies, [])

    def test_memory_prompt_does_not_count_as_main(self) -> None:
        """
        反向②——**本次修的就是这一条**。

        ⚠ 喂的是 `build_memory_request` **真实产出**的 system，不是一句手写的
        近似文本。手写的话，记忆提示词哪天改了开头，这条护栏会继续通过，
        而产品里的分类会重新错掉——护栏必须钉在真正的生产者上。
        """
        from rhinecode.memory.memory_updater import build_memory_request
        from rhinecode.provider.base import Message

        system, _req = build_memory_request([Message(role="user", content="x")], "", "")
        provider = self._stub()
        self._feed(provider, system)
        self.assertEqual(
            provider.main_bodies,
            [],
            "记忆线程的请求不是主对话那一轮，算进去会让 main_bodies[-1] 随机漂移",
        )


class TraceE2ETest(E2EBase):
    """AC23：从 trace 能完整复现一次委派，且与主对话分作用域。"""

    def setUp(self) -> None:
        self.provider = _ScriptedProvider()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.trace_path = Path(tmp.name) / "t.jsonl"
        recorder = create_recorder(self.trace_path)
        self.result, _ = self._build(self.provider, recorder=recorder)
        list(self.result.manager.submit_user_message("找一下"))
        self.result.cleanup("normal_exit")
        self.records = [
            json.loads(line)
            for line in self.trace_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]

    def test_lifecycle_events_present(self) -> None:
        types = {r.get("type") for r in self.records}
        self.assertIn("subagent_start", types)
        self.assertIn("subagent_end", types)

    def test_subagent_has_its_own_scope(self) -> None:
        scopes = {r.get("scope") for r in self.records}
        self.assertTrue(
            any(s and s.startswith("subagent:") for s in scopes),
            f"应有 subagent: 作用域，实际：{scopes}",
        )

    def test_subagent_requests_not_counted_as_main(self) -> None:
        """
        AC21b / AC23：子 Agent 的模型请求**不算进主对话**。

        混在一起的话，读 trace 的人会看到「用户只说了一句话，却发了三轮请求」
        的假象，无从判断哪一轮是谁发的。
        """
        sub = [
            r for r in self.records
            if r.get("type") == "api_request"
            and str(r.get("scope", "")).startswith("subagent:")
        ]
        main = [
            r for r in self.records
            if r.get("type") == "api_request" and r.get("scope") == "main"
        ]
        self.assertGreaterEqual(len(sub), 1)
        self.assertGreaterEqual(len(main), 1)

    def test_start_event_carries_enough_to_reproduce(self) -> None:
        start = next(r for r in self.records if r.get("type") == "subagent_start")
        for key in ("kind", "agent", "task_id", "task", "tool_count"):
            with self.subTest(field=key):
                self.assertIn(key, start)

    def test_end_event_carries_outcome(self) -> None:
        end = next(r for r in self.records if r.get("type") == "subagent_end")
        self.assertEqual(end.get("status"), "completed")
        for key in ("task_id", "turns", "stop_reason"):
            with self.subTest(field=key):
                self.assertIn(key, end)


if __name__ == "__main__":
    unittest.main()
