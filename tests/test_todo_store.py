"""
主对话待办清单的存放层单测（todo-list 扩展 T4，覆盖 AC1–AC5）。

重点四处：**覆写真的是覆写**、**被拒时一个字节都不改**、
**超上限是拒绝不是截断**、**版本号只在成功时递增**。

⚠ 第二、三条是本文件里最要紧的两条：它们钉住的都是「静默降级」形态——
边解析边写会留下半张表，而调用方拿到的是「失败」；截断会让模型以为
整份写进去了，而清单上少了几条。**两种错法都不报错。**
"""

from __future__ import annotations

import threading
import unittest

from rhinecode.todo.models import TodoItem, TodoState
from rhinecode.todo.store import MAX_ITEMS, TodoStore


def titles(store: TodoStore) -> list[str]:
    return [i.title for i in store.snapshot()]


def states(store: TodoStore) -> list[TodoState]:
    return [i.state for i in store.snapshot()]


class ReplaceSemanticsTest(unittest.TestCase):
    """AC1：整表覆写真的是覆写，不是合并。"""

    def setUp(self) -> None:
        self.store = TodoStore()

    def test_second_replace_wins_entirely(self) -> None:
        """
        先 3 条再 2 条 → **恰好是那 2 条**。

        这条是覆写语义的全部内容。写成「合并」的话第一次那 3 条会残留，
        而模型以为自己已经把清单缩短了——两边对同一份数据的认知不一致。
        """
        self.store.replace([{"title": "a"}, {"title": "b"}, {"title": "c"}])
        self.assertEqual(titles(self.store), ["a", "b", "c"])

        self.store.replace([{"title": "x"}, {"title": "y"}])
        self.assertEqual(titles(self.store), ["x", "y"])

    def test_snapshot_keeps_the_model_given_order(self) -> None:
        """
        快照保持原序。⚠ 显示时的优先级排序只发生在 `build_view` 里，
        绝不能渗回这一层——原序是模型表达的执行次序。
        """
        self.store.replace(
            [
                {"title": "先做这个", "state": "completed"},
                {"title": "再做那个", "state": "in_progress"},
                {"title": "最后这个", "state": "pending"},
            ]
        )
        self.assertEqual(titles(self.store), ["先做这个", "再做那个", "最后这个"])

    def test_missing_state_defaults_to_pending(self) -> None:
        """不写 state 就是待办——强制模型每条都写一遍只是徒增出错机会。"""
        self.store.replace([{"title": "a"}])
        self.assertEqual(states(self.store), [TodoState.PENDING])

    def test_titles_are_stripped(self) -> None:
        self.store.replace([{"title": "  两头有空格  "}])
        self.assertEqual(titles(self.store), ["两头有空格"])

    def test_empty_list_is_legal(self) -> None:
        """
        空清单是合法输入，不是错误——它是模型「我做完了，收起来吧」的表达方式。
        """
        self.store.replace([{"title": "a"}])
        result = self.store.replace([])
        self.assertTrue(result.ok)
        self.assertEqual(titles(self.store), [])


class InProgressIsNotCappedTest(unittest.TestCase):
    """
    AC3：**不限制「进行中」的条数**（spec F4）。

    ⚠ Claude Code 强制「同时最多一条进行中」，本扩展**刻意不抄**：
    本项目的模型确实会并行发起多个工具调用，强制单条会让清单在那种场合
    被迫说假话。这条用例钉住这个「刻意」，免得后来的人当成漏做补上去。
    """

    def test_two_items_can_be_in_progress_at_once(self) -> None:
        store = TodoStore()
        result = store.replace(
            [
                {"title": "改后端", "state": "in_progress"},
                {"title": "改前端", "state": "in_progress"},
                {"title": "跑测试", "state": "pending"},
            ]
        )
        self.assertTrue(result.ok, result.reason)
        self.assertEqual(
            states(store),
            [TodoState.IN_PROGRESS, TodoState.IN_PROGRESS, TodoState.PENDING],
        )


