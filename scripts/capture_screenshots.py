"""
把 RhineCode 的真实界面导出成 SVG 截图，供 README 与文档使用。

    python scripts/capture_screenshots.py            # 全部三张
    python scripts/capture_screenshots.py chat       # 只出其中一张

## 这是什么、为什么这么做

README 需要「一眼看得出它真的能跑」的画面。动图（GIF）只能由人在真实终端里录
（Windows 下用 ScreenToGif），而**静态画面可以由程序自己出**——Textual 自带
`App.export_screenshot()`，它把当前屏幕的每一个字符与颜色渲染成一份 SVG。
SVG 是矢量的、能被 GitHub 直接渲染、体积只有几十 KB，而且**里面的文字是真文字**，
不是截图里的像素。

关键的一点是：这些画面**不是画出来的**。脚本走的是产品真实的装配入口
`bootstrap.build_app`，真实的 TUI、真实的 Agent Loop、真实的五层权限管线；
唯一被替换掉的是**模型本身**——换成 `tests/e2e/scripted.py` 里的剧本模型，
让它按写死的台词逐块吐字。所以画面里的工具行、确认面板、活动区，
全都是产品自己渲染的结果。

## 为什么不直接复用 `tests/e2e/host.py`

宿主是**常驻**的：它起一个服务端口、等客户端一条条发指令。截图只需要
「起来 → 发一句话 → 等静默 → 导一张图 → 退出」这一条直线，
用宿主要多绕一整套进程间协议。而 `tests/e2e/` 那几个文件带着四条加锁不变量，
为截图去动它们是不划算的风险。因此这里**只复用它的剧本模型与预置函数**
（纯数据 + 纯函数，没有状态），编排自己写。

## 副作用

- 在系统临时目录下建一个临时工作区与临时用户目录，**跑完即删**。
- 在 `docs/assets/` 下写入 `*.svg`。
- 不发任何网络请求（模型是假的，`api_key` 也是假值）。
"""

from __future__ import annotations

import argparse
import asyncio
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path
from typing import Callable

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from rhinecode.bootstrap import build_app  # noqa: E402
from rhinecode.config import Config  # noqa: E402
from rhinecode.trace.recorder import create_recorder  # noqa: E402
from rhinecode.tui.widgets import InputBar  # noqa: E402
from tests.e2e import scripts as e2e_scripts  # noqa: E402
from tests.e2e.scripted import (  # noqa: E402
    ScopedScriptedProvider,
    ScriptedProvider,
    done,
    text,
    thinking,
    tool,
)


class SlowScopedProvider(ScopedScriptedProvider):
    """
    每个数据块之间插一个固定停顿的剧本模型。

    只给「子 Agent 并行」那张图用。理由见本文件顶部——剧本模型瞬间吐完全部台词，
    两个队员并行的窗口短到界面来不及画出来，而活动区是**主线程轮询**刷新的
    （那是一条致命不变量，不能改成推送），所以窗口必须比一个轮询周期长。

    停顿加在**产出侧**，不碰父类的游标与加锁逻辑：`stream_chat` 仍由父类实现，
    这里只是把它返回的迭代器包一层。改父类的临界区是本项目明令禁止的那类改动。
    """

    #: 每块之间的停顿（秒）。0.4 是「窗口够界面轮询到并画全两个人」与「整张图别跑太久」的折中。
    CHUNK_DELAY = 0.4

    def stream_chat(self, *args, **kwargs):
        for chunk in super().stream_chat(*args, **kwargs):
            time.sleep(self.CHUNK_DELAY)
            yield chunk

# 终端尺寸。80 列在 GitHub 的正文宽度里显得太挤，110×30 是「一屏能看完一次
# 完整往返」与「字不至于小到看不清」之间的折中；三张图统一用它，
# 放在一起时高度一致、不会一张高一张矮。
TERMINAL_SIZE = (110, 30)

OUT_DIR = REPO_ROOT / "docs" / "assets"

# 宿主摘掉的两个工具，这里同样摘掉：`mcp_add_server` 会写**真实**用户主目录
# （它不吃 user_dir），`mcp_resolve_server` 要访问外部包索引。截图跑在临时目录里，
# 让这两个工具留着等于把隔离撕开一个口子。
EXCLUDED_TOOLS = ("mcp_add_server", "mcp_resolve_server")


# ---------------------------------------------------------------------------
# 三个场景：每个 = 一份剧本 + 一个预置函数 + 一句用户输入
# ---------------------------------------------------------------------------

