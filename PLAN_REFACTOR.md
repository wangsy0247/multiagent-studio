# Multiagent-Studio 完整重构计划

> **总览**: 后端对齐 DeerFlow 20 中间件架构 + 前端从"画布编排"重构为"Agent 团队协作"

## 目标

1. **`make_lead_agent`**（应用工厂）— 与 DeerFlow 相同的 20 个中间件，正确的钩子位置
2. **`create_agent`**（SDK 工厂）— 相同的 20 个中间件，通过 `RuntimeFeatures` 声明开关配置
3. 修复当前 5 个中间件的钩子位置错误
4. 补齐缺失的 3 个中间件
5. **前端重构**: 移除画布 → Agent 管理 + 项目协作 + 任务面板

## Phase 概览

```
Phase 1-3   ████████ 后端中间件修复 + 补齐 (无架构变更)
Phase 4-7   ████████ 后端工厂模式重构 (make_lead_agent + create_agent)
Phase 8-10  ████████ 前端重构 (画布 → Agent团队)
```

---

## 一、现状 vs 目标对比

### 1.1 中间件数量

| 项目 | 中间件数量 |
|------|-----------|
| DeerFlow | 20 |
| multiagent-studio 当前 | 17 |
| multiagent-studio 目标 | 20 |

### 1.2 缺失的 3 个中间件

| # | 中间件 | DeerFlow 文件 | 钩子 | 作用 |
|---|--------|--------------|------|------|
| 7 | **SandboxAuditMiddleware** | `sandbox_audit_middleware.py` | `wrap_tool_call` + `awrap_tool_call` | 审计沙箱 shell/文件操作 |
| 18 | **SafetyFinishReasonMiddleware** | `safety_finish_reason_middleware.py` | `after_model` + `aafter_model` | 检测提供者安全终止并抑制 tool_calls |
| 16 | **DeferredToolFilterMiddleware** | `deferred_tool_filter_middleware.py` | `wrap_model_call`+`awrap_model_call`, `wrap_tool_call`+`awrap_tool_call` | 隐藏延迟 MCP 工具 schema |

### 1.3 钩子位置错误的 5 个中间件

| 中间件 | 当前钩子（错误） | 正确钩子（DeerFlow） | 问题影响 |
|--------|-----------------|---------------------|---------|
| **GuardrailMiddleware** | `abefore_agent` | `awrap_tool_call` | 工具授权检查应在工具调用时触发，而非 agent 启动时。每次新消息轮才触发，跳过了 ReAct 循环中的重复工具调用 |
| **LoopDetectionMiddleware** | `abefore_model` + `awrap_model_call`（透传） | `before_agent`（清理旧警告）+ `after_model`（检测循环）+ `wrap_model_call`（注入警告）+ `after_agent`（清理） | 缺少完整的警告注入/清理机制；`abefore_model` 无法获取最新的模型输出来检测循环 |
| **ViewImageMiddleware** | `awrap_tool_call` | `before_model` + `abefore_model` | 图像数据应该在模型看到消息之前注入到消息历史中，而不是包装工具调用 |
| **SubagentLimitMiddleware** | `abefore_model` | `after_model` + `aafter_model` | 截断过量 task 调用应在模型输出后（`after_model`），而非模型调用前 |
| **ClarificationMiddleware** | `abefore_agent` + `aafter_agent` + `wrap_tool_call` + `awrap_tool_call` | `wrap_tool_call` + `awrap_tool_call` | 多余的 `abefore_agent`/`aafter_agent` 处理澄清回复注入，应由 worker 层处理 |

---

## 二、分阶段计划

### Phase 1: 修复中间件钩子位置（不改架构）

#### 1.1 GuardrailMiddleware

**文件**: `harness/middleware/guardrail.py`

**当前钩子**:
```python
async def abefore_agent(self, state, runtime) -> dict | None:
    # 基于 agent_type 计算工具权限，存入 state["metadata"]
```

**改为**:
```python
async def awrap_tool_call(self, request, handler):
    # 对每个 tool_call 执行授权检查
    # 拒绝 → 返回 error ToolMessage
    # 通过 → await handler(request)
```

**注意**: 当前 GuardrailMiddleware 是基于 agent_type 的 allowlist/denylist，改为 `awrap_tool_call` 后需要在每个 tool_call 级别检查。当前实现是"在 agent 启动时计算权限"——这在多轮 ReAct 中只执行一次。应改为 DeerFlow 的 `GuardrailProvider` 协议模式。

#### 1.2 LoopDetectionMiddleware

**文件**: `harness/middleware/loop_detection.py`

**当前钩子**:
```python
async def abefore_model(self, state, runtime):  # ← 检测循环
    # 哈希最近 N 条消息，匹配 loop_history 计数
    # 超阈值 → 剥离 tool_calls + 注入 SystemMessage

async def awrap_model_call(self, request, handler):  # ← 透传
    return await handler(request)
```

**改为 DeerFlow 模式**:
```python
async def abefore_agent(self, state, runtime):
    # 清理同一 thread 的其他 run 的陈旧的 pending warnings

async def aafter_model(self, state, runtime):
    # 基于哈希 + 基于频率的双重检测
    # 超过 warn_threshold → 排队警告消息
    # 超过 hard_limit → 剥离 tool_calls，强制文本回答

async def awrap_model_call(self, request, handler):
    # 将排队的警告注入到下一个模型请求的 messages 中
    # 警告是隐藏 HumanMessage，不破坏 tool_call 配对

async def aafter_agent(self, state, runtime):
    # 清理当前 thread/run 的 pending warnings
```

**关键变更**:
- 检测逻辑从 `abefore_model` 移到 `aafter_model`（模型输出后才能判断循环）
- `awrap_model_call` 不再透传，而是注入排队的警告
- 新增 `abefore_agent` / `aafter_agent` 的清理逻辑

#### 1.3 ViewImageMiddleware

**文件**: `harness/middleware/view_image.py`

