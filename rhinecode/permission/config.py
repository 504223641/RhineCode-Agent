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
# c14：项目级/本地级 permissions.yaml 同理，位置固定在主项目根。
from rhinecode.tools.path_guard import main_project_root

# 配置目录与文件名常量，集中定义便于统一调整。
_CONFIG_DIR_NAME = ".rhinecode"
_CONFIG_FILE = "permissions.yaml"
_LOCAL_FILE = "permissions.local.yaml"

# 网络访问工具的规则体系名与域名模式前缀（web_fetch 扩展 spec F10）。
# 与 rules.py 的 _DOMAIN_PREFIX 是同一个字面量——两处都要认，改一处必须同步另一处。
WEB_FETCH_RULE_NAME = "WebFetch"
DOMAIN_PREFIX = "domain:"

# 「策略层」= 手写进配置的层级。只有这两层的 allow 域名规则才建立白名单（spec F6a）。
# 本地级是「永久放行」自动写入的授权记录，不算策略声明。
POLICY_SOURCES: frozenset[str] = frozenset({"user", "project"})

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
#
# 网络访问（web_fetch）的域名规则写成 WebFetch(domain:模式)：
#
# allow:
#   - "WebFetch(domain:github.com)"    # 精确，不含子域
#   - "WebFetch(domain:*.python.org)"  # 任意深度子域，但不含裸域本身
# deny:
#   - "WebFetch(domain:*.evil.com)"
#
# 网络搜索（web_search）的规则**只支持不带括号的整工具形式**：
#
# deny:
#   - "WebSearch"          # 彻底禁止模型上网搜索（任何情况下都生效）
# allow:
#   - "WebSearch"          # 放行搜索，不再逐次确认
#
# ⚠ 带括号的任何写法（WebSearch(*)、WebSearch(domain:x)、WebSearch(关键词)…）
#   **一律不生效，而且不会有任何警告**——包括 deny。想禁止搜索请写上面那条
#   不带括号的 deny，或在 config.yaml 里设 search.enabled: false。
#
# ⚠ 启用安全审查分类器（config.yaml 的 classifier.enabled，缺省开）时，
#   上面那条 allow: WebSearch 会被**丢弃**并在启动时逐条告知你——
#   因为 allow 会直接短路分类器，等于对搜索这一类关掉整层审查。
#   deny 不受影响。
#
# ⚠ 在**本文件或用户级** permissions.yaml 里写下任何一条 allow 域名规则，
#   就等于声明「只许访问这些」——此后未列出的域名一律被直接拒绝，
#   且任何权限档都翻不过来（放行档也不行）。
#   本地级 permissions.local.yaml（确认面板选「永久放行」自动写入的那份）
#   **只放行、不建立白名单**，所以在那里加一条不会锁住其它域名。
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
    return main_project_root() / _CONFIG_DIR_NAME / _CONFIG_FILE


def local_config_path() -> Path:
    """本地级配置路径：<项目根>/.rhinecode/permissions.local.yaml（不提交，永久放行写此）。"""
    return main_project_root() / _CONFIG_DIR_NAME / _LOCAL_FILE


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