class ValidationTest(unittest.TestCase):
    """AC4：四类非法输入各自被拒，且原因**指明是第几条**。"""

    def setUp(self) -> None:
        self.store = TodoStore()

    def test_non_list_is_rejected(self) -> None:
        result = self.store.replace("不是数组")
        self.assertFalse(result.ok)
        self.assertIn("数组", result.reason)

    def test_missing_todos_is_rejected(self) -> None:
        result = self.store.replace(None)
        self.assertFalse(result.ok)
        self.assertIn("todos", result.reason)

    def test_non_object_entry_names_the_index(self) -> None:
        result = self.store.replace([{"title": "ok"}, "字符串不是条目"])
        self.assertFalse(result.ok)
        self.assertIn("第 2 条", result.reason)

    def test_blank_title_names_the_index(self) -> None:
        result = self.store.replace([{"title": "ok"}, {"title": "   "}])
        self.assertFalse(result.ok)
        self.assertIn("第 2 条", result.reason)
        self.assertIn("title", result.reason)

    def test_unknown_state_names_the_index_and_lists_legal_values(self) -> None:
        """
        认不出的状态要同时给出**是第几条**与**合法取值有哪三个**——
        模型据此能一次改对，只说「不合法」的话它只能猜。
        """
        result = self.store.replace([{"title": "a", "state": "blocked"}])
        self.assertFalse(result.ok)
        self.assertIn("第 1 条", result.reason)
        for legal in ("pending", "in_progress", "completed"):
            self.assertIn(legal, result.reason)

    def test_state_parsing_is_forgiving(self) -> None:
        """大小写与连字符都认——它表达的意思没有任何歧义。"""
        result = self.store.replace(
            [{"title": "a", "state": "In-Progress"}, {"title": "b", "state": "COMPLETED"}]
        )
        self.assertTrue(result.ok, result.reason)
        self.assertEqual(states(self.store), [TodoState.IN_PROGRESS, TodoState.COMPLETED])


class RejectionLeavesNothingDirtyTest(unittest.TestCase):
    """
    AC4 后半：**被拒时清单一个字节都不改。**

    ⚠ 这条钉的是「校验必须在写入之前全部跑完」。边解析边写的实现会让
    一份「前两条合法、第三条非法」的输入在清单上留下半张表，
    而调用方拿到的是「失败」——两边对同一份数据的认知不一致，且都不报错。
    """

    def setUp(self) -> None:
        self.store = TodoStore()
        self.store.replace([{"title": "原有一", "state": "completed"}, {"title": "原有二"}])
        self.before = self.store.snapshot()
        self.before_version = self.store.version()

    def _assert_untouched(self) -> None:
        self.assertEqual(self.store.snapshot(), self.before)
        self.assertEqual(self.store.version(), self.before_version)

    def test_partially_valid_input_writes_nothing(self) -> None:
        result = self.store.replace(
            [{"title": "新一"}, {"title": "新二"}, {"title": ""}]
        )
        self.assertFalse(result.ok)
        self._assert_untouched()

    def test_bad_state_in_the_middle_writes_nothing(self) -> None:
        result = self.store.replace(
            [{"title": "新一"}, {"title": "新二", "state": "???"}, {"title": "新三"}]
        )
        self.assertFalse(result.ok)
        self._assert_untouched()