**当前钩子**:
```python
async def awrap_tool_call(self, request, handler):
    # 拦截 view_image 工具调用
    # 将文件路径解析为 base64 data URL
```

**改为**:
```python
async def abefore_model(self, state, runtime):
    # 在模型调用前，扫描消息历史中的 view_image ToolMessage
    # 将图像文件内容注入为 HumanMessage(content=[{"type": "image_url", ...}])
    # 缓存已查看图像到 state["viewed_images"] 避免重复读取
```

**关键变更**: 图像注入应该是"消息预处理"（`before_model`），不是"工具调用包装"（`wrap_tool_call`）。工具本身正常执行返回文件路径，中间件在下一轮 LLM 调用前把路径替换为 base64 数据。

#### 1.4 SubagentLimitMiddleware

**文件**: `harness/middleware/subagent_limit.py`

**当前钩子**:
```python
async def abefore_model(self, state, runtime):
    # 检查最后的 AIMessage 中的 task tool_calls
    # 如果超过 max_concurrent → 截断
```

**改为**:
```python
async def aafter_model(self, state, runtime):
    # 模型刚输出了 AIMessage(tool_calls=[...])
    # 如果 task 调用数 > max_concurrent → 截断多余的
    # 注入警告提醒模型下次批处理
```

**关键变更**: 从 `abefore_model`（消息发送给模型前）移到 `aafter_model`（模型输出后）。截断应该在模型产出了 tool_calls 之后立即执行，而不是在下一轮开始前。

#### 1.5 ClarificationMiddleware

**文件**: `harness/middleware/clarification.py`

**当前钩子**（2 个）:
```python
# wrap_tool_call — 拦截 ask_clarification → Command(goto=END)
# awrap_tool_call — 同上 async
```

**关键变更**: 对齐 DeerFlow 的消息驱动 HITL，不再使用自定义 `pending_clarification` 状态键。澄清请求被转换成 `ToolMessage`，结构化元数据保存在 `additional_kwargs["clarification"]` 中；pending 状态通过扫描消息历史（`get_pending_clarification`）推断。用户回答由 `main.py::respond_to_clarification()` 直接作为 `HumanMessage` 追加到消息列表后恢复执行。

---

### Phase 2: 补齐缺失的 3 个中间件

#### 2.1 新建 SandboxAuditMiddleware

**新文件**: `harness/middleware/sandbox_audit.py`

**钩子**: `wrap_tool_call` + `awrap_tool_call`

**功能**:
- 审计 `bash` / `file_write` / `file_read` / `str_replace` 等沙箱工具调用
- 将命令/参数/结果记录到 RunJournal
- 对高风险命令发出警告日志

**参考实现**: DeerFlow `sandbox_audit_middleware.py`

**链中位置**: 在 `GuardrailMiddleware` 之后、`ToolErrorHandlingMiddleware` 之前

#### 2.2 新建 SafetyFinishReasonMiddleware

**新文件**: `harness/middleware/safety_finish_reason.py`

**钩子**: `after_model` + `aafter_model`

**功能**:
- 检测 LLM 响应的 `finish_reason`（OpenAI `content_filter`、Anthropic `refusal`、Gemini `SAFETY`）
- 如果模型因安全原因终止但仍有 `tool_calls`，剥离它们
- 附加用户可见的解释消息
- 记录 `safety_termination` 流事件

**参考实现**: DeerFlow `safety_finish_reason_middleware.py` + `safety_termination_detectors.py`

**链中位置**: 在自定义中间件之后、`ClarificationMiddleware` 之前（确保 `after_model` 逆序链中 Safety 先运行）

#### 2.3 新建 DeferredToolFilterMiddleware

**新文件**: `harness/middleware/deferred_tool_filter.py`

**钩子**: `wrap_model_call` + `awrap_model_call`, `wrap_tool_call` + `awrap_tool_call`

**功能**:
- `wrap_model_call`: 从 `request.tools` 中过滤掉延迟工具 schema（MCP 工具默认隐藏）
- `wrap_tool_call`: 阻止执行任何未被 `tool_search` 提升的延迟工具调用

**链中位置**: 在 `ViewImageMiddleware` 之后、`SubagentLimitMiddleware` 之前

**依赖**: 需要 `tool_search.enabled` 配置项（当前项目是否已有此配置？需要确认）

---

### Phase 3: 更新中间件顺序

当前 `AGENT_MIDDLEWARE_ORDER`（17 个）需要更新为 20 个：

```python
AGENT_MIDDLEWARE_ORDER: list[type[HarnessAgentMiddleware]] = [
    # [0-2] Sandbox infrastructure
    ThreadDataMiddleware,
    UploadsMiddleware,
    SandboxMiddleware,
    # [3-5] wrap_model_call onion
    LLMErrorHandlingMiddleware,     # outermost
    LoopDetectionMiddleware,         # middle
    DanglingToolCallMiddleware,     # innermost
    # [6] Guardrail (awrap_tool_call)
    GuardrailMiddleware,
    # [7] SandboxAudit (awrap_tool_call) ← 新增
    SandboxAuditMiddleware,
    # [8] ToolErrorHandling (awrap_tool_call)
    ToolErrorHandlingMiddleware,
    # [9] DynamicContext (abefore_agent)
    DynamicContextMiddleware,
    # [10] Summarization (abefore_model)
    SummarizationMiddleware,
    # [11] Todo (Plan Mode)
    TodoMiddleware,
    # [12] TokenUsage (aafter_model)
    TokenUsageMiddleware,
    # [13] Title (aafter_model)
    TitleMiddleware,
    # [14] Memory (aafter_agent)
    MemoryMiddleware,
    # [15] ViewImage (abefore_model)  ← 钩子修正
    ViewImageMiddleware,
    # [16] DeferredToolFilter (wrap_model_call + wrap_tool_call) ← 新增
    DeferredToolFilterMiddleware,
    # [17] SubagentLimit (aafter_model) ← 钩子修正
    SubagentLimitMiddleware,
    # [18] SafetyFinishReason (aafter_model) ← 新增
    SafetyFinishReasonMiddleware,
    # [19] Clarification (always last, wrap_tool_call) ← 钩子修正
    ClarificationMiddleware,
]
```

