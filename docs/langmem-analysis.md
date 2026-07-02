# LangMem 记忆原理分析

> 分析日期：2026-07-01
> 项目：multiagent-studio (LangGraph + LangChain 多智能体框架)
> 已有文档：`docs/mem0-analysis.md`（mem0）、`docs/gbrain-vs-mem0-comparison.md`（对比）

---

## 一、LangMem 是什么

LangMem 是 **LangChain 官方推出的长期记忆框架**，专为 LLM Agent 设计。它不是独立的记忆数据库，而是一套"记忆管理工具集"——提供从对话中提取信息、组织记忆、优化 Agent 行为的函数式原语，并与 LangGraph 的存储层原生集成。

**核心定位**：让 Agent 从交互中学习和适应，维护跨会话的长期记忆。

| 维度 | 说明 |
|------|------|
| **作者** | LangChain 团队 |
| **许可证** | MIT |
| **安装** | `pip install langmem` |
| **依赖** | LangGraph（可选，有状态集成时需要） |
| **GitHub** | langchain-ai/langmem |
| **与 mem0 的关系** | 同类竞品，但设计哲学不同（见后文对比） |

---

## 二、核心设计哲学

LangMem 的设计有三个核心理念，与 mem0 形成鲜明对比：

### 2.1 记忆是"状态转换函数"，不是"数据库"

LangMem 的核心 API 是**无副作用的记忆状态转换函数**：

```
输入：对话消息 + 当前记忆状态
  ↓ LLM 决策
输出：更新后的记忆状态（新增/修改/删除）
```

> "LangMem's core is a collection of stateless memory state transition functions."

这意味着 LangMem **不绑定任何特定存储**——你可以用 LangGraph 的 BaseStore、自己的向量库、甚至纯内存 dict。mem0 则是"向量库即真值"，存储与逻辑耦合。

### 2.2 三种记忆类型，各有定位

LangMem 借鉴人类认知科学，将记忆分为三类：

| 记忆类型 | 存什么 | 人类类比 | Agent 示例 | 存储方式 |
|---------|--------|---------|-----------|---------|
| **语义记忆** | 事实/知识 | 知道 Python 是编程语言 | 用户偏好、知识三元组 | Collection 或 Profile |
| **情景记忆** | 过去经历 | 记住第一天上班 | 少样本示例、对话摘要 | Collection |
| **程序记忆** | 行为规则 | 知道如何骑车 | 系统提示词、响应模式 | Prompt 规则 |

**mem0 只有语义记忆**（facts），没有情景记忆和程序记忆的概念。这是 LangMem 的独特优势。

### 2.3 记忆形成有"热路径"和"后台"两种模式

| 模式 | 时机 | 延迟 | 适用场景 |
|------|------|------|---------|
| **热路径（主动）** | 对话过程中，Agent 自己调工具存 | 高（用户可感知） | 关键上下文即时更新 |
| **后台（潜意识）** | 对话结束后，异步反思提取 | 无 | 模式分析、摘要、不紧急的洞察 |

**mem0 只有后台模式**（debounce 后异步 add），没有热路径。LangMem 的双轨设计更灵活。

---

## 三、三种记忆类型详解

### 3.1 语义记忆（Semantic Memory）

存储 Agent 响应所依赖的核心事实。有两种表示形式：

#### Collection（集合）

- 每条记忆是独立文档/记录
- 新信息到达时**插入新记忆**，并与旧记忆协调（更新或删除）
- 适合：大量分散的事实（"用户喜欢科幻"、"用户有只叫 Fido 的狗"）

```python
from langmem import create_memory_manager

manager = create_memory_manager(
    "anthropic:claude-3-5-sonnet-latest",
    instructions="Extract all noteworthy facts, events, and relationships.",
    enable_inserts=True,   # 允许新增
    enable_updates=True,   # 允许更新（upsert 语义）
    enable_deletes=False,  # 默认不删除
)

memories = manager.invoke({"messages": conversation})
# 返回 ExtractedMemory 列表
```