class MaxItemsRejectsRatherThanTruncatesTest(unittest.TestCase):
    """
    AC5：超上限是**拒绝**不是截断。

    ⚠ 截断是本项目通篇最忌讳的静默降级形态：模型以为整份写进去了，
    而清单上少了几条，它下一轮据此做的判断全是错的——界面上看不出异常。
    """

    def test_over_limit_is_rejected(self) -> None:
        store = TodoStore()
        store.replace([{"title": "原有"}])
        result = store.replace([{"title": f"t{i}"} for i in range(MAX_ITEMS + 1)])

        self.assertFalse(result.ok)
        self.assertIn(str(MAX_ITEMS), result.reason)
        # 反证：不是被截成 MAX_ITEMS 条，而是**完全没写进去**
        self.assertEqual(titles(store), ["原有"])
        self.assertNotEqual(len(store.snapshot()), MAX_ITEMS)

    def test_exactly_at_limit_is_accepted(self) -> None:
        """边界：正好 MAX_ITEMS 条要通过——否则上限实际是 MAX_ITEMS-1。"""
        store = TodoStore()
        result = store.replace([{"title": f"t{i}"} for i in range(MAX_ITEMS)])
        self.assertTrue(result.ok, result.reason)
        self.assertEqual(len(store.snapshot()), MAX_ITEMS)


class VersionTest(unittest.TestCase):
    """
    版本号是**界面刷新的唯一依据**，因此它的递增条件必须精确。
    """

    def setUp(self) -> None:
        self.store = TodoStore()

    def test_success_bumps_version(self) -> None:
        self.assertEqual(self.store.version(), 0)
        self.store.replace([{"title": "a"}])
        self.assertEqual(self.store.version(), 1)
        self.store.replace([{"title": "b"}])
        self.assertEqual(self.store.version(), 2)

    def test_failure_does_not_bump_version(self) -> None:
        """
        反证。失败也加一的话，界面会为一次**什么都没发生**的调用重绘一次
        ——重绘本身无害，但那说明版本号不再等价于「内容变了」，
        而后来的人会依赖那个等价关系。
        """
        self.store.replace([{"title": "a"}])
        before = self.store.version()
        self.store.replace("非法")
        self.assertEqual(self.store.version(), before)

    def test_clear_bumps_version_but_only_when_there_was_something(self) -> None:
        """
        清空非空清单要让界面知道；清空一个本来就空的清单不该触发重绘。
        """
        self.store.replace([{"title": "a"}])
        v1 = self.store.version()
        self.store.clear()
        self.assertGreater(self.store.version(), v1)

        v2 = self.store.version()
        self.store.clear()
        self.assertEqual(self.store.version(), v2)


class CountsAndCompletionTest(unittest.TestCase):
    def test_counts(self) -> None:
        store = TodoStore()
        store.replace(
            [
                {"title": "a", "state": "completed"},
                {"title": "b", "state": "completed"},
                {"title": "c", "state": "in_progress"},
                {"title": "d"},
            ]
        )
        self.assertEqual(store.counts(), (2, 4))

    def test_all_completed(self) -> None:
        store = TodoStore()
        store.replace([{"title": "a", "state": "completed"}])
        self.assertTrue(store.all_completed())

        store.replace([{"title": "a", "state": "completed"}, {"title": "b"}])
        self.assertFalse(store.all_completed())

    def test_empty_list_is_not_all_completed(self) -> None:
        """
        ⚠ 空 ≠ 全做完。这条直接决定「全部完成」那行记录会不会在一个
        从来没列过待办的会话里凭空冒出来。
        """
        store = TodoStore()
        self.assertFalse(store.all_completed())
        store.replace([])
        self.assertFalse(store.all_completed())

    def test_clear_empties_the_list(self) -> None:
        store = TodoStore()
        store.replace([{"title": "a"}])
        store.clear()
        self.assertEqual(store.snapshot(), ())
        self.assertFalse(store.all_completed())
        self.assertEqual(store.counts(), (0, 0))


