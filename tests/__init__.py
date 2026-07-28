# 刻意留空（理由见 docs/c11/testing/p1-driver/plan.md §5）。
#
# 本文件存在的唯一目的：让 tests 成为一个 package，从而 `python -m tests.e2e.host`
# 与 `python -m tests.e2e.client` 这两个入口可用（宿主与瘦客户端都是以模块方式启动的）。
#
# **只允许 `#` 注释，不放任何 import、不放 docstring**：
# 加上本文件后 `unittest discover -s tests` 会把 tests 当包导入，
# 任何写在这里的导入都会成为全量测试的固定开销甚至副作用。