#### Profile（配置文件）

- 单个文档，代表"当前状态"
- 新信息到达时**更新该文档**，不创建新文档
- 适合：只关心最新状态（如用户档案：姓名、偏好、技能）

```python
from pydantic import BaseModel

class UserProfile(BaseModel):
    name: str
    preferred_name: str
    response_style_preference: str
    special_skills: list[str]

manager = create_memory_manager(
    "anthropic:claude-3-5-sonnet-latest",
    schemas=[UserProfile],
    enable_inserts=False,  # Profile 模式关闭插入
)

profile = manager.invoke({"messages": conversation})[0]
# 返回结构化的 UserProfile 对象
```

#### Collection vs Profile 选择

| 特性 | Profile | Collection |
|------|---------|-----------|
| 访问速度 | 快（直接读取当前状态） | 按上下文语义检索 |
| 数据结构 | 严格 schema | 灵活文档 |
| 信息丢失 | 可能丢历史（只留最新） | 无丢失 |
| 用户编辑 | 易于呈现给用户 | 不适合直接编辑 |

### 3.2 情景记忆（Episodic Memory）

保存成功的交互作为"学习示例"，指导未来行为：

```python
class Episode(BaseModel):
    observation: str  # 情境和上下文
    thoughts: str     # 推理过程
    action: str       # 采取了什么响应
    result: str       # 结果和为什么有效

manager = create_memory_manager(
    "anthropic:claude-3-5-sonnet-latest",
    schemas=[Episode],
    instructions="Extract examples of successful interactions...",
    enable_inserts=True,
)
```

**用途**：当 Agent 遇到类似情境时，检索过去的成功 Episode 作为少样本示例（few-shot）。

### 3.3 程序记忆（Procedural Memory）

编码 Agent 应如何行为——**这是 LangMem 最独特的功能**：

```python
from langmem import create_prompt_optimizer

optimizer = create_prompt_optimizer(
    "anthropic:claude-3-5-sonnet-latest",
    kind="gradient",  # 优化策略
    config={"max_reflection_steps": 3}
)

# 用对话轨迹 + 反馈优化系统提示词
optimized = optimizer.invoke({
    "trajectories": [(conversation, {"score": 0, "feedback": "too pushy"})],
    "prompt": "You are a helpful assistant."
})
# 返回优化后的提示词
```

**三种优化策略**：

| 策略 | LLM 调用次数 | 特点 |
|------|-------------|------|
| `prompt_memory` | 1 次 | 最快，从历史学习模式 |
| `metaprompt` | 1-5 次 | 平衡，元学习直接提建议 |
| `gradient` | 2-10 次 | 最彻底，反思循环+关注点分离 |

**mem0 和 GBrain 都没有程序记忆**——这是 LangMem 独有的能力，让 Agent 的行为规则能随交互自动进化。

---

## 四、记忆形成：热路径 vs 后台

### 4.1 热路径（Hot Path）

Agent 在对话过程中**主动调用记忆工具**：

```python
from langgraph.prebuilt import create_react_agent
from langgraph.store.memory import InMemoryStore
from langmem import create_manage_memory_tool, create_search_memory_tool

store = InMemoryStore(index={"dims": 1536, "embed": "openai:text-embedding-3-small"})

agent = create_react_agent(
    "anthropic:claude-3-5-sonnet-latest",
    tools=[
        create_manage_memory_tool(namespace=("memories", "{user_id}")),  # 存/改/删
        create_search_memory_tool(namespace=("memories", "{user_id}")),  # 查
    ],
    store=store,
)

# Agent 自己决定何时存记忆
agent.invoke({"messages": [{"role": "user", "content": "Remember I prefer dark mode."}]})
# Agent 会调用 manage_memory 工具存储偏好
```

**特点**：
- Agent 自主决策"什么值得记"
- 即时更新，下一轮就能用
- 缺点：增加延迟，占用 Agent 的工具选择决策

### 4.2 后台（Background）

对话结束后**异步提取记忆**：

