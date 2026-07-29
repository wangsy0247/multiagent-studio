# session_search 功能实现总结

> 参照 hermes-agent 的 session_search 设计，为 multiagent-studio 增加历史会话全文检索能力。
> Lead Agent 可在对话中调用 `session_search` 工具，用关键词搜索当前用户的历史会话消息。

## 整体架构

```
Lead Agent 调用 session_search(query)
  │
  ▼  httpx + X-Internal-Token（与 cron 工具同一模式）
App: POST /api/internal/session-search
  │  ① FTS5 三路由分流检索（unicode61 / trigram / LIKE）
  │  ② 按会话分组取 Top N（限定当前用户、未归档、排除当前会话）
  │  ③ 拉取命中会话全部消息，围绕命中位置裁剪（≤ 12k 字符/会话）
  ▼
Harness 工具侧: 小模型并行总结（Semaphore(3)，失败降级原始 transcript）
  ▼
返回给 Agent
```

数据流关键决策：

- **存储**：业务消息本就在 app.db 的 `messages` 表（完整原文），无需改写入路径
- **索引**：新增两张 FTS5 虚拟表 + 3 个触发器自动同步，写入侧零改动
- **进程边界**：harness 不直连 app.db，复用 cron 工具的内部 HTTP API 模式
- **LLM 总结**：放在 harness 侧（app 层无 LLM 客户端，凭证由 harness 管理）

## 文件清单

| 文件 | 变更 | 说明 |
|---|---|---|
| `app/db/fts.py` | 新增 | FTS5 双表 DDL + 触发器 + 存量回填（`ensure_fts()`，幂等） |
| `app/db/engine.py` | 修改 | `init_db()` 在 SQLite 分支挂接 `ensure_fts()` |
| `app/services/session_search.py` | 新增 | 三路由搜索 + 会话分组 + 全文裁剪 |
| `app/api/internal.py` | 修改 | 新增 `POST /api/internal/session-search` 端点 |
| `harness/tools/builtins/session_search_tool.py` | 新增 | Agent 工具：HTTP 调用 + 小模型总结 |
| `harness/tools/builtins/lead_tools.py` | 修改 | `build_lead_tools()` 默认注册 |
| `app/tests/test_session_search.py` | 新增 | 19 个服务端测试 |
| `harness/tests/test_session_search_tool.py` | 新增 | 9 个工具侧测试 |

## 核心设计

### 1. FTS5 索引层（`app/db/fts.py`）

- `messages_fts`（unicode61 分词，英文/通用）+ `messages_fts_trigram`（trigram 分词，中日韩）
- FTS rowid 与 `messages.rowid` 一一对应，只建一列索引列，其余字段查询时 JOIN 回主表
- 触发器（INSERT/UPDATE/DELETE）自动同步；每次启动 DROP+CREATE 保证定义最新
- **索引文本 = content + extra_metadata 中所有 JSON 文本值**（`json_tree` 提取）：
  - `tool_call` 消息 content 为空，工具名/参数只在 extra_metadata 里，不索引就完全搜不到
  - SQLAlchemy JSON 序列化默认 `ensure_ascii=True`，中文存为 `\uXXXX`，必须经 `json_tree` 还原
- 存量回填：两表独立判空，FTS 为空且 messages 非空时 `INSERT ... SELECT`

### 2. 三路由分流（`app/services/session_search.py`）

移植自 hermes `hermes_state.py:search_messages`：

| 路由 | 判据 | 实现 |
|---|---|---|
| 1 | 非 CJK 查询 | `messages_fts MATCH`，BM25 排序 + `snippet()` 高亮，查询先经 `_sanitize_fts5_query()` 清洗 |
| 2 | CJK 且每 token ≥ 3 字 | `messages_fts_trigram MATCH`（trigram 需 ≥ 9 UTF-8 字节才能命中） |
| 3 | CJK 短词/混合 | LIKE 兜底（`content` + `json_tree(extra_metadata)` 文本值），每 token 独立条件 OR 连接 |

统一约束：`JOIN threads` 限定 `user_id` + `is_archived = 0`，排除 `exclude_thread_id`（当前会话），按会话分组取 Top N（默认 3，钳制 [1,5]），每会话最多 5 条命中证据。

### 3. 全文拉取与裁剪

