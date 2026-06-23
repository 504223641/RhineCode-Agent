# RhineCode 工具系统 Checklist

> 每一项通过运行代码或观察行为验证，聚焦系统行为。

## 工具实现完整性
- [ ] 任一工具能报出 `name/description/parameters` 并 `to_schema()` 结构正确（验证：实例化打印 schema，对应 AC1）
- [ ] 读文件：存在文件返回内容；不存在路径返回 `ok=False` 不崩溃（AC2）
- [ ] 写文件：覆盖/新建后内容一致；父目录不存在能创建（AC3）
- [ ] 改文件：唯一匹配替换成功；0 次/多次返回带次数错误且文件不变（AC4）
- [ ] 执行命令：简单命令拿到 stdout 与退出码；超长命令触发超时错误且不挂死（AC5）
- [ ] 找文件：glob 命中返回列表；无匹配返回空列表非报错（AC6）
- [ ] 搜代码：命中返回文件+匹配行；无匹配返回空结果（AC7）

## 集成
- [ ] 注册中心按名查到工具，且 `schemas()` 输出含 6 个工具的 API 列表（AC8）
- [ ] DeepSeek `_to_sdk_messages` 正确转换 user/assistant(tool_calls)/tool 三类消息，arguments 序列化为 JSON 字符串
- [ ] DeepSeek 流式按 `index` 拼接 `tool_calls` 碎片，产出完整 `ToolCall`；多个调用全部解析（AC9）
- [ ] 协调层：tool_call → 执行 → 回灌 role=tool 消息 → 第二轮自动产出最终 assistant 文本 → 停（AC10）
- [ ] 协调层不再执行第二轮里模型新发的 tool_call（AC10 边界）
- [ ] 未知工具名 / arguments 解析失败 均转结构化错误回灌，流程不崩溃（AC16）

## 确认与并发
- [ ] 写/改/命令执行前弹 `ConfirmScreen` 展示工具与关键参数；选 Yes 正常执行（AC11）
- [ ] 选 No 不执行，回灌"用户拒绝执行"，模型据此回答（AC12）
- [ ] 读/找/搜 三个只读工具执行前不弹确认（AC13）
- [ ] 单响应多只读工具并发执行；多副作用工具串行执行、写操作无冲突（AC14）

## TUI 行为
- [ ] 工具执行期间历史区实时显示已耗时且为橘色；完成转绿；失败转红（AC15）
- [ ] 历史区可见工具调用（名称+关键参数）与结果摘要，与普通文本可区分（AC17）

## 编译与导入
- [ ] 全部模块可导入无错误：`python -c "import rhinecode.__main__, rhinecode.conversation, rhinecode.tools.registry, rhinecode.provider.deepseek, rhinecode.tui.app"`
- [ ] `python -m rhinecode --config nonexist.yaml` 输出友好配置错误（入口装配完整）

## 端到端场景
- [ ] 场景 1（读取并回答）：DeepSeek 配置启动，提问"读取 README/某文件并总结" → 出现 `read_file` 工具行（橘→绿，带耗时）→ 模型基于内容流式给出最终回答
- [ ] 场景 2（确认拒绝）：提问触发 `write_file`/`run_command` → 弹确认选 No → 工具不执行 → 模型回复说明被拒绝
- [ ] 场景 3（改文件唯一匹配失败）：让模型对不存在/重复的原文做 `edit_file` → 工具行变红返回匹配次数错误 → 模型可据错误调整（本轮不自动重试，符合单轮边界）
