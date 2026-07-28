"""
用 P1a 驱动设施验收 **P0（Trace 记录器）** 端到端场景的剧本与预置。

对应 `docs/c11/testing/p0-trace/checklist.md` 第九节那 9 条手测场景。它们当初留作手测，
是因为**没有能驱动界面的东西**；P1a 交付后其中大部分已经可以无人驱动。

放在独立模块而不是塞进 `scripts.py`，是因为这些 fixture 的服务对象是**另一份
checklist**——混在一起会让「这个剧本是给谁用的」变得说不清。P1b 做无人值守
场景时可以直接复用本模块。

⚠️ 预置 Skill 的 `allowed_tools` 不得写 `mcp_add_server` / `mcp_resolve_server`
（宿主会摘掉它们，见 `host.EXCLUDED_TOOLS`）。
"""

from __future__ import annotations

from pathlib import Path

from tests.e2e import seeding
from tests.e2e.scripted import done, text, thinking, tool, usage


# ---------------------------------------------------------------------------
# 场景 2：复现立项理由——白名单外调用
# ---------------------------------------------------------------------------
# 原始 bug 是「模型调用了本轮没发给它的工具」，属**偶发幻觉、不可控**，
# 所以 P0 手测时只能听天由命（checklist 原话：「若本次未触发……如实记录未自然复现」）。
#
# 但用脚本化假模型可以**确定性地复现**它：第 2 轮故意去调一个白名单已经排除掉的
# `write_file`。这正是假模型相对真实模型的价值——**能精确造出真实模型只会偶发的行为**。
WHITELIST_OVERSTEP = [
    # 第 1 轮：模型自己加载 Skill（工具集收窄发生在这之后）
    [text("我先加载这个 skill。"), tool("load_skill", {"name": "readonly"}), done()],
    # 第 2 轮：白名单只剩 read_file，这里**故意越界**去写文件
    [text("我来改一下文件。"), tool("write_file", {"path": "x.txt", "content": "越界"}), done()],
    [text("看来我改不了。"), done()],
]


def seed_readonly_skill(workspace: Path, user_dir: Path) -> None:
    """一个**纯只读**白名单的共享模式 Skill（P0 场景 2 的前置准备）。"""
    seeding.seed_files(workspace, {"note.txt": "一行内容\n"})
    seeding.seed_project_skill(
        workspace,
        "readonly",
        {"description": "只读分析，不改任何文件",
                  # ⚠️ 只给 read_file 会让模型没法发现目录里有什么（CLAUDE.md 记着的实测教训），
         # 但本场景要的正是「白名单足够窄」，故刻意只留它。
         "allowed-tools": ["read_file"]},
        "只读地分析，不要修改任何文件。\n\n$ARGUMENTS",
    )


# ---------------------------------------------------------------------------
# 场景 3：作用域交错可读
# ---------------------------------------------------------------------------
# 独立模式子对话的事件必须落在 `isolated:<skill>` 作用域，与主对话互不混淆。
# 脚本里的每一轮都是**子对话**的轮次——独立模式下主对话不再调模型，
# 只是把结论作为两条配对消息追加进主历史。
ISOLATED_SCOPE = [
    [text("我先读一下。"), tool("read_file", {"path": "note.txt"}), done()],
    [text("再读一个。"), tool("read_file", {"path": "src/app.py"}), done()],
    [text("结论：代码结构清晰，无需改动。"), done()],
]


def seed_isolated_skill(workspace: Path, user_dir: Path) -> None:
    """一个独立模式 Skill（P0 场景 3）。"""
    seeding.seed_files(
        workspace,
        {"note.txt": "一行内容\n", "src/app.py": "def main():\n    print('hi')\n"},
    )
    seeding.seed_project_skill(
        workspace,
        "solo",
        {"description": "独立跑一遍分析", "context": "fork",
         "allowed-tools": ["read_file", "glob_files"]},
        "在子对话里完成分析并给出结论。\n\n$ARGUMENTS",
    )


# ---------------------------------------------------------------------------
# 场景 6：人在回路与拒绝路径
# ---------------------------------------------------------------------------
# 上半场：写文件 → 弹确认面板 → 选拒绝 → 三条事件串成完整因果。
REJECT_PATH = [
    [text("我来写个文件。"), tool("write_file", {"path": "x.txt", "content": "hi"}), done()],
    [text("你拒绝了，那我不写了。"), done()],
]

# 下半场：危险命令 → 第①层黑名单直接拒绝，**面板根本不弹**。
BLACKLIST_DIRECT = [
    [text("我来清理一下。"), tool("run_command", {"command": "rm -rf /"}), done()],
    [text("被拦下了。"), done()],
]


# ---------------------------------------------------------------------------
# 场景 7：压缩动作可解释
# ---------------------------------------------------------------------------
# 两层都要触发，且都要**可控**（checklist 明确「不要靠聊很久等自动触发」）：
#
# - **第一层存盘**：靠读一个大文件，让工具结果超过单个 4K token 的阈值。
# - **第二层摘要**：靠 `usage` 块把估算锚点直接顶高——C8 的估算是
#   「锚点（上次 API 的 prompt_tokens，精确）+ 增量」，所以脚本报一个大数字，
#   下一轮请求前的估算就会逼近窗口上限而触发摘要。配 `context_window: 8192` 使用。
COMPACTION = [
    [
        text("我读一下那个大文件。"),
        tool("read_file", {"path": "big.txt"}),
        # 报一个逼近 8192 窗口的用量，把锚点顶高
        usage(prompt=7000, completion=50),
        done(),
    ],
    [text("再读一次。"), tool("read_file", {"path": "big.txt"}), usage(prompt=7500, completion=50), done()],
    [text("读完了。"), done()],
]


