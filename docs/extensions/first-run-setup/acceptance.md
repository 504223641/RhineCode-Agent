# 首次启动配置向导 验收记录

> 日期：2026-09-02　分支：`first-run-setup`
> 依据：`checklist.md` 的 37 条。每条分**机器判到了什么**与**据此做的判断**两栏。

## 一、总览

| | 条数 | 结果 |
| --- | --- | --- |
| 🤖 自动化可判定 | 24 | **24/24 通过** |
| 👁 需人眼看真实终端 | 13 | **待用户手测**（清单见第四节） |

新增护栏 **154 条**，分布：

| 文件 | 条数 | 盯什么 |
| --- | --- | --- |
| `test_setup_writer.py` | 40 | 定点替换、保注释、空 key 不清空、不覆盖读不出来的文件 |
| `test_setup_probe.py` | 25 | 四类失败分流、密钥打码（含真 SDK 客户端下的动态验证） |
| `test_setup_screen.py` | 21 | 四屏流转、Esc、兜底提示、markup 转义 |
| `test_setup_entry.py` | 19 | 入口三分支、`isatty` 判据、反向依赖扫描 |
| `test_setup_command.py` | 17 | `/setup` 三处接线 |
| `test_setup_catalog.py` | 15 | 兜底清单形状、与配置模板的配对 |
| `test_setup_trigger.py` | 11 | 三种触发 + 一种不触发 + 两条反证 |
| `test_setup_host.py` | 6 | 同进程连跑两个 Textual App |

全量回归：

- `python -m unittest discover -s tests` → **OK (skipped=4)，退出码 0**
- `python -m tests.run_parallel` → **3706/3706 通过**，分片条数与 discover 的期望值一致
- `python -m compileall rhinecode tests` → 无错误

## 二、自动化验收明细

### 触发与退出

| 条目 | 机器判到了什么 | 据此做的判断 |
| --- | --- | --- |
| AC1 缺文件弹向导 | `test_setup_entry.py::InteractiveTest::test_missing_config_opens_wizard` —— `run_setup.call_count == 1` | 通过 |
| AC2 占位符弹向导 | 同上 `test_placeholder_key_opens_wizard` | 通过 |
| AC3 坏 YAML 弹向导 | 同上 `test_broken_config_opens_wizard` | 通过 |
| AC4 配置可用不打扰 | `run_setup.call_count == 0` 且 `build_app.call_count == 1` | 通过 |
| AC5 非交互逐字不变 | ① `isatty=False` 时 `run_setup.call_count == 0`；② 与 `9b443ad` 逐字比对提示文案，**完全相同**；③ 退出码 0（刚生成模板）/ 1（占位符、坏配置）三种分支各一条 | 通过 |
| AC6 显式 `--config` 不弹 | `test_missing_explicit_config_is_an_error` —— 向导零次调用、退出码 1、**不擅自造文件** | 通过 |
| AC7 Esc 放弃 | `test_escape_abandons_without_writing` —— 交回 `ABANDONED` 且目标文件不存在 | 通过（真实终端上逐屏按一遍仍需手测） |
| AC8 仍然保存并继续 | `test_save_anyway_writes_and_exits` —— 校验失败后仍写盘并交回 `SAVED` | 通过 |
| AC9 保存后直接进主界面 | `test_saved_continues_into_the_app` —— 退出码 0 且 `build_app.call_count == 1` | 通过 |

### 四屏

| 条目 | 机器判到了什么 | 据此做的判断 |
| --- | --- | --- |
| AC11 key 必填 / 地址预填 | 不填 key 按下一步仍停在第二屏；地址初值为官方默认 | 通过 |
| AC12 清单来自服务端 | 注入返回 `srv-flash` / `srv-pro` 的假客户端，界面上出现的是这两个名字而**不是**兜底清单 | 通过 |
| AC13 兜底要说出来 | 注入必定失败的假客户端，界面文本同时含「兜底」「过期」与失败原因 | 通过 |
| AC14 可读原因 | 401 → 展示「密钥无效…」；反证：异常里含 `sk-secret-key` 时展示文本**不含**该串 | 通过 |

### 写盘

| 条目 | 机器判到了什么 | 据此做的判断 |
| --- | --- | --- |
| AC16 注释一行不少 | 拿真实 `_CONFIG_TEMPLATE` 连写五个字段，`#` 开头的行数不减；另有一条更硬的：写一个字段时**只允许一行不同** | 通过 |
| AC17 四字段正确 | `apply` 后经 `config.load` 读回逐字段比对 | 通过 |
| AC18 三份模板全注释 | 入口分支测试断言四份文件都存在 | 通过 |
| AC19 产物不含明文 key | ① 静态：`setup/` 与两个界面文件里没有任何 `logging` / `recorder` / `trace` 的 import；② **动态**：走真实 SDK 客户端连 `127.0.0.1:1`、全程 DEBUG 日志，跑完 grep 密钥**零命中**，且同时断言这次调用确实发生过、确实失败在网络上 | 通过 |
| 用户段落不被抹掉 | 手写 `worktree:` 段后跑 `/setup`，该段原样还在 | 通过 |

