# MultiAgent-Studio 记忆系统详细总结

## 一、架构全景

记忆系统由 **6 个核心模块 + 2 个中间件 + 1 个安全模块** 构成，实现了一套 LLM 驱动的长期记忆机制。

```
                        ┌─────────────────────────────┐
                        │   config.yaml (运行配置)       │
                        │   defaults.py (L0 硬编码)      │
                        │   users/{uid}/config.yaml(L1)  │
                        │   agents/{name}/config.yaml(L2)│
                        └─────────────┬───────────────┘
                                      │
              ┌───────────────────────┼───────────────────────┐
              ▼                       ▼                       ▼
   ┌──────────────────┐   ┌──────────────────┐   ┌──────────────────┐
   │  MemoryConfig    │   │FileMemoryStorage │   │   mem0 Client    │
   │  (全局单例)       │   │  (memory.json)   │   │   (pgvector)     │
   └──────────────────┘   └──────────────────┘   └──────────────────┘
              │                       │                       │
              └───────────────────────┼───────────────────────┘
                                      │
          ┌───────────────────────────┴───────────────────────────┐
          │                                                       │
          ▼                                                       ▼
┌──────────────────────┐                              ┌──────────────────────┐
│ DynamicContextMW     │  ← 读路径                      │ MemoryMiddleware     │  ← 写路径
│ abefore_agent        │  (abefore_agent 钩子)          │ aafter_agent         │  (aafter_agent 钩子)
│ 注入记忆到 system     │                              │ 排队更新记忆到队列     │
│ prompt 的             │                              │                      │
│ <system-reminder>    │                              └──────────┬───────────┘
└──────────────────────┘                                        │
                                                                ▼
                                                      ┌──────────────────┐
                                                      │ MemoryUpdateQueue│
                                                      │ (debounce 120s)  │
                                                      └────────┬─────────┘
                                                               │
                                                               ▼
                                                      ┌──────────────────┐
                                                      │  MemoryUpdater   │
                                                      │  LLM 提取事实      │
                                                      └────────┬─────────┘
                                                               │
                                              ┌────────────────┼────────────────┐
                                              ▼                                 ▼
                                     memory.json (file)                  mem0 (pgvector)
                                    (FileMemoryStorage)                (mem0 client)
```

**数据流核心原则**：读路径和写路径解耦，通过 `MemoryConfig` 全局单例协调。

---

## 二、模块清单

| 模块 | 文件 | 职责 |
|------|------|------|
| **MemoryConfig** | `harness/config/memory_config.py` | 全局记忆配置单例，所有模块通过 `get_memory_config()` 读取 |
| **FileMemoryStorage** | `harness/memory/storage.py` | JSON 文件读写，mtime 缓存，fcntl 文件锁，惰性 TTL 清理 |
| **MemoryUpdateQueue** | `harness/memory/queue.py` | asyncio 去抖队列，合并同一 thread 的重复更新 |
| **MemoryUpdater** | `harness/memory/updater.py` | LLM 驱动的记忆提取，JSON 解析、去重、置信度过滤 |
| **prompt** | `harness/memory/prompt.py` | `MEMORY_UPDATE_PROMPT` 模板 + `format_memory_for_injection()` 注入格式化 |
| **mem0_client** | `harness/memory/mem0_client.py` | mem0 向量数据库客户端（pgvector 后端），惰性初始化 |
| **safety** | `harness/memory/safety.py` | 三层正则安全检测（提示注入 / 凭证外泄 / 不可见字符） |
| **project_storage** | `harness/memory/project_storage.py` | 项目记忆渐进式加载（仅 Team 模式） |
| **DynamicContextMiddleware** | `harness/middleware/dynamic_context.py` | 读路径入口 — 在 `abefore_agent` 注入记忆 + 日期 + 项目记忆 |
| **MemoryMiddleware** | `harness/middleware/memory.py` | 写路径入口 — 在 `aafter_agent` 排队增量更新 |

---

## 三、存储文件布局

