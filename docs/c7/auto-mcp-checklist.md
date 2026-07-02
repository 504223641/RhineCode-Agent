# C7 MCP 自动配置补充 Checklist

- [ ] URL 输入生成 HTTP MCP 配置（验证：`tests.test_mcp_auto_config`）。
- [ ] mock NPM 搜索中 `context7` 解析到 `@upstash/context7-mcp`（验证：`tests.test_mcp_auto_config`）。
- [ ] 多个候选接近时返回 `ambiguous` 且不写配置（验证：`tests.test_mcp_auto_config`）。
- [ ] 项目级、用户级、`scope=auto` 写入正确路径（验证：`tests.test_mcp_auto_config`）。
- [ ] 坏 YAML、不合法 `mcpServers`、同名冲突且未 replace 时不覆盖（验证：`tests.test_mcp_auto_config`）。
- [ ] 同名相同配置 no-op，同名不同配置 `replace=true` 可替换（验证：`tests.test_mcp_auto_config`）。
- [ ] `reload_server` 新增工具立即注册，替换时旧工具被 unregister（验证：`tests.test_mcp_manager`）。
- [ ] 旧 stdio client 在重载时被关闭（验证：`tests.test_mcp_manager`）。
- [ ] Windows `npx` 命令可解析到 `npx.cmd` 或可执行路径（验证：`tests.test_mcp_auto_config` 或 transport 单测）。
- [ ] Agent 提示词包含“添加 MCP 用 mcp_resolve_server/mcp_add_server，不直接手写 YAML”（验证：阅读提示词或 prompt 单测）。
