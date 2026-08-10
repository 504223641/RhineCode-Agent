"""
MemoryManager：Memory 层唯一的编排者（c9，对标 c8 ContextManager 的定位）。

唯一持 provider 引用、唯一有副作用编排的模块，把五个下层模块串成完整流程：

- 启动：加载三层 RHINE.md（instructions）→ 清理过期会话（session）→
  开新档或 --continue 恢复最近会话。
- 每次运行：提供「自定义指令」「长期记忆索引」两个系统提示槽位的内容；
  消费一次性 pending 提醒（时间跨度）；record_message 做会话追加写。
- 自然停止后：起 daemon 线程跑记忆更新（拿锁 → 调 LLM → 写盘 → 重建索引 →
  释放锁 → notify 界面）。
- 命令：/resume 列表与载入、/memory 只读报告。

线程模型：主流程（startup / record_message / resume_*）都在 TUI 主线程或
Worker 线程串行执行；唯一的并发点是记忆更新线程——用 in-flight 标志保证
进程内同一时刻至多一个记忆线程（占用即跳过本轮，不排队），跨进程互斥交给
memory 目录的锁文件（F22）。
"""

import threading
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

from rhinecode.provider.base import BaseProvider, Message
from rhinecode.trace import SCOPE_MEMORY, NullRecorder, TraceRecorderProtocol
from rhinecode.memory import lockfile
from rhinecode.memory.instructions import LoadedInstructions, load_instructions
from rhinecode.memory.session import SessionStore, SessionInfo
from rhinecode.memory.memories import (
    CATEGORIES,
    CATEGORY_LABELS,
    parse_memory,
    render_memory,
    rebuild_index,
    truncate_index,
    INDEX_MAX_LINES,
    INDEX_MAX_BYTES,
)
from rhinecode.memory.memory_updater import (
    MemoryAction,
    build_memory_request,
    parse_memory_response,
)

# 记忆锁过期阈值（秒）：记忆写入是秒级临界区，10 分钟没释放必是崩溃残留（F22/F24）。
MEMORY_LOCK_STALE = 600.0

# 时间跨度提醒阈值（小时）：恢复会话时距最后一条消息超过该值则注入提醒（F11④）。
TIME_GAP_HOURS = 24

# 记忆索引文件名（每个 memory 目录一份）。
INDEX_FILENAME = "MEMORY.md"


