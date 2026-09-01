"""
会话存档 SessionStore（c9 F5–F13/F23）：JSONL 追加写、扫描列表、容错载入、清理与会话锁。

存储设计（spec 第二/三组需求）：
- 每个会话一个 `<sessions_dir>/<会话ID>.jsonl`，ID 形如 `YYYYMMDD-HHMMSS-xxxx`
  （时间戳 + 4 位随机后缀防同秒撞车），文件名即 ID。
- **追加写**：每条消息序列化成一行 JSON 追加到末尾，只追加、不回写、不重排——
  追加是常数开销，崩溃最多丢最后一行（F6）。
- **无独立 meta 文件**：列表所需的标题/消息数/时间直接扫 JSONL 现算（F7），
  少一份要保持同步的状态。
- **惰性建档**：start_new 只生成 ID，首条消息真正落盘时才创建文件与会话锁——
  「打开看一眼就退」不留空档垃圾（plan 技术决策）。
- 存档记录的是 **c8 压缩前的原始消息流**：上层在消息刚追加进 history 时就调
  append，此后 c8 对 history 的原地改写（占位/摘要重构）不会回写存档。

行格式（未知字段忽略、缺 ts 容忍，spec N5）：
    {"ts": "...", "role": "user", "content": "..."}
    {"ts": "...", "role": "assistant", "content": "", "tool_calls": [{"id","name","arguments"}]}
    {"ts": "...", "role": "tool", "tool_call_id": "...", "content": "..."}

会话锁（F23）：活跃写入的会话在同目录持有 `<会话ID>.lock`，append 时顺带刷新
mtime（另有 TUI 心跳定期 touch）；/resume、--continue 载入前检查目标锁，新鲜则
拒绝、过期则清除接管。锁的过期阈值 SESSION_LOCK_STALE 秒。
"""

import json
import random
import string
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional

from rhinecode.provider.base import Message, ToolCall
from rhinecode.memory import lockfile

# 会话锁过期阈值（秒）：TUI 心跳每 2 分钟 touch 一次，10 分钟没动静即视为
# 持有进程已死，可被其它实例接管（plan 技术决策：心跳间隔远小于过期阈值）。
SESSION_LOCK_STALE = 600.0

# 过期清理阈值（天）：最后修改超过 30 天的会话存档启动时静默删除（F13）。
EXPIRE_DAYS = 30

# /resume 列表默认展示的最近会话数。
LIST_LIMIT = 10

# 列表里标题的最大长度（取首条 user 消息截断）。
TITLE_MAX_CHARS = 30


def _iter_archive_lines(path: Path):
    """
    读存档的**唯一开档入口**（三个读点共用：`message_count` / `_scan_one` / `load`）。

    :param path: 存档文件路径
    :yields: 每行的文本；**这一行解码不出来时 yield `None`**
    :raises OSError: 文件不存在 / 权限不足等——在**首次取值**时抛出，
                     由各调用方按既有方式退化（生成器的惰性：`open` 发生在
                     第一次迭代时，所以调用方的 `try` 必须裹住 for 循环本身，
                     现有三处都是这么写的）

    ⚠ **「按二进制读、逐行自己解码」是这里唯一的设计点，理由值得写全。**

    本模块**早就有一条「坏行跳过并计数」的通路**（`_deserialize_line` 返回 None
    → `skipped_lines` → `/resume` 回执里那句「跳过损坏行 N 条」）。问题在于
    `open(encoding="utf-8")` 的解码失败发生在**迭代那一行的时候**，会把整个 for
    循环掀翻——于是一行坏字节的代价不是「少一条消息」，而是**整份会话都载不
    进来**，且异常直接冒到 `/resume` 与会话面板上（实测两处都抛
    `UnicodeDecodeError`）。逐行解码把失败**限制在那一行**，于是它落进上面那条
    早就准备好的通路：**一行坏字节只花掉一条消息**，其余照常恢复，用户还能从
    回执里看到跳了几条。

    ⚠ **不要「优化」成 `open(..., errors="replace")`**——那个写法看起来更简单，
    实测**是错的**：存档行的 JSON 结构部分（`{"role":"user","content":"`）全是
    ASCII，在任何中文编码下都与 UTF-8 逐字节相同，坏掉的只有中文那几个字节。
    于是 `errors="replace"` 之后 `json.loads` **照常成功**，产出一条内容是
    `����` 的消息——它**不会**进坏行通路（`skipped_lines` 仍是 0），而是**静默
    地进入对话历史并被发给模型**。实跑三行样本（好 / 坏 / 好）确认过：
    `errors="replace"` 恢复 3 条、跳过 0 条，中间那条是乱码；逐行解码恢复 2 条、
    跳过 1 条并如实告知。**「救回一条乱码」比「丢掉一条并说出来」更糟。**

    ⚠ 同理不用 `errors="surrogateescape"`：那会把坏字节变成落单的代理字符，
    JSON 照样解析得动，而那种字符在后续编码进 API 请求时才会炸——把问题从
    「读存档时」推迟到「发请求时」，追起来更难。

    ⚠ **写入路径刻意不共用本函数**（`append` 仍是 `encoding="utf-8"` 直接写）：
    读的时候「尽量救回来」是对的，写的时候做任何容错都是在**制造**坏数据。
    """
    # 二进制打开：迭代产出 bytes 行，解码由下面逐行进行，一行坏不影响下一行。
    with open(path, "rb") as f:
        for raw in f:
            try:
                yield raw.decode("utf-8")
            except UnicodeDecodeError:
                # ⚠ 只吞这一行。调用方按各自的既有方式处理 None：
                # `load` 计入 skipped_lines（用户看得见），扫描与计数则跳过解析。
                yield None


