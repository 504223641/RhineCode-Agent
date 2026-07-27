"""
③规则层的配置支撑（spec F4/F9/N1）：三层 YAML 文件的定位、加载、容错与回写。

三层配置（越靠近项目越「本地」，但求值用哲学 A 不分层级优先，见 rules.py）：
- 用户级 ~/.rhinecode/permissions.yaml          —— 跨项目的全局默认
- 项目级 <项目根>/.rhinecode/permissions.yaml   —— 随仓库走、可提交
- 本地级 <项目根>/.rhinecode/permissions.local.yaml —— 不提交；F6「永久放行」写这里

文件内容格式（人类可读、可手编）：
    allow:
      - "Bash(git *)"
      - "Read(src/**)"
    deny:
      - "Bash(git push *)"

容错原则（fail-safe，spec N1/F9）：文件缺失视为空规则集；YAML 解析失败时该层降级为
空并收集一条可读错误，**绝不**因配置坏掉而崩溃或意外放开权限。
"""

from pathlib import Path
from typing import Optional

import yaml

from rhinecode.permission.models import Rule
from rhinecode.permission.rules import RuleSet
from rhinecode.tools.path_guard import workspace_root

# 配置目录与文件名常量，集中定义便于统一调整。
_CONFIG_DIR_NAME = ".rhinecode"
_CONFIG_FILE = "permissions.yaml"
_LOCAL_FILE = "permissions.local.yaml"

# 首次运行自动生成的权限配置模板。内容**全部注释**：yaml.safe_load 一个全注释文件得到 None，
# _load_layer 对 None 返回空规则集，因此「有此模板」与「无文件」对权限系统完全等价——
# 生成它只为方便用户发现和编辑，绝不改变运行时行为（fail-safe 不变）。
_CONFIG_TEMPLATE = """\
# RhineCode 权限规则配置（可选）。
# 全部注释时等价于「无额外规则」：权限系统仍由危险命令黑名单 / 路径沙箱 /
# 权限模式 / 人在回路四层防御正常工作，本文件只是让你「额外声明」放行或拒绝。
#
# 每条写成 Tool(模式)；多层配置合并后 **deny 永远优先**（不按层级覆盖）。
# 取消下面的注释即可生效：
#
# allow:
#   - "Bash(git *)"       # 放行 git 子命令（前缀 + glob，带词边界）
#   - "Read(src/**)"      # 放行读取 src 目录（gitignore 风格路径）
# deny:
#   - "Bash(git push *)"  # 拒绝 push（deny 优先于任何 allow）
"""


def user_config_path(user_dir: Optional[Path] = None) -> Path:
    """
    用户级配置路径：~/.rhinecode/permissions.yaml（跨项目全局默认）。

    :param user_dir: 用户级目录。**必须可选**——缺省时保持原有的
                     `Path.home() / ".rhinecode"` 取值，行为与参数化之前完全一致。
                     给定时改用该目录，供装配层把整套用户级内容重定向到临时目录，
                     使测试能在不污染真实主目录的前提下装配一次完整应用
                     （trace spec F23）。
    :returns: 权限配置文件的绝对路径

    副作用：无（纯路径计算）。
    """
    if user_dir is not None:
        return user_dir / _CONFIG_FILE
    return Path.home() / _CONFIG_DIR_NAME / _CONFIG_FILE


def project_config_path() -> Path:
    """项目级配置路径：<项目根>/.rhinecode/permissions.yaml（随仓库走）。"""
    return workspace_root() / _CONFIG_DIR_NAME / _CONFIG_FILE


def local_config_path() -> Path:
    """本地级配置路径：<项目根>/.rhinecode/permissions.local.yaml（不提交，永久放行写此）。"""
    return workspace_root() / _CONFIG_DIR_NAME / _LOCAL_FILE


def scaffold_user_config(path: Path) -> bool:
    """
    在指定路径生成权限配置模板，供首次运行引导使用（与 rhinecode/config.py 同构）。

    执行步骤：
    1. 目标文件已存在 → 直接返回 False，绝不覆盖用户已有规则（幂等、防误伤）。
    2. 创建父目录（parents=True, exist_ok=True）。
    3. 写入 _CONFIG_TEMPLATE（全注释，解析后为空规则集，行为等价于无文件）。

    :param path: 目标配置文件路径（通常是 user_config_path()）
    :returns: 实际写入了模板返回 True；文件已存在未改动返回 False
    :raises OSError: 目录创建或文件写入失败时抛出（由调用方决定如何提示）

    副作用：可能创建目录并写入文件。
    """
    if path.exists():
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_CONFIG_TEMPLATE, encoding="utf-8")
    return True


