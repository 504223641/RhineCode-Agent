# -*- coding: utf-8 -*-
"""
分片并行测试 runner（**开发期工具，不进产品包**）。

`python -m unittest discover -s tests` 是纯单线程的：3325 条用例挨个跑，
本机实测 239 秒。这个脚本把测试模块分成 N 组、起 N 个进程同时跑，
配合 provider 延迟导入之后实测降到 **约 33 秒**（8 路，16 核机器）。

用法：

    python -m tests.run_parallel                    # 缺省分片数 = min(8, CPU 核数)
    python -m tests.run_parallel -j 4               # 指定分片数
    python -m tests.run_parallel --timing t.json    # 用实测耗时装箱（见下）
    python -m tests.run_parallel --keep-logs        # 保留各分片日志

⚠ **它不是 `discover` 的替代品，是它的快速通道。** 判据以 `discover` 为准：
本脚本每次都先做一次「只收集不执行」的 discover 取得**期望用例总数**，
跑完再与各分片实际跑的条数比对，对不上就**报错退出**——并行方案最危险的
失败形态是「某个模块被漏掉了却没人发现」，那比慢 4 分钟糟糕得多。

## 两个关键设计点

### ① 每个分片必须有自己的临时目录

e2e 宿主把「名片」发布在系统临时目录下（`tests/e2e/discovery.py` 的
`publish_dir()` 取 `tempfile.gettempdir()`），而 `HostFixture.start_host` 认
自己那个宿主的办法是**「启动前后名片集合的差集」**：

    before = {h.pid for h in discovery.list_hosts()}
    # 起进程…
    for host in discovery.list_hosts():
        if host.pid not in before:   # 新出现的就认作自己的

两个分片同时起宿主，A 就可能把 B 刚起的那个认作自己的——**串台**。

`tempfile.gettempdir()` 读的是 `TEMP` / `TMP` / `TMPDIR` 环境变量，所以给每个
分片派一个独立临时目录，各分片看到的就是各自的名片池，串台从根上消失。
**这一条是整个方案成立的前提，改动分片启动逻辑时别把它丢了。**

### ② 装箱用 LPT，权重表过期不影响正确性

LPT（Longest Processing Time first）：把最耗时的模块先放，每次塞给当前
最闲的那个分片。这是个经典的负载均衡近似算法，实测 8 路下各分片落在
26~32 秒。

权重来自下面的 `_WEIGHTS`（2026-08-20 实测值，只列了重量级的那些，
其余按 `_DEFAULT_WEIGHT` 估）。⚠ **这张表过期了只会让负载不均、总时间变长，
不会漏跑也不会误判**——所以不必跟着每次改动维护它。想按实测重新装箱：
`--timing` 传一份 `{"records": [{"id": ..., "sec": ...}]}` 即可。

## 稳定性

方案定下来之前连跑 5 次（40 个分片）验证：每次都是 3325 条、skip 4、
全部 rc=0，墙钟波动 3%。并行最怕的是偶发 flaky，所以**改动本脚本或
测试夹具之后，建议再连跑几次确认**，别只跑一次就下结论。

## 已知的下界

`test_e2e_host` 单模块约 29 秒（28 条，每条真起一个宿主进程），**它是当前的
瓶颈**——分片再多也快不过它。想再往下压的话可以把它按测试类切开
（`unittest tests.test_e2e_host.ClassName` 是支持的），但那会让同模块的类
并行，共享状态的风险比模块级分片高，需要另外验证。
"""

import argparse
import collections
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

# 未登记模块的估算权重（秒）。绝大多数纯逻辑模块都远小于这个数，
# 给一个偏大的值可以让它们被摊得更均匀一些。
_DEFAULT_WEIGHT = 0.05

# 重量级模块的实测耗时（秒），2026-08-20 本机测得，只列 >=1s 的。
# `test_e2e_host` 那格是 provider 延迟导入之后的复测值（改动前是 44.7）。
_WEIGHTS = {
    "test_e2e_host": 28.7, "test_bootstrap": 17.4, "test_e2e_control": 16.0,
    "test_subprocess_timeout": 13.8, "test_e2e_protected": 9.8, "test_e2e_hooks": 9.5,
    "test_skill_tui": 9.1, "test_subagent_integration": 8.8, "test_subagent_e2e": 8.2,
    "test_worktree_lifecycle": 7.3, "test_e2e_activity": 6.7, "test_tui_quit": 6.3,
    "test_tui_layout": 5.5, "test_command_tui": 4.9, "test_e2e_ask_user": 4.8,
    "test_web_search_bootstrap": 4.6, "test_worktree_gitcmd": 4.5, "test_review_fixes": 4.4,
    "test_worktree_cleanup": 3.8, "test_auto_plan_integration": 3.6,
    "test_tui_detail_level": 3.5, "test_tui_history_view": 2.9, "test_subagent_isolation": 2.9,
    "test_tui_panels": 2.9, "test_tui_batch": 2.5, "test_web_bootstrap": 2.4,
    "test_e2e_fold_run": 2.3, "test_e2e_discovery": 2.1, "test_tui_selection": 2.0,
    "test_protected_wiring": 1.8, "test_tui_tool_pending": 1.7, "test_hook_actions": 1.5,
    "test_tui_status_line": 1.5, "test_subagent_cancel_notice": 1.4,
    "test_trace_reader": 1.3, "test_run_command_encoding": 1.2, "test_subagent_service": 1.0,
}


