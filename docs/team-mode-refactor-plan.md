# Team 模式重构总体方案

> 状态：六个阶段全部实施完成（2026-07-31）
> 依据：mewcode 多 agent 架构分析 + 本项目 team 模式现状审计
> 范围：`harness/team/`、`harness/memory/`、`harness/middleware/dynamic_context.py`、`harness/tools/builtins/`、前端任务板展示

## 0. 已确认的决策（拍板项）

| # | 决策点 | 结论 |
|---|--------|------|
| 1 | 领域匹配存废 | 去除 orchestrator 兜底领域匹配，Lead 按成员能力边界全权分配；保留 AgentCard 生成（Lead 决策的信息源） |
| 2 | 任务格式 | 结构化 JSON 任务（背景/目标/描述/注意事项/格式约束/验收标准），schema 校验 + 失败降级纯文本 |
| 3 | Review 机制 | 风险分级 + 独立 Verifier 验收，**不采用** member 自评不确定性直通（自评 calibration 不可靠） |
| 4 | 记忆分层 | 按项目×成员分离（L0-L3 四层，见 §3） |
| 5 | L3→L1 晋升 | **程序计数**判定 + LLM 只做泛化改写，LLM 无权直接晋升 |
| 6 | 单 agent 对项目 | **只读**（索引注入 + 只读工具），不写任务板/团队记忆 |
| 7 | worktree 隔离 | 成员级可选配置，默认共享 thread 工作区（保留协作产物互见特性） |
| 8 | 跨进程 mailbox | 不做（无外部 worker 进程需求） |

## 1. 设计原则

**借鉴 mewcode 的（约束与协议）**：
- Lead 纯协调者：工具级硬约束（非 prompt 恳求）+ 周期性 prompt 复述防漂移
- 独立 Verification worker 验收："实现者不得验证自己的代码"做成硬约束
- 自包含任务委派：禁止"based on your findings"式懒惰委托
- per-agent 上下文压缩，上下文当稀缺资源分配

**保留本项目已更强的（调度与韧性）**：
- 任务板 + 依赖 + dispatch + claim CAS（mewcode 无调度器，依赖靠自觉）
- INTERRUPTED / crash 恢复 / 重试（mewcode 无失败自愈）
- progress_event 事件驱动唤醒（mewcode 是 0.5s 轮询）
- 澄清暂停/恢复（ask_clarification + TTL + resume/respawn）
- 团队记忆 L2 / 任务记忆 / AgentCard 体系

## 2. 目标架构

```
用户 ⇄ Lead（纯协调者：协调工具 + ask_clarification，无执行类工具）
        │  delegate(JSON 任务) / message (mailbox)
        ├──── Member A ──┬─ L1 成员全局记忆 + L3 项目×成员记忆
        │                └─ 私有技能库（可进化：probation → 转正）
        ├──── Member B ──┤ ...
        └──── Verifier（独立上下文验收，强制证据校验，输出 VERDICT）

任务板（保留：依赖 / dispatch / claim CAS / recover_orphaned_tasks）
单 agent（只读消费者：项目索引注入 + project_info / project_memory_search）
```

## 3. 记忆四层体系（项目×成员分离）

```
users/{uid}/
  memory.json                          # L0 用户全局（现有）— 偏好、习惯、跨项目事实
  agents/{agent}/memory.json           # L1 成员全局（新增）— 跨项目通用经验
  projects/{pid}/memory/
    team_memory.json                   # L2 项目团队记忆（现有）— 协作教训
    members/{agent}.json               # L3 项目×成员记忆（新增）— 项目内领域经验
    tasks/*.json                       # 任务记忆（现有）— 结构化 result JSON 存档
```