```
{memory_root}/                           ← 默认 ~/.multiagent-studio/memory/
└── users/
    └── {user_id}/
        ├── memory.json                  ← 用户级记忆（agent_name=None 时使用）
        └── agents/
            ├── {agent_name}/
            │   ├── config.yaml          ← Agent 配置
            │   └── memory.json          ← Agent 专属记忆
            └── __team_lead__/
                └── memory.json          ← Team 模式 Lead Agent 专属记忆

{data_root}/                             ← 默认 ~/.multiagent-studio/
└── users/
    └── {user_id}/
        └── projects/
            └── {project_id}/
                ├── project.json         ← 项目定义 (name, members, description)
                └── memory/
                    └── description.md   ← 项目记忆 (仅 Team 模式加载)
```

### 记忆文件隔离规则

| 场景 | agent_name | 加载路径 |
|------|-----------|----------|
| 单 Agent 模式 (default) | `"default"` (按 Agent 名) | `users/{uid}/agents/default/memory.json` |
| 单 Agent 模式 (指定 Agent) | `"{agent_name}"` | `users/{uid}/agents/{agent_name}/memory.json` |
| Team 模式 Lead | `"__team_lead__"` | `users/{uid}/agents/__team_lead__/memory.json` |
| Team 模式 Member | `"{member_name}"` | `users/{uid}/agents/{member_name}/memory.json` |
| 语言检测（系统提示构建） | `None` (硬编码) | `users/{uid}/memory.json` |

> **注意**：`LeadAgent._build_language_section()` 中硬编码了 `get_memory_data(agent_name=None)`，语言偏好始终从用户级记忆读取。

---

## 四、memory.json 数据结构

```json
{
  "version": "1.0",
  "lastUpdated": "2026-07-24T16:00:00.000Z",
  "user": {
    "workContext": {
      "summary": "Full-stack developer, 主要在 multiagent-studio 项目上工作",
      "updatedAt": "2026-07-24T16:00:00.000Z"
    },
    "personalContext": {
      "summary": "偏好中文交流，简洁风格",
      "updatedAt": "2026-07-24T16:00:00.000Z"
    },
    "topOfMind": {
      "summary": "正在重构记忆系统，添加安全检测",
      "updatedAt": "2026-07-24T16:00:00.000Z"
    },
    "avoidances": {
      "summary": "不要过度解释简单概念，避免使用 sudo 运行 docker 命令",
      "updatedAt": "2026-07-24T16:00:00.000Z"
    }
  },
  "history": {
    "recentWeeks": {
      "summary": "过去几周的活动摘要 (4-6 句)",
      "updatedAt": "2026-07-24T16:00:00.000Z"
    },
    "earlierContext": {
      "summary": "更早的重要历史模式 (3-5 句)",
      "updatedAt": "2026-07-24T16:00:00.000Z"
    },
    "longTermBackground": {
      "summary": "持久背景和基础上下文 (2-4 句)",
      "updatedAt": "2026-07-24T16:00:00.000Z"
    }
  },
  "facts": [
    {
      "id": "fact_a1b2c3d4",
      "content": "用户偏好 TypeScript 而非 JavaScript",
      "category": "preference",
      "confidence": 0.95,
      "createdAt": "2026-07-24T12:00:00.000Z",
      "source": "thread_xxx",
      "sourceError": "曾经错误地使用 JavaScript 导致用户纠正"
    }
  ]
}
```

### Facts 分类体系

| 分类 | 含义 | 示例 |
|------|------|------|
| `preference` | 用户偏好 | "用户偏好简洁的中文回复" |
| `knowledge` | 用户知识背景 | "用户熟悉 Python 但不懂 Rust" |
| `context` | 上下文信息 | "用户公司在做电商平台" |
| `behavior` | 行为模式 | "用户习惯先在 dev 分支测试" |
| `goal` | 目标 | "用户想在下季度完成微服务迁移" |
| `correction` | 错误纠正 | "不要对 docker 用 sudo，用户已在 docker 组" |
| `technique` | 工具技巧/踩坑记录 | "pytest 用 -x 标志可以在首个失败时停止" |

---

## 五、三层配置系统

