"""
装配层测试（trace T26/T27）：进程内装配、清理、致命错误、用户目录隔离、命令行形态。

对应 spec AC3（关闭时无中间层）/ AC22（不阻断）/ AC23（两种命令行形态都落盘）/
AC28（可在测试进程内装配）/ AC29（笔误时 MCP 未连接）/ AC30（user_dir 隔离）/
AC31（连续装配互不污染）/ AC32（既有启动行为不变）。

统一 fixture：**临时目录作工作区 + os.chdir + 临时 user_dir + finally 里 cleanup**。
`build_app` 会经 MemoryManager.startup() 在**当前工作目录**建 `.rhinecode/sessions/`
与会话锁，不切工作目录会把垃圾写进仓库、不清理会留下锁（靠 600 秒过期自愈）。
"""

import json
import os
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import rhinecode.bootstrap as bootstrap
from rhinecode.bootstrap import BootstrapError, build_app
from rhinecode.commands import CommandRegistrationError
from rhinecode.config import Config
from rhinecode.tools import path_guard
from rhinecode.trace.recorder import TraceRecorder
from rhinecode.trace.tracing_provider import TracingProvider


def _cfg(**over) -> Config:
    """构造一份可用的假配置。create_provider 不联网（只构造客户端对象），假 key 可用。"""
    base = dict(
        protocol="deepseek",
        model="deepseek-chat",
        base_url="https://api.deepseek.com",
        api_key="fake-key-for-test",
        debug_log=False,
        context_window=65536,
    )
    base.update(over)
    return Config(**base)


class BootstrapFixture(unittest.TestCase):
    """切到临时工作区、备好临时 user_dir，并保证每个用例都清理进程级全局状态。"""

    def setUp(self) -> None:
        self._work = tempfile.TemporaryDirectory()
        self._user = tempfile.TemporaryDirectory()
        self.work = Path(self._work.name).resolve()
        self.user_dir = Path(self._user.name).resolve()
        self._cwd = os.getcwd()
        os.chdir(self.work)
        # 只读白名单是进程级全局状态，用例间必须互不污染
        path_guard.clear_read_roots()

    def tearDown(self) -> None:
        os.chdir(self._cwd)
        path_guard.clear_read_roots()
        for d in (self._work, self._user):
            try:
                d.cleanup()
            except OSError:
                # Windows 下会话锁文件可能仍被持有；留给系统临时目录清理
                pass

    def build(self, **kw):
        """装配一次并登记 cleanup（cleanup 幂等，用例里可再显式调）。"""
        result = build_app(_cfg(), user_dir=self.user_dir, **kw)
        self.addCleanup(result.cleanup)
        return result


