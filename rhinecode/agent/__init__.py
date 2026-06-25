"""
agent 包：RhineCode 的 Agent Loop（ReAct 自主循环）与事件层。

c4 把「自主循环」从 ConversationManager 中抽出，形成独立、与界面解耦的一层：
- events.py   —— 与界面解耦的事件/枚举/数据类型（AgentEvent 等）
- collector.py—— 双路流式收集器（实时展示 + 内部累积完整响应）
- prompt.py   —— Plan Mode 引导 system prompt 构造
- plan_tools.py—— Plan Mode 特殊交互工具（ask_user / present_plan）的 schema
- loop.py     —— Agent，ReAct 循环引擎，对外只产出 AgentEvent

上层（ConversationManager）通过 Agent.run() 驱动循环，TUI 只消费 AgentEvent，
需要用户决定的交互（确认/澄清/审批）通过阻塞回调完成。
"""
