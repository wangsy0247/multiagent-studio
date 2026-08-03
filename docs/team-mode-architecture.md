# Team 模式架构分析

> 基于代码全量阅读（重构后版本）。核心代码：`harness/team/`（orchestrator / teammate_agent / tools / task_store / message_bus / models / context / agent_card / worktree）。

## 1. 总览

Team 模式是 **Lead-Worker 星型架构 + 平台内置 Verifier**，纯 asyncio 单进程：

```
用户 ⇄ app(8000, 认证/落库) ⇄ harness(8001, HarnessService.execute)
                                    │
                          TeamOrchestrator (每 run 一个实例)
                                    │ message_bus (进程内文件信箱)
        ┌───────────────┬───────────┼───────────────┬────────────────┐
   __team_lead__    Member A      Member B     Member C      __team_verifier__
   (平台内置 Lead)  (项目成员, 懒加载)                        (平台内置验收者, 懒加载)
                                    │
              task_store (tasks.json, thread 级持久化, fcntl 锁)
```

- Lead：纯协调者（工具白名单 `files_readonly`/`search` + 12 个协调工具 + ask_clarification），无执行类工具
- Member：项目成员，**懒加载**（initialize 只拉起 Lead，成员首次派单时才 spawn）
- Verifier：平台内置（`__team_verifier__`），首个高危任务进验收时才拉起，执行者不得自验

## 2. 生命周期

### 2.1 Team Run 生命周期

```
initialize()
  ├─ 加载 project.json → 名册 (self._member_names)
  ├─ 生成/复用 AgentCard (mtime 缓存) → <team_capabilities>
  ├─ 创建 Lead (default 配置 + 系统 SOUL)          ← 唯一立即 spawn 的
  ├─ recover_orphaned_tasks()                      ← crash 恢复入口
  ├─ 加载 L2 团队记忆 → team_context
  └─ 补发 member_status(idle) 占位事件 (前端兼容)

run(message) / resume(answer)
  ├─ Phase 0 triage: Lead 分析目标 (120s 超时)
  │    ├─ Lead 调 ask_clarification → 暂停 (_clarification_pending, TTL 30min)
  │    │    → 用户回答 → respond_to_clarification → resume() 续跑
  │    ├─ Lead 直接回答 → [Lead 独立完成] → 跳过 dispatch+synthesis
  │    └─ Lead 建子任务 → 进入 dispatch
  ├─ Phase 2 dispatch loop (事件驱动, progress_event 唤醒, 无轮询 sleep)
  │    每轮: propagate_failures → _resume_interrupted_tasks
  │         → _process_verifications (高危 IN_REVIEW → 建验收子任务/消化 VERDICT)
  │         → _dispatch_ready_tasks (REVISION_NEEDED 回派 / 指定成员 / 领域匹配+负载均衡)
  │         → 成员空闲通知 Lead (去重) → watchdog (死锁检测 300s)
  ├─ Phase 3 synthesis: _llm_synthesize (30s 超时 → _synthesize_results 静态兜底)
  ├─ Team Memory 提取 (fire-and-forget): L2 协作教训 + L3 按成员路由
  └─ _finalize_run: 结算残留 IN_PROGRESS → INTERRUPTED → 关闭所有已 spawn 成员
       → tracer.shutdown (asyncio.to_thread, 15s 超时)
```

### 2.2 成员生命周期

```
(名册 standby) → SPAWNING → IDLE ⇄ WORKING → SHUTTING_DOWN → SHUTDOWN
                                ↘ FAILED (last_error 不可恢复)
```

- 懒加载触发点：dispatch 派单、REVISION_NEEDED 回派、INTERRUPTED 恢复、Lead 调 spawn_teammate、高危任务验收（Verifier）
- 工作循环（`_work_loop`）：领任务 → ReAct（中间件链 ~12-21 层）→ 结算（task_update）→ drain inbox → IDLE 等 `_wake_event`
- 关机：run 结束 orchestrator 直接 cancel；Lead 也可用 shutdown_teammate 发起握手（成员可拒绝）

### 2.3 任务状态机（`TeamTaskStatus`）

```
PENDING → IN_PROGRESS → IN_REVIEW ──→ APPROVED (终态)
   ↑          │  │            └──→ REVISION_NEEDED → IN_PROGRESS (回派原成员, 附验收意见)
   │          │  └─→ COMPLETED (终态)   ← 仅低危: 证据程序校验直通 / 轻任务
   │          └─→ FAILED (终态)
   └─ (crash) IN_PROGRESS → INTERRUPTED → 原成员恢复 IN_PROGRESS / 回池 PENDING
                               └── 达 max_retries → CANCELLED (终态)
```

