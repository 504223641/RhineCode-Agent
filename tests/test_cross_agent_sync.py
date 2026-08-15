"""跨 Agent 一致性护栏（Claude Code ↔ Codex）。

## 这份文件在防什么

这个项目同时被两个 Agent 开发：Claude Code 与 Codex。让它们「对项目的理解一致」
靠的是三条接线，而**三条全都属于漏改不报错的形态**——坏掉之后编译过、测试绿、
两个 Agent 也都照常回答问题，只是其中一个悄悄少知道了一大块东西：

1. **指令文件的字节上限**。Codex 默认只读 32 KiB 的项目指令，超出部分
   **从文件中间按字节截断**，只在日志里 warn、TUI 上没有任何提示
   （源码 `codex-rs/core/src/agents_md.rs` 的 `data.truncate(remaining)`）。
   `.codex/config.toml` 把它抬到了 256 KiB，但 CLAUDE.md 是会长的——
   哪天它涨过上限，Codex 就会从某一行开始什么都收不到，而**没有任何东西会响**。

2. **文件名回退**。Codex 原生读 AGENTS.md，靠 `project_doc_fallback_filenames`
   才会回退去读 CLAUDE.md，而这个回退**只在 AGENTS.md 不存在时**生效。
   将来谁顺手建一个 AGENTS.md（哪怕只写一行），CLAUDE.md 对 Codex 就整个失联了。

3. **共享记忆的索引**。两个 harness 的私有记忆互相看不见且都不可重定位，
   所以唯一的共享记忆在 `docs/agent-memory/`，靠 CLAUDE.md 引用它才对双方生效。
   索引与目录一旦对不上，新写的记忆就成了没人读的孤儿文件。

## 判据边界

⚠ **上限那条必须从 `.codex/config.toml` 里把数字读出来比，不能硬编码。**
写死一个 262144 的话，将来有人调低配置（或误删那一行退回 32 KiB 默认值），
测试照样绿——而那恰恰是最该被抓住的一次改动。护栏要钉的是「文档大小与**实际
生效的**上限」这层关系，不是某个具体数字。
"""

from __future__ import annotations

import re
import tomllib
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

CLAUDE_MD = REPO_ROOT / "CLAUDE.md"
CODEX_CONFIG = REPO_ROOT / ".codex" / "config.toml"
MEMORY_DIR = REPO_ROOT / "docs" / "agent-memory"
MEMORY_INDEX = MEMORY_DIR / "INDEX.md"
CLAUDE_SPEC = REPO_ROOT / ".claude" / "commands" / "spec.md"
CODEX_SPEC = REPO_ROOT / ".codex" / "skills" / "spec" / "SKILL.md"

# Codex 内置默认值。配置里没写那一行时它就是实际生效的上限，
# 用它兜底才能让「误删配置」也被本护栏抓到。
CODEX_DEFAULT_MAX_BYTES = 32768


def _read_codex_config() -> dict:
    """读项目级 Codex 配置。缺文件直接失败——它是整套接线的地基。"""
    with CODEX_CONFIG.open("rb") as fh:
        return tomllib.load(fh)


def _frontmatter(path: Path) -> dict[str, str]:
    """取 Markdown 文件的 YAML frontmatter 顶层标量字段。

    刻意不引 yaml：这里只需要 name / description 两个顶层字符串，
    手写解析可以让本护栏不依赖任何第三方库（它要在最朴素的环境里也能跑）。
    值两侧的引号会被剥掉，便于与另一份逐字比对。
    """
    text = path.read_text(encoding="utf-8")
    if not text.startswith("---"):
        return {}
    end = text.find("\n---", 3)
    if end == -1:
        return {}
    fields: dict[str, str] = {}
    for line in text[3:end].splitlines():
        if not line.strip() or line.startswith((" ", "\t")) or ":" not in line:
            continue
        key, _, value = line.partition(":")
        fields[key.strip()] = value.strip().strip('"').strip("'")
    return fields


class ProjectDocBudgetTest(unittest.TestCase):
    """CLAUDE.md 必须完整装得进 Codex 实际生效的字节上限。"""

    def test_claude_md_fits_in_codex_budget(self) -> None:
        limit = _read_codex_config().get(
            "project_doc_max_bytes", CODEX_DEFAULT_MAX_BYTES
        )
        size = CLAUDE_MD.stat().st_size
        self.assertLess(
            size,
            limit,
            f"CLAUDE.md 现在 {size} 字节，超过了 Codex 生效的上限 {limit} 字节。"
            f"Codex 会从中间把它截断且不报错——被切掉的是文件尾部（安全边界、"
            f"已知工程项、注释规范这些「不请自来才有用」的章节）。"
            f"要么精简 CLAUDE.md，要么调高 .codex/config.toml 的 project_doc_max_bytes。",
        )

    def test_budget_is_raised_above_the_codex_default(self) -> None:
        """反证：配置必须真的抬高过默认值，否则这套接线等于没配。"""
        limit = _read_codex_config().get(
            "project_doc_max_bytes", CODEX_DEFAULT_MAX_BYTES
        )
        self.assertGreater(
            limit,
            CODEX_DEFAULT_MAX_BYTES,
            "project_doc_max_bytes 没有高于 Codex 的内置默认值 32768，"
            "CLAUDE.md 会被截掉绝大部分。",
        )


