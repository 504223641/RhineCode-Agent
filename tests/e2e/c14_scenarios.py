"""
C14（子 Agent 工作区隔离）真实模型验收的场景预置。

与 `c11_scenarios.py` / `sweep_scenarios.py` 同性质：**只负责把场面搭好**，
判定留给驱动方（读 trace + 直接看磁盘）。每个函数签名固定为
`(workspace, user_dir)`，由宿主的 `--seed` 载入。

## 为什么 C14 的预置比前几章都重

前几章验的是「模型在一个像样的项目里表现如何」，本章验的是「**文件到底落在哪**」，
因此每个场景都必须同时满足三个前置，缺一个都会让场景失去意义：

1. **工作区必须是真实 git 仓库**——`git worktree add` 是隔离的物理地基，
   非仓库里它必然失败（那正是场景 3 要验的东西，故场景 3 反过来**刻意不建**仓库）。
2. **必须预置 allow 规则**——子 Agent **全程非交互，判 ASK 一律自动拒绝**（c13）。
   缺省档下写文件、跑命令都会被挡，隔离子 Agent 会一个字节都写不出来，
   于是「隔离有没有生效」根本无从观察。这与 `seed_subagents` 刻意**不给** allow
   规则的用意正相反：那一章验的是「被拒之后模型收不收敛」。
3. **角色必须声明 `isolation: worktree`**——否则模型即便委派了也不隔离，
   而两者在界面上的差别只有一行交付信息，很容易误判为「隔离失效」。

## 一条实测约束：分支名无法预先占位

C14 的分支名是 `agent/<角色名>-<任务 ID 前 8 位>`，任务 ID 每次随机。
所以 checklist 场景 6「交付信息不被模型污染」在真实模型下**不能靠预占分支**来构造，
只能让角色正文**指使模型说一个假分支名**（见 `_REPORTER_BODY`），
再对照系统追加的交付信息段。判据不变：系统给的必须是实际值。
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

from tests.e2e import seeding


# ---------------------------------------------------------------------------
# 一个够真实的小项目：命令行计算器
# ---------------------------------------------------------------------------
# 刻意让 `calc/core.py` 与 `calc/cli.py` 各有一处待改的地方，这样「主 Agent 改一处、
# 子 Agent 改另一处」是自然的分工，而不是硬凑出来的并发。
_CORE_PY = '''"""计算器核心：四则运算。"""


def add(a, b):
    return a + b


def sub(a, b):
    return a - b


def mul(a, b):
    return a * b
'''

_CLI_PY = '''"""命令行入口。"""

import sys

from calc.core import add, sub, mul

OPS = {"add": add, "sub": sub, "mul": mul}


def main(argv):
    op = argv[1]
    a, b = float(argv[2]), float(argv[3])
    print(OPS[op](a, b))


if __name__ == "__main__":
    main(sys.argv)
'''

_TEST_PY = '''from calc.core import add, sub, mul


def test_add():
    assert add(1, 2) == 3


def test_sub():
    assert sub(5, 2) == 3


def test_mul():
    assert mul(3, 4) == 12
'''

_README = """# calc

一个命令行计算器。目前支持 add / sub / mul 三种运算。

## 待办

- [ ] 支持除法（div），除数为 0 时要给出清晰的错误
- [ ] CLI 支持 `--help`
"""


def _project_files() -> dict:
    """返回小项目的「相对路径 → 内容」映射（几处调用点共用，避免各写一份漂移）。"""
    return {
        "calc/__init__.py": "",
        "calc/core.py": _CORE_PY,
        "calc/cli.py": _CLI_PY,
        "tests/test_core.py": _TEST_PY,
        "README.md": _README,
    }


def _seed_project_repo(workspace: Path) -> None:
    """写下小项目并做成真实 git 仓库（一条初始提交）。"""
    seeding.seed_files(workspace, _project_files())
    seeding.seed_git_repo(
        workspace,
        [{"message": "feat: 计算器骨架", "files": _project_files()}],
    )


# ---------------------------------------------------------------------------
# 角色正文
# ---------------------------------------------------------------------------
# ⚠ 子 Agent 的系统提示**只有角色正文**，RHINE.md 里的项目约定（比如「用中文回答」）
#   到不了它（CLAUDE.md 的成对维护点有记）。所以要它做的每件事都得写在这里。
_WORKER_BODY = """你是代码实现者。按任务要求修改代码。