# ① 一次完整往返：思考 → 正文 → 只读工具 → 结论。
#    这是最常见的画面，放 README 第一张。
CHAT_SCRIPT = [
    [
        thinking("先确认一下项目里有什么，再决定从哪读起。"),
        text("我先看一下 `seed.txt` 的内容。"),
        tool("read_file", {"path": "seed.txt"}),
        done(),
    ],
    [
        text(
            "读到了。`seed.txt` 里是一行中文文本，用来验证从终端输入到文件读取"
            "这条链路上的编码没有问题。\n\n"
            "接下来我可以：\n"
            "1. 按内容继续改这个文件；\n"
            "2. 或者先看看项目里还有哪些文件。\n\n"
            "你想从哪一步开始？"
        ),
        done(),
    ],
]

# ② 人在回路的确认面板。
#
#    ⚠ 这里刻意**不用普通的写文件**来触发面板：缺省预设是 `auto`（放行档），
#    工作区内的写入根本不弹面板。真正在缺省档下仍然弹的是**②″保护路径**
#    ——`.rhinecode/` 下的配置决定「以后会发生什么」，写它必须过人眼。
#    用它当样例，画面上展示的就是产品**当前真实的**缺省行为，
#    而不是一个要先改配置才复现得出来的假象。
CONFIRM_SCRIPT = [
    [
        text("我给项目加一条 Hook 规则，让每次写文件之后自动跑一遍格式化。"),
        tool(
            "write_file",
            {
                "path": ".rhinecode/hooks.yaml",
                "content": "hooks:\n  - event: post_tool_use\n    action:\n      type: command\n      command: python -m black .\n",
            },
        ),
        done(),
    ],
    [text("好的，我不改这份配置了。"), done()],
]

# ③ 子 Agent 并行活动区：两个队员同时在跑，主对话在等结论。
#    直接复用 C15 端到端场景里的那份剧本与预置——它已经被测试跑过无数次，
#    行为是确定的，不必为截图重写一份可能对不上的。
TEAM_SCRIPT = e2e_scripts.TEAM_PARALLEL


def _seed_chat(workspace: Path, user_dir: Path) -> None:
    """场景①的预置：一个可读的中文文件。"""
    e2e_scripts.seed_basic(workspace, user_dir)


def _seed_confirm(workspace: Path, user_dir: Path) -> None:
    """场景②的预置：只要一个空工作区即可，面板由保护路径层触发。"""
    (workspace / "README.md").write_text("# demo\n", encoding="utf-8")


def _seed_team(workspace: Path, user_dir: Path) -> None:
    """
    场景③的预置：两个 `worker` 角色 + 两个可改的源文件 + allow 规则。

    ⚠ 必须用 `c15_scenarios.seed_team` 而不是 `scripts.seed_team_project`——
    `TEAM_PARALLEL` 那份剧本改的正是这里铺的 `src/app.py` / `src/util.py`，
    派的角色也正是这里写下的 `worker`。用另一份预置的话队员一起来就报
    「找不到角色」，活动区里永远不会有两个人在跑。
    """
    from tests.e2e import c15_scenarios

    c15_scenarios.seed_team(workspace, user_dir)


SCENES: dict[str, dict] = {
    "chat": {
        "script": CHAT_SCRIPT,
        "seed": _seed_chat,
        "input": "读一下 seed.txt，告诉我里面是什么",
        "out": "01-chat-and-tools.svg",
        "desc": "一次完整往返：思考 → 正文 → 工具调用 → 结论",
        # 走到「模型说完话、循环停下来」即可
        "settle": "idle",
    },
    "confirm": {
        "script": CONFIRM_SCRIPT,
        "seed": _seed_confirm,
        "input": "给项目加一条 Hook：每次写完文件自动跑格式化",
        "out": "02-permission-panel.svg",
        "desc": "人在回路：写 .rhinecode/ 下的配置必须过人眼（保护路径层 ②″）",
        # 停在面板挂起的那一刻——那正是要截的画面
        "settle": "pending",
    },
    "team": {
        "script": TEAM_SCRIPT,
        "seed": _seed_team,
        "input": "这两件事互不相干，组个队并行做",
        "out": "03-subagents-parallel.svg",
        "desc": "子 Agent 并行：两个队员同时在跑，活动区实时显示各自进度",
        "settle": "activity",
        # 见 SlowScopedProvider 的 docstring：不放慢的话窗口短到界面画不出两个人
        "slow": True,
        # 活动区缺省是折叠档（只有一行汇总）。这张图要展示的恰恰是
        # 「每个队员各自在干什么」，所以导出前按一次 Ctrl+O 切到展开档。
        "keys": ("ctrl+o",),
    },
}