| 层级 | 来源 | 作用 | 示例字段 |
|------|------|------|----------|
| **L0** | `harness/config/defaults.py` 硬编码 | 系统出厂默认值 | `backend="file"`, `max_facts=10`, `ttl_days=90` |
| **L1** | `~/.multiagent-studio/users/{uid}/config.yaml` | 用户全局设置（通过前端「设置」页面） | `api_key`, `base_url`, `default_model`, `mem0_config` |
| **L2** | `~/.multiagent-studio/users/{uid}/agents/{name}/config.yaml` | Per-Agent 覆盖 | `model`, `memory.backend`, `memory.mem0_tool_enabled` |

合并后的 `EffectiveConfig` 通过 `ConfigLoader.load_effective(user_id, agent_name)` 获取，最终构建为 `MemoryConfig` 全局单例。

### MemoryConfig 关键字段

| 字段 | 默认值 | 说明 |
|------|--------|------|
| `enabled` | `True` | 记忆系统总开关 |
| `backend` | `"file"` | 后端类型：`"file"` 或 `"mem0"` |
| `debounce_seconds` | `120` | 去抖间隔（秒） |
| `max_facts` | `30` | 最大事实存储数（超出时保留最新的 N 条） |
| `memory_ttl_days` | `90` | 事实过期天数（0 = 永不过期） |
| `fact_confidence_threshold` | `0.9` | 事实置信度过滤阈值 |
| `injection_enabled` | `True` | 是否注入记忆到 system prompt |
| `max_injection_tokens` | `1000` | 注入时的 token 预算（按置信度排序截断） |
| `mem0_tool_enabled` | `False` | 是否注册 `memory_search` 工具 |
| `project_memory_enabled` | `True` | Team 模式下是否加载项目记忆 |
| `project_memory_root` | `""` | 项目记忆根目录（空 = 自动从项目路径推断） |

---

## 六、读路径 — DynamicContextMiddleware

### 位置

`harness/middleware/dynamic_context.py`

### 钩子

`abefore_agent` — 每次 Agent 执行前触发

### 注入逻辑

```
abefore_agent(state, runtime)
  → _inject(state)
    → 检查消息历史中是否有上次注入的日期
    ├── last_date is None (首轮)
    │   → _build_full_reminder(user_id)
    │     → get_memory_data(agent_name, user_id)           ← 从 memory.json 加载
    │     → sanitize_memory_if_unsafe()                     ← 安全检测
    │     → format_memory_for_injection(data, max_tokens)   ← token 预算截断
    │     → 组装完整 reminder
    │
    ├── last_date == today (同日)
    │   → return None (memory 快照持久存在)
    │
    └── last_date != today (跨午夜)
        → _build_date_update_reminder()                     ← 仅注入日期更新
```

### 注入格式

首轮注入完整 `<system-reminder>`：

```xml
<system-reminder>
<project_memory>
## Project: multiagent-studio
...
</project_memory>

<memory>
User Context:
- Work: Full-stack developer, 主要在 multiagent-studio 项目上工作
- Personal: 偏好中文交流，简洁风格
- Current Focus: 正在重构记忆系统
- Avoid: 不要过度解释简单概念

History:
- Recent: 过去几周的活动摘要
- Earlier: 更早的重要历史模式
- Background: 持久背景和基础上下文

Facts:
- [preference | 0.95] 用户偏好 TypeScript 而非 JavaScript
- [technique | 0.95] docker 命令不需要 sudo，用户已在 docker 组
</memory>

<current_date>2026-07-24, Friday</current_date>
</system-reminder>
```

其中 `<project_memory>` 块**仅在 Team 模式下出现**。跨午夜时只注入 `<current_date>` 更新块。

### ID 交换机制

注入的提醒消息和用户消息共享原始 `message.id`：
- 提醒消息：`id = stable_id` + `hide_from_ui: true` + `dynamic_context_reminder: true`
- 用户消息：`id = "{stable_id}__user"` + 原始内容

`SummarizationMiddleware` 识别 `dynamic_context_reminder` 标记，在压缩时保留提醒消息。

### 传递链路

```
单 Agent 模式:
  main._build_graph_context(user_id, agent_name)
    → _build_middlewares_for(eff, user_id, agent_name=agent_name)
      → build_lead_middlewares(agent_name=agent_name)
        → DynamicContextMiddleware(agent_name=agent_name)

Team 模式:
  TeammateAgent._build_middlewares()
    → build_teammate_middlewares(agent_name=self.name, project_context=...)
      → DynamicContextMiddleware(agent_name=agent_name, project_context=...)
```

