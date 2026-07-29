"""
`web` 包（web_fetch 扩展）：网络抓取与内容抽取的实现。

## 与 permission 的分工

**`permission` 决定「能不能去」，`web` 负责「去了之后怎么办」。**

两者唯一的耦合点是一组纯函数——`permission.network` 的 `check_hard` 与
`is_forbidden_address`。判定期（`engine.decide`）与连接期（`web.fetcher`）
**共用同一份实现**，保证两处口径不会漂移（spec F5a 硬要求）。
**不要在本包里另写一份地址判断**——那是典型的「改一处漏一处」，且漏改不报错，
只是某个地址悄悄能访问了。

## 模块划分

- `models`   —— 值对象（FetchOutcome / ExtractOutcome），纯数据
- `decode`   —— 字节 → 文本，字符集推断链
- `convert`  —— HTML → 纯文本、截断
- `fetcher`  —— 真实 HTTP 抓取 + 逐跳硬校验 + 重定向控制 + 体量上限
- `extract`  —— 抽取提示构造与结果解析（**纯逻辑，不持 provider、不发请求**）
- `render`   —— 不可信标记 + 元信息渲染 + TUI 摘要
- `manager`  —— WebFetchManager：**本包唯一持 provider 引用、唯一编排副作用的模块**

最后一条沿用 `context/manager.py` 与 `memory/manager.py` 的既有约定：
一层里只有 manager 有副作用，其余模块保持纯逻辑、可脱离网络与模型单测。

## ⚠ 本包是叶子包

不被 `permission` / `agent` / `commands` / `context` / `memory` / `skills` 反向依赖。

`tools/web_fetch.py → web → permission → tools.path_guard` 在**包级别**看是一个环。
它不成环，靠的仍是 `rhinecode/tools/__init__.py` **不 re-export 任何子模块**——
导入 `tools.path_guard` 不会连带执行 `tools/web_fetch.py`。这与既有的
`tools ↔ skills`、`tools ↔ mcp` 是同一个机制、同一条戒律。

## ⚠ 本文件只 re-export 值对象与常量

**不要 re-export `WebFetchManager`。** 叶子包的 `__init__` 不该拉起需要活 provider
的编排对象——`import rhinecode.web` 应当只是「拿两个值对象定义」这么轻的一件事，
不该顺带把 httpx 与编排层的依赖树牵进来。护栏见 checklist 第八节那条子进程断言。
"""

from rhinecode.web.models import ExtractOutcome, FetchOutcome

__all__ = ["FetchOutcome", "ExtractOutcome"]