class BuildAndCleanupTest(BootstrapFixture):
    def test_build_app_usable_in_process(self) -> None:
        """AC28：测试进程内即可装配出可用的 app 与 cleanup。"""
        result = self.build()
        self.assertTrue(hasattr(result.app, "run"))
        self.assertTrue(callable(result.cleanup))
        self.assertIsNotNone(result.manager)
        self.assertIsNotNone(result.tool_registry)
        self.assertIsNotNone(result.command_registry)
        self.assertIsNotNone(result.skill_manager)
        self.assertIsNotNone(result.mcp_manager)
        # 证明真跑了完整装配而不是空壳：命令注册表可解析、工具中心有内置工具、
        # Skill 的三个内置样板已扫到
        self.assertIsNotNone(result.command_registry.resolve("/help"))
        self.assertIn("read_file", result.tool_registry.names())
        self.assertIn("commit", result.skill_manager.index_text())

    def test_no_arg_call_works(self) -> None:
        """T13–T16/T18 的参数必须全部可选：只传 cfg 也能装配。"""
        result = build_app(_cfg())
        try:
            self.assertTrue(hasattr(result.app, "run"))
        finally:
            result.cleanup()

    def test_cleanup_is_idempotent_and_emits_single_session_end(self) -> None:
        """cleanup 连调两次不抛，且 session_end 恰好一条（T23 幂等守卫）。"""
        trace_path = self.work / "t.jsonl"
        recorder = TraceRecorder(trace_path)
        result = build_app(_cfg(), user_dir=self.user_dir, recorder=recorder)

        result.cleanup()
        result.cleanup()  # 第二次必须静默返回

        records = _read(trace_path)
        ends = [r for r in records if r["type"] == "session_end"]
        self.assertEqual(len(ends), 1)
        self.assertEqual(ends[0]["reason"], "normal_exit")
        self.assertIn("turn_total", ends[0])
        self.assertIn("elapsed_seconds", ends[0])

    def test_cleanup_releases_session_lock_and_mcp(self) -> None:
        result = build_app(_cfg(), user_dir=self.user_dir)
        result.mcp_manager.close_all = MagicMock()
        result.manager.memory_manager.close = MagicMock()
        result.cleanup()
        result.mcp_manager.close_all.assert_called_once()
        result.manager.memory_manager.close.assert_called_once()

    def test_disabled_recorder_leaves_no_tracing_provider(self) -> None:
        """AC3 的装配侧：关闭记录时链路上不得有中间层（不是「开销小」，是「不存在」）。"""
        result = self.build()
        self.assertNotIsInstance(result.manager._provider, TracingProvider)

    def test_enabled_recorder_wraps_provider(self) -> None:
        trace_path = self.work / "t.jsonl"
        recorder = TraceRecorder(trace_path)
        result = build_app(_cfg(), user_dir=self.user_dir, recorder=recorder)
        try:
            self.assertIsInstance(result.manager._provider, TracingProvider)
        finally:
            result.cleanup()

    def test_session_start_snapshot(self) -> None:
        """装配期快照：工具清单非空、MCP 状态字段存在、api_key 为掩码（AC18）。"""
        from rhinecode.trace.models import REDACTED

        trace_path = self.work / "t.jsonl"
        recorder = TraceRecorder(trace_path)
        result = build_app(_cfg(), user_dir=self.user_dir, recorder=recorder)
        try:
            starts = [r for r in _read(trace_path) if r["type"] == "session_start"]
            self.assertEqual(len(starts), 1)
            s = starts[0]
            self.assertIn("read_file", s["tool_names"])
            self.assertIn("load_skill", s["tool_names"])
            self.assertIn("mcp_status", s)
            self.assertEqual(s["config"]["api_key"], REDACTED)
            self.assertNotIn("fake-key-for-test", json.dumps(s, ensure_ascii=False))
        finally:
            result.cleanup()


