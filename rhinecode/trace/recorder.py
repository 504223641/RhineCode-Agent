"""
行为记录器：把结构化事件按时间顺序追加写进一个 JSONL 文件。

两个类，一个工厂：
- `TraceRecorder`：真正落盘的实现。
- `NullRecorder`：**Null Object 模式**——一个「什么都不做」的替身。关闭记录时，
  各层拿到的是它，于是埋点调用照样发生但零开销，调用方**不需要到处写 `if recorder:`**。
- `create_recorder`：带 fail-safe 的构造工厂（目标路径不可写时降级为 Null）。

为什么同时需要 Null Object 和 `enabled` 开关（spec N1）：这两者是**分工**而不是二选一。
- Null Object 负责「调用点不用判空」——所有层都能无条件 `self._recorder.emit(...)`。
- `enabled` 负责「昂贵负载不要白构造」——比如把整份对话历史逐条渲染一遍再丢掉是浪费。
  但判断 `enabled` 的责任不落在调用方（那又回到到处写 if 了），而是收在 `emit_lazy` 里：
  调用方给一个「构造负载的函数」，Null 版本干脆不调它。

关于线程安全（这是本模块最需要理解的地方）：
本程序在多个线程里同时产生事件——Textual 的 Worker 线程跑 Agent Loop、只读工具的
并发线程池、自动笔记的 daemon 线程。因此写文件必须加锁串行化。但**锁的临界区里
绝对不能做回调或跨线程调度**，理由见 `_write` 的注释（C11 的 SkillManager 留下的教训）。
"""

from __future__ import annotations

import contextlib
import json
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Iterator, Protocol

from rhinecode.trace.models import SCOPE_MAIN, TraceEventType

# ---------------------------------------------------------------------------
# 作用域的线程本地存储
# ---------------------------------------------------------------------------
# `threading.local()` 是标准库提供的「每个线程各有一份」的存储。同一个属性名，
# 在 A 线程里赋的值，B 线程读不到——正是我们需要的语义：
# 主对话跑在 Worker 线程、笔记跑在 daemon 线程，两者的作用域天然隔离，
# 不需要显式传参穿过十几层调用栈（那会污染大量既有函数签名）。
#
# 代价是：**thread-local 不会被子线程继承**。把任务丢进线程池执行时，
# 池线程读到的是「未设置」，必须在任务入口显式重新绑定（见 loop.py 的并发桶）。
_scope_state = threading.local()


def current_scope() -> str:
    """
    读取当前线程的作用域名。

    :returns: 作用域字符串；**未设置时回退 SCOPE_MAIN**

    为什么回退主作用域而不是抛错或返回 None（spec F2）：会话启停、命令分发、
    状态栏刷新这些事件不隶属任何一条对话，它们跑在主线程上、从未绑定过作用域，
    按语义就该归主对话。回退让这件事自然发生，不需要在每个埋点处补一次判断。

    本函数**不取锁**——它会被 emit 路径高频调用，而 thread-local 的读取本身
    就是线程私有的，加锁毫无意义且会与落盘锁竞争。
    """
    return getattr(_scope_state, "name", SCOPE_MAIN)


def bind_scope(name: str) -> None:
    """
    把当前线程的作用域**永久**设为 name（直到再次绑定）。

    用途有两处：① 线程入口绑定（笔记线程、并发池任务）；② 兜底复位
    （`_do_stream` 的 finally 无条件复位成主作用域，防止泄漏污染被复用的池化线程）。

    与 `scope()` 的区别：`scope()` 是「进去 / 出来自动恢复」，适合包住一段调用；
    `bind_scope` 是单向设置，适合「这个线程从现在起就属于某个作用域」的场景。

    副作用：修改当前线程的 thread-local 状态。
    """
    _scope_state.name = name


