# Agent Team 记忆系统设计

## 目录

- [一、现状分析](#一现状分析)
  - [1.1 当前记忆架构](#11-当前记忆架构)
  - [1.2 关键缺口](#12-关键缺口)
  - [1.3 现有系统详解](#13-现有系统详解)
- [二、设计目标](#二设计目标)
- [三、五层记忆架构](#三五层记忆架构)
  - [3.1 架构总览](#31-架构总览)
  - [3.2 L0 — 会话记忆](#32-l0--会话记忆-session-memory)
  - [3.3 L1 — 任务记忆](#33-l1--任务记忆-task-memory)
  - [3.4 L2 — Agent 私有记忆](#34-l2--agent-私有记忆-agent-memory)
  - [3.5 L3 — 团队记忆](#35-l3--团队记忆-team-memory)
  - [3.6 L4 — 项目记忆](#36-l4--项目记忆-project-memory)
  - [3.7 L5 — 用户记忆](#37-l5--用户记忆-user-memory)
- [四、数据流设计](#四数据流设计)
  - [4.1 团队模式完整记忆流](#41-团队模式完整记忆流)
  - [4.2 记忆检索优先级](#42-记忆检索优先级)
- [五、实现方案](#五实现方案)
  - [5.1 文件变更清单](#51-文件变更清单)
  - [5.2 实施分阶段计划](#52-实施分阶段计划)
  - [5.3 Phase 1: 任务记忆](#53-phase-1-任务记忆-task-memory)
  - [5.4 Phase 2: 团队记忆](#54-phase-2-团队记忆-team-memory)
  - [5.5 Phase 3: 项目记忆增强](#55-phase-3-项目记忆增强)
  - [5.6 Phase 4: SubAgent 记忆注入](#56-phase-4-subagent-记忆注入)
  - [5.7 Phase 5: 语义搜索](#57-phase-5-语义搜索-mem0-集成)
- [六、配置设计](#六配置设计)
- [七、安全考量](#七安全考量)
- [八、总结](#八总结)

---

## 一、现状分析

### 1.1 当前记忆架构

项目 `multiagent-studio` 的记忆系统改编自 DeerFlow，由四部分组成：

```
┌─────────────────────────────────────────────────────────────┐
│                    1. 输入层（中间件）                          │
│                                                             │
│  DynamicContextMiddleware (abefore_agent)                    │
│    ├─ 读取 memory.json → format_memory_for_injection()       │
│    └─ 将 <memory> 块注入到第一条 HumanMessage                 │
│                                                             │
│  MemoryMiddleware (aafter_agent)                             │
│    └─ 过滤最新交换 → 排队到 MemoryUpdateQueue                  │
│                                                             │
│  memory_search 工具（mem0_tool_enabled）                      │
│    └─ 允许 agent 主动查询 mem0                               │
├─────────────────────────────────────────────────────────────┤
│                    2. 处理层（去抖 + LLM）                      │
│                                                             │
│  MemoryUpdateQueue                                           │
│    └─ asyncio 去抖 → 批量处理                                  │
│                                                             │
│  MemoryUpdater                                               │
│    ├─ MEMORY_UPDATE_PROMPT → LLM 提取                        │
│    ├─ 事实去重、TTL 过期、max_facts 截断                       │
│    └─ 双写：file + mem0（取决于配置）                           │
├─────────────────────────────────────────────────────────────┤
│                    3. 存储层                                  │
│                                                             │
│  FileMemoryStorage                                           │
│    └─ ~/.multiagent-studio/users/{uid}/memory.json           │
│       ~/.multiagent-studio/users/{uid}/agents/{name}/        │
│           memory.json                                        │
│                                                             │
│  mem0 客户端                                                  │
│    └─ mem0.add() / mem0.search() / Chroma 向量存储           │
│                                                             │
│  ProjectMemoryStorage (仅团队模式)                             │
│    └─ {project_root}/memory/description.md                    │
├─────────────────────────────────────────────────────────────┤
│                    4. 安全层                                  │
│                                                             │
│  safety.py                                                   │
│    ├─ 提示注入检测                                            │
│    ├─ 凭据窃取检测                                            │
│    └─ 不可见 Unicode 拦截                                     │
└─────────────────────────────────────────────────────────────┘
```

### 1.2 关键缺口

当前记忆系统实际上是 **5 层中的 3 层**：

```
L5 用户记忆   ←──── ✅ 已实现 (memory.json per user)
     ↑
L4 项目记忆   ←──── ⚠️ 部分实现 (description.md 只读, 不演进)
     ↑
L3 团队记忆   ←──── ❌ 缺失 (agent team 最需要的共享记忆)
     ↑
L2 Agent私有  ←──── ✅ 已实现 (per-agent memory.json)
     ↑
L1 任务记忆   ←──── ❌ 缺失 (task.output 有文本但无结构化记忆)
     ↑
L0 会话记忆   ←──── ✅ 已实现 (LangGraph checkpointer)
```

| 缺口 | 影响 | 严重程度 |
|------|------|---------|
| **无团队共享记忆** | Agent A 学到的东西 Agent B 不知道，下次合作从零开始 | 🔴 高 |
| **任务记忆缺失** | 任务失败后重试没有上下文，不知道为什么上次失败了 | 🔴 高 |
| **项目记忆静态** | 项目演进后 memory 不更新，agents 基于过时信息决策 | 🟡 中 |
| **SubAgent 无记忆** | SubAgent 每次都是"失忆"状态，完全依赖 instruction | 🟡 中 |
| **无记忆检索** | 只能 dump 全部事实到 prompt，token 浪费且信息过载 | 🟡 中 |
| **无决策追踪** | 不知道为什么选了方案 A 而不是 B，无法回溯 | 🟢 低 |

### 1.3 现有系统详解

#### 1.3.1 记忆注入流程

`DynamicContextMiddleware`（位于 `harness/middleware/dynamic_context.py`）在 `abefore_agent` 时注入记忆：

- **首轮**：构建完整 `<system-reminder>`，包含 `<memory>`（用户上下文、历史、事实）和 `<current_date>`
- **团队模式**：额外注入 `<project_memory>`（来自 `description.md`）
- **同一天后续轮次**：跳过注入（快照已在历史中）
- **跨天**：重新注入

注入格式：

```xml
<system-reminder>
<project_memory>项目架构、约定...</project_memory>
<memory>
User Context:
- Work: ...
- Personal: ...
- Current Focus: ...
- Avoid: ...
History:
- Recent: ...
- Earlier: ...
- Background: ...
Facts:
- [preference | 0.95] User prefers Chinese
</memory>
<current_date>2026-07-27, Monday</current_date>
</system-reminder>
```

#### 1.3.2 记忆更新流程

`MemoryMiddleware`（`harness/middleware/memory.py`）在 `aafter_agent` 时：

1. 过滤消息（仅保留用户输入 + 最终 AI 回复）
2. 提取最新 `(human, ai)` 交换对
3. 检测纠正/强化信号
4. 排队到 `MemoryUpdateQueue`

`MemoryUpdateQueue`（`harness/memory/queue.py`）：

- asyncio 去抖（默认 120s）
- 同一 `(thread_id, user_id, agent_name)` 的多次入队合并

`MemoryUpdater`（`harness/memory/updater.py`）：

- 调用 LLM（默认 `gpt-4o-mini`）提取事实
- 事实去重、置信度阈值（0.7）、TTL 过期（90 天）、max_facts 截断
- 双写：file + mem0（取决于配置）

#### 1.3.3 存储结构

用户记忆文件 `memory.json` 结构：

```json
{
  "version": "1.0",
  "lastUpdated": "2026-07-27T10:00:00Z",
  "user": {
    "workContext": {"summary": "...", "updatedAt": "..."},
    "personalContext": {"summary": "...", "updatedAt": "..."},
    "topOfMind": {"summary": "...", "updatedAt": "..."},
    "avoidances": {"summary": "...", "updatedAt": "..."}
  },
  "history": {
    "recentWeeks": {"summary": "...", "updatedAt": "..."},
    "earlierContext": {"summary": "...", "updatedAt": "..."},
    "longTermBackground": {"summary": "...", "updatedAt": "..."}
  },
  "facts": [
    {
      "id": "fact_abc123",
      "content": "User prefers communicating in Chinese",
      "category": "preference",
      "confidence": 0.95,
      "createdAt": "2026-07-20T08:00:00Z",
      "source": "thread_xxx",
      "sourceError": null
    }
  ]
}
```

#### 1.3.4 团队模式下的记忆现状

在团队模式下，每个 `TeammateAgent` 拥有独立的中间件链（通过 `teammate_middleware.py` 的 `build_teammate_middlewares()` 构建，共 17-18 层）：

- 位置 #11：`MemoryMiddleware`（记忆更新）
- `DynamicContextMiddleware`（记忆注入，含项目上下文）
- 每个 teammate 有自己的 `agent_name`，用于作用域限定其记忆文件

**缺失的部分**：

- 没有团队级别的共享记忆（Team A 的发现对 Team B 不可见）
- 任务完成后没有结构化的经验提取
- `ProjectMemoryStorage` 只加载静态 `description.md`，不演进
- SubAgent 没有任何记忆注入

---

## 二、设计目标

1. **分层记忆**：每层有明确的 scope（作用域）、生命周期、读写权限
2. **自动提取 + 主动查询**：既有中间件被动注入，也有 agent 通过工具主动搜索
3. **共享 + 私有**：团队有共享记忆池，每个 agent 也有私有记忆
4. **演进式**：记忆随项目演进而更新，不是一次性写入、永不改变
5. **渐进实现**：先做 ROI 最高的层，逐步完善，每层可独立上线
6. **Token 预算可控**：每层记忆注入有明确的 token 上限，总量可控

---

## 三、五层记忆架构

### 3.1 架构总览

```
┌──────────────────────────────────────────────────────────────────┐
│                        记忆系统五层架构                             │
├──────────────────────────────────────────────────────────────────┤
│                                                                  │
│  ┌─────────────────────────────────────────────────────────┐    │
│  │ L5 用户记忆 (User Memory)                                │    │
│  │ Scope: 跨项目、跨会话                                      │    │
│  │ 内容: 用户偏好、工作背景、长期历史、个人 facts               │    │
│  │ 存储: ~/.multiagent-studio/users/{uid}/memory.json        │    │
│  │ 更新: MemoryMiddleware → MemoryUpdater (LLM 自动提取)      │    │
│  │ 注入: DynamicContextMiddleware → 所有 agent               │    │
│  │ 状态: ✅ 已实现                                            │    │
│  └─────────────────────────────────────────────────────────┘    │
│                          │                                       │
│  ┌───────────────────────┼──────────────────────────────────┐    │
│  │ L4 项目记忆 (Project Memory)                              │    │
│  │ Scope: 单个项目、所有团队成员                               │    │
│  │ 内容: 项目架构、技术决策、约定、已知问题、代码模式            │    │
│  │ 存储: {project_root}/memory/ (多层文件)                    │    │
│  │ 更新: TeamOrchestrator + agent 工具 (混合)                 │    │
│  │ 注入: DynamicContextMiddleware → 团队成员                  │    │
│  │ 状态: ⚠️ 部分实现 (仅 description.md 只读, 需增强)         │    │
│  └─────────────────────────────────────────────────────────┘    │
│                          │                                       │
│  ┌───────────────────────┼──────────────────────────────────┐    │
│  │ L3 团队记忆 (Team Memory)          ← 🆕 本次设计重点       │    │
│  │ Scope: 单个项目内的 agent team                             │    │
│  │ 内容: 协作经验、成员能力认知、任务分配模式、沟通记录摘要       │    │
│  │ 存储: {project_root}/memory/team_memory.json               │    │
│  │ 更新: TeamOrchestrator synthesis 阶段自动提取               │    │
│  │ 注入: TeamMemoryMiddleware → 所有团队成员                   │    │
│  │ 状态: ❌ 全新设计                                           │    │
│  └─────────────────────────────────────────────────────────┘    │
│                          │                                       │
│  ┌───────────────────────┼──────────────────────────────────┐    │
│  │ L2 Agent 私有记忆 (Agent Memory)                           │    │
│  │ Scope: 单个 agent (跨项目)                                 │    │
│  │ 内容: agent 自身学习、工具使用技巧、常见错误修正             │    │
│  │ 存储: {data_root}/users/{uid}/agents/{name}/memory.json    │    │
│  │ 更新: MemoryMiddleware → MemoryUpdater (per-agent LLM)     │    │
│  │ 注入: DynamicContextMiddleware → 仅该 agent                │    │
│  │ 状态: ✅ 已实现                                            │    │
│  └─────────────────────────────────────────────────────────┘    │
│                          │                                       │
│  ┌───────────────────────┼──────────────────────────────────┐    │
│  │ L1 任务记忆 (Task Memory)          ← 🆕 本次设计重点       │    │
│  │ Scope: 单个任务 (task_id)                                  │    │
│  │ 内容: 任务推理链、决策理由、失败原因、成功方案、依赖上下文    │    │
│  │ 存储: {project_root}/memory/tasks/{task_id}.json           │    │
│  │ 更新: TeammateAgent 任务完成/失败时自动保存                  │    │
│  │ 注入: 分配相关任务时注入给 agent                            │    │
│  │ 状态: ❌ 全新设计                                           │    │
│  └─────────────────────────────────────────────────────────┘    │
│                          │                                       │
│  ┌───────────────────────┼──────────────────────────────────┐    │
│  │ L0 会话记忆 (Session Memory)                               │    │
│  │ Scope: 单次对话 (thread_id)                                │    │
│  │ 内容: 对话历史、tool call 结果、状态快照                     │    │
│  │ 存储: deerflow.db (LangGraph checkpointer)                 │    │
│  │ 更新: LangGraph 自动                                       │    │
│  │ 注入: 消息列表直接传递                                      │    │
│  │ 状态: ✅ 已实现                                            │    │
│  └─────────────────────────────────────────────────────────┘    │
│                                                                  │
└──────────────────────────────────────────────────────────────────┘
```

### 3.2 L0 — 会话记忆 (Session Memory)

**状态**：✅ 已实现，无需改动。

| 属性 | 说明 |
|------|------|
| Scope | 单次对话 (`thread_id`) |
| 生命周期 | 对话进行中；checkpoint 持久化到数据库后可跨会话恢复 |
| 内容 | 对话历史（HumanMessage、AIMessage、ToolMessage）、工具调用结果、中间件注入的动态上下文 |
| 存储 | `deerflow.db` → `checkpoints` 表 + `writes` 表，通过 `thread_id` 索引 |
| 更新方式 | LangGraph 在每次节点转换后自动 checkpoint |
| 注入方式 | 消息列表直接作为 LLM 输入 |
| 读写权限 | LangGraph 引擎内部读写，不对外暴露直接修改 |

**与记忆系统的关系**：会话记忆是记忆提取的**数据源**。`MemoryMiddleware` 在 `aafter_agent` 时从会话记忆（消息列表）中提取 `(human, ai)` 交换对，送入 `MemoryUpdateQueue`，最终由 LLM 从会话中提取长期事实写入 L2/L5。

### 3.3 L1 — 任务记忆 (Task Memory) 🆕

**为什么需要**：当前 `TeamTask` 有 `output` 字段（一段文本），但没有结构化记忆。当类似任务再次出现时，agent 无法知道：

- 上次是怎么做这个任务的？
- 遇到了什么坑？
- 为什么选择了方案 A 而不是 B？
- 有哪些发现可以复用？

#### 存储结构

文件路径：`{project_root}/memory/tasks/{task_id}.json`

```json
{
  "task_id": "a1b2c3d4",
  "title": "实现用户登录功能",
  "status": "completed",
  "assigned_agent": "backend-dev",
  "created_at": "2026-07-27T10:00:00Z",
  "completed_at": "2026-07-27T10:30:00Z",

  "summary": "使用 JWT + bcrypt 实现了登录 API，包含 token 刷新机制",

  "decisions": [
    {
      "question": "JWT 存储方案？",
      "options": ["httpOnly cookie", "localStorage", "memory"],
      "chosen": "httpOnly cookie",
      "reasoning": "安全性考虑，防止 XSS 窃取",
      "tradeoffs": "需要 CSRF 保护"
    }
  ],

  "pitfalls": [
    {
      "problem": "bcrypt hash 在 SQLite 上性能差",
      "solution": "降低 rounds 从 12 到 10，对 SQLite 可接受",
      "would_redo": "生产环境用 PostgreSQL"
    }
  ],

  "discoveries": [
    "项目的 User 模型已经包含 hashed_password 字段，不需要新建表",
    "auth middleware 在 app/api/deps.py 中已实现，可直接复用"
  ],

  "dependency_context": {
    "task_id": "setup-db",
    "what_was_needed": "User 表结构和索引",
    "what_was_available": "完整的 SQLAlchemy 模型，WAL 模式已启用"
  },

  "tags": ["auth", "jwt", "api", "security", "backend"],
  "embedding": null
}
```

#### 字段说明

| 字段 | 类型 | 说明 |
|------|------|------|
| `summary` | `str` | 2-3 句话的执行摘要 |
| `decisions` | `list[Decision]` | 关键决策及其理由、备选方案、权衡 |
| `pitfalls` | `list[Pitfall]` | 遇到的问题、解决方案、如果重做会怎么做 |
| `discoveries` | `list[str]` | 对后续任务有价值的发现 |
| `dependency_context` | `dict` | 依赖任务的上下文（需要什么、实际有什么） |
| `tags` | `list[str]` | 用于相似任务匹配的标签 |
| `embedding` | `list[float] \| null` | 可选，语义搜索用 |

#### 生命周期

```
任务创建 (PENDING)
    │
    ▼
任务认领 (IN_PROGRESS)  ← 此时注入相关历史任务记忆
    │
    ▼
任务完成 (COMPLETED / FAILED)
    │
    ▼
TeammateAgent._handle_task_completion()
    │
    ├─ 1. 收集对话上下文 (HumanMessage + AIMessage + ToolMessage)
    ├─ 2. 调用轻量 LLM (gpt-4o-mini) + TASK_MEMORY_UPDATE_PROMPT
    ├─ 3. 生成结构化 TaskMemory
    └─ 4. 保存到 {project_root}/memory/tasks/{task_id}.json
```

#### 检索方式

| 方法 | 触发时机 | 策略 |
|------|---------|------|
| **标签匹配** | `assign_task()` 时 | Jaccard 相似度匹配 tags，取 top_k 个 |
| **依赖关联** | 任务有 `dependencies` 时 | 直接加载依赖任务的记忆 |
| **语义搜索** | `memory_search` 工具调用时 | mem0 向量搜索（Phase 5） |

#### 注入格式

在 `assign_task()` 时，通过 `TaskMemoryMiddleware` 注入：

```xml
<task_memory>
以下是与当前任务相关的历史任务经验，供参考：

相关任务 1: "添加 JWT 认证" (task_xyz, 完成于 2026-07-20)
- 决策: 使用 httpOnly cookie 存储 token，而非 localStorage
- 发现: User 模型已有 hashed_password 字段
- 踩坑: SQLite 上 bcrypt rounds 需降至 10

相关任务 2: "数据库迁移 PG → SQLite" (task_abc, 完成于 2026-07-25)
- 发现: SQLAlchemy UUID 在 SQLite 中不带横线存储
- 踩坑: 所有 UUID 查询需要显式转换
</task_memory>
```

### 3.4 L2 — Agent 私有记忆 (Agent Memory)

**状态**：✅ 已实现，无需改动。

| 属性 | 说明 |
|------|------|
| Scope | 单个 agent，跨项目 |
| 生命周期 | 持久化，TTL 90 天自动清理 |
| 内容 | Agent 自身学习、工具使用技巧、常见错误修正、该 agent 的用户偏好 |
| 存储 | `{data_root}/users/{uid}/agents/{name}/memory.json` |
| 更新方式 | `MemoryMiddleware` → `MemoryUpdateQueue` → `MemoryUpdater`（LLM 自动提取） |
| 注入方式 | `DynamicContextMiddleware`，仅注入给该 agent |
| 读写权限 | 仅该 agent 读写；其他 agent 不可见 |

**与团队记忆的区别**：L2 是 agent **私有的**、**跨项目**的学习。例如 agent "backend-dev" 在所有项目中都会记得 "SQLite 不支持 UUID 自动转换"。L3 团队记忆是 **共享的**、**项目内**的经验。

### 3.5 L3 — 团队记忆 (Team Memory) 🆕

**为什么需要**：当前团队每次都是"初次见面"：

- Agent A 不知道 Agent B 擅长什么（`AgentCard` 是静态定义，不是实际表现）
- 不知道上次谁处理了类似任务
- 不知道团队整体的协作模式
- 上次合作中发现的坑这次可能再踩一遍

#### 存储结构

文件路径：`{project_root}/memory/team_memory.json`

```json
{
  "version": "1.0",
  "last_updated": "2026-07-27T10:30:00Z",
  "updated_by": "__team_lead__",

  "member_profiles": {
    "backend-dev": {
      "strengths": ["API 设计", "数据库优化", "测试编写详尽"],
      "weaknesses": ["前端 CSS 不熟悉"],
      "best_for": ["后端 API", "数据模型", "认证授权"],
      "not_for": ["UI 组件", "样式调整"],
      "collaboration_notes": "倾向于先写测试再实现，输出包含完整测试用例",
      "success_rate": 0.95,
      "avg_completion_time_minutes": 25
    }
  },

  "collaboration_patterns": [
    {
      "pattern": "backend-dev 实现 API → frontend-dev 对接接口",
      "agents_involved": ["backend-dev", "frontend-dev"],
      "effectiveness": "high",
      "when_to_use": "全栈功能开发",
      "lessons": "backend-dev 需要提前给出 API 文档让 frontend-dev 并行开发"
    }
  ],

  "best_practices": [
    {
      "id": "bp_001",
      "practice": "数据库迁移前先备份 SQLite 文件",
      "discovered_by": "backend-dev",
      "source_task": "migrate-pg-to-sqlite",
      "importance": "critical",
      "created_at": "2026-07-25T14:00:00Z"
    }
  ],

  "known_pitfalls": [
    {
      "id": "pf_001",
      "pitfall": "SQLite 不支持 UUID 类型的自动转换，所有涉及 UUID 列的查询都需要显式转换",
      "context": "PostgreSQL → SQLite 迁移后遗症",
      "discovered_in": "fix-uuid-crash",
      "affected_components": ["execute.py", "auth.py", "threads.py"],
      "created_at": "2026-07-25T15:00:00Z"
    }
  ],

  "project_state": {
    "phase": "开发阶段",
    "last_major_change": "PostgreSQL → SQLite 迁移完成",
    "technical_debt": [
      "迁移脚本需要错误处理改进",
      "app.db 有多份副本需要统一"
    ],
    "next_priorities": ["记忆系统设计"],
    "updated_at": "2026-07-27T10:30:00Z"
  },

  "communication_summary": {
    "last_session_highlights": [
      "backend-dev 和 __team_lead__ 协作解决了 UUID 格式问题",
      "确认了 3 个 app.db 文件的重复问题"
    ],
    "updated_at": "2026-07-27T10:30:00Z"
  }
}
```

#### 更新时机

1. **Synthesis 阶段自动提取**（主路径）：`TeamOrchestrator._llm_synthesize()` 完成后，调用轻量 LLM + `TEAM_MEMORY_UPDATE_PROMPT`，从本会话完成的所有任务中提取团队级发现
2. **增量合并**：不重写整个文件，而是合并新发现（去重 `best_practices`、`known_pitfalls`；更新 `member_profiles` 的统计数据）
3. **衰退机制**：超过 90 天且未被引用的条目自动降低权重或移除

#### 注入方式

新建 `TeamMemoryMiddleware`，在 `abefore_agent` 时注入，紧跟在 `<project_memory>` 之后：

```xml
<team_memory>
成员能力认知:
- backend-dev: 擅长 API 设计、数据库优化 | 不擅长前端 CSS
- frontend-dev: 擅长 React 组件、样式 | 不擅长后端逻辑

团队最佳实践:
- [critical] 数据库迁移前先备份 SQLite 文件
- [high] 修改 execute.py 后必须测试完整的 SSE 流

已知的坑:
- SQLite UUID 查询需要显式转换 (影响 execute.py, auth.py)

项目状态: 开发阶段 | 最近: PG→SQLite 迁移完成
</team_memory>
```

### 3.6 L4 — 项目记忆 (Project Memory)

**状态**：⚠️ 部分实现，需增强。

**当前**：只有 `description.md`，手动编写、静态不演进。

**增强后**：

```
{project_root}/memory/
├── description.md          ← 人工编写 (L0, 始终加载)
├── architecture.md         ← 🆕 自动生成/更新
├── conventions.md          ← 🆕 自动提取
├── codebase_summary.md     ← 🆕 自动生成
├── team_memory.json        ← L3 团队记忆 (自动)
├── tasks/                  ← L1 任务记忆目录 (自动)
│   ├── task_a1b2c3d4.json
│   └── task_e5f6g7h8.json
└── decisions/              ← 🆕 重大决策记录 (半自动)
    └── 2026-07-27-db-migration.md
```

#### 自动生成的文件

| 文件 | 生成方式 | 更新时机 |
|------|---------|---------|
| `architecture.md` | Lead Agent 扫描项目结构生成 | 项目初始化 / 重大变更后 |
| `conventions.md` | 从任务记忆中提取规律（如 "API 端点统一放在 app/api/ 下"） | 累计 5+ 相关发现时 |
| `codebase_summary.md` | Agent 扫描代码库生成摘要（框架、入口文件、关键模块） | 按需，或项目初始化时 |
| `decisions/*.md` | 从任务记忆中提取标记为 "重要" 的决策 | 任务完成时 |

#### 加载策略

沿用现有 `ProjectMemoryStorage` 的渐进式加载：

- **L0**（始终加载）：`description.md` → 注入到 `<project_memory>`
- **L1**（按需加载）：`architecture.md`、`conventions.md` → 通过 `memory_search` 工具或 agent 判断需要时主动加载
- **L2**（工具访问）：`decisions/*.md`、`tasks/*.json` → 通过 `memory_search` 工具查询

### 3.7 L5 — 用户记忆 (User Memory)

**状态**：✅ 已实现，无需改动。

| 属性 | 说明 |
|------|------|
| Scope | 跨项目、跨会话、跨 agent |
| 生命周期 | 持久化，TTL 90 天自动清理 |
| 内容 | 用户偏好（语言、沟通风格）、工作背景、长期历史、行为模式 |
| 存储 | `~/.multiagent-studio/users/{uid}/memory.json` |
| 更新方式 | `MemoryMiddleware` → `MemoryUpdateQueue` → `MemoryUpdater`（LLM 自动提取） |
| 注入方式 | `DynamicContextMiddleware`，注入给所有 agent（Lead + Member + SubAgent） |
| 读写权限 | 所有 agent 可读；仅 `MemoryUpdater` 写入 |

---

## 四、数据流设计

### 4.1 团队模式完整记忆流

```
用户消息 "实现用户登录功能"
    │
    ▼
TeamOrchestrator.run()
    │
    ├─ [Phase 0: Triage]
    │   Lead Agent 收到:
    │   ┌──────────────────────────────────────────┐
    │   │ <system-reminder>                         │
    │   │   <project_memory>                        │  ← L4: description.md
    │   │     architecture: FastAPI + SQLite + React │
    │   │     conventions: RESTful API, JWT auth    │
    │   │   </project_memory>                       │
    │   │   <team_memory>                           │  ← L3: team_memory.json
    │   │     成员: backend-dev 擅长 API 设计          │
    │   │     上次类似: "添加 JWT 认证" 已完成         │
    │   │     团队约定: API 先写测试                  │
    │   │   </team_memory>                          │
    │   │   <memory>                                │  ← L5: 用户 memory
    │   │     User prefers Chinese                  │
    │   │     Work: 全栈开发, Python + TypeScript    │
    │   │   </memory>                               │
    │   │   <current_date>2026-07-27</current_date> │
    │   └──────────────────────────────────────────┘
    │
    │   Lead 决策: 拆解为 2 个子任务 →
    │     任务 A: "实现 /api/auth/login 端点" → 分配给 backend-dev
    │     任务 B: "实现前端登录表单" → 分配给 frontend-dev
    │
    ├─ [Phase 2: Dispatch]
    │
    │   backend-dev 收到任务 A:
    │   ┌──────────────────────────────────────────┐
    │   │ <system-reminder>                         │
    │   │   <project_memory>...</project_memory>    │  ← L4
    │   │   <team_memory>...</team_memory>          │  ← L3
    │   │   <task_memory>                           │  ← L1: 相关历史任务
    │   │     上次 "添加 JWT 认证" (task_xyz)        │
    │   │       - 决策: httpOnly cookie > localStorage│
    │   │       - 发现: User 模型已有 hashed_password│
    │   │       - 坑: SQLite bcrypt rounds 要调低    │
    │   │   </task_memory>                          │
    │   │   <memory>                                │  ← L2: backend-dev 私有
    │   │     后端工具: FastAPI, SQLAlchemy, bcrypt   │
    │   │     编码偏好: 先写 pydantic schema          │
    │   │   </memory>                               │
    │   │   <memory>                                │  ← L5: 用户记忆 (共享)
    │   │     User prefers Chinese                  │
    │   │   </memory>                               │
    │   │ </system-reminder>                        │
    │   └──────────────────────────────────────────┘
    │
    │   backend-dev 执行中:
    │   - 使用 memory_search 工具查找 mem0 中是否已有相关经验
    │   - 完成实现, 记录决策和发现
    │
    ├─ [Phase 3: Synthesis]
    │   Lead 汇总结果 →
    │
    │   自动触发记忆更新:
    │   ┌────────────────────────────────────────────────┐
    │   │ 1. 任务记忆保存 (L1)                             │
    │   │    tasks/task_a.json ← backend-dev 的决策和发现  │
    │   │    tasks/task_b.json ← frontend-dev 的执行记录   │
    │   │                                                 │
    │   │ 2. 团队记忆更新 (L3)                             │
    │   │    - backend-dev 成功完成 API 认证任务            │
    │   │    - 协作模式: backend→frontend 有效             │
    │   │    - 新最佳实践: "先定义 API schema 再并行开发"   │
    │   │                                                 │
    │   │ 3. 项目记忆更新 (L4) (条件触发)                   │
    │   │    - conventions.md: 记录了认证端点约定           │
    │   │    - architecture.md: 如有重大变更则更新          │
    │   │                                                 │
    │   │ 4. Agent 私有记忆更新 (L2)                       │
    │   │    - backend-dev: 更新了工具使用技巧              │
    │   │    - 通过现有 MemoryMiddleware 自动处理           │
    │   │                                                 │
    │   │ 5. 用户记忆更新 (L5)                              │
    │   │    - 通过现有 MemoryMiddleware 自动处理           │
    │   └────────────────────────────────────────────────┘
    │
    ▼
最终响应 + SSE 事件流
```

### 4.2 记忆检索优先级

当 agent 处理任务时，记忆注入遵循以下优先级和 token 预算（总计控制在 ~3000 tokens 内）：

| 优先级 | 记忆层 | Token 预算 | 策略 |
|--------|--------|-----------|------|
| 1 | L4 项目记忆 (L0) | 500 | 始终注入 `description.md` |
| 2 | L3 团队记忆 | 800 | 注入最相关的条目（按重要性排序） |
| 3 | L1 任务记忆 | 600 | 标签匹配最近 3 个相关任务 |
| 4 | L2 Agent 私有 | 500 | 注入该 agent 的 top facts（按置信度排序） |
| 5 | L5 用户记忆 | 500 | 注入用户偏好和 top facts |
| 6 | L4 项目记忆 (L1) | 按需 | Agent 通过 `memory_search` 主动查询 |

**裁剪规则**：

- 每层独立计算 token 数，超过预算时按优先级截断
- L3 团队记忆中的 `best_practices`（importance=critical）永远优先于 `communication_summary`
- L1 任务记忆中 `pitfalls` 优先于 `discoveries`

---

## 五、实现方案

### 5.1 文件变更清单

#### 新增文件

| 文件 | 用途 |
|------|------|
| `harness/memory/task_memory.py` | `TaskMemoryStore` — 任务记忆的存储、检索、标签匹配 |
| `harness/memory/team_memory.py` | `TeamMemoryStore` — 团队记忆的存储、更新、检索 |
| `harness/middleware/team_memory.py` | `TeamMemoryMiddleware` — 注入 L3 团队记忆 |
| `harness/middleware/task_memory.py` | `TaskMemoryMiddleware` — 注入 L1 相关任务记忆 |

#### 修改文件

| 文件 | 改动 |
|------|------|
| `harness/memory/__init__.py` | 导出 `TaskMemoryStore`、`TeamMemoryStore` |
| `harness/memory/prompt.py` | 添加 `TASK_MEMORY_UPDATE_PROMPT`、`TEAM_MEMORY_UPDATE_PROMPT` |
| `harness/memory/project_storage.py` | 增强：支持动态生成/更新 L1 项目记忆文件 |
| `harness/team/orchestrator.py` | `run()` synthesis 后触发任务记忆 + 团队记忆更新 |
| `harness/team/teammate_agent.py` | `_handle_task_completion()` 中生成任务记忆 |
| `harness/team/teammate_middleware.py` | 中间件链添加 `TeamMemoryMiddleware` + `TaskMemoryMiddleware` |
| `harness/middleware/dynamic_context.py` | 团队模式下额外加载团队记忆（作为 project_context 的一部分） |
| `harness/config/memory_config.py` | 添加 `team_memory_*` 和 `task_memory_*` 配置项 |

### 5.2 实施分阶段计划

| 阶段 | 内容 | 新增代码量 | 改动代码量 | ROI |
|------|------|-----------|-----------|-----|
| **Phase 1** | L1 任务记忆 (TaskMemoryStore + TaskMemoryMiddleware) | ~300 行 | ~80 行 | 🔴 最高 |
| **Phase 2** | L3 团队记忆 (TeamMemoryStore + TeamMemoryMiddleware) | ~400 行 | ~100 行 | 🔴 高 |
| **Phase 3** | L4 项目记忆增强 (自动生成 architecture.md 等) | ~200 行 | ~50 行 | 🟡 中 |
| **Phase 4** | SubAgent 记忆注入 | ~50 行 | ~30 行 | 🟡 中 |
| **Phase 5** | 语义搜索集成 (mem0) | ~100 行 | ~20 行 | 🟢 低 |

### 5.3 Phase 1: 任务记忆 (Task Memory)

#### 5.3.1 核心类: `TaskMemoryStore`

```python
# harness/memory/task_memory.py

import json
import os
import uuid as uuid_mod
from datetime import datetime, timezone
from dataclasses import dataclass, field
from typing import Optional

from harness.config.paths import get_paths


@dataclass
class TaskMemory:
    """任务级别的结构化记忆."""
    task_id: str
    title: str
    status: str
    assigned_agent: str
    created_at: str
    completed_at: str = ""
    summary: str = ""
    decisions: list[dict] = field(default_factory=list)
    pitfalls: list[dict] = field(default_factory=list)
    discoveries: list[str] = field(default_factory=list)
    dependency_context: Optional[dict] = None
    tags: list[str] = field(default_factory=list)


class TaskMemoryStore:
    """任务记忆的持久化存储与检索.

    存储路径: {data_root}/users/{uid}/projects/{pid}/memory/tasks/{task_id}.json
    """

    def __init__(self, project_id: str, user_id: str):
        paths = get_paths()
        self._task_dir = (
            paths.base_dir / "users" / user_id / "projects" /
            project_id / "memory" / "tasks"
        )
        os.makedirs(self._task_dir, exist_ok=True)

    def _file_path(self, task_id: str) -> str:
        return self._task_dir / f"{task_id}.json"

    # ── 写入 ──

    async def save(self, memory: TaskMemory) -> None:
        """保存任务记忆到 JSON 文件."""
        data = {
            "task_id": memory.task_id,
            "title": memory.title,
            "status": memory.status,
            "assigned_agent": memory.assigned_agent,
            "created_at": memory.created_at,
            "completed_at": memory.completed_at,
            "summary": memory.summary,
            "decisions": memory.decisions,
            "pitfalls": memory.pitfalls,
            "discoveries": memory.discoveries,
            "dependency_context": memory.dependency_context,
            "tags": memory.tags,
        }
        path = self._file_path(memory.task_id)
        with open(path, "w") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)

    async def extract_and_save(
        self,
        task_id: str,
        title: str,
        status: str,
        assigned_agent: str,
        conversation_context: list,
        llm,
        created_at: str = "",
    ) -> TaskMemory:
        """通过 LLM 从对话上下文中提取结构化任务记忆并保存.

        Args:
            task_id: 任务 ID
            title: 任务标题
            status: 终态 (completed / failed)
            assigned_agent: 执行者名称
            conversation_context: 对话消息列表 (HumanMessage / AIMessage / ToolMessage)
            llm: ChatOpenAI 实例 (轻量模型, 如 gpt-4o-mini)
            created_at: 任务创建时间

        Returns:
            TaskMemory: 提取并保存的任务记忆
        """
        from harness.memory.prompt import TASK_MEMORY_UPDATE_PROMPT

        # 格式化对话
        from harness.memory.prompt import format_conversation_for_update
        conv_text = format_conversation_for_update(conversation_context)

        # LLM 提取
        prompt = TASK_MEMORY_UPDATE_PROMPT.format(
            task_title=title,
            status=status,
            agent_name=assigned_agent,
            conversation=conv_text,
        )
        response = await llm.ainvoke(prompt)
        result = json.loads(response.content)

        # 构建 TaskMemory
        memory = TaskMemory(
            task_id=task_id,
            title=title,
            status=status,
            assigned_agent=assigned_agent,
            created_at=created_at or datetime.now(timezone.utc).isoformat(),
            completed_at=datetime.now(timezone.utc).isoformat(),
            summary=result.get("summary", ""),
            decisions=result.get("decisions", []),
            pitfalls=result.get("pitfalls", []),
            discoveries=result.get("discoveries", []),
            dependency_context=result.get("dependency_context"),
            tags=result.get("tags", []),
        )

        await self.save(memory)
        return memory

    # ── 读取 ──

    async def get(self, task_id: str) -> Optional[TaskMemory]:
        """获取单个任务记忆."""
        path = self._file_path(task_id)
        if not path.exists():
            return None
        with open(path) as f:
            data = json.load(f)
        return TaskMemory(**data)

    async def find_related(
        self,
        title: str,
        tags: list[str] = None,
        top_k: int = 3,
    ) -> list[TaskMemory]:
        """通过标签 Jaccard 相似度找到相关的历史任务.

        Args:
            title: 新任务标题 (用于标签提取)
            tags: 新任务的标签 (如果已知)
            top_k: 返回数量

        Returns:
            按相似度降序排列的相关任务记忆列表
        """
        # 列出所有已完成的任务记忆文件
        task_files = list(self._task_dir.glob("*.json"))
        if not task_files:
            return []

        scored: list[tuple[float, TaskMemory]] = []
        query_tags = set(tags or [])

        for path in task_files:
            with open(path) as f:
                data = json.load(f)
            if data.get("status") not in ("completed", "failed"):
                continue

            mem_tags = set(data.get("tags", []))
            if not mem_tags:
                continue

            # Jaccard 相似度
            if not query_tags:
                # 无标签时: 用标题的简单关键词匹配
                title_words = set(title.lower().split())
                task_title_words = set(data.get("title", "").lower().split())
                intersection = title_words & task_title_words
                union = title_words | task_title_words
                score = len(intersection) / len(union) if union else 0
            else:
                intersection = query_tags & mem_tags
                union = query_tags | mem_tags
                score = len(intersection) / len(union) if union else 0

            if score > 0:
                scored.append((score, TaskMemory(**data)))

        scored.sort(key=lambda x: -x[0])
        return [m for _, m in scored[:top_k]]
```

#### 5.3.2 LLM Prompt

```python
# 添加到 harness/memory/prompt.py

TASK_MEMORY_UPDATE_PROMPT = """你是一个任务记忆提取系统。从 agent 执行任务的对话中提取结构化记忆。

任务信息:
- 标题: {task_title}
- 执行者: {agent_name}
- 结果: {status}

对话记录:
<conversation>
{conversation}
</conversation>

提取以下信息 (JSON):
{{
  "summary": "任务完成摘要 (2-3 句话, 说明做了什么、结果如何)",
  "decisions": [
    {{
      "question": "需要决策的问题",
      "options": ["方案A", "方案B"],
      "chosen": "最终选择的方案",
      "reasoning": "选择理由",
      "tradeoffs": "已知的权衡和代价"
    }}
  ],
  "pitfalls": [
    {{
      "problem": "遇到的问题或踩到的坑",
      "solution": "怎么解决的",
      "would_redo": "如果重新做会怎么做 (可选)"
    }}
  ],
  "discoveries": ["对后续任务有价值的发现 (环境、代码结构、约定等)"],
  "tags": ["标签1", "标签2", "标签3"]
}}

提取规则:
- 只提取对后续类似任务有参考价值的信息
- 7 天后会过时的内容 (如具体文件路径、临时状态、PR 号) 不要记录
- 如果对话中没有决策/踩坑/发现, 相应字段返回空数组
- decisions 最多 3 条, pitfalls 最多 5 条, discoveries 最多 5 条
- tags 应该是通用的领域标签 (如 "api", "auth", "database"), 不要用具体的文件名或 ID

Return ONLY valid JSON, no explanation or markdown."""
```

#### 5.3.3 中间件: `TaskMemoryMiddleware`

```python
# harness/middleware/task_memory.py

from typing import Any, Callable

from langgraph.runtime import Runtime


class TaskMemoryMiddleware:
    """在 agent 执行前注入相关的历史任务记忆.

    注入时机: abefore_agent
    注入格式: <system-reminder> 中的 <task_memory> 块
    """

    def __init__(
        self,
        task_memory_store,  # TaskMemoryStore
        current_task_title: str = "",
        current_task_tags: list[str] | None = None,
        dependency_task_ids: list[str] | None = None,
        max_related: int = 3,
        max_tokens: int = 600,
    ):
        self._store = task_memory_store
        self._task_title = current_task_title
        self._task_tags = current_task_tags or []
        self._dependency_ids = dependency_task_ids or []
        self._max_related = max_related
        self._max_tokens = max_tokens

    async def abefore_agent(
        self,
        state: dict[str, Any],
        runtime: Runtime,
    ) -> dict[str, Any] | None:
        """注入相关任务记忆到第一条 HumanMessage."""
        memories = []

        # 1. 加载依赖任务记忆 (强制注入)
        for dep_id in self._dependency_ids:
            mem = await self._store.get(dep_id)
            if mem:
                memories.append(("dependency", mem))

        # 2. 标签匹配相关任务
        related = await self._store.find_related(
            title=self._task_title,
            tags=self._task_tags,
            top_k=self._max_related,
        )
        for mem in related:
            if mem.task_id not in {m.task_id for _, m in memories}:
                memories.append(("related", mem))

        if not memories:
            return None

        # 3. 格式化注入文本
        reminder = self._format_memories(memories)
        return self._inject_reminder(state, reminder)

    def _format_memories(
        self, memories: list[tuple[str, "TaskMemory"]]
    ) -> str:
        """格式化任务记忆为注入文本, 控制在 token 预算内."""
        lines = ["<task_memory>",
                  "以下是与当前任务相关的历史任务经验:"]
        token_count = len(lines[0]) // 4 + len(lines[1]) // 4

        for source, mem in memories:
            prefix = "依赖任务" if source == "dependency" else "相关任务"
            block = (f"\n{prefix}: \"{mem.title}\" "
                     f"(执行者: {mem.assigned_agent}, "
                     f"结果: {mem.status})")

            if mem.summary:
                block += f"\n  摘要: {mem.summary}"

            for d in mem.decisions[:2]:
                block += (f"\n  - 决策: {d.get('question', '')} "
                          f"→ {d.get('chosen', '')}")

            for p in mem.pitfalls[:2]:
                block += (f"\n  - 踩坑: {p.get('problem', '')} "
                          f"→ {p.get('solution', '')}")

            for d in mem.discoveries[:2]:
                block += f"\n  - 发现: {d}"

            block_tokens = len(block) // 4
            if token_count + block_tokens > self._max_tokens:
                break
            lines.append(block)
            token_count += block_tokens

        lines.append("</task_memory>")
        return "\n".join(lines)

    def _inject_reminder(
        self, state: dict, reminder: str
    ) -> dict | None:
        """将任务记忆注入到消息列表."""
        from langchain_core.messages import HumanMessage

        messages = state.get("messages", [])
        if not messages:
            return None

        # 找到第一条 HumanMessage 或 dynamic_context_reminder
        for i, msg in enumerate(messages):
            if isinstance(msg, HumanMessage):
                existing = msg.content if isinstance(msg.content, str) else ""
                new_content = existing + "\n\n" + reminder
                messages[i] = HumanMessage(
                    content=new_content,
                    additional_kwargs=getattr(msg, "additional_kwargs", {}),
                )
                return {"messages": messages}

        return None
```

#### 5.3.4 集成点

**`TeammateAgent`** 中：

```python
# 任务完成时保存任务记忆
async def _handle_task_completion(self, task, status, conversation_messages):
    task_store = TaskMemoryStore(self._project_id, self._user_id)
    await task_store.extract_and_save(
        task_id=task.id,
        title=task.title,
        status=status,
        assigned_agent=self.name,
        conversation_context=conversation_messages,
        llm=self._memory_llm,  # 轻量 LLM, 复用 memory config 中的模型
    )
```

**`TeammateAgent.assign_task()`** 中：

```python
async def assign_task(self, task):
    # 加载相关任务记忆用于注入
    task_store = TaskMemoryStore(self._project_id, self._user_id)
    related = await task_store.find_related(
        title=task.title,
        top_k=3,
    )
    # ... 构建中间件时传入 TaskMemoryMiddleware
```

### 5.4 Phase 2: 团队记忆 (Team Memory)

#### 5.4.1 核心类: `TeamMemoryStore`

```python
# harness/memory/team_memory.py

import json
import os
from datetime import datetime, timezone
from dataclasses import dataclass, field
from typing import Optional

from harness.config.paths import get_paths


@dataclass
class TeamMemory:
    """团队共享记忆."""
    version: str = "1.0"
    last_updated: str = ""
    updated_by: str = ""
    member_profiles: dict[str, dict] = field(default_factory=dict)
    collaboration_patterns: list[dict] = field(default_factory=list)
    best_practices: list[dict] = field(default_factory=list)
    known_pitfalls: list[dict] = field(default_factory=list)
    project_state: dict = field(default_factory=dict)
    communication_summary: dict = field(default_factory=dict)


class TeamMemoryStore:
    """团队共享记忆的持久化存储.

    存储路径: {data_root}/users/{uid}/projects/{pid}/memory/team_memory.json
    """

    def __init__(self, project_id: str, user_id: str):
        paths = get_paths()
        self._memory_path = (
            paths.base_dir / "users" / user_id / "projects" /
            project_id / "memory" / "team_memory.json"
        )
        os.makedirs(self._memory_path.parent, exist_ok=True)

    # ── 读取 ──

    async def load(self) -> TeamMemory:
        """加载团队记忆, 不存在时返回空结构."""
        if not self._memory_path.exists():
            return TeamMemory()
        with open(self._memory_path) as f:
            data = json.load(f)
        return TeamMemory(**data)

    async def get_relevant_context(
        self, agent_name: str, task_title: str = ""
    ) -> str:
        """获取与当前 agent 和任务最相关的团队记忆上下文.

        Returns:
            格式化后的文本, 用于注入到 system prompt
        """
        mem = await self.load()
        lines = []

        # 1. 当前 agent 的能力认知
        profile = mem.member_profiles.get(agent_name, {})
        # ... 省略详细格式化逻辑

        # 2. 相关的最佳实践 (按 importance 排序)
        priority = {"critical": 0, "high": 1, "medium": 2}
        sorted_bps = sorted(
            mem.best_practices,
            key=lambda x: priority.get(x.get("importance", "medium"), 2),
        )
        for bp in sorted_bps[:5]:
            lines.append(
                f"- [{bp.get('importance', 'medium')}] {bp.get('practice', '')}"
            )

        # 3. 相关的已知坑
        for pf in mem.known_pitfalls[:3]:
            lines.append(f"- ⚠️ {pf.get('pitfall', '')}")

        # 4. 项目状态
        ps = mem.project_state
        if ps:
            lines.append(
                f"项目阶段: {ps.get('phase', '未知')} | "
                f"最近变更: {ps.get('last_major_change', '无')}"
            )

        return "\n".join(lines) if lines else ""

    # ── 更新 ──

    async def update_from_synthesis(
        self,
        completed_tasks: list,
        lead_summary: str,
        llm,
        updated_by: str = "__team_lead__",
    ) -> TeamMemory:
        """Synthesis 阶段通过 LLM 从完成的任务中提取团队级发现."""
        from harness.memory.prompt import TEAM_MEMORY_UPDATE_PROMPT

        current = await self.load()

        # 格式化任务摘要
        tasks_text = "\n".join(
            f"- [{t.id}] {t.title} (执行者: {t.assigned_agent}, "
            f"结果: {t.status.value if hasattr(t.status, 'value') else t.status})"
            for t in completed_tasks
        )

        prompt = TEAM_MEMORY_UPDATE_PROMPT.format(
            current_memory=json.dumps(current.__dict__, ensure_ascii=False, indent=2),
            tasks_summary=tasks_text,
            lead_summary=lead_summary,
        )
        response = await llm.ainvoke(prompt)
        updates = json.loads(response.content)

        # 合并更新
        self._merge_member_profiles(current, updates.get("member_updates", {}))
        self._merge_list(
            current.collaboration_patterns,
            updates.get("new_patterns", []),
            key="pattern",
        )
        self._merge_list(
            current.best_practices,
            updates.get("new_best_practices", []),
            key="practice",
        )
        self._merge_list(
            current.known_pitfalls,
            updates.get("new_pitfalls", []),
            key="pitfall",
        )

        if updates.get("project_state_update"):
            current.project_state.update(updates["project_state_update"])

        # 移除标记为删除的条目
        for entry_id in updates.get("entries_to_remove", []):
            self._remove_by_id(current, entry_id)

        current.last_updated = datetime.now(timezone.utc).isoformat()
        current.updated_by = updated_by
        await self._save(current)
        return current

    async def add_best_practice(
        self, practice: str, discovered_by: str,
        source_task: str, importance: str = "medium",
    ) -> None:
        """手动添加团队最佳实践."""
        mem = await self.load()
        mem.best_practices.append({
            "id": f"bp_{len(mem.best_practices) + 1:03d}",
            "practice": practice,
            "discovered_by": discovered_by,
            "source_task": source_task,
            "importance": importance,
            "created_at": datetime.now(timezone.utc).isoformat(),
        })
        await self._save(mem)

    # ── 内部方法 ──

    async def _save(self, memory: TeamMemory) -> None:
        """原子写入团队记忆."""
        tmp_path = self._memory_path.with_suffix(".json.tmp")
        with open(tmp_path, "w") as f:
            json.dump(memory.__dict__, f, indent=2, ensure_ascii=False)
        os.replace(tmp_path, self._memory_path)

    def _merge_member_profiles(
        self, current: TeamMemory, updates: dict,
    ) -> None:
        """增量合并成员能力档案."""
        for name, profile_update in updates.items():
            if name not in current.member_profiles:
                current.member_profiles[name] = {}
            existing = current.member_profiles[name]
            for key in ("strengths", "weaknesses", "best_for", "not_for"):
                new_items = profile_update.get(key, [])
                existing_items = set(existing.get(key, []))
                existing_items.update(new_items)
                existing[key] = list(existing_items)

    def _merge_list(
        self, target: list, updates: list, key: str,
    ) -> None:
        """按 key 去重合并列表."""
        existing_keys = {item.get(key, "") for item in target}
        for item in updates:
            if item.get(key, "") not in existing_keys:
                item["id"] = f"{key[:2]}_{len(target) + 1:03d}"
                item["created_at"] = datetime.now(timezone.utc).isoformat()
                target.append(item)

    def _remove_by_id(self, memory: TeamMemory, entry_id: str) -> None:
        """按 ID 移除条目."""
        for attr in ("best_practices", "known_pitfalls", "collaboration_patterns"):
            lst = getattr(memory, attr, [])
            setattr(memory, attr, [x for x in lst if x.get("id") != entry_id])
```

#### 5.4.2 LLM Prompt

```python
# 添加到 harness/memory/prompt.py

TEAM_MEMORY_UPDATE_PROMPT = """你是一个团队记忆管理系统。从完成的团队任务中提取团队级别的发现。

当前团队记忆:
<current_team_memory>
{current_memory}
</current_team_memory>

本会话完成的任务:
<completed_tasks>
{tasks_summary}
</completed_tasks>

Lead 的汇总:
<lead_summary>
{lead_summary}
</lead_summary>

提取以下更新信息 (JSON):
{{
  "member_updates": {{
    "agent_name": {{
      "strengths": ["本次表现出的新优势 (最多3条, 无新增则空数组)"],
      "weaknesses": ["本次暴露的新不足 (最多2条, 无新增则空数组)"],
      "best_for": ["本次执行的任务类型标签 (最多3条)"]
    }}
  }},
  "new_patterns": [
    {{
      "pattern": "协作模式描述",
      "agents_involved": ["agent_a", "agent_b"],
      "effectiveness": "high|medium|low",
      "when_to_use": "什么场景下适用"
    }}
  ],
  "new_best_practices": [
    {{
      "practice": "通用最佳实践，对后续任务有价值",
      "discovered_by": "发现者",
      "importance": "critical|high|medium"
    }}
  ],
  "new_pitfalls": [
    {{
      "pitfall": "需要警惕的问题描述",
      "context": "出现的上下文",
      "affected_components": ["受影响的组件列表"]
    }}
  ],
  "project_state_update": {{
    "phase": "项目阶段 (如有变化)",
    "last_major_change": "最近重大变更 (如有)",
    "technical_debt": ["新增技术债 (如有)"]
  }},
  "entries_to_remove": ["需要移除的过时条目 ID (可选)"]
}}

更新规则:
- 只记录有长期价值 (>7天) 的信息
- 不要重复已有条目 (检查 <current_team_memory>)
- 成员档案更新只记录新发现, 不重复已知信息
- best_practices: 只在 agent 明确发现了一个通用模式时才记录
- known_pitfalls: 只在 agent 踩坑并花费了显著时间才记录
- 如果本轮没有新的团队级发现, 所有字段返回空数组

Return ONLY valid JSON, no explanation or markdown."""
```

#### 5.4.3 中间件: `TeamMemoryMiddleware`

```python
# harness/middleware/team_memory.py

from typing import Any

from langgraph.runtime import Runtime
from langchain_core.messages import HumanMessage


class TeamMemoryMiddleware:
    """注入团队共享记忆到 agent 的 system prompt.

    注入时机: abefore_agent (在 DynamicContextMiddleware 之后)
    注入格式: <team_memory> 块
    """

    def __init__(
        self,
        team_memory_store,  # TeamMemoryStore
        agent_name: str = "",
        task_title: str = "",
        max_tokens: int = 800,
    ):
        self._store = team_memory_store
        self._agent_name = agent_name
        self._task_title = task_title
        self._max_tokens = max_tokens

    async def abefore_agent(
        self,
        state: dict[str, Any],
        runtime: Runtime,
    ) -> dict[str, Any] | None:
        context = await self._store.get_relevant_context(
            agent_name=self._agent_name,
            task_title=self._task_title,
        )
        if not context:
            return None

        reminder = f"<team_memory>\n{context}\n</team_memory>"

        # 注入到第一条 HumanMessage
        messages = state.get("messages", [])
        for i, msg in enumerate(messages):
            if isinstance(msg, HumanMessage):
                existing = msg.content if isinstance(msg.content, str) else ""
                messages[i] = HumanMessage(
                    content=existing + "\n\n" + reminder,
                    additional_kwargs=getattr(msg, "additional_kwargs", {}),
                )
                return {"messages": messages}
        return None
```

#### 5.4.4 集成点

**`TeamOrchestrator.run()`** synthesis 阶段之后：

```python
# 在 _llm_synthesize() 返回之后, yield team_end 之前
if has_sub_tasks:
    team_store = TeamMemoryStore(self._project_id, self._user_id)
    completed = [t for t in all_tasks if t.status == TeamTaskStatus.COMPLETED]
    if completed:
        await team_store.update_from_synthesis(
            completed_tasks=completed,
            lead_summary=synthesis_result,
            llm=self._memory_llm,
        )
```

**`TeammateAgent._build_middlewares()`** 中添加：

```python
# 在第 11 位 (MemoryMiddleware 之前) 插入
if self._project_id and self._user_id:
    from harness.memory.team_memory import TeamMemoryStore
    team_store = TeamMemoryStore(self._project_id, self._user_id)
    middlewares.append(TeamMemoryMiddleware(
        team_memory_store=team_store,
        agent_name=self.name,
        task_title=self.current_task_title if hasattr(self, 'current_task_title') else "",
    ))
```

### 5.5 Phase 3: 项目记忆增强

#### 5.5.1 增强方案

在 `ProjectMemoryStorage` 中新增方法：

```python
# harness/memory/project_storage.py 新增

class ProjectMemoryStorage:
    # ... 现有代码 ...

    async def generate_architecture_summary(
        self, project_root: str, llm,
    ) -> str:
        """扫描项目目录结构，通过 LLM 生成 architecture.md."""
        # 1. walk 项目目录, 收集文件树 (跳过 node_modules, .git, __pycache__ 等)
        # 2. 读取关键文件 (main.py, pyproject.toml, package.json 等)
        # 3. LLM 生成架构摘要
        pass

    async def extract_conventions(
        self, task_memories: list[TaskMemory], llm,
    ) -> str:
        """从任务记忆中提取项目约定并更新 conventions.md."""
        # 1. 收集所有 discoveriess
        # 2. LLM 从中提取规律性的约定
        # 3. 合并到现有 conventions.md (不覆盖人工编写的部分)
        pass

    async def record_decision(
        self, title: str, context: str, decision: str,
        reasoning: str, alternatives: list[str],
    ) -> str:
        """记录重大决策到 decisions/ 目录."""
        # 1. 生成文件名: YYYY-MM-DD-{slug}.md
        # 2. 写入结构化决策记录
        pass
```

### 5.6 Phase 4: SubAgent 记忆注入

SubAgent 通过 `SubagentExecutor` 运行在隔离的 daemon 线程事件循环中，当前没有任何记忆注入。改动点：

1. **`subagent_middleware.py`**：在 SubAgent 的中间件链中添加 `DynamicContextMiddleware`（注入 L5 用户记忆）
2. **`subagent_executor.py`**：在 `execute()` 时传入 `user_id`，加载对应用户的记忆
3. 注意：SubAgent 不注入 L3 团队记忆和 L1 任务记忆（SubAgent 不需要知道团队协作细节，其 task instruction 已包含足够上下文）

### 5.7 Phase 5: 语义搜索 (mem0 集成)

当 mem0 后端启用时，利用向量搜索做语义匹配：

1. **任务记忆 embedding**：任务完成时将 `summary + discoveries` 文本做 embedding 存入 mem0，`filters={"type": "task_memory", "project_id": "..."}`
2. **团队记忆 embedding**：将 `best_practices` 和 `known_pitfalls` 文本做 embedding
3. **增强检索**：`find_related()` 同时查询标签匹配（精确）和语义匹配（模糊），合并结果

---

## 六、配置设计

在 `MemoryConfig`（`harness/config/memory_config.py`）中新增配置项：

```python
# ── 团队记忆配置 ──
team_memory_enabled: bool = True
"""是否启用团队共享记忆 (L3)"""

team_memory_max_entries: int = 50
"""best_practices + known_pitfalls 最大条目数"""

team_memory_ttl_days: int = 90
"""团队记忆条目过期天数，超期未引用的条目自动清理"""

team_memory_injection_tokens: int = 800
"""注入到 agent prompt 的最大 token 数"""

team_memory_update_model: str = "gpt-4o-mini"
"""用于提取团队记忆的轻量 LLM 模型"""

# ── 任务记忆配置 ──
task_memory_enabled: bool = True
"""是否启用任务级别记忆 (L1)"""

task_memory_max_related: int = 3
"""注入时检索的最大相关任务数"""

task_memory_injection_tokens: int = 600
"""注入到 agent prompt 的最大 token 数"""

task_memory_update_model: str = "gpt-4o-mini"
"""用于提取任务记忆的轻量 LLM 模型"""

# ── 项目记忆增强配置 ──
project_memory_auto_update: bool = True
"""是否自动更新项目记忆文件 (architecture.md, conventions.md)"""

project_memory_auto_update_threshold: int = 5
"""累计多少个相关发现后触发自动更新"""
```

默认值（`harness/config/defaults.py`）：

```python
SYSTEM_DEFAULTS["memory"].update({
    "team_memory_enabled": True,
    "team_memory_max_entries": 50,
    "team_memory_ttl_days": 90,
    "team_memory_injection_tokens": 800,
    "team_memory_update_model": "gpt-4o-mini",
    "task_memory_enabled": True,
    "task_memory_max_related": 3,
    "task_memory_injection_tokens": 600,
    "task_memory_update_model": "gpt-4o-mini",
    "project_memory_auto_update": True,
    "project_memory_auto_update_threshold": 5,
})
```

---

## 七、安全考量

沿用现有的 `harness/memory/safety.py` 安全机制（提示注入检测、凭据窃取检测、不可见 Unicode 拦截），并在新模块中添加：

1. **文件写入前扫描**：`TeamMemoryStore._save()` 和 `TaskMemoryStore.save()` 写入前调用 `validate_memory_json()` 进行安全扫描
2. **LLM 输出验证**：LLM 提取的结构化记忆在反序列化后验证字段类型和长度限制
3. **文件读取后扫描**：注入前调用 `sanitize_memory_if_unsafe()` 扫描加载的记忆
4. **大小限制**：单个任务记忆文件不超过 50KB，团队记忆文件不超过 200KB

---

## 八、总结

### 8.1 改动前后对比

| 维度 | 改动前 | 改动后 |
|------|--------|--------|
| 记忆层数 | 3 层（L0/L2/L5）+ L4 静态 | 5 层全部就位 |
| 任务经验 | ❌ 黑盒，只有 output 文本 | ✅ 结构化决策、踩坑、发现 |
| 团队协作 | ❌ 每次"初次见面" | ✅ 越合作越默契 |
| 项目记忆 | ⚠️ 静态 description.md | ✅ 自动演进的多文件体系 |
| SubAgent | ❌ 失忆状态 | ✅ 注入用户记忆 |
| 检索方式 | ⚠️ 全量 dump 到 prompt | ✅ 优先级排序 + 标签匹配 + 按需搜索 |

### 8.2 实施路线图

```
Phase 1 (任务记忆)     Phase 2 (团队记忆)     Phase 3 (项目记忆增强)
    │                       │                       │
    ▼                       ▼                       ▼
Week 1-2                Week 2-3                Week 3-4
┌──────────┐          ┌──────────┐           ┌──────────┐
│TaskMemory│          │TeamMemory│           │ auto-gen │
│  Store   │          │  Store   │           │ architecture│
│Middleware│          │Middleware│           │ conventions│
│  Prompt  │          │  Prompt  │           │ decisions │
│Teammate  │          │Orchestrator│         │          │
│integration│         │integration│          └──────────┘
└──────────┘          └──────────┘
                             │
                    ┌────────┴────────┐
                    ▼                 ▼
              Phase 4 (SubAgent)  Phase 5 (mem0)
              Week 4-5            Week 5-6
              ┌──────────┐      ┌──────────┐
              │DynamicContext│   │ semantic │
              │for SubAgents│    │ search   │
              └──────────┘      └──────────┘
```

### 8.3 关键设计决策

1. **L1 任务记忆先于 L3 团队记忆**：任务记忆的 ROI 最高——下次执行类似任务时立即可用。团队记忆需要积累足够的协作经验才有价值
2. **LLM 提取而非规则提取**：使用轻量 LLM（gpt-4o-mini）做记忆提取，而非正则或模板。对话内容变化大，规则无法覆盖
3. **文件存储而非数据库**：与现有 FileMemoryStorage 保持一致，简单、可调试、可手动编辑。不引入额外的数据库表
4. **注入到 prompt 而非独立节点**：沿用现有 DynamicContextMiddleware 的注入模式，不改变 LangGraph 图结构
5. **增量更新而非全量重写**：团队记忆和项目记忆都采用增量合并策略，避免丢失已有信息
