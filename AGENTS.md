# 仓库指南

## 项目结构与模块组织

RhineCode 是一个 Python 3.11+ 项目，主包位于 `rhinecode/`。核心目录按职责划分：`agent/` 负责 Agent Loop 与提示词组装，`provider/` 负责模型后端适配，`tools/` 放置内置工具，`permission/` 实现权限引擎，`mcp/` 实现 MCP 客户端能力，`tui/` 负责 Textual 终端界面。测试位于 `tests/`，复用测试辅助代码放在 `tests/fixtures/`。阶段性规格文档位于 `docs/c*/`，通常包含 `spec.md`、`plan.md`、`task.md` 和 `checklist.md`。根目录的 `config.example.yaml`、`mcp.example.yaml`、`permissions.example.yaml` 用作配置模板。

## 构建、测试与开发命令

- `pip install -e .`：以可编辑模式安装项目，并安装 `pyproject.toml` 中声明的依赖。
- `rhine`：启动已安装的 CLI，默认读取用户级配置。
- `python -m rhinecode --config config.yaml`：开发时从源码入口启动，并指定项目配置。
- `python -m unittest discover -s tests -p "test_*.py"`：运行完整单元测试套件。

## 编码风格与命名约定

遵循常规 Python 风格：使用 4 空格缩进，模块和函数使用 `snake_case`，类名使用 `PascalCase`。公共函数或逻辑较复杂的函数应保留清晰的类型标注。当前 `pyproject.toml` 未配置格式化或 lint 工具，因此修改时优先保持与相邻代码一致。导入顺序建议为标准库、第三方库、本地包。不要大范围重写既有注释，除非确实能解释本次行为变化。

## 代码注释规范

新增或修改代码必须遵循 `CLAUDE.md` 中的“代码注释规范”。注释主体使用中文，技术名词、协议名、类名、函数名和配置键可以保留英文。关键模块、核心函数、复杂逻辑、边界处理、错误处理、数据转换、状态变化、异步流程、缓存策略和权限判断都应保留足够清晰的注释，帮助首次接触项目的开发者理解设计意图、执行流程、边界条件与安全修改方式。避免给自解释代码添加机械注释；注释优先解释“为什么这样做”、该代码承担的责任、输入输出、依赖、副作用和可能的失败场景。

## 测试指南

项目使用标准库 `unittest`。新增测试放在 `tests/test_*.py`，测试类按被测行为命名，测试方法应具体，例如 `test_deny_beats_allow`。涉及文件系统的测试应使用 `tempfile` 或 fixture，避免依赖本机固定路径。修改权限、MCP、提示词、配置或工具行为时，应补充对应回归测试。

## 提交与 Pull Request 规范

历史提交同时存在普通描述和 Conventional Commit 风格；建议优先使用简洁的带作用域格式，例如 `feat(cli): add first-run config bootstrap` 或 `docs(c7): update MCP notes`。优先使用中文commit。Pull Request 应包含行为变更摘要、已运行的测试命令与结果、相关 issue 或规格文档链接；涉及 TUI 可见变化时，应补充截图或终端说明。

## 安全与配置提示

不要提交真实密钥。`config.yaml`、`.rhinecode_debug.log`、构建产物、虚拟环境以及 `**/.rhinecode/*.local.yaml` 已被刻意忽略。共享默认配置请使用示例 YAML 文件；MCP 配置中的敏感值应通过 `${API_KEY}` 这类环境变量引用。