def parse_rule_string(text: str, effect: str, source: str) -> Optional[Rule]:
    """
    把一条配置里的规则字符串解析成 Rule。

    支持两种写法：
    - "Bash(git *)" → tool="Bash"，pattern="git *"
    - "Bash"        → tool="Bash"，pattern=""（匹配该工具所有调用）

    :param text: 规则字符串
    :param effect: 该规则的效果，"allow" 或 "deny"（由它来自哪个 YAML 键决定）
    :param source: 来源层（user/project/local/session），仅用于原因展示与调试
    :returns: 解析出的 Rule；text 为空或非法时返回 None（调用方跳过该条）

    副作用：无。
    """
    text = (text or "").strip()
    if not text:
        return None
    if text.endswith(")") and "(" in text:
        idx = text.index("(")
        tool = text[:idx].strip()
        pattern = text[idx + 1 : -1].strip()
    else:
        tool = text
        pattern = ""
    if not tool:
        return None
    return Rule(effect=effect, tool=tool, pattern=pattern, source=source)


def _load_layer(path: Path, source: str) -> tuple[list[Rule], Optional[str]]:
    """
    加载单层配置文件，返回 (规则列表, 错误信息或 None)。

    容错（fail-safe）：
    - 文件不存在 → ([], None)：视为该层无规则，正常情况。
    - 读取/解析异常或结构非法 → ([], 可读错误)：该层降级为空，不抛异常、不放权。

    :param path: 配置文件路径
    :param source: 来源层标记，写入每条 Rule 的 source
    :returns: (该层解析出的规则, 错误信息)；无错误时第二项为 None
    """
    if not path.exists():
        return [], None
    try:
        raw = path.read_text(encoding="utf-8")
        data = yaml.safe_load(raw)
    except Exception as exc:  # noqa: BLE001 —— 任何读/解析异常都按降级处理
        return [], f"配置文件解析失败（{path}）：{exc}"

    if data is None:
        return [], None
    if not isinstance(data, dict):
        return [], f"配置文件格式应为映射（allow/deny 列表）：{path}"

    rules: list[Rule] = []
    for effect in ("allow", "deny"):
        items = data.get(effect)
        if items is None:
            continue
        if not isinstance(items, list):
            return [], f"配置项 {effect} 应为列表：{path}"
        for item in items:
            rule = parse_rule_string(str(item), effect, source)
            if rule is not None:
                rules.append(rule)
    return rules, None


def load_all(user_dir: Optional[Path] = None) -> tuple[RuleSet, list[str]]:
    """
    加载三层配置并合并成一个 RuleSet。

    逐层加载用户级 / 项目级 / 本地级，把各层规则汇总进同一个 RuleSet（合并后由
    rules.py 以 deny 优先求值，层级不决定优先级）。任一层的加载错误收集进列表一并返回，
    供上层（ConversationManager）以系统提示展示，但不阻断启动（fail-safe）。

    :param user_dir: 用户级目录，透传给 `user_config_path()`。**必须可选**——
                     缺省等于现状（读真实主目录）。既有测试全部走
                     `mock.patch(Path.home)` + 无参调用，改成必选会让它们成批失败。
    :returns: (合并后的 RuleSet, 错误信息列表)；无错误时列表为空

    副作用：读取文件系统上的三个配置文件（若存在）。
    """
    all_rules: list[Rule] = []
    errors: list[str] = []
    for path, source in (
        (user_config_path(user_dir), "user"),
        (project_config_path(), "project"),
        (local_config_path(), "local"),
    ):
        rules, err = _load_layer(path, source)
        all_rules.extend(rules)
        if err is not None:
            errors.append(err)
    return RuleSet(all_rules), errors


def append_local_allow(rule_string: str) -> None:
    """
    把一条 allow 规则追加写入本地级配置文件（spec F6「永久放行」）。

    行为：
    - 读取现有本地级 YAML；若文件损坏或顶层不是映射，则拒绝写入，避免覆盖用户内容。
    - 在 allow 列表里追加 rule_string；若已存在相同条目则跳过（幂等）。
    - 目录不存在时创建后写回，使用 UTF-8。

    :param rule_string: 形如 "Bash(git *)" 的规则字符串

    副作用：创建/写入本地级配置文件 permissions.local.yaml。
    """
    path = local_config_path()
    data: dict = {}
    if path.exists():
        try:
            loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001 —— 坏文件不覆盖，交由上层会话级规则兜底
            raise ValueError(f"本地权限配置解析失败，未写入：{path}：{exc}") from exc
        if loaded is None:
            data = {}
        elif isinstance(loaded, dict):
            data = loaded
        else:
            raise ValueError(f"本地权限配置顶层应为映射，未写入：{path}")

    allow_list = data.get("allow")
    if not isinstance(allow_list, list):
        allow_list = []
    if rule_string not in allow_list:
        allow_list.append(rule_string)
    data["allow"] = allow_list

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8")