```python
from langmem import ReflectionExecutor, create_memory_store_manager

memory_manager = create_memory_store_manager(
    "anthropic:claude-3-5-sonnet-latest",
    namespace=("memories",),
)

@entrypoint(store=store)
async def chat(message: str):
    response = llm.invoke(message)
    # 对话正常进行，记忆在后台提取
    await memory_manager.ainvoke({
        "messages": [{"role": "user", "content": message}, response]
    })
    return response.content
```

**特点**：
- 不拖慢即时交互
- 确保更高的信息召回率（LLM 有时间反思）
- 用 `ReflectionExecutor` 可调度到后台线程

### 4.3 两种模式的对比

| 维度 | 热路径 | 后台 |
|------|--------|------|
| 延迟影响 | 高（用户可感知） | 无 |
| 更新速度 | 即时 | 延迟 |
| Agent 复杂度 | 增加（要管工具） | 不增加 |
| 信息召回率 | 可能遗漏（Agent 专注任务） | 更高（专注提取） |
| 适用场景 | 关键上下文 | 模式分析、摘要 |

**与项目现有方案的对应**：
- 项目的 `MemoryMiddleware`（aafter_agent 入队）≈ 后台模式
- 项目的 `DynamicContextMiddleware`（abefore_agent 注入）≈ 检索侧
- 项目**没有热路径**（Agent 无法主动存/查记忆）

---

## 五、记忆增强过程（Memory Enrichment）

这是 LangMem 的核心算法——决定如何从对话中提取和组织记忆。

### 5.1 统一三步模式

```
① 接受输入：对话消息 + 当前记忆状态（existing）
② LLM 决策：分析对话，决定如何扩展或整合记忆
③ 输出更新：返回 ExtractedMemory 列表（新增/更新/删除）
```

### 5.2 插入/更新/删除决策

```python
manager = create_memory_manager(
    model,
    schemas=[Memory],
    enable_inserts=True,   # 是否允许新增
    enable_updates=True,   # 是否允许更新（upsert）
    enable_deletes=False,  # 是否允许删除
)
```

| 操作 | 控制参数 | 默认 | 行为 |
|------|---------|------|------|
| **INSERT** | `enable_inserts` | True | 创建新记忆 |
| **UPDATE** | `enable_updates` | True | upsert——与已有记忆协调更新 |
| **DELETE** | `enable_deletes` | False | 删除过时/矛盾的旧记忆 |

**关键**：`enable_updates=True` 时执行 **upsert 语义**——不是盲目新增，而是与已有记忆协调。这与 mem0 的 ADD/UPDATE/DELETE/NONE 决策类似，但 LangMem 的控制更显式（三个独立开关）。

### 5.3 记忆相关性排序

LangMem 强调记忆召回不只是语义相似度，而是三维排序：

| 维度 | 含义 |
|------|------|
| **相似度（similarity）** | 语义向量相似 |
| **重要性（importance）** | 记忆本身的重要程度 |
| **强度（strength）** | 基于最近使用频率/次数 |

> "Recall should combine similarity with 'importance' of the memory, as well as the memory's 'strength', which is a function of how recently/frequently it was used."

**mem0 主要依赖相似度**（+ BM25 + 实体匹配），没有显式的重要性和强度维度。

---

## 六、集成架构：两层模式

### 6.1 Layer 1：Core API（无状态）

无副作用的记忆状态转换函数，不依赖任何存储：

| 组件 | API | 功能 |
|------|-----|------|
| Memory Manager | `create_memory_manager` | 提取/更新/删除记忆 |
| Prompt Optimizer | `create_prompt_optimizer` | 优化提示词 |

```python
# 纯函数式用法——不持久化，只返回提取结果
manager = create_memory_manager("anthropic:claude-3-5-sonnet-latest")
memories = manager.invoke({"messages": conversation})
# memories 是 ExtractedMemory 列表，你自己决定怎么存
```

### 6.2 Layer 2：Stateful Integration（有状态）

基于 LangGraph BaseStore，自动持久化：