硬约束（tools.py 守卫）：
- **高危任务禁止 IN_PROGRESS→COMPLETED 直达**（必须 in_review 验收）
- INTERRUPTED 不在 `_VALID_TRANSITIONS` 表中——只能 orchestrator 恢复流程处理，member 不可手动流转
- 认领经 `task_store.claim()` CAS 原子收口，orchestrator 派单与成员自认领无双执行窗口

### 2.4 风险分级与验收路由（Phase 3）

```
创建时: Lead 标 high → 锁定; Lead 标 low 但命中写操作信号 → 程序单向升级 high;
        未指定 → infer_task_risk (关键词/acceptance_criteria/下游依赖)
低危: member 提交 in_review → _validate_evidence (文件存在性, /mnt/user-data 映射)
      → 通过直接 COMPLETED; 失败 → 留 IN_REVIEW (fail-safe)
高危: member 提交 in_review → _process_verifications 建验收子任务
      → __team_verifier__ 独立验收 (只看 spec+result+evidence)
      → VERDICT: PASS → APPROVED / FAIL → REVISION_NEEDED (附理由回派)
      → Verifier 拉起失败/VERDICT 解析失败 → Lead task_review 兜底
```

## 3. 传输与 JSON 格式

### 3.1 SSE 事件（orchestrator → harness → app → 前端）

| type | 关键字段 | 说明 |
|---|---|---|
| `team_start` | `members[], project_id, thread_id` | run 开始（members 为名册） |
| `team_status` | `phase, content` | 阶段提示（triage/dispatching/synthesizing） |
| `team_task_update` | `task: TeamTask 全量 model_dump` | 任务创建/状态变更（含 spec/result/risk） |
| `member_status` | `agent_name, status, task_id, current_task_id, task_title` | 成员状态（idle/working/standby） |
| `team_message` | `message: TeamMessage` | agent 间消息透传 |
| `message` | `content, subagent_name` | Lead 的流式文本（member 文本不发 SSE，写 JSONL） |
| `tool_call` / `tool_result` | `tool_name, tool_args / tool_result, is_error?` | 仅 Lead；`read_inbox` 已过滤；`on_tool_error` 以 `is_error` 推送 |
| `clarification` | `question, context` | 暂停等待用户回答 |
| `team_end` | `status, total_rounds` | run 结束（completed/cancelled/failed） |
| `team_error` / `team_degrade` | `error/content` | 错误 / 降级单 agent |
| `finished` | — | app 层追加的流终止标记 |

### 3.2 Agent 间消息（message_bus，文件信箱）

`users/{uid}/projects/{pid}/threads/{tid}/messages/{agent}.json`（fcntl 锁）：

```jsonc
// TeamMessage
{
  "id": "a1b2c3d4",
  "from_agent": "AI-Engineer",
  "to_agent": "__team_lead__",      // null = 广播
  "msg_type": "text | broadcast | lifecycle | shutdown_request |
               shutdown_response | plan_approval_request | plan_approval_response",
  "content": "...",
  "task_id": "be543bd4",            // 可选, 关联任务
  "request_id": "xyz",              // 协议消息: 关联请求/响应
  "approved": true                  // 协议响应结构化结果 (避免 content 子串误判)
}
```

接收路径：`InboxDrainMiddleware` 每次 LLM 调用前自动 drain 注入上下文（程序式，非 agent 工具调用）+ IDLE 时 `_wake_event` 唤醒轮询。协议请求在 teammate 的 `_pending_requests` 登记，FSM: pending → approved/rejected。

### 3.3 任务板（tasks.json，thread 级持久化）

```jsonc
// TeamTask（关键字段）
{
  "id": "be543bd4", "project_id": "3072c532",
  "title": "...", "description": "...",       // spec 渲染文本 + [提交要求]
  "spec": {                                    // TaskSpec (Phase 2, 可空=轻任务)
    "background": "", "goal": "", "description": "",
    "constraints": [], "format": "", "acceptance_criteria": []
  },
  "result": {                                  // TaskResult (成员提交)
    "status": "in_review", "output": "...",
    "evidence": ["/mnt/user-data/workspace/x.html"],
    "uncertainty": "low|medium|high",          // 仅展示, 不参与判断
    "failure_reason": "",
    "skill_feedback": [{"name": "...", "success": true}]
  },
  "status": "in_review", "assigned_agent": "Frontend-Developer",
  "dependencies": ["..."], "priority": "high",
  "risk": "high", "risk_locked": false,        // 风险分级 (Phase 3)
  "verifies_task_id": null,                    // 验收子任务 → 原任务
  "retry_count": 0, "max_retries": 3,          // crash 恢复
  "review_feedback": "...", "created_at": "...", "updated_at": "..."
}
```