def _format_gap(last_time: datetime) -> str:
    """把时间差格式化成人类可读的「X 天 / X 小时」。"""
    delta = datetime.now() - last_time
    hours = int(delta.total_seconds() // 3600)
    if hours >= 48:
        return f"{hours // 24} 天"
    return f"{hours} 小时"


class MemoryManager:
    """
    记忆系统编排者。所有 Provider 都构造（RHINE.md 注入与会话存档不依赖工具），
    记忆能力由 memories_enabled 门控（仅 DeepSeek 工具模式为 True，F21）。
    """

    def __init__(
        self,
        provider: BaseProvider,
        model: str,
        project_root: Path,
        user_dir: Path,
        memories_enabled: bool,
        notify: Optional[Callable[[str], None]] = None,
        recorder: "Optional[TraceRecorderProtocol]" = None,
    ) -> None:
        """
        :param provider: Provider（记忆 LLM 调用复用 stream_chat）
        :param model: 模型名（日志/报告语义）
        :param project_root: 项目根（sessions 与项目级 memory 都在其 .rhinecode 下）
        :param user_dir: 用户级目录（~/.rhinecode）
        :param memories_enabled: 是否启用自动记忆（仅 DeepSeek 工具模式）
        :param notify: 界面通知回调（TUI 挂载后注入，线程安全由 TUI 侧保证）
        :param recorder: 行为记录器（trace 设施）。缺省 `NullRecorder()`，不传等于零回归。
        """
        self._recorder: TraceRecorderProtocol = recorder or NullRecorder()
        self._provider = provider
        self._model = model
        self._project_root = project_root
        self._user_dir = user_dir
        self.memories_enabled = memories_enabled
        # 界面通知回调：记忆实际变更时调用（F20）。公开属性，TUI 挂载后赋值。
        self.notify = notify

        self._session = SessionStore(project_root / ".rhinecode" / "sessions")
        # 两级 memory 目录：scope → 目录路径（记忆写盘的唯一合法去处，N6）。
        self._memory_dirs = {
            "user": user_dir / "memory",
            "project": project_root / ".rhinecode" / "memory",
        }

        # RHINE.md 加载结果：startup 时填充，之后整个会话期只读。
        self._instructions = LoadedInstructions(text="", layers=[])
        # 一次性动态提醒（时间跨度等）：consume_pending_notice 取走即清。
        self._pending_notice: str = ""
        # 记忆线程状态：in-flight 标志（进程内互斥）、高水位（上次审视到的消息数）、
        # 最近一次更新结果（/memory 报告用）。
        self._memory_inflight = threading.Event()
        self._memory_watermark = 0
        self._last_memory_result = "（本次会话尚未触发）"
        # /resume 最近一次列表的缓存：让「/resume 看列表 → /resume 2 选编号」之间
        # 编号稳定，不受期间新会话出现影响。
        self._last_list: list[SessionInfo] = []

    # ------------------------------------------------------------------ #
    # 启动（F1/F10/F13）
    # ------------------------------------------------------------------ #
    def startup(self, resume_latest: bool, history: list[Message]) -> Optional[str]:
        """
        启动编排：加载 RHINE.md → 清理过期会话 → 开新档或恢复最近会话。

        :param resume_latest: True = `rhine --continue`，恢复最近的未锁定会话
        :param history: ConversationManager 的历史列表（恢复时原地填充）
        :returns: 启动提示文本（TUI 挂载时显示）；无需提示时返回 None

        副作用：读文件系统；清理过期存档；恢复时改写 history、持有会话锁。
        全程 fail-safe：任何一步异常都退化为「没有这部分记忆」，不阻断启动（N2）。
        """
        try:
            self._instructions = load_instructions(self._user_dir, self._project_root)
        except Exception:
            self._instructions = LoadedInstructions(text="", layers=[])

        try:
            self._session.cleanup_expired()
        except Exception:
            pass

        if resume_latest:
            try:
                return self._resume_latest(history)
            except Exception:
                # 恢复失败退化为新会话，不阻断启动。
                self._session.start_new()
                return "恢复最近会话失败，已开始新会话。"
        self._session.start_new()
        return None

    def _resume_latest(self, history: list[Message]) -> str:
        """
        --continue 路径：按最后时间倒序找第一个**未被新鲜锁占用**的会话接管（F10/F23）。

        被占用的顺延到下一个；全部被占/没有会话时开新档并如实说明。
        """
        for info in self._session.list_sessions():
            if info.locked:
                continue
            if not self._session.attach(info.session_id):
                continue  # 竞态下刚被别人抢到，继续顺延
            return self._fill_history(info.session_id, history)
        self._session.start_new()
        return "没有可恢复的会话（不存在或均被其它实例占用），已开始新会话。"

    def _fill_history(self, session_id: str, history: list[Message]) -> str:
        """
        载入指定会话进 history（attach 成功后的共用收尾）：容错载入、原地替换、
        时间跨度提醒登记、记忆高水位重置。

        :returns: 恢复结果的提示文本（含坏行/丢组统计）
        """
        result = self._session.load(session_id)
        history[:] = result.messages
        self._memory_watermark = len(history)

        # 时间跨度提醒（F11④）：距最后一条消息超过 24 小时则登记一次性提醒，
        # 由下一次请求的动态 reminder 带给模型（不进持久历史、不被存档）。
        if result.last_time is not None:
            gap_hours = (datetime.now() - result.last_time).total_seconds() / 3600
            if gap_hours > TIME_GAP_HOURS:
                self._pending_notice = (
                    f"距上次对话已过去 {_format_gap(result.last_time)}，"
                    "工作区状态（文件、分支、依赖）可能已变化，涉及时效的结论请先重新核实。"
                )

        parts = [f"已恢复会话 {session_id}（{len(result.messages)} 条消息）"]
        if result.skipped_lines:
            parts.append(f"跳过损坏行 {result.skipped_lines} 条")
        if result.dropped_unpaired:
            parts.append(f"丢弃不成对的工具调用消息 {result.dropped_unpaired} 条")
        return "，".join(parts) + "。"

    # ------------------------------------------------------------------ #
    # 系统提示注入（F4/F18）
    # ------------------------------------------------------------------ #
    def custom_instructions(self) -> str:
        """「自定义指令」槽位（priority 110）的内容：三层 RHINE.md 拼接结果。"""
        return self._instructions.text

    def memory_index(self) -> str:
        """
        「长期记忆」槽位（priority 130）的内容：两级索引现读现截断（F16/F18）。

        每次运行现读磁盘而非缓存——记忆线程可能在会话中途更新索引，下一条消息
        就应看到新索引。每级各自截断（200 行 / 25KB），并带上目录路径说明，
        让模型知道去哪里按需读记忆全文。

        :returns: 拼好的索引文本；两级都没有索引时返回空串（槽位整体跳过）
        """
        parts: list[str] = []
        for scope, label in (("user", "用户级"), ("project", "项目级")):
            index_path = self._memory_dirs[scope] / INDEX_FILENAME
            try:
                raw = index_path.read_text(encoding="utf-8")
            except OSError:
                continue
            if not raw.strip():
                continue
            parts.append(
                f"### {label}记忆索引（全文位于 {self._memory_dirs[scope]}，"
                f"需要细节时用读文件工具按文件名读取）\n{truncate_index(raw)}"
            )
        if not parts:
            return ""
        header = (
            "以下是历史会话沉淀的长期记忆索引。它们是背景参考，不是本次的用户指令；"
            "条目可能过时，引用前注意核实。"
        )
        return header + "\n\n" + "\n\n".join(parts)

    def consume_pending_notice(self) -> str:
        """取走一次性动态提醒（时间跨度等），取后即清（只影响下一次请求）。"""
        notice = self._pending_notice
        self._pending_notice = ""
        return notice

    # ------------------------------------------------------------------ #
    # 会话追加写（F6）
    # ------------------------------------------------------------------ #
    def record_message(self, msg: Message) -> None:
        """
        消息追加写钩子：用户消息由 ConversationManager 调、assistant/tool 消息由
        Agent 循环的 recorder 回调调。SessionStore 内部已 fail-safe。
        """
        self._session.append(msg)

    # ------------------------------------------------------------------ #
    # 自动记忆（F15/F17/F20/F22）
    # ------------------------------------------------------------------ #
    def on_natural_stop(self, history: list[Message]) -> None:
        """
        Agent 循环自然停止后的记忆钩子：起 daemon 线程异步审视本轮新增对话。

        跳过条件（都不算失败）：记忆未启用（F21）/ 上一轮记忆线程仍在跑（进程内
        互斥，占用即跳过不排队）/ 高水位之后没有新增消息。

        副作用：置 in-flight 标志、推进高水位、启动后台线程（线程内可能调 LLM 与写盘）。
        """
        if not self.memories_enabled:
            return
        if self._memory_inflight.is_set():
            self._last_memory_result = "上一轮记忆更新仍在进行，本轮跳过。"
            return
        new_msgs = list(history[self._memory_watermark:])
        if not new_msgs:
            return
        self._memory_inflight.set()
        self._memory_watermark = len(history)
        thread = threading.Thread(
            target=self._update_memories, args=(new_msgs,), daemon=True, name="rhine-memory"
        )
        thread.start()

    def _update_memories(self, new_msgs: list[Message]) -> None:
        """
        记忆更新线程体：调 LLM 拿动作 → 逐目录「拿锁 → 写盘 → 重建索引 → 释放」。

        完整临界区在本方法内闭合：拿锁的人就是写盘的人（plan 设计说明）。
        任何异常都收敛为 _last_memory_result 记录 + 静默返回（F17），绝不外抛
        （daemon 线程里未捕获异常只会打印堆栈吓到用户）。

        副作用：一次 LLM 调用（tools=None）；memory 目录内写/删文件；notify 回调。
        """
        # 记忆作用域绑定（trace F2）。本方法是 daemon 线程的目标函数，所以在函数体
        # 第一行绑定一次就够——thread-local 天然把它与主对话线程隔开，不需要 `with`。
        #
        # ⚠️ **不要绑在 `on_natural_stop`**：那个方法跑在 Worker 线程上
        # （由 `_wrap_events` 调用），绑在那里会把**主对话线程**永久标成 notes，
        # 此后用户的每一条消息都会被记成记忆作用域。
        self._recorder.bind_scope(SCOPE_MEMORY)
        try:
            actions = self._decide_actions(new_msgs)
            if not actions:
                self._last_memory_result = "本轮无值得记录的内容。"
                return
            applied, skipped_locked = self._apply_actions(actions)
            if applied:
                self._last_memory_result = f"已更新 {applied} 条记忆。"
                if self.notify is not None:
                    self.notify(f"已更新记忆（{applied} 条）")
            elif skipped_locked:
                self._last_memory_result = "目标记忆目录正被其它实例写入，本轮跳过。"
            else:
                self._last_memory_result = "本轮无值得记录的内容。"
        except Exception as e:  # noqa: BLE001 —— 记忆是尽力而为的增强项，任何异常都静默（F17）
            self._last_memory_result = f"最近一次更新失败：{e}"
        finally:
            self._memory_inflight.clear()

    def _decide_actions(self, new_msgs: list[Message]) -> list[MemoryAction]:
        """调记忆 LLM 并解析动作列表；流错误抛异常交由上层记入结果。"""
        system, req = build_memory_request(
            new_msgs,
            self._read_index("user"),
            self._read_index("project"),
        )
        parts: list[str] = []
        # 强制 tools=None：记忆模型在此阶段没有任何工具可用（F15/N6④）。
        for chunk in self._provider.stream_chat(req, thinking_effort="off", tools=None, system=system):
            if chunk.type == "error":
                raise RuntimeError(chunk.content or "记忆流出错")
            if chunk.type == "text":
                parts.append(chunk.content)
        return parse_memory_response("".join(parts))

    def _read_index(self, scope: str) -> str:
        """读某级索引文件全文；不存在/失败返回空串。"""
        try:
            return (self._memory_dirs[scope] / INDEX_FILENAME).read_text(encoding="utf-8")
        except OSError:
            return ""

    def _apply_actions(self, actions: list[MemoryAction]) -> "tuple[int, int]":
        """
        按 scope 分组落盘：每个目录一个锁临界区（F22）。

        拿不到锁 → 该目录整组跳过（非阻塞退让）；拿到后执行动作并**全量重建索引**
        （扫目录解析全部记忆，幂等自愈），finally 释放锁。

        :returns: (实际应用的动作数, 因锁被占跳过的动作数)
        """
        applied = 0
        skipped_locked = 0
        by_scope: dict[str, list[MemoryAction]] = {}
        for a in actions:
            by_scope.setdefault(a.scope, []).append(a)

        for scope, group in by_scope.items():
            target_dir = self._memory_dirs[scope]
            try:
                target_dir.mkdir(parents=True, exist_ok=True)
            except OSError:
                continue
            lock = target_dir / ".lock"
            if not lockfile.try_acquire(lock, MEMORY_LOCK_STALE):
                skipped_locked += len(group)
                continue
            try:
                for a in group:
                    if self._apply_one(target_dir, a):
                        applied += 1
                self._rebuild_index_file(target_dir)
            finally:
                lockfile.release(lock)
        return applied, skipped_locked

    @staticmethod
    def _apply_one(target_dir: Path, action: MemoryAction) -> bool:
        """
        执行单个动作。路径 = 目录 + 已过白名单校验的文件名，物理上出不了 memory 目录。

        :returns: 是否实际发生了写/删；失败返回 False（单条失败不连坐）
        """
        path = target_dir / action.filename
        try:
            if action.op == "delete":
                if not path.exists():
                    return False
                path.unlink()
                return True
            if action.memory is None:
                return False
            path.write_text(render_memory(action.memory), encoding="utf-8")
            return True
        except OSError:
            return False

    @staticmethod
    def _rebuild_index_file(target_dir: Path) -> None:
        """
        扫描目录全量重建索引文件（幂等自愈：手工增删的记忆也会被如实收录/剔除）。

        跳过索引自身与解析失败的坏文件；写入失败静默（下次重建再补）。
        """
        memories = []
        try:
            for path in sorted(target_dir.glob("*.md")):
                if path.name == INDEX_FILENAME:
                    continue
                try:
                    parsed = parse_memory(path.read_text(encoding="utf-8"), filename=path.name)
                except OSError:
                    continue
                if parsed is not None:
                    memories.append(parsed)
            (target_dir / INDEX_FILENAME).write_text(rebuild_index(memories), encoding="utf-8")
        except OSError:
            pass

    # ------------------------------------------------------------------ #
    # /resume（F9/F11/F12/F23）
    # ------------------------------------------------------------------ #
    @property
    def session_id(self) -> str:
        """
        当前会话 ID（只读）。

        暴露给上层（TUI 会话选择面板）标注「（当前）」条目用，
        避免上层直接穿透 self._session 访问内部实现。
        """
        return self._session.session_id

    def list_resume_sessions(self) -> list[SessionInfo]:
        """
        返回结构化的可恢复会话列表（TUI 交互式选择面板的数据源）。

        与文本版 resume_list 的差异：
        - 不限条数（limit=None 取全量），面板可滚动展示全部历史会话；
        - 返回 SessionInfo 列表而非渲染文本，由面板自行决定展示与 disabled 逻辑。

        副作用：同步写 self._last_list 编号缓存——保证用户看完面板按 Esc 退出后，
        手输 `/resume <编号>` 时 _resolve_key 解析到的编号与面板展示一致。
        """
        infos = self._session.list_sessions(limit=None)
        self._last_list = infos
        return infos

    def resume_list(self) -> str:
        """
        /resume 无参：渲染最近会话列表（编号供 `/resume <编号>` 使用）。

        注：TUI 已改用 list_resume_sessions 的结构化面板，本方法保留给
        测试与非 TUI 场景（文本版只展示最近 10 条，与 c9 原行为一致）。

        副作用：缓存本次列表（编号 → 会话的映射在下次列表刷新前保持稳定）。
        """
        infos = self._session.list_sessions()
        self._last_list = infos
        if not infos:
            return "没有可恢复的会话存档。"
        lines = ["最近的会话（/resume <编号或ID> 载入）："]
        for i, info in enumerate(infos, start=1):
            when = info.last_time.strftime("%Y-%m-%d %H:%M") if info.last_time else "未知时间"
            current = "（当前）" if info.session_id == self._session.session_id else ""
            # `🔒`→`[锁定]`（F28/F30）：语义由文字承担，不靠一个图形
            locked = "[锁定] " if info.locked and not current else ""
            lines.append(
                f"  {i}. {locked}{info.session_id}{current} · {when} · "
                f"{info.message_count} 条 · {info.title}"
            )
        return "\n".join(lines)

    def resume_into(self, key: str, history: list[Message]) -> "tuple[bool, str]":
        """
        /resume <编号或ID>：锁检查 → 接管 → 容错载入 → 替换 history（F9）。

        :param key: 列表编号（基于最近一次 resume_list 的缓存）或完整会话 ID
        :param history: 当前历史（成功时原地替换）
        :returns: (是否成功, 提示文本)；失败时 history 保持原样

        副作用：成功时切换会话锁、改写 history、重置记忆高水位、可能登记时间提醒。
        """
        session_id = self._resolve_key(key)
        if session_id is None:
            return False, f"找不到会话：{key}（先输入 /resume 查看列表）"
        if session_id == self._session.session_id:
            return False, "该会话就是当前会话，无需恢复。"
        if not self._session.attach(session_id):
            return False, f"会话 {session_id} 正被另一个 RhineCode 实例使用，或无法接管。"
        return True, self._fill_history(session_id, history)

    def _resolve_key(self, key: str) -> Optional[str]:
        """把用户输入解析为会话 ID：纯数字按缓存列表编号，否则按 ID 精确匹配。"""
        key = key.strip()
        if key.isdigit():
            idx = int(key) - 1
            infos = self._last_list or self._session.list_sessions()
            if 0 <= idx < len(infos):
                return infos[idx].session_id
            return None
        if self._session._archive_path(key).is_file():
            return key
        return None

    # ------------------------------------------------------------------ #
    # 可观测与生命周期（F19/F23）
    # ------------------------------------------------------------------ #
    def memory_report(self) -> str:
        """/memory 的只读报告（F19）：指令层、索引、记忆数、最近更新、会话与锁状态。"""
        lines = ["记忆系统状态", "", "RHINE.md 项目指令："]
        for layer in self._instructions.layers:
            mark = f"已加载（{layer.size} 字符）" if layer.loaded else "未找到"
            lines.append(f"  [{layer.label}] {layer.path} — {mark}")
            for err in layer.errors:
                lines.append(f"    警告：{err}")

        lines.append("")
        lines.append("自动记忆：" + ("启用" if self.memories_enabled else "未启用（仅 DeepSeek 工具模式）"))
        for scope, label in (("user", "用户级"), ("project", "项目级")):
            target_dir = self._memory_dirs[scope]
            counts = self._count_memories(target_dir)
            total = sum(counts.values())
            detail = "、".join(
                f"{CATEGORY_LABELS[c]} {counts[c]}" for c in CATEGORIES if counts[c]
            ) or "无记忆"
            index_path = target_dir / INDEX_FILENAME
            over = self._index_over_limit(index_path)
            index_state = ("存在" + ("，超出注入上限（已截断）" if over else "")) if index_path.is_file() else "不存在"
            locked = "占用中" if lockfile.is_fresh(target_dir / ".lock", MEMORY_LOCK_STALE) else "空闲"
            lines.append(f"  [{label}] {target_dir} — {total} 条（{detail}）· 索引{index_state} · 写锁{locked}")
        lines.append(f"  最近一次自动更新：{self._last_memory_result}")

        lines.append("")
        sid = self._session.session_id or "（未开始）"
        lines.append(f"当前会话：{sid} · 已存档 {self._session.message_count} 条消息")
        return "\n".join(lines)

    @staticmethod
    def _count_memories(target_dir: Path) -> dict[str, int]:
        """统计某目录四类记忆数量（坏文件不计）。"""
        counts = {c: 0 for c in CATEGORIES}
        try:
            for path in target_dir.glob("*.md"):
                if path.name == INDEX_FILENAME:
                    continue
                try:
                    parsed = parse_memory(path.read_text(encoding="utf-8"), filename=path.name)
                except OSError:
                    continue
                if parsed is not None:
                    counts[parsed.category] += 1
        except OSError:
            pass
        return counts

    @staticmethod
    def _index_over_limit(index_path: Path) -> bool:
        """判断索引是否超过注入上限（/memory 报告的「已截断」标记）。"""
        try:
            raw = index_path.read_text(encoding="utf-8")
        except OSError:
            return False
        return (
            len(raw.splitlines()) > INDEX_MAX_LINES
            or len(raw.encode("utf-8")) > INDEX_MAX_BYTES
        )

    def on_clear(self) -> None:
        """/clear：释放旧会话锁、开新档、记忆高水位归零（F8）。"""
        self._session.start_new()
        self._memory_watermark = 0

    def touch_session_lock(self) -> None:
        """TUI 心跳定时器：刷新会话锁 mtime（活着即新鲜，F23）。"""
        self._session.touch_lock()

    def close(self) -> None:
        """退出：释放会话锁（与 MCP close_all 并列在 __main__ 的 finally 里）。"""
        self._session.release()
