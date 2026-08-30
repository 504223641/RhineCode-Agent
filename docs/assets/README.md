# docs/assets —— README 里的那几张图

## 现在有什么

| 文件 | 画的是什么 | 出现在 |
| --- | --- | --- |
| `01-chat-and-tools.svg` | 一次完整往返：思考 → 正文 → 工具调用 → 结论 | README 首屏 |
| `02-permission-panel.svg` | 人在回路的确认面板（保护路径层 ②″ 拦下写 `.rhinecode/hooks.yaml`） | README「用之前请先读这一段」 |
| `03-subagents-parallel.svg` | 两个子 Agent 并行跑，活动区各显示各自的工具调用 | README「架构一图流」段尾 |

## 这些图是怎么来的

```bash
python scripts/capture_screenshots.py            # 三张全出
python scripts/capture_screenshots.py confirm    # 只重出其中一张
```

**它们不是画的，也不是手工截的。** 脚本走产品真实的装配入口
`bootstrap.build_app`，起真实的 Textual 界面、真实的 Agent Loop、
真实的五层权限管线，然后用 Textual 自带的 `App.export_screenshot()`
把当前屏幕导成 SVG。唯一被替换掉的是**模型本身**——换成
`tests/e2e/scripted.py` 里的剧本模型，让它按写死的台词逐块吐字。

所以画面里的工具行、确认面板、活动区、状态栏，全是产品自己渲染的结果。
改了界面之后重跑一次脚本，图就跟着更新——**它们不会像手工截图那样悄悄过期**。

选 SVG 而不是 PNG 的三个理由：GitHub 直接渲染；体积只有 40 KB 上下；
里面的文字**是真文字**，能选中、能被搜索引擎读到、放大不糊。

### ⚠ 重跑之前要知道的两件事

1. **三个场景必须串行跑**（脚本本来就是串行的）。它们靠 `os.chdir` 把项目根
   切到各自的临时工作区，而工作目录与 `path_guard` 的只读白名单都是**进程级**状态。
2. **子 Agent 那张图刻意把模型放慢了**（`SlowScopedProvider.CHUNK_DELAY`）。
   剧本模型是瞬间吐完全部台词的，两个队员并行的窗口只有几毫秒；
   而活动区是**主线程轮询**刷新的（那是一条致命不变量，不能改成推送），
   窗口必须比一个轮询周期长，界面才画得出两个人。
   放慢的只是每个数据块之间的间隔——**并行是真的**，等价于把真实模型的
   吐字速度还原回来。不放慢才是失真的那一个。