---

## 七、写路径 — MemoryMiddleware → Queue → Updater

### 7.1 MemoryMiddleware

**位置**：`harness/middleware/memory.py`

**钩子**：`aafter_agent` — Agent 执行完毕后触发

**增量提交**：每轮只提交最新的 **HumanMessage + AIMessage** 交换对，不是全量历史：

```python
# memory.py:90-118
filtered = filter_messages_for_memory(messages)        # 过滤掉 tool 往返 / 上传块 / 摘要
last_ai = filtered[last_ai_idx]                         # 最后一条无 tool_calls 的 AI 消息
last_human = 它之前最近的一条 human                     # 配对的人类消息
leading = [第一条 dynamic_context_reminder]             # 附带给 LLM 提供时间/记忆上下文
latest_exchange = leading + [last_human, last_ai]       # 增量提交
```

**跳过条件**：
- `finish_reason != "stop"` (截断 / content_filter / length)
- `memory_enabled` 为 `False`

**信号检测**：`detect_correction()` 和 `detect_reinforcement()` 在最近 6 条人类消息中检测纠正/强化信号，传递给 `MemoryUpdater` 以增强事实置信度。

### 7.2 MemoryUpdateQueue

**位置**：`harness/memory/queue.py`

**去抖机制**（默认 120 秒）：

```
add(thread_id, messages, ...)
  → _enqueue_locked()           ← 合并同一 thread 的现有条目
  → _ensure_task()              ← 创建/重置 asyncio 定时器
    → asyncio.sleep(120)
      → _process_queue()
        → 逐个处理队列中的 ConversationContext
```

**合并策略**：
- 同一 `(thread_id, user_id, agent_name)` 的多个更新 → 合并为一个
- 纠正/强化标志：取 OR
- Metadata：新值覆盖旧值
- Per-user 凭证：新值优先

### 7.3 MemoryUpdater

**位置**：`harness/memory/updater.py`

**后端路由**：

| backend | mem0_tool_enabled | 写入目标 |
|---------|-------------------|----------|
| `"file"` | `false` | → 仅 memory.json |
| `"file"` | `true` | → memory.json **+** mem0 (双写) |
| `"mem0"` | `false` | → 仅 mem0 |
| `"mem0"` | `true` | → 仅 mem0 |

**file 写入流程**：

```
aupdate_memory(messages, thread_id, agent_name, user_id, ...)
  → _do_update_memory()
    ├── 1. _prepare_update_prompt()
    │     → get_memory_data()                        ← 加载现有 JSON
    │     → _compact_memory_for_prompt()             ← 事实 > 25 条时截取 top 25
    │     → MEMORY_UPDATE_PROMPT.format(...)          ← 组装 LLM prompt
    │
    ├── 2. model.ainvoke(prompt)                     ← LLM 提取事实
    │
    └── 3. _finalize_update()
          → _apply_updates(current, llm_response)
            ├── 更新 user 段 (workContext/personalContext/topOfMind/avoidances)
            ├── 更新 history 段 (recentWeeks/earlierContext/longTermBackground)
            ├── 删除 factsToRemove 中的事实
            ├── 添加 newFacts（去重 + 置信度过滤 >= threshold）
            ├── TTL 过期清理（memory_ttl_days 天前的事实删除）
            └── max_facts 限制（按 createdAt 降序保留最新 N 条）
          → _strip_upload_mentions_from_memory()     ← 正则清理文件上传提及
          → FileMemoryStorage.save()                  ← 原子写入
```

**mem0 写入流程**：

```
_update_mem0(messages, user_id, agent_name, ...)
  → 转换消息为 [{"role": "user/assistant", "content": "..."}]
  → asyncio.to_thread(mem0.add, messages, user_id, agent_id, metadata)
```

### 7.4 凭证管道

记忆更新的 LLM 凭证优先级（高到低）：

1. 显式传入的 per-user `api_key` / `base_url`（来自 `GraphContext` 配置，通过 `MemoryMiddleware` 传入队列）
2. `MemoryConfig.api_key` / `MemoryConfig.base_url`（全局单例）
3. `OPENAI_API_KEY` / `OPENAI_BASE_URL` 环境变量

