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

## Team 模式架构（分析于 2026-07-12）
- TeamOrchestrator 三阶段：PLANNING → DISPATCH_LOOP → SYNTHESIS
- Watchdog：超时 30min / 死锁 2min / 循环依赖 DFS 检测
- TeamTaskStore：文件持久化 + fcntl 文件锁 + DAG 依赖解析
- TeamMessageBus：JSONL 追加 + 游标追踪 + 实时通知
- 执行模式定义：lead_driven / user_driven / hybrid（hybrid 可能未完全实现）
- 与 learn-claude-code s17 的关键差异：本项目 Lead-Driven，s17 推崇 Autonomous 自认领
- 缺失特性（对比 s16）：未实现 shutdown/plan_approval 协议握手
- 缺失特性（对比 s15）：未实现权限冒泡（permission_request/response）
- 详细分析报告：docs/multi-agent-analysis-report.html

## 关键文件
- FRAMEWORK.md — 完整框架文档（1300+ 行）
- PLAN_REFACTOR.md — 重构计划（17→20 中间件）
- harness/main.py — HarnessService 核心编排器
- harness/agents/lead_agent.py — Lead Agent 配置提供者
- harness/agents/subagent_executor.py — SubAgent 隔离执行
- harness/agents/subagent_manager.py — SubAgent 生命周期管理
- harness/team/orchestrator.py — TeamOrchestrator 调度器
- harness/team/models.py — Team 数据模型
- harness/team/task_store.py — 持久化任务板
- harness/team/message_bus.py — Agent 间消息总线
- harness/middleware/ — 20 层中间件实现
- harness/memory/ — 记忆系统（storage/queue/updater）