---

### Phase 4: 实现 `make_lead_agent`（应用工厂）

**新文件**: `harness/agents/lead_agent.py`

**参考**: DeerFlow `agents/lead_agent/agent.py::_make_lead_agent()`

```python
def make_lead_agent(config: RunnableConfig):
    """LangGraph graph factory — 应用工厂，从 config.yaml 全自动装配"""
    # 1. 从 RunnableConfig.configurable 提取运行时配置
    # 2. 加载 ConfigManager 获取 YAML 配置
    # 3. 解析模型名称: 请求 → agent config → 全局默认
    # 4. 验证 thinking/vision 支持
    # 5. 注入 metadata（agent name, model name, flags, tool groups, skills）
    # 6. 注入 tracing callbacks（Langfuse）到图根节点
    # 7. 加载 skills + 应用 allowed-tools 策略
    # 8. 构建工具列表
    # 9. 调用 _build_middlewares() 组装 20 个中间件
    # 10. 调用 create_agent(model, tools, middleware, system_prompt, state_schema=HarnessState)
```

**改造现有 `main.py`**: `HarnessService.initialize()` 改为调用 `make_lead_agent` 而不是手动 `_register_middlewares()` + `build_harness_graph()`。

**关键代码位置**: 
- `main.py:338 _register_middlewares()` → 改为 `_build_middlewares(config)` 
- `main.py:119-218 initialize()` → 简化，中间件组装交给 `make_lead_agent`
- `graph_factory.py` → 可能保留作为 `make_lead_agent` 的子调用

---

### Phase 5: 实现 `create_agent`（SDK 工厂）

**新文件**: `harness/agents/factory.py`

**参考**: DeerFlow `agents/factory.py::create_deerflow_agent()` + `_assemble_from_features()`

**对外 API**:
```python
from harness.agents.factory import create_agent
from harness.agents.features import RuntimeFeatures

# 最小用法
graph = create_agent(model)

# 声明式
graph = create_agent(
    model,
    tools=[...],
    features=RuntimeFeatures(guardrail=custom_guardrail, auto_title=True),
    system_prompt="You are...",
)

# 完全接管
graph = create_agent(model, middleware=[mw1, mw2, ...])
```

**RuntimeFeatures 字段**（12 个开关，控制 20 个中间件）:
```python
@dataclass
class RuntimeFeatures:
    # Sandbox (3 middlewares: ThreadData + Uploads + Sandbox)
    sandbox: bool | AgentMiddleware = True

    # Guardrail (awrap_tool_call) ← 用户指定为可选
    guardrail: Literal[False] | AgentMiddleware = False

    # Dynamic context (date + memory injection)
    dynamic_context: bool | AgentMiddleware = True

    # Summarization
    summarization: Literal[False] | AgentMiddleware = False

    # Plan Mode Todo
    todo: bool | AgentMiddleware = False

    # Token usage tracking
    token_usage: bool | AgentMiddleware = True

    # Auto Title ← 用户指定为可选
    auto_title: bool | AgentMiddleware = False

    # Memory
    memory: bool | AgentMiddleware = True

    # Vision / ViewImage ← 用户指定为可选
    vision: bool | AgentMiddleware = False

    # Subagent ← 用户指定为可选
    subagent: bool | AgentMiddleware = False

    # Loop detection
    loop_detection: bool | AgentMiddleware = True

    # Tool search (controls DeferredToolFilterMiddleware)
    tool_search: bool | AgentMiddleware = False

    # Tool error handling
    tool_error_handling: bool | AgentMiddleware = True

    # Clarification
    clarification: bool | AgentMiddleware = True
```

**中间件装配逻辑**（`_assemble_from_features()`）:

```text
[0-2]   Sandbox 组        → feat.sandbox
[3-5]   wrap_model_call   → 始终启用 (LLMError + LoopDetection + DanglingToolCall)
[6]     Guardrail          → feat.guardrail
[7]     SandboxAudit       → 始终启用
[8]     ToolErrorHandling  → feat.tool_error_handling
[9]     DynamicContext     → feat.dynamic_context
[10]    Summarization      → feat.summarization
[11]    Todo               → feat.todo
[12]    TokenUsage         → feat.token_usage
[13]    Title              → feat.auto_title
[14]    Memory             → feat.memory
[15]    ViewImage          → feat.vision
[16]    DeferredToolFilter → feat.tool_search
[17]    SubagentLimit      → feat.subagent
[18]    LoopDetection      → feat.loop_detection
[19]    SafetyFinishReason → 始终启用
[20]    Clarification      → 始终启用 (always last)
```

---

### Phase 6: SDK 工厂的 SOUL 与记忆路径系统

这是 `create_agent`（SDK 工厂）与 `make_lead_agent`（应用工厂）的关键设计差异。

#### 6.1 核心设计：`system_prompt` 即 SOUL

在 DeerFlow 中，`make_lead_agent` 的系统提示词是**静态模板**（通过 `apply_prompt_template()` 从 skills + subagent 指令 + 记忆指令组装），所有动态内容（记忆、日期）通过 `DynamicContextMiddleware` 以 `<system-reminder>` 注入。

在 `create_agent`（SDK 工厂）中，设计不同：

```
create_agent(model, system_prompt="You are a helpful assistant...")
                           │
                           ▼
              DynamicContextMiddleware(soul=system_prompt)
                           │
                           ▼
              <system-reminder>
              <soul>You are a helpful assistant...</soul>   ← SOUL 来自调用者
              <memory>...</memory>                            ← 记忆来自 JSON 文件
              <current_date>2026-06-28</current_date>        ← 日期始终注入
              </system-reminder>
```

**与 DeerFlow 的差异**：