def parse_rule_string(
    text: str,
    effect: str,
    source: str,
    *,
    warnings: Optional[list[str]] = None,
    web_fetch_enabled: bool = True,
) -> Optional[Rule]:
    """
    把一条配置里的规则字符串解析成 Rule。

    支持两种写法：
    - "Bash(git *)" → tool="Bash"，pattern="git *"
    - "Bash"        → tool="Bash"，pattern=""（匹配该工具所有调用）

    :param text: 规则字符串
    :param effect: 该规则的效果，"allow" 或 "deny"（由它来自哪个 YAML 键决定）
    :param source: 来源层（user/project/local/session），仅用于原因展示与调试
    :param warnings: 可选的**出参**列表；本函数发现问题时往里 append 一条中文说明。
                     传 None 表示调用方不收集警告。
    :param web_fetch_enabled: 网络访问能力是否启用。关闭时跳过 WebFetch 的 domain 语法校验，
                              使「关闭后行为与本扩展之前逐字一致」成立（spec F4）。
    :returns: 解析出的 Rule；text 为空或非法时返回 None（调用方跳过该条）

    ## ⚠ 返回类型必须保持 Optional[Rule]，警告走出参

    本函数有**三个**调用方：`config._load_layer`、`engine.persist_local_rule`、
    `skills/validation.grants_for`。后两处的写法都是
    `rule = parse_rule_string(...)` 紧跟 `if rule is not None: ...append(rule)`。
    若把返回改成 `(Rule|None, warning|None)` 元组，元组恒非 None，会把**元组本身**
    塞进 session_rules / turn_rules，下一次规则求值访问 `.effect` 时 AttributeError。
    用出参 + 默认值，「后两处不改也能跑」才成立。

    ## WebFetch 域名规则的语法校验（spec F13）

    `WebFetch(...)` 的括号内容必须以 `domain:` 开头。写坏时**按效果分两支处理，
    两支都偏严**（N1 fail-safe）：

    | 写坏的是 | 怎么处理 | 为什么 |
    |---|---|---|
    | allow | **整条丢弃** | 丢弃一条放行 = 少放行一些，偏严 |
    | deny  | **降级为整工具拒绝**（等价 `deny: WebFetch`） | 丢弃一条拒绝 = 少拦一些，偏松，不可接受。用户意图明确是「要拦」，看不懂拦什么就拦全部 |

    副作用：可能往 `warnings` 追加元素。
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

    if web_fetch_enabled and tool == WEB_FETCH_RULE_NAME and pattern:
        if not pattern.startswith(DOMAIN_PREFIX):
            if effect == "deny":
                if warnings is not None:
                    warnings.append(
                        f"权限规则 `{text}`（来源：{source}）的括号内容无法识别："
                        f"域名规则必须写成 `{WEB_FETCH_RULE_NAME}({DOMAIN_PREFIX}模式)`。"
                        f"这是一条 deny 规则，为避免「看不懂就少拦」，已**降级为拒绝该工具的全部调用**。"
                    )
                return Rule(effect=effect, tool=tool, pattern="", source=source)
            if warnings is not None:
                warnings.append(
                    f"权限规则 `{text}`（来源：{source}）的括号内容无法识别："
                    f"域名规则必须写成 `{WEB_FETCH_RULE_NAME}({DOMAIN_PREFIX}模式)`，"
                    f"该条已被丢弃。"
                    f"（提示：要建立域名白名单，请写在**用户级或项目级**配置里——"
                    f"本地级只放行、不建立白名单。）"
                )
            return None

    return Rule(effect=effect, tool=tool, pattern=pattern, source=source)


def _load_layer(
    path: Path, source: str, *, web_fetch_enabled: bool = True
) -> tuple[list[Rule], list[str]]:
    """
    加载单层配置文件，返回 (规则列表, 消息列表)。

    :param path: 配置文件路径
    :param source: 来源层标记，写入每条 Rule 的 source
    :param web_fetch_enabled: 透传给 parse_rule_string，关闭时跳过域名语法校验（spec F4）
    :returns: (该层解析出的规则, 消息列表)；无消息时第二项为空列表

    ## ⚠ 三处「整层降级」的行为一个字都不许动

    返回类型从「单个错误」改成「消息列表」，**只是为了让「单条规则级」的警告能多条并存**，
    控制流实质只影响最后一处。现有的错误 return 共三处，性质完全不同：

    | 位置 | 性质 | 行为 |
    |---|---|---|
    | YAML 解析失败 | 整文件级 | **整层降级为空并 return** |
    | 顶层非映射 | 整文件级 | **整层降级为空并 return** |
    | allow/deny 非列表 | 整字段级 | **整层降级为空并 return** |
    | 单条规则解析失败 | 单条级 | 跳过该条、继续解析其余（本来就是这样，只是补了警告出口） |

    **第三行尤其危险**：若把它改成「记下警告后 continue」，一个写成
    `deny: <不是列表>` + `allow: [一堆规则]` 的文件会变成「deny 全丢、allow 照常生效」——
    从「整层降级为空（少放行，偏严）」滑向「只丢拒绝规则（偏松）」，直接违反 N1。
    护栏见 `tests/test_perm_rule_loading.py` 里那条反证。
    """
    if not path.exists():
        return [], []
    try:
        raw = path.read_text(encoding="utf-8")
        data = yaml.safe_load(raw)
    except Exception as exc:  # noqa: BLE001 —— 任何读/解析异常都按降级处理
        return [], [f"配置文件解析失败（{path}）：{exc}"]

    if data is None:
        return [], []
    if not isinstance(data, dict):
        return [], [f"配置文件格式应为映射（allow/deny 列表）：{path}"]

    rules: list[Rule] = []
    warnings: list[str] = []
    for effect in ("allow", "deny"):
        items = data.get(effect)
        if items is None:
            continue
        if not isinstance(items, list):
            # 整字段级失败 → 整层降级为空。**不要改成 continue**（见上方表格）。
            return [], [f"配置项 {effect} 应为列表：{path}"]
        for item in items:
            rule = parse_rule_string(
                str(item),
                effect,
                source,
                warnings=warnings,
                web_fetch_enabled=web_fetch_enabled,
            )
            if rule is not None:
                rules.append(rule)
    return rules, warnings


def load_all(
    user_dir: Optional[Path] = None, *, web_fetch_enabled: bool = True
) -> tuple[RuleSet, RuleSet, list[str]]:
    """
    加载三层配置，返回「全量规则集 + 策略规则集 + 消息列表」。

    逐层加载用户级 / 项目级 / 本地级，把各层规则汇总进同一个 RuleSet（合并后由
    rules.py 以 deny 优先求值，层级不决定优先级）。任一层的加载错误/警告收集进列表
    一并返回，供上层以启动提示展示，但不阻断启动（fail-safe）。

    :param user_dir: 用户级目录，透传给 `user_config_path()`。**必须可选**——
                     缺省等于现状（读真实主目录）。既有测试全部走
                     `mock.patch(Path.home)` + 无参调用，改成必选会让它们成批失败。
    :param web_fetch_enabled: 网络访问能力是否启用；关闭时跳过域名语法校验（spec F4）
    :returns: (全量 RuleSet, **策略** RuleSet, 消息列表)

    ## 为什么返回两个 RuleSet

    第一个是全量的，用于「这次请求有没有被某条规则命中」——所有层级一视同仁、
    deny 优先，这是 c6 的既有哲学，不变。

    第二个**只含用户级 + 项目级**，唯一用途是判断「域名白名单是否已被建立」
    （web_fetch 扩展 spec F6a）。这是本项目对「层级不决定优先级」这条哲学的
    **唯一一处例外**，理由：

    - 用户级 / 项目级 YAML 是**人手写下的策略声明**。在那里写「放行 github.com」，
      合理的解读就是「我只打算让它访问这些」。
    - 本地级是**「永久放行」自动写入的授权记录**（见 append_local_allow），
      是「我批准这一个」，不是「我只允许这些」。

    不做这个区分会造成一个设计级自锁：用户在确认面板点一次「永久放行」，
    就等于建立了只含一个域名的白名单，此后其它所有域名从「弹确认」变成
    「硬拒且永不再问」，而界面上没有任何恢复手段。

    副作用：读取文件系统上的三个配置文件（若存在）。
    """
    all_rules: list[Rule] = []
    policy_rules: list[Rule] = []
    errors: list[str] = []
    for path, source in (
        (user_config_path(user_dir), "user"),
        (project_config_path(), "project"),
        (local_config_path(), "local"),
    ):
        rules, msgs = _load_layer(path, source, web_fetch_enabled=web_fetch_enabled)
        all_rules.extend(rules)
        if source in POLICY_SOURCES:
            policy_rules.extend(rules)
        errors.extend(msgs)
    return RuleSet(all_rules), RuleSet(policy_rules), errors


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
        # ⚠ 同 `mcp/auto_config.py`：下一行 `raise ... from exc` 是**重新抛出**，
        # 不是吞掉，所以**不需要**标 BLE001。语义不变——
        # 坏文件不覆盖，交由上层会话级规则兜底。
        except Exception as exc:
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