| 组件 | API | 功能 |
|------|-----|------|
| Store Manager | `create_memory_store_manager` | 自动持久化提取的记忆 |
| Memory Tools | `create_manage_memory_tool` | Agent 主动存/改/删 |
| Search Tool | `create_search_memory_tool` | Agent 主动查记忆 |

```python
# 有状态用法——自动存入 LangGraph Store
store_manager = create_memory_store_manager(
    "anthropic:claude-3-5-sonnet-latest",
    namespace=("memories", "{user_id}"),
)
await store_manager.ainvoke({"messages": conversation})
# 记忆自动存入 store
```

---

## 七、存储系统

### 7.1 命名空间（Namespaces）

记忆通过多层级命名空间组织：

```python
# 按 组织 → 用户 → 应用 组织
namespace = ("acme_corp", "{user_id}", "code_assistant")

# 模板变量在运行时从 RunnableConfig 填充
# config = {"configurable": {"user_id": "alice"}}
# → 实际命名空间 = ("acme_corp", "alice", "code_assistant")
```

**与 mem0 的对比**：
- mem0 用 `filters={"user_id":..., "agent_id":..., "run_id":...}` 元数据过滤
- LangMem 用命名空间层级组织——更结构化，但灵活性略低

### 7.2 检索方式

基于 LangGraph BaseStore，支持三种检索：

| 方式 | API | 说明 |
|------|-----|------|
| 直接访问 | `store.get(namespace, key)` | 按键取特定记忆 |
| 语义搜索 | `store.search(namespace, query=...)` | 向量相似度 |
| 元数据过滤 | `store.search(namespace, filter={...})` | 按属性过滤 |

---

## 八、与 mem0 的对比

| 维度 | LangMem | mem0 |
|------|---------|------|
| **定位** | 记忆管理工具集（函数式原语） | 记忆管理引擎（库/SDK） |
| **存储绑定** | 无（Core API 无副作用） | 强绑定（向量库即真值） |
| **记忆类型** | 语义 + 情景 + **程序** | 仅语义（facts） |
| **程序记忆** | ✅ 提示词优化器 | ❌ 无 |
| **热路径** | ✅ Agent 主动调工具 | ❌ 仅后台 |
| **后台** | ✅ ReflectionExecutor | ✅ debounce add |
| **冲突处理** | upsert（三个独立开关） | LLM 判断 ADD/UPDATE/DELETE/NONE |
| **检索排序** | 相似度 + 重要性 + 强度 | 相似度 + BM25 + 实体 + 时间 |
| **命名空间** | 多层级（模板变量） | 元数据过滤（user_id/agent_id/run_id） |
| **存储后端** | LangGraph BaseStore（可扩展） | 24+ 向量库 |
| **与 LangGraph 集成** | 原生（官方出品） | 需适配 |
| **结构化记忆** | ✅ Pydantic schema | ❌ 非结构化字符串 |
| **时间感知** | ❌ 无显式时间推理 | ✅ Platform v3 有 Temporal Reasoning |
| **图记忆** | ❌ 无 | ✅ 可选 Neo4j |
| **论文支撑** | 无独立论文 | arXiv:2504.19413 |
| **生产规模** | 未公开 | LOCOMO 基准测试 |

### 关键差异

**LangMem 的独特优势**：
1. **程序记忆**——提示词优化器让 Agent 行为自动进化（mem0/GBrain 都没有）
2. **热路径**——Agent 可主动存/查记忆（mem0 只有后台）
3. **结构化记忆**——Pydantic schema 定义记忆结构（mem0 是非结构化字符串）
4. **与 LangGraph 原生集成**——官方出品，无适配成本

**mem0 的独特优势**：
1. **更强的检索**——BM25 + 实体匹配 + 时间感知（LangMem 主要靠相似度）
2. **图记忆**——Neo4j 实体关系（LangMem 无）
3. **时间推理**——Temporal Reasoning（LangMem 无）
4. **论文验证**——LOCOMO 基准测试，性能有保证
5. **向量库选择多**——24+ 向量库（LangMem 依赖 LangGraph BaseStore）