| 对比维度 | `make_lead_agent` (DeerFlow) | `create_agent` (SDK) |
|---------|---------------------------|----------------------|
| 系统提示词来源 | `apply_prompt_template()` 模板组装 | 调用者传入 `system_prompt` 参数 |
| 系统提示词内容 | skills + subagent 指令 + 记忆指令 | 纯 SOUL（人格定义） |
| SOUL 位置 | 静态 system prompt（前缀缓存友好） | `<system-reminder>` 中的 `<soul>` 块 |
| 动态内容 | `<system-reminder>`（记忆 + 日期） | `<system-reminder>`（SOUL + 记忆 + 日期） |

#### 6.2 DynamicContextMiddleware 改造

**文件**: `harness/middleware/dynamic_context.py`

**新增参数**: `soul: str | None = None`

```python
class DynamicContextMiddleware(HarnessAgentMiddleware):
    def __init__(self, config: dict | None = None, *,
                 agent_name: str | None = None,
                 soul: str | None = None):    # ← 新增
        super().__init__(config)
        self._agent_name = agent_name
        self._soul = soul

    def _build_full_reminder(self, *, user_id: str | None = None) -> tuple[str, str]:
        # 构建结构:
        #   <system-reminder>
        #   <soul>调用者传入的 system_prompt</soul>      ← SDK 模式
        #   <memory>从 JSON 文件加载的记忆</memory>
        #   <current_date>...</current_date>
        #   </system-reminder>
```

**在 SDK 工厂中的使用**:

```python
# harness/agents/factory.py
def _assemble_from_features(feat, *, name, system_prompt, ...):
    if feat.dynamic_context is not False:
        if isinstance(feat.dynamic_context, AgentMiddleware):
            chain.append(feat.dynamic_context)
        else:
            chain.append(DynamicContextMiddleware(
                agent_name=name,
                soul=system_prompt,   # ← 将 system_prompt 作为 SOUL 注入
            ))
```

**在应用工厂中的使用**（保持不变）:

```python
# harness/agents/lead_agent.py
def _build_middlewares(config, agent_name, ...):
    middlewares.append(DynamicContextMiddleware(
        agent_name=agent_name,
        soul=None,   # ← 应用工厂不注入 SOUL（已在静态 prompt 中）
    ))
```

#### 6.3 记忆路径：per-agent per-user 存储

**当前路径结构**（已支持，无需修改）:

```text
~/.multiagent-studio/
├── memory/
│   └── users/
│       └── {user_id}/
│           ├── memory.json                          ← 默认 agent (agent_name=None)
│           └── agents/
│               └── {agent_name}/
│                   ├── memory.json                  ← per-agent 记忆
│                   ├── SOUL.md                      ← agent 人格定义
│                   └── config.yaml                  ← agent 配置（model、tools 等）
```

**对应代码**:

| 操作 | 代码路径 | 最终文件 |
|------|---------|---------|
| **写记忆** | `MemoryMiddleware.aafter_agent()` → `queue.add(agent_name=...)` → `MemoryUpdater._do_update_memory()` → `storage.save(updated_memory, agent_name, user_id=user_id)` | `{root}/users/{uid}/agents/{name}/memory.json` |
| **读记忆** | `DynamicContextMiddleware._build_full_reminder()` → `get_memory_data(self._agent_name, user_id=...)` → `storage.load(agent_name, user_id=user_id)` | `{root}/users/{uid}/agents/{name}/memory.json` |
| **读 SOUL** | `create_agent()` → `DynamicContextMiddleware(soul=system_prompt)` | 不读文件，直接使用传入的 `system_prompt` |
| **写 SOUL** | SDK 调用者负责持久化（可选，通过 `agents_config` 模块） | `{root}/users/{uid}/agents/{name}/SOUL.md` |

#### 6.4 路径一致性检查

**验证规则**: `MemoryMiddleware` 和 `DynamicContextMiddleware` **必须**使用相同的 `agent_name` 初始化。

```python
# harness/agents/factory.py — SDK 工厂中的一致性保障
def create_agent(model, *, name="default", system_prompt=None, features=None, ...):
    # agent_name 同时传给两个中间件，确保读写同一路径
    chain = _assemble_from_features(features, name=name, system_prompt=system_prompt)
    # MemoryMiddleware(agent_name=name)       → 写路径
    # DynamicContextMiddleware(agent_name=name, soul=system_prompt)  → 读路径
    # 两者使用相同的 agent_name → 读写同一 memory.json ✓
```

**潜在陷阱**: 如果调用者绕过工厂直接构造中间件，可能传入不同的 `agent_name`:

```python
# ❌ 错误：读路径使用 "agent-a"，写路径使用 "agent-b"
middlewares = [
    DynamicContextMiddleware(agent_name="agent-a"),   # 读 agent-a/memory.json
    MemoryMiddleware(agent_name="agent-b"),            # 写 agent-b/memory.json
]
# → 记忆永远不会被注入，因为读写的不是同一个文件
```

**解决方案**: 在 `DynamicContextMiddleware` 的 `__init__` 中添加验证日志:

```python
def __init__(self, ..., agent_name=None, soul=None):
    if soul:
        logger.info("DynamicContextMiddleware: SDK mode — using caller-provided SOUL (agent=%s)", agent_name)
    else:
        logger.debug("DynamicContextMiddleware: App mode — reading memory for agent=%s", agent_name)
```

#### 6.5 AgentsConfig 模块（SOUL.md 持久化）

**新文件**: `harness/config/agents_config.py`

**参考**: DeerFlow `config/agents_config.py`

提供 per-agent 配置和 SOUL.md 的读写:

