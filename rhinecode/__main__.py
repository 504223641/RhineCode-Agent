"""
RhineCode 命令行入口模块。

本模块只负责「命令行的事」：解析参数、首次运行的模板生成、加载配置、
构造行为记录器，然后把真正的装配工作交给 `rhinecode.bootstrap.build_app`。

执行流程：
1. 解析 --config / --continue / --trace / --log-file 参数
2. 首次运行时生成三类用户级配置模板
3. 加载并校验 YAML 配置文件（含占位符 api_key 拦截）
4. 按 --trace 的三态语义构造行为记录器，按 --log-file 的三态语义装日志 handler
5. 调 build_app 完成装配，启动 Textual 事件循环，退出时统一清理

用法：
    rhine                          # 安装后直接运行，读 ~/.rhinecode/config.yaml
    rhine --config config.yaml     # 显式指定配置文件覆盖全局配置
    rhine --continue               # 启动时恢复最近一次会话
    rhine --trace                  # 开启行为记录，写 <项目根>/.rhinecode/traces/
    rhine --trace /tmp/x.jsonl     # 开启行为记录并指定文件
    rhine --log-file               # 开启运行日志，写 <项目根>/.rhinecode/logs/
    rhine --log-file /tmp/x.log    # 开启运行日志并指定文件
    python -m rhinecode            # 未安装或开发调试时的等价入口
"""

import argparse
import logging
import sys
from pathlib import Path

from rhinecode.bootstrap import BootstrapError, build_app
from rhinecode.config import load, user_config_path, scaffold_user_config, PLACEHOLDER_API_KEY
from rhinecode import logsetup
from rhinecode.permission import config as perm_config
from rhinecode.hooks import config as hook_config
from rhinecode.mcp import config as mcp_config
# c14：trace 默认输出路径落在主项目根，不随子 Agent 的隔离工作区变化。
from rhinecode.tools.path_guard import main_project_root
from rhinecode.trace import NullRecorder, default_trace_path
from rhinecode.trace.recorder import create_recorder

# `--trace` / `--log-file` 不带值时 argparse 填进 args 的哨兵字符串。
# 取一个不可能是真实路径的值，与「用户显式给了路径」区分开。
# 两个选项共用同一个哨兵：它们的三态语义逐字相同，各写一份只会让
# 「改了一个忘了另一个」成为可能。
_PATH_DEFAULT = "<default>"

# 入口层的 logger。日志缺省关闭，因此这些调用在不开 --log-file 时**零开销地
# 什么都不做**（根 logger 上没有 handler）。
_logger = logging.getLogger(__name__)