@dataclass
class SessionInfo:
    """
    /resume 列表的一项（扫描 JSONL 现算，无 meta 文件，F7）。

    :param session_id: 会话 ID（文件名去掉 .jsonl）
    :param path: 存档文件路径
    :param title: 首条 user 消息截断（无 user 消息时为占位文本）
    :param message_count: 行数（≈消息数；坏行也计入，列表场景精度足够）
    :param last_time: 末行 ts；行内无 ts 时回退文件 mtime
    :param locked: 会话锁存在且新鲜（正被其它实例使用）
    """

    session_id: str
    path: Path
    title: str
    message_count: int
    last_time: Optional[datetime]
    locked: bool


@dataclass
class SessionLoadResult:
    """
    容错载入的结果（F11）。

    :param messages: 还原出的消息列表（已做不成对清理）
    :param skipped_lines: 跳过的坏行数
    :param dropped_unpaired: 因工具调用不成对被丢弃的消息数
    :param last_time: 存档最后一条消息的时间（时间跨度提醒用）
    """

    messages: list[Message]
    skipped_lines: int
    dropped_unpaired: int
    last_time: Optional[datetime]


class SessionStore:
    """
    会话存档的全部磁盘操作 + 当前活跃会话锁的持有。

    一个 SessionStore 实例对应一个 sessions 目录，同一时刻至多「活跃」一个会话
    （当前正在追加写的那个）。线程安全性：append 可能被 Worker 线程调用，但
    RhineCode 同一时刻只有一个 Agent 运行，追加天然串行，无需加内存锁。
    """

    def __init__(self, sessions_dir: Path):
        """
        :param sessions_dir: 存档目录（<项目根>/.rhinecode/sessions）；不存在时
                             首次落盘会自动创建（惰性）。
        """
        self._dir = sessions_dir
        self._session_id: str = ""
        self._created = False  # 惰性建档标志：首条消息落盘后才为 True

    # ------------------------------------------------------------------ #
    # 基本属性
    # ------------------------------------------------------------------ #
    @property
    def session_id(self) -> str:
        """当前活跃会话 ID（start_new / attach 之后有值）。"""
        return self._session_id

    @property
    def message_count(self) -> int:
        """当前活跃存档的行数（/memory 报告用）；未建档时为 0。"""
        if not self._created:
            return 0
        try:
            # 走唯一开档入口：解码失败被限制在单行，不会掀翻这里的计数循环
            # （此前一份坏编码的存档会让 `/memory` 整个抛 UnicodeDecodeError）。
            # 解码不出来的行**照数**，与 SessionInfo.message_count 的既有口径
            # 「坏行也计入」一致——这里要的是「存档有多大」，不是「能读出几条」。
            return sum(1 for _ in _iter_archive_lines(self._archive_path()))
        except OSError:
            return 0

    def _archive_path(self, session_id: str = "") -> Path:
        return self._dir / f"{session_id or self._session_id}.jsonl"

    def _lock_path(self, session_id: str = "") -> Path:
        return self._dir / f"{session_id or self._session_id}.lock"

    # ------------------------------------------------------------------ #
    # 建档与追加（F5/F6/F8）
    # ------------------------------------------------------------------ #
    def start_new(self) -> str:
        """
        开启新会话：生成 ID，但**不建文件**（惰性建档）。

        若当前还持有旧会话锁（如 /clear 切换），先释放。

        :returns: 新会话 ID（YYYYMMDD-HHMMSS-xxxx）
        """
        self.release()
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        suffix = "".join(random.choices(string.ascii_lowercase + string.digits, k=4))
        self._session_id = f"{stamp}-{suffix}"
        self._created = False
        return self._session_id

    def append(self, msg: Message) -> None:
        """
        把一条消息序列化为一行 JSON 追加到当前存档（F6）。

        首次调用时创建目录、存档文件与会话锁（惰性建档收口在这里）。
        每次追加顺带 touch 会话锁（活跃即新鲜）。任何 IO 失败静默——
        本条丢失、后续继续尝试，绝不阻断对话（N2）。

        副作用：可能创建目录/文件/锁；向存档追加一行；刷新锁 mtime。
        """
        if not self._session_id:
            return
        try:
            if not self._created:
                self._dir.mkdir(parents=True, exist_ok=True)
                # 新会话 ID 唯一、天然无冲突，锁拿不到也不阻断（尽力而为持锁）。
                lockfile.try_acquire(self._lock_path(), SESSION_LOCK_STALE)
                self._created = True
            line = json.dumps(_serialize(msg), ensure_ascii=False)
            with open(self._archive_path(), "a", encoding="utf-8") as f:
                f.write(line + "\n")
            lockfile.touch(self._lock_path())
        except Exception:
            # 追加失败不阻断对话：最坏丢这一条，下一条继续尝试（F6）。
            pass

    def touch_lock(self) -> None:
        """刷新当前会话锁的 mtime（TUI 心跳定时器调用；未建档时无锁可刷）。"""
        if self._created:
            lockfile.touch(self._lock_path())

    def release(self) -> None:
        """释放当前会话锁（退出 / 切换会话时调用）；未建档时无操作。"""
        if self._created and self._session_id:
            lockfile.release(self._lock_path())

    # ------------------------------------------------------------------ #
    # 扫描列表（F7）
    # ------------------------------------------------------------------ #
    def list_sessions(self, limit: Optional[int] = LIST_LIMIT) -> list[SessionInfo]:
        """
        扫描目录得到最近会话列表（按最后时间倒序）。

        全部信息现场从 JSONL 推导（F7 无 meta 文件）：标题取首条 role=user 行的
        content 截断；last_time 取末行 ts、无 ts 回退 mtime。坏文件整个跳过。

        :param limit: 返回条数上限；None 表示不限（TUI 会话面板取全量用）

        副作用：只读文件系统。
        """
        infos: list[SessionInfo] = []
        try:
            files = list(self._dir.glob("*.jsonl"))
        except OSError:
            return []
        for path in files:
            info = self._scan_one(path)
            if info is not None:
                infos.append(info)
        infos.sort(
            key=lambda i: i.last_time or datetime.fromtimestamp(0),
            reverse=True,
        )
        return infos[:limit]

    def _scan_one(self, path: Path) -> Optional[SessionInfo]:
        """扫描单个存档推导 SessionInfo；无法读取返回 None。"""
        try:
            title = ""
            count = 0
            last_ts: Optional[str] = None
            for line in _iter_archive_lines(path):
                if line is None:
                    # 这一行解码不出来：照既有的「坏行」口径**计数但不解析**
                    # （标题与时间取不到就取不到，不该因此丢掉整个会话——
                    # 列表里少一项，用户根本不知道它存在过）。
                    count += 1
                    continue
                if not line.strip():
                    continue
                count += 1
                try:
                    data = json.loads(line)
                except ValueError:
                    continue  # 坏行不影响扫描
                if not isinstance(data, dict):
                    continue
                ts = data.get("ts")
                if isinstance(ts, str):
                    last_ts = ts
                if not title and data.get("role") == "user":
                    # 标题优先用显示内容（c10 F26）：/init 等提示词命令的会话
                    # 在列表里显示原命令而非展开后的长提示词；缺失或空则回退 content。
                    display = data.get("display_content")
                    raw = display if isinstance(display, str) and display.strip() else data.get("content", "")
                    content = str(raw).strip().replace("\n", " ")
                    title = content[:TITLE_MAX_CHARS]
            last_time = _parse_ts(last_ts)
            if last_time is None:
                try:
                    last_time = datetime.fromtimestamp(path.stat().st_mtime)
                except OSError:
                    last_time = None
            session_id = path.stem
            return SessionInfo(
                session_id=session_id,
                path=path,
                title=title or "（无用户消息）",
                message_count=count,
                last_time=last_time,
                locked=lockfile.is_fresh(self._lock_path(session_id), SESSION_LOCK_STALE),
            )
        except OSError:
            return None

    # ------------------------------------------------------------------ #
    # 容错载入与接管（F9/F11/F12/F23）
    # ------------------------------------------------------------------ #
    def load(self, session_id: str) -> SessionLoadResult:
        """
        容错载入一个会话存档（不改变当前活跃会话，纯读取）。

        分级容错（F11）：
        1. 坏行（JSON 解析失败 / 非对象 / role 非法）跳过并计数；
        2. 未知字段忽略、缺 ts 容忍（N5）；
        3. **不成对清理**：assistant(tool_calls) 的每个调用 id 都必须有对应的
           role=tool 行，否则整组（该 assistant + 它已有的部分 tool 行）丢弃；
           孤儿 tool 行（无归属 assistant）同样丢弃。中间与结尾统一处理——
           恢复后继续追加会让「断口」出现在文件中间，载入逻辑必须两处都能修
           （F12）。丢组而非截断，保住断口之后的完整消息。

        :returns: SessionLoadResult；文件不存在/不可读时 messages 为空列表
        """
        raw_messages: list[Message] = []
        skipped = 0
        last_ts: Optional[str] = None
        try:
            for line in _iter_archive_lines(self._archive_path(session_id)):
                if line is None:
                    # 解码不出来 = 坏行，走既有计数（用户会在 /resume 的回执里
                    # 看到「跳过损坏行 N 条」）。⚠ 别改成「救回来一条乱码」：
                    # 那会静默地把 U+FFFD 塞进对话历史并发给模型，见
                    # `_iter_archive_lines` 里那段实测记录。
                    skipped += 1
                    continue
                if not line.strip():
                    continue
                msg, ts = _deserialize_line(line)
                if msg is None:
                    skipped += 1
                    continue
                if ts:
                    last_ts = ts
                raw_messages.append(msg)
        except OSError:
            return SessionLoadResult([], 0, 0, None)

        messages, dropped = drop_unpaired(raw_messages)
        return SessionLoadResult(messages, skipped, dropped, _parse_ts(last_ts))

    def attach(self, session_id: str) -> bool:
        """
        接管一个已有会话，使其成为当前活跃存档（F12/F23）。

        执行流程：
        1. 目标存档必须存在；
        2. 目标锁新鲜 → 正被其它实例使用，返回 False（拒绝载入）；
        3. 过期锁清除；`try_acquire` 拿新锁，失败返回 False；
        4. 释放旧会话锁，切换当前 ID，`_created=True`（后续 append 直接追加进该文件）。

        :returns: True = 接管成功；False = 被占用或拿锁失败
        """
        if not self._archive_path(session_id).is_file():
            return False
        lock = self._lock_path(session_id)
        if lockfile.is_fresh(lock, SESSION_LOCK_STALE):
            return False
        # try_acquire 内部会清除过期残留锁后重试（lockfile 的自愈逻辑）。
        if not lockfile.try_acquire(lock, SESSION_LOCK_STALE):
            return False
        self.release()  # 释放旧会话锁（如有）
        self._session_id = session_id
        self._created = True
        return True

    # ------------------------------------------------------------------ #
    # 过期清理（F13）
    # ------------------------------------------------------------------ #
    def cleanup_expired(self, days: int = EXPIRE_DAYS) -> int:
        """
        启动时清理：删除最后修改超过 days 天且未被新鲜锁保护的存档（连带其锁），
        并清理孤儿锁（有 .lock 无同名 .jsonl）。

        :returns: 删除的存档文件数

        副作用：删除文件；任何单个文件的失败都跳过，不影响其余清理。
        """
        removed = 0
        cutoff = time.time() - days * 86400
        try:
            files = list(self._dir.glob("*.jsonl"))
        except OSError:
            return 0
        for path in files:
            try:
                if path.stat().st_mtime >= cutoff:
                    continue
                if lockfile.is_fresh(self._lock_path(path.stem), SESSION_LOCK_STALE):
                    continue  # 被新鲜锁保护（另一实例正用）不删
                path.unlink()
                lockfile.release(self._lock_path(path.stem))
                removed += 1
            except OSError:
                continue
        # 孤儿锁：有锁没档（档被删 / 手工清理过）一并回收。
        try:
            for lock in self._dir.glob("*.lock"):
                if not (self._dir / f"{lock.stem}.jsonl").exists():
                    lockfile.release(lock)
        except OSError:
            pass
        return removed