def seed_big_file(workspace: Path, user_dir: Path) -> None:
    """
    造一个足够大的文件（P0 场景 7）。

    单个工具结果超过 4K token 才会触发第一层存盘；按 `CHARS_PER_TOKEN=3.0` 估算，
    需要约 12000 字符以上，这里给到约 6 万字符留足余量。
    """
    body = "".join(f"{i:05d} 这是第 {i} 行内容，用来把文件撑大到足以触发第一层存盘。\n"
                   for i in range(1500))
    seeding.seed_files(workspace, {"big.txt": body})


# ---------------------------------------------------------------------------
# 场景 9：敏感产物确认
# ---------------------------------------------------------------------------
# 两件事要同时成立：
#   ① 会话启动事件里的 `api_key` 是**掩码**（`redact_config` 的职责）；
#   ② 工具输出里**可能出现明文密钥**（已登记的边界，不是缺陷）。
# 后者要能演示出来，就得让模型去读一个含密钥的文件。
SENSITIVE = [
    [text("我看看配置。"), tool("read_file", {"path": "config.yaml"}), done()],
    [text("读到了。"), done()],
]

# 预置进工作区的**假**密钥。故意取一个一眼能认出、又绝不可能是真key的值。
FAKE_KEY = "sk-FAKE0000000000000000000000000000"


def seed_config_with_key(workspace: Path, user_dir: Path) -> None:
    """在工作区里放一份含（假）密钥的 config.yaml（P0 场景 9）。"""
    seeding.seed_files(
        workspace,
        {"config.yaml": (
            "protocol: deepseek\n"
            "model: deepseek-chat\n"
            "base_url: https://api.deepseek.com\n"
            f"api_key: {FAKE_KEY}\n"
        )},
    )


# ---------------------------------------------------------------------------
# 场景 1：基本可用（确定性版本）
# ---------------------------------------------------------------------------
# 场景 1 的判据是「时间线能看出脉络」+「能回答原本答不出的问题：
# 模型这一轮实际收到了哪些工具」。后者取自 `api_request` 的 `tool_names`。
BASIC_FLOW = [
    [
        thinking("先看看项目里有什么。"),
        text("我先列一下文件。"),
        tool("glob_files", {"pattern": "**/*.py"}),
        done(),
    ],
    [text("再读其中一个。"), tool("read_file", {"path": "src/app.py"}), done()],
    [text("这个项目的测试放在 tests/ 下，按模块分文件。"), done()],
]


def seed_small_project(workspace: Path, user_dir: Path) -> None:
    """一个像样的小项目（P0 场景 1）。"""
    seeding.seed_files(
        workspace,
        {
            "src/app.py": "def main():\n    print('hi')\n",
            "src/util.py": "def helper():\n    return 1\n",
            "tests/test_app.py": "def test_main():\n    assert True\n",
            "note.txt": "一行内容\n",
        },
    )
    seeding.seed_rhine_md(workspace, "# 演示项目\n\n用中文回答。\n")


# ---------------------------------------------------------------------------
# 场景 7 补充：让第二层**真的压缩一次**
# ---------------------------------------------------------------------------
# P0 checklist 给的「可控做法」是 `context_window: 8192`，但那个配方**只能走到
# 第二层、永远压不动**——`summarize.RETAIN_TOKENS = 10000` 是固定常量、不随窗口缩放，
# 窗口 8192 时保留区比整个窗口还大，早段恒为空（实测三次全是 `no_early_segment`）。
#
# 要真正压一次，需要同时满足三件事：
#   ① 窗口正常（用默认 64K），且估算逼近 `窗口 − 13K 余量` → 靠 `usage` 把锚点顶到 6 万；
#   ② 历史里有**多轮 user 消息** —— 保留边界要回退到「最近一个 user」，
#      只有一条 user 消息时必然回退到 0，早段还是空的（这点最容易被忽略）；
#   ③ 历史体量够大且**不被第一层吃掉** —— 第一层只动 `role="tool"` 的消息，
#      所以把体量放在 assistant 正文里。
#
# 每一轮的正文都带 `<<<正式摘要>>>` 标记，是为了让**摘要调用本身**也能被同一个剧本
# 应答（摘要请求和主对话请求共用同一个假模型，调用次序不易预测，与其精确排期
# 不如让每条都可解析）。
_FILLER = "这是用来把历史撑大的填充内容，重复若干次以超过保留区阈值。" * 700
_SUMMARY_BODY = (
    "\n<<<正式摘要>>>\n"
    "① 任务目标：验证第二层摘要能真正压缩历史。\n"
    "② 已完成的关键步骤与结论：多轮对话累积了足够体量的历史。\n"
    "③ 涉及的关键文件与改动：无。\n"
    "④ 当前状态与待办：等待压缩生效。\n"
    "⑤ 其它：本条由脚本化假模型产出。"
)
REAL_SUMMARY = [[text(_FILLER + _SUMMARY_BODY), usage(prompt=60000, completion=100), done()]
                for _ in range(12)]