```python
# 目录布局
SOUL_FILENAME = "SOUL.md"
AGENT_NAME_PATTERN = re.compile(r"^[A-Za-z0-9-]+$")

def resolve_agent_dir(name: str, *, user_id: str | None = None) -> Path:
    """返回 agent 目录：{base_dir}/users/{user_id}/agents/{name}/"""

def load_agent_config(name: str, *, user_id: str | None = None) -> AgentConfig | None:
    """加载 agent 的 config.yaml"""

def load_agent_soul(name: str, *, user_id: str | None = None) -> str | None:
    """加载 agent 的 SOUL.md"""

def save_agent_soul(name: str, content: str, *, user_id: str | None = None) -> bool:
    """保存 agent 的 SOUL.md（原子写入）"""

def list_custom_agents(*, user_id: str | None = None) -> list[AgentConfig]:
    """列出所有自定义 agent"""
```

---

### Phase 7: 更新 `__init__.py` 导出

**文件**: `harness/middleware/__init__.py`

新增导出:
```python
from harness.middleware.sandbox_audit import SandboxAuditMiddleware
from harness.middleware.safety_finish_reason import SafetyFinishReasonMiddleware
from harness.middleware.deferred_tool_filter import DeferredToolFilterMiddleware
```

**文件**: `harness/agents/__init__.py`（新建或更新）

```python
from harness.agents.factory import create_agent
from harness.agents.features import RuntimeFeatures, Next, Prev
from harness.agents.lead_agent import make_lead_agent
```

**文件**: `harness/config/__init__.py`（更新导出）

```python
from harness.config.agents_config import (
    AgentConfig,
    resolve_agent_dir,
    load_agent_config,
    load_agent_soul,
    list_custom_agents,
)
```

---

## 三、涉及文件清单

| 文件 | 操作 | Phase |
|------|------|-------|
| `harness/middleware/guardrail.py` | 重写钩子: `abefore_agent` → `awrap_tool_call` | 1 |
| `harness/middleware/loop_detection.py` | 重写: 增加 `abefore_agent`/`aafter_agent`, 检测从 `abefore_model` 移到 `aafter_model` | 1 |
| `harness/middleware/view_image.py` | 重写钩子: `awrap_tool_call` → `abefore_model` | 1 |
| `harness/middleware/subagent_limit.py` | 移动钩子: `abefore_model` → `aafter_model` | 1 |
| `harness/middleware/clarification.py` | 删除 `abefore_agent` 和 `aafter_agent` | 1 |
| `harness/middleware/dynamic_context.py` | 新增 `soul` 参数，`_build_full_reminder()` 注入 `<soul>` 块 | 6 |
| `harness/middleware/sandbox_audit.py` | **新建** | 2 |
| `harness/middleware/safety_finish_reason.py` | **新建** | 2 |
| `harness/middleware/deferred_tool_filter.py` | **新建** | 2 |
| `harness/middleware/__init__.py` | 更新 `AGENT_MIDDLEWARE_ORDER` + 导出 | 7 |
| `harness/middleware/base.py` | 无修改 | - |
| `harness/agents/__init__.py` | **新建** | 7 |
| `harness/agents/features.py` | 更新 `RuntimeFeatures` 字段（14 个开关） | 5 |
| `harness/agents/factory.py` | **新建** — `create_agent()` + `_assemble_from_features()` + `_insert_extra()` | 5 |
| `harness/agents/lead_agent.py` | **新建** — `make_lead_agent()` + `_build_middlewares()` | 4 |
| `harness/config/agents_config.py` | **新建** — `AgentConfig`、`resolve_agent_dir()`、`load_agent_soul()` 等 | 6 |
| `harness/main.py` | 简化 `_register_middlewares()`，改为调用 `make_lead_agent` 的 `_build_middlewares` | 4 |
| `harness/graph_factory.py` | 可能简化或保留为内部工具函数 | 4 |
| `harness/config/yaml_config.py` | 可能需要新增 `safety_finish_reason` 和 `sandbox_audit` 配置节 | 2 |
| `harness/config/config_manager.py` | 可能需要新增配置加载 | 2 |

---

## 四、前端重构计划

### 设计理念：从"画布编排"到"Agent 团队协作"

```
当前架构（画布模式）                          目标架构（团队模式）
─────────────────────                      ─────────────────────

线程 (Thread)                              项目 (Project)
  ├── 对话标签 (Chat)                         ├── 对话标签 (Chat)
  ├── 画布标签 (Graph) ← 移除                 ├── 任务面板标签 (Tasks)  ← 新增
  │   └── 拖拽 SubAgent 节点                  ├── 成员标签 (Members)    ← 新增
  │   └── 连线定义拓扑                         │   └── 从 Agent 库添加
  │   └── ConfigPanel 配置                     │   └── 团队协作配置
  └── 监控标签 (Monitor)                      └── 监控标签 (Monitor)

Agent (画布内临时创建)                       Agent (独立管理)          ← 新增一级导航
  └── 依附于线程，无持久化                       ├── Agent 列表页
                                               ├── Agent 创建/编辑页
                                               │   ├── SOUL.md 编辑器
                                               │   ├── config.yaml 表单
                                               │   └── 记忆查看
                                               └── Agent 可被多个项目复用
```

### Phase 8: 移除画布 + 新增 Agent 管理

#### 8.1 移除画布相关代码

| 操作 | 文件/目录 | 说明 |
|------|----------|------|
| 删除目录 | `src/components/canvas/` | 5 个文件：AgentCanvas, AgentNode, CanvasControls, ConfigPanel, NodePalette |
| 删除文件 | `src/lib/canvas-store.ts` | Zustand canvas store |
| 修改文件 | `frontend/package.json` | 移除 `reactflow` 依赖 |
| 修改文件 | `src/lib/types.ts` | 移除 `AgentNode`, `ExecutionGraph`, `CanvasNode`, `CanvasEdge` 类型 |
| 修改文件 | `src/app/(dashboard)/threads/[thread_id]/page.tsx` | 移除 "画布" tab，移除 `AgentCanvas` lazy import |

#### 8.2 新增一级导航项

**修改文件**: `src/components/layout/Sidebar.tsx`

在现有 "会话" 列表之上，新增两个导航分组：