---

## 九、与 multiagent-studio 项目的契合度

### 9.1 架构契合度

| 评估项 | LangMem | mem0 |
|--------|---------|------|
| **语言栈** | ✅ Python | ✅ Python |
| **与 LangGraph 集成** | ✅ 原生（官方） | ⚠️ 需适配 |
| **与现有中间件** | ⚠️ 需适配（工具式 vs 中间件式） | ✅ 可实现 MemoryStorage 子类 |
| **现有 checkpointer** | ✅ 可复用 LangGraph checkpointer | 独立的 |
| **命名空间映射** | `("memories", user_id, agent_name)` | `filters={user_id, agent_id}` |

### 9.2 关键考量

**LangMem 的优势**：
- 项目是 LangGraph + LangChain 架构，LangMem 是官方配套——集成最自然
- 程序记忆（提示词优化）是项目目前缺失的能力——可以让 Agent 行为自进化
- 热路径让 Agent 主动查记忆——解决了"无主动查询工具"的缺陷

**LangMem 的劣势**：
- 存储依赖 LangGraph BaseStore——项目的 checkpointer 已是 SQLite，需确认 BaseStore 的向量检索能力
- 没有时间感知——项目如果需要"上周做了什么"这类查询，需应用层补齐
- 没有图记忆——如果需要实体关系网络，LangMem 不支持
- 没有论文验证——性能表现未知（mem0 有 LOCOMO 基准）

### 9.3 接入方案对比

| 方面 | LangMem 接入 | mem0 接入 |
|------|-------------|-----------|
| **存储层** | LangGraph BaseStore（需配向量索引） | Chroma/Qdrant/pgvector |
| **写入** | `create_memory_store_manager` + ReflectionExecutor | `mem0.add()` |
| **读取** | `store.search()` 在 prompt 里调 | `mem0.search()` 在中间件里调 |
| **热路径** | `create_manage_memory_tool` + `create_search_memory_tool` | 需自己实现 LangChain Tool |
| **程序记忆** | `create_prompt_optimizer` 直接用 | ❌ 无 |
| **改动量** | ~250 行（更少，因为原生集成） | ~380 行 |

---

## 十、总结

LangMem 是 LangChain 官方的记忆框架，设计哲学是"**记忆是状态转换函数**"——核心 API 无副作用，不绑定存储，与 LangGraph 原生集成。

### 核心特点

1. **三种记忆类型**：语义（事实）、情景（经历）、程序（行为规则）——比 mem0 多了情景和程序记忆
2. **双轨形成**：热路径（Agent 主动存）+ 后台（异步提取）——比 mem0 多了热路径
3. **结构化记忆**：Pydantic schema 定义记忆结构——比 mem0 的非结构化字符串更严谨
4. **提示词优化器**：让 Agent 行为自动进化——mem0 和 GBrain 都没有的独特能力
5. **三维检索排序**：相似度 + 重要性 + 强度——比 mem0 的纯相似度更全面（但缺 BM25 和实体匹配）

### 不足

1. **无时间感知**——没有 mem0 的 Temporal Reasoning
2. **无图记忆**——没有 mem0 的 Neo4j 实体关系
3. **检索深度不如 mem0**——没有 BM25 关键词匹配和实体 boost
4. **无论文验证**——性能表现未知
5. **存储选择少**——依赖 LangGraph BaseStore，不如 mem0 的 24+ 向量库

### 对项目的建议

如果项目最看重：
- **与 LangGraph 的原生集成** + **Agent 行为自进化** → 选 LangMem
- **检索深度** + **时间感知** + **论文验证** → 选 mem0
- **两者兼得** → 可以用 LangMem 的程序记忆（提示词优化）+ mem0 的语义记忆（事实存储），互补使用

---

*本分析基于 LangMem 官方文档（langchain-ai.github.io/langmem）、API Reference、概念指南和快速入门编写。*