@contextlib.contextmanager
def scope(name: str) -> Iterator[None]:
    """
    上下文管理器：在 `with` 块内把当前线程的作用域切成 name，退出后恢复原值。

    :param name: 目标作用域（如 `isolated:review` / `summary`）

    保存并恢复**旧值**而不是无条件复位成主作用域，是为了支持嵌套
    （虽然目前没有嵌套场景，但这样写不会有意外）。
    """
    previous = current_scope()
    _scope_state.name = name
    try:
        yield
    finally:
        _scope_state.name = previous


class TraceRecorderProtocol(Protocol):
    """
    记录器的结构化类型协议，供各层做类型标注。

    用 `Protocol`（结构化子类型，也叫 duck typing 的静态版本）而不是抽象基类，
    好处是 `TraceRecorder` 与 `NullRecorder` 不需要有共同父类——它们只要
    「长得像」就满足标注。这契合下面 NullRecorder 不继承 TraceRecorder 的决定。
    """

    enabled: bool

    def emit(self, type: TraceEventType, **payload: Any) -> None: ...

    def emit_lazy(self, type: TraceEventType, factory: Callable[[], dict]) -> None: ...

    def scope(self, name: str) -> Any: ...

    def current_scope(self) -> str: ...

    def bind_scope(self, name: str) -> None: ...

    def next_turn(self, scope: str) -> int: ...

    def turn_total(self) -> int: ...

    def elapsed(self) -> float: ...

    def close(self) -> None: ...