```text
导航结构:
  🏠 首页
  📁 项目        ← 新增
  🤖 Agent       ← 新增
  ─────────
  💬 会话
  ⚙️ 设置
  🔧 管理 (admin only)
```

#### 8.3 新增 Agent 管理页面

**路由**: `/agents`

**新文件**:

| 文件 | 说明 |
|------|------|
| `src/app/(dashboard)/agents/page.tsx` | Agent 列表页 |
| `src/app/(dashboard)/agents/[name]/page.tsx` | Agent 详情/编辑页 |
| `src/components/agents/AgentCard.tsx` | Agent 卡片组件 |
| `src/components/agents/AgentForm.tsx` | Agent 创建/编辑表单 |
| `src/components/agents/SoulEditor.tsx` | SOUL.md 编辑器 |
| `src/lib/agent-store.ts` | Agent 状态管理 (Zustand) |

**Agent 列表页功能**:
- 卡片网格展示所有自定义 Agent
- 每个卡片：名称、描述、模型、工具数、记忆大小
- "新建 Agent" 按钮
- 点击卡片进入详情页

**Agent 创建/编辑页功能**:
```
┌──────────────────────────────────────────────┐
│  Agent 名称: [my-agent]                       │
│  显示名称:   [My Agent]                       │
│  描述:       [A helpful coding assistant]     │
│                                              │
│  ┌─ SOUL.md ──────────────────────────────┐  │
│  │  # My Agent Soul                        │  │
│  │                                          │  │
│  │  You are a helpful coding assistant...   │  │
│  │                                          │  │
│  └──────────────────────────────────────────┘  │
│                                              │
│  模型: [gpt-4o ▾]                            │
│  工具组: [☑ coding] [☑ search] [☐ vision]   │
│  技能:   [☑ code-review] [☐ deploy]          │
│                                              │
│  ┌─ 长期记忆 ──────────────────────────────┐  │
│  │  事实数: 42  最后更新: 2026-06-28        │  │
│  │  [查看记忆] [清除记忆]                    │  │
│  └──────────────────────────────────────────┘  │
│                                              │
│  [保存] [删除 Agent]                          │
└──────────────────────────────────────────────┘
```

**后端 API 需求**（新增或复用）:

| 端点 | 方法 | 说明 |
|------|------|------|
| `/api/agents` | GET | 列出所有 Agent |
| `/api/agents` | POST | 创建 Agent (SOUL.md + config.yaml) |
| `/api/agents/{name}` | GET | 获取 Agent 详情 |
| `/api/agents/{name}` | PUT | 更新 Agent |
| `/api/agents/{name}` | DELETE | 删除 Agent |
| `/api/agents/{name}/memory` | GET | 获取 Agent 记忆数据 |
| `/api/agents/{name}/memory` | DELETE | 清除 Agent 记忆 |

---

### Phase 9: 新增项目 + 任务面板

#### 9.1 项目概念

项目 (Project) 是对线程 (Thread) 的上层封装：

```text
Project
├── id, name, description
├── members: Agent[]          ← 团队成员（可添加多个 Agent）
├── threads: Thread[]         ← 项目内的会话
│   └── 每个 Thread 关联一个 Agent 执行
└── tasks: Task[]             ← 任务面板
```

#### 9.2 新增项目相关页面

**路由**: `/projects`, `/projects/[id]`

**新文件**:

| 文件 | 说明 |
|------|------|
| `src/app/(dashboard)/projects/page.tsx` | 项目列表页 |
| `src/app/(dashboard)/projects/[id]/page.tsx` | 项目详情页 |
| `src/components/projects/ProjectCard.tsx` | 项目卡片组件 |
| `src/components/projects/CreateProjectDialog.tsx` | 创建项目对话框 |
| `src/components/projects/MemberList.tsx` | Agent 团队成员列表 |
| `src/components/projects/AddMemberDialog.tsx` | 从 Agent 库添加成员 |
| `src/lib/project-store.ts` | 项目状态管理 (Zustand) |

**项目列表页**:
```
┌──────────────────────────────────────────────┐
│  📁 项目                        [+ 新建项目]  │
│                                              │
│  ┌────────────────────────────┐              │
│  │ 🚀 前端重构项目              │              │
│  │ 团队成员: coder, reviewer   │              │
│  │ 3 个会话 · 5 个任务          │              │
│  └────────────────────────────┘              │
│  ┌────────────────────────────┐              │
│  │ 📊 数据分析项目              │              │
│  │ 团队成员: analyst, coder    │              │
│  │ 1 个会话 · 2 个任务          │              │
│  └────────────────────────────┘              │
└──────────────────────────────────────────────┘
```

**项目详情页**（3 个标签）:

```
┌────────────────────────────────────────────────────┐
│  🚀 前端重构项目                                    │
│  [对话] [任务面板] [团队成员] [监控]                   │
├────────────────────────────────────────────────────┤
│                                                    │
│  标签 1: 对话 (Chat)                                │
│  ┌─ 左侧：会话列表（可切换 Agent） ─────────────────┐ │
│  │ 💬 与 coder 的对话                              │ │
│  │ 💬 与 reviewer 的对话                           │ │
│  │ [+ 新建会话]                                    │ │
│  └────────────────────────────────────────────────┘ │
│  ┌─ 右侧：ChatPanel (复用现有组件) ────────────────┐ │
│  │ [消息列表]                                       │ │
│  │ [输入框]                                         │ │
│  └────────────────────────────────────────────────┘ │
│                                                    │
│  标签 2: 任务面板 (Tasks)                            │
│  ┌──────────────────────────────────────────────┐  │
│  │  待办        │  进行中       │  已完成        │  │
│  │  ┌────────┐  │  ┌────────┐  │  ┌────────┐   │  │
│  │  │ 任务 1  │  │  │ 任务 3  │  │  │ 任务 2  │   │  │
│  │  │ 任务 4  │  │  └────────┘  │  │ 任务 5  │   │  │
│  │  └────────┘  │              │  └────────┘   │  │
│  └──────────────────────────────────────────────┘  │
│  [+ 添加任务]                                       │
│                                                    │
│  标签 3: 团队成员 (Members)                          │
│  ┌──────────────────────────────────────────────┐  │
│  │  🤖 coder       [gpt-4o]    [移除]           │  │
│  │  🤖 reviewer    [claude-sonnet] [移除]        │  │
│  │  [+ 添加 Agent]                               │  │
│  └──────────────────────────────────────────────┘  │
└────────────────────────────────────────────────────┘
```

