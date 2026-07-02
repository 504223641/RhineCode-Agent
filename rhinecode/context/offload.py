"""
第一层·轻量预防：工具结果存盘（c8 F4/F5/F6/F7）。

思路：token 大头是工具结果（read_file / grep / run_command 的输出）。第一层不调模型、
成本低——把「过大的工具结果」完整写入磁盘文件，历史里只留「预览片段 + 文件路径」占位，
从源头抑制单条消息膨胀。模型若需要完整内容，可按占位里的路径用 read_file 重新读取。

两条规则（每次请求前由 ContextManager 调 run 执行）：
- 单结果：任一工具结果估算超过 SINGLE_RESULT_TOKENS，直接存盘。
- 聚合：即便单个都不超阈值，若剩余工具结果合计超过 COMBINED_RESULT_TOKENS，
  按体积从大到小依次存盘，直到合计降到阈值以下（挑大的先存，最小改动换最大收益）。

边界与安全：
- 只作用于 role="tool" 消息，绝不触碰 role="user"（用户原文是任务锚点，F6）。
- 幂等：以 tool_call_id 为键记录已存盘集合，已替换为占位的消息不再二次处理（F7）。
- 写盘 I/O 失败时跳过该条、保留原文（fail-safe，N2），不因存盘失败中断对话。
- 写入目录锁定在 <项目根>/.rhinecode/context/，属内部可信写盘，不经工具权限管线（N6）。
"""

from pathlib import Path

from rhinecode.provider.base import Message
from rhinecode.tools.base import human_size
from rhinecode.context.estimate import estimate_message_tokens
from rhinecode.context.models import CompactionNotice

# 单个工具结果超过此估算 token 数即单独存盘。
SINGLE_RESULT_TOKENS: int = 4000
# 剩余（未存盘的）工具结果合计超过此估算 token 数，触发聚合存盘。
COMBINED_RESULT_TOKENS: int = 16000
# 占位里保留的预览字符数（结果头部），供模型判断相关性、决定是否重读。
PREVIEW_CHARS: int = 500


class Offloader:
    """
    工具结果存盘器。长期持有于 ContextManager 内，跨请求维护「已存盘集合」保证幂等。

    :ivar _store_dir: 存盘目录（<项目根>/.rhinecode/context/），惰性创建
    :ivar _offloaded: 已存盘的消息键集合（tool_call_id 或回退序号），用于幂等
    :ivar _seq: 无 tool_call_id 时的回退递增序号，保证文件名唯一
    """

    def __init__(self, store_dir: Path) -> None:
        """
        :param store_dir: 存盘目录；不在此创建，真正写盘时才 mkdir（惰性、避免空目录）
        """
        self._store_dir = store_dir
        self._offloaded: set[str] = set()
        self._seq: int = 0

    @property
    def count(self) -> int:
        """累计已存盘的工具结果数（供 /context 报告）。"""
        return len(self._offloaded)

    def reset(self) -> None:
        """清空已存盘集合（/clear 时调用；不删除磁盘文件，仅重置幂等状态）。"""
        self._offloaded.clear()
        self._seq = 0

    def _key(self, msg: Message) -> str:
        """
        计算消息的幂等键。优先用 tool_call_id（tool 消息天然唯一）；
        缺失时用递增序号兜底，保证键与文件名不冲突。
        """
        if msg.tool_call_id:
            return msg.tool_call_id
        self._seq += 1
        return f"_seq{self._seq}"

    def _placeholder(self, original: str, path: Path) -> str:
        """
        构造替换原始工具结果的占位文本：体量说明 + 头部预览 + 文件路径 + 重读提示。

        预览让模型能判断这段结果是否与当前任务相关；路径与提示引导它在需要完整内容时
        重新 read_file，而不是凭空假设内容（呼应第二层的边界消息理念）。
        """
        preview = original[:PREVIEW_CHARS]
        ellipsis = "…" if len(original) > PREVIEW_CHARS else ""
        return (
            f"[大型工具结果已存盘 · 原 {human_size(len(original.encode('utf-8')))}]\n"
            f"预览（前 {PREVIEW_CHARS} 字）：\n{preview}{ellipsis}\n"
            f"完整内容见文件：{path}\n"
            f"（需要完整内容时，请用 read_file 读取该文件路径）"
        )

    def _offload_one(self, msg: Message) -> bool:
        """
        把单条工具结果存盘并原地替换为占位。

        :param msg: 待存盘的 role="tool" 消息（原地修改其 content）
        :returns: 存盘成功 True；写盘失败 False（此时保留原文，不改 content）

        副作用：可能创建目录、写文件；成功时修改 msg.content 并登记幂等键。
        """
        key = self._key(msg)
        try:
            self._store_dir.mkdir(parents=True, exist_ok=True)
            file_path = self._store_dir / f"{key}.txt"
            file_path.write_text(msg.content, encoding="utf-8")
        except OSError:
            # 写盘失败：保留原文、不登记幂等键，本次不压缩这条（fail-safe，N2）。
            return False
        msg.content = self._placeholder(msg.content, file_path)
        self._offloaded.add(key)
        return True

    def run(self, history: list[Message]) -> list[CompactionNotice]:
        """
        对历史执行第一层存盘：单结果趟 + 聚合趟。

        :param history: 当前对话历史（原地修改超大工具结果的 content）
        :returns: 本次有存盘则返回单条 CompactionNotice(kind="offload")，否则空列表

        副作用：可能写盘并原地修改若干工具结果消息的 content。
        """
        # 候选：尚未存盘的 role="tool" 消息（幂等：已存盘的 content 是占位，不再处理）。
        # 以 id(msg) 判重来跳过「已在集合里」的消息——占位后其 tool_call_id 已入集合。
        def is_candidate(m: Message) -> bool:
            if m.role != "tool":
                return False
            return not (m.tool_call_id and m.tool_call_id in self._offloaded)

        offloaded_count = 0

        # 第一趟：单结果超阈值直接存盘。
        for m in history:
            if not is_candidate(m):
                continue
            if estimate_message_tokens(m) > SINGLE_RESULT_TOKENS:
                if self._offload_one(m):
                    offloaded_count += 1

        # 第二趟：聚合。对仍未存盘的工具结果算合计，若超阈值则按体积降序依次存盘。
        remaining = [m for m in history if is_candidate(m)]
        total = sum(estimate_message_tokens(m) for m in remaining)
        if total > COMBINED_RESULT_TOKENS:
            # 从大到小排序，挑大的先存，直到合计降到阈值以下。
            remaining.sort(key=estimate_message_tokens, reverse=True)
            for m in remaining:
                if total <= COMBINED_RESULT_TOKENS:
                    break
                tokens = estimate_message_tokens(m)
                if self._offload_one(m):
                    offloaded_count += 1
                    total -= tokens

        if offloaded_count == 0:
            return []
        return [
            CompactionNotice(
                kind="offload",
                message=f"📦 已把 {offloaded_count} 个大型工具结果存盘，历史仅保留预览与路径。",
            )
        ]
