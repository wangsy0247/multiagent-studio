# Agent Team 全系统分析

> 分析时间：2026-07-10
> 覆盖范围：`harness/team/` 全部模块 + `harness/main.py` Team 集成 + 相关数据模型

---

## 目录

1. [架构概览](#一架构概览)
2. [完整生命周期](#二完整生命周期)
3. [Agent 间通信与协调](#三agent-间通信与协调)
4. [Agent 与任务状态管理](#四agent-与任务状态管理)
5. [冲突预防](#五冲突预防)
6. [死循环与死锁防护](#六死循环与死锁防护)
7. [兜底与边界条件](#七兜底与边界条件)
8. [已知待完善点](#八已知待完善点)

---

## 一、架构概览

### 1.1 分层结构

```
HarnessService (main.py)
  ├── mode="single" → 现有 LeadAgent + SubAgent 路径（不变）
  └── mode="team"   → _execute_team()
                         └── TeamOrchestrator
                               ├── TeamTaskStore      ← 持久化任务板
                               ├── TeamMessageBus     ← 消息总线
                               ├── TeamContext        ← 共享上下文
                               ├── ProjectLeadAgent   ← Team 模式 Lead
                               └── MemberAgentExecutor[] ← 每个 Member 一个
                                     └── SubagentExecutor (复用现有引擎)
```

### 1.2 新增文件清单

| 文件 | 职责 |
|---|---|
| `harness/team/models.py` | TeamTask、TeamMessage、TeamMemberRuntime 数据模型 |
| `harness/team/task_store.py` | 任务板持久化 + 依赖 DAG 解析 + 原子更新 + 环检测 |
| `harness/team/message_bus.py` | Agent 间消息 JSONL 持久化 + 实时通知 + 循环检测 |
| `harness/team/context.py` | TeamContext 数据类 + prompt 注入片段生成 |
| `harness/team/member_executor.py` | 封装 SubagentExecutor，注入 SOUL + Team 上下文 |
| `harness/team/orchestrator.py` | 核心调度循环 + 3 项 watchdog 检测 |
| `harness/team/tools.py` | 8 个 Team 专用 LangChain 工具 |
| `harness/team/project_lead_agent.py` | Team 模式 Lead Agent system prompt + 工具组合 |

---

## 二、完整生命周期

### 2.1 TeamOrchestrator 生命周期

```
构造 → 初始化 → 调度循环 → 合成 → 终止
```

#### 阶段 1：构造 (`__init__`)

```python
# orchestrator.py:76-110
def __init__(self, project_id, thread_id, user_id, *,
             llm_factory, tool_registry, subagent_manager, skill_storage):
```

- 接收外部依赖但不执行任何 I/O
- 立即创建 `TeamTaskStore(project_id, user_id)` — 仅设置路径，不读文件
- 立即创建 `TeamMessageBus(project_id, user_id)` — 仅设置路径，不读文件
- `members` 和 `_member_executors` 为空字典

**边界条件**：构造阶段不会失败 — 所有初始化延迟到 `initialize()`。

#### 阶段 2：初始化 (`initialize()`)

```python
# orchestrator.py:116-192
async def initialize(self):
```

执行流程：

```
1. 加载项目 JSON
   └── 不存在 → raise ValueError("Project not found")
       └── 上层 (main.py _execute_team) 捕获 → 降级为单 Agent 模式

2. 验证成员列表
   └── 空列表 → warning 日志，继续执行（后续调度循环立即结束）

3. 为每个 member 创建 TeamMemberRuntime
   ├── 根据 AgentConfig.can_be_lead 决定 role ("lead" | "member")
   └── 初始状态均为 "idle"

4. 构建 TeamContext（一次性，所有 member 共享引用）

5. 为每个 member 创建 MemberAgentExecutor
   ├── load_agent_config(name) → AgentConfig
   ├── load_agent_soul(name) → SOUL.md 内容
   ├── llm_factory(model) → LLM 实例
   ├── tool_registry.get_tools_by_category(group) → 工具列表
   └── 单个 member 初始化失败 → 标记 status=failed, last_error
       其他 member 不受影响，继续初始化
```

**边界条件**：

| 场景 | 行为 |
|---|---|
| 项目 JSON 不存在 | `ValueError` → 上层降级为单 Agent |
| 成员列表为空 | 正常返回，`run()` 立即结束 |
| 某个 member 的 AgentConfig 不存在 | 该 member 标记 `failed`，其他正常 |
| 某个 member 的 LLM 创建失败 | 该 member 标记 `failed`，其他正常 |
| 所有 member 初始化都失败 | `run()` 中 `_member_executors` 为空，调度循环无可用 worker，最终 watchdog 检测到死锁并终止 |

#### 阶段 3：调度循环 (`run()`)

```python
# orchestrator.py:198-313
async def run(self, message: str) -> AsyncIterator[dict[str, Any]]:
```

完整的 SSE 事件流：

```
yield team_start              ← {type: "team_start", project_id, members, mode}
    ↓
Phase 1: Planning
    yield team_status(planning)
    task_store.create_task(用户消息)  ← 将用户目标写入任务板
    yield team_task_update
    ↓
Phase 2: Dispatch Loop
    asyncio.create_task(watchdog)     ← 启动后台看门狗
    while not is_complete() and not cancelled:
        _round += 1
        if _round > 100 → yield team_error + break   ← 硬上限
        
        ready_tasks = get_ready_tasks()  ← 依赖已完成的 PENDING 任务
        
        for task in ready_tasks:
            if task.assigned_agent and member.idle:
                → _run_member_task(agent, task)     ← asyncio.create_task (非阻塞)
            elif not task.assigned_agent:
                agent = _select_idle_agent()         ← 负载均衡
                → task.assigned_agent = agent
                → _run_member_task(agent, task)
        
        # 处理消息总线
        for member in members:
            unread = message_bus.get_unread(member)
            yield team_message for each unread
        
        # 排空 watchdog 事件队列
        while event_queue:
            yield event_queue.get()
        
        if dispatched == 0 and not ready_tasks:
            await asyncio.sleep(0.2)  ← 空闲等待
    ↓
Phase 3: Synthesis
    yield team_status(synthesizing)
    ↓
finally:
    watchdog_task.cancel()
    yield team_end(status=completed|cancelled|error)
```

**关键设计**：
- `_run_member_task` 通过 `asyncio.create_task` 在后台运行，主循环不阻塞等待
- 每轮调度最多 0.2s（空闲时 sleep），繁忙时立即进入下一轮
- watchdog 与主循环并行运行，通过 `_event_queue` 异步通知

#### 阶段 4：终止 (`cancel()`)

```python
# orchestrator.py:449-459
async def cancel(self):
    self._cancelled = True
    for name, member in self.members.items():
        if member.status == "busy":
            member.status = "idle"
            member.current_task_id = None
```

取消传播链：
```
用户 stop()
  → HarnessService.stop() → _active_runs[thread_id]["cancelled"] = True
  → orchestrator.cancel()  → self._cancelled = True
  → 主循环 while 条件检测 → break
  → watchdog 检测 _cancelled → return
```

### 2.2 Member Agent 单次任务生命周期

```
idle → busy → idle
       │
       ├── SUCCESS → task=COMPLETED + completed_tasks++
       ├── 失败 + retry<3 → task=PENDING (重试, 冷却 2s)
       └── 失败 + retry≥3 → task=FAILED + 广播通知
```

`_run_member_task()` 详细流程 (`orchestrator.py:319-418`)：

```
1. 入口校验
   ├── member 不存在 → return（静默跳过）
   └── executor 不存在 → logger.error + return

2. 状态更新
   ├── member.status = "busy"
   ├── member.current_task_id = task.id
   ├── task_store.update_task(task.id, status=IN_PROGRESS)
   └── yield member_status + team_task_update (SSE)

3. 指令构建
   ├── base = "【任务】{title}\n\n{description}"
   └── if dependencies:
         for dep_id in dependencies:
             注入已完成依赖的 output 文本
         instruction += "\n【依赖任务结果】\n" + dep_results

4. 执行
   └── result = await executor.execute(instruction, task)
       └── 内部: SubagentExecutor 在隔离 daemon 线程的 event loop 中运行
       └── 通过 asyncio.to_thread 避免阻塞主循环

5. 结果处理
   ├── result.status == SUCCESS:
   │     task → COMPLETED
   │     member.completed_tasks++
   │     _last_progress_at = now()  ← 重置死锁计时器
   │
   ├── 失败 + retry_count < max_retries(3):
   │     task → PENDING (重试)
   │     asyncio.sleep(2)  ← 冷却
   │
   └── 失败 + retry_count >= max_retries:
         task → FAILED
         message_bus.send(broadcast, "任务失败: {error}")

6. 异常捕获
   └── except Exception:
         task → FAILED
         member.last_error = str(exc)

7. finally (保证执行)
   ├── member.status = "idle"
   ├── member.current_task_id = None
   ├── yield member_status (SSE)
   └── yield team_task_update (SSE)
```

**关键保证**：`finally` 块确保无论执行成功、失败还是崩溃，member 状态必定回到 `idle`，不会出现"僵尸 member"永久占用。

### 2.3 TeamTask 状态机

```
                    ┌─────────┐
                    │ PENDING │ ← 等待依赖完成或待分配
                    └────┬────┘
                         │ assign
                    ┌────▼──────┐
                    │ ASSIGNED  │ ← 已分配但未开始
                    └────┬──────┘
                         │ start
                    ┌────▼─────────┐
              ┌─────│ IN_PROGRESS  │─────┐
              │     └──────────────┘     │
          SUCCESS                    异常/FAIL
              │                          │
              │                   ┌──────▼──────┐
              │                   │ retry < 3?  │
              │                   └──────┬──────┘
              │                    YES    │    NO
              │                    │      │      │
              │               PENDING    │   FAILED
              │               (重试)     │   (广播)
              │                          │
         ┌────▼──────┐                   │
         │ COMPLETED │ ← 终态            │
         └───────────┘              ┌────▼──────┐
                                    │  FAILED   │ ← 终态
                                    └───────────┘
```

**终态定义** (`models.py:29-34`)：

```python
@property
def is_terminal(self) -> bool:
    return self in {TeamTaskStatus.COMPLETED, TeamTaskStatus.FAILED}
```

---

## 三、Agent 间通信与协调

### 3.1 通信架构

```
                 ┌──────────────────────────┐
                 │     TeamMessageBus        │
                 │  mailbox.jsonl (JSONL)    │  ← 持久化
                 │  asyncio.Event per agent  │  ← 实时通知
                 └──┬────────┬──────────┬────┘
                    │        │          │
            send()  │   get_unread()    │  mark_read()
           ┌────────▼──┐ ┌──▼──────┐ ┌─▼──────────┐
           │ LeadAgent │ │  Coder  │ │ Researcher │
           └─────┬─────┘ └───┬─────┘ └──┬─────────┘
                 │            │          │
     task_create │   task_update  task_update
     review_task│            │          │
           ┌────▼────────────▼──────────▼──────┐
           │         TeamTaskStore             │
           │    tasks.json (JSON + fcntl)      │  ← 持久化 + 文件锁
           └───────────────────────────────────┘
```

### 3.2 三条通信路径

#### 路径 A：任务板（TeamTaskStore）— 结构化协调

这是 Team 协作的**主通道**。所有 Agent 通过 tools 读写共享任务板来实现结构化协调。

**delegate_to_member 的完整调用链**：

```
1. LeadAgent 调用 delegate_to_member(coder, "修复登录BUG", task_id="t1")
                                  ↓
2. tools.py delegate_to_member():
   ├── 校验 task 存在且未被分配给其他 agent
   └── subagent_manager.execute(agent_name, instruction)
        ↓
3. SubagentManager.execute():
   ├── 获取 SubAgentConfig + LLM + Tools
   ├── 可选: 创建 git worktree
   ├── asyncio.to_thread(SubagentExecutor.execute)
   └── 返回 SubAgentResult (output + status)
        ↓
4. 结果返回给 LeadAgent (仅 output 文本)
```

**依赖任务结果传递** (`orchestrator.py:342-352`)：

```python
# 当前任务 B 依赖任务 A → A 的 output 注入到 B 的指令中
if task.dependencies:
    for dep_id in task.dependencies:
        dep_task = await self.task_store.get_task(dep_id)
        if dep_task and dep_task.output:
            dep_results.append(
                f"依赖任务 [{dep_id}] {dep_task.title} 的结果:\n{dep_task.output}"
            )
    instruction += "\n\n【依赖任务结果】\n" + "\n\n".join(dep_results)
```

#### 路径 B：消息总线（TeamMessageBus）— 非结构化通信

**三种消息模式**：

| 模式 | 实现 | 过滤规则 |
|---|---|---|
| 点对点 | `send_message(to_agent="coder", ...)` | 只发给 `to_agent` |
| 广播 | `broadcast(content="全体注意")` (`to_agent=None`) | 发给除发送者外的所有人 |
| 任务通知 | `msg_type=TASK_UPDATE` + `task_id=...` | 与任务关联的消息 |

**未读消息追踪** (`message_bus.py:150-154`)：

```python
async def get_unread(self, agent_name: str) -> list[TeamMessage]:
    all_msgs = await self.get_messages(agent_name=agent_name)
    read_ids = self._read_cursors.get(agent_name, set())  # 已读消息 ID 集合
    return [m for m in all_msgs if m.id not in read_ids]
```

游标持久化到 `.read_cursors.json`，崩溃后可恢复。

**实时通知机制** (`message_bus.py:83-111`)：

```python
async def send(self, message: TeamMessage):
    # 1. 追加写入 JSONL（天然并发安全）
    with open(self._file, "a") as f:
        f.write(message.model_dump_json() + "\n")
    
    # 2. 触发实时通知（push 模式）
    if message.to_agent:
        event = self._events.get(message.to_agent)
        if event: event.set()        # 点对点：只通知收件人
    else:
        for name, event in self._events.items():
            if name != message.from_agent:
                event.set()          # 广播：通知除发送者外的所有人
```

#### 路径 C：SSE 事件流 — 外部可观测

调度器通过 SSE 事件向前端暴露内部状态：

| SSE 事件 | 触发时机 | 关键字段 |
|---|---|---|
| `team_start` | `run()` 开始 | `project_id`, `members[]`, `mode` |
| `team_status` | 阶段切换 | `phase` (planning/dispatching/synthesizing) |
| `team_task_update` | 任务创建/状态变更 | `task` (完整 TeamTask dump) |
| `member_status` | member 空闲/忙碌切换 | `agent_name`, `status`, `current_task_id` |
| `team_message` | 消息总线有新消息 | `from_agent`, `to_agent`, `content`, `task_id` |
| `team_error` | watchdog 检测到问题 | `content` (错误描述) |
| `team_end` | `run()` 结束 | `status` (completed/cancelled/error), `total_rounds` |

### 3.3 延迟特性

| 事件类型 | 通知机制 | 最大延迟 |
|---|---|---|
| SSE 事件 | `asyncio.Queue` push | 即时 |
| 消息通知 | `asyncio.Event.set()` push | 即时 |
| 任务板变更被调度器感知 | 主循环轮询 (`asyncio.sleep(0.2)`) | ~200ms |
| 新任务被分配 | 同上 | ~200ms |

---

## 四、Agent 与任务状态管理

### 4.1 Member Agent 运行时状态

| 字段 | 类型 | 用途 |
|---|---|---|
| `status` | `idle \| busy \| done \| failed` | 当前是否可分配新任务 |
| `current_task_id` | `str \| None` | 正在执行的任务（busy 时有值） |
| `completed_tasks` | `int` | 累计完成数 — 负载均衡排序依据 |
| `failed_tasks` | `int` | 累计失败数 — 健康度指标 |
| `last_error` | `str \| None` | 最近一次错误的详细信息 |
| `last_heartbeat` | `str \| None` | 最后活跃时间（ISO format） |

**状态转换**（全程由 TeamOrchestrator 管理）：

```
idle ──[_run_member_task 被调用]──→ busy ──[finally 块]──→ idle
                                       │
                                       └──[异常]──→ idle (last_error 记录异常)
```

**负载均衡** (`orchestrator.py:424-435`)：

```python
def _select_idle_agent(self) -> str | None:
    idle = [(name, m) for name, m in self.members.items()
            if m.status == "idle" and name in self._member_executors]
    if not idle:
        return None
    # 按已完成任务数升序 → 优先分配给轻负载的 member
    idle.sort(key=lambda item: item[1].completed_tasks)
    return idle[0][0]
```

### 4.2 TeamTask 状态管理 (8 种状态)

```python
class TeamTaskStatus(str, Enum):
    PENDING = "pending"          # 等待依赖完成或待分配
    ASSIGNED = "assigned"        # 已分配给 member，等待调度器触发执行
    IN_PROGRESS = "in_progress"  # member 正在执行
    REVIEWING = "reviewing"      # Lead 正在审阅结果
    MERGING = "merging"          # 正在合并 worktree 结果
    COMPLETED = "completed"      # 已完成（终态）
    FAILED = "failed"            # 执行失败（终态）
    BLOCKED = "blocked"          # 被阻塞
```

**并发安全** (`task_store.py:159-174`)：

```python
async def update_task(self, task_id: str, **fields) -> TeamTask | None:
    with open(self._file, "a+") as f:
        fcntl.flock(f, fcntl.LOCK_EX)    # 排他文件锁
        try:
            tasks = self._load_locked()  # 读
            for t in tasks:
                if t.id == task_id:
                    for key, value in fields.items():
                        setattr(t, key, value)  # 改
                    t.updated_at = _now_iso()
                    result = t
                    break
            if result is not None:
                self._save_locked(tasks)  # 写
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)  # 释放锁
```

**缓存一致性** (`task_store.py:95-111`)：

```python
async def load_tasks(self) -> list[TeamTask]:
    if self._file.exists():
        mtime = self._file.stat().st_mtime
        if self._cache is not None and mtime == self._cache_mtime:
            return list(self._cache)  # 缓存命中，避免 JSON 解析
    # 缓存未命中 → 加共享锁 → 重新加载
    with open(self._file, "a+") as f:
        fcntl.flock(f, fcntl.LOCK_SH)
        ...
```

### 4.3 TeamOrchestrator 内部调度状态

| 字段 | 类型 | 用途 |
|---|---|---|
| `_round` | `int` | 调度轮次计数器（硬上限 100） |
| `_cancelled` | `bool` | 取消标志（主循环 + watchdog 双重检测） |
| `_started_at` | `str` | 执行开始时间（watchdog 超时计算用） |
| `_last_progress_at` | `str` | 最后一次任务成功完成的时间（死锁检测用） |

---

## 五、冲突预防

### 5.1 文件系统并发冲突

| 资源 | 保护机制 | 实现位置 |
|---|---|---|
| `tasks.json` 写入 | `fcntl.flock(LOCK_EX)` 排他锁 | `task_store.py:160` |
| `tasks.json` 读取 | `fcntl.flock(LOCK_SH)` 共享锁 | `task_store.py:103` |
| `mailbox.jsonl` 写入 | JSONL 追加写入（每行独立，天然无竞态） | `message_bus.py:92` |
| `mailbox.jsonl` 读取 | 只读模式打开，无锁 | `message_bus.py:124` |
| 游标文件 `.read_cursors.json` | asyncio 单线程协程天然串行 | `message_bus.py:71-76` |

### 5.2 任务分配冲突

**双重防护**：

1. **工具层前置检查** (`tools.py:61-68`)：
   ```python
   if task.assigned_agent and task.assigned_agent != agent_name:
       return f"Error: Task is already assigned to '{task.assigned_agent}'"
   ```

2. **调度器原子分配** (`task_store.py:284-291`)：
   `assign_task()` 在持有 `LOCK_EX` 时完成"检查→分配→写入"的原子操作

### 5.3 工具权限隔离

| 角色 | 可用工具 | 限制 |
|---|---|---|
| **Project Lead** | ask_clarification + 8 Team 工具 + config.yaml 配置的基础工具 | 排除 `task`、`create_subagent` |
| **Member Agent** | `tool_groups` 配置的工具（如 files, code, shell） | 默认不含 `delegate_to_member` |
| **禁止递归委派** | Member 不能调用 `delegate_to_member` | 通过工具集配置控制 |

### 5.4 Member 异常隔离

每个 `_run_member_task` 在独立的 `asyncio.create_task` 中运行。`except Exception` 兜底 (`orchestrator.py:401-409`)：

```python
except Exception as exc:
    logger.exception("Member '%s' task execution crashed", agent_name)
    await self.task_store.update_task(task.id, status=FAILED, error=str(exc))
    member.failed_tasks += 1
    member.last_error = str(exc)
finally:
    member.status = "idle"  # 保证恢复空闲
```

**一个 member 崩溃不传播到**：
- 其他 member（各自独立的 `asyncio.Task`）
- TeamOrchestrator 主循环（被 `try/except` 包围）
- 上层 HarnessService（异常被捕获而非传播）

### 5.5 Worktree 写冲突

当前 `merge_result` 工具仅有骨架。完整实现后：
- 每个 member 可选 `isolation: worktree` → 独立 git worktree
- 合并通过 `GitWorktreeManager.merge()` 串行化（`asyncio.Lock`）
- 冲突时 LLM 裁决 → 失败则 `git merge --abort` 回滚

---

## 六、死循环与死锁防护

### 四层防线全景

```
┌──────────────────────────────────────────┐
│ 第一层：单 Member 级别                     │
│ SubagentExecutor.max_turns / timeout      │
│ + 精简 middleware 链 (含 LoopDetection)    │
├──────────────────────────────────────────┤
│ 第二层：TeamTask 级别                      │
│ retry_count < max_retries (3)            │
│ 重试冷却 2s                               │
├──────────────────────────────────────────┤
│ 第三层：Team 级别 (Watchdog, 5s 周期)      │
│ 整体超时 1800s / 死锁检测 120s / 依赖环    │
├──────────────────────────────────────────┤
│ 第四层：调度循环级别                        │
│ MAX_TEAM_ROUNDS 硬上限 (100)              │
└──────────────────────────────────────────┘
```

### 第一层：单 Member 级别

复用现有 `SubagentExecutor` 的内置防护：
- `recursion_limit = AgentConfig.max_turns`（默认 50）
- `future.result(timeout=AgentConfig.timeout_seconds)`（默认 900s）
- `cancel_event` 协作取消（每次 `astream` 迭代前检查）
- SubAgent 精简 middleware 链中的 `LoopDetectionMiddleware`

### 第二层：TeamTask 级别

```python
# orchestrator.py:370-399
task.retry_count += 1
if task.retry_count < task.max_retries:  # 默认 3
    await self.task_store.update_task(task.id, status=PENDING, error=...,
                                       retry_count=task.retry_count)
    await asyncio.sleep(2.0)  # 冷却，避免疯狂重试
else:
    # 重试耗尽 → 终态 FAILED + 广播通知 Lead
    await self.task_store.update_task(task.id, status=FAILED, ...)
    await self.message_bus.send(TeamMessage(
        msg_type=TASK_UPDATE,
        content=f"任务 {task.title} 执行失败: {result.error}",
        task_id=task.id,
    ))
```

### 第三层：Team 级别 Watchdog

```python
# orchestrator.py:465-514
async def _watchdog(self):
    while True:
        await asyncio.sleep(5)  # 每 5 秒巡检一次
        
        if self._cancelled:
            return  # 已被取消，退出
```

三项检查：

| 检查项 | 阈值 | 检测条件 | 动作 |
|---|---|---|---|
| **整体超时** | 1800s (30min) | `elapsed > OVERALL_TIMEOUT` | `_cancelled = True` → 调度循环终止 |
| **死锁检测** | 120s | `since_progress > DEADLOCK_TIMEOUT AND not ready_tasks AND busy_count == 0` | `_cancelled = True` + yield `team_error` |
| **依赖环检测** | 每次检查 | DFS 三色标记算法找到环 | `_cancelled = True` + yield `team_error` |

死锁检测逻辑：

```python
# orchestrator.py:488-503
if since_progress > DEADLOCK_TIMEOUT:
    ready = await self.task_store.get_ready_tasks()  # 有依赖已完成的 PENDING 任务?
    busy = sum(1 for m in self.members.values() if m.status == "busy")  # 有人在执行?
    if not ready and busy == 0:
        # 无人忙 + 无就绪任务 + 长时间无进展 = 死锁
        self._cancelled = True
```

依赖环检测算法 (`task_store.py:251-282`)：

```python
async def check_circular_dependency(self) -> list[list[str]]:
    # DFS 三色标记法
    WHITE, GRAY, BLACK = 0, 1, 2  # 未访问 / 访问中 / 已完成
    color: dict[str, int] = {tid: WHITE for tid in task_map}
    cycles: list[list[str]] = []
    
    def dfs(node, path):
        color[node] = GRAY
        path.append(node)
        for dep in task_map.get(node, []):
            if color[dep] == GRAY:           # 发现回边 → 环
                cycle_start = path.index(dep)
                cycles.append(path[cycle_start:] + [dep])
            elif color[dep] == WHITE:
                dfs(dep, path)
        path.pop()
        color[node] = BLACK
```

### 第四层：调度循环级别

```python
# orchestrator.py:238-243
if self._round > MAX_TEAM_ROUNDS:  # 100
    yield await self._emit_team_error("Team 执行超过最大轮次限制")
    break
```

### 消息循环检测

```python
# message_bus.py:198-214
async def check_message_loop(self, agent_a, agent_b, window=10):
    # 检查最近 4 条 A↔B 消息发送者是否交替 → A→B→A→B
    ab_msgs = [m for m in messages 
               if {m.from_agent, m.to_agent} == {agent_a, agent_b}]
    if len(ab_msgs) < 4:
        return False
    recent = ab_msgs[-4:]
    senders = [m.from_agent for m in recent]
    return senders == [agent_a, agent_b, agent_a, agent_b]
```

> **注意**：`check_message_loop` 已实现但 watchdog 尚未自动调用它，需要后续集成。

---

## 七、兜底与边界条件

### 7.1 初始化失败兜底

| 失败场景 | 行为 | 代码路径 |
|---|---|---|
| 项目 JSON 缺失 | `ValueError` → `main.py _execute_team` 捕获 → **降级为单 Agent** | `main.py:600-611` |
| 成员列表为空 | `initialize()` 正常返回，`run()` 立即结束循环 | `orchestrator.py:127-128` |
| 某个 member 的 config.yaml 读取失败 | 该 member 标记 `status=failed`，`last_error` 记录原因 | `orchestrator.py:183-187` |
| 某个 member 的 LLM 创建失败 | 同上 | 同上 |

**降级逻辑** (`main.py:600-611`)：

```python
except ValueError as exc:
    logger.warning("Team init failed, degrading to single-agent: %s", exc)
    yield {"type": "team_degrade", "reason": str(exc)}
    # 重新以单 Agent 模式执行
    async for event in self.execute(mode="single", ...):
        yield event
```

### 7.2 执行失败兜底

| 失败场景 | 捕获位置 | 行为 |
|---|---|---|
| Member 执行返回非 SUCCESS | `_run_member_task` 分支判断 | 自动重试（最多 3 次）→ 标记 FAILED + 广播 |
| Member 执行抛出未捕获异常 | `except Exception` in `_run_member_task` | 标记 FAILED + 记录 `last_error` |
| TeamOrchestrator.run() 异常 | `except Exception` in `run()` | yield `team_error` 事件 |
| 消息总线 JSONL 行损坏 | `model_validate_json` except | `continue` 跳过该行 |
| 任务文件损坏 | `json.JSONDecodeError` in `_read()` | 返回空列表 + warning |

### 7.3 数据一致性兜底

| 场景 | 机制 |
|---|---|
| 进程崩溃后任务恢复 | `tasks.json` 每次写操作后由 OS 保证 fsync |
| 进程崩溃后消息恢复 | JSONL 追加写入，每行独立完整，崩溃不丢数据 |
| 任务文件被外部损坏 | `_read()` 返回 `[]` + warning 日志，不崩溃 |
| 游标文件被损坏 | `_load_cursors()` 静默回退到空字典 |
| member `finally` 块 | 保证状态回到 `idle`，无论执行成功/失败/崩溃 |
| 文件 mtime 缓存 | 读操作先检查 mtime，过期才重新解析 JSON |

### 7.4 完整边界条件矩阵

| 边界 | 当前行为 | 代码位置 |
|---|---|---|
| `_select_idle_agent()` 无可用 member | 返回 `None`，主循环 `sleep(0.2)` 等待 | `orchestrator.py:431-432` |
| 所有任务终态但 member 仍在 busy | `_is_complete()` 返回 `False`（检测 busy 状态） | `orchestrator.py:437-443` |
| 依赖引用了不存在的 task ID | `check_circular_dependency` DFS 跳过不在 `task_map` 中的引用 | `task_store.py:267-268` |
| 重复分配同一任务 | `delegate_to_member` 检查 `assigned_agent` 是否已占用 | `tools.py:62-68` |
| `message_bus` 文件被外部删除 | `get_messages()` 返回空列表 | `message_bus.py:120-121` |
| 空项目进入 Team 模式 | `initialize()` 正常，`run()` 空调度循环立即结束 | `orchestrator.py:286-287` |

### 7.5 Team 调度器总异常处理

```
_orchestrator.run()
  ├── try:
  │     ├── Phase 1: Planning
  │     ├── Phase 2: Dispatch (try/finally → watchdog cleanup)
  │     └── Phase 3: Synthesis
  ├── except Exception:
  │     └── yield team_error
  └── finally:
        └── yield team_end(status=completed|cancelled|error)

_main._execute_team()
  ├── try:
  │     └── orchestrator.run()
  ├── except ValueError:
  │     └── 降级为单 Agent
  ├── except asyncio.CancelledError:
  │     └── orchestrator.cancel() + yield team_end(cancelled)
  ├── except Exception:
  │     └── yield team_error
  └── finally:
        └── _active_runs.pop(thread_id)  ← 释放运行时资源
```

---

## 八、已知待完善点

### 8.1 P0 — 影响核心功能

| 问题 | 位置 | 影响 |
|---|---|---|
| **`cancel()` 未传播到 SubagentExecutor** | `orchestrator.py:452-458` 中的 `pass` | 用户点击停止后，正在运行的 member 不会真正停止 |
| **Member 未注入 Team 工具** | `member_executor.py` 只使用 `tool_groups` 配置的基础工具 | Member 无法调用 `send_message`、`task_update` 等 Team 工具 |

### 8.2 P1 — 功能不完整

| 问题 | 位置 |
|---|---|
| **`merge_result` 仅有骨架** | `tools.py:320-343`，未集成 `GitWorktreeManager` |
| **`send_message`/`broadcast` 的 `from_agent` 固定为 `"system"`** | `tools.py:239,268`，需从运行时上下文获取真实 agent name |
| **Watchdog 未调用 `check_message_loop`** | 方法已实现但未集成到 `_watchdog()` 检查中 |

### 8.3 P2 — 优化

| 问题 | 位置 |
|---|---|
| **TaskStore 使用 `a+` 模式打开已有文件进行锁定** | `task_store.py:102`，在空文件场景下行为正常但有轻微语义开销 |
| **缺少 Team 级别 token budget** | 当前仅 member 独立有 `max_turns`，无 Team 整体 token 预算 |

---

## 附录：关键常量速查

| 常量 | 值 | 位置 |
|---|---|---|
| `MAX_RETRIES` | 3 | `orchestrator.py:45` |
| `MAX_TEAM_ROUNDS` | 100 | `orchestrator.py:46` |
| `OVERALL_TIMEOUT` | 1800s (30 min) | `orchestrator.py:47` |
| `DEADLOCK_TIMEOUT` | 120s (2 min) | `orchestrator.py:48` |
| `TASK_RETRY_DELAY` | 2.0s | `orchestrator.py:49` |
| Watchdog 巡检间隔 | 5s | `orchestrator.py:468` |
| 调度循环空闲等待 | 0.2s | `orchestrator.py:288` |
| 默认 `max_turns` (Agent) | 50 | `agents_config.py` |
| 默认 `timeout_seconds` (Agent) | 900s | `agents_config.py` |
| 默认 `max_retries` (Task) | 3 | `models.py:55` |