### 3.4 VERDICT 协议（验收结论）

Verifier 的 result.output 末行必须是 `VERDICT: PASS` / `VERDICT: FAIL`，orchestrator 用正则 `_VERDICT_RE` 解析（`_parse_verdict`），解析失败 fail-safe 转 Lead 审查。

### 3.5 AgentCard（agent_card.json，项目级，mtime 缓存）

```jsonc
{ "project_id": "...", "updated_at": "...",
  "cards": { "AI-Engineer": { "agent_name", "role", "description",
             "tools": [...], "skills": [...], "updated_at", ... } } }
```

注入 Lead prompt 的 `<team_capabilities>`，是 Lead 分配决策的能力信息源；skill 加载失败时 `sync_agent_card_skills` 收敛到真实可用集合。

## 4. 记忆体系（L0-L3 + 任务记忆）

```
users/{uid}/
  memory.json                     L0 用户全局: 偏好/习惯 → 全量注入单 agent + Lead
  agents/{agent}/memory.json      L1 成员全局: 跨项目通用经验 → 全量注入该成员
  projects/{pid}/memory/
    team_memory.json              L2 团队协作: {best_practices, known_pitfalls, recent_runs}
                                  → 全量注入所有 teammate (≤20/≤5 条)
    members/{agent}.json          L3 项目×成员: {practices, pitfalls, domain_notes}
                                  → 按任务相关性检索 top-K 注入该成员
    tasks/*.json                  任务记忆: {task_id, summary, decisions[],
                                  pitfalls[], discoveries[], tags[]}
```

- **写入**：任务完成 → `extract_lessons_from_task`（程序式：failed→pitfall、completed+goal→practice，不跑 LLM）→ L3；run 结束 LLM 提取协作教训 → L2（`TEAM_MEMORY_UPDATE_PROMPT` 限定只提取协作层）
- **晋升 L3→L1**：程序计数（语义指纹 Jaccard≥0.6 去重；跨 ≥2 项目 或 单项目复用 ≥3）→ LLM **只做泛化改写**（不得新增事实）→ 写 L1 + 审计日志
- **检索**：`memory_search` 工具（team 内查任务记忆）；单 agent 侧 `project_info` / `project_memory_search`（只读）
- **skill 自进化**（Phase 5）：L1 程序化经验 → LLM 提炼候选 SKILL.md → probation（注入标"试验性"）→ 成功 ≥3 → plan_approval 审批转正 → active；连续失败/30 天未用 → archived

## 5. 工具矩阵

| 工具 | Lead | Member | Verifier | 单 agent |
|---|---|---|---|---|
| task_create / delegate_to_member | ✅ | ✅(create) | ❌ | — |
| task_list / task_review / list_teammates | ✅ | ✅(list) | ✅(list) | — |
| task_update | ❌ | ✅ | ✅ | — |
| send_message / read_inbox / broadcast | ✅ | ✅ | ✅ | — |
| spawn_teammate / shutdown_teammate / approve_plan | ✅ | ❌ | ❌ | — |
| request_plan_approval / shutdown_response | ❌ | ✅ | ✅ | — |
| ask_clarification | ✅ | ❌ | ❌ | ✅ |
| Agent（子 agent） | ❌（有意） | ✅（≤3 并发） | ❌ | ✅ |
| cron | ❌ | ❌ | ❌ | ✅ |
| files（写入） | ❌（白名单） | ✅ | ❌（白名单） | ✅ |
| files_readonly / search | ✅ | ✅ | ✅ | ✅ |
| memory_search | ✅ | ✅ | ✅ | ❌ |
| project_info / project_memory_search | ❌ | ❌ | ❌ | ✅（只读） |

## 6. 关键机制索引

| 机制 | 位置 |
|---|---|
| 懒加载 `_ensure_teammate` / `_ensure_verifier` | orchestrator.py |
| triage/自处理判断 | orchestrator.py `run()`（`[Lead 独立完成]` 前缀约定） |
| 澄清暂停/恢复 (TTL 1800s) | orchestrator.py `resume()` + teammate_agent `respawn()` |
| crash 恢复 | task_store `recover_orphaned_tasks` + orchestrator `_resume_interrupted_tasks` |
| 死锁检测 watchdog | orchestrator.py `_watchdog`（300s 无进展） |
| 防漂移复述 (每 5 轮) | teammate_agent.py `CoordinatorReminderMiddleware` |
| inbox 程序式 drain | teammate_agent.py `InboxDrainMiddleware` |
| L1/L3 成员记忆 | harness/memory/member_memory.py |
| skill 进化 | harness/skills/evolution/member.py |
| worktree 隔离（可选） | harness/team/worktree.py（默认 shared） |
