# Multiagent-Studio Agent Team 改进建议

> 生成时间：2026-07-15
> 基于对 `harness/team/`、`frontend/src/`、设计文档及现有分析文档的梳理
> 目标：帮助团队从"功能骨架"走向"可运行的端到端产品"

---

## 一、整体判断

**当前状态**：multiagent-studio 的 Agent Team 后端骨架已经搭得很完整（Orchestrator、TeammateAgent、TaskStore、MessageBus、Tracer、15 个 Team 工具），前端基础页面也已就位。但项目目前处于关键节点：

> **"有器官，缺血液循环"**

前后端在**任务状态、SSE 事件、持久化存储**三个核心契约上存在不一致，导致 Team 模式在真实运行时很可能出现：
- 任务创建了但看板不更新
- 成员状态不刷新
- 任务状态前后端不匹配
- 端到端流程未在真实 LLM 下验证通过

**核心建议**：
> **立刻停止增加新功能，用 1-2 周时间把最小链路跑通**：创建项目 → 添加成员 → 发送目标 → Lead 拆任务 → Member 执行 → 前端看板实时更新 → Lead 汇总。

---

## 二、P0：必须立即修复的契约问题

### 2.1 统一任务状态枚举

**问题**

| 位置 | 枚举 |
|------|------|
| 后端 `harness/team/models.py` | `PENDING / IN_PROGRESS / COMPLETED / FAILED / CANCELLED`（5 态） |
| 前端 `frontend/src/lib/types.ts` | `todo / in_progress / in_review / completed / failed / rejected / merged`（7 态） |
| 项目 API `/projects/{id}/tasks` | 使用 7 态（`TaskItem` 默认 `todo`） |

**影响**
- Team 运行时创建的任务状态是 `pending`，前端看板按 7 态过滤，这些任务不会出现在任何列中。
- `frontend/src/app/(dashboard)/projects/[id]/page.tsx` 虽然合并了 `team-store.tasks`，但直接把 `rt.status` 塞进 7 态列，导致 `pending`/`cancelled` 等状态丢失。
- `test_team_flow.py` 的 Pydantic warning 反映了字符串/枚举混用。

**建议**
1. **后端保留 5 态作为运行时状态机**（简单、A2A 兼容、不易死锁）。
2. **前端做显示映射**：
   - `pending → todo`
   - `in_progress → in_progress`
   - `completed → completed`
   - `failed → failed`
   - `cancelled → rejected` 或 `failed`
3. 如果未来确实需要 `in_review / merged`，把它作为 task 的**额外字段或子状态**，而不是替换核心状态机。
4. 立即修复 `test_team_flow.py` 里的 Pydantic warning（字符串/枚举混用）。

---

### 2.2 补齐 SSE 事件契约

**问题**

前端 `types.ts` 与 `chat-store.ts` 期待的事件：

```ts
"team_start" | "team_end" | "team_status" | "team_task_update" | "team_message" | "member_status" | "team_error" | "team_degrade"
```

后端 `orchestrator.py` 实际发送的：

```python
team_start, team_status, team_end, team_error, team_degrade
```

`team_task_update`、`member_status`、`team_message` 在当前后端代码中没有找到发送点。

**影响**
- 前端看板、成员状态、消息流都无法通过 SSE 实时更新。
- 任务实际是通过 `task_create / task_update` 工具副作用写入 `TeamTaskStore`，但前端没有消费这些工具事件来刷新 UI。
- 用户只能看到 `team_start / team_status / team_end`，中间过程黑盒。

**建议（推荐方案 A）**

在关键状态变更点补发 SSE 事件：

| 触发点 | 事件 |
|--------|------|
| `task_create / task_update` 工具执行后 | `team_task_update` |
| `TeammateAgent` 状态切换（idle/working） | `member_status` |
| `message_bus.send()` 成功时 | `team_message` |

**备选方案 B**：精简前端事件集合，只保留后端实际会发送的事件，任务/成员状态通过前端定时轮询 `/projects/{id}/tasks` 刷新。但 SSE 是 multiagent-studio 的核心体验，不建议退化成轮询。

---

### 2.3 消除任务"双存储"

**问题**

当前存在两个独立的任务存储：

| 存储 | 路径 | 用途 |
|------|------|------|
| 项目任务板 | `users/{user_id}/projects/{project_id}.json` | 前端看板读取 |
| Team 运行时任务板 | `users/{user_id}/team_tasks/{project_id}.json` | Team 运行时写入 |

两者没有同步机制，前端看板读的是项目 JSON，Team 运行写的是另一个文件。

**建议**

**最小改动**：让 `task_create / task_update` 工具在写 `TeamTaskStore` 的同时，也写回项目 JSON 的 `tasks` 字段。