def main() -> None:
    """
    程序主入口：解析命令行、加载配置、装配并启动 TUI。

    异常处理：
    - FileNotFoundError / ValueError（配置文件缺失或字段缺失）
    - BootstrapError（装配阶段三类致命错误：命令注册冲突 / Provider 初始化失败 /
      Skill 白名单笔误）
    三类情况均打印可读错误信息后 sys.exit(1)，不向用户暴露堆栈。

    退出码（C6 缺口一）：正常退出 0；TUI 运行期崩溃 1（读 `app.return_code`，
    它由 Textual 的 `_handle_exception` 置位）；事件循环自身抛异常也是 1。
    此前**从不读 return_code**，于是崩溃退出的进程退出码是 0，CI 与包装脚本
    一律认为这次运行成功了。

    副作用：可能生成配置模板、连接外部 MCP Server、创建会话存档与锁、
    创建行为记录文件。
    """
    parser = argparse.ArgumentParser(description="RhineCode - 终端 AI 编程助手")
    # --config 缺省为 None，表示「用用户级全局配置 ~/.rhinecode/config.yaml」；
    # 显式传入时按给定路径加载，供临时覆盖或多配置切换。
    parser.add_argument(
        "--config",
        default=None,
        help="配置文件路径（缺省使用 ~/.rhinecode/config.yaml）",
    )
    # --continue（c9 F10）：启动时恢复最近的未锁定会话。dest 必须显式指定——
    # `continue` 是 Python 关键字，argparse 默认派生的 args.continue 会语法错误。
    parser.add_argument(
        "--continue",
        dest="continue_session",
        action="store_true",
        help="启动时恢复最近一次会话，接着上次继续",
    )
    # --trace（trace spec F8/F25）：**三态**语义，靠 nargs="?" + const 实现——
    #   ① 完全没写 --trace       → args.trace 为 None（default）       → 关闭
    #   ② 写了 --trace 但没给值  → args.trace 为 _PATH_DEFAULT（const）→ 缺省路径
    #   ③ 写了 --trace <路径>    → args.trace 为该路径                  → 指定路径
    #
    # 为什么必须有 const：不给的话「给了但无值」也会拿到 None，与「未给」无法区分，
    # 于是 `rhine --trace` 会静默地什么都不记。
    # 为什么必须 nargs="?"：写成普通带值选项（nargs 默认）时 `--trace` 后面若紧跟
    # 其它参数（`rhine --trace --continue`），会把 `--continue` 当成路径吞掉。
    parser.add_argument(
        "--trace",
        nargs="?",
        const=_PATH_DEFAULT,
        default=None,
        metavar="PATH",
        help="开启行为记录（测试设施）；不给值时写 <项目根>/.rhinecode/traces/<时间戳>.jsonl",
    )
    # --log-file（C5）：三态语义与 --trace 完全一致，理由也一样（见上面那段注释）。
    #
    # ⚠ 它与 --trace 是**两件事**，别合成一个开关：trace 记的是完整请求/响应/工具
    # 输出**原文**（与会话存档同级敏感，可能含明文 API Key），日志记的是「哪一步、
    # 什么结果、耗时多少」的一行摘要。排查「它卡在哪了」只需要后者，而让用户为此
    # 交出一份含对话原文的产物是不成比例的代价。
    parser.add_argument(
        "--log-file",
        dest="log_file",
        nargs="?",
        const=_PATH_DEFAULT,
        default=None,
        metavar="PATH",
        help="开启运行日志；不给值时写 <项目根>/.rhinecode/logs/<时间戳>.log",
    )
    args = parser.parse_args()

    # 日志要**尽早**装上——它的用途之一就是排查启动期的问题（配置加载、MCP 连接、
    # Provider 初始化），装晚了那段恰恰记不到。
    # ⚠ 位置仍在 parse_args 之后：路径要用到 args。
    if args.log_file is None:
        log_file = None
    elif args.log_file == _PATH_DEFAULT:
        log_file = logsetup.default_log_path(main_project_root())
    else:
        log_file = Path(args.log_file)
    # configure 自己 fail-safe（路径不可写时提示一行、返回 None），不会抛。
    logsetup.configure(log_file)
    _logger.info("RhineCode 启动：argv=%s", sys.argv[1:])

    # 决定实际配置路径：显式 --config 优先，否则用用户级全局配置。
    # explicit 用于区分「用户点名的文件」和「缺省全局文件」——只有缺省文件缺失时才自动生成模板，
    # 显式指到一个不存在的文件仍按错误处理（不擅自造文件）。
    explicit = args.config is not None
    config_path = Path(args.config) if explicit else user_config_path()

    # 首次运行引导：仅在缺省流程（未显式 --config）里为用户级 ~/.rhinecode 生成四类模板。
    # 两类语义不同：
    # - config.yaml 必需（含 api_key）→ 本次才生成时，引导填 key 后退出。
    # - permissions.yaml / mcp.yaml / hooks.yaml 可选（fail-safe，缺省即空）→
    #   模板全注释、等价于空，静默生成、不因它们退出；已有 config.yaml 的老用户
    #   下次运行会顺带补上这几份。
    if not explicit:
        try:
            config_created = scaffold_user_config(config_path)
            perm_config.scaffold_user_config(perm_config.user_config_path())
            mcp_config.scaffold_user_config(mcp_config.user_config_path())
            hook_config.scaffold_user_config(hook_config.user_config_path())
        except OSError as e:
            print(f"无法生成配置模板：{e}", file=sys.stderr)
            sys.exit(1)
        if config_created:
            print(
                f"已在 {config_path.parent} 生成配置模板"
                "（config.yaml / permissions.yaml / mcp.yaml / hooks.yaml），"
                "请在 config.yaml 填入真实 api_key 后重新运行 rhine。"
            )
            sys.exit(0)

    # 加载配置，捕获文件缺失和字段缺失两类错误并友好提示
    try:
        cfg = load(str(config_path))
    except (FileNotFoundError, ValueError) as e:
        print(f"配置错误：{e}", file=sys.stderr)
        sys.exit(1)

    # 占位符未替换引导：模板里的 YOUR_API_KEY 是非空字符串，能通过 load() 的非空校验，
    # 这里单独拦下，避免带着假 key 启动、到调用模型 API 时才报难懂的错。
    if cfg.api_key == PLACEHOLDER_API_KEY:
        print(
            f"检测到 {config_path} 的 api_key 仍是占位符，请填入真实 api_key 后重新运行 rhine。",
            file=sys.stderr,
        )
        sys.exit(1)

    # 按三态构造记录器。后两态**一律经 create_recorder 工厂**，不得直接
    # TraceRecorder(...)：目标路径不可写时构造必然抛异常，直接构造会让进程
    # 带着 traceback 崩在装配之前——用户只是想开个日志，结果程序起不来
    # （trace spec AC22 明确要求这种情形不阻断）。
    if args.trace is None:
        recorder = NullRecorder()
    elif args.trace == _PATH_DEFAULT:
        recorder = create_recorder(default_trace_path(main_project_root()))
    else:
        recorder = create_recorder(Path(args.trace))

    # 装配：顺序与理由全部收在 bootstrap.build_app 里。
    # BootstrapError 的 args[0] 已是成文的完整文案，这里原样打印、不再拼前缀。
    try:
        result = build_app(
            cfg,
            resume_latest=args.continue_session,
            recorder=recorder,
        )
    except BootstrapError as e:
        print(e, file=sys.stderr)
        sys.exit(1)

    # try/finally 保证无论正常退出还是异常，都统一回收资源（清理动作幂等，
    # 五步顺序与理由见 bootstrap.build_app 里的 cleanup）。
    crashed = False
    try:
        result.app.run()
    except KeyboardInterrupt:
        # 兜底的兜底。正常情况下 SIGINT 会被 `RhineApp._install_sigint_guard`
        # 接管、转成一次「按了 Ctrl+C」，走连按两次的判定；这里只覆盖它**还没装上**
        # 或**已经卸掉**的那两个窄窗口（界面挂载前、退出收尾中）。
        # 那两个窗口里没有界面可提示，退出是唯一合理的结果——但**不要甩回溯**：
        # 用户按的是 Ctrl+C，不是程序出了错。
        pass
    except Exception as e:  # noqa: BLE001 —— 见下方那段说明
        # C6 形态②：异常发生在 `_process_messages` 自身，逃出了 Textual 的接管。
        #
        # ⚠ **它是补漏不是主菜。** 绝大多数崩溃发生在消息处理器与事件回调里
        # （`on_mount` / `on_key` / Worker 的 done callback），那些被 Textual 的
        # `_handle_exception` 接住、`app.run()` **正常返回**，这个 except 一个字
        # 都收不到——真正治那一类的是 `RhineApp._handle_exception` 那个覆写。
        # 本分支只覆盖罕见的形态②，而它同样不该把 traceback 甩给用户。
        _logger.exception("Textual 事件循环异常退出")
        path = logsetup.log_path()
        where = f"完整堆栈见 {path}" if path else "用 `rhine --log-file` 重跑可留下完整堆栈"
        print(f"RhineCode 遇到未预期的错误已退出：{e}（{where}）", file=sys.stderr)
        crashed = True
    finally:
        result.cleanup()

    # C6 缺口一：把退出码交出去。
    #
    # 在此之前，`app.run()` 之后直接走 `finally: result.cleanup()` 就结束了，
    # **从不读 `app.return_code`**——于是一次崩溃退出的进程退出码是 **0**，
    # CI、包装脚本、`&&` 链、systemd/supervisor 一律认为这次运行成功了。
    # 上游那 5 处配置/装配错误的 `sys.exit(1)` 都做对了，唯独运行期崩溃这条漏了，
    # 而它恰恰是最需要被上游知道的一种。
    #
    # ⚠ **必须放在 `finally: result.cleanup()` 之后**：`sys.exit` 抛的是
    # `SystemExit`，写进 try 里会跳过清理（MCP 子进程、会话锁、trace 句柄全留着）。
    # 装配层 cleanup 的五步顺序与幂等守卫一个字都不要动，退出码只排在它后面。
    if crashed:
        sys.exit(1)
    if result.app.return_code:
        sys.exit(result.app.return_code)


if __name__ == "__main__":
    main()
