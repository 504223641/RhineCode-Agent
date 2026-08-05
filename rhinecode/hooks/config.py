"""
Hook 配置的文件层（spec F9）：两层 YAML 的定位、加载、容错与模板生成。

两层配置（**没有本地级**）：

- 用户级 `~/.rhinecode/hooks.yaml`            —— 跨项目的个人自动化
- 项目级 `<项目根>/.rhinecode/hooks.yaml`     —— 随仓库走、可提交

**为什么没有本地级**：C6 的本地级 `permissions.local.yaml` 是「永久放行」自动写入的
授权记录，Hook 没有这种自动写入场景——规则永远由人手写。多一层只会多一个要解释的概念。

**合并规则**：两层规则**全部生效**，不按层级覆盖（与 C6 的哲学一致）。
执行顺序固定为「用户级 → 项目级 → 层内声明序」，**不提供优先级字段**。

容错原则（fail-safe，spec N1）：文件缺失视为空规则集；YAML 解析失败时该层降级为空
并收集一条可读警告，**绝不**因配置坏掉而崩溃或阻断启动。

## ⚠ 项目级的安全性质

项目级 `hooks.yaml` 随代码仓库分发，`git pull` 之后可能凭空多出几条规则，
而 Hook 的动作**直接执行**、不经模型、不经人在回路确认。这比项目级 Skill 严重
一个量级（Skill 正文只是「发给模型的文本」，它指挥的每个工具调用照样过五层管线）。

对冲手段是 `report.render_project_notice`：启动时**逐条**列出项目级规则的
「事件 → 动作原文」，命令串与 URL 完整展示。评审 `.rhinecode/hooks.yaml`
应与评审代码同等对待。
"""

from pathlib import Path
from typing import Optional

import yaml

from rhinecode.hooks.models import HookRule
from rhinecode.hooks.parser import parse_rules
from rhinecode.tools.path_guard import workspace_root

# 配置目录与文件名常量，集中定义便于统一调整（与 permission/config.py 同构）。
_CONFIG_DIR_NAME = ".rhinecode"
_CONFIG_FILE = "hooks.yaml"

# 来源层标记。顺序即执行顺序（spec F9）。
SOURCE_USER = "user"
SOURCE_PROJECT = "project"

# 首次运行自动生成的模板。内容**全部注释**：`yaml.safe_load` 一个全注释文件得到 None，
# `parse_rules` 对 None 返回空规则集且无警告，因此「有此模板」与「无文件」
# 对运行时完全等价——生成它只为方便用户发现和编辑，绝不改变任何行为（spec F13）。
_CONFIG_TEMPLATE = """\
# RhineCode Hook 规则配置（可选）。
#
# 一条规则 = 事件（何时） + 条件（可省，省略即无条件） + 动作（做什么）。
# 全部注释时等价于「没有任何 Hook」，运行时行为与不存在本文件完全一致。
#
# 取消下面的注释即可生效：
#
# hooks:
#   # ① 改完 Python 文件自动格式化
#   - name: 格式化改动的 Python 文件
#     event: post_tool_use
#     if:
#       all:
#         - tool: edit_file
#         - file_path: "**/*.py"
#     action:
#       type: command
#       command: "python -m black $(python -c \\"import json,sys;print(json.load(sys.stdin)['file_path'])\\")"
#       timeout: 30
#
#   # ② 拦住直接 push 到 main
#   - name: 禁止直接 push 到 main
#     event: pre_tool_use
#     if:
#       all:
#         - tool: run_command
#         - command: "git push * main*"
#     action:
#       type: command
#       command: "echo '请走 PR，不要直接 push 到 main' >&2; exit 2"
#
#   # ③ 每个回合开始时给模型注入一句提醒
#   - name: 提醒当前分支
#     event: turn_start
#     if:
#       all:
#         - scope: main
#     action:
#       type: prompt
#       text: "改动代码前先确认当前分支不是 main。"
#
# ── 事件（十二个）──
#   会话级：session_start / session_end
#   回合级：turn_start / turn_end            （scope 为 main 或 isolated:<skill名>）
#   消息级：user_message / assistant_message
#   工具级：pre_tool_use / post_tool_use / post_tool_use_failure
#   系统级：pre_compact / post_compact / notification
#
# ── 条件 ──
#   if 下写 all（全部满足）或 any（任一满足），**二选一，不混用不嵌套**。
#   每项写成「字段: 模式」，模式支持四种形态：
#     精确  tool: run_command
#     通配  command: "git *"          （命令类带词边界，路径类是 gitignore 风格）
#     正则  command: "/^git (push|reset)/"   （一对 / 包裹，非锚定）
#     反向  tool: "!run_command"       （值前缀 !，可与上面三种叠加）
#
# ── 动作（四种）──
#   command  执行 shell 命令。**事件负载以 JSON 从标准输入喂给命令**，
#            配置里不做任何字符串插值（避免命令注入）。
#            退出码 0=通过，2=拦截（仅 pre_tool_use 有效），其它=失败。
#   prompt   往 <system-reminder> 注入一段文本，模型下一次请求可见，**只出现一次**。
#   http     发一个 HTTP 请求。响应不参与任何决策。
#   agent    启动子 Agent —— **本版本仅占位，不会真的运行**。
#
# ── 执行控制 ──
#   once: true     本次进程运行内只触发一次（不持久化，重启重置）
#   async: true    后台执行不等结果（**pre_tool_use 上禁止**）
#   timeout: 30    超时秒数（command 缺省 60，http 缺省 10）
#
# ⚠ 两条安全性质
#   1. Hook **只能收紧不能放宽**：pre_tool_use 只能拦截（deny）或升级为人工确认（ask），
#      **没有放行**。写 {"decision":"allow"} 会被忽略。
#   2. 拦截类 Hook **自身失败即拦截**（fail-closed）：脚本崩了、超时了、退出码不对，
#      都按拦下处理。这样一条写坏的安全 Hook 会立刻可见，而不是静默失效。
#      其余事件的 Hook 失败只记录，不影响任何流程。
#
# ⚠ 项目级 <项目根>/.rhinecode/hooks.yaml 随仓库分发，里面的命令会在启动/运行时
#   **直接执行**（不经模型、不经确认面板）。启动时会逐条列出它们的内容，请当作
#   代码来评审。
"""