**更彻底**：把 `Project` 和 `Task` 迁移到 PostgreSQL（`Thread` 表已经在 DB 里了），JSON 只作为缓存/导出。

**附加**：统一目录命名。当前 `users/{user_id}/project/...` 和 `users/{user_id}/projects/...` 混用，必须选一个。

---

## 三、P1：让 Team 模式真正可运行

### 3.1 跑一次真实的端到端验证

**当前状态**
- `harness/tests/test_team_flow.py`：42 passed，但只是单元测试。
- `test_team_e2e.py`（项目根目录）：存在，但需要启动 App(8000)+Harness(8001)，尚未运行。
- `eval/` 目录：完全为空。

**建议**
1. 启动 `start.sh`（Harness 8001 → App 8000 → Frontend 3000）。
2. 运行 `python test_team_e2e.py`。
3. 观察是否能产生 `team_task_update`、`task_create`、`task_update` 等事件，任务是否被正确认领和完成。
4. 把这次运行的日志和卡点写成 issue，作为下一阶段的输入。

**验收标准**：创建一个含 2 个 Member 的 Project，完成一次简单任务分工（如"研究员搜索资料，写手生成报告"），前端看板实时更新。

---

### 3.2 确认 `ProjectLeadAgent` 的去留

**问题**
- `harness/team/project_lead_agent.py` 存在且功能完整。
- 全局搜索未发现有其他模块实例化它。
- 实际 `orchestrator.py` 中 Lead 也是一个 `TeammateAgent(role="lead")`。

**建议**
- 如果 `TeammateAgent(role="lead")` 方案可行，**删除或归档 `project_lead_agent.py`**，避免维护两份 Lead 逻辑。
- 如果计划切换到 `ProjectLeadAgent`，完成 Orchestrator 的集成，并明确它与 `TeammateAgent` 的边界。

---

### 3.3 修复 `teammate_middleware.py` 文档/实现矛盾

**问题**
- 文件顶部注释声称排除 `DynamicContextMiddleware`。
- 实际代码第 118 行：`middlewares.append(DynamicContextMiddleware(agent_name=agent_name))`。

**建议**
- 明确是否保留 `DynamicContextMiddleware`。
- 这个决策直接影响每个 member agent 的上下文注入是否正确，必须解决。

---

### 3.4 把 `agent_card.py` 纳入版本控制并规范路径

**问题**
- `harness/team/agent_card.py` 是 untracked 文件。
- Agent Card 输出路径为 `users/{user_id}/project/{project_id}/agent_card.json`，与 `projects/` 目录命名不一致。

**建议**
1. 把 `agent_card.py` 加入 git 版本控制。
2. 统一目录命名（`project/` → `projects/` 或反之）。
3. 在 UI 上暴露"查看 Agent 能力卡片"功能。

---

### 3.5 验证取消与资源清理

**问题**
- `/stop/{thread_id}` 调用 `harness.stop(thread_id)`。
- Team 编排器内多个 `asyncio.Task` 是否会被正确取消、资源是否清理，尚未验证。

**建议**
- 写一个专门的 stress test：启动 Team → 立即 stop → 检查是否有残留任务。
- 确保取消时清理：文件锁、inbox、worktree、tracer。

---

## 四、架构层面的长期建议

### 4.1 先做一个"最小可运行的 Team 场景"

不要试图一次性支持 Lead-driven / User-driven / Hybrid 三种模式。建议先锁定一个场景：

> 用户在一个 Project 里发一句"帮我写一个 Flask CRUD 后端"，Lead 创建 2 个任务（设计 API、实现代码），分别派给 member A 和 member B，前端看板实时刷新，最后 Lead 汇总结果。

把这个场景跑通，再扩展其他模式。

---

### 4.2 明确 Task Board 的"唯一真相源"

建议把 `TeamTaskStore` 作为唯一真相源，项目 API 的 tasks 只是它的**投影（projection）**。所有工具只写 `TeamTaskStore`，项目 API 读取时从 `TeamTaskStore` 反序列化。

好处：
- 消除双写不一致。
- 前端轮询和 SSE 都基于同一份数据。
- 未来迁移到数据库时，只需要替换 `TeamTaskStore` 的实现。

---

### 4.3 给 Member Agent 增强"上下文意识"

目前 member 的 system prompt 通过 middleware 注入，但 Team 上下文（项目目标、其他成员、当前任务板）注入是否充分需要验证。

**建议**
- 在 `teammate_agent.py` 每次执行前，把以下信息拼进 prompt：
  - `task_board_summary`
  - `unread_messages`
  - `team_capabilities`
  - `project_goal`
- 避免 member "埋头干活却不知道项目全局"。

---

### 4.4 谨慎处理 Worktree 隔离

