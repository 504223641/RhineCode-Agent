"""
配置加载模块。

负责从 YAML 文件中读取 LLM 供应商信息，并校验四个必填字段。
配置文件示例见项目根目录的 config.example.yaml。

注意：api_key 属于敏感信息，config.yaml 已加入 .gitignore，禁止提交到版本库。
"""

from dataclasses import dataclass
from pathlib import Path
from typing import Any
import yaml


# 用户级配置目录/文件名：与权限、MCP 配置同放 ~/.rhinecode 下，仅文件名不同。
_CONFIG_DIR_NAME = ".rhinecode"
_CONFIG_FILE = "config.yaml"

# 已知的搜索服务商（web_search 扩展 F17）。
#
# ⚠ **与 `rhinecode/web/search.py` 的 `PROVIDERS` 是成对维护点**，加第二家时两处齐改。
# 这里刻意**不 import** 那个模块：配置层依赖能力层是层级倒挂，而且
# `web/search.py` 会连带把 `web/models.py` 拉起来，只为拿一个名字清单不划算。
# 漏改的表现是「配置里写了新服务商，启动时说不认识」——会当场报错，不会静默。
_KNOWN_PROVIDERS = ("bocha", "brave")

# 模板里的占位 api_key。它是「非空字符串」，能通过 load() 的非空校验，
# 因此需要单独识别，用来区分「用户已填真实 key」和「刚生成模板还没填」。
PLACEHOLDER_API_KEY = "YOUR_API_KEY"

# 首次运行自动生成的配置模板。内容与 config.example.yaml 对齐（默认 deepseek），
# api_key 用占位符，引导用户填入真实值后再运行。
_CONFIG_TEMPLATE = """\
# RhineCode 全局配置。填入真实 api_key 后即可在任意目录运行 `rhine`。
# 也可用 `rhine --config <路径>` 指定其它配置文件覆盖本文件。

# 后端协议。本项目只支持 DeepSeek——anthropic / openai 两个 Provider
# 已于 2026-08-20 删除（它们一直停留在纯对话能力，工具调用 / Plan Mode /
# 权限系统 / Skill / 子 Agent 全都只在 deepseek 下可用）。
protocol: deepseek
model: deepseek-chat
base_url: https://api.deepseek.com
api_key: YOUR_API_KEY

# ---- 可选项（不写即用缺省值）----

# 网络访问工具（web_fetch）的总开关，缺省启用。
# 关掉之后：工具不注册、系统提示不含「外部不可信内容」约束、
# 权限规则里的 WebFetch(domain:...) 不做语法校验——行为与没有这个工具时一致。
# web_fetch_enabled: false

# ---- 子 Agent 工作区隔离（c14）----
# 声明了 isolation: worktree 的角色，每次委派会在
# <项目根>/.rhinecode/worktrees/ 下开一个独立的 Git 工作目录。
# worktree:
#   # 启动时清理多少天没动过的隔离工作区。0 或负数 = 不清理。
#   # ⚠ 有未提交改动的工作区**永远不会**被清理，不受本项设置影响；
#   #   有提交的只删目录、保留分支（成果仍可 git checkout 取回）。
#   cleanup_days: 7
#   # 建好工作区后要**复制**进去的文件（各自独立一份，改了不影响主目录）。
#   # 适合本地配置——它们被 .gitignore 排除，checkout 出来的工作区里没有。
#   copy: []
#   # 建好工作区后要**软链**进去的目录（共享同一份，省空间省时间）。
#   # 适合大型依赖目录，如 node_modules / .venv。
#   link: []

# ---- 安全审查分类器（c16）----
# auto 档下，跑命令 / 访问网络 / 给队友发消息这三类动作在执行前先问一个
# **独立的分类器模型**「这该不该做」。文件读写不进分类器（那一侧由路径沙箱
# 物理保证边界）。
#
# ⚠ 你写在 permissions.yaml 里的 deny 规则仍然压得过它；
#   你写的 allow 规则会**直接短路**它（连模型调用都不会发生）——
#   因此启用分类器时，过宽的命令放行规则（如 Bash(python *)）会被暂时丢弃，
#   启动时会逐条告诉你是哪些、为什么。
# classifier:
#   # 总开关，缺省开。关掉之后这三类回到「命令一律放行、网络每次弹面板、
#   # 队友消息直接投递」，且过宽的放行规则不再被丢弃。
#   enabled: true
#   # 分类器用哪个模型。留空 = 跟主对话同一个。跑命令是高频操作，
#   # 每次多一次往返有感，可以在这里换一个更便宜的。
#   model:
#   # 单次判定超时（秒）。超时按调用失败处理：未熔断时拒绝该次动作，
#   # 连续 3 次失败则停用分类器并改为逐次确认。
#   timeout: 10

# ---- 网络搜索（web_search）----
# 让模型能「上网找」——给一句话，返回若干条「标题 + 地址 + 摘要」。
# 要取正文仍然靠 web_fetch：搜索给路标，抓取才取货。
#
# ⚠ **你的查询词会原样发给下面这家服务商，并留在他们的日志里。**
#   web_fetch 的「只取不发」在这里不成立——搜索的本质就是把问题发出去。
#   模型在排查报错时很自然就会把项目内部的类名、模块名拼进查询词，
#   那不是滥用而是正常使用。缺省启用安全审查分类器会在每次搜索前
#   看一眼查询词，但它会误判；真正的硬边界只有两个：
#   permissions.yaml 里写 `deny: WebSearch`（不带括号），或把下面的 enabled 设为 false。
# search:
#   # 总开关，缺省开。关掉之后工具不注册、模型看不到它，行为与没有这个能力时一致。
#   enabled: true
#   # 搜索服务商，缺省 bocha。
#   #   bocha —— 博查（https://open.bochaai.com/），国内可直连，POST + Bearer
#   #   brave —— Brave Search API（境外，本机网络环境下多半连不通）
#   provider: bocha
#   # 服务商密钥。⚠ 与 api_key 同级敏感，勿提交进版本库。
#   # 不填则工具照常注册，但每次调用返回一条「未配置密钥、不要重试」的说明。
#   # 博查的密钥在 https://open.bochaai.com/ 控制台里拿。
#   api_key: YOUR_SEARCH_API_KEY
#   # 端点地址。留空 = 用服务商官方地址（博查是 https://api.bocha.cn/v1/web-search）；
#   # 填写可走内网搜索代理，或在服务商换域名时应急覆盖。
#   # 只接受 http / https，其它协议启动时直接报错；非 https 会有一条启动警告。
#   endpoint:
#   # 每次搜索返回几条（1-10）。模型可以在调用时指定，越界只夹取。
#   max_results: 5
#   # 一次会话最多搜几次。搜索通常按次计费，一个跑飞的循环能几分钟烧光额度。
#   # 达到上限后工具不报错，而是回一句「用已有信息继续」——回报错会让模型
#   # 以为是临时故障而不停重试。0 或负数 = 不限制。/clear 会把它清零。
#   session_quota: 50
#   # 单次搜索超时（秒）。超时按「服务不可用」处理。
#   timeout: 10
"""