工作流程：
1. 先读相关文件，了解现状。
2. 做出修改。
3. **改完必须用 run_command 执行 `git add -A` 和 `git commit -m "<说明>"` 把改动提交**
   ——你在一个独立的工作目录里，不提交的话成果无法交回给主 Agent。
4. 最后一条回复的全文会被原样带回主对话，请在其中说清你改了什么、怎么验证的。
"""

_SURVEYOR_BODY = """你是只读调研员。只读地把问题查清楚，不修改任何文件、不执行任何命令。

最后一条回复的全文会被原样带回主对话，请给出自包含的结论：
写清每个结论对应的文件路径与具体位置。
"""

# 场景 6 用：**故意**让模型在正文里写一个与实际不符的分支名。
# 这不是钓鱼，是复现真实偏差——模型确实会凭印象自述分支名，而它并不知道
# 系统给它取了什么名字（撞名时还会改名）。
_REPORTER_BODY = """你是代码实现者。按任务要求修改代码，改完用 run_command 执行
`git add -A` 与 `git commit -m "<说明>"` 提交。

汇报格式要求（必须遵守）：最后一条回复的末尾单独写一行
`成果已提交到 agent/my-feature 分支，请合并该分支。`
"""


def _seed_roles(workspace: Path) -> None:
    """三个隔离角色。三者的差别刻意只在「工具集 + 正文」，隔离声明完全相同。"""
    seeding.seed_project_agent(
        workspace,
        "worker",
        {
            "description": (
                "需要实际修改代码、新增文件、跑命令时用它。"
                "它在一个独立的 Git 工作目录中运行，改动经分支交付，"
                "不会与主对话手上未提交的改动互相覆盖。"
            ),
            "tools": "read_file, write_file, edit_file, glob_files, grep_content, run_command",
            "isolation": "worktree",
            "max_turns": 12,
        },
        _WORKER_BODY,
    )
    seeding.seed_project_agent(
        workspace,
        "surveyor",
        {
            "description": (
                "需要通读多个文件才能回答的调研、定位、梳理类问题用它。"
                "只读，不会修改任何东西。"
            ),
            "tools": "read_file, glob_files, grep_content",
            "isolation": "worktree",
            "max_turns": 10,
        },
        _SURVEYOR_BODY,
    )
    seeding.seed_project_agent(
        workspace,
        "reporter",
        {
            "description": "需要修改代码并汇报成果时用它。在独立工作目录中运行。",
            "tools": "read_file, write_file, edit_file, glob_files, grep_content, run_command",
            "isolation": "worktree",
            "max_turns": 12,
        },
        _REPORTER_BODY,
    )


def _seed_allow(workspace: Path) -> None:
    """
    预置 allow 规则，让子 Agent 在非交互环境下真的能写、能提交。

    ⚠ 这不是「把安全性关掉」：①黑名单与②路径沙箱翻不过去，
    隔离子 Agent 越界写主项目根仍然会被②层拒。allow 只影响③层。
    """
    seeding.seed_permissions(
        Path(workspace) / ".rhinecode",
        allow=["Write(*)", "Edit(*)", "Read(*)", "Bash(git *)", "Bash(python *)"],
    )


# ---------------------------------------------------------------------------
# 场景 1 / 2 / 4 / 6 / 7：主场（一个 git 仓库跑完五条）
# ---------------------------------------------------------------------------
def seed_isolation(workspace: Path, user_dir: Path) -> None:
    """
    C14 主场预置：真实 git 仓库 + 三个隔离角色 + allow 规则 + 项目指令。

    支撑 checklist 的场景 1（并行不覆盖）、2（只读不留垃圾）、4（单向加严）、
    6（交付信息不被污染）、7（主 Agent 搜索不被污染）——它们共用同一套场面，
    放在一个宿主会话里连着跑，省掉四次重启与重复的模型开销。

    副作用：写文件、跑 git（本机需装 git）。
    """
    _seed_project_repo(workspace)
    _seed_roles(workspace)
    _seed_allow(workspace)
    seeding.seed_rhine_md(
        workspace,
        "# calc\n\n用中文回答。这是一个 Python 小项目，改动前先看清现状。\n",
    )


# ---------------------------------------------------------------------------
# 场景 3：非 Git 环境
# ---------------------------------------------------------------------------
def seed_no_git(workspace: Path, user_dir: Path) -> None:
    """
    场景 3 预置：**刻意不建 git 仓库**，同时给一个隔离角色与一个非隔离角色。

    验两件事：隔离委派失败的原因是否具体、模型会不会退而求其次改派非隔离角色。
    非隔离角色的存在是关键对照——没有它，「失败」与「这个环境什么都干不了」
    在观察上分不开。
    """
    seeding.seed_files(workspace, _project_files())
    _seed_roles(workspace)
    seeding.seed_project_agent(
        workspace,
        "plain",
        {
            "description": (
                "需要实际修改代码但不要求工作目录隔离时用它。"
                "它直接在当前项目目录中工作。"
            ),
            "tools": "read_file, write_file, edit_file, glob_files, grep_content",
            "max_turns": 10,
        },
        "你是代码实现者。按任务要求修改代码。最后一条回复的全文会被带回主对话。",
    )
    _seed_allow(workspace)
    seeding.seed_rhine_md(workspace, "# calc\n\n用中文回答。\n")


# ---------------------------------------------------------------------------
# 场景 8：环境初始化（copy / link）
# ---------------------------------------------------------------------------
def seed_provision(workspace: Path, user_dir: Path) -> None:
    """
    场景 8 预置：一个被 .gitignore 排除的本地配置 + 一个大目录。

    两者都**不在版本库里**，所以隔离工作区里天生没有它们（README 的实测结论 2）
    ——这正是环境初始化存在的全部理由。清单本身写在 `--config` 指向的配置文件里
    （`worktree.copy` / `worktree.link`），由驱动方生成。

    任务设计成「必须用上 local.yaml 才能回答」，这样「文件有没有真的带过去」
    可以从模型的结论里直接读出来，不必只看磁盘。
    """
    _seed_project_repo(workspace)
    _seed_roles(workspace)
    _seed_allow(workspace)

    # .gitignore 让这两样东西留在版本库之外 —— 场景成立的前提
    seeding.seed_files(
        workspace,
        {
            ".gitignore": "local.yaml\nvendor/\n",
            "local.yaml": (
                "# 本地环境配置（不进版本库）\n"
                "service_name: calc-local\n"
                "max_workers: 7\n"
                "feature_flag: EXPERIMENTAL_DIV\n"
            ),
            "vendor/bigdep/__init__.py": "VERSION = '3.1.4'\n",
            "vendor/bigdep/data.txt": "x" * 2048,
        },
    )
    # .gitignore 本身要进版本库，否则工作区里没有它、行为与主项目根不一致
    subprocess.run(["git", "add", ".gitignore"], cwd=str(workspace), check=True,
                   capture_output=True)
    subprocess.run(["git", "commit", "-m", "chore: 忽略本地配置与 vendor"],
                   cwd=str(workspace), check=True, capture_output=True)
    seeding.seed_rhine_md(workspace, "# calc\n\n用中文回答。\n")


# ---------------------------------------------------------------------------
# 场景 9：Hook 在隔离下的工作目录
# ---------------------------------------------------------------------------
_HOOK_PROBE = """import json
import os
import sys

