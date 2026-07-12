"""
笔记 LLM 的请求渲染与响应解析（c9 F15，纯逻辑，不做 IO、不调网络）。

职责边界（与 c8 summarize 同定位）：本模块只负责「把素材变成请求」和「把响应变成
结构化动作」，provider 调用与写盘都由 MemoryManager 编排——**LLM 只产出意图，
写盘权收拢在 manager 的锁临界区内**（spec F15/N6）。

安全设计：
- 请求强制 tools=None（由 manager 保证），Prompt 里也明确告知模型无工具可用；
- 响应要求纯 JSON 数组；解析时对 filename 做 `[a-z0-9_-]+\\.md` 白名单校验——
  这是防注入的最后一道闸：即使模型输出 `../../etc/passwd` 这样的文件名，也会
  作为坏项被丢弃，写盘永远出不了 memory 目录。
"""

import json
import re
from dataclasses import dataclass
from typing import Optional

from rhinecode.provider.base import Message
from rhinecode.memory.notes import CATEGORIES, Note

# 笔记文件名白名单：小写字母/数字/连字符/下划线 + .md 后缀，禁止任何路径分隔符。
_FILENAME_RE = re.compile(r"^[a-z0-9_-]+\.md$")

# 转录里单条工具结果的最大字符数：笔记 LLM 只需要判断「值不值得记」，
# 不需要工具结果全文；截断控制请求体积（工具结果动辄数千字符）。
_TOOL_RESULT_PREVIEW_CHARS = 500

# 合法的动作与归属取值。
_VALID_OPS = ("add", "update", "delete")
_VALID_SCOPES = ("user", "project")

# 笔记更新的系统提示：定义四类分类、归属标准、去重要求与输出格式。
NOTE_SYSTEM_PROMPT = """\
你是一个编程助手的记忆管理器。你会看到两份「现有记忆索引」和一段「最近的对话」，\
任务是判断这段对话里有没有**将来的会话仍然有用**的信息值得记成笔记。

## 四类笔记（category）
- preference（用户偏好）：用户的个人习惯与口味，如「注释用中文」「不要用缩写」。
- feedback（纠正反馈）：用户对助手行为的纠正与确认，应记录原因和以后怎么做。
- project（项目知识）：这个项目特有的、代码里看不出来的知识，如架构决策背景、坑。
- reference（参考资料）：外部资源指针，如文档 URL、issue 链接。

## 归属（scope）
- user：跨项目的用户个人信息（偏好、习惯）→ 存用户级。
- project：只与当前项目相关的知识 → 存项目级。

## 判断标准
- 只记「将来会再用到」的信息；一次性的问答、闲聊、代码本身能看出来的内容不记。
- **对照现有索引去重**：已有等价笔记就不要新增；信息有更新就输出 update（沿用原 filename）；
  已明确失效的可输出 delete。
- 没有值得记的内容时输出空数组 []。这是常态，不要为了输出而编造。

## 输出格式（严格遵守）
你没有任何工具可用，也不要输出解释文字。只输出一个 JSON 数组，每个元素形如：
{"op": "add|update|delete", "scope": "user|project", "filename": "kebab-case-name.md",
 "name": "笔记标识", "summary": "一行摘要（索引钩子）", "category": "preference|feedback|project|reference",
 "body": "笔记正文，写清楚事实、原因和怎么应用"}
- filename 只能用小写字母/数字/连字符/下划线加 .md 后缀，不含任何目录。
- delete 时只需 op/scope/filename 三个字段。
"""


@dataclass
class NoteAction:
    """
    LLM 决定的一个笔记动作（只是意图，不含 IO）。

    :param op: "add" | "update" | "delete"
    :param scope: "user"（用户级目录）| "project"（项目级目录）
    :param filename: 目标文件名（已过白名单校验，不含目录）
    :param note: 要写入的笔记内容；op="delete" 时为 None
    """

    op: str
    scope: str
    filename: str
    note: Optional[Note] = None