def make_config() -> Config:
    """
    一份假配置。

    `api_key` 是假值且不会被用到——`provider_factory` 被替换成剧本模型之后，
    真正的 DeepSeek 客户端根本不会发出请求。写成 `fake-key-for-screenshots`
    而不是留空，是因为装配期会校验占位符。
    """
    return Config(
        protocol="deepseek",
        model="deepseek-chat",
        base_url="https://api.deepseek.com",
        api_key="fake-key-for-screenshots",
        debug_log=False,
        context_window=65536,
        # ⚠ 搜索密钥也给一个假值，为的是**不让启动提示挤进画面**。
        #
        # 不给的话，启动时会挂一条「网络搜索已启用但未配置密钥」的提示——那条提示
        # 本身是对的，但一个配好了 key 的真实用户看不到它，让它占掉截图顶上两行
        # 反而**不代表真实使用**。同理没有关掉 `search_enabled`：关掉是另一种
        # 不真实（真实用户多半开着）。
        search_api_key="fake-search-key-for-screenshots",
    )


def _pending_kind(app) -> str | None:
    """
    当前挂着的是哪种面板（没有则 None）。

    读的是 App 的私有 `_pending_interaction`——与 `tests/e2e/control.py` 同一个
    判据。刻意读私有属性而不是另写一份「等价判断」：产品改了状态机时，
    抄一份的那种写法会静默分叉，而这里会直接对不上。
    """
    box = getattr(app, "_pending_interaction", None)
    return box.get("kind") if isinstance(box, dict) else None


def _stream_active(app) -> bool:
    """流式 Worker 是否还在跑。"""
    return bool(getattr(app, "_stream_active", False))


async def _settle_idle(app, pilot) -> None:
    """
    等到 Agent Loop 自然停下来。

    ⚠ 判据必须**先看面板再看忙碌态**：面板挂着的时候 `_stream_active` 仍为真
    （Worker 正阻塞等待结算）。反过来写会把「等你应答」误判成「还在忙」，
    于是永远等不到终态。这里只要 idle，面板出现即视为异常。
    """
    for _ in range(600):
        await pilot.pause(0.05)
        if _pending_kind(app) is not None:
            raise RuntimeError("这个场景不该弹面板，却弹了")
        if not _stream_active(app):
            await pilot.pause(0.4)
            return
    raise TimeoutError("等不到 idle 态")


async def _settle_pending(app, pilot) -> None:
    """等到确认面板挂起——那一帧就是要截的画面。"""
    for _ in range(600):
        await pilot.pause(0.05)
        if _pending_kind(app) == "confirm":
            await pilot.pause(0.4)
            return
    raise TimeoutError("等不到确认面板")


async def _settle_activity(app, pilot) -> None:
    """
    等到活动区里**同时**有两个队员在跑。

    子 Agent 是异步的：第一个队员可能在第二个还没起来时就已经跑完，
    那一帧截下来只有一个人在干活，说明不了「并行」。所以判据是
    `running_subagent_count() >= 2`，而不是简单地等几秒。
    """
    manager = getattr(app, "_manager", None)
    best = 0
    for _ in range(1200):
        await pilot.pause(0.05)
        try:
            running = manager.running_subagent_count()
        except Exception:  # noqa: BLE001
            running = 0
        best = max(best, running)
        if running >= 2:
            # ⚠ 不能数到就导出。活动区是**主线程轮询**刷新的（致命不变量：
            # 数据一律轮询，绝不新增从子 Agent 线程到界面的推送），
            # 所以「计数到 2」与「界面画出来」之间隔着至少一个轮询周期。
            # 等足几个周期，让两个队员都真的出现在活动区里。
            for _ in range(24):
                await pilot.pause(0.05)
            return
    raise TimeoutError(f"等不到两个队员同时在跑（最多同时 {best} 个）")


async def _unwind(app, pilot) -> None:
    """
    收尾：结算掉还挂着的面板，并等流真的停下来。

    有面板就按 `Esc`（产品语义 = 拒绝 / 取消），然后最多等 10 秒到
    `_stream_active` 转假。等不到也不抛——收尾失败不该盖掉已经导出的那张图，
    调用方只会在下一个场景上看到超时，而那条信息更有用。
    """
    if _pending_kind(app) is not None:
        await pilot.press("escape")
    for _ in range(200):
        await pilot.pause(0.05)
        if not _stream_active(app) and _pending_kind(app) is None:
            return


SETTLERS: dict[str, Callable] = {
    "idle": _settle_idle,
    "pending": _settle_pending,
    "activity": _settle_activity,
}