# ---------------------------------------------------------------------- #
# 序列化 / 反序列化（模块级纯函数）
# ---------------------------------------------------------------------- #
def _serialize(msg: Message) -> dict:
    """Message → 存档行 dict：只写有值的字段，附加 ts。"""
    data: dict = {"ts": datetime.now().isoformat(timespec="seconds"), "role": msg.role, "content": msg.content}
    if msg.tool_calls:
        data["tool_calls"] = [
            {"id": tc.id, "name": tc.name, "arguments": tc.arguments} for tc in msg.tool_calls
        ]
    if msg.tool_call_id:
        data["tool_call_id"] = msg.tool_call_id
    # 双内容（c10 F27）：仅提示词型命令设置 display_content，非 None 才写入——
    # 普通消息的存档行保持与旧格式逐字节一致（向后兼容零迁移）。
    if msg.display_content is not None:
        data["display_content"] = msg.display_content
    return data


def _deserialize_line(line: str) -> "tuple[Optional[Message], Optional[str]]":
    """
    存档行 → (Message, ts)；任何形态问题返回 (None, None) 由调用方按坏行计数。

    未知字段直接忽略（只取认识的键，N5）；tool_calls 里的坏项跳过。
    """
    try:
        data = json.loads(line)
    except ValueError:
        return None, None
    if not isinstance(data, dict):
        return None, None
    role = data.get("role")
    if role not in ("user", "assistant", "tool"):
        return None, None
    content = data.get("content")
    if not isinstance(content, str):
        content = "" if content is None else str(content)

    tool_calls: Optional[list[ToolCall]] = None
    raw_calls = data.get("tool_calls")
    if isinstance(raw_calls, list):
        parsed: list[ToolCall] = []
        for item in raw_calls:
            if not isinstance(item, dict):
                continue
            cid = item.get("id")
            name = item.get("name")
            if not isinstance(cid, str) or not isinstance(name, str):
                continue
            args = item.get("arguments")
            parsed.append(ToolCall(id=cid, name=name, arguments=args if isinstance(args, dict) else None))
        tool_calls = parsed or None

    tool_call_id = data.get("tool_call_id")
    if not isinstance(tool_call_id, str):
        tool_call_id = None

    # 双内容（c10 F27）：缺失 / null / 非法类型统一回退 None（旧档兼容，无需迁移）。
    display_content = data.get("display_content")
    if not isinstance(display_content, str):
        display_content = None

    ts = data.get("ts")
    return (
        Message(
            role=role,
            content=content,
            tool_calls=tool_calls,
            tool_call_id=tool_call_id,
            display_content=display_content,
        ),
        ts if isinstance(ts, str) else None,
    )