# Hook 的上下文只经**标准输入的 JSON** 抵达（c12 F：配置里不做任何字符串插值）。
# 这里把「当前工作目录」与「触发它的作用域」一并追加到一个探针文件里，
# 由驱动方事后读取比对：主对话触发时应是主项目根，隔离子 Agent 触发时应是工作区。
raw = sys.stdin.read()
try:
    payload = json.loads(raw) if raw.strip() else {}
except Exception:
    payload = {}
# 两个都记：`process_cwd` 是命令子进程实际的工作目录，`payload_cwd` 是负载里
# 那个 `cwd` 公共字段。两者应当一致；不一致本身就是一条值得记下的发现。
line = json.dumps(
    {
        "process_cwd": os.getcwd(),
        "payload_cwd": payload.get("cwd", ""),
        "scope": payload.get("scope", ""),
        "tool": payload.get("tool", ""),
    },
    ensure_ascii=False,
)
with open(os.environ["RHINE_PROBE_FILE"], "a", encoding="utf-8") as fh:
    fh.write(line + "\\n")
"""


def seed_hook_cwd(workspace: Path, user_dir: Path) -> None:
    """
    场景 9 预置：一条记录当前工作目录的 `pre_tool_use` Hook + 一条拦截规则。

    探针文件路径经环境变量传给脚本（**不经配置插值**——那是 c12 明令禁止的形态）。
    环境变量在这里设置即可：宿主是当前进程的子进程，会继承它。

    副作用：设置 `RHINE_PROBE_FILE` 环境变量、写 hooks.yaml 与探针脚本。
    """
    _seed_project_repo(workspace)
    _seed_roles(workspace)
    _seed_allow(workspace)
    seeding.seed_rhine_md(workspace, "# calc\n\n用中文回答。\n")

    probe_path = workspace / "hook_probe.jsonl"
    probe_path.write_text("", encoding="utf-8")
    os.environ["RHINE_PROBE_FILE"] = str(probe_path)

    script = workspace / "hook_cwd.py"
    script.write_text(_HOOK_PROBE, encoding="utf-8")
    probe_cmd = f'"{sys.executable}" "{script}"'

    block = workspace / "hook_block.py"
    block.write_text(
        "import sys\n"
        "sys.stderr.write('本仓库禁止 git push，任何 Agent 都不例外')\n"
        "sys.exit(2)\n",
        encoding="utf-8",
    )
    block_cmd = f'"{sys.executable}" "{block}"'

    hooks = workspace / ".rhinecode" / "hooks.yaml"
    hooks.parent.mkdir(parents=True, exist_ok=True)
    hooks.write_text(
        "hooks:\n"
        "  - name: 记录工具执行时的工作目录\n"
        "    event: pre_tool_use\n"
        "    if:\n"
        "      all:\n"
        "        - tool: write_file\n"
        "    action:\n"
        "      type: command\n"
        f"      command: '{probe_cmd}'\n"
        "  - name: 禁止 git push\n"
        "    event: pre_tool_use\n"
        "    if:\n"
        "      all:\n"
        "        - tool: run_command\n"
        "        - command: \"git push *\"\n"
        "    action:\n"
        "      type: command\n"
        f"      command: '{block_cmd}'\n",
        encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# 真实工作场景（checklist 之外）：模拟「一个人真的在用它干活」
# ---------------------------------------------------------------------------
_TASKS_PY = '''"""待办清单：内存实现。"""


class TaskStore:
    """一个极简的待办存储。"""

    def __init__(self):
        self._items = {}
        self._next_id = 1

    def add(self, title, priority=1):
        """新增一条待办，返回它的 id。"""
        item_id = self._next_id
        self._next_id += 1
        self._items[item_id] = {"title": title, "priority": priority, "done": False}
        return item_id

    def complete(self, item_id):
        """标记完成。id 不存在时抛 KeyError。"""
        self._items[item_id]["done"] = True

    def pending(self):
        """返回未完成的条目，按 priority 从大到小。"""
        items = [(i, v) for i, v in self._items.items() if not v["done"]]
        items.sort(key=lambda pair: pair[1]["priority"], reverse=True)
        return items
'''

_STORAGE_PY = '''"""持久化：把 TaskStore 存成 JSON。"""

import json


def save(store, path):
    """把 store 的内容写成 JSON 文件。"""
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(store._items, fh, ensure_ascii=False)


def load(store, path):
    """从 JSON 文件恢复内容。文件不存在时什么都不做。"""
    try:
        with open(path, encoding="utf-8") as fh:
            store._items = {int(k): v for k, v in json.load(fh).items()}
    except FileNotFoundError:
        pass
'''

_TEST_TASKS_PY = '''import unittest

from todo.tasks import TaskStore


class TaskStoreTest(unittest.TestCase):
    def test_add_returns_increasing_ids(self):
        store = TaskStore()
        self.assertEqual(store.add("a"), 1)
        self.assertEqual(store.add("b"), 2)

    def test_pending_sorted_by_priority(self):
        store = TaskStore()
        store.add("low", priority=1)
        store.add("high", priority=9)
        self.assertEqual(store.pending()[0][1]["title"], "high")

    def test_complete_removes_from_pending(self):
        store = TaskStore()
        item = store.add("a")
        store.complete(item)
        self.assertEqual(store.pending(), [])
'''

_REAL_README = """# todo