---

## 八、FileMemoryStorage

**位置**：`harness/memory/storage.py`

### 原子写入机制

```python
save(memory_data)
  ├── validate_memory_json(memory_data)              ← 安全检测
  │     └── 检测到威胁 → return False（拒绝写入）
  ├── 写入 .{uuid}.tmp 临时文件
  ├── fcntl.flock(LOCK_EX) 文件锁
  ├── temp_path.replace(file_path)                    ← 原子替换
  └── 更新 mtime 缓存
```

### mtime 缓存

```
load(agent_name, user_id)
  ├── 检查文件 mtime
  ├── 缓存命中 (mtime 未变) → 直接返回缓存
  └── 缓存未命中 → _load_memory_from_file()
        ├── sanitize_memory_if_unsafe()               ← 安全检测
        │     └── 检测到威胁 → return create_empty_memory()
        ├── _maybe_cleanup_expired()                  ← 惰性 TTL 清理
        └── 存入缓存 + 返回
```

### 惰性 TTL 清理

在 `load()` 时进行（不修改磁盘文件），过滤 `createdAt` 超过 `memory_ttl_days` 天的事实。写回时 `_apply_updates()` 也会同步执行 TTL 清理。

---

## 九、安全检测 (safety.py)

**位置**：`harness/memory/safety.py`

### 三道防线

| 防线 | 位置 | 触发条件 | 行为 |
|------|------|----------|------|
| **Load** | `FileMemoryStorage._load_memory_from_file()` | 用户手动编辑 JSON 后加载 | 返回空记忆 |
| **Save** | `FileMemoryStorage.save()` | LLM 提取结果写入前 | 拒绝写入，返回 `False` |
| **Inject** | `DynamicContextMiddleware._build_full_reminder()` | 注入 system prompt 前 | 该文件不注入 |

### 三层检测规则

**Layer 1 — 提示注入 (10 种模式)**：

| 模式 | 检测 ID |
|------|---------|
| `ignore (previous\|all\|above\|prior) instructions` | `prompt_injection` |
| `ignore all previous instructions` | `prompt_injection` |
| `do not tell the user` | `deception_hide` |
| `system prompt override` | `sys_prompt_override` |
| `disregard (your\|all\|any) (instructions\|rules\|guidelines)` | `disregard_rules` |
| `act as (if\|though) you (have no\|don't have) (restrictions\|limits\|rules)` | `bypass_restrictions` |
| HTML 注释隐藏注入 `<!--...ignore...-->` | `html_comment_injection` |
| `div style="display:none"` 隐藏文本 | `hidden_div` |
| `translate ... into ... and (execute\|run\|eval)` | `translate_execute` |

**Layer 2 — 凭证外泄 / SSH 后门 (7 种模式)**：

| 模式 | 检测 ID |
|------|---------|
| `curl ... ${KEY\|TOKEN\|SECRET\|PASSWORD}` | `exfil_curl` |
| `curl ... https://... ${KEY\|TOKEN\|SECRET}` | `exfil_curl_remote` |
| `cat ... (.env\|credentials\|.netrc\|.pgpass\|id_rsa)` | `read_secrets` |
| `ssh ... -o ProxyCommand=...` | `ssh_backdoor` |
| `ssh ... -o RemoteForward ...:22` | `ssh_backdoor_reverse` |
| `nc -[ln] ... -[ec] ... /bin/...` | `netcat_backdoor` |
| `printenv ... ${KEY\|TOKEN\|SECRET}` 或 `env \| ...` | `env_exfil` |

**Layer 3 — 不可见 Unicode (10 个字符)**：

`​` ZERO WIDTH SPACE, `‌` ZWNJ, `‍` ZWJ, `⁠` WORD JOINER, `﻿` BOM, `‪-‮` 双向文本控制符。

### 检测流程

```python
validate_content(content, source)
  → 遍历三层检测规则
  → 返回 finding ID 列表（空 = 安全）

validate_memory_json(memory_data, source)
  → 递归遍历 JSON 中所有 string 值
  → 对每个 string 调用 validate_content()
  → 返回汇总的 findings 列表

sanitize_memory_if_unsafe(memory_data, source)
  → validate_memory_json()
  → findings 非空 → 返回 ({}, findings)
  → findings 为空 → 返回 (memory_data, [])
```

