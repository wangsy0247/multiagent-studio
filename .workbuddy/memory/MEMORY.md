# Multiagent-Studio 项目记忆

## 项目概要
- 全栈多智能体协作平台，基于 LangGraph + LangChain（版本 v2.0.0）
- 后端：Python (harness/ 151 文件 ~31.5K LOC + app/ 32 文件 ~2.8K LOC = ~34K LOC)
- 前端：Next.js 14 + React 18 + Zustand (39 个 TS/TSX 文件)
- 部署：Docker Compose (PostgreSQL pgvector + Redis + Nginx)
- ~38 个 REST API 端点，20+ SSE 事件类型

## 核心架构
- 20 层 AgentMiddleware 管线（洋葱模型，对齐字节 DeerFlow）
- Lead Agent + SubAgent 双层编排（ReAct 循环 + 信号量并发控制）
- Team Mode：事件驱动多 Agent 团队协作（TeamOrchestrator + TeammateAgent × N）
- L0/L1/L2 三层配置合并（系统默认 → 用户全局 → Per-Agent + SOUL.md）
- LLM 驱动长期记忆（异步 debounce 队列 + file/mem0 双后端 + DeerFlow schema）
- 上下文自动压缩（三触发条件 + 安全切割 + Dynamic Context 保护 + 摘要前 flush）
- Token 级 SSE 流式（20+ 事件类型 + 自研客户端 + 指数退避重连）
- Skill 系统（.skill ZIP 安装 + 安全扫描 + JSONL 版本管理 + 回滚）
- MCP 协议集成（stdio/SSE + 持久化 session pool + OAuth + mtime cache）
- 多层沙箱隔离（虚拟路径 + Docker 容器 + 三级审计 + Git Worktree）
- HITL 澄清（消息驱动，无自定义状态键）

## Team 模式架构（更新于 2026-07-15）
- TeamOrchestrator 四阶段：PLANNING → SPAWN → DISPATCH_LOOP → SYNTHESIS（~990 LOC）
- TeammateAgent 持久化 Agent：SPAWNING→IDLE↔WORKING→SHUTDOWN 状态机（~975 LOC）
- Watchdog：超时 30min / 死锁 2min / 循环依赖 DFS 三色标记法检测
- TeamTaskStore：JSON + fcntl.flock + 内存缓存 + mtime 校验 + DAG 依赖解析
- TeamMessageBus：per-agent JSONL inbox + drain-on-read + asyncio.Event 通知
- 15 个 Team 工具（Lead 6 / 共享 5 / Member 4），ContextVar 注入身份
- 自主认领三级优先级：强制分配 / 饥饿预防(>2min) / 领域匹配(score>=25)
- 结构化协议握手已实现：shutdown_request/response + plan_approval_request/response
- Agent Team 短期记忆：每个 TeammateAgent 独立 LangGraph checkpointer

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