Worktree 是个双刃剑：
- **好处**：成员互不污染。
- **风险**：合并冲突、文件锁、跨 worktree 工具调用容易出错。

**建议**
- 默认 `isolation: none`，只有明确需要时才开 worktree。
- 在 `merge_result` 工具里必须有 `git merge --abort` 回滚保护。
- 合并前先 `git diff --check` 检查冲突标记。

---

## 五、工程治理建议

### 5.1 补齐根目录基础设施

| 缺失 | 建议 |
|------|------|
| `README.md` | 安装、启动、环境变量说明 |
| `pyproject.toml` | 当前依赖分散在 `app/requirements.txt` 和 `harness/requirements.txt`，建议统一 |
| `docker-compose.yml` | 目前只有 PostgreSQL + Redis，建议可选地加入 App / Harness / Frontend |

---

### 5.2 测试策略

当前单元测试覆盖不错，但缺少：
- **端到端测试**：至少一个真实 LLM 下的完整 Team 流程。
- **契约测试**：前后端 SSE 事件、API 接口的 schema 一致性检查。
- **压力测试**：多 member 并发、取消、超时。

**建议**
- 先把 `test_team_e2e.py` 跑起来并加入 CI。
- 在 `eval/` 目录添加至少一个端到端基准任务集（如 SWE-bench Lite 子集、规划+代码任务）。

---

### 5.3 代码质量

- 统一中文注释风格（项目已有 `CLAUDE.md` 要求）。
- 减少 `Any` 类型使用，特别是 `orchestrator.py` 和 `tools.py` 里大量 `Any` 会降低可维护性。
- 把 magic number（`MAX_TEAM_ROUNDS=100`、`OVERALL_TIMEOUT=1800`）提到 config。

---

## 六、可借鉴 Hermes-Agent 的经验

Hermes-Agent 在自进化机制上的设计对 multiagent-studio 有重要参考价值：

| Hermes 机制 | 对 multiagent-studio 的借鉴 |
|---|---|
| **记忆分 user/memory 两类** | 给 Agent 增加 `memory_scope`（`user/project/team/local`），避免所有 agent 共享同一份记忆 |
| **技能（Skills）作为过程记忆** | 当某个 member 反复执行同类任务时，自动生成/复用 skill，减少重复 prompt |
| **后台 review fork** | Team 执行结束后，轻量级 review 流程把成功经验沉淀为 skill 或项目记忆 |
| **轨迹系统（Trajectory）** | Team 执行的完整 message trace 保存为 JSONL，用于 RL 微调、失败分析、eval |
| **Curator 自动维护技能库** | Agent 越来越多时，定期合并重复技能、归档过期技能 |

**具体落地建议**
- 充分利用现有的 `harness/observability/team_tracer.py`，把每次 Team 运行完整 trace 保存到 `users/{user_id}/team_traces/{project_id}/{thread_id}/`。
- 参考 Hermes 的 `SKILL.md` 格式，为每个 Agent 建立可复用的技能库。
- 在 Team 运行结束后增加一个轻量级"复盘"步骤，把高频操作模式沉淀为 skill。

---

## 七、下一步行动清单

### 本周（P0）

- [ ] 统一前后端任务状态枚举，前端做状态映射
- [ ] 在 `harness/team/tools.py` 的 `task_create / task_update` 工具中增加 SSE `team_task_update` 事件发送
- [ ] 在 `harness/team/teammate_agent.py` 状态切换点发送 `member_status` SSE
- [ ] 统一任务存储：`TeamTaskStore` 作为真相源，项目 API 读取它
- [ ] 跑一次 `python test_team_e2e.py`，记录卡点

### 下周（P1）

- [ ] 决定 `ProjectLeadAgent` 去留
- [ ] 修复 `teammate_middleware.py` 文档/实现矛盾
- [ ] 验证 `/stop/{thread_id}` 能正确终止所有 teammate 任务
- [ ] 把 `agent_card.py` 加入版本控制
- [ ] 写一份根目录 `README.md`

### 下月（P2）

- [ ] 把 Project / Task 迁移到 PostgreSQL
- [ ] 补齐 `eval/` 目录，至少一个端到端基准
- [ ] 接入 Hermes 式的 skill / memory 沉淀机制
- [ ] 完善 docker-compose，支持一键启动全栈

---

## 八、总结

multiagent-studio 的 Agent Team 方向正确，后端设计有野心也有深度。但现在最危险的不是"缺功能"，而是**"前后端各跑各的，主流程没有闭环"**。

**最关键的三件事**：
1. **统一状态枚举**
2. **补齐 SSE 事件**
3. **打通任务双存储**

做完这三件事，再跑一次真实端到端验证，项目就会从"骨架"进入"可运行"阶段。之后再基于真实运行数据，决定是扩展模式、增强记忆/技能，还是优化 UI。