一个极简待办清单库。

## 模块

- `todo/tasks.py` —— 内存存储 `TaskStore`
- `todo/storage.py` —— JSON 持久化

## 跑测试

    python -m unittest discover -s tests
"""


def _realistic_files() -> dict:
    """一个「像真项目」的骨架：有包、有测试、有 .gitignore。"""
    return {
        "todo/__init__.py": "",
        "todo/tasks.py": _TASKS_PY,
        "todo/storage.py": _STORAGE_PY,
        "tests/__init__.py": "",
        "tests/test_tasks.py": _TEST_TASKS_PY,
        "README.md": _REAL_README,
        # ⚠ .gitignore 必须**进版本库**，否则隔离工作区里没有它 —— 实测过：
        # 子 Agent 的 `git add -A` 会把 __pycache__ 一并提交，然后花好几轮
        # 去清理，最后耗尽轮次预算。真实项目都有这个文件，缺了它构造出来的
        # 是一个比现实更糟的环境，抓到的问题也就不算数。
        ".gitignore": "__pycache__/\n*.py[cod]\n.rhinecode/\n",
    }


def seed_realistic(workspace: Path, user_dir: Path) -> None:
    """
    真实工作场景的预置：一个有包结构、有测试、有 .gitignore 的小项目。

    与 `seed_isolation` 的差别是**刻意的**：那一套服务于 checklist 的九个场景
    （每个只验一条判据），这一套服务于「一个人真的在用它干活」——所以要有
    能跑的测试套件、多个互相 import 的模块、以及一份真实的 .gitignore。

    角色只给一个 `dev`：真实使用里用户不会为每件事各写一个角色。
    轮次上限调到 20（实测 12 轮在「读几个文件 + 改 + 跑测试 + 提交」这条
    完整链路上是不够的，会在提交前耗尽）。

    副作用：写文件、跑 git（本机需装 git）。
    """
    seeding.seed_files(workspace, _realistic_files())
    seeding.seed_git_repo(
        workspace,
        [{"message": "feat: 待办清单骨架", "files": _realistic_files()}],
    )
    seeding.seed_project_agent(
        workspace,
        "dev",
        {
            "description": (
                "需要实际改代码、加功能、修 bug、补测试时用它。"
                "它在一个独立的 Git 工作目录中运行，成果经分支交付，"
                "不会与主对话手上未提交的改动互相覆盖。"
            ),
            "tools": "read_file, write_file, edit_file, glob_files, grep_content, run_command",
            "isolation": "worktree",
            "max_turns": 20,
        },
        """你是这个项目的开发者。按任务要求改代码。