| 层 | 内容 | 注入谁 | 注入方式 |
|----|------|--------|----------|
| L0 | 用户偏好、习惯 | 单 agent + Lead | 全量（现有逻辑不变） |
| L1 | 成员跨项目通用经验 | 该成员（所有项目） | 全量（体量小、稳定） |
| L2 | 项目协作教训（谁擅长什么、配合踩坑） | Lead 为主 | 全量（≤20 条上限，现有） |
| L3 | 项目内领域经验（本项目 API 用 JWT 等） | 该成员（仅本项目） | 按任务相关性检索，不全量灌 |

**写入与晋升**（原料统一为任务完成的 result JSON，不再额外跑 LLM 总结全对话）：

1. 任务完成 → result JSON 提取经验 → 写 **L3**
2. L3 晋升 L1（程序计数 + LLM 泛化改写）：
   - 程序维护计数：同类经验（按标签/语义指纹去重）出现 ≥2 个项目 或 被复用 ≥N 次（N 可配，默认 3）
   - 达标后由 LLM **只做泛化改写**（去除项目特定细节，抽象为通用经验）写入 L1
   - LLM 无晋升决定权；晋升记录审计日志
3. 协作配合类教训 → 写 **L2**（现有 `_extract_team_memory` 路径改造）
4. L1 中稳定复用的程序化流程 → skill 进化候选（接 Phase 5）

**分工边界**：L2 = 怎么配合（协作协议层）；L3/L1 = 怎么干活（领域能力层）。成员换项目：L1 带走，L3 隔离，避免跨项目污染。

## 4. 单 agent 项目感知（只读）

现状：`execute(project_id)` 仅 team 模式使用；单 agent 只有 L0 全局记忆，无项目通道。

- **索引注入**：单 agent 的 DynamicContext 增加 `<projects>` 块（项目名/描述/成员名单，每项目几十 token）
- **只读工具**（注册进单 agent 工具组）：
  - `project_info(project_id)` → project.json + AgentCard 摘要 + L2 practices/pitfalls
  - `project_memory_search(project_id, query)` → 复用 `TaskMemoryStore.find_related` 跨项目检索任务记忆
- **硬约束**：单 agent 对 `projects/` 下所有状态只读，防止与 team run 并发写冲突

## 5. 分阶段实施计划

### Phase 1：Lead 协调者化 ✅（已完成，test_lead_coordinator.py 12 测试）
- `LEAD_ALLOWED_TOOL_GROUPS` 收窄：去掉 `files`，Lead 只留协调工具 + ask_clarification + memory_search
- 新增 coordinator 调度 prompt（借鉴 mewcode `coordinator.py`）：四阶段工作流、continue-vs-spawn 决策、禁止懒惰委托
- 防漂移：长会话每 N 轮复述调度指引精简版
- 改动：`harness/team/orchestrator.py`、`harness/team/teammate_agent.py`（`_get_lead_instructions`）

### Phase 2：任务协议 JSON 化 ✅（已完成，test_task_protocol.py 15 测试）
- `task_create` + `delegate_to_member` 合并为结构化委派：`{background, goal, description, constraints, format, acceptance_criteria}`
- schema 校验（pydantic），失败降级纯文本 description
- `TeamTask` 增加 `spec` JSON 字段，tasks.json 兼容历史任务（无 spec 按纯文本处理）
- member 完成输出 result JSON：`{status, output, evidence, uncertainty, failure_reason}`
- 前端任务板同步适配 spec 展示
- 改动：`harness/team/tools.py`、`harness/team/models.py`、`harness/team/task_store.py`、`frontend/src`（任务板组件）

### Phase 3：验收改为风险分级 + 独立 Verifier ✅（已完成，test_team_verification.py 29 测试；Verifier 已平台内置化 __team_verifier__，高危任务禁止直达 COMPLETED）
- 风险分级：只读/探索类 → member 自报 + 程序校验证据（文件存在性、命令可复跑）直通；写操作/交付物/有下游依赖 → 强制 Verifier 验收
- Verifier 为独立上下文成员（看不到实现过程，只验收），强制输出 VERDICT
- 状态机调整：`completed` 直达终态的条件分支，`_VALID_TRANSITIONS` / REVISION_NEEDED / `_is_complete` / deadlock 检测联动更新
- 不确定性字段降级为参考信号，不作为直通依据
- 改动：`harness/team/orchestrator.py`、`harness/team/tools.py`、`harness/team/models.py`