def user_config_path() -> Path:
    """
    返回用户级全局配置文件路径 ~/.rhinecode/config.yaml。

    这是「命令未显式传 --config 时」的缺省配置位置：把 api_key 等全局设置放在
    用户主目录下的固定位置，使 `rhine` 在任意工作目录都能读到同一份配置
    （工作目录本身仍作为 AI 操作的项目根，二者互不影响）。

    :returns: ~/.rhinecode/config.yaml 的 Path（不保证文件已存在）
    """
    return Path.home() / _CONFIG_DIR_NAME / _CONFIG_FILE


def scaffold_user_config(path: Path) -> bool:
    """
    在指定路径生成配置模板，供首次运行引导使用。

    执行步骤：
    1. 若目标文件已存在，直接返回 False，绝不覆盖用户已有配置（幂等、防误伤）。
    2. 创建父目录（parents=True, exist_ok=True）。
    3. 写入 _CONFIG_TEMPLATE 模板（含占位 api_key）。

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


@dataclass
class Config:
    """
    LLM 供应商配置。

    字段说明：
    - protocol：后端协议类型，决定使用哪个 Provider 实现。目前只支持 deepseek
                （anthropic / openai 已于 2026-08-20 删除，写这两个值会在装配期
                 报错并给出迁移说明，见 provider/factory.py）
    - model：模型名称，直接传给 API（例如 deepseek-chat）
    - base_url：API 请求基础地址，支持自定义代理或私有部署
    - api_key：身份认证密钥，仅在运行时内存中使用，不打印到界面或日志
    - debug_log：是否把每次请求的缓存命中/未命中 token 追加到 <项目根>/.rhinecode_debug.log，
                 用于验证缓存策略是否生效（c5 F10）。可选字段，缺省为 True；每次请求仅写一行，
                 IO 异常会静默降级，不影响对话。不想生成该文件时在配置里设为 false。
    - context_window：上下文窗口上限（token），作为「历史是否逼近溢出」的判断基准（c8 F1）。
                 可选字段，缺省 65536；不同模型/账号窗口不同，可按需调大调小。非法或 <=0 时
                 由 load() 回退默认值，不阻断启动（fail-safe）。
    - web_fetch_enabled：网络访问工具的总开关（web_fetch 扩展 F4）。可选字段，缺省 True。
    - worktree.cleanup_days / worktree.copy / worktree.link：子 Agent 隔离工作区的
      清理阈值与环境初始化清单（c14 F10/F19）。整段可缺省。
    - search.*：网络搜索（web_search 扩展）。整段可缺省。**两种解析口径**：
      `enabled` / `provider` **非法值抛错**（它们决定「要不要把查询词发出去、
      发给谁」，静默回退会让用户以为关掉了而其实没关，或以为在用 A 而其实在用 B）；
      `max_results` / `session_quota` / `timeout` **回退默认**（调优项，
      写错了最坏是数值不对，不该阻断启动）。
                 设为 false 后：工具不注册、不出现在模型可见的工具清单里、系统提示不含
                 「外部不可信内容」那条约束、权限规则里的 `WebFetch(domain:...)` 不做语法
                 校验也不产生警告——**行为与本扩展之前逐字一致**。
                 非法值抛 ValueError（与 debug_log 同口径，见 _parse_bool；
                 注意这与 context_window 的「回退默认」是**两种**口径）。
    """
    protocol: str
    model: str
    base_url: str
    api_key: str
    # 调试日志开关：默认开启便于随时验证缓存；非必填字段，老配置不写也能正常加载。
    debug_log: bool = True
    # 上下文窗口上限（token）：c8 两层压缩据此判断是否逼近溢出；非必填，老配置不写也能加载。
    context_window: int = 65536
    # 网络访问工具总开关：缺省启用；非必填，老配置不写也能加载（web_fetch 扩展 F4）。
    web_fetch_enabled: bool = True
    # c14：隔离工作区的清理阈值（天）与环境初始化清单。
    # 三项都可缺省——不写等于「隔离工作区就是一次纯 checkout，7 天后清理」。
    worktree_cleanup_days: int = 7
    worktree_copy: tuple = ()
    worktree_link: tuple = ()
    # c16：安全审查分类器。整段可缺省 = 「开、跟主模型、10 秒」。
    classifier_enabled: bool = True
    classifier_model: str = ""
    classifier_timeout: float = 10.0
    # web_search 扩展：网络搜索。整段可缺省 = 「开、brave、没密钥、5 条、50 次、10 秒」。
    #
    # ⚠ `search_api_key` 与 `api_key` **同级敏感**：它进 trace 配置快照时必须掩码
    # （见 `trace/models.redact_config`），也不该被提交进版本库。
    search_enabled: bool = True
    search_provider: str = "bocha"
    search_api_key: str = ""
    search_endpoint: str = ""
    search_max_results: int = 5
    search_session_quota: int = 50
    search_timeout: float = 10.0
    # c16：Provider 客户端的请求超时（秒）。**缺省 None = 不传给 SDK**，
    # 行为与本章之前逐字一致。
    #
    # ⚠ **它刻意不在配置模板里**：这不是给用户调的旋钮，而是装配层给
    # **分类器专用的那个 Provider 副本**设超时用的。分类器的超时必须落到
    # SDK 客户端上——只在调用方计时是假超时，第一个数据块永远不到达时
    # 外面的计时器一点用都没有（`run_shell_captured` 那次
    # 「shell+捕获下 timeout 是假的」是同一个教训）。
    #
    # ⚠ 也刻意**不给主对话用**：主对话的一次请求可能生成几分钟
    # （模型在吐一份大文件的内容），给它设超时会把正常工作腰斩。
    request_timeout: "float | None" = None


def _parse_int(
    value: Any, field_name: str, default: int, allow_zero: bool = False
) -> int:
    """
    把配置值解析为正整数，fail-safe：非法/缺失/非正数一律回退默认值，不抛异常。

    与 _parse_bool 不同，这里刻意「不抛错」——context_window 是可选调优项，
    即便用户写错也不该阻断启动，回退到内置默认值即可保证行为稳定（c8 F1 容错）。

    :param value: 原始配置值（可能是 int、数字字符串，或任意非法值）
    :param field_name: 字段名（仅用于潜在调试，本函数不抛错故当前未用到）
    :param default: 回退默认值
    :param allow_zero: 是否接受 0 与负数（c14）。
        `worktree.cleanup_days` 用它——0 的语义是「关掉清理」，
        而不是「写错了」。缺省 False，既有调用点行为逐字不变
    :returns: 解析出的整数；无法解析时返回 default
    """
    if isinstance(value, bool):
        # bool 是 int 的子类，需先排除，避免 True 被当成 1 静默接受。
        return default
    if isinstance(value, int):
        return value if (allow_zero or value > 0) else default
    if isinstance(value, str):
        text = value.strip()
        negative = text.startswith("-") and text[1:].isdigit()
        if text.isdigit() or (allow_zero and negative):
            n = int(text)
            return n if (allow_zero or n > 0) else default
    return default


def _parse_float(value: Any, default: float) -> float:
    """
    把配置值解析为正浮点数，fail-safe：非法/缺失/非正数一律回退默认值，不抛异常（c16）。

    :param value: 原始配置值（可能是数字、数字字符串，或任意非法值）
    :param default: 回退默认值
    :returns: 解析出的浮点数；无法解析时返回 default

    与 `_parse_int` 同口径（不抛错），理由也相同：超时是可选调优项，
    写错了最坏是超时时长不对，不该阻断启动。

    ⚠ bool 要先排除：它是 int 的子类，`True` 会被静默当成 1.0 秒——
    那会让每一次分类器判定都超时，而配置文件看起来「写了个值」。

    副作用：无（纯函数）。
    """
    if isinstance(value, bool):
        return default
    if isinstance(value, (int, float)):
        return float(value) if value > 0 else default
    if isinstance(value, str):
        try:
            parsed = float(value.strip())
        except ValueError:
            return default
        return parsed if parsed > 0 else default
    return default


def _parse_str_list(value: Any) -> tuple:
    """
    把配置值解析为字符串元组，fail-safe（c14）。

    :param value: 原始配置值
    :returns: 去空白、去空项后的字符串元组；非列表或缺失时返回空元组

    **不抛错**：清单写错了最坏是「某个文件没被复制进隔离工作区」，
    用户会在子 Agent 跑不起来时发现；而抛错会让一个可选调优项阻断启动。
    非字符串项（数字、嵌套结构）静默跳过——它们不可能是合法的相对路径。
    """
    if not isinstance(value, (list, tuple)):
        return ()
    items = []
    for raw in value:
        if isinstance(raw, str) and raw.strip():
            items.append(raw.strip())
    return tuple(items)


def _parse_bool(value: Any, field_name: str) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "y", "on"}:
            return True
        if normalized in {"0", "false", "no", "n", "off"}:
            return False
    raise ValueError(f"配置字段 {field_name} 必须是布尔值 true/false")


def load(path: str) -> Config:
    """
    从指定路径加载并校验 YAML 配置文件。

    执行步骤：
    1. 打开并解析 YAML 文件
    2. 逐一检查四个必填字段是否存在且非空
    3. 构造并返回 Config 对象

    :param path: 配置文件的文件系统路径
    :returns: 填充完毕的 Config 对象
    :raises FileNotFoundError: 文件路径不存在时抛出，错误信息包含路径
    :raises ValueError: 任意必填字段缺失或为空时抛出，错误信息包含字段名

    副作用：无（纯读取，不修改任何状态）
    """
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
    except FileNotFoundError:
        raise FileNotFoundError(f"{path} 配置文件不存在")
    except yaml.YAMLError as e:
        raise ValueError(f"配置文件 YAML 解析失败 {e}") from e

    if not isinstance(data, dict):
        raise ValueError("顶层必须是 YAML 对象，且包含 protocol/model/base_url/api_key")

    # 逐字段校验，确保错误信息精确到具体缺失的字段，方便用户定位问题
    for field in ("protocol", "model", "base_url", "api_key"):
        if not data.get(field):
            raise ValueError(f"配置文件缺少必填字段 {field}")

    # debug_log 为可选项：缺省为 True，字符串写法需显式表达 true/false，避免 "false" 被当成 True。
    debug_log = _parse_bool(data.get("debug_log", True), "debug_log")

    # context_window 为可选项：缺省 65536，非法/非正值回退默认（c8 F1，见 _parse_int）。
    context_window = _parse_int(data.get("context_window", 65536), "context_window", 65536)

    # web_fetch_enabled 为可选项：缺省 True。走 _parse_bool（非法值**抛错**），
    # 与 debug_log 同口径——一个开关被写成 "maybe" 是明确的配置错误，
    # 静默回退会让用户以为自己关掉了网络访问而实际上没关。
    # 注意这与上一行 context_window 的「回退默认」是两种口径，别混。
    web_fetch_enabled = _parse_bool(data.get("web_fetch_enabled", True), "web_fetch_enabled")

    # c14：worktree 段整段可缺省。**非法结构一律回退默认、不抛错**——
    # 与 context_window 同口径而非与 web_fetch_enabled 同口径，理由是这里
    # 三项都不是安全开关：写错了最坏是「没清理」或「没复制文件」，
    # 而 web_fetch_enabled 写错会让用户以为关掉了网络访问却没关。
    worktree_raw = data.get("worktree")
    if not isinstance(worktree_raw, dict):
        worktree_raw = {}
    worktree_cleanup_days = _parse_int(
        worktree_raw.get("cleanup_days", 7), "worktree.cleanup_days", 7, allow_zero=True
    )
    worktree_copy = _parse_str_list(worktree_raw.get("copy"))
    worktree_link = _parse_str_list(worktree_raw.get("link"))

    # c16：classifier 段整段可缺省。
    #
    # ⚠ **两个字段刻意用两种口径**，别顺手统一：
    # - `enabled` 走 `_parse_bool`（**非法值抛错**），与 `web_fetch_enabled` 同口径
    #   ——它是**安全开关**，一个开关被写成 "maybe" 是明确的配置错误，
    #   静默回退会让用户以为自己关掉了分类器而实际上没关（或反过来）。
    # - `timeout` 走回退默认（**不抛错**），与 `context_window` 同口径
    #   ——它是调优项，写错了最坏是超时时长不对，不该阻断启动。
    classifier_raw = data.get("classifier")
    if not isinstance(classifier_raw, dict):
        classifier_raw = {}
    classifier_enabled = _parse_bool(
        classifier_raw.get("enabled", True), "classifier.enabled"
    )
    classifier_model = str(classifier_raw.get("model") or "").strip()
    classifier_timeout = _parse_float(
        classifier_raw.get("timeout", 10.0), 10.0
    )

    # web_search 扩展：search 段整段可缺省。
    #
    # ⚠ **两种口径是刻意的，别顺手统一**（与 classifier 段同形）：
    # - `enabled` / `provider` **抛错**——它们决定「要不要把查询词发出去、发给谁」。
    #   一个开关被写成 "maybe" 是明确的配置错误，静默回退会让用户以为自己关掉了
    #   搜索而实际上没关；服务商名写错则会让他以为数据发给了 A 而其实发给了 B
    #   （或根本发不出去）。
    # - 其余三项**回退默认**——调优项，写错了最坏是数值不对。
    search_raw = data.get("search")
    if not isinstance(search_raw, dict):
        search_raw = {}
    search_enabled = _parse_bool(search_raw.get("enabled", True), "search.enabled")
    search_provider = str(search_raw.get("provider") or "bocha").strip().lower()
    if search_provider not in _KNOWN_PROVIDERS:
        raise ValueError(
            f"search.provider 不认识的搜索服务商：{search_provider}。"
            f"目前支持：{', '.join(_KNOWN_PROVIDERS)}"
        )
    search_api_key = str(search_raw.get("api_key") or "").strip()
    search_endpoint = str(search_raw.get("endpoint") or "").strip()
    search_max_results = _parse_int(search_raw.get("max_results", 5), "search.max_results", 5)
    search_session_quota = _parse_int(
        search_raw.get("session_quota", 50), "search.session_quota", 50, allow_zero=True
    )
    search_timeout = _parse_float(search_raw.get("timeout", 10.0), 10.0)

    return Config(
        protocol=data["protocol"],
        model=data["model"],
        base_url=data["base_url"],
        api_key=data["api_key"],
        debug_log=debug_log,
        context_window=context_window,
        web_fetch_enabled=web_fetch_enabled,
        worktree_cleanup_days=worktree_cleanup_days,
        worktree_copy=worktree_copy,
        worktree_link=worktree_link,
        classifier_enabled=classifier_enabled,
        classifier_model=classifier_model,
        classifier_timeout=classifier_timeout,
        search_enabled=search_enabled,
        search_provider=search_provider,
        search_api_key=search_api_key,
        search_endpoint=search_endpoint,
        search_max_results=search_max_results,
        search_session_quota=search_session_quota,
        search_timeout=search_timeout,
    )