工作流程：
1. 先读相关文件了解现状，别凭空猜。
2. 做出修改。
3. 跑 `python -m unittest discover -s tests` 确认没弄坏别的。
4. **改完必须 `git add -A` 且 `git commit -m "<说明>"`**——你在一个独立的
   工作目录里，不提交的话成果无法交回给主 Agent。
5. 最后一条回复的全文会被原样带回主对话，写清你改了什么、测试结果如何。
""",
    )
    _seed_allow(workspace)
    seeding.seed_rhine_md(
        workspace,
        "# todo\n\n用中文回答。改动前先跑一遍测试，改完再跑一遍。\n",
    )


def seed_conflict(workspace: Path, user_dir: Path) -> None:
    """
    真实场景「合并冲突」的预置：与 `seed_realistic` 相同，只是多一条提交历史。

    冲突本身由驱动方制造（让主 Agent 与子 Agent 改同一个函数）——
    这里只保证仓库里有足够真实的历史，让 `git merge` 的输出像模像样。

    ⚠ 合并冲突是隔离交付的**必经之路**，而 C14 的 spec / checklist 一条都没覆盖：
    子 Agent 的基点是委派时的 HEAD，主 Agent 在它跑的这段时间里完全可能
    提交新东西——这在真实使用里是常态，不是边角情况。
    """
    seed_realistic(workspace, user_dir)
    subprocess.run(
        ["git", "commit", "--allow-empty", "-m", "chore: 起个头"],
        cwd=str(workspace), check=True, capture_output=True,
    )


# ---------------------------------------------------------------------------
# 场景 5：启动清理的三种结局
# ---------------------------------------------------------------------------
def seed_stale_worktrees(workspace: Path, user_dir: Path) -> None:
    """
    场景 5 预置：造三个**真实的** git worktree，并把 mtime 拨回 30 天前。

    三者对应三层过滤的三种结局：

    | 名字 | 状态 | 期望结局 |
    | --- | --- | --- |
    | `clean` | 无提交、无未提交改动 | 目录与分支一并删 |
    | `committed` | 有一条提交、无未提交改动 | 删目录、**留分支**（成果可取回） |
    | `wip` | 有未提交改动 | **原样保留**（未提交改动是无条件否决） |

    ⚠ 拨 mtime 必须**递归**到每个文件：清理用的是「目录内最新的 mtime」
    （`cleanup._latest_mtime`），只拨目录自身的话仍会被判为「还在用」。

    副作用：跑 git 建三个 worktree、改文件 mtime。
    """
    _seed_project_repo(workspace)
    _seed_roles(workspace)
    _seed_allow(workspace)

    base = Path(workspace) / ".rhinecode" / "worktrees"
    base.mkdir(parents=True, exist_ok=True)

    def git(*args: str, cwd: Path) -> None:
        subprocess.run(["git", *args], cwd=str(cwd), check=True, capture_output=True)

    for name in ("clean", "committed", "wip"):
        git("worktree", "add", "-b", f"agent/{name}", str(base / name), "HEAD",
            cwd=Path(workspace))

    # committed：造一条真提交，删目录后成果应仍能从分支取回
    (base / "committed" / "result.py").write_text("ANSWER = 42\n", encoding="utf-8")
    git("add", "-A", cwd=base / "committed")
    git("commit", "-m", "feat: 子 Agent 的成果", cwd=base / "committed")

    # wip：只改不提交 —— 这份内容**只存在于这个目录里**，删掉就没了
    (base / "wip" / "half_done.py").write_text("# 写了一半\n", encoding="utf-8")

    old = time.time() - 30 * 86400
    for name in ("clean", "committed", "wip"):
        for root, _dirs, files in os.walk(base / name):
            for fname in files:
                try:
                    os.utime(Path(root) / fname, (old, old))
                except OSError:
                    pass
        os.utime(base / name, (old, old))

    seeding.seed_rhine_md(workspace, "# calc\n\n用中文回答。\n")
