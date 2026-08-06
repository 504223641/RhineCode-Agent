"""
C13 人眼验收的测试工作区搭建脚本。

用法（在本仓库根目录）：

    python docs/c13/acceptance/setup_manual.py

它会在 `~/rhine-c13-manual/` 下造一个独立的小项目：**不碰本仓库**，
子 Agent 在里面读写都影响不到 RhineCode 的代码。

造出来的东西：

- 24 个源文件，同一个常量 `RETRY_LIMIT` 散落在其中若干处
  （文件数量刻意做多，好让子 Agent 跑够时间，「并行」与「循环等待」才看得出来）；
- 一个项目级角色 `auditor`（只读审查，用于验「角色边界读不读得懂」）；
- 一个项目级角色 `counter`（**故意跑得慢**：要求它逐个文件读完再汇总，
  用于「多委派是否并行」与「并发上限」那两条需要「子 Agent 别太快结束」的场景）。

脚本可重复运行：每次先清空目标目录再重建。
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

TARGET = Path.home() / "rhine-c13-manual"

# ── 角色定义 ──────────────────────────────────────────────────────────

AUDITOR = """---
name: auditor
description: 需要检查代码里是否存在硬编码常量、重复定义、缺少测试覆盖这类问题时用它。只读，不会修改任何东西。
tools: read_file, glob_files, grep_content
permission_mode: strict
max_turns: 10
---

你是代码审查员。只读地检查问题，最后一段给出自包含的结论，
写清每个问题的文件路径与具体位置。不要尝试修改任何文件。
"""

COUNTER = """---
name: counter
description: 需要逐个文件统计、清点、盘点某个东西在项目里的分布时用它。它会把每个文件单独打开确认，因此比较慢但很彻底。
tools: read_file, glob_files, grep_content
permission_mode: strict
max_turns: 20
---

你是清点员。你的工作方式是**逐个文件确认**，不允许只靠一次全局搜索就下结论。

工作步骤（必须按顺序做完）：

1. 先用 glob_files 列出项目里所有 `.py` 文件；
2. **逐个** read_file 打开每一个文件，逐个确认里面有没有目标内容
   ——一次只读一个文件，读完再读下一个，不要并发批量读；
3. 每读完一个文件，简短记一句「文件 X：有/没有」；
4. 全部读完之后，才给出汇总结论。

最后一段必须是自包含的结论：目标内容出现在哪几个文件、各在第几行、共几处。
不要尝试修改任何文件。
"""


def _sources() -> dict[str, str]:
    """造 24 个源文件，其中 6 个含 RETRY_LIMIT。"""
    files: dict[str, str] = {
        "src/config.py": "# 全局配置\nRETRY_LIMIT = 3\nTIMEOUT_SECONDS = 30\nPOOL_SIZE = 8\n",
        "src/client.py": (
            "from src.config import RETRY_LIMIT\n\n\n"
            "def fetch(url):\n"
            "    for attempt in range(RETRY_LIMIT):\n"
            "        pass\n"
        ),
        "src/worker.py": (
            "from src.config import RETRY_LIMIT\n\n\n"
            "def run_job(job):\n"
            "    remaining = RETRY_LIMIT\n"
            "    while remaining > 0:\n"
            "        remaining -= 1\n"
        ),
        "src/uploader.py": (
            "from src.config import RETRY_LIMIT\n\n\n"
            "def upload(blob):\n"
            "    tries = RETRY_LIMIT * 2\n"
            "    return tries\n"
        ),
        "tests/test_client.py": (
            "def test_retry():\n"
            "    # RETRY_LIMIT 改成 5 之后这条要跟着改\n"
            "    assert True\n"
        ),
        "docs/notes.md": "# 备注\n\n重试次数（RETRY_LIMIT）目前是 3，考虑调大。\n",
        "README.md": "# 演示项目\n\n一个用来做 C13 人眼验收的小项目。\n",
        "RHINE.md": "# 本项目\n\n用中文回答。这是一个验收用的临时项目，不必谨慎对待。\n",
    }
    # 再填一批不含目标常量的文件，把项目撑大，让逐个读文件真的要花时间
    for i in range(1, 17):
        files[f"src/module_{i:02d}.py"] = (
            f'"""模块 {i}：与重试无关的业务代码。"""\n\n\n'
            f"def handler_{i:02d}(payload):\n"
            f"    total = 0\n"
            f"    for item in payload:\n"
            f"        total += len(str(item))\n"
            f"    return total\n\n\n"
            f"def helper_{i:02d}(value):\n"
            f"    return value * {i}\n"
        )
    return files


def main() -> int:
    if TARGET.exists():
        # 只删我们自己造的那个目录，且必须在用户主目录下、名字完全匹配
        assert TARGET.parent == Path.home(), TARGET
        assert TARGET.name == "rhine-c13-manual", TARGET
        shutil.rmtree(TARGET)

    for rel, text in _sources().items():
        path = TARGET / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")

    agents = TARGET / ".rhinecode" / "agents"
    agents.mkdir(parents=True, exist_ok=True)
    (agents / "auditor.md").write_text(AUDITOR, encoding="utf-8")
    (agents / "counter.md").write_text(COUNTER, encoding="utf-8")

    # trace 产物不要被 git 之类的东西盯上（这个目录本来也不是仓库，纯属好习惯）
    (TARGET / ".gitignore").write_text(".rhinecode/\n", encoding="utf-8")

    py_count = len(list(TARGET.rglob("*.py")))
    print(f"已建好测试工作区：{TARGET}")
    print(f"  Python 文件 {py_count} 个，其中 4 个含 RETRY_LIMIT")
    print(f"  项目级角色：auditor（只读审查）、counter（逐文件清点，故意慢）")
    print()
    print("下一步：")
    print(f"  cd {TARGET}")
    print("  rhine --trace")
    return 0


if __name__ == "__main__":
    sys.exit(main())