### Phase 4：记忆分层（L1/L3）+ 单 agent 项目感知 ✅（已完成，test_member_memory.py 27 + test_project_awareness.py 18 测试）
- 新增 `harness/memory/member_memory.py`：L1/L3 读写、语义指纹去重、程序计数晋升、LLM 泛化改写、审计日志
- `teammate_agent.py`：prompt 组装注入 L1 全量 + L3 检索结果；任务完成时从 result JSON 写 L3
- `orchestrator.py`：`_extract_team_memory` 拆分 L2（协作教训）/ L3（按 assigned_agent 路由）
- `dynamic_context.py`：单 agent `<projects>` 索引块
- `harness/tools/builtins/`：新增 `project_info` / `project_memory_search` 只读工具
- `memory_config`：L1/L3 容量、晋升阈值、检索阈值参数

### Phase 5：Skill 自进化 ✅（已完成，test_skill_evolution.py 31 测试；核心在 harness/skills/evolution/member.py）
- 候选提取：L1 中稳定复用的程序化流程 → 候选 SKILL.md（agent 级技能库）
- 试用期（probation）：注入 prompt 标注"试验性"；成功使用 ≥N 次转正，连续失败/长期未用归档
- 安全闸：转正需 Lead 审批（复用 plan_approval 通道）；含写操作的 skill 需用户确认
- spawn 自检：skill 加载数量/白名单过滤/prompt 注入确认，失败时降级并更新 AgentCard（ Lead 看到真实能力）
- 改动：`harness/skills/`、`harness/team/teammate_agent.py`、`harness/team/tools.py`

### Phase 6（可选）：成员级 worktree 隔离 ✅（已完成，test_team_worktree.py 21 测试；核心在 harness/team/worktree.py，默认 shared 零行为变化）
- 成员配置增加 `isolation: worktree|shared`，默认 shared
- 写代码类成员可选独立 worktree，删除时回收
- 改动：`harness/team/orchestrator.py`、agent 配置 schema

## 6. 实施顺序与依赖

```
Phase 1 ──→ Phase 2 ──→ Phase 4 ──→ Phase 3 ──→ Phase 5 ──→ Phase 6(可选)
              │            │
              └─ result JSON 是 3/4/5 的共同原料
                           └─ L1/L3 记忆 + 单agent感知同层推进
```

每 Phase 独立可交付、可回滚；Phase 2 未就位前不动 3/5。

## 7. 测试策略

- 基线守护：`harness/tests -k "team or task_store or teammate or message_bus"` 当前 32 failed（用户 WIP）/ 49 passed——任何 Phase 不得新增失败；`app/tests` 保持 79 passed
- 每 Phase 配套新增测试：schema 校验降级、晋升计数、风险分级路由、Verifier 流程、只读工具权限
- 前端：`npx tsc --noEmit` 通过
- 集成验证：真实 team run 冒烟（简单问候自处理 / 拆解委派 / 验收 / 记忆写入与晋升）

## 8. 风险清单

| 风险 | 缓解 |
|------|------|
| LLM 产 JSON 不可靠 | schema 校验 + 纯文本降级，降级路径先于主路径实现 |
| 错误经验泛化污染 L1 | 程序计数门槛 + LLM 仅改写 + 审计日志 + 可人工清除 |
| Verifier 误放行/误杀 | VERDICT 必须附证据；争议时升级 Lead → 用户 |
| 状态机改造波及面大（Phase 3） | 单独 Phase，状态流转图先画后改，全量回归 |
| 简单任务被重型协议拖累 | 轻重两档：Lead 判断简单任务只填必填字段，只读类免验收 |
| tasks.json 历史数据兼容 | 无 spec 字段按纯文本处理，不迁移旧数据 |