def build_note_request(
    new_messages: list[Message],
    user_index: str,
    project_index: str,
) -> "tuple[str, list[Message]]":
    """
    把「本轮新增对话 + 现有两级索引」渲染成一次笔记 LLM 请求。

    与 c8 摘要同一手法：素材渲染成**一条 user 转录**而非转发原始消息，规避 API 对
    tool 消息配对的校验；工具结果截断到前 _TOOL_RESULT_PREVIEW_CHARS 字符控制体积。

    :param new_messages: 高水位之后的新增消息（本轮要审视的对话段）
    :param user_index: 用户级现有索引文本（无则空串）
    :param project_index: 项目级现有索引文本（无则空串）
    :returns: (system 提示, 消息列表)——直接交给 provider.stream_chat(system=..., tools=None)

    副作用：无。
    """
    lines: list[str] = [
        "## 现有记忆索引（用户级）",
        user_index.strip() or "（暂无）",
        "",
        "## 现有记忆索引（项目级）",
        project_index.strip() or "（暂无）",
        "",
        "## 最近的对话",
    ]
    for m in new_messages:
        if m.role == "user":
            lines.append(f"【用户】{m.content}")
        elif m.role == "assistant":
            text = m.content or ""
            if m.tool_calls:
                calls = ", ".join(tc.name for tc in m.tool_calls)
                text = (text + f"\n（发起工具调用：{calls}）").strip()
            lines.append(f"【助手】{text}")
        elif m.role == "tool":
            content = m.content or ""
            if len(content) > _TOOL_RESULT_PREVIEW_CHARS:
                content = content[:_TOOL_RESULT_PREVIEW_CHARS] + "…（已截断）"
            lines.append(f"【工具结果】{content}")
    return NOTE_SYSTEM_PROMPT, [Message(role="user", content="\n\n".join(lines))]


def parse_note_response(text: str) -> list[NoteAction]:
    """
    把笔记 LLM 的输出解析为动作列表（宽松容错，F17）。

    执行流程：
    1. 截取首个 `[` 到末个 `]` 的子串（容忍模型在 JSON 外包了解释文字或代码围栏）；
    2. json.loads 失败或结果非数组 → 返回 []（本轮无动作，不算崩溃）；
    3. 逐项校验：op / scope / category 必须在白名单内，filename 必须过 _FILENAME_RE；
       add/update 还须有非空 name 与 summary；坏项**逐个跳过**，不连坐。

    :param text: LLM 输出全文
    :returns: 合法的 NoteAction 列表（可能为空）

    副作用：无。
    """
    if not text:
        return []
    start, end = text.find("["), text.rfind("]")
    if start < 0 or end <= start:
        return []
    try:
        data = json.loads(text[start : end + 1])
    except ValueError:
        return []
    if not isinstance(data, list):
        return []

    actions: list[NoteAction] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        op = item.get("op")
        scope = item.get("scope")
        filename = item.get("filename")
        if op not in _VALID_OPS or scope not in _VALID_SCOPES:
            continue
        if not isinstance(filename, str) or not _FILENAME_RE.match(filename):
            continue  # 防注入：非法文件名（含路径分隔符/大写/怪字符）直接丢弃
        if op == "delete":
            actions.append(NoteAction(op=op, scope=scope, filename=filename))
            continue
        name = item.get("name")
        summary = item.get("summary")
        category = item.get("category")
        if not isinstance(name, str) or not name.strip():
            continue
        if not isinstance(summary, str) or not summary.strip():
            continue
        if category not in CATEGORIES:
            continue
        body = item.get("body")
        note = Note(
            filename=filename,
            name=name.strip(),
            summary=summary.strip(),
            category=category,
            body=body.strip() if isinstance(body, str) else "",
        )
        actions.append(NoteAction(op=op, scope=scope, filename=filename, note=note))
    return actions