class TraceRecorder:
    """
    真正落盘的行为记录器：每条事件一行 JSON，追加写入并立即 flush。

    为什么用 JSONL（每行一个独立 JSON 对象）而不是一个大 JSON 数组：
    ① 追加写不需要回头改文件尾部的 `]`；② 程序崩溃时已写的行仍然完整可读
    ——而这恰恰是排查崩溃最需要的；③ 阅读器可以逐行处理，不必先把整个文件读进内存。
    这与 c9 会话存档的选择同一套理由。

    :param path: 记录文件路径；父目录不存在会被创建

    副作用：创建目录、以追加模式打开文件句柄并持有到 `close()`。
    构造失败会抛 `OSError`——调用方应经 `create_recorder` 工厂而不是直接构造。
    """

    # 类属性而非实例属性：调用方可以在不构造实例的情况下参照，语义上也确实是「这个类的性质」
    enabled = True

    def __init__(self, path: Path) -> None:
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = open(path, "a", encoding="utf-8")
        self._seq = 0
        self._lock = threading.Lock()
        self._start = time.monotonic()
        # 各作用域各自的轮次计数：主对话第 3 轮与摘要第 1 轮互不干扰
        self._turns: dict[str, int] = {}
        self._closed = False

    # -- 落盘 ---------------------------------------------------------------

    def emit(self, type: TraceEventType, **payload: Any) -> None:
        """
        记录一条事件。**任何失败都被静默吞掉**（spec F4 / N3）。

        为什么必须吞异常，而且是整体包住：本方法会在**只读工具的并发线程池**里
        被间接调用。那里的异常会被 `future.result()` 抓住并当成「工具执行异常」
        回灌给模型——于是「日志写不进去」会伪装成「你的工具坏了」，模型开始
        绕路重试，把一个观测设施变成了行为污染源。记录失败必须是彻底无声的。

        :param type: 事件类型
        :param payload: 事件负载，按类型各异；会与固定四字段合并后落盘

        副作用：向文件追加一行并 flush；推进序号与该作用域的轮次无关。
        """
        try:
            self._write(type, payload)
        except Exception:
            pass

    def emit_lazy(self, type: TraceEventType, factory: Callable[[], dict]) -> None:
        """
        记录一条**负载构造代价较高**的事件：负载由 factory 现场构造。

        为什么需要它而不是直接 `if recorder.enabled: recorder.emit(...)`（spec N1 + F4）：
        后者把负载构造留在了 `try` **之外**——一旦构造过程本身抛异常
        （比如某个字段是意料之外的类型、`full_text` 拿到了没实现 `__str__` 的对象），
        异常就会外泄到业务代码里，正好违反 F4「记录失败不得影响主流程」。
        本方法把「调 factory」和「落盘」一起包进同一个 try，两类失败都被吞掉。

        **约定：昂贵埋点一律走本方法，廉价埋点走 emit。** 判断标准是「构造负载
        要不要遍历集合或做字符串拼接」——比如把整份消息历史逐条渲染就是昂贵的。

        :param type: 事件类型
        :param factory: 无参函数，返回负载字典；Null 版本**不会调用它**
        """
        try:
            self._write(type, factory())
        except Exception:
            pass

    def _write(self, type: TraceEventType, payload: dict) -> None:
        """
        在锁的保护下把一条事件序列化并落盘。异常向上抛给 emit 处理。

        临界区内**按固定顺序**做四件事：
          ① 算出候选序号 n = 当前序号 + 1
          ② 组装字典并序列化成一行 JSON
          ③ 写入文件并 flush
          ④ 序号真正推进到 n

        两个不能动的顺序理由：

        **序列化必须在锁内。** `seq` 是 JSON 的首个字段，所以「在锁外组装那一行」
        等价于「在锁外读取计数器」。并发下两个线程会各自读到同一个旧值、各写一行
        同号记录，直接违反 AC8 的「序号无重复」。把组装挪进临界区只多占用几微秒，
        换来的是确定性。

        **序号只在 flush 成功后推进。** F4 允许静默丢弃写入失败的记录，AC8 又要求
        序号不跳号——两者相容的唯一方式就是「写成功才占号」。若先自增再写，
        一次磁盘满就会在时间线上留下一个永久的空洞，而读的人无法判断那里
        原本是什么（是丢了一条，还是根本没发生过）。

        ⚠️ **临界区禁令**：不得做落盘之外的阻塞操作、不得触发任何回调、
        不得跨线程调度。这是 C11 的 SkillManager 用一次确定性死锁换来的教训——
        临界区内做回调 → 回调走 Textual 的阻塞式 `call_from_thread` → 主线程醒来
        后想申请同一把锁 → 双向等待、整个界面冻结。本方法的临界区里只有
        `json.dumps` 与文件写入，没有任何外部可注入的代码。
        """
        with self._lock:
            if self._closed:
                return
            n = self._seq + 1
            record = {
                "seq": n,
                "ts": datetime.now().isoformat(timespec="milliseconds"),
                "type": type.value if isinstance(type, TraceEventType) else str(type),
                "scope": current_scope(),
            }
            record.update(payload)
            # ensure_ascii=False：中文原样落盘。若用默认的 True，中文会变成
            # \uXXXX 转义，人眼无法直接阅读——而本模块立项的动因之一就是
            # 一个中文编码问题，记录本身把中文编码坏掉是最讽刺的失败方式（spec N8）。
            line = json.dumps(record, ensure_ascii=False, default=str)
            self._fh.write(line + "\n")
            self._fh.flush()
            self._seq = n

    # -- 作用域与计数 -------------------------------------------------------

    def scope(self, name: str) -> Any:
        """委托给模块级 `scope()`，让各层只依赖记录器对象、不用额外 import。"""
        return scope(name)

    def current_scope(self) -> str:
        """委托给模块级 `current_scope()`。"""
        return current_scope()

    def bind_scope(self, name: str) -> None:
        """委托给模块级 `bind_scope()`。"""
        bind_scope(name)

    def next_turn(self, scope: str) -> int:
        """
        给指定作用域分配下一个轮次号（从 1 开始）。

        :param scope: 作用域名
        :returns: 该作用域的新轮次号

        副作用：递增内部计数（取锁）。「轮次」指一次模型请求——读 trace 时
        用它把 `api_request` 与对应的 `api_response` 配对。
        """
        with self._lock:
            self._turns[scope] = self._turns.get(scope, 0) + 1
            return self._turns[scope]

    def turn_total(self) -> int:
        """返回所有作用域的轮次总和，供 `session_end` 汇总用。"""
        with self._lock:
            return sum(self._turns.values())

    def elapsed(self) -> float:
        """
        返回自记录器创建以来经过的秒数。

        用 `time.monotonic()`（单调时钟）而不是 `time.time()`：后者会被系统时间
        调整（NTP 校准、用户改表）影响，可能算出负数耗时。
        """
        return time.monotonic() - self._start

    def close(self) -> None:
        """
        关闭文件句柄。**幂等**——重复调用无副作用、不抛异常。

        幂等是必需的：装配层的 cleanup 可能因异常路径被调用多次，而
        「关闭已关闭的文件」在某些情形下会抛 ValueError。
        """
        with self._lock:
            if self._closed:
                return
            self._closed = True
            try:
                self._fh.close()
            except Exception:
                pass


