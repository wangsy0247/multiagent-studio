"""Agent Team 协作引擎 — 项目级多 Agent 编排。

提供:
- TeamTaskStore: 持久化任务板（依赖解析、原子更新）
- TeamMessageBus: Agent 间消息总线（JSONL 持久化 + 实时通知）
- TeamOrchestrator: 调度循环、状态机、watchdog
- MemberAgentExecutor: 封装 SubagentExecutor，注入 Team 上下文
- TeamContext: Team 运行时上下文数据类
"""