async def capture(name: str, scene: dict) -> Path:
    """
    跑完一个场景并导出一张 SVG。

    :param name: 场景名（仅用于日志）
    :param scene: SCENES 里的一条
    :returns: 写出的 SVG 路径

    执行流程：
    1. 建临时工作区与临时用户目录，跑预置函数把样例文件铺好；
    2. `build_app` 装配整个应用，`provider_factory` 换成剧本模型；
    3. `app.run_test()` 起一个无头终端，按真人路径提交一句输入；
    4. 按场景的 settle 判据等到该截的那一帧；
    5. `export_screenshot()` 导出 SVG，落到 docs/assets/。

    副作用：写 SVG 文件；临时目录在 finally 里删除。
    """
    workspace = Path(tempfile.mkdtemp(prefix="rhine-shot-ws-"))
    user_dir = Path(tempfile.mkdtemp(prefix="rhine-shot-home-"))
    cwd_before = Path.cwd()
    try:
        scene["seed"](workspace, user_dir)

        trace_path = workspace / ".rhinecode" / "traces" / "shot.jsonl"
        trace_path.parent.mkdir(parents=True, exist_ok=True)
        recorder = create_recorder(trace_path)

        script = scene["script"]
        if isinstance(script, dict):
            cls = SlowScopedProvider if scene.get("slow") else ScopedScriptedProvider
            provider = cls(script)
        else:
            provider = ScriptedProvider(script)

        # 路径边界以「当前工作目录」为项目根，所以必须先 chdir 进临时工作区。
        # ⚠ 这是**进程级**状态，所以三个场景只能串行跑（本脚本本来就是串行的）。
        os.chdir(workspace)

        # ⚠ `path_guard` 的只读白名单是**进程级**状态。三个场景在同一个进程里
        # 串行跑，上一个场景注册的白名单会漏到下一个——清一次，让每个场景
        # 从同一个起点开始。
        from rhinecode.tools import path_guard

        path_guard.clear_read_roots()

        result = build_app(
            make_config(),
            user_dir=user_dir,
            recorder=recorder,
            provider_factory=lambda cfg: provider,
            exclude_tools=EXCLUDED_TOOLS,
        )
        # 自动记忆是不确定性来源：它另起一条对话、另调一次模型，
        # 而剧本模型的台词是按轮次写死的，多出来的那一次会让后面全部错位。
        # 既有的集成测试跑这份剧本时也是先关掉它。
        result.manager.memory_manager.memories_enabled = False
        app = result.app

        async with app.run_test(size=TERMINAL_SIZE) as pilot:
            await pilot.pause(0.4)
            bar = app.query_one(InputBar)
            bar.focus()
            bar.value = scene["input"]
            await pilot.press("enter")

            await SETTLERS[scene["settle"]](app, pilot)

            # 场景声明的收尾按键（目前只有活动区的展开档）
            for key in scene.get("keys", ()):
                await pilot.press(key)
                await pilot.pause(0.3)

            svg = app.export_screenshot(title="RhineCode")

            # ⚠ 导完图必须把挂着的面板结算掉，**不能直接退出**。
            #
            # `confirm` 场景刻意停在面板挂起的那一刻，此时 Agent Loop 的
            # Worker 线程正阻塞在「等用户结算」上。直接退出会把那个线程连同
            # 它持有的东西留在进程里，而三个场景跑在**同一个进程**里——
            # 下一个场景的子 Agent 于是排在它后面，表现为「单独跑都对、
            # 连着跑就卡在第三个」。按 `Esc` 等价于「拒绝」，是产品自己的逃生口。
            await _unwind(app, pilot)

        OUT_DIR.mkdir(parents=True, exist_ok=True)
        out = OUT_DIR / scene["out"]
        out.write_text(svg, encoding="utf-8")
        print(f"  [OK] {name}: {out.relative_to(REPO_ROOT)}  ({len(svg) // 1024} KB)")
        return out
    finally:
        try:
            from rhinecode.tools import path_guard

            path_guard.clear_read_roots()
        except Exception:  # noqa: BLE001 —— 清理失败不该盖住真正的异常
            pass
        os.chdir(cwd_before)
        shutil.rmtree(workspace, ignore_errors=True)
        shutil.rmtree(user_dir, ignore_errors=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    # ⚠ 不给 `choices`：argparse 对 `nargs="*"` 会把**空列表本身**拿去和候选项
    # 比对，于是「一个参数都不给」反而报 `invalid choice: []`。校验自己做。
    parser.add_argument(
        "scenes",
        nargs="*",
        help=f"要出的场景（{' / '.join(SCENES)}），缺省全部",
    )
    args = parser.parse_args(argv)
    wanted = args.scenes or list(SCENES)
    unknown = [n for n in wanted if n not in SCENES]
    if unknown:
        parser.error(f"未知场景：{', '.join(unknown)}（可选：{' / '.join(SCENES)}）")

    failed = []
    for name in wanted:
        print(f"[{name}] {SCENES[name]['desc']}")
        try:
            asyncio.run(capture(name, SCENES[name]))
        except Exception as e:  # noqa: BLE001
            print(f"  [FAIL] {name} 失败：{type(e).__name__}: {e}")
            failed.append(name)

    if failed:
        print(f"\n失败 {len(failed)} 个：{', '.join(failed)}")
        return 1
    print("\n全部完成。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