def user_config_path(user_dir: Optional[Path] = None) -> Path:
    """
    用户级配置路径：`~/.rhinecode/hooks.yaml`。

    :param user_dir: 用户级目录。**必须可选**——缺省时保持 `Path.home() / ".rhinecode"`，
                     给定时改用该目录，供装配层把整套用户级内容重定向到临时目录，
                     使测试与端到端宿主能在不污染真实主目录的前提下装配一次完整应用
                     （与 `permission/config.py`、`skills` 同形态）。
    :returns: 配置文件的绝对路径

    副作用：无（纯路径计算）。
    """
    if user_dir is not None:
        return user_dir / _CONFIG_FILE
    return Path.home() / _CONFIG_DIR_NAME / _CONFIG_FILE


def project_config_path() -> Path:
    """项目级配置路径：`<项目根>/.rhinecode/hooks.yaml`（随仓库走、可提交）。"""
    return workspace_root() / _CONFIG_DIR_NAME / _CONFIG_FILE


def scaffold_user_config(path: Path) -> bool:
    """
    在指定路径生成 Hook 配置模板，供首次运行引导使用。

    执行步骤：
    1. 目标文件已存在 → 直接返回 False，**绝不覆盖**用户已有规则（幂等、防误伤）。
    2. 创建父目录。
    3. 写入 `_CONFIG_TEMPLATE`（全注释，解析后为空规则集，行为等价于无文件）。

    :param path: 目标路径（通常是 `user_config_path()`）
    :returns: 实际写入返回 True；文件已存在未改动返回 False
    :raises OSError: 目录创建或写入失败（由调用方决定如何提示，不应阻断启动）

    副作用：可能创建目录并写入文件。
    """
    if path.exists():
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_CONFIG_TEMPLATE, encoding="utf-8")
    return True


def _load_layer(path: Path, source: str) -> tuple[list[HookRule], list[str]]:
    """
    加载单层配置文件。

    :param path: 配置文件路径
    :param source: 来源层标记，写入每条规则的 `source`
    :returns: `(规则列表, 警告列表)`

    文件不存在 → 空且无警告（这是正常状态，不是错误）。
    读取或 YAML 解析异常 → 整层降级为空 + 一条警告（fail-safe：坏配置不阻断启动）。

    副作用：读取文件系统。
    """
    if not path.exists():
        return [], []
    try:
        raw = path.read_text(encoding="utf-8")
        data = yaml.safe_load(raw)
    except Exception as exc:  # noqa: BLE001 —— 任何读/解析异常都按降级处理
        return [], [f"Hook 配置解析失败（{path}）：{exc}，该层已忽略。"]
    return parse_rules(data, source)


def load_all(user_dir: Optional[Path] = None) -> tuple[list[HookRule], list[str]]:
    """
    加载两层配置并拼接。

    :param user_dir: 用户级目录，透传给 `user_config_path()`。**必须可选**
    :returns: `(规则列表, 警告列表)`

    **返回顺序即执行顺序**（spec F9）：用户级全部规则在前，项目级全部规则在后，
    层内按声明序。这是本章唯一的顺序约定——不提供 `priority` 字段，
    因为「谁先谁后」在一个只能收紧的系统里不改变最终结论（多条命中时按最严合并），
    引入优先级只会增加一个要解释、要测试的维度。

    副作用：读取文件系统上的两个配置文件（若存在）。
    """
    rules: list[HookRule] = []
    warnings: list[str] = []
    for path, source in (
        (user_config_path(user_dir), SOURCE_USER),
        (project_config_path(), SOURCE_PROJECT),
    ):
        layer_rules, layer_warnings = _load_layer(path, source)
        rules.extend(layer_rules)
        warnings.extend(layer_warnings)
    return rules, warnings


__all__ = [
    "SOURCE_USER",
    "SOURCE_PROJECT",
    "user_config_path",
    "project_config_path",
    "scaffold_user_config",
    "load_all",
]