class ConcurrencyTest(unittest.TestCase):
    """
    并发覆写不产生**混合**结果。

    实际使用中只有一个写入方（这正是选整表覆写的前提），但「只有一个写入方」
    是调用方的性质、不是本类的性质——本类自己得站得住。
    """

    def test_concurrent_replaces_never_interleave(self) -> None:
        store = TodoStore()
        thread_count = 20
        payloads = {
            i: [{"title": f"线程{i}-第{j}条"} for j in range(5)] for i in range(thread_count)
        }
        barrier = threading.Barrier(thread_count)

        def worker(index: int) -> None:
            barrier.wait()
            store.replace(payloads[index])

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(thread_count)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        final = titles(store)
        expected = {i: [e["title"] for e in payloads[i]] for i in range(thread_count)}
        self.assertIn(
            final,
            list(expected.values()),
            "并发覆写后的清单必须恰好等于某一个线程提交的那一份，不能是几份的混合",
        )
        self.assertEqual(store.version(), thread_count)


class RecorderTest(unittest.TestCase):
    """埋点：成功与被拒都留痕，且记录失败绝不打断被观测的系统。"""

    class _Spy:
        def __init__(self) -> None:
            self.events: list[tuple] = []

        def emit(self, event_type, **fields):
            self.events.append((event_type, fields))

    class _Broken:
        def emit(self, event_type, **fields):
            raise RuntimeError("记录器坏了")

    def test_success_and_rejection_both_emit(self) -> None:
        spy = self._Spy()
        store = TodoStore(recorder=spy)
        store.replace([{"title": "a", "state": "in_progress"}])
        store.replace("非法")

        self.assertEqual(len(spy.events), 2)
        self.assertTrue(spy.events[0][1]["ok"])
        self.assertEqual(spy.events[0][1]["total"], 1)
        self.assertEqual(spy.events[0][1]["in_progress"], 1)
        self.assertFalse(spy.events[1][1]["ok"])
        self.assertTrue(spy.events[1][1]["reason"])

    def test_broken_recorder_does_not_break_the_write(self) -> None:
        """
        观测设施绝不能反过来阻断被观测的系统——记录器抛异常时，
        覆写照样成功。
        """
        store = TodoStore(recorder=self._Broken())
        result = store.replace([{"title": "a"}])
        self.assertTrue(result.ok)
        self.assertEqual(titles(store), ["a"])


class LockInvariantTest(unittest.TestCase):
    """
    ⚠ **结构护栏**（spec N3）：临界区只做纯内存读写，
    而保证这一点最可靠的方式是**本类根本不持有可调用的东西**。

    本项目已因「加锁临界区内做跨线程调度」死锁五次，每次的表现都一样：
    整个 TUI 冻结，调用栈上没有任何线索。

    `_recorder` 显式豁免：它是行为记录器不是回调（方向是「本类 → 记录器」
    而不是「本类 → 外部逻辑」），且只在锁外调用。
    """

    def test_store_holds_no_callables_except_the_recorder(self) -> None:
        store = TodoStore()
        store.replace([{"title": "随便"}])
        for name, value in vars(store).items():
            if name == "_recorder":
                continue
            self.assertFalse(
                callable(value),
                f"TodoStore 不得持有可调用成员，但 {name} 是 {type(value).__name__}",
            )

    def test_the_recorder_exemption_is_the_only_one(self) -> None:
        """
        反证：豁免名单只有一项。将来若有人再加一个回调型字段，
        上面那条会红——除非他也把新字段加进豁免，而那需要在这里改，
        改的时候就会读到上面那段说明。
        """
        store = TodoStore(recorder=object())
        callables = [n for n, v in vars(store).items() if callable(v)]
        self.assertEqual(callables, [])


class ItemImmutabilityTest(unittest.TestCase):
    def test_items_are_frozen(self) -> None:
        """
        `TodoItem` 不可变是「快照零复制」的全部依据——可变之后
        调用方在锁外拿到的东西就能被别的线程改到。
        """
        item = TodoItem(title="a", state=TodoState.PENDING)
        with self.assertRaises(Exception):
            item.title = "b"  # type: ignore[misc]


if __name__ == "__main__":
    unittest.main()
