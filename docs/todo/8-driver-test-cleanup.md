# 驱动测试的收尾没放 `finally`——一条失败会变成一次永久挂起

> 状态：待开工 · **不必走 `/spec`**（改的是测试设施，不新增能力）· 预计 1–2 小时
>
> 建议分支：`e2e-teardown-hardening`（从 `main` 起）

## 症状

`tests/test_e2e_control.py`（以及 `tests/test_e2e_host.py` 里同型的那些）的
大多数用例长这样：

```python
async def test_xxx(self):
    app, provider = self.assemble(SOME_SCRIPT)
    async with app.run_test(size=(120, 40)) as pilot:
        core = self.make_core(app, pilot, asyncio.get_running_loop())
        ...
        ...一堆断言...
        await core.shutdown_on_main("test")   # ← 收尾写在函数最后一行
```

**任何一条断言失败，`shutdown_on_main` 就不会执行。** 后果不是「这条红了」，
而是**整个测试套件永久挂住**：

```
断言抛出
  → shutdown_on_main 跳过，被阻塞在确认盒 `box["event"].wait()` 上的
    工作线程永远醒不过来
  → IsolatedAsyncioTestCase 收尾调 loop.shutdown_default_executor()
  → Python 3.11 的该方法**没有超时参数、永不返回**（3.12 才加了 5 分钟默认值）
  → 卡死
```

## 为什么值得单独修

**它把「一条失败」放大成了「一次挂起 + 失败信息全丢」。**

标准 runner 把 traceback 留到最后统一打印，而这里根本走不到「最后」。
tui-activity-fold 验收期实测：全量测试连续几次「跑十几分钟不出结果」，
看到的只有一串点和一个 `F`，**不知道哪条红、更不知道为什么红**。
最后是临时写了个在 `addFailure` 里当场 flush traceback 的 runner 才定位到。

这条同时**掩盖了两个真实缺陷**（一个我引入的竞态、一处漏改的判据），
它们本可以在第一次跑全量时就被看见。

⚠ 另有一层代价：挂起期间那个 Python 进程不退出。验收期一度有三四个这样的
进程同时挂着（我起了新的全量跑却没停旧的），彼此争 CPU，把**别的**时序敏感
用例也压翻了——「偶发 flaky」里有一部分是这么来的。

## 建议做法

给 `DriverFixture` 加一个上下文管理器，把「建 core → 用 → 收尾」包起来：

```python
@asynccontextmanager
async def driving(self, app, size=(120, 40)):
    async with app.run_test(size=size) as pilot:
        core = self.make_core(app, pilot, asyncio.get_running_loop())
        try:
            yield pilot, core
        finally:
            await core.shutdown_on_main("test")
```

然后把用例逐个改成：

```python
async def test_xxx(self):
    app, provider = self.assemble(SOME_SCRIPT)
    async with self.driving(app) as (pilot, core):
        ...断言...
```

⚠ **两条要注意的**：

1. **`shutdown_on_main` 自己也可能抛。** 收尾里再抛一次会把原始断言错误盖掉
   ——那比挂起好一点，但仍然丢信息。收尾要包 `try/except` 并把异常
   **附加**到原错误上（或至少打到 stderr），不能替换它。
2. **少数用例故意在中途 `shutdown`**（验的就是关停行为，比如
   `shutdown_on_main` 的强制结算反证）。那些不能套用同一个包装，
   或者要让 `shutdown_on_main` 幂等。**先确认它幂等再动手。**

## 验证

改完之后，**故意把某条断言改成必失败**，跑全量，确认：

- 套件在合理时间内跑完（不挂起）；
- 汇总里能看到那条的 traceback；
- 把断言改回去后全量恢复全绿。

⚠ **这一步不可省。** 这个 todo 的全部价值就是「失败要看得见」，
不实际制造一次失败就验不了它。

## 一键开工 Prompt

```
读 docs/todo/8-driver-test-cleanup.md。

驱动测试（tests/test_e2e_control.py 等）的 shutdown_on_main 写在用例最后一行，
断言失败时不执行，导致工作线程醒不过来、Python 3.11 的
shutdown_default_executor() 永久挂住——一条失败变成整个套件挂起，
连 traceback 都看不到。

给 DriverFixture 加一个 async 上下文管理器把收尾放进 finally，逐个改用例。
注意两条：收尾自己抛异常时不能盖掉原始断言错误；少数故意中途 shutdown 的
用例要先确认 shutdown_on_main 幂等。

验证必须**故意制造一次失败**：确认套件不挂起、且汇总里看得到 traceback，
然后改回去跑全量确认全绿。
```