---

## 十、项目记忆 (仅 Team 模式)

**位置**：`harness/memory/project_storage.py`

### 目录结构

```
~/.multiagent-studio/users/{uid}/projects/{project_id}/
├── project.json                               ← 项目定义
└── memory/
    ├── description.md                         ← L0: 始终加载
    ├── architecture.md                        ← L1: 按需加载
    ├── conventions.md                         ← L1
    └── ...
```

### 加载策略

| 层级 | 文件 | 触发条件 | 内容 |
|------|------|----------|------|
| L0 | `description.md` | 每次 Agent 启动 | 项目概览、技术栈、架构、文件夹说明 |
| L1 | `*.md` | 按需 (未来 `project_memory_search` 工具) | 架构约定、团队规范、环境信息 |

### 初始化链路

```
Team 模式:
  TeammateAgent._build_middlewares()
    → ProjectMemoryStorage.set_project_root(project_root)
    → load_description()
    → 传入 build_teammate_middlewares(project_context=description_md)

单 Agent 模式:
  不启用项目记忆
```

### description.md 模板

```markdown
# Project: multiagent-studio

## Tech Stack
- Backend: Python 3.12, FastAPI, LangGraph
- Frontend: Next.js 15, React 19, TypeScript
- Storage: SQLite (checkpointer), PostgreSQL (mem0)
- Sandbox: OpenSandbox (Docker containers)

## Architecture
- `harness/` — Backend agent runtime (middleware chain, memory, tools)
- `frontend/` — Next.js chat UI
- `app/` — FastAPI routes

## Environment
- OS: Linux (WSL2 Debian)
- Shell: bash, POSIX syntax

## Team Workflows
- Feature branches: `feature/xxx` → `dev` → `main`
- Code review: 1 approval minimum

## Folder Descriptions
- `harness/main.py` — Entry point, HarnessService class
- `harness/middleware/` — 20-layer middleware chain
- `harness/memory/` — Memory system
```

---

## 十一、LLM 提取 Prompt 设计

### MEMORY_UPDATE_PROMPT 结构

LLM 提取使用单一 prompt（`harness/memory/prompt.py` 中的 `MEMORY_UPDATE_PROMPT`），包含以下部分：

1. **当前记忆状态** — 压缩后的现有记忆视图（summaries + top 25 facts）
2. **新对话** — 增量提交的最新交换对
3. **指令** — 结构化反思（错误检测 → 纠正检测 → 约束发现）
4. **各段指南** — user/history/facts 各段的内容和长度指南
5. **输出格式** — JSON schema（user.updates + history.updates + newFacts + factsToRemove）

### 7 天价值过滤

Prompt 中包含明确规则：

> Do NOT record facts that will be stale within a week. Task progress ("fixed bug X"), PR numbers, commit SHAs, "Phase N done", temporary file paths, session outcomes — these belong in session history, not memory. Ask yourself: "Will this fact still matter 7 days from now?"

### 工具技巧提取

> **Tool & Technique facts**: Record discovered tool quirks, CLI flags that actually work, effective patterns, and pitfalls with their workarounds. Example: "docker commands don't need sudo — user is already in the docker group" → technique, confidence 0.95.

---

## 十二、mem0 集成

### 架构

mem0 作为一个**独立的向量记忆后端**，与 file 后端平行运行。

**双轨制**：
- `mem0_tool_enabled` 与 `backend` 独立
- `backend=file` + `mem0_tool_enabled=true` → 双写（file 用于被动注入 + mem0 用于主动查询）
- `backend=mem0` + `mem0_tool_enabled=true` → 仅 mem0

### memory_search 工具

由 `harness/tools/builtins/memory_tools.py` 中的 `create_memory_search_tool()` 工厂函数创建。

- **注册位置**：`lead_tools.py` 中的 `build_lead_tools()`，当 `mem0_tool_enabled=True` 时注册
- **自动上下文提取**：从 `langgraph.config.get_config()` 读取 `user_id` 和 `agent_id`
- **查询**：`mem0.search(query, filters, top_k=5)` → 格式化结果（含 relevance score）
- **仅 Lead Agent 可用**：子 agent 和 teammate 没有此工具