对齐 hermes 的 `_format_conversation` + `_truncate_around_matches`：

- 命中会话拉取**全部消息**（按 rowid 序），渲染为 `[HUMAN]/[AI]/[TOOL:工具名]` 对话文本；tool_call content 为空时展示 tool_args；超长工具输出中间截断
- 裁剪窗口三级策略：整句短语匹配 → 全部词项 200 字符邻近共现 → 单词项兜底；选覆盖最多命中点的窗口（25% 在前、75% 在后），两端带 `...[truncated]...` 标记
- 预算 `_MAX_SESSION_CHARS = 12_000` 字符/会话（hermes 用 ~100k 是喂辅助 LLM；我们直接进调用方上下文，取保守值）

### 4. 小模型总结（harness 工具侧）

- 对每个会话 transcript 用聚焦 query 的 prompt 总结，保留结论/代码/配置/报错等具体细节
- `asyncio.gather` + `Semaphore(3)` 限并发（对齐 hermes `max_concurrency` 默认）
- 凭证解析：请求级 contextvar（延迟导入 `_current_req_creds` 避免循环依赖）→ L1 用户全局配置 → 环境变量
- 模型：`SESSION_SEARCH_MODEL` env → L1 `summary_model`/`title_model` → `default_model` → `gpt-4o-mini`
- ChatOpenAI 实例按 `(api_key, base_url, model)` 缓存（仿 `TitleMiddleware`），`extra_body` 关闭 thinking
- **降级链**：无凭证 → 全部返回原始 transcript；单会话失败 → 该会话回退 transcript

### 5. Agent 工具（`harness/tools/builtins/session_search_tool.py`）

- `user_id` 从 `InjectedState` 取、`thread_id` 从 `RunnableConfig` 取（自动排除当前会话），模型只需传 `query` / `max_sessions`
- docstring 写明使用场景与纪律（关键词而非整句、OR 组合、截断标记含义）
- `build_lead_tools()` 默认注册，无需 config.yaml 改动，前端无需改动

## 与 hermes 的差异

| 能力 | hermes | 本实现 |
|---|---|---|
| 三路由 FTS 检索 | ✅ | ✅ 对齐 |
| 工具名/参数入索引 | ✅ | ✅ 对齐（json_tree 方案） |
| 全文裁剪 | ✅ ~100k | ✅ 12k/会话（直接进上下文，更保守） |
| LLM 总结 | ✅ | ✅ 对齐 |
| 父会话谱系归并 | ✅（压缩/委派子会话） | 不需要（本项目无此机制） |
| 空 query 列最近会话 | ✅ | 未做（可选后续项） |
| 隐藏 source 过滤 | ✅ exclude_sources | 用 `is_archived` + 用户隔离代替 |

## 已知边界

- 软删除（`is_archived=True`）的会话消息物理上仍在库中，但搜索不可见（三条路由均过滤）
- 路由 3 的 LIKE 多 token 一律 OR 连接（与 hermes 一致），AND 语义仅在 FTS 路由成立
- `json_tree` 只索引 `type='text'` 的值，数字/布尔参数不进索引
- 团队成员间消息落库时截断到 500 字符（`execute.py:124`），属既有行为
- 仅 SQLite 后端启用；PostgreSQL 场景跳过（可后续用 pg_trgm 扩展）

## 测试

- `app/tests/test_session_search.py`（19 个）：三条路由、用户隔离、排除当前会话、归档过滤、Top N 钳制、触发器 UPDATE/DELETE 同步、回填幂等、工具名/中文参数可搜、transcript 完整性与裁剪窗口、tool_call 渲染
- `harness/tests/test_session_search_tool.py`（9 个）：工具注册、格式优先级（总结 > transcript）、总结成功/无凭证跳过/失败降级、端到端（mock HTTP）、缺 user_id、零命中
- 回归：app 全量 79 passed，harness cron 工具测试 10 passed，ruff 全绿

## 手动验证步骤

1. 重启 app 服务（`init_db()` 自动建 FTS 表 + 触发器 + 回填存量消息）
2. 在某会话聊一个独特关键词（如"大别山项目"），触发几次工具调用
3. 开新会话问"我们之前讨论过大别山项目的什么内容"，agent 应调用 session_search 并返回总结
4. 中文 2 字词（如"桂林"）、英文短语、工具名（如"web_search"）各验一次