def discover_modules() -> tuple[list[str], int]:
    """
    收集测试模块名与**期望的用例总数**。

    用 `unittest` 自己的 discover 而不是 glob，是为了与 `discover -s tests`
    的收集口径**完全一致**——跑完的完整性自检要拿它当基准，口径不同的话
    自检本身就没有意义。

    :returns: (模块名列表, 期望用例总数)
    :raises SystemExit: discover 阶段就有模块 import 失败时（此时并行跑没有意义）

    副作用：会 import 全部测试模块（约 2 秒）。
    """
    loader = unittest.TestLoader()
    suite = loader.discover(start_dir=str(REPO_ROOT / "tests"), top_level_dir=str(REPO_ROOT))
    if loader.errors:
        for err in loader.errors:
            print(err, file=sys.stderr)
        sys.exit("discover 阶段有模块无法加载，先修好它再跑并行")

    mods: set[str] = set()

    def walk(s) -> None:
        for item in s:
            if isinstance(item, unittest.TestSuite):
                walk(item)
            else:
                part = next((p for p in item.id().split(".") if p.startswith("test_")), None)
                if part:
                    mods.add(part)

    walk(suite)
    return sorted(mods), suite.countTestCases()


def pack(modules: list[str], n: int, weights: dict) -> list[list[str]]:
    """
    LPT 装箱：把模块按估算耗时降序，逐个塞给当前累计负载最小的分片。

    :param modules: 模块名列表
    :param n: 分片数
    :param weights: 模块名 → 估算耗时（秒），缺失的按 `_DEFAULT_WEIGHT` 计
    :returns: n 个分片，每个是模块名列表
    """
    shards: list[list[str]] = [[] for _ in range(n)]
    loads = [0.0] * n
    for m in sorted(modules, key=lambda m: -weights.get(m, _DEFAULT_WEIGHT)):
        i = min(range(n), key=lambda k: loads[k])
        shards[i].append(m)
        loads[i] += weights.get(m, _DEFAULT_WEIGHT)
    return shards


def main() -> int:
    """
    入口：装箱 → 起 N 个子进程 → 汇总 → 完整性自检。

    :returns: 进程退出码。0 全通过；1 有分片失败；2 用例总数对不上（有模块漏跑）

    副作用：为每个分片建一个临时目录（跑完删除）、写各分片日志到另一个
    临时目录（全通过且未加 --keep-logs 时删除）。
    """
    ap = argparse.ArgumentParser(description="分片并行跑测试（开发期工具）")
    ap.add_argument("-j", "--jobs", type=int, default=min(8, os.cpu_count() or 4),
                    help="分片数（缺省 min(8, CPU 核数)）")
    ap.add_argument("--timing", help="实测耗时 JSON，用它替代内置权重表装箱")
    ap.add_argument("--keep-logs", action="store_true", help="保留各分片日志文件")
    args = ap.parse_args()

    weights = dict(_WEIGHTS)
    if args.timing:
        measured: dict = collections.defaultdict(float)
        with open(args.timing, encoding="utf-8") as f:
            for r in json.load(f)["records"]:
                m = next((p for p in r["id"].split(".") if p.startswith("test_")), None)
                if m:
                    measured[m] += r["sec"]
        weights = dict(measured)

    print("收集测试模块…", flush=True)
    modules, expected = discover_modules()
    shards = pack(modules, args.jobs, weights)
    print(f"{len(modules)} 个模块 / 期望 {expected} 条用例 -> {args.jobs} 个分片\n", flush=True)

    log_dir = Path(tempfile.mkdtemp(prefix="rhine_parallel_logs_"))
    tmpdirs: list[str] = []
    procs = []
    t0 = time.perf_counter()
    for i, mods in enumerate(shards):
        # ⚠ 每个分片一个独立临时目录——e2e 名片池靠它隔离，见模块 docstring ①
        td = tempfile.mkdtemp(prefix=f"rhine_shard{i}_")
        tmpdirs.append(td)
        env = dict(os.environ, TEMP=td, TMP=td, TMPDIR=td, PYTHONIOENCODING="utf-8")
        log = (log_dir / f"shard{i}.log").open("w", encoding="utf-8", errors="replace")
        procs.append((i, subprocess.Popen(
            [sys.executable, "-m", "unittest", *[f"tests.{m}" for m in mods]],
            cwd=str(REPO_ROOT), env=env, stdout=log, stderr=subprocess.STDOUT), log))

    failed: list[int] = []
    ran_total = 0
    for i, p, log in procs:
        rc = p.wait()
        log.close()
        text = (log_dir / f"shard{i}.log").read_text(encoding="utf-8", errors="replace")
        for line in text.splitlines():
            if line.startswith("Ran "):
                ran_total += int(line.split()[1])
        print(f"  分片{i}: {'通过' if rc == 0 else '失败'}（{len(shards[i])} 模块）")
        if rc != 0:
            failed.append(i)
            print(text)

    wall = time.perf_counter() - t0
    for td in tmpdirs:
        shutil.rmtree(td, ignore_errors=True)
    if args.keep_logs or failed:
        print(f"\n日志保留在 {log_dir}")
    else:
        shutil.rmtree(log_dir, ignore_errors=True)

    print(f"\n用例 {ran_total}/{expected}   墙钟 {wall:.1f}s")

    # ⚠ 完整性自检：并行最危险的失败形态是「某个模块被漏掉却没人发现」，
    #    所以这一条比「有没有失败用例」还先判。
    if ran_total != expected:
        print(f"\n错误：用例数对不上（跑了 {ran_total}，期望 {expected}）——有模块被漏掉了",
              file=sys.stderr)
        return 2
    if failed:
        print(f"\n错误：分片 {failed} 有失败用例", file=sys.stderr)
        return 1
    print("\n全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
