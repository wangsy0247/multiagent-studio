"""Agent Team 协作引擎 — 项目级多 Agent 编排.

提供:
- TeamTaskStore: 持久化任务板（依赖解析、原子更新）
- TeamMessageBus: per-agent inbox + drain-on-read 消息总线
- TeamOrchestrator: 事件驱动的多 Agent 编排器
- TeammateAgent: 持久化 teammate, 拥有独立 agent loop + SOUL + 中间件
- TeammateMiddleware: Teammate 中间件链构建器 (按角色分层)
- TeamContext: Team 运行时上下文数据类
"""