class FallbackFilenameTest(unittest.TestCase):
    """Codex 必须能回退读到 CLAUDE.md，且不能有第二份事实来源。"""

    def test_claude_md_is_registered_as_fallback(self) -> None:
        fallbacks = _read_codex_config().get("project_doc_fallback_filenames", [])
        self.assertIn(
            "CLAUDE.md",
            fallbacks,
            "project_doc_fallback_filenames 里没有 CLAUDE.md，"
            "Codex 找不到 AGENTS.md 时会什么项目指令都读不到。",
        )

    def test_no_agents_md_shadows_claude_md(self) -> None:
        """AGENTS.md 一旦存在，回退就不发生，CLAUDE.md 对 Codex 整个失联。

        ⚠ 这条是「刻意不建」而不是「还没建」：单一事实来源是本套接线的全部依据，
        建第二份文档必然随时间分叉，而分叉的表现正是这套东西要解决的原问题。
        """
        for name in ("AGENTS.md", "AGENTS.override.md"):
            self.assertFalse(
                (REPO_ROOT / name).exists(),
                f"仓库根出现了 {name}。它会顶替掉 CLAUDE.md 的回退，"
                f"使 Codex 不再读 CLAUDE.md。项目指令只应有 CLAUDE.md 一份。",
            )


class SharedMemoryTest(unittest.TestCase):
    """共享记忆层：索引与目录必须双向对齐，且不含 harness 私有字段。"""

    def _index_links(self) -> list[str]:
        return re.findall(r"\]\((\w+\.md)\)", MEMORY_INDEX.read_text(encoding="utf-8"))

    def test_index_has_no_dangling_links(self) -> None:
        missing = [name for name in self._index_links() if not (MEMORY_DIR / name).exists()]
        self.assertEqual(missing, [], f"共享记忆索引指向了不存在的文件：{missing}")

    def test_every_memory_file_is_indexed(self) -> None:
        """反向：目录里的记忆必须都在索引上。

        没有这一条的话，新写的记忆会成为孤儿——两个 Agent 都只读索引来判断
        相关性，不在索引上就等于不存在。
        """
        linked = set(self._index_links())
        on_disk = {p.name for p in MEMORY_DIR.glob("*.md")} - {"INDEX.md"}
        orphans = sorted(on_disk - linked)
        self.assertEqual(
            orphans, [], f"这些记忆没有登记进 INDEX.md，两个 Agent 都不会读到：{orphans}"
        )

    def test_no_harness_private_fields_leak(self) -> None:
        """originSessionId 是会话标识，不应进版本库；node_type 无跨 Agent 语义。"""
        for path in sorted(MEMORY_DIR.glob("*.md")):
            text = path.read_text(encoding="utf-8")
            for field in ("originSessionId", "node_type"):
                self.assertNotIn(
                    field, text, f"{path.name} 残留了 harness 私有字段 {field}"
                )

    def test_claude_md_points_at_the_shared_memory(self) -> None:
        """接线本身：CLAUDE.md 必须引用共享记忆索引。

        它是两个 Agent 唯一都会自动读到的文档，索引只有被它引用才对双方生效。
        """
        self.assertIn(
            "docs/agent-memory/INDEX.md",
            CLAUDE_MD.read_text(encoding="utf-8"),
            "CLAUDE.md 没有引用 docs/agent-memory/INDEX.md，共享记忆对两个 Agent 都不可见。",
        )


class SpecSkillParityTest(unittest.TestCase):
    """`/spec` 在两个 Agent 里必须是同一个东西。"""

    def test_pointer_target_exists(self) -> None:
        self.assertTrue(
            CLAUDE_SPEC.exists(),
            f"Codex 技能指向的 {CLAUDE_SPEC} 不存在，/spec 在 Codex 里会指向空气。",
        )

    def test_frontmatter_is_identical(self) -> None:
        """name 与 description 逐字相同——description 是 Codex 决定要不要触发的唯一依据。"""
        claude_fm = _frontmatter(CLAUDE_SPEC)
        codex_fm = _frontmatter(CODEX_SPEC)
        for field in ("name", "description"):
            self.assertEqual(
                claude_fm.get(field),
                codex_fm.get(field),
                f"/spec 的 {field} 在两个 Agent 里不一致：\n"
                f"  .claude/commands/spec.md: {claude_fm.get(field)!r}\n"
                f"  .codex/skills/spec/SKILL.md: {codex_fm.get(field)!r}",
            )

    def test_codex_skill_stays_a_pointer(self) -> None:
        """反证：它必须继续是指针，不能被复制成正文。

        复制过来不会报错，两边一开始也完全一致——分叉是几个月后才发生的，
        那时已经没人记得它们本该同源。用体积差异钉住这个形态。
        """
        pointer_size = CODEX_SPEC.stat().st_size
        body_size = CLAUDE_SPEC.stat().st_size
        self.assertLess(
            pointer_size,
            body_size // 2,
            f".codex/skills/spec/SKILL.md 有 {pointer_size} 字节，接近正文的 "
            f"{body_size} 字节，像是被复制成了正文。它应当只是一个指向 "
            f".claude/commands/spec.md 的指针——两份正文必然分叉。",
        )


if __name__ == "__main__":
    unittest.main()
