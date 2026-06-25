# Harness 项目工具调用实现分析

> 分析范围：`multiagent-studio/harness` 目录下的工具注册、构建、执行与拦截链路。

---

## 目录

1. [整体架构](#1-整体架构)
2. [工具注册中心](#2-工具注册中心-toolsregistry)
3. [内置工具工厂](#3-内置工具工厂)
   - 3.1 [core.py](#31-corepy)
   - 3.2 [search.py](#32-searchpy)
   - 3.3 [code.py](#33-codepy)
   - 3.4 [files.py](#34-filespy)
   - 3.5 [abacus.py](#35-abacuspy)
   - 3.6 [mcp_adapter.py](#36-mcp_adapterpy)
4. [Lead Agent 工具组装](#4-lead-agent-工具组装)
5. [图编译与执行](#5-图编译与执行)
6. [工具调用中间件](#6-工具调用中间件)
7. [子代理工具调用](#7-子代理工具调用)
8. [完整数据流](#8-完整数据流)

---

## 1. 整体架构

Harness 的工具调用基于 **LangChain `create_agent()` + LangGraph** 的 ReAct 循环：

- **工具注册**：`ToolRegistry` 集中管理所有本地工具和 MCP 外部工具。
- **工具组装**：`LeadAgent.build_tools()` 把核心工具与子代理工具打包给 `create_agent()`。
- **图编译**：`HarnessGraphFactory.build()` 用 `create_agent()` 生成内部 Agent 图，外层再包一层 `StateGraph`。
- **执行与拦截**：`HarnessService.execute()` 通过 `astream_events` 监听 `on_tool_start/on_tool_end`；中间件通过 `awrap_tool_call` 钩子拦截实际工具调用。
- **子代理**：`task` 工具本身也是一个普通工具，内部调用 `SubagentManager.execute()` 驱动另一个 `create_agent()` 子图。

---

## 2. 工具注册中心 `tools/registry.py`

| 类/函数 | 作用 |
|---|---|
| `ToolRegistry.__init__` | 初始化本地工具字典 `_tools`、MCP 工具字典 `_mcp_tools`、分类映射 `_categories`，以及沙盒/工作区/线程上下文。 |
| `ToolRegistry.register(tool, category)` | 将单个 `BaseTool` 按名称注册到 `_tools`，并记录分类。 |
| `ToolRegistry.register_from_function(func, ...)` | 用 `@tool` 装饰器把普通函数包装成工具后注册。 |
| `ToolRegistry.get_tool(name)` | 按名称查找工具（先本地，后 MCP），找不到抛 `KeyError`。 |
| `ToolRegistry.has_tool(name)` | 判断工具是否已注册。 |
| `ToolRegistry.get_core_tools()` | 返回所有本地注册的工具列表。 |
| `ToolRegistry.get_tools_by_category(cat)` | 按分类返回工具。 |
| `ToolRegistry.load_mcp_tools(path)` | 异步从 MCP 配置文件加载外部工具，存到 `_mcp_tools`。 |
| `ToolRegistry.setup_tool_groups()` | 返回预定义的工具分组（search/code/files/data/abacus/mcp），用于前端展示。 |
| `ToolRegistry.bind_context(sandbox, workspace, thread_id)` | 把运行时上下文绑定到注册表，重新注册 code/file 工具（使沙盒/工作区生效）。 |
| `ToolRegistry.initialize_defaults(config)` | 注册所有内置工具集：core、search、code、files、abacus。 |

---

## 3. 内置工具工厂

### 3.1 `core.py`

| 函数 | 作用 |
|---|---|
| `create_ask_clarification_tool()` | 创建 `ask_clarification` 工具，向用户发起确认/澄清问题。 |
| `create_data_query_tool()` | 创建 `data_query` 工具，模拟查询数据源。 |
| `create_chart_generate_tool()` | 创建 `chart_generate` 工具，生成图表描述。 |
| `create_csv_process_tool()` | 创建 `csv_process` 工具，模拟 CSV 处理。 |
| `create_template_render_tool()` | 创建 `template_render` 工具，模拟模板渲染。 |
| `create_code_check_tool()` | 创建 `code_check` 工具，简单检查代码中是否包含 `TODO` 等。 |
| `build_core_tools()` | 汇总核心工具，包含 search、code、files、abacus 的部分工具实例。 |

### 3.2 `search.py`

| 函数 | 作用 |
|---|---|
| `_simulate_web_results(query, num)` | 无 API Key 时返回模拟搜索结果。 |
| `create_web_search_tool()` | 创建 `web_search` 工具：优先使用 Tavily API，回退到模拟结果。 |
| `create_arxiv_search_tool()` | 创建 `arxiv_search` 工具：调用 arXiv API 返回论文信息。 |
| `create_paper_search_tool()` | 创建 `paper_search` 工具，实际是 `arxiv_search` 的别名。 |
| `build_search_tools()` | 返回全部搜索工具。 |

### 3.3 `code.py`

| 函数/类 | 作用 |
|---|---|
| `set_tool_context(thread_id, sandbox, workspace)` | 通过 `ContextVar` 设置代码工具的运行时上下文，避免把沙盒对象暴露在 LLM 看到的 schema 中。 |
| `_current_ctx()` | 读取当前上下文。 |
| `_sandbox_run(...)` | 辅助函数：在异步工具里准备沙盒（当前版本实际逻辑已内联到各工具）。 |
| `create_python_tool(sandbox)` | 创建 `python` 工具：如果配置了沙盒，先把代码写入临时 `.py` 文件再执行；否则返回 mock 输出。 |
| `create_bash_tool(sandbox)` | 创建 `bash` 工具：在沙盒中执行 shell 命令。 |
| `create_execute_code_tool(sandbox)` | 创建 `execute_code` 工具：根据语言分发到 python 工具或沙盒通用执行。 |
| `CodeTools` 类 | 封装沙盒依赖，提供 `python_tool/bash_tool/execute_code_tool/get_tools`。 |
| `build_code_tools(sandbox)` | 返回代码工具列表。 |

### 3.4 `files.py`

| 函数 | 作用 |
|---|---|
| `set_file_tool_context(workspace)` | 通过 `ContextVar` 设置文件工具的工作区。 |
| `_resolve_path(path, workspace)` | 解析路径并做路径遍历防护，确保不会访问工作区之外。 |
| `create_file_read_tool(workspace)` | 创建 `file_read` 工具，读取工作区内文件。 |
| `create_file_write_tool(workspace)` | 创建 `file_write` 工具，写入文件并自动创建父目录。 |
| `create_list_files_tool(workspace)` | 创建 `list_files` 工具，列出工作区目录内容。 |
| `build_file_tools(workspace)` | 返回文件工具列表。 |

### 3.5 `abacus.py`

| 函数 | 作用 |
|---|---|
| `create_generate_abacus_input_tool()` | 创建 `generate_abacus_input` 工具，生成 Abacus 的 INPUT/STRU 输入文件内容。 |
| `create_submit_abacus_job_tool()` | 创建 `submit_abacus_job` 工具，模拟提交 Abacus 计算任务（需用户确认）。 |
| `build_abacus_tools()` | 返回 Abacus 工具列表。 |

### 3.6 `mcp_adapter.py`

| 函数 | 作用 |
|---|---|
| `_mcp_sessions` | 全局列表，保持 MCP 会话存活。 |
| `load_mcp_tools_from_config(config_path)` | 读取 `mcpServers` 格式的 JSON 配置，为每个启用且加载成功的 MCP 服务器加载工具，并缓存会话。 |

---

## 4. Lead Agent 工具组装

文件：`agents/lead_agent.py`

| 函数 | 作用 |
|---|---|
| `_create_subagent_tool()` | 创建 `create_subagent` 工具，让 LLM 动态创建子代理。 |
| `_task_tool()` | 创建 `task` 工具，让 LLM 把任务委派给已创建的子代理。 |
| `_ask_clarification_tool()` | 创建 Lead Agent 专用的 `ask_clarification` 工具。 |
| `get_system_prompt()` | 构建 DeerFlow 风格的系统提示，包含子代理并发限制、澄清优先等规则。 |
| `build_tools()` | **关键入口**：返回 Lead Agent 使用的全部工具 `[create_subagent, task, ask_clarification] + registry.get_core_tools()`。 |

---

## 5. 图编译与执行

### 5.1 `graph_factory.py`

| 函数/类 | 作用 |
|---|---|
| `HarnessGraphFactory.__init__` | 接收 LLM、工具列表、中间件、系统提示、checkpointer。 |
| `HarnessGraphFactory.build()` | 用 `create_agent(model=llm, tools=tools, system_prompt=..., middleware=middlewares, state_schema=HarnessState)` 生成内部 Agent 图，再用 `StateGraph` 包一层 `START → agent → END`，最后编译。 |
| `build_harness_graph(...)` | 便捷函数，封装 `HarnessGraphFactory`。 |

### 5.2 `main.py`

| 函数 | 作用 |
|---|---|
| `HarnessService.__init__` | 创建 `ToolRegistry` 实例。 |
| `HarnessService.initialize()` | 生命周期：初始化 LLM → `tool_registry.initialize_defaults()` 注册内置工具 → 加载 MCP 工具 → 创建 `SubagentManager` → 创建 `LeadAgent` → `build_harness_graph()` 编译图。 |
| `HarnessService._register_middlewares()` | 按顺序注册 14 个中间件，其中包括 `ToolErrorHandlingMiddleware` 和 `SandboxMiddleware`。 |
| `HarnessService.execute()` | 运行图并通过 `astream_events` 实时流式输出：把 `on_tool_start` 映射为 `tool_call`/`subagent_start`，把 `on_tool_end` 映射为 `tool_result`/`subagent_end`。 |

---

## 6. 工具调用中间件

### 6.1 `middleware/base.py` — 中间件基类

| 函数 | 作用 |
|---|---|
| `abefore_agent` | 每轮 Agent 执行前调用。 |
| `aafter_agent` | 每轮 Agent 执行后调用。 |
| `abefore_model/aafter_model` | 每次 LLM 调用前后调用。 |
| `awrap_tool_call(request, handler)` | **包裹单次工具调用**：调用 `await handler(request)` 才会真正执行工具。 |
| `awrap_model_call(request, handler)` | 包裹 LLM 调用。 |

### 6.2 `middleware/tool_error.py` — 工具错误重试

| 函数 | 作用 |
|---|---|
| `ToolErrorHandlingMiddleware.__init__` | 读取配置 `max_retries`，默认 3 次。 |
| `ToolErrorHandlingMiddleware.awrap_tool_call` | 拦截工具调用，失败时最多重试 `max_retries` 次；全部失败后返回 `status="error"` 的 `ToolMessage`，避免异常直接抛到外层。 |

### 6.3 `middleware/sandbox.py` — 沙盒上下文注入

| 函数 | 作用 |
|---|---|
| `SandboxMiddleware.__init__` | 配置沙盒镜像、内存限制、是否懒加载。 |
| `SandboxMiddleware.abefore_agent` | 非懒加载时立即创建 Docker 容器；懒加载时把 `thread_id/workspace` 存入 `ContextVar`。 |
| `SandboxMiddleware.awrap_tool_call` | 当工具名为 `bash/python/execute_code` 且是懒加载模式时，首次调用会创建沙盒，并通过 `set_tool_context()` 把沙盒注入到 code 工具的 `ContextVar`。 |
| `SandboxMiddleware._acquire_sandbox()` | 实际创建 Docker 容器并返回状态更新。 |

---

## 7. 子代理工具调用

文件：`agents/subagent_manager.py`、`agents/subagent.py`

| 函数/类 | 作用 |
|---|---|
| `SubagentManager.__init__` | 持有 `ToolRegistry`、中间件列表、并发信号量。 |
| `SubagentManager.create(config, parent_model)` | 创建 `SubAgent`；若 `config.tools` 为 None 则继承全部核心工具。 |
| `SubagentManager.get/list/delete` | 子代理的 CRUD。 |
| `SubagentManager.execute(name, instruction, context)` | **task 工具的底层实现**：通过信号量控制并发，调用对应 `SubAgent.execute()`。 |
| `SubAgent.__init__` | 用过滤后的工具 + 系统提示 + 中间件调用 `create_agent()` 编译子代理图。 |
| `SubAgent.execute(...)` | 构造消息状态并 `ainvoke` 子图，返回 `SubAgentResult`。 |

---

## 8. 完整数据流

```
用户请求
  │
  ▼
HarnessService.execute()
  │
  ▼
build_harness_graph()
  └── create_agent(tools=lead_agent.build_tools(), middleware=middlewares)
  │
  ▼
LLM 决定调用某个工具（如 python / task / web_search）
  │
  ▼
awrap_tool_call 中间件链
  ├── SandboxMiddleware：注入沙盒上下文
  └── ToolErrorHandlingMiddleware：失败重试
  │
  ▼
实际工具函数执行
  │
  ▼
on_tool_start / on_tool_end 事件
  └── HarnessService.execute() 捕获并转成 SSE 推给前端
```

如果工具是 `task`，则不会直接执行本地函数，而是进入 `SubagentManager.execute()` 驱动另一个独立的 `create_agent()` 子图，实现多智能体并行。