class FatalErrorTest(BootstrapFixture):
    def test_command_conflict_raises_with_exact_text(self) -> None:
        conflict = CommandRegistrationError(
            "command name collision: /ctx is declared by /context and /other"
        )
        with patch.object(bootstrap, "build_builtin_registry", side_effect=conflict):
            with self.assertRaises(BootstrapError) as ctx:
                build_app(_cfg(), user_dir=self.user_dir)
        # 文案是不可变契约：既有启动测试逐字断言它
        self.assertTrue(str(ctx.exception).startswith("命令注册冲突："))
        self.assertIn("/ctx", str(ctx.exception))

    def test_provider_error_raises_with_exact_text(self) -> None:
        with patch.object(bootstrap, "create_provider", side_effect=ValueError("坏协议")):
            with self.assertRaises(BootstrapError) as ctx:
                build_app(_cfg(), user_dir=self.user_dir)
        self.assertEqual(str(ctx.exception), "Provider 初始化错误：坏协议")

    def test_unknown_granted_tool_no_longer_fatal(self) -> None:
        """
        `allowed-tools` 里的无法识别项**不再让装配失败**（对齐改造 F13）。

        C11 时它是三类致命错误之一（白名单笔误 fail-fast）；对齐之后
        `allowed-tools` 是预授权声明，来源可能是外部工具，出现本系统没有的
        工具名是正常现象，只警告不致命。

        连带：`connect_all` 现在**会**被调用（C11 时它被 fail-fast 挡在前面）。
        """
        skills = self.work / ".rhinecode" / "skills"
        skills.mkdir(parents=True, exist_ok=True)
        (skills / "ext.md").write_text(
            "---\nname: ext\ndescription: d\nallowed-tools: [WebFetch]\n---\n正文\n",
            encoding="utf-8",
        )
        result = build_app(_cfg(), user_dir=self.user_dir)
        self.addCleanup(lambda: result.cleanup("test"))
        self.assertIsNotNone(result.app)

    def test_no_sys_exit_inside_factory(self) -> None:
        """
        spec F22：工厂内不得出现 sys.exit 调用（否则测试捕不到，只能起子进程）。

        用 AST 而不是字符串查找：`sys.exit` 这四个字在本模块的文档与注释里
        合法地出现了好几次（解释「为什么改成抛异常」），字符串查找会误报。
        """
        import ast

        tree = ast.parse(Path(bootstrap.__file__).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                self.assertNotEqual(
                    ast.unparse(node.func), "sys.exit", "装配工厂内不得调用 sys.exit"
                )


class UserDirIsolationTest(BootstrapFixture):
    def test_four_kinds_of_user_content_isolated(self) -> None:
        """
        AC30：在 A 目录预置四类用户级内容，用 B 目录装配，四类均不生效。

        四类分别经**不同**的取值点，必须逐项验证：
        项目指令与笔记索引 → memory；Skill → skills；权限规则 → permission.config；
        MCP 声明 → mcp.config。
        """
        other = Path(tempfile.mkdtemp()).resolve()
        self.addCleanup(lambda: None)

        (other / "RHINE.md").write_text("MAGIC_USER_INSTRUCTION", encoding="utf-8")
        (other / "memory").mkdir(parents=True, exist_ok=True)
        (other / "memory" / "MEMORY.md").write_text("MAGIC_USER_NOTE", encoding="utf-8")
        (other / "skills").mkdir(parents=True, exist_ok=True)
        (other / "skills" / "magicskill.md").write_text(
            "---\nname: magicskill\ndescription: MAGIC_USER_SKILL\n---\n正文\n",
            encoding="utf-8",
        )
        (other / "permissions.yaml").write_text(
            'deny:\n  - "Read(magic-secret.txt)"\n', encoding="utf-8"
        )
        (other / "mcp.yaml").write_text(
            "mcpServers:\n  magicserver:\n    url: https://magic.invalid/mcp\n",
            encoding="utf-8",
        )

        # 用另一个（空的）临时目录装配
        result = self.build()

        mm = result.manager.memory_manager
        # ① 用户级项目指令不进「自定义指令」槽位
        self.assertNotIn("MAGIC_USER_INSTRUCTION", mm.custom_instructions())
        # ② 用户级笔记索引不进「长期记忆」槽位
        self.assertNotIn("MAGIC_USER_NOTE", mm.memory_index())
        # ③ 用户级 Skill 不在第一阶段清单里
        self.assertNotIn("magicskill", result.skill_manager.index_text())
        # ④ 用户级权限规则不参与求值
        patterns = [r.pattern for r in result.manager._engine.file_ruleset.rules]
        self.assertNotIn("magic-secret.txt", patterns)
        # ⑤ 用户级 MCP 未被连接
        self.assertNotIn("magicserver", [s.name for s in result.mcp_manager.states])

    def test_consecutive_builds_do_not_leak_read_roots(self) -> None:
        """AC31：同进程连续两次装配 + 清理，第二次的白名单不含第一次注册的路径。"""
        first_user = Path(tempfile.mkdtemp()).resolve()
        second_user = Path(tempfile.mkdtemp()).resolve()

        r1 = build_app(_cfg(), user_dir=first_user)
        roots_1 = [str(p) for p in path_guard._EXTRA_READ_ROOTS]
        self.assertTrue(any(str(first_user) in p for p in roots_1))
        r1.cleanup()
        # cleanup 第四步 clear_read_roots 已复位
        self.assertEqual(path_guard._EXTRA_READ_ROOTS, [])

        r2 = build_app(_cfg(), user_dir=second_user)
        try:
            roots_2 = [str(p) for p in path_guard._EXTRA_READ_ROOTS]
            self.assertFalse(
                any(str(first_user) in p for p in roots_2),
                "第二次装配继承了第一次注册的只读根，沙箱边界被悄悄放宽",
            )
            self.assertTrue(any(str(second_user) in p for p in roots_2))
        finally:
            r2.cleanup()


class SubprocessTest(unittest.TestCase):
    """
    子进程实跑（AC23 / AC32）。

    两类用例、两套做法，区别在于「验证点在记录器构造之前还是之后」：

    ① **启动行为不变 / 构造降级**（AC32、AC22）：必须让进程**确定性退出**，
       用「--config 指向一份 api_key 仍是占位符的配置」——命中占位符拦截、
       退出码 1、不进 Textual 事件循环。

    ② **两种 --trace 形态都在目标位置落盘**（AC23）：这需要至少产出一条事件，
       而全部 CLI 级的确定性退出路径都发生在第一条事件（装配末尾的 session_start）
       **之前**——`BootstrapError` 也一样，抛出时一条事件都还没产生。
       所以这里只能真启动，然后**轮询到文件有内容即终止进程**。
       记录器每条都 flush，因此不必等进程退出就能读到内容。
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name).resolve()

    def tearDown(self) -> None:
        try:
            self._tmp.cleanup()
        except OSError:
            # Windows 下被终止进程可能仍短暂持有会话锁，留给系统临时目录清理
            pass

    def _env(self) -> dict:
        env = dict(os.environ)
        # 子进程的 stderr 必须是 UTF-8，否则中文提示在 Windows 默认代码页下变成乱码，
        # 断言「stderr 含某段中文」会莫名失败
        env["PYTHONIOENCODING"] = "utf-8"
        return env

    def _run(self, *args: str, cwd: Path) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, "-m", "rhinecode", *args],
            cwd=str(cwd),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=120,
            env=self._env(),
        )

    def _launch_until_trace(self, *args: str, cwd: Path, probe) -> Path:
        """
        真启动一次并轮询 `probe()`，拿到非空记录文件后立刻终止子进程。

        :param probe: 无参函数，返回候选记录文件路径或 None
        :returns: 已有内容的记录文件路径
        """
        proc = subprocess.Popen(
            [sys.executable, "-m", "rhinecode", *args],
            cwd=str(cwd),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=self._env(),
        )
        try:
            deadline = time.time() + 60
            while time.time() < deadline:
                found = probe()
                if found is not None and found.exists() and found.stat().st_size > 0:
                    return found
                if proc.poll() is not None:
                    break
                time.sleep(0.2)
            # 进程提前退出或超时：把 stderr 带进失败信息，便于定位
            out, err = proc.communicate(timeout=10)
            self.fail(
                "未在超时内看到有内容的记录文件；"
                f"returncode={proc.returncode} stderr={err.decode('utf-8', 'replace')[:500]}"
            )
        finally:
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.communicate(timeout=15)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.communicate()

    def _placeholder_config(self) -> Path:
        p = self.tmp / "placeholder.yaml"
        p.write_text(
            "protocol: deepseek\nmodel: deepseek-chat\n"
            "base_url: https://api.deepseek.com\napi_key: YOUR_API_KEY\n",
            encoding="utf-8",
        )
        return p

    def _good_config(self) -> Path:
        p = self.tmp / "good.yaml"
        p.write_text(
            "protocol: deepseek\nmodel: deepseek-chat\n"
            "base_url: https://api.deepseek.com\napi_key: real-looking-key\n",
            encoding="utf-8",
        )
        return p

    def test_without_trace_behaves_as_before(self) -> None:
        """AC32：不带 --trace 时退出行为与 stderr 与既有一致。"""
        cfg = self._placeholder_config()
        proc = self._run("--config", str(cfg), cwd=self.tmp)
        self.assertEqual(proc.returncode, 1)
        self.assertIn("占位符", proc.stderr)
        # 不开记录时不产生任何 traces 目录（AC4 的子进程侧）
        self.assertFalse((self.tmp / ".rhinecode" / "traces").exists())

    def test_unknown_granted_tool_starts_normally_in_subprocess(self) -> None:
        """
        真起一个子进程验：`allowed-tools` 写了本系统没有的工具名 → **正常启动**。

        C11 时同一份样本会以退出码 1 终止（白名单笔误 fail-fast）。
        对齐之后它只是一条警告——本条用例就是那次行为反转的子进程级护栏。
        """
        work = self.tmp / "ext-work"
        (work / ".rhinecode" / "skills").mkdir(parents=True, exist_ok=True)
        (work / ".rhinecode" / "skills" / "ext.md").write_text(
            "---\nname: ext\ndescription: 外部来的\nallowed-tools: [WebFetch]\n---\n正文\n",
            encoding="utf-8",
        )
        # 用 `_launch_until_trace`：启动成功意味着进程会一直跑着 TUI，
        # 不能用 `_run`（它会等到超时）。产出记录即证明它跨过了装配。
        traces_dir = work / ".rhinecode" / "traces"

        def probe():
            """
            等到记录里**真的出现 `session_start`** 才算就绪。

            ⚠ 不能只等「文件非空」——`session_start` **不是文件里的第一条事件**
            （`bind_tools` 的 `skill_state` 排在它前面，`bootstrap.py` 的第 ⑦ 步
            注释写着这条）。只等非空的话，会读到「只有 skill_state」的那一瞬间，
            下面的断言随即失败。

            这个竞态一直都在，但窗口很窄，只在**全量测试的并发负载**下才偶尔命中
            （单跑必绿）——与 CLAUDE.md 里记着的 `force_rmtree` 那条是同一类坑。
            c12 在 `skill_state` 与 `session_start` 之间插入了 Hook 配置加载，
            把窗口拉宽了，于是它稳定复现了出来。
            """
            files = sorted(traces_dir.glob("*.jsonl")) if traces_dir.exists() else []
            if not files or files[0].stat().st_size == 0:
                return None
            try:
                records = _read(files[0])
            except Exception:  # noqa: BLE001 —— 可能正读到写了一半的行
                return None
            return files[0] if any(r.get("type") == "session_start" for r in records) else None

        path = self._launch_until_trace(
            "--config", str(self._good_config()), "--trace", cwd=work, probe=probe
        )
        records = _read(path)
        self.assertTrue(
            any(r["type"] == "session_start" for r in records),
            "装配跨过去了才会有 session_start",
        )

    def test_trace_flag_creates_default_file(self) -> None:
        """AC23 形态一：`--trace` 不带值 → 缺省路径下产出可解析的记录。"""
        work = self.tmp / "w1"
        work.mkdir(parents=True, exist_ok=True)
        traces_dir = work / ".rhinecode" / "traces"

        def probe():
            files = sorted(traces_dir.glob("*.jsonl")) if traces_dir.exists() else []
            return files[0] if files else None

        found = self._launch_until_trace(
            "--config", str(self._good_config()), "--trace", cwd=work, probe=probe
        )
        first = json.loads(found.read_text(encoding="utf-8").splitlines()[0])
        self.assertEqual(first["seq"], 1)
        # 首条**不一定**是 session_start：装配期的 bind_tools 会先产一条 skill_state
        # （session_start 必须等 connect_all + bind_tools 完成才能记出完整快照）。
        # 这里只断言「首行是一条格式合法的已登记事件」。
        from rhinecode.trace.models import TraceEventType

        self.assertIn(first["type"], {t.value for t in TraceEventType})
        self.assertIn("scope", first)
        self.assertIn("ts", first)

    def test_trace_flag_with_explicit_path(self) -> None:
        """AC23 形态二：`--trace <路径>` → 指定文件被创建且首行可解析。"""
        work = self.tmp / "w2"
        work.mkdir(parents=True, exist_ok=True)
        target = self.tmp / "custom" / "mytrace.jsonl"

        found = self._launch_until_trace(
            "--config",
            str(self._good_config()),
            "--trace",
            str(target),
            cwd=work,
            probe=lambda: target,
        )
        self.assertEqual(found, target)
        first = json.loads(found.read_text(encoding="utf-8").splitlines()[0])
        self.assertEqual(first["seq"], 1)

    def test_trace_unwritable_path_does_not_block_startup(self) -> None:
        """AC22：--trace 指向不可写路径时降级为不记录，启动流程照常走。"""
        cfg = self._placeholder_config()
        blocker = self.tmp / "iam-a-file"
        blocker.write_text("x", encoding="utf-8")
        proc = self._run(
            "--config", str(cfg), "--trace", str(blocker / "t.jsonl"), cwd=self.tmp
        )
        # 仍然是「占位符」这条既有的退出路径，而不是记录器构造抛出的 traceback
        self.assertEqual(proc.returncode, 1)
        self.assertIn("占位符", proc.stderr)
        self.assertNotIn("Traceback", proc.stderr)


def _read(path: Path) -> list[dict]:
    text = path.read_text(encoding="utf-8")
    return [json.loads(l) for l in text.splitlines() if l.strip()]


if __name__ == "__main__":
    unittest.main()