#### 9.3 任务面板组件

**新文件**:

| 文件 | 说明 |
|------|------|
| `src/components/tasks/TaskBoard.tsx` | 看板主组件（3 列：待办/进行中/已完成） |
| `src/components/tasks/TaskCard.tsx` | 单个任务卡片 |
| `src/components/tasks/CreateTaskDialog.tsx` | 创建任务对话框 |
| `src/lib/task-store.ts` | 任务状态管理 (Zustand) |

**后端 API 需求**:

| 端点 | 方法 | 说明 |
|------|------|------|
| `/api/projects` | GET | 列出项目 |
| `/api/projects` | POST | 创建项目 |
| `/api/projects/{id}` | GET | 获取项目详情 |
| `/api/projects/{id}` | PUT | 更新项目 |
| `/api/projects/{id}` | DELETE | 删除项目 |
| `/api/projects/{id}/members` | POST | 添加 Agent 到项目 |
| `/api/projects/{id}/members/{agent_name}` | DELETE | 从项目移除 Agent |
| `/api/projects/{id}/tasks` | GET | 列出任务 |
| `/api/projects/{id}/tasks` | POST | 创建任务 |
| `/api/projects/{id}/tasks/{task_id}` | PUT | 更新任务（状态/描述/分配） |
| `/api/projects/{id}/tasks/{task_id}` | DELETE | 删除任务 |
| `/api/projects/{id}/threads` | GET | 列出项目下的会话 |
| `/api/projects/{id}/threads` | POST | 创建新会话（指定 Agent） |

---

### Phase 10: 数据模型 + 后端 API 适配

#### 10.1 前端类型新增

**修改文件**: `src/lib/types.ts`

```typescript
// Agent 定义（持久化在 ~/.multiagent-studio/users/{uid}/agents/{name}/）
export interface AgentDefinition {
  name: string;           // 唯一标识，如 "my-coder"
  display_name: string;   // 显示名称
  description: string;
  soul: string;           // SOUL.md 内容
  model: string;
  tool_groups: string[];
  skills: string[];
  memory_size: number;    // 记忆事实数
  created_at: string;
  updated_at: string;
}

// 项目
export interface Project {
  id: string;
  name: string;
  description: string;
  members: AgentDefinition[];
  thread_count: number;
  task_count: number;
  created_at: string;
  updated_at: string;
}

// 任务
export interface ProjectTask {
  id: string;
  project_id: string;
  title: string;
  description: string;
  status: "todo" | "in_progress" | "done";
  assigned_agent: string | null;  // agent name
  priority: "low" | "medium" | "high";
  created_at: string;
  updated_at: string;
}
```

#### 10.2 API Client 新增

**修改文件**: `src/lib/api-client.ts`

```typescript
// Agent API
export const agentsAPI = {
  list: () => api.get("/agents"),
  get: (name: string) => api.get(`/agents/${name}`),
  create: (data: Partial<AgentDefinition>) => api.post("/agents", data),
  update: (name: string, data: Partial<AgentDefinition>) => api.put(`/agents/${name}`, data),
  delete: (name: string) => api.delete(`/agents/${name}`),
  getMemory: (name: string) => api.get(`/agents/${name}/memory`),
  clearMemory: (name: string) => api.delete(`/agents/${name}/memory`),
};

// Project API
export const projectsAPI = {
  list: () => api.get("/projects"),
  get: (id: string) => api.get(`/projects/${id}`),
  create: (data: Partial<Project>) => api.post("/projects", data),
  update: (id: string, data: Partial<Project>) => api.put(`/projects/${id}`, data),
  delete: (id: string) => api.delete(`/projects/${id}`),
  addMember: (id: string, agentName: string) => api.post(`/projects/${id}/members`, { agent_name: agentName }),
  removeMember: (id: string, agentName: string) => api.delete(`/projects/${id}/members/${agentName}`),
  // Tasks
  listTasks: (id: string) => api.get(`/projects/${id}/tasks`),
  createTask: (id: string, data: Partial<ProjectTask>) => api.post(`/projects/${id}/tasks`, data),
  updateTask: (id: string, taskId: string, data: Partial<ProjectTask>) => api.put(`/projects/${id}/tasks/${taskId}`, data),
  deleteTask: (id: string, taskId: string) => api.delete(`/projects/${id}/tasks/${taskId}`),
  // Threads within project
  listThreads: (id: string) => api.get(`/projects/${id}/threads`),
  createThread: (id: string, agentName: string) => api.post(`/projects/${id}/threads`, { agent_name: agentName }),
};
```

#### 10.3 后端需新增的 API 路由

**文件**: `harness/api/routers.py` — 新增路由组

| 路由组 | 端点前缀 | 说明 |
|--------|---------|------|
| `agents_router` | `/api/agents` | Agent CRUD + 记忆管理 |
| `projects_router` | `/api/projects` | 项目 CRUD + 成员管理 + 任务管理 |

**数据存储**:

| 数据 | 存储位置 | 格式 |
|------|---------|------|
| Agent 定义 | `~/.multiagent-studio/users/{uid}/agents/{name}/SOUL.md` + `config.yaml` | Markdown + YAML |
| Agent 记忆 | `~/.multiagent-studio/users/{uid}/agents/{name}/memory.json` | JSON（复用现有记忆系统） |
| 项目定义 | `~/.multiagent-studio/users/{uid}/projects/{id}.json` | JSON |
| 项目任务 | `~/.multiagent-studio/users/{uid}/projects/{id}_tasks.json` | JSON |