def drop_unpaired(messages: list[Message]) -> "tuple[list[Message], int]":
    """
    不成对清理（F11②/F12）：保证一段历史里 assistant(tool_calls) ↔ tool 严格配对。

    **两个调用方**（c13 起）：

    1. 会话存档载入（本模块 `load`）——存档可能在写到一半时被中断；
    2. **c13 分支式子 Agent 的父快照**（`conversation.parent_snapshot`）——
       快照是在 Agent Loop 的**串行段内**取的，此刻循环已经把
       `assistant(tool_calls)` 追加进历史、而对应的 `tool` 结果要等本轮
       全部工具跑完才追加。不清理的话，子 Agent 的首次请求必然 400：
       *"An assistant message with 'tool_calls' must be followed by tool messages"*。
       这是真实模型验收实测到的缺陷——**单元测试抓不到**，因为它们都是从
       一段干净的历史建快照。

    两趟扫描：
    1. 收集全部 role=tool 行的 tool_call_id 集合；
    2. 逐条判定——assistant(tool_calls) 的所有调用 id 都在集合中才保留，否则该
       assistant 进入「丢弃组」，其名下 id 记入丢弃集合；role=tool 行只有其 id
       归属某个**被保留**的 assistant 才保留（孤儿/属被丢组的都丢）。

    :returns: (清理后的消息, 丢弃条数)
    """
    tool_ids = {m.tool_call_id for m in messages if m.role == "tool" and m.tool_call_id}

    kept: list[Message] = []
    dropped = 0
    valid_call_ids: set[str] = set()  # 被保留的 assistant 名下的调用 id
    for m in messages:
        if m.role == "assistant" and m.tool_calls:
            if all(tc.id in tool_ids for tc in m.tool_calls):
                kept.append(m)
                valid_call_ids.update(tc.id for tc in m.tool_calls)
            else:
                dropped += 1  # 组内已有的 tool 行在下方各自计数
        elif m.role == "tool":
            if m.tool_call_id in valid_call_ids:
                kept.append(m)
            else:
                dropped += 1
        else:
            kept.append(m)
    return kept, dropped


def _parse_ts(ts: Optional[str]) -> Optional[datetime]:
    """ISO 时间戳的宽容解析：坏格式返回 None。"""
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts)
    except ValueError:
        return None