class NullRecorder:
    """
    什么都不做的记录器替身（Null Object 模式）。

    为什么**不做 `TraceRecorder` 的子类**：两者没有任何可共享的实现——真实记录器
    的每个方法都围绕文件句柄与锁展开，而这里全是空操作。继承会让 Null 对象
    在 `__init__` 里被迫持有一个永远不用的文件句柄（或者需要重写 `__init__`
    绕开父类构造，那继承就只剩形式意义了）。二者靠 `TraceRecorderProtocol`
    在类型层面统一即可。
    """

    enabled = False

    def emit(self, type: TraceEventType, **payload: Any) -> None:
        """空操作。调用方无需判空，这是 Null Object 的全部价值。"""

    def emit_lazy(self, type: TraceEventType, factory: Callable[[], dict]) -> None:
        """
        空操作，且**刻意不调用 factory**。

        这是「关闭时零开销」承诺的兑现点：昂贵负载的构造函数压根不会执行。
        """

    def scope(self, name: str) -> Any:
        """返回 `contextlib.nullcontext()`——一个进出都不做事的上下文管理器。"""
        return contextlib.nullcontext()

    def current_scope(self) -> str:
        return SCOPE_MAIN

    def bind_scope(self, name: str) -> None:
        """空操作：关闭记录时连 thread-local 都不该被污染。"""

    def next_turn(self, scope: str) -> int:
        return 0

    def turn_total(self) -> int:
        return 0

    def elapsed(self) -> float:
        return 0.0

    def close(self) -> None:
        """空操作。"""


def create_recorder(path: Path) -> TraceRecorderProtocol:
    """
    构造记录器的**唯一推荐入口**：构造失败时降级为 NullRecorder，绝不让进程崩掉。

    为什么需要这个工厂而不是让调用方直接 `TraceRecorder(path)`：
    `TraceRecorder.__init__` 会 `mkdir` + `open`，目标路径不可写时**必然抛异常**
    （目录没权限、父路径其实是个文件、磁盘只读……）。而 spec AC22 明确把
    「目标目录不可写」列为三种「必须不阻断」的失败情形之一。若直接构造，
    进程会带着 traceback 崩在装配之前——用户只是想开个日志，结果程序起不来了。

    捕获范围是 `OSError` 而不是某个具体子类，因为同一个错误在不同平台上
    抛不同的类型：父路径是普通文件时，Windows 抛 `FileExistsError`、
    POSIX 抛 `NotADirectoryError`，两者都是 `OSError` 的子类。

    :param path: 目标记录文件路径
    :returns: 成功时是 `TraceRecorder`，失败时是 `NullRecorder`

    副作用：成功时创建目录并打开文件；失败时往 stderr 打一行说明。

    注意那行 stderr 提示**大概率会被 Textual 的 alternate screen 盖住**
    （与 C11 启动期 print 的已知限制同源）。所以对用户而言，主要的可观测后果是
    「没有产出记录文件」；那行输出只是给「重定向了 stderr」的场景兜底。
    """
    try:
        return TraceRecorder(path)
    except OSError as e:
        print(f"已跳过行为记录：{e}", file=sys.stderr)
        return NullRecorder()


__all__ = [
    "TraceRecorder",
    "NullRecorder",
    "TraceRecorderProtocol",
    "create_recorder",
    "current_scope",
    "bind_scope",
    "scope",
]