### 重跑入口

| 条目 | 机器判到了什么 | 据此做的判断 |
| --- | --- | --- |
| AC20 注册与补全 | 在注册表、是 `UI` 类型、`complete("/se")` 含 `/setup`、无重名 | 通过 |
| AC21 预填与提示 | `read_current` 的 `api_key` 恒为空串；`RERUN` 模式下 placeholder 含「不改」 | 通过 |
| AC22 留空不清空 | `test_empty_api_key_keeps_existing_one` + 界面端到端 `test_rerun_with_blank_key_keeps_key_and_sections` | 通过 |
| AC24 放弃不动文件 | `test_abandoned_says_nothing` + `test_escape_abandons_without_writing` | 通过 |

### 默认值修正

| 条目 | 机器判到了什么 | 据此做的判断 |
| --- | --- | --- |
| AC25 无残留停用别名 | `test_template_does_not_ship_a_retired_alias` —— 模板的**生效配置行**里没有 `deepseek-chat` / `deepseek-reasoner`（注释里那句说明照留） | 通过 |
| AC26 缺省窗口 1000000 | `Config.context_window == 1000000`，且 `load()` 对不写该字段的配置返回同值 | 通过 |
| AC27 文档与代码一致 | `test_docs_facts` + `test_docs_links` 全绿 | 通过 |
| 兜底清单 ↔ 模板默认模型 | `PairedWithConfigTest` 三条 | 通过 |

### 集成

| 条目 | 机器判到了什么 | 据此做的判断 |
| --- | --- | --- |
| `setup/` 无反向依赖 | 用 `ast` 解析真实 import 语句（**不是 grep**——包 docstring 里就写着那几个包名，文本搜索会被自己的说明判红），零命中 | 通过 |
| `/setup` 三处接线齐全 | 注册项 / 协议 / 实现各一条，外加签名一致性 | 通过 |
| e2e 宿主不受影响 | `tests.test_e2e_host` 在全量里通过 | 通过 |
| 既有配置护栏 | `tests.test_config_timeout` 通过 | 通过 |

## 三、实现期撞出来的四件事

都已写进对应文件的注释，这里只留索引。

**① `_render` 是 Textual 的方法名，覆盖它整个 app 起不来。**
`CLAUDE.md` 架构表 TUI 层第三条不变量说的是「新增组件的**字段名**先 `hasattr` 查一遍」。
本次字段名全查过了，恰恰漏了**方法名**——我把重绘方法命名成 `_render`，
于是 Screen 渲染自身时拿到的 visual 是 None，抛
`AttributeError: NoneType has no attribute render_strips`，
且抛在**布局阶段**、堆栈里全是 Textual 内部帧，看不出跟本文件有关（21 条用例红了 20 条）。
已改名 `_repaint`。**教训：那条不变量应当读作「字段名与方法名都要查」。**

**② `UnicodeDecodeError` 是 `ValueError` 的子类。**
于是「配置文件不是合法 UTF-8」已经落进 `trigger` 的 `INVALID` 分支。结果本身是对的，
但它带来一个写盘侧的义务：`apply` 遇到读不出来的文件**先改名备份再写**。
两处成对，只判 INVALID 而写盘直接覆盖等于把用户的配置悄悄删了。

**③ 「GBK 配置」有两种形态，只堵得住一种。**
一份 GBK 文件是不是合法 UTF-8 **取决于里面具体是哪些汉字**——
`中` 的 `D6 D0` 解不出来，`一` 的 `D2 BB` 恰好是一个合法的 UTF-8 双字节序列。
后者整份读得出来、只是全是乱码、一个错都不报，**无从察觉**，不在本扩展范围内。
（这条是被一条挂掉的用例逼出来的：原样本用「一」，根本没触发备份路径。）

**④ Windows 上 `HOME` 伪造不了用户目录。**
`Path.home()` 读的是 `USERPROFILE`。用 `HOME=...` 做冒烟测试时进程照样读到真实配置、
把 TUI 起了起来一直挂到超时。入口测试因此直接 patch `user_config_path`，不碰环境变量。

## 四、待用户手测的 13 条

自动化覆盖不到的是「界面观感、粘贴行为、等待态」这三类，以及三个需要真实环境的场景。
逐条见 `checklist.md` 里标 👁 的项，其中最要紧的是：

- **场景 1（AC28）全新机器一次跑通** —— ⚠ **必须在真的干净环境里做**：
  不能只删 `config.yaml` 再跑（那时另外三份模板还在，走到的分支不一样）。
  整个 `~/.rhinecode/` 要不存在，或临时改 `USERPROFILE`（**不是 `HOME`**，见上面第 ④ 条）。
- **场景 3 断网也能进去** —— 把第二屏地址改成一个不通的值走到第四屏，
  选「仍然保存并继续」。
- **场景 4 `/setup` 改模型后重启生效** —— 退出重启后看状态栏是不是新模型。
- **场景 5 非交互路径** —— `python -m rhinecode < /dev/null`，
  输出与退出码应与改动前一致。

⚠ **第四屏的终验会真的调一次模型**（`max_tokens=1`），花费极小但确实发生。
