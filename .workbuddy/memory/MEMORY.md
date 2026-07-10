# Multiagent-Studio 项目记忆

## 项目概要
- 全栈多智能体协作平台，基于 LangGraph + LangChain
- 后端：Python (harness/ 133 文件, 24K+ 行) + FastAPI (app/ 业务层)
- 前端：Next.js 14 + React 18 + Zustand + ReactFlow (39 个 TS/TSX 文件)
- 部署：Docker Compose (PostgreSQL + Redis + Langfuse + Nginx)

## 核心架构
- 20 层 AgentMiddleware 管线（洋葱模型，对齐字节 DeerFlow）
- Lead Agent + SubAgent 双层编排（ReAct 循环 + 信号量并发控制）
- LLM 驱动长期记忆（异步 debounce 队列 + file/mem0 双后端）
- 上下文自动压缩（三触发条件 + 安全切割 + 摘要前 flush）
- Token 级 SSE 流式（16 种事件类型 + 自研客户端）
- Skill 系统（10 阶段生命周期 + 渐进式加载 + 安全扫描）
- MCP 协议集成（stdio/SSE + 持久化 session）
- HITL 澄清（消息驱动，无自定义状态键）

## 关键文件
- FRAMEWORK.md — 完整框架文档（1300+ 行）
- PLAN_REFACTOR.md — 重构计划（17→20 中间件）
- harness/main.py — HarnessService 核心编排器
- harness/agents/lead_agent.py — Lead Agent 配置提供者
- harness/agents/subagent_executor.py — SubAgent 隔离执行
- harness/middleware/ — 20 层中间件实现
- harness/memory/ — 记忆系统（storage/queue/updater）