---

## 五、修改文件清单（完整）

### 后端

| 文件 | 操作 | Phase |
|------|------|-------|
| `harness/middleware/guardrail.py` | 重写钩子: `abefore_agent` → `awrap_tool_call` | 1 |
| `harness/middleware/loop_detection.py` | 重写: 增加 `abefore_agent`/`aafter_agent`，检测从 `abefore_model` 移到 `aafter_model` | 1 |
| `harness/middleware/view_image.py` | 重写钩子: `awrap_tool_call` → `abefore_model` | 1 |
| `harness/middleware/subagent_limit.py` | 移动钩子: `abefore_model` → `aafter_model` | 1 |
| `harness/middleware/clarification.py` | 删除 `abefore_agent` 和 `aafter_agent` | 1 |
| `harness/middleware/dynamic_context.py` | 新增 `soul` 参数，`_build_full_reminder()` 注入 `<soul>` 块 | 6 |
| `harness/middleware/sandbox_audit.py` | **新建** | 2 |
| `harness/middleware/safety_finish_reason.py` | **新建** | 2 |
| `harness/middleware/deferred_tool_filter.py` | **新建** | 2 |
| `harness/middleware/__init__.py` | 更新 `AGENT_MIDDLEWARE_ORDER`（17→20）+ 导出 | 7 |
| `harness/agents/__init__.py` | **新建** | 7 |
| `harness/agents/features.py` | 更新 `RuntimeFeatures` 字段 | 5 |
| `harness/agents/factory.py` | **新建** — `create_agent()` + `_assemble_from_features()` + `_insert_extra()` | 5 |
| `harness/agents/lead_agent.py` | **新建** — `make_lead_agent()` + `_build_middlewares()` | 4 |
| `harness/config/agents_config.py` | **新建** — `AgentConfig`、`resolve_agent_dir()`、`load_agent_soul()` 等 | 6 |
| `harness/main.py` | 简化 `_register_middlewares()`，调用 `make_lead_agent` 的 `_build_middlewares` | 4 |
| `harness/graph_factory.py` | 简化或保留为内部工具函数 | 4 |
| `harness/api/routers.py` | 新增 `/api/agents`、`/api/projects` 路由组 | 10 |
| `harness/config/yaml_config.py` | 新增 `safety_finish_reason`、`sandbox_audit` 配置节 | 2 |

### 前端

| 文件 | 操作 | Phase |
|------|------|-------|
| `src/components/canvas/` (5 文件) | **删除** | 8 |
| `src/lib/canvas-store.ts` | **删除** | 8 |
| `src/lib/types.ts` | 移除画布类型，新增 Agent/Project/Task 类型 | 8 |
| `src/lib/api-client.ts` | 新增 `agentsAPI`、`projectsAPI` | 10 |
| `src/app/(dashboard)/threads/[thread_id]/page.tsx` | 移除"画布"标签 | 8 |
| `src/components/layout/Sidebar.tsx` | 新增"项目"和"Agent"导航项 | 8 |
| `src/app/(dashboard)/page.tsx` | 更新首页，移除画布编排卡片 | 8 |
| `src/app/(dashboard)/agents/page.tsx` | **新建** — Agent 列表页 | 8 |
| `src/app/(dashboard)/agents/[name]/page.tsx` | **新建** — Agent 编辑页 | 8 |
| `src/components/agents/AgentCard.tsx` | **新建** | 8 |
| `src/components/agents/AgentForm.tsx` | **新建** | 8 |
| `src/components/agents/SoulEditor.tsx` | **新建** | 8 |
| `src/lib/agent-store.ts` | **新建** | 8 |
| `src/app/(dashboard)/projects/page.tsx` | **新建** — 项目列表页 | 9 |
| `src/app/(dashboard)/projects/[id]/page.tsx` | **新建** — 项目详情页 | 9 |
| `src/components/projects/ProjectCard.tsx` | **新建** | 9 |
| `src/components/projects/CreateProjectDialog.tsx` | **新建** | 9 |
| `src/components/projects/MemberList.tsx` | **新建** | 9 |
| `src/components/projects/AddMemberDialog.tsx` | **新建** | 9 |
| `src/lib/project-store.ts` | **新建** | 9 |
| `src/components/tasks/TaskBoard.tsx` | **新建** — 看板组件 | 9 |
| `src/components/tasks/TaskCard.tsx` | **新建** | 9 |
| `src/components/tasks/CreateTaskDialog.tsx` | **新建** | 9 |
| `src/lib/task-store.ts` | **新建** | 9 |
| `frontend/package.json` | 移除 `reactflow` 依赖 | 8 |

---

## 六、验证标准（每个 Phase）

| Phase | 验证内容 |
|-------|---------|
| **1** | 现有测试全部通过；5 个中间件钩子在正确时机被调用 |
| **2** | 3 个新中间件各有一组单元测试；注册顺序正确 |
| **3** | `AGENT_MIDDLEWARE_ORDER` 20 个中间件与 DeerFlow 顺序一致；Clarification 始终最后 |
| **4** | `make_lead_agent(config)` 可被 `HarnessService` 使用；`POST /api/v1/execute` 正常响应 |
| **5** | `create_agent(model, features=RuntimeFeatures(...))` 独立可用；4 个可选中间件开关正确 |
| **6** | SDK 工厂的 `system_prompt` 正确出现在 `<soul>` 块中；记忆读写路径一致（相同 `agent_name`） |
| **7** | `from harness.agents import create_agent, make_lead_agent` 正常导入 |
| **8** | 画布组件全部移除无残留；Agent 列表页可访问；Agent 创建/编辑表单可正常保存 SOUL.md |
| **9** | 项目列表/详情页可访问；Agent 可被添加到项目；任务看板可拖拽改变状态 |
| **10** | 端到端：创建 Agent → 创建项目 → 添加 Agent 到项目 → 创建任务 → 在项目中与 Agent 对话 → SSE 流式响应正常 |
