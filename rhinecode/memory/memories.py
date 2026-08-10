"""
笔记与索引的纯逻辑（c9 F14/F16）：格式转换，零 IO 决策。

一条笔记 = 一个带 frontmatter 的 Markdown 文件：

    ---
    name: prefer-chinese-comments
    summary: 用户要求所有代码注释使用中文
    category: preference
    ---

    正文……

设计取舍：
- frontmatter 用手写的宽松解析而非引入 YAML 库解析——字段只有三个扁平的
  `key: value`，宽松逐行解析足够，且**未知字段忽略**（spec N5 向前兼容：
  旧版本能读新版本写的文件）比严格 schema 更重要。
- 索引文件是「每条笔记占一行」的 Markdown 列表，重建式生成（扫目录 → 全量渲染），
  不做增量编辑——重建幂等、天然自愈（手工删了笔记文件，下次重建索引自动同步）。
- 200 行 / 25KB 截断（spec F16）只发生在**注入**时，索引文件本身保持完整。
"""

from dataclasses import dataclass
from typing import Optional

# 四类笔记（spec F14）：用户偏好 / 纠正反馈 / 项目知识 / 参考资料。
CATEGORIES = ("preference", "feedback", "project", "reference")

# 分类的中文标签（/memory 报告与索引展示用）。
CATEGORY_LABELS = {
    "preference": "用户偏好",
    "feedback": "纠正反馈",
    "project": "项目知识",
    "reference": "参考资料",
}

# 索引注入上限（spec F16）：先到为准。
INDEX_MAX_LINES = 200
INDEX_MAX_BYTES = 25 * 1024


@dataclass
class Memory:
    """
    一条笔记的内存形态。

    :param filename: 笔记文件名（如 prefer-chinese-comments.md），不含目录
    :param name: 笔记标识（frontmatter 的 name）
    :param summary: 一行摘要（索引里的钩子，供模型判断要不要读全文）
    :param category: 四类之一（CATEGORIES）
    :param body: 正文（frontmatter 之后的内容）
    """

    filename: str
    name: str
    summary: str
    category: str
    body: str = ""


def parse_memory(text: str, filename: str = "") -> Optional[Memory]:
    """
    宽松解析一个笔记文件的文本。

    执行流程：
    1. 找 frontmatter：首个非空行必须是 `---`，到下一个 `---` 为止；
    2. 逐行解析 `key: value`（首个冒号分割）；未知 key 忽略（N5）；
    3. 校验 name / summary / category 三个必填字段齐全且 category 合法；
    4. 其余内容为正文。

    :param text: 文件全文
    :param filename: 文件名（回填到 Memory.filename，便于索引渲染）
    :returns: Note；格式坏 / 缺必填字段 / 非法分类 → None（调用方跳过该文件）

    副作用：无。
    """
    lines = text.splitlines()
    # 定位 frontmatter 起始：跳过前导空行后必须是 "---"。
    idx = 0
    while idx < len(lines) and not lines[idx].strip():
        idx += 1
    if idx >= len(lines) or lines[idx].strip() != "---":
        return None
    # 收集 frontmatter 行直到闭合的 "---"。
    fields: dict[str, str] = {}
    end = -1
    for i in range(idx + 1, len(lines)):
        stripped = lines[i].strip()
        if stripped == "---":
            end = i
            break
        if not stripped or ":" not in stripped:
            continue  # 空行/坏行宽松跳过
        key, _, value = stripped.partition(":")
        fields[key.strip()] = value.strip()
    if end < 0:
        return None  # frontmatter 未闭合

    name = fields.get("name", "")
    summary = fields.get("summary", "")
    category = fields.get("category", "")
    if not name or not summary or category not in CATEGORIES:
        return None

    body = "\n".join(lines[end + 1:]).strip()
    return Memory(filename=filename, name=name, summary=summary, category=category, body=body)


def render_memory(memory: Memory) -> str:
    """
    把 Memory 渲染为磁盘文本（与 parse_memory 往返一致）。

    副作用：无。
    """
    return (
        "---\n"
        f"name: {memory.name}\n"
        f"summary: {memory.summary}\n"
        f"category: {memory.category}\n"
        "---\n"
        "\n"
        f"{memory.body.strip()}\n"
    )


def rebuild_index(memories: list[Memory]) -> str:
    """
    由笔记列表全量重建索引文本：首行标题 + 每条一行。

    行格式：`- {name}（{filename}）[分类] — {summary}`——name 是标识、filename 是
    模型按需读全文的路径线索、summary 是判断相关性的钩子（spec F16）。

    副作用：无。
    """
    lines = ["# 记忆索引", ""]
    for n in memories:
        label = CATEGORY_LABELS.get(n.category, n.category)
        lines.append(f"- {n.name}（{n.filename}）[{label}] — {n.summary}")
    return "\n".join(lines) + "\n"


def truncate_index(text: str, max_lines: int = INDEX_MAX_LINES, max_bytes: int = INDEX_MAX_BYTES) -> str:
    """
    索引注入截断（spec F16）：先按行截、再按 UTF-8 字节截，先到为准。

    字节截断不能直接 text.encode()[:N] 再 decode——可能把一个多字节字符切成两半
    产生非法 UTF-8。这里按**整行**回退：逐行累加字节数，放不下的行整行丢弃。

    副作用：无。
    """
    lines = text.splitlines()[:max_lines]
    out: list[str] = []
    total = 0
    for line in lines:
        size = len(line.encode("utf-8")) + 1  # +1 计换行符
        if total + size > max_bytes:
            break
        out.append(line)
        total += size
    return "\n".join(out)