### mem0 客户端初始化

```python
get_mem0()
  → is_mem0_enabled() 检查
  → 读取 MemoryConfig.mem0_config
  → Memory.from_config(expanded_config)  # mem0 库的初始化
```

`mem0_config` 包含三个 provider：`vector_store` (pgvector)、`llm` (OpenAI 兼容)、`embedder` (OpenAI 兼容)。

---

## 十三、Team 模式记忆隔离

### Lead Agent (`__team_lead__`)

| 维度 | 值 |
|------|-----|
| Agent 名称 | `__team_lead__` (平台内置，不可配置) |
| 模型配置 | 继承 `default` Agent 的 L2 配置 |
| LLM 凭证 | 来自 `default` Agent 的 `api_key` / `base_url` |
| 记忆存储 | `users/{uid}/agents/__team_lead__/memory.json` |
| 项目记忆 | ✅ 加载 `description.md` |
| 记忆内容 | 任务编排、调度决策、团队管理经验 |

### Member Agent

| 维度 | 值 |
|------|-----|
| Agent 名称 | 用户创建的名称（如 `Frontend-Developer`） |
| 模型配置 | 自身 L2 `config.yaml` |
| LLM 凭证 | 来自用户全局 L1 `api_key` / `base_url` |
| 记忆存储 | `users/{uid}/agents/{name}/memory.json` |
| 项目记忆 | ✅ 加载 `description.md` |
| 记忆内容 | 专业领域的执行经验、工具技巧 |

### 记忆隔离保证

每个 Agent 的 `DynamicContextMiddleware` 和 `MemoryMiddleware` 分别使用各自的 `agent_name`，读写完全隔离。

---

## 十四、关键设计决策

1. **增量更新**：`MemoryMiddleware` 每轮只提交最新交换对，不是全量重放。这使得去抖安全且高效。

2. **读/写解耦**：`DynamicContextMiddleware`（读）和 `MemoryMiddleware`（写）运行在不同的钩子上（`abefore_agent` vs `aafter_agent`），通过 `MemoryConfig` 协调。

3. **双后端支持**：file (JSON) 和 mem0 (pgvector) 可以独立或同时使用。

4. **per-user 凭证**：API 密钥通过 per-user 管道流动，支持多租户使用。

5. **原子文件写入**：`fcntl.flock` + 临时文件 + `replace` 保证多进程安全。

6. **mtime 缓存**：缓存文件修改时间，避免不必要的 JSON 解析。

7. **prompt 压缩**：事实数 > 25 时只发送 top 25（按置信度+时间排序），控制 LLM token 成本。

8. **惰性 TTL**：过期事实在加载时清理（不改磁盘），写回时同步清理。

9. **ID 交换**：提醒消息和用户消息共享原始 ID，保护 LangGraph checkpoint 的一致性。

10. **安全检测分层**：存储（load/save）和注入（inject）各有一道防线，用户手动编辑的恶意内容在所有路径都会被拦截。

---

## 十五、近期改动记录

| 日期 | 改动 | 文件 |
|------|------|------|
| 2026-07-24 | 新建 `safety.py` 安全检测模块 | `harness/memory/safety.py` |
| 2026-07-24 | 新建 `project_storage.py` 项目记忆模块 | `harness/memory/project_storage.py` |
| 2026-07-24 | `memory.json` 新增 `avoidances` 字段 | `storage.py`, `prompt.py`, `updater.py` |
| 2026-07-24 | `MEMORY_UPDATE_PROMPT` 追加工具技巧 + 7 天价值规则 + `technique` 分类 | `harness/memory/prompt.py` |
| 2026-07-24 | 安全检测集成到 `load()` / `save()` / 注入前 | `storage.py`, `dynamic_context.py` |
| 2026-07-24 | Team 模式项目记忆加载 (dead code 激活) | `teammate_middleware.py`, `teammate_agent.py` |
| 2026-07-24 | 修复单 Agent 模式 per-agent 记忆隔离 (agent_name 传递) | `harness/main.py` |
| 2026-07-24 | MemoryConfig 新增 `project_memory_enabled` / `project_memory_root` | `harness/config/memory_config.py` |
